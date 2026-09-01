import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";
import Database from "better-sqlite3";

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
  run: (app: Hono, db: MoPDatabase, directory: string) => Promise<void>,
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
    await run(app, db, directory);
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
  work_kind: "implementation",
  handoff_id: "handoff-route-default",
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

test("numbered assignment routes reject the slot-0 PM boundary", async () => {
  await withAssignmentRoute(async (app) => {
    for (const path of ["/slots/0/assign", "/slots/0/adopt-issue-claim"]) {
      const response = await app.request(path, assignmentRequest(
        PM_TRANSITION_ASSIGNMENT_AUTHORITY,
        assignment,
      ));
      assert.equal(response.status, 400, path);
    }
  });
});

test("issue-claim adoption route is authority-gated and atomic", async () => {
  await withAssignmentRoute(async (app, db) => {
    const placeholder = {
      repository_id: assignment.repository_id,
      issue: assignment.issue,
      task: "route authority fixture",
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
    assert.equal(db.getSlot(1)?.branch, null);
    assert.equal(db.getSlot(1)?.head_sha, null);
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
      repository_id: assignment.repository_id,
      issue: assignment.issue,
      task: "route authority fixture",
    };
    const assigned = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, placeholder),
    );
    assert.equal(assigned.status, 200);
    db.startAgentTurn(1, "turn-a");
    assert.equal(db.getSlot(1)?.active_turn_state, "active");

    const adopt = completeRebindBody(db.getSlot(1)!);
    const refused = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, adopt),
    );
    assert.equal(refused.status, 409);
    assert.equal((await refused.json() as Record<string, unknown>).reason, "active_turn");
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
    db.finishAgentTurn(1, "turn-a");
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
    assert.equal(adopted.active_turn_state, "inactive");
    assert.equal(adopted.active_turn_id, null);
    assert.equal(db.getEvents(1, 10, "slot_issue_claim_adopted").length, 1);
  });
});

test("issue-claim adoption route refuses a stale successor rewrite", async () => {
  await withAssignmentRoute(async (app, db) => {
    const placeholder = {
      repository_id: assignment.repository_id,
      issue: assignment.issue,
      task: "route authority fixture",
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

test("production assignment route accepts the minimal issue contract", async () => {
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
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
        issue: assignment.issue,
        task: assignment.task,
      }),
    );
    assert.equal(authorized.status, 200);
    const assigned = await authorized.json() as Record<string, unknown>;
    assert.equal(assigned.occupied, true);
    assert.equal(assigned.repository_id, "heydonna-app/heydonna-app");
    assert.equal(assigned.issue, assignment.issue);
    assert.equal(assigned.pr, null);
    assert.equal(assigned.branch, null);
    assert.equal(assigned.head_sha, null);
    assert.equal(assigned.assignment_epoch, 1);
    assert.equal(db.getEvents(1, 10, "slot_assigned").length, 1);
  });
});

test("claim route requires only a positive issue", async () => {
  await withAssignmentRoute(async (app, db) => {
    for (const body of [{}, { issue: null }, { issue: 0 }]) {
      const response = await app.request(
        "/slots/1/assign",
        assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, body),
      );
      assert.equal(response.status, 409);
      assert.equal((await response.json() as Record<string, unknown>).reason, "invalid_issue");
      assert.equal(db.getSlot(1)?.occupied, false);
    }
  });
});

test("issue-only assignment refuses a stale active turn", async () => {
  await withAssignmentRoute(async (app, db) => {
    const active = {
      issue: assignment.issue,
      task: assignment.task,
      session_id: "caller-must-not-own",
    };
    db.startAgentTurn(1, "hook-session-a");
    db.updateSlot(1, { occupied: false, repository_id: null, issue: null, branch: null, branch_ref: null, pr: null, head_sha: null });
    const before = db.getSlot(1);
    const refused = await app.request("/slots/1/assign", assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, active));
    assert.equal(refused.status, 409);
    assert.equal((await refused.json() as Record<string, unknown>).reason, "active_turn");
    assert.deepEqual(db.getSlot(1), before);
    assert.equal(db.getEvents(1, 10, "slot_assigned").length, 0);
  });
});

