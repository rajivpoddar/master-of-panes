/**
 * MoP Hook Processor — Business logic for incoming Claude Code HTTP hooks
 *
 * Receives hook payloads, logs them, detects significant events (slot idle,
 * plan ready), updates slot state, and relays notifications to PM.
 */

import { execSync } from "node:child_process";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";
import type { HookPayload, HookResponse } from "./types.js";

function debugLog(line: string): void {
  try {
    appendFileSync(
      "/tmp/mop-debug.log",
      `${new Date().toISOString()} ${line}\n`
    );
  } catch {
    // never fail hook processing on log write errors
  }
}

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
   * ExitPlanMode stores here; Stop handler sends to PM once the prompt renders.
   * This prevents the race where PM sends "2" before the slot shows the prompt.
   *
   * IMPORTANT: Only ExitPlanMode sets this — NOT plan file writes.
   * Plan file writes during revision would trigger repeated notifications.
   * (Bug fix 2026-03-18: plan-ready fired on every Write to docs/plans/ during
   * revision, causing 6+ duplicate notifications. Now ties to ExitPlanMode only.)
   */
  private pendingPlanReady = new Map<number, { issueNum: number; planFile: string; isRevision?: boolean }>();

  /**
   * Last plan file written per slot. Recorded on Write/Edit to docs/plans/*.md.
   * Used to populate pendingPlanReady with the correct filename.
   */
  private lastPlanFile = new Map<number, string>();

  /**
   * Timestamp of last plan-ready notification sent per slot.
   * Used to detect revisions (within cooldown = revision, outside = first submission).
   */
  private lastPlanReadySent = new Map<number, number>();

  /** Cooldown period for revision detection */
  private static readonly PLAN_READY_COOLDOWN_MS = 5 * 60 * 1000;

  /**
   * Write-debounce timers for plan files, keyed by slot number.
   * Each Write/Edit to docs/plans/*.md resets this timer. When it fires (10s after
   * last write), we set pendingPlanReady. The Stop handler then polls for the prompt.
   * This replaces the ExitPlanMode-based trigger — handles both initial plans and revisions.
   * (Rajiv directive 2026-03-19: "debounce on MoP side, trigger only after last hook fire
   * and tmux capture shows the plan approval prompt")
   */
  private planWriteDebounceTimers = new Map<number, ReturnType<typeof setTimeout>>();

  /** Debounce delay after last plan file write before triggering plan-ready */
  private static readonly PLAN_WRITE_DEBOUNCE_MS = 10_000;

  /**
   * Idle notification debounce timers, keyed by slot number.
   * When a Stop hook fires, instead of immediately notifying PM, we start a 60s timer.
   * If the slot becomes active (PostToolUse/UserPromptSubmit with idle→active transition)
   * within that window, the timer is cancelled — no notification sent.
   * This prevents 24+ duplicate idle notifications per session.
   * (Rajiv directive 2026-03-31: "debounce on MoP side, wait 60s, cancel on re-activation")
   */
  private pendingIdleTimers = new Map<number, ReturnType<typeof setTimeout>>();

  /** Idle debounce delay: 60 seconds (Rajiv directive 2026-05-15 12:02 IST thread `1778825746.293759`:
   * bumped from 30s → 60s to reduce duplicate slot-notification injection. Modern slots run longer
   * atomic tool sequences than 2026-04-01 baseline; 30s window fired during mid-sequence "pause"
   * causing 5+ FIRINGs in 30min on healthy slots. See feedback memo dated 2026-05-15.) */
  private static readonly IDLE_DEBOUNCE_MS = 60_000;

  /**
   * Slot-idle staleness gate window — after the IDLE_DEBOUNCE_MS timer fires,
   * suppress /slot-idle if a Task subagent dispatched within this many seconds
   * AND has no later Stop event. (Rajiv directive 2026-05-05: PM nudge interrupted
   * slot 4 plan-agent — subagent dispatch fired 43s after the JSONL classifier
   * captured "subagent_active=false".)
   *
   * Set to 90s — covers the full 30s debounce + a generous tail for late Task
   * dispatches that ride the same Stop window. Mirrors check-slot's lazy
   * "skip when idle" semantics.
   */
  private static readonly IDLE_STALENESS_GATE_SEC = 90;

  /**
   * Slot-idle recent-tool-fire gate. Independent of subagent detection: if any
   * PostToolUse event fired within this many seconds of the debounce expiry,
   * the slot is still doing work (just bounced through Stop between tool calls).
   * Set to 15s — slightly longer than the typical inter-tool gap (~5-8s) so
   * an active slot's normal cadence doesn't trigger /slot-idle relays.
   */
  private static readonly IDLE_RECENT_TOOL_GATE_SEC = 15;

  /**
   * Plan approval timeout timers, keyed by slot number.
   * Started when plan-ready notification is sent to PM; cleared on ExitPlanMode
   * or slot release. If 15 minutes elapse with no approval, re-sends notification.
   * Rajiv directive 2026-03-13: "no slot should be stuck on plan-ready for more than 15m."
   */
  private planApprovalTimers = new Map<number, ReturnType<typeof setTimeout>>();

  /** Plan approval timeout duration: 15 minutes */
  private static readonly PLAN_APPROVAL_TIMEOUT_MS = 15 * 60 * 1000;

  /**
   * Check-slot periodic timers, keyed by slot number.
   * Started when slot transitions idle→active. Every 5 minutes, captures tmux
   * output to /tmp/slot-N-check.txt and injects /check-slot N into PM pane.
   * Cleared when slot goes idle (Stop hook).
   * Rajiv directive 2026-04-03: "trigger check slot from MoP instead of through the loop"
   */
  private checkSlotTimers = new Map<number, ReturnType<typeof setInterval>>();

  /** Check-slot interval: 5 minutes */
  private static readonly CHECK_SLOT_INTERVAL_MS = 5 * 60 * 1000;
  /**
   * Emergency containment: keep the heavyweight check-slot classifier out of
   * the MoP HTTP process unless explicitly re-enabled. The classifier can run
   * Codex/gh/tmux work for up to 30s per active slot; doing that via execSync
   * inside this process starves /health and the hourly ops scheduler.
   */
  private static readonly CHECK_SLOT_BG_ENABLED =
    process.env.MOP_CHECK_SLOT_BG_ENABLED === "1";

  constructor(
    private db: MoPDatabase,
    private relay: TmuxRelay
  ) {}

  /**
   * Wire a StuckDetector reference. server.ts calls this so SessionStart:compact
   * recovery can reach into the detector's per-slot lastMatchLine tracker.
   * Currently a no-op — the detector is independently managed in server.ts.
   * Method exists so server.ts:52 doesn't fail at runtime.
   * (Pre-existing call from commit 463e392; declaration was missing.)
   */
  setStuckDetector(_detector: unknown): void {
    // intentionally no-op — see method docstring
  }

  /**
   * Start periodic check-slot timer for an active slot.
   * Fires every 5 minutes: captures tmux, writes to file, injects /check-slot N.
   * Idempotent: if a timer is already running for this slot, keeps it (doesn't restart).
   * This prevents rapid idle↔active transitions from killing the timer.
   */
  private startCheckSlotTimer(slotNum: number): void {
    // If timer already exists, keep it running — don't restart
    if (this.checkSlotTimers.has(slotNum)) {
      return;
    }

    const now = new Date().toISOString().slice(11, 19);
    console.log(`[check-slot] ${now} Starting timer for slot ${slotNum} (interval: ${HookProcessor.CHECK_SLOT_INTERVAL_MS}ms, active timers: ${this.checkSlotTimers.size})`);

    const timer = setInterval(() => {
      const now = new Date().toISOString().slice(11, 19);
      // Only fire if slot is occupied and not DND.
      // Do NOT check idle — slots are idle between tool calls (every few seconds).
      // The timer should survive through normal idle↔active cycling.
      // Only stop when truly unoccupied (released) or DND.
      const currentSlot = this.db.getSlot(slotNum);
      console.log(`[check-slot] ${now} Slot ${slotNum} timer tick — occupied=${currentSlot?.occupied}, idle=${currentSlot?.idle}, dnd=${currentSlot?.dnd}, task=${currentSlot?.task?.slice(0,30)}`);
      if (!currentSlot || !currentSlot.occupied || currentSlot.dnd) {
        console.log(`[check-slot] ${now} Slot ${slotNum} STOPPING timer — occupied=${currentSlot?.occupied}, dnd=${currentSlot?.dnd}`);
        this.stopCheckSlotTimer(slotNum);
        return;
      }

      // Skip firing if slot is momentarily idle (between tool calls), but keep timer alive
      if (currentSlot.idle) {
        console.log(`[check-slot] ${now} Slot ${slotNum} SKIP tick — idle between tool calls, timer stays alive`);
        return;
      }

      if (!HookProcessor.CHECK_SLOT_BG_ENABLED) {
        const checkFile = `/tmp/slot-${slotNum}-check.txt`;
        try {
          writeFileSync(
            checkFile,
            `STATUS:SKIP reason=check-slot-bg-disabled slot=${slotNum}`
          );
        } catch {
          // Best-effort diagnostic only.
        }
        console.log(
          `[check-slot] ${now} Slot ${slotNum} SKIPPED — check-slot-bg disabled in MoP HTTP process`
        );
        this.db.logEvent(slotNum, "check_slot_skipped", "Timer", null, {
          check_file: checkFile,
          reason: "check-slot-bg-disabled",
          interval_ms: HookProcessor.CHECK_SLOT_INTERVAL_MS,
        });
        return;
      }

      // Run bg-script GATE first — only inject when there's a real JSONL delta.
      // Bg-script (~/.claude/scripts/check-slot-bg.sh) emits "INJECT_DECISION:skip ..."
      // when the slot's JSONL hasn't changed since last tick (UNCHANGED path).
      // On Codex review path, the prompt template emits "INJECT_DECISION:inject" or
      // "INJECT_DECISION:skip" based on classified slot health.
      // Default behavior: if the marker is absent or the bg-script fails/times out,
      // FALL THROUGH to inject (preserves prior behavior, never drops a notification
      // due to gate-script issues).
      // (Rajiv directive 2026-05-08 20:34 IST — gate at MoP server, not PM-side.)
      const checkFile = `/tmp/slot-${slotNum}-check.txt`;
      const bgScript = `${process.env.HOME}/.claude/scripts/check-slot-bg.sh`;
      let bgOutput = "";
      let bgFailed = false;
      try {
        bgOutput = execSync(`bash ${bgScript} ${slotNum}`, {
          timeout: 30_000,
          maxBuffer: 1024 * 1024,
        }).toString();
      } catch (err) {
        bgFailed = true;
        const msg = err instanceof Error ? err.message : String(err);
        console.log(`[check-slot] ${now} Slot ${slotNum} bg-script FAILED (falling through to inject): ${msg.slice(0, 200)}`);
      }

      // Write bg output to /tmp/slot-N-check.txt.
      // On bg-script failure, write structured error — NOT tmux capture (deprecated).
      // Rajiv directive 2026-05-14 14:46 IST: tmux capture is not needed.
      try {
        if (!bgFailed && bgOutput.length > 0) {
          writeFileSync(checkFile, bgOutput);
        } else {
          writeFileSync(checkFile, `STATUS:ERROR reason=${bgFailed ? 'bg-script-failed' : 'empty-output'} slot=${slotNum}`);
        }
      } catch (err) {
        writeFileSync(checkFile, "STATUS:ERROR reason=write-failed");
        console.log(`[check-slot] Slot ${slotNum} write FAILED: ${err}`);
      }

      // Decide based on bg-script INJECT_DECISION marker.
      // skip → log + return (timer keeps running for next tick).
      // inject (or absent/failed) → fire injection.
      const skipMatch = bgOutput.match(/INJECT_DECISION:skip(?:\s+REASON:([^\n]*))?/);
      if (!bgFailed && skipMatch) {
        const reason = (skipMatch[1] || "unspecified").trim().slice(0, 120);
        console.log(`[check-slot] ${now} Slot ${slotNum} SKIPPED — bg-script INJECT_DECISION:skip REASON:${reason}`);
        this.db.logEvent(slotNum, "check_slot_skipped", "Timer", null, {
          check_file: checkFile,
          reason,
          interval_ms: HookProcessor.CHECK_SLOT_INTERVAL_MS,
        });
        return;
      }

      console.log(`[check-slot] ${now} Slot ${slotNum} FIRING — bg-script ${bgFailed ? "FAILED (falling through)" : "INJECT_DECISION:inject"}`);

      // Inject "MoP: check slot N for <reason>" + bg-output as ONE atomic
      // multi-line payload.
      // Rajiv directive 2026-05-15 13:44 IST thread `1778831723.165019`:
      // "make it inline" — eliminate /tmp/slot-N-check.txt file dependency;
      // PM sees prefix + summary as a single prompt.
      // Rajiv directive 2026-05-22 22:23 IST thread `1779468118.901709`:
      // "inject is a string instead of a command." Switched from slash
      // command `/check-slot N` to message prefix `MoP: check slot N for <reason>`.
      // PM-side pm-context-injector.sh recognizes the prefix and emits
      // [MoP_SLOT_NOTIFICATION] system-reminder hinting Skill(check-slot).
      // The relay detects \n and uses load-buffer + paste-buffer so embedded
      // newlines don't submit the prompt prematurely.
      const payload = bgFailed
        ? `STATUS:ERROR reason=bg-script-failed slot=${slotNum}`
        : (bgOutput.trimEnd() || `STATUS:ERROR reason=empty-output slot=${slotNum}`);
      // Derive a one-line <reason> from the bg-script summary so PM sees
      // the headline status without re-parsing the payload. Order of
      // preference: WARNING line > NEXT_ACTION line > HEALTH line > STATUS
      // line > fallback "routine timer". Strips the marker prefix so the
      // reason reads cleanly inline.
      let reason = "routine timer";
      if (bgFailed) {
        reason = "bg-script failed";
      } else {
        const warn = payload.match(/^WARNING:([^\n]+)/m);
        const next = payload.match(/^NEXT_ACTION:([^\n]+)/m);
        const health = payload.match(/^HEALTH:([^\n]+)/m);
        const status = payload.match(/^STATUS:([^\n]+)/m);
        if (warn) reason = `WARNING:${warn[1].trim()}`;
        else if (next) reason = `NEXT_ACTION:${next[1].trim()}`;
        else if (health) reason = `HEALTH:${health[1].trim()}`;
        else if (status) reason = `STATUS:${status[1].trim()}`;
        reason = reason.replace(/\s+/g, " ").slice(0, 120);
      }
      this.relay.injectToPM(`MoP: check slot ${slotNum} for ${reason}\n${payload}`);
      console.log(`[check-slot] Slot ${slotNum} — injected MoP message inline (reason="${reason.slice(0,60)}", payload ${payload.length} chars)`);
      this.db.logEvent(slotNum, "check_slot_triggered", "Timer", null, {
        check_file: checkFile,
        interval_ms: HookProcessor.CHECK_SLOT_INTERVAL_MS,
        bg_failed: bgFailed,
        payload_chars: payload.length,
        inline: true,
      });
    }, HookProcessor.CHECK_SLOT_INTERVAL_MS);

    if (timer.unref) timer.unref();
    this.checkSlotTimers.set(slotNum, timer);

    this.db.logEvent(slotNum, "check_slot_timer_started", "PostToolUse", null, {
      interval_ms: HookProcessor.CHECK_SLOT_INTERVAL_MS,
    });
  }

  /**
   * Stop check-slot timer for a slot (slot went idle or DND).
   */
  private stopCheckSlotTimer(slotNum: number): void {
    const existing = this.checkSlotTimers.get(slotNum);
    if (existing) {
      clearInterval(existing);
      this.checkSlotTimers.delete(slotNum);
      console.log(`[check-slot] Stopped timer for slot ${slotNum}`);
      this.db.logEvent(slotNum, "check_slot_timer_stopped", "Stop", null, {});
    }
  }

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
   * Poll for the plan approval prompt (numbered choices) before notifying PM.
   * The prompt renders AFTER Claude stops, so we check the pane output.
   * Retries every 5s for up to 60s, then sends anyway as a fallback.
   * (Rajiv directive 2026-03-18: "change MoP to look for the plan approval prompt")
   */
  private pollForPlanApprovalPrompt(
    slotNum: number,
    pending: { issueNum: number; planFile: string; isRevision?: boolean },
    attempt: number,
  ): void {
    const MAX_ATTEMPTS = 12; // 12 × 5s = 60s
    const POLL_INTERVAL_MS = 5_000;

    // MUST use tmux capture-pane directly — NOT relay.captureOutput() which prefers
    // log-based output (pipe-pane). The plan approval prompt is rendered by Claude Code's
    // TUI directly to the terminal, NOT to stdout — so the log file never captures it.
    // (Bug fix 2026-03-18: polling timed out every time because captureOutput returned
    // log content without the TUI prompt.)
    let output = "";
    try {
      const raw = execSync(`tmux capture-pane -t 0:0.${slotNum} -p -S -20`, { timeout: 5_000 });
      output = raw.toString();
    } catch { output = ""; }
    const hasPrompt = /Would you like to proceed|❯\s*1\.\s*Yes|ctrl-g to edit/i.test(output);

    if (hasPrompt || attempt >= MAX_ATTEMPTS) {
      // Prompt found (or timeout) — send notification to PM
      const reason = hasPrompt
        ? `Plan approval prompt detected on attempt ${attempt + 1}`
        : `Timeout after ${MAX_ATTEMPTS} attempts (${MAX_ATTEMPTS * 5}s) — sending anyway`;

      this.relay.notifyPlanReady(slotNum, pending.issueNum, pending.planFile, pending.isRevision ?? false);
      this.lastPlanReadySent.set(slotNum, Date.now()); // Record for cooldown
      this.db.logEvent(slotNum, "plan_ready_deferred_sent", "Stop", null, {
        issue: pending.issueNum,
        planFile: pending.planFile,
        attempt: attempt + 1,
        reason,
      });
      this.startPlanApprovalTimer(slotNum, pending.issueNum);
      return;
    }

    // Not found yet — retry after interval
    const timer = setTimeout(() => {
      this.pollForPlanApprovalPrompt(slotNum, pending, attempt + 1);
    }, POLL_INTERVAL_MS);
    if (timer.unref) timer.unref();
  }

  /**
   * Cancel pending idle notification timer for a slot.
   * Called when a slot becomes active during the 30s debounce window.
   * Returns true if a timer was cancelled, false if no timer was pending.
   */
  cancelPendingIdleTimer(slotNum: number): boolean {
    const existing = this.pendingIdleTimers.get(slotNum);
    if (existing) {
      clearTimeout(existing);
      this.pendingIdleTimers.delete(slotNum);
      return true;
    }
    return false;
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

  // handleApi500MopDirectNudge REMOVED 2026-05-17 08:00 IST per Rajiv
  // directive thread `1778957625.997439`: API 500 detection colocated with
  // autocompact handler in stuck.ts checkApi500Backoff (same setInterval
  // timer that injects "continue your work" after autocompact). bg-script
  // Step 1a + INJECT_DECISION:mop-direct-nudge marker path was dead when
  // slots were idle (MoP skips bg-script on idle=true). Refer to stuck.ts.

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

    // Capture pre-update idle state for idle→active transition detection.
    // Must read BEFORE updateSlot clears the idle flag, otherwise
    // handlePostToolUse never sees the transition. (Bug fix 2026-03-16)
    const wasIdle = this.db.getSlot(slotNum)?.idle ?? false;

    // Update last_activity and mark as busy (any hook = slot is working).
    // SessionStart / PreCompact / PostCompact don't change idle flag — they're
    // lifecycle events that don't reflect work-in-progress state.
    const lifecycleEvents = new Set([
      "SessionStart",
      "PreCompact",
      "PostCompact",
    ]);
    if (!lifecycleEvents.has(payload.type)) {
      this.db.updateSlot(slotNum, {
        last_activity: new Date().toISOString(),
        idle: payload.type === "Stop", // Stop marks idle; everything else marks busy
      });
    } else {
      this.db.updateSlot(slotNum, {
        last_activity: new Date().toISOString(),
      });
    }

    // Route by hook type
    switch (payload.type) {
      case "Stop":
        return this.handleStop(slotNum, payload);
      case "PostToolUse":
        return this.handlePostToolUse(slotNum, payload, wasIdle);
      case "PreToolUse":
        return this.handlePreToolUse(slotNum, payload);
      case "Notification":
        return this.handleNotification(slotNum, payload);
      case "UserPromptSubmit":
        // User sent a message — slot is becoming active. Handle like PostToolUse
        // for idle→active transition detection. (Added 2026-03-18)
        return this.handlePostToolUse(slotNum, payload, wasIdle);
      case "SessionStart":
        return this.handleSessionStart(slotNum, payload);
      case "PreCompact":
      case "PostCompact":
        // Logged via this.db.logEvent at top of process(); no special action.
        // SessionStart:compact carries the resume signal we react to.
        return {};
      default:
        return {};
    }
  }

  // ─── SessionStart Hook ─────────────────────────────────
  /**
   * Fires when Claude Code (re)initializes a session.
   * Matchers: "startup" | "resume" | "clear" | "compact"
   *
   * For source="compact" (post-/compact), send "continue your work"
   * directly to the slot's pane. Critical for slots 1+4 (codex-proxy /
   * GPT-5.5) which do not auto-resume their task after compaction.
   * Slots 2+3 (native Sonnet) auto-resume but receive the same nudge —
   * a duplicate "continue" is harmless and keeps the path uniform.
   *
   * Direct-recovery model (Rajiv directive 2026-04-30 12:44-12:45):
   * Previously injected /slot-compact-resumed into the PM pane; the PM
   * skill then sent "continue" to the slot. That round-trip is now gone.
   * The legacy /slot-compact-resumed PM-side skill is deprecated.
   *
   * Reference: feedback_gpt55_compact_needs_continue_trigger.md
   */
  private handleSessionStart(slotNum: number, payload: HookPayload): HookResponse {
    const source = payload.source ?? "";
    debugLog(
      `[hooks] SessionStart slot=${slotNum} source=${source || "(none)"}`
    );
    if (slotNum === 0) {
      // PM pane — covers compact-resume so PM doesn't sit silent post-/compact.
      // PM pane hook coverage gap — direct tmux inject because Stop/SessionStart hook
      // delivery is unreliable on slot 0 (see feedback_mop_clear_all_slots_pm_direct_inject.md
      // + 2026-05-06 wedge analysis /tmp/mop-midnight-wedge-2026-05-06.md). tmux paste-buffers
      // the keystrokes; PM processes them when the current turn finishes — same pattern as
      // mop_clear_all_slots commit 93ab9aa.
      if (source === "compact") {
        this.db.logEvent(0, "session_start_compact", "SessionStart", null, {
          source,
          via: "tmux_paste_buffer",
        });
        try {
          execSync(
            `tmux send-keys -t 0:0.0 'continue your work' Enter`,
            { timeout: 5_000 }
          );
          debugLog(`[hooks] SessionStart:compact slot=0 (PM) — tmux inject OK`);
        } catch (err) {
          debugLog(`[hooks] SessionStart:compact slot=0 (PM) — tmux inject FAILED: ${err}`);
        }
      } else {
        debugLog(`[hooks] SessionStart slot=0 (PM) source=${source} — no nudge (only 'compact' triggers)`);
      }
      return {};
    }
    if (source === "compact") {
      this.db.logEvent(slotNum, "session_start_compact", "SessionStart", null, {
        source,
      });
      // Direct-recovery: send "continue your work" straight to the slot.
      // force=true because the slot just resumed and may not yet show
      // the active prompt to the active-check.
      const ok = this.relay.sendToSlot(slotNum, "continue your work", true);
      debugLog(
        `[hooks] SessionStart:compact slot=${slotNum} sendToSlot(continue)=${ok}`
      );
    } else {
      debugLog(
        `[hooks] SessionStart slot=${slotNum} source=${source} — no nudge (only 'compact' triggers)`
      );
    }
    return {};
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

    // Check if slot was awaiting plan approval.
    // The plan approval prompt (numbered choices) renders AFTER Stop fires,
    // so we poll for it before notifying PM. (Rajiv directive 2026-03-18)
    if (slot.activity === "awaiting_plan_approval") {
      const pending = this.pendingPlanReady.get(slotNum);
      if (pending) {
        // Start polling for the plan approval prompt before notifying PM.
        this.pendingPlanReady.delete(slotNum);
        this.pollForPlanApprovalPrompt(slotNum, pending, 0);
      } else {
        // No pending = PM was already notified. Check cooldown before re-sending.
        // During revision cycles, the slot may stop multiple times while
        // awaiting_plan_approval — don't spam PM with reminders.
        const lastSent = this.lastPlanReadySent.get(slotNum) ?? 0;
        const elapsed = Date.now() - lastSent;
        if (elapsed > HookProcessor.PLAN_READY_COOLDOWN_MS) {
          // Outside cooldown — genuine re-display (post-compaction). Send reminder.
          this.db.logEvent(slotNum, "plan_approval_still_pending", "Stop", null, {
            task: slot.task,
            issue: slot.issue,
            reason: "Stop hook fired while awaiting_plan_approval — plan prompt re-displayed (likely post-compaction)",
          });
          this.relay.notifyPlanApprovalNeeded(slotNum, slot.issue ?? 0);
        } else {
          // Inside cooldown — suppress. Slot is mid-revision.
          this.db.logEvent(slotNum, "plan_approval_reminder_suppressed", "Stop", null, {
            task: slot.task,
            issue: slot.issue,
            elapsed_ms: elapsed,
            reason: "Suppressed — within cooldown, slot likely mid-revision",
          });
        }
      }
      // Don't clear activity — slot is still awaiting approval
      return {};
    }

    // Slot went idle — clear activity, cancel any pending plan timer.
    // NOTE: Do NOT stop check-slot timer here — idle fires between every tool call.
    // Timer self-manages: skips ticks while idle, stops when unoccupied or DND.
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

    // ─── Clear Pending Check ───────────────────────────────
    // If this slot has a pending clear (from mop_clear_all_slots), send /clear now
    // that the slot is idle, then release it. Similar pattern to exit_pending.
    // Rajiv directive 2026-04-04: "if idle trigger immediately or wait till next idle"
    if (this.db.hasPendingClear(slotNum)) {
      try {
        const paneAddress = `0:0.${slotNum}`;
        execSync(`tmux send-keys -t ${paneAddress} '/clear' Enter`, { timeout: 5_000 });

        // Release slot state (slots 1-4 only)
        if (slotNum >= 1 && slotNum <= 4) {
          this.db.releaseSlot(slotNum);
        }

        this.db.clearPendingClear(slotNum);
        this.db.logEvent(slotNum, "clear_pending_executed", "Stop", null, {
          name: slot.name,
          reason: "Slot went idle — executing queued /clear from mop_clear_all_slots",
        });

        this.relay.injectToPM(
          `# 🧹 Slot ${slotNum} (${slot.name ?? "unnamed"}) cleared (was queued, now idle)`,
        );

        // Check if all pending clears are done
        const pendingStatus = this.db.getClearPendingStatus();
        const allDone = Object.values(pendingStatus).every((v) => !v);
        if (allDone) {
          this.relay.injectToPM("# ✅ All queued slot clears completed");
        }
      } catch (err) {
        this.db.logEvent(slotNum, "clear_pending_failed", "Stop", null, {
          error: String(err),
        });
      }
      return {};
    }

    // Normal idle flow — debounced notification to PM.
    // Wait 60s before notifying. If slot becomes active, cancel the timer.
    // (Rajiv directive 2026-03-31: 24 duplicate idle notifications on Mar 30)
    this.cancelPendingIdleTimer(slotNum);

    const idleTimer = setTimeout(() => {
      this.pendingIdleTimers.delete(slotNum);
      // Re-check slot is still idle before notifying
      const currentSlot = this.db.getSlot(slotNum);
      console.log(`[idle-debug] slot ${slotNum} debounce fired: occupied=${currentSlot?.occupied}, idle=${currentSlot?.idle}, dnd=${currentSlot?.dnd}, task=${currentSlot?.task ?? 'undefined'}`);
      if (currentSlot?.idle) {
        // ─── Staleness gate (Rajiv directive 2026-05-05) ──────
        // Mirror check-slot's "skip when idle" gate but for the slot-idle
        // notification path. If a Task subagent dispatched within the last
        // STALE_GATE_WINDOW_SEC and has no closing Stop yet, the slot is
        // active even though MoP's idle flag is true (plan-agent / qa-tester
        // running in foreground). Suppress the /slot-idle relay; the next
        // Stop hook (after the subagent returns) will re-open the debounce.
        const subagent = this.db.hasRecentSubagentDispatch(
          slotNum,
          HookProcessor.IDLE_STALENESS_GATE_SEC
        );
        if (subagent) {
          console.log(
            `[idle-debug] slot ${slotNum} STALENESS GATE — subagent Task dispatched at ${subagent.taskTs} with no later Stop; suppressing /slot-idle`
          );
          this.db.logEvent(slotNum, "slot_idle_suppressed_subagent_active", "Timer", null, {
            relay_path: "Stop.debounce.gate",
            reason: "subagent_active_post_debounce",
            task_dispatch_ts: subagent.taskTs,
            window_sec: HookProcessor.IDLE_STALENESS_GATE_SEC,
          });
          return;
        }

        // Secondary gate: any tool fired in the last RECENT_TOOL_GATE_SEC.
        // The slot's MoP idle flag is set on Stop, but Stop fires between
        // every tool call. If the most-recent PostToolUse is younger than
        // the gate window (and the debounce was 30s), the slot has
        // resumed work since the debounce started.
        const lastTool = this.db.getLastToolFire(
          slotNum,
          HookProcessor.IDLE_RECENT_TOOL_GATE_SEC
        );
        if (lastTool) {
          console.log(
            `[idle-debug] slot ${slotNum} STALENESS GATE — last tool ${lastTool.tool}@${lastTool.timestamp} within ${HookProcessor.IDLE_RECENT_TOOL_GATE_SEC}s; suppressing /slot-idle`
          );
          this.db.logEvent(slotNum, "slot_idle_suppressed_recent_tool", "Timer", null, {
            relay_path: "Stop.debounce.gate",
            reason: "recent_tool_post_debounce",
            last_tool: lastTool.tool,
            last_tool_ts: lastTool.timestamp,
            window_sec: HookProcessor.IDLE_RECENT_TOOL_GATE_SEC,
          });
          return;
        }
        console.log(`[idle-debug] slot ${slotNum} still idle after debounce — preparing /slot-idle relay`);
        // ─── Capture tmux output and write to file ────────────
        // Rajiv directive 2026-04-03: "inject the tmux capture of the slot
        // along with the command through MoP during slot idle"
        // PM just reads the file instead of launching a classification agent.
        const captureFile = `/tmp/slot-${slotNum}-idle-capture.txt`;
        try {
          const capture = execSync(
            `tmux capture-pane -t 0:0.${slotNum} -p -S -30`,
            { timeout: 5_000 }
          ).toString();
          writeFileSync(captureFile, capture);
        } catch {
          writeFileSync(captureFile, "[capture failed]");
        }

        // ─── INJECT_DECISION gate (Rajiv 2026-05-16 18:26 IST thread `1778935572.419629`) ───
        // Mirror /check-slot gate at hooks.ts:208-250. Run check-slot-bg.sh; if
        // it emits "INJECT_DECISION:skip REASON:...", suppress the /slot-idle
        // relay (slot is qualitatively healthy / no JSONL delta).
        //
        // ALSO handles INJECT_DECISION:mop-direct-nudge (Rajiv 2026-05-17 00:31
        // IST thread `1778957625.997439`): MoP itself sends "continue your work"
        // to the slot's tmux pane on API-500 backoff window expiry. PM remains
        // uninvolved through retry 0-4. After successful injection, bump
        // retry_count + reschedule next_nudge_at via --api-500-state-tick.
        //
        // Fail-open: bg-script failure → fall through to inject (preserves
        // prior behavior).
        if (HookProcessor.CHECK_SLOT_BG_ENABLED) {
          const bgScript = `${process.env.HOME}/.claude/scripts/check-slot-bg.sh`;
          let bgOutput = "";
          let bgFailed = false;
          try {
            bgOutput = execSync(`bash ${bgScript} ${slotNum}`, {
              timeout: 8_000,
              maxBuffer: 1024 * 1024,
            }).toString();
          } catch {
            bgFailed = true;
          }
          // (INJECT_DECISION:mop-direct-nudge consumer REMOVED 2026-05-17 08:00 IST
          // per Rajiv directive thread `1778957625.997439`. API 500 detection
          // colocated with autocompact handler in stuck.ts checkApi500Backoff,
          // running independently of slot idle state. bg-script Step 1a no
          // longer emits this marker.)
          const skipMatch = bgOutput.match(/INJECT_DECISION:skip(?:\s+REASON:([^\n]*))?/);
          if (!bgFailed && skipMatch) {
            const reason = (skipMatch[1] || "unspecified").trim().slice(0, 120);
            console.log(`[idle-debug] slot ${slotNum} INJECT_DECISION GATE — bg-script skip REASON:${reason}; suppressing /slot-idle`);
            this.db.logEvent(slotNum, "slot_idle_suppressed_inject_decision", "Stop", null, {
              relay_path: "Stop.debounce.inject_decision",
              reason,
            });
            return;
          }
        } else {
          console.log(
            `[idle-debug] slot ${slotNum} INJECT_DECISION GATE SKIPPED — check-slot-bg disabled in MoP HTTP process`
          );
          this.db.logEvent(slotNum, "slot_idle_inject_decision_skipped", "Stop", null, {
            relay_path: "Stop.debounce.inject_decision",
            reason: "check-slot-bg-disabled",
          });
        }

        console.log(`[idle-debug] slot ${slotNum} calling relay.notifySlotIdle()`);
        this.relay.notifySlotIdle(currentSlot);
        this.db.logEvent(slotNum, "slot_idle_notified", "Stop", null, {
            relay_path: "Stop.debounce.notifySlotIdle",
            notification_type: "slot-idle",
          task: currentSlot.task,
          branch: currentSlot.branch,
          capture_file: captureFile,
          debounce_ms: HookProcessor.IDLE_DEBOUNCE_MS,
        });
      } else {
        this.db.logEvent(slotNum, "slot_idle_cancelled", "Timer", null, {
          reason: "slot became active during debounce window",
        });
      }
    }, HookProcessor.IDLE_DEBOUNCE_MS);
    if (idleTimer.unref) idleTimer.unref();
    this.pendingIdleTimers.set(slotNum, idleTimer);

    this.db.logEvent(slotNum, "slot_idle_debounce_started", "Stop", null, {
      task: slot.task,
      branch: slot.branch,
      debounce_ms: HookProcessor.IDLE_DEBOUNCE_MS,
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

  private handlePostToolUse(slotNum: number, payload: HookPayload, wasIdle?: boolean): HookResponse {
    // ─── Active Notification (idle → active transition) ──────
    // First PostToolUse after a Stop means the slot became active.
    // Notify PM so they know not to send new work to this slot.
    // Uses wasIdle from process() — captured BEFORE updateSlot clears idle flag.
    const slot = this.db.getSlot(slotNum);
    if (slot && wasIdle) {
      // Cancel pending idle timer — slot is active again, no notification needed
      const hadPendingIdle = this.cancelPendingIdleTimer(slotNum);
      if (hadPendingIdle) {
        this.db.logEvent(slotNum, "idle_debounce_cancelled", "PostToolUse", payload.tool_name ?? null, {
          reason: "slot became active during 60s debounce window",
        });
      }

      // Notify PM for ALL slots (not just occupied — rework slots may be released)
      if (!slot.dnd) {
        // ─── Capture tmux output on active transition ────────
        // Rajiv directive 2026-04-03: inject tmux capture with slot-active too
        const captureFile = `/tmp/slot-${slotNum}-active-capture.txt`;
        try {
          const capture = execSync(
            `tmux capture-pane -t 0:0.${slotNum} -p -S -15`,
            { timeout: 5_000 }
          ).toString();
          writeFileSync(captureFile, capture);
        } catch {
          writeFileSync(captureFile, "[capture failed]");
        }

        // ─── INJECT_DECISION gate (Rajiv 2026-05-16 18:26 IST thread `1778935572.419629`) ───
        // Mirror /check-slot gate at hooks.ts:208-250. /slot-active is
        // informational; suppress when bg-script classifies the slot as
        // qualitatively-healthy with no new signal for PM to act on.
        // Fail-open: bg-script failure → fall through to inject.
        // NOTE: timer-start (below) runs regardless — gate only suppresses
        // the PM injection, not check-slot lifecycle wiring.
        let suppressSlotActive = false;
        if (HookProcessor.CHECK_SLOT_BG_ENABLED) {
          const bgScript = `${process.env.HOME}/.claude/scripts/check-slot-bg.sh`;
          let bgOutput = "";
          let bgFailed = false;
          try {
            bgOutput = execSync(`bash ${bgScript} ${slotNum}`, {
              timeout: 8_000,
              maxBuffer: 1024 * 1024,
            }).toString();
          } catch {
            bgFailed = true;
          }
          const skipMatch = bgOutput.match(/INJECT_DECISION:skip(?:\s+REASON:([^\n]*))?/);
          if (!bgFailed && skipMatch) {
            const reason = (skipMatch[1] || "unspecified").trim().slice(0, 120);
            console.log(`[active-debug] slot ${slotNum} INJECT_DECISION GATE — bg-script skip REASON:${reason}; suppressing /slot-active`);
            this.db.logEvent(slotNum, "slot_active_suppressed_inject_decision", "PostToolUse", payload.tool_name ?? null, {
              relay_path: "PostToolUse.slot_active.inject_decision",
              reason,
            });
            suppressSlotActive = true;
          }
        } else {
          console.log(
            `[active-debug] slot ${slotNum} INJECT_DECISION GATE SKIPPED — check-slot-bg disabled in MoP HTTP process`
          );
          this.db.logEvent(slotNum, "slot_active_inject_decision_skipped", "PostToolUse", payload.tool_name ?? null, {
            relay_path: "PostToolUse.slot_active.inject_decision",
            reason: "check-slot-bg-disabled",
          });
        }

        if (!suppressSlotActive) {
            // Inject MoP message prefix (replaces former slash command `/slot-active N`).
            // Rajiv directive 2026-05-22 22:23 IST thread `1779468118.901709`:
            // "inject is a string instead of a command." PM-side
            // pm-context-injector.sh recognizes the prefix and emits
            // [MoP_SLOT_NOTIFICATION] system-reminder hinting Skill(slot-active).
            this.relay.injectToPM(`MoP: slot ${slotNum} active`);
            this.db.logEvent(slotNum, "slot_active_notified", "PostToolUse", payload.tool_name ?? null, {
              task: slot.task,
              issue: slot.issue,
              capture_file: captureFile,
            });
        }

        // ─── Start check-slot periodic timer ────────────────
        // Rajiv directive 2026-04-03: MoP drives check-slot instead of PM cron loop
        // Only start timer if slot is occupied (has a task). Released slots in auto-compact
        // cycles trigger active transitions but should NOT get timers.
        if (slot.occupied) {
          this.startCheckSlotTimer(slotNum);
        }
      }
    }

    // Plan file write → debounce timer. After last write settles (10s), set pending.
    // RETIRED (2026-03-24): Plan-ready detection removed.
    // Slots now use plan-agent subagent + /codex-plan-review skill.
    // PM is no longer in the plan approval loop.
    // The old flow: Write to docs/plans/ → debounce → poll for approval prompt → notify PM
    // is replaced by: plan-agent → slot runs Codex itself → implements.
    if (
      (payload.tool_name === "Write" || payload.tool_name === "Edit") &&
      typeof payload.tool_input === "object" &&
      payload.tool_input !== null
    ) {
      const filePath = (payload.tool_input as Record<string, string>).file_path ?? "";
      if (filePath.includes("/plans/") && filePath.endsWith(".md")) {
        // Log for visibility but do NOT set awaiting_plan_approval or notify PM
        this.db.logEvent(slotNum, "plan_file_written", "PostToolUse", null, {
          file: filePath,
          note: "Plan-ready detection retired — slot self-reviews via /codex-plan-review",
        });

        // Auto-assign slot if not already occupied (extract issue from task)
        const slot = this.db.getSlot(slotNum);
        const issueMatch = slot?.task?.match(/#(\d+)/);
        const issueNum = issueMatch ? parseInt(issueMatch[1], 10) : 0;
        if (!slot?.occupied && issueNum > 0) {
          const taskLabel = slot?.task || `#${issueNum}`;
          this.db.assignSlot(slotNum, taskLabel, issueNum, null, null);
        }

        // Don't early return — let normal activity tracking classify this as "coding"
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
      // ExitPlanMode just clears the timer and sets activity.
      // Plan-ready notification is triggered by the write-debounce timer (not ExitPlanMode).
      // (Rajiv directive 2026-03-19: debounce on file writes, poll for prompt after last write)
      // RETIRED (2026-03-24): No longer set awaiting_plan_approval.
      // Slots self-review via /codex-plan-review. PM is not in the loop.
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
    const slot = this.db.getSlot(slotNum);
    console.log(`[idle-debug] Notification hook slot ${slotNum}: type=${notifType}, occupied=${slot?.occupied}, idle=${slot?.idle}, dnd=${slot?.dnd}, task=${slot?.task ?? 'undefined'}`);

    // Log notification type for queryability
    this.db.logEvent(slotNum, `notification_${notifType}`, "Notification", null, {
      notification_type: notifType,
      occupied: slot?.occupied,
      idle: slot?.idle,
      dnd: slot?.dnd,
      task: slot?.task,
    });

    if (notifType === "idle_prompt") {
      // Defensive secondary relay path (2026-05-08, Rajiv directive "fix MoP and restart").
      // Claude Code's runtime fires Notification(idle_prompt) when the slot's CLI is
      // displaying an idle prompt awaiting user input. This is a strong runtime fact —
      // stronger than MoP's `idle` flag, which is a derived signal that bounces between
      // every PostToolUse/Stop pair. Trust the notification fact and relay /slot-idle as
      // a redundant trigger to the Stop-debounce path. Coalescing happens at the
      // injectToPM layer (PM busy-queue PRIMARY KEY (slot, event_type) — two enqueues
      // of /slot-idle N collapse to one).
      //
      // Apply the SAME staleness gates as the Stop-debounce path so we don't relay
      // during fg subagent runs (plan-agent, qa-tester, codex-companion).
      if (slot?.occupied && !slot.dnd) {
        const subagent = this.db.hasRecentSubagentDispatch(
          slotNum,
          HookProcessor.IDLE_STALENESS_GATE_SEC
        );
        if (subagent) {
          console.log(
            `[idle-debug] slot ${slotNum} Notification(idle_prompt) STALENESS GATE — subagent active (taskTs=${subagent.taskTs}); suppressing relay`
          );
          this.db.logEvent(slotNum, "slot_idle_suppressed_subagent_active", "Notification", null, {
            relay_path: "Notification.idle_prompt.gate",
            reason: "subagent_active",
            task_dispatch_ts: subagent.taskTs,
            window_sec: HookProcessor.IDLE_STALENESS_GATE_SEC,
          });
        } else {
          const lastTool = this.db.getLastToolFire(
            slotNum,
            HookProcessor.IDLE_RECENT_TOOL_GATE_SEC
          );
          if (lastTool) {
            console.log(
              `[idle-debug] slot ${slotNum} Notification(idle_prompt) STALENESS GATE — recent tool ${lastTool.tool}@${lastTool.timestamp}; suppressing relay`
            );
            this.db.logEvent(slotNum, "slot_idle_suppressed_recent_tool", "Notification", null, {
              relay_path: "Notification.idle_prompt.gate",
              reason: "recent_tool",
              last_tool: lastTool.tool,
              last_tool_ts: lastTool.timestamp,
              window_sec: HookProcessor.IDLE_RECENT_TOOL_GATE_SEC,
            });
          } else {
            // Capture tmux output so PM's slot-idle skill has the same artifact
            // shape as Path 1 (Stop.debounce.notifySlotIdle).
            const captureFile = `/tmp/slot-${slotNum}-idle-capture.txt`;
            try {
              const capture = execSync(
                `tmux capture-pane -t 0:0.${slotNum} -p -S -30`,
                { timeout: 5_000 }
              ).toString();
              writeFileSync(captureFile, capture);
            } catch {
              writeFileSync(captureFile, "[capture failed]");
            }

            // ─── INJECT_DECISION gate (Rajiv 2026-05-16 18:26 IST thread `1778935572.419629`) ───
            // Mirror /check-slot gate at hooks.ts:208-250 — also gate the
            // defensive secondary path (Notification idle_prompt) so a
            // qualitatively-healthy slot doesn't surface a false-positive
            // /slot-idle.
            //
            // ALSO handles INJECT_DECISION:mop-direct-nudge (Rajiv 2026-05-17
            // 00:31 IST thread `1778957625.997439`). See Stop.debounce path
            // above for full semantics.
            //
            // Fail-open on bg-script failure.
            if (HookProcessor.CHECK_SLOT_BG_ENABLED) {
              const bgScript = `${process.env.HOME}/.claude/scripts/check-slot-bg.sh`;
              let bgOutput = "";
              let bgFailed = false;
              try {
                bgOutput = execSync(`bash ${bgScript} ${slotNum}`, {
                  timeout: 8_000,
                  maxBuffer: 1024 * 1024,
                }).toString();
              } catch {
                bgFailed = true;
              }
              // (INJECT_DECISION:mop-direct-nudge consumer REMOVED 2026-05-17 08:00 IST
              // per Rajiv directive thread `1778957625.997439`. API 500 detection
              // colocated with autocompact handler in stuck.ts checkApi500Backoff,
              // running independently of slot idle state. bg-script Step 1a no
              // longer emits this marker.)
              const skipMatch = bgOutput.match(/INJECT_DECISION:skip(?:\s+REASON:([^\n]*))?/);
              if (!bgFailed && skipMatch) {
                const reason = (skipMatch[1] || "unspecified").trim().slice(0, 120);
                console.log(`[idle-debug] slot ${slotNum} Notification(idle_prompt) INJECT_DECISION GATE — bg-script skip REASON:${reason}; suppressing /slot-idle`);
                this.db.logEvent(slotNum, "slot_idle_suppressed_inject_decision", "Notification", null, {
                  relay_path: "Notification.idle_prompt.inject_decision",
                  reason,
                });
                return {};
              }
            } else {
              console.log(
                `[idle-debug] slot ${slotNum} Notification(idle_prompt) INJECT_DECISION GATE SKIPPED — check-slot-bg disabled in MoP HTTP process`
              );
              this.db.logEvent(slotNum, "slot_idle_inject_decision_skipped", "Notification", null, {
                relay_path: "Notification.idle_prompt.inject_decision",
                reason: "check-slot-bg-disabled",
              });
            }

            console.log(`[idle-debug] slot ${slotNum} Notification(idle_prompt) — relaying /slot-idle (defensive secondary path)`);
            this.relay.notifySlotIdle(slot);
            this.db.logEvent(slotNum, "slot_idle_notified", "Notification", null, {
              relay_path: "Notification.idle_prompt.notifySlotIdle",
              notification_type: "slot-idle",
              task: slot.task,
              branch: slot.branch,
              capture_file: captureFile,
              source: "idle_prompt_notification",
            });
          }
        }
      } else {
        console.log(`[idle-debug] slot ${slotNum} Notification(idle_prompt) — skipping (occupied=${slot?.occupied}, dnd=${slot?.dnd})`);
      }
    }

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
