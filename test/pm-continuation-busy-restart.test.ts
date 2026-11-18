// CTO REVISE C0ALZJHGE49 thread 1794970059.617269, ts 1794973351.556359, on b2395c7: the focused check is the SAME PM-only
// exit episode driven through (1) a busy replacement boot, (2) a concurrent inbound write and (3) a MoP restart after the
// delivery receipt. One continuation, no mixed paste, PM-only (or with_devs) completion, and Stop / inbound /
// UserPromptSubmit are never delivery prerequisites.
// Real TmuxRelay + real ProcessHealthChecker against ONE fake tmux/ps world: a pane whose pastes APPEND (so mixing is
// observable), a shell that boots a BUSY Claude when the launcher is submitted, and process listings.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { PM_RESTART_CONTINUATION_PROMPT, ProcessHealthChecker } from "../src/health.js";
import { TmuxRelay } from "../src/relay.js";
import { withSlotSendLock } from "../src/slotSendLock.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const CHECKOUT = "/Users/rajiv/Downloads/projects/heydonna-app";
const CLAUDE = join(homedir(), ".local/opt/claude-code/claude");
const RULE = "─".repeat(40);
const SHELL_TEXT = "some earlier output\n\x1b[32mrajiv@heydonna\x1b[39m ~ \x1b[31m»\x1b[39m\n$ \n\n";
const PROMPT = "saved continuation";
const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

type World = ReturnType<typeof world>;

// One tmux/ps world shared by the relay and the health checker. "shell": the old PM exited, a zsh prompt is up.
// "booted": the launcher's Claude (pid 4001, child of shell 4000) is up and BUSY with a turn.
function world(db: MoPDatabase) {
  const pane = {
    phase: "shell" as "shell" | "booted", composer: "", queuedBanner: false, bannerBeforeEnter: false,
    keepTextOnEnter: false, buffer: "", typed: [] as string[], pasted: [] as string[], submitted: [] as string[],
    enters: 0, onPaste: null as null | (() => void), onBoot: null as null | (() => void), bootSession: "replacement",
    captures: 0,
  };
  const runShell = async (command: string) => {
    const out = (stdout: string) => ({ stdout, stderr: "" });
    if (command.includes("#{pane_id}|#{pane_pid}|#{pane_tty}")) return out("%1|4000|/dev/ttys040|" + CHECKOUT + "|" + (pane.phase === "shell" ? "zsh" : "claude") + "\n");
    if (command.includes("#{pane_pid}|#{pane_tty}")) return out("4000|/dev/ttys040\n");
    if (command.includes("#{pane_id}|#{pane_current_path}")) return out("%1|" + CHECKOUT + "\n");
    if (command.includes("#{pane_id}|#{pane_current_command}")) return out("%1|" + (pane.phase === "shell" ? "zsh" : "claude") + "\n");
    if (command.includes("'#{pane_id}'")) return out("%1\n");
    if (command.includes("rev-parse --show-toplevel")) return out(CHECKOUT + "\n");
    if (command.startsWith("ps -t") && command.includes("pid=,ppid=,command=")) {
      return out("4000 1 -zsh\n" + (pane.phase === "booted" ? "4001 4000 " + CLAUDE + " --model claude-opus-5-5\n" : ""));
    }
    if (command.startsWith("ps -t") && command.includes("lstart=")) {
      return out(pane.phase === "booted" ? "4001 4001 S+ Wed Nov 18 08:18:49 2026 " + CLAUDE + "\n" : "4000 4000 Ss+ Wed Nov 18 08:00:00 2026 zsh\n");
    }
    if (command.startsWith("tmux capture-pane")) {
      pane.captures++;
      if (pane.phase === "shell") return out(SHELL_TEXT);
      // A busy turn: never an idle prompt. The queued banner sits above the composer box once something is queued.
      return out("✻ Working… (esc to interrupt)\n" + (pane.queuedBanner ? "  Press up to edit queued messages\n" : "")
        + RULE + "\n❯ " + pane.composer + "\n" + RULE + "\n  ? for shortcuts");
    }
    const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(command);
    if (load) pane.buffer = readFileSync(load[1]!, "utf8");
    if (command.startsWith("tmux paste-buffer")) {
      pane.composer += pane.buffer; // a paste lands after whatever is already in the composer
      pane.pasted.push(pane.buffer);
      const hook = pane.onPaste;
      pane.onPaste = null; // one-shot
      hook?.();
    }
    if (command.startsWith("tmux send-keys -l")) pane.typed.push(command);
    if (command.endsWith(" C-u")) pane.composer = "";
    if (command.startsWith("tmux send-keys") && command.endsWith(" Enter")) {
      if (pane.phase === "shell" && pane.typed.length) {
        pane.phase = "booted";
        db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: pane.bootSession, cwd: CHECKOUT });
        pane.onBoot?.();
      } else if (pane.phase === "booted") {
        pane.enters++;
        pane.bannerBeforeEnter = pane.queuedBanner;
        pane.submitted.push(pane.composer);
        if (!pane.keepTextOnEnter) pane.composer = "";
        pane.queuedBanner = true; // the busy turn queues what was submitted
      }
    }
    return out("");
  };
  const relays: TmuxRelay[] = [];
  const build = () => {
    const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude", pmPaneReady: async () => true, runShell: runShell as never });
    relay.setDatabase(db);
    // Never idle: no step of the replacement binding or the delivery may need an idle PM.
    (relay as any).getSlotActivityState = async () => "active";
    relays.push(relay);
    const health = new ProcessHealthChecker(db, relay as never) as any;
    health.runShell = runShell;
    health.PM_RESTART_POLL_MS = 2;
    health.PM_EXIT_SHELL_TIMEOUT_MS = 500;
    health.PM_FORCE_SHELL_TIMEOUT_MS = 100;
    health.PM_PROMPT_TIMEOUT_MS = 500;
    health.PM_BOOT_EVENT_TIMEOUT_MS = 200;
    health.getPaneCommand = async () => (pane.phase === "shell" ? "zsh" : "claude");
    health.startTime = 0;
    health.isSlotProvisioned = (slot: number) => slot === 0;
    health.checkClaudeResponsiveness = async () => {};
    return { relay, health };
  };
  return { pane, runShell, build, relays };
}

