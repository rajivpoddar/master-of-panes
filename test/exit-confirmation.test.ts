import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import test from "node:test";
import { Hono } from "hono";
import ts from "typescript";
import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { ProcessHealthChecker } from "../src/health.js";
import { TmuxRelay } from "../src/relay.js";
import * as relayExports from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const rule = "─".repeat(40);
const dialog = "Background work is running\n❯ 1. Exit and stop tasks\n  2. Move to background and exit\n  3. Stay\nEnter to confirm · Esc to cancel";

function actualHookRoute(db: MoPDatabase, processor: HookProcessor, slot: number) {
  const source = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const tree = ts.createSourceFile("server.ts", source, ts.ScriptTarget.Latest, true);
  let callback: ts.Node | undefined;
  const find = (node: ts.Node): void => {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === "app.post"
        && node.arguments[0]?.getText(tree) === JSON.stringify("/hooks/slot/:slotNum")) callback = node.arguments[1];
    ts.forEachChild(node, find);
  };
  find(tree); assert.ok(callback);
  const js = ts.transpileModule(`const handler = ${callback.getText(tree)};`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const handler = new Function("db", "processor", "slotParamSchema", "hookPayloadSchema", "normalizePayload",
    `let pmClearDrainGeneration = 0; ${js}; return handler;`)(db, processor,
      { safeParse: () => ({ success: true, data: slot }) },
      { safeParse: (body: unknown) => ({ success: true, data: body }) }, (body: unknown) => body);
  const app = new Hono(); app.post("/hooks/slot/:slotNum", handler);
  return app;
}

function harness(slot: number, options: { persistent?: boolean; noDialog?: boolean; delay?: number; afterStop?: boolean } = {}) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  db.setExitPending(true, slot !== 0, slot === 0);
  db.logEvent(slot, "SessionStart", "SessionStart", null, { session_id: "original" });
  if (slot) db.startAgentTurn(slot, "original");
  const path = slot ? `/Users/rajiv/Downloads/projects/heydonna-app-${3000 + slot}` : "/Users/rajiv/Downloads/projects/heydonna-app";
  const pane = { id: "%40", process: "100 100 S+ Mon Nov 2 00:00:00 2026 claude", composer: "", buffer: "", exitAt: 0, stopRespondedAt: 0,
    showing: !options.noDialog, exitEnters: 0, keys: [] as string[], onVisible: undefined as undefined | (() => void),
    onOne: undefined as undefined | (() => void), onConfirmation: undefined as undefined | (() => void),
    onIdentity: undefined as undefined | (() => void), failOne: false };
  const shell = async (command: string) => {
    let stdout = "";
    if (command.startsWith("tmux display-message")) {
      if (!command.includes("pane_pid")) pane.onIdentity?.();
      stdout = command.includes("pane_pid") ? `${pane.id}|100|/dev/pts/1|${path}|claude\n` : `${pane.id}|${path}\n`;
    }
    else if (command.startsWith("git -C")) stdout = path + "\n";
    else if (command.startsWith("ps -t")) stdout = pane.process;
    else if (command.startsWith("tmux capture-pane")) {
      if (command.includes(" -S -")) stdout = `${rule}\n❯ ${pane.composer}\n${rule}`;
      else {
        pane.onVisible?.();
        const renderAt = options.afterStop ? pane.stopRespondedAt : pane.exitAt;
        stdout = renderAt && pane.showing && Date.now() - renderAt >= (options.delay ?? 0) ? dialog : "shell $";
      }
    } else if (command.startsWith("tmux load-buffer")) {
      pane.buffer = await readFile(command.match(/'([^']+)'\s*$/)![1], "utf8");
    } else if (command.startsWith("tmux paste-buffer")) pane.composer = pane.buffer;
    else if (command.endsWith(" Enter")) {
      if (pane.composer === "/exit") { pane.exitEnters++; pane.exitAt = Date.now(); pane.composer = ""; }
      else { pane.keys.push("Enter"); if (!options.persistent) pane.showing = false; pane.onConfirmation?.(); }
    } else if (command.endsWith(" 1")) {
      pane.keys.push("1"); pane.onOne?.();
      if (pane.failOne) throw new Error("key acknowledgement lost");
    } else if (!command.startsWith("tmux send-keys")) throw new Error(`Unexpected shell command: ${command}`);
    return { stdout, stderr: "" };
  };
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: shell });
  relay.setDatabase(db);
  relay.injectToPM = () => true;
  (relay as any).slashSubmitTiming = () => ({ dwellMs: 0, pollMs: 5, payloadStableMs: 1, payloadGraceMs: 40, clearGraceMs: 20 });
  const processor = new HookProcessor(db, relay) as any;
  const episode = db.getExitStatus().episode!;
  let finish!: () => void;
  const finished = new Promise<void>(resolve => { finish = resolve; });
  const setConfig = db.setConfig.bind(db);
  db.setConfig = (key, value) => {
    setConfig(key, value);
    if (key === `exit_dialog_confirmation_${slot}` && JSON.parse(value).done) finish();
  };
  const dispatch = () => relay.deliverGuardedToSlot(slot, "/exit", {
    expectedPaneId: "%40", recheck: async () => null, skipSlashClear: true, exitEpisode: episode,
    timing: { dwellMs: 0, pollMs: 5, payloadStableMs: 1, payloadGraceMs: 40, clearGraceMs: 20 },
  });
  const send = async () => { const result = await dispatch(); await finished; return result; };
  const app = actualHookRoute(db, processor, slot);
  const serverStop = async () => {
    const response = await app.request(`/hooks/slot/${slot}`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "Stop", session_id: "original" }) });
    pane.stopRespondedAt = Date.now();
    return response;
  };
  const awaitExit = async () => {
    await (relay as any).pendingExitDispatch.get(slot)?.work;
    assert.equal(pane.exitEnters, 1, "the actual server-ordered Stop must submit one /exit");
    await finished;
  };
  return { db, pane, relay, processor, episode, send, dispatch, serverStop, awaitExit, finished };
}

