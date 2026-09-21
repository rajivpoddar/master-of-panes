import type { Hono } from "hono";
import { z } from "zod";
import { createHash } from "node:crypto";

import type { AssignmentTupleInput, MoPDatabase } from "./db.js";
import type { SlotState } from "./types.js";
import {
  normalizeAssignmentTuple,
  normalizeBranchIdentity,
  normalizeRepositoryId,
} from "./db.js";
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
}

function refusal(
  stepFailed: AssignmentEffectRefusalBody["step_failed"],
  reason: string,
  slotStateAfter: string,
): AssignmentEffectRefusalBody {
  return {
    status: "refused",
    step_failed: stepFailed,
    reason,
    slot_state_after: slotStateAfter,
    sanctioned_path: "mop-assign-slot",
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
    if (
      typeof raw.effect_id !== "string"
      || raw.effect_id.trim() === ""
      || selectionClass === null
      || !Number.isInteger(raw.expected_epoch)
      || typeof raw.task !== "string"
      || raw.task.trim() === ""
      || typeof raw.task_file !== "string"
      || raw.task_file.trim() === ""
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
      claimed_at: (raw.claimed_at ?? null) as string | null,
    };
    const normalizedTuple = normalizeAssignmentTuple(desiredTuple);
    const branchIdentity = normalizeBranchIdentity(desiredTuple.branch);
    if (
      !normalizedTuple
      || normalizeRepositoryId(desiredTuple.repository_id) === null
      || !Number.isInteger(desiredTuple.issue)
      || (desiredTuple.issue as number) <= 0
      || !branchIdentity
      || typeof desiredTuple.head_sha !== "string"
      || !/^[0-9a-f]{40}$/i.test(desiredTuple.head_sha)
      || typeof desiredTuple.work_kind !== "string"
      || desiredTuple.work_kind.trim() === ""
      || typeof desiredTuple.handoff_id !== "string"
      || desiredTuple.handoff_id.trim() === ""
    ) {
      return c.json(
        refusal("ownership", "invalid_assignment_tuple", slotStateSummary(db.getSlot(slotNum))),
        400,
      );
    }

    const request: AssignmentEffectRequestBody = {
      effect_id: raw.effect_id.trim(),
      selection_class: selectionClass,
      expected_epoch: raw.expected_epoch as number,
      desired_tuple: desiredTuple,
      task: raw.task,
      task_file: raw.task_file.trim(),
    };
    const taskDigest = createHash("sha256").update(request.task).digest("hex");
    const requestDigest = computeAssignmentEffectDigest(request);

    // Idempotent replay / binding fence.
    const existing = db.getAssignmentEffectIntent(request.effect_id);
    if (existing) {
      if (existing.request_digest !== requestDigest || existing.slot !== slotNum) {
        return c.json(
          refusal("ownership", "effect_binding_conflict", slotStateSummary(db.getSlot(slotNum))),
          409,
        );
      }
      if (existing.state === "delivered") {
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
          delivery_receipt: { verified: true, detail: existing.delivery_receipt, idempotent: true },
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

    if (ownershipPending && request.selection_class === "new_issue") {
      const cleared = await dependencies.clearSlot(slotNum);
      if (!cleared.ok) {
        return c.json(
          refusal("clean", cleared.reason, slotStateSummary(db.getSlot(slotNum))),
          409,
        );
      }
    }

    let committedEpoch = intent.committed_epoch;
    if (ownershipPending) {
      const current = db.getSlot(slotNum);
      if (!current) {
        return c.json(refusal("ownership", "slot_not_found", slotStateSummary(null)), 404);
      }
      if (desiredTupleAlreadyCommitted(current, desiredTuple)) {
        // The ownership commit already happened for this binding (e.g. a crash
        // between the commit and the intent update). Never bump a second epoch.
        committedEpoch = current.assignment_epoch;
      } else if (current.occupied) {
        const rebind = db.rebindSlot(
          slotNum,
          current.assignment_epoch,
          {
            repository_id: current.repository_id,
            issue: current.issue,
            pr: current.pr,
            branch: current.branch,
            head_sha: current.head_sha,
            work_kind: current.work_kind,
            handoff_id: current.handoff_id,
            claimed_at: current.claimed_at,
          },
          desiredTuple,
          request.task,
        );
        if (!rebind.ok) {
          return c.json(
            refusal("ownership", rebind.reason ?? "ownership_rebind_refused", slotStateSummary(db.getSlot(slotNum))),
            409,
          );
        }
        committedEpoch = rebind.assignment_epoch;
      } else {
        const assign = db.assignSlot(
          slotNum,
          request.task,
          desiredTuple.repository_id,
          desiredTuple.issue,
          desiredTuple.branch,
          desiredTuple.pr,
          desiredTuple.head_sha,
          request.expected_epoch,
          desiredTuple.work_kind,
          desiredTuple.handoff_id,
          true,
        );
        if (!assign.ok) {
          return c.json(
            refusal("ownership", assign.reason ?? "ownership_assign_refused", slotStateSummary(db.getSlot(slotNum))),
            409,
          );
        }
        committedEpoch = assign.assignment_epoch;
      }
      db.markAssignmentEffectCommitted(request.effect_id, committedEpoch ?? 0);
      db.logEvent(slotNum, "assignment_effect_committed", null, null, {
        effect_id: request.effect_id,
        selection_class: request.selection_class,
        assignment_epoch: committedEpoch,
        request_digest: requestDigest,
      });
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

    const delivery = await dependencies.deliverTaskFile(slotNum, request.task_file);
    if (!delivery.verified) {
      // NAMED recoverable state: ownership is committed and the task text is
      // recorded, but the session has not been proven to receive it. Never a
      // silent success, never an implicit rollback.
      return c.json(
        refusal("delivery", delivery.reason ?? "session_delivery_unverified", slotStateSummary(db.getSlot(slotNum))),
        502,
      );
    }
    const after = db.getSlot(slotNum);
    const readbackOk = !!after
      && after.occupied === true
      && after.issue === desiredTuple.issue
      && typeof after.task === "string"
      && after.task.trim() !== "";
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

    db.markAssignmentEffectDelivered(request.effect_id, JSON.stringify(delivery.receipt));
    db.logEvent(slotNum, "assignment_effect_delivered", null, null, {
      effect_id: request.effect_id,
      assignment_epoch: committedEpoch,
      delivery: delivery.receipt,
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
    });
  });
}
