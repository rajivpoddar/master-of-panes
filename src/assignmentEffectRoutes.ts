import type { Hono } from "hono";
import { z } from "zod";
import { createHash } from "node:crypto";

import type { AssignmentTupleInput, MoPDatabase } from "./db.js";
import type { SlotState } from "./types.js";
import {
  ASSIGNMENT_WORK_KINDS,
  normalizeAssignmentTuple,
  normalizeBranchIdentity,
  normalizeRepositoryId,
} from "./db.js";
import {
  NO_ISSUE_PROJECTION,
  type IssueOwnershipProjection,
  type IssueProjectionOutcome,
} from "./issueProjection.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

const assignmentEffectSlotParamSchema = z.coerce
  .number()
  .int()
  .min(1)
  .max(DEFAULT_DEV_SLOT_COUNT);

/**
 * The atomic assignment boundary.
 *
 * Exactly one durable operation performs the whole new-issue/repro/rework
 * assignment: fresh read, (new_issue only) session clear, the ownership
 * transition, the literal task delivery, and the dual readback.  The durable
 * intent row is the delivery-pending marker, mirroring the Family-2 release
 * effect shape:
 *
 *   mint durable effect id -> commit ownership bound to the id -> deliver -> finalize
 *
 * Ordering is deliberate: ownership is committed BEFORE delivery, because a
 * pane acting on a task it does not own is strictly worse than a recorded but
 * undelivered slot.  A delivery failure therefore leaves a NAMED recoverable
 * state (occupied + task present + delivery pending) and never rolls the
 * ownership back implicitly.
 */
export const ASSIGNMENT_SELECTION_CLASSES = ["new_issue", "repro", "rework"] as const;

export type AssignmentSelectionClass = (typeof ASSIGNMENT_SELECTION_CLASSES)[number];

export interface AssignmentEffectClearResult {
  ok: boolean;
  reason: string;
  detail?: string;
}

export interface AssignmentEffectDeliveryResult {
  verified: boolean;
  receipt: Record<string, unknown>;
  reason?: string;
}

export interface AssignmentEffectDependencies {
  db: MoPDatabase;
  /** Session clear through the existing clear path (new_issue class only). */
  clearSlot: (slot: number) => Promise<AssignmentEffectClearResult>;
  /** Literal task delivery through the existing message-slot/file-send path. */
  deliverTaskFile: (
    slot: number,
    filePath: string,
  ) => Promise<AssignmentEffectDeliveryResult>;
  /**
  * Best-effort interrupt of any live turn in the pane (Ctrl-C through the
  * existing relay path). The outcome is audited and never refuses.
   */
  interruptTurn: (slot: number) => Promise<{ ok: boolean; reason: string }>;
  /**
   * Read-only observation of the pane worktree dirty/clean state for the
  * displacement audit row. Never mutates; null clean means unobserved.
   */
  observeWorktree: (slot: number) => Promise<{ clean: boolean | null; detail: string }>;
  /**
   * Issue-side label projection (GitHub). Called after the ownership commit
   * with the same shape as the deprecated assign route's projectOwnership:
   * onReleased for a displaced prior lane, then onAssigned for the new
   * lane. Labels are a projection, so a failure is recorded and never
   * refuses. Optional so tests stay hermetic.
   */
  issueProjection?: IssueOwnershipProjection;
}

/**
 * Mirror of the deprecated assign route's projectOwnership: project one
 * lane's labels, translating a throw into a typed failed outcome so the
 * durable assignment is always reported truthfully.
 */
async function projectIssue(
  projection: IssueOwnershipProjection,
  mode: "assigned" | "released",
  issue: number | null | undefined,
  slot: number,
  repositoryId: string | number | null | undefined,
): Promise<IssueProjectionOutcome | null> {
  const target = Number(issue);
  if (!Number.isInteger(target) || target <= 0) {
    return null;
  }
  const repositoryKey = repositoryId === null || repositoryId === undefined
    ? null
    : String(repositoryId);
  try {
    return mode === "assigned"
      ? await projection.onAssigned(target, slot, repositoryKey)
      : await projection.onReleased(target, slot, repositoryKey);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return {
      status: "failed",
      reason: `issue_projection_unexpected:${detail.split("\n")[0].slice(0, 200)}`,
      repository: null,
      issue: target,
      slot,
      added_labels: [],
      removed_labels: [],
      verified: false,
    };
  }
}

