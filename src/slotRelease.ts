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

export function buildLiteralResetInstruction(
  request: NormalizedReleaseRequest,
  checkoutPath: string,
): string {
  return [
    "Stop work on the current assignment now and remain idle; do not run another tool.",
    `Master of Panes is synchronously switching your owning checkout ${checkoutPath} to branch main, pulling origin/main, and requiring exact head ${request.intended_main_head} with a clean worktree before it clears your slot.`,
    "If any reset or attestation step fails, the slot remains occupied.",
  ].join("\n");
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

      if (!checkoutSettled) {
        const instruction = buildLiteralResetInstruction(
          { ...request, intended_main_head: request.intended_main_head.toLowerCase() },
          checkoutPath,
        );
        const delivered = await this.dependencies.deliverInstruction(request.slot, instruction);
        if (!delivered) {
          return result(
            "delivery_failed",
            "The owning slot did not receive the stop/reset instruction.",
            this.dependencies.db.getSlot(request.slot),
            "Leave the slot occupied, repair delivery, and retry from a fresh MoP read.",
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
