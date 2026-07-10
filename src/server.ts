/**
 * MoP HTTP Server — Receives Claude Code HTTP hook POSTs
 *
 * Each slot is configured to POST hooks to:
 *   http://localhost:3100/hooks/slot/:slotNum
 *
 * The server:
 * 1. Validates the payload
 * 2. Logs to SQLite
 * 3. Processes events (detects idle, plan-ready, etc.)
 * 4. Relays to PM pane via tmux
 * 5. Returns a HookResponse that Claude Code acts on
 */

import { appendFileSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { monitorEventLoopDelay } from "node:perf_hooks";
import { Hono } from "hono";
import { serve } from "@hono/node-server";
import { z } from "zod";
import { MoPDatabase } from "./db.js";
import { TmuxRelay } from "./relay.js";
import { HookProcessor } from "./hooks.js";
import { LogManager } from "./logs.js";
import { StuckDetector } from "./stuck.js";
import { OpsAuditScheduler } from "./opsAudit.js";
import { PMCadenceScheduler } from "./pmCadence.js";
import { P0EscalationWatcher } from "./p0EscalationWatch.js";
import { ProcessHealthChecker, RESTART_COMMANDS, SHELL_COMMANDS } from "./health.js";
import { execShell, execShellOk, sleep } from "./asyncCommand.js";
import { DEFAULT_CONFIG } from "./types.js";
import type { HookPayload, MoPConfig } from "./types.js";

// ─── Config ──────────────────────────────────────────────

const config: MoPConfig = {
  ...DEFAULT_CONFIG,
  httpPort: parseInt(process.env.MOP_PORT ?? "3100", 10),
  dbPath: process.env.MOP_DB_PATH ?? DEFAULT_CONFIG.dbPath,
};

// ─── Initialize ──────────────────────────────────────────

const db = new MoPDatabase(config);
const relay = new TmuxRelay(config);
// Wire DB into relay so injectToPM can queue when PM is busy.
// Rajiv directive 2026-05-06 11:18 IST.
relay.setDatabase(db);
const processor = new HookProcessor(db, relay);

// MoP events are a bounded operational ring, not an audit archive. Prune once
// after startup and then every six hours so recent-event endpoints stay cheap.
const eventRetentionMaxRows = parseInt(process.env.MOP_EVENT_RETENTION_MAX_ROWS ?? "200000", 10);
const eventRetentionDays = parseInt(process.env.MOP_EVENT_RETENTION_DAYS ?? "14", 10);
const pruneEvents = (): void => {
  const removed = db.pruneEvents(eventRetentionMaxRows, eventRetentionDays);
  if (removed > 0) {
    console.log(`[mop] pruned ${removed} old events; retention rows=${eventRetentionMaxRows} days=${eventRetentionDays}`);
  }
};
pruneEvents();
const eventRetentionTimer = setInterval(pruneEvents, 6 * 60 * 60 * 1000);

// ─── Pane Logging (Phase 2) ─────────────────────────────
const logManager = new LogManager();
await logManager.enableLogging(config.slotCount);
relay.setLogManager(logManager);

// ─── Stuck Detection (Phase 3) ──────────────────────────
const stuckDetector = new StuckDetector(db, logManager, relay);
// Wire stuckDetector into HookProcessor so SessionStart:compact can clear
// the per-slot lastMatchLine tracker (Bug A defense — see stuck.ts).
processor.setStuckDetector(stuckDetector);
stuckDetector.start();

// ─── Process Health (Phase 4) ───────────────────────────
const healthChecker = new ProcessHealthChecker(db, relay);
healthChecker.start();

// ─── Ops Audit Scheduler (hourly PM-pane exceptions review) ──
// Owns cadence (1h), in-process lock, PM busy queueing (via TmuxRelay).
// Delegates business logic to ~/.claude/scripts/hourly-ops-review-bg.sh.
// Rajiv CTO directive 2026-05-26 thread C0ALZJHGE49/1779790681.847219.
const opsAuditScheduler = new OpsAuditScheduler(db, relay);
opsAuditScheduler.start();

// ─── PM Cadence Scheduler (3h heartbeat + daily morning brief) ──
// MoP owns these recurring PM injections so ops cadence state is observable and
// persisted in one control plane. launchd remains only the MoP watchdog.
const pmCadenceScheduler = new PMCadenceScheduler(db, relay);
pmCadenceScheduler.start();

// ─── P0 Escalation Watcher ──────────────────────────────────
// Reads PM-owned p0_escalation obligations and wakes PM with a normal prompt
// that tells PM to invoke Skill(alert-processing). It never injects slash
// commands and never clears PM obligations.
const p0EscalationWatcher = new P0EscalationWatcher(db, relay);
p0EscalationWatcher.start();

// ─── Log Rotation (every 10 minutes) ────────────────────
const rotationTimer = setInterval(() => {
  for (let i = 1; i <= config.slotCount; i++) {
    logManager.rotateIfNeeded(i);
  }
}, 10 * 60 * 1000);

// ─── Event-Loop Lag Instrumentation ─────────────────────
// /health must stay trivial, but healthcheck needs to distinguish "dead"
// from "alive but event-loop starved." Sample here and expose the last value.
const eventLoopHist = monitorEventLoopDelay({ resolution: 20 });
eventLoopHist.enable();
let lastLagSampleMs = 0;
const eventLoopLagTimer = setInterval(() => {
  const maxMs = eventLoopHist.max / 1e6;
  const p99Ms = eventLoopHist.percentile(99) / 1e6;
  const meanMs = eventLoopHist.mean / 1e6;
  lastLagSampleMs = maxMs;
  if (maxMs >= 1000) {
    console.log(
      `[event-loop-lag] WARN max=${maxMs.toFixed(0)}ms p99=${p99Ms.toFixed(0)}ms mean=${meanMs.toFixed(1)}ms — event loop starved, /health may flap`
    );
  } else {
    console.log(`[event-loop-lag] max=${maxMs.toFixed(0)}ms p99=${p99Ms.toFixed(0)}ms mean=${meanMs.toFixed(1)}ms`);
  }
  eventLoopHist.reset();
}, 60 * 1000);

const app = new Hono();

// ─── MoP Clear Helpers ─────────────────────────────────
// HTTP path for deterministic schedulers that cannot call MCP tools.
// This keeps clears logged in MoP and avoids raw /clear injection from shell.

type ClearSlotResult = { slot: number; name: string; status: string };

const PM_CLEAR_RECENT_SUPPRESS_MS = parseInt(
  process.env.MOP_PM_CLEAR_RECENT_SUPPRESS_MS ?? `${10 * 60 * 1000}`,
  10,
);
const PM_CLEAR_STALE_ACK_REPAIR_MS = parseInt(
  process.env.MOP_PM_CLEAR_STALE_ACK_REPAIR_MS ?? `${10 * 60 * 1000}`,
  10,
);
const PM_CLEAR_RETRY_SUPPRESS_MS = parseInt(
  process.env.MOP_PM_CLEAR_RETRY_SUPPRESS_MS ?? `${60 * 1000}`,
  10,
);
const PM_CLEAR_REQUESTED_AT_KEY = "pm_clear_requested_at";
const PM_CLEAR_CONFIRMED_AT_KEY = "pm_clear_confirmed_at";

function normalizeClearTarget(raw: string): number[] | null {
  const normalized = raw.trim().toLowerCase();
  if (normalized === "all") return [0, 1, 2, 3, 4];
  if (normalized === "pm") return [0];
  if (/^[0-4]$/.test(normalized)) return [Number(normalized)];
  return null;
}

function isRecentIso(value: string | null, windowMs: number): boolean {
  if (!value) return false;
  const ts = Date.parse(value);
  return Number.isFinite(ts) && Date.now() - ts >= 0 && Date.now() - ts < windowMs;
}

function parseMoPIsoMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(trimmed) ? trimmed : `${trimmed}Z`;
  const ts = Date.parse(normalized);
  return Number.isFinite(ts) ? ts : null;
}

function hasRecentClearEvent(slotNum: number, windowMs: number): boolean {
  const cutoff = Date.now() - windowMs;
  return db
    .getEvents(slotNum, 20)
    .some((event) => {
      if (!["slot_cleared", "clear_pending_executed"].includes(event.event_type)) return false;
      const ts = parseMoPIsoMs(event.timestamp);
      return ts !== null && ts >= cutoff;
    });
}

function hasRecentPmClearSend(windowMs: number): boolean {
  const cutoff = Date.now() - windowMs;
  return db
    .getEvents(0, 30)
    .some((event) => {
      if (
        ![
          "send_allowed_pm_clear_control_command",
          "clear_pending_queued",
          "clear_pending_pm_retry_sent",
        ].includes(event.event_type)
      ) {
        return false;
      }

      const ts = parseMoPIsoMs(event.timestamp);
      if (ts === null || ts < cutoff) return false;

      if (event.event_type === "send_allowed_pm_clear_control_command") {
        try {
          const payload = JSON.parse(event.payload) as { command?: unknown };
          return payload.command === "/clear";
        } catch {
          return false;
        }
      }

      return true;
    });
}

