import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";
import { MoPDatabase } from "../src/db.js";
import { PMSoftBoundary, PM_SOFT_MAX_DEFERRAL_MS, registerPMSoftBoundary } from "../src/pmSoftBoundary.js";
import { TmuxRelay } from "../src/relay.js";
import { deliverRespawnExit } from "../src/respawnWhenIdle.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function fixture() {
  const directory = mkdtempSync(join(tmpdir(), "mop-soft-boundary-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const boot = db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
  let now = Date.now();
  const pending = { key: "daily:test", requestedAt: now, boot, kind: "daily" as const };
  const boundary = new PMSoftBoundary(db, () => pending, () => now);
  const observe = (hook_event_name: string, tool_use_id?: string, extra = {}) => boundary.observe({
    hook_event_name, session_id: "pm", mop_tool_tracking: 1, tool_use_id, ...extra,
  });
  return { db, boot, pending, boundary, observe, advance: (ms: number) => { now += ms; },
    close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); } };
}

test("parallel tools and failures: a Stop never bypasses an admitted in-flight tool", () => {
  const f = fixture();
  try {
    assert.equal(f.observe("PreToolUse", "a"), null);
    f.observe("PreToolUse", "b");
    f.observe("PostToolUse", "a");
    f.advance(PM_SOFT_MAX_DEFERRAL_MS * 2);
    f.observe("Stop");
    assert.equal(f.boundary.isSafe(), false);
    assert.equal(f.boundary.isHeld(), false);
    assert.equal(f.boundary.status().tools_in_flight, 1);
    f.observe("PostToolUseFailure", "b");
    f.observe("Stop");
    assert.equal(f.boundary.isSafe(), true);
    assert.equal(f.boundary.isHeld(), true);
    assert.match(f.observe("PreToolUse", "c")!, /fenced/);
    assert.equal(f.boundary.status().tools_in_flight, 0);
  } finally { f.close(); }
});

test("45-minute policy waits for the next identified Stop, retaining queued messages", () => {
  const f = fixture();
  try {
    f.db.enqueuePendingPMEvent(0, "one", "first message");
    f.db.enqueuePendingPMEvent(0, "two", "second message");
    const before = f.db.peekPendingPMEvents();
    f.observe("Stop");
    assert.equal(f.boundary.isHeld(), false);
    f.advance(PM_SOFT_MAX_DEFERRAL_MS - 1);
    f.observe("Stop");
    assert.equal(f.boundary.isHeld(), false);
    f.advance(1);
    assert.equal(f.boundary.isHeld(), false, "elapsed time alone never interrupts work");
    f.observe("Stop", undefined, { mop_tool_tracking: undefined });
    assert.equal(f.boundary.isHeld(), false, "legacy untracked Stop is not authority");
    f.observe("Stop");
    assert.equal(f.boundary.isHeld(), true);
    assert.deepEqual(f.db.peekPendingPMEvents(), before);
  } finally { f.close(); }
});

test("session, tool-id and duplicate fences survive MoP restart", () => {
  const f = fixture();
  try {
    assert.match(f.observe("PreToolUse")!, /identified/);
    assert.match(f.observe("PreToolUse", "a", { session_id: "old" })!, /session/);
    f.observe("PreToolUse", "a");
    f.observe("PreToolUse", "a");
    f.observe("PostToolUse", "a", { session_id: "old" });
    f.observe("PostToolUse", "not-a");
    assert.equal(f.boundary.status().tools_in_flight, 1);
    let restored = new PMSoftBoundary(f.db, () => f.pending);
    assert.equal(restored.status().tools_in_flight, 1);
    f.observe("PostToolUse", "a");
    f.observe("Stop");
    restored = new PMSoftBoundary(f.db, () => f.pending);
    assert.equal(restored.isHeld(), true);
    assert.match(restored.observe({ hook_event_name: "PreToolUse", session_id: "pm", tool_use_id: "new", mop_tool_tracking: 1 })!, /fenced/);
  } finally { f.close(); }
});

test("successor session cannot inherit an old checkpoint or complete an old tool", () => {
  const f = fixture();
  try {
    f.observe("PreToolUse", "a");
    f.db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "successor" });
    f.boundary.sessionStarted("startup");
    f.observe("PostToolUse", "a");
    f.observe("Stop", undefined, { session_id: "successor" });
    assert.equal(f.boundary.isHeld(), false);
    assert.equal(f.boundary.status().tools_in_flight, 0);
  } finally { f.close(); }
});

