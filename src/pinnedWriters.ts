/**
 * Pinned writers for interrupt-before-clear (CTO REVISE on d15f074,
 * C0ALZJHGE49 thread 1791348298.888079, ts 1791352881.985519): the operation
 * pin is carried INTO the cleanup, clear and task-delivery writers and is
 * enforced inside their per-slot send lock immediately before each
 * C-u / paste / Enter, after any waits. Every effect targets the pinned
 * immutable pane id, never a re-resolved slot address.
 */
import { composerText } from "./composer.js";
import type { GuardedSlotDelivery, TmuxRelay } from "./relay.js";
import { withSlotSendLock } from "./slotSendLock.js";

/** Synchronous fence: null when the operation still owns the slot. */
export type OperationFence = (stage: string) => string | null;

/**
 * One guarded send (the /clear command or the task packet) to the pinned
 * pane. Waits (bounded) for a readable empty composer, then fences; the
 * relay re-runs the sync fence immediately before the paste and the Enter.
 * requireIdle: the pinned pane must also read idle inside the lock (a new
 * turn refuses; it never gains interrupt authority).
 */
export async function pinnedGuardedSend(args: {
  relay: Pick<TmuxRelay, "deliverGuardedToSlot" | "getSlotActivityState">;
  slot: number;
  paneId: string;
  text: string;
  fence: OperationFence;
  requireIdle: boolean;
  bracketedPaste?: boolean;
  sleep: (ms: number) => Promise<void>;
  waitEmptyMs?: number;
  pollMs?: number;
}): Promise<GuardedSlotDelivery> {
  const waitEmptyMs = args.waitEmptyMs ?? 10_000;
  const pollMs = args.pollMs ?? 250;
  return args.relay.deliverGuardedToSlot(args.slot, args.text, {
    expectedPaneId: args.paneId,
    bracketedPaste: args.bracketedPaste,
    recheck: async (stage, capture) => {
      if (stage === "pre_paste") {
        for (let waited = 0; ; waited += pollMs) {
          const early = args.fence(stage);
          if (early) return early;
          if (composerText(await capture()) === "") break;
          if (waited >= waitEmptyMs) return "composer_not_ready";
          await args.sleep(pollMs);
        }
        if (args.requireIdle && (await args.relay.getSlotActivityState(args.slot, args.paneId)) !== "idle") {
          return args.fence(stage) ?? "pane_not_idle";
        }
      }
      return args.fence(stage);
    },
    finalCheck: (stage) => args.fence(stage),
  });
}

export type ComposerCleanupResult =
  | "empty" | "cleared" | "unreadable" | "mode_refused" | "not_cleared" | `refused:${string}`;

/**
 * Claude Code restores an interrupted turn's prompt into the composer; clear
 * it with C-u. Each C-u runs inside the slot send lock, after the capture,
 * with the fence evaluated immediately before the key. Bounded; never Enter.
 */
export async function pinnedComposerCleanup(args: {
  slot: number;
  capture: () => Promise<string | null>;
  modeRefused: () => Promise<boolean>;
  sendCtrlU: () => Promise<void>;
  fence: OperationFence;
  sleep: (ms: number) => Promise<void>;
}): Promise<ComposerCleanupResult> {
  let sawText = false;
  for (let i = 0; i < 12; i += 1) {
    await args.sleep(250);
    const step = await withSlotSendLock(args.slot, async (): Promise<ComposerCleanupResult | "continue"> => {
      const composer = composerText(await args.capture());
      if (composer === null) return sawText ? "not_cleared" : "unreadable";
      if (composer === "") {
        if (sawText) return "cleared";
        return i >= 5 ? "empty" : "continue";
      }
      sawText = true;
      if (await args.modeRefused()) return "mode_refused";
      const refusal = args.fence("pre_ctrl_u");
      if (refusal) return `refused:${refusal}`;
      await args.sendCtrlU();
      return "continue";
    });
    if (step !== "continue") return step;
  }
  return sawText ? "not_cleared" : "empty";
}
