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
import type { MoPDatabase } from "./db.js";
import type { MoPConfig, SlotState } from "./types.js";

const SEND_TO_SLOT_SCRIPT =
  `${process.env.HOME}/.claude/skills/tmux-slot-command/scripts/send-to-slot.sh`;

/**
 * Parse a slash-command relay message (e.g. "/slot-idle 3", "/slot-active 1",
 * "/check-slot 2") into (eventType, slot). Returns null for free-form messages
 * (escalation comments, plan-approval-needed, etc.) — those go through the
 * queue too but keyed by their own slot extraction or fall back to slot 0.
 */
function parseRelayMessage(message: string): { eventType: string; slot: number } | null {
  const m = /^\/(slot-idle|slot-active|check-slot|slot-blocked)\s+(\d+)\b/.exec(message);
  if (!m) return null;
  const slot = parseInt(m[2], 10);
  if (!Number.isFinite(slot) || slot < 0 || slot > 4) return null;
  return { eventType: m[1], slot };
}

export class TmuxRelay {
  private pmPaneAddress: string;
  private logManager: LogManager | null = null;
  private db: MoPDatabase | null = null;
  /**
   * PM busy flag. Default TRUE — assume PM is busy until /pm-status proves
   * otherwise (this prevents a flood of injects on MoP startup before the
   * first PM Stop hook lands).
   */
  private pmBusy: boolean = true;

  /**
   * Debounce timer for busy→idle drain. Claude Code's Stop hook fires after
   * EVERY tool call, not only at end-of-turn. Without debounce, the first
   * Stop in an agentic loop would flush the queue and subsequent injectToPM
   * calls would bypass the queue (pmBusy=false) and paste directly into the
   * PM tmux input, stacking up in the CLI's queued-prompt buffer.
   *
   * Behavior:
   *   - busy=true  → cancel any pending drain, set pmBusy=true immediately.
   *   - busy=false → start (or reset) the debounce timer; pmBusy STAYS TRUE
   *                  during the window. Drain only fires after
   *                  DRAIN_DEBOUNCE_MS of sustained idle.
   *
   * Configurable via env var `MOP_DRAIN_DEBOUNCE_MS` (default 7000).
   */
  private drainTimer: ReturnType<typeof setTimeout> | null = null;
  private static readonly DRAIN_DEBOUNCE_MS: number = (() => {
    const raw = process.env.MOP_DRAIN_DEBOUNCE_MS;
    const n = raw ? parseInt(raw, 10) : NaN;
    return Number.isFinite(n) && n >= 0 ? n : 7000;
  })();

  constructor(config: MoPConfig) {
    this.pmPaneAddress = config.pmPaneAddress;
  }

  /** Attach the MoP DB so injectToPM can enqueue when PM is busy. */
  setDatabase(db: MoPDatabase): void {
    this.db = db;
  }

  /**
   * Set the PM busy state.
   *   busy=true  → cancel pending drain, mark PM busy.
   *   busy=false → schedule debounced drain (DRAIN_DEBOUNCE_MS); pmBusy
   *                stays TRUE until the timer fires. Subsequent busy=true
   *                or busy=false within the window resets/cancels the timer.
   *
   * Called by /pm-status HTTP endpoint via UserPromptSubmit (start) + Stop
   * (stop) + SessionStart (stop) hooks fired from the PM project's
   * settings.json.
   *
   * Return value `drained` is the SYNCHRONOUS drain count, which is now
   * always 0 (drain is debounced/async). The async drain logs its count
   * separately via console + DB event.
   */
  setPMBusy(busy: boolean): { drained: number } {
    if (busy) {
      // Cancel any pending drain — PM is back in a tool call.
      if (this.drainTimer) {
        clearTimeout(this.drainTimer);
        this.drainTimer = null;
        console.log(`[relay-debug] setPMBusy(true) — cancelled pending drain`);
      }
      this.pmBusy = true;
      return { drained: 0 };
    }
    // busy=false — schedule debounced drain. Reset timer if one is already
    // pending so the window is from the LAST Stop, not the first.
    if (this.drainTimer) {
      clearTimeout(this.drainTimer);
    }
    const debounceMs = TmuxRelay.DRAIN_DEBOUNCE_MS;
    this.drainTimer = setTimeout(() => {
      this.pmBusy = false;
      const n = this.drainPMQueue();
      this.drainTimer = null;
      console.log(
        `[relay-debug] debounce drain fired after ${debounceMs}ms idle, drained ${n}`
      );
      if (this.db) {
        this.db.logEvent(0, "pm_debounce_drain_fired", null, null, {
          debounce_ms: debounceMs,
          drained: n,
        });
      }
    }, debounceMs);
    return { drained: 0 };
  }

