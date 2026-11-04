/**
 * PM exit-pending with a saved handoff and stopped subagents (Rajiv, DM
 * D0AMF0XE6TS thread 1793731816.594829, ts 1793752982.460979: "We should
 * implement the pm exit pending the same way. The additional step here will be
 * to stop all subagents after save handoff").
 *
 * A REST PM-only exit-pending request opens ONE `pm_save_handoff` obligation of
 * variant `exit`. The PM saves the SAME two handoff files the clear flow
 * watches (both mtimes later than the request, or an explicit resolve of this
 * request's own row), stops every background agent with TaskStop, and then, as
 * the LAST tool call of the turn, POSTs /exit-pending/subagents-stopped with
 * this request's exact `episode` and `requested_at`.
 *
 * CTO REVISE (C0ALZJHGE49 thread 1793754459.969419, ts 1793754925.429599):
 * - The ACK is bound to the exact episode/request, the current PM session and
 *   the PM work version at ACK time. It is refused before the save evidence.
 *   Any later PM work (prompt, tool call, agent launch, new boot) voids it.
 * - The pinned pre-await Stop authority selects exactly ONE maintenance command
 *   (clear or exit). A latched clear keeps custody (exit yields); an exit that
 *   was consumed keeps custody until its episode ends (clear yields). Losing
 *   intents are kept, never resent or reset.
 * - Obligation custody: an unknown lookup is not absence; an exactly matching
 *   row (open or completed) is adopted; a write happens only on proven absence.
 *
 * Rule 50: nothing here gates, holds or delays PM input or tools. Until ready,
 * PM Stops fall through to normal handling and the exit stays pending.
 */
import type { MoPDatabase } from "./db.js";
import {
  handoffFilesSaved,
  PM_HANDOFF_FILES,
  type HandoffObligationOps,
  type HandoffObligationVariant,
} from "./pmHandoffClear.js";

export const PM_EXIT_HANDOFF_EPISODE_KEY = "pm_exit_handoff_episode";
export const PM_EXIT_HANDOFF_REQUESTED_AT_KEY = "pm_exit_handoff_requested_at";
export const PM_EXIT_HANDOFF_SESSION_KEY = "pm_exit_handoff_session";
export const PM_EXIT_HANDOFF_OBLIGATION_KEY = "pm_exit_handoff_obligation_id";
/** "" | "uncertain": whether this request's obligation write outcome is unknown. */
export const PM_EXIT_HANDOFF_OBLIGATION_STATE_KEY = "pm_exit_handoff_obligation_state";
/** Display only (ISO time of the accepted ACK). */
export const PM_EXIT_SUBAGENTS_STOPPED_KEY = "pm_exit_subagents_stopped_at";
/** Authoritative bound ACK (JSON). */
export const PM_EXIT_SUBAGENTS_ACK_KEY = "pm_exit_subagents_ack";
/** Episode whose single /exit attempt was taken and may have been written. */
export const PM_EXIT_CONSUMED_EPISODE_KEY = "pm_exit_handoff_consumed_episode";

/** PM work events: any of them after an ACK voids it; after a Stop it makes the Stop stale. */
const PM_WORK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse"] as const;

export function exitAckCommand(episode: string, requestedAt: string): string {
  return "curl -s -X POST http://127.0.0.1:3100/exit-pending/subagents-stopped -H 'Content-Type: application/json' "
    + `-d '${JSON.stringify({ episode, requested_at: requestedAt })}'`;
}

export const PM_EXIT_HANDOFF_ACTION =
  "PM exit is pending (daily restart). 1) Save the PM handoff: update MEMORY-session-handoff-current.md "
  + "(~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/) and "
  + "data/pm-continuation-note.md. 2) Stop ALL running background agents/tasks with TaskStop. "
  + "3) As the LAST tool call of the turn, acknowledge with this request's episode and requested_at "
  + "(GET /exit-status shows them). A later prompt, tool call or agent launch voids the ACK; re-send it. "
  + "MoP then sends /exit at the next PM Stop; the watchdog respawns PM with the saved continuation. "
  + "Nothing is blocked meanwhile.";

