import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase, computeFamily2ReleaseDigest, slotAssignmentTuple } from "../src/db.js";
import { NativeSlotReleaseCoordinator } from "../src/slotRelease.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const TURN_ID = "b7bebc16-8dbd-48c9-941e-4ebf6d432875";
const REPO = "github:heydonna-app/heydonna-app";

interface Fixture {
  db: MoPDatabase;
  directory: string;
  slot: number;
}

/** The live slot-6 shape: idle=true while an old hook turn is still active. */
function orphanedTurnFixture(): Fixture {
  const directory = mkdtempSync(join(tmpdir(), "mop-s6-orphan-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const slot = 6;
  assert.equal(db.assignSlot(slot, "PM -> S6 lane", REPO, 7478, null, null, null, 0).ok, true);
  db.updateSlot(slot, {
    idle: true,
    activity: "branching",
    active_turn_id: TURN_ID,
    active_turn_started_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
    active_turn_state: "active",
    last_meaningful_work_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
  });
  return { db, directory, slot };
}

function close(value: Fixture): void {
  value.db.close();
  rmSync(value.directory, { recursive: true, force: true });
}

function releaseCoordinator(db: MoPDatabase, checkout: string) {
  return new NativeSlotReleaseCoordinator({
    db,
    resolveOwningCheckout: async () => checkout,
    interruptTurn: async () => ({ ok: true, reason: "interrupt_sent" }),
    observeCheckout: async () => ({
      checkout_path: checkout,
      clean: true,
      unpushed_commits: [],
      branch: "main",
      head: "b".repeat(40),
      error: null,
    }),
  });
}

function releaseRequest(value: Fixture) {
  const current = value.db.getSlot(value.slot)!;
  const tuple = slotAssignmentTuple(current)!;
  const request = {
    slot: value.slot,
    expected_epoch: current.assignment_epoch,
    expected_tuple: {
      repository_id: tuple.repository_id,
      issue: tuple.issue,
      pr: tuple.pr,
      branch: tuple.branch,
      head_sha: tuple.head_sha,
      work_kind: tuple.work_kind,
      handoff_id: tuple.handoff_id,
      claimed_at: tuple.claimed_at,
    },
    intended_main_head: "b".repeat(40),
    release_mode: "quiescent_legacy_issue_only" as const,
    effect_id: `s6-orphan-release-${value.slot}-${current.assignment_epoch}`,
    request_digest: "",
  };
  request.request_digest = computeFamily2ReleaseDigest(request);
  return request;
}

test("the canonical abandon-turn path clears the orphaned turn for the live slot-6 shape", () => {
  const value = orphanedTurnFixture();
  try {
    const before = value.db.getSlot(value.slot)!;
    assert.equal(before.active_turn_id, TURN_ID);
    assert.equal(before.active_turn_state, "active");
    assert.equal(before.idle, true, "the wedge shape: idle=true with an active turn record");

    const cleared = value.db.abandonTurn(value.slot, TURN_ID, "session relaunched after boot timeout", "cto");
    assert.equal(cleared.ok, true);
    const after = value.db.getSlot(value.slot)!;
    assert.equal(after.active_turn_id, null);
    assert.equal(after.active_turn_state, "inactive");
    assert.equal(after.assignment_epoch, before.assignment_epoch, "ownership/epoch untouched");
    assert.equal(after.issue, before.issue, "assignment identity untouched");
    assert.equal(value.db.getEvents(value.slot, 10, "agent_turn_abandoned").length, 1);

    // idempotency: a repeated recovery is a no-op readback, no double-kill
    const replay = value.db.abandonTurn(value.slot, TURN_ID, "retry", "cto");
    assert.equal(replay.ok, true);
    assert.equal(replay.idempotent, true);
    assert.equal(value.db.getEvents(value.slot, 10, "agent_turn_abandoned").length, 1);
  } finally {
    close(value);
  }
});

test("a genuinely active turn is never force-cleared by the recovery path", () => {
  const value = orphanedTurnFixture();
  try {
    // fresh progress => fail closed
    value.db.updateSlot(value.slot, { last_meaningful_work_at: new Date().toISOString() });
    const recent = value.db.abandonTurn(value.slot, TURN_ID, "too eager", "cto");
    assert.equal(recent.ok, false);
    assert.equal(recent.reason, "turn_recently_active");
    assert.equal(value.db.getSlot(value.slot)?.active_turn_id, TURN_ID);

    // a replacement turn id is refused outright
    value.db.updateSlot(value.slot, {
      last_meaningful_work_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
    });
    const mismatch = value.db.abandonTurn(value.slot, "some-other-turn", "wrong id", "cto");
    assert.equal(mismatch.ok, false);
    assert.equal(mismatch.reason, "turn_mismatch");
    assert.equal(value.db.getSlot(value.slot)?.active_turn_id, TURN_ID);
  } finally {
    close(value);
  }
});

test("release frees the slot despite the orphaned turn: interrupt, terminalize, audit", async () => {
  const value = orphanedTurnFixture();
  const checkout = "/tmp/mop-s6-orphan-checkout";
  try {
    const release = releaseCoordinator(value.db, checkout);
    // No abandon-turn round-trip: the orphaned turn no longer blocks release.
    // (The db-level abandon-turn path above stays intact for explicit use.)
    const freed = await release.release({ slot: value.slot });
    assert.equal(freed.success, true);
    assert.equal(freed.code, "released");
    const row = value.db.getSlot(value.slot)!;
    assert.equal(row.occupied, false);
    assert.equal(row.active_turn_id, null, "the orphaned turn id is terminalized");
    assert.equal(row.active_turn_state, "inactive");
    const audits = value.db.getEvents(value.slot, 10, "slot_released_simple");
    assert.equal(audits.length, 1);
    assert.match(audits[0].payload, new RegExp(TURN_ID), "the audit names the displaced turn");
  } finally {
    close(value);
  }
});
