import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { ProcessHealthChecker } from "../src/health.js";
import { TmuxRelay } from "../src/relay.js";
import * as relayExports from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const rule = "─".repeat(40);
const dialog = "Background work is running\n❯ 1. Exit and stop tasks\n  2. Move to background and exit\n  3. Stay\nEnter to confirm · Esc to cancel";

function harness(slot: number, options: { persistent?: boolean; noDialog?: boolean; delay?: number } = {}) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  db.setExitPending(true, slot !== 0, slot === 0);
  db.logEvent(slot, "SessionStart", "SessionStart", null, { session_id: "original" });
  if (slot) db.updateSlot(slot, { active_turn_state: "inactive", idle: true });
  const path = slot ? `/Users/rajiv/Downloads/projects/heydonna-app-${3000 + slot}` : "/Users/rajiv/Downloads/projects/heydonna-app";
  const pane = { id: "%40", process: "100 Mon Nov 2 00:00:00 2026 claude", composer: "", buffer: "", exitAt: 0,
    showing: !options.noDialog, exitEnters: 0, keys: [] as string[], onVisible: undefined as undefined | (() => void),
    onOne: undefined as undefined | (() => void), onConfirmation: undefined as undefined | (() => void), failOne: false };
  const shell = async (command: string) => {
    let stdout = "";
    if (command.startsWith("tmux display-message")) stdout = command.includes("pane_pid") ? `${pane.id}|100|/dev/pts/1|${path}\n` : `${pane.id}|${path}\n`;
    else if (command.startsWith("git -C")) stdout = path + "\n";
    else if (command.startsWith("ps -t")) stdout = pane.process;
    else if (command.startsWith("tmux capture-pane")) {
      if (command.includes(" -S -")) stdout = `${rule}\n❯ ${pane.composer}\n${rule}`;
      else {
        pane.onVisible?.();
        stdout = pane.exitAt && pane.showing && Date.now() - pane.exitAt >= (options.delay ?? 0) ? dialog : "shell $";
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
  const send = () => relay.deliverGuardedToSlot(slot, "/exit", {
    expectedPaneId: "%40", recheck: async () => null, skipSlashClear: true, exitEpisode: episode,
    timing: { dwellMs: 0, pollMs: 5, payloadStableMs: 1, payloadGraceMs: 40, clearGraceMs: 20 },
  });
  return { db, pane, relay, processor, episode, send };
}

test("recognizes only the visible exit dialog; quoted/history text and a successor prompt are not a dialog", () => {
  const { isExitConfirmationDialog } = relayExports;
  assert.equal(isExitConfirmationDialog(dialog), true);
  assert.equal(isExitConfirmationDialog('Report says "Background work is running" and "Exit and stop tasks"'), false);
  assert.equal(isExitConfirmationDialog(dialog + "\n❯ continue new work"), false);
  assert.equal(isExitConfirmationDialog(dialog.replace("1. Exit and stop tasks", "1. Some other action")), false);
});

for (const slot of [0, 1]) test(`${slot ? "dev" : "PM"} actual Stop sends /exit, then 1 and Enter exactly once`, async () => {
  const h = harness(slot); const before = h.db.getSlot(1)?.assignment_epoch;
  try {
    await h.processor.process(slot, { type: "Stop", session_id: "original" });
    await h.processor.process(slot, { type: "Stop", session_id: "original" });
    assert.equal(h.pane.exitEnters, 1);
    assert.deepEqual(h.pane.keys, ["1", "Enter"]);
    assert.equal(h.db.getExitStatus().cycled[slot], true);
    assert.equal(h.db.getSlot(1)?.assignment_epoch, before);
  } finally { h.db.close(); }
});

test("idle dev health entry uses the same confirmation path", async () => {
  const h = harness(1);
  try {
    const health = new ProcessHealthChecker(h.db, h.relay) as any;
    health.getPaneCommand = async (slot: number) => slot === 1 ? "claude" : null;
    h.relay.getSlotActivityState = async () => "idle";
    await health.checkExitPending();
    await health.checkExitPending();
    assert.equal(h.pane.exitEnters, 1); assert.deepEqual(h.pane.keys, ["1", "Enter"]);
  } finally { h.db.close(); }
});

test("no dialog adds no input after the original exit", async () => {
  const h = harness(1, { noDialog: true });
  try { await h.send(); assert.equal(h.pane.exitEnters, 1); assert.deepEqual(h.pane.keys, []); }
  finally { h.db.close(); }
});

test("the short bounded render interval recognizes a delayed dialog", async () => {
  const h = harness(0, { delay: 150 });
  try { await h.send(); assert.deepEqual(h.pane.keys, ["1", "Enter"]); }
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
    if (change === "process") h.pane.process = "200 Tue Nov 3 00:00:00 2026 claude";
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

test("lost key acknowledgment is consumed and never retries", async () => {
  const h = harness(1, { persistent: true }); h.pane.failOne = true;
  try {
    await h.send(); assert.deepEqual(h.pane.keys, ["1"]);
    const record = JSON.parse(h.db.getConfig("exit_dialog_confirmation_1")!);
    assert.equal(record.attempts, 1); assert.equal(record.done, true);
    assert.equal(JSON.parse(h.db.getEvents(1, 1, "exit_dialog_confirmation")[0].payload).outcome, "unknown_no_retry");
  } finally { h.db.close(); }
});
