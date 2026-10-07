import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase, slotAssignmentTuple, type AssignmentTupleInput } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";

// Rajiv C0ALZJHGE49/1791337506.322739: hourly report shows time on lane, so
// assigned_at must be set whenever the issue/PR lane changes and kept on re-pins.
function withDb(run: (db: MoPDatabase) => void): void {
  const dir = mkdtempSync(join(tmpdir(), "mop-lane-at-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(dir, "mop.db") });
  try { run(db); } finally { db.close(); rmSync(dir, { recursive: true, force: true }); }
}

const base: AssignmentTupleInput = {
  repository_id: "github:heydonna-app/heydonna-app",
  issue: 9013, pr: null, branch: null, head_sha: null,
  work_kind: null, handoff_id: null, claimed_at: null,
};

function rebind(db: MoPDatabase, changes: Partial<AssignmentTupleInput>) {
  const cur = db.getSlot(1)!;
  const tuple = slotAssignmentTuple(cur)!;
  return db.rebindSlot(1, cur.assignment_epoch, tuple, { ...tuple, ...changes }, cur.task);
}

test("assignSlotSimple on a free slot records assigned_at", () => {
  withDb((db) => {
    assert.equal(db.getSlot(1)!.assigned_at, null);
    assert.equal(db.assignSlotSimple(1, base, "issue 9013").ok, true);
    const at = db.getSlot(1)!.assigned_at;
    assert.ok(at && Number.isFinite(Date.parse(at)), `assigned_at=${at}`);
  });
});

test("same-lane re-pin keeps assigned_at; issue/PR change resets it", async () => {
  const dir = mkdtempSync(join(tmpdir(), "mop-lane-at-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(dir, "mop.db") });
  try {
    assert.equal(db.assignSlotSimple(1, base, "issue 9013").ok, true);
    const first = db.getSlot(1)!.assigned_at!;
    await new Promise((r) => setTimeout(r, 15));
    assert.equal(rebind(db, { branch: "fix/9013", head_sha: "a".repeat(40) }).ok, true);
    assert.equal(db.getSlot(1)!.assigned_at, first, "same issue/pr re-pin keeps lane start");
    await new Promise((r) => setTimeout(r, 15));
    assert.equal(rebind(db, { pr: 9100 }).ok, true);
    const second = db.getSlot(1)!.assigned_at!;
    assert.ok(Date.parse(second) > Date.parse(first), "PR change resets lane start");
    const epochBefore = db.getSlot(1)!.assignment_epoch;
    await new Promise((r) => setTimeout(r, 15));
    assert.equal(db.assignSlotSimple(1, { ...slotAssignmentTuple(db.getSlot(1)!)!, issue: 9200, pr: null, branch: null, head_sha: null }, "issue 9200").ok, true);
    assert.ok(Date.parse(db.getSlot(1)!.assigned_at!) > Date.parse(second), "issue change resets lane start");
    assert.ok(db.getSlot(1)!.assignment_epoch > epochBefore);
  } finally { db.close(); rmSync(dir, { recursive: true, force: true }); }
});