function findPmSessionStartAfter(cutoffMs: number) {
  return db
    .getEvents(0, 50, "SessionStart")
    .find((event) => {
      const ts = parseMoPIsoMs(event.timestamp);
      return ts !== null && ts >= cutoffMs;
    });
}

function reconcileStalePmClearRequest(source: string): boolean {
  if (!db.hasPendingClear(0)) return false;

  const requestedAt = db.getConfig(PM_CLEAR_REQUESTED_AT_KEY);
  const requestedMs = parseMoPIsoMs(requestedAt);
  if (requestedMs === null) return false;
  if (Date.now() - requestedMs < PM_CLEAR_STALE_ACK_REPAIR_MS) return false;

  const laterSessionStart = findPmSessionStartAfter(requestedMs);
  if (!laterSessionStart) return false;

  db.clearPendingClear(0);
  db.logEvent(0, "clear_pending_stale_repaired", null, null, {
    name: "PM",
    requested_at: requestedAt,
    repaired_at: new Date().toISOString(),
    later_session_start_event_id: laterSessionStart.id,
    later_session_start_timestamp: laterSessionStart.timestamp,
    via: source,
    reason: "PM clear request never received SessionStart:clear ack; later SessionStart proves the latch is stale. Clearing latch only, not marking clear complete.",
  });
  return true;
}

