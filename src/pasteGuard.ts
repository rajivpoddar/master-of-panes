/**
 * A pin enforced INSIDE the serialized PM writer.
 *
 * A keyed send records its recipient (immutable pane id + session token) when custody
 * begins, but the handler and relay used to target "the current PM" afterwards, so a
 * replacement during those awaits could receive text recorded under the old recipient.
 * The relay now pastes to `paneId` (never the mutable pane address) and calls
 * `beforePaste` after load-buffer and immediately before paste-buffer, in the writer's
 * serialization order. A refusal there means nothing was written.
 *
 * The paste is followed by a dwell and then Enter, so the pin is enforced a second time,
 * immediately before Enter, in the same writer (`beforeSubmit`). A refusal there happens
 * AFTER a paste may have landed: no further key is sent and the outcome is never zero-effect.
 * The guard only ever stops this one write to a mismatched recipient; it never queues, holds
 * or delays any other input.
 */
export interface PasteGuard {
  /** Immutable tmux pane id (for example "%12") the paste and Enter must target. */
  readonly paneId: string;
  /**
   * `text` is exactly what will be pasted. Refuse by returning `{ ok: false }`. The verdict must
   * reflect the recipient AFTER any asynchronous preparation the guard does, not before it.
   */
  beforePaste(text: string): Promise<{ ok: true } | { ok: false; reason: string }>;
  /**
   * Called after the paste and its dwell, immediately before Enter. `{ ok: false }` (or a throw)
   * means send NO further key: the paste may already be in the pane, so the caller keeps the
   * send UNKNOWN and never resends it.
   */
  beforeSubmit(): Promise<{ ok: true } | { ok: false; reason: string }>;
}
