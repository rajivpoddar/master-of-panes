/**
 * Core MoP MCP surface.
 *
 * This provider exposes slot visibility and explicit native operations only.
 * PM lifecycle, cadence, review, audit, screenshot streaming, and policy
 * orchestration tools are retired; direct assignment remains the guarded REST
 * boundary in assignmentRoute.ts.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { MoPDatabase } from "./db.js";
import { TmuxRelay } from "./relay.js";
import { DEFAULT_CONFIG } from "./types.js";
import type { MoPConfig } from "./types.js";
import { DEFAULT_DEV_SLOT_COUNT } from "./slotConfig.js";
import {
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "./assignmentAuthority.js";

export async function startMcpServer(config: MoPConfig): Promise<void> {
  const db = new MoPDatabase(config);
  const relay = new TmuxRelay(config);
  const server = new McpServer({ name: "master-of-panes", version: "0.1.0" });

  server.tool(
    "mop_slot_status",
    "Read one authoritative numbered-slot state.",
    { slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT) },
    async ({ slot }) => ({
      content: [{ type: "text" as const, text: JSON.stringify(db.getSlot(slot), null, 2) }],
    }),
  );

  server.tool(
    "mop_all_slots",
    "Read the authoritative numbered-slot registry.",
    {},
    async () => {
      const slots = db.getAllSlots();
      return {
        content: [{
          type: "text" as const,
          text: JSON.stringify({
            summary: {
              free: slots.filter((slot) => !slot.occupied).length,
              active: slots.filter((slot) => slot.occupied).length,
              dnd: slots.filter((slot) => slot.dnd).length,
            },
            slots,
          }, null, 2),
        }],
      };
    },
  );

  server.tool(
    "mop_slot_history",
    "Read recent events for one numbered slot.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT),
      limit: z.number().int().min(1).max(200).default(20),
    },
    async ({ slot, limit }) => ({
      content: [{ type: "text" as const, text: JSON.stringify(db.getSlotHistory(slot, limit), null, 2) }],
    }),
  );

  server.tool(
    "mop_recent_activity",
    "Read recent MoP activity without causing an effect.",
    { minutes: z.number().int().min(1).max(1440).default(60) },
    async ({ minutes }) => ({
      content: [{
        type: "text" as const,
        text: JSON.stringify({ events: db.getRecentActivity(minutes) }, null, 2),
      }],
    }),
  );

  server.tool(
    "mop_send_to_slot",
    "Deliver one explicit literal message to a numbered slot.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT),
      command: z.string().min(1),
      force: z.boolean().default(false),
    },
    async ({ slot, command, force }) => {
      const state = db.getSlot(slot);
      if (state?.dnd) {
        return { isError: true, content: [{ type: "text" as const, text: "slot is DND" }] };
      }
      const success = relay.sendToSlot(slot, command, force);
      db.logEvent(slot, success ? "command_sent" : "command_send_failed", null, null, {
        command: command.slice(0, 200),
        force,
        explicit: true,
      });
      return {
        isError: !success,
        content: [{ type: "text" as const, text: JSON.stringify({ success, slot }) }],
      };
    },
  );

  server.tool(
    "mop_release_slot",
    "Release one exact owning tuple through the native epoch/CAS boundary.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT),
      expected_epoch: z.number().int().nonnegative(),
      expected_repository_id: z.union([z.string(), z.number()]),
      expected_issue: z.number().int().positive().nullable(),
      expected_pr: z.number().int().positive().nullable(),
      expected_branch: z.string().nullable(),
      expected_head_sha: z.string().regex(/^[0-9a-f]{40}$/i).nullable(),
      expected_work_kind: z.string().nullable(),
      expected_handoff_id: z.string().nullable(),
      expected_claimed_at: z.string().min(1),
      intended_main_head: z.string().regex(/^[0-9a-f]{40}$/i),
    },
    async (input) => {
      try {
        const response = await fetch(
          `http://127.0.0.1:${config.httpPort}/slots/${input.slot}/release`,
          {
            method: "POST",
            headers: {
              "content-type": "application/json",
              [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY,
            },
            body: JSON.stringify(input),
          },
        );
        const payload = await response.json().catch(() => ({
          success: false,
          code: "invalid_response",
          message: `release returned HTTP ${response.status}`,
        }));
        return {
          isError: !response.ok || (payload as { success?: boolean }).success !== true,
          content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }],
        };
      } catch (error) {
        return {
          isError: true,
          content: [{
            type: "text" as const,
            text: JSON.stringify({
              success: false,
              code: "release_service_unavailable",
              message: error instanceof Error ? error.message : String(error),
            }),
          }],
        };
      }
    },
  );

  server.tool(
    "mop_respawn_slot",
    "Explicitly respawn one idle slot through the native REST operation.",
    {
      slot: z.number().int().min(0).max(DEFAULT_DEV_SLOT_COUNT),
      continue_session: z.boolean().default(true),
      model: z.string().optional(),
    },
    async ({ slot, continue_session, model }) => {
      const response = await fetch(
        `http://127.0.0.1:${config.httpPort}/slots/${slot}/respawn`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ continue_session, model }),
        },
      );
      const payload = await response.json().catch(() => ({ success: false, code: "invalid_response" }));
      return {
        isError: !response.ok || (payload as { success?: boolean }).success !== true,
        content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }],
      };
    },
  );

  server.tool(
    "mop_set_dnd",
    "Set or clear DND on a numbered slot.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT),
      dnd: z.boolean(),
    },
    async ({ slot, dnd }) => {
      const current = db.getSlot(slot);
      if (dnd && current && !current.occupied) {
        db.updateSlot(slot, { dnd: false });
        return { content: [{ type: "text" as const, text: "free slots cannot be DND" }] };
      }
      db.updateSlot(slot, { dnd });
      db.logEvent(slot, dnd ? "dnd_enabled" : "dnd_disabled", null, null, { explicit: true });
      return { content: [{ type: "text" as const, text: JSON.stringify(db.getSlot(slot), null, 2) }] };
    },
  );

  server.tool(
    "mop_capture_output",
    "Read the current tmux output for one numbered slot.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT),
      lines: z.number().int().min(5).max(200).default(30),
    },
    async ({ slot, lines }) => {
      const capture = await relay.captureOutput(slot, lines);
      return {
        content: [{
          type: "text" as const,
          text: `[slot ${slot}: ${capture.activity}]\n\n${capture.output}`,
        }],
      };
    },
  );

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[mop-mcp] core MCP server connected");

  process.on("SIGINT", () => {
    db.close();
    process.exit(0);
  });
}
if (process.argv[1]?.endsWith("mcp.ts") || process.argv[1]?.endsWith("mcp.js")) {
  const config: MoPConfig = {
    ...DEFAULT_CONFIG,
    dbPath: process.env.MOP_DB_PATH ?? DEFAULT_CONFIG.dbPath,
    legacyRepositoryId:
      process.env.MOP_LEGACY_REPOSITORY_ID
      ?? DEFAULT_CONFIG.legacyRepositoryId,
  };
  startMcpServer(config).catch(console.error);
}
