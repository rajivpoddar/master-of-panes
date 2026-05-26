/**
 * MoP Tmux Relay — Inject formatted messages into PM pane
 *
 * Replaces scattered bash scripts (slot-idle-notify.sh, etc.) with a single
 * relay that formats events and injects them into the PM's tmux pane.
 *
 * Uses the send-to-slot.sh script infrastructure for reliable delivery.
 */

import { execSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import type { LogManager } from "./logs.js";
import type { MoPDatabase } from "./db.js";
import type { MoPConfig, SlotState } from "./types.js";

/**
 * Resolve the PM session JSONL directory. Claude Code stores per-project
 * session transcripts under `~/.claude/projects/<encoded-cwd>/*.jsonl`.
 *
 * Override via env `MOP_PM_JSONL_DIR` if needed (e.g., PM main project
 * isn't heydonna-app).
 */
const PM_JSONL_DIR =
  process.env.MOP_PM_JSONL_DIR ??
  `${process.env.HOME}/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app`;

/**
 * Threshold (ms) — if PM JSONL has been written within this window, PM is
 * still actively producing tokens / running tools and we MUST NOT drain
 * the queue (Rajiv directive 2026-05-13 14:13 IST thread `1778661820.586119`).
 *
 * Configurable via `MOP_PM_JSONL_IDLE_MS` (default 15s). The drain timer
 * uses this to verify true idle before injecting; if JSONL is fresher
 * than this threshold the timer re-arms instead of draining.
 */
const PM_JSONL_IDLE_MS: number = (() => {
  const raw = process.env.MOP_PM_JSONL_IDLE_MS;
  const n = raw ? parseInt(raw, 10) : NaN;
  return Number.isFinite(n) && n >= 0 ? n : 15_000;
})();

/**
 * Return mtime (ms epoch) of the most recently modified PM session JSONL,
 * or null if none found. Used to verify PM is genuinely idle before
 * draining the relay queue.
 */
function pmJsonlMtimeMs(): number | null {
  try {
    const entries = fs.readdirSync(PM_JSONL_DIR);
    let max = 0;
    for (const name of entries) {
      if (!name.endsWith(".jsonl")) continue;
      try {
        const st = fs.statSync(path.join(PM_JSONL_DIR, name));
        if (st.mtimeMs > max) max = st.mtimeMs;
      } catch { /* ignore unreadable */ }
    }
    return max > 0 ? max : null;
  } catch {
    return null;
  }
}

// v3 (2026-05-09 14:15 IST Rajiv): direct tmux send-keys, no wait-for-idle
// wrapper. Kept for reference / fallback diagnostic; no longer invoked by
// sendToSlot. send-to-slot.sh waits for chevron-idle confirmation that
// never arrives when slot is at "Context limit reached" wall — keystrokes
// must land regardless of slot state.
const SEND_TO_SLOT_SCRIPT =
  `${process.env.HOME}/.claude/skills/tmux-slot-command/scripts/send-to-slot.sh`;
void SEND_TO_SLOT_SCRIPT;

/**
 * Parse a slot-event relay message into (eventType, slot). Two shapes are
 * accepted so the busy-queue PRIMARY KEY (slot, event_type) keeps coalescing
 * across the cutover:
 *
 *   1. Legacy slash command (kept for manual PM invocation + back-compat):
 *      "/slot-idle 3", "/slot-active 1", "/check-slot 2", "/slot-blocked 4".
 *
 *   2. MoP message prefix (Rajiv directive 2026-05-22 22:23 IST thread
 *      `1779468118.901709` — "inject is a string instead of a command"):
 *      "MoP: slot 3 idle", "MoP: slot 1 active",
 *      "MoP: check slot 2 for <reason>", "MoP: slot 4 blocked".
 *
 * Both shapes resolve to the same `eventType` taxonomy
 * (slot-idle | slot-active | check-slot | slot-blocked) so logging and the
 * (slot, event_type) coalesce key stay stable.
 *
 * Returns null for free-form messages (escalation comments,
 * plan-approval-needed, hourly-ops-audit "MoP: hourly ops audit for <reason>",
 * etc.) — those go through the queue keyed by their own slot extraction or
 * fall back to slot 0 in the freeform path.
 *
 * NOTE (R1 fix 4 option b, 2026-05-26 thread `1779790681.847219`): hourly-ops-audit
 * payloads (Shape 3: "MoP: hourly ops audit for <reason>") are INTENTIONALLY
 * NOT keyed here. They route through the freeform branch of `injectToPM` so
 * multiple distinct audit payloads do not collapse onto one another when PM is
 * busy. The (slot, event_type) coalesce key in Shapes 1/2a/2b is the right
 * choice for slot-keyed events (only the latest matters); the audit payload
 * is a one-shot exception list whose contents matter individually.
 */
function parseRelayMessage(message: string): { eventType: string; slot: number } | null {
  // First-line scan: a multi-line check-slot payload starts with the prefix
  // line and is followed by the bg-script summary. We only key off the
  // prefix.
  const firstLine = message.split("\n", 1)[0];

  // Shape 1: legacy slash command.
  const slash = /^\/(slot-idle|slot-active|check-slot|slot-blocked)\s+(\d+)\b/.exec(firstLine);
  if (slash) {
    const slot = parseInt(slash[2], 10);
    if (!Number.isFinite(slot) || slot < 0 || slot > 4) return null;
    return { eventType: slash[1], slot };
  }

  // Shape 2a: "MoP: check slot N ..." (with optional `for <reason>` tail).
  const mopCheck = /^MoP:\s+check\s+slot\s+(\d+)\b/i.exec(firstLine);
  if (mopCheck) {
    const slot = parseInt(mopCheck[1], 10);
    if (!Number.isFinite(slot) || slot < 0 || slot > 4) return null;
    return { eventType: "check-slot", slot };
  }

  // Shape 2b: "MoP: slot N idle | active | blocked".
  const mopSlot = /^MoP:\s+slot\s+(\d+)\s+(idle|active|blocked)\b/i.exec(firstLine);
  if (mopSlot) {
    const slot = parseInt(mopSlot[1], 10);
    if (!Number.isFinite(slot) || slot < 0 || slot > 4) return null;
    return { eventType: `slot-${mopSlot[2].toLowerCase()}`, slot };
  }

  // Shape 3 ("MoP: hourly ops audit for <reason>"): intentional pass-through.
  // Returning null here routes the message through injectToPM's freeform branch,
  // which enqueues with slot=0 + a hash-derived event_type so concurrent audit
  // payloads do not collapse onto each other when PM is busy.
  return null;
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
  /**
   * Debounce default raised 7s → 30s on 2026-05-13 14:13 IST per Rajiv
   * directive thread `1778661820.586119`: "queue all notifications and send
   * the last one when pm goes idle after stop hook fire." 7s was too short
   * for agentic PM loops — inter-tool gaps can exceed 7s without the PM
   * being truly idle, causing premature drains that injected commands
   * into the PM's queued-prompt buffer while the model was still working.
   * 30s + JSONL-mtime re-verification (see PM_JSONL_IDLE_MS) is the new
   * idle floor.
   */
  private static readonly DRAIN_DEBOUNCE_MS: number = (() => {
    const raw = process.env.MOP_DRAIN_DEBOUNCE_MS;
    const n = raw ? parseInt(raw, 10) : NaN;
    return Number.isFinite(n) && n >= 0 ? n : 30_000;
  })();
  /**
   * Bound on how many times the drain timer may re-arm while the PM JSONL
   * keeps moving. Prevents infinite re-arm if PM is in a long agentic
   * burst — after this many re-arms we accept the drain and trust the
   * coalesce layer to collapse rows to a single per-slot notification.
   */
  private static readonly DRAIN_MAX_REARMS = 10;
  private drainRearmCount = 0;

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
      this.drainRearmCount = 0;
      this.pmBusy = true;
      return { drained: 0 };
    }
    // busy=false — schedule debounced drain. Reset timer if one is already
    // pending so the window is from the LAST Stop, not the first.
    if (this.drainTimer) {
      clearTimeout(this.drainTimer);
    }
    this.drainRearmCount = 0;
    this.armDrainTimer(TmuxRelay.DRAIN_DEBOUNCE_MS);
    return { drained: 0 };
  }

  /**
   * Arm the debounced drain timer with optional JSONL-mtime re-verification.
   *
   * On timer fire:
   *   1. Read PM JSONL mtime. If JSONL was written within PM_JSONL_IDLE_MS,
   *      PM is still actively producing tokens (mid-tool or mid-message).
   *      Re-arm the timer; do NOT drain.
   *   2. If re-arm count exceeds DRAIN_MAX_REARMS (PM in a sustained burst),
   *      accept the drain — the coalesce layer collapses to one row per slot
   *      so at most 4 notifications fire.
   *   3. Else, drain and clear pmBusy.
   *
   * Rajiv directive 2026-05-13 14:13 IST thread `1778661820.586119`:
   * "queue up all these notifications and send the last one when pm goes
   * idle after stop hook fire" — Stop hook alone is not a strong enough
   * idle signal in agentic loops, so we cross-check JSONL mtime.
   */
  private armDrainTimer(delayMs: number): void {
    this.drainTimer = setTimeout(() => {
      const mtimeMs = pmJsonlMtimeMs();
      const ageMs = mtimeMs ? Date.now() - mtimeMs : Number.POSITIVE_INFINITY;
      const stillActive = ageMs < PM_JSONL_IDLE_MS;
      const maxRearmsHit = this.drainRearmCount >= TmuxRelay.DRAIN_MAX_REARMS;

      if (stillActive && !maxRearmsHit) {
        this.drainRearmCount += 1;
        console.log(
          `[relay-debug] drain re-armed — PM JSONL active (age=${Math.round(ageMs)}ms < ${PM_JSONL_IDLE_MS}ms) ` +
          `rearm=${this.drainRearmCount}/${TmuxRelay.DRAIN_MAX_REARMS}`
        );
        if (this.db) {
          this.db.logEvent(0, "pm_drain_rearmed", null, null, {
            jsonl_age_ms: Math.round(ageMs),
            jsonl_idle_threshold_ms: PM_JSONL_IDLE_MS,
            rearm_count: this.drainRearmCount,
            max_rearms: TmuxRelay.DRAIN_MAX_REARMS,
          });
        }
        // Re-arm for another PM_JSONL_IDLE_MS window (shorter than initial
        // debounce — we already passed the initial Stop debounce).
        this.armDrainTimer(PM_JSONL_IDLE_MS);
        return;
      }

      // Drain.
      this.pmBusy = false;
      const n = this.drainPMQueue();
      this.drainTimer = null;
      const reason = maxRearmsHit ? "max_rearms_hit" : "jsonl_idle_confirmed";
      console.log(
        `[relay-debug] debounce drain fired (${reason}) after ${delayMs}ms timer, ` +
        `jsonl_age=${Math.round(ageMs)}ms, drained ${n}`
      );
      if (this.db) {
        this.db.logEvent(0, "pm_debounce_drain_fired", null, null, {
          debounce_ms: delayMs,
          drained: n,
          reason,
          jsonl_age_ms: Math.round(ageMs),
          rearm_count: this.drainRearmCount,
        });
      }
      this.drainRearmCount = 0;
    }, delayMs);
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
      // Reconstruct message when payload is null (slot-event enqueue path).
      // Rajiv directive 2026-05-22 22:23 IST thread `1779468118.901709` —
      // inject is a string, not a command. Reconstruct in MoP shape so the
      // drained event hits the PM-side pm-context-injector.sh recognizer.
      // slot-blocked is preserved as slash form (out of cutover scope).
      let fallback: string;
      switch (row.event_type) {
        case "slot-idle":
        case "slot-active":
          fallback = `MoP: slot ${row.slot} ${row.event_type === "slot-idle" ? "idle" : "active"}`;
          break;
        case "check-slot":
          // Payload-less check-slot drain (rare — check-slot normally enqueues
          // with full bg-script payload via the freeform path because it's a
          // multi-line message). Emit MoP prefix without a reason.
          fallback = `MoP: check slot ${row.slot} for queued`;
          break;
        case "slot-blocked":
          fallback = `/slot-blocked ${row.slot}`;
          break;
        default:
          fallback = `/${row.event_type} ${row.slot}`;
      }
      const message = row.payload ?? fallback;
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
      // compact warning, queued-clear ack, hourly-ops-audit). Try to extract
      // slot from "slot N" substring; otherwise key by slot 0 + a synthetic
      // event type derived from a hash so multiple distinct free-form
      // messages for the same slot don't collapse onto each other.
      // Hourly-audit ("MoP: hourly ops audit for <reason>") deliberately
      // lands here per parseRelayMessage Shape 3 note (R1 fix 4 option b,
      // 2026-05-26 thread `1779790681.847219`) — each audit payload is a
      // one-shot exception list whose contents matter individually.
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
      const firstLine = message.split("\n", 1)[0];
      console.log(`[relay-debug] injectToPM → ${firstLine}${message.includes("\n") ? " (+multiline payload)" : ""}`);
      if (message.includes("\n")) {
        // Multi-line payload (e.g., /check-slot N\n<bg-summary>): use
        // load-buffer + paste-buffer + Enter — same pattern sendToSlot uses
        // for multi-line content. send-keys with embedded \n would press
        // Enter on each newline and submit the prompt prematurely.
        // Rajiv directive 2026-05-15 13:44 IST thread `1778831723.165019`:
        // "make it inline" — MoP injects command + bg-output as one atomic prompt.
        const tmpFile = `/tmp/mop-pm-inject-${Date.now()}.txt`;
        const bufName = `mop-pm-inject`;
        fs.writeFileSync(tmpFile, message);
        try {
          execSync(
            `tmux load-buffer -b ${bufName} ${shellEscape(tmpFile)} && ` +
            `tmux paste-buffer -b ${bufName} -t ${this.pmPaneAddress} -d && ` +
            `sleep 0.3 && ` +
            `tmux send-keys -t ${this.pmPaneAddress} Enter && ` +
            `sleep 0.5`,
            { timeout: 10_000 }
          );
        } finally {
          try { fs.unlinkSync(tmpFile); } catch { /* ignore */ }
        }
      } else {
        execSync(
          `tmux send-keys -t ${this.pmPaneAddress} ${shellEscape(message)} && ` +
          `sleep 0.3 && ` +
          `tmux send-keys -t ${this.pmPaneAddress} Enter && ` +
          `sleep 0.5`,
          { timeout: 10_000 }
        );
      }
      console.log(`[relay-debug] injectToPM success → ${firstLine}`);
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
    // Inject MoP message prefix (replaces former slash command `/slot-idle N`).
    // Rajiv directive 2026-05-22 22:23 IST thread `1779468118.901709`:
    // "inject is a string instead of a command." PM-side
    // pm-context-injector.sh recognizes the prefix and emits
    // [MoP_SLOT_NOTIFICATION] system-reminder hinting Skill(slot-idle).
    // Busy-queue (slot, event_type) coalesce key unchanged — parseRelayMessage
    // recognizes both shapes.
    this.injectToPM(`MoP: slot ${slot.slot} idle`);
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

  /**
   * v3 (Rajiv directive 2026-05-09 14:15 IST): direct tmux send-keys.
   *
   * Drops the send-to-slot.sh wait-for-idle wrapper. When a slot is hung
   * at "Context limit reached" wall (or any other state that prevents
   * chevron-idle transition), the wrapper's pre-send confirmation poll
   * times out and the wrapper exits ETIMEDOUT — but keystrokes never
   * reach the pane because the wrapper bails before the send.
   *
   * v3 sends keystrokes directly via `tmux send-keys` / `tmux paste-buffer`
   * regardless of slot's main-thread state. Force flag becomes implicit
   * (every send is unconditional). Raw mode dispatches key literals
   * (Escape, C-c, BTab, S-Enter); text mode uses load-buffer + paste-buffer
   * for multi-line safety, then Enter.
   *
   * Retry semantics kept: only true tmux command failures (pane gone,
   * server down) will fail now. Per-(slot, command) cooldown kept as
   * defense-in-depth.
   *
   * `force` parameter is now a no-op (kept for callsite compatibility).
   */
  sendToSlot(slotNum: number, command: string, _force = false, raw = false): boolean {
    void _force; // v3: every send is unconditional
    const paneAddr = `0:0.${slotNum}`;
    const cooldownKey = `${slotNum}:${command.slice(0, 80)}`;

    // Per-(slot, command) cooldown — defensive layer against tight retry
    // loops from external callers that lack their own dedup tracker.
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
        if (raw) {
          // Raw: tmux interprets key names (Escape, C-c, BTab, etc.).
          // Do NOT pass -l (literal) flag — that would type the name as text.
          execSync(
            `tmux send-keys -t ${paneAddr} ${shellEscape(command)}`,
            { timeout: 5_000 }
          );
        } else {
          // Text mode: load-buffer + paste-buffer + Enter is multi-line safe
          // and avoids quoting hell vs. a single send-keys 'long $string'.
          const tmpFile = `/tmp/mop-send-${slotNum}-${Date.now()}-${attempt}.txt`;
          const bufName = `mop-send-${slotNum}`;
          fs.writeFileSync(tmpFile, command);
          try {
            execSync(
              `tmux load-buffer -b ${bufName} ${shellEscape(tmpFile)}`,
              { timeout: 3_000 }
            );
            execSync(
              `tmux paste-buffer -b ${bufName} -t ${paneAddr} -d`,
              { timeout: 3_000 }
            );
            // Small breathing room so the TUI registers the paste before Enter.
            // Matches the 0.3s that injectDirect uses for PM pane sends.
            execSync(`sleep 0.3`, { timeout: 2_000 });
            execSync(
              `tmux send-keys -t ${paneAddr} Enter`,
              { timeout: 3_000 }
            );
          } finally {
            try { fs.unlinkSync(tmpFile); } catch { /* ignore */ }
          }
        }
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
          `${TmuxRelay.SEND_MAX_RETRIES + 1} failed (${isTimeout ? "tmux timeout" : `code=${code ?? "unknown"}`})`
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
    const reason = code === "ETIMEDOUT" ? "tmux_send_timeout" : "send_error";
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
