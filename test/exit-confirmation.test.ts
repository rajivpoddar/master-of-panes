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

function harness(slot: number, options: { persistent?: boolean; noDialog?: boolean; delay?: number; afterStop?: boolean; partial?: boolean } = {}) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  db.setExitPending(true, slot !== 0, slot === 0);
  db.logEvent(slot, "SessionStart", "SessionStart", null, { session_id: "original" });
  if (slot) db.startAgentTurn(slot, "original");
  const path = slot ? `/Users/rajiv/Downloads/projects/heydonna-app-${3000 + slot}` : "/Users/rajiv/Downloads/projects/heydonna-app";
  const pane = { id: "%40", process: "100 100 S+ Mon Nov 2 00:00:00 2026 claude", composer: "", buffer: "", exitAt: 0, stopRespondedAt: 0,
    showing: !options.noDialog, exitEnters: 0, keys: [] as string[], onVisible: undefined as undefined | (() => void),
    onOne: undefined as undefined | (() => void), onConfirmation: undefined as undefined | (() => void),
    onIdentity: undefined as undefined | (() => void), onPaste: undefined as undefined | (() => void), failOne: false };
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
        // Before /exit is submitted the pane is an idle Claude composer; the
        // dialog can only render after the submitted /exit (the dev Stop path
        // rechecks pane idleness before its single send).
        const renderAt = options.afterStop ? pane.stopRespondedAt : pane.exitAt;
        stdout = !pane.exitAt ? `${rule}\n❯ \n${rule}`
          : renderAt && pane.showing && Date.now() - renderAt >= (options.delay ?? 0) ? (options.partial ? "Background work is running\n❯ 1. Exit and stop tasks\nEnter to confirm" : dialog) : "shell $";
      }
    } else if (command.startsWith("tmux load-buffer")) {
      pane.buffer = await readFile(command.match(/'([^']+)'\s*$/)![1], "utf8");
    } else if (command.startsWith("tmux paste-buffer")) { pane.composer = pane.buffer; pane.onPaste?.(); }
    else if (command.endsWith(" C-u")) pane.composer = "";
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
  const dispatch = () => {
    db.logEvent(slot, "Stop", "Stop", null, { session_id: "original" });
    return relay.deliverGuardedToSlot(slot, "/exit", {
    exitOwnerPin: relay.captureExitOwner(slot, "original")!,
    expectedPaneId: "%40", recheck: async () => null, skipSlashClear: true, exitEpisode: episode,
    timing: { dwellMs: 0, pollMs: 5, payloadStableMs: 1, payloadGraceMs: 40, clearGraceMs: 20 },
  }); };
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
  return { db, pane, relay, processor, episode, send, dispatch, serverStop, awaitExit, finished, shell };
}


test("canonical background dialog is recognized but never authorizes stopping agents", async () => {
  assert.equal(relayExports.isExitConfirmationDialog(dialog), true);
  assert.equal(relayExports.isExitConfirmationDialog(dialog + "\n❯ continue new work"), false);
  const h = harness(1);
  try {
    await h.send();
    assert.equal(h.pane.exitEnters, 1);
    assert.deepEqual(h.pane.keys, [], "never select Exit and stop tasks");
    assert.equal(h.db.getExitAttempt(1)?.phase, "blocked");
    assert.equal(h.db.getExitAttempt(1)?.blockedReason, "exit_dialog_observed");
    assert.equal(h.db.getExitStatus().cycled[1], false);
  } finally { h.db.close(); }
});

test("health only observes: neither initially idle nor blocked slots receive timer exits", async () => {
  const h = harness(1);
  try {
    const health = new ProcessHealthChecker(h.db, h.relay) as any;
    await health.checkExitPending();
    assert.equal(h.pane.exitEnters, 0);
    await h.send();
    await health.checkExitPending();
    assert.equal(h.pane.exitEnters, 1);
    assert.equal(h.db.getExitStatus().cycled[1], false);
  } finally { h.db.close(); }
});

