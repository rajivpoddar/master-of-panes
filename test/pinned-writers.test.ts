/**
 * CTO REVISE on d15f074 (C0ALZJHGE49 thread 1791348298.888079, ts
 * 1791352881.985519): the operation pin must be enforced INSIDE the actual
 * cleanup / clear / task-delivery writers, in the send lock, immediately
 * before each C-u / paste / Enter, after any waits. These run the REAL
 * TmuxRelay.deliverGuardedToSlot writer against a fake tmux pane, counting
 * effects at the tmux command boundary.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { pinnedComposerCleanup, pinnedGuardedSend } from "../src/pinnedWriters.js";
import {
  interruptLiveTurnBeforeClear,
  pinDrift,
  pinFrom,
  type OperationPin,
  type SlotRowLike,
} from "../src/interruptBeforeClear.js";

const CHECKOUT = "/Users/rajiv/Downloads/projects/heydonna-app-3001";
const RULE = "──────────────────────────";
const GRAY = "\x1b[38;2;153;153;153m";
const CLEAR_TEXT = "/" + "clear";

interface Pane {
  paneId: string;
  active: boolean;
  composer: string;
  buffer: string;
  effects: string[]; // every key/paste that reached a pane: "<pane>:<what>"
  captures: number;
  onCapture?: (n: number, p: Pane) => void;
  onPaste?: (p: Pane) => void;
}

function render(p: Pane): string {
  const prompt = p.active ? `${GRAY}❯\x1b[0m ${p.composer}` : `❯ ${p.composer}`;
  return ["⏺ working", "", RULE, prompt, RULE, "  ⏵⏵ bypass permissions on"].join("\n");
}

function fakeShell(p: Pane) {
  return async (command: string) => {
    if (command.startsWith("tmux display-message")) return { stdout: `${p.paneId}|${CHECKOUT}\n`, stderr: "" };
    if (command.startsWith("git -C")) return { stdout: `${CHECKOUT}\n`, stderr: "" };
    if (command.startsWith("tmux capture-pane")) {
      p.captures += 1;
      const out = render(p);
      await new Promise((r) => setImmediate(r));
      p.onCapture?.(p.captures, p);
      return { stdout: out, stderr: "" };
    }
    if (command.startsWith("tmux load-buffer")) {
      const file = command.match(/'([^']+)'\s*$/)?.[1] ?? "";
      p.buffer = await readFile(file, "utf8");
      return { stdout: "", stderr: "" };
    }
    if (command.startsWith("tmux paste-buffer")) {
      const target = command.match(/-t (\S+)/)?.[1];
      p.effects.push(`${target}:paste:${p.buffer}`);
      p.composer = p.buffer;
      p.onPaste?.(p);
      return { stdout: "", stderr: "" };
    }
    const key = command.match(/^tmux send-keys -t (\S+) (\S+)$/);
    if (key) {
      p.effects.push(`${key[1]}:${key[2]}`);
      if (key[2] === "Enter") p.composer = "";
      return { stdout: "", stderr: "" };
    }
    throw new Error(`unexpected command: ${command}`);
  };
}

function rowA(over: Partial<SlotRowLike> = {}): SlotRowLike {
  return {
    occupied: true, repository_id: "r", issue: 8970, pr: null, handoff_id: "h", branch: "b",
    assignment_epoch: 2, session_id: "s1", active_turn_id: null, active_turn_state: "inactive", ...over,
  };
}

function setup(paneOver: Partial<Pane> = {}) {
  const p: Pane = { paneId: "%50", active: false, composer: "", buffer: "", effects: [], captures: 0, ...paneOver };
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: fakeShell(p) as never });
  const world = { row: rowA() };
  const pin: OperationPin = pinFrom({ paneId: "%50", checkout: CHECKOUT }, world.row);
  const fence = (stage: string) => {
    const d = pinDrift(pin, world.row);
    return d ? `${d}@${stage}` : null;
  };
  const send = (text: string, requireIdle = true) => pinnedGuardedSend({
    relay, slot: 1, paneId: "%50", text, fence, requireIdle, bracketedPaste: true,
    sleep: async () => undefined, waitEmptyMs: 1000, pollMs: 250,
  });
  return { p, world, send };
}

test("writer positive: pinned clear pastes and presses Enter once on the pinned pane", async () => {
  const h = setup();
  const r = await h.send(CLEAR_TEXT);
  assert.equal(r.outcome, "delivered");
  assert.deepEqual(h.p.effects, [`%50:paste:${CLEAR_TEXT}`, "%50:Enter"]);
});

test("writer: pane rebound %50 -> %51 before the lock: zero effects anywhere", async () => {
  const h = setup({ paneId: "%51" });
  const r = await h.send(CLEAR_TEXT);
  assert.equal(r.outcome, "refused_pre_effect");
  assert.deepEqual(h.p.effects, []);
});

test("writer: turn B starts DURING the composer wait (inside the lock): no paste, no Enter", async () => {
  const h = setup({ composer: "restored prompt" });
  h.p.onCapture = (n, p) => {
    if (n === 2) { h.world.row = { ...h.world.row, active_turn_id: "turnB", active_turn_state: "active" }; p.composer = ""; }
  };
  const r = await h.send(CLEAR_TEXT);
  assert.equal(r.outcome, "refused_pre_effect");
  assert.match((r as { reason: string }).reason, /drift:turn/);
  assert.deepEqual(h.p.effects, []);
});

test("writer: epoch and session drift during the wait refuse before any paste", async () => {
  for (const change of [{ assignment_epoch: 3 }, { session_id: "s2" }]) {
    const h = setup({ composer: "x" });
    h.p.onCapture = (n, p) => { if (n === 2) { h.world.row = { ...h.world.row, ...change }; p.composer = ""; } };
    const r = await h.send("packet");
    assert.equal(r.outcome, "refused_pre_effect");
    assert.deepEqual(h.p.effects, []);
  }
});

test("writer: turn B starts between paste and Enter: Enter is never pressed", async () => {
  const h = setup();
  h.p.onPaste = () => { h.world.row = { ...h.world.row, active_turn_id: "turnB", active_turn_state: "active" }; };
  const r = await h.send("TASK PACKET");
  assert.notEqual(r.outcome, "delivered");
  assert.deepEqual(h.p.effects, ["%50:paste:TASK PACKET"], "no Enter reached turn B");
});

test("terminal-only late start: pane turns active before the clear -> refused, no Escape, no paste", async () => {
  const h = setup();
  h.p.active = true; // a turn started after the terminal-only idle check
  const r = await h.send(CLEAR_TEXT, true);
  assert.equal(r.outcome, "refused_pre_effect");
  assert.deepEqual(h.p.effects, [], "no Escape, no C-c, no paste, no Enter");
});

test("cleanup writer: C-u is fenced after the capture wait; drift during the capture sends no C-u", async () => {
  const effects: string[] = [];
  const world = { drift: false };
  const r = await pinnedComposerCleanup({
    slot: 1,
    capture: async () => { world.drift = true; return `${RULE}\n❯ restored turn A prompt\n${RULE}`; },
    modeRefused: async () => false,
    sendCtrlU: async () => { effects.push("C-u"); },
    fence: (stage) => (world.drift ? `drift:turn@${stage}` : null),
    sleep: async () => undefined,
  });
  assert.equal(r, "refused:drift:turn@pre_ctrl_u");
  assert.deepEqual(effects, []);
});

test("cleanup writer positive: residue is cleared with C-u when the fence holds", async () => {
  let composer = "restored";
  const effects: string[] = [];
  const r = await pinnedComposerCleanup({
    slot: 1,
    capture: async () => `${RULE}\n❯ ${composer}\n${RULE}`,
    modeRefused: async () => false,
    sendCtrlU: async () => { effects.push("C-u"); composer = ""; },
    fence: () => null,
    sleep: async () => undefined,
  });
  assert.equal(r, "cleared");
  assert.deepEqual(effects, ["C-u"]);
});

test("helper passes the bound-turn fence into cleanup: turn B at cleanup -> no C-u, no CAS", async () => {
  let row: SlotRowLike = rowA({ active_turn_id: "turnA", active_turn_state: "active" });
  const keys: string[] = [];
  let cas = 0;
  let n = 0;
  const r = await interruptLiveTurnBeforeClear({
    observePane: async () => ({ paneId: "%50", checkout: CHECKOUT }),
    readRow: () => ({ ...row }),
    paneActivity: async () => (++n >= 3 ? "idle" : "active"),
    recordPossibleEffect: () => undefined,
    sendKeyOnce: async (_p, k) => { keys.push(k); return "sent"; },
    casTerminalize: () => { cas += 1; return true; },
    afterInterrupt: async (_paneId, fence) => {
      row = { ...row, active_turn_id: "turnB" }; // B starts while cleanup waits
      const refusal = fence("pre_ctrl_u");
      if (refusal) return `refused:${refusal}`;
      keys.push("C-u");
      return "cleared";
    },
    log: () => undefined,
    sleep: async () => undefined,
    now: () => 0,
  });
  assert.equal(r.reason, "operation_drift");
  assert.deepEqual(keys, ["Escape"], "no C-u reached turn B");
  assert.equal(cas, 0);
});