test("respawn acknowledgement requires a new boot; uncertain attempts are never replayed", () => {
  const f = fixture();
  try {
    f.observe("Stop");
    assert.equal(f.boundary.beginRespawn("wrong"), false);
    assert.equal(f.boundary.beginRespawn(f.pending.key), true);
    assert.equal(f.boundary.beginRespawn(f.pending.key), false);
    f.boundary.respawnAcknowledged();
    assert.equal(f.boundary.isHeld(), true, "HTTP success without a boot is not acknowledgement");
    assert.equal(f.db.getEvents(0, 10, "slot_cleared").length, 0);
    f.db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
    f.boundary.sessionStarted("resume");
    assert.equal(f.boundary.isHeld(), true, "respawn keeps the fence through the guarded response");
    f.boundary.respawnAcknowledged();
    f.boundary.respawnAcknowledged();
    assert.equal(f.boundary.isHeld(), false);
    assert.equal(f.db.getEvents(0, 10, "slot_cleared").length, 1);
    assert.equal(f.db.getEvents(0, 10, "clear_pending_executed").length, 1);
  } finally { f.close(); }
});

test("real relay prioritizes fenced maintenance over a nonempty queue, then replays once", async () => {
  const f = fixture();
  try {
    let handoffs = 0;
    const commands: string[] = [];
    const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude", pmPaneReady: async () => true,
      pmIdleHandoff: async () => { handoffs++; },
      runShell: async command => { commands.push(command); return { stdout: command.includes("display-message") ? "%40|claude" : command.includes("capture-pane") ? "─".repeat(30) + "\n❯ \n" + "─".repeat(30) : "", stderr: "", code: 0 }; },
    });
    relay.setDatabase(f.db);
    relay.setPMMaintenanceGate(f.boundary);
    f.db.enqueuePendingPMEvent(0, "original", "retain original");
    f.advance(PM_SOFT_MAX_DEFERRAL_MS);
    f.observe("Stop");
    relay.injectToPMDirect("retain direct");
    const submitted = await relay.submitToPM("retain HTTP send");
    assert.equal(submitted.queued, true);
    // Invoke the real debounce control point without waiting 30 seconds.
    await (relay as unknown as { runDrainCheck(ms: number, generation: number): Promise<void> }).runDrainCheck(0, 0);
    assert.equal(handoffs, 1);
    assert.equal(commands.filter(command => /paste-buffer|send-keys/.test(command)).length, 0);
    assert.equal(f.db.getPendingPMEventCount(), 3);
    assert.equal(await relay.drainPMQueue(), 0);
    f.db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "fresh" });
    f.boundary.sessionStarted("clear");
    assert.equal(await relay.drainPMQueue(), 3);
    assert.equal(f.db.getPendingPMEventCount(), 0);
    assert.equal(await relay.drainPMQueue(), 0);
    assert.equal(commands.filter(command => command.includes("paste-buffer")).length, 3);
  } finally { f.close(); }
});

test("HTTP admission fences tools before the processor and retains new inbound prompts", async () => {
  const f = fixture();
  try {
    const relay = new TmuxRelay(DEFAULT_CONFIG, { pmPaneReady: async () => true });
    relay.setDatabase(f.db);
    const app = new Hono();
    const boundary = registerPMSoftBoundary(app, f.db, relay);
    let processed = 0;
    app.post("/hooks/slot/0", c => { processed++; return c.json({}); });
    const post = async (event: string, extra = {}) => (await app.request("/hooks/slot/0", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hook_event_name: event, session_id: "pm", mop_tool_tracking: 1, ...extra }),
    })).json();
    assert.equal((await post("PreToolUse", { tool_use_id: "a" })).mop_tool_admitted, true);
    await post("PostToolUseFailure", { tool_use_id: "a" });
    f.db.setPendingClear(0);
    f.db.setConfig("pm_clear_delivery_state", "deferred_busy");
    f.db.setConfig("pm_clear_requested_at", new Date(Date.now() - PM_SOFT_MAX_DEFERRAL_MS).toISOString());
    // Claim synchronously through the production controller, without arming a
    // real relay timer or interacting with the live PM.
    boundary.observe({ hook_event_name: "Stop", session_id: "pm", mop_tool_tracking: 1 });
    const before = processed;
    const denied = await post("PreToolUse", { tool_use_id: "b" });
    assert.equal(denied.hookSpecificOutput.permissionDecision, "deny");
    assert.equal(processed, before);
    const blocked = await post("UserPromptSubmit", { prompt: "inbound during maintenance" });
    assert.equal(blocked.decision, "block");
    assert.equal(f.db.peekPendingPMEvents()[0].payload, "inbound during maintenance");
    await post("UserPromptSubmit", { prompt: "inbound during maintenance" });
    assert.equal(f.db.getPendingPMEventCount(), 1, "duplicate hook does not duplicate replay");
  } finally { f.close(); }
});

