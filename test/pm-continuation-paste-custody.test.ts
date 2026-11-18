// CTO REVISE C0ALZJHGE49/1794941233.245629, ts 1794950616.405579, on 43fcc72: a PM restart continuation whose Enter is
// withheld after the paste (PM inbound arrived between paste and Enter) must not leave its draft in the composer for the
// next queued message to ride along with. Real TmuxRelay + real ProcessHealthChecker.deliverPMRestartContinuation against a
// fake tmux pane that APPENDS pastes (so mixing is observable) and records the composer text at every Enter.
// Round 3 (CTO REVISE ts 1794951624.218829): the ownership probe before EVERY Ctrl-U is followed by a fresh composer read
// that must equal the last verified owned text (multiline human-interference case), and an unresolved Enter (lost key or
// lost acknowledgement) holds automated paste alone: no Ctrl-U, the attempt stays consumed.
// Fixtures only: not executed (Rajiv DM D0AMF0XE6TS thread 1793979986.975849, ts 1793983042.752819: MoP changes need no
// local testing or proof).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { ProcessHealthChecker } from "../src/health.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const CHECKOUT = "/Users/rajiv/Downloads/projects/heydonna-app";
const HOLD = "pm_exit_cleanup_pending";

// `lineCtrlU`: Ctrl-U deletes only the cursor's (last) input line, as in the real composer, so a multiline draft needs one
// press per line and leaves a multiline remainder between presses. `trace` orders every capture, probe and key.
function pane(opts: { lineCtrlU?: boolean } = {}) {
  let composer = "";
  let buffer = "";
  let ctrlU = 0;
  let onPaste: (() => void) | null = null;
  let failEnter: "lost_key" | "lost_ack" | null = null;
  const pasted: string[] = [];
  const submitted: string[] = [];
  const trace: string[] = [];
  const runShell = async (command: string) => {
    if (command.includes("#{pane_id}|#{pane_current_command}")) return { stdout: "%1|claude\n", stderr: "" };
    if (command.includes("#{pane_current_command}")) return { stdout: "claude\n", stderr: "" };
    if (command.includes("#{pane_id}|#{pane_current_path}")) return { stdout: `%1|${CHECKOUT}\n`, stderr: "" };
    if (command.includes("rev-parse --show-toplevel")) return { stdout: `${CHECKOUT}\n`, stderr: "" };
    if (command.startsWith("tmux capture-pane")) {
      trace.push("capture");
      return { stdout: `${"─".repeat(40)}\n❯ ${composer}\n${"─".repeat(40)}`, stderr: "" };
    }
    const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(command);
    if (load) buffer = readFileSync(load[1]!, "utf8");
    if (command.startsWith("tmux paste-buffer")) {
      composer += buffer; // a paste lands after whatever is already in the composer
      pasted.push(buffer);
      const hook = onPaste;
      onPaste = null; // one-shot: only the continuation's own paste triggers the scenario
      hook?.();
    }
    if (command.endsWith(" C-u")) {
      ctrlU++;
      trace.push("C-u");
      const cut = composer.lastIndexOf("\n");
      composer = opts.lineCtrlU && cut >= 0 ? composer.slice(0, cut) : "";
    }
    if (command.endsWith(" Enter")) {
      const fail = failEnter;
      failEnter = null; // one-shot: only the continuation's Enter fails
      if (fail === "lost_key") throw new Error("tmux send-keys timed out before the key reached the pane");
      submitted.push(composer); composer = "";
      if (fail === "lost_ack") throw new Error("tmux acknowledgement lost after the key landed");
    }
    return { stdout: "", stderr: "" };
  };
  return {
    runShell,
    composer: () => composer, setComposer: (text: string) => { composer = text; },
    setOnPaste: (hook: (() => void) | null) => { onPaste = hook; },
    failNextEnter: (mode: "lost_key" | "lost_ack") => { failEnter = mode; },
    ctrlU: () => ctrlU, pasted: () => pasted, submitted: () => submitted, trace: () => trace,
  };
}

// A one-shot Enter withhold that leaves draft ownership intact: the first process probe after the paste (the pre-Enter
// recheck) refuses, every later probe (the draft-ownership proof) passes. It replaces the former trigger, PM inbound
// between paste and Enter, which no longer withholds a continuation (Rajiv C0ALZJHGE49 thread 1794970059.617269, ts
// 1794971912.636829: no gate). The inbound is still submitted in the hook so the "queued message goes out alone" checks hold.
function withholdEnterOnce(p: ReturnType<typeof pane>, then: () => Promise<string | null> = async () => null) {
  let done = false;
  return async () => {
    if (p.pasted().length > 0 && !done) { done = true; return "pm_process_changed"; }
    return then();
  };
}

