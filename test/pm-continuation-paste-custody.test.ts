// CTO REVISE C0ALZJHGE49/1794941233.245629, ts 1794950616.405579, on 43fcc72: a PM restart continuation whose Enter is
// withheld after the paste (PM inbound arrived between paste and Enter) must not leave its draft in the composer for the
// next queued message to ride along with. Real TmuxRelay + real ProcessHealthChecker.deliverPMRestartContinuation against a
// fake tmux pane that APPENDS pastes (so mixing is observable) and records the composer text at every Enter.
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

function pane() {
  let composer = "";
  let buffer = "";
  let ctrlU = 0;
  let onPaste: (() => void) | null = null;
  const pasted: string[] = [];
  const submitted: string[] = [];
  const runShell = async (command: string) => {
    if (command.includes("#{pane_id}|#{pane_current_command}")) return { stdout: "%1|claude\n", stderr: "" };
    if (command.includes("#{pane_current_command}")) return { stdout: "claude\n", stderr: "" };
    if (command.includes("#{pane_id}|#{pane_current_path}")) return { stdout: `%1|${CHECKOUT}\n`, stderr: "" };
    if (command.includes("rev-parse --show-toplevel")) return { stdout: `${CHECKOUT}\n`, stderr: "" };
    if (command.startsWith("tmux capture-pane")) {
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
    if (command.endsWith(" C-u")) { ctrlU++; composer = ""; }
    if (command.endsWith(" Enter")) { submitted.push(composer); composer = ""; }
    return { stdout: "", stderr: "" };
  };
  return {
    runShell,
    composer: () => composer, setComposer: (text: string) => { composer = text; },
    setOnPaste: (hook: (() => void) | null) => { onPaste = hook; },
    ctrlU: () => ctrlU, pasted: () => pasted, submitted: () => submitted,
  };
}

function fixture(p: ReturnType<typeof pane>) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude", pmPaneReady: async () => true, runShell: p.runShell as never });
  relay.setDatabase(db);
  db.setExitPending(true, false, true, "resume");
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

test("inbound between paste and Enter: zero continuation Enter, the exact owned draft is removed in the same lock, the queued message goes out alone", async () => {
  const p = pane();
  const f = fixture(p);
  let inbound: Promise<unknown> | undefined;
  p.setOnPaste(() => { inbound = f.relay.submitToPM("hello PM"); });
  try {
    await f.health.deliverPMRestartContinuation("%1", f.bootId, undefined, async () => null);
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
