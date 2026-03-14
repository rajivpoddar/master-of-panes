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

// ─── Activity Classification ──────────────────────────────

/**
 * Classify a Bash command into an activity type.
 * Returns null for unrecognized commands (most commands are noise).
 */
function classifyBashCommand(cmd: string): string | null {
  // Test patterns (most specific first)
  if (/vitest\s+run|bun\s+run\s+test|npx\s+vitest/.test(cmd)) return "testing";
  if (/tsc\s+--noEmit/.test(cmd)) return "type_checking";
  if (/bun\s+lint|eslint/.test(cmd)) return "linting";
  if (/git\s+commit/.test(cmd)) return "committing";
  if (/git\s+push/.test(cmd)) return "pushing";
  if (/git\s+(checkout|branch|switch)/.test(cmd)) return "branching";
  if (/gh\s+pr\s+create/.test(cmd)) return "creating_pr";
  if (/modal\s+deploy/.test(cmd)) return "deploying_modal";
  if (/npx\s+convex\s+deploy/.test(cmd)) return "deploying_convex";
  if (/sg\s+--lang/.test(cmd)) return "exploring";
  return null;
}

export class HookProcessor {
  /**
   * Pending plan-ready notifications, keyed by slot number.
   * PostToolUse stores here; Stop handler sends to PM once the prompt renders.
   * This prevents the race where PM sends "2" before the slot shows the prompt.
   */
  private pendingPlanReady = new Map<number, { issueNum: number; planFile: string }>();

  /**
   * Plan approval timeout timers, keyed by slot number.
   * Started when plan-ready notification is sent to PM; cleared on ExitPlanMode
   * or slot release. If 15 minutes elapse with no approval, re-sends notification.
   * Rajiv directive 2026-03-13: "no slot should be stuck on plan-ready for more than 15m."
   */
  private planApprovalTimers = new Map<number, ReturnType<typeof setTimeout>>();

  /** Plan approval timeout duration: 15 minutes */
  private static readonly PLAN_APPROVAL_TIMEOUT_MS = 15 * 60 * 1000;

  constructor(
    private db: MoPDatabase,
    private relay: TmuxRelay
  ) {}

  /**
   * Start a 15m timer for plan approval. If PM doesn't respond, re-notify.
   * Clears any existing timer for this slot first.
   */
  private startPlanApprovalTimer(slotNum: number, issueNum: number): void {
    this.clearPlanApprovalTimer(slotNum);

    const timer = setTimeout(() => {
      this.planApprovalTimers.delete(slotNum);

      // Check if slot is still awaiting plan approval
      const slot = this.db.getSlot(slotNum);
      if (slot?.activity === "awaiting_plan_approval") {
        this.relay.notifyPlanApprovalNeeded(slotNum, issueNum);
        this.db.logEvent(slotNum, "plan_approval_timeout", "Timer", null, {
          issue: issueNum,
          timeout_ms: HookProcessor.PLAN_APPROVAL_TIMEOUT_MS,
          reason: "15m elapsed without PM approval — re-sending notification",
        });

        // Restart the timer for another 15m (recurring until resolved)
        this.startPlanApprovalTimer(slotNum, issueNum);
      }
    }, HookProcessor.PLAN_APPROVAL_TIMEOUT_MS);

    // Ensure timer doesn't prevent process exit
    if (timer.unref) timer.unref();

    this.planApprovalTimers.set(slotNum, timer);
    this.db.logEvent(slotNum, "plan_approval_timer_started", "Timer", null, {
      issue: issueNum,
      timeout_ms: HookProcessor.PLAN_APPROVAL_TIMEOUT_MS,
    });
  }

  /**
   * Clear plan approval timer for a slot (approval received or slot released).
   * Public so server.ts can call it on slot release via MCP/HTTP.
   */
  clearPlanApprovalTimer(slotNum: number): void {
    const existing = this.planApprovalTimers.get(slotNum);
    if (existing) {
      clearTimeout(existing);
      this.planApprovalTimers.delete(slotNum);
    }
  }

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

