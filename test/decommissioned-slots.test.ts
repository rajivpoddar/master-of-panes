import assert from "node:assert/strict";
import test from "node:test";
import { Hono } from "hono";
import { readFileSync } from "node:fs";
import { HookProcessor } from "../src/hooks.js";
import { TmuxRelay } from "../src/relay.js";
import { StuckDetector } from "../src/stuck.js";
import type { LogManager } from "../src/logs.js";
import type { SlotState } from "../src/types.js";
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
    for (const route of ["assign", "adopt-issue-claim", "retarget-pr"]) {
      const response = await app.request(`/slots/${slot}/${route}`, { method: "POST" });
      assert.equal(response.status, 409);
      assert.equal((await response.json() as { reason: string }).reason, "slot_decommissioned");
    }
  }
});

test("retired slots refuse relay delivery before any pane access", async () => {
  const relay = Object.create(TmuxRelay.prototype) as TmuxRelay;
  for (let slot = 1; slot <= 8; slot++) {
    assert.equal(await relay.sendToSlotAsync(slot, "/clear", true, true), false);
  }
});

test("idle-free recovery returns before reading state for a retired slot", async () => {
  const detector = new StuckDetector({} as MoPDatabase, {} as LogManager, {} as TmuxRelay);
  await detector.checkIdleFree({ slot: 1 } as SlotState);
});

test("checkout observation does not read or overwrite retired ownership", async () => {
  const processor = new HookProcessor({} as MoPDatabase, {} as TmuxRelay);
  const internal = processor as unknown as { syncObservedCheckout: (slot: number, payload: object) => Promise<void> };
  await internal.syncObservedCheckout(1, { type: "Stop", cwd: "/tmp" });
});

test("send route and queued-clear boundary check retirement before effects", () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const send = server.slice(server.indexOf('app.post("/slots/:slotNum/send"'));
  assert.ok(send.indexOf("isDecommissionedSlot(slotNum)") < send.indexOf("verifyPaneIdentity(slotNum)"));
  const hooks = readFileSync(new URL("../src/hooks.ts", import.meta.url), "utf8");
  const clear = hooks.slice(hooks.indexOf("// ─── Clear Pending Check"));
  assert.ok(clear.indexOf("isDecommissionedSlot(slotNum)") < clear.indexOf("this.db.clearPendingClear(slotNum)"));
  assert.ok(clear.indexOf("isDecommissionedSlot(slotNum)") < clear.indexOf("this.sendClearViaMopSendPath"));
});
