import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import { MoPDatabase } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

async function withRoute(run: (app: Hono, db: MoPDatabase) => Promise<void>): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-direct-assign-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const app = new Hono();
  registerAssignmentRoute(app, db);
  try {
    await run(app, db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

function assign(app: Hono, slot: number, issue: number, task = "PM-authored task"): Promise<Response> {
  return app.request(`http://mop/slots/${slot}/assign`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ issue, task }),
  });
}

test("direct assignment accepts the minimal issue and task without an authority header", async () => {
  await withRoute(async (app, db) => {
    const response = await assign(app, 1, 7601);
    assert.equal(response.status, 200);
    const slot = await response.json() as Record<string, unknown>;
    assert.equal(slot.occupied, true);
    assert.equal(slot.issue, 7601);
    assert.equal(slot.task, "PM-authored task");
    assert.equal(slot.repository_id, "heydonna-app/heydonna-app");
    assert.equal(db.getEvents(1, 10, "slot_assigned").length, 1);
  });
});

test("direct assignment refuses malformed input and slot zero", async () => {
  await withRoute(async (app, db) => {
    assert.equal((await assign(app, 0, 7601)).status, 400);
    const malformed = await app.request("http://mop/slots/1/assign", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ issue: 7601, task: "" }),
    });
    assert.equal(malformed.status, 400);
    assert.equal(db.getSlot(1)?.occupied, false);
  });
});

test("direct assignment never overwrites an occupied slot", async () => {
  await withRoute(async (app, db) => {
    assert.equal((await assign(app, 1, 7601)).status, 200);
    const response = await assign(app, 1, 7602);
    assert.equal(response.status, 409);
    assert.equal((await response.json() as Record<string, unknown>).reason, "slot_already_occupied");
    assert.equal(db.getSlot(1)?.issue, 7601);
  });
});

test("direct assignment never takes a free slot with an active turn", async () => {
  await withRoute(async (app, db) => {
    db.startAgentTurn(1, "live-turn");
    const response = await assign(app, 1, 7601);
    assert.equal(response.status, 409);
    assert.equal((await response.json() as Record<string, unknown>).reason, "active_turn");
    assert.equal(db.getSlot(1)?.occupied, false);
  });
});

test("MCP exposes direct assign and release tools", () => {
  const source = new URL("../src/mcp.ts", import.meta.url);
  const text = readFileSync(source, "utf8");
  assert.match(text, /"mop_assign_slot"/);
  assert.match(text, /"mop_release_slot"/);
});
