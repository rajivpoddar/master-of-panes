import { resolve } from "node:path";

import {
  assignmentTupleMatches,
  computeFamily2ReleaseDigest,
  computeNoPaneReleaseDigest,
  normalizeAssignmentTuple,
  slotAssignmentTuple,
  type AssignmentTuple,
  type AssignmentTupleInput,
  type MoPDatabase,
} from "./db.js";
import type {
  IssueOwnershipProjection,
  IssueProjectionOutcome,
} from "./issueProjection.js";
import type { SlotState } from "./types.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

export interface NativeSlotReleaseRequest {
  slot: number;
  expected_epoch: number;
  expected_tuple: AssignmentTupleInput;
  intended_main_head: string;
  /** Immutable Family-2 effect identity; absent for legacy native callers. */
  effect_id?: string;
  request_digest?: string;
}

export interface NativeSlotNoPaneReleaseRequest {
  slot: number;
  expected_epoch: number;
  expected_tuple: AssignmentTupleInput;
  expected_task: string;
  checkout_path: string;
  effect_id: string;
  request_digest: string;
}

export interface CheckoutReadOnlyObservation {
  checkout_path: string;
  clean: boolean;
  unpushed_commits: string[];
  branch?: string | null;
  head?: string | null;
  error?: string | null;
}

export interface CheckoutResetObservation {
  checkout_path: string;
  branch: string | null;
  head: string | null;
  clean: boolean;
  reset_succeeded: boolean;
  error?: string | null;
}

/**
 * Short quiescence floor for release. A slot whose last meaningful work is
 * inside this window may still be settling (a queued continuation, a resuming
 * session), so release waits rather than discarding live work. This is the
 * release-side companion of the 5-minute stale-turn standard: it is short
 * because an idle slot with no active turn is already proven not to be working.
 */
export const RELEASE_QUIESCENCE_MS = 60 * 1000;

/**
 * Bounded wait for the reset instruction's OWN induced turn to settle.
 *
 * The pane-mediated release delivers a stop/reset instruction into the owning
 * pane. That instruction is a user prompt, so the slot's UserPromptSubmit hook
 * records it as an agent turn (`db.startAgentTurn(slot, session_id)`). The
 * release must wait for that self-created turn to close before its authoritative
 * re-check, otherwise it mistakes its own instruction for a pre-existing active
 * turn and refuses forever. The wait is bounded and never treats a different
 * turn id as self-induced.
 */
export const RELEASE_SELF_TURN_SETTLE_MS = 180 * 1000;
export const RELEASE_SELF_TURN_POLL_MS = 2 * 1000;
/**
 * Delivery-registration grace, measured in complete settle poll intervals.
 *
 * The delivered instruction is a user prompt, so its `UserPromptSubmit` turn can
 * register a moment AFTER the first post-delivery sample. A row that still shows
 * no induced turn is therefore NOT proof of settlement until the prompt has had
 * at least this many complete poll intervals to register. Once an induced turn
 * has actually been observed, its own closure is authoritative and this grace no
 * longer applies.
 */
export const RELEASE_SELF_TURN_REGISTRATION_POLLS = 2;

/** Outcome of the bounded settle wait that follows a reset-instruction delivery. */
export type ReleaseSelfTurnSettle =
  | { ok: true; induced_turn_id: string | null; waited_ms: number }
  | { ok: false; kind: "timeout"; slot: SlotState | null; induced_turn_id: string | null; waited_ms: number; cause: ReleaseBlockCause }
  | { ok: false; kind: "replacement_turn"; slot: SlotState; induced_turn_id: string | null; turn_id: string; waited_ms: number }
  | { ok: false; kind: "epoch_mismatch" | "observed_tuple_mismatch" | "slot_free" | "slot_missing"; slot: SlotState | null; induced_turn_id: string | null; waited_ms: number };

/** Why the single release refusal fired. */
export type ReleaseBlockCause = "active_turn" | "dnd" | "productive_work" | "quiescence" | "state_moved";

export type ReleaseReadiness =
  | { ok: true }
  | { ok: false; cause: ReleaseBlockCause; message: string; remediation: string };

/**
 * The ONE release refusal: the slot is still working. Everything else about a
 * release (tuple drift, a stale/mis-moded intent, a moved main head) is
 * superseded against the live row rather than refused.
 */
export function evaluateReleaseReadiness(
  slot: Pick<SlotState, "idle" | "dnd" | "activity" | "active_turn_id" | "active_turn_state" | "last_meaningful_work_at">,
  nowMs: number,
): ReleaseReadiness {
  const turnId = typeof slot.active_turn_id === "string" && slot.active_turn_id.length > 0 ? slot.active_turn_id : null;
  if (turnId !== null || slot.active_turn_state !== "inactive") {
    const indeterminate = slot.active_turn_state === "indeterminate";
    return {
      ok: false,
      cause: "active_turn",
      message: turnId
        ? `Slot is not releasable: ${indeterminate ? "turn" : "hook turn"} ${turnId} is still active or indeterminate.`
        : "Slot is not releasable: an agent turn is still active or indeterminate.",
      remediation: turnId
        ? `If the owning session is gone (relaunched or replaced, so no Stop/SessionEnd hook can arrive) terminalize exactly this turn through the canonical path: POST /slots/{slot}/abandon-turn {"turn_id":"${turnId}","reason":"<why>","actor":"<who>"} (idempotent on repeat; a replacement or indeterminate turn is refused), then retry this release; it then succeeds. Otherwise wait for the authoritative Stop/SessionEnd hook and retry.`
        : "Wait for the authoritative Stop/SessionEnd hook and retry.",
    };
  }
  if (slot.dnd) {
    return {
      ok: false,
      cause: "dnd",
      message: "Slot is not releasable: DND is active.",
      remediation: "Clear DND, then retry this release.",
    };
  }
  if (!slot.idle) {
    return {
      ok: false,
      cause: "productive_work",
      message: `Slot is not releasable: it reports itself busy (idle=false, activity=${slot.activity ?? "null"}).`,
      remediation: "Leave the owner untouched and retry once the slot reports idle with no active turn.",
    };
  }
  const last = typeof slot.last_meaningful_work_at === "string" && slot.last_meaningful_work_at.length > 0
    ? Date.parse(slot.last_meaningful_work_at)
    : Number.NaN;
  if (!Number.isNaN(last)) {
    const quietMs = nowMs - last;
    if (quietMs < RELEASE_QUIESCENCE_MS) {
      const remaining = Math.max(1, Math.ceil((RELEASE_QUIESCENCE_MS - quietMs) / 1000));
      return {
        ok: false,
        cause: "quiescence",
        message: `Slot is not releasable yet: the quiescence window has ${remaining}s left.`,
        remediation: `Retry this release in about ${remaining}s; the slot is settling, not blocked.`,
      };
    }
  }
  return { ok: true };
}

