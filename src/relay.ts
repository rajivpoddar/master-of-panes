/**
 * MoP Tmux Relay — Inject formatted messages into PM pane
 *
 * Replaces scattered bash scripts (slot-idle-notify.sh, etc.) with a single
 * relay that formats events and injects them into the PM's tmux pane.
 *
 * Uses the send-to-slot.sh script infrastructure for reliable delivery.
 */

import * as fs from "node:fs/promises";
import { execShell, sleep } from "./asyncCommand.js";
import type { LogManager } from "./logs.js";
import type { MoPDatabase } from "./db.js";
import type { JsonlActivitySignal } from "./jsonlActivity.js";
import type { MoPConfig, SlotState } from "./types.js";
import { DEFAULT_DEV_SLOT_COUNT, isValidDevSlot, isValidRuntimeSlot } from "./slotConfig.js";
import { paneAddress, paneAddress as resolvePaneAddress, verifyPaneIdentity } from "./paneIdentity.js";
import { QUEUED_HINT, composerOwnsOnlyPayload, composerText, INJECT_ENTER_DELAY_MS, isSlashCommand, resolveSlashCommandEnterDelayMs, slashComposerClearable, submitWithComposerCheck } from "./composer.js";
import { withSlotSendLock } from "./slotSendLock.js";
import { parseForegroundClaude, type DevExitAttempt } from "./devExitAttempt.js";
import type { PMClearFenceRefusal } from "./pmClearLatch.js";
import type { PasteGuard } from "./pasteGuard.js";

export type SlotActivityState = "active" | "idle" | "unknown";

/** Slots 1-4 set HEYDONNA_SLOT_STOP_GATED. A Stop that reaches MoP from them is allowed only when marked. */
export function isGatedDevSlot(slotNum: number): boolean {
  return slotNum >= 1 && slotNum <= 4;
}

type ExitOwnerPin = Readonly<{
  assignment: string;
  boot: number | null;
  input: number | null;
  work: string | null;
  turn: string | null;
  turnStarted: string | null;
  turnState: string;
  stopSession: string | null;
  stop: number | null;
  inboundGeneration?: number;
}>;

type PendingExitDispatch = {
  episode: string;
  work: Promise<unknown>;
  successor?: ExitOwnerPin;
  /** Monotonic Stop id of the newest pin this episode has accepted. */
  acceptedStop: number | null;
};

/** Shared PM + numbered-slot paste->Enter dwell (see composer.ts). */
export const PM_INJECT_ENTER_DELAY_MS: number = INJECT_ENTER_DELAY_MS;

/** Visible Claude exit dialog, not a phrase left in scrollback or a new composer. */
export function isExitConfirmationDialog(text: string): boolean {
  const lines = text.replace(/\x1b\[[0-9;]*m/g, "").split(/\r?\n/);
  const title = lines.reduce((last, line, index) => /^\s*[│┃║]?\s*Background work is running[.!]?\s*$/.test(line) ? index : last, -1);
  if (title < 0) return false;
  const dialog = lines.slice(title + 1);
  return dialog.some(line => /^[\s│┃║❯>]*1[.)]\s+Exit and stop tasks\s*[│┃║]?\s*$/.test(line))
    && dialog.some(line => /Enter to confirm/i.test(line))
    && !dialog.some(line => /^\s*[❯>]\s*/.test(line) && !/^\s*[❯>]\s*[123][.)]\s/.test(line));
}

/** Complete numbered PM menu from the captured canonical dialog fixture. */
export function isCompletePMExitMenu(text: string): boolean {
  const plain = text.replace(/\x1b\[[0-9;]*m/g, "");
  return isExitConfirmationDialog(plain)
    && /^[\s│┃║❯>]*1\.\s+Exit and stop tasks\s*[│┃║]?\s*$/m.test(plain)
    && /^[\s│┃║❯>]*2\.\s+Move to background and exit\s*[│┃║]?\s*$/m.test(plain)
    && /^[\s│┃║❯>]*3\.\s+Stay\s*[│┃║]?\s*$/m.test(plain);
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
  const lines = message.split("\n");
  const firstLine = /^#\s*mop\b/i.test(lines[0] ?? "")
    ? (lines[1] ?? "")
    : (lines[0] ?? "");

  // Shape 1: legacy slash command.
  const slash = /^\/(slot-idle|slot-active|check-slot|slot-blocked)\s+(\d+)\b/.exec(firstLine);
  if (slash) {
    const slot = parseInt(slash[2], 10);
    if (!isValidRuntimeSlot(slot)) return null;
    return { eventType: slash[1], slot };
  }

  // Shape 2a: "MoP: check slot N ..." (with optional `for <reason>` tail).
  const mopCheck = /^MoP:\s+check\s+slot\s+(\d+)\b/i.exec(firstLine);
  if (mopCheck) {
    const slot = parseInt(mopCheck[1], 10);
    if (!isValidRuntimeSlot(slot)) return null;
    return { eventType: "check-slot", slot };
  }

  // Shape 2b: "MoP: slot N idle | active | blocked".
  const mopSlot = /^MoP:\s+slot\s+(\d+)\s+(idle|active|blocked)\b/i.exec(firstLine);
  if (mopSlot) {
    const slot = parseInt(mopSlot[1], 10);
    if (!isValidRuntimeSlot(slot)) return null;
    return { eventType: `slot-${mopSlot[2].toLowerCase()}`, slot };
  }

  // Shape 3 ("MoP: hourly ops audit for <reason>"): intentional pass-through.
  // Returning null here routes the message through injectToPM's freeform branch,
  // which enqueues with slot=0 + a hash-derived event_type so concurrent audit
  // payloads do not collapse onto each other when PM is busy.
  return null;
}

function formatMopTimestamp(date = new Date()): string {
  return date.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  });
}