    // Update last_activity and mark as busy (any hook = slot is working)
    this.db.updateSlot(slotNum, {
      last_activity: new Date().toISOString(),
      idle: payload.type === "Stop", // Stop marks idle; everything else marks busy
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

    // Check if slot was awaiting plan approval — the prompt is now visible.
    if (slot.activity === "awaiting_plan_approval") {
      const pending = this.pendingPlanReady.get(slotNum);
      if (pending) {
        // First Stop after plan was written — prompt is now rendered.
        // Send the full /plan-ready notification to PM (deferred from PostToolUse).
        this.pendingPlanReady.delete(slotNum);
        this.relay.notifyPlanReady(slotNum, pending.issueNum, pending.planFile);
        this.db.logEvent(slotNum, "plan_ready_deferred_sent", "Stop", null, {
          issue: pending.issueNum,
          planFile: pending.planFile,
          reason: "Plan prompt now visible — deferred notification sent to PM",
        });

        // Start 15m watchdog — if PM doesn't approve, re-notify
        this.startPlanApprovalTimer(slotNum, pending.issueNum);
      } else {
        // No pending = PM was already notified. This is a re-display
        // (e.g., post-compaction). Send reminder to re-approve.
        this.db.logEvent(slotNum, "plan_approval_still_pending", "Stop", null, {
          task: slot.task,
          issue: slot.issue,
          reason: "Stop hook fired while awaiting_plan_approval — plan prompt re-displayed (likely post-compaction)",
        });
        this.relay.notifyPlanApprovalNeeded(slotNum, slot.issue ?? 0);
      }
      // Don't clear activity — slot is still awaiting approval
      return {};
    }

    // Slot went idle — clear activity, cancel any pending plan timer
    this.clearPlanApprovalTimer(slotNum);
    this.db.updateSlot(slotNum, { activity: null });

    // ─── Exit Pending Check ────────────────────────────────
    // If exit_pending is set, send /exit to gracefully terminate the slot
    // instead of normal idle notification. Watchdog will restart it.
    // Guard: skip if this slot already cycled (prevents restart loops after
    // watchdog restarts the slot with --continue and it goes idle again).
    if (this.db.getExitPending() && !this.db.getExitStatus().cycled[slotNum]) {
      this.relay.sendToSlot(slotNum, "/exit", true);
      this.db.markSlotExitCycled(slotNum);
      this.db.logEvent(slotNum, "exit_pending_triggered", "Stop", null, {
        task: slot.task,
        branch: slot.branch,
        reason: "exit_pending flag set — sending /exit for graceful restart",
      });
      this.relay.injectToPM(
        `# 🔄 slot ${slotNum} sent /exit (exit_pending) — watchdog will restart`,
      );

      // Check if all slots have cycled — if so, auto-clear the flag
      const status = this.db.getExitStatus();
      const allCycled = Object.entries(status.cycled)
        .every(([, v]) => v);
      if (allCycled) {
        this.db.setExitPending(false);
        this.relay.injectToPM(
          "# ✅ All slots have cycled — exit_pending auto-cleared",
        );
        this.db.logEvent(slotNum, "exit_pending_complete", "Stop", null, {
          reason: "All slots (0-4) have cycled — flag cleared",
        });
      }

      return {};
    }

    // Normal idle flow — notify PM
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
    // Detect plan-ready: Write or Edit tool to a plan file
    if (
      (payload.tool_name === "Write" || payload.tool_name === "Edit") &&
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

        // Defer notification — slot hasn't rendered the plan prompt yet.
        // Store details; the Stop handler will notify PM once the prompt appears.
        this.pendingPlanReady.set(slotNum, { issueNum, planFile });
        this.db.updateSlot(slotNum, { activity: "awaiting_plan_approval" });
        this.db.logEvent(slotNum, "plan_ready_deferred", "PostToolUse", payload.tool_name, {
          file: filePath,
          issue: issueNum,
          reason: "Notification deferred to Stop hook — prompt not yet visible",
        });

        // Auto-assign slot if not already occupied (handles handoff agent failure)
        if (!slot?.occupied && issueNum > 0) {
          const taskLabel = slot?.task || `#${issueNum}`;
          this.db.assignSlot(slotNum, taskLabel, issueNum, null, null);
          this.db.logEvent(slotNum, "auto_assigned_plan_ready", "PostToolUse", payload.tool_name, {
            issue: issueNum,
            reason: "plan-ready detected but slot not occupied — handoff agent likely failed to assign",
          });
        }
      }
    }

    // ─── Activity Tracking ─────────────────────────────────
    // Classify what the slot is doing based on tool usage

    if (payload.tool_name === "Bash") {
      const cmd = (payload.tool_input as Record<string, string>)?.command ?? "";
      const activity = classifyBashCommand(cmd);
      if (activity) {
        this.db.updateSlot(slotNum, { activity });
        this.db.logEvent(slotNum, `activity_${activity}`, "PostToolUse", "Bash", {
          command: cmd.slice(0, 200),
          activity,
        });
      }
    }

    if (payload.tool_name === "Write" || payload.tool_name === "Edit") {
      this.db.updateSlot(slotNum, { activity: "coding" });
    }

    if (payload.tool_name === "Read" || payload.tool_name === "Glob" || payload.tool_name === "Grep") {
      this.db.updateSlot(slotNum, { activity: "exploring" });
    }

    // ─── Escalation Detection ────────────────────────────────
    // When a slot invokes /escalate skill, detect it and notify PM immediately

    if (payload.tool_name === "Skill") {
      const skillName = (payload.tool_input as Record<string, string>)?.skill ?? "";
      if (skillName === "escalate" || skillName.includes("escalate")) {
        const slot = this.db.getSlot(slotNum);
        const args = (payload.tool_input as Record<string, string>)?.args ?? "";
        const issueMatch = slot?.task?.match(/#(\d+)/);
        const issueNum = issueMatch ? parseInt(issueMatch[1], 10) : 0;

        this.relay.notifyEscalation(slotNum, issueNum, args);
        this.db.logEvent(slotNum, "escalation", "PostToolUse", "Skill", {
          issue: issueNum,
          description: args.slice(0, 500),
          task: slot?.task,
        });
      }
    }

    // ─── Agent Completion Detection ────────────────────────
    // When Agent tool returns, a background subagent has completed

    if (payload.tool_name === "Agent" && payload.tool_output) {
      this.db.logEvent(slotNum, "subagent_completed", "PostToolUse", "Agent", {
        output_preview: (payload.tool_output ?? "").slice(0, 500),
      });
      this.relay.notifySubagentComplete(slotNum);
    }

    // ─── Plan Mode Detection ─────────────────────────────────
    // EnterPlanMode/ExitPlanMode indicate slot state transitions.
    // Ensures MoP state reflects planning vs implementing.

    if (payload.tool_name === "EnterPlanMode") {
      const slot = this.db.getSlot(slotNum);
      this.db.updateSlot(slotNum, { activity: "planning" });
      // Auto-assign if slot is working but not yet marked occupied
      if (!slot?.occupied) {
        const taskLabel = slot?.task || "planning";
        const issueMatch = slot?.task?.match(/#(\d+)/);
        const issueNum = issueMatch ? parseInt(issueMatch[1], 10) : null;
        this.db.assignSlot(slotNum, taskLabel, issueNum, null, null);
        this.db.logEvent(slotNum, "auto_assigned_enter_plan", "PostToolUse", "EnterPlanMode", {
          issue: issueNum,
          reason: "EnterPlanMode detected but slot not occupied",
        });
      }
    }

    if (payload.tool_name === "ExitPlanMode") {
      this.clearPlanApprovalTimer(slotNum);
      this.db.updateSlot(slotNum, { activity: "implementing" });
      this.db.logEvent(slotNum, "exit_plan_mode", "PostToolUse", "ExitPlanMode", {});
    }

    // ─── AskUserQuestion Detection ──────────────────────────
    // When a slot asks the user a question, it's actively working.
    // Ensures MoP state reflects the slot is occupied and waiting.

    if (payload.tool_name === "AskUserQuestion") {
      const slot = this.db.getSlot(slotNum);
      this.db.updateSlot(slotNum, { activity: "waiting_for_input" });
      // Auto-assign if slot is asking questions but not marked occupied
      if (!slot?.occupied) {
        const taskLabel = slot?.task || "awaiting input";
        const issueMatch = slot?.task?.match(/#(\d+)/);
        const issueNum = issueMatch ? parseInt(issueMatch[1], 10) : null;
        this.db.assignSlot(slotNum, taskLabel, issueNum, null, null);
        this.db.logEvent(slotNum, "auto_assigned_ask_user", "PostToolUse", "AskUserQuestion", {
          issue: issueNum,
          reason: "AskUserQuestion detected but slot not occupied",
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
    const notifType = payload.notification_type ?? "unknown";

    // Log notification type for queryability
    this.db.logEvent(slotNum, `notification_${notifType}`, "Notification", null, {
      notification_type: notifType,
    });

    // Handle autocompact — slot is losing context
    if (notifType === "autocompact") {
      const slot = this.db.getSlot(slotNum);

      // Persist critical state snapshot before memory loss
      this.db.logEvent(slotNum, "pre_compact", "Notification", null, {
        task: slot?.task,
        issue: slot?.issue,
        branch: slot?.branch,
        activity: slot?.activity,
        context_warning: true,
      });

      // If slot is mid-task with no PR yet, warn PM
      if (slot?.occupied && !slot.pr && !slot.dnd) {
        const issuePart = slot.issue ? ` | #${slot.issue}` : "";
        const activityPart = slot.activity ? ` | ${slot.activity}` : "";
        const approvalNote = slot.activity === "awaiting_plan_approval"
          ? " ⏳ WAS AWAITING PLAN APPROVAL — will re-notify after compaction completes"
          : "";
        const comment = `# ⚠️ slot ${slotNum} compacting — mid-task, no PR yet${issuePart}${activityPart}${approvalNote}`;
        this.relay.notifyCompactWarning(slotNum, comment);
      }
    }

    return {};
  }
}
