import {
  pinDrift,
  type OperationPin,
  type PaneActivity,
  type PaneObservation,
  type SlotRowLike,
} from "./interruptBeforeClear.js";

export interface ComposerClearFenceDeps {
  pin: OperationPin;
  /** force may bypass activity and turn changes, never generation identity. */
  force: boolean;
  observePane: () => Promise<PaneObservation | null>;
  readRow: () => SlotRowLike | null;
  /** Activity of the PINNED pane id. */
  activity: () => Promise<PaneActivity>;
}

/**
 * Generation fence for an explicit composer clear. Null when the pinned
 * operation (pane id, checkout, hook session, assignment epoch, owner) still
 * matches the live slot; else a typed refusal reason. Without force the slot
 * must also still be idle with the pinned turn state, so idle->active between
 * presses refuses. Run it inside the send lock before EVERY C-u.
 */
export async function composerClearFence(deps: ComposerClearFenceDeps, stage: string): Promise<string | null> {
  const pane = await deps.observePane();
  const live = deps.readRow();
  if (!deps.force) {
    const drift = pinDrift(deps.pin, live, pane);
    if (drift) return `${drift}@${stage}`;
    const activity = await deps.activity();
    if (activity !== "idle") return `drift:activity_${activity}@${stage}`;
    // The activity probe awaited; re-read the row so a change during it is caught.
    const late = pinDrift(deps.pin, deps.readRow());
    return late ? `${late}@${stage}` : null;
  }
  // force: keep generation identity, ignore the turn fields.
  const masked = live
    ? { ...live, active_turn_id: deps.pin.turnId, active_turn_state: deps.pin.turnState }
    : live;
  const drift = pinDrift(deps.pin, masked, pane);
  if (drift) return `${drift}@${stage}`;
  const lateRow = deps.readRow();
  const lateMasked = lateRow
    ? { ...lateRow, active_turn_id: deps.pin.turnId, active_turn_state: deps.pin.turnState }
    : lateRow;
  const late = pinDrift(deps.pin, lateMasked);
  return late ? `${late}@${stage}` : null;
}

/**
 * SYNCHRONOUS row fence for the instant of key issuance. No await may sit
 * between a null return and the C-u it authorises. Without force the pinned
 * turn state must also be unchanged; force masks turn/activity only, never
 * epoch, owner or hook session (pane id and checkout are fenced by the async
 * check and by the immutable pane-id key target).
 */
export function composerClearRowFence(
  deps: Pick<ComposerClearFenceDeps, "pin" | "force" | "readRow">,
  stage: string,
): string | null {
  const live = deps.readRow();
  const row = live && deps.force
    ? { ...live, active_turn_id: deps.pin.turnId, active_turn_state: deps.pin.turnState }
    : live;
  const drift = pinDrift(deps.pin, row);
  return drift ? `${drift}@${stage}` : null;
}

/**
 * One C-u under the full ordering the CTO required: (1) read the pane input
 * mode FIRST (a tmux await), (2) THEN the async generation/activity fence,
 * (3) end with the synchronous row check with no await before the key.
 * Returns true after the key, false when the input mode forbids it, or a
 * typed drift reason (the generation moved at any point, including inside the
 * mode query) with zero keys sent.
 */
export function makeComposerClearKeyWriter(deps: ComposerClearFenceDeps & {
  inputModeRefusal: () => Promise<string | null>;
  sendKey: () => Promise<void>;
}): () => Promise<boolean | string> {
  return async () => {
    if (await deps.inputModeRefusal()) return false;
    const drift = await composerClearFence(deps, "clear_composer_key");
    if (drift) return drift;
    const final = composerClearRowFence(deps, "clear_composer_issue");
    if (final) return final;
    await deps.sendKey(); // sendKey() starts synchronously: no await since the row check
    return true;
  };
}
