import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";
import Database from "better-sqlite3";

import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import { MoPDatabase } from "../src/db.js";
import { registerFamily2Routes } from "../src/family2Routes.js";
import type { MoPConfig } from "../src/types.js";

test("direct assign and release need no authority, tuple, receipt, or acknowledgement", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-simple-lifecycle-"));
  const dbPath = join(directory, "mop.db");
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath,
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const app = new Hono();
  registerAssignmentRoute(app, db);
  registerFamily2Routes(app, {
    db,
    clearPlanApprovalTimer: () => undefined,
  });

  try {
    const assigned = await app.request("http://mop/slots/1/assign", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ issue: 7599, task: "Investigate issue 7599" }),
    });
    assert.equal(assigned.status, 200);
    const occupied = await assigned.json() as Record<string, unknown>;
    assert.equal(occupied.occupied, true);
    assert.equal(occupied.issue, 7599);
    assert.equal(occupied.task, "Investigate issue 7599");

    const overwrite = await app.request("http://mop/slots/1/assign", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ issue: 7600, task: "Do not overwrite" }),
    });
    assert.equal(overwrite.status, 409);
    assert.equal((await overwrite.json() as Record<string, unknown>).reason, "slot_already_occupied");
    assert.equal(db.getSlot(1)?.issue, 7599);

    db.startAgentTurn(1, "active-turn");
    const activeRelease = await app.request("http://mop/slots/1/release", { method: "POST" });
    assert.equal(activeRelease.status, 409);
    assert.equal((await activeRelease.json() as Record<string, unknown>).reason, "active_turn");
    assert.equal(db.getSlot(1)?.occupied, true);

    db.finishAgentTurn(1, "active-turn");
    db.updateSlot(1, { idle: true, activity: "waiting_for_pm_direction" });
    const released = await app.request("http://mop/slots/1/release", { method: "POST" });
    assert.equal(released.status, 200);
    const free = await released.json() as Record<string, unknown>;
    assert.equal(free.occupied, false);
    assert.equal(free.issue, null);
    assert.equal(free.task, null);
    const sqlite = new Database(dbPath, { readonly: true });
    try {
      const receiptCount = sqlite.prepare("SELECT COUNT(*) AS count FROM native_release_effect_receipts").get() as { count: number };
      assert.equal(receiptCount.count, 0);
    } finally {
      sqlite.close();
    }
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
