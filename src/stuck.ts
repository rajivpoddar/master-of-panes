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
  private readonly PLAN_APPROVAL_THRESHOLD_MS = 5 * 60 * 1000; // 5 min waiting for approval (Rajiv directive 2026-03-23)
  private readonly CHECK_INTERVAL_MS = 60 * 1000; // Check every minute
  private readonly DEDUP_WINDOW_MS = 5 * 60 * 1000; // Notify at most every 5 min per slot (Rajiv directive 2026-03-23)
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
      // RETIRED (2026-03-24): Plan approval watchdog disabled.
      // Slots now use plan-agent + /codex-plan-review (self-managing).
      // PM is not in the plan approval loop. Rajiv directive: "remove the plan approval watchdog as well"
      if (slot.activity === "awaiting_plan_approval" && !slot.dnd) {
        continue; // Skip — no longer monitoring plan approvals
      }

      // Rajiv directive 2026-03-17: stuck detection only for plan approval waits.
      // General "no output" detection creates too much noise.
      // Skip all non-plan-approval slots.
      continue;

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
    // Redesign 2026-03-23 (Rajiv directive): Use last_activity timestamp as
    // the sole anchor. No event-based anchors — they get stale across sessions
    // and the 1-hour guard prevented detection.
    //
    // Simple logic: if activity === "awaiting_plan_approval" AND last_activity
    // was set >5min ago, remind PM. Dedup every 5min.
    if (!slot.last_activity) return;

    const waitMs = Date.now() - new Date(slot.last_activity).getTime();
    if (waitMs < this.PLAN_APPROVAL_THRESHOLD_MS) return;

    // Dedup: don't spam — check if we already notified within the window
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
      anchor_source: "last_activity",
    });

    const issuePart = slot.issue ? ` #${slot.issue}` : "";
    const comment = `# ⏰ slot ${slot.slot} waiting for plan approval${issuePart} for ${minutes}min`;
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
