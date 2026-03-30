/**
 * MoP Tmux Relay — Inject formatted messages into PM pane
 *
 * Replaces scattered bash scripts (slot-idle-notify.sh, etc.) with a single
 * relay that formats events and injects them into the PM's tmux pane.
 *
 * Uses the send-to-slot.sh script infrastructure for reliable delivery.
 */

import { execSync } from "node:child_process";
import type { LogManager } from "./logs.js";
import type { MoPConfig, SlotState } from "./types.js";

const SEND_TO_SLOT_SCRIPT =
  `${process.env.HOME}/.claude/skills/tmux-slot-command/scripts/send-to-slot.sh`;

export class TmuxRelay {
  private pmPaneAddress: string;
  private logManager: LogManager | null = null;

  constructor(config: MoPConfig) {
    this.pmPaneAddress = config.pmPaneAddress;
  }

  /** Attach a LogManager for log-based output capture and activity detection. */
  setLogManager(lm: LogManager): void {
    this.logManager = lm;
  }

  /**
   * Inject a raw message into the PM pane via tmux send-keys.
   * IMPORTANT: Text and Enter must be separate send-keys calls —
   * appending Enter to the text send-keys can silently drop the Enter.
   */
  injectToPM(message: string): boolean {
    try {
      execSync(
        `tmux send-keys -t ${this.pmPaneAddress} ${shellEscape(message)} && ` +
        `sleep 0.3 && ` +
        `tmux send-keys -t ${this.pmPaneAddress} Enter && ` +
        `sleep 0.5`,
        { timeout: 10_000 }
      );
      return true;
    } catch (err) {
      console.error(`[relay] Failed to inject into PM pane:`, err);
      return false;
    }
  }

  /**
   * Inject a comment line + slash command into the PM pane as ONE message.
   * Types comment, Shift+Enter (newline without submit), command, then Enter.
   * This ensures the MoP relay delivers a single combined user message.
   */
  private injectCommandToPM(comment: string, command: string): boolean {
    try {
      execSync(
        // Type comment + Shift+Enter (newline) + command as one input
        `tmux send-keys -t ${this.pmPaneAddress} ${shellEscape(comment)} S-Enter ${shellEscape(command)} && ` +
        `sleep 0.5 && ` +
        `tmux send-keys -t ${this.pmPaneAddress} Enter`,
        { timeout: 10_000 }
      );
      return true;
    } catch (err) {
      console.error(`[relay] Failed to inject command into PM pane:`, err);
      return false;
    }
  }

  /**
   * Notify PM that a slot went idle.
   * Sends: # comment line, then /slot-idle N slash command.
   */
  notifySlotIdle(slot: SlotState): void {
    // Send /slot-idle N command — triggers the PM's slot-idle skill for proper handling.
    // Root cause of earlier failures was MoP HTTP server being down + missing HTTP hooks
    // on slots, NOT the slash command format. (Lesson: 2026-03-18)
    this.injectToPM(`/slot-idle ${slot.slot}`);
  }

  /**
   * Notify PM that a plan is ready for review.
   * Sends: # comment line, then /plan-ready N ISSUE slash command.
   */
  notifyPlanReady(slotNum: number, issueNum = 0, planFile = "", isRevision = false): void {
    // RETIRED (2026-03-24): Plan-ready notification disabled.
    // Slots now self-review via /codex-plan-review. PM is not in the loop.
    // Rajiv directive: "remove the plan ready processing from MoP as well"
    const verb = isRevision ? "plan revised" : "plan written";
    const filePart = planFile || "plan.md";
    const issuePart = issueNum ? ` | #${issueNum}` : "";
    // Log for visibility only — no command injection to PM
    const comment = `# ${verb} by slot ${slotNum}${issuePart}: ${filePart}`;
    console.log(`[relay] Plan-ready SUPPRESSED (retired): ${comment}`);
  }

  /**
   * Notify PM that a slot is (still) waiting for plan approval.
   * Fires when Stop hook detects awaiting_plan_approval state — typically
   * after autocompact re-displays the plan prompt. PM should re-send "2".
   */
  notifyPlanApprovalNeeded(slotNum: number, issueNum: number): void {
    const time = new Date().toLocaleTimeString("en-US", { hour12: false });
    const issuePart = issueNum ? ` #${issueNum}` : "";
    const comment = `# ⚠️ slot ${slotNum} still awaiting plan approval${issuePart} — re-send 2 | ${time}`;
    this.injectToPM(comment);
  }

