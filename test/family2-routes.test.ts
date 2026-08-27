import assert from "node:assert/strict";
import test from "node:test";
import { Hono } from "hono";

import { PM_TRANSITION_ASSIGNMENT_AUTHORITY, PM_TRANSITION_ASSIGNMENT_HEADER } from "../src/assignmentAuthority.js";
import { registerFamily2Routes } from "../src/family2Routes.js";
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
  const payload = { base_url: "http://mop", slot: 4, effect_id: "effect-7525", expected_epoch: 581, expected_session_id: "session-7525", expected_tuple: { repository_id: current.repository_id, issue: current.issue, pr: current.pr, branch: current.branch, head_sha: current.head_sha, work_kind: current.work_kind, handoff_id: current.handoff_id, claimed_at: current.claimed_at }, intended_main_head: "a".repeat(40) };
  const response = await app.request("http://mop/family2/release-effect", { method: "POST", headers: { [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY, "content-type": "application/json" }, body: JSON.stringify(payload) });
  assert.equal(response.status, 200);
  assert.equal(calls.length, 4);
  assert.equal(calls.some((call) => call.url.endsWith("/release")), true);
  assert.equal(calls.every((call) => call.init?.headers?.[PM_TRANSITION_ASSIGNMENT_HEADER] === PM_TRANSITION_ASSIGNMENT_AUTHORITY || call.url.endsWith("/slots/4")), true);
});
