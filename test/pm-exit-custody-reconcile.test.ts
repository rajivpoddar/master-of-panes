// CTO C0ALZJHGE49 ts 1795795900.421179: a PM /exit whose replacement could not be bound
// (attempt unknown / replacement_not_owned_by_restart) held exit custody forever, so every
// PM Stop chose exit_custody and pending handoff clear 45610 never reached the /clear writer.
// CTO REVISE C0ALZJHGE49 ts 1795798363.328589: an UNKNOWN exit is never credited from a newer
// SessionStart/Stop alone; only a replacement bound to this exact attempt and its process reconciles.
// CTO REVISE 2 C0ALZJHGE49 thread 1795798240.220189, ts 1795800748.304029: "attempted" stays in custody until
// authoritative delivery or a terminal disposition, and the credit needs the relay's LIVE process plus its ownership
// checks (assignment epoch), never the receipt's own process value.
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import {
  PM_EXIT_CONSUMED_EPISODE_KEY,
  pmExitInCustody,
  pmExitReplacementCandidate,
  reconcilePmExitReplacementAtStop,
  selectPmStopMaintenance,
} from "../src/pmHandoffExit.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const SESSION = "213444f3-9a67-4bc3-aaf6-53dc24cecce5";

const REPLACEMENT = JSON.stringify(["%1", 61234, "Fri Nov 27 16:40:00 2026"]);

function stuckEpisode(options: {
  bootAfterAttempt?: boolean; continuation?: string; delivery?: string; deliveredReceipt?: boolean;
  attemptEpoch?: number; ownerPin?: unknown;
  receipt?: "bound" | "none" | ((r: Record<string, unknown>) => Record<string, unknown>);
} = {}) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(mkdtempSync(join(tmpdir(), "mop-exit-reconcile-")), "mop.db") });
  db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: SESSION, source: "startup" });
  db.setExitPending(true, false, true, options.continuation, true);
  const episode = db.getConfig("exit_pending_episode")!;
  db.setConfig(PM_EXIT_CONSUMED_EPISODE_KEY, episode);
  db.setConfig("exit_pending_continuation_delivery", options.delivery ?? "attempted");
  // Default: the continuation settled authoritatively (attempted, then this episode's delivered receipt).
  if (options.deliveredReceipt ?? true) {
    db.logEvent(0, "pm_restart_continuation", null, null, { episode, trigger: "exit", boot_event_id: 1, delivery: "delivered" });
  }
  const stopEventId = db.logEvent(0, "Stop", "Stop", null, { session_id: SESSION });
  db.saveExitAttempt(0, {
    episode, paneId: "%1", originalClaudePid: 58216, originalStart: "Fri Nov 27 13:16:29 2026",
    originalProcess: JSON.stringify(["%1", 58216, "Fri Nov 27 13:16:29 2026"]), sessionId: SESSION,
    inputEventId: null, assignmentEpoch: options.attemptEpoch ?? 0, stopEventId, phase: "unknown", attemptCount: 1,
    ownerPin: options.ownerPin === undefined ? { assignment: "pm", savedPmHandoff: true } : options.ownerPin,
    submittedAt: new Date().toISOString(), blockedReason: "replacement_not_owned_by_restart",
  });
  const attempt = db.getExitAttempt(0)!;
  // The owned restart logs its launch, then the replacement PM (claude --continue keeps the session id) boots.
  const launchEventId = db.logEvent(0, "process_dead", null, null, { trigger: "exit" });
  if (options.bootAfterAttempt === false) return { db, episode };
  const bootEventId = db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: SESSION, source: "resume" });
  const mode = options.receipt ?? "bound";
  if (mode !== "none") {
    const bound: Record<string, unknown> = {
      episode, submittedAt: attempt.submittedAt, assignmentEpoch: attempt.assignmentEpoch, launchEventId, bootEventId,
      bootSession: SESSION, ownedProcess: "61200:61234", replacementProcess: REPLACEMENT,
    };
    db.logEvent(0, "exit_replacement_bound", null, null, mode === "bound" ? bound : mode(bound));
  }
  return { db, episode };
}

const select = (db: MoPDatabase) => selectPmStopMaintenance(db as any, { ops: null, files: [] });

test("a current, live, receipt-bound replacement with a settled continuation releases stale exit custody exactly once", () => {
  const { db, episode } = stuckEpisode();
  assert.equal(select(db), "exit_custody");
  assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION, liveProcess: REPLACEMENT }), true);
  assert.equal(pmExitInCustody(db), false);
  assert.equal(db.getExitStatus().pending, false);
  assert.equal(select(db), "clear");
  // The settled marker is preserved as it was; no delivery receipt is invented.
  assert.equal(db.getConfig("exit_pending_continuation_delivery"), "attempted");
  assert.equal(db.getEvents(0, 5, "pm_restart_continuation").length, 1);
  const event = JSON.parse(db.getEvents(0, 1, "pm_exit_custody_reconciled")[0]!.payload);
  assert.equal(event.episode, episode);
  assert.equal(event.previous.blockedReason, "replacement_not_owned_by_restart");
  assert.equal(event.replacement_process, REPLACEMENT);
  assert.equal(event.receipt_event_id, db.getEvents(0, 1, "exit_replacement_bound")[0]!.id);
  // Exactly once: a second Stop has nothing to reconcile, and no clear request was created.
  assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION, liveProcess: REPLACEMENT }), false);
  assert.equal(db.hasPendingClear(0), false);
});