async function sendClearViaMopSendPath(
  slotNum: number,
  source: string,
): Promise<{ success: boolean; status: number; reason?: string; error?: string }> {
  try {
    const res = await fetch(`http://127.0.0.1:${config.httpPort}/slots/${slotNum}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command: "/clear",
        force: true,
        allow_pm_clear: slotNum === 0,
        source,
      }),
    });
    const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    return {
      success: res.ok && data.success === true,
      status: res.status,
      reason: typeof data.reason === "string" ? data.reason : undefined,
      error: typeof data.error === "string" ? data.error : undefined,
    };
  } catch (err) {
    return {
      success: false,
      status: 0,
      reason: "mop_send_path_error",
      error: String(err),
    };
  }
}

async function clearSlotsThroughMopHttp(
  targetSlots: number[],
  options: { clearExistingPendingForTargets: boolean; source: string; terminalOnly: boolean },
): Promise<ClearSlotResult[]> {
  const normalizedTargets = Array.from(new Set(targetSlots))
    .filter((slot) => slot >= 0 && slot <= 4);
  const results: ClearSlotResult[] = [];

  if (options.clearExistingPendingForTargets) {
    for (const slotNum of normalizedTargets) {
      db.clearPendingClear(slotNum);
    }
  }

  const devSlots = normalizedTargets.filter((slotNum) => slotNum !== 0);
  const includePmSlot = normalizedTargets.includes(0);

  for (const slotNum of devSlots) {
    const isActive = await relay.isSlotActive(slotNum);
    db.updateSlot(slotNum, { idle: !isActive });

    const slotState = db.getSlot(slotNum);
    const name = slotState?.name ?? `slot-${slotNum}`;
    const isIdle = slotState?.idle ?? true;

    if (isIdle) {
      try {
        const sent = await sendClearViaMopSendPath(slotNum, options.source);
        if (!sent.success) {
          throw new Error(sent.error ?? sent.reason ?? `send failed status=${sent.status}`);
        }

        db.releaseSlot(slotNum);
        db.clearPendingClear(slotNum);
        db.logEvent(slotNum, "slot_cleared", null, null, {
          name,
          cleared_at: new Date().toISOString(),
          immediate: true,
          via: options.source,
          delivery: "mop_send_to_slot",
        });

        results.push({ slot: slotNum, name, status: "cleared (idle)" });
      } catch (err) {
        results.push({ slot: slotNum, name, status: `failed: ${err}` });
      }
    } else if (options.terminalOnly) {
      db.logEvent(slotNum, "clear_terminal_only_skipped", null, null, {
        name,
        checked_at: new Date().toISOString(),
        reason: "Slot is active; terminal-only clear did not arm a pending clear",
        via: options.source,
        delivery: "http_clear_endpoint",
      });
      results.push({ slot: slotNum, name, status: "active (not terminal; no clear queued)" });
    } else {
      db.setPendingClear(slotNum);
      db.logEvent(slotNum, "clear_pending_queued", null, null, {
        name,
        queued_at: new Date().toISOString(),
        reason: "Slot is active - will clear on next idle notification",
        via: options.source,
        delivery: "http_clear_endpoint",
      });
      results.push({ slot: slotNum, name, status: "queued (active - will clear on next idle)" });
    }

    await sleep(500);
  }

  if (includePmSlot) {
    reconcileStalePmClearRequest(options.source);

    if (
      db.hasPendingClear(0) ||
      isRecentIso(db.getConfig(PM_CLEAR_REQUESTED_AT_KEY), PM_CLEAR_RECENT_SUPPRESS_MS)
    ) {
      db.logEvent(0, "clear_pending_duplicate_suppressed", null, null, {
        name: "PM",
        via: options.source,
        reason: "pm_clear_already_requested",
        requested_at: db.getConfig(PM_CLEAR_REQUESTED_AT_KEY),
      });
      results.push({ slot: 0, name: "PM", status: "skipped (PM clear already requested)" });
      return results;
    }

    if (
      isRecentIso(db.getConfig(PM_CLEAR_CONFIRMED_AT_KEY), PM_CLEAR_RECENT_SUPPRESS_MS) ||
      hasRecentClearEvent(0, PM_CLEAR_RECENT_SUPPRESS_MS)
    ) {
      db.logEvent(0, "clear_recent_duplicate_suppressed", null, null, {
        name: "PM",
        via: options.source,
        reason: "pm_clear_recently_confirmed",
        confirmed_at: db.getConfig(PM_CLEAR_CONFIRMED_AT_KEY),
        suppress_window_ms: PM_CLEAR_RECENT_SUPPRESS_MS,
      });
      results.push({ slot: 0, name: "PM", status: "skipped (PM clear recently confirmed)" });
      return results;
    }

    try {
      db.setPendingClear(0);
      const requestedAt = new Date().toISOString();
      db.setConfig(PM_CLEAR_REQUESTED_AT_KEY, requestedAt);

      const sent = await sendClearViaMopSendPath(0, options.source);
      if (!sent.success) {
        db.clearPendingClear(0);
        throw new Error(sent.error ?? sent.reason ?? `send failed status=${sent.status}`);
      }

      db.logEvent(0, "clear_pending_queued", null, null, {
        name: "PM",
        queued_at: requestedAt,
        reason: "PM clear sent through MoP send path; awaiting SessionStart source=clear acknowledgement",
        via: options.source,
        delivery: "mop_send_to_slot",
      });

      results.push({ slot: 0, name: "PM", status: "queued (PM /clear sent; awaiting clear acknowledgement)" });
    } catch (err) {
      results.push({ slot: 0, name: "PM", status: `failed: ${err}` });
    }
  }

  return results;
}

// ─── Validation ──────────────────────────────────────────

// Claude Code HTTP hooks send `hook_event_name` (not `type`), along with
// session_id, cwd, transcript_path, permission_mode, and event-specific
// fields like tool_name/tool_input (PreToolUse/PostToolUse),
// stop_hook_active/last_assistant_message (Stop).
const hookPayloadSchema = z.object({
  // Core fields present in ALL hook events
  hook_event_name: z.enum([
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "UserPromptSubmit",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "SessionStart",
  ]),
  session_id: z.string().optional(),
  cwd: z.string().optional(),
  transcript_path: z.string().optional(),
  permission_mode: z.string().optional(),

  // PreToolUse / PostToolUse fields
  tool_name: z.string().optional(),
  tool_input: z.record(z.unknown()).optional(),
  tool_output: z.string().optional(),

  // Stop fields
  stop_hook_active: z.boolean().optional(),
  last_assistant_message: z.string().optional(),
  stop_reason: z.string().optional(),

  // Notification fields
  notification_type: z.string().optional(),

  // SessionStart fields
  source: z.string().optional(),

  // PreCompact / PostCompact fields
  trigger: z.string().optional(),
  custom_instructions: z.string().optional(),
  compact_summary: z.string().optional(),
}).passthrough(); // Accept additional unknown fields gracefully

const slotParamSchema = z.coerce.number().int().min(0).max(4);

// ─── Normalize Payload ───────────────────────────────────

/** Convert Claude Code's wire format to our internal HookPayload */
function normalizePayload(raw: z.infer<typeof hookPayloadSchema>): HookPayload {
  return {
    type: raw.hook_event_name,
    tool_name: raw.tool_name,
    tool_input: raw.tool_input,
    tool_output: raw.tool_output,
    session_id: raw.session_id,
    cwd: raw.cwd,
    transcript_path: raw.transcript_path,
    notification_type: raw.notification_type,
    stop_reason: raw.stop_reason,
    // Preserve useful context
    transcript: raw.last_assistant_message,
    // SessionStart / PreCompact / PostCompact
    source: raw.source,
    trigger: raw.trigger,
    compact_summary: raw.compact_summary,
  };
}

// ─── Routes ──────────────────────────────────────────────

/**
 * Liveness probe. Keep this event-loop trivial: no SQLite, tmux, log scans,
 * or filesystem reads. Deep diagnostics live at /ready.
 */
app.get("/health", (c) => {
  return c.json({
    status: "ok",
    uptime: process.uptime(),
    lastLagMs: Math.round(lastLagSampleMs),
  });
});

/** Deep readiness / control-plane status. Not used by launchd liveness. */
app.get("/ready", (c) => {
  const slots = (() => {
    try {
      return db.getAllSlots().map((s) => ({
        slot: s.slot,
        occupied: s.occupied,
        dnd: s.dnd,
        activity: s.activity,
      }));
    } catch (e) {
      return { error: String(e) };
    }
  })();
  return c.json({
    status: "ok",
    uptime: process.uptime(),
    eventLoop: {
      lastSampleMs: Math.round(lastLagSampleMs),
      warnThresholdMs: 1000,
    },
    watchdogs: {
      stuckDetector: !!stuckDetector,
      healthChecker: !!healthChecker,
      opsAuditScheduler: !!opsAuditScheduler,
      pmCadenceScheduler: !!pmCadenceScheduler,
      p0EscalationWatcher: !!p0EscalationWatcher,
      rotationTimer: !!rotationTimer,
      eventLoopLagTimer: !!eventLoopLagTimer,
    },
    slots,
  });
});

/** Restart the MoP server — exits process, session watcher restarts it */
app.post("/restart", (c) => {
  console.log("[mop] Restart requested via /restart endpoint");
  // Respond first, then exit. The session-start health watcher restarts us.
  setTimeout(() => process.exit(0), 100);
  return c.json({ status: "restarting" });
});

/**
 * /pm-status — toggle the PM busy flag.
 *
 * Rajiv directive 2026-05-06 11:18 IST: queue slot-idle / slot-active /
 * check-slot relays (and any other PM-bound message) while PM is busy
 * (mid-tool/turn). Drain on PM Stop hook.
 *
 * Wired from the heydonna-app project's `.claude/settings.json` Stop +
 * SessionStart hooks via curl POST. Body: `{ "event": "start" | "stop" }`.
 *   start → pm_busy = true
 *   stop  → pm_busy = false + drain queue
 *
 * GET returns current busy flag + queued event peek (diagnostics).
 */
app.post("/pm-status", async (c) => {
  let body: { event?: string } = {};
  try {
    body = (await c.req.json()) as { event?: string };
  } catch {
    return c.json({ success: false, error: "Body must be JSON: { event: 'start'|'stop' }" }, 400);
  }
  const event = body.event;
  if (event !== "start" && event !== "stop") {
    return c.json({ success: false, error: "event must be 'start' or 'stop'" }, 400);
  }
  if (event === "start") {
    const result = relay.setPMBusy(true);
    db.logEvent(0, "pm_status_busy", null, null, { event });
    return c.json({ success: true, pm_busy: true, drained: result.drained });
  } else {
    // event === "stop" → drain
    const before = db.getPendingPMEventCount();
    const result = relay.setPMBusy(false);
    db.logEvent(0, "pm_status_idle_drained", null, null, {
      event,
      queued_before: before,
      drained: result.drained,
    });
    if (db.hasPendingClear(0)) {
      const requestedAt = db.getConfig(PM_CLEAR_REQUESTED_AT_KEY);
      if (
        isRecentIso(requestedAt, PM_CLEAR_RETRY_SUPPRESS_MS) ||
        hasRecentPmClearSend(PM_CLEAR_RETRY_SUPPRESS_MS)
      ) {
        db.logEvent(0, "clear_pending_pm_retry_suppressed", null, null, {
          name: "PM",
          reason: "PM clear is already pending and a /clear was recently sent; suppressing duplicate retry",
          requested_at: requestedAt,
          suppress_window_ms: PM_CLEAR_RETRY_SUPPRESS_MS,
        });
      } else {
        const sent = await sendClearViaMopSendPath(0, "pm_status_stop_pending_clear");
        db.logEvent(0, sent.success ? "clear_pending_pm_retry_sent" : "clear_pending_failed", null, null, {
          name: "PM",
          reason: "PM status stop observed while clear_pending_0=true; retrying /clear through MoP clear path",
          status: sent.status,
          send_reason: sent.reason ?? null,
          error: sent.error ?? null,
        });
      }
    }
    return c.json({ success: true, pm_busy: false, queued_before: before, drained: result.drained });
  }
});

app.get("/pm-status", (c) => {
  return c.json({
    pm_busy: relay.isPMBusy(),
    queue: db.peekPendingPMEvents(),
    queue_count: db.getPendingPMEventCount(),
  });
});

// ─── Ops Audit Endpoints ────────────────────────────────
// Enqueue an audit run (manual trigger from MCP tool / operator).
// reason defaults to "manual" — bypasses pause for operator override.
// Returns immediately with a durable job id; the bg-script runs out of band.
app.post("/ops-audit/run", async (c) => {
  let body: { reason?: string } = {};
  try {
    body = (await c.req.json()) as { reason?: string };
  } catch {
    /* allow empty body */
  }
  const triggerReason = body.reason === "scheduled" || body.reason === "boot" ? body.reason : "manual";
  const result = opsAuditScheduler.enqueue(triggerReason);
  return c.json({ success: true, ...result });
});

// Status of the scheduler (paused?, last-run, payload bytes, etc.).
app.get("/ops-audit/status", (c) => {
  return c.json(opsAuditScheduler.getStatus());
});

app.get("/ops-audit/jobs/:jobId", (c) => {
  const job = opsAuditScheduler.getJob(c.req.param("jobId"));
  if (!job) {
    return c.json({ success: false, error: "job not found" }, 404);
  }
  return c.json({ success: true, job });
});

// Pause/unpause the scheduler. Body: { paused: boolean }
app.post("/ops-audit/pause", async (c) => {
  let body: { paused?: boolean } = {};
  try {
    body = (await c.req.json()) as { paused?: boolean };
  } catch {
    return c.json({ success: false, error: "Body must be JSON: { paused: boolean }" }, 400);
  }
  if (typeof body.paused !== "boolean") {
    return c.json({ success: false, error: "paused must be boolean" }, 400);
  }
  opsAuditScheduler.setPaused(body.paused);
  return c.json({ success: true, status: opsAuditScheduler.getStatus() });
});

// ─── PM Cadence Endpoints ─────────────────────────────────
// MoP-owned recurring PM tasks:
// - heartbeat: `[scheduled-task | 3h heartbeat | HH:MM]`
// - morning-brief: `/morning-brief`
app.get("/pm-cadence/status", (c) => {
  return c.json(pmCadenceScheduler.getStatus());
});

app.post("/pm-cadence/run", async (c) => {
  let body: { task?: string } = {};
  try {
    body = (await c.req.json()) as { task?: string };
  } catch {
    return c.json({ success: false, error: "Body must be JSON: { task: 'heartbeat'|'morning-brief' }" }, 400);
  }
  if (body.task !== "heartbeat" && body.task !== "morning-brief") {
    return c.json({ success: false, error: "task must be 'heartbeat' or 'morning-brief'" }, 400);
  }
  const result = pmCadenceScheduler.runManual(body.task);
  return c.json({ success: true, result, status: pmCadenceScheduler.getStatus() });
});

app.post("/pm-cadence/pause", async (c) => {
  let body: { paused?: boolean; task?: string } = {};
  try {
    body = (await c.req.json()) as { paused?: boolean; task?: string };
  } catch {
    return c.json({ success: false, error: "Body must be JSON: { paused: boolean, task?: 'heartbeat'|'morning-brief' }" }, 400);
  }
  if (typeof body.paused !== "boolean") {
    return c.json({ success: false, error: "paused must be boolean" }, 400);
  }
  if (body.task !== undefined && body.task !== "heartbeat" && body.task !== "morning-brief") {
    return c.json({ success: false, error: "task must be 'heartbeat' or 'morning-brief' when provided" }, 400);
  }
  pmCadenceScheduler.setPaused(body.paused, body.task);
  return c.json({ success: true, status: pmCadenceScheduler.getStatus() });
});

// ─── P0 Escalation Watch Endpoints ───────────────────────────
app.get("/p0-escalation-watch/status", (c) => {
  return c.json(p0EscalationWatcher.getStatus());
});

app.post("/p0-escalation-watch/run", async (c) => {
  const result = await p0EscalationWatcher.tick("manual");
  return c.json({ success: true, result, status: p0EscalationWatcher.getStatus() });
});

/** Receive hook from a specific slot */
app.post("/hooks/slot/:slotNum", async (c) => {
  const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
  if (!slotParse.success) {
    return c.json({ error: "Invalid slot number" }, 400);
  }
  const slotNum = slotParse.data;

  let body: unknown;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "Invalid JSON" }, 400);
  }

  const payloadParse = hookPayloadSchema.safeParse(body);
  if (!payloadParse.success) {
    // Log invalid payloads for debugging but still return 200
    // to avoid Claude Code retrying or showing errors
    db.logEvent(slotNum, "invalid_payload", null, null, {
      raw: body,
      errors: payloadParse.error.issues,
    });
    return c.json({});
  }

  const payload = normalizePayload(payloadParse.data);

  // Process the hook event
  const response = await processor.process(slotNum, payload);

  // Return hook response — Claude Code uses this to modify behavior
  return c.json(response);
});

/** Get all slot statuses */
app.get("/slots", (c) => {
  const slots = db.getAllSlots();
  return c.json({ slots });
});

/** Get a single slot's status */
app.get("/slots/:slotNum", (c) => {
  const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
  if (!slotParse.success) {
    return c.json({ error: "Invalid slot number" }, 400);
  }

  const slot = db.getSlot(slotParse.data);
  if (!slot) {
    return c.json({ error: "Slot not found" }, 404);
  }

  return c.json(slot);
});

/** Clear one slot, PM, or all slots through MoP logging. */
app.post("/slots/:slotNum/clear", async (c) => {
  const targetSlots = normalizeClearTarget(c.req.param("slotNum"));
  if (!targetSlots) {
    return c.json({ error: "slotNum must be 0, 1, 2, 3, 4, pm, or all" }, 400);
  }

  let body: { source?: string; clear_existing_pending?: boolean; terminal_only?: boolean } = {};
  try {
    body = await c.req.json();
  } catch {
    body = {};
  }

  const source = body.source ?? "http_clear_endpoint";
  const results = await clearSlotsThroughMopHttp(targetSlots, {
    clearExistingPendingForTargets: body.clear_existing_pending ?? false,
    source,
    terminalOnly: body.terminal_only ?? false,
  });
  return c.json({ ok: true, source, results });
});

/** Clear endpoint that accepts {slot:"pm"|"all"|"0"..}. */
app.post("/clear", async (c) => {
  let body: { slot?: string; source?: string; clear_existing_pending?: boolean; terminal_only?: boolean } = {};
  try {
    body = await c.req.json();
  } catch {
    body = {};
  }

  const targetSlots = normalizeClearTarget(body.slot ?? "all");
  if (!targetSlots) {
    return c.json({ error: "slot must be 0, 1, 2, 3, 4, pm, or all" }, 400);
  }

  const source = body.source ?? "http_clear_endpoint";
  const results = await clearSlotsThroughMopHttp(targetSlots, {
    clearExistingPendingForTargets: body.clear_existing_pending ?? false,
    source,
    terminalOnly: body.terminal_only ?? false,
  });
  return c.json({ ok: true, source, results });
});

/** Get event log (optionally filtered by slot) */
app.get("/events", (c) => {
  const slot = c.req.query("slot")
    ? parseInt(c.req.query("slot")!, 10)
    : undefined;
  const limit = parseInt(c.req.query("limit") ?? "50", 10);
  const eventType = c.req.query("type") ?? undefined;

  const events = db.getEvents(slot, limit, eventType);
  return c.json({ events, count: events.length });
});

/** Get recent activity (last N minutes) */
app.get("/activity", (c) => {
  const minutes = parseInt(c.req.query("minutes") ?? "60", 10);
  const events = db.getRecentActivity(minutes);
  return c.json({ events, count: events.length });
});

/**
 * Review status endpoint — unforgeable gate for PR creation hooks.
 *
 * Checks MoP's event log for actual Skill invocations (codex-app-code-review,
 * zen-code-review, etc.) matching the issue number. Slots cannot forge these
 * entries — only real tool invocations logged by MoP hooks create them.
 *
 * Used by: block-direct-pr-create.sh hook (curl http://localhost:3100/review-status/ISSUE)
 *
 * Query params:
 *   window - minutes to search back (default 60)
 *
 * Debug log: /tmp/mop-review-status-debug.log
 */
app.get("/review-status/:issueNumber", (c) => {
  const issueNumber = parseInt(c.req.param("issueNumber"), 10);
  if (isNaN(issueNumber)) {
    return c.json({ error: "Invalid issue number" }, 400);
  }

  const window = parseInt(c.req.query("window") ?? "60", 10);
  const result = db.findReviewEvent(issueNumber, window);

  // Debug log for traceability (Rajiv directive 2026-04-02)
  const debugLine = `${new Date().toISOString()} | /review-status/${issueNumber} | window=${window}m | found=${result.found} | method=${result.method} | slot=${result.slot} | ts=${result.timestamp}\n`;
  try {
    appendFileSync("/tmp/mop-review-status-debug.log", debugLine);
  } catch { /* ignore log errors */ }

  return c.json({
    issueNumber,
    reviewed: result.found,
    method: result.method,
    timestamp: result.timestamp,
    slot: result.slot,
    details: result.details,
    windowMinutes: window,
  });
});

/** Update slot state (for PM to manage slots) */
app.patch("/slots/:slotNum", async (c) => {
  const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
  if (!slotParse.success) {
    return c.json({ error: "Invalid slot number" }, 400);
  }

  const updates = await c.req.json();
  const current = db.getSlot(slotParse.data);
  if (updates?.dnd === true && current && !current.occupied) {
    updates.dnd = false;
    db.logEvent(slotParse.data, "dnd_free_slot_rejected", null, null, {
      requested: true,
      surface: "rest_patch",
      reason: "free_slot_cannot_be_dnd",
    });
  }
  db.updateSlot(slotParse.data, updates);

  const updated = db.getSlot(slotParse.data);
  return c.json(updated);
});

/** Assign a slot */
app.post("/slots/:slotNum/assign", async (c) => {
  const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
  if (!slotParse.success) {
    return c.json({ error: "Invalid slot number" }, 400);
  }

  const body = await c.req.json();
  const rawPr = body.pr ?? null;
  const pr = rawPr === null ? null : Number(rawPr);
  db.assignSlot(
    slotParse.data,
    body.task ?? "",
    body.issue ?? null,
    body.branch ?? null,
    body.session_id ?? null,
    Number.isInteger(pr) ? pr : null
  );

  db.logEvent(slotParse.data, "slot_assigned", null, null, body);

  const updated = db.getSlot(slotParse.data);
  return c.json(updated);
});

/** Release a slot */
app.post("/slots/:slotNum/release", (c) => {
  const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
  if (!slotParse.success) {
    return c.json({ error: "Invalid slot number" }, 400);
  }

  processor.clearPlanApprovalTimer(slotParse.data);
  db.releaseSlot(slotParse.data);
  db.logEvent(slotParse.data, "slot_released", null, null, {});

  return c.json({ success: true });
});

// ─── Respawn Slot (MoP-orchestrated /exit → launch → continue) ────────

/**
 * Orchestrate a slot respawn — /exit at idle, wait for shell, launch script, wait for claude, inject continue.
 * Replaces slot-side respawn.sh. Suppresses crash notifications via healthChecker.markPmInitiatedRespawn.
 *
 * POST /slots/:slotNum/respawn { continue_session?: boolean, model?: "opus"|"sonnet"|"kimi"|"glm" }
 *
 * Rajiv directive 2026-04-05: "we need to change the respawn behaviour. it should be a MoP command.
 * MoP should inject /exit at idle. then the start command at zsh prompt and inject continue when it
 * is back up. also not send slot crash events to pm."
 *
 * Rajiv directive 2026-04-16: "update the respawn command to accept the model". When `model` is
 * supplied, we inject it as the first positional arg to launch-slot-N.sh — the launch script's
 * shared lib (launch-slot-lib.sh) parses `[model] [--continue|--fresh]` in any order and switches
 * the env vars accordingly (Max subscription for opus/sonnet, Moonshot proxy for kimi, Z.AI for glm).
 */
const ALLOWED_MODELS = new Set(["opus", "sonnet", "kimi", "kimi26", "glm", "gpt55"]);

app.post("/slots/:slotNum/respawn", async (c) => {
  const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
  if (!slotParse.success) return c.json({ error: "Invalid slot number" }, 400);

  const slotNum = slotParse.data;
  const body = await c.req.json().catch(() => ({}));
  const continueSession = body.continue_session !== false; // default true
  const model: string | undefined = typeof body.model === "string" ? body.model : undefined;
  if (model !== undefined && !ALLOWED_MODELS.has(model)) {
    return c.json({
      error: `Invalid model '${model}'. Expected one of: opus, sonnet, kimi, kimi26, glm, gpt55`,
    }, 400);
  }
  const paneAddress = `0:0.${slotNum}`;
  const restartCmd = RESTART_COMMANDS[slotNum];

  if (!restartCmd) {
    return c.json({ error: `No restart command configured for slot ${slotNum}` }, 500);
  }

  // Guard: don't allow concurrent respawns on the same slot.
  if (healthChecker.isPmInitiatedRespawn(slotNum)) {
    return c.json({ error: `Slot ${slotNum} respawn already in progress` }, 409);
  }

  // Guard: slot must be idle before we send /exit. Avoid killing in-flight work.
  const slotState = db.getSlot(slotNum);
  if (slotState && slotState.occupied && !slotState.idle) {
    return c.json({
      error: `Slot ${slotNum} is busy (not idle). Wait for idle before respawning.`,
    }, 409);
  }

  const steps: Array<{ step: string; elapsed_ms: number; detail?: string }> = [];
  const startTime = Date.now();
  const recordStep = (step: string, detail?: string) => {
    steps.push({ step, elapsed_ms: Date.now() - startTime, detail });
  };

  // Mark as PM-initiated to suppress crash notifications.
  healthChecker.markPmInitiatedRespawn(slotNum);
  recordStep("marked_pm_initiated");

  try {
    // Step 1: Inject /exit into the Claude Code session.
    try {
      await execShell(`tmux send-keys -t ${paneAddress} "/exit" Enter`, { timeout: 5_000 });
      recordStep("sent_exit");
    } catch (err) {
      healthChecker.clearPmInitiatedRespawn(slotNum);
      return c.json({
        error: `Failed to send /exit to slot ${slotNum}`,
        detail: String(err),
        steps,
      }, 500);
    }

    // Step 2: Wait for claude to actually exit (pane command transitions to shell).
    const exitTimeout = 20_000;
    const exitDeadline = Date.now() + exitTimeout;
    let exited = false;
    while (Date.now() < exitDeadline) {
      await sleep(500);
      const cmd = await healthChecker.getPaneCommandPublic(slotNum);
      if (cmd && SHELL_COMMANDS.has(cmd)) {
        exited = true;
        recordStep("claude_exited", `shell=${cmd}`);
        break;
      }
    }
    if (!exited) {
      healthChecker.clearPmInitiatedRespawn(slotNum);
      return c.json({
        error: `Claude did not exit after /exit (waited ${exitTimeout}ms)`,
        steps,
      }, 504);
    }

    // Step 3: Send the launch script at the zsh prompt.
    // RESTART_COMMANDS[slot] looks like `bash /abs/path/launch-slot-N.sh --continue`.
    // Rebuild as `bash /abs/path/launch-slot-N.sh [model] [--continue]` so the slot's
    // shared launcher lib can switch env vars based on the model arg.
    const baseCmd = restartCmd.replace(" --continue", "");
    const parts = [baseCmd];
    if (model) parts.push(model);
    if (continueSession) parts.push("--continue");
    const launchCmd = parts.join(" ");
    try {
      await execShell(
        `tmux send-keys -t ${paneAddress} '${launchCmd}' Enter`,
        { timeout: 10_000 },
      );
      recordStep("sent_launch_cmd", launchCmd);
    } catch (err) {
      healthChecker.clearPmInitiatedRespawn(slotNum);
      return c.json({
        error: `Failed to send launch command to slot ${slotNum}`,
        detail: String(err),
        steps,
      }, 500);
    }

    // Step 4: Wait for claude to boot (pane command back to "claude").
    const bootTimeout = 60_000;
    const bootDeadline = Date.now() + bootTimeout;
    let booted = false;
    while (Date.now() < bootDeadline) {
      await sleep(500);
      const cmd = await healthChecker.getPaneCommandPublic(slotNum);
      if (cmd === "claude") {
        booted = true;
        recordStep("claude_booted");
        break;
      }
    }
    if (!booted) {
      healthChecker.clearPmInitiatedRespawn(slotNum);
      return c.json({
        error: `Claude did not boot after launch command (waited ${bootTimeout}ms)`,
        steps,
      }, 504);
    }

    // Step 5: Let the UI settle, then inject "continue" to resume the previous prompt.
    await sleep(2_000);
    if (continueSession) {
      try {
        await execShell(
          `tmux send-keys -t ${paneAddress} 'continue' && tmux send-keys -t ${paneAddress} Enter`,
          { timeout: 5_000 },
        );
        recordStep("sent_continue");
      } catch (err) {
        // Not fatal — Claude is up, user just has to manually send "continue".
        recordStep("continue_inject_failed", String(err));
      }
    }
  } finally {
    // Always clear the flag — restore normal crash detection.
    healthChecker.clearPmInitiatedRespawn(slotNum);
    recordStep("cleared_pm_initiated");
  }

  db.logEvent(slotNum, "slot_respawned", null, null, {
    continue_session: continueSession,
    model: model ?? null,
    duration_ms: Date.now() - startTime,
    steps: steps.map((s) => s.step),
  });

  return c.json({
    success: true,
    slot: slotNum,
    continue_session: continueSession,
    model: model ?? null,
    duration_ms: Date.now() - startTime,
    steps,
  });
});

// ─── Send Command to Slot (Single Gateway) ─────────────

/**
 * Send a command or file content to a dev slot. ALL slot communication
 * goes through this endpoint — send-to-slot.sh calls this instead of
 * tmux directly.
 *
 * If the command is "2" and the slot is awaiting plan approval, this
 * internally routes to the approve-plan handler (with Codex gate).
 *
 * POST /slots/:slotNum/send { command: string, file?: string, force?: boolean }
 *
 * Rajiv directive 2026-03-20: "route all tmux commands through MoP.
 * the send to slot 2 should trigger plan approval internally."
 */
/**
 * Verify pane exists and return a snapshot of recent content for delivery
 * verification. Returns null if the pane doesn't exist or tmux is unreachable.
 */
async function capturePaneSnapshot(paneAddress: string): Promise<string | null> {
  try {
    const result = await execShell(`tmux capture-pane -t ${paneAddress} -p`, { timeout: 5000 });
    return result.stdout;
  } catch {
    return null;
  }
}

/**
 * Confirm a tmux pane address is live (session + window + pane exist).
 * `tmux list-panes -t <pane>` exits 0 only when the address resolves.
 */
async function paneExists(paneAddress: string): Promise<boolean> {
  return execShellOk(`tmux list-panes -t ${paneAddress}`, { timeout: 3000 });
}

function isPmControlCommand(command: string): boolean {
  return command.trim().startsWith("/");
}

function shellWords(input: string): string[] {
  const words: string[] = [];
  let current = "";
  let quote: string | null = null;
  let escaping = false;

  for (const ch of input) {
    if (escaping) {
      current += ch;
      escaping = false;
      continue;
    }
    if (ch === "\\" && quote !== "'") {
      escaping = true;
      continue;
    }
    if (quote) {
      if (ch === quote) {
        quote = null;
      } else {
        current += ch;
      }
      continue;
    }
    if (ch === "'" || ch === "\"") {
      quote = ch;
      continue;
    }
    if (/\s/.test(ch)) {
      if (current) {
        words.push(current);
        current = "";
      }
      continue;
    }
    current += ch;
  }

  if (current) {
    words.push(current);
  }
  return quote || escaping ? [] : words;
}

function parseMessageSlotWrapper(command: string): { targetSlot: number | null; file: string | null; sawMessageToken: boolean } | null {
  const words = shellWords(command.trim());
  const scriptIndex = words.findIndex((word) => /(^|\/)message-slot\.sh$/.test(word));
  if (scriptIndex < 0) {
    return null;
  }

  let targetSlot: number | null = null;
  let file: string | null = null;
  let sawMessageToken = false;
  const args = words.slice(scriptIndex + 1);

  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if ((arg === "--slot" || arg === "--to") && args[i + 1]) {
      targetSlot = Number(args[++i]);
      continue;
    }
    if (arg === "--file" && args[i + 1]) {
      file = args[++i];
      continue;
    }
    if (arg === "--force" || arg === "--wait" || arg === "--allow-command" || arg === "--dry-run") {
      continue;
    }
    if (arg === "--from" && args[i + 1]) {
      i++;
      continue;
    }
    if (!arg.startsWith("-") && targetSlot === null && /^[0-9]+$/.test(arg)) {
      targetSlot = Number(arg);
      continue;
    }
    if (!arg.startsWith("-")) {
      sawMessageToken = true;
    }
  }

  return { targetSlot, file, sawMessageToken };
}

/**
 * Verify keystrokes actually landed in the receiving pane after send.
 * Strategy: post-send pane content must DIFFER from pre-send (Enter cleared
 * input box, or text is now visible, or tool output appeared, or composer
 * scrolled). If the pane is byte-for-byte identical, tmux silently dropped
 * the keystrokes (dead/detached pane, or — worst case — the TUI is in a
 * state that ignores input). Either way: return failure.
 */
async function deliveryConfirmed(paneAddress: string, preSnapshot: string): Promise<{ ok: boolean; reason?: string }> {
  const post = await capturePaneSnapshot(paneAddress);
  if (post === null) {
    return { ok: false, reason: "pane disappeared after send (capture-pane failed)" };
  }
  if (post === preSnapshot) {
    return { ok: false, reason: "post-send pane content identical to pre-send (keystrokes dropped)" };
  }
  return { ok: true };
}

function shellEscape(str: string): string {
  return `'${str.replace(/'/g, "'\\''")}'`;
}

