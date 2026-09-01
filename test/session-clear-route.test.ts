import assert from "node:assert/strict";
import test from "node:test";
import { Hono } from "hono";

import { registerSessionClearRoute } from "../src/sessionClearRoute.js";

const AUTHORITY = "pm-transition-v1";
const STARTED = "2020-01-01T00:00:00.000Z";
const NEW_STARTED = "2020-01-01T07:00:01.000Z";
const HEAD = "a".repeat(40);

function makeHarness(
  deliver: () => Promise<boolean>,
  options: { beforeResolve?: () => Promise<void>; observeCheckout?: () => Promise<any> } = {},
) {
  const state: any = {
    slot: 5,
    assignment_epoch: 18,
    occupied: false,
    dnd: false,
    idle: true,
    active_turn_id: null,
    active_turn_state: "inactive",
    session_id: "old-session",
    session_started_at: STARTED,
  };
  let intent: any = null;
  const events: unknown[] = [];
  const app = new Hono();
  registerSessionClearRoute(app, {
    db: {
      getSlot: () => state,
      getSessionClearIntent: () => intent,
      hasSessionClearIntent: () => intent !== null,
      claimSessionClearIntent: (_slot, epoch, sessionId, startedAt, token) => {
        if (intent) return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "session_clear_in_progress" };
        intent = { token, slot: 5, expected_epoch: epoch, expected_session_id: sessionId, expected_session_started_at: startedAt, created_at: STARTED };
        return { ok: true, conflict: false, assignment_epoch: epoch, idempotent: false };
      },
      markSessionClearDeliveryStarted: (_slot, epoch, sessionId, startedAt, token) => {
        if (!intent || intent.token !== token || intent.expected_epoch !== epoch || intent.expected_session_id !== sessionId || intent.expected_session_started_at !== startedAt) {
          return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "observed_tuple_mismatch" };
        }
        if (intent.delivery_started) return { ok: true, conflict: false, assignment_epoch: epoch, idempotent: true };
        intent.delivery_started = true;
        return { ok: true, conflict: false, assignment_epoch: epoch, idempotent: false };
      },
      clearSessionClearIntent: (_slot, token) => {
        if (intent?.token !== token) return false;
        intent = null;
        return true;
      },
      logEvent: (_slot, type) => { events.push(type); return events.length; },
    } as any,
    resolveCheckout: async () => {
      await options.beforeResolve?.();
      return "/checkout-3005";
    },
    observeCheckout: options.observeCheckout ?? (async () => ({ checkout_path: "/checkout-3005", clean: true, unpushed_commits: [], branch: "main", head: HEAD })),
    deliverClear: async (_slot, _beforeEffect) => {
      const delivered = await deliver();
      return { ok: delivered, effect_started: true };
    },
    now: () => Date.parse(STARTED) + 7 * 60 * 60 * 1000,
    sleep: async () => undefined,
  });
  return { app, state, getIntent: () => intent, events };
}

function requestBody(token = "clear-1") {
  return {
    expected_epoch: 18,
    expected_session_id: "old-session",
    expected_session_started_at: STARTED,
    expected_age_seconds: 7 * 60 * 60,
    checkout_path: "/checkout-3005",
    checkout_branch: "main",
    checkout_head: HEAD,
    checkout_clean: true,
    unpushed_commits: [],
    request_token: token,
  };
}

test("direct session clear delivers once and returns fresh free-session readback", async () => {
  let deliveries = 0;
  const harness = makeHarness(async () => {
    deliveries += 1;
    harness.state.session_id = "new-session";
    harness.state.session_started_at = NEW_STARTED;
    return true;
  });
  const response = await harness.app.request("http://mop/slots/5/session/clear", {
    method: "POST",
    headers: { "content-type": "application/json", "x-heydonna-assignment-authority": AUTHORITY },
    body: JSON.stringify(requestBody()),
  });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).code, "session_cleared");
  assert.equal(deliveries, 1);
  assert.deepEqual(harness.getIntent(), null);
});

test("response loss reconciles by token without a second clear", async () => {
  let deliveries = 0;
  const harness = makeHarness(async () => {
    deliveries += 1;
    harness.state.session_id = "new-session";
    harness.state.session_started_at = NEW_STARTED;
    return false;
  });
  const first = await harness.app.request("http://mop/slots/5/session/clear", {
    method: "POST", headers: { "content-type": "application/json", "x-heydonna-assignment-authority": AUTHORITY }, body: JSON.stringify(requestBody()),
  });
  assert.equal(first.status, 409);
  assert.equal((await first.json()).code, "session_clear_response_ambiguous");
  const replay = await harness.app.request("http://mop/slots/5/session/clear", {
    method: "POST", headers: { "content-type": "application/json", "x-heydonna-assignment-authority": AUTHORITY }, body: JSON.stringify(requestBody()),
  });
  assert.equal(replay.status, 200);
  assert.equal((await replay.json()).idempotent, true);
  assert.equal(deliveries, 1);
});

test("occupied, active, and unauthenticated session clear refuse before delivery", async () => {
  let deliveries = 0;
  const harness = makeHarness(async () => { deliveries += 1; return true; });
  harness.state.occupied = true;
  const body = JSON.stringify(requestBody());
  const occupied = await harness.app.request("http://mop/slots/5/session/clear", {
    method: "POST", headers: { "content-type": "application/json", "x-heydonna-assignment-authority": AUTHORITY }, body,
  });
  assert.equal(occupied.status, 409);
  harness.state.occupied = false;
  harness.state.active_turn_state = "active";
  const active = await harness.app.request("http://mop/slots/5/session/clear", {
    method: "POST", headers: { "content-type": "application/json", "x-heydonna-assignment-authority": AUTHORITY }, body,
  });
  assert.equal(active.status, 409);
  const unauthenticated = await harness.app.request("http://mop/slots/5/session/clear", {
    method: "POST", headers: { "content-type": "application/json" }, body,
  });
  assert.equal(unauthenticated.status, 403);
  assert.equal(deliveries, 0);
});

test("drift after checkout preparation refuses before any clear delivery", async () => {
  let deliveries = 0;
  let resume!: () => void;
  const paused = new Promise<void>((resolve) => { resume = resolve; });
  const harness = makeHarness(async () => { deliveries += 1; return true; }, { beforeResolve: async () => paused });
  const pending = harness.app.request("http://mop/slots/5/session/clear", {
    method: "POST", headers: { "content-type": "application/json", "x-heydonna-assignment-authority": AUTHORITY }, body: JSON.stringify(requestBody()),
  });
  await new Promise((resolve) => setImmediate(resolve));
  harness.state.active_turn_state = "active";
  resume();
  const response = await pending;
  assert.equal(response.status, 409);
  assert.equal((await response.json()).code, "session_state_drift");
  assert.equal(deliveries, 0);
});

test("dirty checkout drift refuses before any clear delivery", async () => {
  let deliveries = 0;
  const harness = makeHarness(async () => { deliveries += 1; return true; }, {
    observeCheckout: async () => ({ checkout_path: "/checkout-3005", clean: false, unpushed_commits: ["dirty"], branch: "main", head: HEAD }),
  });
  const response = await harness.app.request("http://mop/slots/5/session/clear", {
    method: "POST", headers: { "content-type": "application/json", "x-heydonna-assignment-authority": AUTHORITY }, body: JSON.stringify(requestBody()),
  });
  assert.equal(response.status, 409);
  assert.equal((await response.json()).code, "checkout_not_clean");
  assert.equal(deliveries, 0);
});
