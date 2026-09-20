import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";
import Database from "better-sqlite3";

import { assignmentIdentityPatchFields, registerAssignmentRoute } from "../src/assignmentRoute.js";
import { MoPDatabase } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

/**
 * The x-heydonna-assignment-authority header is RETIRED (Rajiv 2026-09-20):
 * MoP is a single-user local tool used only by PM and CTO, so the header and
 * its refusal path were friction without security. Call sites below still pass
 * the old value to keep the fixtures readable; the helper never sends it.
 */
const RETIRED_AUTHORITY_VALUE = "pm-transition-v1";

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
  _retiredAuthority?: string,
  body: Record<string, unknown> = assignment,
): RequestInit {
  return {
    method: "POST",
    headers: new Headers({ "content-type": "application/json" }),
    body: JSON.stringify(body),
  };
}

test("the retired authority contract is gone: no header, no refusal path, no remediation", () => {
  assert.equal(existsSync(new URL("../src/assignmentAuthority.ts", import.meta.url)), false, "the authority module is deleted");
  const route = readFileSync(new URL("../src/assignmentRoute.ts", import.meta.url), "utf8");
  const family2 = readFileSync(new URL("../src/family2Routes.ts", import.meta.url), "utf8");
  for (const source of [route, family2]) {
    assert.equal(source.includes("assignment_authority_required"), false);
    assert.equal(source.includes("x-heydonna-assignment-authority"), false);
  }
});

test("a stale authority header is ignored, and assignment needs no header at all", async () => {
  await withAssignmentRoute(async (app, db) => {
    const bare = await app.request("/slots/1/assign", {
      method: "POST",
      headers: new Headers({ "content-type": "application/json" }),
      body: JSON.stringify(assignment),
    });
    assert.equal(bare.status, 200, "assignment must not require an authority header");
    assert.equal(db.getSlot(1)?.issue, assignment.issue);
  });
  await withAssignmentRoute(async (app, db) => {
    // Same call with a stale/wrong header value: inert, never a refusal.
    const stale = await app.request("/slots/1/assign", {
      method: "POST",
      headers: new Headers({ "content-type": "application/json", "x-heydonna-assignment-authority": "wrong-authority" }),
      body: JSON.stringify(assignment),
    });
    assert.equal(stale.status, 200);
    const body = await stale.json() as Record<string, unknown>;
    assert.equal(body.occupied, true);
    assert.equal(body.reason, undefined);
    assert.equal(db.getSlot(1)?.issue, assignment.issue);
  });
});
test("numbered assignment routes reject the slot-0 PM boundary", async () => {
  await withAssignmentRoute(async (app) => {
    for (const path of ["/slots/0/assign", "/slots/0/adopt-issue-claim"]) {
      const response = await app.request(path, assignmentRequest(
        RETIRED_AUTHORITY_VALUE,
        assignment,
      ));
      assert.equal(response.status, 400, path);
    }
  });
});

test("issue-claim adoption route needs no authority header and stays atomic", async () => {
  await withAssignmentRoute(async (app, db) => {
    const placeholder = {
      repository_id: assignment.repository_id,
      issue: assignment.issue,
      task: "route authority fixture",
    };
    const assigned = await app.request(
      "/slots/1/assign",
      assignmentRequest(RETIRED_AUTHORITY_VALUE, placeholder),
    );
    assert.equal(assigned.status, 200);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);

    const adopt = completeRebindBody(db.getSlot(1)!);
    // No header is sent: the retired gate must not refuse this call.
    const accepted = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(undefined, adopt),
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, placeholder),
    );
    assert.equal(assigned.status, 200);
    db.startAgentTurn(1, "turn-a");
    assert.equal(db.getSlot(1)?.active_turn_state, "active");

    const adopt = completeRebindBody(db.getSlot(1)!);
    const refused = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(RETIRED_AUTHORITY_VALUE, adopt),
    );
    assert.equal(refused.status, 409);
    assert.equal((await refused.json() as Record<string, unknown>).reason, "active_turn");
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
    db.finishAgentTurn(1, "turn-a");
    const accepted = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(RETIRED_AUTHORITY_VALUE, adopt),
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, placeholder),
    );
    assert.equal(assigned.status, 200);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);

    const beforeBind = db.getSlot(1)!;
    const bind = completeRebindBody(beforeBind);
    const bound = await app.request(
      "/slots/1/adopt-issue-claim",
      assignmentRequest(RETIRED_AUTHORITY_VALUE, bind),
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, rewrite),
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, replay),
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
    const authorized = await app.request(
      "/slots/1/assign",
      assignmentRequest(undefined, { issue: assignment.issue, task: assignment.task }),
    );
    assert.equal(authorized.status, 200);
    assert.deepEqual(db.getEvents(1, 10, "slot_assigned").length, 1);
    assert.notDeepEqual(db.getSlot(1), initial);
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
        assignmentRequest(RETIRED_AUTHORITY_VALUE, body),
      );
      assert.equal(response.status, 409);
      assert.equal((await response.json() as Record<string, unknown>).reason, "invalid_issue");
      assert.equal(db.getSlot(1)?.occupied, false);
    }
  });
});

