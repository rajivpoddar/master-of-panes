import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import {
  assignmentIdentityPatchFields,
  isPmTransitionAssignmentRequest,
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "../src/assignmentAuthority.js";
import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import { MoPDatabase } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

async function withAssignmentRoute(
  run: (app: Hono, db: MoPDatabase) => Promise<void>,
): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-assignment-route-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const app = new Hono();
  registerAssignmentRoute(app, db);
  try {
    await run(app, db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

const assignment = {
  task: "route authority fixture",
  repository_id: "github:repo-1",
  issue: 10,
  pr: 20,
  branch: "fix/10",
  head_sha: "a".repeat(40),
  expected_epoch: 0,
};

type SlotReadback = NonNullable<ReturnType<MoPDatabase["getSlot"]>>;

function completeRebindBody(
  current: SlotReadback,
  desired: Record<string, unknown> = assignment,
): Record<string, unknown> {
  return {
    expected_epoch: current.assignment_epoch,
    expected_current_repository_id: current.repository_id,
    expected_current_issue: current.issue,
    expected_current_pr: current.pr,
    expected_current_branch: current.branch,
    expected_current_head_sha: current.head_sha,
    expected_current_work_kind: current.work_kind,
    expected_current_handoff_id: current.handoff_id,
    expected_current_claimed_at: current.claimed_at,
    repository_id: desired.repository_id,
    issue: desired.issue,
    pr: desired.pr,
    branch: desired.branch,
    head_sha: desired.head_sha,
    work_kind: desired.work_kind ?? current.work_kind ?? null,
    handoff_id: desired.handoff_id ?? current.handoff_id ?? null,
    claimed_at: desired.claimed_at ?? current.claimed_at ?? null,
    task: desired.task ?? current.task,
  };
}

function assignmentRequest(
  authority?: string,
  body: Record<string, unknown> = assignment,
): RequestInit {
  const headers = new Headers({ "content-type": "application/json" });
  if (authority !== undefined) {
    headers.set(PM_TRANSITION_ASSIGNMENT_HEADER, authority);
  }
  return {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  };
}

test("only the guarded PM transition authority reaches REST assignment", () => {
  assert.equal(
    isPmTransitionAssignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY),
    true,
  );
  assert.equal(isPmTransitionAssignmentRequest(undefined), false);
  assert.equal(isPmTransitionAssignmentRequest("mop"), false);
  assert.equal(isPmTransitionAssignmentRequest("pm-transition"), false);
});

test("issue-claim adoption route is authority-gated and atomic", async () => {
  await withAssignmentRoute(async (app, db) => {
    const placeholder = {
      ...assignment,
      pr: null,
      branch: "fix/10-pending",
      head_sha: null,
    };
    const assigned = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, placeholder),
    );
    assert.equal(assigned.status, 200);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);

    const adopt = completeRebindBody(db.getSlot(1)!);
    const denied = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(undefined, adopt),
    );
    assert.equal(denied.status, 403);
    assert.equal(db.getSlot(1)?.branch, "fix/10-pending");
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);

    const accepted = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, adopt),
    );
    assert.equal(accepted.status, 200);
    const adopted = await accepted.json() as Record<string, unknown>;
    assert.equal(adopted.occupied, true);
    assert.equal(adopted.issue, assignment.issue);
    assert.equal(adopted.pr, assignment.pr);
    assert.equal(adopted.branch, assignment.branch);
    assert.equal(adopted.head_sha, assignment.head_sha);
    assert.equal(adopted.assignment_epoch, 2);
    assert.equal(db.getEvents(1, 10, "slot_issue_claim_adopted").length, 1);
  });
});

test("issue-claim adoption route binds an active-turn claim preserving epoch and turn", async () => {
  await withAssignmentRoute(async (app, db) => {
    const placeholder = {
      ...assignment,
      pr: null,
      branch: "fix/10-pending",
      head_sha: null,
    };
    const assigned = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, placeholder),
    );
    assert.equal(assigned.status, 200);
    db.startAgentTurn(1, "turn-a");
    assert.equal(db.getSlot(1)?.active_turn_state, "active");

    const adopt = completeRebindBody(db.getSlot(1)!);
    const accepted = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, adopt),
    );
    assert.equal(accepted.status, 200);
    const adopted = await accepted.json() as Record<string, unknown>;
    assert.equal(adopted.occupied, true);
    assert.equal(adopted.issue, assignment.issue);
    assert.equal(adopted.pr, assignment.pr);
    assert.equal(adopted.branch, assignment.branch);
    assert.equal(adopted.head_sha, assignment.head_sha);
    assert.equal(adopted.assignment_epoch, 2);
    assert.equal(adopted.active_turn_state, "active");
    assert.equal(adopted.active_turn_id, "turn-a");
    assert.equal(db.getEvents(1, 10, "slot_issue_claim_adopted").length, 1);
  });
});