test("issue-only claim refuses an occupied slot without mutating its owner", async () => {
  await withAssignmentRoute(async (app, db) => {
    const first = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
        issue: assignment.issue,
        task: assignment.task,
      }),
    );
    assert.equal(first.status, 200);
    const before = db.getSlot(1);

    const occupiedClaim = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
        issue: assignment.issue + 1,
        task: "replaced issue-only claim",
      }),
    );
    assert.equal(occupiedClaim.status, 409);
    const body = await occupiedClaim.json() as Record<string, unknown>;
    assert.equal(body.reason, "slot_already_occupied");
    assert.deepEqual(db.getSlot(1), before);
    assert.equal(db.getEvents(1, 10, "slot_assigned").length, 1);
  });
});

test("issue-only idempotency is bound to the normalized repository", async () => {
  await withAssignmentRoute(async (_app, db) => {
    const first = db.assignIssueToSlot(1, 7343, "repo-one issue", "github:repo-1");
    assert.equal(first.ok, true);
    assert.equal(first.idempotent, false);
    db.logEvent(1, "slot_assigned", null, null, {
      issue: 7343,
      assignment_epoch: first.assignment_epoch,
    });

    const before = db.getSlot(1);
    assert.ok(before);
    const eventsBefore = db.getEvents(1, 10, "slot_assigned");

    const sameRepositoryReplay = db.assignIssueToSlot(
      1,
      7343,
      "same issue replay",
      "github:repo-1",
    );
    assert.equal(sameRepositoryReplay.ok, true);
    assert.equal(sameRepositoryReplay.idempotent, true);
    assert.equal(sameRepositoryReplay.assignment_epoch, before.assignment_epoch);
    assert.deepEqual(db.getSlot(1), before);
    assert.equal(db.getEvents(1, 10, "slot_assigned").length, eventsBefore.length);

    const differentRepository = db.assignIssueToSlot(
      1,
      7343,
      "same issue in another repository",
      "github:repo-2",
    );
    assert.equal(differentRepository.ok, false);
    assert.equal(differentRepository.reason, "slot_already_occupied");
    assert.equal(differentRepository.assignment_epoch, before.assignment_epoch);
    assert.deepEqual(db.getSlot(1), before);
    assert.equal(db.getEvents(1, 10, "slot_assigned").length, eventsBefore.length);
  });
});

test("issue-only claim requires a free inactive non-DND hook boundary", async () => {
  await withAssignmentRoute(async (app, db) => {
    const cases: Array<{ name: string; updates: Record<string, unknown>; reason: string }> = [
      { name: "active turn", updates: { active_turn_id: "turn-a", active_turn_state: "active", idle: false }, reason: "active_turn" },
      { name: "indeterminate turn", updates: { active_turn_id: null, active_turn_state: "indeterminate", idle: false }, reason: "active_turn" },
      { name: "DND", updates: { active_turn_id: null, active_turn_state: "inactive", idle: true, dnd: true }, reason: "dnd_active" },
    ];
    for (const testCase of cases) {
      db.updateSlot(1, { occupied: false, repository_id: null, issue: null, branch: null, branch_ref: null, pr: null, head_sha: null, ...testCase.updates });
      const before = db.getSlot(1);
      const response = await app.request(
        "/slots/1/assign",
        assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, { issue: assignment.issue, repository_id: assignment.repository_id }),
      );
      assert.equal(response.status, 409, testCase.name);
      assert.equal((await response.json() as Record<string, unknown>).reason, testCase.reason, testCase.name);
      assert.deepEqual(db.getSlot(1), before, testCase.name);
    }
  });
});

test("issue-only duplicate detection is scoped to the normalized repository", async () => {
  await withAssignmentRoute(async (app, db) => {
    const first = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, { issue: assignment.issue, repository_id: "github:repo-1" }),
    );
    assert.equal(first.status, 200);
    const duplicate = await app.request(
      "/slots/2/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, { issue: assignment.issue, repository_id: "github:repo-1" }),
    );
    assert.equal(duplicate.status, 409);
    assert.equal((await duplicate.json() as Record<string, unknown>).reason, "target_already_assigned");

    const differentRepository = await app.request(
      "/slots/3/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, { issue: assignment.issue, repository_id: "github:repo-2" }),
    );
    assert.equal(differentRepository.status, 200);
    assert.equal(db.getSlot(3)?.repository_id, "github:repo-2");
  });
});

