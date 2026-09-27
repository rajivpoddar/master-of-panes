export type PaneInputModeRefusalReason = "pane_in_copy_mode" | "pane_mode_unreadable";

/** Fail closed unless tmux reports the pane is in its normal input mode. */
export function paneInputModeRefusalReason(mode: string | null | undefined): PaneInputModeRefusalReason | null {
  const value = mode?.trim();
  if (value === "0") return null;
  if (value === "1") return "pane_in_copy_mode";
  return "pane_mode_unreadable";
}
