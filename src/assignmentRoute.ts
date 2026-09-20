import type { Hono } from "hono";
import { z } from "zod";

/**
 * Assignment identity may only be written through the guarded assign/release
 * paths, never through the generic slot PATCH. This is an identity-integrity
 * guard, not an authority check (MoP is a single-user local tool).
 */
export const ASSIGNMENT_IDENTITY_PATCH_FIELDS = new Set([
  "occupied",
  "status",
  "repository_id",
  "issue",
  "pr",
  "branch",
  "branch_ref",
  "head_sha",
  "assignment_epoch",
  "assigned_at",
  "work_kind",
  "handoff_id",
  "claimed_at",
]);


export function assignmentIdentityPatchFields(
  updates: Record<string, unknown>
): string[] {
  return Object.keys(updates)
    .filter((field) => ASSIGNMENT_IDENTITY_PATCH_FIELDS.has(field))
    .sort();
}
import {
  normalizeBranchIdentity,
  normalizeRepositoryId,
  type AssignmentTupleInput,
  type MoPDatabase,
  type SlotPredecessorContext,
} from "./db.js";
import {
  NO_ISSUE_PROJECTION,
  type IssueOwnershipProjection,
  type IssueProjectionOutcome,
} from "./issueProjection.js";
import {
  defaultPaneTargetForSlot,
  isValidPaneTarget,
  verifySessionPane,
} from "./sessionDelivery.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

const assignmentSlotParamSchema = z.coerce.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT);

// The historical /assign caller is intentionally issue-only. Once any
// extended identity field is supplied, the request is a complete claim and
// must not be silently downgraded to assignIssueToSlot.
const COMPLETE_ASSIGN_FIELDS = [
  "expected_epoch",
  "repository_id",
  "issue",
  "pr",
  "branch",
  "head_sha",
  "work_kind",
  "handoff_id",
  "task",
] as const;

const COMPLETE_ASSIGN_DISCRIMINATORS = [
  "expected_epoch",
  "pr",
  "branch",
  "branch_ref",
  "head_sha",
  "work_kind",
  "handoff_id",
  "claimed_at",
] as const;

const COMPLETE_REBIND_EXPECTED_FIELDS = [
  "expected_current_repository_id",
  "expected_current_issue",
  "expected_current_pr",
  "expected_current_branch",
  "expected_current_head_sha",
  "expected_current_work_kind",
  "expected_current_handoff_id",
  "expected_current_claimed_at",
] as const;

const COMPLETE_REBIND_DESIRED_FIELDS = [
  "repository_id",
  "issue",
  "pr",
  "branch",
  "head_sha",
  "work_kind",
  "handoff_id",
  "claimed_at",
] as const;

function hasOwn(body: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(body, key);
}

