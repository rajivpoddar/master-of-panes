import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  assignmentIdentityPatchFields,
  isPmTransitionAssignmentRequest,
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
} from "../src/assignmentAuthority.js";

test("only the guarded PM transition authority reaches REST assignment", () => {
  assert.equal(
    isPmTransitionAssignmentRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY),
    true,
  );
  assert.equal(isPmTransitionAssignmentRequest(undefined), false);
  assert.equal(isPmTransitionAssignmentRequest("mop"), false);
  assert.equal(isPmTransitionAssignmentRequest("pm-transition"), false);
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
      status: "active",
    }),
    [
      "assigned_at",
      "assignment_epoch",
      "branch",
      "branch_ref",
      "head_sha",
      "issue",
      "occupied",
      "pr",
      "repository_id",
      "status",
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
