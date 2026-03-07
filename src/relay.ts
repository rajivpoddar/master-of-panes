/**
 * MoP Tmux Relay — Inject formatted messages into PM pane
 *
 * Replaces scattered bash scripts (slot-idle-notify.sh, etc.) with a single
 * relay that formats events and injects them into the PM's tmux pane.
 *
 * Uses the send-to-slot.sh script infrastructure for reliable delivery.
 */

import { execSync } from "node:child_process";
import type { MoPConfig, SlotState } from "./types.js";

const SEND_TO_SLOT_SCRIPT =
  `${process.env.HOME}/.claude/skills/tmux-slot-command/scripts/send-to-slot.sh`;

export class TmuxRelay {
  private pmPaneAddress: string;

  constructor(config: MoPConfig) {
    this.pmPaneAddress = config.pmPaneAddress;
  }

  /**
   * Inject a raw message into the PM pane via tmux send-keys.
   * IMPORTANT: Text and Enter must be separate send-keys calls —
   * appending Enter to the text send-keys can silently drop the Enter.
   */
  private injectToPM(message: string): boolean {
    try {
      execSync(
        `tmux send-keys -t ${this.pmPaneAddress} ${shellEscape(message)} && ` +
        `sleep 0.3 && ` +
        `tmux send-keys -t ${this.pmPaneAddress} Enter`,
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
    const time = new Date().toLocaleTimeString("en-US", { hour12: false });
    const taskPart = slot.task ? ` — ${truncate(slot.task, 40)}` : "";
    const branchPart =
      slot.branch && slot.branch !== "main"
        ? ` | branch: ${slot.branch}`
        : "";

    const comment = `# slot ${slot.slot} idle${taskPart}${branchPart} | ${time}`;
    const command = `/slot-idle ${slot.slot}`;
    this.injectCommandToPM(comment, command);
  }

  /**
   * Notify PM that a plan is ready for review.
   * Sends: # comment line, then /plan-ready N ISSUE slash command.
   */
  notifyPlanReady(slotNum: number, issueNum = 0, planFile = ""): void {
    const issuePart = issueNum ? ` | #${issueNum}` : "";
    const filePart = planFile || "plan.md";

    const comment = `# plan written by slot ${slotNum}${issuePart}: ${filePart}`;
    const command = `/plan-ready ${slotNum} ${issueNum || 0}`;
    this.injectCommandToPM(comment, command);
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
  sendToSlot(slotNum: number, command: string, force = false): boolean {
    try {
      const forceFlag = force ? " --force" : "";
      execSync(
        `${SEND_TO_SLOT_SCRIPT} ${slotNum} ${shellEscape(command)}${forceFlag}`,
        { timeout: 130_000 } // send-to-slot.sh has 120s wait timeout
      );
      return true;
    } catch (err) {
      console.error(`[relay] Failed to send to slot ${slotNum}:`, err);
      return false;
    }
  }

  /**
   * Check if a slot is currently active (processing).
   */
  isSlotActive(slotNum: number): boolean {
    try {
      const result = execSync(
        `${process.env.HOME}/.claude/skills/tmux-slot-command/scripts/is-active.sh ${slotNum}`,
        { timeout: 5_000 }
      );
      return result.toString().trim() === "ACTIVE";
    } catch {
      return false;
    }
  }
}

// ─── Helpers ───────────────────────────────────────────────

function shellEscape(str: string): string {
  return `'${str.replace(/'/g, "'\\''")}'`;
}

function truncate(str: string, maxLen: number): string {
  return str.length <= maxLen ? str : str.slice(0, maxLen - 1) + "…";
}
