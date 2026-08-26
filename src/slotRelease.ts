import { resolve } from "node:path";

import {
  assignmentTupleMatches,
  normalizeAssignmentTuple,
  slotAssignmentTuple,
  type AssignmentTuple,
  type AssignmentTupleInput,
  type MoPDatabase,
} from "./db.js";
import type { SlotState } from "./types.js";

export interface NativeSlotReleaseRequest {
  slot: number;
  expected_epoch: number;
  expected_session_id: string;
  expected_tuple: AssignmentTupleInput;
  intended_main_head: string;
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
  session_id: string;
  assignment_epoch: number;
  expected_tuple: AssignmentTupleInput;
}

export type NativeSlotReleaseCode =
  | "released"
  | "invalid_request"
  | "slot_not_found"
  | "slot_already_free_unverifiable"
  | "epoch_mismatch"
  | "session_mismatch"
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
  | "clear_conflict"
  | "free_readback_failed";

export interface NativeSlotReleaseResult {
  success: boolean;
  code: NativeSlotReleaseCode;
  message: string;
  slot: SlotState | null;
  assignment_epoch: number | null;
  remediation: string | null;
  acknowledgement?: NativeSlotReleaseAcknowledgement;
}

interface NormalizedReleaseRequest extends NativeSlotReleaseRequest {
  expected_session_id: string;
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

  private validateInitialRequest(
    request: NativeSlotReleaseRequest,
  ): { request: NormalizedReleaseRequest; tuple: AssignmentTuple } | NativeSlotReleaseResult {
    const current = this.dependencies.db.getSlot(request.slot);
    const tuple = normalizeAssignmentTuple(request.expected_tuple);
    if (
      !Number.isInteger(request.slot)
      || request.slot < 1
      || request.slot > 4
      || !Number.isInteger(request.expected_epoch)
      || typeof request.expected_session_id !== "string"
      || request.expected_session_id.trim() === ""
      || !/^[0-9a-f]{40}$/i.test(request.intended_main_head)
      || !tuple
    ) {
      return result(
        "invalid_request",
        "A complete release tuple, session, epoch, and intended main head are required.",
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
    if (current.session_id !== request.expected_session_id) {
      return result("session_mismatch", "Owning session changed before release delivery.", current, "Re-read MoP and retry with the current owning session.");
    }
    if (!assignmentTupleMatches(slotAssignmentTuple(current), tuple)) {
      return result("observed_tuple_mismatch", "Complete owner tuple changed before release delivery.", current, "Re-read MoP and retry with the current tuple.");
    }
    return {
      request: {
        ...request,
        expected_session_id: request.expected_session_id.trim(),
        intended_main_head: request.intended_main_head.toLowerCase(),
      },
      tuple,
    };
  }

  async release(request: NativeSlotReleaseRequest): Promise<NativeSlotReleaseResult> {
    const validated = this.validateInitialRequest(request);
    if ("success" in validated) return validated;
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
        session_id: validated.request.expected_session_id,
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
        validated.request.expected_session_id,
        validated.request.expected_tuple,
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
        || readback.session_id !== null
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
        acknowledgement,
      };
    } finally {
      this.inProgressSlots.delete(request.slot);
    }
  }
}
