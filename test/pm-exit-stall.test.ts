import assert from "node:assert/strict";
import { mkdtempSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { ProcessHealthChecker } from "../src/health.js";
import { type DevExitAttempt } from "../src/devExitAttempt.js";
import { HookProcessor } from "../src/hooks.js";
import { drainPmExitStop, exitHandoffRequestKey, PM_EXIT_CONSUMED_EPISODE_KEY, pmExitStallReason } from "../src/pmHandoffExit.js";
import { pmRssArmOwner } from "../src/pmRssArm.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const database = (dbPath = ":memory:") => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });

test("unknown exit retains the same episode and consumed custody (never a retry)", (t) => {
  const db = database();
  t.after(() => db.close());
  db.setExitPending(true, false, true, "resume", true);
  const episode = db.getExitStatus().episode!;
  db.setConfig(PM_EXIT_CONSUMED_EPISODE_KEY, episode);
  const attempt: DevExitAttempt = { episode, paneId: "%9", originalClaudePid: 1, originalStart: "original",
    sessionId: "pm", inputEventId: null, assignmentEpoch: 0, stopEventId: 1, phase: "unknown", attemptCount: 1,
    originalProcess: "original", ownerPin: null, submittedAt: new Date().toISOString(), blockedReason: null };
  const health = new ProcessHealthChecker(db, { hasPendingExitConfirmation: () => false } as any) as any;
  for (const reason of ["pane_unobservable", "same_original_process_no_exit_receipt", "replacement_not_owned_by_restart"]) {
    db.saveExitAttempt(0, { ...attempt, blockedReason: reason });
    health.reconcileStuckPmExit(Date.now() + 300_001);
    assert.equal(db.getExitStatus().episode, episode);
    assert.equal(db.getExitStatus().pm_exit_stall, null);
    assert.equal(db.getConfig(PM_EXIT_CONSUMED_EPISODE_KEY), episode);
  }
});

test("only a Stop selecting a saved exit with no dispatch starts the clock and can retry once", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "mop-exit-retry-"));
  const path = join(dir, "mop.db");
  let db = database(path);
  t.after(() => { db.close(); rmSync(dir, { recursive: true, force: true }); });
  const notified: string[] = [];
  const files = [join(dir, "handoff"), join(dir, "continuation")];
  const saved = () => {
    const fresh = new Date(Date.parse(db.getExitStatus().handoff_requested_at!) + 5000);
    for (const file of files) { writeFileSync(file, "saved"); utimesSync(file, fresh, fresh); }
  };
  const stop = () => {
    const processor = new HookProcessor(db, { captureExitOwner: () => null } as any);
    processor.pmExitHandoffFiles = files;
    return processor.process(0, { type: "Stop", session_id: "pm" });
  };
  const tick = (now: number) => {
    const checker = new ProcessHealthChecker(db, { hasPendingExitConfirmation: () => false } as any);
    checker.onPmExitStall = (terminal) => { if (terminal.retry_episode) notified.push(terminal.episode); };
    (checker as any).reconcileStuckPmExit(now);
  };
  db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
  db.setExitPending(true, false, true, "resume", true);
  const original = db.getExitStatus().episode!;
  db.setConfig("pm_exit_allowed_stop", `${original}:${Date.now() - 600_000}`); // old release's drain receipt is not authority
  await stop(); // handoff not saved
  saved();
  db.setPendingClear(0);
  await stop(); // clear wins
  assert.equal(pmExitStallReason(db, Date.now() + 300_001), null);
  db.clearPendingClear(0);
  await stop(); // owns selection and saved handoff, no relay call possible
  const at = Number(db.getConfig("pm_exit_selected_stop_no_effect")?.split(":")[1]);
  assert.ok(at > 0);
  assert.equal(pmExitStallReason(db, at + 299_999), null);
  assert.equal(pmExitStallReason(db, at + 300_000), "no_pm_attempt_after_stop");
  tick(at + 300_000);
  const retry = db.getExitStatus();
  assert.notEqual(retry.episode, original);
  assert.equal(retry.pm_exit_stall?.retry_episode, retry.episode);
  assert.equal(retry.handoff_episode, retry.episode);
  assert.equal(retry.pending && retry.pm_only && !retry.with_devs, true);
  assert.equal(retry.continuation_prompt, "resume");
  db.close();
  db = database(path);
  saved();
  await stop();
  tick(Date.now() + 300_001);
  tick(Date.now() + 600_001);
  assert.equal(db.getExitStatus().pending, false);
  assert.equal(db.getExitStatus().episode, retry.episode);
  assert.equal(db.getExitStatus().pm_exit_stall?.retry_episode, null);
  assert.equal(pmRssArmOwner(db, { pendingMaintenance: () => null, boundaryHeld: () => false })?.kind, "exit_stalled_terminal");
  assert.deepEqual(notified, [original]);
});

test("PM Stop blocks only its open exit-handoff row until both notes are saved and resolves on HANDOFF_FRESH", async () => {
  const db = database();
  const dir = mkdtempSync(join(tmpdir(), "mop-exit-stop-"));
  try {
    db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm-session" });
    db.setExitPending(true, false, true, undefined, true);
    const { handoff_requested_at: requested } = db.getExitStatus();
    const resolved: string[] = [];
    const ops = {
      lookup: (key: string) => key === exitHandoffRequestKey(requested!)
        ? { status: "found" as const, open: true, id: "44723", requestedAt: requested, sessionId: "pm-session" }
        : { status: "absent" as const },
      upsert: async () => { throw Error("must not write another obligation"); },
      resolveById: async (id: string) => { resolved.push(id); return true; },
    };
    const files = [join(dir, "handoff"), join(dir, "continuation")];
    const stop = () => drainPmExitStop(db, ops, "pm-session", async () => true, files);
    assert.deepEqual(stop(), { pending: true, block_obligation_id: "44723" });
    assert.equal(db.getConfig("pm_exit_selected_stop_no_effect"), null);
    const fresh = new Date(Date.parse(requested!) + 5_000);
    for (const [i, file] of files.entries()) {
      writeFileSync(file, "saved handoff");
      utimesSync(file, fresh, fresh);
      assert.equal(stop().block_obligation_id, i === 0 ? "44723" : null);
    }
    await Promise.resolve();
    assert.deepEqual(resolved, ["44723"]);
    assert.equal(db.getConfig("pm_exit_selected_stop_no_effect"), null, "an allowed drain is not an authoritative Stop");
  } finally { db.close(); rmSync(dir, { recursive: true, force: true }); }
});