test("adopt route has no partial expected-tuple fallback", async () => {
  await withAssignmentRoute(async (app, db) => {
    const assigned = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
        repository_id: assignment.repository_id,
        issue: assignment.issue,
        task: "route authority fixture",
      }),
    );
    assert.equal(assigned.status, 200);
    const partial = {
      expected_epoch: 1,
      expected_current_pr: null,
      expected_current_branch_ref: "refs/heads/fix/10-pending",
      expected_current_head_sha: null,
      repository_id: assignment.repository_id,
      issue: assignment.issue,
      pr: assignment.pr,
      branch: assignment.branch,
      head_sha: assignment.head_sha,
    };
    const refused = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, partial),
    );
    assert.equal(refused.status, 409);
    assert.equal((await refused.json() as Record<string, unknown>).reason, "observed_tuple_mismatch");
    assert.equal(db.getSlot(1)?.pr, null);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
  });
});

test("assignment route rejects only a duplicate issue on another slot", async () => {
  await withAssignmentRoute(async (app, db) => {
    const response = await app.request(
      "/slots/1/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
        repository_id: assignment.repository_id,
        issue: assignment.issue,
        task: assignment.task,
      }),
    );
    assert.equal(response.status, 200);
    const assigned = await response.json() as Record<string, unknown>;
    assert.equal(assigned.work_kind, null);
    assert.equal(assigned.handoff_id, null);
    assert.equal(typeof assigned.claimed_at, "string");
    assert.notEqual(assigned.claimed_at, "");
    assert.equal(assigned.claimed_at, assigned.assigned_at);
    assert.equal(db.getSlot(1)?.work_kind, null);
    assert.equal(db.getSlot(1)?.handoff_id, null);

    const duplicate = await app.request(
      "/slots/2/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
        issue: assignment.issue,
        repository_id: assignment.repository_id,
      }),
    );
    assert.equal(duplicate.status, 409);
    const duplicateBody = await duplicate.json() as Record<string, unknown>;
    assert.equal(duplicateBody.reason, "target_already_assigned");
    assert.deepEqual(duplicateBody.owner_slots, [1]);
    assert.equal(db.getSlot(2)?.occupied, false);

  });
});

test("complete assignment atomically persists the exact epoch and owner tuple", async () => {
  await withAssignmentRoute(async (app, db, directory) => {
    const raw = new Database(join(directory, "mop.db"));
    try {
      raw.prepare("UPDATE slots SET assignment_epoch = 613 WHERE slot = 4").run();
    } finally {
      raw.close();
    }
    const body = {
      task: "repro PR #7591 #3787 save-admission",
      repository_id: 992731533,
      issue: 7554,
      pr: 7591,
      branch: "codex/cloudflare-clerk-build-binding",
      head_sha: "f109414c02cc296510103fe2c090ce964e9b9dfb",
      work_kind: "repro",
      handoff_id: "repro-7591-s4-f109414c0-f27748d8",
      expected_epoch: 613,
    };
    const response = await app.request(
      "/slots/4/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, body),
    );
    assert.equal(response.status, 200);
    const row = await response.json() as Record<string, unknown>;
    assert.equal(row.assignment_epoch, 614);
    assert.equal(row.repository_id, "992731533");
    assert.equal(row.issue, 7554);
    assert.equal(row.pr, 7591);
    assert.equal(row.branch, body.branch);
    assert.equal(row.head_sha, body.head_sha);
    assert.equal(row.work_kind, body.work_kind);
    assert.equal(row.handoff_id, body.handoff_id);
    assert.equal(row.task, body.task);
    assert.equal(db.getEvents(4, 10, "slot_assigned").length, 1);

    // A response-loss retry with the consumed epoch cannot create a second
    // assignment or event; the CAS refuses the stale request.
    const replay = await app.request(
      "/slots/4/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, body),
    );
    assert.equal(replay.status, 409);
    assert.equal((await replay.json() as Record<string, unknown>).reason, "epoch_mismatch");
    assert.equal(db.getSlot(4)?.assignment_epoch, 614);
    assert.equal(db.getEvents(4, 10, "slot_assigned").length, 1);
  });
});

