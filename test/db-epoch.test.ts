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
