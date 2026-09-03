import type { Hono } from "hono";
import { z } from "zod";

import type { MoPDatabase } from "./db.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";

const assignmentSlotParamSchema = z.coerce.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Assign one free numbered slot with the minimum PM-authored contract. */
export function registerAssignmentRoute(app: Hono, db: MoPDatabase): void {
  app.post("/slots/:slotNum/assign", async (c) => {
    const slotParse = assignmentSlotParamSchema.safeParse(c.req.param("slotNum"));
    if (!slotParse.success) {
      return c.json({ success: false, reason: "invalid_slot" }, 400);
    }

    const body = await c.req.json().catch(() => null);
    if (
      !isRecord(body)
      || !Number.isInteger(body.issue)
      || Number(body.issue) <= 0
      || typeof body.task !== "string"
      || body.task.trim() === ""
    ) {
      return c.json({ success: false, reason: "invalid_assignment" }, 400);
    }

    const repositoryId = (
      typeof body.repository_id === "string" || typeof body.repository_id === "number"
    ) ? body.repository_id : (process.env.MOP_LEGACY_REPOSITORY_ID ?? "heydonna-app/heydonna-app");
    const result = db.assignIssueToSlot(
      slotParse.data,
      Number(body.issue),
      body.task.trim(),
      repositoryId,
    );
    if (!result.ok) {
      return c.json({ success: false, reason: result.reason }, 409);
    }

    db.logEvent(slotParse.data, "slot_assigned", null, null, {
      issue: Number(body.issue),
      assignment_epoch: result.assignment_epoch,
    });
    return c.json(db.getSlot(slotParse.data));
  });
}
