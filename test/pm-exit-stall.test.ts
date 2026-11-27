import assert from "node:assert/strict";
import { mkdtempSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { ProcessHealthChecker } from "../src/health.js";
import { type DevExitAttempt } from "../src/devExitAttempt.js";
import { drainPmExitStop, exitHandoffRequestKey, pmExitStallReason } from "../src/pmHandoffExit.js";
import { pmRssArmOwner } from "../src/pmRssArm.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const database = (dbPath = ":memory:") => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });

test("a blocked PM-only exit claims a fresh handoff and one retry across MoP restart", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "mop-exit-retry-"));
  const path = join(dir, "mop.db");
  let db = database(path);
  t.after(() => { db.close(); rmSync(dir, { recursive: true, force: true }); });
  const notified: string[] = [];
  const tick = () => {
    const checker = new ProcessHealthChecker(db, { hasPendingExitConfirmation: () => false } as any);
    checker.onPmExitStall = (terminal) => { if (terminal.retry_episode) notified.push(terminal.episode); };
    (checker as any).reconcileStuckPmExit(Date.now());
  };
  db.setExitPending(true, false, true, "resume", true);
  const original = db.getExitStatus().episode!;
  const attempt: DevExitAttempt = { episode: original, paneId: "%9", originalClaudePid: 1, originalStart: "original",
    sessionId: "pm", inputEventId: null, assignmentEpoch: 0, stopEventId: 1, phase: "unknown", attemptCount: 1,
    originalProcess: "original", ownerPin: null, submittedAt: new Date().toISOString(), blockedReason: "replacement_not_owned_by_restart" };
  db.saveExitAttempt(0, attempt);
  tick();
  const status = db.getExitStatus();
  const retry = status.episode!;
  assert.notEqual(retry, original);
  assert.equal(status.pm_exit_stall?.episode, original);
  assert.equal(status.pm_exit_stall?.retry_episode, retry);
  assert.equal(status.handoff_episode, retry);
  assert.equal(status.pending && status.pm_only && !status.with_devs, true);
  assert.equal(status.continuation_prompt, "resume");
  assert.equal(status.attempts[0], null);
  db.close();
  db = database(path);
  db.saveExitAttempt(0, { ...attempt, episode: retry });
  tick();
  tick();
  assert.equal(db.getExitStatus().pending, false);
  assert.equal(db.getExitStatus().episode, retry);
  assert.equal(db.getExitStatus().pm_exit_stall?.retry_episode, null);
  assert.equal(pmRssArmOwner(db, { pendingMaintenance: () => null, boundaryHeld: () => false })?.kind, "exit_stalled_terminal");
  assert.deepEqual(notified, [original]);
});

test("before five minutes, or before an allowed Stop, no-attempt is not a stall", () => {
  const db = database();
  try {
    db.setExitPending(true, false, true);
    const episode = db.getExitStatus().episode!;
    const at = 1_800_000_000_000;
    assert.equal(pmExitStallReason(db, at + 300_000), null);
    db.recordPmExitStop(episode, at);
    db.recordPmExitStop(episode, at + 1000); // Later Stops cannot restart the timer.
    assert.equal(pmExitStallReason(db, at + 299_999), null);
    assert.equal(pmExitStallReason(db, at + 300_000), "no_pm_attempt_after_stop");
    db.setExitPending(true, false, true, undefined, true, true);
    db.recordPmExitStop(db.getExitStatus().episode!, at);
    assert.equal(pmExitStallReason(db, at + 300_000), null, "explicit dev cohort is not superseded");
  } finally { db.close(); }
});

test("PM Stop blocks only its open exit-handoff row until both notes are saved and resolves on HANDOFF_FRESH", async () => {
  const db = database();
  const dir = mkdtempSync(join(tmpdir(), "mop-exit-stop-"));
  try {
    db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm-session" });
    db.setExitPending(true, false, true, undefined, true);
    const { handoff_requested_at: requested, episode } = db.getExitStatus();
    const resolved: string[] = [];
    const ops = {
      lookup: (key: string) => key === exitHandoffRequestKey(requested!)
        ? { status: "found" as const, open: true, id: "44723", requestedAt: requested, sessionId: "pm-session" }
        : { status: "absent" as const },
      upsert: async () => { throw Error("must not write another obligation"); },
      resolveById: async (id: string) => { resolved.push(id); return true; },
    };
    const files = [join(dir, "handoff"), join(dir, "continuation")];
    const stop = () => drainPmExitStop(db, ops, "pm-session", async () => true, () => Date.now(), files);
    assert.deepEqual(stop(), { pending: true, block_obligation_id: "44723" });
    assert.equal(db.getConfig("pm_exit_allowed_stop"), null);
    const fresh = new Date(Date.parse(requested!) + 5_000);
    for (const [i, file] of files.entries()) {
      writeFileSync(file, "saved handoff");
      utimesSync(file, fresh, fresh);
      assert.equal(stop().block_obligation_id, i === 0 ? "44723" : null);
    }
    await Promise.resolve();
    assert.deepEqual(resolved, ["44723"]);
    assert.ok(db.getConfig("pm_exit_allowed_stop")?.startsWith(`${episode}:`));
  } finally { db.close(); rmSync(dir, { recursive: true, force: true }); }
});