function isIssueProjectionOutcome(value: unknown): value is IssueProjectionOutcome {
  return (
    isRecord(value)
    && typeof value.status === "string"
    && ["projected", "unchanged", "skipped", "failed"].includes(value.status)
    && typeof value.issue === "number"
    && typeof value.slot === "number"
  );
}

interface ParsedDeliveredReceipt {
  /** Stored delivery fields with the projection keys removed. */
  delivery: Record<string, unknown>;
  issue_projection: IssueProjectionOutcome | null;
  release_projection: IssueProjectionOutcome | null;
  /** False for rows finalized before labels existed: release is not retried. */
  has_release_projection: boolean;
}

function parseDeliveredReceipt(raw: unknown): ParsedDeliveredReceipt {
  const empty: ParsedDeliveredReceipt = {
    delivery: {},
    issue_projection: null,
    release_projection: null,
    has_release_projection: false,
  };
  if (typeof raw !== "string") return empty;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return empty;
  }
  if (!isRecord(parsed)) return empty;
  const { issue_projection: rawAssign, release_projection: rawRelease, ...delivery } = parsed;
  return {
    delivery,
    issue_projection: isIssueProjectionOutcome(rawAssign) ? rawAssign : null,
    release_projection: isIssueProjectionOutcome(rawRelease) ? rawRelease : null,
    has_release_projection: Object.hasOwn(parsed, "release_projection"),
  };
}

function mergeProjectionReceipt(
  delivery: Record<string, unknown>,
  assign: IssueProjectionOutcome | null,
  release: IssueProjectionOutcome | null,
): string {
  return JSON.stringify({ ...delivery, issue_projection: assign, release_projection: release });
}

/**
 * Parse the displaced prior lane recorded on the committed intent. Returns
 * null for free-slot assigns, legacy rows, and malformed payloads (never
 * throw on the projection path).
 */
function parseDisplacedTuple(raw: string | null | undefined): { issue: number; repository_id: string | null } | null {
  if (typeof raw !== "string") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(parsed)) return null;
  const { issue, repository_id: repositoryId } = parsed as { issue?: unknown; repository_id?: unknown };
  if (!Number.isInteger(issue) || (issue as number) <= 0) return null;
  if (repositoryId !== null && repositoryId !== undefined && typeof repositoryId !== "string") return null;
  return { issue: issue as number, repository_id: (repositoryId as string | null | undefined) ?? null };
}

export interface AssignmentEffectRequestBody {
  effect_id: string;
  selection_class: AssignmentSelectionClass;
  expected_epoch: number;
  desired_tuple: AssignmentTupleInput;
  task: string;
  task_file: string;
}

