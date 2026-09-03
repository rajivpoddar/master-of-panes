import type { Hono } from "hono";
import { z } from "zod";

import type { MoPDatabase } from "./db.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

const slotParamSchema = z.coerce.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT);

export interface Family2RouteDependencies {
  db: MoPDatabase;
  clearPlanApprovalTimer: (slot: number) => void;
}

/** Release one quiescent numbered slot with one direct MoP mutation. */
export function registerFamily2Routes(
  app: Hono,
  dependencies: Family2RouteDependencies,
): void {
  app.post("/slots/:slotNum/release", (c) => {
    const slotParse = slotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ success: false, reason: "invalid_slot" }, 400);
    }

    const result = dependencies.db.releaseSlot(slotParse.data);
    if (!result.ok) {
      return c.json({ success: false, reason: result.reason }, 409);
    }
    if (!result.idempotent) {
      dependencies.clearPlanApprovalTimer(slotParse.data);
      dependencies.db.logEvent(slotParse.data, "slot_released", null, null, {
        assignment_epoch: result.assignment_epoch,
      });
    }
    return c.json(dependencies.db.getSlot(slotParse.data));
  });
}