  /**
   * Notify PM that a background subagent completed in a slot.
   * Informational only — PM decides what to do next.
   */
  notifySubagentComplete(slotNum: number): void {
    const time = new Date().toLocaleTimeString("en-US", { hour12: false });
    const comment = `# subagent completed in slot ${slotNum} | ${time}`;
    this.injectToPM(comment);
  }

  /**
   * Notify PM that a slot has escalated — needs PM intervention.
   * This is a high-priority notification: slot is blocked and waiting.
   */
  notifyEscalation(slotNum: number, issueNum: number, description: string): void {
    const time = new Date().toLocaleTimeString("en-US", { hour12: false });
    const issuePart = issueNum ? ` #${issueNum}` : "";
    const descPart = description ? ` — ${truncate(description, 60)}` : "";
    const comment = `# 🚨 slot ${slotNum} ESCALATED${issuePart}${descPart} | ${time}`;
    this.injectToPM(comment);
  }

  /**
   * Notify PM that a slot is about to compact (lose context).
   */
  notifyCompactWarning(slotNum: number, comment: string): void {
    this.injectToPM(comment);
  }

  /**
   * Notify PM of a scheduled task trigger.
   * Format: [scheduled-task | <name> | HH:MM]
   */
  notifyScheduledTask(name: string): void {
    const time = new Date().toLocaleTimeString("en-US", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
    });
    const message = `[scheduled-task | ${name} | ${time}]`;
    this.injectToPM(message);
  }

  /**
   * Send a command to a dev slot.
   */
  sendToSlot(slotNum: number, command: string, force = false, raw = false): boolean {
    try {
      const forceFlag = force ? " --force" : "";
      const rawFlag = raw ? " --raw" : "";
      execSync(
        `${SEND_TO_SLOT_SCRIPT} ${slotNum} ${shellEscape(command)}${forceFlag}${rawFlag}`,
        { timeout: 15_000 } // send-to-slot.sh has 10s wait timeout
      );
      return true;
    } catch (err) {
      console.error(`[relay] Failed to send to slot ${slotNum}:`, err);
      return false;
    }
  }

  /**
   * Check if a slot is currently active (processing).
   * is-active.sh communicates via exit codes: 0=ACTIVE, 1=IDLE, 2=ERROR.
   * execSync throws on non-zero exit — so reaching the return means exit 0 (ACTIVE).
   */
  isSlotActive(slotNum: number): boolean {
    try {
      execSync(
        `${process.env.HOME}/.claude/skills/tmux-slot-command/scripts/is-active.sh ${slotNum}`,
        { timeout: 5_000 }
      );
      // exit code 0 = ACTIVE
      return true;
    } catch {
      // exit code 1 = IDLE, exit code 2 = ERROR, timeout = assume idle
      return false;
    }
  }

  /**
   * Capture the current output of a slot's tmux pane.
   * Prefers log-based capture (persistent, never loses content) when LogManager is attached.
   * Falls back to tmux capture-pane if no LogManager.
   */
  captureOutput(slotNum: number, lines = 30): { output: string; activity: "busy" | "idle" } {
    // Always use tmux capture-pane — captures the visible terminal screen including
    // Claude Code TUI prompts (plan approval, status bar) that pipe-pane logs miss.
    // (Rajiv directive 2026-03-18: "change it to use tmux capture pane instead")
    let output = "";
    const paneAddress = `0:0.${slotNum}`;
    try {
      const raw = execSync(
        `tmux capture-pane -t ${paneAddress} -p -S -${lines}`,
        { timeout: 5_000 }
      );
      output = raw.toString();
    } catch (err) {
      output = `[capture failed: ${err}]`;
    }

    const activity = this.isSlotActive(slotNum) ? "busy" as const : "idle" as const;
    return { output, activity };
  }

  /**
   * Check if a slot is actively producing output based on log mtime.
   * If log was modified in the last 5 seconds, the slot is actively working.
   * Falls back to is-active.sh if no LogManager.
   */
  isSlotActiveFromLog(slotNum: number): boolean {
    if (!this.logManager) return this.isSlotActive(slotNum);

    const mtime = this.logManager.getLogMtime(slotNum);
    if (!mtime) return this.isSlotActive(slotNum); // No log → fallback

    const ageMs = Date.now() - mtime.getTime();
    if (ageMs < 5_000) return true; // Log modified recently → active

    // Log is stale, but slot might be waiting for input (no output)
    // Fall back to is-active.sh as secondary check
    return this.isSlotActive(slotNum);
  }
}

// ─── Helpers ───────────────────────────────────────────────

function shellEscape(str: string): string {
  return `'${str.replace(/'/g, "'\\''")}'`;
}

function truncate(str: string, maxLen: number): string {
  return str.length <= maxLen ? str : str.slice(0, maxLen - 1) + "…";
}
