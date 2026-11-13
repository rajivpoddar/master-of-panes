import assert from "node:assert/strict";
import test from "node:test";
import { Hono } from "hono";
import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import type { MoPDatabase } from "../src/db.js";
import { isDecommissionedSlot } from "../src/slotConfig.js";

test("numbered slots are decommissioned without decommissioning PM", () => {
  assert.equal(isDecommissionedSlot(0), false);
  for (let slot = 1; slot <= 8; slot++) assert.equal(isDecommissionedSlot(slot), true);
});

test("assignment and adoption refuse before reading or changing state", async () => {
  const app = new Hono();
  registerAssignmentRoute(app, {} as MoPDatabase);
  for (let slot = 1; slot <= 8; slot++) {
    for (const route of ["assign", "adopt-issue-claim"]) {
      const response = await app.request(`/slots/${slot}/${route}`, { method: "POST" });
      assert.equal(response.status, 409);
      assert.equal((await response.json() as { reason: string }).reason, "slot_decommissioned");
    }
  }
});
