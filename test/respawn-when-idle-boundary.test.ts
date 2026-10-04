import assert from "node:assert/strict";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import { Hono } from "hono";
const ROOT = process.env.MOP_RESPAWN_PROOF_ROOT ?? fileURLToPath(new URL("..", import.meta.url));
const load = (name: string) => import(pathToFileURL(join(ROOT, "dist", `${name}.js`)).href);
const idle = await load("respawnWhenIdle");
const turn = await load("respawnTurnGuard");
const { MoPDatabase } = await load("db");
const { TmuxRelay } = await load("relay");
const { DEFAULT_CONFIG } = await load("types");

const PATH = "/Users/rajiv/Downloads/projects/heydonna-app-3001";
const RULE = "─".repeat(40);
async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), "mop-respawn-boundary-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db"), legacyRepositoryId: null });
  db.assignIssueToSlot(1, 10, "fixture", "heydonna-app/heydonna-app");
  db.updateSlot(1, { idle: true, active_turn_state: "inactive", active_turn_id: null });
  db.logEvent(1, "Stop", "Stop", null, { session_id: "s1", cwd: PATH });
  const state = { activity: "idle", pane: "%11", composer: "", buffer: "", exits: 0, pastes: 0,
    probes: 0, identities: 0, routeCalls: 0, loseAck: false, shellAck: true, launches: 0,
    drift: {} as Record<string, unknown>, onProbe: (_n: number) => {}, onIdentity: (_n: number) => {}, onPaste: () => {} };
  const readSlot = db.getSlot.bind(db);
  db.getSlot = slot => { const row = readSlot(slot); return row && { ...row, ...state.drift }; };
  const render = () => `${RULE}\n❯ ${state.composer}\n${RULE}\n ? for shortcuts\n`;
  const identity = async () => {
    state.onIdentity(++state.identities);
    return { ok: true as const, snapshot: { slot: 1, address: "0:0.1", paneId: state.pane, currentPath: PATH, expectedPath: PATH } };
  };
  const runShell = async (command: string) => {
    if (command.startsWith("tmux display-message")) { const i = await identity(); return { stdout: `${i.snapshot.paneId}|${PATH}`, stderr: "" }; }
    if (command.startsWith("git -C")) return { stdout: PATH, stderr: "" };
    if (command.startsWith("tmux capture-pane")) {
      if (state.loseAck && state.exits) throw Error("lost submit acknowledgement");
      if (command.includes(" -e ")) {
        state.onProbe(++state.probes);
        if (state.activity === "unknown") throw Error("activity unavailable");
        if (state.activity === "missing") return { stdout: "", stderr: "" };
        return { stdout: state.activity === "active" ? render().replace("❯", "\x1b[38;2;153;153;153m❯\x1b[0m") : render(), stderr: "" };
      }
      return { stdout: render(), stderr: "" };
    }
    if (command.startsWith("tmux load-buffer")) { state.buffer = await readFile(command.match(/'([^']+)'\s*$/)![1], "utf8"); return { stdout: "", stderr: "" }; }
    if (command.startsWith("tmux paste-buffer")) { state.pastes++; state.composer = state.buffer; state.onPaste(); return { stdout: "", stderr: "" }; }
    if (command.startsWith("tmux send-keys")) {
      if (command.includes('"/exit"') || state.composer === "/exit") { state.exits++; state.composer = ""; }
      if (command.includes("/fake/launch.sh")) state.launches++;
      return { stdout: "", stderr: "" };
    }
    throw Error(`unexpected fake shell: ${command}`);
  };
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell });
  const pinnedProbe = relay.getSlotActivityState.bind(relay);
  // Exercise the actual legacy boolean adapter without executing is-active.sh.
  relay.getSlotActivityState = (slot, pane = "%11") => pinnedProbe(slot, pane);
  const realDelivery = relay.deliverGuardedToSlot.bind(relay);
  relay.deliverGuardedToSlot = (slot, command, opts) => realDelivery(slot, command, {
    ...opts, timing: { dwellMs: 1, pollMs: 1, payloadGraceMs: 10, payloadStableMs: 1, clearGraceMs: 5 },
  });
  const app = new Hono();
  // Register the ACTUAL compiled server route without starting its listener,
  // ticker, hooks, or health processes. All command/DB boundaries are offline.
  const server = await readFile(join(ROOT, "dist/server.js"), "utf8");
  const route = server.slice(server.indexOf("const ALLOWED_MODELS"), server.indexOf("// ─── Respawn When Idle"));
  assert.ok(route.includes('app.post("/slots/:slotNum/respawn"'));
  let paneCommands = 0;
  let clock = 0;
  const globals = { app, db, relay, verifyPaneIdentity: identity, slotParamSchema: { safeParse: (s: string) => ({ success: true, data: +s }) },
    RESTART_COMMANDS: { 1: "bash /fake/launch.sh --continue" }, WEDGED_BUSY_REMEDIATION: "fixture",
    healthChecker: { isPmInitiatedRespawn: () => false, markPmInitiatedRespawn: () => {}, clearPmInitiatedRespawn: () => {}, completePmInitiatedRespawn: () => {}, getPaneCommandPublic: async () => !state.shellAck ? "claude" : ++paneCommands === 1 ? "zsh" : "omp" },
    execShell: runShell, sleep: async (ms: number) => { clock += ms; }, Date: { now: () => clock },
    SHELL_COMMANDS: new Set(["zsh"]), AGENT_COMMANDS: new Set(["omp"]), ...turn, ...idle };
  new Function(...Object.keys(globals), route)(...Object.values(globals));
  const deps = { db, devSlots: [1], verifyPaneIdentity: identity,
    getSlotActivityState: (slot: number, pane: string) => relay.getSlotActivityState(slot, pane),
    isSlotActive: (slot: number) => relay.isSlotActive(slot),
    respawn: async (slot: number, request?: unknown) => {
      state.routeCalls++;
      const response = await app.request(`/slots/${slot}/respawn`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ continue_session: true, idle_request: request }) });
      return { status: response.status, body: await response.json() };
    } };
  idle.registerRespawnWhenIdleRoutes(app, deps);
  const ticker = idle.createRespawnWhenIdleTicker(deps);
  const schedule = () => app.request("/slots/1/respawn-when-idle", { method: "POST" });
  const start = (source = "resume", session = "s1", cwd = PATH) => db.logEvent(1, "SessionStart", "SessionStart", null, { source, session_id: session, cwd });
  return { db, state, ticker, app, schedule, start, cleanup: async () => { db.close(); await rm(directory, { recursive: true, force: true }); } };
}