// The exit episode as the PM Stop leaves it: the old PM exited (attempt recorded, saved handoff), a zsh prompt is up.
function arm(db: MoPDatabase, opts: { withDevs?: boolean } = {}) {
  db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "old", cwd: CHECKOUT });
  db.setExitPending(true, false, true, PROMPT, true, opts.withDevs === true);
  const episode = db.getExitStatus().episode!;
  if (opts.withDevs) {
    db.recordExitArm(1, episode, "dev-session-1", 7);
    for (const slot of [2, 3, 4]) db.recordExitArm(slot, episode, "dev-session-" + slot, 7);
  }
  db.saveExitAttempt(0, {
    episode, paneId: "%1", originalClaudePid: 3999, originalStart: "Wed Nov 18 00:29:17 2026",
    originalProcess: '["%1",3999,"Wed Nov 18 00:29:17 2026"]', sessionId: "old", inputEventId: null, assignmentEpoch: 0,
    stopEventId: null, ownerPin: { savedPmHandoff: true } as never, phase: "delivered", attemptCount: 1,
    submittedAt: new Date().toISOString(), blockedReason: null,
  });
  db.logEvent(0, "exit_pending_triggered", "Stop", null, { source: "pm_only_stop", delivery: "delivered", episode, session_id: "old" });
  return episode;
}

const eventId = (db: MoPDatabase, type: string) => db.getEvents(0, 1, type)[0]?.id ?? 0;
const deliveries = (db: MoPDatabase) => db.getEvents(0, 20, "pm_restart_continuation")
  .map((row) => JSON.parse(row.payload)).filter((payload) => payload.delivery === "delivered");

function fresh() {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const w = world(db);
  // The relay fires queue drains and the health checker joins them after a delivery; let them finish before the database closes.
  const done = async () => { await Promise.all(w.relays.map((relay) => relay.drainPMQueue())); await wait(100); db.close(); };
  return { db, w, done };
}

