import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";
import { Hono } from "hono";

import {
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "../src/assignmentAuthority.js";
import { registerFamily2Routes } from "../src/family2Routes.js";

const REPO_ROOT = new URL("..", import.meta.url).pathname;
const MCP_JSON = `${REPO_ROOT}.mcp.json`;
const CURRENT_DIST = "/Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/current/dist/mcp.js";
const FROZEN_DIST = "/Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/dist/mcp.js";
const SKILL = `${REPO_ROOT}scripts/pm/shared-assets/claude/skills/direct-release/SKILL.md`;

/** The current release's built MCP bundle, when the local release pointer exists. */
function currentBundle(): string | null {
  return existsSync(CURRENT_DIST) ? readFileSync(CURRENT_DIST, "utf8") : null;
}

test("the MCP client resolves from the canonical current release, not the frozen 1.0.0 bundle", () => {
  const config = JSON.parse(readFileSync(MCP_JSON, "utf8")) as {
    mop: { command: string; args: string[]; env: Record<string, string> };
  };
  assert.equal(config.mop.args.length, 1);
  assert.equal(config.mop.args[0], CURRENT_DIST);
  assert.equal(config.mop.args[0].includes("/1.0.0/"), false, "the stale frozen client must not be registered");
  assert.equal(config.mop.env.MOP_DB_PATH.endsWith("/1.0.0/data/mop.db"), true);
});

test("the registered MCP bundle implements the current release contract", () => {
  const bundle = currentBundle();
  if (bundle === null) {
    // The release pointer is absent in a bare checkout; the contract is then
    // asserted against the source of truth instead.
    const source = readFileSync(`${REPO_ROOT}src/mcp.ts`, "utf8");
    assert.match(source, /mopReleaseSlotInputShape/);
    assert.match(source, /PM_TRANSITION_ASSIGNMENT_HEADER\]: PM_TRANSITION_ASSIGNMENT_AUTHORITY/);
    assert.match(source, /intended_main_head/);
    assert.match(source, /release_mode/);
    return;
  }
  assert.match(bundle, /mop_release_slot/);
  assert.match(bundle, /PM_TRANSITION_ASSIGNMENT_HEADER/);
  assert.match(bundle, /intended_main_head/);
  assert.match(bundle, /release_mode/);
  assert.match(bundle, /expected_claimed_at/);
});

test("RED evidence: the frozen 1.0.0 client shape could never satisfy the release authority", () => {
  const frozen = existsSync(FROZEN_DIST) ? readFileSync(FROZEN_DIST, "utf8") : null;
  if (frozen === null) return;
  assert.match(frozen, /"mop_release_slot", "Release one idle, inactive, non-DND numbered slot with one direct MoP call\."/);
  assert.equal(frozen.includes("intended_main_head"), false, "frozen client predates the expected-tuple contract");
  assert.equal(frozen.includes("PM_TRANSITION_ASSIGNMENT_HEADER"), false, "frozen client sends no authority header");
});

function routeFixture() {
  const calls: Array<Record<string, unknown>> = [];
  let callsCount = 0;
  const db = { logEvent: () => undefined, getNativeReleaseEffectReceipt: () => null } as any;
  const nativeSlotRelease = {
    release: async (request: Record<string, unknown>) => {
      callsCount += 1;
      calls.push(request);
      if (request.expected_epoch !== 883) {
        return { success: false, code: "observed_tuple_mismatch", message: "Complete owner tuple changed before release delivery.", slot: null, assignment_epoch: 883, remediation: "Re-read MoP." };
      }
      if (callsCount > 1) {
        return { success: true, code: "released", message: "already committed", slot: { slot: 3, occupied: false, assignment_epoch: 884 }, assignment_epoch: 884, remediation: null, idempotent: true };
      }
      return { success: true, code: "released", message: "Slot 3 reset and released.", slot: { slot: 3, occupied: false, assignment_epoch: 884 }, assignment_epoch: 884, remediation: null, idempotent: false };
    },
  } as any;
  const app = new Hono();
  registerFamily2Routes(app, {
    db,
    nativeSlotRelease,
    family2ReleaseEffectAdapter: {} as any,
    clearPlanApprovalTimer: () => undefined,
  });
  return { app, calls, get callsCount() { return callsCount; } };
}