test("issue-claim adoption route refuses a stale successor rewrite", async () => {
  await withAssignmentRoute(async (app, db) => {
    const placeholder = {
      ...assignment,
      pr: null,
      branch: "fix/10-pending",
      head_sha: null,
    };
    const assigned = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, placeholder),
    );
    assert.equal(assigned.status, 200);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);

    const beforeBind = db.getSlot(1)!;
    const bind = completeRebindBody(beforeBind);
    const bound = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, bind),
    );
    assert.equal(bound.status, 200);
    assert.equal(db.getSlot(1)?.pr, assignment.pr);

    const eventsBefore = db.getEvents(1, 10, "slot_issue_claim_adopted").length;
    const rewrite = completeRebindBody(beforeBind, {
      ...assignment,
      pr: assignment.pr + 1,
    });
    const refused = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, rewrite),
    );
    assert.equal(refused.status, 409);
    const body = await refused.json() as Record<string, unknown>;
    assert.equal(body.reason, "epoch_mismatch");
    assert.equal(body.conflict, true);
    assert.equal(db.getSlot(1)?.pr, assignment.pr);
    assert.equal(db.getSlot(1)?.assignment_epoch, 2);
    assert.equal(
      db.getEvents(1, 10, "slot_issue_claim_adopted").length,
      eventsBefore,
      "refused PR rewrite must not log an adoption event",
    );

    // Re-issued adoption claiming the PR-bound observed tuple with the SAME
    // pr is an idempotent no-op success.
    const replay = completeRebindBody(db.getSlot(1)!);
    const replayed = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, replay),
    );
    assert.equal(replayed.status, 200);
    const row = await replayed.json() as Record<string, unknown>;
    assert.equal(row.pr, assignment.pr);
    assert.equal(row.assignment_epoch, 2);
    assert.equal(db.getSlot(1)?.pr, assignment.pr);
    assert.equal(db.getSlot(1)?.assignment_epoch, 2);
  });
});

test("production assignment route enforces PM authority before mutation", async () => {
  await withAssignmentRoute(async (app, db) => {
    const initial = db.getSlot(1);

    for (const authority of [undefined, "wrong-authority"]) {
      const response = await app.request(
        "/slots/1/assign",
        assignmentRequest(authority),
      );
      assert.equal(response.status, 403);
      assert.deepEqual(await response.json(), {
        success: false,
        conflict: true,
        error: "assignment authority is required",
        reason: "assignment_authority_required",
      });
      assert.deepEqual(db.getSlot(1), initial);
      assert.equal(db.getEvents(1, 10, "slot_assigned").length, 0);
    }

    const authorized = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY),
    );
    assert.equal(authorized.status, 200);
    const assigned = await authorized.json() as Record<string, unknown>;
    assert.equal(assigned.occupied, true);
    assert.equal(assigned.repository_id, assignment.repository_id);
    assert.equal(assigned.issue, assignment.issue);
    assert.equal(assigned.pr, assignment.pr);
    assert.equal(assigned.branch_ref, `refs/heads/${assignment.branch}`);
    assert.equal(assigned.head_sha, assignment.head_sha);
    assert.equal(assigned.assignment_epoch, 1);
    assert.equal(db.getEvents(1, 10, "slot_assigned").length, 1);
  });
});

test("assignment route persists claim metadata and release clears it", async () => {
  await withAssignmentRoute(async (app, db) => {
    const response = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
        ...assignment,
        work_kind: "implementation",
        handoff_id: "handoff-route-1",
      }),
    );
    assert.equal(response.status, 200);
    const assigned = await response.json() as Record<string, unknown>;
    assert.equal(assigned.work_kind, "implementation");
    assert.equal(assigned.handoff_id, "handoff-route-1");
    assert.equal(typeof assigned.claimed_at, "string");
    assert.notEqual(assigned.claimed_at, "");
    assert.equal(assigned.claimed_at, assigned.assigned_at);
    assert.equal(db.getSlot(1)?.work_kind, "implementation");
    assert.equal(db.getSlot(1)?.handoff_id, "handoff-route-1");

    const omittedMetadataReplay = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
        ...assignment,
        expected_epoch: 1,
      }),
    );
    assert.equal(omittedMetadataReplay.status, 409);
    const replayBody = await omittedMetadataReplay.json() as Record<string, unknown>;
    assert.equal(replayBody.reason, "slot_already_occupied");
    assert.equal(db.getSlot(1)?.work_kind, "implementation");
    assert.equal(db.getSlot(1)?.handoff_id, "handoff-route-1");

    const released = db.releaseSlot(1, 1);
    assert.deepEqual(released, {
      ok: true,
      conflict: false,
      assignment_epoch: 2,
      idempotent: false,
    });
    const free = db.getSlot(1);
    assert.equal(free?.work_kind, null);
    assert.equal(free?.handoff_id, null);
    assert.equal(free?.claimed_at, null);
  });
});

test("generic PATCH refuses every assignment identity field", () => {
  assert.deepEqual(
    assignmentIdentityPatchFields({
      name: "Rohini",
      dnd: false,
      repository_id: "github:repo-1",
      occupied: true,
      issue: 10,
      pr: 20,
      branch: "fix/10",
      branch_ref: "refs/heads/fix/10",
      head_sha: "a".repeat(40),
      assignment_epoch: 3,
      assigned_at: "2026-07-28T00:00:00Z",
      work_kind: "implementation",
      handoff_id: "handoff-1",
      claimed_at: "2026-07-28T00:00:00Z",
      status: "active",
    }),
    [
      "assigned_at",
      "assignment_epoch",
      "branch",
      "branch_ref",
      "claimed_at",
      "handoff_id",
      "head_sha",
      "issue",
      "occupied",
      "pr",
      "repository_id",
      "status",
      "work_kind",
    ],
  );
  assert.deepEqual(
    assignmentIdentityPatchFields({
      name: "Rohini",
      task: "same assignment metadata",
      dnd: false,
      idle: true,
      activity: "testing",
    }),
    [],
  );
});

test("MCP and hooks expose no direct assignment writer", () => {
  const mcp = readFileSync(new URL("../src/mcp.ts", import.meta.url), "utf8");
  const hooks = readFileSync(new URL("../src/hooks.ts", import.meta.url), "utf8");
  assert.equal(mcp.includes('"mop_assign_slot"'), false);
  assert.equal(hooks.includes(".assignSlot("), false);
  assert.match(hooks, /assignment_bypass_refused/);
});
