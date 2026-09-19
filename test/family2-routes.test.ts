import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { PM_TRANSITION_ASSIGNMENT_AUTHORITY, PM_TRANSITION_ASSIGNMENT_HEADER } from "../src/assignmentAuthority.js";
import { registerFamily2Routes } from "../src/family2Routes.js";
import { MoPDatabase } from "../src/db.js";
import { NativeSlotReleaseCoordinator } from "../src/slotRelease.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { Family2ReleaseEffectAdapter, type Family2ReleaseFetch } from "../src/family2ReleaseEffect.js";

function routeFixture() {
  let releaseCalls = 0;
  let receiptLookups = 0;
  const db = {
    getNativeReleaseEffectReceipt: () => { receiptLookups += 1; return null; },
    logEvent: () => undefined,
  } as any;
  const nativeSlotRelease = {
    release: async () => { releaseCalls += 1; return { success: true, idempotent: false, assignment_epoch: 4 }; },
  } as any;
  const adapter = new Family2ReleaseEffectAdapter(async () => ({
    ok: false, status: 404, json: async () => ({}),
  }) as any);
  const app = new Hono();
  registerFamily2Routes(app, {
    db,
    nativeSlotRelease,
    family2ReleaseEffectAdapter: adapter,
    clearPlanApprovalTimer: () => undefined,
  });
  return { app, get releaseCalls() { return releaseCalls; }, get receiptLookups() { return receiptLookups; } };
}

test("release and receipt routes refuse missing or wrong authority before side effects", async () => {
  for (const authority of [undefined, "wrong-authority"]) {
    const fixture = routeFixture();
    const headers = authority === undefined ? undefined : { [PM_TRANSITION_ASSIGNMENT_HEADER]: authority };
    const release = await fixture.app.request("http://mop/slots/1/release", {
      method: "POST", headers, body: "not-json",
    });
    assert.equal(release.status, 403);
    const receipt = await fixture.app.request("http://mop/slots/not-a-slot/release-receipt?effect_id=secret", { headers });
    assert.equal(receipt.status, 403);
    assert.equal(fixture.releaseCalls, 0);
    assert.equal(fixture.receiptLookups, 0);
  }
});

test("authenticated Family-2 route invokes the committed-effect consumer", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  let occupied = true;
  const current = { slot: 4, occupied: true, idle: true, active_turn_state: "inactive", assignment_epoch: 581, session_id: "session-7525", repository_id: "github:heydonna-app/heydonna-app", issue: 7517, pr: 7525, branch: "test/7517-r3-pagination-proof-hardening", head_sha: "b".repeat(40), work_kind: "implementation", handoff_id: "handoff-7525", claimed_at: "2026-08-26T23:51:22.392Z" };
  const fetch: Family2ReleaseFetch = async (input, init) => {
    calls.push({ url: input, init });
    const url = new URL(input);
    if (url.pathname.endsWith("/release-receipt")) return { ok: false, status: 404, json: async () => ({}) } as any;
    if (url.pathname.endsWith("/release") && init?.method === "POST") { occupied = false; return { ok: true, status: 200, json: async () => ({ success: true }) } as any; }
    if (url.pathname.endsWith("/slots/4")) return { ok: true, status: 200, json: async () => occupied ? current : ({ ...current, occupied: false, assignment_epoch: 582, session_id: null }) } as any;
    return { ok: false, status: 404, json: async () => ({}) } as any;
  };
  const app = new Hono();
  registerFamily2Routes(app, {
    db: {} as any,
    nativeSlotRelease: {} as any,
    family2ReleaseEffectAdapter: new Family2ReleaseEffectAdapter(fetch),
    clearPlanApprovalTimer: () => undefined,
  });
  const payload = { base_url: "http://mop", slot: 4, effect_id: "effect-7525", expected_epoch: 581, expected_tuple: { repository_id: current.repository_id, issue: current.issue, pr: current.pr, branch: current.branch, head_sha: current.head_sha, work_kind: current.work_kind, handoff_id: current.handoff_id, claimed_at: current.claimed_at }, intended_main_head: "a".repeat(40) };
  const response = await app.request("http://mop/family2/release-effect", { method: "POST", headers: { [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY, "content-type": "application/json" }, body: JSON.stringify(payload) });
  assert.equal(response.status, 200);
  assert.equal(calls.length, 4);
  assert.equal(calls.some((call) => call.url.endsWith("/release")), true);
  assert.equal(calls.every((call) => call.init?.headers?.[PM_TRANSITION_ASSIGNMENT_HEADER] === PM_TRANSITION_ASSIGNMENT_AUTHORITY || call.url.endsWith("/slots/4")), true);
});