test("cycle requires actual replacement Claude PID/start in the SAME immutable pane", async () => {
  const h = harness(1);
  try {
    await h.send();
    await h.relay.observeExitAttempts();
    assert.equal(h.db.getExitStatus().cycled[1], false);
    h.pane.process = "100 100 Ss Mon Nov 2 00:00:00 2026 zsh";
    await h.relay.observeExitAttempts();
    assert.equal(h.db.getExitStatus().cycled[1], false, "shell is not a replacement");
    h.pane.process = "";
    await h.relay.observeExitAttempts();
    assert.equal(h.db.getExitStatus().cycled[1], false, "absence is not a replacement");
    h.pane.id = "%41";
    h.pane.process = "200 200 S+ Tue Nov 3 00:00:00 2026 claude";
    await h.relay.observeExitAttempts();
    assert.equal(h.db.getExitStatus().cycled[1], false, "numeric address successor is not our pane");
    h.pane.id = "%40";
    const attempt = h.db.getExitAttempt(1)!;
    h.db.logEvent(1, "exit_replacement_bound", null, null, {
      episode: attempt.episode, submittedAt: attempt.submittedAt, assignmentEpoch: attempt.assignmentEpoch,
      replacementProcess: JSON.stringify(["%40", 200, "Tue Nov 3 00:00:00 2026"]),
    });
    await h.relay.observeExitAttempts();
    assert.equal(h.db.getExitStatus().cycled[1], true);
    assert.equal(h.db.getExitAttempt(1)?.replacementProcess, JSON.stringify(["%40", 200, "Tue Nov 3 00:00:00 2026"]));
  } finally { h.db.close(); }
});

test("legacy sent/cycled flags and cohort retention do not claim verified cycles", () => {
  const h = harness(1);
  try {
    h.db.setConfig("exit_cycled_1", "true");
    assert.equal(h.db.getExitStatus().cycled[1], false);
    h.db.retainExitSlot(1, "session_changed");
    assert.equal(h.db.getExitStatus().retained[1], true);
    assert.equal(h.db.getExitStatus().cycled[1], false);
  } finally { h.db.close(); }
});

test("dispatching/UNKNOWN persists across relay restart and is never blindly replayed", async () => {
  const h = harness(1);
  try {
    await h.send();
    const attempt = h.db.getExitAttempt(1)!;
    h.db.saveExitAttempt(1, { ...attempt, phase: "dispatching", reported: false });
    const restarted = new TmuxRelay(DEFAULT_CONFIG, { runShell: h.shell }); restarted.setDatabase(h.db);
    const result = await restarted.deliverGuardedToSlot(1, "/exit", {
      expectedPaneId: "%40", recheck: async () => null,
      exitEpisode: h.episode, exitOwnerPin: restarted.captureExitOwner(1, "original")!,
    });
    assert.equal(result.outcome, "refused_pre_effect");
    assert.equal(h.pane.exitEnters, 1);
    assert.equal(h.db.getExitStatus().cycled[1], false);
  } finally { h.db.close(); }
});

test("PM inbound arriving after exit paste proceeds alone; exit cancels with no merged composer", async () => {
  const h = harness(0);
  let inbound: Promise<unknown> | undefined;
  const submitted: string[] = [];
  (h.relay as any).injectDirectNow = async (message: string) => {
    h.pane.composer += message;
    submitted.push(h.pane.composer);
    h.pane.composer = "";
    return { ok: true, ambiguous: false };
  };
  h.pane.onPaste = () => {
    h.pane.onPaste = undefined;
    inbound = (h.relay as any).injectDirectWithSubmitKey("# slack-dm Rajiv: new input", "Enter");
  };
  try {
    const result = await h.dispatch();
    await inbound;
    assert.notEqual(result.outcome, "delivered");
    assert.equal(h.pane.exitEnters, 0);
    assert.deepEqual(submitted, ["# slack-dm Rajiv: new input"]);
    assert.equal(h.pane.composer, "");
  } finally { h.db.close(); }
});

