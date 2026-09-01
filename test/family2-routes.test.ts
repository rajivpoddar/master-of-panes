import assert from "node:assert/strict";
import test from "node:test";
import { Hono } from "hono";

import { PM_TRANSITION_ASSIGNMENT_AUTHORITY, PM_TRANSITION_ASSIGNMENT_HEADER } from "../src/assignmentAuthority.js";
import { registerFamily2Routes } from "../src/family2Routes.js";

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
  const app = new Hono();
  registerFamily2Routes(app, {
    db,
    nativeSlotRelease,
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