function sendChunkSizeBytes(): number {
  const raw = process.env.MOP_SEND_BUFFER_CHUNK_BYTES;
  const parsed = raw ? Number.parseInt(raw, 10) : NaN;
  return Number.isFinite(parsed) && parsed >= 4096 ? parsed : 48 * 1024;
}

async function pastePayloadWithTmuxBuffer(
  slotNum: number,
  paneAddress: string,
  payload: Buffer,
  meta: { source: "command" | "file"; label: string },
): Promise<{ chunks: number; bytes: number; chunkSize: number }> {
  const chunkSize = sendChunkSizeBytes();
  const bytes = payload.byteLength;
  const chunks = Math.max(1, Math.ceil(bytes / chunkSize));
  const bufName = `mop-send-${slotNum}-${Date.now()}`;

  db.logEvent(slotNum, "send_buffer_start", null, null, {
    source: meta.source,
    label: meta.label.slice(0, 200),
    bytes,
    chunkSize,
    chunks,
    paste: "buffer",
  });
  console.log(
    `[slots/send] slot=${slotNum} source=${meta.source} bytes=${bytes} chunks=${chunks} chunkSize=${chunkSize} paste=buffer`
  );

  for (let index = 0; index < chunks; index++) {
    const start = index * chunkSize;
    const end = Math.min(start + chunkSize, bytes);
    const tmpFile = `/tmp/mop-send-${slotNum}-${Date.now()}-${index + 1}-of-${chunks}.txt`;
    writeFileSync(tmpFile, payload.subarray(start, end));
    try {
      await execShell(`tmux load-buffer -b ${shellEscape(bufName)} ${shellEscape(tmpFile)}`, { timeout: 10_000 });
      await execShell(`tmux paste-buffer -b ${shellEscape(bufName)} -t ${paneAddress} -d`, { timeout: 10_000 });
      db.logEvent(slotNum, "send_buffer_chunk", null, null, {
        source: meta.source,
        chunk: index + 1,
        chunks,
        bytes: end - start,
      });
    } finally {
      try { unlinkSync(tmpFile); } catch {}
    }
    if (chunks > 1) {
      await sleep(150);
    }
  }

  await sleep(bytes > chunkSize ? 1000 : 500);
  await execShell(`tmux send-keys -t ${paneAddress} Enter`, { timeout: 10_000 });
  return { chunks, bytes, chunkSize };
}