function hasEvery(body: Record<string, unknown>, fields: readonly string[]): boolean {
  return fields.every((field) => hasOwn(body, field));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasCompleteAssignmentValues(body: Record<string, unknown>): boolean {
  return normalizeRepositoryId(body.repository_id) !== null
    && Number.isInteger(body.issue) && Number(body.issue) > 0
    // A complete issue-only claim deliberately carries an explicit null PR.
    // Omitting pr still fails the complete-field presence check, while a
    // PR-bearing claim must remain a positive integer.
    && (body.pr === null || (Number.isInteger(body.pr) && Number(body.pr) > 0))
    && typeof body.branch === "string"
    && normalizeBranchIdentity(body.branch) !== null
    && typeof body.head_sha === "string"
    && /^[0-9a-f]{40}$/i.test(body.head_sha)
    && typeof body.work_kind === "string"
    && body.work_kind.trim() !== ""
    && typeof body.handoff_id === "string"
    && body.handoff_id.trim() !== ""
    && typeof body.task === "string"
    && body.task.trim() !== "";
}

function completeTuple(
  body: Record<string, unknown>,
  prefix = "",
): AssignmentTupleInput {
  return {
    repository_id: body[`${prefix}repository_id`] as string | number | null,
    issue: body[`${prefix}issue`] as number | null,
    pr: body[`${prefix}pr`] as number | null,
    branch: body[`${prefix}branch`] as string | null,
    head_sha: body[`${prefix}head_sha`] as string | null,
    work_kind: body[`${prefix}work_kind`] as string | null,
    handoff_id: body[`${prefix}handoff_id`] as string | null,
    claimed_at: body[`${prefix}claimed_at`] as string | null,
  };
}

/**
 * Decide whether a forced assignment displaced an uncleared predecessor.
 *
 * MoP can invalidate row-level owner state during the commit, but it must never
 * report a silent success when the displaced slot still carries a live
 * predecessor context (an active turn, mid-flight activity, or an owned task).
 */
function predecessorClearance(predecessor: SlotPredecessorContext | null | undefined): {
  cleared: boolean;
  reason: string;
} {
  if (!predecessor) {
    return { cleared: true, reason: "no_predecessor" };
  }
  // An occupied accepted lane is not cleared merely because its turn state
  // reads inactive: only an explicit idle=true qualifies for
  // predecessor_idle. A freshly accepted-not-started lane (idle=false,
  // no activity, inactive turn) still carries its predecessor context.
  if (predecessor.idle !== true) {
    return { cleared: false, reason: "predecessor_context_uncleared" };
  }
  const liveTurn = predecessor.active_turn_id !== null
    || predecessor.active_turn_state !== "inactive";
  const busy = predecessor.activity !== null && predecessor.activity !== "waiting_for_pm_direction";
  if (liveTurn || busy) {
    return { cleared: false, reason: "predecessor_context_uncleared" };
  }
  return { cleared: true, reason: "predecessor_idle" };
}

/**
 * Project the derived issue-side ownership surface for a committed transition.
 *
 * The durable MoP row is already committed when this runs, so a projection
 * failure is reported as a typed sibling field and never rewrites the durable
 * outcome, the HTTP status, or the exact tuple/CAS refusal path.
 */
async function projectOwnership(
  projection: IssueOwnershipProjection,
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
    return await projection.onAssigned(target, slot, repositoryKey);
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

/**
 * Mirror of the durable layer's complete-claim idempotency predicate
 * (db.assignSlot): same occupied row, same repository/issue/pr/branch/head
 * and same work metadata. Used only to tell an idempotent same-tuple replay
 * (which stays on the normal path) from a forced displace attempt.
 */
function isSameCompleteTuple(
  current: {
    occupied: boolean;
    repository_id: string | null;
    issue: number | null;
    pr: number | null;
    branch_ref: string | null;
    head_sha: string | null;
    work_kind: string | null;
    handoff_id: string | null;
  },
  body: Record<string, unknown>,
): boolean {
  const normalizedRepositoryId = normalizeRepositoryId(body.repository_id);
  const branchIdentity = normalizeBranchIdentity(body.branch as string | null | undefined);
  if (!normalizedRepositoryId || !branchIdentity) {
    return false;
  }
  const normalizedIssue = Number.isInteger(body.issue) && (body.issue as number) > 0
    ? (body.issue as number)
    : null;
  const normalizedPr = Number.isInteger(body.pr) && (body.pr as number) > 0
    ? (body.pr as number)
    : null;
  const normalizedWorkKind = typeof body.work_kind === "string" ? body.work_kind.trim() : body.work_kind;
  const normalizedHandoffId = typeof body.handoff_id === "string" ? body.handoff_id.trim() : body.handoff_id;
  const metadataMatches = normalizedWorkKind === null
    ? current.work_kind === null && current.handoff_id === null
    : current.work_kind === normalizedWorkKind && current.handoff_id === normalizedHandoffId;
  return current.occupied
    && current.repository_id === normalizedRepositoryId
    && current.issue === normalizedIssue
    && current.pr === normalizedPr
    && current.branch_ref === branchIdentity.branchRef
    && current.head_sha === body.head_sha
    && metadataMatches;
}

export function registerAssignmentRoute(
  app: Hono,
  db: MoPDatabase,
  issueProjection: IssueOwnershipProjection = NO_ISSUE_PROJECTION,
): void {
  app.post("/slots/:slotNum/assign", async (c) => {
    const slotParse = assignmentSlotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ error: "Invalid slot number" }, 400);
    }

    const body = await c.req.json();
    if (!isRecord(body) || !Number.isInteger(body.issue) || Number(body.issue) <= 0) {
      return c.json({
        success: false,
        conflict: true,
        error: "positive issue is required",
        reason: "invalid_issue",
      }, 409);
    }
    if (body.force_over_occupied !== undefined && typeof body.force_over_occupied !== "boolean") {
      return c.json({
        success: false,
        conflict: true,
        error: "force_over_occupied must be a boolean",
        reason: "invalid_force_over_occupied",
        slot: db.getSlot(slotParse.data),
      }, 409);
    }
    if (body.clear_predecessor !== undefined && typeof body.clear_predecessor !== "boolean") {
      return c.json({
        success: false,
        conflict: true,
        error: "clear_predecessor must be a boolean",
        reason: "invalid_clear_predecessor",
        slot: db.getSlot(slotParse.data),
      }, 409);
    }
    if (body.clear_predecessor === true) {
      // This route invalidates only row-level owner state during the commit; it
      // must not pretend to clear a live predecessor pane. Refuse truthfully
      // and name the canonical path instead of silently ignoring the request.
      return c.json({
        success: false,
        conflict: true,
        error: "clear_predecessor is not supported on the assign route",
        reason: "clear_predecessor_unsupported_on_assign",
        slot: db.getSlot(slotParse.data),
        remediation:
          "Release or clear the previous owner through the canonical path (Skill(direct-release) / the slot clear route) and then assign; the assign route never clears a live predecessor pane.",
      }, 409);
    }
    const forceOverOccupied = body.force_over_occupied === true;
    const completeRequested = COMPLETE_ASSIGN_DISCRIMINATORS.some((field) => hasOwn(body, field));
    const task = typeof body.task === "string" ? body.task : "";
    // Silent-wedge guard: verify the target session pane is live BEFORE any
    // epoch advance. A dead/missing pane fails typed with no mutation; an
    // unverifiable checker degrades to an explicit marker, never silence.
    // The `delivery` attestation is not a tuple field and never trips the
    // complete-claim discriminator above.
    const delivery = isRecord(body.delivery) ? body.delivery : null;
    const attestedPaneId = typeof delivery?.pane_id === "string" ? delivery.pane_id : null;
    if (attestedPaneId !== null && attestedPaneId !== "" && !isValidPaneTarget(attestedPaneId)) {
      return c.json({
        success: false,
        conflict: true,
        reason: "session_delivery_unverified",
        error: `delivery pane target is not a pinned tmux pane: ${attestedPaneId}`,
        pane: attestedPaneId,
      }, 409);
    }
    const paneTarget = attestedPaneId ? attestedPaneId : defaultPaneTargetForSlot(slotParse.data);
    const paneCheck = await verifySessionPane(paneTarget);
    if (paneCheck.state === "dead" ||
        (paneCheck.state === "unknown" && paneCheck.reason === "invalid_pane_target")) {
      return c.json({
        success: false,
        conflict: true,
        reason: "session_delivery_unverified",
        error: `target session pane is not live: ${paneTarget}; assignment not recorded`,
        pane: paneTarget,
      }, 409);
    }
    const paneCheckUnavailable = paneCheck.state === "unknown";
    const attested = delivery !== null;
    const attestedTransport = typeof delivery?.transport === "string" ? delivery.transport : null;
    const attestedPacketSha = typeof delivery?.packet_sha256 === "string" ? delivery.packet_sha256 : null;
    const sessionDelivery = paneCheckUnavailable
      ? { verified: false, reason: "pane_check_unavailable", pane: paneTarget, attested }
      : {
          verified: true,
          pane: paneTarget,
          attested,
          ...(attestedTransport !== null ? { transport: attestedTransport } : {}),
          ...(attestedPacketSha !== null ? { packet_sha256: attestedPacketSha } : {}),
        };
    let result;
    if (completeRequested) {
      if (!hasEvery(body, COMPLETE_ASSIGN_FIELDS) || !hasCompleteAssignmentValues(body)) {
        return c.json({
          success: false,
          conflict: true,
          error: "complete assignment tuple is required",
          reason: "observed_tuple_mismatch",
        }, 409);
      }
      if (forceOverOccupied) {
        // Complete claims stay strict: force has no meaning on this path, so
        // a forced complete claim over a different occupied tuple must fail
        // with a typed reason before any mutation — never silently displace
        // the live owner (and never silently ignore the flag). An idempotent
        // same-tuple replay falls through to the durable layer below.
        const current = db.getSlot(slotParse.data);
        if (current?.occupied && !isSameCompleteTuple(current, body)) {
          const predecessor: SlotPredecessorContext = {
            issue: current.issue,
            pr: current.pr,
            task: current.task,
            active_turn_id: current.active_turn_id,
            active_turn_state: current.active_turn_state,
            activity: current.activity,
            idle: current.idle,
            claimed_at: current.claimed_at,
          };
          return c.json({
            success: false,
            conflict: true,
            error: "force_over_occupied is not supported for complete-claim requests over an occupied slot",
            reason: "force_over_occupied_unsupported_for_complete_claim",
            slot: current,
            predecessor,
            remediation:
              "Release the occupied slot through the canonical release-first path (Skill(direct-release)) and assign without force_over_occupied; the assign route never force-displaces a live complete-claim owner.",
          }, 409);
        }
      }
      result = db.assignSlot(
        slotParse.data,
        task,
        body.repository_id as string | number | null,
        body.issue as number,
        body.branch as string | null,
        body.pr as number | null,
        body.head_sha as string | null,
        body.expected_epoch as number,
        body.work_kind as string | null,
        body.handoff_id as string | null,
        !forceOverOccupied,
      );
    } else {
      const repositoryId = (
        typeof body.repository_id === "string" || typeof body.repository_id === "number"
      ) ? body.repository_id : (process.env.MOP_LEGACY_REPOSITORY_ID ?? "heydonna-app/heydonna-app");
      result = db.assignIssueToSlot(
        slotParse.data,
        body.issue as number,
        task,
        repositoryId,
        { forceOverOccupied: forceOverOccupied === true },
      );
    }

    if (!result.ok) {
      // Truthful refusal: the envelope carries the actual current slot state so
      // the caller sees exactly what blocked the transition.
      return c.json({ success: false, slot: db.getSlot(slotParse.data), ...result }, 409);
    }

    db.logEvent(slotParse.data, "slot_assigned", null, null, {
      issue: body.issue,
      assignment_epoch: result.assignment_epoch,
      idempotent: result.idempotent,
      assignment_mode: completeRequested ? "complete" : "issue-only",
      session_delivery: sessionDelivery,
      ...(completeRequested ? {
        repository_id: body.repository_id,
        pr: body.pr,
        branch: body.branch,
        head_sha: body.head_sha,
        work_kind: body.work_kind,
        handoff_id: body.handoff_id,
      } : {}),
    });

    const updated = db.getSlot(slotParse.data);
    const expectedBranch = completeRequested && typeof body.branch === "string"
      ? normalizeBranchIdentity(body.branch)?.branch
      : null;
    const expectedWorkKind = completeRequested && typeof body.work_kind === "string"
      ? body.work_kind.trim()
      : null;
    const expectedHandoffId = completeRequested && typeof body.handoff_id === "string"
      ? body.handoff_id.trim()
      : null;
    if (completeRequested && (
      !updated
      || !updated.occupied
      || updated.assignment_epoch !== result.assignment_epoch
      || updated.task !== task
      || updated.repository_id !== normalizeRepositoryId(body.repository_id)
      || updated.issue !== body.issue
      || updated.pr !== body.pr
      || updated.branch !== expectedBranch
      || updated.head_sha !== body.head_sha
      || updated.work_kind !== expectedWorkKind
      || updated.handoff_id !== expectedHandoffId
    )) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete assignment readback is not durable",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    const ownershipProjection = await projectOwnership(
      issueProjection,
      updated?.issue ?? (body.issue as number),
      slotParse.data,
      updated?.repository_id ?? null,
    );
    const clearance = predecessorClearance(result.predecessor);
    if (!clearance.cleared) {
      // The durable assignment committed (the readback above is truthful and
      // authoritative), but the displaced owner still carries a live context.
      // That is never reported as a silent success.
      db.logEvent(slotParse.data, "slot_assigned_predecessor_uncleared", null, null, {
        issue: body.issue,
        assignment_epoch: result.assignment_epoch,
        predecessor: result.predecessor ?? null,
        reason: clearance.reason,
      });
      return c.json({
        success: false,
        conflict: false,
        error: "the predecessor owner context was not cleared",
        reason: clearance.reason,
        predecessor: result.predecessor ?? null,
        slot: updated,
        issue_projection: ownershipProjection,
        remediation:
          "Release or clear the previous owner through the canonical path (Skill(direct-release), or an explicit forced clear) before re-tasking this slot; the durable assignment already committed, so the readback above is the current truth.",
      }, 409);
    }
    return c.json({
      ...updated,
      session_delivery: sessionDelivery,
      issue_projection: ownershipProjection,
      predecessor_context: { cleared: clearance.cleared, reason: clearance.reason },
    });
  });

  app.post("/slots/:slotNum/adopt-issue-claim", async (c) => {
    const slotParse = assignmentSlotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ error: "Invalid slot number" }, 400);
    }

    const body = await c.req.json();
    if (!isRecord(body)) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete expected and desired assignment tuples are required",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    if (!Number.isInteger(body.expected_epoch)) {
      return c.json({
        success: false,
        conflict: true,
        error: "expected_epoch is required and must be an integer",
      }, 409);
    }
    if (!hasEvery(body, COMPLETE_REBIND_EXPECTED_FIELDS)) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete expected assignment tuple is required",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    if (!hasEvery(body, COMPLETE_REBIND_DESIRED_FIELDS)) {
      return c.json({
        success: false,
        conflict: true,
        error: "complete desired assignment tuple is required",
        reason: "observed_tuple_mismatch",
      }, 409);
    }
    const expectedTuple = completeTuple(body, "expected_current_");
    const desiredTuple = completeTuple(body);
    const result = db.rebindSlot(
      slotParse.data,
      body.expected_epoch as number,
      expectedTuple,
      desiredTuple,
      typeof body.task === "string" ? body.task : null,
    );
    if (!result.ok) {
      return c.json({ success: false, ...result }, 409);
    }

    if (!result.idempotent) {
      db.logEvent(slotParse.data, "slot_issue_claim_adopted", null, null, {
        ...body,
        rebind: true,
        assignment_epoch: result.assignment_epoch,
        idempotent: false,
      });
    }
    const rebound = db.getSlot(slotParse.data);
    return c.json({
      ...rebound,
      issue_projection: await projectOwnership(
        issueProjection,
        rebound?.issue ?? desiredTuple.issue,
        slotParse.data,
        rebound?.repository_id ?? desiredTuple.repository_id,
      ),
    });
  });
}