  /** Public read of the busy flag (diagnostics). */
  isPMBusy(): boolean {
    return this.pmBusy;
  }

  /**
   * Drain queued PM-bound events into the PM pane. Called from setPMBusy
   * on busy→idle transition. Each row's payload (if non-null) is preferred
   * over the canonical slash-command form so callers can queue free-form
   * messages too.
   *
   * Returns the count of rows actually injected.
   */
  drainPMQueue(): number {
    if (!this.db) return 0;
    const rows = this.db.drainPendingPMEvents();
    let injected = 0;
    for (const row of rows) {
      const message = row.payload ?? `/${row.event_type} ${row.slot}`;
      const ok = this.injectDirect(message);
      if (ok) injected++;
      this.db.logEvent(row.slot, "pm_queue_drained", null, null, {
        event_type: row.event_type,
        message: message.slice(0, 200),
        enqueued_at: row.enqueued_at,
        injected: ok,
      });
    }
    return injected;
  }

  /** Attach a LogManager for log-based output capture and activity detection. */
  setLogManager(lm: LogManager): void {
    this.logManager = lm;
  }

  /**
   * Inject a raw message into the PM pane via tmux send-keys, OR queue it
   * for later drain if PM is currently busy.
   *
   * Rajiv directive 2026-05-06 11:18 IST: queue slot-idle / slot-active /
   * check-slot relays (and any other PM-bound message) while PM is busy
   * (mid-tool/turn). Drain on PM Stop hook via /pm-status.
   *
   * Coalesce semantics live in the DB layer (PRIMARY KEY (slot, event_type)
   * means latest enqueue per (slot, event_type) wins via INSERT OR REPLACE,
   * cross-event-type drop happens in drainPendingPMEvents).
   *
   * IMPORTANT EXCLUSIONS (do NOT route through this queue):
   * - `mop_clear_all_slots` PM-pane direct inject (mcp.ts) — uses raw
   *   `tmux send-keys '/clear' Enter` directly, bypassing the relay.
   * - `handleSessionStart` slot=0 source=compact (hooks.ts) — uses raw
   *   `tmux send-keys 'continue your work' Enter` directly, bypassing
   *   the relay.
   *
   * Both excluded paths are PM-internal recovery nudges, not slot-event
   * signals — they must never sit in a queue waiting to drain.
   */
  injectToPM(message: string): boolean {
    if (this.pmBusy && this.db) {
      const parsed = parseRelayMessage(message);
      if (parsed) {
        // Slash-command relay (slot-idle/active/check-slot/blocked) —
        // queue keyed by (slot, event_type). PRIMARY KEY auto-coalesces.
        this.db.enqueuePendingPMEvent(parsed.slot, parsed.eventType, null);
        this.db.logEvent(parsed.slot, "pm_queue_enqueued", null, null, {
          event_type: parsed.eventType,
          via: "injectToPM",
        });
        console.log(`[relay-debug] injectToPM queued (PM busy) → ${message}`);
        return true;
      }
      // Free-form message (escalation, plan-approval-needed, scheduled-task,
      // compact warning, queued-clear ack). Try to extract slot from
      // "slot N" substring; otherwise key by slot 0 + a synthetic event
      // type derived from a hash so multiple distinct free-form messages
      // for the same slot don't collapse onto each other.
      const slotMatch = /\bslot\s+(\d+)\b/i.exec(message);
      const slot = slotMatch ? Math.min(4, Math.max(0, parseInt(slotMatch[1], 10))) : 0;
      const synthHash = simpleHash(message);
      const eventType = `freeform-${synthHash}`;
      this.db.enqueuePendingPMEvent(slot, eventType, message);
      this.db.logEvent(slot, "pm_queue_enqueued", null, null, {
        event_type: eventType,
        via: "injectToPM",
        message: message.slice(0, 200),
      });
      console.log(`[relay-debug] injectToPM queued freeform (PM busy) → ${message.slice(0, 80)}`);
      return true;
    }
    return this.injectDirect(message);
  }