app.post("/slots/:slotNum/send", async (c) => {
  const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
  if (!slotParse.success) return c.json({ error: "Invalid slot number" }, 400);

  const slotNum = slotParse.data;
  const body = await c.req.json().catch(() => ({}));
  let command = body.command?.trim() || "";
  let filePath = body.file || "";
  const force = body.force === true;
  const allowPmClear =
    slotNum === 0 &&
    body.allow_pm_clear === true &&
    command === "/clear";
  const paneAddress = `0:0.${slotNum}`;

  if (!command && !filePath) {
    return c.json({ error: "Missing 'command' or 'file' field" }, 400);
  }

  const messageSlotWrapper = command ? parseMessageSlotWrapper(command) : null;
  if (messageSlotWrapper) {
    if (messageSlotWrapper.targetSlot !== slotNum) {
      db.logEvent(slotNum, "send_rejected_message_slot_wrapper", null, null, {
        command: command.slice(0, 200),
        targetSlot: messageSlotWrapper.targetSlot,
        reason: "message_slot_wrapper_target_mismatch",
      });
      return c.json(
        {
          success: false,
          error: `Refused message-slot wrapper command for slot ${messageSlotWrapper.targetSlot ?? "unknown"} on /slots/${slotNum}/send. Execute message-slot.sh locally or call /slots/${messageSlotWrapper.targetSlot}/send.`,
          reason: "message_slot_wrapper_target_mismatch",
        },
        400,
      );
    }
    if (!messageSlotWrapper.file) {
      db.logEvent(slotNum, "send_rejected_message_slot_wrapper", null, null, {
        command: command.slice(0, 200),
        reason: messageSlotWrapper.sawMessageToken ? "message_slot_wrapper_inline_message_blocked" : "message_slot_wrapper_missing_file",
      });
      return c.json(
        {
          success: false,
          error: "Refused to paste message-slot.sh into a dev slot. Execute message-slot.sh locally; file-backed deliveries must use the file transport.",
          reason: messageSlotWrapper.sawMessageToken ? "message_slot_wrapper_inline_message_blocked" : "message_slot_wrapper_missing_file",
        },
        400,
      );
    }
    filePath = messageSlotWrapper.file;
    command = "";
    db.logEvent(slotNum, "send_converted_message_slot_wrapper", null, null, {
      file: filePath,
      reason: "message_slot_wrapper_file_transport",
    });
  }

  // Slot 0 is PM. Dev slots may send PM status text, but must not be able to
  // execute PM-pane slash commands such as /exit, /clear, or /compact.
  if (slotNum === 0 && isPmControlCommand(command) && !allowPmClear) {
    db.logEvent(slotNum, "send_rejected_pm_control_command", null, null, {
      command: command.slice(0, 200),
      force,
      reason: "pm_control_command_blocked",
    });
    return c.json(
      {
        success: false,
        error: "Refused PM-pane slash command. Use message-pm with a plain status body; hard blocks should start with ESCALATION:.",
        reason: "pm_control_command_blocked",
      },
      403,
    );
  }
  if (allowPmClear) {
    db.logEvent(slotNum, "send_allowed_pm_clear_control_command", null, null, {
      command,
      force,
      via: body.source ?? "unknown",
    });
  }

  // ── GATE 1: pane existence ─────────────────────────────
  // tmux can have a dead/detached session. Catching this up-front prevents
  // false-success where send-keys silently fails. (Rajiv directive 2026-05-05)
  if (!(await paneExists(paneAddress))) {
    db.logEvent(slotNum, "send_error", null, null, {
      error: "pane does not exist",
      command: command.slice(0, 100),
      paneAddress,
    });
    return c.json(
      {
        success: false,
        error: `Pane ${paneAddress} does not exist (tmux session detached, or slot not booted). Run /slot-boot ${slotNum} or check tmux session.`,
        reason: "pane_not_found",
      },
      404,
    );
  }

  // Check DND
  const slotState = db.getSlot(slotNum);
  if (slotState?.dnd && !force) {
    return c.json(
      {
        success: false,
        error: `Slot ${slotNum} is DND. Use force: true to override.`,
        reason: "dnd_no_force",
      },
      409,
    );
  }

  // ── GATE 2: force=false on active slot returns failure ─
  // Previous behavior: send-to-slot.sh waited up to 10s for idle, then
  // exited 1; the HTTP route ignored force on the active-slot dimension and
  // pasted regardless. Either path returned a misleading status to the
  // caller. New behavior: explicit force:false on an active slot is a
  // first-class failure with a clear reason. Callers must opt into queued
  // delivery by passing force:true (the new default).
  // (Rajiv directive 2026-05-05 21:31 IST — "should never return success
  // if message was not sent.")
  if (!force && slotNum >= 1 && slotNum <= 4) {
    let active = false;
    try {
      active = await relay.isSlotActive(slotNum);
    } catch {
      active = false;
    }
    if (active) {
      db.logEvent(slotNum, "send_rejected_force_required", null, null, {
        command: command.slice(0, 100),
        force,
      });
      return c.json(
        {
          success: false,
          error: `Slot ${slotNum} is active and force=false. Pass force: true to deliver immediately, or wait for idle.`,
          reason: "slot_active_force_required",
        },
        409,
      );
    }
  }

  // ── GATE: If command is "2" and slot is awaiting plan approval → route to approve-plan
  if (command === "2") {
    const activity = slotState?.activity;
    if (activity === "awaiting_plan_approval") {
      db.logEvent(slotNum, "send_routed_to_approve_plan", null, null, { command, activity });
      // Internally call the approve-plan handler via fetch to localhost
      try {
        const approveRes = await fetch(`http://localhost:${config.httpPort}/slots/${slotNum}/approve-plan`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ option: "2" }),
        });
        const approveData = await approveRes.json() as Record<string, unknown>;
        return c.json(approveData, approveRes.status as 200 | 403 | 408);
      } catch (err: any) {
        return c.json({ success: false, error: `Approve-plan internal call failed: ${err.message?.slice(0, 200)}`, reason: "approve_plan_failed" }, 500);
      }
    }
    // If NOT awaiting plan approval, "2" is just a normal command — send it through
    db.logEvent(slotNum, "send_command_2_not_plan_approval", null, null, { activity });
  }

  // Capture pane snapshot before send, for post-send delivery verification.
  const preSnapshot = (await capturePaneSnapshot(paneAddress)) ?? "";

  try {
    if (filePath) {
      // File mode: load-buffer + paste-buffer, chunked when needed. No payload cap.
      const filePayload = readFileSync(filePath);
      const paste = await pastePayloadWithTmuxBuffer(slotNum, paneAddress, filePayload, {
        source: "file",
        label: filePath,
      });
      // Verify pane content actually changed.
      await sleep(600);
      const verify = await deliveryConfirmed(paneAddress, preSnapshot);
      if (!verify.ok) {
        db.logEvent(slotNum, "send_unverified", null, null, {
          file: filePath,
          paste: "buffer",
          bytes: paste.bytes,
          chunks: paste.chunks,
          reason: verify.reason,
        });
        return c.json(
          {
            success: false,
            error: `Send dispatched but delivery not verified: ${verify.reason}`,
            reason: "delivery_unverified",
          },
          502,
        );
      }
      db.logEvent(slotNum, "send_file", null, null, { file: filePath, paste: "buffer", bytes: paste.bytes, chunks: paste.chunks, chunkSize: paste.chunkSize });
      return c.json({ success: true, mode: "file", slot: slotNum, paste: "buffer", bytes: paste.bytes, chunks: paste.chunks });
    } else {
      // Command mode: detect INSERT/NORMAL, then always paste through tmux buffer.
      let output = "";
      try {
        const result = await execShell(`tmux capture-pane -t ${paneAddress} -p | tail -5`, { timeout: 5000 });
        output = result.stdout;
      } catch { output = ""; }

      const isInsert = /INSERT/.test(output);
      const isNormal = /NORMAL/.test(output);

      if (isNormal) {
        await execShell(`tmux send-keys -t ${paneAddress} i`, { timeout: 5000 });
        await sleep(300);
      }

      const commandPayload = Buffer.from(command, "utf8");
      const paste = await pastePayloadWithTmuxBuffer(slotNum, paneAddress, commandPayload, {
        source: "command",
        label: command.slice(0, 200),
      });

      // Post-send verification.
      await sleep(500);
      const verify = await deliveryConfirmed(paneAddress, preSnapshot);
      if (!verify.ok) {
        db.logEvent(slotNum, "send_unverified", null, null, {
          command: command.slice(0, 200),
          paste: "buffer",
          bytes: paste.bytes,
          chunks: paste.chunks,
          reason: verify.reason,
        });
        return c.json(
          {
            success: false,
            error: `Send dispatched but delivery not verified: ${verify.reason}`,
            reason: "delivery_unverified",
          },
          502,
        );
      }

      db.logEvent(slotNum, "send_command", null, null, {
        command: command.slice(0, 200),
        force,
        mode: isInsert ? "insert" : isNormal ? "normal" : "unknown",
        paste: "buffer",
        bytes: paste.bytes,
        chunks: paste.chunks,
        chunkSize: paste.chunkSize,
      });
      return c.json({ success: true, mode: "command", slot: slotNum, paste: "buffer", bytes: paste.bytes, chunks: paste.chunks });
    }
  } catch (err: any) {
    db.logEvent(slotNum, "send_error", null, null, { error: err.message?.slice(0, 200), command: command.slice(0, 100) });
    return c.json(
      {
        success: false,
        error: `Send failed: ${err.message?.slice(0, 200)}`,
        reason: "tmux_exec_error",
      },
      500,
    );
  }
});