test("same-session compaction does not forget an in-flight tool or cancel deferred intent", () => {
  const f = fixture();
  try {
    f.observe("PreToolUse", "a");
    f.pending.boot = f.db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm", source: "compact" });
    f.boundary.sessionStarted("compact");
    assert.equal(f.boundary.status().tools_in_flight, 1);
    f.advance(PM_SOFT_MAX_DEFERRAL_MS);
    f.observe("Stop");
    assert.equal(f.boundary.isHeld(), false);
    f.observe("PostToolUse", "a");
    f.observe("Stop");
    assert.equal(f.boundary.isHeld(), true);
  } finally { f.close(); }
});


test("CLI queued input, including the empty-looking placeholder, prevents maintenance", async () => {
  const f = fixture();
  try {
    let handoffs = 0;
    let queued = true;
    const commands: string[] = [];
    const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude",
      pmIdleHandoff: async () => { handoffs++; },
      runShell: async command => {
        commands.push(command);
        return { stdout: command.includes("display-message") ? "%40|claude"
          : "─".repeat(30) + "\n❯ " + (queued ? "press up to edit queued messages" : "") + "\n" + "─".repeat(30), stderr: "", code: 0 };
      },
    });
    relay.setDatabase(f.db);
    relay.setPMMaintenanceGate(f.boundary);
    f.db.enqueuePendingPMEvent(0, "retained", "retained across CLI work");
    f.advance(PM_SOFT_MAX_DEFERRAL_MS);
    f.observe("Stop");
    const drain = () => (relay as unknown as { runDrainCheck(ms: number, generation: number): Promise<void> }).runDrainCheck(0, 0);
    await drain();
    assert.equal(handoffs, 0, "45 minutes never authorizes clearing CLI-queued input");
    assert.equal(f.boundary.isHeld(), true, "MoP queue stays held");
    assert.equal(f.boundary.isAdmissionFenced(), false, "CLI-queued work can finish");
    assert.equal(f.db.getPendingPMEventCount(), 1);
    assert.equal(f.observe("PreToolUse", "queued-work"), null);
    f.observe("PostToolUse", "queued-work");
    queued = false;
    f.observe("Stop");
    await drain();
    assert.equal(handoffs, 1, "later tool-free and CLI-empty Stop executes maintenance");
    assert.equal(f.boundary.isAdmissionFenced(), true);
    assert.equal(f.db.getPendingPMEventCount(), 1);
    assert.equal(commands.some(command => /paste-buffer|send-keys/.test(command)), false);
  } finally { f.close(); }
});


test("PM respawn uses hook authority without a dev row and rejects CLI queue at both effects", async () => {
  const f = fixture();
  try {
    assert.equal(f.db.getSlot(0), undefined, "production PM has no dev-slot row");
    f.observe("Stop");
    const pane = { slot: 0, address: "0:0.0", paneId: "%40", currentPath: "/synthetic/pm", expectedPath: "/synthetic/pm" };
    for (const queuedAt of ["pre_paste", "pre_enter", "never"]) {
      let effects = 0;
      const result = await deliverRespawnExit({
        db: f.db, slot: 0, pane,
        verifyPaneIdentity: async () => ({ ok: true as const, snapshot: pane }),
        pmMaintenanceFence: () => f.boundary.isSafe() ? null : "slot_busy_respawn_refused",
        beforeEffect: () => null,
        relay: {
          getSlotActivityState: async () => "idle" as const,
          deliverGuardedToSlot: async (_slot, _command, options) => {
            for (const stage of ["pre_paste", "pre_enter"] as const) {
              const text = stage === "pre_enter" ? "/exit" : "";
              const snapshot = "─".repeat(30) + "\n❯ " + text + "\n" + "─".repeat(30)
                + (stage === queuedAt ? "\nctrl+x ctrl+s to send now" : "");
              const refusal = await options.recheck(stage, async () => snapshot) ?? options.finalCheck?.(stage);
              if (refusal) return { outcome: effects ? "uncertain" as const : "refused_pre_effect" as const, reason: refusal, paneId: pane.paneId };
              effects++;
            }
            return { outcome: "delivered" as const, paneId: pane.paneId };
          },
        },
      });
      assert.equal(effects, queuedAt === "pre_paste" ? 0 : queuedAt === "pre_enter" ? 1 : 2);
      assert.equal(result.outcome, queuedAt === "never" ? "delivered" : queuedAt === "pre_paste" ? "refused_pre_effect" : "uncertain");
    }
  } finally { f.close(); }
});
