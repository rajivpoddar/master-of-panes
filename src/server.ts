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

import { execSync } from "node:child_process";
import { Hono } from "hono";
import { serve } from "@hono/node-server";
import { z } from "zod";
import { MoPDatabase } from "./db.js";
import { TmuxRelay } from "./relay.js";
import { HookProcessor } from "./hooks.js";
import { LogManager } from "./logs.js";
import { StuckDetector } from "./stuck.js";
import { ProcessHealthChecker } from "./health.js";
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
const processor = new HookProcessor(db, relay);

// ─── Pane Logging (Phase 2) ─────────────────────────────
const logManager = new LogManager();
logManager.enableLogging(config.slotCount);
relay.setLogManager(logManager);

// ─── Stuck Detection (Phase 3) ──────────────────────────
const stuckDetector = new StuckDetector(db, logManager, relay);
stuckDetector.start();

// ─── Process Health (Phase 4) ───────────────────────────
const healthChecker = new ProcessHealthChecker(db, relay);
healthChecker.start();

// ─── Log Rotation (every 10 minutes) ────────────────────
const rotationTimer = setInterval(() => {
  for (let i = 1; i <= config.slotCount; i++) {
    logManager.rotateIfNeeded(i);
  }
}, 10 * 60 * 1000);

const app = new Hono();

// ─── Validation ──────────────────────────────────────────

// Claude Code HTTP hooks send `hook_event_name` (not `type`), along with
// session_id, cwd, transcript_path, permission_mode, and event-specific
// fields like tool_name/tool_input (PreToolUse/PostToolUse),
// stop_hook_active/last_assistant_message (Stop).
const hookPayloadSchema = z.object({
  // Core fields present in ALL hook events
  hook_event_name: z.enum(["PreToolUse", "PostToolUse", "Notification", "Stop"]),
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
    notification_type: raw.notification_type,
    stop_reason: raw.stop_reason,
    // Preserve useful context
    transcript: raw.last_assistant_message,
  };
}

// ─── Routes ──────────────────────────────────────────────

/** Health check */
app.get("/health", (c) => {
  return c.json({ status: "ok", uptime: process.uptime() });
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
  const response = processor.process(slotNum, payload);

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

/** Update slot state (for PM to manage slots) */
app.patch("/slots/:slotNum", async (c) => {
  const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
  if (!slotParse.success) {
    return c.json({ error: "Invalid slot number" }, 400);
  }

  const updates = await c.req.json();
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
  db.assignSlot(
    slotParse.data,
    body.task ?? "",
    body.issue ?? null,
    body.branch ?? null,
    body.session_id ?? null
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
      execSync(`tmux load-buffer ${tmpFile} && tmux paste-buffer -t ${pane}`, { timeout: 5000 });
      execSync(`tmux send-keys -t ${pane} Enter`, { timeout: 3000 });
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
  clearInterval(rotationTimer);
  logManager.disableLogging(config.slotCount);
  db.close();
  process.exit(0);
});

process.on("SIGTERM", () => {
  console.log("[mop] Terminated");
  healthChecker.stop();
  stuckDetector.stop();
  clearInterval(rotationTimer);
  logManager.disableLogging(config.slotCount);
  db.close();
  process.exit(0);
});
