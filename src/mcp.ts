/**
 * MoP MCP Server — Exposes slot state and event log as MCP tools
 *
 * The PM (Claude Code pane 0:0.0) connects to this MCP server and gets
 * structured tools to query slot state, event history, and send commands
 * to slots — replacing fragile bash + JSON file parsing.
 *
 * Tools provided:
 * - mop_slot_status: Get a single slot's current state
 * - mop_all_slots: Get all 4 slots in one call
 * - mop_slot_history: Get recent events for a slot
 * - mop_recent_activity: Get all events in last N minutes
 * - mop_send_to_slot: Send a command to a slot
 * - mop_assign_slot: Assign a task to a slot
 * - mop_release_slot: Release a slot (mark free)
 * - mop_set_dnd: Set/clear DND on a slot
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { MoPDatabase } from "./db.js";
import { TmuxRelay } from "./relay.js";
import { DEFAULT_CONFIG } from "./types.js";
import type { MoPConfig } from "./types.js";

export async function startMcpServer(config: MoPConfig): Promise<void> {
  const db = new MoPDatabase(config);
  const relay = new TmuxRelay(config);

  const server = new McpServer({
    name: "master-of-panes",
    version: "0.1.0",
  });

  // ─── mop_slot_status ────────────────────────────────────

  server.tool(
    "mop_slot_status",
    "Get the current state of a specific dev slot (1-4). Returns status, task, issue, branch, DND flag, and last activity.",
    { slot: z.number().int().min(1).max(4).describe("Slot number (1-4)") },
    async ({ slot }) => {
      const state = db.getSlot(slot);
      if (!state) {
        return { content: [{ type: "text" as const, text: `Slot ${slot} not found` }] };
      }
      return {
        content: [{ type: "text" as const, text: JSON.stringify(state, null, 2) }],
      };
    }
  );

  // ─── mop_all_slots ──────────────────────────────────────

  server.tool(
    "mop_all_slots",
    "Get the status of all 4 dev slots in one call. Returns an array of slot states with a summary line.",
    {},
    async () => {
      const slots = db.getAllSlots();
      const free = slots.filter((s) => s.status === "free").length;
      const active = slots.filter((s) => s.status === "active").length;
      const dnd = slots.filter((s) => s.dnd).length;
      const summary = `${free} free, ${active} active, ${dnd} DND`;

      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({ summary, slots }, null, 2),
          },
        ],
      };
    }
  );

  // ─── mop_slot_history ───────────────────────────────────

  server.tool(
    "mop_slot_history",
    "Get recent events for a specific slot. Returns the last N events from the event log.",
    {
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
      limit: z.number().int().min(1).max(200).default(20).describe("Max events to return"),
    },
    async ({ slot, limit }) => {
      const events = db.getSlotHistory(slot, limit);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(events, null, 2) }],
      };
    }
  );

  // ─── mop_recent_activity ────────────────────────────────

  server.tool(
    "mop_recent_activity",
    "Get all events across all slots in the last N minutes. Useful for status checks and heartbeat reports.",
    {
      minutes: z.number().int().min(1).max(1440).default(60).describe("Look back N minutes"),
    },
    async ({ minutes }) => {
      const events = db.getRecentActivity(minutes);
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({ count: events.length, events }, null, 2),
          },
        ],
      };
    }
  );

  // ─── mop_send_to_slot ──────────────────────────────────

  server.tool(
    "mop_send_to_slot",
    "Send a command or message to a dev slot. Uses send-to-slot.sh for reliable delivery (waits for idle, handles vim mode).",
    {
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
      command: z.string().describe("Command or message to send"),
      force: z.boolean().default(false).describe("Skip idle wait (for urgent corrections)"),
    },
    async ({ slot, command, force }) => {
      const slotState = db.getSlot(slot);
      if (slotState?.dnd) {
        return {
          content: [
            {
              type: "text" as const,
              text: `⚠️ Slot ${slot} is DND. Cannot send command. Clear DND first with mop_set_dnd.`,
            },
          ],
        };
      }

      const success = relay.sendToSlot(slot, command, force);
      db.logEvent(slot, "command_sent", null, null, { command, force, success });

      return {
        content: [
          {
            type: "text" as const,
            text: success
              ? `✓ Sent to slot ${slot}: ${command.slice(0, 100)}`
              : `✗ Failed to send to slot ${slot}`,
          },
        ],
      };
    }
  );

  // ─── mop_assign_slot ───────────────────────────────────

  server.tool(
    "mop_assign_slot",
    "Assign a task to a slot. Sets status to active, stores task/issue/branch metadata.",
    {
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
      task: z.string().describe("Task description"),
      issue: z.number().int().nullable().default(null).describe("GitHub issue number"),
      branch: z.string().nullable().default(null).describe("Git branch name"),
      session_id: z.string().nullable().default(null).describe("Claude Code session ID"),
    },
    async ({ slot, task, issue, branch, session_id }) => {
      db.assignSlot(slot, task, issue, branch, session_id);
      db.logEvent(slot, "slot_assigned", null, null, { task, issue, branch });
      const updated = db.getSlot(slot);
      return {
        content: [{ type: "text" as const, text: JSON.stringify(updated, null, 2) }],
      };
    }
  );

  // ─── mop_release_slot ──────────────────────────────────

  server.tool(
    "mop_release_slot",
    "Release a slot — mark it free, clear task/issue/branch/session metadata.",
    {
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
    },
    async ({ slot }) => {
      db.releaseSlot(slot);
      db.logEvent(slot, "slot_released", null, null, {});
      return {
        content: [{ type: "text" as const, text: `✓ Slot ${slot} released` }],
      };
    }
  );

  // ─── mop_set_dnd ───────────────────────────────────────

  server.tool(
    "mop_set_dnd",
    "Set or clear Do Not Disturb on a slot. DND slots are skipped by hook processing.",
    {
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
      dnd: z.boolean().describe("true to enable DND, false to clear"),
    },
    async ({ slot, dnd }) => {
      db.updateSlot(slot, { dnd });
      db.logEvent(slot, dnd ? "dnd_enabled" : "dnd_disabled", null, null, {});
      return {
        content: [
          {
            type: "text" as const,
            text: `✓ Slot ${slot} DND ${dnd ? "enabled" : "disabled"}`,
          },
        ],
      };
    }
  );

  // ─── Start Transport ───────────────────────────────────

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[mop-mcp] MCP server connected via stdio");

  // Cleanup on exit
  process.on("SIGINT", () => {
    db.close();
    process.exit(0);
  });
}

// ─── Standalone Mode ─────────────────────────────────────

// When run directly: `node dist/mcp.js` or `tsx src/mcp.ts`
if (process.argv[1]?.endsWith("mcp.ts") || process.argv[1]?.endsWith("mcp.js")) {
  const config: MoPConfig = {
    ...DEFAULT_CONFIG,
    dbPath: process.env.MOP_DB_PATH ?? DEFAULT_CONFIG.dbPath,
  };
  startMcpServer(config).catch(console.error);
}