for (const activity of ["unknown", "missing"]) test(`${activity} actual relay evidence never reaches /exit`, async () => {
  const f = await fixture(); try { await f.schedule(); f.state.activity = activity; await f.ticker.tickSlot(1); assert.equal(f.state.exits, 0); assert.equal(f.state.pastes, 0); assert.equal(f.state.routeCalls, 0); assert.ok(idle.getRespawnWhenIdle(f.db, 1)); } finally { await f.cleanup(); }
});
test("reassigned issue/epoch remains pending and does not respawn", async () => {
  const f = await fixture(); try { await f.schedule(); f.state.drift = { issue: 20, assignment_epoch: 2 }; await f.ticker.tickSlot(1); assert.equal(f.state.exits, 0); assert.ok(idle.getRespawnWhenIdle(f.db, 1)); } finally { await f.cleanup(); }
});
for (const source of ["compact", "clear"]) test(`${source} is not restart completion`, async () => {
  const f = await fixture(); try { await f.schedule(); f.start(source); f.state.activity = "active"; assert.equal(await f.ticker.tickSlot(1), "deferred"); assert.ok(idle.getRespawnWhenIdle(f.db, 1)); } finally { await f.cleanup(); }
});
test("matching resume during awaited activity satisfies once, without /exit", async () => {
  const f = await fixture(); try { await f.schedule(); f.state.onProbe = n => { if (n === 1) f.start(); }; assert.equal(await f.ticker.tickSlot(1), "satisfied"); assert.equal(f.state.exits, 0); assert.equal(f.state.routeCalls, 0); assert.equal(await f.ticker.tickSlot(1), "none"); } finally { await f.cleanup(); }
});
test("foreign SessionStart does not consume the request", async () => {
  const f = await fixture(); try { await f.schedule(); f.start("resume", "foreign"); await f.ticker.tickSlot(1); assert.equal(f.state.exits, 0); assert.ok(idle.getRespawnWhenIdle(f.db, 1)); } finally { await f.cleanup(); }
});
test("normal pinned idle path submits once and retains assignment", async () => {
  const f = await fixture(); try { await f.schedule(); const before = f.db.getSlot(1)!; assert.equal(await f.ticker.tickSlot(1), "fired"); assert.equal(f.state.exits, 1); assert.equal(await f.ticker.tickSlot(1), "none"); assert.equal(f.db.getSlot(1)!.issue, before.issue); assert.equal(f.db.getSlot(1)!.assignment_epoch, before.assignment_epoch); } finally { await f.cleanup(); }
});