/**
 * Record of every input the release superseded rather than refused. Present on
 * every result so the caller can see exactly which presented values lost to
 * MoP's live row.
 */
export interface ReleaseSupersession {
  presented_epoch: number;
  live_epoch: number | null;
  epoch_drift: boolean;
  /** False when the presented tuple could not be normalized at all. */
  presented_identity_usable: boolean;
  /** Stale activity telemetry on a closed turn; recorded, never a refusal. */
  ignored_activity: string | null;
  tuple_drift: boolean;
  intended_main_head: string;
  observed_main_head: string | null;
  head_drift: boolean;
  superseded_intent_id: string | null;
  repair: string[];
}

export function describeSupersession(supersession: ReleaseSupersession): string[] {
  const repair: string[] = [];
  if (!supersession.presented_identity_usable) repair.push("presented identity unusable; live row used");
  if (supersession.epoch_drift) repair.push(`epoch ${supersession.presented_epoch} -> ${supersession.live_epoch}`);
  if (supersession.tuple_drift) repair.push("owner tuple superseded by the live row");
  if (supersession.ignored_activity) repair.push(`stale activity '${supersession.ignored_activity}' ignored on a closed turn`);
  if (supersession.head_drift) repair.push(`intended_main_head ${supersession.intended_main_head} -> ${supersession.observed_main_head}`);
  if (supersession.superseded_intent_id) repair.push(`release intent ${supersession.superseded_intent_id} superseded`);
  return repair;
}

export interface NativeSlotReleaseAcknowledgement extends CheckoutResetObservation {
  slot: number;
  assignment_epoch: number;
  expected_tuple: AssignmentTupleInput;
}

export type NativeSlotReleaseCode =
  | "released"
  | "invalid_request"
  | "slot_not_found"
  | "slot_already_free_unverifiable"
  | "active_turn"
  | "epoch_mismatch"
  | "observed_tuple_mismatch"
  | "release_in_progress"
  | "checkout_identity_unavailable"
  | "delivery_failed"
  | "slot_not_idle"
  | "ack_checkout_mismatch"
  | "checkout_reset_failed"
  | "dirty_checkout"
  | "wrong_branch"
  | "wrong_head"
  | "effect_digest_mismatch"
  | "clear_conflict"
  | "free_readback_failed"
  | "effect_receipt_conflict"
  | "effect_receipt_malformed"
  | "dnd_active"
  | "task_mismatch"
  | "productive_work"
  | "checkout_not_clean"
  | "quiescent_release_required"
  | "quiescent_attestation_failed";

export interface NativeSlotReleaseResult {
  success: boolean;
  code: NativeSlotReleaseCode;
  message: string;
  slot: SlotState | null;
  assignment_epoch: number | null;
  remediation: string | null;
  effect_id?: string;
  request_digest?: string;
  idempotent?: boolean;
  acknowledgement?: NativeSlotReleaseAcknowledgement;
  issue_projection?: IssueProjectionOutcome | null;
  /** Why the single `slot_not_idle` refusal fired. */
  cause?: ReleaseBlockCause;
  /** Everything this release superseded instead of refusing. */
  superseded?: ReleaseSupersession;
}

interface NormalizedReleaseRequest extends NativeSlotReleaseRequest {
  intended_main_head: string;
}

export interface NativeSlotReleaseDependencies {
  db: MoPDatabase;
  /** Canonical issue-side ownership projection; optional so tests stay hermetic. */
  issueProjection?: IssueOwnershipProjection;
  resolveOwningCheckout: (slot: number) => Promise<string | null>;
  deliverInstruction: (slot: number, instruction: string) => Promise<boolean>;
  owningSlotIsIdle: (slot: number) => Promise<boolean>;
  resetAndObserveCheckout: (
    checkoutPath: string,
    intendedMainHead: string,
  ) => Promise<CheckoutResetObservation>;
  observeCheckout: (checkoutPath: string) => Promise<CheckoutReadOnlyObservation>;
  /** Bounded settle wait tuning + clock seam (defaults are production values). */
  selfTurnSettleTimeoutMs?: number;
  selfTurnSettlePollMs?: number;
  nowMs?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

function result(
  code: NativeSlotReleaseCode,
  message: string,
  slot: SlotState | null | undefined,
  remediation: string | null,
  success = false,
  cause?: ReleaseBlockCause,
): NativeSlotReleaseResult {
  return {
    success,
    code,
    message,
    slot: slot ?? null,
    assignment_epoch: slot?.assignment_epoch ?? null,
    remediation,
    ...(cause ? { cause } : {}),
  };
}

/**
 * The ONLY slot-facing release message. It is the actionable literal from the
 * direct-release contract: the slot switches itself to main with its own tools.
 *
 * It must never tell the slot to stop work or stay idle. The release gate has
 * already proved the slot is idle, inactive and quiescent before this is ever
 * delivered, so a "stop work" instruction addressed a slot that was not working
 * (Rajiv ruling 2026-09-21). MoP keeps the enforcement: it resets the checkout to
 * the exact intended head, attests clean main through the pane, and leaves the
 * slot occupied when attestation fails.
 */
export const RELEASE_ACTIONABLE_INSTRUCTION = "Switch to main and pull the latest origin/main.";

export function buildLiteralResetInstruction(): string {
  return RELEASE_ACTIONABLE_INSTRUCTION;
}

export class NativeSlotReleaseCoordinator {
  private readonly inProgressSlots = new Set<number>();

