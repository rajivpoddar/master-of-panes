/**
 * MoP Stuck Detector — Watchdog for unresponsive slots
 *
 * Checks all occupied, non-idle, non-DND slots every 60 seconds.
 * If a slot's log file hasn't been modified in 5+ minutes, it's
 * considered "stuck" and PM is notified.
 *
 * Dedup: Only notifies PM once per 10 minutes per slot to prevent spam.
 */

import { appendFileSync, readFileSync, writeFileSync, unlinkSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { execShell } from "./asyncCommand.js";
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
    /API Error: 400[\s\S]*?maximum context length/i,
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
  // Auto-/compact injection is opt-in. check-slot-bg may stay enabled for
  // telemetry without causing MoP to type /compact into worker panes.
  private readonly AUTO_COMPACT_INJECTION_ENABLED =
    process.env.MOP_AUTO_COMPACT_DISABLED !== "1" &&
    process.env.MOP_AUTO_COMPACT_INJECTION_ENABLED === "1";
  // Per-slot last fired block-prompt match line offset. Mirrors
  // lastMatchLine for the block detector. Reset on idle→active transition
  // so a NEW block prompt later in the session can fire even if its line
  // offset happens to be ≤ a previous fire offset.
  private lastBlockMatchLine: Map<number, number> = new Map();
  // Per-slot consecutive bg-script failure count. Incremented each detector
  // tick that sees a [check-slot] Slot N bg-script FAILED line in the MoP
  // server log. Reset when a tick passes without a failure for the slot.
  private bgScriptFailures: Map<number, number> = new Map();
  // Dedup: don't re-inject /compact for bg-script failures within 10 min.
  private readonly BG_SCRIPT_FAILURE_DEDUP_MS = 10 * 60 * 1000;
  // Bg-script failure is INFRA error (Codex CLI auth, ETIMEDOUT, script crash),
  // NOT slot-stuck signal. Require an ADDITIONAL real-slot-stuck signal before
  // firing /compact: slot's tmux pipe-pane log mtime older than
  // BG_SCRIPT_COMPACT_LOG_STALE_MS = the slot has produced no output recently
  // and is plausibly stuck. If log is fresh, the slot is actively running —
  // bg-script failure is its OWN problem (PM-direct investigation needed), not
  // a slot to compact.
  // Per Rajiv directive 2026-05-17 12:33+12:40 IST thread `1779001405.544099`:
  //   *"are you triggering /compact on slot 1 and 2? check MoP logs? why?"*
  //   *"was this fixed?"*
  // Companion: feedback_mop_bg_script_failure_compact_misfire_2026_05_17.md
  private readonly BG_SCRIPT_COMPACT_LOG_STALE_MS = 30 * 60 * 1000; // 5 → 30 min (Rajiv directive 2026-05-25 13:28 IST channel C0ALZJHGE49 thread 1779695516.850089 — slot 4 false-positive compacts during long-thinking bursts that exceed 5min)

  // ─── API 500 backoff detector ───────────────────────────────────────────
  // Colocated with autocompact detector per Rajiv directive 2026-05-17 07:59 IST
  // thread `1778957625.997439`: *"note that mop also detects context limit
  // reached and injects 'continue your work' after autocompact. this change
  // should do in the same place."*
  //
  // Prior bg-script path (check-slot-bg.sh Step 1a → INJECT_DECISION:mop-
  // direct-nudge marker → hooks.ts handleApi500MopDirectNudge) had a fatal
  // flaw: MoP skips check-slot-bg.sh when slot is `idle:true` (between tool
  // calls). When a slot is stuck at an API 500 error waiting for retry, the
  // slot IS idle from MoP's view → bg-script never runs → marker never emitted
  // → nudge never sent. Slots 1+3 stayed stuck 1h+ on 2026-05-17 morning
  // through that path.
  //
  // This detector colocates with checkContextOverflow — same setInterval timer,
  // same tmux capture-pane mechanism, same direct-injection delivery. Runs
  // regardless of slot idle state since the timer doesn't gate on idleness.
  //
  // Backoff schedule (cumulative from first_seen_ts):
  //   retry 0: nudge at +120s (2m suppress)
  //   retry 1: nudge at +360s (next 4m suppress; cumulative 6m)
  //   retry 2: nudge at +840s (next 8m suppress; cumulative 14m)
  //   retry 3: nudge at +1800s (next 16m suppress; cumulative 30m)
  //   retry 4: nudge at +3720s (next 32m suppress; cumulative 62m)
  //   retry ≥5: cap → PM-surfaced as bg-script API_500_PERSISTENT
  //
  // State file: /tmp/slot-N-api500-state.json
  //   {first_seen_ts, retry_count, next_nudge_at, last_500_ts, last_nudge_at}
  //   Cleared when tmux scrollback no longer shows the error (recovery).
  private readonly API_500_PATTERN =
    /API Error: 500|API Error: 529|Internal server error/;
  private readonly API_500_BACKOFF_SCHEDULE = [120, 360, 840, 1800, 3720];
  private readonly API_500_RETRY_CAP = 5;

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
   * Reset bg-script failure tracking for a slot. Called by hooks.ts when
   * SessionStart:compact fires (post-compact recovery). After /compact
   * completes the failure count should reset so a genuine new failure
   * series is detected fresh.
   */
  resetBgScriptFailureTracking(slotNum: number): void {
    this.bgScriptFailures.delete(slotNum);
    debugLog(`[stuck] slot=${slotNum} bg-script failure tracking reset (post-compact)`);
  }

  /**
   * Check all slots for stuck state.
   * A slot is stuck when:
   * - occupied = true (assigned to a task)
   * - idle = false (not at prompt — actively processing)
   * - dnd = false (not under manual control)
   * - Log mtime > STUCK_THRESHOLD_MS ago (no output produced)
   */
  async checkAll(): Promise<void> {
    const slots = this.db.getAllSlots();

    // Phase 1: context-overflow detection for active+occupied DEV slots only.
    // Slot 0 (PM) is intentionally excluded: Rajiv removed the PM-pane
    // auto-continue path, so MoP should no longer auto-compact PM and then
    // inject "continue your work". Dev slots still keep the direct-recovery
    // path because their overflow remediation remains deterministic.
    //
    // Skip released slots: nothing to recover, and the banner won't appear
    // because Claude isn't actively running tools there. checkContextOverflow
    // also has its own idle-prompt + dedup + in-flight guards.
    // (2026-04-29 analysis /tmp/mop-compact-hook-analysis.md)
    for (const slot of slots) {
      if (slot.slot === 0) continue;
      if (slot.dnd) continue;
      if (!slot.occupied) continue;
      await this.checkContextOverflow(slot);
    }

    // Phase 1a-API500: API 500 backoff detection colocated with autocompact.
    // Rajiv directive 2026-05-17 07:59 IST thread `1778957625.997439`:
    //   *"note that mop also detects context limit reached and injects
    //   'continue your work' after autocompact. this change should do in the
    //   same place."*
    // Applies to slots 1..N (not PM/slot 0). Same tmux-grep + direct-injection
    // mechanism as checkContextOverflow, but on the API 500 error string.
    // Runs every 60s regardless of slot idle state.
    for (const slot of slots) {
      if (slot.slot === 0) continue;
      if (slot.dnd) continue;
      if (!slot.occupied) continue;
      await this.checkApi500Backoff(slot);
    }

    // Phase 1b: answer-prompt block detection. Applies to ALL slots, not
    // just wrappers — any slot can hit a numbered-options menu mid-task
    // (codex-companion, plan approval, manual interactive shell, etc.).
    // PM resolves via /slot-blocked skill. (Rajiv directive 2026-04-30)
    for (const slot of slots) {
      if (slot.dnd) continue;
      if (slot.slot === 0) continue; // PM pane — no self-detection
      await this.detectAnswerPromptBlock(slot);
    }

    // Phase 1c: bg-script failure detection. Watches the MoP server log
    // for persistent [check-slot] Slot N bg-script FAILED lines. Two
    // consecutive failures (10 min = 2 check-slot cycles) indicate the
    // slot may be hitting resource limits or context exhaustion. Auto-
    // inject /compact directly (deduped, same guards as checkContextOverflow).
    this.detectBgScriptFailures();

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
  private async checkContextOverflow(slot: SlotState): Promise<void> {
    let pane = "";
    try {
      const { output } = await this.relay.captureOutput(slot.slot, 60);
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
      // API Error 400 (model context limit) is a provider-level rejection —
      // it never appears in PM tool output quoting other slots. Allow it
      // through without the canonical TUI banner check (Rajiv directive
      // 2026-05-14 16:17 IST thread 1778754566.417819).
      const isApiContextError = /API Error: 400[\s\S]*?maximum context length/i.test(pane);
      if (!isApiContextError) {
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
    }

    // Idle-prompt check REMOVED 2026-05-09 (Rajiv directive thread 1778329240.526399):
    // raine GPT-5.5 chevron rendering can confuse is-active.sh, causing the
    // detector to defer indefinitely. Slot 1 stuck ~58min at context-wall on
    // 2026-05-09 because of this. Send /compact regardless; existing dedup
    // (signature, line-offset, in-flight, post-compact) prevents duplicate fires.
    // The original duplicate-/compact-into-input-buffer concern (2026-04-30)
    // is mitigated by the line-offset + signature dedup landed since.
    console.log(
      `[stuck-info] slot=${slot.slot} overflow-banner-detected pattern=${matched.source} pane_tail=${pane.slice(-200).replace(/\n/g, "\\n")}`
    );

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
      console.log(
        `[stuck-info] slot=${slot.slot} suppress=stale-line-offset curr=${matchLine} last=${lastLine}`
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
        console.log(
          `[stuck-info] slot=${slot.slot} suppress=stale-scrollback sig=${signature.slice(0, 12)}`
        );
        return;
      }

      // Time window (covers brief flapping or different-signature near-miss).
      if (Date.now() - lastTs < this.CONTEXT_OVERFLOW_DEDUP_MS && !lastIsPreResume) {
        console.log(
          `[stuck-info] slot=${slot.slot} suppress=time-window age_ms=${Date.now() - lastTs}`
        );
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
      console.log(
        `[stuck-info] slot=${slot.slot} suppress=compact-in-flight age_ms=${Date.now() - inFlightAt}`
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
        console.log(
          `[stuck-info] slot=${slot.slot} suppress=post-compact sig=${signature.slice(0, 12)} age_ms=${Date.now() - lastDispatchTs}`
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

    console.log(
      `[stuck-info] slot=${slot.slot} overflow-direct-candidate sig=${signature.slice(0, 12)} pattern=${matched.source}`
    );

    if (!this.AUTO_COMPACT_INJECTION_ENABLED) {
      this.db.logEvent(
        slot.slot,
        "compact_suppressed_disabled",
        null,
        null,
        {
          slot: slot.slot,
          task: slot.task,
          issue: slot.issue,
          matched_pattern: matched.source,
          match_signature: signature,
          capture_excerpt: pane.slice(-400),
          action: "suppressed_auto_compact_disabled",
        }
      );
      if (matchLine >= 0) {
        this.lastMatchLine.set(slot.slot, matchLine);
      }
      console.log(
        `[stuck-info] slot=${slot.slot} suppress=auto-compact-disabled sig=${signature.slice(0, 12)}`
      );
      return;
    }

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
   * API 500 backoff detector — colocated with autocompact "continue your
   * work" injection per Rajiv directive 2026-05-17 07:59 IST thread
   * `1778957625.997439`.
   *
   * Tmux-grep input model (refactor 2026-05-17 07:40 IST):
   *   tmux capture-pane -p -S -100 | grep -qE "API Error: 500|API Error:
   *   529|Internal server error"
   *
   * On detection:
   *   - First seen: initialize state {first_seen_ts, retry_count: 0,
   *     next_nudge_at: now + 120s, last_500_ts}; suppress this tick.
   *   - In backoff window (now < next_nudge_at): suppress.
   *   - Window expired (now >= next_nudge_at) AND retry_count < cap:
   *     direct-inject "continue your work" via relay.sendToSlot(slot,
   *     "continue your work", true). On success, bump retry_count + reschedule
   *     next_nudge_at against next backoff index. On failure, do NOT bump —
   *     next 60s tick will retry the injection.
   *   - retry_count >= cap: log PERSISTENT line; don't auto-nudge.
   *
   * On non-detection AND state file exists: slot recovered (error scrolled
   * out or slot resumed rendering tool output). Clear state file.
   *
   * State file: /tmp/slot-N-api500-state.json
   * Forensic log: /tmp/mop-api500-nudges.log
   */
  private async checkApi500Backoff(slot: SlotState): Promise<void> {
    const slotNum = slot.slot;
    const stateFile = `/tmp/slot-${slotNum}-api500-state.json`;
    const logFile = `/tmp/mop-api500-nudges.log`;

    // 1. Capture last 100 lines of slot's tmux pane scrollback.
    let pane = "";
    try {
      const result = await execShell(`tmux capture-pane -t 0:0.${slotNum} -p -S -100`, {
        timeout: 5_000,
      });
      pane = result.stdout;
    } catch {
      return; // capture failure — try again next tick
    }

    const detected = this.API_500_PATTERN.test(pane);
    const nowEpoch = Math.floor(Date.now() / 1000);

    // 2. Load existing state file (if any).
    let state: {
      first_seen_ts: number;
      retry_count: number;
      next_nudge_at: number;
      last_500_ts: number;
      last_nudge_at?: number;
    } | null = null;
    try {
      if (existsSync(stateFile)) {
        const parsed = JSON.parse(readFileSync(stateFile, "utf8"));
        if (parsed && typeof parsed === "object" &&
            typeof parsed.first_seen_ts === "number") {
          state = parsed;
        }
      }
    } catch {
      state = null;
    }

    // 3. Recovery branch — error string no longer in scrollback.
    if (!detected) {
      if (state) {
        try {
          unlinkSync(stateFile);
        } catch { /* non-fatal */ }
        const msg = `[api500-nudge] ${new Date().toISOString()} slot ${slotNum} RECOVERED — error string no longer in scrollback; state file cleared`;
        debugLog(msg);
        try {
          appendFileSync(logFile, msg + "\n");
        } catch { /* non-fatal */ }
      }
      return;
    }

    // 4. Detected. Initialize state if missing.
    if (!state) {
      state = {
        first_seen_ts: nowEpoch,
        retry_count: 0,
        next_nudge_at: nowEpoch + this.API_500_BACKOFF_SCHEDULE[0],
        last_500_ts: nowEpoch,
      };
      try {
        writeFileSync(stateFile, JSON.stringify(state));
      } catch { /* non-fatal */ }
      const msg = `[api500-nudge] ${new Date().toISOString()} slot ${slotNum} FIRST_SEEN — state initialized retry_count=0 next_nudge_at=${state.next_nudge_at} (in ${this.API_500_BACKOFF_SCHEDULE[0]}s)`;
      debugLog(msg);
      try {
        appendFileSync(logFile, msg + "\n");
      } catch { /* non-fatal */ }
      return;
    }

    // 5. State exists. Refresh last_500_ts (metadata only).
    if (nowEpoch > (state.last_500_ts || 0)) {
      state.last_500_ts = nowEpoch;
      try {
        writeFileSync(stateFile, JSON.stringify(state));
      } catch { /* non-fatal */ }
    }

    // 6. Cap check — already past retry limit.
    if (state.retry_count >= this.API_500_RETRY_CAP) {
      const firstSeenAge = nowEpoch - state.first_seen_ts;
      const msg = `[api500-nudge] ${new Date().toISOString()} slot ${slotNum} PERSISTENT — retry_count=${state.retry_count} (past cap of ${this.API_500_RETRY_CAP}) first_seen_age=${firstSeenAge}s; auto-nudge no longer applies, PM triage required`;
      debugLog(msg);
      // Only log to file once per minute to avoid spam.
      try {
        const lastLog = state.last_nudge_at || 0;
        if (nowEpoch - lastLog > 60) {
          appendFileSync(logFile, msg + "\n");
          state.last_nudge_at = nowEpoch;
          writeFileSync(stateFile, JSON.stringify(state));
        }
      } catch { /* non-fatal */ }
      return;
    }

    // 7. In suppression window — skip injection.
    if (nowEpoch < (state.next_nudge_at || 0)) {
      const remaining = state.next_nudge_at - nowEpoch;
      debugLog(
        `[api500-nudge] slot=${slotNum} backoff window (retry_count=${state.retry_count} next_nudge_in=${remaining}s) — suppress`
      );
      return;
    }

    // 8. Window expired AND retry_count < cap — inject "continue your work".
    // Verify slot is occupied + not DND (caller already filtered, defensive).
    const dbSlot = this.db.getSlot(slotNum);
    if (!dbSlot?.occupied || dbSlot.dnd) {
      const msg = `[api500-nudge] ${new Date().toISOString()} slot ${slotNum} SKIPPED (occupied=${!!dbSlot?.occupied} dnd=${!!dbSlot?.dnd}) retry=${state.retry_count}`;
      debugLog(msg);
      try {
        appendFileSync(logFile, msg + "\n");
      } catch { /* non-fatal */ }
      return;
    }

    // 9. Inject via tmux paste-buffer. force=true to land regardless of pane
    //    prompt state — same shape as SessionStart:compact direct-recovery.
    let ok = false;
    try {
      ok = this.relay.sendToSlot(slotNum, "continue your work", true);
    } catch (err) {
      debugLog(`[api500-nudge] slot=${slotNum} sendToSlot threw: ${err}`);
      ok = false;
    }

    if (!ok) {
      const msg = `[api500-nudge] ${new Date().toISOString()} slot ${slotNum} FAILED (sendToSlot returned false) retry=${state.retry_count} — state NOT ticked; will retry next tick`;
      debugLog(msg);
      try {
        appendFileSync(logFile, msg + "\n");
      } catch { /* non-fatal */ }
      return;
    }

    // 10. Success — bump retry_count + reschedule next_nudge_at against the
    //     NEXT backoff index. Cumulative from first_seen_ts.
    state.retry_count = (state.retry_count || 0) + 1;
    state.last_nudge_at = nowEpoch;
    if (state.retry_count >= this.API_500_RETRY_CAP) {
      // Just crossed cap — write final state, no further scheduling.
    } else {
      const delta =
        this.API_500_BACKOFF_SCHEDULE[state.retry_count] ||
        this.API_500_BACKOFF_SCHEDULE[this.API_500_BACKOFF_SCHEDULE.length - 1];
      // Schedule cumulatively from first_seen_ts to preserve
      // 2m/6m/14m/30m/62m semantics across ticks.
      state.next_nudge_at = state.first_seen_ts + delta;
    }
    try {
      writeFileSync(stateFile, JSON.stringify(state));
    } catch { /* non-fatal */ }

    const msg = `[api500-nudge] ${new Date().toISOString()} slot ${slotNum} OK retry_count=${state.retry_count} next_nudge_at=${state.next_nudge_at} (cumulative from first_seen_ts=${state.first_seen_ts})`;
    debugLog(msg);
    try {
      appendFileSync(logFile, msg + "\n");
    } catch { /* non-fatal */ }

    // Log MoP DB event for forensic timeline (mirrors prior
    // slot_idle_suppressed_api500_mop_direct event shape).
    try {
      this.db.logEvent(slotNum, "api500_direct_nudge", "Stuck", null, {
        relay_path: "stuck.checkApi500Backoff",
        retry_count: state.retry_count,
        first_seen_ts: state.first_seen_ts,
        next_nudge_at: state.next_nudge_at,
      });
    } catch { /* non-fatal */ }
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
  private async detectAnswerPromptBlock(slot: SlotState): Promise<void> {
    let pane = "";
    try {
      const { output } = await this.relay.captureOutput(slot.slot, 50);
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
    if (await this.relay.isSlotActive(slot.slot)) {
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
   * Detect persistent check-slot bg-script failures from the MoP server log.
   *
   * The check-slot bg-script fires every ~5 min per slot. When it fails
   * repeatedly (≥2 failures for the same slot in the log tail), the slot
   * may be hitting resource limits or context exhaustion. Auto-inject
   * /compact directly into the affected slot.
   *
   * Dedup: logs a `bg_script_failure_compact` event per slot; no re-fire
   * within BG_SCRIPT_FAILURE_DEDUP_MS (10 min). Also respects the existing
   * compactInFlightAt and COMPACT_DISPATCH_DEDUP_MS guards.
   */
  private detectBgScriptFailures(): void {
    const LOG_PATH = "/tmp/mop-server.log";
    let logText: string;
    try {
      logText = readFileSync(LOG_PATH, "utf-8");
    } catch {
      return; // log file not available yet
    }

    const lines = logText.split("\n");
    const tail = lines.slice(-200);
    const regex = /\[check-slot\].*Slot (\d+) bg-script FAILED/;

    // Count unique failure occurrences per slot. Each check-slot tick
    // produces a distinct log line (different timestamp), so ≥2 lines
    // for the same slot = ≥2 consecutive failed ticks.
    const failureCounts = new Map<number, number>();
    for (const line of tail) {
      const match = line.match(regex);
      if (match) {
        const slotNum = parseInt(match[1], 10);
        failureCounts.set(slotNum, (failureCounts.get(slotNum) || 0) + 1);
      }
    }

    const slots = this.db.getAllSlots();

    for (const slot of slots) {
      if (slot.dnd) continue;
      if (!slot.occupied) continue;

      const failCount = failureCounts.get(slot.slot) || 0;

      if (failCount >= 2) {
        // Update the per-slot consecutive failure tracker.
        const prev = this.bgScriptFailures.get(slot.slot) || 0;
        this.bgScriptFailures.set(slot.slot, prev + 1);

        console.log(
          `[stuck-info] slot=${slot.slot} bg-script-failure consecutive_fails=${prev + 1} log_lines=${failCount}`
        );

        // Dedup: don't re-fire within 10 min of last bg_script_failure_compact.
        const recentEvents = this.db.getEvents(
          slot.slot,
          1,
          "bg_script_failure_compact"
        );
        if (recentEvents.length > 0) {
          const lastTs = parseDbTimestampMs(recentEvents[0].timestamp);
          if (
            Date.now() - lastTs <
            this.BG_SCRIPT_FAILURE_DEDUP_MS
          ) {
            console.log(
              `[stuck-info] slot=${slot.slot} suppress=bg-script-failure-dedup age_ms=${Date.now() - lastTs}`
            );
            continue;
          }
        }

        // In-flight guard: don't send /compact if one was just dispatched.
        const inFlightAt = this.compactInFlightAt.get(slot.slot);
        if (
          inFlightAt !== undefined &&
          Date.now() - inFlightAt < this.COMPACT_INFLIGHT_DEDUP_MS
        ) {
          console.log(
            `[stuck-info] slot=${slot.slot} suppress=compact-in-flight age_ms=${Date.now() - inFlightAt}`
          );
          continue;
        }

        // Post-/compact dispatch guard: don't double-fire.
        const lastDispatch = this.db.getEvents(slot.slot, 1, "compact_dispatched");
        if (lastDispatch.length > 0) {
          const lastDispatchTs = parseDbTimestampMs(lastDispatch[0].timestamp);
          if (
            Date.now() - lastDispatchTs <
            this.COMPACT_DISPATCH_DEDUP_MS
          ) {
            console.log(
              `[stuck-info] slot=${slot.slot} suppress=post-compact age_ms=${Date.now() - lastDispatchTs}`
            );
            continue;
          }
        }

        // Real-slot-stuck signal gate (Rajiv directive 2026-05-17 12:33 IST
        // thread `1779001405.544099`). Bg-script failure alone = INFRA error
        // (Codex CLI auth expired, spawnSync ETIMEDOUT, script crash). It does
        // NOT mean the slot itself is stuck. /compact on an actively producing
        // slot destroys productive context.
        //
        // Require: slot's tmux pipe-pane log mtime is OLDER than
        // BG_SCRIPT_COMPACT_LOG_STALE_MS. If the slot has produced output
        // recently, it's actively running — skip /compact, alarm PM instead.
        //
        // See feedback_mop_bg_script_failure_compact_misfire_2026_05_17.md.
        const slotLogMtime = this.logManager.getLogMtime(slot.slot);
        if (slotLogMtime) {
          const logAgeMs = Date.now() - slotLogMtime.getTime();
          if (logAgeMs < this.BG_SCRIPT_COMPACT_LOG_STALE_MS) {
            // Rajiv directive 2026-05-27 19:14 IST channel C0ALZJHGE49
            // thread `1779889477.891309`: "fix the classifier with a bg agent
            // and restart MoP". The PM was getting 100+ false-positive
            // bg-script-failed warnings on actively-producing slots (latest
            // 109 consecutive ticks for slot 3 during a legitimate 60+min
            // DeepSeek v4 Flash xhigh envisioning cycle).
            //
            // When the slot's tmux log is FRESH (active < threshold), the
            // bg-script telemetry failure is uninteresting infra noise —
            // slot is actually working, just no Codex heartbeat. Do NOT
            // inject a PM warning. Reset the consecutive-fails counter so
            // the count doesn't grow unbounded into the next genuine wedge.
            //
            // Event row STILL logged (for observability + 10-min dedup), but
            // with action="suppressed_silent_slot_active" so it's distinct
            // from a real wedge.
            console.log(
              `[stuck-info] slot=${slot.slot} suppress=bg-script-compact-slot-active-silent log_age_ms=${logAgeMs} threshold_ms=${this.BG_SCRIPT_COMPACT_LOG_STALE_MS} — slot actively producing output, no PM alarm`
            );
            this.db.logEvent(
              slot.slot,
              "bg_script_failure_compact",
              null,
              null,
              {
                slot: slot.slot,
                consecutive_fails: prev + 1,
                log_lines: failCount,
                task: slot.task,
                issue: slot.issue,
                action: "suppressed_silent_slot_active",
                log_age_ms: logAgeMs,
              }
            );
            // Reset the consecutive-fails counter — slot is healthy from
            // the standpoint of producing output, so don't carry a stale
            // tick count forward (which inflates the "109 ticks" framing
            // when the next genuine bg-script glitch occurs).
            this.bgScriptFailures.set(slot.slot, 0);
            // NOTE: PM inject removed. To re-enable for debug:
            //   this.relay.injectToPM(
            //     `# warning slot ${slot.slot} bg-script failing (${prev + 1} consecutive ticks) — slot itself active (log age ${Math.round(logAgeMs / 1000)}s). Likely Codex CLI auth / ETIMEDOUT / script crash. /compact SUPPRESSED. PM-direct investigation needed.`
            //   );
            continue;
          }
        }

        // Log detection event before dispatch so next tick sees dedup.
        this.db.logEvent(
          slot.slot,
          "bg_script_failure_compact",
          null,
          null,
          {
            slot: slot.slot,
            consecutive_fails: prev + 1,
            log_lines: failCount,
            task: slot.task,
            issue: slot.issue,
          }
        );

        console.log(
          `[stuck-info] slot=${slot.slot} bg-script-failure-compact-candidate consecutive_fails=${prev + 1}`
        );

        if (!this.AUTO_COMPACT_INJECTION_ENABLED) {
          this.db.logEvent(
            slot.slot,
            "bg_script_failure_compact",
            null,
            null,
            {
              slot: slot.slot,
              consecutive_fails: prev + 1,
              log_lines: failCount,
              task: slot.task,
              issue: slot.issue,
              action: "suppressed_auto_compact_disabled",
            }
          );
          console.log(
            `[stuck-info] slot=${slot.slot} suppress=auto-compact-disabled trigger=bg-script-failure consecutive_fails=${prev + 1}`
          );
          continue;
        }

        // Log compact dispatch for post-compact dedup.
        this.db.logEvent(slot.slot, "compact_dispatched", null, null, {
          slot: slot.slot,
          trigger: "bg_script_failure",
          consecutive_fails: prev + 1,
        });

        // Mark in-flight and send /compact (same direct-recovery path as
        // checkContextOverflow — Rajiv directive 2026-04-30).
        this.compactInFlightAt.set(slot.slot, Date.now());
        this.relay.sendToSlot(slot.slot, "/compact");
      } else {
        // No (or only 1) failure line in log tail for this slot — reset
        // the consecutive failure counter.
        if (this.bgScriptFailures.has(slot.slot)) {
          this.bgScriptFailures.delete(slot.slot);
          debugLog(
            `[stuck] slot=${slot.slot} bg-script failure count reset (tick passed without ≥2 failures)`
          );
        }
      }
    }
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
        void this.checkAll();
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
