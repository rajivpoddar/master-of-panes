import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import {
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "../src/assignmentAuthority.js";
import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import { MoPDatabase } from "../src/db.js";
import type { IssueOwnershipProjection, IssueProjectionOutcome } from "../src/issueProjection.js";
import type { MoPConfig } from "../src/types.js";

const REPOSITORY = "992731533";

interface Harness {
  db: MoPDatabase;
  directory: string;
  calls: string[];
  app: Hono;
}

function harness(): Harness {
  const directory = mkdtempSync(join(tmpdir(), "mop-assign-occupied-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 6,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const calls: string[] = [];
  const projection: IssueOwnershipProjection = {
    onAssigned: async (issue, slot): Promise<IssueProjectionOutcome> => {
      calls.push(`assign:${issue}:${slot}`);
      return {
        status: "projected",
        reason: null,
        repository: "heydonna-app/heydonna-app",
        issue,
        slot,
        added_labels: ["status:in-progress", `slot:${slot}`],
        removed_labels: ["status:todo"],
        verified: true,
      };
    },
    onReleased: async (issue, slot): Promise<IssueProjectionOutcome> => {
      calls.push(`release:${issue}:${slot}`);
      return {
        status: "projected",
        reason: null,
        repository: "heydonna-app/heydonna-app",
        issue,
        slot,
        added_labels: ["status:todo"],
        removed_labels: [`slot:${slot}`],
        verified: true,
      };
    },
  };
  const app = new Hono();
  registerAssignmentRoute(app, db, projection);
  return { db, directory, calls, app };
}

function close(value: Harness): void {
  value.db.close();
  rmSync(value.directory, { recursive: true, force: true });
}

function assignRequest(body: Record<string, unknown>): RequestInit {
  const headers = new Headers({ "content-type": "application/json" });
  headers.set(PM_TRANSITION_ASSIGNMENT_HEADER, PM_TRANSITION_ASSIGNMENT_AUTHORITY);
  return { method: "POST", headers, body: JSON.stringify(body) };
}

/** Occupy a slot through the durable layer (predecessor lane residue). */
function occupy(value: Harness, slot: number, issue: number, opts: {
  live?: boolean;
} = {}): void {
  const assigned = value.db.assignIssueToSlot(slot, issue, `predecessor #${issue} lane`, REPOSITORY);
  assert.equal(assigned.ok, true);
  if (opts.live) {
    value.db.updateSlot(slot, {
      idle: false,
      activity: "coding",
      active_turn_id: `turn-${issue}`,
      active_turn_started_at: "2026-09-20T04:00:00Z",
      active_turn_state: "active",
    });
  } else {
    value.db.updateSlot(slot, { idle: true, activity: "waiting_for_pm_direction" });
  }
}

test("assign refuses an occupied, unreleased slot and reports the real current state", async () => {
  const value = harness();
  try {
    occupy(value, 6, 7924, { live: false });
    const before = value.db.getSlot(6)!;

    const response = await value.app.request("/slots/6/assign", assignRequest({
      repository_id: REPOSITORY, issue: 7999, task: "new lane over an unreleased owner",
    }));
    assert.equal(response.status, 409);
    const body = await response.json() as Record<string, unknown>;
    assert.equal(body.reason, "slot_already_occupied");
    assert.deepEqual(body.owner_slots, [6]);
    assert.equal((body.slot as Record<string, unknown>).issue, 7924, "the envelope carries the real current owner");
    assert.equal((body.slot as Record<string, unknown>).assignment_epoch, before.assignment_epoch);

    const after = value.db.getSlot(6)!;
    assert.equal(after.issue, 7924, "no silent re-task");
    assert.equal(after.assignment_epoch, before.assignment_epoch, "no epoch bump without a release");
    assert.deepEqual(value.calls, [], "a refused assign never projects an issue surface");
  } finally {
    close(value);
  }
});

test("the complete-claim path still refuses an occupied slot (parity with the issue-only path)", async () => {
  const value = harness();
  try {
    occupy(value, 4, 7943, { live: false });
    const before = value.db.getSlot(4)!;
    const response = await value.app.request("/slots/4/assign", assignRequest({
      expected_epoch: before.assignment_epoch,
      repository_id: REPOSITORY,
      issue: 7997,
      pr: 9001,
      branch: "fix/7997",
      head_sha: "a".repeat(40),
      work_kind: "implementation",
      handoff_id: "handoff-7997",
      task: "complete claim over an unreleased owner",
    }));
    assert.equal(response.status, 409);
    const body = await response.json() as Record<string, unknown>;
    assert.equal(body.reason, "slot_already_occupied");
    assert.equal(value.db.getSlot(4)!.assignment_epoch, before.assignment_epoch);
  } finally {
    close(value);
  }
});

test("explicit force over a non-idle predecessor commits durably but never reports silent success", async () => {
  const value = harness();
  try {
    occupy(value, 3, 7952, { live: true });
    const before = value.db.getSlot(3)!;

    const response = await value.app.request("/slots/3/assign", assignRequest({
      repository_id: REPOSITORY,
      issue: 7996,
      task: "forced re-task after a failed release/clear",
      force_over_occupied: true,
    }));
    assert.equal(response.status, 409, "an uncleared predecessor is a typed non-success");
    const body = await response.json() as Record<string, unknown>;
    assert.equal(body.success, false);
    assert.equal(body.reason, "predecessor_context_uncleared");
    const predecessor = body.predecessor as Record<string, unknown>;
    assert.equal(predecessor.issue, 7952);
    assert.equal(predecessor.task, "predecessor #7952 lane");
    assert.equal(predecessor.active_turn_id, "turn-7952");
    assert.equal(predecessor.active_turn_state, "active");

    // The durable mutation did commit and the envelope reports the real state.
    const after = value.db.getSlot(3)!;
    assert.equal(after.issue, 7996);
    assert.equal(after.assignment_epoch, before.assignment_epoch + 1);
    assert.equal((body.slot as Record<string, unknown>).issue, 7996);
    assert.equal((body.slot as Record<string, unknown>).assignment_epoch, after.assignment_epoch);
    assert.match(String(body.remediation), /release or clear the previous owner/i);
    assert.deepEqual(value.calls, ["assign:7996:3"], "the durable owner still projects its surface");
    assert.equal(
      value.db.getEvents(3, 10, "slot_assigned_predecessor_uncleared").length,
      1,
      "the uncleared displace is audited",
    );
  } finally {
    close(value);
  }
});

test("explicit force over an idle predecessor succeeds and names the cleared context", async () => {
  const value = harness();
  try {
    occupy(value, 2, 7944, { live: false });
    const before = value.db.getSlot(2)!;

    const response = await value.app.request("/slots/2/assign", assignRequest({
      repository_id: REPOSITORY,
      issue: 7995,
      task: "forced re-task over an idle owner",
      force_over_occupied: true,
    }));
    assert.equal(response.status, 200);
    const body = await response.json() as Record<string, unknown>;
    assert.equal(body.issue, 7995);
    assert.equal(body.assignment_epoch, before.assignment_epoch + 1);
    const context = body.predecessor_context as Record<string, unknown>;
    assert.equal(context.cleared, true);
    assert.equal(context.reason, "predecessor_idle");
    assert.deepEqual(value.calls, ["assign:7995:2"]);
  } finally {
    close(value);
  }
});

test("idempotent same-issue assign and typed flag validation are unchanged", async () => {
  const value = harness();
  try {
    occupy(value, 1, 7896, { live: false });
    const before = value.db.getSlot(1)!;

    const idempotent = await value.app.request("/slots/1/assign", assignRequest({
      repository_id: REPOSITORY, issue: 7896, task: "same owner replay",
    }));
    assert.equal(idempotent.status, 200);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, before.assignment_epoch);

    const malformed = await value.app.request("/slots/1/assign", assignRequest({
      repository_id: REPOSITORY, issue: 7994, task: "bad flag", force_over_occupied: "yes",
    }));
    assert.equal(malformed.status, 409);
    const body = await malformed.json() as Record<string, unknown>;
    assert.equal(body.reason, "invalid_force_over_occupied");
    assert.equal((body.slot as Record<string, unknown>).issue, 7896);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, before.assignment_epoch);

    // A requested predecessor clear is refused truthfully, never silently ignored.
    const clearRequested = await value.app.request("/slots/1/assign", assignRequest({
      repository_id: REPOSITORY, issue: 7994, task: "pretend clear", force_over_occupied: true, clear_predecessor: true,
    }));
    assert.equal(clearRequested.status, 409);
    const clearBody = await clearRequested.json() as Record<string, unknown>;
    assert.equal(clearBody.reason, "clear_predecessor_unsupported_on_assign");
    assert.match(String(clearBody.remediation), /release or clear the previous owner/i);
    assert.equal(value.db.getSlot(1)!.issue, 7896, "the refused clear request mutates nothing");
  } finally {
    close(value);
  }
});