  constructor(private readonly dependencies: NativeSlotReleaseDependencies) {}

  async releaseWithoutPane(
    request: NativeSlotNoPaneReleaseRequest,
  ): Promise<NativeSlotReleaseResult> {
    const current = this.dependencies.db.getSlot(request.slot);
    const tuple = normalizeAssignmentTuple(request.expected_tuple);
    if (
      !Number.isInteger(request.slot)
      || request.slot < 1
      || request.slot > DEFAULT_DEV_SLOT_COUNT
      || !Number.isInteger(request.expected_epoch)
      || !tuple
      || typeof request.expected_task !== "string"
      || !request.expected_task.trim()
      || typeof request.checkout_path !== "string"
      || !request.checkout_path.startsWith("/")
      || typeof request.effect_id !== "string"
      || !request.effect_id.trim()
      || !/^[0-9a-f]{64}$/i.test(request.request_digest)
    ) {
      return result("invalid_request", "A complete no-pane release identity is required.", current, "Re-read the exact owner and checkout state.");
    }
    let computedDigest: string;
    try {
      computedDigest = computeNoPaneReleaseDigest({
        effect_id: request.effect_id,
        expected_epoch: request.expected_epoch,
        expected_tuple: request.expected_tuple,
        expected_task: request.expected_task,
        checkout_path: resolve(request.checkout_path),
      });
    } catch {
      return result("invalid_request", "The no-pane release identity is invalid.", current, "Recompute the effect from one exact tuple snapshot.");
    }
      if (computedDigest !== request.request_digest.toLowerCase()) {
      return {
        ...result("effect_digest_mismatch", "The no-pane release digest does not match its exact owner binding.", current, "Do not retry with a changed tuple."),
        effect_id: request.effect_id,
        request_digest: request.request_digest,
      };
    }
    // A lost response must be reconciled from the durable native receipt before
    // consulting pane/checkout state again.  The committed effect is already
    // the authority; a replay must never require a second precondition read or
    // issue another mutation after the original CAS succeeded.
    let priorReceipt;
    try {
      priorReceipt = this.dependencies.db.getNativeReleaseEffectReceipt(request.effect_id);
    } catch {
      return result("effect_receipt_malformed", "The no-pane release receipt is malformed.", current, "Stop and inspect the native release receipt.");
    }
    if (priorReceipt) {
      const priorTuple = normalizeAssignmentTuple(priorReceipt.expected_tuple);
      const priorValue = priorReceipt.expected_tuple as AssignmentTupleInput & { task?: unknown; checkout_path?: unknown };
      const same = priorReceipt.slot === request.slot
        && priorReceipt.expected_epoch === request.expected_epoch
        && priorReceipt.request_digest.toLowerCase() === request.request_digest.toLowerCase()
        && priorReceipt.intended_main_head === ""
        && assignmentTupleMatches(priorTuple, tuple)
        && priorValue.task === request.expected_task.trim()
        && priorValue.checkout_path === resolve(request.checkout_path);
      if (!same) {
        return result("effect_receipt_conflict", "The no-pane release effect receipt does not match the request.", current, "Do not retry with a changed identity.");
      }
      return {
        ...result("released", `Slot ${request.slot} no-pane release already committed.`, current, null, true),
        assignment_epoch: priorReceipt.released_epoch,
        effect_id: request.effect_id,
        request_digest: request.request_digest,
        idempotent: true,
        issue_projection: await this.projectReleasedOwner(
          normalizeAssignmentTuple(request.expected_tuple) ?? undefined,
          request.slot,
        ),
      };
    }
    if (this.inProgressSlots.has(request.slot)) {
      return result("release_in_progress", `Slot ${request.slot} already has an in-process no-pane release request.`, current, "Wait for the existing request to finish and re-read MoP.");
    }
    this.inProgressSlots.add(request.slot);
    try {
      const ownerCheckoutRaw = await this.dependencies.resolveOwningCheckout(request.slot);
      if (!ownerCheckoutRaw || resolve(ownerCheckoutRaw) !== resolve(request.checkout_path)) {
        return result("checkout_identity_unavailable", "The requested checkout is not the owning pane checkout.", this.dependencies.db.getSlot(request.slot), "Leave the slot occupied and re-read the pane-derived checkout identity.");
      }
      const observed = await this.dependencies.observeCheckout(resolve(ownerCheckoutRaw));
      if (
        !observed
        || observed.checkout_path !== resolve(ownerCheckoutRaw)
        || !observed.clean
        || !Array.isArray(observed.unpushed_commits)
        || observed.unpushed_commits.length !== 0
      ) {
        return result("checkout_not_clean", "The owning checkout is dirty or has unpushed commits.", this.dependencies.db.getSlot(request.slot), "Preserve the owner and repair the checkout before release.");
      }
      const committed = this.dependencies.db.commitNativeNoPaneRelease(
        request.slot,
        request.expected_epoch,
        request.expected_tuple,
        request.expected_task,
        {
          effect_id: request.effect_id,
          request_digest: request.request_digest,
          checkout_path: resolve(request.checkout_path),
        },
      );
      if (!committed.ok) {
        const code = committed.reason === "dnd_active"
          || committed.reason === "active_turn"
          || committed.reason === "task_mismatch"
          || committed.reason === "productive_work"
          || committed.reason === "epoch_mismatch"
          || committed.reason === "observed_tuple_mismatch"
          || committed.reason === "slot_already_free_unverifiable"
          ? committed.reason
          : "clear_conflict";
        return result(code, `No-pane release refused: ${committed.reason ?? "unknown"}.`, this.dependencies.db.getSlot(request.slot), "Leave the owner untouched and re-read the exact tuple.");
      }
      const readback = this.dependencies.db.getSlot(request.slot);
      if (!readback || readback.occupied || readback.assignment_epoch !== request.expected_epoch + 1 || slotAssignmentTuple(readback) !== null || readback.task !== null) {
        return result("free_readback_failed", "No-pane release readback did not prove the exact FREE postcondition.", readback, "Stop and inspect MoP before any further mutation.");
      }
      return {
        ...result("released", `Slot ${request.slot} released without pane delivery.`, readback, null, true),
        effect_id: request.effect_id,
        request_digest: request.request_digest,
        idempotent: committed.idempotent,
        issue_projection: await this.projectReleasedOwner(
          normalizeAssignmentTuple(request.expected_tuple) ?? undefined,
          request.slot,
        ),
      };
    } finally {
      this.inProgressSlots.delete(request.slot);
    }
  }

