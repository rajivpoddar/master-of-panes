import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import { MoPDatabase } from "../src/db.js";
import {
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "../src/assignmentAuthority.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const HEAD = "a".repeat(40);

function withDatabase(run: (db: MoPDatabase) => void): void {
  const directory = mkdtempSync(join(tmpdir(), "mop-rebind-release-test-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    run(db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

function tuple(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    repository_id: "github:repo-1",
    issue: 7400,
    pr: null,
    branch: "fix/7400",
    head_sha: null,
    work_kind: "rework",
    handoff_id: "handoff-7400",
    claimed_at: "2026-08-20T09:59:00Z",
    ...overrides,
  };
}

test("full tuple rebind increments once, reads back, and replays idempotently", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-rebind-route-test-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const app = new Hono();
  registerAssignmentRoute(app, db);
  try {
    assert.equal(
      db.assignSlot(1, "issue", "github:repo-1", 7400, "fix/7400", null, null, null, 0, false, null, "rework", "handoff-7400").ok,
      true,
    );
    const current = db.getSlot(1)!;
    const requestBody = {
      expected_epoch: current.assignment_epoch,
      expected_current_repository_id: current.repository_id,
      expected_current_issue: current.issue,
      expected_current_pr: current.pr,
      expected_current_branch: current.branch,
      expected_current_head_sha: current.head_sha,
      expected_current_work_kind: current.work_kind,
      expected_current_handoff_id: current.handoff_id,
      expected_current_claimed_at: current.claimed_at,
      repository_id: current.repository_id,
      issue: current.issue,
      pr: 7401,
      branch: "fix/7400-successor",
      head_sha: HEAD,
      work_kind: current.work_kind,
      handoff_id: current.handoff_id,
      claimed_at: current.claimed_at,
      task: "rebound",
    };
    const init = { method: "POST", headers: new Headers({
      "content-type": "application/json",
      [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY,
    }), body: JSON.stringify(requestBody) };

    const first = await app.request("/slots/1/adopt-issue-claim", init);
    assert.equal(first.status, 200);
    const row = await first.json() as Record<string, unknown>;
    assert.equal(row.assignment_epoch, 2);
    assert.equal(row.pr, 7401);
    assert.equal(row.branch_ref, "refs/heads/fix/7400-successor");
    assert.equal(row.work_kind, "rework");
    assert.equal(row.handoff_id, "handoff-7400");
    assert.equal(row.claimed_at, current.claimed_at);

    const replay = await app.request("/slots/1/adopt-issue-claim", init);
    assert.equal(replay.status, 200);
    assert.equal(db.getSlot(1)?.assignment_epoch, 2);
    const events = db.getEvents(1, 10, "slot_issue_claim_adopted");
    assert.equal(events.length, 2);
    assert.equal(JSON.parse(events[0].payload).idempotent, true);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("full tuple rebind refuses stale observation and another repository may reuse identities", () => {
  withDatabase((db) => {
    assert.equal(db.assignSlot(1, "issue", "github:repo-1", 7400, "fix/7400", null, null, null, 0, false, null, "rework", "handoff-7400").ok, true);
    const current = db.getSlot(1)!;
    const expected = tuple({ claimed_at: current.claimed_at });
    const desired = { ...expected, issue: 7400, pr: 7401, branch: "fix/7400-successor", head_sha: HEAD };
    const stale = db.rebindSlot(1, 0, expected, desired, "stale");
    assert.equal(stale.reason, "epoch_mismatch");
    assert.equal(db.getSlot(1)?.assignment_epoch, current.assignment_epoch);

    const wrongTuple = db.rebindSlot(
      1,
      current.assignment_epoch,
      { ...expected, branch: "fix/7400-wrong" },
      desired,
      "wrong tuple",
    );
    assert.equal(wrongTuple.reason, "observed_tuple_mismatch");
    assert.equal(db.getSlot(1)?.assignment_epoch, current.assignment_epoch);

    assert.equal(db.assignSlot(2, "other repo", "github:repo-2", 7400, "fix/7400-successor", null, 7401, HEAD, 0, false, null, "rework", "handoff-other").ok, true);
    const conflict = db.rebindSlot(
      1,
      current.assignment_epoch,
      expected,
      { ...desired, repository_id: "github:repo-1" },
      "conflict",
    );
    // Repository-scoped uniqueness allows the same identity in repo-2; the
    // rebind itself is still free to proceed in repo-1.
    assert.equal(conflict.ok, true);
  });
});

test("release clears the complete tuple at epoch plus one and lost-ack replay is idempotent", () => {
  withDatabase((db) => {
    assert.equal(db.assignSlot(1, "issue", "github:repo-1", 7400, "fix/7400", null, null, null, 0, false, null, "rework", "handoff-7400").ok, true);
    const current = db.getSlot(1)!;
    const expected = tuple({ claimed_at: current.claimed_at });
    const released = db.releaseSlot(1, current.assignment_epoch, expected);
    assert.deepEqual(released, {
      ok: true,
      conflict: false,
      assignment_epoch: current.assignment_epoch + 1,
      idempotent: false,
    });
    const free = db.getSlot(1)!;
    assert.equal(free.occupied, false);
    assert.equal(free.assignment_epoch, current.assignment_epoch + 1);
    for (const field of ["repository_id", "issue", "pr", "branch", "branch_ref", "head_sha", "work_kind", "handoff_id", "claimed_at"]) {
      assert.equal(free[field as keyof typeof free], null, `${field} must clear on release`);
    }

    const replay = db.releaseSlot(1, current.assignment_epoch, expected);
    assert.deepEqual(replay, {
      ok: true,
      conflict: false,
      assignment_epoch: current.assignment_epoch + 1,
      idempotent: true,
    });
    assert.equal(db.getSlot(1)?.assignment_epoch, current.assignment_epoch + 1);
  });
});