const FULL_TUPLE_REQUEST = {
  slot: 3,
  expected_epoch: 883,
  expected_repository_id: "992731533",
  expected_issue: 7952,
  expected_pr: null,
  expected_branch: null,
  expected_head_sha: null,
  expected_work_kind: null,
  expected_handoff_id: null,
  expected_claimed_at: "2026-09-19T21:59:50.855Z",
  intended_main_head: "a".repeat(40),
  release_mode: "quiescent_legacy_issue_only",
};

test("RED: the stale client shape (no authority header, slot-only body) is refused before any release", async () => {
  const fixture = routeFixture();
  const stale = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ slot: 3 }),
  });
  assert.equal(stale.status, 403);
  const body = await stale.json() as Record<string, unknown>;
  assert.equal(body.code, "assignment_authority_required");
  assert.equal(body.success, false);
  assert.equal(fixture.callsCount, 0, "the stale shape must not reach the release coordinator");
});

test("GREEN: the documented contract (header + complete tuple + intended_main_head + release_mode) releases", async () => {
  const fixture = routeFixture();
  const response = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY,
    },
    body: JSON.stringify(FULL_TUPLE_REQUEST),
  });
  assert.equal(response.status, 200);
  const body = await response.json() as Record<string, unknown>;
  assert.equal(body.success, true);
  assert.equal(body.code, "released");
  assert.equal((body.slot as Record<string, unknown>).occupied, false, "authoritative post-commit readback");
  assert.equal(fixture.callsCount, 1);
  // The route maps the documented flat body onto the coordinator's request
  // shape; assert the mapped contract the client must therefore supply.
  assert.deepEqual(fixture.calls[0], {
    slot: 3,
    expected_epoch: 883,
    expected_tuple: {
      repository_id: "992731533",
      issue: 7952,
      pr: null,
      branch: null,
      head_sha: null,
      work_kind: null,
      handoff_id: null,
      claimed_at: "2026-09-19T21:59:50.855Z",
    },
    intended_main_head: "a".repeat(40),
    effect_id: undefined,
    request_digest: undefined,
    release_mode: "quiescent_legacy_issue_only",
  });
});

test("refusal and idempotency: a drifted tuple is typed, and an exact replay is idempotent", async () => {
  const fixture = routeFixture();
  const drifted = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: { "content-type": "application/json", [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY },
    body: JSON.stringify({ ...FULL_TUPLE_REQUEST, expected_epoch: 882 }),
  });
  assert.equal(drifted.status, 409);
  assert.equal(((await drifted.json()) as Record<string, unknown>).code, "observed_tuple_mismatch");

  const first = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: { "content-type": "application/json", [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY },
    body: JSON.stringify(FULL_TUPLE_REQUEST),
  });
  assert.equal(first.status, 200);
  const replay = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: { "content-type": "application/json", [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY },
    body: JSON.stringify(FULL_TUPLE_REQUEST),
  });
  assert.equal(replay.status, 200);
  const replayBody = await replay.json() as Record<string, unknown>;
  assert.equal(replayBody.success, true);
  assert.equal(replayBody.idempotent, true);
});

test("the documented skill states the same contract as the working client", () => {
  const skill = readFileSync(SKILL, "utf8");
  assert.match(skill, /x-heydonna-assignment-authority: pm-transition-v1/);
  assert.match(skill, /expected_claimed_at/);
  assert.match(skill, /intended_main_head/);
  assert.match(skill, /release_mode: "quiescent_legacy_issue_only"/);
  assert.match(skill, /mop_release_slot/);
  assert.match(skill, /authoritative readback/);
  assert.equal(skill.includes("POST http://127.0.0.1:<MOP_PORT>/slots/{slot}/release\n```"), false,
    "the bare no-header POST block must be gone");
});
