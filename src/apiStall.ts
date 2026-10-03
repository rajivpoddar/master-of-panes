/**
 * Transient model-API stall detection + nudge policy (pure, unit-tested).
 *
 * Rajiv 2026-10-03 23:19 IST (C0ALZJHGE49 / 1791049473.052849):
 * "Can mop detect these stalls and nudge the slots?"
 *
 * A slot stalls when its turn ENDS on a transient provider failure (stream
 * disconnect, overload, 5xx/529, connection reset) and then sits idle at the
 * prompt. Claude Code's own "Waiting for API response · will retry in Nm"
 * banner is an in-flight retry, NOT a stall.
 */

export const API_STALL_NUDGE_MESSAGE =
  "Your last turn ended on a transient model API error. Resume your current " +
  "assignment from where you stopped; commit and push progress first.";

export const API_STALL_GRACE_MS = 75_000;
export const API_STALL_RESTALL_WINDOW_MS = 10 * 60_000;
export const API_STALL_MAX_CONSECUTIVE_NUDGES = 3;

/** Explicit transient-failure signatures, matched only on an `API Error:` line. */
export const API_STALL_SIGNATURES: ReadonlyArray<{ name: string; pattern: RegExp }> = [
  { name: "stream_disconnected", pattern: /stream (?:error|disconnected|closed before)/i },
  { name: "servers_overloaded", pattern: /overloaded/i },
  { name: "http_5xx", pattern: /API Error:\s*(?:\(?\s*)?5\d\d\b/ },
  { name: "internal_server_error", pattern: /internal server error|server_error|bad gateway|service unavailable|gateway time-?out/i },
  { name: "connection_reset", pattern: /ECONNRESET|connection reset|socket hang up|connection error|ETIMEDOUT|EPIPE|premature close/i },
];

/** Claude Code's own retry banner: the turn is still alive. */
export const API_RETRY_BANNER = /Waiting for API response|will retry in|Retrying in \d/i;

/** A turn-ending API error line (the assistant bullet line, not a ⎿ tool result). */
const API_ERROR_LINE = /^\s*(?:[⏺●]\s*)?API Error:/;
/** Lines that prove the slot resumed after the error. */
const RESUMED_LINE = /^\s*(?:[⏺●]\s*\S|[❯>]\s+\S)/;

const TAIL_LINES = 25;

export interface ApiStallClassification {
  stalled: boolean;
  signature: string | null;
  reason:
    | "stall"
    | "no_api_error"
    | "non_transient_api_error"
    | "retry_in_progress"
    | "resumed_after_error";
  /** Stable-ish fingerprint of this stall (error text + error-line count). */
  fingerprint: string | null;
  errorText: string | null;
}

export function classifyApiStallTail(pane: string): ApiStallClassification {
  const all = pane.replace(/\r/g, "").split("\n");
  const nonBlank = all.filter((l) => l.trim().length > 0);
  const tail = nonBlank.slice(-TAIL_LINES);
  const none = (reason: ApiStallClassification["reason"]): ApiStallClassification => ({
    stalled: false, signature: null, reason, fingerprint: null, errorText: null,
  });

  let idx = -1;
  for (let i = tail.length - 1; i >= 0; i--) {
    if (API_ERROR_LINE.test(tail[i])) { idx = i; break; }
  }
  if (idx < 0) return none("no_api_error");

  // Join wrapped continuation lines (indented, no bullet) into the error text.
  let errorText = tail[idx].trim();
  let j = idx + 1;
  while (j < tail.length && /^\s{2,}\S/.test(tail[j]) && !RESUMED_LINE.test(tail[j])) {
    errorText += " " + tail[j].trim();
    j++;
  }
  const after = tail.slice(idx + 1);
  if (after.some((l) => API_RETRY_BANNER.test(l)) || API_RETRY_BANNER.test(errorText)) {
    return none("retry_in_progress");
  }
  if (tail.slice(j).some((l) => RESUMED_LINE.test(l))) return none("resumed_after_error");

  const sig = API_STALL_SIGNATURES.find((s) => s.pattern.test(errorText));
  if (!sig) return none("non_transient_api_error");

  const errorCount = all.filter((l) => API_ERROR_LINE.test(l)).length;
  return {
    stalled: true,
    signature: sig.name,
    reason: "stall",
    fingerprint: `${errorCount}:${errorText}`,
    errorText,
  };
}

export type ApiStallAction =
  | { kind: "none"; reason: string }
  | { kind: "wait"; remainingMs: number }
  | { kind: "nudge"; consecutive: number }
  | { kind: "cap_alert"; consecutive: number };

export interface ApiStallSlotStatus {
  state: "clear" | "stall_pending" | "nudged" | "capped";
  signature: string | null;
  first_seen_at: string | null;
  last_nudge_at: string | null;
  consecutive_nudges: number;
  assignment_epoch: number | null;
}

interface SlotTrack {
  epoch: number | null;
  episode: { fingerprint: string; signature: string; firstSeenMs: number; nudged: boolean } | null;
  lastNudgeMs: number | null;
  consecutive: number;
  capped: boolean;
}

/**
 * Per-slot stall policy:
 * - one nudge per stall episode, after a grace period;
 * - a stall that starts within RESTALL_WINDOW of the previous nudge extends
 *   the consecutive chain; otherwise the chain resets;
 * - once MAX consecutive nudges each re-stalled, stop nudging and alert PM
 *   exactly once, until the slot recovers (a stall outside the window) or the
 *   assignment changes.
 */
export class ApiStallTracker {
  private slots = new Map<number, SlotTrack>();

  constructor(
    private readonly graceMs = API_STALL_GRACE_MS,
    private readonly restallWindowMs = API_STALL_RESTALL_WINDOW_MS,
    private readonly maxConsecutive = API_STALL_MAX_CONSECUTIVE_NUDGES,
  ) {}

  private track(slot: number, epoch: number | null): SlotTrack {
    let t = this.slots.get(slot);
    if (!t || t.epoch !== epoch) {
      t = { epoch, episode: null, lastNudgeMs: null, consecutive: 0, capped: false };
      this.slots.set(slot, t);
    }
    return t;
  }

  observe(
    slot: number,
    nowMs: number,
    input: { occupied: boolean; turnIdle: boolean; epoch: number | null; classification: ApiStallClassification },
  ): ApiStallAction {
    if (!input.occupied) {
      this.slots.delete(slot);
      return { kind: "none", reason: "not_occupied" };
    }
    const t = this.track(slot, input.epoch);
    if (!input.turnIdle) {
      t.episode = null;
      return { kind: "none", reason: "turn_active" };
    }
    const c = input.classification;
    if (!c.stalled || !c.fingerprint || !c.signature) {
      t.episode = null;
      return { kind: "none", reason: c.reason };
    }
    if (!t.episode || t.episode.fingerprint !== c.fingerprint) {
      // New stall episode.
      const restall = t.lastNudgeMs !== null && nowMs - t.lastNudgeMs <= this.restallWindowMs;
      if (!restall) {
        t.consecutive = 0;
        t.capped = false;
      }
      t.episode = { fingerprint: c.fingerprint, signature: c.signature, firstSeenMs: nowMs, nudged: false };
    }
    const ep = t.episode;
    if (ep.nudged) return { kind: "none", reason: "already_nudged" };
    if (t.capped) return { kind: "none", reason: "capped" };
    if (t.consecutive >= this.maxConsecutive) {
      t.capped = true;
      return { kind: "cap_alert", consecutive: t.consecutive };
    }
    const elapsed = nowMs - ep.firstSeenMs;
    if (elapsed < this.graceMs) return { kind: "wait", remainingMs: this.graceMs - elapsed };
    return { kind: "nudge", consecutive: t.consecutive + 1 };
  }

  /** Call only after the nudge was actually delivered. */
  recordNudge(slot: number, nowMs: number): void {
    const t = this.slots.get(slot);
    if (!t?.episode) return;
    t.episode.nudged = true;
    t.lastNudgeMs = nowMs;
    t.consecutive += 1;
  }

  status(slot: number): ApiStallSlotStatus {
    const t = this.slots.get(slot);
    const iso = (ms: number | null | undefined) => (ms == null ? null : new Date(ms).toISOString());
    if (!t) {
      return { state: "clear", signature: null, first_seen_at: null, last_nudge_at: null, consecutive_nudges: 0, assignment_epoch: null };
    }
    const state: ApiStallSlotStatus["state"] = t.capped
      ? "capped"
      : t.episode
        ? (t.episode.nudged ? "nudged" : "stall_pending")
        : "clear";
    return {
      state,
      signature: t.episode?.signature ?? null,
      first_seen_at: iso(t.episode?.firstSeenMs),
      last_nudge_at: iso(t.lastNudgeMs),
      consecutive_nudges: t.consecutive,
      assignment_epoch: t.epoch,
    };
  }
}
