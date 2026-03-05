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
   * Uses send-to-slot.sh with --force to skip idle wait (PM is always "active").
   */
  private injectToPM(message: string): boolean {
    try {
      // For PM pane (slot 0), we use tmux directly since send-to-slot.sh
      // expects slot numbers 1-4
      execSync(
        `tmux send-keys -t ${this.pmPaneAddress} ${shellEscape(message)} Enter`,
        { timeout: 10_000 }
      );
      return true;
    } catch (err) {
      console.error(`[relay] Failed to inject into PM pane:`, err);
      return false;
    }
  }

  /**
   * Notify PM that a slot went idle.
   * Format: [slot N idle — <task> | branch: <branch>] [HH:MM:SS]
   */
  notifySlotIdle(slot: SlotState): void {
    const time = new Date().toLocaleTimeString("en-US", { hour12: false });
    const taskPart = slot.task ? ` — ${truncate(slot.task, 50)}` : "";
    const branchPart = slot.branch ? ` | branch: ${slot.branch}` : "";
    const message = `[slot ${slot.slot} idle${taskPart}${branchPart}] [${time}]`;
    this.injectToPM(message);
  }

  /**
   * Notify PM that a plan is ready for review.
   * Format: [plan-ready | slot N] [HH:MM:SS]
   */
  notifyPlanReady(slotNum: number): void {
    const time = new Date().toLocaleTimeString("en-US", { hour12: false });
    const message = `[plan-ready | slot ${slotNum}] [${time}]`;
    this.injectToPM(message);
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