// ─── Plan Approval ──────────────────────────────────────

/**
 * Approve or reject a slot's plan. MoP handles sending the option
 * and verifying the slot becomes active (retries up to 3 times).
 *
 * For approvals (option "2"): verifies that MoP observed a real plan-review
 * tool event after plan-ready was sent. MoP DB events are authoritative here;
 * raw Codex session/transcript files are not used for approval proof.
 * (Constitutional Principle #1 enforcement)
 *
 * POST /slots/:slotNum/approve-plan { option: "2" | "4", comment?: string }
 */
app.post("/slots/:slotNum/approve-plan", async (c) => {
  const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
  if (!slotParse.success) return c.json({ error: "Invalid slot number" }, 400);

  const slotNum = slotParse.data;
  const body = await c.req.json().catch(() => ({}));
  const option = body.option || "2";
  const comment = body.comment || "";
  // skipCodexCheck removed — Codex gate is mandatory, no bypass. (Rajiv directive 2026-03-20)
  const paneAddress = `0:0.${slotNum}`;
  const MAX_RETRIES = 3;

  // ── MoP Plan Review Verification (approvals only) ──────────
  // Constitutional Principle #1: Every plan approval must be backed by a real Codex review.
  // Check that MoP recorded a plan-review tool event AFTER plan-ready was sent.
  if (option === "2") {
    const slotState = db.getSlot(slotNum);
    const issueNum = slotState?.issue;

    // Find when plan-ready was sent for this slot
    const planReadyEvents = db.getEvents(slotNum, 1, "plan_ready_deferred_sent");
    const planReadyHookEvents = db.getEvents(slotNum, 1, "plan_ready_hook");
    const timerEvents = db.getEvents(slotNum, 1, "plan_approval_timer_started");

    // Pick the most recent plan-ready timestamp across all sources
    const allEvents = [...planReadyEvents, ...planReadyHookEvents, ...timerEvents];
    const latestPlanReady = allEvents.reduce((latest, ev) => {
      const evTime = new Date(ev.timestamp).getTime();
      return evTime > latest ? evTime : latest;
    }, 0);

    if (latestPlanReady === 0) {
      db.logEvent(slotNum, "codex_check_no_plan_ready", null, null, { reason: "No plan-ready event found" });
      // Allow approval anyway — plan-ready event might have been lost (MoP restart)
    } else {
      try {
        const cutoff = latestPlanReady - 10_000;
        const events = db.getEvents(slotNum, 500, "PostToolUse");
        const matchingEvent = events.find((event) => {
          const eventTime = new Date(event.timestamp).getTime();
          if (!Number.isFinite(eventTime) || eventTime < cutoff) return false;

          let payload: Record<string, unknown> = {};
          try {
            payload = event.payload ? JSON.parse(event.payload) : {};
          } catch {
            payload = {};
          }

          const toolName = event.tool_name ?? (payload.tool_name as string | undefined) ?? "";
          const toolInput =
            typeof payload.tool_input === "object" && payload.tool_input !== null
              ? (payload.tool_input as Record<string, unknown>)
              : {};
          const rawInput = JSON.stringify(toolInput).toLowerCase();
          const issueText = issueNum ? String(issueNum) : "";
          if (issueText && !rawInput.includes(issueText)) return false;

          if (toolName === "Skill") {
            const skill = String(toolInput.skill ?? "").toLowerCase();
            return ["codex-app-plan-review", "codex-plan-review", "zen-plan-review"].includes(skill);
          }

          if (toolName === "Agent" || toolName === "Task") {
            return rawInput.includes("plan review") && (rawInput.includes("verdict") || rawInput.includes("review"));
          }

          if (toolName === "Bash") {
            const command = String(toolInput.command ?? "").toLowerCase();
            return command.includes("codex-review-companion") && command.includes("--review-type plan");
          }

          return false;
        });

        if (!matchingEvent) {
          db.logEvent(slotNum, "codex_check_failed", null, null, {
            reason: "No MoP plan-review event found after plan-ready",
            plan_ready_at: new Date(latestPlanReady).toISOString(),
            issue: issueNum,
          });
          relay.injectToPM(
            `# ⚠️ CODEX GATE: No MoP plan-review event found after plan-ready for slot ${slotNum} (#${issueNum || "?"}). ` +
            `Plan-ready at ${new Date(latestPlanReady).toISOString()}. Run Codex plan review through the slot before approving.`
          );
          return c.json({
            success: false,
            error: "No MoP plan-review event found after plan-ready. Constitutional Principle #1 requires Codex review before approval.",
            status: "codex_gate_blocked",
            plan_ready_at: new Date(latestPlanReady).toISOString(),
          }, 403);
        }

        db.logEvent(slotNum, "codex_check_passed", null, null, {
          event_id: matchingEvent.id,
          tool_name: matchingEvent.tool_name,
          event_at: matchingEvent.timestamp,
          plan_ready_at: new Date(latestPlanReady).toISOString(),
          issue: issueNum,
        });
      } catch (err: any) {
        // If MoP event inspection itself errors, log but allow (do not wedge PM on control-plane read errors).
        db.logEvent(slotNum, "codex_check_error", null, null, {
          error: err.message?.slice(0, 200),
          issue: issueNum,
        });
      }
    }
  }

  // Phase 1: Wait for the choices prompt to be visible before sending.
  // The prompt can be absent during compaction, rendering, or queued-messages state.
  // Poll pane output for up to 60s looking for the choices prompt. (Rajiv directive 2026-03-18)
  const PROMPT_POLL_MAX = 12; // 12 × 5s = 60s
  const PROMPT_POLL_INTERVAL = 5000;
  const promptPattern = /Would you like to proceed|❯\s*1\.\s*Yes|ctrl-g to edit/i;

  let promptVisible = false;
  for (let poll = 0; poll < PROMPT_POLL_MAX; poll++) {
    // Use tmux capture-pane directly — relay.captureOutput prefers log-based output
    // which doesn't contain the TUI prompt. (Bug fix 2026-03-18)
    let output = "";
    try {
      const raw = await execShell(`tmux capture-pane -t 0:0.${slotNum} -p -S -20`, { timeout: 5_000 });
      output = raw.stdout;
    } catch { output = ""; }
    if (promptPattern.test(output)) {
      promptVisible = true;
      break;
    }
    db.logEvent(slotNum, "plan_approval_waiting_for_prompt", null, null, { poll: poll + 1 });
    await sleep(PROMPT_POLL_INTERVAL);
  }

  if (!promptVisible) {
    db.logEvent(slotNum, "plan_approval_prompt_timeout", null, null, { waited_s: PROMPT_POLL_MAX * 5 });
    return c.json({ success: false, error: "Plan approval prompt not visible after 60s", status: "prompt_timeout" }, 408);
  }

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      // Send the option (2 = approve, 4 = comment)
      if (option === "4" && comment) {
        await execShell(`tmux send-keys -t ${paneAddress} -l '4' && tmux send-keys -t ${paneAddress} Enter`, { timeout: 5000 });
        await sleep(1000);
        const escaped = comment.replace(/'/g, "'\\''");
        await execShell(`tmux send-keys -t ${paneAddress} -l '${escaped}' && tmux send-keys -t ${paneAddress} Enter`, { timeout: 5000 });
      } else {
        await execShell(`tmux send-keys -t ${paneAddress} -l '${option}' && tmux send-keys -t ${paneAddress} Enter`, { timeout: 5000 });
      }

      // Wait longer for the approval to process — slot needs time to
      // render the prompt, receive "2", and trigger ExitPlanMode.
      await sleep(3000);

      // Verify approval landed by checking if activity changed from
      // awaiting_plan_approval. is-active.sh alone is insufficient —
      // the slot can be "active" (processing plan output) without
      // having received the option. (Bug fix 2026-03-16)
      const slotAfter = db.getSlot(slotNum);
      const approvalLanded = slotAfter?.activity !== "awaiting_plan_approval";

      try {
        await execShell(`${process.env.HOME}/.claude/skills/tmux-slot-command/scripts/is-active.sh ${slotNum}`, { timeout: 5000 });
        // Pane is active — approval landed. The pane becoming active IS the success signal.
        // Don't re-check MoP activity state — it may not have updated yet (race condition).
        // (Bug fix 2026-03-21: MoP reported failure after 3 retries even though attempt 1 succeeded,
        //  because activity state hadn't updated from "awaiting_plan_approval" in time.)
        processor.clearPlanApprovalTimer(slotNum);
        db.updateSlot(slotNum, { activity: "implementing" });
        db.logEvent(slotNum, "plan_approved", null, null, { attempt, option });
        return c.json({ success: true, attempt, status: "active" });
      } catch {
        // Exit code 1 = still idle — retry
        if (attempt < MAX_RETRIES) {
          db.logEvent(slotNum, "plan_approval_retry", null, null, { attempt });
          await sleep(1000);
        }
      }
    } catch (err: any) {
      db.logEvent(slotNum, "plan_approval_error", null, null, { attempt, error: err.message?.slice(0, 200) });
    }
  }

  // All retries exhausted
  db.logEvent(slotNum, "plan_approval_failed", null, null, { maxRetries: MAX_RETRIES });
  relay.injectToPM(`# ⚠️ Plan approval failed for slot ${slotNum} after ${MAX_RETRIES} retries`);
  return c.json({ success: false, error: `Approval failed after ${MAX_RETRIES} retries`, status: "idle" });
});