test("a BUSY replacement boot: the launcher binds it without an idle prompt, one continuation, a concurrent inbound goes out alone, the PM-only episode completes", async () => {
  const { db, w, done } = fresh();
  try {
    const episode = arm(db);
    const { relay, health } = w.build();
    let inbound: Promise<unknown> | undefined;
    // A Slack inbound lands exactly when the continuation's paste does.
    w.pane.onPaste = () => { inbound = relay.submitToPM("hello PM"); };
    const result = await health.restartPMFresh("exit", episode);
    await inbound;
    assert.equal(result.success, true, result.reason);
    // Boot and replacement receipts were written while the pane was busy, then the cycle was verified.
    assert.equal(JSON.parse(db.getConfig("pm_restart_launch")!).status, "booted");
    assert.ok(eventId(db, "agent_booted") > 0 && eventId(db, "exit_replacement_bound") > 0);
    assert.ok(eventId(db, "exit_replacement_bound") < eventId(db, "exit_cycle_verified"));
    // One continuation; the inbound was serialized behind it: two submits, neither contains the other's text.
    assert.deepEqual(w.pane.pasted, [PROMPT, "hello PM"]);
    assert.deepEqual(w.pane.submitted, [PROMPT, "hello PM"]);
    assert.equal(deliveries(db).length, 1);
    // PM-only completion: pending cleared, delivered, dev slots never armed.
    const status = db.getExitStatus();
    assert.equal(status.pending, false);
    assert.equal(status.continuation_delivery, "delivered");
    assert.equal(status.dev_only, false);
    assert.equal(db.getEvents(0, 5, "exit_pending_complete").length, 1);
    // No Stop, UserPromptSubmit or idle observation was needed for any of it.
    assert.equal(db.getEvents(0, 5, "Stop").length, 0);
  } finally { await done(); }
});

test("a launch that bound LATE (issued record, busy replacement) records the replacement BEFORE the cycle verification, then delivers once", async () => {
  const { db, w, done } = fresh();
  try {
    const episode = arm(db);
    const launchEventId = db.logEvent(0, "pm_restart_started", null, null, { trigger: "exit", episode });
    db.setConfig("pm_restart_launch", JSON.stringify({
      episode, launch_event_id: launchEventId, pane_id: "%1", command: "launch-pm.sh", status: "issued", pane_pid: "4000",
    }));
    w.pane.phase = "booted"; // the bounded launcher wait ended earlier; the busy Claude is up now
    db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "replacement", cwd: CHECKOUT });
    assert.equal(db.getExitStatus().cycled[0], false, "no receipt yet: the circular dependency of b2395c7");
    // A service restart's first tick, then a second tick.
    const { health } = w.build();
    await health.reconcilePMRestartContinuation();
    await health.reconcilePMRestartContinuation();
    assert.equal(JSON.parse(db.getConfig("pm_restart_launch")!).status, "booted");
    const bound = db.getEvents(0, 1, "process_restarted")[0]!;
    assert.equal(JSON.parse(bound.payload).reason, "late_owned_boot");
    assert.ok(bound.id < eventId(db, "exit_replacement_bound"), "the replacement is recorded after the boot binds");
    assert.ok(eventId(db, "exit_replacement_bound") < eventId(db, "exit_cycle_verified"), "...and BEFORE the cycle verification");
    assert.deepEqual(w.pane.submitted, [PROMPT]);
    assert.equal(db.getExitStatus().pending, false);
    assert.equal(db.getEvents(0, 5, "exit_replacement_bound").length, 1, "idempotent: one receipt");
  } finally { await done(); }
});

test("a successor boot never binds an issued launch: no replacement receipt, no continuation", async () => {
  const { db, w, done } = fresh();
  try {
    const episode = arm(db);
    const launchEventId = db.logEvent(0, "pm_restart_started", null, null, { trigger: "exit", episode });
    db.setConfig("pm_restart_launch", JSON.stringify({
      episode, launch_event_id: launchEventId, pane_id: "%1", command: "launch-pm.sh", status: "issued", pane_pid: "4000",
    }));
    w.pane.phase = "booted";
    for (const session_id of ["first", "successor"]) {
      db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id, cwd: CHECKOUT });
    }
    const { health } = w.build();
    await health.reconcilePMRestartContinuation();
    assert.equal(db.getEvents(0, 5, "exit_replacement_bound").length, 0);
    assert.deepEqual(w.pane.pasted, []);
    assert.equal(JSON.parse(db.getConfig("pm_restart_launch")!).status, "issued");
    assert.equal(db.getExitStatus().pending, true);
  } finally { await done(); }
});

test("one PM paste/Enter lock: an inbound write waits for ANY holder of the slot-0 send lock, not only the chain", async () => {
  const { db, w, done } = fresh();
  try {
    w.pane.phase = "booted";
    const { relay } = w.build();
    let release: (() => void) | undefined;
    const holder = withSlotSendLock(0, () => new Promise<void>((resolve) => { release = resolve; }));
    const inbound = relay.submitToPM("hello PM");
    const prompt = relay.writePMPromptFenced("cadence redelivery", () => null);
    try {
      await wait(200);
      assert.deepEqual(w.pane.pasted, [], "nothing pastes while another writer owns the lock");
    } finally {
      release?.(); // never leave the process-wide lock held for the next test
    }
    await holder;
    await Promise.all([inbound, prompt]);
    assert.deepEqual(w.pane.submitted.sort(), ["cadence redelivery", "hello PM"], "each goes out alone, in turn");
  } finally { await done(); }
});

