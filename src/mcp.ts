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
    "Get the current state of a specific dev slot (1-4). Returns status, task, issue, branch, DND flag, and last activity. Refreshes idle state via is-active.sh for real-time accuracy.",
    { slot: z.number().int().min(1).max(4).describe("Slot number (1-4)") },
    async ({ slot }) => {
      // Refresh idle state from is-active.sh (real-time chevron + content check)
      const isActive = relay.isSlotActive(slot);
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
        const isActive = relay.isSlotActive(i);
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
      if (command.includes("/review-and-pr") && relay.isSlotActive(slot)) {
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
      const success = relay.sendToSlot(slot, command, force, raw);
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
    "Assign a task to a slot. Sets status to active, stores task/issue/branch metadata. Optionally set a human-readable name.",
    {
      slot: z.number().int().min(1).max(4).describe("Slot number (1-4)"),
      task: z.string().describe("Task description"),
      issue: z.number().int().nullable().default(null).describe("GitHub issue number"),
      branch: z.string().nullable().default(null).describe("Git branch name"),
      session_id: z.string().nullable().default(null).describe("Claude Code session ID"),
      name: z.string().nullable().default(null).describe("Human-readable slot name (e.g., 'Rohini')"),
    },
    async ({ slot, task, issue, branch, session_id, name }) => {
      db.assignSlot(slot, task, issue, branch, session_id);
      if (name !== null) {
        db.updateSlot(slot, { name } as Partial<import("./types.js").SlotState>);
      }
      db.logEvent(slot, "slot_assigned", null, null, { task, issue, branch, name });
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
      const { output, activity } = relay.captureOutput(slot, lines);
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
          const { execSync } = await import("node:child_process");
          const token = execSync(
            `source /Users/rajiv/Downloads/projects/heydonna-app/.env.local 2>/dev/null && echo $SLACK_BOT_TOKEN`,
            { timeout: 5000 }
          ).toString().trim();
          if (token) process.env.SLACK_BOT_TOKEN = token;
        } catch { /* ignore */ }
      }

      const captureAndPost = async () => {
        try {
          // Check if slot is active
          const isActive = relay.isSlotActive(slot);
          if (!isActive) return; // Skip idle slots

          const { execSync } = await import("node:child_process");

          // Use pane-screenshot.sh which does: tmux zoom → ttyd → Playwright → unzoom → Slack upload
          // Pass thread_ts so the script handles the Slack upload directly
          execSync(
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

  // ─── mop_clear_all_slots ──────────────────────────────
  // Clears all slot contexts and releases MoP state in one call.
  // Rajiv directive 2026-04-03: "we need an MoP command that clears all slots"

  server.tool(
    "mop_clear_all_slots",
    "Clear ALL slot contexts (0-4 including PM) and release MoP state. Sends /clear to idle dev slots immediately. PM (slot 0) gets /clear injected directly via tmux send-keys (paste-buffered to PM input — executes when PM finishes current turn). Non-idle dev slots are queued for next idle. Use after session export or at start of day.",
    {
      slots: z.array(z.number().int().min(0).max(4)).optional().describe("Specific slots to clear (default: all 0-4 including PM)."),
    },
    async ({ slots: specificSlots }) => {
      const targetSlots = specificSlots ?? [0, 1, 2, 3, 4]; // Always include PM by default
      const results: Array<{ slot: number; name: string; status: string }> = [];

      // Clear any stale pending clears from previous invocation
      db.clearAllPendingClears();

      // Process dev slots (1-4) first, PM (0) last with deferred approach
      const devSlots = targetSlots.filter((s) => s !== 0);
      const includePmSlot = targetSlots.includes(0);

      for (const slotNum of devSlots) {
        // Refresh idle state from is-active.sh (real-time) — same as mop_all_slots
        const isActive = relay.isSlotActive(slotNum);
        db.updateSlot(slotNum, { idle: !isActive });

        const slotState = db.getSlot(slotNum);
        const name = slotState?.name ?? `slot-${slotNum}`;
        const isIdle = slotState?.idle ?? true;

        if (isIdle) {
          // Slot is idle — clear immediately
          try {
            const paneAddress = `0:0.${slotNum}`;
            const { execSync } = await import("node:child_process");
            execSync(
              `tmux send-keys -t ${paneAddress} '/clear' Enter`,
              { timeout: 5_000 }
            );

            // Release slot state in DB
            db.releaseSlot(slotNum);

            db.logEvent(slotNum, "slot_cleared", null, null, {
              name,
              cleared_at: new Date().toISOString(),
              immediate: true,
            });

            results.push({ slot: slotNum, name, status: "✅ cleared (idle)" });
          } catch (err) {
            results.push({ slot: slotNum, name, status: `❌ failed: ${err}` });
          }
        } else {
          // Slot is active — queue for next idle
          db.setPendingClear(slotNum);
          db.logEvent(slotNum, "clear_pending_queued", null, null, {
            name,
            queued_at: new Date().toISOString(),
            reason: "Slot is active — will clear on next idle notification",
          });
          results.push({ slot: slotNum, name, status: "⏳ queued (active — will clear on next idle)" });
        }

        // Small delay between clears to avoid tmux race conditions
        await new Promise((resolve) => setTimeout(resolve, 500));
      }

      // PM pane (slot 0) — inject /clear directly via tmux send-keys.
      // tmux paste-buffers the keystrokes; PM processes them when current turn finishes.
      // Rajiv directive 2026-05-06 10:24 IST: deferred Stop-hook approach was unreliable,
      // switched to direct paste-buffer (matches dev-slot path).
      // Original Rajiv directive 2026-04-10: "use the idle notify hook instead of poll"
      if (includePmSlot) {
        try {
          const paneAddress = `0:0.0`;
          const { execSync } = await import("node:child_process");
          execSync(
            `tmux send-keys -t ${paneAddress} '/clear' Enter`,
            { timeout: 5_000 }
          );

          // Release slot state in DB
          db.releaseSlot(0);

          db.logEvent(0, "slot_cleared", null, null, {
            name: "PM",
            cleared_at: new Date().toISOString(),
            immediate: true,
            via: "tmux_paste_buffer",
          });

          results.push({ slot: 0, name: "PM", status: "✅ cleared (queued via tmux paste-buffer)" });
        } catch (err) {
          results.push({ slot: 0, name: "PM", status: `❌ failed: ${err}` });
        }
      }

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

      return {
        content: [
          {
            type: "text" as const,
            text: `Clear results (${summary}):\n\n${table}\n\nIdle slots cleared immediately. Active slots will receive /clear when they next go idle.`,
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
    "Force one ops-audit tick immediately (bypasses pause). Returns the decision (inject|skip|error), reason, elapsed_ms, payload_bytes, and whether the payload was injected into PM. Use when you suspect an exception that the next hourly tick would catch — equivalent to running ~/.claude/scripts/hourly-ops-review-bg.sh through MoP's lock + PM-busy queueing path.",
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
    "Get ops-audit scheduler status: paused flag, in-process running flag, interval_ms, bg_script presence, last_run_ts, last_run_decision (inject|skip|error), last_run_reason, last_run_elapsed_ms, last_run_payload_bytes.",
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
