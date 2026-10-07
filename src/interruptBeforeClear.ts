/**
 * Interrupt-before-clear: a MoP clear must never be queued behind a live turn.
 *
 * Rajiv 2026-10-07 (C0ALZJHGE49 thread 1791348298.888079, ts 1791349855.166769):
 * "fix MoP to trigger interrupt automatically before clear."
 *
 * CTO REVISE on d85ede4 (same thread, ts 1791351726.554489):
 * 1. One operation authority: the pane id, checkout, hook session, assignment
 *    epoch + owner tuple and turn are pinned at the start and re-checked after
 *    every await and before every effect. Terminalization is a CAS on the
 *    ORIGINAL turn only.
 * 2. Unknown is not idle: no_live_turn needs a readable, same-pane idle prompt
 *    AND a present, freshly read inactive turn row. Unknown or missing
 *    evidence refuses with no effect.
 * 3. Raw keys are single-attempt. The possible effect is recorded before the
 *    send; an uncertain send returns uncertainty and is never replayed.
 *    Escape -> C-c escalation happens only against the unchanged bound turn.
 */

export type PaneActivity = "active" | "idle" | "unknown";

export interface SlotRowLike {
  occupied: boolean;
  repository_id: string | null;
  issue: number | null;
  pr: number | null;
  handoff_id: string | null;
  branch: string | null;
  assignment_epoch: number;
  session_id: string | null;
  active_turn_id: string | null;
  active_turn_state: string;
}

export interface PaneObservation {
  paneId: string;
  checkout: string;
}

export interface OperationPin {
  paneId: string;
  checkout: string;
  sessionId: string | null;
  epoch: number;
  owner: string;
  turnId: string | null;
  turnState: string;
}

export function ownerKey(row: SlotRowLike): string {
  return JSON.stringify([row.occupied, row.repository_id, row.issue, row.pr, row.handoff_id, row.branch]);
}

export function pinFrom(pane: PaneObservation, row: SlotRowLike): OperationPin {
  return {
    paneId: pane.paneId,
    checkout: pane.checkout,
    sessionId: row.session_id,
    epoch: row.assignment_epoch,
    owner: ownerKey(row),
    turnId: row.active_turn_id,
    turnState: row.active_turn_state,
  };
}

/**
 * Null when the live row (and pane, when given) still match the pin; else a
 * typed drift reason. Synchronous on the row so callers can run it with no
 * await before an effect.
 */
export function pinDrift(
  pin: OperationPin,
  row: SlotRowLike | null,
  pane?: PaneObservation | null,
  opts: { allowSessionChange?: boolean } = {},
): string | null {
  if (!row) return "drift:row_missing";
  if (pane === null) return "drift:pane_unobservable";
  if (pane && pane.paneId !== pin.paneId) return "drift:pane";
  if (pane && pane.checkout !== pin.checkout) return "drift:checkout";
  if (row.assignment_epoch !== pin.epoch) return "drift:epoch";
  if (ownerKey(row) !== pin.owner) return "drift:owner";
  if (!opts.allowSessionChange && row.session_id !== pin.sessionId) return "drift:session";
  if (row.active_turn_id !== pin.turnId || row.active_turn_state !== pin.turnState) return "drift:turn";
  return null;
}

export type KeySendOutcome = "sent" | "uncertain";

export interface InterruptBeforeClearDeps {
  observePane: () => Promise<PaneObservation | null>;
  readRow: () => SlotRowLike | null;
  /** Activity of the PINNED pane id (never a re-resolved numeric address). */
  paneActivity: (paneId: string) => Promise<PaneActivity>;
  /** Called synchronously immediately before a key send; durable audit. */
  recordPossibleEffect: (key: "Escape" | "C-c", pin: OperationPin) => void;
  /** Exactly one send attempt to the pinned pane id. Any error = uncertain. */
  sendKeyOnce: (paneId: string, key: "Escape" | "C-c") => Promise<KeySendOutcome>;
  /**
   * Synchronous CAS: mark the turn inactive ONLY if the row still carries
   * the pinned turn, epoch, owner and session. Returns whether it wrote.
   */
  casTerminalize: (pin: OperationPin) => boolean;
  /** Optional composer cleanup on the pinned pane after the turn stopped. */
  afterInterrupt?: (paneId: string) => Promise<string>;
  log: (event: string, data: Record<string, unknown>) => void;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
  boundMs?: number;
  pollMs?: number;
}

export type InterruptReason =
  | "no_live_turn"
  | "interrupted_verified"
  | "interrupt_unverified"
  | "interrupt_uncertain"
  | "evidence_unknown"
  | "evidence_missing"
  | "operation_drift";

export interface InterruptBeforeClearResult {
  ok: boolean;
  reason: InterruptReason;
  steps: string[];
  detail?: string;
  /** Present only on ok: the fence for later clear/commit/delivery effects. */
  pin?: OperationPin;
}

