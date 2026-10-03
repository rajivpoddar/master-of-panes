// CTO REVISE on b054f319 (C0ALZJHGE49/1791049473.052849): boundary
// discriminators that run through the REAL relay delivery helper
// (TmuxRelay.deliverGuardedToSlot -> submitWithComposerCheck) against a fake
// tmux pane model. Effects are counted at the tmux command boundary.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import type { MoPDatabase } from "../src/db.js";
import type { LogManager } from "../src/logs.js";
import { TmuxRelay } from "../src/relay.js";
import { ApiStallTracker, StuckDetector } from "../src/stuck.js";
import { DEFAULT_CONFIG, type EventLogEntry, type SlotState } from "../src/types.js";

process.env.MOP_PM_WAIT_NUDGE_INTERVAL_MS = "300000";

const CHECKOUT = "/Users/rajiv/Downloads/projects/heydonna-app-3001";
const RULE = "──────────────────────────";
const STALL_A = [
  "⏺ Bash(npm test)",
  "  ⎿  ok",
  "⏺ API Error: stream error: stream disconnected before completion",
  "✻ Cogitated for 3m · done",
];
const STALL_B_EXTRA = [
  "❯ Your last turn ended on a transient model API error. Resume.",
  "⏺ Resuming.",
  "⏺ API Error: Our servers are currently overloaded. Please try again later.",
  "✻ Worked for 1m",
];
const BLOCKER = ["⏺ BLOCKED: needs PM decision on AC3 (prod creds).", "✻ Worked for 1m"];

type Pane = {
  paneIds: string[]; // successive display-message answers (last one sticks)
  history: string[];
  composer: string;
  buffer: string;
  pastes: number;
  enters: number;
  ackLoss: boolean;
  /** Enter lands but the TUI never redraws the cleared composer. */
  silentEnter?: boolean;
  onIdentity?: (n: number) => void;
  onPaste?: () => void;
  identityCalls: number;
};

function render(p: Pane): string {
  return [...p.history, "", RULE, `❯ ${p.composer}`, RULE, "  ⏵⏵ bypass permissions on"].join("\n");
}

function fakeShell(p: Pane) {
  return async (command: string) => {
    if (command.startsWith("tmux display-message")) {
      p.identityCalls += 1;
      p.onIdentity?.(p.identityCalls);
      const id = p.paneIds[Math.min(p.identityCalls - 1, p.paneIds.length - 1)];
      return { stdout: `${id}|${CHECKOUT}\n`, stderr: "" };
    }
    if (command.startsWith("git -C")) return { stdout: `${CHECKOUT}\n`, stderr: "" };
    if (command.startsWith("tmux capture-pane")) {
      if (p.ackLoss && p.enters > 0) throw Object.assign(new Error("timeout"), { code: "ETIMEDOUT" });
      return { stdout: render(p), stderr: "" };
    }
    if (command.startsWith("tmux load-buffer")) {
      const file = command.match(/'([^']+)'\s*$/)?.[1] ?? "";
      p.buffer = await readFile(file, "utf8");
      return { stdout: "", stderr: "" };
    }
    if (command.startsWith("tmux paste-buffer")) {
      p.pastes += 1;
      p.composer = p.buffer;
      p.onPaste?.();
      return { stdout: "", stderr: "" };
    }
    if (/^tmux send-keys .* Enter$/.test(command)) {
      p.enters += 1;
      if (p.ackLoss) throw Object.assign(new Error("timeout"), { code: "ETIMEDOUT" });
      if (p.silentEnter) return { stdout: "", stderr: "" };
      p.composer = "";
      return { stdout: "", stderr: "" };
    }
    throw new Error(`unexpected command: ${command}`);
  };
}

function slotState(overrides: Partial<SlotState> = {}): SlotState {
  return {
    slot: 1, address: "0:0.1", name: "Rohini", status: "active", occupied: true, session_id: "s1",
    task: "issue 8776", repository_id: "github:heydonna-app/heydonna-app", issue: 8776, branch: "fix/8776",
    branch_ref: "refs/heads/fix/8776", pr: 8790, head_sha: "a".repeat(40), assignment_epoch: 9,
    assigned_at: "2026-10-03T10:00:00.000Z", last_activity: null, dnd: false, idle: true, activity: null,
    active_turn_id: null, active_turn_started_at: null, active_turn_state: "inactive",
    last_meaningful_work_at: null,
    ...overrides,
  } as SlotState;
}