export const PM_HANDOFF_EXIT_VARIANT: HandoffObligationVariant = {
  title: "Save PM handoff and stop subagents before MoP sends /exit",
  action: PM_EXIT_HANDOFF_ACTION,
  dedupeGroup: "pm_save_handoff:pm:exit",
  actionFor: (evidence) => {
    if (evidence.variant === "exit_refused") return pmExitRefusedAction(evidence);
    const episode = typeof evidence.episode === "string" ? evidence.episode : "";
    const requestedAt = typeof evidence.requested_at === "string" ? evidence.requested_at : "";
    return `${PM_EXIT_HANDOFF_ACTION} ACK (last tool call): ${exitAckCommand(episode, requestedAt)}`;
  },
};

type Db = Pick<MoPDatabase, "getConfig" | "setConfig" | "logEvent" | "getExitStatus" | "getEvents" | "hasPendingClear">;

function parseMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/** Latest PM work event id (monotonic DB ids). */
export function latestPmWork(db: Pick<MoPDatabase, "getEvents">): number {
  return Math.max(0, ...PM_WORK_EVENTS.map((type) => db.getEvents(0, 1, type)[0]?.id ?? 0));
}

/** Latest PM boot (SessionStart) id and its session id. */
export function latestPmBoot(db: Pick<MoPDatabase, "getEvents">): { id: number; sessionId: string } {
  const boot = db.getEvents(0, 1, "SessionStart")[0];
  let sessionId = "";
  try { sessionId = String(JSON.parse(boot?.payload || "{}").session_id ?? ""); } catch { sessionId = ""; }
  return { id: boot?.id ?? 0, sessionId };
}

/** Obligation target per ORIGINAL request; never collides with a clear request row. */
export function exitHandoffRequestKey(requestedAt: string): string {
  return `pm-dhruva:exit:${requestedAt}`;
}

/** The live exit-handoff request, only while it belongs to the current PM-only episode. */
export function currentPmExitHandoff(db: Pick<MoPDatabase, "getConfig" | "getExitStatus">):
  { requestedAt: string; requestedMs: number; episode: string; session: string } | null {
  const status = db.getExitStatus();
  const requestedAt = db.getConfig(PM_EXIT_HANDOFF_REQUESTED_AT_KEY) || "";
  const requestedMs = parseMs(requestedAt);
  const episode = db.getConfig(PM_EXIT_HANDOFF_EPISODE_KEY) || "";
  if (!status.pending || !status.pm_only || requestedMs === null || !episode || episode !== status.episode) return null;
  return { requestedAt, requestedMs, episode, session: db.getConfig(PM_EXIT_HANDOFF_SESSION_KEY) || "" };
}

async function resolveRow(ops: HandoffObligationOps | null, requestedAt: string, knownId: string | null, reason: string): Promise<boolean> {
  if (!ops) return false;
  let id = knownId;
  if (!id) {
    const found = ops.lookup(exitHandoffRequestKey(requestedAt));
    if (found.status !== "found" || !found.open) return false;
    id = found.id;
  }
  return ops.resolveById(id, reason).catch(() => false);
}

const writersInFlight = new Map<string, Promise<string | null>>();

/**
 * Obligation custody for the live request. Adopt an exactly matching row
 * (open OR completed); an unknown lookup never authorizes a write; a keyed
 * write happens only on proven absence with no writer in flight. A failed or
 * lost-ACK write leaves the state uncertain and is reconciled the same way.
 */