/** Canonical digest for one immutable assignment-effect binding. */
export function computeAssignmentEffectDigest(request: AssignmentEffectRequestBody): string {
  const tuple = normalizeAssignmentTuple(request.desired_tuple);
  if (!tuple) throw new Error("assignment effect tuple is invalid");
  return createHash("sha256")
    .update(
      JSON.stringify({
        effect_id: request.effect_id,
        selection_class: request.selection_class,
        expected_epoch: request.expected_epoch,
        repository_id: tuple.repository_id,
        issue: tuple.issue,
        pr: tuple.pr,
        branch: tuple.branch,
        head_sha: tuple.head_sha,
        work_kind: tuple.work_kind,
        handoff_id: tuple.handoff_id,
        claimed_at: tuple.claimed_at,
        task_digest: createHash("sha256").update(request.task).digest("hex"),
        task_file: request.task_file,
      }),
    )
    .digest("hex");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeSelectionClass(value: unknown): AssignmentSelectionClass | null {
  return typeof value === "string" && (ASSIGNMENT_SELECTION_CLASSES as readonly string[]).includes(value)
    ? (value as AssignmentSelectionClass)
    : null;
}

function slotStateSummary(slot: SlotState | null | undefined): string {
  if (!slot) return "slot_missing";
  return [
    `occupied=${slot.occupied}`,
    `issue=${slot.issue ?? "null"}`,
    `pr=${slot.pr ?? "null"}`,
    `assignment_epoch=${slot.assignment_epoch}`,
    `task_present=${typeof slot.task === "string" && slot.task.trim() !== ""}`,
  ].join(" ");
}

interface AssignmentEffectRefusalBody {
  status: "refused";
  step_failed: "clean" | "ownership" | "delivery" | "readback";
  reason: string;
  slot_state_after: string;
  sanctioned_path: "mop-assign-slot";
  /** Additive diagnosability: which tuple predicate failed. Never present on success. */
  failed_field?: string;
}

function refusal(
  stepFailed: AssignmentEffectRefusalBody["step_failed"],
  reason: string,
  slotStateAfter: string,
  failedField?: string | null,
): AssignmentEffectRefusalBody {
  return {
    status: "refused",
    step_failed: stepFailed,
    reason,
    slot_state_after: slotStateAfter,
    sanctioned_path: "mop-assign-slot",
    // Additive diagnosability only: names which predicate of the tuple validation failed, so a
    // caller does not have to permute class flags to discover it. No behaviour change.
    ...(failedField ? { failed_field: failedField } : {}),
  };
}

/**
 * Decide whether the current durable row already carries the desired binding
 * identity, i.e. the ownership commit for this effect has already happened and
 * a retry must NOT bump the epoch or rewrite the task text a second time.
 *
 * `claimed_at` is deliberately excluded from this comparison. MoP owns that
 * timestamp -- `db.assignSlot` stamps it at commit time rather than persisting
 * the caller's value -- so comparing it would make every resume path look like
 * ownership drift and defeat the idempotent-retry contract. The binding
 * identity plus the assignment epoch are the real generation fence; the resume
 * path additionally pins `assignment_epoch` to the intent's `committed_epoch`.
 */
function desiredTupleAlreadyCommitted(
  current: SlotState,
  desired: AssignmentTupleInput,
): boolean {
  if (!current.occupied) return false;
  const currentNormalized = normalizeAssignmentTuple({
    repository_id: current.repository_id,
    issue: current.issue,
    pr: current.pr,
    branch: current.branch,
    head_sha: current.head_sha,
    work_kind: current.work_kind,
    handoff_id: current.handoff_id,
    claimed_at: current.claimed_at,
  });
  const desiredNormalized = normalizeAssignmentTuple(desired);
  if (!currentNormalized || !desiredNormalized) return false;
  return currentNormalized.repository_id === desiredNormalized.repository_id
    && currentNormalized.issue === desiredNormalized.issue
    && currentNormalized.pr === desiredNormalized.pr
    && currentNormalized.branch_ref === desiredNormalized.branch_ref
    && currentNormalized.head_sha === desiredNormalized.head_sha
    && currentNormalized.work_kind === desiredNormalized.work_kind
    && currentNormalized.handoff_id === desiredNormalized.handoff_id;
}

export function registerAssignmentEffectRoutes(
  app: Hono,
  dependencies: AssignmentEffectDependencies,
): void {
  const { db } = dependencies;

  app.get("/slots/:slotNum/assignment-effect-receipt", (c) => {
    const slotParse = assignmentEffectSlotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ success: false, code: "invalid_request" }, 400);
    }
    const effectId = c.req.query("effect_id");
    if (!effectId) {
      return c.json({ success: false, code: "invalid_request" }, 400);
    }
    const intent = db.getAssignmentEffectIntent(effectId);
    if (!intent || intent.slot !== slotParse.data) {
      return c.json({ success: false, code: "assignment_effect_receipt_not_found" }, 404);
    }
    return c.json({ success: true, ...intent });
  });

  app.post("/slots/:slotNum/assign-effect", async (c) => {
    const slotParse = assignmentEffectSlotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ error: "Invalid slot number" }, 400);
    }
    const slotNum = slotParse.data;

    let raw: unknown;
    try {
      raw = await c.req.json();
    } catch {
      return c.json(refusal("ownership", "invalid_request_body", slotStateSummary(db.getSlot(slotNum))), 400);
    }
    if (!isRecord(raw)) {
      return c.json(refusal("ownership", "invalid_request_body", slotStateSummary(db.getSlot(slotNum))), 400);
    }

    const selectionClass = normalizeSelectionClass(raw.selection_class);
    // Thin PM surface (Rajiv directive 2026-09-23 15:57): only the lane
    // binding matters. Epoch, full tuple detail, and task text are advisory;
    // the live row is authoritative and nothing here is a precondition retry.
    if (
      typeof raw.effect_id !== "string"
      || raw.effect_id.trim() === ""
      || selectionClass === null
      || (raw.expected_epoch !== undefined && !Number.isInteger(raw.expected_epoch))
      || (raw.task !== undefined && typeof raw.task !== "string")
      || (raw.task_file !== undefined && typeof raw.task_file !== "string")
    ) {
      return c.json(
      refusal("ownership", "invalid_assignment_effect_request", slotStateSummary(db.getSlot(slotNum))),
        400,
      );
    }

    const desiredTuple: AssignmentTupleInput = {
      repository_id: raw.repository_id as string | number | null,
      issue: raw.issue as number | null,
      pr: (raw.pr ?? null) as number | null,
      branch: (raw.branch ?? null) as string | null,
      head_sha: (raw.head_sha ?? null) as string | null,
      work_kind: (raw.work_kind ?? null) as string | null,
      handoff_id: (raw.handoff_id ?? null) as string | null,
      claimed_at: (typeof raw.claimed_at === "string" && raw.claimed_at.trim() === ""
        ? null
        : (raw.claimed_at ?? null)) as string | null,
    };
    const normalizedTuple = normalizeAssignmentTuple(desiredTuple);
    const branchIdentity = normalizeBranchIdentity(desiredTuple.branch);
    // Name the failing predicate for diagnosability. The lane identity is
    // the issue; every other tuple field is optional detail validated with
    // the same predicates as normalizeAssignmentTuple (work_kind membership
    // via the imported ASSIGNMENT_WORK_KINDS set, never duplicated).
    const workKindRaw = desiredTuple.work_kind;
    const handoffRaw = desiredTuple.handoff_id;
    const workKindValid = workKindRaw === null
      || (typeof workKindRaw === "string" && ASSIGNMENT_WORK_KINDS.has(workKindRaw.trim()));
    const handoffValid = handoffRaw === null
      || (typeof handoffRaw === "string" && handoffRaw.trim() !== "");
    const failedField =
      normalizeRepositoryId(desiredTuple.repository_id) === null ? "repository_id"
      : !Number.isInteger(desiredTuple.issue) || (desiredTuple.issue as number) <= 0 ? "issue"
      : (desiredTuple.pr !== null && (!Number.isInteger(desiredTuple.pr) || (desiredTuple.pr as number) <= 0)) ? "pr"
      : (desiredTuple.branch !== null && !branchIdentity) ? "branch"
      : (desiredTuple.head_sha !== null && (typeof desiredTuple.head_sha !== "string" || !/^[0-9a-f]{40}$/i.test(desiredTuple.head_sha))) ? "head_sha"
      : (!workKindValid || !handoffValid || (workKindRaw === null) !== (handoffRaw === null)) ? "work_kind"
      : (desiredTuple.pr !== null && (branchIdentity?.branchRef == null || desiredTuple.head_sha === null)) ? "branch"
      : null;
    if (
      !normalizedTuple
      || normalizeRepositoryId(desiredTuple.repository_id) === null
      || !Number.isInteger(desiredTuple.issue)
      || (desiredTuple.issue as number) <= 0
    ) {
      return c.json(
        refusal("ownership", "invalid_assignment_tuple", slotStateSummary(db.getSlot(slotNum)), failedField),
        400,
      );
    }

    const request: AssignmentEffectRequestBody = {
      effect_id: raw.effect_id.trim(),
      selection_class: selectionClass,
      expected_epoch: Number.isInteger(raw.expected_epoch)
        ? (raw.expected_epoch as number)
        : (db.getSlot(slotNum)?.assignment_epoch ?? 0),
      desired_tuple: desiredTuple,
      task: typeof raw.task === "string" ? raw.task : "",
      task_file: typeof raw.task_file === "string" ? raw.task_file.trim() : "",
    };
    const taskDigest = createHash("sha256").update(request.task).digest("hex");
    const requestDigest = computeAssignmentEffectDigest(request);

    // Idempotent replay / binding fence.
    const existing = db.getAssignmentEffectIntent(request.effect_id);
    if (existing) {
      // A completed assignment may be retried with its now-current epoch.
      // Re-hashing at the stored pre-commit epoch proves every other binding
      // field, including task_file, is unchanged.
      const committedEpochReplay =
        existing.state === "delivered" &&
        existing.committed_epoch !== null &&
        request.expected_epoch === existing.committed_epoch &&
        computeAssignmentEffectDigest({
          ...request,
          expected_epoch: existing.before_epoch,
        }) === existing.request_digest;
      if (
        existing.slot !== slotNum ||
        (existing.request_digest !== requestDigest && !committedEpochReplay)
      ) {
        return c.json(
          refusal("ownership", "effect_binding_conflict", slotStateSummary(db.getSlot(slotNum))),
          409,
        );
      }
      if (existing.state === "delivered") {
        const live = db.getSlot(slotNum);
        if (
          existing.committed_epoch === null
          || !live
          || live.assignment_epoch !== existing.committed_epoch
          || !desiredTupleAlreadyCommitted(live, existing.desired_tuple)
        ) {
          return c.json(
            refusal("ownership", "assignment_superseded", slotStateSummary(live)),
            409,
          );
        }
        // Delivered replay: clear, commit, and delivery all already happened,
        // so none of them repeats. Only a MISSING or FAILED label projection
        // is retried now; a recorded successful projection is reused verbatim
        // and never re-projected or clobbered.
        const projector = dependencies.issueProjection ?? NO_ISSUE_PROJECTION;
        const stored = parseDeliveredReceipt(existing.delivery_receipt);
        let replayAssign = stored.issue_projection ?? null;
        // Absent key = row finalized before labels existed: the claim still
        // needs its projection. Present-null = no displacement, skip release.
        let replayRelease = stored.has_release_projection ? stored.release_projection : undefined;
        if (!replayAssign || replayAssign.status === "failed") {
          replayAssign = await projectIssue(
            projector, "assigned", desiredTuple.issue, slotNum, desiredTuple.repository_id,
          );
        }
        if (replayRelease !== undefined && replayRelease !== null && replayRelease.status === "failed") {
          replayRelease = await projectIssue(
            projector, "released", replayRelease.issue, slotNum, replayRelease.repository,
          );
        }
        if (replayRelease === undefined) replayRelease = null;
        const mergedReplayReceipt = mergeProjectionReceipt(stored.delivery, replayAssign, replayRelease);
        if (
          mergedReplayReceipt !== null
          && (replayAssign?.status !== stored.issue_projection?.status
            || (replayRelease?.status ?? null) !== (stored.release_projection?.status ?? null))
        ) {
          db.markAssignmentEffectDelivered(request.effect_id, mergedReplayReceipt);
        }
        const delivered = db.getSlot(slotNum);
        return c.json({
          status: "assigned",
          slot: slotNum,
          assignment_epoch: existing.committed_epoch,
          ownership_receipt: {
            ...existing.desired_tuple,
            assignment_epoch: existing.committed_epoch,
            idempotent: true,
          },
          delivery_receipt: { verified: true, detail: mergedReplayReceipt ?? existing.delivery_receipt, idempotent: true },
          issue_projection: replayAssign,
          ...(replayRelease ? { release_projection: replayRelease } : {}),
          idempotent: true,
          slot_state_after: slotStateSummary(delivered),
        });
      }
    }

    const before = db.getSlot(slotNum);
    if (!before) {
      return c.json(refusal("ownership", "slot_not_found", slotStateSummary(null)), 404);
    }

    // Mint the durable intent BEFORE any ownership mutation so a crash at any
    // later boundary leaves a reconcilable delivery-pending row.
    const minted = db.mintAssignmentEffectIntent({
      effect_id: request.effect_id,
      request_digest: requestDigest,
      slot: slotNum,
      selection_class: request.selection_class,
      before_epoch: before.assignment_epoch,
      desired_tuple: desiredTuple,
      task_digest: taskDigest,
    });
    if (!minted.ok) {
      return c.json(
        refusal("ownership", minted.reason ?? "effect_mint_failed", slotStateSummary(db.getSlot(slotNum))),
        409,
      );
    }
    const intent = minted.intent;
    if (!intent) {
      return c.json(refusal("ownership", "effect_mint_missing_intent", slotStateSummary(db.getSlot(slotNum))), 500);
    }

    const ownershipPending = intent.state === "planned";

    // Displacement audit context. Every step below is attempted, recorded,
    // and never refuses: an occupied target is implicitly released and
    // reassigned, and the session clear is never gated on "free".
    const displacement: {
      clear: { ok: boolean; reason: string } | null;
      interrupt: { ok: boolean; reason: string } | null;
      worktree: { clean: boolean | null; detail: string } | null;
      prior: Record<string, unknown> | null;
      conflicting_owner_slots: number[];
    } = {
      clear: null,
      interrupt: null,
      worktree: null,
      prior: null,
      conflicting_owner_slots: [],
    };

    // Pre-decision duplicate gate (fix 3): a lane already bound to another
    // occupied slot refuses BEFORE any clear/interrupt/observe side effect.
    // The atomic commit path re-checks inside its own transaction, so this
    // ordering gate can never admit what the commit would refuse.
    if (ownershipPending) {
      const pre = db.getSlot(slotNum);
      if (!pre) {
        return c.json(refusal("ownership", "slot_not_found", slotStateSummary(null)), 404);
      }
      if (!desiredTupleAlreadyCommitted(pre, desiredTuple)) {
        const holders = db.duplicateHolders(slotNum, desiredTuple);
        if (holders.length > 0) {
          return c.json(
            {
              ...refusal("ownership", "duplicate_assignment", slotStateSummary(pre)),
              owner_slots: holders,
            },
            409,
          );
        }
      }
    }

    if (ownershipPending && request.selection_class === "new_issue") {
      const cleared = await dependencies.clearSlot(slotNum);
      displacement.clear = {
        ok: cleared.ok,
        reason: cleared.ok ? cleared.reason : `recorded:${cleared.reason}`,
      };
    }

    if (ownershipPending) {
      // Interrupt any live turn in the pane before overwriting ownership.
      // Best effort: the outcome is audited and never refuses.
      const live = db.getSlot(slotNum);
      if (live && (live.active_turn_id !== null || live.active_turn_state !== "inactive")) {
        try {
          const interrupted = await dependencies.interruptTurn(slotNum);
          displacement.interrupt = { ok: interrupted.ok, reason: interrupted.reason };
        } catch (error) {
          displacement.interrupt = {
            ok: false,
            reason: `interrupt_threw:${error instanceof Error ? error.message.split("\n")[0].slice(0, 160) : String(error).slice(0, 160)}`,
          };
        }
      } else {
        displacement.interrupt = { ok: true, reason: "no_live_turn" };
      }
      // Observe (never reset) the pane worktree for the audit row.
      try {
        displacement.worktree = await dependencies.observeWorktree(slotNum);
      } catch (error) {
        displacement.worktree = {
          clean: null,
          detail: `observe_threw:${error instanceof Error ? error.message.split("\n")[0].slice(0, 160) : String(error).slice(0, 160)}`,
        };
      }
    }

    let committedEpoch = intent.committed_epoch;
    // Label projection outcomes for the "assigned" response. Labels run only
    // after ownership AND delivery succeed (see below): never before the
    // slot has both the record and the task. Resume and fresh paths share
    // the projection below; only the delivered replay skips straight to it.
    let releaseProjection: IssueProjectionOutcome | null = null;
    let assignProjection: IssueProjectionOutcome | null = null;
    if (ownershipPending) {
      const current = db.getSlot(slotNum);
      if (!current) {
        return c.json(refusal("ownership", "slot_not_found", slotStateSummary(null)), 404);
      }
      if (desiredTupleAlreadyCommitted(current, desiredTuple)) {
        // The ownership commit already happened for this binding (e.g. a crash
        // between the commit and the intent update). Never bump a second epoch.
        committedEpoch = current.assignment_epoch;
      } else {
        // Simple path: one atomic overwrite. Occupied targets are implicitly
        // released; the turn pointer is cleared in the same write; the
        // worktree is untouched. The ONLY refusal is a lane already bound to
        // another occupied slot.
        if (current.occupied) {
          // Persist the displaced prior lane on the intent BEFORE the
          // overwrite, so every later resume can unwind its labels even if
          // this invocation crashes before delivery. Re-recording the same
          // JSON is idempotent and touches no ownership state.
          db.recordAssignmentEffectDisplaced(
            request.effect_id,
            current.issue !== null && Number.isInteger(current.issue) && current.issue > 0
              ? { issue: current.issue, repository_id: current.repository_id }
              : null,
          );
        }
        const assigned = db.assignSlotSimple(slotNum, desiredTuple, request.task);
        if (!assigned.ok) {
          return c.json(
            {
              ...refusal("ownership", assigned.reason ?? "ownership_refused", slotStateSummary(db.getSlot(slotNum))),
              ...(assigned.reason === "duplicate_assignment" ? { owner_slots: assigned.owner_slots ?? [] } : {}),
            },
            409,
          );
        }
        committedEpoch = assigned.assignment_epoch;
        const prior = assigned.predecessor ?? null;
        displacement.prior = prior ? {
          issue: prior.issue,
          pr: prior.pr,
          repository_id: current.repository_id,
          task_present: typeof prior.task === "string" && prior.task.trim() !== "",
          assignment_epoch: current.assignment_epoch,
          active_turn_id: prior.active_turn_id,
          active_turn_state: prior.active_turn_state,
        } : null;
        displacement.conflicting_owner_slots = assigned.owner_slots ?? [];
      }
      db.markAssignmentEffectCommitted(request.effect_id, committedEpoch ?? 0);
      db.logEvent(slotNum, "assignment_effect_ownership_bound", null, null, {
        effect_id: request.effect_id,
        selection_class: request.selection_class,
        assignment_epoch: committedEpoch,
        request_digest: requestDigest,
        delivery_state: request.task.trim() !== "" && request.task_file !== "" ? "pending" : "not_required",
      });
      if (displacement.prior) {
        // One audit row for the displaced in-flight work: prior owner,
        // issue, PR, epoch, turn id, clear/interrupt outcomes, and the
        // observed (never reset) worktree state.
        db.logEvent(slotNum, "assignment_displaced", null, null, {
          effect_id: request.effect_id,
          prior: displacement.prior,
          conflicting_owner_slots: displacement.conflicting_owner_slots,
          clear: displacement.clear,
          interrupt: displacement.interrupt,
          worktree: displacement.worktree,
          worktree_reset: false,
          assignment_epoch: committedEpoch,
        });
      }
    } else {
      // Resume path: the ownership commit already happened for this binding, so
      // revalidate the live row BEFORE re-delivering. If the slot was released,
      // re-tasked, or the epoch moved underneath us, refuse with zero delivery.
      // Never rebind, silently repair, or roll the ownership back here.
      const current = db.getSlot(slotNum);
      const epochMatches = typeof committedEpoch !== "number"
        || current?.assignment_epoch === committedEpoch;
      if (
        !current
        || current.occupied !== true
        || !desiredTupleAlreadyCommitted(current, desiredTuple)
        || !epochMatches
      ) {
        return c.json(
          refusal("ownership", "assignment_ownership_drift_on_resume", slotStateSummary(current)),
          409,
        );
      }
    }

    // Delivery is required only when the caller gave the slot something to
    // do: with no task text there is nothing to deliver, so ownership alone
    // is committed and read back. Otherwise a failed delivery is still a
    // NAMED recoverable state, never a silent success or implicit rollback.
    const needsDelivery = request.task.trim() !== "" && request.task_file !== "";
    const delivery = needsDelivery
      ? await dependencies.deliverTaskFile(slotNum, request.task_file)
      : { verified: true, receipt: { slot: slotNum, skipped: "no_task_text", verified: true } };
    if (!delivery.verified) {
      return c.json(
        refusal("delivery", delivery.reason ?? "session_delivery_unverified", slotStateSummary(db.getSlot(slotNum))),
        502,
      );
    }
    const after = db.getSlot(slotNum);
    const readbackOk = !!after
      && after.occupied === true
      && after.issue === desiredTuple.issue
      && (!needsDelivery || (typeof after.task === "string" && after.task.trim() !== ""));
    if (!readbackOk) {
      // The task was delivered but the durable row does not corroborate it.
      // NEVER finalize here: the intent stays pending_delivery so an identical
      // retry re-enters this readback (ownership revalidated first) instead of
      // replaying as an assigned/idempotent=true success against a row that is
      // still inconsistent.
      return c.json(
        refusal("readback", "assignment_readback_inconsistent", slotStateSummary(after)),
        409,
      );
    }

    // Label projection runs ONLY after ownership and delivery both succeed.
    // Implicit-release-first ordering: unwind the displaced lane, then
    // project the new claim. Failures are recorded and never refuse the
    // durable assignment; a same-binding resume retries them (see the
    // delivered-replay path above) without repeating clear, commit, or
    // delivery. The displaced lane is read from the committed intent (not
    // this invocation's memory), so a resume that completes delivery still
    // unwinds the prior lane's labels even though the overwrite happened in
    // an earlier attempt.
    const projector = dependencies.issueProjection ?? NO_ISSUE_PROJECTION;
    const recordedDisplaced = parseDisplacedTuple(
      db.getAssignmentEffectIntent(request.effect_id)?.displaced_tuple,
    );
    if (recordedDisplaced) {
      releaseProjection = await projectIssue(
        projector, "released",
        recordedDisplaced.issue,
        slotNum,
        recordedDisplaced.repository_id,
      );
    }
    assignProjection = await projectIssue(
      projector, "assigned", desiredTuple.issue, slotNum, desiredTuple.repository_id,
    );
    // The delivered receipt durably carries the projection outcomes so a
    // resume can finish failed labels without repeating any MoP-side step.
    const mergedReceipt = mergeProjectionReceipt(
      isRecord(delivery.receipt) ? delivery.receipt : {},
      assignProjection,
      releaseProjection,
    );
    db.markAssignmentEffectDelivered(request.effect_id, mergedReceipt);
    db.logEvent(slotNum, "assignment_effect_delivered", null, null, {
      effect_id: request.effect_id,
      assignment_epoch: committedEpoch,
      delivery: delivery.receipt,
      release_projection: releaseProjection,
      issue_projection: assignProjection,
    });

    return c.json({
      status: "assigned",
      slot: slotNum,
      assignment_epoch: committedEpoch,
      ownership_receipt: {
        repository_id: after.repository_id,
        issue: after.issue,
        pr: after.pr,
        branch: after.branch,
        head_sha: after.head_sha,
        work_kind: after.work_kind,
        handoff_id: after.handoff_id,
        claimed_at: after.claimed_at,
        assignment_epoch: after.assignment_epoch,
        occupied: after.occupied,
        idempotent: false,
      },
      delivery_receipt: delivery.receipt,
      effect_id: request.effect_id,
      request_digest: requestDigest,
      idempotent: false,
      slot_state_after: slotStateSummary(after),
      issue_projection: assignProjection,
      ...(releaseProjection ? { release_projection: releaseProjection } : {}),
      ...(displacement.prior ? {
        displacement: {
          prior: displacement.prior,
          conflicting_owner_slots: displacement.conflicting_owner_slots,
          clear: displacement.clear,
          interrupt: displacement.interrupt,
          worktree: displacement.worktree,
        },
      } : {}),
    });
  });
}
