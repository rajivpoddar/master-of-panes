import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";
import { Hono } from "hono";

import { registerFamily2Routes } from "../src/family2Routes.js";

const REPO_ROOT = new URL("..", import.meta.url).pathname;
const MCP_JSON = `${REPO_ROOT}.mcp.json`;
const CURRENT_DIST = "/Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/current/dist/mcp.js";
const FROZEN_DIST = "/Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/dist/mcp.js";
const SKILL = `${REPO_ROOT}scripts/pm/shared-assets/claude/skills/direct-release/SKILL.md`;
const RETIRED_HEADER = "x-heydonna-assignment-authority";

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

test("the release client carries no authority header and no release mode", () => {
  const source = readFileSync(`${REPO_ROOT}src/mcp.ts`, "utf8");
  assert.match(source, /mopReleaseSlotInputShape/);
  // Thin surface: the tool takes only the slot plus an optional reason.
  assert.match(source, /reason: z\.string\(\)\.optional\(\)/);
  assert.equal(source.includes("expected_epoch"), false, "no epoch ceremony in the tool shape");
  assert.equal(source.includes("intended_main_head"), false, "no head attestation in the tool shape");
  assert.equal(source.includes("PM_TRANSITION_ASSIGNMENT"), false, "the authority header is retired");
  assert.equal(source.includes("release_mode"), false, "the mode enum is deleted");
  assert.equal(existsSync(`${REPO_ROOT}src/assignmentAuthority.ts`), false, "the authority module is deleted");

  const bundle = currentBundle();
  if (bundle !== null) {
    assert.match(bundle, /mop_release_slot/);
    assert.equal(bundle.includes("assignment_authority_required"), false);
  }
});

test("RED evidence: the frozen 1.0.0 client predates the observed-identity contract", () => {
  const frozen = existsSync(FROZEN_DIST) ? readFileSync(FROZEN_DIST, "utf8") : null;
  if (frozen === null) return;
  assert.equal(frozen.includes("intended_main_head"), false, "frozen client predates the expected-tuple contract");
});

function routeFixture() {
  const calls: Array<Record<string, unknown>> = [];
  let callsCount = 0;
  const db = { logEvent: () => undefined, getNativeReleaseEffectReceipt: () => null } as any;
  const nativeSlotRelease = {
    release: async (request: Record<string, unknown>) => {
      callsCount += 1;
      calls.push(request);
      if (callsCount > 1) {
        return { success: true, code: "released", message: "Slot 3 is already FREE; release is an idempotent no-op.", slot: { slot: 3, occupied: false, assignment_epoch: 884 }, assignment_epoch: 884, remediation: null, idempotent: true };
      }
      return { success: true, code: "released", message: "Slot 3 released.", slot: { slot: 3, occupied: false, assignment_epoch: 884 }, assignment_epoch: 884, remediation: null, idempotent: false };
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

const OBSERVED_IDENTITY_BODY = {
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
};

test("a slot-only body and a stale authority header both reach the release boundary", async () => {
  const fixture = routeFixture();
  const bare = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ slot: 3 }),
  });
  assert.equal(bare.status, 200, "the retired authority refusal must be gone");
  assert.equal(fixture.callsCount, 1);
  const stale = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: { "content-type": "application/json", [RETIRED_HEADER]: "wrong-authority" },
    body: JSON.stringify({ slot: 3 }),
  });
  assert.equal(stale.status, 200, "a stale authority header is inert");
  assert.equal(fixture.callsCount, 2);
});

test("GREEN: the documented body (observed identity, no header, no mode) releases", async () => {
  const fixture = routeFixture();
  const response = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(OBSERVED_IDENTITY_BODY),
  });
  assert.equal(response.status, 200);
  const body = await response.json() as Record<string, unknown>;
  assert.equal(body.success, true);
  assert.equal(body.code, "released");
  assert.equal((body.slot as Record<string, unknown>).occupied, false, "authoritative post-commit readback");
  assert.equal(fixture.callsCount, 1);
  // The route maps the documented flat body onto the coordinator's request
  // shape; there is no mode to carry.
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
  });
  assert.equal("release_mode" in fixture.calls[0], false, "the mode enum is deleted from the request shape");
});

test("a drifted observation is forwarded, not refused at the route", async () => {
  const fixture = routeFixture();
  const drifted = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ...OBSERVED_IDENTITY_BODY, expected_epoch: 882 }),
  });
  assert.equal(drifted.status, 200);
  assert.equal((fixture.calls[0] as { expected_epoch: number }).expected_epoch, 882);
  const replay = await fixture.app.request("http://mop/slots/3/release", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(OBSERVED_IDENTITY_BODY),
  });
  assert.equal(replay.status, 200);
  assert.equal(((await replay.json()) as Record<string, unknown>).idempotent, true);
});

test("the documented skill states the same contract as the working client", () => {
  const skill = readFileSync(SKILL, "utf8");
  assert.equal(
    new RegExp(`headers:\\s*${RETIRED_HEADER}`).test(skill),
    false,
    "the skill must stop instructing the retired header",
  );
  assert.equal(skill.includes("release_mode"), false, "the skill must stop instructing the retired mode");
  assert.match(skill, /mop release/, "the skill names the REST CLI");
  assert.equal(skill.includes("mop_release_slot"), false, "the skill must not instruct the retired MCP tool");
  assert.match(skill, /JSON \{\s*slot\s*\}/, "the documented body is the slot number");
  assert.match(skill, /always succeeds/, "the always-succeed contract must be stated");
});