for (const change of ["input", "session"]) test(`new ${change} cancels the consumed Stop before paste`, async () => {
  const h = harness(1, { afterStop: true });
  try {
    await h.serverStop();
    h.db.logEvent(1, change === "input" ? "UserPromptSubmit" : "SessionStart", change === "input" ? "UserPromptSubmit" : "SessionStart", null, { session_id: change === "input" ? "original" : "successor" });
    await (h.relay as any).pendingExitDispatch.get(1)?.work;
    assert.equal(h.pane.exitEnters, 0);
    assert.equal(h.db.getExitStatus().cycled[1], false);
  } finally { h.db.close(); }
});

test("persist dispatching before paste; background block gets at most one fresh-Stop retry", async () => {
  const h = harness(1);
  h.pane.onPaste = () => assert.equal(h.db.getExitAttempt(1)?.phase, "dispatching");
  try {
    await h.send();
    assert.equal(h.db.getExitAttempt(1)?.attemptCount, 1);
    await h.dispatch();
    assert.equal(h.pane.exitEnters, 2);
    assert.equal(h.db.getExitAttempt(1)?.attemptCount, 2);
    await h.dispatch();
    assert.equal(h.pane.exitEnters, 2);
    assert.equal(h.db.getExitStatus().cycled[1], false);
  } finally { h.db.close(); }
});

test("after 60 seconds same process reports an honest unknown block, never success or a timer retry", async () => {
  const h = harness(1);
  try {
    await h.send();
    const attempt = h.db.getExitAttempt(1)!;
    h.db.saveExitAttempt(1, { ...attempt, phase: "unknown", reported: false, submittedAt: new Date(Date.now() - 61_000).toISOString() });
    h.pane.showing = false;
    await h.relay.observeExitAttempts();
    assert.equal(h.db.getExitAttempt(1)?.blockedReason, "same_original_process_no_exit_receipt");
    assert.equal(h.db.getExitStatus().cycled[1], false);
    assert.equal(h.pane.exitEnters, 1);
    assert.equal(h.db.getEvents(1, 1, "exit_blocked").length, 1);
  } finally { h.db.close(); }
});

test("all-pane episodes include PM at its genuine Stop, without an idle health send", async () => {
  const h = harness(0, { afterStop: true });
  try {
    h.db.setExitPending(true, false, false);
    await h.serverStop();
    await h.finished;
    assert.equal(h.pane.exitEnters, 1);
    assert.equal(h.db.getExitStatus().cycled[0], false);
    assert.equal(h.db.getExitAttempt(0)?.phase, "blocked");
    assert.deepEqual(h.pane.keys, []);
  } finally { h.db.close(); }
});

test("graceful PM /exit timeout never escalates to respawn-pane -k", async () => {
  const h = harness(0);
  const health = new ProcessHealthChecker(h.db, h.relay) as any;
  let kills = 0;
  health.runShell = async (command: string) => {
    if (command.includes("respawn-pane")) { kills++; throw new Error("must never kill blocked PM"); }
    return h.shell(command);
  };
  health.pmAgentProcess = async () => "100:200";
  health.waitForPMShellPrompt = async () => null;
  try {
    const result = await health.runPMFreshRestart("exit", h.episode);
    assert.equal(result.success, false);
    assert.equal(result.reason, "exit_blocked_shell_not_observed_no_force");
    assert.equal(kills, 0);
  } finally { h.db.close(); }
});


test("PM delayed menu sends one 1 then Enter only after full visible choices", async () => {
  const h = harness(0, { delay: 800 });
  let selectedAt = 0;
  h.pane.onOne = () => { selectedAt = Date.now(); };
  try {
    await h.send();
    assert.ok(selectedAt - h.pane.exitAt >= 800);
    assert.deepEqual(h.pane.keys, ["1", "Enter"]);
  } finally { h.db.close(); }
});

test("PM partial render sends no menu keys", async () => {
  const h = harness(0, { partial: true });
  try {
    assert.equal(relayExports.isCompletePMExitMenu("Background work is running\n1. Exit and stop tasks\nEnter to confirm"), false);
    await h.send();
    assert.deepEqual(h.pane.keys, []);
  } finally { h.db.close(); }
});

test("dev slot complete menu never selects 1", async () => {
  const h = harness(2);
  try { await h.send(); assert.deepEqual(h.pane.keys, []); }
  finally { h.db.close(); }
});
