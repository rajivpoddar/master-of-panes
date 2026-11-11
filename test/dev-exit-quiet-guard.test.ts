import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import test from "node:test";
import { Hono } from "hono";
import ts from "typescript";
import { HookProcessor } from "../src/hooks.js";
import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { isGatedDevSlot, TmuxRelay } from "../src/relay.js";

function harness(options: { active?: boolean; waitForExitQuiet?: (ms: number) => Promise<void> } = {}) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  let now = Date.now();
  const ready = db.exitQuietReady.bind(db);
  db.exitQuietReady = (slot: number) => ready(slot, now);
  db.setExitPending(true, true);
  db.logEvent(1, "SessionStart", "SessionStart", null, { session_id: "original" });
  db.startAgentTurn(1, "original");
  if (!options.active) db.finishAgentTurn(1, "original");
  const rule = "─".repeat(40);
  const state = { composer: "", buffer: "", marker: "", failCapture: false, enters: 0,
    paneId: "%40", process: "100 100 S+ Mon Nov 2 00:00:00 2026 claude", keys: [] as string[] };
  const path = "/Users/rajiv/Downloads/projects/heydonna-app-3001";
  const shell = async (command: string) => {
    let stdout = "";
    if (command.startsWith("tmux display-message")) stdout = command.includes("pane_pid") ? `${state.paneId}|100|/dev/pts/1|${path}|claude\n` : `${state.paneId}|${path}\n`;
    else if (command.startsWith("git -C")) stdout = path + "\n";
    else if (command.startsWith("ps -t")) stdout = state.process;
    else if (command.startsWith("tmux capture-pane")) {
      if (state.failCapture) throw new Error("capture failed");
      stdout = `${state.marker}${rule}\n❯ ${state.composer}\n${rule}`;
    } else if (command.startsWith("tmux load-buffer")) state.buffer = await readFile(command.match(/'([^']+)'\s*$/)![1], "utf8");
    else if (command.startsWith("tmux paste-buffer")) state.composer = state.buffer;
    else if (command.endsWith(" Enter")) { state.enters++; state.composer = ""; }
    else if (command.endsWith(" C-u")) state.composer = "";
    else if (command.startsWith("tmux send-keys")) state.keys.push(command);
    else throw new Error(`unexpected shell: ${command}`);
    return { stdout, stderr: "" };
  };
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: shell, waitForExitQuiet: options.waitForExitQuiet });
  relay.setDatabase(db);
  relay.injectToPM = () => true;
  (relay as any).slashSubmitTiming = () => ({ dwellMs: 0, pollMs: 5, payloadStableMs: 1, payloadGraceMs: 40, clearGraceMs: 20 });
  const dispatch = () => {
    db.logEvent(1, "Stop", "Stop", null, { session_id: "original" });
    return relay.deliverGuardedToSlot(1, "/exit", {
      expectedPaneId: "%40", recheck: async () => null, skipSlashClear: true,
      exitEpisode: db.getExitStatus().episode!, exitOwnerPin: relay.captureExitOwner(1, "original")!,
      timing: { dwellMs: 0, pollMs: 5, payloadStableMs: 1, payloadGraceMs: 40, clearGraceMs: 20 },
    });
  };
  return { db, relay, state, dispatch, advance: (ms: number) => { now += ms; } };
}

for (const c of [
  { name: "background agent marker", elapsed: 60_000, marker: "← 2 agents\n", fail: false, enters: 0 },
  { name: "idle under sixty seconds", elapsed: 59_999, marker: "", fail: false, enters: 0 },
  { name: "idle sixty seconds and clear", elapsed: 60_000, marker: "", fail: false, enters: 1 },
  { name: "capture error", elapsed: 60_000, marker: "", fail: true, enters: 0 },
]) {
  test(`real guarded exit: ${c.name}`, async () => {
    const h = harness();
    try {
      h.advance(c.elapsed); h.state.marker = c.marker; h.state.failCapture = c.fail;
      await h.dispatch();
      await h.dispatch();
      assert.equal(h.state.enters, c.enters);
      assert.equal(h.db.getExitPending(), true);
    } finally { h.db.close(); }
  });
}

test("a busy turn resets quiet time at the real guarded send", async () => {
  const h = harness();
  try {
    h.advance(60_000);
    h.db.startAgentTurn(1, "original");
    h.db.finishAgentTurn(1, "original");
    await h.dispatch();
    assert.equal(h.state.enters, 0);
    h.advance(60_000);
    await h.dispatch();
    assert.equal(h.state.enters, 1);
  } finally { h.db.close(); }
});

