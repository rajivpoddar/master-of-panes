/**
 * MoP MCP Server — Exposes slot state and event log as MCP tools
 *
 * The PM (Claude Code pane 0:0.0) connects to this MCP server and gets
 * structured tools to query slot state, event history, and send commands
 * to slots — replacing fragile bash + JSON file parsing.
 *
 * Tools provided:
 * - mop_slot_status: Get a single slot's current state
 * - mop_all_slots: Get all 6 slots in one call
 * - mop_slot_history: Get recent events for a slot
 * - mop_recent_activity: Get all events in last N minutes
 * - mop_send_to_slot: Send a command to a slot
 * - mop_release_slot: Release a slot (mark free)
 * - mop_set_dnd: Set/clear DND on a slot
 * - mop_capture_output: Capture live tmux output from a slot + busy/idle status
 * - mop_clear_slot: Clear one slot or all slots through MoP logging
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { DEFAULT_CONFIG } from "./types.js";
import { sleep } from "./asyncCommand.js";
import type { MoPConfig } from "./types.js";
import { DEFAULT_DEV_SLOT_COUNT, isValidRuntimeSlot } from "./slotConfig.js";

function isPmControlCommand(command: string): boolean {
  return command.trim().startsWith("/");
}

export const mopReleaseSlotInputShape = {
  slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT).describe("Slot number (1-6)"),
  reason: z.string().optional().describe("Optional operator reason recorded in the release audit row"),
};

export async function startMcpServer(config: MoPConfig): Promise<void> {
  // REST-only: no MoPDatabase or TmuxRelay in this process. All state
  // transitions go through the HTTP coordinator. config is used for httpPort/slotCount only.

  const server = new McpServer({
    name: "master-of-panes",
    version: "0.1.0",
  });

  // ─── mop_slot_status ────────────────────────────────────

  server.tool(
    "mop_slot_status",
    "Get the authoritative hook-derived state of a specific dev slot (1-6). Returns status, task, issue, branch, DND flag, and last activity.",
    { slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT).describe("Slot number (1-6)") },
    async ({ slot }) => {
      // REST-only: no direct DB read from the MCP process. Same server path as GET /slots/:n.
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/slots/${slot}`);
        const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
        if (!res.ok) {
          return { content: [{ type: "text" as const, text: `Slot ${slot} not found` }] };
        }
        return {
          content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `ERROR: failed to reach MoP HTTP server on port ${config.httpPort}: ${err}` }] };
      }
    }
  );

  // ─── mop_all_slots ──────────────────────────────────────

  server.tool(
    "mop_all_slots",
    "Get the authoritative hook-derived status of all 6 dev slots in one call. Returns an array of slot states with a summary line.",
    {},
    async () => {
      // REST-only: same server path as GET /slots.
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/slots`);
        const data = (await res.json().catch(() => ({}))) as { slots?: Array<{ status?: string; dnd?: boolean; name?: string; slot?: number; task?: string | null }> };
        const slots = data.slots ?? [];
        const free = slots.filter((s) => s.status === "free").length;
        const active = slots.filter((s) => s.status === "active").length;
        const dnd = slots.filter((s) => s.dnd).length;
        const slotNames = slots
          .map((s) => `${s.name ?? `slot-${s.slot}`}: ${s.status}${s.dnd ? " (DND)" : ""}${s.task ? ` — ${s.task}` : ""}`)
          .join("\n");
        const summary = `${free} free, ${active} active, ${dnd} DND\n${slotNames}`;
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify({ summary, slots }, null, 2),
            },
          ],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `ERROR: failed to reach MoP HTTP server on port ${config.httpPort}: ${err}` }] };
      }
    }
  );

  // ─── mop_slot_history ───────────────────────────────────

  server.tool(
    "mop_slot_history",
    "Get recent events for a specific slot. Returns the last N events from the event log.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT).describe("Slot number (1-6)"),
      limit: z.number().int().min(1).max(200).default(20).describe("Max events to return"),
    },
    async ({ slot, limit }) => {
      // REST-only: same server path as GET /events?slot=N&limit=N.
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/events?slot=${slot}&limit=${limit}`);
        const data = (await res.json().catch(() => ({}))) as { events?: unknown };
        return {
          content: [{ type: "text" as const, text: JSON.stringify(data.events ?? [], null, 2) }],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `ERROR: failed to reach MoP HTTP server on port ${config.httpPort}: ${err}` }] };
      }
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
      // REST-only: same server path as GET /activity?minutes=N.
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/activity?minutes=${minutes}`);
        const data = (await res.json().catch(() => ({}))) as { events?: unknown[]; count?: number };
        const events = data.events ?? [];
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify({ count: data.count ?? events.length, events }, null, 2),
            },
          ],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `ERROR: failed to reach MoP HTTP server on port ${config.httpPort}: ${err}` }] };
      }
    }
  );

  // ─── mop_send_to_slot ──────────────────────────────────

  server.tool(
    "mop_send_to_slot",
    "Send a command or message to a slot pane. Returns success ONLY if keystrokes actually landed (pane existence + post-send content-diff verification). On failure, the response carries a reason field: pane_not_found | slot_active_force_required | dnd_no_force | delivery_unverified | tmux_exec_error. Slot 0 = PM pane. Use the message-pm skill for slot→PM communication.",
    {
      slot: z.number().int().min(0).max(DEFAULT_DEV_SLOT_COUNT).describe("Slot number (0-6). 0 = PM pane."),
      command: z.string().describe("Command or message to send"),
      force: z.boolean().default(true).describe("Skip idle wait. Default TRUE — queued sends silently swallow during mid-tool-call windows (memory: feedback_pm_always_send_nudges_with_force.md, feedback_slot_to_pm_raw_mop_send_false_success.md). Pass force: false explicitly only when you specifically want queued behavior."),
      raw: z.boolean().default(false).describe("Send as raw tmux key sequence (e.g., Escape, BTab for Shift+Tab, C-c). No Enter appended, no mode detection."),
    },
    async ({ slot, command, force, raw }) => {
      // REST-only: all gates (PM raw/control, DND, review-active, pane identity,
      // force/active, "2" routing, delivery verification) are enforced server-side
      // by POST /slots/:n/send. The MCP process never touches the DB or tmux.
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/slots/${slot}/send`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ command, force: force === true, raw: raw === true }),
        });
        const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
        const ok = res.ok && data.success === true;
        if (ok) {
          const target = slot === 0 ? "PM (slot 0)" : `slot ${slot}`;
          const via = (data as { via?: string }).via === "http_send_raw" ? "raw keys " : "";
          return {
            content: [
              {
                type: "text" as const,
                text: `✓ Sent ${via}to ${target} via HTTP: ${command.slice(0, 100)}`,
              },
            ],
          };
        }
        const errMsg = (data.error as string | undefined) ?? `HTTP ${res.status}`;
        const reason = (data.reason as string | undefined) ?? "unknown";
        const hint = slot === 0
          ? "If you're a dev slot trying to reach PM, use the message-pm skill instead."
          : reason === "slot_active_force_required"
            ? `Pass force: true to deliver immediately (now the default).`
            : reason === "pane_not_found"
              ? `Run /slot-boot ${slot} to bring the slot up, or check tmux session.`
              : reason === "delivery_unverified"
                ? `Keystrokes did not produce a pane-content change. The slot pane may be wedged or the TUI is dropping input.`
                : "";
        return {
          content: [
            {
              type: "text" as const,
              text: `✗ Failed to send to slot ${slot} (reason=${reason}): ${errMsg}${hint ? "\n" + hint : ""}`,
            },
          ],
        };
      } catch (err: any) {
        return {
          content: [
            {
              type: "text" as const,
              text: `✗ HTTP send to slot ${slot} failed: ${err?.message?.slice(0, 200) ?? "unknown"}. MoP HTTP server may be down — run the mop-restart skill.`,
            },
          ],
        };
      }
    }
  );

  // ─── mop_release_slot ──────────────────────────────────

  server.tool(
    "mop_release_slot",
    "Release one numbered slot. Always succeeds on a known slot: a live turn is interrupted and terminalized, then the row is freed with one audit row. An already-free slot is an idempotent success. No epoch, tuple, or head to pass.",
    mopReleaseSlotInputShape,
    async (releaseInput) => {
      try {
        const response = await fetch(`http://127.0.0.1:${config.httpPort}/slots/${releaseInput.slot}/release`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slot: releaseInput.slot, reason: releaseInput.reason ?? null }),
        });
        const releaseResult = await response.json().catch(() => ({
          success: false,
          code: "invalid_response",
          message: `MoP HTTP release returned ${response.status} without JSON.`,
        }));
        if (!response.ok || (releaseResult as { success?: boolean }).success !== true) {
          return {
            isError: true,
            content: [{ type: "text" as const, text: JSON.stringify(releaseResult, null, 2) }],
          };
        }
        return {
          content: [{ type: "text" as const, text: JSON.stringify(releaseResult, null, 2) }],
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
              remediation: "Leave the slot occupied, restore the native MoP HTTP surface, and retry from a fresh read.",
            }, null, 2),
          }],
        };
      }
    }
  );

  // ─── mop_respawn_slot ──────────────────────────────────

  server.tool(
    "mop_respawn_slot",
    "Respawn a slot: /exit at idle → launch script at shell → continue the session. Suppresses crash notifications during the orchestration. Replaces slot-side respawn.sh. Slot must be idle before calling.",
    {
      slot: z.number().int().min(0).max(DEFAULT_DEV_SLOT_COUNT).describe("Slot number (0-6). 0 = PM pane."),
      continue_session: z.boolean().default(true).describe("Use --continue flag and inject 'continue' after boot. Default true. Set false for a fresh session."),
    },
    async ({ slot, continue_session }) => {
      try {
        const res = await fetch(`http://localhost:3100/slots/${slot}/respawn`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ continue_session }),
        });
        const data = await res.json() as Record<string, unknown>;
        const success = data.success === true;
        if (success) {
          const duration = typeof data.duration_ms === "number" ? `${Math.round(data.duration_ms / 1000)}s` : "?";
          return {
            content: [
              {
                type: "text" as const,
                text: `✓ Slot ${slot} respawned in ${duration} (continue=${continue_session})`,
              },
            ],
          };
        }
        return {
          content: [
            {
              type: "text" as const,
              text: `✗ Respawn failed on slot ${slot}: ${JSON.stringify(data)}`,
            },
          ],
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text" as const,
              text: `✗ Respawn request failed: ${err}`,
            },
          ],
        };
      }
    }
  );

  // ─── mop_set_dnd ───────────────────────────────────────

  server.tool(
    "mop_set_dnd",
    "Set or clear Do Not Disturb on a slot. DND slots are skipped by hook processing.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT).describe("Slot number (1-6)"),
      dnd: z.boolean().describe("true to enable DND, false to clear"),
    },
    async ({ slot, dnd }) => {
      // REST-only: same server path as POST /slots/:n/dnd. No direct DB write.
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/slots/${slot}/dnd`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dnd }),
        });
        const data = (await res.json().catch(() => ({}))) as { dnd?: boolean; reason?: string; error?: string };
        if (!res.ok) {
          return { content: [{ type: "text" as const, text: `✗ DND update failed: ${data.error ?? `HTTP ${res.status}`}` }] };
        }
        if (data.reason === "free_slot_cannot_be_dnd") {
          return {
            content: [{
              type: "text" as const,
              text: `Slot ${slot} is free; DND request ignored and cleared so dispatch can use the slot.`,
            }],
          };
        }
        return {
          content: [
            {
              type: "text" as const,
              text: `✓ Slot ${slot} DND ${data.dnd ? "enabled" : "disabled"}`,
            },
          ],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `ERROR: failed to reach MoP HTTP server on port ${config.httpPort}: ${err}` }] };
      }
    }
  );

  // ─── mop_set_exit_pending ──────────────────────────────

  server.tool(
    "mop_set_exit_pending",
    "Set or clear the exit_pending flag. When enabled, slots will receive /exit when they next go idle, allowing graceful restart (e.g., for config changes, upgrades). Watchdog auto-restarts them with --continue. Tracks which slots have cycled.",
    {
      enabled: z.boolean().describe("true to enable exit_pending, false to clear"),
    },
    async ({ enabled }) => {
      // REST-only: same server path as POST /exit-pending. No direct DB write.
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/exit-pending`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        });
        const data = (await res.json().catch(() => ({}))) as { status?: unknown; error?: string };
        if (!res.ok) {
          return { content: [{ type: "text" as const, text: `✗ exit_pending update failed: ${data.error ?? `HTTP ${res.status}`}` }] };
        }
        return {
          content: [
            {
              type: "text" as const,
              text: `✓ exit_pending ${enabled ? "ENABLED" : "DISABLED"}\n${JSON.stringify(data.status, null, 2)}`,
            },
          ],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `ERROR: failed to reach MoP HTTP server on port ${config.httpPort}: ${err}` }] };
      }
    }
  );

  // ─── mop_exit_status ─────────────────────────────────

  server.tool(
    "mop_exit_status",
    "Check exit_pending flag status and which slots have cycled through exit. Slot 0 = PM, slots 1-6 = dev.",
    {},
    async () => {
      // REST-only: same server path as GET /exit-status. No direct DB read.
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/exit-status`);
        const status = (await res.json().catch(() => ({}))) as { pending?: boolean; cycled?: Record<string, boolean> };
        const cycledList = Object.entries(status.cycled ?? {})
          .map(([slot, done]) => `  slot ${slot}: ${done ? "✅ cycled" : "⏳ pending"}`)
          .join("\n");
        return {
          content: [
            {
              type: "text" as const,
              text: `exit_pending: ${status.pending ? "ENABLED" : "disabled"}\n\n${cycledList}`,
            },
          ],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `ERROR: failed to reach MoP HTTP server on port ${config.httpPort}: ${err}` }] };
      }
    }
  );

  // ─── mop_capture_output ────────────────────────────────

  server.tool(
    "mop_capture_output",
    "Capture live tmux pane output from a dev slot. Returns the last N lines of output and whether the slot is busy or idle. Use this instead of raw tmux commands to see what a slot is actually doing.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT).describe("Slot number (1-6)"),
      lines: z.number().int().min(5).max(200).default(30).describe("Number of lines to capture (default 30)"),
    },
    async ({ slot, lines }) => {
      // REST-only: same server path as GET /slots/:n/capture. No direct relay/DB.
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/slots/${slot}/capture?lines=${lines}`);
        const data = (await res.json().catch(() => ({}))) as { activity?: string; output?: string; task?: string | null; error?: string };
        if (!res.ok) {
          return { content: [{ type: "text" as const, text: `✗ capture failed: ${data.error ?? `HTTP ${res.status}`}` }] };
        }
        const taskPart = data.task ? ` | task: ${data.task}` : "";
        return {
          content: [
            {
              type: "text" as const,
              text: `[slot ${slot}: ${data.activity}${taskPart}]\n\n${data.output}`,
            },
          ],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `ERROR: failed to reach MoP HTTP server on port ${config.httpPort}: ${err}` }] };
      }
    }
  );

  // ─── mop_approve_plan ──────────────────────────────────
  // Wraps POST /slots/:slotNum/approve-plan — handles prompt detection,
  // retry, and verification atomically. Use this instead of mop_send_to_slot
  // for plan approvals. (Rajiv directive 2026-03-18)

  server.tool(
    "mop_approve_plan",
    "Approve or reject a slot's implementation plan. Wraps the approve-plan HTTP endpoint which handles prompt detection, retry (up to 3x), and verification. Use this instead of mop_send_to_slot for plan approvals.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT).describe("Slot number (1-6)"),
      option: z.enum(["2", "4"]).default("2").describe("2 = approve, 4 = comment/reject"),
      comment: z.string().optional().describe("Comment text when option is 4 (reject/revise)"),
    },
    async ({ slot, option, comment }) => {
      try {
        const body: Record<string, string> = { option };
        if (comment) body.comment = comment;
        const res = await fetch(`http://localhost:3100/slots/${slot}/approve-plan`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json() as Record<string, unknown>;
        const success = data.success === true;
        return {
          content: [
            {
              type: "text" as const,
              text: success
                ? `✓ Plan ${option === "2" ? "approved" : "rejected"} on slot ${slot} (attempt ${data.attempt})`
                : `✗ Plan approval failed on slot ${slot}: ${JSON.stringify(data)}`,
            },
          ],
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text" as const,
              text: `✗ approve-plan request failed: ${err}`,
            },
          ],
        };
      }
    }
  );

  // ─── mop_stream_slot ──────────────────────────────────
  // Periodic tmux pane screenshots to a Slack thread
  // Rajiv directive 2026-03-19: "Screenshots of the pane to a slack thread
  // whenever it is active, every minute or so, whenever enabled."

    server.tool(
    "mop_stream_slot",
    "DEPRECATED for REST-only: periodic screenshots are not coordinator-owned. Use one-shot capture via GET /slots/:n/capture (mop CLI `mop capture`) in a caller-side loop instead.",
    {
      slot: z.number().int().min(1).max(DEFAULT_DEV_SLOT_COUNT).describe("Slot number (1-6)"),
      enable: z.boolean().describe("true to start streaming, false to stop"),
      thread_ts: z.string().optional().describe("Slack thread timestamp (required when enabling)"),
      channel_id: z.string().optional().describe("Slack channel ID"),
      interval_seconds: z.number().optional().describe("Capture interval in seconds"),
    },
    async ({ slot }) => {
      return {
        content: [
          {
            type: "text" as const,
            text: `✗ mop_stream_slot is retired in the REST-only contract (reason=stream_retired_use_capture). Use the sanctioned path: mop capture --slot ${slot} [--lines N], or GET /slots/${slot}/capture.`,
          },
        ],
      };
    }
  );

  // ─── MoP clear helpers ─────────────────────────────────
  // Clears free slot contexts only. Occupied numbered slots must use the
  // exact acknowledged native release surface above.
  // Rajiv directive 2026-04-03: "we need an MoP command that clears all slots"
  // Rajiv directive 2026-06-09: PM-facing clears use mop_clear_slot for one/all slots.

  type ClearSlotResult = { slot: number; name: string; status: string };

  const formatClearResults = (results: ClearSlotResult[]): string => {
    const cleared = results.filter((r) => r.status.includes("cleared")).length;
    const queued = results.filter((r) => r.status.includes("queued")).length;
    const failed = results.filter((r) => r.status.includes("failed")).length;
    const refused = results.filter((r) => r.status.includes("refused")).length;

    const table = results
      .map((r) => `  ${r.slot} | ${r.name.padEnd(12)} | ${r.status}`)
      .join("\n");

    const summary = [
      cleared > 0 ? `${cleared} cleared` : null,
      queued > 0 ? `${queued} queued` : null,
      failed > 0 ? `${failed} failed` : null,
      refused > 0 ? `${refused} refused` : null,
    ].filter(Boolean).join(", ");

    return `Clear results (${summary}):\n\n${table}\n\nOccupied numbered slots are never released here; use mop_release_slot with exact native inputs.`;
  };

  const clearSlotsThroughMop = async (
    targetSlots: number[],
    options: { clearExistingPendingForTargets: boolean; sourceTool: string },
  ): Promise<ClearSlotResult[]> => {
    const normalizedTargets = Array.from(new Set(targetSlots))
      .filter((slot) => isValidRuntimeSlot(slot, config.slotCount));
    const results: ClearSlotResult[] = [];

    // Process dev slots (1-6) first, PM (0) last. The HTTP clear endpoint is
    // the single authority for clear delivery, duplicate suppression, and
    // SessionStart acknowledgement. Do not duplicate tmux injection here.
    const devSlots = normalizedTargets.filter((s) => s !== 0);
    const includePmSlot = normalizedTargets.includes(0);
    const orderedSlots = includePmSlot ? [...devSlots, 0] : devSlots;

    for (const slotNum of orderedSlots) {
      try {
        const slotLabel = slotNum === 0 ? "pm" : String(slotNum);
        const res = await fetch(`http://localhost:${config.httpPort}/slots/${slotLabel}/clear`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source: options.sourceTool,
            clear_existing_pending: options.clearExistingPendingForTargets,
          }),
        });
        const data = (await res.json().catch(() => ({}))) as {
          results?: ClearSlotResult[];
          error?: string;
        };
        if (!res.ok || !Array.isArray(data.results)) {
          results.push({
            slot: slotNum,
            name: slotNum === 0 ? "PM" : `slot-${slotNum}`,
            status: `❌ failed: ${data.error ?? `HTTP ${res.status}`}`,
          });
        } else {
          results.push(...data.results);
        }
      } catch (err) {
        results.push({
          slot: slotNum,
          name: slotNum === 0 ? "PM" : `slot-${slotNum}`,
          status: `❌ failed: ${err}`,
        });
      }
      await sleep(500);
    }

    return results;
  };

  server.tool(
    "mop_clear_all_slots",
    "Compatibility wrapper for mop_clear_slot(slot: 'all'). Clears PM/free pane contexts only; occupied numbered slots are refused here, release them with mop_release_slot.",
    {
      slots: z.array(z.number().int().min(0).max(DEFAULT_DEV_SLOT_COUNT)).optional().describe("Specific slots to clear (default: all 0-6 including PM)."),
    },
    async ({ slots: specificSlots }) => {
      const targetSlots = specificSlots ?? [0, ...Array.from({ length: config.slotCount }, (_, index) => index + 1)]; // Always include PM by default
      const results = await clearSlotsThroughMop(targetSlots, {
        clearExistingPendingForTargets: true,
        sourceTool: "mop_clear_all_slots",
      });

      return {
        content: [
          {
            type: "text" as const,
            text: formatClearResults(results),
          },
        ],
      };
    }
  );

  // ─── mop_clear_slot ────────────────────────────────────

  server.tool(
    "mop_clear_slot",
    "Clear PM or free pane contexts. Occupied numbered slots are refused here; release them with mop_release_slot, which always succeeds.",
    {
      slot: z.string().describe("Slot to clear: '0' through '6', 'pm', or 'all'."),
    },
    async ({ slot }) => {
      const normalizedSlot = slot.trim().toLowerCase();
      const targetSlots =
        normalizedSlot === "all" ? [0, ...Array.from({ length: config.slotCount }, (_, index) => index + 1)] :
        normalizedSlot === "pm" ? [0] :
        /^\d+$/.test(normalizedSlot) && isValidRuntimeSlot(Number(normalizedSlot), config.slotCount) ? [Number(normalizedSlot)] :
        null;

      if (!targetSlots) {
        return {
          content: [
            {
              type: "text" as const,
              text: "ERROR: slot must be an integer from 0 through 6, 'pm', or 'all'.",
            },
          ],
        };
      }

      const results = await clearSlotsThroughMop(targetSlots, {
        clearExistingPendingForTargets: false,
        sourceTool: "mop_clear_slot",
      });

      return {
        content: [
          {
            type: "text" as const,
            text: formatClearResults(results),
          },
        ],
      };
    }
  );

  // ─── mop_ops_audit_now ─────────────────────────────────
  // Manual trigger for the hourly ops-audit scheduler. POSTs to the HTTP
  // server which owns the in-process lock + relay queue.
  // Rajiv CTO directive 2026-05-26 thread C0ALZJHGE49/1779790681.847219.

  server.tool(
    "mop_ops_audit_now",
    "Enqueue one ops-audit tick immediately (manual bypasses pause). Returns a durable job id immediately; use mop_ops_audit_job or mop_ops_audit_status to inspect completion. Use when you suspect an exception that the next hourly tick would catch.",
    {
      reason: z
        .enum(["manual", "scheduled", "boot"])
        .default("manual")
        .describe("Trigger reason — manual bypasses pause. Default 'manual'."),
    },
    async ({ reason }) => {
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/ops-audit/run`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ reason }),
        });
        const json = (await res.json()) as { success: boolean; result?: unknown; error?: string };
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify(json, null, 2),
            },
          ],
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text" as const,
              text: `ERROR: failed to reach MoP HTTP server on port ${config.httpPort}: ${err}`,
            },
          ],
        };
      }
    }
  );

  // ─── mop_ops_audit_status ──────────────────────────────

  server.tool(
    "mop_ops_audit_status",
    "Get ops-audit scheduler status: paused flag, running flag, current job, recent jobs, bg_script presence, and legacy last-run summary.",
    {},
    async () => {
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/ops-audit/status`);
        const json = await res.json();
        return {
          content: [{ type: "text" as const, text: JSON.stringify(json, null, 2) }],
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text" as const,
              text: `ERROR: failed to reach MoP HTTP server: ${err}`,
            },
          ],
        };
      }
    }
  );

  // ─── mop_ops_audit_job ─────────────────────────────────

  server.tool(
    "mop_ops_audit_job",
    "Get one durable ops-audit job by id, including queued/running/succeeded/skipped/failed/timed_out state and stdout/trace paths.",
    {
      job_id: z.string().describe("Job id returned by mop_ops_audit_now"),
    },
    async ({ job_id }) => {
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/ops-audit/jobs/${encodeURIComponent(job_id)}`);
        const json = await res.json();
        return {
          content: [{ type: "text" as const, text: JSON.stringify(json, null, 2) }],
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text" as const,
              text: `ERROR: failed to reach MoP HTTP server: ${err}`,
            },
          ],
        };
      }
    }
  );

  // ─── mop_ops_audit_pause ───────────────────────────────

  server.tool(
    "mop_ops_audit_pause",
    "Pause or resume the hourly ops-audit scheduler. Pause persists across MoP restarts (stored in MoP SQLite config table). Manual ticks via mop_ops_audit_now still run while paused.",
    {
      paused: z.boolean().describe("true = pause, false = resume"),
    },
    async ({ paused }) => {
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/ops-audit/pause`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ paused }),
        });
        const json = await res.json();
        return {
          content: [{ type: "text" as const, text: JSON.stringify(json, null, 2) }],
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text" as const,
              text: `ERROR: failed to reach MoP HTTP server: ${err}`,
            },
          ],
        };
      }
    }
  );

  // ─── mop_pm_cadence_status ─────────────────────────────

  server.tool(
    "mop_pm_cadence_status",
    "Get MoP-owned PM cadence status for the 3h heartbeat and daily morning brief. Shows persisted last-fired bucket/day, paused flags, and whether each task is currently due.",
    {},
    async () => {
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/pm-cadence/status`);
        const json = await res.json();
        return {
          content: [{ type: "text" as const, text: JSON.stringify(json, null, 2) }],
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text" as const,
              text: `ERROR: failed to reach MoP HTTP server: ${err}`,
            },
          ],
        };
      }
    }
  );

  // ─── mop_pm_cadence_run ────────────────────────────────

  server.tool(
    "mop_pm_cadence_run",
    "Manually inject one MoP-owned PM cadence task now. Use for operator recovery; scheduled ticks are owned by MoP and persisted by due bucket/day.",
    {
      task: z.enum(["heartbeat", "morning-brief"]).describe("Which PM cadence task to inject now"),
    },
    async ({ task }) => {
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/pm-cadence/run`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ task }),
        });
        const json = await res.json();
        return {
          content: [{ type: "text" as const, text: JSON.stringify(json, null, 2) }],
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text" as const,
              text: `ERROR: failed to reach MoP HTTP server: ${err}`,
            },
          ],
        };
      }
    }
  );

  // ─── mop_pm_cadence_pause ──────────────────────────────

  server.tool(
    "mop_pm_cadence_pause",
    "Pause or resume MoP-owned PM cadence injection. Pause globally or for just heartbeat/morning-brief; persisted in MoP SQLite config.",
    {
      paused: z.boolean().describe("true = pause, false = resume"),
      task: z.enum(["heartbeat", "morning-brief"]).optional().describe("Optional specific task. Omit to pause/resume all PM cadence tasks."),
    },
    async ({ paused, task }) => {
      try {
        const res = await fetch(`http://127.0.0.1:${config.httpPort}/pm-cadence/pause`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ paused, task }),
        });
        const json = await res.json();
        return {
          content: [{ type: "text" as const, text: JSON.stringify(json, null, 2) }],
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text" as const,
              text: `ERROR: failed to reach MoP HTTP server: ${err}`,
            },
          ],
        };
      }
    }
  );

  // ─── Start Transport ───────────────────────────────────

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[mop-mcp] MCP server connected via stdio");

  // Cleanup on exit
  process.on("SIGINT", () => {
    // no local DB handle to close (REST-only).
    process.exit(0);
  });
}

// ─── Standalone Mode ─────────────────────────────────────

// When run directly: `node dist/mcp.js` or `tsx src/mcp.ts`
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