function fixture(p: ReturnType<typeof pane>, prompt = "resume") {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude", pmPaneReady: async () => true, runShell: p.runShell as never });
  relay.setDatabase(db);
  db.setExitPending(true, false, true, prompt);
  // A flag alone is never proof of replacement: cycled[0] needs a VERIFIED exit attempt of the current episode.
  db.saveExitAttempt(0, {
    episode: db.getExitStatus().episode!, paneId: "%1", originalClaudePid: 4000, originalStart: "Wed Nov 18 00:29:17 2026",
    originalProcess: '["%1",4000,"Wed Nov 18 00:29:17 2026"]', sessionId: "old", inputEventId: null, assignmentEpoch: 0,
    stopEventId: null, ownerPin: null, phase: "verified", attemptCount: 1, submittedAt: new Date().toISOString(),
    blockedReason: null, replacementProcess: '["%1",4001,"Wed Nov 18 08:18:49 2026"]',
  });
  db.markSlotExitCycled(0);
  db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "old", source: "startup" });
  const bootId = db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "replacement", source: "startup" });
  const health = new ProcessHealthChecker(db, relay as never) as any;
  return { db, relay, bootId, health, close: () => db.close() };
}

test("no inbound: the continuation is pasted and submitted once and no draft cleanup runs", async () => {
  const p = pane();
  const f = fixture(p);
  try {
    await f.health.deliverPMRestartContinuation("%1", f.bootId, undefined, async () => null);
    assert.deepEqual(p.submitted(), ["resume"]);
    assert.equal(f.db.getExitStatus().continuation_delivery, "delivered");
    assert.equal(f.db.getEvents(0, 5, "pm_restart_continuation_draft").length, 0);
    assert.equal(f.db.getConfig(HOLD) || "", "");
  } finally { f.close(); }
});

test("Enter withheld after the paste (ownership intact): zero continuation Enter, the exact owned draft is removed in the same lock, the queued message goes out alone", async () => {
  const p = pane();
  const f = fixture(p);
  let inbound: Promise<unknown> | undefined;
  p.setOnPaste(() => { inbound = f.relay.submitToPM("hello PM"); });
  try {
    await f.health.deliverPMRestartContinuation("%1", f.bootId, undefined, withholdEnterOnce(p));
    await inbound;
    assert.equal(p.pasted()[0], "resume");
    assert.equal(p.submitted().length, 1, "exactly one Enter, and it is the inbound's, never the continuation's");
    assert.ok(p.submitted()[0]!.includes("hello PM"));
    assert.ok(!p.submitted()[0]!.includes("resume"), "no stale continuation text mixed into the queued message");
    assert.equal(p.ctrlU(), 1);
    assert.equal(f.db.getConfig(HOLD) || "", "", "the hold is released once the draft is proven removed");
    // The consumed attempt stays consumed: no resend, devs not armed.
    assert.equal(f.db.getExitStatus().continuation_delivery, "attempted");
    assert.equal(f.db.getExitStatus().pm_only, true);
    assert.equal(JSON.parse(f.db.getEvents(0, 5, "pm_restart_continuation")[0]!.payload).delivery, "uncertain");
    const draft = JSON.parse(f.db.getEvents(0, 5, "pm_restart_continuation_draft")[0]!.payload);
    assert.equal(draft.removed, true);
    assert.equal(draft.held, false);
  } finally { f.close(); }
});

test("uncertain ownership retains the draft and holds automated paste; the queued message waits, then goes out alone once reconciled", async () => {
  const p = pane();
  const f = fixture(p);
  let owned = true;
  let inbound: Promise<unknown> | undefined;
  p.setOnPaste(() => { owned = false; inbound = f.relay.submitToPM("hello PM"); });
  try {
    // The owned PM process stops proving after the paste: cleanup cannot prove the draft is ours.
    await f.health.deliverPMRestartContinuation("%1", f.bootId, undefined, async () => owned ? null : "pm_process_changed");
    await inbound;
    assert.equal(p.composer(), "resume", "the draft is retained, never guessed away");
    assert.equal(p.ctrlU(), 0);
    assert.deepEqual(p.submitted(), [], "zero continuation Enter");
    assert.deepEqual(p.pasted(), ["resume"], "the queued message was not pasted into the draft");
    assert.ok(f.db.getConfig(HOLD), "automated paste is held");
    assert.equal(f.db.peekPendingPMEvents().length, 1, "the held message stays queued, never dropped");
    assert.equal(f.db.getExitStatus().continuation_delivery, "attempted", "the consumed attempt is preserved");
    assert.equal(JSON.parse(f.db.getEvents(0, 5, "pm_restart_continuation_draft")[0]!.payload).reason, "ownership_unproven");

    p.setComposer(""); // reconciled (the operator cleared or submitted the draft)
    await f.relay.drainPMQueue();
    assert.equal(p.submitted().length, 1);
    assert.ok(p.submitted()[0]!.includes("hello PM"));
    assert.ok(!p.submitted()[0]!.includes("resume"));
    assert.equal(f.db.getConfig(HOLD) || "", "");
    assert.equal(f.db.peekPendingPMEvents().length, 0);
  } finally { f.close(); }
});

