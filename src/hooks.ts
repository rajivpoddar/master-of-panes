/**
 * MoP Hook Processor — Business logic for incoming Claude Code HTTP hooks
 *
 * Receives hook payloads, logs them, detects significant events (slot idle,
 * plan ready), updates slot state, and relays notifications to PM.
 */

import { execSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";
import type { HookPayload, HookResponse } from "./types.js";

export class HookProcessor {
  constructor(
    private db: MoPDatabase,
    private relay: TmuxRelay
  ) {}

  /**
   * Process an incoming hook from a Claude Code slot.
   * Returns a HookResponse that Claude Code will act on.
   */
  process(slotNum: number, payload: HookPayload): HookResponse {
    // Log every event
    this.db.logEvent(
      slotNum,
      payload.type,
      payload.type,
      payload.tool_name ?? null,
      payload as unknown as Record<string, unknown>
    );

    // Update last_activity
    this.db.updateSlot(slotNum, {
      last_activity: new Date().toISOString(),
    });

    // Route by hook type
    switch (payload.type) {
      case "Stop":
        return this.handleStop(slotNum, payload);
      case "PostToolUse":
        return this.handlePostToolUse(slotNum, payload);
      case "PreToolUse":
        return this.handlePreToolUse(slotNum, payload);
      case "Notification":
        return this.handleNotification(slotNum, payload);
      default:
        return {};
    }
  }

  // ─── Stop Hook ─────────────────────────────────────────

  private handleStop(slotNum: number, payload: HookPayload): HookResponse {
    const slot = this.db.getSlot(slotNum);
    if (!slot) return {};

    // Skip if DND — slot is under Rajiv's control
    if (slot.dnd) {
      this.db.logEvent(slotNum, "stop_skipped_dnd", "Stop", null, {
        reason: "slot is DND",
      });
      return {};
    }

    // Slot went idle — notify PM
    this.relay.notifySlotIdle(slot);

    this.db.logEvent(slotNum, "slot_idle_notified", "Stop", null, {
      task: slot.task,
      branch: slot.branch,
    });

    // Auto-release slot if POST-PR (a PR exists for current branch).
    // Frees the slot immediately — PM still gets notification for CI watch/labels.
    if (slot.branch && slot.branch !== "main") {
      try {
        const prNum = execSync(
          `gh pr list --head "${slot.branch}" --json number --jq '.[0].number'`,
          { timeout: 10_000 }
        ).toString().trim();

        if (prNum && prNum !== "null" && /^\d+$/.test(prNum)) {
          const stateFile = `${process.env.HOME}/.claude/tmux-panes/pane-${slotNum}.json`;
          try {
            const state = JSON.parse(readFileSync(stateFile, "utf-8"));
            state.occupied = false;
            state.status = "free";
            state.state = "FREE";
            state.pr = parseInt(prNum, 10);
            state.dnd = false;
            writeFileSync(stateFile, JSON.stringify(state, null, 2));
            this.db.logEvent(slotNum, "auto_released_post_pr", "Stop", null, {
              pr: parseInt(prNum, 10),
              branch: slot.branch,
            });
          } catch { /* pane state update failed — non-fatal */ }
        }
      } catch { /* gh pr list failed — non-fatal, PM handles manually */ }
    }

    return {};
  }

  // ─── PostToolUse Hook ──────────────────────────────────

  private handlePostToolUse(slotNum: number, payload: HookPayload): HookResponse {
    // Detect plan-ready: Write tool to a plan file
    if (
      payload.tool_name === "Write" &&
      typeof payload.tool_input === "object" &&
      payload.tool_input !== null
    ) {
      const filePath = (payload.tool_input as Record<string, string>).file_path ?? "";
      if (filePath.includes("/plans/") && filePath.endsWith(".md")) {
        // Extract issue number from slot's task field (e.g., "#1755: Fix something")
        const slot = this.db.getSlot(slotNum);
        const issueMatch = slot?.task?.match(/#(\d+)/);
        const issueNum = issueMatch ? parseInt(issueMatch[1], 10) : 0;
        const planFile = filePath.split("/").pop() ?? "plan.md";

        this.relay.notifyPlanReady(slotNum, issueNum, planFile);
        this.db.logEvent(slotNum, "plan_ready", "PostToolUse", "Write", {
          file: filePath,
          issue: issueNum,
        });
      }
    }

    return {};
  }

  // ─── PreToolUse Hook ───────────────────────────────────

  private handlePreToolUse(_slotNum: number, _payload: HookPayload): HookResponse {
    // Future: could block dangerous operations, enforce conventions
    // For now, pass-through
    return {};
  }

  // ─── Notification Hook ─────────────────────────────────

  private handleNotification(slotNum: number, payload: HookPayload): HookResponse {
    // Log notification type for queryability
    this.db.logEvent(slotNum, `notification_${payload.notification_type ?? "unknown"}`, "Notification", null, {
      notification_type: payload.notification_type,
    });
    return {};
  }
}
