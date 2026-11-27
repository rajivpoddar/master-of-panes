// CTO REVISE 3 of 0143274d (C0ALZJHGE49 thread 1795798240.220189, ts 1795801872.756899): the PM Stop's live
// replacement probe had its 2 s deadline reach exitProcessIdentity only after verifyPaneIdentity finished, and the
// pane identity read gave tmux and git (fallback scan included) 3 s each. The installed hook relay gives the whole Stop
// HTTP request 3 s, so a slow identity read could outlive the hook. Now ONE deadline covers the whole probe; at expiry
// custody is unchanged and the original Stop continues.
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { MoPDatabase } from "../src/db.js";
import { PM_STOP_REPLACEMENT_PROBE_BUDGET_MS } from "../src/devExitAttempt.js";
import { commandTimeout, verifyPaneIdentity } from "../src/paneIdentity.js";
import {
  PM_EXIT_CONSUMED_EPISODE_KEY,
  pmExitInCustody,
  selectPmStopMaintenance,
  selectPmStopWithReplacementProbe,
} from "../src/pmHandoffExit.js";
import { registerPMSoftBoundary } from "../src/pmSoftBoundary.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const SESSION = "213444f3-9a67-4bc3-aaf6-53dc24cecce5";
const PM_PATH = "/Users/rajiv/Downloads/projects/heydonna-app";
const REPLACEMENT = JSON.stringify(["%1", 61234, "Fri Nov 27 16:40:00 2026"]);
/** The installed hook relay's whole-request budget (scripts/hook-relay.sh, urlopen timeout=3). */
const HOOK_BUDGET_MS = 3_000;
/** Scheduling slack for timers on a loaded runner. */
const SLACK_MS = 250;

/** The stale UNKNOWN exit in custody with a bound receipt and a settled continuation (as in the reconcile tests). */
function stuckEpisode() {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(mkdtempSync(join(tmpdir(), "mop-probe-deadline-")), "mop.db") });
  db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: SESSION, source: "startup" });
  db.setExitPending(true, false, true, undefined, true);
  const episode = db.getConfig("exit_pending_episode")!;
  db.setConfig(PM_EXIT_CONSUMED_EPISODE_KEY, episode);
  db.setConfig("exit_pending_continuation_delivery", "attempted");
  db.logEvent(0, "pm_restart_continuation", null, null, { episode, trigger: "exit", boot_event_id: 1, delivery: "delivered" });
  const stopEventId = db.logEvent(0, "Stop", "Stop", null, { session_id: SESSION });
  db.saveExitAttempt(0, {
    episode, paneId: "%1", originalClaudePid: 58216, originalStart: "Fri Nov 27 13:16:29 2026",
    originalProcess: JSON.stringify(["%1", 58216, "Fri Nov 27 13:16:29 2026"]), sessionId: SESSION,
    inputEventId: null, assignmentEpoch: 0, stopEventId, phase: "unknown", attemptCount: 1,
    ownerPin: { assignment: "pm", savedPmHandoff: true },
    submittedAt: new Date().toISOString(), blockedReason: "replacement_not_owned_by_restart",
  });
  const attempt = db.getExitAttempt(0)!;
  const launchEventId = db.logEvent(0, "process_dead", null, null, { trigger: "exit" });
  const bootEventId = db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: SESSION, source: "resume" });
  db.logEvent(0, "exit_replacement_bound", null, null, {
    episode, submittedAt: attempt.submittedAt, assignmentEpoch: attempt.assignmentEpoch, launchEventId, bootEventId,
    bootSession: SESSION, ownedProcess: "61200:61234", replacementProcess: REPLACEMENT,
  });
  return db;
}

type Stage = "pane" | "git" | "scan" | "process" | "none";
type Call = { command: string; timeout: number | undefined; remaining: number };

/**
 * A pane whose live replacement is REPLACEMENT. The stalled stage either never answers ("hang": ignores its timeout,
 * the worst case) or answers like child_process exec at its timeout ("honor"). Every call records its timeout.
 */