  /**
   * Project the derived issue-side surface after a committed release. The
   * durable release already happened, so any failure is returned as a typed
   * sibling field and never changes the reported release outcome.
   */
  /**
   * Wait, bounded, for the reset instruction's own induced turn to settle.
   *
   * `priorTurnId` is the turn the row carried immediately BEFORE delivery (the
   * release refuses earlier when one exists, so it is normally null). The first
   * turn observed after delivery is therefore the one our instruction induced;
   * any OTHER turn id is a replacement/pre-existing turn and refuses.
   */
  private async awaitInducedResetTurnSettle(
    slotNum: number,
    expectedEpoch: number,
    expectedTuple: AssignmentTuple,
    priorTurnId: string | null,
  ): Promise<ReleaseSelfTurnSettle> {
    const now = this.dependencies.nowMs ?? (() => Date.now());
    const sleep = this.dependencies.sleep
      ?? ((ms: number) => new Promise<void>((resolvePromise) => setTimeout(resolvePromise, ms)));
    const timeoutMs = this.dependencies.selfTurnSettleTimeoutMs ?? RELEASE_SELF_TURN_SETTLE_MS;
    const pollMs = this.dependencies.selfTurnSettlePollMs ?? RELEASE_SELF_TURN_POLL_MS;
    const registrationGraceMs = pollMs * RELEASE_SELF_TURN_REGISTRATION_POLLS;
    const startedAt = now();
    let inducedTurnId: string | null = priorTurnId;
    let lastCause: ReleaseBlockCause = "active_turn";
    for (;;) {
      const elapsed = now() - startedAt;
      const row = this.dependencies.db.getSlot(slotNum);
      if (!row) return { ok: false, kind: "slot_missing", slot: null, induced_turn_id: inducedTurnId, waited_ms: elapsed };
      if (row.assignment_epoch !== expectedEpoch) {
        return { ok: false, kind: "epoch_mismatch", slot: row, induced_turn_id: inducedTurnId, waited_ms: elapsed };
      }
      if (!assignmentTupleMatches(slotAssignmentTuple(row), expectedTuple)) {
        return { ok: false, kind: "observed_tuple_mismatch", slot: row, induced_turn_id: inducedTurnId, waited_ms: elapsed };
      }
      if (!row.occupied) {
        return { ok: false, kind: "slot_free", slot: row, induced_turn_id: inducedTurnId, waited_ms: elapsed };
      }
      const turnId = row.active_turn_id;
      if (turnId !== null) {
        if (inducedTurnId === null) {
          inducedTurnId = turnId;
        } else if (turnId !== inducedTurnId) {
          return { ok: false, kind: "replacement_turn", slot: row, induced_turn_id: inducedTurnId, turn_id: turnId, waited_ms: elapsed };
        }
      }
      // Reuse the SAME readiness predicate the release gate uses, so "settled"
      // means exactly "the post-delivery authoritative re-check will pass":
      // the induced turn has closed AND the short settling window has elapsed.
      const readiness = evaluateReleaseReadiness(row, now());
      // A row that reads "ready" before the delivered prompt has had time to
      // register its UserPromptSubmit turn is NOT proof of settlement: the
      // induced turn would then appear after this wait returned, and the
      // authoritative post-delivery re-check would refuse the release on a turn
      // the release itself created. Hold for the bounded two-poll grace while no
      // induced turn has been observed; once one is observed its closure is
      // authoritative and this grace no longer applies.
      const registrationPending = inducedTurnId === null && elapsed < registrationGraceMs;
      if (readiness.ok && !registrationPending) {
        return { ok: true, induced_turn_id: inducedTurnId, waited_ms: elapsed };
      }
      if (!readiness.ok) {
        lastCause = readiness.cause;
      }
      if (elapsed >= timeoutMs) {
        return { ok: false, kind: "timeout", slot: row, induced_turn_id: inducedTurnId, waited_ms: elapsed, cause: lastCause };
      }
      await sleep(pollMs);
    }
  }

  private async projectReleasedOwner(
    tuple: AssignmentTuple | undefined,
    slot: number,
  ): Promise<IssueProjectionOutcome | null> {
    const projection = this.dependencies.issueProjection;
    const issue = Number(tuple?.issue);
    if (!projection || !Number.isInteger(issue) || issue <= 0) {
      return null;
    }
    try {
      return await projection.onReleased(issue, slot, tuple?.repository_id ?? null);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      return {
        status: "failed",
        reason: `issue_projection_unexpected:${detail.split("\n")[0].slice(0, 200)}`,
        repository: null,
        issue,
        slot,
        added_labels: [],
        removed_labels: [],
        verified: false,
      };
    }
  }

