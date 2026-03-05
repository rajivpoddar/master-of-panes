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

import { Hono } from "hono";
import { serve } from "@hono/node-server";
import { z } from "zod";
import { MoPDatabase } from "./db.js";
import { TmuxRelay } from "./relay.js";
import { HookProcessor } from "./hooks.js";
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

const app = new Hono();

// ─── Validation ──────────────────────────────────────────

const hookPayloadSchema = z.object({
  type: z.enum(["PreToolUse", "PostToolUse", "Notification", "Stop"]),
  tool_name: z.string().optional(),
  tool_input: z.record(z.unknown()).optional(),
  tool_output: z.string().optional(),
  session_id: z.string().optional(),
  notification_type: z.string().optional(),
  stop_reason: z.string().optional(),
  transcript: z.string().optional(),
});

const slotParamSchema = z.coerce.number().int().min(0).max(4);

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
    // Log invalid payloads for debugging
    db.logEvent(slotNum, "invalid_payload", null, null, {
      raw: body,
      errors: payloadParse.error.issues,
    });
    return c.json({ error: "Invalid payload", issues: payloadParse.error.issues }, 400);
  }

  const payload = payloadParse.data as HookPayload;

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

  db.releaseSlot(slotParse.data);
  db.logEvent(slotParse.data, "slot_released", null, null, {});

  return c.json({ success: true });
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
╚══════════════════════════════════════════╝
`);

serve({ fetch: app.fetch, port }, (info) => {
  console.log(`[mop] Server listening on port ${info.port}`);
});

// ─── Graceful Shutdown ───────────────────────────────────

process.on("SIGINT", () => {
  console.log("\n[mop] Shutting down...");
  db.close();
  process.exit(0);
});

process.on("SIGTERM", () => {
  console.log("[mop] Terminated");
  db.close();
  process.exit(0);
});