// ─── Slack Message Routing ───────────────────────────────

/** Bot user ID → tmux pane address mapping */
const SLOT_BOT_MAP: Record<string, string> = {
  "U0ALEAYCAUT": "0:0.0",  // Dhruva PM
  "U0AMETSAHC0": "0:0.1",  // Rohini SD
  "U0ALE8Z8X2P": "0:0.2",  // Hasta QA
  "U0AMEUQ8DR6": "0:0.3",  // Ashwini JD
  "U0AMEUZPQ5N": "0:0.4",  // Chitra QA
};

/**
 * Route a Slack message to the correct pane(s) based on @mentions.
 * POST /api/slack-route { text, user, channel, ts, thread_ts? }
 *
 * Routing logic:
 * - If message @mentions a specific slot bot → send to that slot's pane
 * - If message @mentions multiple bots → send to all mentioned panes
 * - If no slot mention → send to PM pane (0:0.0) as default
 * - Always send to PM pane regardless (PM sees everything)
 */
app.post("/api/slack-route", async (c) => {
  const body = await c.req.json();
  const { text, user, channel, ts, formatted } = body;

  if (!text || !formatted) {
    return c.json({ error: "Missing text or formatted" }, 400);
  }

  // Find all @mentioned bot user IDs in the message text
  const mentionPattern = /<@(U[A-Z0-9]+)>/g;
  const mentions = [...text.matchAll(mentionPattern)].map((m: RegExpMatchArray) => m[1]);

  // Determine target panes
  const targetPanes = new Set<string>();

  // Always route to PM
  targetPanes.add("0:0.0");

  // @channel or @here → broadcast to ALL panes
  if (text.includes("<!channel>") || text.includes("<!here>")) {
    for (const pane of Object.values(SLOT_BOT_MAP)) {
      targetPanes.add(pane);
    }
  }

  // Route to mentioned slot panes
  for (const userId of mentions) {
    const pane = SLOT_BOT_MAP[userId];
    if (pane && pane !== "0:0.0") {
      targetPanes.add(pane);
    }
  }

  // Send the formatted message to each target pane via tmux
  const results: string[] = [];
  for (const pane of targetPanes) {
    try {
      // Write to temp file and paste (handles multiline + special chars)
      const tmpFile = `/tmp/slack-route-${Date.now()}.txt`;
      const { writeFileSync, unlinkSync } = await import("node:fs");
      writeFileSync(tmpFile, formatted);
      await execShell(`tmux load-buffer ${tmpFile} && tmux paste-buffer -t ${pane}`, { timeout: 5000 });
      await execShell(`tmux send-keys -t ${pane} Enter`, { timeout: 3000 });
      try { unlinkSync(tmpFile); } catch {}
      results.push(`${pane}: delivered`);
    } catch (e) {
      results.push(`${pane}: failed (${e})`);
    }
  }

  return c.json({ routed: results, mentions, targets: [...targetPanes] });
});

