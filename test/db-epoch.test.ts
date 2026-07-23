import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function withDatabase(run: (db: MoPDatabase) => void): void {
  const directory = mkdtempSync(join(tmpdir(), "mop-epoch-test-"));
  try {
    run(new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") }));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("assignment epochs increment only for new tuples", () => {
  withDatabase((db) => {
    assert.equal(db.getSlot(1)?.assignment_epoch, 0);
    const first = db.assignSlot(1, "issue", 10, "fix/10", null, null, null, 0);
    assert.deepEqual(first, { ok: true, conflict: false, assignment_epoch: 1, idempotent: false });

    const redelivery = db.assignSlot(1, "renamed task", 10, "fix/10", null, null, null, 1);
    assert.deepEqual(redelivery, { ok: true, conflict: false, assignment_epoch: 1, idempotent: true });

    const next = db.assignSlot(1, "next", 11, "fix/11", null, null, null, 1);
    assert.deepEqual(next, { ok: true, conflict: false, assignment_epoch: 2, idempotent: false });
  });
});

test("missing and stale expected epochs fail without mutation", () => {
  withDatabase((db) => {
    const missing = db.assignSlot(1, "issue", 10, "fix/10", null);
    assert.equal(missing.reason, "expected_epoch_required");
    assert.equal(db.getSlot(1)?.occupied, false);

    db.assignSlot(1, "issue", 10, "fix/10", null, null, null, 0);
    const stale = db.releaseSlot(1, 0);
    assert.equal(stale.reason, "epoch_mismatch");
    assert.equal(db.getSlot(1)?.occupied, true);
  });
});

test("release preserves epoch and hook turn state fails closed on mismatch", () => {
  withDatabase((db) => {
    db.assignSlot(1, "issue", 10, "fix/10", null, null, null, 0);
    db.startAgentTurn(1, "turn-a");
    assert.equal(db.getSlot(1)?.active_turn_state, "active");
    db.finishAgentTurn(1, "turn-b");
    assert.equal(db.getSlot(1)?.active_turn_state, "indeterminate");
    db.finishAgentTurn(1, "turn-a");
    assert.equal(db.getSlot(1)?.active_turn_state, "inactive");

    const released = db.releaseSlot(1, 1);
    assert.equal(released.assignment_epoch, 1);
    assert.equal(db.getSlot(1)?.occupied, false);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
  });
});

test("assignment rejects a target already owned by another occupied slot", () => {
  withDatabase((db) => {
    const first = db.assignSlot(4, "original", 6735, "fix/6735-pending", null, 6737, "old-head", 0);
    assert.equal(first.ok, true);

    const duplicate = db.assignSlot(2, "failover", 6735, "fix/6735-pending", null, 6737, "new-head", 0);
    assert.deepEqual(duplicate, {
      ok: false,
      conflict: true,
      assignment_epoch: 0,
      idempotent: false,
      reason: "target_already_assigned",
      owner_slots: [4],
    });
    assert.equal(db.getSlot(2)?.occupied, false);
    assert.equal(db.getSlot(2)?.assignment_epoch, 0);
  });
});

test("assignment rejects issue-only and branch-only duplicate ownership", () => {
  withDatabase((db) => {
    db.assignSlot(4, "original", 6735, "fix/6735-pending", null, null, null, 0);

    const sameIssue = db.assignSlot(2, "same issue", 6735, "different-branch", null, null, null, 0);
    assert.equal(sameIssue.reason, "target_already_assigned");
    assert.deepEqual(sameIssue.owner_slots, [4]);

    const sameBranch = db.assignSlot(3, "same branch", 9999, "fix/6735-pending", null, null, null, 0);
    assert.equal(sameBranch.reason, "target_already_assigned");
    assert.deepEqual(sameBranch.owner_slots, [4]);
  });
});

test("released ownership can be explicitly reassigned", () => {
  withDatabase((db) => {
    db.assignSlot(4, "original", 6735, "fix/6735-pending", null, 6737, "old-head", 0);
    const released = db.releaseSlot(4, 1);
    assert.equal(released.ok, true);

    const reassigned = db.assignSlot(2, "replacement", 6735, "fix/6735-pending", null, 6737, "new-head", 0);
    assert.deepEqual(reassigned, {
      ok: true,
      conflict: false,
      assignment_epoch: 1,
      idempotent: false,
    });
  });
});