function harness(pane: Partial<Pane> = {}) {
  const p: Pane = {
    paneIds: ["%11"], history: [...STALL_A], composer: "", buffer: "", pastes: 0, enters: 0,
    ackLoss: false, identityCalls: 0, ...pane,
  };
  const slot = slotState();
  const events: EventLogEntry[] = [];
  let id = 1;
  const db = {
    getExitPending: () => false,
    hasPendingClear: () => false,
    hasActiveNativeReleaseIntent: () => false,
    claimNativeReleaseIntent: () => true,
    clearNativeReleaseIntent: () => undefined,
    hasRecentSubagentDispatch: () => null,
    getSlot: () => slot,
    getAllSlots: () => [slot],
    getEvents: (_s: number, limit: number, type?: string) =>
      events.filter((e) => !type || e.event_type === type).sort((a, b) => b.id - a.id).slice(0, limit),
    logEvent: (s: number, type: string, hook: string | null, tool: string | null, payload: unknown) => {
      events.push({
        id: id++, timestamp: new Date(Date.now()).toISOString().replace(/Z$/, ""), slot: s,
        event_type: type, hook_type: hook, tool_name: tool, payload: JSON.stringify(payload ?? {}), processed: false,
      });
      return id - 1;
    },
  } as unknown as MoPDatabase;
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: fakeShell(p) as never });
  const pmAlerts: string[] = [];
  (relay as unknown as { injectToPM: (t: string) => void }).injectToPM = (t) => { pmAlerts.push(t); };
  const det = new StuckDetector(db, { getLogMtime: async () => null } as unknown as LogManager, relay);
  const inner = det as unknown as {
    apiStall: ApiStallTracker;
    checkApi500Backoff: (s: SlotState) => Promise<void>;
  };
  inner.apiStall = new ApiStallTracker(0);
  const check = () => inner.checkApi500Backoff(slot);
  const types = () => events.map((e) => e.event_type);
  return { p, slot, events, types, pmAlerts, det, inner, check };
}

test("control: an eligible stall gets exactly one paste and one Enter, then never again", async () => {
  const h = harness();
  await h.check();
  assert.equal(h.p.pastes, 1);
  assert.equal(h.p.enters, 1);
  assert.ok(h.types().includes("api_stall_nudge"));
  assert.equal(h.det.getApiStallStatus(1).state, "nudged");
  await h.check();
  await h.check();
  assert.equal(h.p.pastes, 1);
  assert.equal(h.p.enters, 1);
});

test("stale pane: delivery whose verified pane id differs from the observed one sends nothing", async () => {
  const h = harness({ paneIds: ["%11", "%12"] });
  await h.check();
  assert.equal(h.p.pastes, 0);
  assert.equal(h.p.enters, 0);
  assert.ok(h.types().includes("api_stall_nudge_refused"));
  // A proven pre-effect refusal does not consume the episode.
  assert.equal(h.det.getApiStallStatus(1).state, "stall_pending");
});

test("active turn: a turn that starts after observation gets zero keystrokes", async () => {
  const h = harness();
  h.p.onIdentity = (n) => { if (n === 2) h.slot.active_turn_state = "active"; };
  await h.check();
  assert.equal(h.p.pastes, 0);
  assert.equal(h.p.enters, 0);
  assert.ok(h.types().includes("api_stall_nudge_refused"));
});

test("active turn right before Enter: no Enter, outcome fenced as uncertain, never repasted", async () => {
  const h = harness();
  h.p.onPaste = () => { h.slot.active_turn_state = "active"; };
  await h.check();
  assert.equal(h.p.pastes, 1);
  assert.equal(h.p.enters, 0, "never press Enter into an active turn");
  assert.ok(h.types().includes("api_stall_nudge_uncertain"));
  h.slot.active_turn_state = "inactive";
  h.p.onPaste = undefined;
  h.p.composer = "";
  await h.check();
  await h.check();
  assert.equal(h.p.pastes, 1, "no automatic repaste after an uncertain outcome");
  assert.equal(h.p.enters, 0);
});

