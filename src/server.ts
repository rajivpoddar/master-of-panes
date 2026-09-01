/**
 * MoP HTTP Server — Receives Claude Code HTTP hook POSTs
 *
 * Each slot is configured to POST hooks to:
 *   http://localhost:3100/hooks/slot/:slotNum
 *
 * The server:
 * 1. Validates the payload
 * 2. Logs to SQLite
 * 3. Processes core activity/session events
 * 4. Relays explicit messages through tmux
 * 5. Returns a HookResponse that Claude Code acts on
 */

import { readFile, unlink, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { lstatSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { monitorEventLoopDelay } from "node:perf_hooks";
import { Hono } from "hono";
import { serve } from "@hono/node-server";
import { z } from "zod";
import { MoPDatabase } from "./db.js";
import { assignmentIdentityPatchFields } from "./assignmentAuthority.js";
import { registerAssignmentRoute } from "./assignmentRoute.js";
import { registerFamily2Routes } from "./family2Routes.js";
import { TmuxRelay } from "./relay.js";
import { HookProcessor } from "./hooks.js";
import { LogManager } from "./logs.js";
import { ProcessHealthChecker, RESTART_COMMANDS, SHELL_COMMANDS, AGENT_COMMANDS } from "./health.js";
import { execShell, execShellOk, sleep } from "./asyncCommand.js";
import { DEFAULT_CONFIG } from "./types.js";
import {
  NativeSlotReleaseCoordinator,
  type CheckoutReadOnlyObservation,
  type CheckoutResetObservation,
} from "./slotRelease.js";
import type { HookPayload, MoPConfig } from "./types.js";
import { DEFAULT_DEV_SLOT_COUNT, isValidDevSlot } from "./slotConfig.js";
import { paneAddress, verifyPaneIdentity } from "./paneIdentity.js";

// ─── Config ──────────────────────────────────────────────

const config: MoPConfig = {
  ...DEFAULT_CONFIG,
  httpPort: parseInt(process.env.MOP_PORT ?? "3100", 10),
  dbPath: process.env.MOP_DB_PATH ?? DEFAULT_CONFIG.dbPath,
  // Production is intentionally fixed at six numbered dev slots. Migration
  // fixtures may still open older four-slot databases; no runtime env knob can
  // make health, hooks, or routes disagree with this bound.
  slotCount: DEFAULT_DEV_SLOT_COUNT,
  legacyRepositoryId:
    process.env.MOP_LEGACY_REPOSITORY_ID
    ?? DEFAULT_CONFIG.legacyRepositoryId,
};

// ─── Initialize ──────────────────────────────────────────

const db = new MoPDatabase(config);
const relay = new TmuxRelay(config);
// Wire DB into relay so injectToPM can queue when PM is busy.
// Rajiv directive 2026-05-06 11:18 IST.
relay.setDatabase(db);
const processor = new HookProcessor(db, relay);
const releaseResetHelper =
  process.env.MOP_RELEASE_RESET_HELPER
  ?? fileURLToPath(new URL("../scripts/release-slot-reset-and-ack.py", import.meta.url));

function resetAndObserveCheckout(
  checkoutPath: string,
  intendedMainHead: string,
): Promise<CheckoutResetObservation> {
  return new Promise((resolvePromise, rejectPromise) => {
    execFile(
      "python3",
      [
        releaseResetHelper,
        "--checkout", checkoutPath,
        "--intended-main-head", intendedMainHead,
      ],
      { timeout: 180_000, maxBuffer: 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          rejectPromise(new Error(`checkout reset helper failed: ${stderr.trim() || error.message}`));
          return;
        }
        try {
          resolvePromise(JSON.parse(stdout) as CheckoutResetObservation);
        } catch {
          rejectPromise(new Error("checkout reset helper returned invalid JSON"));
        }
      },
    );
  });
}

function readOnlyGit(checkoutPath: string, args: string[]): Promise<string> {
  return new Promise((resolvePromise, rejectPromise) => {
    execFile("git", ["-C", checkoutPath, ...args], { timeout: 30_000, maxBuffer: 1024 * 1024 }, (error, stdout, stderr) => {
      if (error) {
        rejectPromise(new Error(stderr.trim() || error.message));
        return;
      }
      resolvePromise(stdout);
    });
  });
}

async function observeCheckout(checkoutPath: string): Promise<CheckoutReadOnlyObservation> {
  const resolved = resolve(checkoutPath);
  try {
    const stat = lstatSync(resolved);
    if (stat.isSymbolicLink() || !stat.isDirectory()) {
      return { checkout_path: resolved, clean: false, unpushed_commits: [], error: "checkout is not a regular directory" };
    }
    const status = await readOnlyGit(resolved, ["status", "--porcelain", "--untracked-files=all"]);
    const unpushed = (await readOnlyGit(resolved, ["rev-list", "@{upstream}..HEAD"]))
      .split("\n")
      .map((value) => value.trim())
      .filter(Boolean);
    const branch = (await readOnlyGit(resolved, ["branch", "--show-current"])).trim() || null;
    const head = (await readOnlyGit(resolved, ["rev-parse", "HEAD"])).trim().toLowerCase() || null;
    return {
      checkout_path: resolved,
      clean: status.trim() === "" && unpushed.length === 0,
      unpushed_commits: unpushed,
      branch,
      head,
    };
  } catch (error) {
    return {
      checkout_path: resolved,
      clean: false,
      unpushed_commits: [],
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

async function waitForOwningSlotIdle(slot: number): Promise<boolean> {
  const timeoutMs = parseInt(process.env.MOP_RELEASE_IDLE_TIMEOUT_MS ?? "120000", 10);
  const deadline = Date.now() + timeoutMs;
  // Let the just-delivered prompt reach the slot's hook-derived activity state
  // before accepting an idle observation.
  await sleep(500);
  while (Date.now() < deadline) {
    const activity = await relay.getSlotActivityState(slot);
    if (activity === "idle") return true;
    if (activity === "unknown") return false;
    await sleep(250);
  }
  return false;
}

const nativeSlotRelease = new NativeSlotReleaseCoordinator({
  db,
  resolveOwningCheckout: (slot) => relay.getSlotCheckoutPath(slot),
  deliverInstruction: (slot, instruction) => relay.sendToSlotAsync(slot, instruction, true, false),
  owningSlotIsIdle: waitForOwningSlotIdle,
  resetAndObserveCheckout,
  observeCheckout,
});

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

// ─── Process Health (Phase 4) ───────────────────────────
const healthChecker = new ProcessHealthChecker(db, relay);
healthChecker.start();

// ─── Log Rotation (every 10 minutes) ────────────────────
let rotationInFlight = false;
const rotationTimer = setInterval(() => {
  if (rotationInFlight) {
    console.warn("[logs] Skipping overlapping log rotation");
    return;
  }
  rotationInFlight = true;
  void Promise.all(
    Array.from({ length: config.slotCount }, (_, index) =>
      logManager.rotateIfNeeded(index + 1)
    )
  ).finally(() => {
    rotationInFlight = false;
  });
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

// ─── Validation
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
    "SessionEnd",
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

const slotParamSchema = z.coerce.number().int().min(0).max(DEFAULT_DEV_SLOT_COUNT);

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
      healthChecker: !!healthChecker,
      rotationTimer: !!rotationTimer,
      eventLoopLagTimer: !!eventLoopLagTimer,
    },
    slots,
  });
});

/** Restart the MoP server — exits process, session watcher restarts it */
app.post("/restart", (c) => {
  console.log("[mop] Restart requested via /restart endpoint");
  setTimeout(() => process.exit(0), 100);
  return c.json({ status: "restarting" });
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

/** Get event log
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

  const updates = await c.req.json() as Record<string, unknown>;
  const identityFields = assignmentIdentityPatchFields(updates);
  if (identityFields.length > 0) {
    return c.json({
      success: false,
      conflict: true,
      error: "assignment identity must use the guarded assign/release endpoints",
      reason: "assignment_identity_patch_refused",
      identity_fields: identityFields,
    }, 409);
  }
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

/** Assign a slot through the guarded PM authority route. */
registerAssignmentRoute(app, db);

registerFamily2Routes(app, {
  db,
  nativeSlotRelease,
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
  const restartCmd = RESTART_COMMANDS[slotNum];

  if (!restartCmd) {
    return c.json({ error: `No restart command configured for slot ${slotNum}` }, 500);
  }

  const identity = await verifyPaneIdentity(slotNum);
  if (!identity.ok) {
    return c.json({
      error: `Refused respawn for slot ${slotNum}: ${identity.detail}`,
      reason: "pane_identity_mismatch",
    }, 409);
  }
  // Keep the verified immutable pane id for every respawn step. The numeric
  // slot address is only a lookup key and may be rebound by tmux reindexing.
  const paneTarget = identity.snapshot.paneId;

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
  let respawnCompleted = false;

  try {
    // Step 1: Inject /exit into the Claude Code session.
    try {
      await execShell(`tmux send-keys -t ${paneTarget} "/exit" Enter`, { timeout: 5_000 });
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
        `tmux send-keys -t ${paneTarget} '${launchCmd}' Enter`,
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

    // Step 4: Wait for the agent to boot (pane command back to an agent TUI:
    // "claude" or "omp"). omp does not use the literal "continue" prompt below;
    // its launcher resumes via the --continue flag.
    const bootTimeout = 60_000;
    const bootDeadline = Date.now() + bootTimeout;
    let booted = false;
    let bootCommand = "";
    while (Date.now() < bootDeadline) {
      await sleep(500);
      const cmd = await healthChecker.getPaneCommandPublic(slotNum);
      if (cmd && AGENT_COMMANDS.has(cmd)) {
        booted = true;
        bootCommand = cmd;
        recordStep("agent_booted", `cmd=${cmd}`);
        break;
      }
    }
    if (!booted) {
      healthChecker.clearPmInitiatedRespawn(slotNum);
      return c.json({
        error: `Agent did not boot after launch command (waited ${bootTimeout}ms)`,
        steps,
      }, 504);
    }

    // Step 5: Let the UI settle, then inject "continue" to resume the previous
    // prompt. Claude Code needs the literal prompt; omp resumes from the
    // --continue launch flag instead, so skip text injection for omp.
    await sleep(2_000);
    if (continueSession && bootCommand === "claude") {
      try {
        await execShell(
          `tmux send-keys -t ${paneTarget} 'continue' && tmux send-keys -t ${paneTarget} Enter`,
          { timeout: 5_000 },
        );
        recordStep("sent_continue");
      } catch (err) {
        // Not fatal — Claude is up, user just has to manually send "continue".
        recordStep("continue_inject_failed", String(err));
      }
    }
    respawnCompleted = true;
  } finally {
    if (respawnCompleted) {
      // Seed the health-check cooldown before clearing the fence. Otherwise a
      // tick racing the post-launch shell/readback transition can launch the
      // same slot a second time and bypass the controlled respawn receipt.
      healthChecker.completePmInitiatedRespawn(slotNum);
      recordStep("completed_pm_initiated_with_cooldown");
    } else {
      healthChecker.clearPmInitiatedRespawn(slotNum);
      recordStep("cleared_pm_initiated_after_failure");
    }
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
 * POST /slots/:slotNum/send { command: string, file?: string, force?: boolean }
 *
 * All tmux commands remain routed through MoP; policy/orchestration actions
 * are not synthesized by this native delivery surface.
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
    await writeFile(tmpFile, payload.subarray(start, end));
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
      await unlink(tmpFile).catch(() => undefined);
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
  if (!command && !filePath) {
    return c.json({ error: "Missing 'command' or 'file' field" }, 400);
  }

  const identity = await verifyPaneIdentity(slotNum);
  if (!identity.ok) {
    db.logEvent(slotNum, "send_rejected_pane_identity", null, null, {
      reason: identity.reason,
      detail: identity.detail,
      address: paneAddress(slotNum),
    });
    return c.json({
      success: false,
      error: `Refused pane delivery for slot ${slotNum}: ${identity.detail}`,
      reason: "pane_identity_mismatch",
    }, 409);
  }
  // Pin all sends, pastes, retries, and post-send reads in this request to
  // the pane id captured by the one identity probe.
  const paneTarget = identity.snapshot.paneId;

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
  if (slotNum === 0 && isPmControlCommand(command)) {
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
  // ── GATE 1: pane existence ─────────────────────────────
  // tmux can have a dead/detached session. Catching this up-front prevents
  // false-success where send-keys silently fails. (Rajiv directive 2026-05-05)
  if (!(await paneExists(paneTarget))) {
    db.logEvent(slotNum, "send_error", null, null, {
      error: "pane does not exist",
      command: command.slice(0, 100),
      paneAddress: paneTarget,
    });
    return c.json(
      {
        success: false,
        error: `Pane ${paneTarget} does not exist (tmux session detached, or slot not booted). Run /slot-boot ${slotNum} or check tmux session.`,
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
  if (!force && isValidDevSlot(slotNum, config.slotCount)) {
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

  // Capture pane snapshot before send, for post-send delivery verification.
  const preSnapshot = (await capturePaneSnapshot(paneTarget)) ?? "";

  try {
    if (filePath) {
      // File mode: load-buffer + paste-buffer, chunked when needed. No payload cap.
      const filePayload = await readFile(filePath);
      if (slotNum === 0) {
        // PM file deliveries use the same observation-bound submit primitive
        // as command deliveries. Busy/unknown selects C-q; only a proven idle
        // observation selects Enter.
        const submitted = await relay.submitToPM(filePayload.toString("utf8"));
        if (!submitted.ok) {
          return c.json({
            success: false,
            error: "PM pane file submit failed (tmux send-keys).",
            reason: "tmux_exec_error",
            submit: submitted.submitKey,
          }, 500);
        }
        db.logEvent(slotNum, "send_file", null, null, {
          file: filePath,
          bytes: filePayload.byteLength,
          submit: submitted.submitKey,
          verified: true,
          verification: "submit_aware_receipt",
        });
        return c.json({
          success: true,
          mode: "file",
          slot: slotNum,
          submit: submitted.submitKey,
          verified: true,
          bytes: filePayload.byteLength,
        });
      }
      const paste = await pastePayloadWithTmuxBuffer(slotNum, paneTarget, filePayload, {
        source: "file",
        label: filePath,
      });
      // Verify pane content actually changed.
      await sleep(600);
      const verify = await deliveryConfirmed(paneTarget, preSnapshot);
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
        const result = await execShell(`tmux capture-pane -t ${paneTarget} -p | tail -5`, { timeout: 5000 });
        output = result.stdout;
      } catch { output = ""; }

      const isInsert = /INSERT/.test(output);
      const isNormal = /NORMAL/.test(output);

      if (isNormal) {
        await execShell(`tmux send-keys -t ${paneTarget} i`, { timeout: 5000 });
        await sleep(300);
      }

      if (slotNum === 0) {
        // Slot 0 is the PM pane. Ordinary slot→PM status bodies must route
        // through the relay's busy-aware submit primitive (idle → Enter,
        // busy/unknown → C-q) instead of pastePayloadWithTmuxBuffer's
        // always-Enter, so a busy PM never has the active turn steered by an
        // Enter queue-jump.
        //
        // Delivery verification is preserved as a submit-aware receipt: the
        // relay returns ok=true only when the paste (load/paste-buffer) AND
        // the selected submit key (send-keys) all dispatched without a tmux
        // error, and the chosen key is recorded on the send_command event and
        // the response. The pane-diff deliveryConfirmed check does not apply
        // here because C-q queues a follow-up and does NOT enter the active
        // turn — a pane that looks unchanged after the queue is the expected
        // success state.
        const bytes = Buffer.byteLength(command, "utf8");
        const submitted = await relay.submitToPM(command);
        if (!submitted.ok) {
          db.logEvent(slotNum, "send_error", null, null, {
            error: "pm_submit_failed",
            command: command.slice(0, 200),
            submit: submitted.submitKey,
          });
          return c.json(
            {
              success: false,
              error: "PM pane submit failed (tmux send-keys).",
              reason: "tmux_exec_error",
            },
            500,
          );
        }
        db.logEvent(slotNum, "send_command", null, null, {
          command: command.slice(0, 200),
          force,
          mode: isInsert ? "insert" : isNormal ? "normal" : "unknown",
          paste: "buffer",
          submit: submitted.submitKey,
          verified: true,
          verification: "submit_aware_receipt",
          bytes,
          chunks: 1,
        });
        return c.json({
          success: true,
          mode: "command",
          slot: slotNum,
          paste: "buffer",
          submit: submitted.submitKey,
          verified: true,
          bytes,
          chunks: 1,
        });
      }

      const commandPayload = Buffer.from(command, "utf8");
      const paste = await pastePayloadWithTmuxBuffer(slotNum, paneTarget, commandPayload, {
        source: "command",
        label: command.slice(0, 200),
      });

      // Post-send verification.
      await sleep(500);
      const verify = await deliveryConfirmed(paneTarget, preSnapshot);
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

  // Route every text injection through the shared submit primitives. PM uses
  // its canonical observation-bound key; numbered slots always use Enter.
  const results: string[] = [];
  // If PM is one of the targets, submit it first. A failed PM handoff must
  // produce a non-2xx response before any numbered-slot delivery can be
  // acknowledged, otherwise the bridge may lose the Slack event.
  const orderedPanes = [...targetPanes].sort((a, b) => {
    if (a === "0:0.0") return -1;
    if (b === "0:0.0") return 1;
    return 0;
  });
  for (const pane of orderedPanes) {
    try {
      if (pane === "0:0.0") {
        const submitted = await relay.submitToPM(formatted);
        if (!submitted.ok) {
          return c.json({
            routed: [...results, `${pane}: failed (submit=${submitted.submitKey})`],
            mentions,
            targets: [...targetPanes],
            success: false,
            error: "PM submit failed; Slack event was not acknowledged",
            reason: "pm_submit_failed",
            submit: submitted.submitKey,
          }, 502);
        }
        results.push(`${pane}: ${submitted.ok ? "delivered" : "failed"} (submit=${submitted.submitKey})`);
      } else {
        const match = /0:0\.(\d+)$/.exec(pane);
        const slotNum = match ? Number(match[1]) : NaN;
        if (!isValidDevSlot(slotNum, config.slotCount)) {
          results.push(`${pane}: failed (unsupported pane)`);
          continue;
        }
        const sent = await relay.sendToSlotAsync(slotNum, formatted, true, false);
        results.push(`${pane}: ${sent ? "delivered" : "failed"} (submit=Enter)`);
      }
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
  clearInterval(rotationTimer);
  clearInterval(eventLoopLagTimer);
  clearInterval(eventRetentionTimer);
  eventLoopHist.disable();
  logManager.disableLogging(config.slotCount);
  db.close();
  process.exit(0);
});