export function ensurePmExitHandoffObligation(db: Db, ops: HandoffObligationOps): Promise<string | null> {
  const live = currentPmExitHandoff(db);
  if (!live) return Promise.resolve(null);
  const existing = db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY);
  if (existing) return Promise.resolve(existing);
  const active = writersInFlight.get(live.requestedAt);
  if (active) return active;
  const found = ops.lookup(exitHandoffRequestKey(live.requestedAt));
  if (found.status === "found" && found.requestedAt === live.requestedAt) {
    db.setConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY, found.id);
    db.setConfig(PM_EXIT_HANDOFF_OBLIGATION_STATE_KEY, "");
    db.logEvent(0, "pm_exit_handoff_obligation", null, null, {
      requested_at: live.requestedAt, obligation_id: found.id, via: "adopted", open: found.open,
    });
    return Promise.resolve(found.id);
  }
  if (found.status !== "absent") {
    db.logEvent(0, "pm_exit_handoff_obligation_unknown", null, null, {
      requested_at: live.requestedAt, lookup: found.status,
      reason: "Lookup did not prove absence; no write (custody kept, retried at a later PM Stop)",
    });
    return Promise.resolve(null);
  }
  const writer = (async () => {
    let id: string | null = null;
    let error: string | null = null;
    try {
      id = await ops.upsert(exitHandoffRequestKey(live.requestedAt), {
        producer: "mop-pm-handoff-exit", variant: "exit", pm_stop_actionable: 1,
        requested_at: live.requestedAt, episode: live.episode, session_id: live.session, files: PM_HANDOFF_FILES,
        ack: exitAckCommand(live.episode, live.requestedAt),
      });
    } catch (err) { error = String(err); }
    const still = currentPmExitHandoff(db);
    if (!still || still.requestedAt !== live.requestedAt) {
      if (id) void resolveRow(ops, live.requestedAt, id, `stale pm_save_handoff exit for ${live.requestedAt}; request superseded`);
      return null;
    }
    if (id) {
      db.setConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY, id);
      db.setConfig(PM_EXIT_HANDOFF_OBLIGATION_STATE_KEY, "");
      db.logEvent(0, "pm_exit_handoff_obligation", null, null, { requested_at: live.requestedAt, obligation_id: id, via: "created" });
    } else {
      db.setConfig(PM_EXIT_HANDOFF_OBLIGATION_STATE_KEY, "uncertain");
      db.logEvent(0, "pm_exit_handoff_obligation_uncertain", null, null, {
        requested_at: live.requestedAt, error: error ?? "missing obligation id",
        reason: "Write outcome unknown; reconciled by request-key lookup, never blindly reissued",
      });
    }
    return id;
  })().finally(() => writersInFlight.delete(live.requestedAt));
  writersInFlight.set(live.requestedAt, writer);
  return writer;
}

/** Resolve the row of a request that a newer exit-pending call replaced or disabled. */
export function resolveSupersededPmExitHandoff(ops: HandoffObligationOps, previous: { requestedAt: string; obligationId: string | null },
  reason: string): Promise<boolean> {
  return resolveRow(ops, previous.requestedAt, previous.obligationId, reason);
}

/** Save evidence for the live request: the clear flow's detection, unchanged. */
function saveEvidence(db: Db, live: { requestedAt: string; requestedMs: number }, ops: HandoffObligationOps | null,
  files: readonly string[] | undefined): "mtime" | "explicit_resolve" | null {
  if (handoffFilesSaved(live.requestedMs, files ?? PM_HANDOFF_FILES)) return "mtime";
  const obligationId = db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY) || "";
  if (obligationId && ops) {
    const row = ops.lookup(exitHandoffRequestKey(live.requestedAt));
    if (row.status === "found" && row.id === obligationId && !row.open && row.requestedAt === live.requestedAt) return "explicit_resolve";
  }
  return null;
}

type BoundAck = { episode: string; requested_at: string; session_id: string; work: number; acknowledged_at: string };

function readAck(db: Db): BoundAck | null {
  try {
    const ack = JSON.parse(db.getConfig(PM_EXIT_SUBAGENTS_ACK_KEY) || "null");
    return ack && typeof ack.episode === "string" && typeof ack.requested_at === "string"
      && typeof ack.session_id === "string" && typeof ack.work === "number" ? ack as BoundAck : null;
  } catch { return null; }
}

