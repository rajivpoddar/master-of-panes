import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import {
  createRespawnWhenIdleTicker,
  getRespawnWhenIdle,
  registerRespawnWhenIdleRoutes,
  type RespawnResult,
} from "../src/respawnWhenIdle.js";
import { MoPDatabase } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

function fixture() {
  const directory = mkdtempSync(join(tmpdir(), "mop-respawn-when-idle-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const state = {
    live: false,
    calls: [] as number[],
    next: [] as RespawnResult[],
    pm: [] as string[],
  };
  const deps = {
    db,
    devSlots: [1, 2, 3, 4],
    isSlotActive: async () => state.live,
    respawn: async (slot: number) => {
      state.calls.push(slot);
      return state.next.shift() ?? { status: 200, body: { success: true } };
    },
    injectToPM: (t: string) => state.pm.push(t),
  };
  const app = new Hono();
  registerRespawnWhenIdleRoutes(app, deps);
  const ticker = createRespawnWhenIdleTicker(deps);
  const cleanup = () => {
    db.close?.();
    rmSync(directory, { recursive: true, force: true });
  };
  return { app, db, state, ticker, cleanup };
}

function assignBusy(db: MoPDatabase) {
  const assigned = db.assignIssueToSlot(1, 10, "fixture", "heydonna-app/heydonna-app") as {
    ok: boolean; assignment_epoch: number;
  };
  assert.equal(assigned.ok, true);
  db.startAgentTurn(1, "turn-1");
  db.updateSlot(1, { idle: false });
  return assigned.assignment_epoch;
}

const post = (app: Hono, slot: number) => app.request(`/slots/${slot}/respawn-when-idle`, { method: "POST" });

test("schedule is idempotent and persisted with the current epoch", async () => {
  const f = fixture();
  try {
    const epoch = assignBusy(f.db);
    const first = await (await post(f.app, 1)).json();
    assert.equal(first.already_pending, false);
    assert.equal(first.request.assignment_epoch, epoch);
    const second = await (await post(f.app, 1)).json();
    assert.equal(second.already_pending, true);
    assert.equal(second.request.requested_at, first.request.requested_at);
    const list = await (await f.app.request("/respawn-when-idle")).json();
    assert.equal(list.pending.length, 1);
  } finally {
    f.cleanup();
  }
});

test("rejects PM slot 0 and out-of-range slots", async () => {
  const f = fixture();
  try {
    assert.equal((await post(f.app, 0)).status, 400);
    assert.equal((await post(f.app, 9)).status, 400);
  } finally {
    f.cleanup();
  }
});

test("never fires while a turn is active, fires once at idle, keeps ownership and epoch", async () => {
  const f = fixture();
  try {
    const epoch = assignBusy(f.db);
    await post(f.app, 1);
    assert.equal(await f.ticker.tickSlot(1), "deferred");
    assert.deepEqual(f.state.calls, []);

    // Turn ends but the live pane probe still says working: still deferred.
    f.db.updateSlot(1, { idle: true, active_turn_state: "inactive", active_turn_id: null });
    f.state.live = true;
    assert.equal(await f.ticker.tickSlot(1), "deferred");
    assert.deepEqual(f.state.calls, []);

    f.state.live = false;
    assert.equal(await f.ticker.tickSlot(1), "fired");
    assert.deepEqual(f.state.calls, [1]);
    assert.equal(getRespawnWhenIdle(f.db, 1), null);
    // Fires once: a later idle tick does nothing.
    assert.equal(await f.ticker.tickSlot(1), "none");
    assert.deepEqual(f.state.calls, [1]);

    const row = f.db.getSlot(1)!;
    assert.equal(row.issue, 10);
    assert.equal(row.assignment_epoch, epoch);
  } finally {
    f.cleanup();
  }
});

test("busy / identity refusals stay pending; terminal failure clears", async () => {
  const f = fixture();
  try {
    await post(f.app, 2);
    f.state.next.push({ status: 409, body: { reason: "pane_identity_mismatch", error: "x" } });
    assert.equal(await f.ticker.tickSlot(2), "deferred");
    f.state.next.push({ status: 409, body: { reason: "slot_busy_respawn_refused" } });
    assert.equal(await f.ticker.tickSlot(2), "deferred");
    assert.ok(getRespawnWhenIdle(f.db, 2));
    f.state.next.push({ status: 504, body: { error: "Agent did not boot" } });
    assert.equal(await f.ticker.tickSlot(2), "failed");
    assert.equal(getRespawnWhenIdle(f.db, 2), null);
    assert.equal(f.state.pm.length, 1);
  } finally {
    f.cleanup();
  }
});

test("cancel clears a pending request", async () => {
  const f = fixture();
  try {
    await post(f.app, 3);
    const res = await (await f.app.request("/slots/3/respawn-when-idle", { method: "DELETE" })).json();
    assert.equal(res.cancelled, true);
    assert.equal(await f.ticker.tickSlot(3), "none");
    assert.deepEqual(f.state.calls, []);
  } finally {
    f.cleanup();
  }
});