export const INTERRUPT_BEFORE_CLEAR_BOUND_MS = 20_000;

function rowInactive(row: SlotRowLike): boolean {
  return row.active_turn_id === null && row.active_turn_state === "inactive";
}

export async function interruptLiveTurnBeforeClear(
  deps: InterruptBeforeClearDeps,
): Promise<InterruptBeforeClearResult> {
  const boundMs = deps.boundMs ?? INTERRUPT_BEFORE_CLEAR_BOUND_MS;
  const pollMs = deps.pollMs ?? 250;
  const steps: string[] = [];
  const fail = (reason: InterruptReason, detail: string): InterruptBeforeClearResult => {
    deps.log("interrupt_before_clear_refused", { reason, detail, steps });
    return { ok: false, reason, steps, detail };
  };

  const pane = await deps.observePane();
  const startRow = deps.readRow();
  if (!pane) return fail("evidence_missing", "pane_identity_unavailable");
  if (!startRow) return fail("evidence_missing", "turn_row_missing");
  let pin = pinFrom(pane, startRow);

  const startActivity = await deps.paneActivity(pin.paneId);
  let drift = pinDrift(pin, deps.readRow());
  if (drift) return fail("operation_drift", `${drift}@observe`);
  if (startActivity === "unknown") return fail("evidence_unknown", "pane_activity_unknown");
  if (startActivity === "idle" && rowInactive(startRow)) {
    return { ok: true, reason: "no_live_turn", steps, pin };
  }

  const started = deps.now();
  deps.log("interrupt_before_clear_started", { pane: startActivity, pin });

  /** Bound turn may only move to "stopped" (Stop hook) — never to another turn. */
  const boundTurnDrift = (stage: string): string | null => {
    const row = deps.readRow();
    if (row && row.active_turn_id === null && row.active_turn_state === "inactive") {
      const rest = pinDrift({ ...pin, turnId: null, turnState: "inactive" }, row);
      return rest ? `${rest}@${stage}` : null;
    }
    const d = pinDrift(pin, row);
    return d ? `${d}@${stage}` : null;
  };

  let idle = false;
  for (const key of ["Escape", "C-c"] as const) {
    const observed = await deps.observePane();
    drift = boundTurnDrift(`pre_${key}`);
    if (!observed) return fail("operation_drift", `drift:pane_unobservable@pre_${key}`);
    if (observed.paneId !== pin.paneId || observed.checkout !== pin.checkout) {
      return fail("operation_drift", `drift:pane@pre_${key}`);
    }
    if (drift) return fail("operation_drift", drift);
    deps.recordPossibleEffect(key, pin);
    let sent: KeySendOutcome;
    try {
      sent = await deps.sendKeyOnce(pin.paneId, key);
    } catch {
      sent = "uncertain";
    }
    steps.push(`${key}:${sent}`);
    if (sent !== "sent") return fail("interrupt_uncertain", `${key}_send_uncertain`);
    const until = key === "Escape" ? started + Math.floor(boundMs / 2) : started + boundMs;
    for (;;) {
      const activity = await deps.paneActivity(pin.paneId);
      drift = boundTurnDrift(`poll_${key}`);
      if (drift) return fail("operation_drift", drift);
      if (activity === "idle") { idle = true; break; }
      if (deps.now() >= until) break;
      await deps.sleep(pollMs);
    }
    if (idle) break;
  }
  if (!idle) {
    return fail("interrupt_unverified", `pane_not_idle_within_${boundMs}ms`);
  }

  if (deps.afterInterrupt) {
    try {
      steps.push(`composer:${await deps.afterInterrupt(pin.paneId)}`);
    } catch (error) {
      steps.push(`composer:threw:${String(error).slice(0, 80)}`);
    }
    drift = boundTurnDrift("post_cleanup");
    if (drift) return fail("operation_drift", drift);
  }

  const row = deps.readRow();
  if (row && !rowInactive(row)) {
    if (!deps.casTerminalize(pin)) return fail("operation_drift", "drift:turn@cas");
    steps.push("turn_row_terminalized");
  }

  const finalPane = await deps.observePane();
  const finalActivity = await deps.paneActivity(pin.paneId);
  const finalRow = deps.readRow();
  pin = { ...pin, turnId: null, turnState: "inactive" };
  drift = pinDrift(pin, finalRow, finalPane);
  if (drift) return fail("operation_drift", `${drift}@final`);
  if (finalActivity !== "idle") return fail("interrupt_unverified", `final_pane_${finalActivity}`);
  deps.log("interrupt_before_clear_verified", { steps, elapsed_ms: deps.now() - started });
  return { ok: true, reason: "interrupted_verified", steps, pin };
}