export type AckResult =
  | { ok: true; requested_at: string; episode: string; acknowledged_at: string }
  | { ok: false; status: 400 | 409; error: string };

/**
 * Record the PM's subagents-stopped ACK. Bound to the exact live request
 * (episode + requested_at), the current PM session and the current PM work
 * version; refused when unbound, mismatched, or before the save evidence.
 */
export function recordPmExitSubagentsStopped(db: Db, options: {
  nowMs: number;
  episode?: unknown;
  requestedAt?: unknown;
  ops: HandoffObligationOps | null;
  files?: readonly string[];
}): AckResult {
  if (typeof options.episode !== "string" || !options.episode || typeof options.requestedAt !== "string" || !options.requestedAt) {
    return { ok: false, status: 400, error: "ACK must name this request's 'episode' and 'requested_at' (GET /exit-status)" };
  }
  const live = currentPmExitHandoff(db);
  if (!live) return { ok: false, status: 409, error: "no PM exit handoff is pending" };
  if (options.episode !== live.episode || options.requestedAt !== live.requestedAt) {
    db.logEvent(0, "pm_exit_subagents_ack_refused", null, null, {
      reason: "unbound", episode: options.episode, requested_at: options.requestedAt, live_requested_at: live.requestedAt,
    });
    return { ok: false, status: 409, error: "ACK does not match the pending PM exit request" };
  }
  const boot = latestPmBoot(db);
  if (!boot.sessionId) return { ok: false, status: 409, error: "PM session unknown" };
  const saved = saveEvidence(db, live, options.ops, options.files);
  if (!saved) {
    db.logEvent(0, "pm_exit_subagents_ack_refused", null, null, { reason: "before_save", requested_at: live.requestedAt });
    return { ok: false, status: 409, error: "handoff save not detected yet: save both handoff files, then stop agents, then ACK" };
  }
  const acknowledgedAt = new Date(Math.max(options.nowMs, live.requestedMs)).toISOString();
  const ack: BoundAck = {
    episode: live.episode, requested_at: live.requestedAt, session_id: boot.sessionId, work: latestPmWork(db), acknowledged_at: acknowledgedAt,
  };
  db.setConfig(PM_EXIT_SUBAGENTS_ACK_KEY, JSON.stringify(ack));
  db.setConfig(PM_EXIT_SUBAGENTS_STOPPED_KEY, acknowledgedAt);
  db.logEvent(0, "pm_exit_subagents_stopped", null, null, {
    ...ack, save: saved, reason: "PM acknowledged all background agents/tasks stopped (after the save)",
  });
  return { ok: true, requested_at: live.requestedAt, episode: live.episode, acknowledged_at: acknowledgedAt };
}

export type ExitHandoffReadiness =
  | { kind: "not_required" }
  | { kind: "waiting"; saved: boolean; acked: boolean }
  | { kind: "ready"; via: "mtime" | "explicit_resolve"; requestedAt: string };

/**
 * Pure readiness at a PM Stop pinned at `workPin` (the latest PM work id when
 * the Stop arrived). Ready only when this request's save evidence holds and its
 * bound ACK names this request, this session and exactly `workPin` (no PM work
 * between the ACK and the Stop), and nothing newer than the pin exists.
 */
export function pmExitHandoffReadiness(db: Db, options: {
  currentSessionId: string;
  workPin: number;
  ops: HandoffObligationOps | null;
  files?: readonly string[];
}): ExitHandoffReadiness {
  const live = currentPmExitHandoff(db);
  if (!live) return { kind: "not_required" };
  const via = saveEvidence(db, live, options.ops, options.files);
  const ack = readAck(db);
  const acked = !!ack && ack.episode === live.episode && ack.requested_at === live.requestedAt
    && !!options.currentSessionId && ack.session_id === options.currentSessionId
    && ack.work === options.workPin && latestPmWork(db) === options.workPin;
  if (via && acked) return { kind: "ready", via, requestedAt: live.requestedAt };
  return { kind: "waiting", saved: via !== null, acked };
}

