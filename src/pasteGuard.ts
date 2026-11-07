/**
 * A pin enforced INSIDE the serialized PM writer.
 *
 * A keyed send records its recipient (immutable pane id + session token) when custody
 * begins, but the handler and relay used to target "the current PM" afterwards, so a
 * replacement during those awaits could receive text recorded under the old recipient.
 * The relay now pastes to `paneId` (never the mutable pane address) and calls
 * `beforePaste` after load-buffer and immediately before paste-buffer, in the writer's
 * serialization order. A refusal there means nothing was written.
 */
export interface PasteGuard {
  /** Immutable tmux pane id (for example "%12") the paste and Enter must target. */
  readonly paneId: string;
  /** `text` is exactly what will be pasted. Refuse by returning `{ ok: false }`. */
  beforePaste(text: string): Promise<{ ok: true } | { ok: false; reason: string }>;
}
