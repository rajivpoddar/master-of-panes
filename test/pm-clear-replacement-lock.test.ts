import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";
import { MoPDatabase } from "../src/db.js";
import { isPmClearUndelivered, makePmClearWriteFence, retryDeferredPmClearOnStop } from "../src/pmClearLatch.js";
import { registerPMSoftBoundary } from "../src/pmSoftBoundary.js";
import { TmuxRelay } from "../src/relay.js";
import { deliverRespawnExit } from "../src/respawnWhenIdle.js";
import { withSlotSendLock } from "../src/slotSendLock.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function barrier() {
  let open!: () => void;
  let reached!: () => void;
  const waiting = new Promise<void>(r => { reached = r; });
  const released = new Promise<void>(r => { open = r; });
  return { waiting, open, pause: async () => { reached(); await released; } };
}

// Execute the server's actual launch lock/rechecks/effects, not a copied writer.
const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
const launchStart = server.indexOf("const launchRefusal = await withSlotSendLock(slotNum, async () => {");
const launchEnd = server.indexOf("      if (launchRefusal)", launchStart);
assert.ok(launchStart > 0 && launchEnd > launchStart);
const launchBody = server.slice(launchStart, launchEnd);

async function fixture(holdAt: string) {
  const directory = mkdtempSync(join(tmpdir(), "mop-clear-replacement-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const path = "/Users/rajiv/Downloads/projects/heydonna-app";
  const pane = { slot: 0, address: "0:0.0", paneId: "%1", currentPath: path, expectedPath: path };
  let session = String(db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" }));
  let generation = 0, composer = "", buffer = "", command = "claude", paused = false;
  const commands: string[] = [], effects: string[] = [];
  const gate = barrier();
  const runShell = async (cmd: string) => {
    commands.push(cmd);
    const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(cmd);
    if (load) buffer = readFileSync(load[1]!, "utf8");
    const stage = load ? `${buffer}:pre-paste`
      : cmd.startsWith("tmux send-keys -l") ? "launch:text"
      : cmd.endsWith(" Enter") ? `${composer === "/exit" || composer === "/clear" ? composer : "launch"}:Enter` : "";
    if (!paused && stage === holdAt) { paused = true; await gate.pause(); }
    if (cmd.includes("#{pane_current_path}")) return { stdout: `%1|${path}`, stderr: "" };
    if (cmd.startsWith("git -C")) return { stdout: path, stderr: "" };
    if (cmd.includes("#{pane_id}|#{pane_current_command}")) return { stdout: `%1|${command}`, stderr: "" };
    if (cmd.startsWith("tmux capture-pane")) return { stdout: `${"─".repeat(40)}\n❯ ${composer}\n${"─".repeat(40)}`, stderr: "" };
    if (cmd.startsWith("tmux paste-buffer")) { composer = buffer; effects.push(`paste:${buffer}`); }
    if (cmd.startsWith("tmux send-keys -l")) { composer = "launch"; effects.push("launch:text"); }
    if (cmd.endsWith(" Enter")) { effects.push(`Enter:${composer}`); if (composer === "/exit") command = "zsh"; composer = ""; }
    if (cmd.endsWith(" C-u")) { effects.push(`cleanup:${composer}`); composer = ""; }
    return { stdout: "", stderr: "" };
  };
  const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude", runShell });
  relay.setDatabase(db);
  // Keep the production guarded writer, replacing only external activity observation.
  relay.getSlotActivityState = async () => "idle";
  const app = new Hono();
  let delivery: Promise<unknown> = Promise.resolve();
  const requestedAt = new Date().toISOString();
  const boundary = registerPMSoftBoundary(app, db, relay, () => {
    if (!isPmClearUndelivered(db)) return Promise.resolve();
    const pin = generation;
    const boot = session;
    delivery = retryDeferredPmClearOnStop({ db, source: "pm_native_stop", nowMs: Date.now(),
      staleAfterMs: 0, recentSuppressMs: 0, hasRecentClearEvent: false, laterLifecycleEvent: null,
      backoffMs: 0, settleMs: 0, wait: async () => { throw new Error("no timer"); },
      isCurrent: () => pin === generation, isIdleProven: () => false,
      send: async () => {
        const result = await boundary.withMaintenanceWriter(() => relay.writePMClearFenced({ paneId: "%1",
          check: makePmClearWriteFence({ db, requestedAt, generation: pin, sessionToken: boot,
            currentGeneration: () => generation, currentSessionToken: () => session, idleProven: () => false }) }));
        return { success: result.ok, zeroEffect: result.zeroEffect, busy: result.busy };
      } });
    return delivery;
  });
  app.post("/hooks/slot/0", async c => {
    const raw = await c.req.json();
    if (["UserPromptSubmit", "SessionStart"].includes(raw.hook_event_name)) generation++;
    if (raw.hook_event_name === "SessionStart") session = String(db.logEvent(0, "SessionStart", "SessionStart", null, raw));
    return c.json({});
  });
  const identity = async () => ({ ok: true as const, snapshot: pane });
  const launch = new Function("withSlotSendLock", "verifyPaneIdentity", "readPinnedCommand", "sessionFence", "execShell", "SHELL_COMMANDS",
    `return async function(slotNum, paneTarget, identity, originalSession, launchCmd) { ${launchBody} return launchRefusal; }`)(
      withSlotSendLock, identity, async () => command,
      (original: string) => original === session ? null : "respawn_session_drift", runShell, new Set(["zsh"]));
  app.post("/slots/0/respawn", async c => {
    const original = session;
    const exit = await deliverRespawnExit({ db, relay, slot: 0, pane, verifyPaneIdentity: identity,
      pmMaintenanceFence: () => original === session && boundary.isSafe() ? null : "respawn_session_drift", beforeEffect: () => null });
    if (exit.outcome !== "delivered") return c.json({ success: false, reason: exit.reason }, 409);
    const refusal = await launch(0, "%1", { snapshot: pane }, original, "bash launch-pm.sh");
    return c.json({ success: !refusal, reason: refusal }, refusal ? 409 : 200);
  });
  const hook = (event: string, extra = {}) => app.request("/hooks/slot/0", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hook_event_name: event, session_id: "pm", mop_tool_tracking: 1, ...extra }) });
  const pending = () => {
    db.setPendingClear(0); db.setConfig("pm_clear_requested_at", requestedAt); db.setConfig("pm_clear_delivery_state", "deferred_busy");
  };
  // Establish a real daily claim before accepting the separate clear intent.
  db.setConfig("pm_soft_boundary_v1", "");
  // The controller normally obtains this claim from its existing daily checkpoint.
  const controller = boundary as unknown as { state: { request: string | null; fenced: boolean; stopped: boolean; boot: number; session: string } };
  controller.state.request = "daily:fixture"; controller.state.fenced = true; controller.state.stopped = true;
  controller.state.boot = Number(session); controller.state.session = "pm";
  const restarts: Promise<Response>[] = [];
  const restart = () => {
    const job = Promise.resolve(app.request("/slots/0/respawn", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pm_soft_boundary_request: "daily:fixture" }) }));
    restarts.push(job);
    return job;
  };
  return { db, commands, effects, gate, hook, pending, restart, requestedAt, delivery: () => delivery,
    close: async () => {
      gate.open(); await Promise.allSettled([delivery, ...restarts]);
      relay.setPMBusy(true); db.close(); rmSync(directory, { recursive: true, force: true });
    } };
}