  /**
   * Raw paste-buffer inject into PM pane. Used by injectToPM (for the
   * not-busy fast path) and drainPMQueue (when replaying queued events).
   * Bypasses the busy-queue. Do NOT call from outside the relay.
   *
   * IMPORTANT: Text and Enter must be separate send-keys calls —
   * appending Enter to the text send-keys can silently drop the Enter.
   */
  private injectDirect(message: string): boolean {
    try {
      console.log(`[relay-debug] injectToPM → ${message}`);
      execSync(
        `tmux send-keys -t ${this.pmPaneAddress} ${shellEscape(message)} && ` +
        `sleep 0.3 && ` +
        `tmux send-keys -t ${this.pmPaneAddress} Enter && ` +
        `sleep 0.5`,
        { timeout: 10_000 }
      );
      console.log(`[relay-debug] injectToPM success → ${message}`);
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
    // Dedup: suppress same-(slot, issue) escalation within 5 min window.
    // Prevents duplicate 🚨 inject when a slot re-invokes /escalate
    // (autocompact-resumed retry, follow-up Codex round, etc.).
    // The hooks.ts caller logs an `escalation` event AFTER this call returns,
    // so the row we look up here is the PRIOR escalation, not the current one.
    if (this.db && issueNum > 0) {
      const cutoff = new Date(Date.now() - 5 * 60_000).toISOString();
      const recent = this.db.getRecentEscalation(slotNum, issueNum, cutoff);
      if (recent) {
        console.log(
          `[relay-debug] notifyEscalation SUPPRESSED (dupe within 5min) → slot ${slotNum} #${issueNum} (last escalation id=${recent.id} at ${recent.timestamp})`
        );
        this.db.logEvent(slotNum, "escalation_suppressed", null, null, {
          issue: issueNum,
          reason: "dupe_within_5min",
          last_escalation_id: recent.id,
          last_escalation_timestamp: recent.timestamp,
          description_preview: description.slice(0, 200),
        });
        return;
      }
    }

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
   *
   * Wrapper-timeout handling (Rajiv directive 2026-05-09 12:25 IST):
   * `tmux send-keys` is fire-and-forget at the paste-buffer level — keystrokes
   * may have landed, but if `send-to-slot.sh --wait` ETIMEDOUTs the post-send
   * delivery verification (content-diff or ack poll) did NOT confirm. Prior
   * behavior treated ETIMEDOUT as success, masking the case where the slot is
   * actually wedged (TUI dropping input, pane unresponsive). Observed slot 1
   * #4228 2026-05-09 11:53 IST: /compact wrapper timed out, slot still parked
   * at "Context limit reached" wall 16+ min later, manual recovery required.
   *
   * New behavior: wrapper-timeout = ERROR class. Retry up to MAX_RETRIES with
   * short backoff. If all retries time out, return false with the call
   * surfaced via console.error. Per-(slot, command) cooldown prevents the
   * caller (e.g., stuck.ts /compact) from immediately re-firing on the next
   * detector tick — see `lastFailureAt` map.
   *
   * Loop-protection: stuck.ts already sets compactInFlightAt BEFORE calling
   * sendToSlot and uses COMPACT_INFLIGHT_DEDUP_MS (30s) + COMPACT_DISPATCH_DEDUP_MS
   * (5min) windows; a returned-failure does not reset those, so /compact
   * cannot enter a tight retry loop here. The local cooldown is a defensive
   * second layer for non-stuck.ts callers (mcp.ts raw mode, hooks.ts
   * SessionStart:compact "continue your work").
   */
  private static readonly SEND_MAX_RETRIES = 2; // initial + 2 retries = 3 total attempts
  private static readonly SEND_RETRY_BACKOFF_MS = [1000, 2000];
  private static readonly SEND_FAILURE_COOLDOWN_MS = 30_000;
  private lastSendFailureAt: Map<string, number> = new Map();

  sendToSlot(slotNum: number, command: string, force = false, raw = false): boolean {
    const forceFlag = force ? " --force" : "";
    const rawFlag = raw ? " --raw" : "";
    const cmdLine = `${SEND_TO_SLOT_SCRIPT} ${slotNum} ${shellEscape(command)}${forceFlag}${rawFlag}`;
    const cooldownKey = `${slotNum}:${command.slice(0, 80)}`;

    // Per-(slot, command) cooldown: if we just failed delivery for this same
    // pair within COOLDOWN_MS, skip the send entirely. Prevents tight retry
    // loops from external callers without their own dedup tracker.
    const lastFail = this.lastSendFailureAt.get(cooldownKey);
    if (lastFail && Date.now() - lastFail < TmuxRelay.SEND_FAILURE_COOLDOWN_MS) {
      console.warn(
        `[relay] sendToSlot ${slotNum} (${command.slice(0, 60)}) suppressed by ` +
        `failure cooldown (last failure ${Math.round((Date.now() - lastFail) / 1000)}s ago)`
      );
      if (this.db) {
        this.db.logEvent(slotNum, "send_suppressed_cooldown", null, null, {
          command: command.slice(0, 200),
          last_failure_at: new Date(lastFail).toISOString(),
          cooldown_ms: TmuxRelay.SEND_FAILURE_COOLDOWN_MS,
        });
      }
      return false;
    }

    let lastErr: unknown = null;
    for (let attempt = 0; attempt <= TmuxRelay.SEND_MAX_RETRIES; attempt++) {
      try {
        execSync(cmdLine, { timeout: 15_000 }); // send-to-slot.sh has 10s wait timeout
        if (attempt > 0) {
          console.log(
            `[relay] sendToSlot ${slotNum} (${command.slice(0, 60)}) succeeded on retry ${attempt}`
          );
          if (this.db) {
            this.db.logEvent(slotNum, "send_succeeded_after_retry", null, null, {
              command: command.slice(0, 200),
              attempt,
            });
          }
        }
        // Clear any prior cooldown on success.
        this.lastSendFailureAt.delete(cooldownKey);
        return true;
      } catch (err) {
        lastErr = err;
        const code = (err as NodeJS.ErrnoException)?.code;
        const isTimeout = code === "ETIMEDOUT";
        console.warn(
          `[relay] sendToSlot ${slotNum} (${command.slice(0, 60)}) attempt ${attempt + 1}/` +
          `${TmuxRelay.SEND_MAX_RETRIES + 1} failed (${isTimeout ? "wrapper timeout" : `code=${code ?? "unknown"}`})`
        );
        if (attempt < TmuxRelay.SEND_MAX_RETRIES) {
          const backoff = TmuxRelay.SEND_RETRY_BACKOFF_MS[attempt] ?? 2000;
          // Synchronous sleep via Atomics.wait on a dummy SharedArrayBuffer —
          // blocks the event loop without burning CPU. sendToSlot is sync
          // to preserve callsite contract (callers in stuck.ts/hooks.ts use
          // it as fire-and-forget without await).
          const sab = new SharedArrayBuffer(4);
          const view = new Int32Array(sab);
          Atomics.wait(view, 0, 0, backoff);
        }
      }
    }

    // All retries exhausted. Mark cooldown so the next caller within 30s
    // is short-circuited (defensive; primary dedup lives in callers).
    this.lastSendFailureAt.set(cooldownKey, Date.now());
    const code = (lastErr as NodeJS.ErrnoException)?.code;
    const reason = code === "ETIMEDOUT" ? "delivery_unverified_after_retries" : "send_error";
    console.error(
      `[relay] sendToSlot ${slotNum} (${command.slice(0, 60)}) FAILED after ` +
      `${TmuxRelay.SEND_MAX_RETRIES + 1} attempts (reason=${reason}):`,
      lastErr
    );
    if (this.db) {
      this.db.logEvent(slotNum, "send_failed_after_retries", null, null, {
        command: command.slice(0, 200),
        attempts: TmuxRelay.SEND_MAX_RETRIES + 1,
        reason,
        last_error_code: code ?? null,
      });
    }
    return false;
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

/**
 * Compact 32-bit FNV-1a hash, base36 encoded. Used to key free-form PM
 * relay messages so distinct messages for the same slot don't collapse
 * onto each other in the busy-queue.
 */
function simpleHash(str: string): string {
  let hash = 2166136261;
  for (let i = 0; i < str.length; i++) {
    hash ^= str.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}