test("assignment ignores stale turn and tuple telemetry", async () => {
  await withAssignmentRoute(async (app, db) => {
    const active = {
      issue: assignment.issue,
      task: assignment.task,
      session_id: "caller-must-not-own",
    };
    db.startAgentTurn(1, "hook-session-a");
    db.updateSlot(1, { occupied: false, repository_id: null, issue: null, branch: null, branch_ref: null, pr: null, head_sha: null });
    const accepted = await app.request("/slots/1/assign", assignmentRequest(RETIRED_AUTHORITY_VALUE, active));
    assert.equal(accepted.status, 200);
    const row = db.getSlot(1)!;
    assert.equal(row.assignment_epoch, 1);
    assert.equal(row.active_turn_state, "inactive");
    assert.equal(row.active_turn_id, null);
    assert.equal("session_id" in JSON.parse(db.getEvents(1, 1, "slot_assigned")[0].payload), false);
  });
});

test("claim route refuses to overwrite an occupied slot unless the caller forces it", async () => {
  await withAssignmentRoute(async (app, db) => {
    const first = await app.request(
      "/slots/1/assign",
      assignmentRequest(RETIRED_AUTHORITY_VALUE, {
        issue: assignment.issue,
        task: assignment.task,
      }),
    );
    assert.equal(first.status, 200);
    const before = db.getSlot(1);

    // Release-first invariant: an occupied, unreleased owner is never silently
    // re-tasked (this is the S6 77 -> 78 boundary that produced lane residue).
    const occupiedClaim = await app.request(
      "/slots/1/assign",
      assignmentRequest(RETIRED_AUTHORITY_VALUE, {
        issue: assignment.issue + 1,
        task: "replaced issue-only claim",
      }),
    );
    assert.equal(occupiedClaim.status, 409);
    const refused = await occupiedClaim.json() as Record<string, unknown>;
    assert.equal(refused.reason, "slot_already_occupied");
    assert.equal(db.getSlot(1)?.issue, assignment.issue);
    assert.equal(db.getSlot(1)?.assignment_epoch, before!.assignment_epoch);

    // The explicit force override still commits durably, but a freshly
    // accepted-not-started predecessor (idle=false) is never reported as a
    // silent success: only an explicit idle=true qualifies for
    // predecessor_idle.
    const forced = await app.request(
      "/slots/1/assign",
      assignmentRequest(RETIRED_AUTHORITY_VALUE, {
        issue: assignment.issue + 1,
        task: "forced issue-only claim",
        force_over_occupied: true,
      }),
    );
    assert.equal(forced.status, 409);
    const forcedBody = await forced.json() as Record<string, unknown>;
    assert.equal(forcedBody.success, false);
    assert.equal(forcedBody.reason, "predecessor_context_uncleared");
    assert.equal((forcedBody.predecessor as Record<string, unknown>).issue, assignment.issue);
    assert.match(String(forcedBody.remediation), /release or clear the previous owner/i);
    assert.equal(db.getSlot(1)?.issue, assignment.issue + 1);
    assert.equal(db.getSlot(1)?.assignment_epoch, before!.assignment_epoch + 1);
  });
});

test("adopt route has no partial expected-tuple fallback", async () => {
  await withAssignmentRoute(async (app, db) => {
    const assigned = await app.request(
      "/slots/1/assign",
      assignmentRequest(RETIRED_AUTHORITY_VALUE, {
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, partial),
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, {
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, {
        issue: assignment.issue,
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, body),
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, body),
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, body),
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, body),
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
        assignmentRequest(RETIRED_AUTHORITY_VALUE, body),
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
      assignmentRequest(RETIRED_AUTHORITY_VALUE, {
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