  private replayDurableEffect(
    request: NativeSlotReleaseRequest,
  ): NativeSlotReleaseResult | null {
    if (request.effect_id === undefined) return null;
    if (
      typeof request.effect_id !== "string"
      || request.effect_id.trim() === ""
      || typeof request.request_digest !== "string"
      || !/^[0-9a-f]{64}$/i.test(request.request_digest)
    ) {
      return {
        ...result(
          "effect_receipt_malformed",
          "Family-2 release effect identity or request digest is malformed.",
          this.dependencies.db.getSlot(request.slot),
          "Preserve the committed outbox row and retry with its immutable effect identity.",
        ),
        effect_id: request.effect_id,
        request_digest: request.request_digest,
      };
    }
    if (!request.expected_tuple || typeof request.expected_tuple !== "object") {
      return {
        ...result(
          "effect_receipt_malformed",
          "Family-2 release effect is missing its immutable ownership tuple.",
          this.dependencies.db.getSlot(request.slot),
          "Preserve the committed outbox row and retry with its complete immutable tuple.",
        ),
        effect_id: request.effect_id,
        request_digest: request.request_digest,
      };
    }
    let computedDigest: string;
    try {
      computedDigest = computeFamily2ReleaseDigest({
        effect_id: request.effect_id,
        expected_epoch: request.expected_epoch,
        expected_tuple: request.expected_tuple,
        intended_main_head: request.intended_main_head,
      });
    } catch {
      return {
        ...result(
          "effect_receipt_malformed",
          "Family-2 release effect identity cannot be normalized.",
          this.dependencies.db.getSlot(request.slot),
          "Preserve the committed outbox row and retry with its complete immutable tuple.",
        ),
        effect_id: request.effect_id,
        request_digest: request.request_digest,
      };
    }
    if (computedDigest !== request.request_digest.toLowerCase()) {
      return {
        ...result(
          "effect_digest_mismatch",
          "Family-2 release digest does not match the normalized ownership tuple.",
          this.dependencies.db.getSlot(request.slot),
          "Reject the effect without delivery, reset, or clear; recompute it from the committed tuple.",
        ),
        effect_id: request.effect_id,
        request_digest: request.request_digest,
      };
    }
    let prior;
    try {
      prior = this.dependencies.db.getNativeReleaseEffectReceipt(request.effect_id);
    } catch {
      return {
        ...result(
          "effect_receipt_malformed",
          "Durable Family-2 release receipt is malformed.",
          this.dependencies.db.getSlot(request.slot),
          "Stop and repair the receipt store before retrying this effect.",
        ),
        effect_id: request.effect_id,
        request_digest: request.request_digest,
      };
    }
    if (!prior) return null;
    const expectedTuple = normalizeAssignmentTuple(request.expected_tuple);
    const priorTuple = normalizeAssignmentTuple(prior.expected_tuple);
    const same = expectedTuple && priorTuple
      && assignmentTupleMatches(expectedTuple, priorTuple)
      && prior.slot === request.slot
      && prior.expected_epoch === request.expected_epoch
      && prior.request_digest.toLowerCase() === request.request_digest.toLowerCase()
      && prior.intended_main_head.toLowerCase() === request.intended_main_head.toLowerCase();
    if (!same) {
      return {
        ...result(
          "effect_receipt_conflict",
          "A durable Family-2 release receipt exists for a conflicting effect binding.",
          this.dependencies.db.getSlot(request.slot),
          "Do not reuse the effect identity; reconcile the committed outbox tuple before retrying.",
        ),
        effect_id: request.effect_id,
        request_digest: request.request_digest,
      };
    }
    return {
      ...result(
        "released",
        "The exact Family-2 release effect was already committed; durable receipt consumed idempotently.",
        this.dependencies.db.getSlot(request.slot),
        null,
        true,
      ),
      assignment_epoch: prior.released_epoch,
      effect_id: request.effect_id,
      request_digest: request.request_digest,
      idempotent: true,
    };
  }