function paneRunner(stall: Stage, mode: "hang" | "honor", deadline: () => number) {
  const calls: Call[] = [];
  let gitCalls = 0;
  const runShell = async (command: string, options?: { timeout?: number }) => {
    calls.push({ command, timeout: options?.timeout, remaining: deadline() - Date.now() });
    const stage: Stage = command.includes("#{pane_pid}") || command.startsWith("ps -t") ? "process"
      : command.startsWith("tmux display-message") && command.includes("#{pane_id}|#{pane_current_path}") ? "pane"
      : command.startsWith("git -C") ? (++gitCalls === 1 ? "git" : "scan")
      : "none";
    // The scan stage needs the direct git check to miss quickly so the fallback scan runs.
    if (stall === "scan" && stage === "git") throw new Error("direct checkout moved");
    if (stage === stall && (stall !== "process" || command.startsWith("ps -t"))) {
      if (mode === "hang") return new Promise<never>(() => { /* never answers */ });
      await new Promise(resolve => setTimeout(resolve, options?.timeout ?? 60_000));
      throw new Error(`timed out after ${options?.timeout}ms`);
    }
    if (command.includes("#{pane_pid}")) return { stdout: `%1|61200|/dev/ttys009|${PM_PATH}|claude`, stderr: "" };
    if (command.startsWith("ps -t")) return { stdout: "61234 61234 S+   Fri Nov 27 16:40:00 2026 claude\n", stderr: "" };
    if (command.includes("#{pane_current_path}")) return { stdout: `%1|${PM_PATH}`, stderr: "" };
    if (command.startsWith("git -C")) return { stdout: PM_PATH, stderr: "" };
    return { stdout: "", stderr: "" };
  };
  return { runShell, calls };
}

test("commandTimeout caps each command at the shared deadline and never starts one past it (never 0)", () => {
  const now = 1_000_000;
  assert.equal(commandTimeout(undefined, 3_000, now), 3_000);
  assert.equal(commandTimeout(now + 500, 3_000, now), 500);
  assert.equal(commandTimeout(now + 5_000, 3_000, now), 3_000);
  assert.equal(commandTimeout(now + 0.4, 3_000, now), null);
  assert.equal(commandTimeout(now, 3_000, now), null);
  assert.equal(commandTimeout(now - 1, 3_000, now), null);
});

for (const stall of ["pane", "git", "scan"] as const) {
  test(`verifyPaneIdentity under a deadline: a slow ${stall} read uses only the remaining budget and fails closed`, async () => {
    const deadline = Date.now() + 300;
    const { runShell, calls } = paneRunner(stall, "honor", () => deadline);
    const started = Date.now();
    const result = await verifyPaneIdentity(0, runShell, deadline);
    const elapsed = Date.now() - started;
    assert.equal(result.ok, false);
    assert.ok(elapsed < 300 + SLACK_MS, `returned after ${elapsed}ms`);
    assert.ok(calls.length > 0);
    for (const call of calls) {
      assert.ok(call.timeout !== undefined && call.timeout > 0, `${call.command}: timeout ${call.timeout}`);
      assert.ok(call.timeout <= Math.ceil(call.remaining), `${call.command}: timeout ${call.timeout} > remaining ${call.remaining}`);
    }
  });
}

for (const stall of ["pane", "git", "scan", "process"] as const) {
  test(`a ${stall} probe that never answers: the replacement probe returns null at the deadline`, async () => {
    const db = stuckEpisode();
    const deadline = Date.now() + 300;
    const { runShell } = paneRunner(stall, "hang", () => deadline);
    const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude", runShell });
    relay.setDatabase(db);
    const started = Date.now();
    const live = await relay.currentExitReplacementProcess(0, db.getExitAttempt(0)!, deadline);
    const elapsed = Date.now() - started;
    assert.equal(live, null);
    assert.ok(elapsed < 300 + SLACK_MS, `returned after ${elapsed}ms`);
  });
}