test("native queue evidence: a banner already visible before Enter alone cannot confirm the send; a new queue indication can", async () => {
  for (const preexisting of [true, false]) {
    const { db, w, done } = fresh();
    try {
      w.pane.phase = "booted";
      w.pane.keepTextOnEnter = true; // the text stays in the composer after Enter: only the banner could confirm
      w.pane.queuedBanner = preexisting;
      const { relay } = w.build();
      const result = await relay.deliverGuardedToSlot(0, "resume", {
        expectedPaneId: "%1", recheck: async () => null, bracketedPaste: true, allowNativeQueuedInput: true,
      });
      if (preexisting) {
        assert.equal(result.outcome, "uncertain", "a banner that was already there proves nothing about THIS send");
      } else {
        assert.equal(result.outcome, "delivered", "a queue indication that appeared after our Enter confirms it");
      }
      assert.equal(w.pane.enters, 1, "never a second Enter");
    } finally { await done(); }
  }
});

test("a non-episode restart retries only a PROVEN pre-effect refusal: a human draft refuses, then the continuation is pasted once", async () => {
  const { db, w, done } = fresh();
  try {
    db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "old", cwd: CHECKOUT });
    const { health } = w.build();
    // The replacement boots with a human draft in its composer; the human sends it a moment later.
    w.pane.onBoot = () => { w.pane.composer = "half-typed thought"; setTimeout(() => { w.pane.composer = ""; }, 40); };
    const result = await health.restartPMFresh("crash");
    assert.equal(result.success, true, result.reason);
    assert.deepEqual(w.pane.pasted, [PM_RESTART_CONTINUATION_PROMPT], "refused attempts wrote nothing; exactly one paste");
    assert.deepEqual(w.pane.submitted, [PM_RESTART_CONTINUATION_PROMPT], "the human draft is never mixed into the continuation");
  } finally { await done(); }
});

test("an exit episode retries a PROVEN pre-effect refusal in flow: a composer that is not empty yet refuses, then the one continuation lands", async () => {
  const { db, w, done } = fresh();
  try {
    const episode = arm(db);
    const { health } = w.build();
    // The busy replacement boots with a draft in its composer that clears a moment later.
    w.pane.onBoot = () => { w.pane.composer = "half-typed thought"; setTimeout(() => { w.pane.composer = ""; }, 40); };
    const result = await health.restartPMFresh("exit", episode);
    assert.equal(result.success, true, result.reason);
    const outcomes = db.getEvents(0, 20, "pm_restart_continuation").map((row) => JSON.parse(row.payload).delivery).reverse();
    assert.ok(outcomes.length >= 2 && outcomes[0] === "refused_pre_effect" && outcomes.at(-1) === "delivered", outcomes.join(","));
    assert.deepEqual(w.pane.pasted, [PROMPT], "refused attempts wrote nothing; one paste");
    assert.deepEqual(w.pane.submitted, [PROMPT], "the human draft is never mixed into the continuation");
    assert.equal(db.getExitStatus().pending, false);
  } finally { await done(); }
});

// A MoP crash right after the delivered receipt, before the transition finished: the claim still reads "attempted".
async function interruptedAfterReceipt(opts: { withDevs?: boolean } = {}) {
  const { db, w, done } = fresh();
  const episode = arm(db, opts);
  const first = w.build();
  (first.health as any).finishPMContinuation = () => { throw new Error("MoP killed between the receipt and finishPMContinuation"); };
  await first.health.restartPMFresh("exit", episode).catch(() => undefined);
  return { db, w, episode, done };
}

test("service restart after the delivery receipt (PM-only): the receipt finishes the episode on the first tick, with no second send", async () => {
  const { db, w, episode, done } = await interruptedAfterReceipt();
  try {
    assert.equal(db.getExitStatus().continuation_delivery, "attempted", "interrupted: still attempted");
    assert.equal(deliveries(db).length, 1);
    assert.deepEqual(w.pane.submitted, [PROMPT]);
    // New process: new relay, new health checker, same database and pane.
    const second = w.build();
    await second.health.checkAll();
    await second.health.checkAll();
    const status = db.getExitStatus();
    assert.equal(status.episode, episode);
    assert.equal(status.pending, false);
    assert.equal(status.continuation_delivery, "delivered");
    assert.equal(status.dev_only, false, "PM-only: dev slots untouched");
    assert.deepEqual(w.pane.submitted, [PROMPT], "no second send");
    assert.equal(deliveries(db).length, 1);
    assert.equal(db.getEvents(0, 5, "pm_restart_continuation_settled").length, 1);
    assert.equal(db.getEvents(0, 5, "exit_pending_complete").length, 1);
  } finally { await done(); }
});