  async release(request: NativeSlotReleaseRequest): Promise<NativeSlotReleaseResult> {
    // Only the request SHAPE is validated here. The presented identity itself
    // is advisory: MoP's live row is authoritative, and anything it disagrees
    // with is superseded and audited rather than refused.
    const presentedTuple = normalizeAssignmentTuple(request.expected_tuple);
    if (
      !Number.isInteger(request.slot)
      || request.slot < 1
      || request.slot > DEFAULT_DEV_SLOT_COUNT
      || !Number.isInteger(request.expected_epoch)
      || typeof request.intended_main_head !== "string"
      || !/^[0-9a-f]{40}$/i.test(request.intended_main_head)
    ) {
      return result(
        "invalid_request",
        "A slot number, an epoch, and intended_main_head (40 hex) are required.",
        this.dependencies.db.getSlot(request.slot),
        "Re-read the slot and resend the request with those fields.",
      );
    }
    const current = this.dependencies.db.getSlot(request.slot);
    if (!current) {
      return result("slot_not_found", `Slot ${request.slot} does not exist.`, null, "Re-read MoP slot inventory.");
    }

    // Drift between the presented identity and MoP's live row is superseded,
    // never refused: the callers here are PM/CTO on one local machine, and the
    // live row is authoritative. Only "the slot is still working" refuses.
    const drift = (live: SlotState | null): ReleaseSupersession => ({
      presented_epoch: request.expected_epoch,
      live_epoch: live?.assignment_epoch ?? null,
      epoch_drift: live?.assignment_epoch !== request.expected_epoch,
      presented_identity_usable: presentedTuple !== null,
      tuple_drift: presentedTuple === null
        || !assignmentTupleMatches(live ? slotAssignmentTuple(live) : null, presentedTuple),
      intended_main_head: request.intended_main_head.toLowerCase(),
      observed_main_head: null,
      head_drift: false,
      ignored_activity: null,
      superseded_intent_id: null,
      repair: [],
    });
    const superseded = drift(current);
    if (
      current
      && current.idle
      && current.activity !== null
      && current.activity !== "waiting_for_pm_direction"
    ) {
      superseded.ignored_activity = current.activity;
    }
    /** Fill the human-readable repair summary once every field is known. */
    const withRepair = (): ReleaseSupersession => {
      superseded.repair = describeSupersession(superseded);
      return superseded;
    };

    // A durable Family-2 receipt is already-committed authority: honour it and
    // re-run the release projection repair before returning (a projection that
    // failed after the FREE commit must be healed by the identical retry).
    const replay = this.replayDurableEffect(request);
    if (replay) {
      if (!replay.success) return replay;
      return {
        ...replay,
        issue_projection: await this.projectReleasedOwner(presentedTuple ?? undefined, request.slot),
      };
    }

    // The durable Family-2 outbox path keeps its strict identity binding: its
    // exactly-once receipt is keyed to the immutable effect tuple, so drift
    // there is a receipt conflict rather than an operator release.
    let computedDigest: string | undefined;
    if (request.effect_id !== undefined) {
      if (presentedTuple === null) {
        return result(
          "invalid_request",
          "A committed Family-2 effect requires its complete immutable tuple.",
          current,
          "Resend the exact committed outbox tuple.",
        );
      }
      try {
        computedDigest = computeFamily2ReleaseDigest({
          effect_id: request.effect_id,
          expected_epoch: request.expected_epoch,
          expected_tuple: request.expected_tuple,
          intended_main_head: request.intended_main_head,
        });
      } catch {
        return {
          ...result(
            "effect_receipt_malformed",
            "Family-2 release effect identity cannot be normalized.",
            current,
            "Preserve the committed outbox row and retry with its complete immutable tuple.",
          ),
          effect_id: request.effect_id,
          request_digest: request.request_digest,
        };
      }
      if (computedDigest !== request.request_digest?.toLowerCase()) {
        return {
          ...result(
            "effect_digest_mismatch",
            "Family-2 release digest does not match the normalized ownership tuple.",
            current,
            "Reject the effect before delivery, reset, or clear; recompute it from the committed tuple.",
          ),
          effect_id: request.effect_id,
          request_digest: request.request_digest,
        };
      }
      if (!current.occupied) {
        return result(
          "slot_already_free_unverifiable",
          `Slot ${request.slot} is already FREE; the committed effect has no durable receipt to reconcile against.`,
          current,
          "Reconcile the committed outbox effect before retrying.",
        );
      }
      if (
        current.assignment_epoch !== request.expected_epoch
        || !assignmentTupleMatches(slotAssignmentTuple(current), presentedTuple)
      ) {
        return result(
          "observed_tuple_mismatch",
          "The committed effect no longer matches the live owner tuple.",
          current,
          "Reconcile the committed outbox tuple; the durable effect binding is immutable.",
        );
      }
    }

    if (!current.occupied) {
      // Retry idempotency: a second identical operator release is a no-op
      // success, not a refusal and never a second epoch bump.
      return {
        ...result("released", `Slot ${request.slot} is already FREE; release is an idempotent no-op.`, current, null, true),
        idempotent: true,
        superseded: withRepair(),
      };
    }

    // The ONE surviving refusal: the slot is still working.
    const readiness = evaluateReleaseReadiness(current, Date.now());
    if (!readiness.ok) {
      return result(
        "slot_not_idle",
        readiness.message,
        current,
        readiness.remediation,
        false,
        readiness.cause,
      );
    }

    if (this.inProgressSlots.has(request.slot)) {
      return result(
        "release_in_progress",
        `Slot ${request.slot} already has an in-process native release request.`,
        this.dependencies.db.getSlot(request.slot),
        "Wait for that synchronous request to finish, then re-read MoP.",
      );
    }
    this.inProgressSlots.add(request.slot);

    // Claim the live owner for the critical section. A stale or mis-moded
    // intent is superseded atomically here (and audited) instead of blocking
    // every later attempt.
    const claim = this.dependencies.db.claimNativeReleaseIntentForLiveOwner(request.slot);
    if (!claim) {
      this.inProgressSlots.delete(request.slot);
      const live = this.dependencies.db.getSlot(request.slot);
      const blocked = live ? evaluateReleaseReadiness(live, Date.now()) : null;
      if (blocked && !blocked.ok) {
        return result("slot_not_idle", blocked.message, live, blocked.remediation, false, blocked.cause);
      }
      return result(
        "slot_not_idle",
        `Slot ${request.slot} changed while the release was starting; re-read and retry.`,
        live,
        "Re-read the slot identity and retry the release.",
        false,
        "state_moved",
      );
    }
    const releaseIntentToken: string = claim.token;
    superseded.live_epoch = claim.expected_epoch;
    superseded.epoch_drift = claim.expected_epoch !== request.expected_epoch;
    superseded.tuple_drift = !assignmentTupleMatches(claim.expected_tuple, presentedTuple);
    superseded.superseded_intent_id = claim.superseded_intent_id;
    if (claim.superseded_intent_id) {
      this.dependencies.db.logEvent(request.slot, "native_release_intent_superseded", null, null, {
        superseded_intent_id: claim.superseded_intent_id,
        live_epoch: claim.expected_epoch,
        intended_main_head: request.intended_main_head.toLowerCase(),
        presentation: describeSupersession(superseded),
      });
    }

    try {
      const checkoutPathRaw = await this.dependencies.resolveOwningCheckout(request.slot);
      if (!checkoutPathRaw) {
        return result(
          "checkout_identity_unavailable",
          "MoP could not derive the owning checkout from the numbered pane.",
          this.dependencies.db.getSlot(request.slot),
          "Leave the slot occupied, restore the pane checkout identity, and retry from a fresh MoP read.",
        );
      }
      const checkoutPath = resolve(checkoutPathRaw);

      // Already clean on main => nothing to discard, so release without
      // disturbing the pane (this replaces the caller-facing release mode).
      let readOnly: CheckoutReadOnlyObservation | null = null;
      try {
        readOnly = await this.dependencies.observeCheckout(checkoutPath);
      } catch {
        readOnly = null;
      }
      const checkoutSettled = Boolean(
        readOnly
        && readOnly.checkout_path === checkoutPath
        && readOnly.clean
        && Array.isArray(readOnly.unpushed_commits)
        && readOnly.unpushed_commits.length === 0
        && readOnly.error == null
        && readOnly.branch === "main"
        && typeof readOnly.head === "string"
        && /^[0-9a-f]{40}$/i.test(readOnly.head),
      );

      // A pane instruction is only worth delivering when the slot's own tools
      // are uniquely able to satisfy it: leaving whatever branch/worktree state
      // it owns and landing on main. A checkout that already reports main needs
      // no pane instruction — the reset/attestation path below pulls the exact
      // head itself, and the acknowledgement checks still refuse a dirty or
      // wrong-branch checkout. Delivering a prompt the slot cannot act on only
      // re-arms the quiescence window with work this release created, so the
      // identical retry (live S6, 2026-09-23: main + one untracked plan file)
      // could never converge.
      const paneInstructionRequired = !checkoutSettled && readOnly?.branch !== "main";

      if (paneInstructionRequired) {
        // Retry guard: never inject a reset instruction while the row reports an
        // active turn. A repeat call made while a prior release-induced turn is
        // still live refuses HERE, without delivering another prompt, so the
        // release cannot pile instructions onto a busy slot.
        const preDelivery = this.dependencies.db.getSlot(request.slot);
        if (
          preDelivery
          && (preDelivery.active_turn_id !== null || preDelivery.active_turn_state !== "inactive")
        ) {
          const busy = evaluateReleaseReadiness(preDelivery, Date.now());
          return result(
            "slot_not_idle",
            busy.ok
              ? `Slot ${request.slot} is still working; no reset instruction was delivered.`
              : busy.message,
            preDelivery,
            busy.ok ? "Wait for the active turn to close, then retry the release." : busy.remediation,
            false,
            "active_turn",
          );
        }
        const preDeliveryTurnId = preDelivery?.active_turn_id ?? null;
        const instruction = buildLiteralResetInstruction();
        const delivered = await this.dependencies.deliverInstruction(request.slot, instruction);
        if (!delivered) {
          return result(
            "delivery_failed",
            "The owning slot did not receive the stop/reset instruction.",
            this.dependencies.db.getSlot(request.slot),
            "Leave the slot occupied, repair delivery, and retry from a fresh MoP read.",
          );
        }
        // The delivered instruction IS a user prompt, so the slot's
        // UserPromptSubmit hook records it as an agent turn. Wait, bounded, for
        // that self-induced turn to settle before the authoritative re-check;
        // any OTHER turn id is a replacement/pre-existing turn and refuses.
        const settle = await this.awaitInducedResetTurnSettle(
          request.slot,
          claim.expected_epoch,
          claim.expected_tuple,
          preDeliveryTurnId,
        );
        this.dependencies.db.logEvent(request.slot, "release_instruction_delivered", null, null, {
          checkout_path: checkoutPath,
          delivery: "pane",
          instruction_bytes: instruction.length,
          prior_turn_id: preDeliveryTurnId,
          induced_turn_id: settle.induced_turn_id,
          settle_ms: settle.waited_ms,
          settle_outcome: settle.ok ? "settled" : settle.kind,
          epoch: claim.expected_epoch,
        });
        if (!settle.ok) {
          if (settle.kind === "timeout") {
            return result(
              "slot_not_idle",
              `Slot ${request.slot} did not settle within ${Math.round(settle.waited_ms / 1000)}s after the reset instruction${
                settle.induced_turn_id ? ` (turn ${settle.induced_turn_id})` : ""
              }; no release was performed.`,
              settle.slot,
              settle.cause === "quiescence"
                ? "The slot is settling after the reset instruction; retry once the stated window elapses. Do not hand-edit slot state."
                : "Wait for the slot's turn to close, then retry the release; do not hand-edit slot state.",
              false,
              settle.cause,
            );
          }
          if (settle.kind === "replacement_turn") {
            return result(
              "slot_not_idle",
              `Slot ${request.slot} is running a different turn (${settle.turn_id}) than the reset instruction${
                settle.induced_turn_id ? ` (${settle.induced_turn_id})` : ""
              } induced; no release was performed.`,
              settle.slot,
              "A replacement/pre-existing turn owns the slot now; wait for it to close, then re-read and release.",
              false,
              "active_turn",
            );
          }
          if (settle.kind === "epoch_mismatch") {
            return result(
              "epoch_mismatch",
              "Assignment epoch changed while the reset instruction settled.",
              settle.slot,
              "Re-read the slot and retry the release with fresh state.",
            );
          }
          if (settle.kind === "observed_tuple_mismatch") {
            return result(
              "observed_tuple_mismatch",
              "The owner tuple changed while the reset instruction settled.",
              settle.slot,
              "Re-read the slot and retry the release with fresh state.",
            );
          }
          if (settle.kind === "slot_free") {
            return result(
              "slot_already_free_unverifiable",
              `Slot ${request.slot} became FREE while the reset instruction settled.`,
              settle.slot,
              "Re-read the caller's state; the slot is already free.",
            );
          }
          return result(
            "slot_not_found",
            `Slot ${request.slot} disappeared while the reset instruction settled.`,
            null,
            "Re-read MoP slot inventory.",
          );
        }
        if (!(await this.dependencies.owningSlotIsIdle(request.slot))) {
          return result(
            "slot_not_idle",
            "The owning slot did not reach idle after the stop instruction.",
            this.dependencies.db.getSlot(request.slot),
            "Leave the slot occupied and retry from a fresh read once the turn is inactive.",
            false,
            "active_turn",
          );
        }
        const postDelivery = this.dependencies.db.getSlot(request.slot);
        if (postDelivery) {
          const afterDelivery = evaluateReleaseReadiness(postDelivery, Date.now());
          if (!afterDelivery.ok) {
            return result(
              "slot_not_idle",
              afterDelivery.message,
              postDelivery,
              afterDelivery.remediation,
              false,
              afterDelivery.cause,
            );
          }
        }
      }

      let observation: CheckoutResetObservation;
      if (checkoutSettled) {
        observation = {
          checkout_path: checkoutPath,
          branch: "main",
          head: readOnly?.head ?? null,
          clean: true,
          reset_succeeded: true,
          error: null,
        };
      } else {
        try {
          observation = await this.dependencies.resetAndObserveCheckout(
            checkoutPath,
            request.intended_main_head.toLowerCase(),
          );
          if (
            !observation
            || typeof observation.checkout_path !== "string"
            || (observation.branch !== null && typeof observation.branch !== "string")
            || (observation.head !== null && typeof observation.head !== "string")
            || typeof observation.clean !== "boolean"
            || typeof observation.reset_succeeded !== "boolean"
          ) {
            throw new Error("checkout reset helper returned an invalid structured observation");
          }
        } catch (error) {
          observation = {
            checkout_path: checkoutPath,
            branch: null,
            head: null,
            clean: false,
            reset_succeeded: false,
            error: error instanceof Error ? error.message : String(error),
          };
        }
      }

      const observedHead = typeof observation.head === "string" ? observation.head.toLowerCase() : null;
      superseded.observed_main_head = observedHead;
      superseded.head_drift = observedHead !== null && observedHead !== request.intended_main_head.toLowerCase();

      const acknowledgement: NativeSlotReleaseAcknowledgement = {
        ...observation,
        slot: request.slot,
        assignment_epoch: claim.expected_epoch,
        expected_tuple: claim.expected_tuple,
      };

      let refusal: NativeSlotReleaseCode | null = null;
      const finalPaneCheckoutRaw = await this.dependencies.resolveOwningCheckout(request.slot);
      if (
        !finalPaneCheckoutRaw
        || resolve(finalPaneCheckoutRaw) !== checkoutPath
      ) refusal = "ack_checkout_mismatch";
      else if (resolve(observation.checkout_path) !== checkoutPath) refusal = "ack_checkout_mismatch";
      else if (!observation.reset_succeeded) refusal = "checkout_reset_failed";
      else if (!observation.clean) refusal = "dirty_checkout";
      else if (observation.branch !== "main") refusal = "wrong_branch";
      if (refusal) {
        return {
          ...result(
            refusal,
            `Checkout reset acknowledgement refused: ${refusal}.`,
            this.dependencies.db.getSlot(request.slot),
            "Leave the slot occupied, correct the checkout state, and retry from a fresh MoP read.",
          ),
          acknowledgement,
          superseded: withRepair(),
        };
      }

      const effectBinding = request.effect_id && request.request_digest
        ? { effect_id: request.effect_id, request_digest: request.request_digest, intended_main_head: request.intended_main_head.toLowerCase() }
        : undefined;
      const cleared = this.dependencies.db.commitNativeRelease(
        request.slot,
        claim.expected_epoch,
        claim.expected_tuple,
        effectBinding,
      );
      if (!cleared.ok) {
        const working = cleared.reason === "dnd_active"
          || cleared.reason === "active_turn"
          || cleared.reason === "productive_work";
        return {
          ...result(
            working ? "slot_not_idle" : "clear_conflict",
            `Release clear refused: ${cleared.reason ?? "unknown"}.`,
            this.dependencies.db.getSlot(request.slot),
            working
              ? "The slot started working again during the release; leave it occupied and retry once it is idle."
              : "Re-read MoP and retry the release; drift is superseded automatically.",
            false,
            working ? "state_moved" : undefined,
          ),
          acknowledgement,
          superseded: withRepair(),
        };
      }
      const readback = this.dependencies.db.getSlot(request.slot);
      if (
        !readback
        || readback.occupied
        || readback.assignment_epoch !== claim.expected_epoch + 1
        || slotAssignmentTuple(readback) !== null
      ) {
        return {
          ...result(
            "free_readback_failed",
            "Final MoP readback did not prove the exact FREE postcondition.",
            readback,
            "Stop; inspect MoP authority before any further slot mutation.",
          ),
          acknowledgement,
          superseded: withRepair(),
        };
      }
      if (superseded.epoch_drift || superseded.tuple_drift || superseded.head_drift || superseded.superseded_intent_id) {
        this.dependencies.db.logEvent(request.slot, "native_release_superseded", null, null, {
          presentation: describeSupersession(superseded),
          live_epoch: claim.expected_epoch,
          live_tuple: claim.expected_tuple,
          observed_main_head: superseded.observed_main_head,
        });
      }
      return {
        ...result("released", `Slot ${request.slot} released.`, readback, null, true),
        effect_id: request.effect_id,
        request_digest: computedDigest ?? request.request_digest,
        idempotent: false,
        acknowledgement,
        superseded: withRepair(),
        issue_projection: await this.projectReleasedOwner(claim.expected_tuple, request.slot),
      };
    } finally {
      if (releaseIntentToken) {
        this.dependencies.db.clearNativeReleaseIntent(
          request.slot,
          claim.expected_epoch,
          claim.expected_tuple,
          releaseIntentToken,
        );
      }
      this.inProgressSlots.delete(request.slot);
    }
  }
}