// ─── Start ───────────────────────────────────────────────

const port = config.httpPort;

console.log(`
╔══════════════════════════════════════════╗
║  MoP — Master of Panes                  ║
║  HTTP hooks:  http://localhost:${port}     ║
║  DB:          ${config.dbPath.padEnd(26)}║
║  PM pane:     ${config.pmPaneAddress.padEnd(26)}║
╠══════════════════════════════════════════╣
║  Hook URL:    /hooks/slot/:N             ║
║  Slots API:   /slots, /slots/:N         ║
║  Events:      /events?slot=N&limit=50   ║
║  Activity:    /activity?minutes=60       ║
║  Health:      /health                    ║
╠══════════════════════════════════════════╣
║  Pipe-pane:   /tmp/slot-N.log            ║
║  Stuck watch: 60s check, 5min threshold  ║
║  Log rotate:  10min, 100KB cap           ║
╚══════════════════════════════════════════╝
`);

serve({ fetch: app.fetch, port }, (info) => {
  console.log(`[mop] Server listening on port ${info.port}`);
});

// ─── Graceful Shutdown ───────────────────────────────────

process.on("SIGINT", () => {
  console.log("\n[mop] Shutting down...");
  healthChecker.stop();
  stuckDetector.stop();
  opsAuditScheduler.stop();
  pmCadenceScheduler.stop();
  p0EscalationWatcher.stop();
  clearInterval(rotationTimer);
  clearInterval(eventLoopLagTimer);
  clearInterval(eventRetentionTimer);
  eventLoopHist.disable();
  logManager.disableLogging(config.slotCount);
  db.close();
  process.exit(0);
});

process.on("SIGTERM", () => {
  console.log("[mop] Terminated");
  healthChecker.stop();
  stuckDetector.stop();
  opsAuditScheduler.stop();
  pmCadenceScheduler.stop();
  p0EscalationWatcher.stop();
  clearInterval(rotationTimer);
  clearInterval(eventLoopLagTimer);
  clearInterval(eventRetentionTimer);
  eventLoopHist.disable();
  logManager.disableLogging(config.slotCount);
  db.close();
  process.exit(0);
});
