/**
 * MoP Stuck Detector — Watchdog for unresponsive slots
 *
 * Checks all occupied, non-idle, non-DND slots every 60 seconds.
 * If a slot's log file hasn't been modified in 5+ minutes, it's
 * considered "stuck" and PM is notified.
 *
 * Dedup: Only notifies PM once per 10 minutes per slot to prevent spam.
 */

import { appendFileSync, writeFileSync } from "node:fs";
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

/**
 * Parse a SQLite timestamp string as UTC.
 *
 * SQLite's `strftime('%Y-%m-%dT%H:%M:%f', 'now')` produces UTC timestamps
 * WITHOUT a 'Z' suffix (e.g., "2026-04-30T14:40:40.462"). JavaScript's
 * `new Date(string).getTime()` interprets ISO-8601 strings without a
 * timezone marker as LOCAL time, which on a non-UTC host (e.g., IST UTC+5:30)
 * produces a getTime() value off by the timezone offset.
 *
 * This breaks every `Date.now() - parsedTs < windowMs` dedup check in the
 * detector — the parsed timestamp is shifted hours earlier than reality, so
 * the time window never triggers and dedup leaks.
 *
 * Appending 'Z' forces UTC interpretation and aligns with Date.now()
 * (always UTC ms since epoch).
 *
 * Discovered: 2026-04-30 ~14:42 UTC. Slot 1 received a duplicate /compact
 * at 14:42:42 — only 2min after the prior dispatch at 14:40:40, well inside
 * the 5min COMPACT_DISPATCH_DEDUP_MS window. Local-parsing turned the 2min
 * gap into ~5h32m apparent gap, defeating the guard.
 *
 * Reference: feedback_mop_overflow_detector_scrollback_dedup_gap.md
 */