/** An exit attempt for the current episode was taken and may have been written. */
export function pmExitInCustody(db: Pick<MoPDatabase, "getConfig" | "getExitStatus">): boolean {
  const status = db.getExitStatus();
  const consumed = db.getConfig(PM_EXIT_CONSUMED_EPISODE_KEY) || "";
  return !!consumed && status.pending && status.pm_only && status.episode === consumed;
}

export type PmStopMaintenance = "exit" | "exit_custody" | "clear";

/**
 * ONE maintenance command per pinned PM Stop. A latched clear keeps custody;
 * otherwise a ready (or legacy unconditioned) PM-only exit wins; an exit in
 * custody holds the clear. Pure: selects, never mutates.
 */
export function selectPmStopMaintenance(db: Db, options: {
  sessionId: string;
  workPin: number;
  ops: HandoffObligationOps | null;
  files?: readonly string[];
}): PmStopMaintenance {
  if (pmExitInCustody(db)) return "exit_custody";
  if (db.hasPendingClear(0)) return "clear";
  const status = db.getExitStatus();
  if (!status.pending || !status.pm_only || status.cycled[0]) return "clear";
  const handoffRequired = !!db.getConfig(PM_EXIT_HANDOFF_EPISODE_KEY);
  if (!handoffRequired) return "exit"; // legacy direct arming: unconditioned single-Stop exit
  const readiness = pmExitHandoffReadiness(db, {
    currentSessionId: options.sessionId, workPin: options.workPin, ops: options.ops, files: options.files,
  });
  return readiness.kind === "ready" ? "exit" : "clear";
}

/** Consume the request when its single /exit attempt is taken; custody is kept until the episode ends. */
export function consumePmExitHandoff(db: Db, ops: HandoffObligationOps | null, requestedAt: string, via: string): void {
  const id = db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY) || null;
  const episode = db.getConfig(PM_EXIT_HANDOFF_EPISODE_KEY) || "";
  for (const key of [PM_EXIT_HANDOFF_EPISODE_KEY, PM_EXIT_HANDOFF_REQUESTED_AT_KEY, PM_EXIT_HANDOFF_SESSION_KEY,
    PM_EXIT_HANDOFF_OBLIGATION_KEY, PM_EXIT_HANDOFF_OBLIGATION_STATE_KEY, PM_EXIT_SUBAGENTS_ACK_KEY]) db.setConfig(key, "");
  db.setConfig(PM_EXIT_CONSUMED_EPISODE_KEY, episode);
  void resolveRow(ops, requestedAt, id, `PM handoff saved (${via}) and subagents stopped; MoP sends /exit at this Stop`);
  db.logEvent(0, "pm_exit_handoff_ready", "Stop", null, {
    requested_at: requestedAt, via, obligation_id: id, episode,
    reason: "Handoff saved and subagents stopped; consuming the single PM /exit attempt",
  });
}

/** Durable terminal of a PM-only exit episode whose single /exit was refused before any effect. */
export const PM_EXIT_TERMINAL_KEY = "pm_exit_terminal";

export type PmExitTerminal = {
  episode: string; outcome: "refused_pre_effect"; reason: string; continuation_prompt: string | null;
  at: string; pinned_work: number; session_id: string; obligation_id: string | null;
};

export function readPmExitTerminal(db: Pick<MoPDatabase, "getConfig">): PmExitTerminal | null {
  try {
    const t = JSON.parse(db.getConfig(PM_EXIT_TERMINAL_KEY) || "null");
    return t && typeof t.episode === "string" && t.outcome === "refused_pre_effect" ? t as PmExitTerminal : null;
  } catch { return null; }
}

export function pmExitRecoveryCommand(episode: string): string {
  return "curl -s -X POST http://127.0.0.1:3100/exit-pending -H 'Content-Type: application/json' "
    + `-d '${JSON.stringify({ enabled: true, pm_only: true, recover_episode: episode })}'`;
}

