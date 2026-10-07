/**
 * Interrupt-before-clear: a MoP clear must never be queued behind a live turn.
 *
 * Rajiv 2026-10-07 (C0ALZJHGE49 thread 1791348298.888079, ts 1791349855.166769):
 * "the interrupt was sent after the clear. it should be the other way round.
 * fix MoP to trigger interrupt automatically before clear."
 *
 * Incident: mop-assign-slot new_issue on S5 mid-turn sent `/clear` first; the
 * pane queued it ("Press up to edit queued messages"), the live turn kept
 * running (it opened PR #9024), and only then did the clear and the packet land.
 *
 * Order enforced here: if the slot has a live or indeterminate turn (pane
 * active OR active_turn_state != inactive), send Escape, then C-c if the pane
 * is still active, poll until the pane is idle, terminalize the MoP turn row
 * (an interrupt fires no Stop hook), and re-verify BOTH signals inactive.
 * Only then may the caller clear and deliver. If that cannot be verified
 * within the bound, fail closed with the typed reason `interrupt_unverified`.
 */

export type PaneActivity = "active" | "idle" | "unknown";

export interface TurnRow {
  active_turn_id: string | null;
  active_turn_state: string;
}

export interface InterruptBeforeClearDeps {
  readTurn: () => TurnRow | null;
  paneActivity: () => Promise<PaneActivity>;
  sendKey: (key: "Escape" | "C-c") => Promise<boolean>;
  /** Mark the MoP turn row inactive once the pane is verified idle. */
  terminalizeTurn: (prior: TurnRow) => void;
  /** Optional post-interrupt composer cleanup (restored prompt text). */
  afterInterrupt?: () => Promise<string>;
  log: (event: string, data: Record<string, unknown>) => void;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
  /** Total bound for the interrupt + verify, ms. */
  boundMs?: number;
  pollMs?: number;
}

export interface InterruptBeforeClearResult {
  ok: boolean;
  reason: "no_live_turn" | "interrupted_verified" | "interrupt_unverified";
  steps: string[];
  detail?: string;
}

export const INTERRUPT_BEFORE_CLEAR_BOUND_MS = 20_000;

function dbLive(row: TurnRow | null): boolean {
  return row !== null && (row.active_turn_id !== null || row.active_turn_state !== "inactive");
}

export async function interruptLiveTurnBeforeClear(
  deps: InterruptBeforeClearDeps,
): Promise<InterruptBeforeClearResult> {
  const boundMs = deps.boundMs ?? INTERRUPT_BEFORE_CLEAR_BOUND_MS;
  const pollMs = deps.pollMs ?? 250;
  const steps: string[] = [];
  const startRow = deps.readTurn();
  const startPane = await deps.paneActivity();
  if (!dbLive(startRow) && startPane !== "active") {
    return { ok: true, reason: "no_live_turn", steps };
  }
  const started = deps.now();
  deps.log("interrupt_before_clear_started", {
    pane: startPane,
    active_turn_id: startRow?.active_turn_id ?? null,
    active_turn_state: startRow?.active_turn_state ?? null,
  });

  const waitIdle = async (untilMs: number): Promise<boolean> => {
    for (;;) {
      if ((await deps.paneActivity()) === "idle") return true;
      if (deps.now() >= untilMs) return false;
      await deps.sleep(pollMs);
    }
  };

  let idle = false;
  for (const key of ["Escape", "C-c"] as const) {
    const sent = await deps.sendKey(key);
    steps.push(`${key}:${sent ? "sent" : "send_failed"}`);
    const until = key === "Escape" ? started + Math.floor(boundMs / 2) : started + boundMs;
    idle = await waitIdle(until);
    if (idle) break;
  }

  if (idle) {
    if (deps.afterInterrupt) {
      try {
        steps.push(`composer:${await deps.afterInterrupt()}`);
      } catch (error) {
        steps.push(`composer:threw:${String(error).slice(0, 80)}`);
      }
    }
    const row = deps.readTurn();
    if (row && dbLive(row)) {
      deps.terminalizeTurn(row);
      steps.push("turn_row_terminalized");
    }
  }

  const finalRow = deps.readTurn();
  const finalPane = await deps.paneActivity();
  const verified = idle && finalPane === "idle" && !dbLive(finalRow);
  const result: InterruptBeforeClearResult = verified
    ? { ok: true, reason: "interrupted_verified", steps }
    : {
        ok: false,
        reason: "interrupt_unverified",
        steps,
        detail: `pane=${finalPane};turn_state=${finalRow?.active_turn_state ?? "unknown"};elapsed_ms=${deps.now() - started}`,
      };
  deps.log(verified ? "interrupt_before_clear_verified" : "interrupt_before_clear_unverified", {
    steps,
    pane: finalPane,
    active_turn_state: finalRow?.active_turn_state ?? null,
    elapsed_ms: deps.now() - started,
  });
  return result;
}
