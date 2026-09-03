import assert from "node:assert/strict";
import test from "node:test";
import { Hono } from "hono";

import { registerFamily2Routes } from "../src/family2Routes.js";

test("direct release needs no authority, body, tuple, receipt, or acknowledgement", async () => {
  let releases = 0;
  let timerClears = 0;
  const released = { slot: 1, occupied: false, issue: null, task: null, assignment_epoch: 4 };
  const app = new Hono();
  registerFamily2Routes(app, {
    db: {
      releaseSlot: () => {
        releases += 1;
        return { ok: true, conflict: false, assignment_epoch: 4, idempotent: false };
      },
      getSlot: () => released,
      logEvent: () => undefined,
    } as any,
    clearPlanApprovalTimer: () => { timerClears += 1; },
  });

  const response = await app.request("http://mop/slots/1/release", { method: "POST" });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), released);
  assert.equal(releases, 1);
  assert.equal(timerClears, 1);
});

test("direct release returns the current state reason without a second effect", async () => {
  let releases = 0;
  const app = new Hono();
  registerFamily2Routes(app, {
    db: {
      releaseSlot: () => {
        releases += 1;
        return { ok: false, conflict: true, assignment_epoch: 9, idempotent: false, reason: "active_turn" };
      },
    } as any,
    clearPlanApprovalTimer: () => { throw new Error("must not clear timer"); },
  });

  const response = await app.request("http://mop/slots/1/release", { method: "POST" });
  assert.equal(response.status, 409);
  assert.equal((await response.json() as Record<string, unknown>).reason, "active_turn");
  assert.equal(releases, 1);
});

test("direct release rejects non-numbered slots before database access", async () => {
  const app = new Hono();
  registerFamily2Routes(app, {
    db: { releaseSlot: () => { throw new Error("must not reach DB"); } } as any,
    clearPlanApprovalTimer: () => undefined,
  });
  assert.equal((await app.request("http://mop/slots/0/release", { method: "POST" })).status, 400);
  assert.equal((await app.request("http://mop/slots/7/release", { method: "POST" })).status, 400);
});
