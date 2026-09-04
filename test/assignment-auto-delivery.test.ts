import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import { MoPDatabase } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

function request(issue: number, task: string): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      issue,
      repository_id: "github:heydonna-app/heydonna-app",
      task,
    }),
  };
}

async function withRoute(
  deliver: (slot: number, task: string) => Promise<boolean>,
  run: (app: Hono, db: MoPDatabase) => Promise<void>,
): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-assignment-delivery-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 6,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const app = new Hono();
  registerAssignmentRoute(app, db, deliver);
  try {
    await run(app, db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

test("one native assignment delivers the exact multiline task once", async () => {
  const deliveries: Array<[number, string]> = [];
  await withRoute(async (slot, task) => {
    deliveries.push([slot, task]);
    return true;
  }, async (app) => {
    const task = "NEW ISSUE HANDOFF\nissue contract\nworkflow chain";
    const response = await app.request("/slots/1/assign", request(7616, task));
    assert.equal(response.status, 200);
    const result = await response.json() as Record<string, unknown>;
    assert.equal(result.success, true);
    assert.equal(result.assigned, true);
    assert.equal(result.delivery_verified, true);
    assert.equal(result.assignment_epoch, 1);
    assert.deepEqual(deliveries, [[1, task]]);
  });
});

test("occupied and exact replay never deliver a second task", async () => {
  let deliveryCount = 0;
  await withRoute(async () => {
    deliveryCount += 1;
    return true;
  }, async (app) => {
    const first = await app.request("/slots/1/assign", request(7616, "first"));
    assert.equal(first.status, 200);
    const occupied = await app.request("/slots/1/assign", request(7617, "different"));
    assert.equal(occupied.status, 409);
    const replay = await app.request("/slots/1/assign", request(7616, "first"));
    assert.equal(replay.status, 409);
    assert.equal(deliveryCount, 1);
  });
});

test("delivery failure is explicit and never retried", async () => {
  let deliveryCount = 0;
  await withRoute(async () => {
    deliveryCount += 1;
    return false;
  }, async (app) => {
    const response = await app.request("/slots/1/assign", request(7616, "no false success"));
    assert.equal(response.status, 502);
    const result = await response.json() as Record<string, unknown>;
    assert.equal(result.reason, "assignment_delivery_failed");
    assert.equal((result.slot as Record<string, unknown>).occupied, true);
    assert.equal(deliveryCount, 1);
  });
});
