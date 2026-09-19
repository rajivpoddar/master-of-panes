import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export type PaneCheckResult =
  | { state: "live"; paneId: string }
  | { state: "dead" }
  | { state: "unknown"; reason: string };

export type PaneCheckExecutor = (target: string) => Promise<PaneCheckResult>;

// Pinned tmux pane targets only: %id, $id, or session:window.pane.
// Mirrors the message-slot-tmux delivery helper, which pins the address and
// refuses drift. Anything else is rejected before it can reach a shell.
const PANE_TARGET_RE = /^(?:%[0-9]+|\$[0-9]+|[0-9]+:[0-9]+\.[0-9]+)$/;

export function isValidPaneTarget(target: unknown): target is string {
  return typeof target === "string" && PANE_TARGET_RE.test(target);
}

// Same slot->pane convention the server already uses for capture-pane reads.
export function defaultPaneTargetForSlot(slot: number): string {
  return `0:0.${slot}`;
}

async function realPaneCheck(target: string): Promise<PaneCheckResult> {
  let stdout: string;
  try {
    ({ stdout } = await execFileAsync(
      "tmux",
      ["display-message", "-p", "-t", target, "#{pane_id}|#{pane_dead}"],
      { timeout: 5_000 },
    ));
  } catch (error: unknown) {
    const code = (error as { code?: unknown })?.code;
    // tmux itself missing/un-runnable: the checker is down, not the pane.
    if (code === "ENOENT" || code === "EACCES") {
      return { state: "unknown", reason: "tmux_unavailable" };
    }
    if (code === "ETIMEDOUT" || (error as Error)?.message?.includes("timed out")) {
      return { state: "unknown", reason: "pane_check_timeout" };
    }
    // Nonzero tmux exit (e.g. "can't find pane"): target not live.
    return { state: "dead" };
  }
  const [paneId, dead] = stdout.trim().split("|");
  if (!paneId || (dead !== "0" && dead !== "1")) {
    return { state: "unknown", reason: "pane_check_unparseable" };
  }
  return dead === "0" ? { state: "live", paneId } : { state: "dead" };
}

let overrideExecutor: PaneCheckExecutor | null = null;

// Test seam only. Production always uses the real tmux check.
export function setPaneCheckExecutor(executor: PaneCheckExecutor | null): void {
  overrideExecutor = executor;
}

export async function verifySessionPane(target: string): Promise<PaneCheckResult> {
  if (!isValidPaneTarget(target)) {
    return { state: "unknown", reason: "invalid_pane_target" };
  }
  return (overrideExecutor ?? realPaneCheck)(target);
}
