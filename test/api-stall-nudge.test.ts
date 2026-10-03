import assert from "node:assert/strict";
import test from "node:test";

import {
  API_STALL_NUDGE_MESSAGE,
  ApiStallTracker,
  classifyApiStallTail,
} from "../src/apiStall.js";
import type { MoPDatabase } from "../src/db.js";
import type { LogManager } from "../src/logs.js";
import type { TmuxRelay } from "../src/relay.js";

const PROMPT = ["", "──────────────────────────", "❯ ", "──────────────────────────", "  ⏵⏵ bypass permissions on"];

const STREAM_STALL = [
  "⏺ Monitor event: [Monitor expired after 15m with 2 events delivered.",
  "",
  "⏺ API Error: stream error: stream disconnected before completion: stream closed before",
  "  response.completed",
  "",
  "✻ Cogitated for 1h 21m 5s · done 10:36 PM",
  ...PROMPT,
].join("\n");

const OVERLOAD_STALL = [
  "⏺ Bash(git push)",
  "  ⎿  Everything up-to-date",
  "⏺ API Error: Our servers are currently overloaded. Please try again later.",
  "✻ Worked for 12m · done 9:03 PM",
  ...PROMPT,
].join("\n");

test("signatures: stream disconnect, overload, 5xx/529, connection reset are stalls", () => {
  assert.equal(classifyApiStallTail(STREAM_STALL).signature, "stream_disconnected");
  assert.equal(classifyApiStallTail(OVERLOAD_STALL).signature, "servers_overloaded");
  for (const [line, sig] of [
    ["⏺ API Error: 529 {\"type\":\"error\"}", "http_5xx"],
    ["⏺ API Error: 503 Service Unavailable", "http_5xx"],
    ["⏺ API Error: Internal server error", "internal_server_error"],
    ["⏺ API Error: Connection error. (ECONNRESET)", "connection_reset"],
  ]) {
    const c = classifyApiStallTail([line, "✻ Worked for 1m", ...PROMPT].join("\n"));
    assert.equal(c.stalled, true, line);
    assert.equal(c.signature, sig, line);
  }
});

test("retry banner is Claude Code's own retry, not a stall", () => {
  const pane = [
    "⏺ API Error: 529 overloaded",
    "✻ Waiting for API response · will retry in 2m",
    ...PROMPT,
  ].join("\n");
  const c = classifyApiStallTail(pane);
  assert.equal(c.stalled, false);
  assert.equal(c.reason, "retry_in_progress");
});

test("normal completions, typed blockers and message-pm reports are not stalls", () => {
  const normal = ["⏺ Pushed 9f82df0; PR #8790 opened.", "✻ Worked for 4m · done", ...PROMPT].join("\n");
  const blocker = ["⏺ BLOCKED: needs PM decision on AC3 (prod creds).", "✻ Worked for 1m", ...PROMPT].join("\n");
  const report = [
    "⏺ Bash(~/.claude/scripts/message-pm \"slot 1: hit API Error: 529 earlier, now done\")",
    "  ⎿  sent",
    "✻ Worked for 2m",
    ...PROMPT,
  ].join("\n");
  const subagent = [
    "  ⎿  Error: Agent terminated early due to an API error: API Error: Our servers are currently",
    "     overloaded.",
    "✻ Worked for 2m",
    ...PROMPT,
  ].join("\n");
  const nonTransient = ["⏺ API Error: 400 invalid_request_error prompt is too long", "✻ Worked", ...PROMPT].join("\n");
  for (const pane of [normal, blocker, report, subagent]) {
    assert.equal(classifyApiStallTail(pane).stalled, false, pane);
  }
  assert.equal(classifyApiStallTail(nonTransient).reason, "non_transient_api_error");
});

test("a slot that resumed after the error is not a stall", () => {
  const pane = [
    "⏺ API Error: stream error: stream disconnected before completion",
    "✻ Cogitated for 3m · done",
    "❯ [11:14 PM] PM -> slot 1: resume #8776",
    "⏺ The budget-fixed code is already committed and pushed.",
    "✻ Worked for 1m",
    ...PROMPT,
  ].join("\n");
  assert.equal(classifyApiStallTail(pane).reason, "resumed_after_error");
});

const T0 = 1_000_000_000;
const idle = (pane: string) => ({ occupied: true, turnIdle: true, epoch: 7, classification: classifyApiStallTail(pane) });

test("tracker: grace period, then exactly one nudge per stall", () => {
  const t = new ApiStallTracker(75_000);
  assert.equal(t.observe(1, T0, idle(STREAM_STALL)).kind, "wait");
  assert.equal(t.observe(1, T0 + 60_000, idle(STREAM_STALL)).kind, "wait");
  assert.equal(t.observe(1, T0 + 80_000, idle(STREAM_STALL)).kind, "nudge");
  t.recordNudge(1, T0 + 80_000);
  assert.equal(t.status(1).state, "nudged");
  assert.equal(t.observe(1, T0 + 140_000, idle(STREAM_STALL)).kind, "none");
  assert.equal(t.observe(1, T0 + 900_000, idle(STREAM_STALL)).kind, "none");
});

