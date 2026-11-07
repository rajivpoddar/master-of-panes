/**
 * PM exit-pending with a saved handoff and stopped subagents (Rajiv, DM
 * D0AMF0XE6TS thread 1793731816.594829, ts 1793752982.460979: "We should
 * implement the pm exit pending the same way. The additional step here will be
 * to stop all subagents after save handoff").
 *
 * A REST PM-only exit-pending request opens ONE `pm_save_handoff` obligation of
 * variant `exit`. The PM saves the SAME two handoff files the clear flow
 * watches (both mtimes later than the request, or an explicit resolve of this
 * request's own row), stops every background agent with TaskStop, and ends the
 * turn. MoP sends /exit at the next PM Stop once the save is detected.
 *
 * Rajiv, DM D0AMF0XE6TS thread 1793754554.496299, ts 1793758450.451269:
 * "remove the acks and the gates. as long all subgents are stopped and resume
 * point is saved, it should work." This supersedes the CTO ACK/fence REVISE
 * (ts 1793754925.429599): there is no subagents-stopped ACK, no work-bound
 * readiness invalidation and no busy-pane refusal. Stopping subagents is a PM
 * duty stated in the obligation text; MoP does not gate on it. A busy pane
 * queues /exit (Claude Code), so later PM work never voids the exit.
 * Kept from the CTO REVISE:
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
/** Episode whose single /exit attempt was taken and may have been written. */
export const PM_EXIT_CONSUMED_EPISODE_KEY = "pm_exit_handoff_consumed_episode";

/** PM work events (the latest id is logged as a Stop's pinned work; never a gate). */
const PM_WORK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse"] as const;

export const PM_EXIT_HANDOFF_ACTION =
  "PM exit is pending (daily restart). 1) Save the PM handoff: update MEMORY-session-handoff-current.md "
  + "(~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/memory/) and "
  + "data/pm-continuation-note.md. 2) Stop ALL running background agents/tasks with TaskStop. "
  + "3) End the turn. MoP sends /exit at the next PM Stop after the save (a busy pane queues it); "
  + "MoP waits for the zsh prompt, relaunches PM fresh with the configured model and injects the saved continuation "
  + "(daily 04:00: dev slots exit at their own next Stop in the same episode). Nothing is blocked meanwhile.";

export const PM_HANDOFF_EXIT_VARIANT: HandoffObligationVariant = {
  title: "Save PM handoff and stop subagents before MoP sends /exit",
  action: PM_EXIT_HANDOFF_ACTION,
  dedupeGroup: "pm_save_handoff:pm:exit",
  actionFor: (evidence) => evidence.variant === "exit_refused" ? pmExitRefusedAction(evidence) : PM_EXIT_HANDOFF_ACTION,
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

export type ExitHandoffReadiness =
  | { kind: "not_required" }
  | { kind: "waiting"; saved: false }
  | { kind: "ready"; via: "mtime" | "explicit_resolve"; requestedAt: string };

/**
 * Pure readiness at a PM Stop: ready once this request's resume point is saved
 * (both handoff files after the request, or an explicit resolve of its own row).
 * No ACK and no work/session binding (Rajiv ts 1793758450.451269).
 */
export function pmExitHandoffReadiness(db: Db, options: {
  ops: HandoffObligationOps | null;
  files?: readonly string[];
}): ExitHandoffReadiness {
  const live = currentPmExitHandoff(db);
  if (!live) return { kind: "not_required" };
  const via = saveEvidence(db, live, options.ops, options.files);
  return via ? { kind: "ready", via, requestedAt: live.requestedAt } : { kind: "waiting", saved: false };
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
  ops: HandoffObligationOps | null;
  files?: readonly string[];
}): PmStopMaintenance {
  if (pmExitInCustody(db)) return "exit_custody";
  if (db.hasPendingClear(0)) return "clear";
  const status = db.getExitStatus();
  if (!status.pending || !status.pm_only || status.cycled[0]) return "clear";
  const handoffRequired = !!db.getConfig(PM_EXIT_HANDOFF_EPISODE_KEY);
  if (!handoffRequired) return "exit"; // legacy direct arming: unconditioned single-Stop exit
  const readiness = pmExitHandoffReadiness(db, { ops: options.ops, files: options.files });
  return readiness.kind === "ready" ? "exit" : "clear";
}

/** Consume the request when its single /exit attempt is taken; custody is kept until the episode ends. */
export function consumePmExitHandoff(db: Db, ops: HandoffObligationOps | null, requestedAt: string, via: string): void {
  const id = db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY) || null;
  const episode = db.getConfig(PM_EXIT_HANDOFF_EPISODE_KEY) || "";
  for (const key of [PM_EXIT_HANDOFF_EPISODE_KEY, PM_EXIT_HANDOFF_REQUESTED_AT_KEY, PM_EXIT_HANDOFF_SESSION_KEY,
    PM_EXIT_HANDOFF_OBLIGATION_KEY, PM_EXIT_HANDOFF_OBLIGATION_STATE_KEY]) db.setConfig(key, "");
  db.setConfig(PM_EXIT_CONSUMED_EPISODE_KEY, episode);
  void resolveRow(ops, requestedAt, id, `PM handoff saved (${via}); MoP sends /exit at this Stop`);
  db.logEvent(0, "pm_exit_handoff_ready", "Stop", null, {
    requested_at: requestedAt, via, obligation_id: id, episode,
    reason: "Handoff saved; consuming the single PM /exit attempt",
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
    + `(a fresh handoff save is required), or arm a new PM-only exit: ${pmExitRecoveryCommand(episode)}`;
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