function stopHarness(stall: Stage) {
  const db = stuckEpisode();
  const { runShell, calls } = paneRunner(stall, "hang", () => Date.now() + PM_STOP_REPLACEMENT_PROBE_BUDGET_MS);
  const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude", runShell });
  relay.setDatabase(db);
  const app = new Hono();
  let downstreamStops = 0;
  // The exact selection server.ts wires into the boundary.
  registerPMSoftBoundary(app, db, relay, undefined, undefined, undefined, (sessionId) =>
    selectPmStopWithReplacementProbe(db, {
      sessionId,
      pendingConfirmation: (episode) => relay.hasPendingExitConfirmation(episode),
      select: () => selectPmStopMaintenance(db as any, { ops: null, files: [] }),
      probe: (candidate, deadline) => relay.currentExitReplacementProcess(0, candidate, deadline),
    }));
  app.post("/hooks/slot/0", async c => {
    const raw = await c.req.json();
    if (raw.hook_event_name === "Stop") downstreamStops++;
    return c.json({ ok: true });
  });
  const stop = async () => {
    const started = Date.now();
    const response = await app.request("/hooks/slot/0", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hook_event_name: "Stop", session_id: SESSION, mop_tool_tracking: 1 }) });
    return { response, elapsed: Date.now() - started };
  };
  const selected = () => JSON.parse(db.getEvents(0, 1, "pm_stop_maintenance_selected")[0]!.payload).selected;
  return { db, calls, stop, selected, downstream: () => downstreamStops };
}

test("a stalled pane probe: the PM Stop returns within the hook budget, the downstream Stop runs, custody is unchanged", async () => {
  const h = stopHarness("pane");
  const before = { attempt: h.db.getExitAttempt(0), status: h.db.getExitStatus(), clear: h.db.hasPendingClear(0) };
  const { response, elapsed } = await h.stop();
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  assert.ok(elapsed < PM_STOP_REPLACEMENT_PROBE_BUDGET_MS + SLACK_MS, `Stop answered after ${elapsed}ms`);
  assert.ok(elapsed < HOOK_BUDGET_MS);
  assert.equal(h.downstream(), 1, "the original Stop still reaches HookProcessor");
  assert.ok(h.calls.some(call => call.command.startsWith("tmux display-message")), "the live probe actually ran");
  // Custody unchanged: same attempt, still pending, nothing reconciled, no clear invented.
  assert.equal(h.selected(), "exit_custody");
  assert.equal(pmExitInCustody(h.db), true);
  assert.deepEqual(h.db.getExitAttempt(0), before.attempt);
  assert.equal(h.db.getExitStatus().pending, before.status.pending);
  assert.equal(h.db.getExitStatus().episode, before.status.episode);
  assert.equal(h.db.getEvents(0, 5, "pm_exit_custody_reconciled").length, 0);
  assert.equal(h.db.hasPendingClear(0), before.clear);
});

test("positive control: a live, receipt-bound replacement answered in time releases custody exactly once through the Stop", async () => {
  const h = stopHarness("none");
  assert.equal(pmExitInCustody(h.db), true);
  const first = await h.stop();
  assert.equal(first.response.status, 200);
  assert.ok(first.elapsed < HOOK_BUDGET_MS);
  assert.equal(h.downstream(), 1);
  assert.equal(pmExitInCustody(h.db), false);
  assert.equal(h.selected(), "clear");
  const reconciled = h.db.getEvents(0, 5, "pm_exit_custody_reconciled");
  assert.equal(reconciled.length, 1);
  assert.equal(JSON.parse(reconciled[0]!.payload).replacement_process, REPLACEMENT);
  // Every probe command ran under the one Stop deadline.
  for (const call of h.calls) assert.ok(call.timeout !== undefined && call.timeout > 0 && call.timeout <= PM_STOP_REPLACEMENT_PROBE_BUDGET_MS);
  const second = await h.stop();
  assert.equal(second.response.status, 200);
  assert.equal(h.downstream(), 2);
  assert.equal(h.db.getEvents(0, 5, "pm_exit_custody_reconciled").length, 1, "released once");
});