function pmExitRefusedAction(evidence: Record<string, unknown>): string {
  const episode = typeof evidence.episode === "string" ? evidence.episode : "";
  const reason = typeof evidence.reason === "string" ? evidence.reason : "refused_pre_effect";
  return `PM /exit was REFUSED before any effect (${reason}); the PM did not restart. The saved continuation is kept `
    + "(GET /exit-status exit_terminal). Nothing is resent automatically. To restart, re-arm this same episode "
    + `(new save + subagents-stopped ACK required): ${pmExitRecoveryCommand(episode)}`;
}

/**
 * CTO REVISE (thread 1793754459.969419, ts 1793756803.183959): a refused
 * (proven zero-effect) /exit ends its episode in an explicit durable terminal
 * instead of leaving it pending/cycled/waiting for a boot that never comes.
 * The continuation and the no-effect evidence are kept, the cycled record is
 * not erased, nothing is resent, and ONE actionable obligation names the
 * same-owner recovery edge (POST /exit-pending with recover_episode).
 */
export function recordPmExitRefusedTerminal(db: Db, ops: HandoffObligationOps | null, options: {
  episode: string | null; reason: string; pinnedWork: number; sessionId: string; nowMs: number;
}): boolean {
  releasePmExitCustodyZeroEffect(db, options.episode, options.reason);
  const status = db.getExitStatus();
  if (!options.episode || status.episode !== options.episode || !status.pending || !status.pm_only) return false;
  const terminal: PmExitTerminal = {
    episode: options.episode, outcome: "refused_pre_effect", reason: options.reason,
    continuation_prompt: status.continuation_prompt, at: new Date(options.nowMs).toISOString(),
    pinned_work: options.pinnedWork, session_id: options.sessionId, obligation_id: null,
  };
  db.setConfig(PM_EXIT_TERMINAL_KEY, JSON.stringify(terminal));
  db.setConfig("exit_pending_continuation_delivery", "exit_refused");
  db.setConfig("exit_pending", "false"); // episode ends; continuation prompt and cycled record are kept
  db.logEvent(0, "pm_exit_refused_terminal", "Stop", null, {
    ...terminal, recovery: pmExitRecoveryCommand(options.episode),
    reason_text: "PM /exit refused before effect; episode ended, continuation kept, no automatic resend",
  });
  if (ops) {
    void ops.upsert(`pm-dhruva:exit-refused:${options.episode}`, {
      producer: "mop-pm-handoff-exit", variant: "exit_refused", pm_stop_actionable: 1,
      requested_at: terminal.at, episode: options.episode, reason: options.reason,
    }).then((id) => {
      const current = readPmExitTerminal(db);
      if (id && current && current.episode === options.episode) {
        db.setConfig(PM_EXIT_TERMINAL_KEY, JSON.stringify({ ...current, obligation_id: id }));
      }
    }).catch(() => undefined);
  }
  return true;
}

/** Resolve the refused-terminal obligation once its episode is recovered (or superseded). */
export async function resolvePmExitTerminalRow(ops: HandoffObligationOps, terminal: PmExitTerminal, reason: string): Promise<boolean> {
  let id = terminal.obligation_id;
  if (!id) {
    const found = ops.lookup(`pm-dhruva:exit-refused:${terminal.episode}`);
    if (found.status !== "found" || !found.open) return false;
    id = found.id;
  }
  return ops.resolveById(id, reason).catch(() => false);
}

/** A /exit write refused before any effect releases custody (proven zero effect); never resent. */
export function releasePmExitCustodyZeroEffect(db: Db, episode: string | null, reason: string): void {
  if (!episode || db.getConfig(PM_EXIT_CONSUMED_EPISODE_KEY) !== episode) return;
  db.setConfig(PM_EXIT_CONSUMED_EPISODE_KEY, "");
  db.logEvent(0, "pm_exit_custody_released_zero_effect", "Stop", null, { episode, reason });
}