test("service restart after the delivery receipt (with_devs): the arm-time cohort and cycle flags survive; the episode waits for its devs", async () => {
  const { db, w, done } = await interruptedAfterReceipt({ withDevs: true });
  try {
    assert.equal(db.getExitStatus().continuation_delivery, "attempted");
    const cohort = [1, 2, 3, 4].map((slot) => db.getConfig("exit_arm_" + slot));
    const cycled = [1, 2, 3, 4].map((slot) => db.getConfig("exit_cycled_" + slot));
    assert.ok(cohort.every(Boolean) && cycled.every((flag) => flag === "false"));
    const second = w.build();
    await second.health.checkAll();
    const status = db.getExitStatus();
    assert.equal(status.pending, true, "devs of the same episode are not done");
    assert.equal(status.with_devs, true);
    assert.equal(status.pm_only, false);
    assert.equal(status.dev_only, true);
    assert.equal(status.continuation_delivery, "delivered");
    assert.deepEqual([1, 2, 3, 4].map((slot) => db.getConfig("exit_arm_" + slot)), cohort, "arm-time cohort preserved");
    assert.deepEqual([1, 2, 3, 4].map((slot) => db.getConfig("exit_cycled_" + slot)), cycled, "cycle flags preserved");
    assert.deepEqual(w.pane.submitted, [PROMPT], "no second send");
    assert.equal(db.getEvents(0, 5, "exit_pending_complete").length, 0);
  } finally { await done(); }
});

test("service restart after the delivery receipt (with_devs, transition interrupted half-way): resumes to the same end state; completes when the devs are done", async () => {
  const { db, w, done } = await interruptedAfterReceipt({ withDevs: true });
  try {
    // finishPMContinuation had already flipped pm_only/dev_only before the kill; the claim still reads "attempted".
    db.setConfig("exit_pending_pm_only", "false");
    db.setConfig("exit_pending_dev_only", "true");
    db.setConfig("exit_pending_stop_only", "true");
    assert.equal(db.getExitStatus().continuation_delivery, "attempted");
    const cohort = [1, 2, 3, 4].map((slot) => db.getConfig("exit_arm_" + slot));
    const second = w.build();
    await second.health.checkAll();
    let status = db.getExitStatus();
    assert.equal(status.pending, true);
    assert.equal(status.continuation_delivery, "delivered");
    assert.equal(status.with_devs, true);
    assert.deepEqual([1, 2, 3, 4].map((slot) => db.getConfig("exit_arm_" + slot)), cohort);
    assert.deepEqual(w.pane.submitted, [PROMPT]);
    // Every dev now has its own cycle verdict: the episode completes on the next tick.
    for (const slot of Object.keys(db.getExitStatus().cycled).map(Number).filter((slot) => slot > 0)) db.retainExitSlot(slot, "test_cohort_settled");
    await second.health.checkAll();
    status = db.getExitStatus();
    assert.equal(status.pending, false);
    assert.equal(db.getEvents(0, 5, "exit_pending_complete").length, 1);
  } finally { await done(); }
});

test("an attempted claim with NO delivered receipt stays consumed: uncertain is never resent", async () => {
  const { db, w, done } = fresh();
  try {
    const episode = arm(db);
    db.setConfig("exit_pending_continuation_delivery", "attempted");
    db.logEvent(0, "pm_restart_continuation", null, null, { episode, boot_event_id: 1, delivery: "uncertain" });
    // A crash restart's receipt carries episode null and must never settle an episode.
    db.logEvent(0, "pm_restart_continuation", null, null, { episode: null, trigger: "crash", delivery: "delivered" });
    w.pane.phase = "booted";
    const { health } = w.build();
    await health.reconcilePMRestartContinuation();
    assert.equal(db.getExitStatus().continuation_delivery, "attempted");
    assert.equal(db.getExitStatus().pending, true);
    assert.deepEqual(w.pane.pasted, []);
    assert.equal(db.getEvents(0, 5, "pm_restart_continuation_settled").length, 0);
  } finally { await done(); }
});