function withMopSlotHeader(message: string, parsed: { eventType: string; slot: number } | null): string {
  if (!parsed) return message;
  if (/^#\s*mop\b/i.test(message)) return message;
  const lines = message.split("\n");
  const firstLine = lines[0] ?? "";
  const timestamp = formatMopTimestamp();
  if (/^MoP:/i.test(firstLine)) {
    if (!/\|\s*\d{1,2}:\d{2}\s*[AP]M\b/i.test(firstLine)) {
      lines[0] = `${firstLine} | ${timestamp}`;
    }
    return lines.join("\n");
  }

  switch (parsed.eventType) {
    case "check-slot":
      lines[0] = `MoP: check slot ${parsed.slot} | ${timestamp}`;
      break;
    case "slot-idle":
      lines[0] = `MoP: slot ${parsed.slot} idle | ${timestamp}`;
      break;
    case "slot-active":
      lines[0] = `MoP: slot ${parsed.slot} active | ${timestamp}`;
      break;
    case "slot-blocked":
      lines[0] = `MoP: slot ${parsed.slot} blocked | ${timestamp}`;
      break;
    default:
      lines[0] = `${firstLine} | ${timestamp}`;
  }
  return lines.join("\n");
}

export type PMDrainDecision =
  | { action: "rearm"; reason: "activity-unknown" | "jsonl-active"; ageMs: number | null }
  | { action: "drain"; reason: "jsonl-idle" | "max-rearms-hit"; ageMs: number };

export function decidePMDrain(
  activity: JsonlActivitySignal,
  nowMs: number,
  idleThresholdMs: number,
  maxRearmsHit: boolean,
): PMDrainDecision {
  if (activity.kind === "unknown") {
    // A healthy watcher with no event since process start is expected after a
    // Codex/OMP restart: Codex does not write the legacy Claude JSONL stream.
    // Keep the conservative busy state through the full debounce window, then
    // honor the existing max-rearm escape hatch so a real Stop can eventually
    // establish idle. A failed watcher remains fail-closed indefinitely.
    if (activity.reason === "no-event" && maxRearmsHit) {
      return { action: "drain", reason: "max-rearms-hit", ageMs: 0 };
    }
    return { action: "rearm", reason: "activity-unknown", ageMs: null };
  }

  const ageMs = Math.max(0, nowMs - activity.observedAtMs);
  if (ageMs < idleThresholdMs && !maxRearmsHit) {
    return { action: "rearm", reason: "jsonl-active", ageMs };
  }
  return {
    action: "drain",
    reason: maxRearmsHit ? "max-rearms-hit" : "jsonl-idle",
    ageMs,
  };
}

export type PMSubmitKey = "Enter" | "C-q";
export type PMRuntime = "claude" | "omp";

/**
 * Effect-boundary revalidation for a queued PM row: deliver it, keep it queued
 * unchanged (uncertain), or drop it because its purpose expired/completed.
 */
export type PMQueuedEffectVerdict = "deliver" | "retain" | { drop: string };
export type PMQueuedEffectCheck = (row: { slot: number; event_type: string; enqueued_at: string }) => PMQueuedEffectVerdict;

export type PMSubmitResult = {
  ok: boolean;
  submitKey: PMSubmitKey;
  /** True when paste succeeded but submit outcome is not authoritative. */
  ambiguous: boolean;
  /** True when MoP durably accepted the message but the pane was not booted. */
  queued?: boolean;
  /** Set when a PasteGuard refused before any pane write (a proven zero-effect). */
  refused?: string;
  /** Set when a PasteGuard withheld Enter AFTER the paste: the text may be in the pane, never zero-effect. */
  withheld?: string;
};

type PMQueueRow = {
  slot: number;
  event_type: string;
  payload: string | null;
  enqueued_at: string;
};

type DirectSubmitResult = {
  ok: boolean;
  ambiguous: boolean;
  notReady?: boolean;
  /** A PasteGuard refused before any pane write. */
  refused?: string;
  /** A PasteGuard withheld Enter after the paste; no further key was sent. */
  withheld?: string;
};

/**
 * Runtime-aware PM submit key for automated PM delivery.
 *
 * Native Claude always receives Enter after the paste/render dwell. Claude
 * Code owns its active-turn prompt queue and does not use OMP's C-q binding.
 * OMP keeps idle → Enter and busy/unknown → C-q.
 *
 * Numbered slots never route through this decision: they keep the always-Enter
 * sendToSlot path where steering the active turn is the intended behavior.
 */
export function resolvePMRuntime(value: string | undefined): PMRuntime {
  return value === "omp" ? "omp" : "claude";
}

export function decidePMSubmitKey(
  pmBusy: boolean | null | undefined,
  runtime: PMRuntime = "claude",
): PMSubmitKey {
  if (runtime === "claude") return "Enter";
  return pmBusy === false ? "Enter" : "C-q";
}

export const PM_CLEAR_COMMAND = "/" + "clear";

export type PMClearWriteResult = { ok: boolean; zeroEffect: boolean; busy: boolean; reason?: string };

export class TmuxRelay {
  private pmPaneAddress: string;
  private pmRuntime: PMRuntime;
  private logManager: LogManager | null = null;
  private db: MoPDatabase | null = null;
  /**
   * PM busy observation. Startup/unknown is null and remains fail-closed until
   * the canonical PM status path proves idle or busy.
   */
  // null is the startup/unknown observation and remains fail-closed.
  private pmBusy: boolean | null = null;

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
   * The timer now debounces the authoritative OMP Stop observation only.
   */
  private static readonly DRAIN_DEBOUNCE_MS: number = (() => {
    const raw = process.env.MOP_DRAIN_DEBOUNCE_MS;
    const n = raw ? parseInt(raw, 10) : NaN;
    return Number.isFinite(n) && n >= 0 ? n : 30_000;
  })();
  private drainGeneration = 0;
  private static readonly CHECK_SLOT_QUEUE_MAX_AGE_MS: number = (() => {
    const raw = process.env.MOP_CHECK_SLOT_QUEUE_MAX_AGE_MS;
    const n = raw ? parseInt(raw, 10) : NaN;
    return Number.isFinite(n) && n >= 0 ? n : 90_000;
  })();
  private directInjectChain: Promise<unknown> = Promise.resolve();
  private pmInboundGeneration = 0;
  private pmInboundPending = 0;
  /** Serialize queue selection through post-submit deletion as one operation. */
  private pmDrainChain: Promise<number> = Promise.resolve(0);
  private directInjectSeq = 0;
  private pendingExitDispatch = new Map<number, PendingExitDispatch>();
  /** One PM notice per (slot, episode, outcome). The exit itself still retries. */
  private exitNoticeSent = new Set<string>();
  /**
   * After a Stop, wait this long before /exit so a turn that continued
   * (a blocked Stop hook) is not treated as idle. Not a PM input gate.
   * Tests set 0.
   */
  exitSettleMs = 500;
  private runShell: typeof execShell;
  private readonly pmPaneReadyOverride?: () => Promise<boolean>;
  private readonly pmIdleHandoff?: () => Promise<void>;

  constructor(
    config: MoPConfig,
    deps: { runShell?: typeof execShell; pmRuntime?: PMRuntime; pmPaneReady?: () => Promise<boolean>; pmIdleHandoff?: () => Promise<void> } = {},
  ) {
    this.pmPaneAddress = config.pmPaneAddress;
    this.pmRuntime = deps.pmRuntime ?? resolvePMRuntime(process.env.MOP_PM_RUNTIME);
    this.runShell = deps.runShell ?? execShell;
    this.pmPaneReadyOverride = deps.pmPaneReady;
    this.pmIdleHandoff = deps.pmIdleHandoff;
  }

  private pmMaintenanceGate?: { isHeld(): boolean; isSafe(): boolean; deferCLIInput(): void; isRespawnInProgress?(): boolean };
  private pmMaintenanceHandoffInFlight = false;

  setPMMaintenanceGate(gate: { isHeld(): boolean; isSafe(): boolean; deferCLIInput(): void; isRespawnInProgress?(): boolean }): void {
    this.pmMaintenanceGate = gate;
  }

  /** Attach the MoP DB so injectToPM can enqueue when PM is busy. */
  setDatabase(db: MoPDatabase): void {
    this.db = db;
  }

  /**
   * Set the PM busy state.
   *   busy=true  → cancel pending drain, mark PM busy.
   *   busy=false → schedule debounced drain (DRAIN_DEBOUNCE_MS); the relay
   *                remains non-idle/fail-closed until the timer fires.
   *                Subsequent busy=true or busy=false resets/cancels it.
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
      this.drainGeneration += 1;
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
    this.armDrainTimer(TmuxRelay.DRAIN_DEBOUNCE_MS);
    return { drained: 0 };
  }

  /**
   * Drain after the canonical OMP Stop observation has remained stable for
   * the debounce window. Legacy Claude JSONL is deliberately not consulted:
   * it is not an authoritative PM runtime signal for OMP sessions.
   */
  private armDrainTimer(delayMs: number): void {
    const generation = ++this.drainGeneration;
    this.drainTimer = setTimeout(() => {
      void this.runDrainCheck(delayMs, generation);
    }, delayMs);
  }

  private async runDrainCheck(delayMs: number, generation: number): Promise<void> {
    try {
      if (generation !== this.drainGeneration) return;

      if (this.pmMaintenanceGate?.isHeld()) {
        // Deliver inbound work first. Only clear/restart waits for the safe
        // boundary; maintenance must never hold the message-delivery queue.
        await this.drainPMQueue();
        await this.directInjectChain;
        if ((this.db?.getPendingPMEventCount() ?? 0) > 0) {
          this.pmMaintenanceGate.deferCLIInput();
          this.pmBusy = true;
          return;
        }
        if (generation !== this.drainGeneration || !this.pmMaintenanceGate.isSafe()) return;
        if (!(await this.isPMComposerQueueEmpty()) || !this.pmMaintenanceGate.isSafe()) {
          this.pmMaintenanceGate.deferCLIInput();
          this.pmBusy = true;
          return;
        }
        this.pmBusy = false;
        this.drainTimer = null;
        this.db?.logEvent(0, "pm_debounce_drain_fired", null, null, {
          debounce_ms: delayMs, drained: 0, reason: "pm_soft_boundary_ready",
        });
        if (this.pmMaintenanceHandoffInFlight) return;
        this.pmMaintenanceHandoffInFlight = true;
        try { await this.pmIdleHandoff?.(); }
        finally { this.pmMaintenanceHandoffInFlight = false; }
        return;
      }

      // The POST /pm-status stop hook is the authoritative OMP observation.
      // Only this explicit terminal observation transitions the relay to idle;
      // absent/unknown observations remain null and select C-q.
      this.pmBusy = false;
      const n = await this.drainPMQueue();
      this.drainTimer = null;
      console.log(
        `[relay-debug] debounce drain fired (omp_idle_confirmed) after ${delayMs}ms timer, drained ${n}`
      );
      if (this.db) {
        this.db.logEvent(0, "pm_debounce_drain_fired", null, null, {
          debounce_ms: delayMs,
          drained: n,
          reason: "omp_idle_confirmed",
        });
      }
      // Continue a checkpoint only after this existing Stop generation is
      // proven idle and the queue drained without starting another turn.
      if (this.pmIdleHandoff && n === 0) {
        await withSlotSendLock(0, async () => {
          if (generation !== this.drainGeneration || this.pmBusy !== false
              || (this.db?.getPendingPMEventCount() ?? 0) !== 0) return;
          await this.pmIdleHandoff!();
        });
      }
    } catch (error) {
      if (generation !== this.drainGeneration) return;
      this.pmBusy = null;
      console.error(`[relay-debug] OMP idle drain failed: ${String(error)}`);
    }
  }

  /** Public read of the busy flag (diagnostics). */
  isPMBusy(): boolean {
    return this.pmBusy !== false;
  }

  /** True only after the canonical PM status path proves idle. */
  isPMIdleProven(): boolean {
    return this.pmBusy === false;
  }

  /**
   * Drain queued PM-bound events into the PM pane. Called from setPMBusy
   * on busy→idle transition. Each row's payload (if non-null) is preferred
   * over the canonical slash-command form so callers can queue free-form
   * messages too.
   *
   * Returns the count of rows actually injected.
   */
  async drainPMQueue(): Promise<number> {
    const run = (): Promise<number> => this.drainPMQueueOnce();
    const result = this.pmDrainChain.then(run, run);
    this.pmDrainChain = result.catch(() => 0);
    return result;
  }

  private async drainPMQueueOnce(): Promise<number> {
    if (!this.db) return 0;
    // Do not destructively coalesce/drain rows until the configured agent is
    // actually running in the PM pane. In particular, never type into zsh.
    if (!(await this.isPMPaneReady())) return 0;
    const rows = this.db.drainPendingPMEvents();
    let injected = 0;
    for (const row of rows) {
      if (row.event_type === "check-slot" && !row.payload) {
        this.db.logEvent(row.slot, "pm_queue_dropped", null, null, {
          event_type: row.event_type,
          reason: "missing-check-slot-payload",
          enqueued_at: row.enqueued_at,
        });
        this.db.deletePendingPMEvent(row.slot, row.event_type, row.enqueued_at);
        continue;
      }
      if (row.event_type === "check-slot") {
        const drop = await this.shouldDropQueuedCheckSlot(row);
        if (drop) {
          this.db.logEvent(row.slot, "pm_queue_dropped", null, null, {
            event_type: row.event_type,
            enqueued_at: row.enqueued_at,
            ...drop,
          });
          console.log(
            `[relay-debug] dropped queued check-slot ${row.slot} — ${drop.reason}` +
            (drop.latest_reason ? ` (${drop.latest_reason})` : "")
          );
          this.db.deletePendingPMEvent(row.slot, row.event_type, row.enqueued_at);
          continue;
        }
      }

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
      const deliveryState = this.db.getPMQueueDeliveryState(
        row.slot,
        row.event_type,
        message,
        row.enqueued_at,
      );
      if (deliveryState === "delivered") {
        this.db.deletePendingPMEvent(row.slot, row.event_type, row.enqueued_at);
        this.db.logEvent(row.slot, "pm_queue_replay_suppressed", null, null, {
          event_type: row.event_type,
          message: message.slice(0, 200),
          enqueued_at: row.enqueued_at,
          reason: "delivery-marker-already-recorded",
        });
        injected++;
        continue;
      }

      if (deliveryState === "started") {
        // A crash after paste/submit but before terminal logging leaves the
        // exact occurrence ambiguous. Never replay its key automatically.
        this.db.logEvent(row.slot, "pm_queue_replay_suppressed", null, null, {
          event_type: row.event_type,
          message: message.slice(0, 200),
          enqueued_at: row.enqueued_at,
          reason: "delivery-attempt-outcome-ambiguous",
        });
        continue;
      }

      const submitted = await this.deliverPMRowNow({
        slot: row.slot,
        event_type: row.event_type,
        payload: message,
        enqueued_at: row.enqueued_at,
      });
      if (!submitted.ok) {
        continue;
      }
      injected++;
    }
    return injected;
  }

  private async shouldDropQueuedCheckSlot(row: {
    slot: number;
    event_type: string;
    payload: string | null;
    enqueued_at: string;
  }): Promise<Record<string, unknown> | null> {
    const enqueuedMs = parseDbTimestampMs(row.enqueued_at);
    if (Number.isFinite(enqueuedMs)) {
      const ageMs = Date.now() - enqueuedMs;
      if (ageMs > TmuxRelay.CHECK_SLOT_QUEUE_MAX_AGE_MS) {
        return {
          reason: "stale-check-slot-queue-age",
          age_ms: Math.round(ageMs),
          max_age_ms: TmuxRelay.CHECK_SLOT_QUEUE_MAX_AGE_MS,
        };
      }

      const checkFile = `/tmp/slot-${row.slot}-check.txt`;
      try {
        const st = await fs.stat(checkFile);
        if (st.mtimeMs > enqueuedMs) {
          const latest = await fs.readFile(checkFile, "utf8");
          const skipMatch = /^INJECT_DECISION:skip(?:\s+REASON:([^\n]*))?/m.exec(latest);
          if (skipMatch) {
            return {
              reason: "superseded-by-latest-check-slot-skip",
              latest_reason: (skipMatch[1] || "unspecified").trim().slice(0, 160),
              check_file: checkFile,
              check_file_mtime: new Date(st.mtimeMs).toISOString(),
            };
          }
        }
      } catch {
        // Missing/unreadable check file should not drop a fresh queued inject.
      }
    }
    return null;
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
   * Coalesce semantics live in the DB layer: slot-state transitions share the
   * (slot, event_type) key and are coalesced by drainPendingPMEvents, while
   * distinct freeform/cadence/ops event identities remain separate.
   *
   * IMPORTANT EXCLUSIONS (do NOT route through this queue):
   * - PM self-clear is handled by the MoP clear path, which routes `/clear`
   *   through `/slots/0/send` and waits for SessionStart:clear acknowledgement.
   * - `handleSessionStart` slot=0 source=compact (hooks.ts) — uses raw
   *   `tmux send-keys 'continue your work' Enter` directly, bypassing
   *   the relay.
   *
   * Both excluded paths are PM-internal recovery nudges, not slot-event
   * signals — they must never sit in a queue waiting to drain.
   */
  injectToPM(message: string, eventTypeOverride?: string): boolean {
    ++this.pmInboundGeneration;
    if (this.pmRuntime === "claude") {
      void this.injectNativeClaudeOrQueue(message, eventTypeOverride);
      return true;
    }
    if (!this.db) {
      console.error("[relay] PM delivery unavailable: no database for durable fallback");
      return false;
    }
    // Persist before attempting delivery, then enter the same serialized
    // boundary regardless of the current observation. Explicit idle selects
    // Enter; busy/unknown selects C-q. No later Stop hook is required for a
    // queued occurrence to become a PM turn.
    this.enqueuePMMessage(
      message,
      eventTypeOverride,
      this.pmBusy === false ? "injectToPM-idle" : "injectToPM",
    );
    void this.drainPMQueue();
    return true;
  }

  private async injectNativeClaudeOrQueue(message: string, eventTypeOverride?: string): Promise<void> {
    this.pmMaintenanceGate?.deferCLIInput();
    if (!(await this.isPMPaneReady())) {
      if (!this.db) {
        console.error("[relay] PM pane is not booted and no database is available for durable queueing");
        return;
      }
      this.enqueuePMMessage(message, eventTypeOverride, "native-claude-pane-not-ready");
      return;
    }
    const parsed = parseRelayMessage(message);
    const decorated = withMopSlotHeader(message, parsed);
    const result = await this.injectDirectWithSubmitKey(decorated, "Enter");
    if (!result.ok) {
      if (result.notReady && this.db) {
        this.enqueuePMMessage(message, eventTypeOverride, "native-claude-pane-became-not-ready");
        return;
      }
      console.error("[relay] Native-Claude PM inject failed; prompt was not persisted");
    }
  }

  private enqueuePMMessage(
    message: string,
    eventTypeOverride: string | undefined,
    via: string,
  ): PMQueueRow {
    if (!this.db) throw new Error("PM queue database is unavailable");
    const parsed = parseRelayMessage(message);
    const payload = withMopSlotHeader(message, parsed);
    const slot = parsed?.slot ?? (() => {
      const slotMatch = /\bslot\s+(\d+)\b/i.exec(message);
      return slotMatch ? Math.min(DEFAULT_DEV_SLOT_COUNT, Math.max(0, parseInt(slotMatch[1], 10))) : 0;
    })();
    const eventType = eventTypeOverride ?? parsed?.eventType ?? `freeform-${simpleHash(message)}`;
    this.db.enqueuePendingPMEvent(slot, eventType, payload);
    const row = this.db.peekPendingPMEvents().find(
      (candidate) => candidate.slot === slot &&
        candidate.event_type === eventType &&
        candidate.payload === payload,
    );
    if (!row) throw new Error("PM queue occurrence was not persisted");
    this.db.logEvent(slot, "pm_queue_enqueued", null, null, {
      event_type: eventType,
      via,
      message: payload.slice(0, 200),
    });
    console.log(`[relay-debug] injectToPM queued (${via}) → ${payload.slice(0, 80)}`);
    return row;
  }

  /**
   * Priority PM signal path. It preserves the exact occurrence in the existing
   * queue before starting an asynchronous drain; a tmux failure therefore
   * remains retryable instead of being reported as a successful fire-and-forget
   * send.
   */
  injectToPMDirect(message: string): boolean {
    ++this.pmInboundGeneration;
    const parsed = parseRelayMessage(message);
    const decorated = withMopSlotHeader(message, parsed);
    if (this.pmRuntime === "claude") {
      void this.injectNativeClaudeOrQueue(message, parsed?.eventType);
      return true;
    }
    if (parsed && this.db) {
      if (parsed.eventType === "slot-active") {
        const deleted = this.db.deletePendingPMEvent(parsed.slot, "check-slot");
        if (deleted > 0) {
          this.db.logEvent(parsed.slot, "pm_queue_dropped", null, null, {
            event_type: "check-slot",
            reason: "superseded-by-direct-slot-active",
            dropped_count: deleted,
          });
          console.log(`[relay-debug] dropped queued check-slot ${parsed.slot} — superseded by direct slot-active`);
        }
      }
      this.db.logEvent(parsed.slot, "pm_direct_inject_bypass_queue", null, null, {
        event_type: parsed.eventType,
        message: decorated.slice(0, 200),
      });
    }
    if (!this.db) {
      console.error("[relay] PM direct delivery unavailable: no database for durable fallback");
      return false;
    }
    this.enqueuePMMessage(decorated, parsed?.eventType, "injectToPMDirect");
    void this.drainPMQueue();
    return true;
  }


  /**
   * The single MoP relay submit primitive for automated PM delivery
   * (ordinary slot→PM status bodies routed via POST /slots/0/send).
   *
   * Busy-aware submit key: idle → Enter, busy/unknown → C-q. See
   * decidePMSubmitKey. Unlike the queued injectToPM path, this primitive
   * always delivers immediately (it never sits in the busy-queue), but it
   * selects the key that queues a follow-up in OMP instead of steering the
   * active turn when PM is busy.
   */
  async submitToPM(message: string, eventTypeOverride?: string, guard?: PasteGuard): Promise<PMSubmitResult> {
    ++this.pmInboundGeneration;
    this.pmMaintenanceGate?.deferCLIInput();
    // Cadence has an exact durable event identity. Native Claude must use the
    // same attempt journal as queue drains, not the ordinary inbound fast path.
    const durableCadence = eventTypeOverride?.startsWith("cadence-") === true;
    if (this.pmRuntime === "claude" && !durableCadence) {
      if (!(await this.isPMPaneReady())) {
        if (!this.db) return { ok: false, submitKey: "Enter", ambiguous: false };
        this.enqueuePMMessage(message, eventTypeOverride, "submitToPM-pane-not-ready");
        return { ok: true, submitKey: "Enter", ambiguous: false, queued: true };
      }
      const parsed = parseRelayMessage(message);
      const decorated = withMopSlotHeader(message, parsed);
      const result = await this.injectDirectWithSubmitKey(decorated, "Enter", guard);
      if (result.notReady && this.db) {
        this.enqueuePMMessage(message, eventTypeOverride, "submitToPM-pane-became-not-ready");
        return { ok: true, submitKey: "Enter", ambiguous: false, queued: true };
      }
      // Wake previously retained inbound work without delaying this receipt or
      // requiring another idle Stop. Durable delivery still owns replay safety.
      if (result.ok) void this.drainPMQueue();
      return { ...result, submitKey: "Enter" };
    }
    if (!this.db) {
      const submitKey = decidePMSubmitKey(this.pmBusy, this.pmRuntime);
      if (durableCadence) return { ok: false, submitKey, ambiguous: false };
      const result = await this.injectDirectWithSubmitKey(message, submitKey);
      return { ...result, submitKey };
    }
    const parsed = parseRelayMessage(message);
    const eventType = eventTypeOverride ?? parsed?.eventType ?? `freeform-${simpleHash(message)}`;
    const existing = this.db.peekPendingPMEvents().find(
      (row) => row.event_type === eventType && row.payload === withMopSlotHeader(message, parsed),
    );
    if (existing) {
      const state = this.db.getPMQueueDeliveryState(
        existing.slot,
        existing.event_type,
        existing.payload ?? "",
        existing.enqueued_at,
      );
      const submitKey = decidePMSubmitKey(this.pmBusy, this.pmRuntime);
      if (state === "started" || state === "ambiguous") {
        return { ok: false, submitKey, ambiguous: true };
      }
      if (state === "delivered") {
        this.db.deletePendingPMEvent(existing.slot, existing.event_type, existing.enqueued_at);
        return { ok: true, submitKey, ambiguous: false };
      }
      // Re-enqueueing would replace enqueued_at and sever attempt authority.
      return this.deliverPMRow(existing);
    }
    const row = this.enqueuePMMessage(message, eventTypeOverride, "submitToPM");
    return this.deliverPMRow(row);
  }

  /**
   * The ONLY writer for a PM self-clear. It is serialized with every other PM
   * pane write, targets the verified immutable pane id (never the mutable
   * PM address), never enqueues for a later turn, and rechecks the caller's
   * authority (request id, turn generation, session, idle), pane identity and
   * a readable composer immediately before the paste AND the single Enter.
   * Any refusal makes no new effect; `zeroEffect` is true only when proven.
   */
  writePMClearFenced(fence: {
    paneId: string;
    check: () => PMClearFenceRefusal | null;
  }): Promise<PMClearWriteResult> {
    const run = (): Promise<PMClearWriteResult> => withSlotSendLock(0, () => this.writePMClearFencedNow({
      ...fence,
      // Respawn retains its existing ownership between /exit and launch, when
      // the pane and hook session may still look unchanged. Never splice a
      // clear into that replacement window, including after waiting for the lock.
      check: () => fence.check() ?? (this.pmMaintenanceGate?.isRespawnInProgress?.()
        ? { reason: "pm_respawn_in_progress", busy: true } : null),
    }));
    const result = this.directInjectChain.then(run, run);
    this.directInjectChain = result.then(() => undefined, () => undefined);
    return result;
  }

  /** Read-only proof pinned to the current PM pane and native runtime. */
  async isPMComposerQueueEmpty(): Promise<boolean> {
    try {
      const pane = await this.runShell(
        `tmux display-message -t ${this.pmPaneAddress} -p '#{pane_id}|#{pane_current_command}'`,
        { timeout: 5_000 },
      );
      const [id, command] = pane.stdout.trim().split("|", 2);
      if (!/^%\d+$/.test(id) || !(await this.isPMCommandReady(command, id))) return false;
      return (await this.pmClearPaneRefusal(id, "")) === null;
    } catch { return false; }
  }

  private async pmClearPaneRefusal(paneId: string, expectComposer: string, allowQueuedClear = false): Promise<PMClearFenceRefusal | null> {
    try {
      const resolved = await this.runShell(
        `tmux display-message -t ${this.pmPaneAddress} -p '#{pane_id}|#{pane_current_command}'`,
        { timeout: 5_000 },
      );
      const [currentPaneId, command] = resolved.stdout.trim().split("|", 2);
      if (currentPaneId !== paneId) return { reason: "pm_pane_identity_drift", busy: false };
      if (!(await this.isPMCommandReady(command, paneId))) return { reason: "pm_pane_not_ready", busy: false };
      const snapshot = await this.runShell(`tmux capture-pane -p -t ${paneId}`, { timeout: 5_000 });
      // Rajiv 1793587783.511949: submit a pending clear at Stop even with CLI backlog.
      // Queue survival is a CLI assumption, not proven here; prompt writes retain the guard.
      if (!allowQueuedClear && QUEUED_HINT.test(snapshot.stdout)) return { reason: "pm_cli_queue_not_empty", busy: true };
      const composer = composerText(snapshot.stdout);
      if (composer === null) return { reason: "pm_composer_unreadable", busy: true };
      if (composer !== expectComposer) {
        return { reason: expectComposer === "" ? "pm_composer_not_empty" : "pm_composer_drift", busy: false };
      }
      return null;
    } catch {
      return { reason: "pm_pane_unobservable", busy: true };
    }
  }

  /**
   * Fenced PM prompt write for MoP-originated re-deliveries (pmCadence.ts).
   * Same serialized, pane-id-pinned, composer-checked writer as the PM
   * self-clear: the caller's check (idle proven + same PM session) is
   * re-evaluated before the paste and before the single Enter, and Enter is
   * refused unless the composer holds exactly our text.
   */
  async writePMPromptFenced(text: string, check: () => PMClearFenceRefusal | null): Promise<PMClearWriteResult> {
    let paneId: string;
    try {
      const resolved = await this.runShell(`tmux display-message -t ${this.pmPaneAddress} -p '#{pane_id}'`, { timeout: 5_000 });
      paneId = resolved.stdout.trim();
    } catch {
      return { ok: false, zeroEffect: true, busy: false, reason: "pm_pane_unobservable" };
    }
    if (!paneId) return { ok: false, zeroEffect: true, busy: false, reason: "pm_pane_unobservable" };
    const guardedCheck = (): PMClearFenceRefusal | null => this.pmMaintenanceGate?.isHeld()
      ? { reason: "pm_busy", busy: true } : check();
    const run = (): Promise<PMClearWriteResult> => this.writePMClearFencedNow({ paneId, check: guardedCheck }, text, "pm_prompt");
    const result = this.directInjectChain.then(run, run);
    this.directInjectChain = result.then(() => undefined, () => undefined);
    return result;
  }

  private async writePMClearFencedNow(fence: {
    paneId: string;
    check: () => PMClearFenceRefusal | null;
  }, text: string = PM_CLEAR_COMMAND, label: string = "pm_clear"): Promise<PMClearWriteResult> {
    const refuse = (refusal: PMClearFenceRefusal, zeroEffect: boolean, stage: string): PMClearWriteResult => {
      this.db?.logEvent(0, `${label}_write_refused`, null, null, {
        stage,
        reason: refusal.reason,
        zero_effect: zeroEffect,
        pane_id: fence.paneId,
      });
      return { ok: false, zeroEffect, busy: zeroEffect && refusal.busy, reason: refusal.reason };
    };
    const prePaste = fence.check() ?? (await this.pmClearPaneRefusal(fence.paneId, "", label === "pm_clear" && text === PM_CLEAR_COMMAND)) ?? fence.check();
    if (prePaste) return refuse(prePaste, true, "pre_paste");

    const seq = ++this.directInjectSeq;
    const tmpFile = `/tmp/mop-${label}-${Date.now()}-${process.pid}-${seq}.txt`;
    const bufName = `mop-${label}-${process.pid}-${seq}`;
    let pasteAttempted = false;
    try {
      await fs.writeFile(tmpFile, text);
      await this.runShell(`tmux load-buffer -b ${bufName} ${shellEscape(tmpFile)}`, { timeout: 10_000 });
      // Revalidate authority after the file/buffer awaits, immediately before the paste.
      const atPaste = fence.check() ?? (await this.pmClearPaneRefusal(fence.paneId, "", label === "pm_clear" && text === PM_CLEAR_COMMAND)) ?? fence.check();
      if (atPaste) {
        await this.runShell(`tmux delete-buffer -b ${bufName}`, { timeout: 5_000 }).catch(() => undefined);
        return refuse(atPaste, true, "at_paste");
      }
      pasteAttempted = true;
      await this.runShell(`tmux paste-buffer -p -b ${bufName} -t ${fence.paneId} -d`, { timeout: 10_000 });
      await sleep(PM_INJECT_ENTER_DELAY_MS);

      const preEnter = fence.check() ?? (await this.pmClearPaneRefusal(fence.paneId, text, label === "pm_clear" && text === PM_CLEAR_COMMAND)) ?? fence.check();
      if (preEnter) {
        // Remove only our own paste, and only while the composer holds exactly it.
        // A paste happened: the outcome is never zero-effect (never rearmed).
        const owned = (await this.pmClearPaneRefusal(fence.paneId, text, label === "pm_clear" && text === PM_CLEAR_COMMAND)) === null;
        if (owned) {
          await this.runShell(`tmux send-keys -t ${fence.paneId} C-u`, { timeout: 10_000 });
        }
        return refuse(preEnter, false, "pre_enter");
      }
      await this.runShell(`tmux send-keys -t ${fence.paneId} Enter`, { timeout: 10_000 });
      this.pmBusy = null;
      this.db?.logEvent(0, `${label}_write_submitted`, null, null, { pane_id: fence.paneId });
      return { ok: true, zeroEffect: false, busy: false };
    } catch (err) {
      return refuse({ reason: `${label}_write_error:${String(err).slice(0, 120)}`, busy: false }, !pasteAttempted, "write");
    } finally {
      await fs.unlink(tmpFile).catch(() => undefined);
    }
  }

  private injectDirectWithSubmitKey(message: string, submitKey: PMSubmitKey, guard?: PasteGuard): Promise<DirectSubmitResult> {
    ++this.pmInboundGeneration;
    ++this.pmInboundPending;
    const run = (): Promise<DirectSubmitResult> => this.injectDirectNow(message, submitKey, guard);
    const result = this.directInjectChain.then(run, run).finally(() => { --this.pmInboundPending; });
    this.directInjectChain = result.then(() => undefined, () => undefined);
    return result;
  }

  private async deliverPMRow(row: PMQueueRow): Promise<PMSubmitResult> {
    const run = (): Promise<PMSubmitResult> => this.deliverPMRowNow(row);
    const result = this.pmDrainChain.then(run, run);
    this.pmDrainChain = result.then(() => 0, () => 0);
    return result;
  }

  private pmQueuedEffectCheck: PMQueuedEffectCheck | null = null;

  /** One owner-supplied check run immediately before a queued row's paste. */
  setPMQueuedEffectCheck(check: PMQueuedEffectCheck | null): void {
    this.pmQueuedEffectCheck = check;
  }

  private async deliverPMRowNow(row: PMQueueRow): Promise<PMSubmitResult> {
    this.pmMaintenanceGate?.deferCLIInput();
    if (!this.db) {
      const submitKey = decidePMSubmitKey(this.pmBusy, this.pmRuntime);
      const result = await this.injectDirectWithSubmitKey(row.payload ?? "", submitKey);
      return { ...result, submitKey };
    }
    const message = row.payload ?? `/${row.event_type} ${row.slot}`;
    const state = this.db.getPMQueueDeliveryState(
      row.slot,
      row.event_type,
      message,
      row.enqueued_at,
    );
    const submitKey = decidePMSubmitKey(this.pmBusy, this.pmRuntime);
    if (state === "delivered" || state === "started" || state === "ambiguous") {
      return { ok: state === "delivered", submitKey, ambiguous: state !== "delivered" };
    }
    let verdict: PMQueuedEffectVerdict = "deliver";
    try {
      verdict = this.pmQueuedEffectCheck?.(row) ?? "deliver";
    } catch {
      verdict = "retain";
    }
    if (verdict === "retain") {
      this.db.logEvent(row.slot, "pm_queue_delivery_deferred", null, null, {
        event_type: row.event_type,
        enqueued_at: row.enqueued_at,
        reason: "effect-revalidation-uncertain-row-retained",
      });
      return { ok: false, submitKey, ambiguous: false };
    }
    if (verdict !== "deliver") {
      this.db.deletePendingPMEvent(row.slot, row.event_type, row.enqueued_at);
      this.db.logEvent(row.slot, "pm_queue_dropped", null, null, {
        event_type: row.event_type,
        enqueued_at: row.enqueued_at,
        reason: verdict.drop,
      });
      return { ok: false, submitKey, ambiguous: false };
    }
    this.db.logEvent(row.slot, "pm_queue_delivery_started", null, null, {
      event_type: row.event_type,
      message,
      enqueued_at: row.enqueued_at,
      submit: submitKey,
    });
    const result = await this.injectDirectWithSubmitKey(message, submitKey);
    if (!result.ok) {
      this.db.logEvent(row.slot, "pm_queue_delivery_deferred", null, null, {
        event_type: row.event_type,
        message,
        enqueued_at: row.enqueued_at,
        submit: submitKey,
        ambiguous: result.ambiguous,
        reason: result.ambiguous ? "submit-outcome-ambiguous" : "submit-failed-row-retained",
      });
      return { ok: false, submitKey, ambiguous: result.ambiguous };
    }
    this.db.logEvent(row.slot, "pm_queue_delivered", null, null, {
      event_type: row.event_type,
      message,
      enqueued_at: row.enqueued_at,
      submit: submitKey,
    });
    this.db.deletePendingPMEvent(row.slot, row.event_type, row.enqueued_at);
    this.db.logEvent(row.slot, "pm_queue_drained", null, null, {
      event_type: row.event_type,
      message: message.slice(0, 200),
      enqueued_at: row.enqueued_at,
      injected: true,
      submit: submitKey,
    });
    if (submitKey === "Enter") {
      // Enter starts a PM turn; subsequent rows in this drain fail closed to
      // C-q until a fresh OMP stop observation proves idle again.
      this.pmBusy = null;
    }
    return { ok: true, submitKey, ambiguous: false };
  }

  private async injectDirectNow(message: string, submitKey: PMSubmitKey, guard?: PasteGuard): Promise<DirectSubmitResult> {
    let pasted = false;
    // A guarded send targets the pinned immutable pane id, never the mutable PM address.
    const target = guard?.paneId ?? this.pmPaneAddress;
    const effectiveSubmitKey = submitKey;
    try {
      if (this.db?.getConfig("pm_exit_cleanup_pending")) {
        // This is a MoP paste-safety boundary, not an input/tool admission gate.
        const observed = await this.observeSlotPane(0);
        if (!observed || composerText(observed.text) !== "") return { ok: false, ambiguous: false, notReady: true };
        this.db.setConfig("pm_exit_cleanup_pending", "");
      }
      if (!(await this.isPMPaneReady())) return { ok: false, ambiguous: false, notReady: true };
      const firstLine = message.split("\n", 1)[0];
      console.log(`[relay-debug] injectToPM → ${firstLine}${message.includes("\n") ? " (+multiline payload)" : ""} (submit=${effectiveSubmitKey})`);
      // Every PM payload goes through load-buffer + paste-buffer, single-line
      // included. `tmux send-keys <text>` streams the text as keystrokes in
      // ~1KB pty writes; Claude Code's composer dropped the leading chunk(s)
      // of long single-line bodies (slot 5 #8406 report 2026-09-28: 1507B
      // injected, PM received only the last 483 chars). Buffer paste is the
      // same path sendToSlot uses and never loses the head.
      {
        const seq = ++this.directInjectSeq;
        const tmpFile = `/tmp/mop-pm-inject-${Date.now()}-${process.pid}-${seq}.txt`;
        const bufName = `mop-pm-inject-${process.pid}-${seq}`;
        await fs.writeFile(tmpFile, message);
        try {
          await this.runShell(`tmux load-buffer -b ${bufName} ${shellEscape(tmpFile)}`, { timeout: 10_000 });
          if (guard) {
            // Inside the serialized writer, immediately before the paste: recheck the pin and
            // take this paste's own transcript cursor. A refusal here wrote nothing.
            let verdict: { ok: true } | { ok: false; reason: string };
            try {
              verdict = await guard.beforePaste(message);
            } catch (guardError) {
              console.error("[relay] PasteGuard threw; refusing before paste:", guardError);
              verdict = { ok: false, reason: "guard_error" };
            }
            if (!verdict.ok) {
              await this.runShell(`tmux delete-buffer -b ${bufName}`, { timeout: 5_000 }).catch(() => undefined);
              return { ok: false, ambiguous: false, refused: verdict.reason };
            }
          }
          // `-p` wraps this as a bracketed paste. Claude Code 2.1.282 keeps a
          // plain multiline paste in its composer after Enter; bracketed paste
          // lets the same single Enter submit the complete message.
          // Once invoked, tmux may have accepted the paste even if its ACK is lost.
          // Conservatively retain an ambiguous attempt instead of replaying it.
          pasted = true;
          await this.runShell(`tmux paste-buffer -p -b ${bufName} -t ${target} -d`, { timeout: 10_000 });
          await sleep(PM_INJECT_ENTER_DELAY_MS);
          if (guard) {
            // The dwell above is an await: the recipient may have been replaced since the paste.
            // Recheck the same pin immediately before Enter, still inside the serialized writer.
            // A paste may already have landed, so on any refusal NO further key is sent and the
            // outcome stays ambiguous (UNKNOWN, never resent).
            let verdict: { ok: true } | { ok: false; reason: string };
            try {
              verdict = await guard.beforeSubmit();
            } catch (guardError) {
              console.error("[relay] PasteGuard threw before Enter; withholding Enter:", guardError);
              verdict = { ok: false, reason: "guard_error" };
            }
            if (!verdict.ok) {
              this.db?.logEvent(0, "pm_submit_withheld", null, null, { reason: verdict.reason, pane_id: target, pasted: true });
              return { ok: false, ambiguous: true, withheld: verdict.reason };
            }
          }
          await this.runShell(`tmux send-keys -t ${target} ${effectiveSubmitKey}`, { timeout: 10_000 });
          await sleep(500);
        } finally {
          await fs.unlink(tmpFile).catch(() => undefined);
        }
      }
      console.log(`[relay-debug] injectToPM success → ${firstLine} (submit=${effectiveSubmitKey})`);
      return { ok: true, ambiguous: false };
    } catch (err) {
      console.error(`[relay] Failed to inject into PM pane:`, err);
      return { ok: false, ambiguous: pasted };
    }
  }

  private async isPMPaneReady(): Promise<boolean> {
    if (this.pmPaneReadyOverride) return this.pmPaneReadyOverride();
    try {
      const result = await this.runShell(
        `tmux display-message -t ${this.pmPaneAddress} -p '#{pane_current_command}'`,
        { timeout: 5_000 },
      );
      return await this.isPMCommandReady(result.stdout.trim(), this.pmPaneAddress);
    } catch {
      return false;
    }
  }

  /** Share executable proof with the pinned clear writer, not just inbound delivery. */
  private async isPMCommandReady(command: string, paneTarget: string): Promise<boolean> {
    try {
      if (command === (this.pmRuntime === "omp" ? "omp" : "claude")) return true;
      // Native Claude can expose its version as the tmux foreground title.
      // A version alone is not identity: verify the pane process executable.
      if (this.pmRuntime !== "claude" || !/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(command)) return false;
      const pane = await this.runShell(
        `tmux display-message -t ${paneTarget} -p '#{pane_pid}'`,
        { timeout: 5_000 },
      );
      const pid = pane.stdout.trim();
      if (!/^\d+$/.test(pid)) return false;
      const process = await this.runShell(`ps -p ${pid} -o comm=`, { timeout: 5_000 });
      return process.stdout.trim().split("/").pop() === "claude";
    } catch {
      return false;
    }
  }

  /**
   * Notify PM that a slot went idle.
   * Sends: # comment line, then /slot-idle N slash command.
   */
  notifySlotIdle(slot: SlotState, direct = false): void {
    // Inject MoP message prefix (replaces former slash command `/slot-idle N`).
    // Rajiv directive 2026-05-22 22:23 IST thread `1779468118.901709`:
    // "inject is a string instead of a command." PM-side
    // pm-context-injector.sh recognizes the prefix and emits
    // [MoP_SLOT_NOTIFICATION] system-reminder hinting Skill(slot-idle).
    // Busy-queue (slot, event_type) coalesce key unchanged — parseRelayMessage
    // recognizes both shapes.
    const message = `MoP: slot ${slot.slot} idle`;
    if (direct) {
      this.injectToPMDirect(message);
    } else {
      this.injectToPM(message);
    }
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
  sendToSlot(slotNum: number, command: string, _force = false, raw = false, exitEpisode?: string, stopSession?: string, stopOwnerPin?: ExitOwnerPin): boolean {
    if (command === "/exit" && exitEpisode) {
      const existing = this.pendingExitDispatch.get(slotNum);
      if (existing?.episode === exitEpisode) {
        // Checkout sync yields, so an older Stop can return after a newer one
        // already owns this episode. Keep the newest entry-captured Stop id.
        const ownerPin = stopOwnerPin ?? this.captureExitOwner(slotNum, stopSession);
        if (!ownerPin) {
          this.db?.logEvent(slotNum, "guarded_delivery", null, null, { outcome: "refused_pre_effect", reason: "exit_owner_unbound", command, episode: exitEpisode });
          return false;
        }
        const accepted = this.laterExitStop(existing.acceptedStop, existing.successor?.stop);
        if (!this.isNewerExitStop(ownerPin, accepted)) {
          this.db?.logEvent(slotNum, "exit_stop_out_of_order", null, null, { outcome: "refused_pre_effect", reason: "exit_stop_out_of_order", command, episode: exitEpisode, stop: ownerPin.stop, accepted });
          return false;
        }
        existing.successor = ownerPin;
        existing.acceptedStop = this.laterExitStop(existing.acceptedStop, ownerPin.stop);
        return true;
      }
      // Capture at consumption, before the timer OR pane verification can yield.
      const ownerPin = stopOwnerPin ?? this.captureExitOwner(slotNum, stopSession);
      if (!ownerPin) {
        this.db?.logEvent(slotNum, "guarded_delivery", null, null, { outcome: "refused_pre_effect", reason: "exit_owner_unbound", command, episode: exitEpisode });
        return false;
      }
      // The server closes this Stop's matching turn after process() returns.
      // Continue the SAME consumed send only after that response can complete.
      const pending: PendingExitDispatch = {
        episode: exitEpisode,
        work: Promise.resolve(),
        acceptedStop: this.exitStopRank(ownerPin.stop),
      };
      const work = sleep(0).then(() => this.settleExitDispatch(slotNum, command, exitEpisode, ownerPin, pending, _force, raw));
      pending.work = work;
      this.pendingExitDispatch.set(slotNum, pending);
      void work.catch(() => undefined).finally(() => {
        if (this.pendingExitDispatch.get(slotNum) === pending) this.pendingExitDispatch.delete(slotNum);
      });
      return true;
    }
    void this.sendToSlotAsync(slotNum, command, _force, raw);
    return true;
  }

  /**
   * Gated slots 1-4: null when this pin is not a classified Stop (legacy quiet
   * check). false when the Stop row has no mop_stop_allowed mark (a raw Stop,
   * including one whose block decision lands after the old 500ms quiet window).
   * true when the slot hook marked the Stop allowed.
   */
  private gatedStopAllowed(slotNum: number, pin: ExitOwnerPin): boolean | null {
    if (!isGatedDevSlot(slotNum) || !this.db || !pin.stop) return null;
    const event = this.db.getEvents(slotNum, 40, "Stop").find((item) => item.id === pin.stop);
    if (!event) return null;
    try {
      const payload = JSON.parse(event.payload) as { mop_stop_allowed?: unknown };
      return payload.mop_stop_allowed === 1 || payload.mop_stop_allowed === true;
    } catch {
      return false;
    }
  }

  /** Stop event ids are monotonic. Anything else is not an order. */
  private exitStopRank(stop: number | null | undefined): number | null {
    return typeof stop === "number" && Number.isSafeInteger(stop) ? stop : null;
  }

  /** Newest of two entry-captured Stop ids. A missing id does not outrank a real one. */
  private laterExitStop(left: number | null | undefined, right: number | null | undefined): number | null {
    const a = this.exitStopRank(left);
    const b = this.exitStopRank(right);
    if (a === null) return b;
    if (b === null) return a;
    return a > b ? a : b;
  }

  /** True only when candidate is a strictly newer owner-bound Stop than current. */
  private isNewerExitStop(candidate: ExitOwnerPin, current: number | null | undefined): boolean {
    const next = this.exitStopRank(candidate.stop);
    const rank = this.exitStopRank(current);
    if (next === null) return false;
    return rank === null || next > rank;
  }

  /** A newer Stop can arrive while this op is suspended. TS can't see that write. */
  private takeExitSuccessor(pending: { successor?: ExitOwnerPin }): ExitOwnerPin | undefined {
    const next = pending.successor;
    pending.successor = undefined;
    return next;
  }

  private async settleExitDispatch(
    slotNum: number,
    command: string,
    exitEpisode: string,
    initialPin: ExitOwnerPin,
    pending: PendingExitDispatch,
    _force: boolean,
    raw: boolean,
  ): Promise<void> {
    let pin = initialPin;
    for (let hop = 0; hop < 4; hop++) {
      const allowed = this.gatedStopAllowed(slotNum, pin);
      let continued: string | null;
      if (allowed === false) {
        // Not the allowed Stop. Do not wait out quiet time and then send.
        continued = "stop_not_allowed";
      } else if (allowed === true) {
        // The mark is the disposition. Owner fences still run at send time.
        continued = this.db?.getSlot(slotNum)?.active_turn_state === "active" ? "turn_active" : null;
        if (!continued) pin = this.exitPinAfterDuplicateStops(slotNum, pin);
      } else {
        continued = await this.exitTurnContinued(slotNum, pin);
        if (!continued) pin = this.exitPinAfterDuplicateStops(slotNum, pin);
      }
      const newer = this.takeExitSuccessor(pending);
      if (newer && this.isNewerExitStop(newer, pin.stop)) {
        if (continued) {
          this.db?.logEvent(slotNum, "exit_dispatch_skipped", null, null, { episode: exitEpisode, reason: continued, superseded: true });
        }
        pin = newer;
        pending.acceptedStop = this.laterExitStop(pending.acceptedStop, newer.stop);
        continue;
      }
      if (newer && this.exitStopRank(newer.stop) !== this.exitStopRank(pin.stop)) {
        this.db?.logEvent(slotNum, "exit_stop_out_of_order", null, null, { outcome: "refused_pre_effect", reason: "exit_stop_out_of_order", command, episode: exitEpisode, stop: newer.stop, accepted: pin.stop });
      }
      if (continued) {
        this.db?.logEvent(slotNum, "exit_dispatch_skipped", null, null, { episode: exitEpisode, reason: continued });
        return;
      }
      const before = this.db?.getEvents(slotNum, 1, "guarded_delivery")[0]?.id;
      let delivered = false;
      let failure = "dispatch_refused_before_attempt";
      try { delivered = await this.sendToSlotAsync(slotNum, command, _force, raw, exitEpisode, pin); }
      catch { failure = "dispatch_error_unknown"; }
      const receipt = this.db?.getEvents(slotNum, 1, "guarded_delivery")[0];
      let detail = failure;
      if (receipt && receipt.id !== before) {
        try { const data = JSON.parse(receipt.payload); detail = `${data.outcome}: ${data.reason ?? "submitted"}`; } catch { /* Report fallback. */ }
      } else this.db?.logEvent(slotNum, "exit_dispatch_refused", null, null, { episode: exitEpisode, reason: failure });
      const late = this.takeExitSuccessor(pending);
      if (late && !delivered && this.isNewerExitStop(late, pin.stop)) {
        pin = late;
        pending.acceptedStop = this.laterExitStop(pending.acceptedStop, late.stop);
        continue;
      }
      if (late && this.exitStopRank(late.stop) !== this.exitStopRank(pin.stop)) {
        this.db?.logEvent(slotNum, "exit_stop_out_of_order", null, null, { outcome: "refused_pre_effect", reason: "exit_stop_out_of_order", command, episode: exitEpisode, stop: late.stop, accepted: pin.stop });
      }
      const outcome = delivered ? "delivered" : detail;
      const noticeKey = `${slotNum}\0${exitEpisode}\0${outcome}`;
      if (this.exitNoticeSent.has(noticeKey)) {
        this.db?.logEvent(slotNum, "exit_pm_notice_suppressed", null, null, { episode: exitEpisode, outcome });
      } else {
        this.exitNoticeSent.add(noticeKey);
        this.injectToPM(`# slot ${slotNum} /exit ${delivered ? "delivered; cycle unverified" : detail}`);
      }
      return;
    }
  }

  private async exitTurnContinued(slotNum: number, pin: ExitOwnerPin): Promise<string | null> {
    const deadline = Date.now() + Math.max(0, this.exitSettleMs);
    while (Date.now() < deadline) await sleep(Math.min(50, deadline - Date.now()));
    return this.exitContinuationReason(slotNum, pin);
  }

  /** A Stop that did not end the turn. Duplicate Stop rows alone do not count. */
  private exitContinuationReason(slotNum: number, pin: ExitOwnerPin): string | null {
    if (!this.db) return null;
    const slot = this.db.getSlot(slotNum);
    if (!slot) return null;
    if (slot.active_turn_state === "active") return "turn_active";
    const floor = pin.stop ?? 0;
    const newer = this.db.getEvents(slotNum, 40).filter(event => event.id > floor);
    const continued = newer.find(event => event.event_type !== "Stop"
      && event.event_type !== "exit_pending_triggered"
      && !event.event_type.startsWith("exit_")
      && event.event_type !== "guarded_delivery");
    return continued ? `continued:${continued.event_type}` : null;
  }

  private exitPinAfterDuplicateStops(slotNum: number, pin: ExitOwnerPin): ExitOwnerPin {
    if (!this.db || this.exitContinuationReason(slotNum, pin)) return pin;
    const latest = this.db.getEvents(slotNum, 1, "Stop")[0];
    if (!latest || latest.id === pin.stop || !pin.stopSession) return pin;
    try {
      const session = JSON.parse(latest.payload).session_id;
      if (session !== pin.stopSession) return pin;
    } catch { return pin; }
    return { ...pin, stop: latest.id };
  }

  private exitAssignment(slot: SlotState): string {
    return JSON.stringify([slot.assignment_epoch, slot.repository_id, slot.issue,
      slot.branch_ref, slot.occupied, slot.dnd]);
  }

  captureExitOwner(slotNum: number, stopSession?: string): ExitOwnerPin | null {
    if (!this.db) return null;
    if (slotNum === 0) {
      if (!stopSession || this.db.latestSessionId(0) !== stopSession) return null;
      return { assignment: "PM", boot: this.db.getEvents(0, 1, "SessionStart")[0]?.id ?? null,
        input: this.db.getEvents(0, 1, "UserPromptSubmit")[0]?.id ?? null,
        stop: this.db.getEvents(0, 1, "Stop")[0]?.id ?? null,
        work: null, turn: null, turnStarted: null, turnState: "inactive", stopSession,
        inboundGeneration: this.pmInboundGeneration };
    }
    const slot = this.db.getSlot(slotNum);
    if (!slot) return null;
    const boot = this.db.getEvents(slotNum, 1, "SessionStart")[0];
    if (stopSession && slot.active_turn_id && slot.active_turn_id !== stopSession) return null;
    if (stopSession && boot) {
      try {
        const session = JSON.parse(boot.payload).session_id;
        if (session && session !== stopSession) return null;
      } catch { return null; }
    }
    return {
      assignment: this.exitAssignment(slot), boot: boot?.id ?? null,
      input: this.db.getEvents(slotNum, 1, "UserPromptSubmit")[0]?.id ?? null,
      work: slot.last_meaningful_work_at, turn: slot.active_turn_id,
      turnStarted: slot.active_turn_started_at, turnState: slot.active_turn_state,
      stopSession: stopSession ?? null,
      stop: this.db.getEvents(slotNum, 1, "Stop")[0]?.id ?? null,
    };
  }

  private exitOwnerRefusal(slotNum: number, pin: ExitOwnerPin, physicalPaste = false): string | null {
    if (!this.db) return "exit_owner_unbound";
    if ((this.db.getEvents(slotNum, 1, "Stop")[0]?.id ?? null) !== pin.stop) return "stop_changed";
    if (!physicalPaste && slotNum === 0 && pin.inboundGeneration !== this.pmInboundGeneration) return "pm_inbound_changed";
    if (slotNum === 0) return (this.db.getEvents(0, 1, "SessionStart")[0]?.id ?? null) === pin.boot
      && (this.db.getEvents(0, 1, "UserPromptSubmit")[0]?.id ?? null) === pin.input ? null : "pm_owner_changed";
    const slot = this.db.getSlot(slotNum);
    if (!slot || this.exitAssignment(slot) !== pin.assignment
        || (this.db.getEvents(slotNum, 1, "SessionStart")[0]?.id ?? null) !== pin.boot
        || (this.db.getEvents(slotNum, 1, "UserPromptSubmit")[0]?.id ?? null) !== pin.input
        || slot.last_meaningful_work_at !== pin.work) return "slot_owner_changed";
    const sameTurn = slot.active_turn_id === pin.turn && slot.active_turn_started_at === pin.turnStarted
      && slot.active_turn_state === pin.turnState;
    // The server may close ONLY this Stop's matching turn. No successor is adopted.
    const matchingClosure = pin.stopSession !== null && pin.turn === pin.stopSession
      && slot.active_turn_id === null && slot.active_turn_started_at === null && slot.active_turn_state === "inactive";
    return sameTurn || matchingClosure ? null : "slot_owner_changed";
  }

  /** Keep only this bounded exit continuation's episode alive until it settles. */
  hasPendingExitConfirmation(episode: string | null): boolean {
    if (!episode) return false;
    if ([...this.pendingExitDispatch.values()].some(pending => pending.episode === episode)) return true;
    if (!this.db) return false;
    return [0, ...this.db.getAllSlots().map(slot => slot.slot)].some(slot => {
      try {
        const state = JSON.parse(this.db!.getConfig(`exit_dialog_confirmation_${slot}`) || "null");
        return state?.episode === episode && !state.done && state.deadline > Date.now();
      } catch { return false; }
    });
  }

  /** Resolve the owning numbered pane's current Git worktree root. */
  async getSlotCheckoutPath(slotNum: number): Promise<string | null> {
    if (!isValidDevSlot(slotNum)) return null;
    try {
      const identity = await verifyPaneIdentity(slotNum, this.runShell);
      if (!identity.ok) return null;
      const panePath = identity.snapshot.currentPath;
      const { stdout: checkoutOutput } = await this.runShell(
        `git -C ${shellEscape(panePath)} rev-parse --show-toplevel`,
        { timeout: 3_000 },
      );
      const checkoutPath = checkoutOutput.trim();
      return checkoutPath.startsWith("/") ? checkoutPath : null;
    } catch {
      return null;
    }
  }

  async sendToSlotAsync(slotNum: number, command: string, _force = false, raw = false, exitEpisode?: string, exitOwnerPin?: ExitOwnerPin): Promise<boolean> {
    void _force; // v3: every send is unconditional
    const pinnedRefusal = exitEpisode ? (exitOwnerPin ? this.exitOwnerRefusal(slotNum, exitOwnerPin) : "exit_owner_unbound") : null;
    if (pinnedRefusal) {
      this.db?.logEvent(slotNum, "guarded_delivery", null, null, { outcome: "refused_pre_effect", reason: pinnedRefusal, command, episode: exitEpisode });
      return false;
    }
    if (slotNum === 0 && !raw && !exitEpisode) {
      // Text delivery to PM must use the observation-bound submit key. Raw
      // key sequences intentionally remain the explicit low-level escape
      // hatch for callers such as clear/compact recovery.
      return (await this.submitToPM(command)).ok;
    }
    const identity = await verifyPaneIdentity(slotNum, this.runShell);
    if (!identity.ok) {
      console.warn(`[relay] refusing pane delivery for slot ${slotNum}: ${identity.detail}`);
      if (this.db) {
        this.db.logEvent(slotNum, "send_rejected_pane_identity", null, null, {
          reason: identity.reason,
          detail: identity.detail,
          address: paneAddress(slotNum),
        });
      }
      return false;
    }
    // Pin every retry to the immutable pane id captured by the identity probe.
    // Never re-resolve the numeric address after verification: tmux can reindex
    // it if a pane is inserted or destroyed.
    const paneTarget = identity.snapshot.paneId;
    if (!raw && isSlashCommand(command)) {
      const owner = () => {
        const row = this.db?.getSlot(slotNum);
        const event = this.db?.getEvents(slotNum, 100).find(e => e.hook_type);
        return JSON.stringify([row && [row.assignment_epoch, row.issue, row.repository_id,
          row.branch_ref, row.occupied, row.active_turn_id, row.active_turn_state,
          row.last_meaningful_work_at], event?.payload]);
      };
      const original = owner();
      const fence = () => exitEpisode && (!this.db?.getExitPending() || this.db.getExitStatus().episode !== exitEpisode) ? "exit_episode_changed"
        : exitOwnerPin && this.exitOwnerRefusal(slotNum, exitOwnerPin) ? "slot_owner_changed"
        : owner() !== original ? "slot_owner_changed"
        : this.db?.getSlot(slotNum)?.active_turn_state === "active" ? "slot_busy" : null;
      return (await this.deliverGuardedToSlot(slotNum, command, {
        expectedPaneId: paneTarget,
        recheck: async () => fence() ?? (await this.getSlotActivityState(slotNum, paneTarget) !== "idle" ? "pane_not_idle" : fence()),
        finalCheck: fence,
        exitEpisode,
        exitOwnerPin,
      })).outcome === "delivered";
    }
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
        const sent = await withSlotSendLock(slotNum, async () => {
          if (raw) {
            // Raw: tmux interprets key names (Escape, C-c, BTab, etc.).
            // Do NOT pass -l (literal) flag — that would type the name as text.
            await this.runShell(
              `tmux send-keys -t ${paneTarget} ${shellEscape(command)}`,
              { timeout: 5_000 }
            );
            return true;
          }

          // Keep the buffer paste, dwell, one Enter, and composer readback
          // serialized with server.ts sends for this pane.
          const tmpFile = `/tmp/mop-send-${slotNum}-${Date.now()}-${attempt}.txt`;
          const bufName = `mop-send-${slotNum}`;
          await fs.writeFile(tmpFile, command);
          try {
            let prePasteComposer: string | null = null;
            const readComposer = async (): Promise<string | null> => {
              try {
                const before = await this.runShell(`tmux capture-pane -t ${paneTarget} -p`, { timeout: 5_000 });
                return composerText(before.stdout);
              } catch {
                // Unknown composer state must not be followed by Enter.
                return null;
              }
            };
            prePasteComposer = await readComposer();
            const slash = isSlashCommand(command);
            await this.runShell(
              `tmux load-buffer -b ${bufName} ${shellEscape(tmpFile)}`,
              { timeout: 3_000 }
            );
            await this.runShell(
              `tmux paste-buffer -b ${bufName} -t ${paneTarget} -d`,
              { timeout: 3_000 }
            );
            const submit = await submitWithComposerCheck(command, {
              capture: async () => {
                try {
                  return (await this.runShell(`tmux capture-pane -t ${paneTarget} -p`, { timeout: 5_000 })).stdout;
                } catch {
                  return null;
                }
              },
              pressSubmit: async () => {
                await this.runShell(`tmux send-keys -t ${paneTarget} Enter`, { timeout: 3_000 });
              },
              sleep,
              prePasteComposer,
              ...(slash ? this.slashSubmitTiming() : {}),
            });
            if (this.db) {
              this.db.logEvent(slotNum, "send_submit_check", null, null, {
                command: command.slice(0, 200),
                dwell_ms: slash ? this.slashSubmitTiming().dwellMs : INJECT_ENTER_DELAY_MS,
                payload_seen: submit.payloadSeen,
                payload_stable: submit.payloadStable,
                cleared: submit.cleared,
                enter_presses: submit.enterPresses,
              });
            }
            // A missing/partial/unreadable composer or an uncleared input is
            // ambiguous. Do not auto-repaste or press Enter a second time.
            return submit.payloadSeen === true
              && submit.payloadStable === true
              && (submit.cleared === true || submit.queued === true)
              && (submit.enterPresses === 1 || (slash && submit.enterPresses === 2));
          } finally {
            await fs.unlink(tmpFile).catch(() => undefined);
          }
        });
        if (!sent) return false;
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
          await sleep(backoff);
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

  /** Dwell before the separate slash-command Enter (MOP_SLASH_COMMAND_ENTER_DELAY_MS). */
  private slashSubmitTiming(): { dwellMs: number; payloadStableMs: number } {
    const ms = resolveSlashCommandEnterDelayMs();
    return { dwellMs: ms, payloadStableMs: ms };
  }

  /** Clear only our slash composer; every key is a separately fenced effect. */
  private async clearComposerForSlash(
    paneTarget: string, read: () => Promise<string | null>, command: string,
    recheck: () => Promise<string | null>, finalCheck: () => string | null,
    attempted: () => void,
  ): Promise<{ composer: string | null; reason?: string }> {
    for (const key of ["Escape", "C-u"]) {
      if (key === "C-u") await sleep(Math.max(250, resolveSlashCommandEnterDelayMs()));
      const refusal = await recheck();
      if (refusal) return { composer: null, reason: refusal };
      const composer = await read();
      if (!slashComposerClearable(composer, command)) return { composer, reason: "composer_not_owned_by_send" };
      const final = finalCheck();
      if (final) return { composer: null, reason: final };
      attempted();
      try { await this.runShell(`tmux send-keys -t ${paneTarget} ${key}`, { timeout: 3_000 }); }
      catch { return { composer: null, reason: "clear_key_uncertain" }; }
    }
    let composer: string | null = null;
    for (let waited = 0; waited <= 1_000; waited += 250) {
      await sleep(250);
      composer = await read();
      if (composer === "") return { composer };
    }
    return { composer };
  }

  /**
   * Observe a numbered slot through its verified immutable pane id. Detectors
   * that later deliver into the slot must pass the returned paneId to
   * deliverGuardedToSlot so observation and effect target the same pane.
   */
  async observeSlotPane(slotNum: number, lines = GUARDED_CAPTURE_LINES): Promise<{ paneId: string; text: string } | null> {
    const identity = await verifyPaneIdentity(slotNum, this.runShell);
    if (!identity.ok) return null;
    const paneId = identity.snapshot.paneId;
    try {
      const result = await this.runShell(`tmux capture-pane -t ${paneId} -p -S -${lines}`, { timeout: 5_000 });
      return { paneId, text: result.stdout };
    } catch {
      return null;
    }
  }

  /** Pin the actual terminal processes as well as the immutable pane. No input. */
  async exitProcessIdentity(_slotNum: number, paneId: string, deadline = Date.now() + 2_000): Promise<string | null> {
    try {
      if (Date.now() >= deadline) return null;
      const probe = await this.runShell(`tmux display-message -t ${paneId} -p '#{pane_id}|#{pane_pid}|#{pane_tty}|#{pane_current_path}|#{pane_current_command}'`, { timeout: Math.min(1_000, deadline - Date.now()) });
      const [id, pid, tty] = probe.stdout.trim().split("|");
      if (id !== paneId || !/^\d+$/.test(pid) || !/^\/dev\/[\w/.-]+$/.test(tty ?? "") || Date.now() >= deadline) return null;
      const processes = await this.runShell(`ps -t ${shellEscape(tty)} -o pid=,pgid=,stat=,lstart=,comm=`, { timeout: Math.min(1_000, deadline - Date.now()) });
      // Hook/MCP children may finish normally after Stop. Pin the foreground
      // process-group leader, not every transient child sharing its terminal.
      const leader = parseForegroundClaude(processes.stdout);
      return leader ? JSON.stringify([paneId, leader.pid, leader.start]) : null;
    } catch { return null; }
  }

  /** Health observes only; initial and bounded retry dispatch belong to Stop. */
  async observeExitAttempts(): Promise<void> {
    const db = this.db;
    if (!db || !db.getExitPending()) return;
    const status = db.getExitStatus();
    for (const slot of [0, ...db.getAllSlots().map(row => row.slot)]) {
      const attempt = db.getExitAttempt(slot);
      if (!attempt || attempt.phase === "verified") continue;
      const identity = await verifyPaneIdentity(slot, this.runShell);
      if (!identity.ok || identity.snapshot.paneId !== attempt.paneId) continue;
      const process = await this.exitProcessIdentity(slot, attempt.paneId);
      if (!db.getExitPending() || db.getExitStatus().episode !== status.episode
          || db.getExitAttempt(slot)?.submittedAt !== attempt.submittedAt) continue;
      const pin = attempt.ownerPin as ExitOwnerPin | null;
      let delayedReceiptRetention = false;
      try {
        const retained = JSON.parse(db.getConfig(`exit_retained_${slot}`) || "null");
        delayedReceiptRetention = retained?.episode === attempt.episode && retained.reason === "replacement_not_owned_by_restart";
      } catch { /* Other retention remains binding. */ }
      const ownerMatches = !!pin && (!db.isExitSlotRetained(slot) || delayedReceiptRetention)
        && attempt.assignmentEpoch === (db.getSlot(slot)?.assignment_epoch ?? 0)
        && (slot === 0 || (!!db.getSlot(slot) && this.exitAssignment(db.getSlot(slot)!) === pin.assignment))
        && attempt.inputEventId === (db.getEvents(slot, 1, "UserPromptSubmit")[0]?.id ?? null);
      if (!ownerMatches) { db.retainExitSlot(slot, "exit_owner_superseded"); continue; }
      if (process && process !== attempt.originalProcess) {
        const restart = db.getEvents(slot, 1, "exit_replacement_bound")[0];
        let bound = false;
        try {
          const receipt = JSON.parse(restart?.payload ?? "null");
          const boot = db.getEvents(slot, 1, "SessionStart")[0];
          const bootPayload = JSON.parse(boot?.payload ?? "null");
          const replacement = JSON.parse(process);

          bound = receipt?.episode === attempt.episode && receipt?.submittedAt === attempt.submittedAt
            && receipt?.replacementProcess === process && receipt?.assignmentEpoch === attempt.assignmentEpoch
            && Number.isInteger(receipt.launchEventId) && receipt.launchEventId > 0
            && receipt.bootEventId > receipt.launchEventId && !!receipt.bootSession
            && /^\d+:\d+$/.test(receipt.ownedProcess ?? "")
            && boot?.id === receipt.bootEventId && bootPayload?.session_id === receipt.bootSession
            && String(replacement[1]) === receipt.ownedProcess.split(":")[1];
        } catch { /* Unbound manual replacement never earns cycle credit. */ }
        if (!bound) {
          attempt.phase = "unknown";
          attempt.blockedReason = "replacement_not_owned_by_restart";
          db.saveExitAttempt(slot, attempt);
          continue;
        }
        if (delayedReceiptRetention) db.setConfig(`exit_retained_${slot}`, "");
        attempt.phase = "verified";
        attempt.replacementProcess = process;
        db.saveExitAttempt(slot, attempt);
        db.markSlotExitCycled(slot);
        db.logEvent(slot, "exit_cycle_verified", null, null, { ...attempt });
        if (slot > 0) this.injectToPM(`# 🔄 slot ${slot} cycled — replacement Claude process verified in ${attempt.paneId}`);
      } else if (process === attempt.originalProcess && Date.now() - Date.parse(attempt.submittedAt) >= 60_000 && !attempt.reported) {
        const screen = await this.runShell(`tmux capture-pane -t ${attempt.paneId} -p`, { timeout: 3_000 }).catch(() => null);
        if (db.getExitAttempt(slot)?.submittedAt !== attempt.submittedAt || db.getExitStatus().episode !== status.episode) continue;
        // A badge or generic running-agent text never promotes UNKNOWN to retryable.
        attempt.phase = "unknown";
        attempt.blockedReason = screen ? "same_original_process_no_exit_receipt" : "pane_unobservable";
        attempt.reported = true;
        db.saveExitAttempt(slot, attempt);
        db.logEvent(slot, "exit_unknown", null, null, { ...attempt });
      }
    }
  }

  /** Claim one bounded continuation after our /exit Enter; never replay it. */
  private scheduleExitConfirmation(slotNum: number, paneId: string, episode: string, process: string, owner: string, ownerPin?: ExitOwnerPin): void {
    if (!this.db) return;
    const key = `exit_dialog_confirmation_${slotNum}`;
    try {
      const saved = JSON.parse(this.db.getConfig(key) || "null");
      if (saved?.episode === episode) return;
    } catch { return; }
    try {
      this.db.setConfig(key, JSON.stringify({ episode, process, attempts: 0, done: false, deadline: Date.now() + (slotNum === 0 ? 10_000 : 5_000), owner_pin: ownerPin ?? null }));
      // Do not await our own send lock or the dialog from the HTTP Stop handler.
      // The finite continuation acquires the same lock after /exit delivery returns.
      const run = () => withSlotSendLock(slotNum, () => this.confirmExitDialog(slotNum, paneId, episode, process, owner, ownerPin));
      if (slotNum === 0) {
        const result = this.directInjectChain.then(run, run);
        this.directInjectChain = result.then(() => undefined, () => undefined);
      } else void run().catch(() => undefined);
    } catch { /* Never change the original /exit receipt or retry an uncertain claim. */ }
  }

  /** One claimed continuation, after the original /exit/Stop can complete. */
  private async confirmExitDialog(slotNum: number, paneId: string, episode: string, process: string,
    owner: string, ownerPin?: ExitOwnerPin): Promise<void> {
    const db = this.db;
    if (!db) return;
    const key = `exit_dialog_confirmation_${slotNum}`;
    const token = () => JSON.stringify([
      db.getEvents(slotNum, 1, "SessionStart")[0]?.id,
      db.getEvents(slotNum, 1, "UserPromptSubmit")[0]?.id,
      db.getSlot(slotNum)?.assignment_epoch,
      db.getSlot(slotNum)?.active_turn_id,
    ]);
    let state: { episode: string; process: string; attempts: number; done: boolean; deadline: number };
    try {
      const saved = JSON.parse(db.getConfig(key) || "null");
      if (saved?.episode !== episode || saved.process !== process || saved.done || !Number.isFinite(saved.deadline)
          || !Number.isInteger(saved.attempts) || saved.attempts < 0 || saved.attempts > 2) return;
      state = saved;
    } catch { return; } // Unknown retained custody never grants more attempts.
    const deadline = state.deadline;
    const current = () => Date.now() < deadline && db.getExitPending() && db.getExitStatus().episode === episode
      && !(slotNum === 0 && (this.pmInboundPending > 0 || db.peekPendingPMEvents().length > 0))
      && (ownerPin ? this.exitOwnerRefusal(slotNum, ownerPin) === null : token() === owner);
    const save = () => db.setConfig(key, JSON.stringify(state));
    const inspect = async () => {
      if (!current() || await this.exitProcessIdentity(slotNum, paneId, deadline) !== process || !current()) return "changed";
      // Visible screen only: a historical dialog in scrollback is not authority.
      const screen = await this.runShell(`tmux capture-pane -t ${paneId} -p`, { timeout: Math.min(1_000, deadline - Date.now()) });
      if (await this.exitProcessIdentity(slotNum, paneId, deadline) !== process || !current()) return "changed";
      return (slotNum === 0 ? isCompletePMExitMenu(screen.stdout) : isExitConfirmationDialog(screen.stdout)) ? "dialog" : "absent";
    };
    let seen = false;
    let enterPresses = 0;
    try {
      while (state.attempts < (slotNum === 0 ? 1 : 2) && current()) {
        await sleep(350); // Allow the dialog to render after /exit or the prior Enter.
        const screen = await inspect();
        if (screen === "changed" || (screen === "absent" && seen)) break;
        if (screen === "absent") continue; // Initial absence never finishes the render window.
        seen = true;
        if (slotNum === 0) {
          // Rajiv 1794244109.359299: PM only. Fully rendered fixture explicitly
          // requires Enter. Consume before the await; uncertainty never retries.
          if (await inspect() !== "dialog" || !current()) break;
          state.attempts = 1;
          save();
          await this.runShell(`tmux send-keys -t ${paneId} 1`, { timeout: Math.min(1_000, deadline - Date.now()) });
          if (await inspect() !== "dialog" || !current()) break;
          enterPresses++;
          await this.runShell(`tmux send-keys -t ${paneId} Enter`, { timeout: Math.min(1_000, deadline - Date.now()) });
          break;
        }
        // This canonical dialog's option 1 STOPS background tasks. Never select
        // it: the agents must finish/handoff, then a fresh Stop may retry once.
        const attempt = db.getExitAttempt(slotNum);
        if (attempt && attempt.originalProcess === process) {
          attempt.phase = "blocked";
          attempt.blockedReason = "exit_dialog_observed";
          attempt.reported = true;
          db.saveExitAttempt(slotNum, attempt);
          db.logEvent(slotNum, "exit_blocked", null, null, { ...attempt, source: "exit_dialog_confirmation" });
          if (slotNum > 0) this.injectToPM(`# ⚠️ slot ${slotNum} exit blocked: background_agents; agents are not stopped. Retry only at the next eligible Stop, once.`);
        }
        break;
      }
      state.done = true;
      save();
      db.logEvent(slotNum, "exit_dialog_confirmation", null, null, { episode, paneId, attempts: state.attempts, dialog_seen: seen, enter_presses: enterPresses });
    } catch {
      state.done = true;
      save();
      db.logEvent(slotNum, "exit_dialog_confirmation", null, null, { episode, paneId, attempts: state.attempts, dialog_seen: seen, enter_presses: enterPresses, outcome: "unknown_no_retry" });
    }
  }

  /** Activity evidence for a previously verified immutable pane, never a
   * numeric address or is-active.sh's error-as-idle exit status. Missing
   * capture/prompt or an unrecognized foreground color is unknown. */
  async getPaneActivityState(paneId: string): Promise<SlotActivityState> {
    if (!/^%\d+$/.test(paneId)) return "unknown";
    try {
      const { stdout } = await this.runShell(`tmux capture-pane -e -t ${paneId} -p`, { timeout: 5_000 });
      const plain = stdout.replace(/\x1b\[[0-9;]*m/g, "");
      if (composerText(plain) === null) return "unknown";
      const lines = stdout.split("\n");
      let promptLine = -1;
      // Slash autocomplete also renders a chevron below the composer. Only
      // the prompt immediately below a composer rule owns activity evidence.
      for (let i = lines.length - 1; i > 0; i--) {
        if (/^\s*─{20,}\s*$/.test(lines[i - 1].replace(/\x1b\[[0-9;]*m/g, ""))
            && /^\s*❯(?:\s|$)/.test(lines[i].replace(/\x1b\[[0-9;]*m/g, ""))
            && lines.slice(i + 1).some(l => /^\s*─{20,}\s*$/.test(l.replace(/\x1b\[[0-9;]*m/g, "")))) {
          promptLine = i;
          break;
        }
      }
      if (promptLine < 0) return "unknown";
      const line = lines[promptLine];
      let color = "default";
      // Foreground can be inherited from a preceding line; process the full
      // capture prefix, not just the prompt line's explicit SGR codes.
      const prefix = lines.slice(0, promptLine).join("\n") + "\n" + line.slice(0, line.indexOf("❯"));
      for (const match of prefix.matchAll(/\x1b\[([0-9;]*)m/g)) {
        const codes = (match[1] || "0").split(";").map(Number);
        for (let i = 0; i < codes.length; i++) {
          if (codes[i] === 0 || codes[i] === 39) color = "default";
          else if (codes[i] === 38 && codes[i + 1] === 2) {
            color = codes.slice(i + 2, i + 5).join(";"); i += 4;
          } else if (codes[i] === 38 && codes[i + 1] === 5) {
            color = `indexed:${codes[i + 2]}`; i += 2;
          } else if ((codes[i] >= 30 && codes[i] <= 37) || (codes[i] >= 90 && codes[i] <= 97)) {
            color = String(codes[i]);
          }
        }
      }
      // Claude renders the busy chevron as truecolor 153;153;153, or as its
      // 256-colour quantization 246 when the terminal lacks truecolor (2026-10-13).
      if (color === "153;153;153" || color === "indexed:246") return "active";
      return ["default", "37", "97", "255;255;255"].includes(color) ? "idle" : "unknown";
    } catch {
      return "unknown";
    }
  }

  /**
   * Single-attempt, fenced recovery delivery (CTO REVISE on b054f319,
   * C0ALZJHGE49/1791049473.052849):
   * - refuses unless the verified pane id equals the observed pane id;
   * - runs the caller's recheck (owner/turn/stall) inside the per-slot send
   *   lock before paste AND again immediately before the one Enter;
   * - never replays clearing keys or repastes after an attempted effect;
   *   slash submissions permit one owned-payload Enter retry;
   * - reports refused_pre_effect only before any clearing key or paste attempt,
   *   and uncertain for unconfirmed outcomes after either effect.
   */
  deliverGuardedToSlot(slotNum: number, command: string,
    opts: Parameters<TmuxRelay["deliverGuardedToSlotNow"]>[2]): Promise<GuardedSlotDelivery> {
    const generation = this.pmInboundGeneration;
    const check = opts.finalCheck;
    const run = () => this.deliverGuardedToSlotNow(slotNum, command, {
      ...opts,
      finalCheck: (stage, snapshot) => slotNum === 0 && command === "/exit"
        && (generation !== this.pmInboundGeneration || this.pmInboundPending > 0 || (this.db?.peekPendingPMEvents().length ?? 0) > 0)
        ? "pm_inbound_pending" : check?.(stage, snapshot) ?? null,
    });
    if (slotNum !== 0) return run();
    // Exit uses the SAME chain as inbound PM writes. No input admission gate.
    const result = this.directInjectChain.then(run, run);
    this.directInjectChain = result.then(() => undefined, () => undefined);
    return result;
  }

  private async deliverGuardedToSlotNow(
    slotNum: number,
    command: string,
    opts: {
      expectedPaneId: string;
      recheck: (stage: "pre_paste" | "pre_enter", capture: () => Promise<string | null>) => Promise<string | null>;
      /** Synchronous fence run with no await before the paste and the Enter effect.
       * The snapshot is the composer capture just taken for that stage. */
      finalCheck?: (stage: "pre_paste" | "pre_enter", snapshot?: string | null) => string | null;
      /** Bracketed paste (-p) so a multi-line payload cannot submit early. */
      bracketedPaste?: boolean;
      /**
       * Never send the slash pre-clear keys (Escape, C-u) or require an idle
       * pane for them: Escape interrupts a busy turn. The composer must already
       * be empty; a busy Claude Code pane queues the command (PM Stop /exit,
       * Rajiv ts 1793758450.451269).
       */
      skipSlashClear?: boolean;
      /**
       * Scoped PM-exit mode only (CTO REVISE ts 1793760137.229639): Claude
       * Code's native "press up to edit queued messages" banner is not foreign
       * composer text, so it does not refuse the paste/Enter. The exact
       * empty-composer and own-payload checks still apply, and a banner seen
       * before the paste or the Enter never counts as proof of this submit.
       */
      allowNativeQueuedInput?: boolean;
      /** Only an already-authorized exit-pending episode may answer its exit dialog. */
      exitEpisode?: string;
      /** Original consumed Stop owner, captured before deferred dispatch. */
      exitOwnerPin?: ExitOwnerPin;
      timing?: { dwellMs?: number; pollMs?: number; payloadGraceMs?: number; payloadStableMs?: number; clearGraceMs?: number };
    },
  ): Promise<GuardedSlotDelivery> {
    if (!isValidRuntimeSlot(slotNum)) return { outcome: "refused_pre_effect", reason: "unknown_slot", paneId: null };
    const ownerRefusal = () => opts.exitOwnerPin ? this.exitOwnerRefusal(slotNum, opts.exitOwnerPin) : null;
    const finalCheck = (stage: "pre_paste" | "pre_enter", snapshot?: string | null) =>
      ownerRefusal() ?? opts.finalCheck?.(stage, snapshot) ?? null;
    if (ownerRefusal()) return { outcome: "refused_pre_effect", reason: "slot_owner_changed", paneId: null };
    const identity = await verifyPaneIdentity(slotNum, this.runShell);
    if (!identity.ok) return { outcome: "refused_pre_effect", reason: `pane_identity:${identity.reason}`, paneId: null };
    const paneId = identity.snapshot.paneId;
    if (paneId !== opts.expectedPaneId) {
      return { outcome: "refused_pre_effect", reason: "pane_changed_since_observation", paneId };
    }
    const capture = async (): Promise<string | null> => {
      try {
        return (await this.runShell(`tmux capture-pane -t ${paneId} -p -S -${GUARDED_CAPTURE_LINES}`, { timeout: 5_000 })).stdout;
      } catch {
        return null;
      }
    };
    const recheck = async (stage: "pre_paste" | "pre_enter"): Promise<string | null> => {
      const current = await verifyPaneIdentity(slotNum, this.runShell);
      if (!current.ok || current.snapshot.paneId !== paneId) return "pane_changed_since_observation";
      return ownerRefusal() ?? await opts.recheck(stage, capture) ?? ownerRefusal();
    };
    const result = await withSlotSendLock(slotNum, async (): Promise<GuardedSlotDelivery> => {
      const exitOwner = !opts.exitOwnerPin && this.db ? JSON.stringify([
        this.db.getEvents(slotNum, 1, "SessionStart")[0]?.id,
        this.db.getEvents(slotNum, 1, "UserPromptSubmit")[0]?.id,
        this.db.getSlot(slotNum)?.assignment_epoch,
        this.db.getSlot(slotNum)?.active_turn_id,
      ]) : "";
      const exitProcess = command === "/exit" && opts.exitEpisode
        ? await this.exitProcessIdentity(slotNum, paneId) : null;
      const previous = this.db?.getExitAttempt(slotNum);
      const trackedExit = command === "/exit" && !!opts.exitEpisode;
      if (trackedExit && !opts.exitOwnerPin) return { outcome: "refused_pre_effect", reason: "stop_owner_required", paneId };
      if (trackedExit && (!exitProcess || !this.db)) return { outcome: "refused_pre_effect", reason: "claude_process_unknown", paneId };
      if (trackedExit && previous && (previous.originalProcess !== exitProcess
          || previous.sessionId !== this.db?.latestSessionId(slotNum)
          || previous.assignmentEpoch !== (this.db?.getSlot(slotNum)?.assignment_epoch ?? 0)
          || previous.phase !== "blocked" || previous.attemptCount >= 2
          || previous.blockedReason !== "exit_dialog_observed"
          || previous.inputEventId !== (this.db?.getEvents(slotNum, 1, "UserPromptSubmit")[0]?.id ?? null)
          || !previous.ownerPin || (previous.ownerPin as ExitOwnerPin).assignment !== opts.exitOwnerPin?.assignment
          || previous.stopEventId === (this.db?.getEvents(slotNum, 1, "Stop")[0]?.id ?? null))) {
        return { outcome: "refused_pre_effect", reason: "exit_attempt_already_owned", paneId };
      }
      const prePaste = await recheck("pre_paste");
      if (prePaste) return { outcome: "refused_pre_effect", reason: prePaste, paneId };
      let attempt: DevExitAttempt | null = null;
      let effectAttempted = false;
      const refuse = (reason: string): GuardedSlotDelivery => ({
        outcome: effectAttempted ? "uncertain" : "refused_pre_effect", reason, paneId,
      });
      const before = await capture();
      let prePasteComposer = composerText(before);
      const slash = isSlashCommand(command);
      if (slash && command !== "/exit" && !opts.skipSlashClear && slashComposerClearable(prePasteComposer, command)) {
        const cleared = await this.clearComposerForSlash(paneId, async () => composerText(await capture()), command,
          async () => (await recheck("pre_paste"))
            ?? (await this.getSlotActivityState(slotNum, paneId) !== "idle" ? "pane_not_idle" : null),
          () => finalCheck("pre_paste"),
          () => { effectAttempted = true; }).catch(() => ({ composer: null, reason: "clear_check_failed" }));
        if (cleared.reason) return refuse(cleared.reason);
        prePasteComposer = cleared.composer;
      }
      if (prePasteComposer === null) return refuse("composer_unreadable");
      if (prePasteComposer !== "") return refuse("composer_not_empty");
      const tmpFile = `/tmp/mop-guarded-${slotNum}-${Date.now()}.txt`;
      const bufName = `mop-guarded-${slotNum}`;
      let enterSent = 0;
      let preEnterRefusal: string | null = null;
      // A native queued banner observed before the paste/Enter is pre-existing
      // state, never evidence that THIS send was queued.
      let nativeQueueSeen = QUEUED_HINT.test(before ?? "");
      try {
        await fs.writeFile(tmpFile, command);
        await this.runShell(`tmux load-buffer -b ${bufName} ${shellEscape(tmpFile)}`, { timeout: 3_000 });
        const loadedRefusal = await recheck("pre_paste");
        if (loadedRefusal) return refuse(loadedRefusal);
        // File/buffer loading and the caller's fence can yield to a new draft.
        // Read the composer again after those awaits, before the paste effect.
        if (trackedExit && await this.exitProcessIdentity(slotNum, paneId) !== exitProcess) return refuse("claude_process_changed");
        const loadedSnapshot = await capture();
        const loadedQueued = QUEUED_HINT.test(loadedSnapshot ?? "");
        if (loadedQueued) nativeQueueSeen = true;
        if (slotNum === 0 && loadedQueued && !opts.allowNativeQueuedInput) return refuse("pm_cli_queue_not_empty");
        const loadedComposer = composerText(loadedSnapshot);
        if (loadedComposer === null) return refuse("composer_unreadable");
        if (loadedComposer !== "") return refuse("composer_not_empty");
        const prePasteFence = finalCheck("pre_paste", loadedSnapshot);
        if (prePasteFence) return refuse(prePasteFence);
        if (trackedExit && exitProcess && this.db) {
          const [, pid, start] = JSON.parse(exitProcess);
          attempt = {
            episode: opts.exitEpisode!, paneId, originalClaudePid: pid, originalStart: start,
            originalProcess: exitProcess, sessionId: this.db.latestSessionId(slotNum),
            inputEventId: this.db.getEvents(slotNum, 1, "UserPromptSubmit")[0]?.id ?? null,
            assignmentEpoch: this.db.getSlot(slotNum)?.assignment_epoch ?? 0,
            stopEventId: this.db.getEvents(slotNum, 1, "Stop")[0]?.id ?? null,
            ownerPin: opts.exitOwnerPin ?? null, phase: "dispatching",
            attemptCount: (previous?.attemptCount ?? 0) + 1, submittedAt: new Date().toISOString(), blockedReason: null,
          };
          this.db.saveExitAttempt(slotNum, attempt); // Durable BEFORE any paste; crash means UNKNOWN, never replay.
        }
        effectAttempted = true;
        await this.runShell(`tmux paste-buffer${opts.bracketedPaste ? " -p" : ""} -b ${bufName} -t ${paneId} -d`, { timeout: 3_000 });
        const submit = await submitWithComposerCheck(command, {
          capture,
          pressSubmit: async () => {
            preEnterRefusal = await recheck("pre_enter");
            if (preEnterRefusal) throw new GuardedEnterRefused(preEnterRefusal);
            // Substring payload detection is insufficient for a recovery
            // command: foreign text alongside it must never be submitted.
            const submitSnapshot = await capture();
            const submitQueued = QUEUED_HINT.test(submitSnapshot ?? "");
            if (submitQueued) nativeQueueSeen = true;
            if (slotNum === 0 && submitQueued && !opts.allowNativeQueuedInput) throw new GuardedEnterRefused("pm_cli_queue_not_empty");
            const submitComposer = composerText(submitSnapshot);
            // Opaque collapsed placeholders are owned only by eligible bracketed
            // multiline sends (assignment packets), never by short recovery sends.
            preEnterRefusal = submitComposer === null ? "composer_unreadable"
              : !composerOwnsOnlyPayload(submitComposer, command, {
                allowOpaquePlaceholder: opts.bracketedPaste === true && command.includes("\n"),
                prePasteComposer,
              }) ? "composer_not_owned_by_send" : null;
            if (preEnterRefusal) throw new GuardedEnterRefused(preEnterRefusal);
            preEnterRefusal = finalCheck("pre_enter", submitSnapshot);
            if (preEnterRefusal) throw new GuardedEnterRefused(preEnterRefusal);
            enterSent++;
            await this.runShell(`tmux send-keys -t ${paneId} Enter`, { timeout: 3_000 });
          },
          sleep,
          prePasteComposer,
          // Slash commands: one Enter retry only while the exact text is still
          // buffered (autocomplete swallowed the first Enter).
          maxEnterPresses: slash && command !== "/exit" ? 2 : 1,
          ...(slash ? this.slashSubmitTiming() : {}),
          ...opts.timing,
        });
        this.db?.logEvent(slotNum, "send_submit_check", null, null, {
          command: command.slice(0, 200),
          guarded: true,
          payload_seen: submit.payloadSeen,
          payload_stable: submit.payloadStable,
          cleared: submit.cleared,
          enter_presses: submit.enterPresses,
        });
        const confirmed = submit.payloadSeen === true && submit.payloadStable === true
          && (submit.cleared === true || (submit.queued === true && !(opts.allowNativeQueuedInput && nativeQueueSeen)))
          && (submit.enterPresses === 1 || (slash && submit.enterPresses === 2));
        if (confirmed) {
          if (attempt && this.db) { attempt.phase = "delivered"; this.db.saveExitAttempt(slotNum, attempt); }
          return { outcome: "delivered", paneId };
        }
        return {
          outcome: "uncertain",
          reason: submit.enterPresses === 0 ? "pasted_not_submitted"
            : slash && command !== "/exit" && submit.cleared === false ? "composer_not_cleared" : "submit_unconfirmed",
          paneId,
        };
      } catch (err) {
        if (err instanceof GuardedEnterRefused) {
          // A refused slash-command retry after the first Enter already reached
          // the pane (e.g. /exit is tearing the TUI down) is an unconfirmed
          // submit, not a pre-Enter refusal: callers prove the effect themselves.
          if (enterSent > 0) return { outcome: "uncertain", reason: "submit_unconfirmed", paneId };
          return { outcome: "uncertain", reason: `pre_enter_refused:${err.reason}`, paneId };
        }
        return effectAttempted
          ? { outcome: "uncertain", reason: "effect_error", paneId }
          : { outcome: "refused_pre_effect", reason: "load_buffer_failed", paneId };
      } finally {
        if (command === "/exit" && effectAttempted && enterSent === 0) {
          // Incoming custody stays queued if our paste cannot be proven removed.
          if (slotNum === 0) this.db?.setConfig("pm_exit_cleanup_pending", JSON.stringify({ paneId, exitProcess }));
          // Inbound cancels submission, not ownership of our unsubmitted paste.
          const physicalRefusal = () => opts.exitOwnerPin
            ? this.exitOwnerRefusal(slotNum, opts.exitOwnerPin, true) : "exit_owner_unbound";
          const owns = async () => physicalRefusal() === null
            && await this.exitProcessIdentity(slotNum, paneId) === exitProcess && physicalRefusal() === null;
          let removed = false;
          if (await owns()) {
            const draft = composerText(await capture());
            if (draft === "") removed = true;
            else if (draft === command) {
              try {
                // All awaited identity checks precede the final composer read.
                if (await owns()) {
                  const exactDraft = composerText(await capture());
                  if (exactDraft === command && physicalRefusal() === null) {
                    await this.runShell(`tmux send-keys -t ${paneId} C-u`, { timeout: 3_000 });
                    removed = await owns() && composerText(await capture()) === "" && physicalRefusal() === null;
                  }
                }
              } catch { /* Unknown cleanup retains inbound custody, never joined paste. */ }
            }
          }
          if (slotNum === 0 && removed) this.db?.setConfig("pm_exit_cleanup_pending", "");
        }
        if (attempt && this.db && attempt.phase === "dispatching") {
          attempt.phase = "unknown";
          this.db.saveExitAttempt(slotNum, attempt);
        }
        await fs.unlink(tmpFile).catch(() => undefined);
        if (enterSent > 0 && opts.exitEpisode && exitProcess) {
          // Preserve the original /exit disposition, including UNKNOWN. This
          // answers only its visible dialog; it never resends /exit.
          this.scheduleExitConfirmation(slotNum, paneId, opts.exitEpisode, exitProcess, exitOwner, opts.exitOwnerPin);
        }
      }
    });
    this.db?.logEvent(slotNum, "guarded_delivery", null, null, { ...result, command: command.slice(0, 200) });
    return result;
  }

  /**
   * Check if a slot is currently active (processing).
   * Activity comes from the verified immutable pane, not an installed helper.
   * Unknown activity is conservatively busy for legacy boolean callers.
   * Writers must also carry the observed pane id through their send lock.
   */
  async isSlotActive(slotNum: number): Promise<boolean> {
    return (await this.getSlotActivityState(slotNum)) !== "idle";
  }

  async getSlotActivityState(slotNum: number, expectedPaneId?: string): Promise<SlotActivityState> {
    const identity = await verifyPaneIdentity(slotNum, this.runShell);
    if (!identity.ok || (expectedPaneId !== undefined && identity.snapshot.paneId !== expectedPaneId)) return "unknown";
    const paneId = identity.snapshot.paneId;
    const activity = await this.getPaneActivityState(paneId);
    // Capture is awaited: do not return evidence for a pane rebound meanwhile.
    const after = await verifyPaneIdentity(slotNum, this.runShell);
    return after.ok && after.snapshot.paneId === paneId ? activity : "unknown";
  }

  /**
   * Capture the current output of a slot's tmux pane.
   * Prefers log-based capture (persistent, never loses content) when LogManager is attached.
   * Falls back to tmux capture-pane if no LogManager.
   */
  async captureOutput(slotNum: number, lines = 30): Promise<{ output: string; activity: "busy" | "idle" }> {
    // Always use tmux capture-pane — captures the visible terminal screen including
    // Claude Code TUI prompts (plan approval, status bar) that pipe-pane logs miss.
    // (Rajiv directive 2026-03-18: "change it to use tmux capture pane instead")
    let output = "";
    const paneAddress = resolvePaneAddress(slotNum);
    try {
      const raw = await execShell(
        `tmux capture-pane -t ${paneAddress} -p -S -${lines}`,
        { timeout: 5_000 }
      );
      output = raw.stdout;
    } catch (err) {
      output = `[capture failed: ${err}]`;
    }

    const activity = (await this.isSlotActive(slotNum)) ? "busy" as const : "idle" as const;
    return { output, activity };
  }

  /**
   * Check if a slot is actively producing output based on log mtime.
   * If log was modified in the last 5 seconds, the slot is actively working.
   * Falls back to verified pane activity if no LogManager.
   */
  async isSlotActiveFromLog(slotNum: number): Promise<boolean> {
    if (!this.logManager) return this.isSlotActive(slotNum);

    const mtime = await this.logManager.getLogMtime(slotNum);
    if (!mtime) return this.isSlotActive(slotNum); // No log → fallback

    const ageMs = Date.now() - mtime.getTime();
    if (ageMs < 5_000) return true; // Log modified recently → active

    // Log is stale, but slot might be waiting for input (no output)
    // Fall back to verified pane activity as secondary check
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

function parseDbTimestampMs(timestamp: string): number {
  if (/Z$|[+-]\d{2}:?\d{2}$/.test(timestamp)) {
    return new Date(timestamp).getTime();
  }
  return new Date(timestamp + "Z").getTime();
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

export type GuardedSlotDelivery =
  | { outcome: "delivered"; paneId: string }
  | { outcome: "refused_pre_effect"; reason: string; paneId: string | null }
  | { outcome: "uncertain"; reason: string; paneId: string };

class GuardedEnterRefused extends Error {
  constructor(readonly reason: string) {
    super(reason);
  }
}

/** Observation and guarded-delivery rechecks must read the same window. */
const GUARDED_CAPTURE_LINES = 60;