test("authenticated no-pane route forwards the complete explicit release identity", async () => {
  let requestBody: unknown;
  const app = new Hono();
  registerFamily2Routes(app, {
    db: {} as any,
    nativeSlotRelease: {
      releaseWithoutPane: async (request: unknown) => {
        requestBody = request;
        return { success: true, code: "released", idempotent: false, assignment_epoch: 603 };
      },
    } as any,
    family2ReleaseEffectAdapter: {} as any,
    clearPlanApprovalTimer: () => undefined,
  });
  const body = {
    expected_epoch: 602,
    expected_repository_id: "992731533",
    expected_issue: 7435,
    expected_pr: null,
    expected_branch: null,
    expected_head_sha: null,
    expected_work_kind: null,
    expected_handoff_id: null,
    expected_claimed_at: "2026-08-30T02:57:58.842Z",
    expected_task: "completed task",
    checkout_path: "/tmp/checkout",
    effect_id: "no-pane-release:test",
    request_digest: "a".repeat(64),
  };
  const response = await app.request("http://mop/slots/4/release-no-pane", {
    method: "POST",
    headers: { [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  assert.equal(response.status, 200);
  assert.equal((requestBody as any).expected_task, body.expected_task);
  assert.equal((requestBody as any).checkout_path, body.checkout_path);
  assert.equal("session_id" in (requestBody as any), false);
});

test("authenticated release route forwards the explicit quiescent legacy mode", async () => {
  let requestBody: any;
  const app = new Hono();
  registerFamily2Routes(app, {
    db: {} as any,
    nativeSlotRelease: {
      release: async (request: unknown) => {
        requestBody = request;
        return { success: false, code: "quiescent_attestation_failed", idempotent: false, assignment_epoch: 614 };
      },
    } as any,
    family2ReleaseEffectAdapter: {} as any,
    clearPlanApprovalTimer: () => undefined,
  });
  const body = {
    expected_epoch: 614,
    expected_repository_id: "992731533",
    expected_issue: 7554,
    expected_pr: null,
    expected_branch: null,
    expected_head_sha: null,
    expected_work_kind: null,
    expected_handoff_id: null,
    expected_claimed_at: "2026-08-31T19:23:34.589Z",
    intended_main_head: "b".repeat(40),
    effect_id: "quiescent-release:4:614",
    request_digest: "a".repeat(64),
    release_mode: "quiescent_legacy_issue_only",
  };
  const response = await app.request("http://mop/slots/4/release", {
    method: "POST",
    headers: { [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  assert.equal(response.status, 409);
  assert.equal(requestBody.release_mode, body.release_mode);
  assert.equal(requestBody.expected_epoch, body.expected_epoch);
  assert.equal(requestBody.expected_tuple.issue, body.expected_issue);
});

test("quiescent legacy documented example body releases an idle issue-only slot", async () => {
  // THE documented operator call. Precondition (verified by the coordinator,
  // not the route): the owning checkout is already on branch main at
  // intended_main_head, clean with no unpushed commits — send the slot the
  // switch-to-main-and-pull instruction and wait for a clean attestation
  // BEFORE calling release. claimed_at carries the live value re-read
  // from MoP; the other five nullable fields are explicit nulls.
  const MAIN = "b".repeat(40);
  const REPO_ID = "github:heydonna-app/heydonna-app";
  const directory = mkdtempSync(join(tmpdir(), "mop-quiescent-route-example-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO_ID, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const live = db.getSlot(6)!;
    const coordinator = new NativeSlotReleaseCoordinator({
      db,
      resolveOwningCheckout: async () => "/tmp/mop-quiescent-route-checkout",
      deliverInstruction: async () => true,
      owningSlotIsIdle: async () => true,
      resetAndObserveCheckout: async () => { throw new Error("pane delivery must not run"); },
      observeCheckout: async () => ({ checkout_path: "/tmp/mop-quiescent-route-checkout", clean: true, unpushed_commits: [], branch: "main", head: MAIN }),
    });
    const app = new Hono();
    registerFamily2Routes(app, {
      db,
      nativeSlotRelease: coordinator,
      family2ReleaseEffectAdapter: {} as any,
      clearPlanApprovalTimer: () => undefined,
    });
    const body = {
      expected_epoch: live.assignment_epoch,
      expected_repository_id: live.repository_id,
      expected_issue: live.issue,
      expected_pr: null,
      expected_branch: null,
      expected_head_sha: null,
      expected_work_kind: null,
      expected_handoff_id: null,
      expected_claimed_at: live.claimed_at,
      intended_main_head: MAIN,
      release_mode: "quiescent_legacy_issue_only",
    };
    const response = await app.request("http://mop/slots/6/release", {
      method: "POST",
      headers: { [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY, "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    assert.equal(response.status, 200);
    const json = await response.json() as { success: boolean; code: string };
    assert.equal(json.success, true);
    assert.equal(json.code, "released");
    assert.equal(db.getSlot(6)!.occupied, false);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("release authority refusal publishes the canonical REST path and working body", async () => {
  const fixture = routeFixture();
  const response = await fixture.app.request("http://mop/slots/6/release", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ expected_epoch: 1 }),
  });
  assert.equal(response.status, 403);
  const denied = await response.json() as Record<string, unknown>;
  assert.equal(denied["success"], false);
  assert.equal(denied["code"], "assignment_authority_required");
  assert.match(String(denied["message"]), /pm-transition\.sh is retired/);
  assert.match(String(denied["message"]), /x-heydonna-assignment-authority/);
  assert.match(String(denied["message"]) + String(denied["remediation"]), /POST \/slots\/:n\/release/);
  const remediation = String(denied["remediation"]);
  for (const field of ["expected_repository_id", "expected_issue", "expected_pr:null",
      "expected_branch:null", "expected_head_sha:null", "expected_work_kind:null",
      "expected_handoff_id:null", "expected_claimed_at:<live value", "intended_main_head",
      "quiescent_legacy_issue_only"]) {
    assert.match(remediation, new RegExp(field.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), `remediation must name ${field}`);
  }
  assert.match(remediation, /switch-to-main-and-pull/);
  assert.match(remediation, /minted server-side/);
  assert.equal(fixture.releaseCalls, 0);
});
