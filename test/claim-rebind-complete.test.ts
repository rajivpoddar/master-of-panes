import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase, slotAssignmentTuple, type AssignmentTupleInput } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

function withDatabase(run: (db: MoPDatabase) => void): void {
  const directory = mkdtempSync(join(tmpdir(), "mop-claim-rebind-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  try {
    run(db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

const HEAD = "a".repeat(40);

function tupleFromSlot(db: MoPDatabase, slot: number): AssignmentTupleInput {
  const tuple = slotAssignmentTuple(db.getSlot(slot)!);
  assert.ok(tuple);
  return tuple;
}

test("claim creates a complete occupied tuple at the exact expected epoch", () => {
  withDatabase((db) => {
    const result = db.assignSlot(
      1, "claim", "github:repo-1", 10, "fix/10", "session-1", 20, HEAD, 0,
      "implementation", "handoff-1",
    );
    assert.deepEqual(result, {
      ok: true, conflict: false, assignment_epoch: 1, idempotent: false,
    });
    const slot = db.getSlot(1)!;
    assert.equal(slot.occupied, true);
    assert.equal(slot.repository_id, "github:repo-1");
    assert.equal(slot.issue, 10);
    assert.equal(slot.pr, 20);
    assert.equal(slot.session_id, "session-1");
    assert.equal(slot.work_kind, "implementation");
    assert.equal(slot.handoff_id, "handoff-1");
    assert.equal(slotAssignmentTuple(slot)?.head_sha, HEAD);
  });
});

test("rebind requires the complete expected tuple and exact epoch", () => {
  withDatabase((db) => {
    db.assignSlot(1, "claim", "github:repo-1", 10, "fix/10", "session-1", null, null, 0, "implementation", "handoff-1");
    const before = db.getSlot(1)!;
    const expected = tupleFromSlot(db, 1);
    const desired: AssignmentTupleInput = {
      ...expected,
      pr: 20,
      branch: "fix/10-bound",
      head_sha: HEAD,
    };

    const rebound = db.rebindSlot(1, before.assignment_epoch, expected, desired, "bound");
    assert.deepEqual(rebound, {
      ok: true, conflict: false, assignment_epoch: 2, idempotent: false,
    });
    assert.equal(db.getSlot(1)?.branch, "fix/10-bound");
    assert.equal(db.getSlot(1)?.assignment_epoch, 2);

    const drifted = db.rebindSlot(
      1,
      2,
      { ...expected, branch: "fix/other" },
      desired,
    );
    assert.equal(drifted.ok, false);
    assert.equal(drifted.reason, "observed_tuple_mismatch");
    assert.equal(db.getSlot(1)?.branch, "fix/10-bound");
  });
});

test("rebind refuses invalid complete tuple values without mutation", () => {
  withDatabase((db) => {
    db.assignSlot(1, "claim", "github:repo-1", 10, "fix/10", "session-1", null, null, 0, "implementation", "handoff-1");
    const expected = tupleFromSlot(db, 1);
    const before = db.getSlot(1);
    const invalidDesired = { ...expected, branch: "", work_kind: "not-a-kind" };
    const refused = db.rebindSlot(1, 1, expected, invalidDesired);
    assert.equal(refused.ok, false);
    assert.equal(refused.reason, "observed_tuple_mismatch");
    assert.deepEqual(db.getSlot(1), before);
  });
});
