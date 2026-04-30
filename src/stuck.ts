/**
 * MoP Stuck Detector — Watchdog for unresponsive slots
 *
 * Checks all occupied, non-idle, non-DND slots every 60 seconds.
 * If a slot's log file hasn't been modified in 5+ minutes, it's
 * considered "stuck" and PM is notified.
 *
 * Dedup: Only notifies PM once per 10 minutes per slot to prevent spam.
 */

import { appendFileSync } from "node:fs";
import { createHash } from "node:crypto";
import type { MoPDatabase } from "./db.js";
import type { LogManager } from "./logs.js";
import type { TmuxRelay } from "./relay.js";
import type { SlotState } from "./types.js";

function debugLog(line: string): void {
  try {
    appendFileSync(
      "/tmp/mop-debug.log",
      `${new Date().toISOString()} ${line}\n`
    );
  } catch {
    // never fail the detector on log write errors
  }
}

export class StuckDetector {
  private readonly STUCK_THRESHOLD_MS = 5 * 60 * 1000; // 5 minutes no output
  private readonly PLAN_APPROVAL_THRESHOLD_MS = 5 * 60 * 1000; // 5 min waiting for approval (Rajiv directive 2026-03-23)
  private readonly CHECK_INTERVAL_MS = 60 * 1000; // Check every minute
  private readonly DEDUP_WINDOW_MS = 5 * 60 * 1000; // Notify at most every 5 min per slot (Rajiv directive 2026-03-23)
  // Context-overflow detector dedup: don't re-inject /slot-context-overflow
  // for the same slot within 10 minutes (banner persists until /compact runs).
  private readonly CONTEXT_OVERFLOW_DEDUP_MS = 10 * 60 * 1000;
  // Post-/compact dispatch dedup. Detector re-fires post-compact because
  // "Context limit reached" lingers in tmux scrollback after Claude clears
  // its conversation. Time-based guard prevents double-fire that
  // concatenates the second /compact with the SessionStart:compact
  // "continue your work" trigger (Rajiv directive 2026-04-30 13:35
  // thread 1777536325.083369). Resets implicitly when no overflow tick
  // occurs within the window — a NEW genuine overflow with a fresh
  // signature ≥5min later passes through.
  private readonly COMPACT_DISPATCH_DEDUP_MS = 5 * 60 * 1000;
  // Slots that bypass native auto-compact (codex-proxy / GPT-5.5 wrapper).
  // These need tmux-string detection because PreCompact:auto won't fire.
  // Reference: feedback_autocompact_v3_native_vs_wrapper.md
  private readonly WRAPPER_SLOTS = new Set<number>([1, 4]);
  // tmux capture-pane string matchers for the Claude Code overflow UI banner.
  // Match either the full "/compact or /clear to continue" string or the
  // earlier "Context low" warning.
  private readonly OVERFLOW_PATTERNS: RegExp[] = [
    /Context limit reached/i,
    /Context low\s*[·•]/i,
    /\/compact or \/clear to continue/i,
    /conversation needs to be compacted/i,
  ];
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

    // Phase 1: context-overflow detection for codex-proxy wrapper slots.
    // These slots bypass native auto-compact, so they get stuck on the
    // "Context limit reached · /compact or /clear to continue" UI string
    // without ever firing PreCompact. tmux-string match is the only reliable
    // primitive here. (2026-04-29 — analysis /tmp/mop-compact-hook-analysis.md)
    for (const slot of slots) {
      if (!this.WRAPPER_SLOTS.has(slot.slot)) continue;
      if (slot.dnd) continue;
      this.checkContextOverflow(slot);
    }

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

