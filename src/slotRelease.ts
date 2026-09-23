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
  // Thin PM surface (Rajiv directive 2026-09-23 15:57): only the slot
  // number is required. Presented identity is advisory; the live row wins.
  expected_epoch?: number;
  expected_tuple?: AssignmentTupleInput;
  intended_main_head?: string;
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
  /** Everything this release superseded instead of refusing. */
  superseded?: ReleaseSupersession;
}

export interface NativeSlotReleaseDependencies {
  db: MoPDatabase;
  /** Canonical issue-side ownership projection; optional so tests stay hermetic. */
  issueProjection?: IssueOwnershipProjection;
  resolveOwningCheckout: (slot: number) => Promise<string | null>;
  /**
   * Best-effort interrupt of any live turn in the pane (Ctrl-C through the
   * existing relay seam). The outcome is audited and never refuses.
   */
  interruptTurn: (slot: number) => Promise<{ ok: boolean; reason: string }>;
  observeCheckout: (checkoutPath: string) => Promise<CheckoutReadOnlyObservation>;
}

function result(
  code: NativeSlotReleaseCode,
  message: string,
  slot: SlotState | null | undefined,
  remediation: string | null,
  success = false,
): NativeSlotReleaseResult {
  return {
    success,
    code,
    message,
    slot: slot ?? null,
    assignment_epoch: slot?.assignment_epoch ?? null,
    remediation,
  };
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
    // The machine-internal effect path keeps strict binding: epoch and head
    // are required here even though the operator surface treats them as
    // advisory.
    if (
      !Number.isInteger(request.expected_epoch)
      || typeof request.intended_main_head !== "string"
      || !/^[0-9a-f]{40}$/i.test(request.intended_main_head)
    ) {
      return {
        ...result(
          "effect_receipt_malformed",
          "Family-2 release effect is missing its immutable epoch/head binding.",
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
        expected_epoch: request.expected_epoch as number,
        expected_tuple: request.expected_tuple as AssignmentTupleInput,
        intended_main_head: request.intended_main_head as string,
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
    const presentedTuple = normalizeAssignmentTuple(request.expected_tuple ?? {
      repository_id: null,
      issue: null,
      pr: null,
      branch: null,
      head_sha: null,
      work_kind: null,
      handoff_id: null,
      claimed_at: null,
    });
    if (
      !Number.isInteger(request.slot)
      || request.slot < 1
      || request.slot > DEFAULT_DEV_SLOT_COUNT
    ) {
      return result(
        "invalid_request",
        "A slot number (1-6) is required.",
        this.dependencies.db.getSlot(request.slot),
        "Resend the request with a valid slot number.",
      );
    }
    const current = this.dependencies.db.getSlot(request.slot);
    if (!current) {
      return result("slot_not_found", `Slot ${request.slot} does not exist.`, null, "Re-read MoP slot inventory.");
    }

    // Drift between the presented identity and MoP's live row is superseded,
    // never refused: the callers here are PM/CTO on one local machine, and the
    // live row is authoritative. The operator release itself never refuses on
    // state: it interrupts, terminalizes, frees, and audits.
    const drift = (live: SlotState | null): ReleaseSupersession => ({
      presented_epoch: Number.isInteger(request.expected_epoch)
        ? (request.expected_epoch as number)
        : (live?.assignment_epoch ?? 0),
      live_epoch: live?.assignment_epoch ?? null,
      epoch_drift: Number.isInteger(request.expected_epoch)
        && live?.assignment_epoch !== request.expected_epoch,
      presented_identity_usable: presentedTuple !== null,
      tuple_drift: presentedTuple === null
        || !assignmentTupleMatches(live ? slotAssignmentTuple(live) : null, presentedTuple),
      intended_main_head: typeof request.intended_main_head === "string"
        ? request.intended_main_head.toLowerCase()
        : "",
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
    let effectBinding: {
      effect_id: string;
      request_digest: string;
      expected_epoch: number;
      expected_tuple: AssignmentTupleInput;
      intended_main_head: string;
    } | undefined;
    if (request.effect_id !== undefined) {
      // The machine-internal effect path keeps strict binding: it needs the
      // complete immutable identity, unlike the thin operator surface.
      if (
        presentedTuple === null
        || !Number.isInteger(request.expected_epoch)
        || typeof request.intended_main_head !== "string"
        || !/^[0-9a-f]{40}$/i.test(request.intended_main_head)
      ) {
        return result(
          "invalid_request",
          "A committed Family-2 effect requires its complete immutable tuple.",
          current,
          "Resend the exact committed outbox tuple.",
        );
      }
      const effectEpoch = request.expected_epoch as number;
      const effectTuple = request.expected_tuple as AssignmentTupleInput;
      const effectHead = request.intended_main_head as string;
      try {
        computedDigest = computeFamily2ReleaseDigest({
          effect_id: request.effect_id,
          expected_epoch: effectEpoch,
          expected_tuple: effectTuple,
          intended_main_head: effectHead,
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
      effectBinding = {
        effect_id: request.effect_id,
        request_digest: computedDigest,
        expected_epoch: effectEpoch,
        expected_tuple: effectTuple,
        intended_main_head: effectHead,
      };
      if (!current.occupied) {
        return result(
          "slot_already_free_unverifiable",
          `Slot ${request.slot} is already FREE; the committed effect has no durable receipt to reconcile against.`,
          current,
          "Reconcile the committed outbox effect before retrying.",
        );
      }
      if (
        current.assignment_epoch !== effectEpoch
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

    // Simple release (Rajiv directive 2026-09-23 15:57): freeing a named
    // slot always succeeds. A live turn is interrupted and terminalized,
    // then the row is freed in one atomic write with a single audit row.
    // Nothing here waits on quiescence, refuses on state, or touches the
    // worktree.
    if (this.inProgressSlots.has(request.slot)) {
      return result(
        "release_in_progress",
        `Slot ${request.slot} already has an in-process native release request.`,
        this.dependencies.db.getSlot(request.slot),
        "Wait for that synchronous request to finish, then re-read MoP.",
      );
    }
    this.inProgressSlots.add(request.slot);
    try {
      const live = this.dependencies.db.getSlot(request.slot);
      if (!live) {
        return result("slot_not_found", `Slot ${request.slot} does not exist.`, null, "Re-read MoP slot inventory.");
      }
      // Best-effort interrupt of any live turn; audited, never refusing.
      let interrupt: { ok: boolean; reason: string } = { ok: true, reason: "no_live_turn" };
      if (live.active_turn_id !== null || live.active_turn_state !== "inactive") {
        try {
          interrupt = await this.dependencies.interruptTurn(request.slot);
        } catch (error) {
          interrupt = {
            ok: false,
            reason: `interrupt_threw:${error instanceof Error ? error.message.split("\n")[0].slice(0, 160) : String(error).slice(0, 160)}`,
          };
        }
      }
      // Read-only worktree observation for the audit row; never a reset.
      let worktree: { clean: boolean | null; detail: string } = { clean: null, detail: "unobserved" };
      try {
        const checkoutPathRaw = await this.dependencies.resolveOwningCheckout(request.slot);
        if (checkoutPathRaw) {
          const observed = await this.dependencies.observeCheckout(resolve(checkoutPathRaw));
          worktree = { clean: observed.clean, detail: observed.checkout_path };
        } else {
          worktree = { clean: null, detail: "checkout_identity_unavailable" };
        }
      } catch (error) {
        worktree = {
          clean: null,
          detail: `observe_threw:${error instanceof Error ? error.message.split("\n")[0].slice(0, 160) : String(error).slice(0, 160)}`,
        };
      }
      const freed = this.dependencies.db.releaseSlotSimple(request.slot, effectBinding);
      if (!freed.ok) {
        return result("slot_not_found", `Slot ${request.slot} does not exist.`, null, "Re-read MoP slot inventory.");
      }
      const prior = freed.predecessor ?? null;
      this.dependencies.db.logEvent(request.slot, "slot_released_simple", null, null, {
        prior: prior ? {
          issue: prior.issue,
          pr: prior.pr,
          assignment_epoch: live.assignment_epoch,
          active_turn_id: prior.active_turn_id,
          active_turn_state: prior.active_turn_state,
        } : null,
        interrupt,
        worktree,
        worktree_reset: false,
        assignment_epoch: freed.assignment_epoch,
        idempotent: freed.idempotent,
      });
      const readback = this.dependencies.db.getSlot(request.slot);
      return {
        ...result("released", `Slot ${request.slot} released.`, readback, null, true),
        effect_id: request.effect_id,
        request_digest: computedDigest ?? request.request_digest,
        idempotent: freed.idempotent,
        superseded: withRepair(),
        // A slot-only release carries no presented tuple; project the live
        // predecessor row instead so the freed lane's labels still unwind.
        // A presented (possibly drifted) tuple keeps its existing meaning.
        issue_projection: await this.projectReleasedOwner(
          presentedTuple ?? slotAssignmentTuple(live) ?? undefined,
          request.slot,
        ),
      };
    } finally {
      this.inProgressSlots.delete(request.slot);
    }
  }
}
