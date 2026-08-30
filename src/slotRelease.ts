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
  | "checkout_not_clean";

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
}

interface NormalizedReleaseRequest extends NativeSlotReleaseRequest {
  intended_main_head: string;
}

export interface NativeSlotReleaseDependencies {
  db: MoPDatabase;
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
      };
    } finally {
      this.inProgressSlots.delete(request.slot);
    }
  }

  private validateInitialRequest(
    request: NativeSlotReleaseRequest,
  ): { request: NormalizedReleaseRequest; tuple: AssignmentTuple } | NativeSlotReleaseResult {
    const current = this.dependencies.db.getSlot(request.slot);
    const tuple = normalizeAssignmentTuple(request.expected_tuple);
    if (
      !Number.isInteger(request.slot)
      || request.slot < 1
      || request.slot > DEFAULT_DEV_SLOT_COUNT
      || !Number.isInteger(request.expected_epoch)
      || !/^[0-9a-f]{40}$/i.test(request.intended_main_head)
      || !tuple
    ) {
      return result(
        "invalid_request",
        "A complete release tuple, epoch, and intended main head are required.",
        current,
        "Re-read MoP and supply the exact current release inputs.",
      );
    }
    if (!current) {
      return result("slot_not_found", `Slot ${request.slot} does not exist.`, null, "Re-read MoP slot inventory.");
    }
    if (!current.occupied) {
      return result(
        "slot_already_free_unverifiable",
        `Slot ${request.slot} is already FREE; MoP no longer has the prior tuple to prove compatibility.`,
        current,
        "Treat this as a safe no-op drift result and re-read the caller's state.",
      );
    }
    if (current.assignment_epoch !== request.expected_epoch) {
      return result("epoch_mismatch", "Assignment epoch changed before release delivery.", current, "Re-read MoP and retry with fresh state.");
    }
    if (!assignmentTupleMatches(slotAssignmentTuple(current), tuple)) {
      return result("observed_tuple_mismatch", "Complete owner tuple changed before release delivery.", current, "Re-read MoP and retry with the current tuple.");
    }
    if (current.active_turn_id !== null || current.active_turn_state !== "inactive") {
      return result("slot_not_idle", "The owning hook turn is still active or indeterminate.", current, "Wait for the authoritative Stop or SessionEnd hook and retry.");
    }
    return {
      request: {
        ...request,
        intended_main_head: request.intended_main_head.toLowerCase(),
      },
      tuple,
    };
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
    const replay = this.replayDurableEffect(request);
    if (replay) return replay;
    const validated = this.validateInitialRequest(request);
    if ("success" in validated) return validated;
    let computedDigest: string | undefined;
    if (request.effect_id !== undefined) {
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
      if (computedDigest !== request.request_digest?.toLowerCase()) {
        return {
          ...result(
            "effect_digest_mismatch",
            "Family-2 release digest does not match the normalized ownership tuple.",
            this.dependencies.db.getSlot(request.slot),
            "Reject the effect before delivery, reset, or clear; recompute it from the committed tuple.",
          ),
          effect_id: request.effect_id,
          request_digest: request.request_digest,
        };
      }
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
    let claimedReleaseIntent = false;

    try {
      // Claim the exact owner/epoch before any pane instruction. StuckDetector
      // reads this durable short lease and suppresses only a matching stale
      // continuation while the release/reset sequence is in flight.
      if (!this.dependencies.db.claimNativeReleaseIntent(
        request.slot,
        validated.request.expected_epoch,
        validated.request.expected_tuple,
      )) {
        return result(
          "release_in_progress",
          `Slot ${request.slot} already has a conflicting or invalid release intent.`,
          this.dependencies.db.getSlot(request.slot),
          "Leave the owner untouched and retry after the current release intent expires or completes.",
        );
      }
      claimedReleaseIntent = true;
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
      const instruction = buildLiteralResetInstruction(validated.request, checkoutPath);
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
          "The owning slot did not prove it was idle after the stop instruction.",
          this.dependencies.db.getSlot(request.slot),
          "Leave the slot occupied and retry from a fresh read after the slot is idle.",
        );
      }

      // The pane activity probe can observe an idle prompt before the Stop
      // hook has finished its authoritative finishAgentTurn update. Re-read
      // the complete owner state before touching the checkout; otherwise a
      // successful reset can race the final CAS and leave the owner occupied
      // after the release turn has already terminalized.
      const postDelivery = this.dependencies.db.getSlot(request.slot);
      if (
        !postDelivery
        || postDelivery.assignment_epoch !== validated.request.expected_epoch
        || !assignmentTupleMatches(slotAssignmentTuple(postDelivery), validated.tuple)
        || postDelivery.active_turn_id !== null
        || postDelivery.active_turn_state !== "inactive"
      ) {
        return result(
          "slot_not_idle",
          "The owning slot activity probe raced the authoritative Stop-hook turn close.",
          postDelivery,
          "Leave the slot occupied and retry from a fresh exact owner read after the turn is inactive.",
        );
      }

      let observation: CheckoutResetObservation;
      try {
        observation = await this.dependencies.resetAndObserveCheckout(
          checkoutPath,
          validated.request.intended_main_head,
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
      const acknowledgement: NativeSlotReleaseAcknowledgement = {
        ...observation,
        slot: request.slot,
        assignment_epoch: validated.request.expected_epoch,
        expected_tuple: validated.request.expected_tuple,
      };

      let refusal: NativeSlotReleaseCode | null = null;
      const finalPaneCheckoutRaw = await this.dependencies.resolveOwningCheckout(request.slot);
      if (
        !finalPaneCheckoutRaw
        || resolve(finalPaneCheckoutRaw) !== checkoutPath
        || resolve(observation.checkout_path) !== checkoutPath
      ) refusal = "ack_checkout_mismatch";
      else if (!observation.reset_succeeded) refusal = "checkout_reset_failed";
      else if (!observation.clean) refusal = "dirty_checkout";
      else if (observation.branch !== "main") refusal = "wrong_branch";
      else if (observation.head?.toLowerCase() !== validated.request.intended_main_head) refusal = "wrong_head";
      if (refusal) {
        return {
          ...result(
            refusal,
            `Checkout reset acknowledgement refused: ${refusal}.`,
            this.dependencies.db.getSlot(request.slot),
            "Leave the slot occupied, correct the checkout state, and retry from a fresh MoP read.",
          ),
          acknowledgement,
        };
      }

      const cleared = this.dependencies.db.commitNativeRelease(
        request.slot,
        validated.request.expected_epoch,
        validated.request.expected_tuple,
        request.effect_id && request.request_digest
          ? {
              effect_id: request.effect_id,
              request_digest: request.request_digest,
              intended_main_head: validated.request.intended_main_head,
            }
          : undefined,
      );
      if (!cleared.ok) {
        return {
          ...result(
            "clear_conflict",
            `Final complete-tuple clear refused: ${cleared.reason ?? "unknown"}.`,
            this.dependencies.db.getSlot(request.slot),
            "Leave the current owner untouched and retry only after a fresh MoP read.",
          ),
          acknowledgement,
        };
      }
      const readback = this.dependencies.db.getSlot(request.slot);
      if (
        !readback
        || readback.occupied
        || readback.assignment_epoch !== validated.request.expected_epoch + 1
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
        };
      }
      return {
        ...result("released", `Slot ${request.slot} reset and released.`, readback, null, true),
        effect_id: request.effect_id,
        request_digest: computedDigest ?? request.request_digest,
        idempotent: false,
        acknowledgement,
      };
    } finally {
      if (claimedReleaseIntent) {
        this.dependencies.db.clearNativeReleaseIntent(
          request.slot,
          validated.request.expected_epoch,
          validated.request.expected_tuple,
        );
      }
      this.inProgressSlots.delete(request.slot);
    }
  }
}
