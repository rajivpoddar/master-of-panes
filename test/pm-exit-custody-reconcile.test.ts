// CTO C0ALZJHGE49 ts 1795795900.421179: a PM /exit whose replacement could not be bound
// (attempt unknown / replacement_not_owned_by_restart) held exit custody forever, so every
// PM Stop chose exit_custody and pending handoff clear 45610 never reached the /clear writer.
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import {
  PM_EXIT_CONSUMED_EPISODE_KEY,
  pmExitInCustody,
  reconcilePmExitReplacementAtStop,
  selectPmStopMaintenance,
} from "../src/pmHandoffExit.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const SESSION = "213444f3-9a67-4bc3-aaf6-53dc24cecce5";

function stuckEpisode(options: { bootAfterAttempt?: boolean; continuation?: string; delivery?: string } = {}) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(mkdtempSync(join(tmpdir(), "mop-exit-reconcile-")), "mop.db") });
  db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: SESSION, source: "startup" });
  db.setExitPending(true, false, true, options.continuation, true);
  const episode = db.getConfig("exit_pending_episode")!;
  db.setConfig(PM_EXIT_CONSUMED_EPISODE_KEY, episode);
  db.setConfig("exit_pending_continuation_delivery", options.delivery ?? "attempted");
  const stopEventId = db.logEvent(0, "Stop", "Stop", null, { session_id: SESSION });
  db.saveExitAttempt(0, {
    episode, paneId: "%1", originalClaudePid: 58216, originalStart: "Fri Nov 27 13:16:29 2026",
    originalProcess: JSON.stringify(["%1", 58216, "Fri Nov 27 13:16:29 2026"]), sessionId: SESSION,
    inputEventId: null, assignmentEpoch: 0, stopEventId, phase: "unknown", attemptCount: 1, ownerPin: null,
    submittedAt: new Date().toISOString(), blockedReason: "replacement_not_owned_by_restart",
  });
  // The replacement PM (claude --continue keeps the session id) boots after the attempt.
  if (options.bootAfterAttempt !== false) db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: SESSION, source: "resume" });
  return { db, episode };
}

const select = (db: MoPDatabase) => selectPmStopMaintenance(db as any, { ops: null, files: [] });

test("current-session Stop after the replacement boot releases stale exit custody once, so the clear path is selected", () => {
  const { db, episode } = stuckEpisode();
  assert.equal(select(db), "exit_custody");
  assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION }), true);
  assert.equal(pmExitInCustody(db), false);
  assert.equal(db.getExitStatus().pending, false);
  assert.equal(select(db), "clear");
  // No delivery receipt is invented for the uncertain continuation.
  assert.equal(db.getConfig("exit_pending_continuation_delivery"), "attempted");
  const event = JSON.parse(db.getEvents(0, 1, "pm_exit_custody_reconciled")[0]!.payload);
  assert.equal(event.episode, episode);
  assert.equal(event.previous.blockedReason, "replacement_not_owned_by_restart");
  // Exactly once: a second Stop has nothing to reconcile, and no clear request was created.
  assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION }), false);
  assert.equal(db.hasPendingClear(0), false);
});

test("no boot after the /exit attempt keeps exit custody", () => {
  const { db } = stuckEpisode({ bootAfterAttempt: false });
  assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION }), false);
  assert.equal(select(db), "exit_custody");
});

test("a Stop from another session, an open exit confirmation or an unsent continuation keeps exit custody", () => {
  const a = stuckEpisode();
  assert.equal(reconcilePmExitReplacementAtStop(a.db, { sessionId: "other-session" }), false);
  assert.equal(reconcilePmExitReplacementAtStop(a.db, { sessionId: SESSION, pendingConfirmation: () => true }), false);
  assert.equal(select(a.db), "exit_custody");
  const b = stuckEpisode({ continuation: "resume work", delivery: "waiting" });
  assert.equal(reconcilePmExitReplacementAtStop(b.db, { sessionId: SESSION }), false);
  assert.equal(select(b.db), "exit_custody");
});