function parseDbTimestampMs(timestamp: string): number {
  // If the string already has a timezone marker (Z or +/-HH:MM), trust it.
  if (/Z$|[+-]\d{2}:?\d{2}$/.test(timestamp)) {
    return new Date(timestamp).getTime();
  }
  return new Date(timestamp + "Z").getTime();
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
  // RETAINED for historical reference / debug logging only — gate REMOVED
  // 2026-05-01 (Rajiv directive thread 1777626989.055709 option a).
  // Auto-compact now fires for any active+occupied slot whose tmux scrollback
  // shows the overflow banner. Native auto-compact CAN fail on Sonnet slots
  // too (see Ashwini/slot 3 incident 2026-05-01) — when it does, the banner
  // is the only reliable signal regardless of model.
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
  // Answer-prompt block detector (Rajiv directive 2026-04-30: "we also need
  // to detect this in MoP and trigger a slot-blocked command on pm pane,
  // same way we do for compact"). Triggers when a slot is parked at a
  // numbered-options menu waiting for the user to pick. Reliable signature
  // requires BOTH the navigation hint footer AND a `❯ N.` cursor on a
  // numbered option line within ~15 lines above (the hint string by itself
  // appears in mid-stream output). Applies to ALL slots, not just wrappers.
  private readonly BLOCK_NAV_HINT =
    /Enter to select\s*[·•]\s*↑\/↓ to navigate\s*[·•]\s*Esc to cancel/i;
  private readonly BLOCK_OPTION_CURSOR = /^\s*❯\s+\d+\./;
  private readonly BLOCK_LOOKBACK_LINES = 15;
  // Block dedup: don't re-inject /slot-blocked for the same slot within
  // 10 minutes (banner persists until PM resolves the prompt).
  private readonly BLOCK_DISPATCH_DEDUP_MS = 10 * 60 * 1000;
  private timer: NodeJS.Timeout | null = null;
  // Per-slot last fired match line offset (relative to capture start).
  // Only fire when current match offset > lastMatchLine[slot]. Reset on
  // SessionStart:compact via resetOverflowTracking(). Belt-and-suspenders
  // with the signature dedup — covers the case where ±1-line context hash
  // shifts due to TUI status-line redraws on an otherwise stale banner.
  private lastMatchLine: Map<number, number> = new Map();
  // Per-slot "compact in flight" timestamp (ms epoch). Set immediately
  // before sendToSlot('/compact') and cleared on SessionStart:compact via
  // resetOverflowTracking(). A new compact dispatch within
  // COMPACT_INFLIGHT_DEDUP_MS of a still-in-flight send is suppressed —
  // belt-and-suspenders defense against duplicate keystroke landing when
  // the wrapper script ETIMEDOUTs and the next 60s detector tick wakes
  // before SessionStart:compact has cleared the tracker.
  // Rajiv-confirmed 2026-05-01 07:46 IST.
  private compactInFlightAt: Map<number, number> = new Map();
  private readonly COMPACT_INFLIGHT_DEDUP_MS = 30_000;
  // Per-slot last fired block-prompt match line offset. Mirrors
  // lastMatchLine for the block detector. Reset on idle→active transition
  // so a NEW block prompt later in the session can fire even if its line
  // offset happens to be ≤ a previous fire offset.
  private lastBlockMatchLine: Map<number, number> = new Map();

  constructor(
    private db: MoPDatabase,
    private logManager: LogManager,
    private relay: TmuxRelay
  ) {}

  /**
   * Reset the lastMatchLine tracker for a slot. Called by hooks.ts when
   * SessionStart:compact fires (post-compact recovery). After /compact
   * completes the conversation is cleared and a new genuine overflow
   * should fire even if its line position happens to be ≤ the pre-compact
   * fire line.
   */
  resetOverflowTracking(slotNum: number): void {
    this.lastMatchLine.delete(slotNum);
    this.compactInFlightAt.delete(slotNum);
    debugLog(`[stuck] slot=${slotNum} overflow tracking reset (post-compact)`);
  }

  /**
   * Reset the block-prompt lastMatchLine tracker for a slot. Called by
   * hooks.ts on idle→active transition (PostToolUse with wasIdle) so
   * multi-block sessions get clean detection — a slot that hits prompt A,
   * is unblocked, then later hits prompt B should fire even if B's line
   * offset is ≤ A's.
   */
  resetBlockTracking(slotNum: number): void {
    this.lastBlockMatchLine.delete(slotNum);
    debugLog(`[stuck] slot=${slotNum} block tracking reset (idle→active)`);
  }

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

    // Phase 1: context-overflow detection for ALL active+occupied slots
    // INCLUDING slot 0 (PM). Originally gated to codex-proxy wrapper slots
    // (1+4) because those slots bypass native auto-compact. But native
    // auto-compact CAN fail on Sonnet/Opus slots too (Ashwini/slot 3 incident
    // 2026-05-01 — Sonnet 4.6 hit "Context limit reached" and sat idle for
    // ~hour because native auto-compact never fired). The overflow banner
    // only appears when native auto-compact has FAILED, so firing /compact
    // on any slot showing it is safe — and necessary. PM slot 0 was added
    // 2026-05-01 14:54 IST (Rajiv directive Slack thread 1777626989.055709
    // option a, "including pm slot"): PM is also subject to native
    // auto-compact failure and needs the same recovery path. Slot 0 has no
    // DB row (slots table holds 1-4 only), so we synthesize a minimal
    // SlotState — the overflow detector only reads .slot, .dnd, .occupied,
    // .task, .issue from it. PM is treated as always occupied.
    //
    // Skip released slots: nothing to recover, and the banner won't appear
    // because Claude isn't actively running tools there. checkContextOverflow
    // also has its own idle-prompt + dedup + in-flight guards.
    // (2026-04-29 analysis /tmp/mop-compact-hook-analysis.md)
    for (const slot of slots) {
      if (slot.slot === 0) continue; // handled separately below (no DB row)
      if (slot.dnd) continue;
      if (!slot.occupied) continue;
      this.checkContextOverflow(slot);
    }
    // Slot 0 (PM) — synthesize a minimal SlotState. PM pane is always
    // occupied with the orchestrator session; never DND from MoP's view.
    // The overflow detector's strict canonical-banner pattern + idle-prompt
    // guard + signature dedup prevent false fires from PM tool output that
    // happens to include the substring "Context limit reached" (e.g., when
    // PM reads memory files or summarizes another slot's overflow event).
    const pmSlot: SlotState = {
      slot: 0,
      address: "0:0.0",
      name: "PM",
      status: "active",
      occupied: true,
      session_id: null,
      task: null,
      issue: null,
      branch: null,
      pr: null,
      assigned_at: null,
      last_activity: new Date().toISOString(),
      dnd: false,
      idle: false,
      activity: null,
    };
    this.checkContextOverflow(pmSlot);

    // Phase 1b: answer-prompt block detection. Applies to ALL slots, not
    // just wrappers — any slot can hit a numbered-options menu mid-task
    // (codex-companion, plan approval, manual interactive shell, etc.).
    // PM resolves via /slot-blocked skill. (Rajiv directive 2026-04-30)
    for (const slot of slots) {
      if (slot.dnd) continue;
      if (slot.slot === 0) continue; // PM pane — no self-detection
      this.detectAnswerPromptBlock(slot);
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

    // PM (slot 0) feedback-loop guard — Rajiv directive 2026-05-01 14:54 IST.
    // PM tool output regularly includes the substring "Context limit reached"
    // (reading memory files, summarizing other slots' overflow events,
    // quoting Slack threads about overflow). For slot 0 only, require the
    // FULL canonical Claude Code TUI banner — both halves must appear, and
    // they must appear together on the SAME line of the captured pane (the
    // banner is rendered as one TUI line). This prevents firing /compact on
    // PM when the substring is buried in tool output rather than the live
    // overflow banner. The standard idle-prompt + signature dedup checks
    // below still apply and provide additional defense.
    if (slot.slot === 0) {
      const lines = pane.split("\n");
      const hasCanonicalBanner = lines.some((line) =>
        /Context limit reached/i.test(line) &&
        /\/compact\s+or\s+\/clear\s+to\s+continue/i.test(line)
      );
      if (!hasCanonicalBanner) {
        debugLog(
          `[stuck] slot=0 PM overflow-substring-without-canonical-banner — suppress (likely PM tool output mentioning overflow, not the live banner)`
        );
        return;
      }
    }

    // Defensive idle-prompt check (Rajiv directive 2026-04-30 18:40:
    // "compact should be sent only at idle prompt"). The detector fired a
    // duplicate /compact because the slot had a tool running — the /compact
    // text landed in the queued-messages input buffer (visible in the
    // screenshot at /tmp/slack-img-1777554603454-image.png) instead of
    // submitting as a slash command. False negatives (skipping when slot
    // really does need /compact) are recoverable on the next 60s tick;
    // false positives (sending while busy) corrupt the input buffer and
    // produce the visible duplicate-fire bug.
    //
    // is-active.sh detects the gray vs white ❯ chevron — when idle, ❯ is
    // white. An overflow-banner slot SHOULD be idle (Claude can't keep
    // running tools once context is exhausted). If is-active reports busy,
    // the banner is stale scrollback or the slot is mid-recovery — defer.
    if (this.relay.isSlotActive(slot.slot)) {
      debugLog(
        `[stuck] slot=${slot.slot} overflow-detected-but-active — defer (banner likely stale, no idle prompt)`
      );
      return;
    }

    // Line-offset dedup (Bug A defense): tmux capture-pane returns lines
    // bottom-aligned. A stale banner stays at (or scrolls UP to) the SAME
    // or LOWER offset across ticks once a real new overflow occurs. If the
    // current match line is ≤ the last fired line (and tracker hasn't been
    // reset by SessionStart:compact), this is the same banner we already
    // fired on. computeMatchLineIndex returns the LAST occurrence of the
    // pattern — so a fresh banner appended below an older stale one will
    // produce a higher index.
    const matchLine = this.computeMatchLineIndex(pane, matched);
    const lastLine = this.lastMatchLine.get(slot.slot);
    if (matchLine >= 0 && lastLine !== undefined && matchLine <= lastLine) {
      debugLog(
        `[stuck] slot=${slot.slot} stale-line-offset-suppress curr=${matchLine} last=${lastLine}`
      );
      return;
    }

    const signature = this.computeMatchSignature(pane, matched);

    const recent = this.db.getEvents(slot.slot, 1, "context_overflow_detected");
    const lastResume = this.db.getEvents(slot.slot, 1, "session_start_compact");
    const lastResumeTs = lastResume.length > 0
      ? parseDbTimestampMs(lastResume[0].timestamp)
      : 0;

    if (recent.length > 0) {
      const lastTs = parseDbTimestampMs(recent[0].timestamp);
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

    // In-flight guard (Rajiv-confirmed 2026-05-01 07:46 IST):
    // The wrapper script can ETIMEDOUT (10s --wait poll) while the
    // tmux send-keys keystrokes already landed. If the relay's ETIMEDOUT-
    // as-success change is bypassed for any reason, a 30s in-flight
    // tracker still suppresses a duplicate /compact landing on top of
    // the first. The tracker is set immediately before the sendToSlot
    // call below and cleared on SessionStart:compact via
    // resetOverflowTracking().
    const inFlightAt = this.compactInFlightAt.get(slot.slot);
    if (
      inFlightAt !== undefined &&
      Date.now() - inFlightAt < this.COMPACT_INFLIGHT_DEDUP_MS
    ) {
      debugLog(
        `[stuck] slot=${slot.slot} compact-in-flight-suppress age_ms=${Date.now() - inFlightAt}`
      );
      return;
    }

    // Post-/compact dispatch guard — prevents duplicate /compact when the
    // overflow banner lingers in tmux scrollback after Claude has already
    // compacted. Without this, the detector re-fires /compact and it
    // concatenates with the SessionStart:compact "continue your work"
    // trigger as `continue your work/compact`. (Rajiv directive
    // 2026-04-30 13:35 thread 1777536325.083369.)
    const lastDispatch = this.db.getEvents(slot.slot, 1, "compact_dispatched");
    if (lastDispatch.length > 0) {
      const lastDispatchTs = parseDbTimestampMs(lastDispatch[0].timestamp);
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

    // Update line-offset tracker BEFORE sending so the next tick's
    // suppress check has the current match line.
    if (matchLine >= 0) {
      this.lastMatchLine.set(slot.slot, matchLine);
    }

    // Direct-recovery (Rajiv directive 2026-04-30): send /compact straight
    // to the slot. We've already verified above (via isSlotActive) that the
    // slot is at an idle prompt — no chevron-active, no in-flight tool.
    //
    // Drop --force flag (Rajiv directive 2026-05-01 07:46 IST):
    // "compact does not have to sent with force. the slot is already idle at
    // that point." The idle gate above guarantees the slot is parked at the
    // prompt, so the --force bypass semantics (skip active-slot check, queue
    // past in-flight Task subagents) are unnecessary and risky here — they
    // can mask a still-running tool which corrupts the input buffer.
    //
    // Mark in-flight BEFORE the send so the next 60s detector tick (which
    // can wake while the wrapper script is still doing its 10s --wait poll)
    // sees the marker and suppresses a duplicate dispatch.
    this.compactInFlightAt.set(slot.slot, Date.now());
    this.relay.sendToSlot(slot.slot, "/compact");
  }

  /**
   * Detect a numbered-options answer prompt where the slot is parked
   * waiting for user input. Reliable signature requires BOTH the
   * navigation hint footer ("Enter to select · ↑/↓ to navigate · Esc to
   * cancel") AND a `❯ N.` cursor on a numbered option line within ~15
   * lines above the hint. The hint string alone appears in mid-stream
   * output (e.g., quoted in plan text) — requiring the cursor co-location
   * eliminates that false positive.
   *
   * On match: inject `/slot-blocked N` into the PM pane (mirror of
   * /slot-context-overflow flow). PM's slot-blocked skill resolves the
   * prompt — pick a concrete numbered option, or escalate to Rajiv as a
   * single DM if the choice is product-scope.
   *
   * Idle-prompt gate via isSlotActive: if the slot is mid-stream the
   * "Enter to select" line is likely flashing through output, not a
   * stable parked prompt — defer to next tick.
   *
   * Per-slot dedup via lastBlockMatchLine + BLOCK_DISPATCH_DEDUP_MS event
   * window. Reset on idle→active via resetBlockTracking().
   *
   * Reference: Rajiv directive 2026-04-30 "we also need to detect this
   * in MoP and trigger a slot-blocked command on pm pane, same way we do
   * for compact".
   */
  private detectAnswerPromptBlock(slot: SlotState): void {
    let pane = "";
    try {
      const { output } = this.relay.captureOutput(slot.slot, 50);
      pane = output;
    } catch {
      return;
    }
    if (!pane) return;

    const lines = pane.split("\n");
    // Find the LAST navigation-hint line — anchor for the prompt.
    let hintLine = -1;
    for (let i = lines.length - 1; i >= 0; i--) {
      if (this.BLOCK_NAV_HINT.test(lines[i])) {
        hintLine = i;
        break;
      }
    }
    if (hintLine < 0) return;

    // Look back up to BLOCK_LOOKBACK_LINES for a `❯ N.` cursor line.
    const lookbackStart = Math.max(0, hintLine - this.BLOCK_LOOKBACK_LINES);
    let cursorLine = -1;
    for (let i = hintLine - 1; i >= lookbackStart; i--) {
      if (this.BLOCK_OPTION_CURSOR.test(lines[i])) {
        cursorLine = i;
        break;
      }
    }
    if (cursorLine < 0) {
      debugLog(
        `[stuck] slot=${slot.slot} block-hint-without-cursor — skip (likely mid-stream "Enter to select" mention)`
      );
      return;
    }

    // Idle-prompt gate. A slot truly parked at an answer menu reports
    // idle (white ❯ chevron). If is-active reports busy, the menu is
    // either stale scrollback or rendering mid-update — defer.
    if (this.relay.isSlotActive(slot.slot)) {
      debugLog(
        `[stuck] slot=${slot.slot} block-detected-but-active — defer (banner likely stale or mid-render)`
      );
      return;
    }

    // Line-offset dedup: don't re-fire on the same parked menu.
    const lastLine = this.lastBlockMatchLine.get(slot.slot);
    if (lastLine !== undefined && hintLine <= lastLine) {
      debugLog(
        `[stuck] slot=${slot.slot} stale-block-line-suppress curr=${hintLine} last=${lastLine}`
      );
      return;
    }

    // Time-window dedup: 10-minute guard against rapid double-fires when
    // line offsets shift due to TUI redraws.
    const lastDispatch = this.db.getEvents(slot.slot, 1, "block_dispatched");
    if (lastDispatch.length > 0) {
      const lastTs = parseDbTimestampMs(lastDispatch[0].timestamp);
      if (Date.now() - lastTs < this.BLOCK_DISPATCH_DEDUP_MS) {
        debugLog(
          `[stuck] slot=${slot.slot} block-dispatch-dedup age_ms=${Date.now() - lastTs}`
        );
        return;
      }
    }

    // Excerpt the menu region for the event payload + the PM-side capture.
    const excerptStart = Math.max(0, cursorLine - 2);
    const excerptEnd = Math.min(lines.length, hintLine + 2);
    const excerpt = lines.slice(excerptStart, excerptEnd).join("\n");

    this.db.logEvent(slot.slot, "answer_prompt_block_detected", null, null, {
      slot: slot.slot,
      task: slot.task,
      issue: slot.issue,
      hint_line: hintLine,
      cursor_line: cursorLine,
      excerpt,
    });

    debugLog(
      `[stuck] slot=${slot.slot} fire-block-direct hint=${hintLine} cursor=${cursorLine}`
    );

    // Log the dispatch BEFORE injecting so the next tick's dedup sees it.
    this.db.logEvent(slot.slot, "block_dispatched", null, null, {
      slot: slot.slot,
      hint_line: hintLine,
      cursor_line: cursorLine,
    });
    this.lastBlockMatchLine.set(slot.slot, hintLine);

    // Write the menu capture so PM's /slot-blocked skill can read it.
    try {
      writeFileSync(`/tmp/slot-${slot.slot}-blocked-capture.txt`, pane);
    } catch (e) {
      debugLog(
        `[stuck] slot=${slot.slot} block-capture-write-failed: ${(e as Error).message}`
      );
    }

    // Inject the slash command into PM pane — mirrors /slot-context-overflow.
    this.relay.injectToPM(`/slot-blocked ${slot.slot}`);
  }

  /**
   * Return the line index (0-based, from top of capture buffer) of the
   * LAST occurrence of the matched pattern. -1 if not found. Used for
   * positional dedup independent of textual hash signature.
   */
  private computeMatchLineIndex(pane: string, rx: RegExp): number {
    const lines = pane.split("\n");
    for (let i = lines.length - 1; i >= 0; i--) {
      if (rx.test(lines[i])) return i;
    }
    return -1;
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

    const waitMs = Date.now() - parseDbTimestampMs(slot.last_activity);
    if (waitMs < this.PLAN_APPROVAL_THRESHOLD_MS) return;

    // Dedup: don't spam — check if we already notified within the window
    const recentEvents = this.db.getEvents(slot.slot, 1, "plan_approval_stale");
    if (recentEvents.length > 0) {
      const lastNotifiedMs = parseDbTimestampMs(recentEvents[0].timestamp);
      if (Date.now() - lastNotifiedMs < this.DEDUP_WINDOW_MS) {
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
      const lastNotifiedMs = parseDbTimestampMs(recentEvents[0].timestamp);
      if (Date.now() - lastNotifiedMs < this.DEDUP_WINDOW_MS) {
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