test("PM-decision blocker visible at the send boundary: zero sends", async () => {
  const h = harness();
  h.p.onIdentity = (n) => { if (n === 2) h.p.history.push(...BLOCKER); };
  await h.check();
  assert.equal(h.p.pastes, 0);
  assert.equal(h.p.enters, 0);
  const refused = h.events.find((e) => e.event_type === "api_stall_nudge_refused");
  assert.match(JSON.parse(refused!.payload).detail, /^stall_changed/);
});

test("ack loss: at most one Enter, no retry or repaste, episode fenced", async () => {
  const h = harness({ ackLoss: true });
  await h.check();
  assert.equal(h.p.pastes, 1);
  assert.equal(h.p.enters, 1);
  assert.ok(h.types().includes("api_stall_nudge_uncertain"));
  h.p.ackLoss = false;
  await h.check();
  await h.check();
  assert.equal(h.p.pastes, 1);
  assert.equal(h.p.enters, 1);
});

test("ack loss with an unchanged composer: exactly one Enter, never a second", async () => {
  const h = harness({ silentEnter: true });
  await h.check();
  assert.equal(h.p.pastes, 1);
  assert.equal(h.p.enters, 1);
  assert.ok(h.types().includes("api_stall_nudge_uncertain"));
  h.p.silentEnter = false;
  h.p.composer = "";
  await h.check();
  assert.equal(h.p.pastes, 1);
  assert.equal(h.p.enters, 1);
});

test("control: a new stall after a delivered nudge gets one more nudge", async () => {
  const h = harness();
  await h.check();
  h.p.history.push(...STALL_B_EXTRA);
  await h.check();
  await h.check();
  assert.equal(h.p.pastes, 2);
  assert.equal(h.p.enters, 2);
});

test("capped stall: exactly one PM escalation and ZERO generic idle continuation", async () => {
  const originalNow = Date.now;
  const NOW = Date.parse("2026-10-03T12:00:00.000Z");
  Date.now = () => NOW;
  delete process.env.MOP_PM_WAIT_NUDGES_DISABLED;
  try {
    const h = harness();
    // Three consecutive re-stalls within the window, each nudged.
    for (let i = 0; i < 3; i++) {
      h.inner.apiStall.observe(1, NOW - 60_000 + i, {
        occupied: true, turnIdle: true, epoch: 9,
        classification: { stalled: true, signature: "http_5xx", reason: "stall", fingerprint: `f${i}`, errorText: "x" },
      });
      h.inner.apiStall.recordNudge(1, NOW - 60_000 + i);
    }
    // Hook idle anchor older than the generic threshold.
    h.events.push({
      id: 999, timestamp: new Date(NOW - 6 * 60_000).toISOString().replace(/Z$/, ""), slot: 1,
      event_type: "Stop", hook_type: "Stop", tool_name: null, payload: "{}", processed: false,
    });
    await h.check();
    assert.equal(h.pmAlerts.length, 1, "one escalation");
    assert.equal(h.det.getApiStallStatus(1).state, "capped");
    await h.det.checkIdleOccupied(h.slot);
    await h.check();
    await h.det.checkIdleOccupied(h.slot);
    assert.equal(h.pmAlerts.length, 1, "still exactly one escalation");
    assert.equal(h.p.pastes, 0, "capped means zero automatic continuation");
    assert.equal(h.p.enters, 0);
    assert.ok(!h.types().includes("idle_occupied_continue_injected"));

    // Control: the same idle slot without an API-stall episode does get the
    // generic continuation, through the same real relay.
    const c = harness({ history: ["⏺ Done with phase 1.", "✻ Worked for 2m"] });
    c.events.push({
      id: 999, timestamp: new Date(NOW - 6 * 60_000).toISOString().replace(/Z$/, ""), slot: 1,
      event_type: "Stop", hook_type: "Stop", tool_name: null, payload: "{}", processed: false,
    });
    await c.det.checkIdleOccupied(c.slot);
    assert.ok(c.types().includes("idle_occupied_continue_injected"), c.types().join(","));
    assert.equal(c.p.enters, 1);
  } finally {
    Date.now = originalNow;
  }
});