test("tracker: active turn or normal idle never nudges", () => {
  const t = new ApiStallTracker(0);
  const active = { ...idle(STREAM_STALL), turnIdle: false };
  assert.equal(t.observe(1, T0, active).kind, "none");
  const normal = ["⏺ done", ...PROMPT].join("\n");
  assert.equal(t.observe(1, T0 + 1, idle(normal)).kind, "none");
  const banner = ["⏺ API Error: 529", "✻ Waiting for API response · will retry in 1m", ...PROMPT].join("\n");
  assert.equal(t.observe(1, T0 + 2, idle(banner)).kind, "none");
});

function restall(n: number): string {
  // Each re-stall adds one more API Error line to the pane -> new fingerprint.
  const lines: string[] = [];
  for (let i = 0; i <= n; i++) {
    lines.push("⏺ API Error: Our servers are currently overloaded.", "✻ Worked for 1m");
    if (i < n) lines.push("❯ nudge", "⏺ resuming");
  }
  // Keep only the last error at the tail (earlier ones are followed by resume lines).
  return [...lines, ...PROMPT].join("\n");
}

test("tracker: backoff cap after 3 consecutive re-stalls within 10 min alerts PM once", () => {
  const t = new ApiStallTracker(60_000);
  let now = T0;
  for (let n = 0; n < 3; n++) {
    assert.equal(t.observe(1, now, idle(restall(n))).kind, "wait");
    now += 61_000;
    const a = t.observe(1, now, idle(restall(n)));
    assert.deepEqual(a, { kind: "nudge", consecutive: n + 1 });
    t.recordNudge(1, now);
    now += 2 * 60_000; // re-stalls 2 min after each nudge
  }
  assert.equal(t.observe(1, now, idle(restall(3))).kind, "cap_alert");
  assert.equal(t.status(1).state, "capped");
  now += 61_000;
  assert.equal(t.observe(1, now, idle(restall(3))).kind, "none");
  // A stall well outside the window after recovery restarts the chain.
  now += 11 * 60_000;
  assert.equal(t.observe(1, now, idle(restall(4))).kind, "wait");
  assert.equal(t.status(1).consecutive_nudges, 0);
});

test("tracker: a stall more than 10 min after the last nudge resets the chain", () => {
  const t = new ApiStallTracker(0);
  let now = T0;
  for (let n = 0; n < 5; n++) {
    assert.equal(t.observe(1, now, idle(restall(n))).kind, "nudge");
    t.recordNudge(1, now);
    now += 11 * 60_000;
  }
});

test("StuckDetector delivers the nudge via the guarded relay, logs it, and surfaces status", async () => {
  const { StuckDetector } = await import("../dist/stuck.js");
  const slot = {
    slot: 1, address: "0:0.1", name: "S1", status: "busy", occupied: true, session_id: "s",
    task: "t", issue: 8776, branch: "b", pr: null, head_sha: null, assignment_epoch: 7,
    assigned_at: "2026-10-03T10:00:00.000", last_activity: null, dnd: false, idle: true,
    activity: null, active_turn_id: null, active_turn_started_at: null,
    active_turn_state: "inactive", last_meaningful_work_at: null,
  };
  const events: Array<{ type: string; payload: unknown }> = [];
  const db = {
    getSlot: () => slot,
    logEvent: (_s: number, type: string, _a: unknown, _b: unknown, payload: unknown) => events.push({ type, payload }),
    hasActiveNativeReleaseIntent: () => false,
  } as unknown as MoPDatabase;
  const sent: string[] = [];
  const pmAlerts: string[] = [];
  const relay = {
    sendToSlot: async (_n: number, text: string) => { sent.push(text); return true; },
    injectToPM: (t: string) => pmAlerts.push(t),
  } as unknown as TmuxRelay;
  const det = new StuckDetector(db, {} as LogManager, relay) as unknown as {
    apiStall: ApiStallTracker;
    sendContinueIfAllowed: (n: number, e: unknown, cmd: string) => Promise<{ sent: boolean; reason: string; slot: unknown }>;
    checkApi500Backoff: (s: unknown) => Promise<void>;
    getApiStallStatus: (n: number) => { state: string };
  };
  det.apiStall = new ApiStallTracker(0);
  det.sendContinueIfAllowed = async (_n, _e, cmd) => { sent.push(cmd); return { sent: true, reason: "sent", slot }; };
  // Fake tmux on PATH returns the stalled pane.
  process.env.PATH = `${await fakeTmux(STREAM_STALL)}:${process.env.PATH}`;
  await det.checkApi500Backoff(slot);
  assert.deepEqual(sent, [API_STALL_NUDGE_MESSAGE]);
  assert.ok(events.some((e) => e.type === "api_stall_nudge"));
  assert.equal(det.getApiStallStatus(1).state, "nudged");
  await det.checkApi500Backoff(slot);
  assert.equal(sent.length, 1, "one nudge per stall");
  assert.equal(pmAlerts.length, 0);
});

async function fakeTmux(pane: string): Promise<string> {
  const { mkdtemp, writeFile, chmod } = await import("node:fs/promises");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const dir = await mkdtemp(join(tmpdir(), "fake-tmux-"));
  const paneFile = join(dir, "pane.txt");
  await writeFile(paneFile, pane);
  const bin = join(dir, "tmux");
  await writeFile(bin, `#!/bin/sh\ncat '${paneFile}'\n`);
  await chmod(bin, 0o755);
  return dir;
}