test("human input typed after the paste is preserved: the draft is not exact, so nothing is removed and automated paste is held", async () => {
  const p = pane();
  const f = fixture(p);
  let inbound: Promise<unknown> | undefined;
  p.setOnPaste(() => {
    p.setComposer(`${p.composer()} but also my own words`);
    inbound = f.relay.submitToPM("hello PM");
  });
  try {
    await f.health.deliverPMRestartContinuation("%1", f.bootId, undefined, async () => null);
    await inbound;
    assert.equal(p.composer(), "resume but also my own words");
    assert.equal(p.ctrlU(), 0, "no clearing key against text we do not exclusively own");
    assert.deepEqual(p.submitted(), []);
    assert.deepEqual(p.pasted(), ["resume"]);
    assert.ok(f.db.getConfig(HOLD));
    assert.equal(f.db.peekPendingPMEvents().length, 1);
    assert.equal(JSON.parse(f.db.getEvents(0, 5, "pm_restart_continuation_draft")[0]!.payload).reason, "composer_not_exact_draft");
  } finally { f.close(); }
});

const DRAFT = "line one\nline two\nline three";

test("multiline draft, no interference: a fresh composer read follows the ownership probe immediately before EVERY Ctrl-U, then the draft is gone and the queued message goes out alone", async () => {
  const p = pane({ lineCtrlU: true });
  const f = fixture(p, DRAFT);
  let inbound: Promise<unknown> | undefined;
  p.setOnPaste(() => { inbound = f.relay.submitToPM("hello PM"); });
  const guard = withholdEnterOnce(p, async () => { p.trace().push("probe"); return null; });
  try {
    await f.health.deliverPMRestartContinuation("%1", f.bootId, undefined, guard);
    await inbound;
    assert.equal(p.pasted()[0], DRAFT);
    assert.equal(p.ctrlU(), 3, "one key per input line");
    const trace = p.trace();
    trace.forEach((step, i) => {
      if (step !== "C-u") return;
      assert.equal(trace[i - 1], "capture", "the composer is re-read right before the key");
      assert.equal(trace[i - 2], "probe", "and that read follows the ownership probe, for every key not only the first");
    });
    assert.equal(p.submitted().length, 1);
    assert.ok(p.submitted()[0]!.includes("hello PM"));
    assert.ok(!p.submitted()[0]!.includes("line one"), "no stale continuation text mixed into the queued message");
    assert.equal(f.db.getConfig(HOLD) || "", "");
    const draft = JSON.parse(f.db.getEvents(0, 5, "pm_restart_continuation_draft")[0]!.payload);
    assert.equal(draft.removed, true);
    assert.equal(f.db.getExitStatus().continuation_delivery, "attempted", "the consumed attempt is preserved");
  } finally { f.close(); }
});