// Execute the actual server callback, not a hand-written ordering simulation.
function actualStopRoute(db: MoPDatabase, relay: TmuxRelay) {
  const source = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const tree = ts.createSourceFile("server.ts", source, ts.ScriptTarget.Latest, true);
  let callback: ts.Node | undefined;
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === "app.post"
        && node.arguments[0]?.getText(tree) === JSON.stringify("/hooks/slot/:slotNum")) callback = node.arguments[1];
    ts.forEachChild(node, visit);
  };
  visit(tree); assert.ok(callback);
  const js = ts.transpileModule(`const handler = ${callback.getText(tree)};`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const handler = new Function("db", "processor", "slotParamSchema", "hookPayloadSchema", "normalizePayload", "isGatedDevSlot",
    `let pmClearDrainGeneration = 0; ${js}; return handler;`)(db, new HookProcessor(db, relay),
      { safeParse: () => ({ success: true, data: 1 }) },
      { safeParse: (data: unknown) => ({ success: true, data }) }, (body: unknown) => body, isGatedDevSlot);
  const app = new Hono(); app.post("/hooks/slot/:slotNum", handler);
  return app;
}

type Harness = ReturnType<typeof harness>;
const routeCases: Array<{ name: string; change?: (h: Harness) => void; enters: number }> = [
  { name: "final Stop alone delivers after quiet", enters: 1 },
  { name: "meaningful work invalidates the original Stop", enters: 0, change: h => h.db.updateSlot(1, { last_meaningful_work_at: "2099-01-01T00:00:00.000" }) },
  { name: "assignment epoch changes", enters: 0, change: h => (h.db as any).updateAssignmentState(1, { assignment_epoch: 99 }) },
  { name: "new input", enters: 0, change: h => { h.db.logEvent(1, "UserPromptSubmit", "UserPromptSubmit", null, { session_id: "original" }); } },
  { name: "new session", enters: 0, change: h => { h.db.logEvent(1, "SessionStart", "SessionStart", null, { session_id: "successor" }); } },
  { name: "episode cancelled", enters: 0, change: h => h.db.setExitPending(false) },
  { name: "process replaced", enters: 0, change: h => { h.state.process = "101 101 S+ Mon Nov 2 00:00:01 2026 claude"; } },
  { name: "pane replaced", enters: 0, change: h => { h.state.paneId = "%41"; } },
  { name: "background marker veto", enters: 0, change: h => { h.state.marker = "← 2 agents\n"; } },
  { name: "capture unknown", enters: 0, change: h => { h.state.failCapture = true; } },
  { name: "existing UNKNOWN attempt never retries", enters: 0, change: h => h.db.saveExitAttempt(1, {
    episode: h.db.getExitStatus().episode!, paneId: "%40", originalClaudePid: 100,
    originalStart: "Mon Nov 2 00:00:00 2026", sessionId: "original", inputEventId: null,
    assignmentEpoch: 0, stopEventId: null, phase: "unknown", attemptCount: 1,
    originalProcess: "unknown", ownerPin: null, submittedAt: new Date().toISOString(), blockedReason: null,
  }) },
];

for (const c of routeCases) test(`real final-Stop quiet continuation: ${c.name}`, { timeout: 5_000 }, async () => {
  let release!: () => void;
  let entered!: () => void;
  const waiting = new Promise<void>(resolve => { entered = resolve; });
  const delay = new Promise<void>(resolve => { release = resolve; });
  let waits = 0;
  const h = harness({ active: true, waitForExitQuiet: async ms => {
    assert.equal(ms, 60_000); waits++; entered(); await delay;
  } });
  try {
    const response = await actualStopRoute(h.db, h.relay).request("/hooks/slot/1", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "Stop", session_id: "original", mop_stop_allowed: true }),
    });
    assert.equal(response.status, 200);
    assert.equal(h.db.getSlot(1)?.active_turn_state, "inactive", "the server closes the turn AFTER processing Stop");
    await waiting;
    const pending = (h.relay as any).pendingExitDispatch.get(1);
    assert.ok(pending, "same consumed Stop retains pending custody during quiet wait");
    assert.equal(h.state.enters, 0);
    c.change?.(h);
    h.advance(60_000); release();
    await pending.work;
    assert.equal(waits, 1, "one bounded wait, not a recurring tick or new Stop");
    assert.equal(h.state.enters, c.enters);
    assert.equal(h.state.keys.some(key => / 1$/.test(key)), false, "never select dev Exit and stop tasks");
    if (c.enters) assert.equal(h.db.getExitAttempt(1)?.attemptCount, 1);
  } finally { release(); h.db.close(); }
});
