import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);

/** Acceptance discharges the PM's action; MoP owns subsequent Stop delivery. */
export async function resolveAcceptedPmSelfClear(
  delivery: string,
  requestedAt: string | null,
  execute: (file: string, args: string[], options: { timeout: number }) => Promise<unknown> = run,
): Promise<boolean> {
  if (!["sent", "deferred_busy", "pending"].includes(delivery)) return false;
  try {
    await execute("python3", [join(homedir(), ".claude/scripts/pm-ops.py"),
      "obligation-resolve", "--kind", "pm-self-clear", "--target-type", "session", "--target-id", "pm-dhruva",
      "--reason", `MoP accepted POST /slots/0/clear; requested_at=${requestedAt}; delivery=${delivery}; delivery owned by MoP at next native Stop (Rajiv 1793292676.746969)`],
    { timeout: 10_000 });
    return true;
  } catch (error) {
    console.error(`[pm-clear] obligation resolution failed: ${String(error)}`);
    return false;
  }
}
