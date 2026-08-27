import assert from "node:assert/strict";
import test from "node:test";
import { Family2ReleaseEffectAdapter, consumeFamily2ReleaseEffect, type Family2ReleaseEffectRequest, type Family2ReleaseFetch } from "../src/family2ReleaseEffect.js";

const T = { repository_id: "github:heydonna-app/heydonna-app", issue: 7517, pr: 7525, branch: "test/7517-r3-pagination-proof-hardening", head_sha: "b".repeat(40), work_kind: "implementation", handoff_id: "handoff-7525", claimed_at: "2026-08-26T23:51:22.392Z" };
const baseRequest = (): Family2ReleaseEffectRequest => ({ base_url: "http://mop", slot: 4, effect_id: "effect-7525", expected_epoch: 581, expected_session_id: "session-7525", expected_tuple: T, intended_main_head: "a".repeat(40) });
function fakeTransport(): { fetch: Family2ReleaseFetch; posts: number[]; reuse: (value: Record<string, unknown>) => void } {
  let current: Record<string, unknown> = { slot: 4, occupied: true, idle: true, active_turn_state: "inactive", assignment_epoch: 581, session_id: "session-7525", ...T };
  const receipts = new Map<string, Record<string, unknown>>(); const posts: number[] = [];
  const fetch: Family2ReleaseFetch = async (input, init) => {
    const url = new URL(input); if (url.pathname.endsWith("/release-receipt")) { const r = receipts.get(url.searchParams.get("effect_id") ?? ""); return r ? ({ ok: true, status: 200, json: async () => ({ success: true, ...r }) } as any) : ({ ok: false, status: 404, json: async () => ({}) } as any); }
    if (url.pathname.endsWith("/release") && init?.method === "POST") { const body = JSON.parse(String(init.body)); if (!body.effect_id || !body.expected_session_id || !body.expected_repository_id) return { ok: false, status: 400, json: async () => ({ success: false, error: "complete release tuple is required" }) } as any; posts.push(1); current = { slot: 4, occupied: false, idle: true, active_turn_state: "inactive", assignment_epoch: 582, session_id: null }; receipts.set(body.effect_id, { effect_id: body.effect_id, request_digest: body.request_digest, slot: 4, expected_epoch: 581, released_epoch: 582, expected_session_id: body.expected_session_id, expected_tuple: T, intended_main_head: body.intended_main_head }); return { ok: true, status: 200, json: async () => ({ success: true, effect_id: body.effect_id, request_digest: body.request_digest }) } as any; }
    return { ok: true, status: 200, json: async () => current } as any;
  };
  return { fetch, posts, reuse: (value) => { current = value; } };
}

test("default Family-2 adapter uses the process fetch without startup failure", () => {
  assert.doesNotThrow(() => new Family2ReleaseEffectAdapter());
});

test("historical eight-field release body is refused while immutable exact tuple releases once", async () => {
  const transport = fakeTransport(); const adapter = new Family2ReleaseEffectAdapter(transport.fetch); const request = baseRequest();
  const legacy = await adapter.release({ ...request, effect_id: "" }); assert.equal(legacy.code, "invalid_request"); assert.equal(transport.posts.length, 0);
  const historical = await transport.fetch("http://mop/slots/4/release", { method: "POST", body: JSON.stringify({ expected_epoch: request.expected_epoch }) }); assert.equal(historical.status, 400);
  const result = await adapter.release(request); assert.equal(result.code, "released"); assert.equal(transport.posts.length, 1);
  const replay = await adapter.release(request); assert.equal(replay.code, "released"); assert.equal(replay.idempotent, true); assert.equal(transport.posts.length, 1);
});

test("delayed effect refuses reused slot before POST", async () => {
  const transport = fakeTransport(); transport.reuse({ slot: 4, occupied: true, idle: true, active_turn_state: "inactive", assignment_epoch: 582, session_id: "new-session", ...T, pr: 9000 });
  const result = await new Family2ReleaseEffectAdapter(transport.fetch).release(baseRequest()); assert.equal(result.code, "slot_not_releasable"); assert.equal(transport.posts.length, 0);
});

test("consumer binds committed outbox identity and response-loss retry consumes receipt", async () => {
  const transport = fakeTransport(); const adapter = new Family2ReleaseEffectAdapter(transport.fetch); const request = baseRequest();
  const first = await adapter.release(request); assert.equal(first.success, true);
  const consumed = await consumeFamily2ReleaseEffect(request, adapter); assert.equal(consumed.idempotent, true); assert.equal(transport.posts.length, 1);
});