test("recognizes only the visible exit dialog; quoted/history text and a successor prompt are not a dialog", () => {
  const { isExitConfirmationDialog } = relayExports;
  assert.equal(isExitConfirmationDialog(dialog), true);
  assert.equal(isExitConfirmationDialog('Report says "Background work is running" and "Exit and stop tasks"'), false);
  assert.equal(isExitConfirmationDialog(dialog + "\n❯ continue new work"), false);
  assert.equal(isExitConfirmationDialog(dialog.replace("1. Exit and stop tasks", "1. Some other action")), false);
});

for (const slot of [0, 1]) test(`${slot ? "dev" : "PM"} actual Stop sends /exit, then 1 and Enter exactly once`, async () => {
  const h = harness(slot, { afterStop: true }); const before = h.db.getSlot(1)?.assignment_epoch;
  try {
    assert.equal((await h.serverStop()).status, 200);
    assert.deepEqual(h.pane.keys, [], "Stop response precedes dialog confirmation");
    await h.awaitExit();
    await h.serverStop();
    assert.equal(h.pane.exitEnters, 1);
    assert.deepEqual(h.pane.keys, ["1", "Enter"]);
    assert.equal(h.db.getExitStatus().cycled[slot], true);
    assert.equal(h.db.getSlot(1)?.assignment_epoch, before);
    if (slot) assert.equal(h.db.getSlot(slot)?.active_turn_state, "inactive");
  } finally { h.db.close(); }
});

test("idle dev health entry uses the same confirmation path", async () => {
  const h = harness(1);
  try {
    const health = new ProcessHealthChecker(h.db, h.relay) as any;
    health.getPaneCommand = async (slot: number) => slot === 1 ? "claude" : null;
    h.relay.getSlotActivityState = async () => "idle";
    await health.checkExitPending();
    await h.finished;
    await health.checkExitPending();
    assert.equal(h.pane.exitEnters, 1); assert.deepEqual(h.pane.keys, ["1", "Enter"]);
  } finally { h.db.close(); }
});

test("no dialog adds no input after the original exit", async () => {
  const h = harness(1, { noDialog: true });
  try { await h.send(); assert.equal(h.pane.exitEnters, 1); assert.deepEqual(h.pane.keys, []); }
  finally { h.db.close(); }
});

test("a PM dialog appearing after the Stop response and later than the first capture is recognized", async () => {
  const h = harness(0, { delay: 900, afterStop: true });
  try {
    await h.serverStop();
    assert.deepEqual(h.pane.keys, []);
    await h.awaitExit();
    assert.deepEqual(h.pane.keys, ["1", "Enter"]);
  }
  finally { h.db.close(); }
});

test("persistent dialog consumes two attempts total, including a fresh relay instance", async () => {
  const h = harness(1, { persistent: true });
  try {
    await h.send(); assert.deepEqual(h.pane.keys, ["1", "Enter", "1", "Enter"]);
    const record = JSON.parse(h.db.getConfig("exit_dialog_confirmation_1")!);
    assert.equal(record.attempts, 2); assert.equal(record.done, true);
    const replacement = new TmuxRelay(DEFAULT_CONFIG); replacement.setDatabase(h.db);
    await (replacement as any).confirmExitDialog(1, "%40", h.episode, record.process, "unused");
    assert.deepEqual(h.pane.keys, ["1", "Enter", "1", "Enter"]);
  } finally { h.db.close(); }
});