test("multiline remainder, human text typed while the NEXT ownership probe is in flight: no further Ctrl-U, the human text survives, automated paste stays held until reconciled", async () => {
  const p = pane({ lineCtrlU: true });
  const f = fixture(p, DRAFT);
  let inbound: Promise<unknown> | undefined;
  p.setOnPaste(() => { inbound = f.relay.submitToPM("hello PM"); });
  let typed = false;
  // The first key goes out cleanly (leaving a two-line remainder); during the probe that precedes the SECOND key a
  // person types a new line. A recheck only before the first key would not see it and the next Ctrl-U would erase it.
  const guard = withholdEnterOnce(p, async () => {
    if (p.ctrlU() === 1 && !typed) { typed = true; p.setComposer(`${p.composer()}\nmy own words`); }
    return null;
  });
  try {
    await f.health.deliverPMRestartContinuation("%1", f.bootId, undefined, guard);
    await inbound;
    assert.ok(typed, "the human typed during the second ownership probe");
    assert.equal(p.ctrlU(), 1, "no clearing key after the composer changed under the probe");
    assert.equal(p.composer(), "line one\nline two\nmy own words", "human text is preserved");
    assert.deepEqual(p.submitted(), [], "zero continuation Enter and no queued message mixed with the draft");
    assert.deepEqual(p.pasted(), [DRAFT], "the queued message was not pasted while the composer is unreconciled");
    assert.ok(f.db.getConfig(HOLD), "the hold is retained");
    assert.equal(f.db.peekPendingPMEvents().length, 1, "the held message stays queued, never dropped");
    assert.equal(f.db.getExitStatus().continuation_delivery, "attempted", "the consumed attempt is preserved");
    const draft = JSON.parse(f.db.getEvents(0, 5, "pm_restart_continuation_draft")[0]!.payload);
    assert.equal(draft.removed, false);
    assert.equal(draft.held, true);
    assert.equal(draft.reason, "draft_changed");

    p.setComposer(""); // reconciled by the person
    await f.relay.drainPMQueue();
    assert.equal(p.submitted().length, 1);
    assert.ok(p.submitted()[0]!.includes("hello PM"));
    assert.ok(!p.submitted()[0]!.includes("my own words") && !p.submitted()[0]!.includes("line one"));
    assert.equal(f.db.getConfig(HOLD) || "", "");
    assert.equal(f.db.peekPendingPMEvents().length, 0);
  } finally { f.close(); }
});

test("unresolved Enter, the key never reached the pane: hold only, no Ctrl-U, the attempt stays consumed, the next inbound waits and then goes out alone", async () => {
  const p = pane();
  const f = fixture(p);
  p.failNextEnter("lost_key");
  try {
    await f.health.deliverPMRestartContinuation("%1", f.bootId, undefined, async () => null);
    assert.equal(p.composer(), "resume", "the draft is still in the composer");
    assert.equal(p.ctrlU(), 0, "no clearing key when the Enter outcome is unresolved");
    assert.deepEqual(p.submitted(), []);
    assert.ok(f.db.getConfig(HOLD), "the hold alone protects the draft");
    assert.equal(f.db.getExitStatus().continuation_delivery, "attempted", "the consumed attempt is preserved, never retried");
    assert.equal(JSON.parse(f.db.getEvents(0, 5, "pm_restart_continuation")[0]!.payload).delivery, "uncertain");
    const draft = JSON.parse(f.db.getEvents(0, 5, "pm_restart_continuation_draft")[0]!.payload);
    assert.equal(draft.reason, "enter_unresolved_hold_only");
    assert.equal(draft.held, true);

    await f.relay.submitToPM("hello PM");
    assert.deepEqual(p.pasted(), ["resume"], "the inbound was not pasted into the unresolved draft");
    assert.deepEqual(p.submitted(), []);
    assert.equal(f.db.peekPendingPMEvents().length, 1, "the message stays queued");
    assert.equal(p.ctrlU(), 0);

    p.setComposer(""); // the composer is observed empty (reconciled)
    await f.relay.drainPMQueue();
    assert.equal(p.submitted().length, 1);
    assert.ok(p.submitted()[0]!.includes("hello PM"));
    assert.ok(!p.submitted()[0]!.includes("resume"));
    assert.equal(f.db.getConfig(HOLD) || "", "");
    assert.equal(f.db.peekPendingPMEvents().length, 0);
  } finally { f.close(); }
});

test("unresolved Enter, the key landed but its acknowledgement was lost: no Ctrl-U, no resend, the hold clears on the next observed-empty composer and the inbound is not blocked", async () => {
  const p = pane();
  const f = fixture(p);
  p.failNextEnter("lost_ack");
  try {
    await f.health.deliverPMRestartContinuation("%1", f.bootId, undefined, async () => null);
    assert.deepEqual(p.submitted(), ["resume"], "the continuation reached the pane exactly once");
    assert.equal(p.ctrlU(), 0);
    assert.ok(f.db.getConfig(HOLD));
    assert.equal(f.db.getExitStatus().continuation_delivery, "attempted", "no resend");

    await f.relay.submitToPM("hello PM"); // composer is empty: the hold is released and the message goes out alone
    assert.equal(p.submitted().length, 2);
    assert.ok(p.submitted()[1]!.includes("hello PM"));
    assert.ok(!p.submitted()[1]!.includes("resume"));
    assert.equal(p.ctrlU(), 0);
    assert.equal(f.db.getConfig(HOLD) || "", "");
    assert.equal(f.db.peekPendingPMEvents().length, 0);
  } finally { f.close(); }
});