      const ageMs = Date.now() - mtime!.getTime();
      if (ageMs > this.STUCK_THRESHOLD_MS) {
        this.handleStuck(slot, ageMs);
      }
    }
  }

  /**
   * Check whether slot's tmux pane shows the Claude Code context-overflow
   * banner. If so, send /compact directly to the slot's pane (deduped).
   * Wrapper-slot fallback for the missing PreCompact:auto signal.
   *
   * Direct-recovery model (Rajiv directive 2026-04-30 12:44):
   * Recovery is deterministic — no PM judgment needed — so we bypass the
   * PM pane round-trip entirely. The slot self-compacts; on
   * SessionStart:compact the hooks handler nudges it with "continue your
   * work". The legacy /slot-context-overflow PM-side skill is deprecated.
   *
   * Dedup strategy (Rajiv directive 2026-04-29 — see
   * feedback_mop_overflow_detector_scrollback_dedup_gap.md):
   *  - Compute a stable signature for the matched banner = sha1 of the
   *    line containing the match plus 1 line before/after. Two ticks that
   *    see the SAME stale tmux scrollback produce the same signature.
   *  - Skip re-fire when the signature matches the most recent
   *    `context_overflow_detected` event for this slot AND that event is
   *    newer than the most recent `session_start_compact` event for the slot.
   *  - When a fresh `session_start_compact` arrives (post-/compact), the old
   *    signature is implicitly invalidated — the next genuine new banner
   *    will fire even if its sha1 happens to collide with a pre-/compact
   *    one.
   *  - Time-based CONTEXT_OVERFLOW_DEDUP_MS window stays as a secondary
   *    guard for rapid double-fires within the same banner instance.
   *  - Without dedup, scrollback persistence would cause the slot to
   *    receive /compact 2-3× per real overflow event. Do NOT remove.
   */
  private checkContextOverflow(slot: SlotState): void {
    let pane = "";
    try {
      const { output } = this.relay.captureOutput(slot.slot, 60);
      pane = output;
    } catch {
      return; // capture failure — try again next tick
    }
    if (!pane) return;

    const matched = this.OVERFLOW_PATTERNS.find((rx) => rx.test(pane));
    if (!matched) return;

    const signature = this.computeMatchSignature(pane, matched);

    const recent = this.db.getEvents(slot.slot, 1, "context_overflow_detected");
    const lastResume = this.db.getEvents(slot.slot, 1, "session_start_compact");
    const lastResumeTs = lastResume.length > 0
      ? new Date(lastResume[0].timestamp).getTime()
      : 0;

    if (recent.length > 0) {
      const lastTs = new Date(recent[0].timestamp).getTime();
      let lastPayload: { match_signature?: string } = {};
      try {
        lastPayload = JSON.parse(recent[0].payload ?? "{}") as {
          match_signature?: string;
        };
      } catch {
        lastPayload = {};
      }
      const sameSignature = lastPayload.match_signature === signature;
      const lastIsPreResume = lastTs < lastResumeTs;

      // If we already fired on this exact banner AND no /compact has run
      // since, treat this tick as stale scrollback — do not re-fire.
      if (sameSignature && !lastIsPreResume) {
        debugLog(
          `[stuck] slot=${slot.slot} stale-scrollback-suppress sig=${signature.slice(0, 12)}`
        );
        return;
      }

      // Time window (covers brief flapping or different-signature near-miss).
      if (Date.now() - lastTs < this.CONTEXT_OVERFLOW_DEDUP_MS && !lastIsPreResume) {
        return;
      }
    }

    // Post-/compact dispatch guard — prevents duplicate /compact when the
    // overflow banner lingers in tmux scrollback after Claude has already
    // compacted. Without this, the detector re-fires /compact and it
    // concatenates with the SessionStart:compact "continue your work"
    // trigger as `continue your work/compact`. (Rajiv directive
    // 2026-04-30 13:35 thread 1777536325.083369.)
    const lastDispatch = this.db.getEvents(slot.slot, 1, "compact_dispatched");
    if (lastDispatch.length > 0) {
      const lastDispatchTs = new Date(lastDispatch[0].timestamp).getTime();
      if (Date.now() - lastDispatchTs < this.COMPACT_DISPATCH_DEDUP_MS) {
        debugLog(
          `[stuck] slot=${slot.slot} post-compact-suppress sig=${signature.slice(0, 12)} age_ms=${Date.now() - lastDispatchTs}`
        );
        return;
      }
    }

    this.db.logEvent(
      slot.slot,
      "context_overflow_detected",
      null,
      null,
      {
        slot: slot.slot,
        task: slot.task,
        issue: slot.issue,
        matched_pattern: matched.source,
        match_signature: signature,
        capture_excerpt: pane.slice(-400),
      }
    );

    debugLog(
      `[stuck] slot=${slot.slot} fire-overflow-direct sig=${signature.slice(0, 12)} pattern=${matched.source}`
    );

    // Log the dispatch BEFORE sending so the next detector tick (which may
    // arrive within seconds if the scrollback banner is still visible)
    // sees the dedup record and suppresses re-fire.
    this.db.logEvent(slot.slot, "compact_dispatched", null, null, {
      slot: slot.slot,
      match_signature: signature,
      matched_pattern: matched.source,
    });

    // Direct-recovery (Rajiv directive 2026-04-30): send /compact straight
    // to the slot. force=true bypasses the active-check because the slot
    // is by definition stuck on the overflow banner at this point.
    this.relay.sendToSlot(slot.slot, "/compact", true);
  }

  /**
   * Build a stable signature for the matched banner instance based on the
   * matched line ± 1 line of context. tmux capture line offsets shift as
   * new output arrives, but the textual neighborhood of a stale banner
   * stays identical until it scrolls off-screen.
   */
  private computeMatchSignature(pane: string, rx: RegExp): string {
    const lines = pane.split("\n");
    let idx = -1;
    for (let i = lines.length - 1; i >= 0; i--) {
      if (rx.test(lines[i])) {
        idx = i;
        break;
      }
    }
    const start = Math.max(0, idx - 1);
    const end = Math.min(lines.length, idx + 2);
    const window = lines.slice(start, end).join("\n");
    return createHash("sha1").update(window).digest("hex");
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