test("dialog disappearance after 1 does not send Enter to resumed work", async () => {
  const h = harness(1); h.pane.onOne = () => { h.pane.showing = false; };
  try { await h.send(); assert.deepEqual(h.pane.keys, ["1"]); }
  finally { h.db.close(); }
});

for (const change of ["pane", "process", "episode", "session", "input"]) test(`${change} change before confirmation prevents all extra input`, async () => {
  const h = harness(0);
  h.pane.onVisible = () => {
    if (change === "pane") h.pane.id = "%41";
    if (change === "process") h.pane.process = "200 200 S+ Tue Nov 3 00:00:00 2026 claude";
    if (change === "episode") h.db.setExitPending(true, false, true);
    if (change === "session") h.db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "successor" });
    if (change === "input") h.db.logEvent(0, "UserPromptSubmit", "UserPromptSubmit", null, {});
  };
  try { await h.send(); assert.deepEqual(h.pane.keys, []); }
  finally { h.db.close(); }
});

test("process change after one confirmed attempt prevents the second attempt", async () => {
  const h = harness(1, { persistent: true }); h.pane.onConfirmation = () => { h.pane.process = "200 successor claude"; };
  try { await h.send(); assert.deepEqual(h.pane.keys, ["1", "Enter"]); }
  finally { h.db.close(); }
});

test("new work after the Stop response keeps its busy fence before the consumed dev send", async () => {
  const h = harness(1, { afterStop: true });
  try {
    await h.serverStop();
    h.db.startAgentTurn(1, "successor");
    h.db.logEvent(1, "UserPromptSubmit", "UserPromptSubmit", null, { session_id: "successor" });
    await (h.relay as any).pendingExitDispatch.get(1)?.work;
    assert.equal(h.pane.exitEnters, 0); assert.deepEqual(h.pane.keys, []);
    assert.equal(h.db.getSlot(1)?.active_turn_id, "successor");
    assert.equal(h.db.getSlot(1)?.active_turn_state, "active");
  } finally { h.db.close(); }
});

test("a Stop hook child leaving the TTY does not change the pinned Claude leader", async () => {
  const h = harness(0);
  h.pane.process += "\n101 100 S+ Mon Nov 2 00:00:01 2026 hook-child";
  h.pane.onVisible = () => { h.pane.process = h.pane.process.split("\n")[0]; };
  try { await h.send(); assert.deepEqual(h.pane.keys, ["1", "Enter"]); }
  finally { h.db.close(); }
});

for (const change of ["epoch-before-timer", "boot-before-timer", "boot-during-pane-lookup", "input-before-timer"]) {
  test(`the consumed Stop never adopts an inactive successor: ${change}`, async () => {
    const h = harness(1, { afterStop: true });
    try {
      await h.serverStop();
      const replaceBoot = () => {
        h.db.logEvent(1, "SessionStart", "SessionStart", null, { session_id: "replacement" });
        h.db.updateSlot(1, { active_turn_id: null, active_turn_started_at: null, active_turn_state: "inactive" });
      };
      if (change === "epoch-before-timer") {
        (h.db as any).db.prepare("UPDATE slots SET assignment_epoch = assignment_epoch + 1 WHERE slot = ?").run(1);
      } else if (change === "boot-before-timer") replaceBoot();
      else if (change === "boot-during-pane-lookup") h.pane.onIdentity = () => { h.pane.onIdentity = undefined; replaceBoot(); };
      else {
        h.db.startAgentTurn(1, "original");
        h.db.logEvent(1, "UserPromptSubmit", "UserPromptSubmit", null, { session_id: "original" });
        h.db.finishAgentTurn(1, "original");
      }
      await (h.relay as any).pendingExitDispatch.get(1)?.work;
      assert.equal(h.pane.exitEnters, 0); assert.deepEqual(h.pane.keys, []);
      assert.equal(h.db.getSlot(1)?.active_turn_state, "inactive", "successor need not be busy to be protected");
      assert.equal(h.db.getExitStatus().cycled[1], true, "the old operation stays consumed, never resent");
    } finally { h.db.close(); }
  });
}

test("lost key acknowledgment is consumed and never retries", async () => {
  const h = harness(1, { persistent: true }); h.pane.failOne = true;
  try {
    await h.send(); assert.deepEqual(h.pane.keys, ["1"]);
    const record = JSON.parse(h.db.getConfig("exit_dialog_confirmation_1")!);
    assert.equal(record.attempts, 1); assert.equal(record.done, true);
    assert.equal(JSON.parse(h.db.getEvents(1, 1, "exit_dialog_confirmation")[0].payload).outcome, "unknown_no_retry");
  } finally { h.db.close(); }
});
