/**
 * Idle-gated session clear for the new_issue assignment boundary.
 *
 * Slow slots (e.g. DGX Spark) queue pane input and process it only when idle.
 * Sending `/clear` and the task packet back-to-back lets the packet land
 * before the clear takes effect (and get wiped) or lets the clear run after
 * the packet. The sequence here is therefore strictly:
 *
 *   1. wait until the slot is idle (bounded);
 *   2. record the newest event id as the clear marker, then send `/clear`;
 *   3. wait for a SessionStart source=clear event newer than the marker AND
 *      an idle slot (bounded);
 *
 * Only then may the caller deliver the packet. A timeout returns a typed
 * `step: "clear_wait"` failure, never a false success.
 */

export type ClearWaitActivity = "active" | "idle" | "unknown";

export interface ClearWaitDependencies {
  getActivity: (slot: number) => Promise<ClearWaitActivity>;
  /** Highest event id currently logged for the slot (0 when none). */
  latestEventId: (slot: number) => number;
  /** Id of a SessionStart source=clear event for the slot newer than `afterId`, or null. */
  findClearAck: (slot: number, afterId: number) => number | null;
  sendClear: (slot: number) => Promise<{ ok: boolean; detail?: string }>;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
}

export interface ClearWaitOptions {
  idleTimeoutMs: number;
  ackTimeoutMs: number;
  pollMs: number;
}

export type ClearWaitResult =
  | { ok: true; reason: "cleared"; clear_marker_event_id: number; clear_ack_event_id: number; waited_ms: number }
  | { ok: false; step: "clear_wait"; reason: string; detail?: string }
  | { ok: false; step: "clear_send"; reason: "assignment_clear_not_applied"; detail?: string };

export function clearWaitOptionsFromEnv(env: NodeJS.ProcessEnv = process.env): ClearWaitOptions {
  const read = (name: string, fallback: number): number => {
    const value = Number(env[name]);
    return Number.isFinite(value) && value > 0 ? value : fallback;
  };
  return {
    idleTimeoutMs: read("MOP_ASSIGN_CLEAR_IDLE_TIMEOUT_MS", 240_000),
    ackTimeoutMs: read("MOP_ASSIGN_CLEAR_ACK_TIMEOUT_MS", 180_000),
    pollMs: read("MOP_ASSIGN_CLEAR_POLL_MS", 1_000),
  };
}

async function waitUntilIdle(
  slot: number,
  deps: ClearWaitDependencies,
  deadline: number,
  pollMs: number,
): Promise<boolean> {
  for (;;) {
    if ((await deps.getActivity(slot)) === "idle") return true;
    if (deps.now() >= deadline) return false;
    await deps.sleep(pollMs);
  }
}

export async function clearSlotWhenIdle(
  slot: number,
  deps: ClearWaitDependencies,
  options: ClearWaitOptions,
): Promise<ClearWaitResult> {
  const started = deps.now();
  if (!(await waitUntilIdle(slot, deps, started + options.idleTimeoutMs, options.pollMs))) {
    return { ok: false, step: "clear_wait", reason: "clear_wait_timeout:slot_not_idle_before_clear" };
  }

  const marker = deps.latestEventId(slot);
  const sent = await deps.sendClear(slot);
  if (!sent.ok) {
    return { ok: false, step: "clear_send", reason: "assignment_clear_not_applied", detail: sent.detail };
  }

  const ackDeadline = deps.now() + options.ackTimeoutMs;
  let ackId: number | null = null;
  for (;;) {
    ackId = deps.findClearAck(slot, marker);
    if (ackId !== null) break;
    if (deps.now() >= ackDeadline) {
      return { ok: false, step: "clear_wait", reason: "clear_wait_timeout:no_session_start_clear" };
    }
    await deps.sleep(options.pollMs);
  }
  if (!(await waitUntilIdle(slot, deps, ackDeadline, options.pollMs))) {
    return { ok: false, step: "clear_wait", reason: "clear_wait_timeout:slot_not_idle_after_clear" };
  }
  return {
    ok: true,
    reason: "cleared",
    clear_marker_event_id: marker,
    clear_ack_event_id: ackId,
    waited_ms: deps.now() - started,
  };
}