test("complete issue-only assignment persists the exact branch/head tuple with null PR", async () => {
  await withAssignmentRoute(async (app, db, directory) => {
    const raw = new Database(join(directory, "mop.db"));
    try {
      raw.prepare("UPDATE slots SET assignment_epoch = 619 WHERE slot = 4").run();
    } finally {
      raw.close();
    }
    const body = {
      task: "S4 PR #7554 direct-evidence retention",
      repository_id: 992731533,
      issue: 7554,
      pr: null,
      branch: "fix/7554-sc-direct-evidence-retention",
      head_sha: "5513e0cd659fec8a22afd93a18465e12d56e87d0",
      work_kind: "repro",
      handoff_id: "repro-7554-s4-5513e0cd",
      expected_epoch: 619,
    };
    const response = await app.request(
      "/slots/4/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, body),
    );
    assert.equal(response.status, 200);
    const row = await response.json() as Record<string, unknown>;
    assert.equal(row.assignment_epoch, 620);
    assert.equal(row.repository_id, "992731533");
    assert.equal(row.issue, body.issue);
    assert.equal(row.pr, null);
    assert.equal(row.branch, body.branch);
    assert.equal(row.head_sha, body.head_sha);
    assert.equal(row.work_kind, body.work_kind);
    assert.equal(row.handoff_id, body.handoff_id);
    assert.equal(row.task, body.task);
    assert.equal(db.getEvents(4, 10, "slot_assigned").length, 1);

    // A lost response cannot be replayed against the consumed epoch or create
    // a second assignment/event.
    const replay = await app.request(
      "/slots/4/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, body),
    );
    assert.equal(replay.status, 409);
    assert.equal((await replay.json() as Record<string, unknown>).reason, "epoch_mismatch");
    assert.equal(db.getSlot(4)?.assignment_epoch, 620);
    assert.equal(db.getEvents(4, 10, "slot_assigned").length, 1);
  });
});

test("complete issue-only assignment requires explicit nullable PR field", async () => {
  await withAssignmentRoute(async (app, db) => {
    const complete = {
      task: "issue-only complete claim",
      repository_id: 992731533,
      issue: 7554,
      branch: "fix/7554-sc-direct-evidence-retention",
      head_sha: "5513e0cd659fec8a22afd93a18465e12d56e87d0",
      work_kind: "repro",
      handoff_id: "repro-7554-s4-missing-pr",
      expected_epoch: 0,
    };
    for (const body of [complete, { ...complete, pr: 0 }, { ...complete, pr: "7591" }]) {
      const response = await app.request(
        "/slots/4/assign",
        assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, body),
      );
      assert.equal(response.status, 409);
      assert.equal((await response.json() as Record<string, unknown>).reason, "observed_tuple_mismatch");
      assert.equal(db.getSlot(4)?.occupied, false);
      assert.equal(db.getEvents(4, 10, "slot_assigned").length, 0);
    }
  });
});

test("complete assignment refuses partial identity instead of downgrading it", async () => {
  await withAssignmentRoute(async (app, db) => {
    const response = await app.request(
      "/slots/4/assign",
      assignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
        issue: 7554,
        task: "incomplete complete claim",
        expected_epoch: 0,
        repository_id: 992731533,
        pr: 7591,
        branch: "codex/cloudflare-clerk-build-binding",
        // head_sha, work_kind, and handoff_id are intentionally absent.
      }),
    );
    assert.equal(response.status, 409);
    assert.equal((await response.json() as Record<string, unknown>).reason, "observed_tuple_mismatch");
    assert.equal(db.getSlot(4)?.occupied, false);
    assert.equal(db.getEvents(4, 10, "slot_assigned").length, 0);
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