for (const drift of ["turn", "epoch", "unknown", "active"]) test(`${drift} changes at the command activity boundary: no paste or /exit`, async () => {
  const f = await fixture(); try {
    await f.schedule();
    f.state.onProbe = n => { if (n !== 2) return;
      if (drift === "turn") f.db.startAgentTurn(1, "new-turn");
      else if (drift === "epoch") f.state.drift = { issue: 20, assignment_epoch: 2 };
      else f.state.activity = drift;
    };
    await f.ticker.tickSlot(1); assert.equal(f.state.exits, 0); assert.equal(f.state.pastes, 0); assert.ok(idle.getRespawnWhenIdle(f.db, 1));
  } finally { await f.cleanup(); }
});
test("pane replacement during the in-lock identity read never pastes", async () => {
  const f = await fixture(); try { await f.schedule(); f.state.onIdentity = n => { if (n === 4) f.state.pane = "%22"; }; await f.ticker.tickSlot(1); assert.equal(f.state.exits, 0); assert.equal(f.state.pastes, 0); assert.ok(idle.getRespawnWhenIdle(f.db, 1)); } finally { await f.cleanup(); }
});
for (const source of ["startup", "resume"]) test(`matching ${source} at the command boundary consumes once`, async () => {
  const f = await fixture(); try { await f.schedule(); f.state.onProbe = n => { if (n === 2) f.start(source); }; assert.equal(await f.ticker.tickSlot(1), "satisfied"); assert.equal(f.state.exits, 0); assert.equal(f.state.pastes, 0); assert.equal(await f.ticker.tickSlot(1), "none"); } finally { await f.cleanup(); }
});
for (const late of ["unknown", "active", "turn", "epoch", "pane"]) test(`${late} after paste refuses Enter and cannot replay`, async () => {
  const f = await fixture(); try {
    await f.schedule(); f.state.onPaste = () => {
      if (late === "turn") f.db.startAgentTurn(1, "late-turn");
      else if (late === "epoch") f.state.drift = { issue: 20, assignment_epoch: 2 };
      else if (late === "pane") f.state.pane = "%22";
      else f.state.activity = late;
    };
    await f.ticker.tickSlot(1); assert.equal(f.state.pastes, 1); assert.equal(f.state.exits, 0); assert.ok(idle.getRespawnWhenIdle(f.db, 1)?.attempted_at);
    await f.ticker.tickSlot(1); assert.equal(f.state.pastes, 1); assert.equal(f.state.exits, 0);
  } finally { await f.cleanup(); }
});
test("lost acknowledgement after one submit preserves attempted record, never a second effect", async () => {
  const f = await fixture(); try { await f.schedule(); f.state.loseAck = true; f.state.shellAck = false; assert.equal(await f.ticker.tickSlot(1), "failed"); assert.equal(f.state.exits, 1); assert.equal(f.state.launches, 0); assert.ok(idle.getRespawnWhenIdle(f.db, 1)?.attempted_at); await f.ticker.tickSlot(1); assert.equal(f.state.exits, 1); assert.equal(f.state.pastes, 1); } finally { await f.cleanup(); }
});
test("TUI disappears after one Enter: proven shell handoff allows exactly one launch", async () => {
  const f = await fixture(); try { await f.schedule(); f.state.loseAck = true; assert.equal(await f.ticker.tickSlot(1), "fired"); assert.equal(f.state.exits, 1); assert.equal(f.state.launches, 1); await f.ticker.tickSlot(1); assert.equal(f.state.launches, 1); } finally { await f.cleanup(); }
});
test("legacy unpinned pending record is retained without guessing an identity", async () => {
  const f = await fixture(); try { f.db.setConfig("respawn_when_idle_1", JSON.stringify({ slot: 1, requested_at: "legacy", assignment_epoch: 1, scheduled_event_id: 0 })); await f.ticker.tickSlot(1); assert.equal(f.state.pastes, 0); assert.equal(idle.getRespawnWhenIdle(f.db, 1)?.requested_at, "legacy"); } finally { await f.cleanup(); }
});
