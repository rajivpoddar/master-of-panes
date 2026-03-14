/**
 * MoP Stuck Detector — Watchdog for unresponsive slots
 *
 * Checks all occupied, non-idle, non-DND slots every 60 seconds.
 * If a slot's log file hasn't been modified in 5+ minutes, it's
 * considered "stuck" and PM is notified.
 *
 * Dedup: Only notifies PM once per 10 minutes per slot to prevent spam.
 */

import type { MoPDatabase } from "./db.js";
import type { LogManager } from "./logs.js";
import type { TmuxRelay } from "./relay.js";
import type { SlotState } from "./types.js";

export class StuckDetector {
  private readonly STUCK_THRESHOLD_MS = 5 * 60 * 1000; // 5 minutes no output
  private readonly PLAN_APPROVAL_THRESHOLD_MS = 10 * 60 * 1000; // 10 min waiting for approval
  private readonly CHECK_INTERVAL_MS = 60 * 1000; // Check every minute
  private readonly DEDUP_WINDOW_MS = 10 * 60 * 1000; // Notify at most every 10 min per slot
  private timer: NodeJS.Timeout | null = null;

  constructor(
    private db: MoPDatabase,
    private logManager: LogManager,
    private relay: TmuxRelay
  ) {}

  /**
   * Check all slots for stuck state.
   * A slot is stuck when:
   * - occupied = true (assigned to a task)
   * - idle = false (not at prompt — actively processing)
   * - dnd = false (not under manual control)
   * - Log mtime > STUCK_THRESHOLD_MS ago (no output produced)
   */
  checkAll(): void {
    const slots = this.db.getAllSlots();
    for (const slot of slots) {
      // Check for stale plan approval waits (separate from stuck detection)
      if (slot.activity === "awaiting_plan_approval" && !slot.dnd) {
        this.checkPlanApproval(slot);
        continue; // Skip normal stuck check — slot is waiting for PM, not hung
      }

      // Only check slots that should be producing output
      if (!slot.occupied || slot.idle || slot.dnd) continue;

      const mtime = this.logManager.getLogMtime(slot.slot);
      if (!mtime) continue; // No log file — can't determine

      const ageMs = Date.now() - mtime.getTime();
      if (ageMs > this.STUCK_THRESHOLD_MS) {
        this.handleStuck(slot, ageMs);
      }
    }
  }

  /**
   * Check if a slot has been waiting for plan approval too long.
   * Uses last_activity timestamp to measure wait duration.
   */
  private checkPlanApproval(slot: SlotState): void {
    const lastActivity = new Date(slot.last_activity);
    const waitMs = Date.now() - lastActivity.getTime();

    if (waitMs < this.PLAN_APPROVAL_THRESHOLD_MS) return;

    // Dedup: check if we already notified recently
    const recentEvents = this.db.getEvents(slot.slot, 1, "plan_approval_stale");
    if (recentEvents.length > 0) {
      const lastNotified = new Date(recentEvents[0].timestamp);
      if (Date.now() - lastNotified.getTime() < this.DEDUP_WINDOW_MS) {
        return;
      }
    }

    const minutes = Math.round(waitMs / 60_000);
    this.db.logEvent(slot.slot, "plan_approval_stale", null, null, {
      task: slot.task,
      issue: slot.issue,
      wait_minutes: minutes,
    });

    const issuePart = slot.issue ? ` #${slot.issue}` : "";
    const comment = `# ⏰ slot ${slot.slot} waiting for plan approval${issuePart} for ${minutes}min — re-send 2 to approve`;
    this.relay.injectToPM(comment);
  }

  private handleStuck(slot: SlotState, silenceMs: number): void {
    const minutes = Math.round(silenceMs / 60_000);

    // Dedup: check if we already notified recently
    const recentEvents = this.db.getEvents(slot.slot, 1, "stuck_detected");
    if (recentEvents.length > 0) {
      const lastNotified = new Date(recentEvents[0].timestamp);
      if (Date.now() - lastNotified.getTime() < this.DEDUP_WINDOW_MS) {
        return; // Already notified within dedup window
      }
    }

    // Log the stuck event
    this.db.logEvent(slot.slot, "stuck_detected", null, null, {
      task: slot.task,
      issue: slot.issue,
      silence_minutes: minutes,
      activity: slot.activity,
    });

    // Notify PM
    const taskPart = slot.task ? ` | task: ${slot.task}` : "";
    const activityPart = slot.activity ? ` | last activity: ${slot.activity}` : "";
    const comment = `# ⚠️ slot ${slot.slot} may be stuck — no output for ${minutes}min${taskPart}${activityPart}`;
    this.relay.injectToPM(comment);
  }

  /**
   * Start periodic stuck checking.
   */
  start(): void {
    if (this.timer) return; // Already running
    this.timer = setInterval(() => {
      try {
        this.checkAll();
      } catch (err) {
        console.error("[stuck] Check failed:", err);
      }
    }, this.CHECK_INTERVAL_MS);
    console.log("[stuck] Watchdog started — checking every 60s, threshold 5min");
  }

  /**
   * Stop periodic checking (for clean shutdown).
   */
  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