for (const stage of ["/exit:pre-paste", "/exit:Enter", "launch:text", "launch:Enter"]) {
  test(`real restart owns ${stage}: native Stop clear cannot splice into replacement`, async () => {
    const f = await fixture(stage);
    try {
      const restart = f.restart();
      await f.gate.waiting;
      f.pending();
      const probesBeforeStop = f.commands.length;
      await f.hook("Stop");
      // Drain microtasks, not a timer/idle heuristic: a lock waiter must issue no probe/effect.
      await new Promise<void>(r => setImmediate(r));
      assert.equal(f.commands.length, probesBeforeStop, "clear cannot inspect a composer owned by the paused restart writer");
      assert.equal(f.effects.some(e => e.includes("/clear")), false);
      f.gate.open();
      assert.equal((await restart).status, 200);
      await f.delivery();
      assert.deepEqual(f.effects, ["cleanup:", "paste:/exit", "Enter:/exit", "launch:text", "Enter:launch"]);
      assert.equal(f.db.getConfig("pm_clear_requested_at"), f.requestedAt);
      assert.equal(f.db.getConfig("pm_clear_delivery_state"), "deferred_busy");
      assert.equal(f.db.getEvents(0, 10, "pm_clear_write_refused")[0]?.payload.includes("pm_respawn_in_progress"), true);
    } finally { await f.close(); }
  });
}

for (const stage of ["/clear:pre-paste", "/clear:Enter"]) {
  test(`reverse ownership at ${stage}: no overlapping clear and respawn effects`, async () => {
    const f = await fixture(stage);
    try {
      f.pending();
      await f.hook("Stop");
      await f.gate.waiting;
      const restart = f.restart();
      await new Promise<void>(r => setImmediate(r));
      assert.equal(f.effects.some(e => e.includes("/exit")), false);
      f.gate.open();
      const response = await restart;
      await f.delivery();
      if (stage === "/clear:pre-paste") {
        assert.equal(response.status, 200);
        assert.deepEqual(f.effects, ["cleanup:", "paste:/exit", "Enter:/exit", "launch:text", "Enter:launch"]);
        assert.equal(f.db.getConfig("pm_clear_delivery_state"), "deferred_busy");
      } else {
        assert.equal(response.status, 409, "the clear composer cannot be commandeered by restart");
        assert.deepEqual(f.effects, ["paste:/clear", "Enter:/clear"]);
        assert.equal(f.db.getConfig("pm_clear_delivery_state"), "awaiting_ack");
        await f.hook("Stop"); await f.delivery();
        assert.equal(f.effects.length, 2, "possible-effect clear is never replayed");
      }
    } finally { await f.close(); }
  });
}

for (const drift of ["input", "session", "successor"]) {
  test(`queued clear rechecks ${drift} after real restart writer ownership`, async () => {
    const f = await fixture("/exit:pre-paste");
    try {
      const restart = f.restart(); await f.gate.waiting;
      f.pending(); await f.hook("Stop");
      if (drift === "input") await f.hook("UserPromptSubmit");
      if (drift === "session") await f.hook("SessionStart", { session_id: "successor", source: "resume" });
      if (drift === "successor") f.db.setConfig("pm_clear_requested_at", "successor-request");
      f.gate.open(); await restart; await f.delivery();
      assert.equal(f.effects.some(e => e.includes("/clear")), false);
      if (drift === "successor") assert.equal(f.db.getConfig("pm_clear_requested_at"), "successor-request");
      else assert.equal(f.db.getConfig("pm_clear_requested_at"), f.requestedAt);
    } finally { await f.close(); }
  });
}
