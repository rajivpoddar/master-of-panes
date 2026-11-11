import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { TmuxRelay } from "../src/relay.js";

function harness() {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  let now = Date.now();
  const ready = db.exitQuietReady.bind(db);
  db.exitQuietReady = (slot: number) => ready(slot, now);
  db.setExitPending(true, true);
  db.logEvent(1, "SessionStart", "SessionStart", null, { session_id: "original" });
  db.startAgentTurn(1, "original");
  db.finishAgentTurn(1, "original");
  const rule = "─".repeat(40);
  const state = { composer: "", buffer: "", marker: "", failCapture: false, enters: 0 };
  const path = "/Users/rajiv/Downloads/projects/heydonna-app-3001";
  const shell = async (command: string) => {
    let stdout = "";
    if (command.startsWith("tmux display-message")) stdout = command.includes("pane_pid") ? `%40|100|/dev/pts/1|${path}|claude\n` : `%40|${path}\n`;
    else if (command.startsWith("git -C")) stdout = path + "\n";
    else if (command.startsWith("ps -t")) stdout = "100 100 S+ Mon Nov 2 00:00:00 2026 claude";
    else if (command.startsWith("tmux capture-pane")) {
      if (state.failCapture) throw new Error("capture failed");
      stdout = `${state.marker}${rule}\n❯ ${state.composer}\n${rule}`;
    } else if (command.startsWith("tmux load-buffer")) state.buffer = await readFile(command.match(/'([^']+)'\s*$/)![1], "utf8");
    else if (command.startsWith("tmux paste-buffer")) state.composer = state.buffer;
    else if (command.endsWith(" Enter")) { state.enters++; state.composer = ""; }
    else if (command.endsWith(" C-u")) state.composer = "";
    else if (!command.startsWith("tmux send-keys")) throw new Error(`unexpected shell: ${command}`);
    return { stdout, stderr: "" };
  };
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: shell });
  relay.setDatabase(db);
  relay.injectToPM = () => true;
  const dispatch = () => {
    db.logEvent(1, "Stop", "Stop", null, { session_id: "original" });
    return relay.deliverGuardedToSlot(1, "/exit", {
      expectedPaneId: "%40", recheck: async () => null, skipSlashClear: true,
      exitEpisode: db.getExitStatus().episode!, exitOwnerPin: relay.captureExitOwner(1, "original")!,
      timing: { dwellMs: 0, pollMs: 5, payloadStableMs: 1, payloadGraceMs: 40, clearGraceMs: 20 },
    });
  };
  return { db, state, dispatch, advance: (ms: number) => { now += ms; } };
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
