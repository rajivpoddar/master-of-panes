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
 * - mop_capture_output: Capture live tmux output from a slot + busy/idle status
 * - mop_clear_slot: Clear one slot or all slots through MoP logging
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { MoPDatabase } from "./db.js";
import { TmuxRelay } from "./relay.js";
import { DEFAULT_CONFIG } from "./types.js";
import { execShell, sleep } from "./asyncCommand.js";
import type { MoPConfig } from "./types.js";

function isPmControlCommand(command: string): boolean {
  return command.trim().startsWith("/");
}

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
    "Get the current state of a specific dev slot (1-4). Returns status, task, issue, branch, DND flag, and last activity. Refreshes idle state via is-active.sh for real-time accuracy.",
    { slot: z.number().int().min(1).max(4).describe("Slot number (1-4)") },
    async ({ slot }) => {
      // Refresh idle state from is-active.sh (real-time chevron + content check)
      const isActive = await relay.isSlotActive(slot);
      db.updateSlot(slot, { idle: !isActive });

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
      // Refresh idle state for all slots from is-active.sh (real-time)
      for (let i = 1; i <= config.slotCount; i++) {
        const isActive = await relay.isSlotActive(i);
        db.updateSlot(i, { idle: !isActive });
      }

      const slots = db.getAllSlots();
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
    "Send a command or message to a slot pane. Returns success ONLY if keystrokes actually landed (pane existence + post-send content-diff verification). On failure, the response carries a reason field: pane_not_found | slot_active_force_required | dnd_no_force | delivery_unverified | tmux_exec_error. Slot 0 = PM pane. Use the message-pm skill for slot→PM communication.",
    {
      slot: z.number().int().min(0).max(4).describe("Slot number (0-4). 0 = PM pane."),
      command: z.string().describe("Command or message to send"),
      force: z.boolean().default(true).describe("Skip idle wait. Default TRUE — queued sends silently swallow during mid-tool-call windows (memory: feedback_pm_always_send_nudges_with_force.md, feedback_slot_to_pm_raw_mop_send_false_success.md). Pass force: false explicitly only when you specifically want queued behavior."),
      raw: z.boolean().default(false).describe("Send as raw tmux key sequence (e.g., Escape, BTab for Shift+Tab, C-c). No Enter appended, no mode detection."),
    },
    async ({ slot, command, force, raw }) => {
      if (slot === 0 && raw) {
        db.logEvent(slot, "send_rejected_pm_raw", null, null, {
          command: command.slice(0, 200),
          raw,
          reason: "pm_raw_send_blocked",
        });
        return {
          content: [
            {
              type: "text" as const,
              text: "✗ Refused raw send to PM pane (reason=pm_raw_send_blocked). Use message-pm with a plain message body.",
            },
          ],
        };
      }
      if (slot === 0 && isPmControlCommand(command)) {
        db.logEvent(slot, "send_rejected_pm_control_command", null, null, {
          command: command.slice(0, 200),
          force,
          raw,
          reason: "pm_control_command_blocked",
        });
        return {
          content: [
            {
              type: "text" as const,
              text: "✗ Refused PM-pane slash command (reason=pm_control_command_blocked). Use message-pm with a plain status body; hard blocks should start with ESCALATION:.",
            },
          ],
        };
      }

      const slotState = db.getSlot(slot);
      if (slotState?.dnd && !force) {
        return {
          content: [
            {
              type: "text" as const,
              text: `⚠️ Slot ${slot} (${slotState.name ?? "unknown"}) is DND. Command NOT sent.\n` +
                `Suggest: escalate to Rajiv, or use force: true to override DND.\n` +
                `To clear DND: mop_set_dnd(slot: ${slot}, dnd: false)`,
            },
          ],
        };
      }
      if (slotState?.dnd && force) {
        db.logEvent(slot, "dnd_override", null, null, {
          command: command.slice(0, 200),
          reason: "force: true used to override DND",
        });
      }

      // Guard: block /review-and-pr when slot is active (even with force)
      if (command.includes("/review-and-pr") && await relay.isSlotActive(slot)) {
        return {
          content: [
            {
              type: "text" as const,
              text: `⚠️ Slot ${slot} is ACTIVE — cannot send /review-and-pr while processing. Wait for idle notification first.`,
            },
          ],
        };
      }

      // Route ALL non-raw sends through the HTTP /slots/N/send endpoint.
      // The HTTP route does pane-existence + force-active gates and post-send
      // delivery verification (capture-pane diff). It returns success: true
      // ONLY if keystrokes actually landed in the receiving pane.
      //
      // `raw: true` keeps the legacy direct-tmux path because raw key
      // sequences (Escape, BTab, C-c) intentionally bypass mode detection
      // and don't carry user content that needs UserPromptSubmit verification.
      //
      // (Fix for feedback_mop_send_to_slot_no_false_success.md, 2026-05-05.
      //  Earlier slot=0-only fix is feedback_slot_to_pm_raw_mop_send_false_success.md.)
      if (!raw) {
        try {
          const res = await fetch(`http://localhost:3100/slots/${slot}/send`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ command, force: force === true }),
          });
          const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
          const ok = res.ok && data.success === true;
          db.logEvent(slot, "command_sent", null, null, {
            command: command.slice(0, 200),
            force,
            raw,
            success: ok,
            via: `http_slots_${slot}_send`,
            status: res.status,
            reason: data.reason,
          });
          if (ok) {
            const target = slot === 0 ? "PM (slot 0)" : `slot ${slot}`;
            return {
              content: [
                {
                  type: "text" as const,
                  text: `✓ Sent to ${target} via HTTP: ${command.slice(0, 100)}`,
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
          db.logEvent(slot, "send_error", null, null, {
            error: err?.message?.slice(0, 200),
            command: command.slice(0, 100),
            via: `http_slots_${slot}_send`,
          });
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

      // raw: true → legacy direct-tmux send (key sequences only)
      const success = await relay.sendToSlotAsync(slot, command, force, raw);
      db.logEvent(slot, "command_sent", null, null, { command, force, raw, success });

      return {
        content: [
          {
            type: "text" as const,
            text: success
              ? `✓ Sent raw keys to slot ${slot}: ${command.slice(0, 100)}`
              : `✗ Slot ${slot} raw send failed (busy or pane unreachable). Use force: true or check pane.`,
          },
        ],
      };
    }
  );

  // ─── mop_assign_slot ───────────────────────────────────

  server.tool(
    "mop_assign_slot",
    "Assign a task to a slot. Sets status to active, stores task/issue/pr/branch metadata. Optionally set a human-readable name.",
    {
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
      task: z.string().describe("Task description"),
      issue: z.number().int().nullable().default(null).describe("GitHub issue number"),
      pr: z.number().int().nullable().default(null).describe("GitHub PR number"),
      branch: z.string().nullable().default(null).describe("Git branch name"),
      session_id: z.string().nullable().default(null).describe("Claude Code session ID"),
      name: z.string().nullable().default(null).describe("Human-readable slot name (e.g., 'Rohini')"),
    },
    async ({ slot, task, issue, pr, branch, session_id, name }) => {
      db.assignSlot(slot, task, issue, branch, session_id, pr);
      if (name !== null) {
        db.updateSlot(slot, { name } as Partial<import("./types.js").SlotState>);
      }
      db.logEvent(slot, "slot_assigned", null, null, { task, issue, pr, branch, name });
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

  // ─── mop_respawn_slot ──────────────────────────────────

  server.tool(
    "mop_respawn_slot",
    "Respawn a slot: /exit at idle → launch script at shell → continue the session. Suppresses crash notifications during the orchestration. Replaces slot-side respawn.sh. Slot must be idle before calling.",
    {
      slot: z.number().int().min(0).max(4).describe("Slot number (0-4). 0 = PM pane."),
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
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
      dnd: z.boolean().describe("true to enable DND, false to clear"),
    },
    async ({ slot, dnd }) => {
      const current = db.getSlot(slot);
      if (dnd && current && !current.occupied) {
        db.updateSlot(slot, { dnd: false });
        db.logEvent(slot, "dnd_free_slot_rejected", null, null, {
          requested: true,
          reason: "free_slot_cannot_be_dnd",
        });
        return {
          content: [{
            type: "text" as const,
            text: `Slot ${slot} is free; DND request ignored and cleared so dispatch can use the slot.`,
          }],
        };
      }
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

  // ─── mop_set_exit_pending ──────────────────────────────

  server.tool(
    "mop_set_exit_pending",
    "Set or clear the exit_pending flag. When enabled, slots will receive /exit when they next go idle, allowing graceful restart (e.g., for config changes, upgrades). Watchdog auto-restarts them with --continue. Tracks which slots have cycled.",
    {
      enabled: z.boolean().describe("true to enable exit_pending, false to clear"),
    },
    async ({ enabled }) => {
      db.setExitPending(enabled);
      db.logEvent(0, enabled ? "exit_pending_enabled" : "exit_pending_disabled", null, null, {
        reason: enabled ? "PM set exit_pending — slots will /exit at next idle" : "PM cleared exit_pending flag",
      });
      const status = db.getExitStatus();
      return {
        content: [
          {
            type: "text" as const,
            text: `✓ exit_pending ${enabled ? "ENABLED" : "DISABLED"}\n${JSON.stringify(status, null, 2)}`,
          },
        ],
      };
    }
  );

  // ─── mop_exit_status ─────────────────────────────────

  server.tool(
    "mop_exit_status",
    "Check exit_pending flag status and which slots have cycled through exit. Slot 0 = PM, 1-3 = dev, 4 = QA.",
    {},
    async () => {
      const status = db.getExitStatus();
      const cycledList = Object.entries(status.cycled)
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
    }
  );

  // ─── mop_capture_output ────────────────────────────────

  server.tool(
    "mop_capture_output",
    "Capture live tmux pane output from a dev slot. Returns the last N lines of output and whether the slot is busy or idle. Use this instead of raw tmux commands to see what a slot is actually doing.",
    {
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
      lines: z.number().int().min(5).max(200).default(30).describe("Number of lines to capture (default 30)"),
    },
    async ({ slot, lines }) => {
      const { output, activity } = await relay.captureOutput(slot, lines);
      const slotState = db.getSlot(slot);
      const taskPart = slotState?.task ? ` | task: ${slotState.task}` : "";

      return {
        content: [
          {
            type: "text" as const,
            text: `[slot ${slot}: ${activity}${taskPart}]\n\n${output}`,
          },
        ],
      };
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
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
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

  const streamingSlots = new Map<number, ReturnType<typeof setInterval>>();

  server.tool(
    "mop_stream_slot",
    "Enable/disable periodic pane screenshots to a Slack thread. Posts a tmux capture every 60s while the slot is active. Stops when slot goes idle or streaming is disabled.",
    {
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
      enable: z.boolean().describe("true to start streaming, false to stop"),
      thread_ts: z.string().optional().describe("Slack thread timestamp to post screenshots to (required when enabling)"),
      channel_id: z.string().optional().describe("Slack channel ID (default: C0ALZJHGE49 #heydonna-dev)"),
      interval_seconds: z.number().optional().describe("Capture interval in seconds (default: 60)"),
    },
    async ({ slot, enable, thread_ts, channel_id, interval_seconds }) => {
      const channelId = channel_id ?? "C0ALZJHGE49";
      const intervalMs = (interval_seconds ?? 60) * 1000;

      if (!enable) {
        // Stop streaming
        const timer = streamingSlots.get(slot);
        if (timer) {
          clearInterval(timer);
          streamingSlots.delete(slot);
        }
        return {
          content: [{ type: "text" as const, text: `✓ Streaming stopped for slot ${slot}` }],
        };
      }

      if (!thread_ts) {
        return {
          content: [{ type: "text" as const, text: `✗ thread_ts required when enabling streaming` }],
        };
      }

      // Stop existing timer if any
      const existing = streamingSlots.get(slot);
      if (existing) clearInterval(existing);

      // Read Slack bot token from env
      const slackToken = process.env.SLACK_BOT_TOKEN;
      if (!slackToken) {
        // Try sourcing from .env.local
        try {
          const token = await execShell(
            `source /Users/rajiv/Downloads/projects/heydonna-app/.env.local 2>/dev/null && echo $SLACK_BOT_TOKEN`,
            { timeout: 5000 }
          );
          if (token.stdout.trim()) process.env.SLACK_BOT_TOKEN = token.stdout.trim();
        } catch { /* ignore */ }
      }

      const captureAndPost = async () => {
        try {
          // Check if slot is active
          const isActive = await relay.isSlotActive(slot);
          if (!isActive) return; // Skip idle slots

          // Use pane-screenshot.sh which does: tmux zoom → ttyd → Playwright → unzoom → Slack upload
          // Pass thread_ts so the script handles the Slack upload directly
          await execShell(
            `bash ${process.env.HOME}/.claude/skills/tmux-pane-screenshot/scripts/pane-screenshot.sh ${slot} ${thread_ts}`,
            { timeout: 30_000, env: { ...process.env, SLACK_CHANNEL: channelId } }
          );

          db.logEvent(slot, "stream_screenshot", "Timer", null, {
            thread_ts,
            channel: channelId,
          });
        } catch {
          // Silent failure — don't break the timer
        }
      };

      // Start interval
      const timer = setInterval(captureAndPost, intervalMs);
      if (timer.unref) timer.unref();
      streamingSlots.set(slot, timer);

      // Fire immediately
      captureAndPost();

      return {
        content: [{
          type: "text" as const,
          text: `✓ Streaming slot ${slot} to thread ${thread_ts} every ${interval_seconds ?? 60}s (while active)`,
        }],
      };
    }
  );

  // ─── MoP clear helpers ─────────────────────────────────
  // Clears slot contexts and releases MoP state in one logged command path.
  // Rajiv directive 2026-04-03: "we need an MoP command that clears all slots"
  // Rajiv directive 2026-06-09: PM-facing clears use mop_clear_slot for one/all slots.

  type ClearSlotResult = { slot: number; name: string; status: string };

  const formatClearResults = (results: ClearSlotResult[]): string => {
    const cleared = results.filter((r) => r.status.includes("cleared")).length;
    const queued = results.filter((r) => r.status.includes("queued")).length;
    const failed = results.filter((r) => r.status.includes("failed")).length;

    const table = results
      .map((r) => `  ${r.slot} | ${r.name.padEnd(12)} | ${r.status}`)
      .join("\n");

    const summary = [
      cleared > 0 ? `${cleared} cleared` : null,
      queued > 0 ? `${queued} queued` : null,
      failed > 0 ? `${failed} failed` : null,
    ].filter(Boolean).join(", ");

    return `Clear results (${summary}):\n\n${table}\n\nIdle slots cleared immediately. Active slots will receive /clear when they next go idle.`;
  };

  const clearSlotsThroughMop = async (
    targetSlots: number[],
    options: { clearExistingPendingForTargets: boolean; sourceTool: string },
  ): Promise<ClearSlotResult[]> => {
    const normalizedTargets = Array.from(new Set(targetSlots))
      .filter((slot) => slot >= 0 && slot <= 4);
    const results: ClearSlotResult[] = [];

    // Process dev slots (1-4) first, PM (0) last. The HTTP clear endpoint is
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
    "Compatibility wrapper for mop_clear_slot(slot: 'all'). Clears ALL slot contexts (0-4 including PM) and releases MoP state. PM should prefer mop_clear_slot for one-slot or all-slot clears. Idle dev slots receive /clear through the MoP send path. Active dev slots are queued for next idle. PM (slot 0) receives /clear through the MoP send path and is acknowledged by SessionStart:clear.",
    {
      slots: z.array(z.number().int().min(0).max(4)).optional().describe("Specific slots to clear (default: all 0-4 including PM)."),
    },
    async ({ slots: specificSlots }) => {
      const targetSlots = specificSlots ?? [0, 1, 2, 3, 4]; // Always include PM by default
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
    "Clear one slot context or all slot contexts through MoP logging. Slot '0' is PM; slots '1'-'4' are dev/QA panes; 'all' clears 0-4. Idle dev slots receive /clear through the MoP send path. Active dev slots are queued for next idle. PM gets /clear through the MoP send path and is acknowledged by SessionStart:clear.",
    {
      slot: z.string().describe("Slot to clear: '0', '1', '2', '3', '4', 'pm', or 'all'."),
    },
    async ({ slot }) => {
      const normalizedSlot = slot.trim().toLowerCase();
      const targetSlots =
        normalizedSlot === "all" ? [0, 1, 2, 3, 4] :
        normalizedSlot === "pm" ? [0] :
        /^[0-4]$/.test(normalizedSlot) ? [Number(normalizedSlot)] :
        null;

      if (!targetSlots) {
        return {
          content: [
            {
              type: "text" as const,
              text: "ERROR: slot must be one of '0', '1', '2', '3', '4', 'pm', or 'all'.",
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