test("no boot after the /exit attempt keeps exit custody", () => {
  const { db } = stuckEpisode({ bootAfterAttempt: false });
  assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION, liveProcess: REPLACEMENT }), false);
  assert.equal(select(db), "exit_custody");
});

test("a Stop from another session, an open exit confirmation or an unsent continuation keeps exit custody", () => {
  const a = stuckEpisode();
  assert.equal(reconcilePmExitReplacementAtStop(a.db, { sessionId: "other-session", liveProcess: REPLACEMENT }), false);
  assert.equal(reconcilePmExitReplacementAtStop(a.db, { sessionId: SESSION, pendingConfirmation: () => true, liveProcess: REPLACEMENT }), false);
  assert.equal(select(a.db), "exit_custody");
  const b = stuckEpisode({ continuation: "resume work", delivery: "waiting" });
  assert.equal(reconcilePmExitReplacementAtStop(b.db, { sessionId: SESSION, liveProcess: REPLACEMENT }), false);
  assert.equal(select(b.db), "exit_custody");
});

test("a newer UNBOUND boot keeps the exit UNKNOWN and in custody", () => {
  // The 16:49Z shape: a newer same-session SessionStart + Stop, but no restart receipt binds it to the attempt.
  const unbound = stuckEpisode({ receipt: "none" });
  assert.equal(reconcilePmExitReplacementAtStop(unbound.db, { sessionId: SESSION, liveProcess: REPLACEMENT }), false);
  assert.equal(select(unbound.db), "exit_custody");
  assert.equal(unbound.db.getExitAttempt(0)!.phase, "unknown");
  assert.equal(unbound.db.getEvents(0, 1, "pm_exit_custody_reconciled").length, 0);
});

test("a receipt that does not bind this exact attempt, boot or process keeps exit custody", () => {
  const mutations: Array<(r: Record<string, unknown>) => Record<string, unknown>> = [
    (r) => ({ ...r, episode: "other-episode" }),
    (r) => ({ ...r, submittedAt: "2026-01-01T00:00:00.000Z" }),
    (r) => ({ ...r, launchEventId: 1 }), // launched before this attempt's Stop
    (r) => ({ ...r, bootEventId: (r.bootEventId as number) + 99 }),
    (r) => ({ ...r, bootSession: "other-session" }),
    (r) => ({ ...r, ownedProcess: "61200:99999" }),
    (r) => ({ ...r, replacementProcess: JSON.stringify(["%1", 58216, "Fri Nov 27 13:16:29 2026"]) }), // the original
  ];
  for (const mutate of mutations) {
    const { db } = stuckEpisode({ receipt: mutate });
    assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION, liveProcess: REPLACEMENT }), false, String(mutate));
    assert.equal(select(db), "exit_custody");
  }
});

test("an \"attempted\" continuation still in flight (no delivered receipt) keeps exit custody and pending untouched", () => {
  const { db, episode } = stuckEpisode({ deliveredReceipt: false });
  assert.equal(pmExitReplacementCandidate(db, { sessionId: SESSION }), null);
  assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION, liveProcess: REPLACEMENT }), false);
  // The transaction itself also refuses, so the writer's exit_pending_changed fence can never trip on a release.
  assert.equal(db.reconcilePmExitReplacement(episode, "direct", {}), false);
  assert.equal(db.getExitStatus().pending, true);
  assert.equal(db.getConfig("exit_pending_continuation_delivery"), "attempted");
  assert.equal(select(db), "exit_custody");
  assert.equal(db.getEvents(0, 1, "pm_exit_custody_reconciled").length, 0);
});

test("a waiting continuation, with or without a prompt, keeps exit custody (the health resume path still sends it)", () => {
  for (const continuation of ["resume work", undefined]) {
    const { db } = stuckEpisode({ continuation, delivery: "waiting", deliveredReceipt: false });
    assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION, liveProcess: REPLACEMENT }), false);
    assert.equal(select(db), "exit_custody");
  }
});

test("a bound receipt whose owner moved (assignment epoch or pin) keeps exit custody", () => {
  // The attempt (and its receipt) carry epoch 3 while slot 0 now reads epoch 0: the owner moved.
  const moved = stuckEpisode({ attemptEpoch: 3 });
  assert.equal(pmExitReplacementCandidate(moved.db, { sessionId: SESSION }), null);
  assert.equal(reconcilePmExitReplacementAtStop(moved.db, { sessionId: SESSION, liveProcess: REPLACEMENT }), false);
  assert.equal(moved.db.reconcilePmExitReplacement(moved.episode, "direct", {}), false);
  assert.equal(select(moved.db), "exit_custody");
  const unpinned = stuckEpisode({ ownerPin: null });
  assert.equal(reconcilePmExitReplacementAtStop(unpinned.db, { sessionId: SESSION, liveProcess: REPLACEMENT }), false);
  assert.equal(select(unpinned.db), "exit_custody");
});

test("the expected process is the LIVE one: no live process, the original, or a live process the receipt does not name keeps custody", () => {
  for (const liveProcess of [null, "", JSON.stringify(["%1", 58216, "Fri Nov 27 13:16:29 2026"]),
    JSON.stringify(["%1", 70000, "Fri Nov 27 17:00:00 2026"])]) {
    const { db } = stuckEpisode();
    assert.ok(pmExitReplacementCandidate(db, { sessionId: SESSION }), "the sync prefilter alone never credits");
    assert.equal(reconcilePmExitReplacementAtStop(db, { sessionId: SESSION, liveProcess }), false, String(liveProcess));
    assert.equal(select(db), "exit_custody");
    assert.equal(db.getExitAttempt(0)!.phase, "unknown");
  }
});
