/**
 * MoP Stuck Detector — Watchdog for unresponsive slots
 *
 * Checks all occupied, non-idle, non-DND slots every 60 seconds.
 * If a slot's log file hasn't been modified in 5+ minutes, it's
 * considered "stuck" and PM is notified.
 *
 * Dedup: Only notifies PM once per 10 minutes per slot to prevent spam.
 */

import { access, appendFile, readFile, unlink, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execShell } from "./asyncCommand.js";
import { slotAssignmentTuple, type MoPDatabase } from "./db.js";
import type { LogManager } from "./logs.js";
import type { GuardedSlotDelivery, TmuxRelay } from "./relay.js";
import type { SlotState } from "./types.js";
import { isValidDevSlot, PM_SLOT } from "./slotConfig.js";
import { composerText } from "./composer.js";

function debugLog(line: string): void {
  void appendFile(
    "/tmp/mop-debug.log",
    `${new Date().toISOString()} ${line}\n`
  ).catch(() => undefined);
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

// PM wait-nudge interval: how long a slot must be idle-occupied/idle-free
// before the detector asks it to request an assignment from PM. Re-enabled
// at Rajiv's direction (2026-09-29) via the launchd
// MOP_PM_WAIT_NUDGE_INTERVAL_MS env var (com.heydonna.mop-server.plist), read
// live at call time so a runtime toggle takes effect without a restart, same
// pattern as MOP_PM_WAIT_NUDGES_DISABLED above. Falls back to the historical
// 30-minute default when unset or invalid, floored at 60s so a misconfigured
// tiny value cannot make the detector spam a slot every tick.
const PM_WAIT_NUDGE_INTERVAL_FLOOR_MS = 60_000;
const PM_WAIT_NUDGE_INTERVAL_DEFAULT_MS = 30 * 60 * 1000;

export function getPmWaitNudgeIntervalMs(): number {
  const raw = process.env.MOP_PM_WAIT_NUDGE_INTERVAL_MS;
  if (raw === undefined || raw === "") return PM_WAIT_NUDGE_INTERVAL_DEFAULT_MS;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) return PM_WAIT_NUDGE_INTERVAL_DEFAULT_MS;
  return Math.max(parsed, PM_WAIT_NUDGE_INTERVAL_FLOOR_MS);
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

type ContinueDeliveryResult = {
  sent: boolean;
  reason:
    | "sent" | "send_failed" | "slot_missing" | "released" | "dnd" | "identity_changed" | "release_in_progress"
    | "guarded_refused" | "guarded_uncertain" | "guarded_unavailable";
  slot: SlotState | null;
  /** Set only for guarded deliveries. */
  outcome?: GuardedSlotDelivery["outcome"];
  detail?: string;
};

type GuardedSendOptions = Parameters<TmuxRelay["deliverGuardedToSlot"]>[2];

type IdleOccupiedUrgency = "REMINDER" | "FOLLOW_UP" | "URGENT" | "ESCALATION";

type IdleOccupiedAnchor = {
  timestamp: string;
  timestampMs: number;
  source: string;
};

type FreeSlotAssignmentGate = {
  allowed: boolean;
  slot: number;
  recommendation_kind: "slot_e2e" | "rework" | "todo" | null;
  rework_packet_count: number;
  rework_pr_count: number;
  ready_pool_size: number;
  recommended_obligation_id: number | null;
  recommended_pr: number | null;
  recommended_issue: number | null;
  recommended_packet: string | null;
  recommended_action: string | null;
  slot_dispatch_wedge_id: number | null;
  reason: string;
};

const RELEASE_OWNED_FREE_SLOT_ASSIGNMENT_GATE = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../scripts/ready-pool-assignment-gate.py",
);

function shellEscape(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

function validateGateRecommendation(parsed: FreeSlotAssignmentGate): void {
  const recommendedPr = parsed.recommended_pr;
  const hasValidPr =
    recommendedPr === null ||
    recommendedPr === undefined ||
    (typeof recommendedPr === "number" &&
      Number.isFinite(recommendedPr) &&
      Number.isInteger(recommendedPr) &&
      recommendedPr > 0);
  if (!hasValidPr) {
    throw new Error("invalid gate response: recommended_pr must be null or a positive integer");
  }

  const requiresPr =
    parsed.recommendation_kind !== null &&
    parsed.recommendation_kind !== undefined &&
    parsed.recommendation_kind !== "todo";
  if (
    requiresPr &&
    !(typeof recommendedPr === "number" && Number.isInteger(recommendedPr) && recommendedPr > 0)
  ) {
    throw new Error("invalid gate response: recommendation kind requires a positive recommended_pr");
  }
}

export class StuckDetector {
  private readonly STUCK_THRESHOLD_MS = 5 * 60 * 1000; // 5 minutes no output
  // PM wait-nudge cadence. Read live from MOP_PM_WAIT_NUDGE_INTERVAL_MS on
  // every check (see getPmWaitNudgeIntervalMs) rather than cached once at
  // construction, matching the MOP_PM_WAIT_NUDGES_DISABLED kill-switch
  // pattern. Kept as getters (not fields) so a runtime env change is
  // honored without a process restart.
  private get IDLE_OCCUPIED_THRESHOLD_MS(): number {
    return getPmWaitNudgeIntervalMs();
  }
  private get IDLE_FREE_THRESHOLD_MS(): number {
    return getPmWaitNudgeIntervalMs();
  }
  // How many recent release events per type are walked to find the newest
  // non-idempotent free-episode start. Idempotent replays are skipped; if no
  // real release/clear marker exists, a stable idle_free_anchor_created
  // watermark is written on first observation instead of using the moving
  // last_activity field (status reads refresh last_activity every minute).
  private readonly IDLE_FREE_RELEASE_LOOKBACK = 25;
  private readonly IDLE_OCCUPIED_SUBAGENT_LOOKBACK_SEC = 6 * 60 * 60;
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

  // ─── Transient model-API stall nudge ─────────────────────────────────────
  // Colocated with autocompact (Rajiv 2026-05-17, thread 1778957625.997439);
  // policy rewritten 2026-10-03 (thread 1791049473.052849). See apiStall.ts.
  private readonly apiStall = new ApiStallTracker();
  private checkInFlight = false;

  constructor(
    private db: MoPDatabase,
    private logManager: LogManager,
    private relay: TmuxRelay
  ) {}

  /**
   * Keep independent watchdog phases isolated. A runtime capability mismatch
   * in one specialized detector (for example an older relay object missing
   * getSlotActivityState) must be observable and fail closed for that phase,
   * but must not suppress the later idle-free/idle-occupied safety checks.
   */
  private async runPhase(name: string, phase: () => Promise<void>): Promise<void> {
    try {
      await phase();
    } catch (error) {
      const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      console.error(`[stuck] phase=${name} failed closed: ${detail}`);
      debugLog(`[stuck] phase=${name} failed closed: ${detail}`);
    }
  }

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
      await this.runPhase(`context-overflow:${slot.slot}`, () => this.checkContextOverflow(slot));
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
      await this.runPhase(`api500:${slot.slot}`, () => this.checkApi500Backoff(slot));
    }

    // Phase 1b: answer-prompt block detection. Applies to ALL slots, not
    // just wrappers — any slot can hit a numbered-options menu mid-task
    // (codex-companion, plan approval, manual interactive shell, etc.).
    // PM resolves via /slot-blocked skill. (Rajiv directive 2026-04-30)
    for (const slot of slots) {
      if (slot.dnd) continue;
      if (slot.slot === 0) continue; // PM pane — no self-detection
      await this.runPhase(`answer-prompt:${slot.slot}`, () => this.detectAnswerPromptBlock(slot));
    }

    // Phase 1c: bg-script failure detection. Watches the MoP server log
    // for persistent [check-slot] Slot N bg-script FAILED lines. Two
    // consecutive failures (10 min = 2 check-slot cycles) indicate the
    // slot may be hitting resource limits or context exhaustion. Auto-
    // inject /compact directly (deduped, same guards as checkContextOverflow).
    await this.runPhase("bg-script-failure", () => this.detectBgScriptFailures());

    // Phase 1d: resume dev slots that remain assigned but sit at a proven idle
    // prompt for more than five minutes. This is intentionally dev-only:
    // slot 0's historical auto-continue path was removed because it flooded
    // the PM input queue. Specialized recovery paths above take precedence.
    for (const slot of slots) {
      await this.runPhase(`idle-occupied:${slot.slot}`, () => this.checkIdleOccupied(slot));
      await this.runPhase(`stale-active:${slot.slot}`, () => this.checkStaleActiveTurn(slot));
    }

    // Phase 1e: a free slot cannot ask PM for work on its own. Wake it once
    // per urgency tier when the read-only PM obligation gate proves that
    // assignable work exists; the slot then uses the same pm-wait-nudge skill
    // as occupied waits. PM remains the sole assignment writer.
    for (const slot of slots) {
      await this.runPhase(`idle-free:${slot.slot}`, () => this.checkIdleFree(slot));
    }

    // Phase 1f: tell PM directly when slots sit free (unassigned) 10+ min.
    await this.runPhase("free-slots-pm", () => this.checkFreeSlotsPmNudge(slots));

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

      const mtime = await this.logManager.getLogMtime(slot.slot);
      if (!mtime) continue; // No log file — can't determine

      const ageMs = Date.now() - mtime!.getTime();
      if (ageMs > this.STUCK_THRESHOLD_MS) {
        this.handleStuck(slot, ageMs);
      }
    }
  }

  /**
   * Inject one polite continuation reminder per idle episode for an
   * occupied dev slot. The idle anchor comes from hook/reconciliation events
   * rather than slots.last_activity because status reads also refresh
   * last_activity.
   */
  async checkIdleOccupied(slot: SlotState): Promise<void> {
    // Master kill switch (Rajiv directive 2026-08-18): read at call time so
    // a runtime-env toggle is honored without a process restart. The flag
    // lives in the launchd EnvironmentVariables
    // (com.heydonna.mop-server.plist).
    if (process.env.MOP_PM_WAIT_NUDGES_DISABLED === "1") return;
    if (!isValidDevSlot(slot.slot)) return;
    if (!slot.occupied || slot.dnd) return;
    if (this.db.getExitPending() || this.db.hasPendingClear(slot.slot)) return;
    if (this.hasActiveReleaseIntent(slot)) return;

    const idleState = this.isIdleByHookState(slot);
    if (!idleState.idle) return;

    const idleAnchor = this.getIdleOccupiedAnchor(slot);
    if (!idleAnchor) return;
    const idleAgeMs = Date.now() - idleAnchor.timestampMs;
    if (!Number.isFinite(idleAgeMs) || idleAgeMs <= this.IDLE_OCCUPIED_THRESHOLD_MS) return;
    const waitAnchor = this.getIdleOccupiedWaitAnchor(slot, idleAnchor);
    const waitAgeMs = Date.now() - waitAnchor.timestampMs;
    const waitAgeMinutes = Math.max(5, Math.floor(waitAgeMs / 60_000));
    const urgency = this.idleOccupiedUrgency(waitAgeMinutes);

    const activeSubagent = this.db.hasRecentSubagentDispatch(
      slot.slot,
      this.IDLE_OCCUPIED_SUBAGENT_LOOKBACK_SEC
    );
    if (activeSubagent) {
      debugLog(
        `[idle-occupied] slot=${slot.slot} suppress=subagent-active ` +
        `dispatch=${activeSubagent.taskTs}`
      );
      return;
    }

    // Do not compete with a more specific recovery path for this idle episode.
    if (await pathExists(`/tmp/slot-${slot.slot}-api500-state.json`)) return;
    if (this.compactInFlightAt.has(slot.slot)) return;
    // An API-stall episode (pending, nudged, uncertain or capped) for this
    // assignment owns recovery: capped means ZERO automatic continuation.
    const stall = this.apiStall.status(slot.slot);
    if (stall.state !== "clear" && stall.assignment_epoch === (slot.assignment_epoch ?? null)) return;
    const specializedEvents = [
      "context_overflow_detected",
      "compact_dispatched",
      "api500_direct_nudge",
      "api_stall_detected",
      "api_stall_nudge",
      "api_stall_nudge_uncertain",
      "api_stall_nudge_refused",
      "api_stall_nudge_capped",
      "block_dispatched",
      "slot_promised_action_continue_injected",
      "continue_injected",
    ];
    for (const eventType of specializedEvents) {
      const event = this.db.getEvents(slot.slot, 1, eventType)[0];
      if (event && parseDbTimestampMs(event.timestamp) >= idleAnchor.timestampMs) return;
    }

    const prior = this.db.getEvents(slot.slot, 1, "idle_occupied_continue_injected")[0];
    if (prior) {
      try {
        const payload = JSON.parse(prior.payload) as {
          assignment_epoch?: number;
          idle_anchor?: string;
          urgency?: IdleOccupiedUrgency;
        };
        if (
          payload.assignment_epoch === slot.assignment_epoch &&
          payload.idle_anchor === idleAnchor.timestamp &&
          this.idleOccupiedUrgencyRank(payload.urgency) >=
            this.idleOccupiedUrgencyRank(urgency)
        ) {
          return;
        }
      } catch {
        // Malformed historical diagnostics must not suppress a current nudge.
      }
    }

    // Hook-derived turn state is authoritative. Re-pin ownership immediately
    // before delivery so a concurrent release/reassign cannot receive a stale
    // nudge. Do not overwrite or veto it with terminal prompt heuristics: those
    // are runtime-specific and cannot represent Claude and OMP uniformly.
    const current = this.db.getSlot(slot.slot);
    const currentIdleState = current ? this.isIdleByHookState(current) : null;
    if (
      !current?.occupied ||
      current.dnd ||
      !currentIdleState?.idle ||
      current.assignment_epoch !== slot.assignment_epoch ||
      current.assigned_at !== slot.assigned_at
    ) {
      return;
    }

    const command =
      `Use Skill(pm-wait-nudge) now with slot=${slot.slot} ` +
      `assignment_epoch=${slot.assignment_epoch} pr=${current.pr ?? "unknown"} ` +
      `issue=${current.issue ?? "unknown"} branch=${current.branch ?? "unknown"} ` +
      `head=${current.head_sha ?? "unknown"} wait_started_at=${waitAnchor.timestamp} ` +
      `wait_age_minutes=${waitAgeMinutes} urgency=${urgency}. ` +
      "Classify PM_WAIT vs LOCAL_CONTINUE. If LOCAL_CONTINUE, continue the exact " +
      "unfinished phase NOW: edits → affected tests → commit → push; do not end " +
      "the turn without a new head, a typed blocker, or a terminal receipt; " +
      "classification-only or \"will continue\" prose is a violation. API timeouts " +
      "and interrupted local work are not PM waits.";

    const delivery = await this.sendContinueIfAllowed(
      slot.slot,
      {
        assignment_epoch: slot.assignment_epoch,
        assigned_at: slot.assigned_at,
      },
      command
    );
    if (delivery.reason === "dnd" || delivery.reason === "released" ||
        delivery.reason === "slot_missing" || delivery.reason === "identity_changed" ||
        delivery.reason === "release_in_progress") {
      return;
    }
    const sent = delivery.sent;
    const deliveredSlot = delivery.slot ?? current;
    const payload = {
      command,
      assignment_epoch: slot.assignment_epoch,
      idle_anchor: idleAnchor.timestamp,
      idle_anchor_source: idleAnchor.source,
      idle_age_ms: idleAgeMs,
      wait_anchor: waitAnchor.timestamp,
      wait_anchor_source: waitAnchor.source,
      wait_age_ms: waitAgeMs,
      wait_age_minutes: waitAgeMinutes,
      urgency,
      turn_state: "inactive",
      issue: deliveredSlot.issue,
      pr: deliveredSlot.pr,
      branch: deliveredSlot.branch,
    };
    this.db.logEvent(
      slot.slot,
      sent ? "idle_occupied_continue_injected" : "idle_occupied_continue_failed",
      "Stuck",
      null,
      payload
    );
    debugLog(
      `[idle-occupied] slot=${slot.slot} ${sent ? "injected" : "failed"} ` +
      `epoch=${slot.assignment_epoch} idle_anchor=${idleAnchor.timestamp} ` +
      `wait_anchor=${waitAnchor.timestamp}`
    );
  }

  /**
   * Stale-active stall fallback (slot 4 incident 2026-10-04 ~08:50Z): a turn
   * that ended on "Owner / next action: PM" plus an interrupted prompt left
   * active_turn_state=active, so checkIdleOccupied never fired. When hook
   * state still says active but the pane is provably idle (is-active.sh idle,
   * empty composer) and the slot log has been silent for
   * STALE_ACTIVE_IDLE_MS (or the nudge interval when the last turn names PM
   * as the owner), send the same PM stall nudge once per silent episode.
   */
  private readonly STALE_ACTIVE_IDLE_MS = 20 * 60 * 1000;
  private readonly PM_OWNER_PATTERN = /Owner\s*\/\s*next action:\s*\**\s*PM\b/i;

  async checkStaleActiveTurn(slot: SlotState): Promise<void> {
    // CTO REVISE on 5742c0d (C0ALZJHGE49/1791105162.268429): OFF by default
    // until CTO reviews the guarded path. Enable with MOP_STALE_ACTIVE_NUDGE_ENABLED=1.
    if (process.env.MOP_STALE_ACTIVE_NUDGE_ENABLED !== "1") return;
    if (process.env.MOP_PM_WAIT_NUDGES_DISABLED === "1") return;
    if (!isValidDevSlot(slot.slot)) return;
    if (!slot.occupied || slot.dnd) return;
    if (slot.active_turn_state !== "active") return;
    if (this.db.getExitPending() || this.db.hasPendingClear(slot.slot)) return;
    if (this.hasActiveReleaseIntent(slot)) return;
    if (this.compactInFlightAt.has(slot.slot)) return;
    // Same exclusions as checkIdleOccupied: active subagent, and any API-stall
    // episode (incl. capped = zero automatic continuation) for this epoch.
    if (this.db.hasRecentSubagentDispatch(slot.slot, this.IDLE_OCCUPIED_SUBAGENT_LOOKBACK_SEC)) return;
    const stall = this.apiStall.status(slot.slot);
    if (stall.state !== "clear" && stall.assignment_epoch === (slot.assignment_epoch ?? null)) return;

    // CTO REVISE on 880774e: only POSITIVELY proven idle qualifies. Unknown
    // or unreadable activity (missing/unreadable log mtime, is-active probe
    // error, capture failure) is treated as NOT idle: no nudge.
    let mtime: Date | null = null;
    try {
      mtime = await this.logManager.getLogMtime(slot.slot);
    } catch {
      return;
    }
    if (!mtime || !Number.isFinite(mtime.getTime())) return;
    const silentMs = Date.now() - mtime.getTime();
    if (!Number.isFinite(silentMs) || silentMs <= this.IDLE_OCCUPIED_THRESHOLD_MS) return;

    if (typeof this.relay.captureOutput !== "function") return;
    if (typeof this.relay.getSlotActivityState !== "function") return;
    let output: string;
    try {
      // captureOutput maps a probe error to "idle"; use the tri-state probe.
      if ((await this.relay.getSlotActivityState(slot.slot)) !== "idle") return;
      ({ output } = await this.relay.captureOutput(slot.slot, 60));
    } catch {
      return;
    }
    if (typeof output !== "string" || output.startsWith("[capture failed")) return;
    if (composerText(output) !== "") return;
    const pmOwner = this.PM_OWNER_PATTERN.test(output);
    if (!pmOwner && silentMs < this.STALE_ACTIVE_IDLE_MS) return;

    const anchor = new Date(mtime.getTime()).toISOString();
    for (const eventType of ["stale_active_stall_nudge_injected", "stale_active_stall_nudge_uncertain"]) {
      const prior = this.db.getEvents(slot.slot, 1, eventType)[0];
      if (!prior) continue;
      try {
        const p = JSON.parse(prior.payload) as { assignment_epoch?: number; idle_anchor?: string };
        if (p.assignment_epoch === slot.assignment_epoch && p.idle_anchor === anchor) return;
      } catch {
        // Malformed historical diagnostics must not suppress a current nudge.
      }
    }

    const waitAgeMinutes = Math.max(5, Math.floor(silentMs / 60_000));
    const urgency = this.idleOccupiedUrgency(waitAgeMinutes);
    const command =
      `Use Skill(pm-wait-nudge) now with slot=${slot.slot} ` +
      `assignment_epoch=${slot.assignment_epoch} pr=${slot.pr ?? "unknown"} ` +
      `issue=${slot.issue ?? "unknown"} branch=${slot.branch ?? "unknown"} ` +
      `head=${slot.head_sha ?? "unknown"} wait_started_at=${anchor} ` +
      `wait_age_minutes=${waitAgeMinutes} urgency=${urgency}. ` +
      "Classify PM_WAIT vs LOCAL_CONTINUE. If LOCAL_CONTINUE, continue the exact " +
      "unfinished phase NOW: edits → affected tests → commit → push; do not end " +
      "the turn without a new head, a typed blocker, or a terminal receipt; " +
      "classification-only or \"will continue\" prose is a violation. API timeouts " +
      "and interrupted local work are not PM waits.";

    if (typeof this.relay.observeSlotPane !== "function") return;
    const observed = await this.relay.observeSlotPane(slot.slot);
    if (!observed || composerText(observed.text) !== "") return;
    const expected = { assignment_epoch: slot.assignment_epoch, assigned_at: slot.assigned_at };
    const finalCheck = (_stage: "pre_paste" | "pre_enter"): string | null => {
      const cur = this.db.getSlot(slot.slot);
      if (!cur?.occupied) return "released";
      if (cur.dnd) return "dnd";
      if (cur.assignment_epoch !== expected.assignment_epoch || cur.assigned_at !== expected.assigned_at) {
        return "identity_changed";
      }
      if (cur.active_turn_state !== "active") return "turn_state_changed";
      return null;
    };
    const recheck = async (
      stage: "pre_paste" | "pre_enter",
      capture: () => Promise<string | null>,
    ): Promise<string | null> => {
      const text = await capture();
      if (text === null) return "pane_unreadable";
      if (stage === "pre_paste" && composerText(text) !== "") return "composer_not_empty";
      if (this.db.hasRecentSubagentDispatch(slot.slot, this.IDLE_OCCUPIED_SUBAGENT_LOOKBACK_SEC)) {
        return "subagent_active";
      }
      return finalCheck(stage);
    };
    const delivery = await this.sendContinueIfAllowed(
      slot.slot,
      expected,
      command,
      true,
      { expectedPaneId: observed.paneId, recheck, finalCheck },
    );
    const outcome = delivery.outcome ?? (delivery.sent ? "delivered" : "refused_pre_effect");
    // Only a proven pre-effect refusal leaves the episode eligible; an
    // uncertain send consumes it (no retry).
    if (outcome === "refused_pre_effect") return;
    this.db.logEvent(
      slot.slot,
      outcome === "delivered" ? "stale_active_stall_nudge_injected" : "stale_active_stall_nudge_uncertain",
      "Stuck",
      null,
      {
        command,
        assignment_epoch: slot.assignment_epoch,
        idle_anchor: anchor,
        idle_anchor_source: "log_mtime",
        silent_ms: silentMs,
        pm_owner: pmOwner,
        urgency,
        turn_state: "active_stale",
        pane_id: observed.paneId,
        reason: delivery.reason ?? null,
        detail: delivery.detail ?? null,
      },
    );
    debugLog(
      `[stale-active] slot=${slot.slot} ${outcome} ` +
      `epoch=${slot.assignment_epoch} anchor=${anchor} pm_owner=${pmOwner}`
    );
  }

  /**
   * Tier boundaries scale with the configured nudge interval (3x/6x/12x),
   * preserving the historical 15/30/60-minute tiers at the original 5-minute
   * interval while staying proportionate at any other configured interval
   * (e.g. a 30-minute interval yields 90/180/360-minute tiers). Each tier
   * fires at most once per wait episode via the rank-comparison dedupe in
   * checkIdleOccupied/checkIdleFree, so a short interval (e.g. 5 minutes)
   * still yields at most one nudge per interval, not one per detector tick.
   */
  private idleOccupiedUrgency(
    waitAgeMinutes: number,
    intervalMs: number = getPmWaitNudgeIntervalMs()
  ): IdleOccupiedUrgency {
    const intervalMinutes = Math.max(intervalMs / 60_000, 1);
    if (waitAgeMinutes >= intervalMinutes * 12) return "ESCALATION";
    if (waitAgeMinutes >= intervalMinutes * 6) return "URGENT";
    if (waitAgeMinutes >= intervalMinutes * 3) return "FOLLOW_UP";
    return "REMINDER";
  }

  /**
   * Wake one genuinely free dev slot to remind PM about one packet-backed
   * rework or Ready Pool obligation. The original release timestamp remains
   * the wait anchor, and each urgency tier fires at most once. The gate is
   * local/read-only and runs asynchronously, so this detector neither scans
   * GitHub nor mutates capacity.
   */
  async checkIdleFree(slot: SlotState): Promise<void> {
    if (process.env.MOP_PM_WAIT_NUDGES_DISABLED === "1") return;
    if (!isValidDevSlot(slot.slot)) return;
    if (slot.occupied || slot.dnd) return;
    if (this.db.getExitPending() || this.db.hasPendingClear(slot.slot)) return;

    const idleState = this.isIdleByHookState(slot);
    if (!idleState.idle) return;

    const anchor = this.getOrCreateFreeAnchor(slot);
    if (!anchor) return;
    const freeAgeMs = Date.now() - anchor.timestampMs;
    if (!Number.isFinite(freeAgeMs) || freeAgeMs <= this.IDLE_FREE_THRESHOLD_MS) return;
    const waitAgeMinutes = Math.max(5, Math.floor(freeAgeMs / 60_000));
    const urgency = this.idleOccupiedUrgency(waitAgeMinutes);

    const prior = this.db.getEvents(slot.slot, 1, "idle_free_assignment_nudge_injected")[0];
    if (prior) {
      try {
        const payload = JSON.parse(prior.payload) as {
          assignment_epoch?: number;
          free_anchor?: string;
          urgency?: IdleOccupiedUrgency;
        };
        if (
          payload.assignment_epoch === slot.assignment_epoch &&
          payload.free_anchor === anchor.timestamp &&
          this.idleOccupiedUrgencyRank(payload.urgency) >=
            this.idleOccupiedUrgencyRank(urgency)
        ) {
          return;
        }
      } catch {
        // Malformed diagnostics must not suppress a current free episode.
      }
    }

    const occupiedPrs = this.db.getAllSlots()
      .filter((candidate) => candidate.occupied && candidate.slot !== slot.slot)
      .map((candidate) => candidate.pr)
      .filter((pr): pr is number => typeof pr === "number" && Number.isInteger(pr) && pr > 0);
    const gate = await this.readFreeSlotAssignmentGate(slot.slot, occupiedPrs);
    if (!gate?.allowed) {
      debugLog(
        `[idle-free] slot=${slot.slot} suppress=${gate?.reason ?? "gate-failed"} ` +
        `rework_pr_count=${gate?.rework_pr_count ?? 0} ` +
        `ready_pool_size=${gate?.ready_pool_size ?? 0}`
      );
      return;
    }

    // Re-pin the free state after the asynchronous gate so a concurrent PM
    // assignment cannot receive a stale reminder command.
    const current = this.db.getSlot(slot.slot);
    const currentIdleState = current ? this.isIdleByHookState(current) : null;
    if (
      !current ||
      current.occupied ||
      current.dnd ||
      !currentIdleState?.idle ||
      current.assignment_epoch !== slot.assignment_epoch
    ) {
      return;
    }

    const command =
      `Use Skill(pm-wait-nudge) now with mode=FREE_WAIT_ASSIGNMENT slot=${slot.slot} ` +
      `assignment_epoch=${slot.assignment_epoch} wait_started_at=${anchor.timestamp} ` +
      `wait_age_minutes=${waitAgeMinutes} urgency=${urgency} ` +
      `recommendation_kind=${gate.recommendation_kind ?? "none"} ` +
      `rework_packet_count=${gate.rework_packet_count} ` +
      `rework_pr_count=${gate.rework_pr_count} ready_pool_size=${gate.ready_pool_size} ` +
      `recommended_obligation_id=${gate.recommended_obligation_id ?? "unknown"} ` +
      `recommended_pr=${gate.recommended_pr ?? "unknown"} ` +
      `recommended_issue=${gate.recommended_issue ?? "unknown"} ` +
      `recommended_packet=${gate.recommended_packet ?? "unknown"}. ` +
      "Revalidate that this slot is still free and the recommended obligation is open, " +
      "then remind PM once; do not assign work or run reconcile-capacity.";

    const sent = await this.relay.sendToSlotAsync(slot.slot, command, false);
    const payload = {
      command,
      assignment_epoch: slot.assignment_epoch,
      free_anchor: anchor.timestamp,
      free_anchor_source: anchor.source,
      free_age_ms: freeAgeMs,
      wait_age_minutes: waitAgeMinutes,
      urgency,
      turn_state: "inactive",
      recommendation_kind: gate.recommendation_kind,
      rework_packet_count: gate.rework_packet_count,
      rework_pr_count: gate.rework_pr_count,
      ready_pool_size: gate.ready_pool_size,
      recommended_obligation_id: gate.recommended_obligation_id,
      recommended_pr: gate.recommended_pr,
      recommended_issue: gate.recommended_issue,
      recommended_packet: gate.recommended_packet,
      slot_dispatch_wedge_id: gate.slot_dispatch_wedge_id,
    };
    this.db.logEvent(
      slot.slot,
      sent ? "idle_free_assignment_nudge_injected" : "idle_free_assignment_nudge_failed",
      "Stuck",
      null,
      payload
    );
    debugLog(
      `[idle-free] slot=${slot.slot} ${sent ? "injected" : "failed"} ` +
      `anchor=${anchor.timestamp} ready_pool_size=${gate.ready_pool_size}`
    );
  }

  /**
   * Free-slot PM nudge (Rajiv 2026-10-04 14:47 IST: S1/S6 sat free and
   * unassigned with no PM nudge). checkIdleFree only wakes the slot when the
   * obligation gate proves work exists; this phase tells PM directly when any
   * dev slot has been free for FREE_SLOT_PM_NUDGE_MS, once per free stretch
   * (keyed by the free anchor), grouping all newly due slots into one message.
   * Free slots cannot carry DND (server rejects it), so no hold applies.
   */
  private readonly FREE_SLOT_PM_NUDGE_MS = 10 * 60 * 1000;

  async checkFreeSlotsPmNudge(slots: SlotState[]): Promise<void> {
    // CTO REVISE on 880774e: OFF unless MOP_FREE_SLOT_NUDGE_ENABLED=1.
    if (process.env.MOP_FREE_SLOT_NUDGE_ENABLED !== "1") return;
    if (process.env.MOP_PM_WAIT_NUDGES_DISABLED === "1") return;
    if (this.db.getExitPending()) return;
    const due: Array<{ slot: SlotState; anchor: string; freeMin: number }> = [];
    for (const slot of slots) {
      if (!isValidDevSlot(slot.slot) || slot.occupied || slot.dnd) continue;
      if (this.db.hasPendingClear(slot.slot)) continue;
      const anchor = this.getOrCreateFreeAnchor(slot);
      if (!anchor) continue;
      const freeMs = Date.now() - anchor.timestampMs;
      if (!Number.isFinite(freeMs) || freeMs < this.FREE_SLOT_PM_NUDGE_MS) continue;
      const prior = this.db.getEvents(slot.slot, 1, "free_slot_pm_nudge_sent")[0];
      if (prior) {
        try {
          if ((JSON.parse(prior.payload) as { free_anchor?: string }).free_anchor === anchor.timestamp) continue;
        } catch {
          // Malformed diagnostics must not suppress a current free stretch.
        }
      }
      due.push({ slot, anchor: anchor.timestamp, freeMin: Math.floor(freeMs / 60_000) });
    }
    if (due.length === 0) return;
    // CTO REVISE on 880774e: re-read each slot immediately before sending and
    // drop any slot assigned (or epoch-changed) since detection this tick.
    const stillFree = due.filter((d) => {
      const cur = this.db.getSlot(d.slot.slot);
      return !!cur && !cur.occupied && !cur.dnd &&
        (cur.assignment_epoch ?? null) === (d.slot.assignment_epoch ?? null);
    });
    due.splice(0, due.length, ...stillFree);
    if (due.length === 0) return;
    const list = due.map((d) => `S${d.slot.slot} (${d.slot.name}) free ${d.freeMin}m`).join(", ");
    const message =
      `[MoP] Free dev slot(s) unassigned 10+ min: ${list}. ` +
      "Assign the next existing-PR rework or Ready Pool todo via mop-assign-slot, " +
      "or record the typed reason no work is eligible.";
    // PM pane only (never a slot pane), via the observation-bound PM submit
    // path. Any attempt consumes the free stretch: no retry on an uncertain send.
    const sent = await this.relay.sendToSlotAsync(PM_SLOT, message, false);
    for (const d of due) {
      this.db.logEvent(d.slot.slot, "free_slot_pm_nudge_sent", "Stuck", null, {
        delivered: sent,
        free_anchor: d.anchor,
        free_minutes: d.freeMin,
        assignment_epoch: d.slot.assignment_epoch,
        message,
      });
    }
    debugLog(`[free-slots-pm] ${sent ? "sent" : "uncertain"} slots=${due.map((d) => d.slot.slot).join(",")}`);
  }

  private async readFreeSlotAssignmentGate(
    slotNum: number,
    occupiedPrs: number[]
  ): Promise<FreeSlotAssignmentGate | null> {
    const gatePath = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE ??
      RELEASE_OWNED_FREE_SLOT_ASSIGNMENT_GATE;
    try {
      const { stdout } = await execShell(
        `${shellEscape(gatePath)} --slot ${slotNum}` +
          occupiedPrs.map((pr) => ` --exclude-pr ${pr}`).join(""),
        { timeout: 4_000, maxBuffer: 64 * 1024 }
      );
      const parsed = JSON.parse(stdout.trim()) as FreeSlotAssignmentGate;
      if (
        typeof parsed.allowed !== "boolean" ||
        parsed.slot !== slotNum ||
        !Number.isInteger(parsed.rework_packet_count) ||
        !Number.isInteger(parsed.rework_pr_count) ||
        !Number.isInteger(parsed.ready_pool_size)
      ) {
        throw new Error("invalid gate response");
      }
      validateGateRecommendation(parsed);
      if (
        parsed.recommended_pr !== null &&
        parsed.recommended_pr !== undefined &&
        occupiedPrs.includes(parsed.recommended_pr)
      ) {
        throw new Error(`gate recommended occupied PR ${parsed.recommended_pr}`);
      }
      return parsed;
    } catch (error) {
      this.db.logEvent(slotNum, "idle_free_assignment_gate_failed", "Stuck", null, {
        error: error instanceof Error ? error.message.slice(0, 200) : String(error).slice(0, 200),
        gate_path: gatePath,
      });
      return null;
    }
  }

  private idleOccupiedUrgencyRank(urgency?: IdleOccupiedUrgency): number {
    if (urgency === "ESCALATION") return 4;
    if (urgency === "URGENT") return 3;
    if (urgency === "FOLLOW_UP") return 2;
    if (urgency === "REMINDER") return 1;
    return 0;
  }

  /**
   * Hook state is the sole turn-boundary authority. Log mtime, pane prompts,
   * and missing active_turn_started_at are telemetry only and cannot turn an
   * active or indeterminate turn into an eligible idle episode.
   */
  private isIdleByHookState(
    slot: SlotState
  ): { idle: boolean } {
    return { idle: slot.active_turn_state === "inactive" };
  }

  private hasActiveReleaseIntent(slot: SlotState): boolean {
    const tuple = slotAssignmentTuple(slot);
    return tuple
      ? Boolean(this.db.hasActiveNativeReleaseIntent?.(slot.slot, slot.assignment_epoch, tuple))
      : false;
  }

  /**
   * Final guard for detector-owned continuation nudges. The relay intentionally
   * supports unconditional recovery sends, so every stuck-detector path must
   * re-read authoritative slot state immediately before asking it to type.
   */
  private async sendContinueIfAllowed(
    slotNum: number,
    expected?: { assignment_epoch?: number; assigned_at?: string | null },
    command = "continue your work or remind pm if blocked",
    allowActiveTurn = false,
    guarded?: GuardedSendOptions,
  ): Promise<ContinueDeliveryResult> {
    const current = this.db.getSlot(slotNum) ?? null;
    let reason: ContinueDeliveryResult["reason"] | null = null;
    if (!current) {
      reason = "slot_missing";
    } else if (!current.occupied) {
      reason = "released";
    } else if (current.dnd) {
      reason = "dnd";
    } else if (this.hasActiveReleaseIntent(current)) {
      reason = "release_in_progress";
    } else if (
      expected &&
      (current.assignment_epoch !== expected.assignment_epoch ||
        current.assigned_at !== expected.assigned_at)
    ) {
      reason = "identity_changed";
    }

    if (reason) {
      this.db.logEvent(slotNum, "continue_suppressed_slot_state", "Stuck", null, {
        command,
        reason,
        occupied: current?.occupied ?? false,
        dnd: current?.dnd ?? false,
        expected_assignment_epoch: expected?.assignment_epoch ?? null,
        observed_assignment_epoch: current?.assignment_epoch ?? null,
      });
      debugLog(
        `[stuck-continue] slot=${slotNum} suppress=${reason} ` +
        `occupied=${current?.occupied ?? false} dnd=${current?.dnd ?? false}`
      );
      return { sent: false, reason, slot: current };
    }
    // The slot-missing branch above returns; this assertion keeps the
    // authoritative state narrowed for the lease transaction below.
    if (!current) {
      return { sent: false, reason: "slot_missing", slot: null };
    }

    // Claim the same durable per-slot lease used by native release before the
    // first asynchronous relay operation. This closes the race where a
    // release claims between the read above and pane delivery. A failed claim
    // is fail-closed; the competing release (or an invalidated owner) wins.
    const tuple = slotAssignmentTuple(current);
    const claimReleaseIntent = this.db.claimNativeReleaseIntent;
    const claimReleaseIntentWithToken = this.db.claimNativeReleaseIntentWithToken;
    let nudgeLeaseToken: string | null = null;
    if (tuple && typeof claimReleaseIntentWithToken === "function") {
      nudgeLeaseToken = claimReleaseIntentWithToken.call(
        this.db,
        slotNum,
        current.assignment_epoch,
        tuple,
        undefined,
        allowActiveTurn,
      );
    } else if (tuple && typeof claimReleaseIntent === "function") {
      // Compatibility for lightweight detector fakes that predate tokens.
      nudgeLeaseToken = claimReleaseIntent.call(
        this.db,
        slotNum,
        current.assignment_epoch,
        tuple,
        undefined,
        allowActiveTurn,
      ) ? "__legacy__" : null;
    } else if (tuple) {
      nudgeLeaseToken = "__legacy__";
    }
    if (!nudgeLeaseToken) {
      const latest = this.db.getSlot(slotNum) ?? current;
      const competingRelease = this.hasActiveReleaseIntent(latest);
      const failedReason: ContinueDeliveryResult["reason"] = competingRelease
        || latest.assignment_epoch === current.assignment_epoch
        ? "release_in_progress"
        : "identity_changed";
      this.db.logEvent(slotNum, "continue_suppressed_slot_state", "Stuck", null, {
        command,
        reason: failedReason,
        occupied: latest.occupied,
        dnd: latest.dnd,
        expected_assignment_epoch: expected?.assignment_epoch ?? null,
        observed_assignment_epoch: latest.assignment_epoch,
      });
      return { sent: false, reason: failedReason, slot: latest };
    }

    try {
      if (guarded) {
        if (typeof this.relay.deliverGuardedToSlot !== "function") {
          return { sent: false, reason: "guarded_unavailable", slot: current, outcome: "refused_pre_effect" };
        }
        const result = await this.relay.deliverGuardedToSlot(slotNum, command, guarded);
        return {
          sent: result.outcome === "delivered",
          reason: result.outcome === "delivered" ? "sent"
            : result.outcome === "uncertain" ? "guarded_uncertain" : "guarded_refused",
          slot: current,
          outcome: result.outcome,
          detail: result.outcome === "delivered" ? undefined : result.reason,
        };
      }
      const sent = await this.relay.sendToSlotAsync(
        slotNum,
        command,
        false
      );
      return {
        sent,
        reason: sent ? "sent" : "send_failed",
        slot: current,
      };
    } finally {
      this.db.clearNativeReleaseIntent?.(
        slotNum,
        current.assignment_epoch,
        tuple!,
        nudgeLeaseToken === "__legacy__" ? undefined : nudgeLeaseToken,
      );
    }
  }

  private getIdleOccupiedAnchor(
    slot: SlotState
  ): IdleOccupiedAnchor | null {
    const assignedMs = slot.assigned_at ? parseDbTimestampMs(slot.assigned_at) : NaN;
    const candidates: Array<{ timestamp: string; timestampMs: number; source: string }> = [];
    for (const eventType of [
      "Stop",
      "SessionEnd",
      "slot_idle_reconciled_from_pane",
    ]) {
      const event = this.db.getEvents(slot.slot, 1, eventType)[0];
      if (!event) continue;
      const timestampMs = parseDbTimestampMs(event.timestamp);
      if (!Number.isFinite(timestampMs)) continue;
      if (Number.isFinite(assignedMs) && timestampMs < assignedMs) continue;
      candidates.push({ timestamp: event.timestamp, timestampMs, source: eventType });
    }
    if (candidates.length > 0) {
      candidates.sort((a, b) => b.timestampMs - a.timestampMs);
      return candidates[0];
    }
    if (!slot.assigned_at || !Number.isFinite(assignedMs)) return null;
    return {
      timestamp: slot.assigned_at,
      timestampMs: assignedMs,
      source: "assigned_at",
    };
  }

  private getIdleFreeAnchor(
    slot: SlotState
  ): { timestamp: string; timestampMs: number; source: string } | null {
    const candidates: Array<{ timestamp: string; timestampMs: number; source: string }> = [];
    for (const eventType of ["slot_released", "auto_released_post_pr", "slot_cleared"]) {
      // A release pass on an already-free slot logs a NEW slot_released event
      // whose payload marks idempotent: true (mcp.ts / server.ts pass through
      // releaseSlot's result). That replay is not a new free episode: treating
      // it as the anchor would reset the wait age and re-fire the REMINDER.
      // Walk back through the newest release events of each type and select
      // the newest one that does not self-identify as an idempotent replay.
      // Events without an explicit idempotent marker (legacy rows and
      // auto_released_post_pr) remain candidates, preserving the previous
      // behavior for every event the writers do not mark.
      const recent = this.db.getEvents(slot.slot, this.IDLE_FREE_RELEASE_LOOKBACK, eventType);
      for (const event of recent) {
        let idempotent = false;
        try {
          idempotent = (JSON.parse(event.payload) as { idempotent?: unknown }).idempotent === true;
        } catch {
          // Unparseable diagnostics payloads are treated as real releases.
        }
        if (idempotent) continue;
        const timestampMs = parseDbTimestampMs(event.timestamp);
        if (Number.isFinite(timestampMs)) {
          candidates.push({ timestamp: event.timestamp, timestampMs, source: eventType });
        }
        break;
      }
    }
    if (candidates.length > 0) {
      candidates.sort((a, b) => b.timestampMs - a.timestampMs);
      return candidates[0];
    }
    // No release/clear marker: do NOT fall back to last_activity. Status
    // reads and idle probes refresh last_activity on a free slot, so a moving
    // fallback would reset the wait age every minute and the nudge would
    // never fire. A stable watermark is created on first observation instead.
    return null;
  }

  /**
   * Free-slot wait anchor that works even when MoP never wrote a
   * `slot_released`/`slot_cleared` marker (a slot can become free through a
   * clear or an assignment handoff that only flips the DB row). On first
   * observation of a genuinely free+idle+inactive slot with no release/clear
   * anchor, persist one `idle_free_anchor_created` watermark so the wait age
   * is stable and the free-slot assignment nudge fires after the threshold.
   */
  private getOrCreateFreeAnchor(
    slot: SlotState
  ): { timestamp: string; timestampMs: number; source: string } | null {
    const existing = this.getIdleFreeAnchor(slot);
    if (existing) return existing;

    const prior = this.db.getEvents(slot.slot, 1, "idle_free_anchor_created")[0];
    if (prior) {
      try {
        const payload = JSON.parse(prior.payload) as { assignment_epoch?: number };
        if (payload.assignment_epoch === slot.assignment_epoch) {
          const priorMs = parseDbTimestampMs(prior.timestamp);
          if (Number.isFinite(priorMs)) {
            return { timestamp: prior.timestamp, timestampMs: priorMs, source: "idle_free_anchor_created" };
          }
        }
      } catch {
        // Malformed watermark is ignored; a fresh one is written below.
      }
    }

    const nowIso = new Date().toISOString().replace(/Z$/, "");
    this.db.logEvent(slot.slot, "idle_free_anchor_created", "Stuck", null, {
      assignment_epoch: slot.assignment_epoch,
      free_since: nowIso,
    });
    return {
      timestamp: nowIso,
      timestampMs: Date.now(),
      source: "idle_free_anchor_created",
    };
  }

  /**
   * Preserve the original wait start across detector-generated PM_WAIT turns.
   *
   * A nudge wakes the slot, which runs pm-wait-nudge, reminds PM, and stops.
   * That authoritative Stop is a new idle episode for re-nudge cadence, but it
   * is not progress on the blocked task. Walk only directly linked nudge
   * completions whose terminal Stop result is classification=PM_WAIT. Any
   * normal work turn, LOCAL_CONTINUE result, ownership change, notification,
   * or missing link leaves the latest idle episode as the new logical wait
   * start.
   */
  private getIdleOccupiedWaitAnchor(
    slot: SlotState,
    latestIdleAnchor: IdleOccupiedAnchor
  ): IdleOccupiedAnchor {
    const nudges = this.db.getEvents(slot.slot, 50, "idle_occupied_continue_injected");
    const stops = this.db
      .getEvents(slot.slot, 200, "Stop")
      .map((event) => ({ event, timestampMs: parseDbTimestampMs(event.timestamp) }))
      .filter((item) => Number.isFinite(item.timestampMs));
    const promptStarts = this.db
      .getEvents(slot.slot, 100, "UserPromptSubmit")
      .map((event) => ({ event, timestampMs: parseDbTimestampMs(event.timestamp) }))
      .filter((item) => Number.isFinite(item.timestampMs));

    let waitAnchor = latestIdleAnchor;
    let episodeTimestampMs = latestIdleAnchor.timestampMs;

    for (const nudge of nudges) {
      const nudgeTimestampMs = parseDbTimestampMs(nudge.timestamp);
      if (!Number.isFinite(nudgeTimestampMs) || nudgeTimestampMs >= episodeTimestampMs) continue;

      let payload: {
        assignment_epoch?: number;
        idle_anchor?: string;
        wait_anchor?: string;
        wait_anchor_source?: string;
      };
      try {
        payload = JSON.parse(nudge.payload) as typeof payload;
      } catch {
        continue;
      }
      if (payload.assignment_epoch !== slot.assignment_epoch || !payload.idle_anchor) continue;

      const completion = stops
        .filter((item) => item.timestampMs > nudgeTimestampMs)
        .sort((a, b) => a.timestampMs - b.timestampMs)[0];
      if (!completion || completion.timestampMs !== episodeTimestampMs) continue;

      const directPromptStarts = promptStarts.filter(
        (item) => item.timestampMs > nudgeTimestampMs && item.timestampMs < completion.timestampMs
      );
      const directStops = stops.filter(
        (item) => item.timestampMs > nudgeTimestampMs && item.timestampMs <= completion.timestampMs
      );
      if (directPromptStarts.length !== 1 || directStops.length === 0) continue;

      const readSessionId = (rawPayload: string, field: string): string | null => {
        try {
          const value = JSON.parse(rawPayload) as Record<string, unknown>;
          return typeof value[field] === "string" ? value[field] : null;
        } catch {
          return null;
        }
      };
      const promptSessionId = readSessionId(directPromptStarts[0].event.payload, "session_id");
      const completionSessionId = readSessionId(
        completion.event.payload,
        "session_id"
      );
      if (!promptSessionId || promptSessionId !== completionSessionId) continue;

      const endedAsPmWait = directStops.every((item) => {
        try {
          const stopPayload = JSON.parse(item.event.payload) as {
            session_id?: unknown;
            transcript?: unknown;
          };
          return stopPayload.session_id === promptSessionId &&
            typeof stopPayload.transcript === "string" &&
            /PM_WAIT_NUDGE_RESULT\s+classification=PM_WAIT\b/.test(stopPayload.transcript);
        } catch {
          return false;
        }
      });
      if (!endedAsPmWait) continue;

      const priorWaitTimestamp = payload.wait_anchor ?? payload.idle_anchor;
      const priorWaitTimestampMs = parseDbTimestampMs(priorWaitTimestamp);
      const priorEpisodeTimestampMs = parseDbTimestampMs(payload.idle_anchor);
      if (!Number.isFinite(priorWaitTimestampMs) || !Number.isFinite(priorEpisodeTimestampMs)) {
        continue;
      }

      waitAnchor = {
        timestamp: priorWaitTimestamp,
        timestampMs: priorWaitTimestampMs,
        source: payload.wait_anchor_source ?? "pm_wait_nudge_carry",
      };
      episodeTimestampMs = priorEpisodeTimestampMs;
    }

    return waitAnchor;
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
   * Transient model-API stall nudge (replaces the 2026-05-17 API-500 backoff).
   * Rajiv 2026-10-03 23:19 IST (C0ALZJHGE49 / 1791049473.052849): "Can mop
   * detect these stalls and nudge the slots?" Occupied + hook-idle turn + pane
   * tail ending on a transient API failure -> after a grace period, ONE nudge
   * per stall via the guarded relay. Three consecutive nudges that each
   * re-stall within 10 min -> stop and alert PM once. Policy lives in
   * apiStall.ts (pure, unit-tested).
   */
  private async checkApi500Backoff(slot: SlotState): Promise<void> {
    const slotNum = slot.slot;
    const turnIdle = this.isIdleByHookState(slot).idle;
    let classification = classifyApiStallTail("");
    let observedPaneId: string | null = null;
    if (turnIdle) {
      // Observe through the verified immutable pane id; delivery is pinned to
      // the same id (CTO REVISE P1, 1791049473.052849).
      const observed = typeof this.relay.observeSlotPane === "function"
        ? await this.relay.observeSlotPane(slotNum)
        : null;
      if (!observed) return; // identity/capture failure: try again next tick
      observedPaneId = observed.paneId;
      classification = classifyApiStallTail(observed.text);
    }
    const before = this.apiStall.status(slotNum).state;
    const action = this.apiStall.observe(slotNum, Date.now(), {
      occupied: slot.occupied,
      turnIdle,
      epoch: slot.assignment_epoch ?? null,
      classification,
    });
    if (action.kind === "wait" && before === "clear") {
      this.db.logEvent(slotNum, "api_stall_detected", "Stuck", null, {
        signature: classification.signature,
        error_text: classification.errorText,
        assignment_epoch: slot.assignment_epoch,
        grace_remaining_ms: action.remainingMs,
      });
      return;
    }
    if (action.kind === "cap_alert") {
      this.db.logEvent(slotNum, "api_stall_nudge_capped", "Stuck", null, {
        signature: classification.signature,
        consecutive_nudges: action.consecutive,
        assignment_epoch: slot.assignment_epoch,
      });
      this.relay.injectToPM(
        `# ⚠️ slot ${slotNum} keeps ending turns on transient model API errors ` +
        `(${classification.signature}); ${action.consecutive} MoP nudges each re-stalled within 10 min. ` +
        `Auto-nudge stopped; PM triage required.`
      );
      return;
    }
    if (action.kind !== "nudge" || !observedPaneId) return;

    const fingerprint = classification.fingerprint;
    const expected = { assignment_epoch: slot.assignment_epoch, assigned_at: slot.assigned_at };
    // Synchronous owner/epoch/turn fence. Runs with no await between it and
    // the effect (CTO REVISE 00:48, C0ALZJHGE49/1791049473.052849), so a hook
    // that flips the turn during an awaited capture is always seen.
    const finalCheck = (_stage: "pre_paste" | "pre_enter"): string | null => {
      const cur = this.db.getSlot(slotNum);
      if (!cur?.occupied) return "released";
      if (cur.dnd) return "dnd";
      if (cur.assignment_epoch !== expected.assignment_epoch || cur.assigned_at !== expected.assigned_at) {
        return "identity_changed";
      }
      if (!this.isIdleByHookState(cur).idle) return "turn_active";
      return null;
    };
    // Async observation first; the owner/turn fence is evaluated after it.
    const recheck = async (
      stage: "pre_paste" | "pre_enter",
      capture: () => Promise<string | null>,
    ): Promise<string | null> => {
      const text = await capture();
      if (text === null) return "pane_unreadable";
      const now = classifyApiStallTail(text);
      if (!now.stalled || now.fingerprint !== fingerprint) return `stall_changed:${now.reason}`;
      return finalCheck(stage);
    };
    const delivery = await this.sendContinueIfAllowed(
      slotNum,
      expected,
      API_STALL_NUDGE_MESSAGE,
      false,
      { expectedPaneId: observedPaneId, recheck, finalCheck },
    );
    const outcome = delivery.outcome ?? (delivery.sent ? "delivered" : "refused_pre_effect");
    // Fence: an attempted or uncertain delivery consumes this stall episode;
    // only a proven pre-effect refusal leaves it eligible.
    if (outcome !== "refused_pre_effect") this.apiStall.recordNudge(slotNum, Date.now());
    const eventType = outcome === "delivered" ? "api_stall_nudge"
      : outcome === "uncertain" ? "api_stall_nudge_uncertain" : "api_stall_nudge_refused";
    this.db.logEvent(slotNum, eventType, "Stuck", null, {
      signature: classification.signature,
      error_text: classification.errorText,
      consecutive_nudges: action.consecutive,
      assignment_epoch: slot.assignment_epoch,
      pane_id: observedPaneId,
      reason: delivery.reason ?? null,
      detail: delivery.detail ?? null,
    });
  }

  /** PM-visible per-slot stall status (surfaced on GET /slots as api_stall). */
  getApiStallStatus(slotNum: number): ApiStallSlotStatus {
    return this.apiStall.status(slotNum);
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
      await writeFile(`/tmp/slot-${slot.slot}-blocked-capture.txt`, pane);
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
  private async detectBgScriptFailures(): Promise<void> {
    const LOG_PATH = "/tmp/mop-server.log";
    let logText: string;
    try {
      logText = await readFile(LOG_PATH, "utf-8");
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
        const slotLogMtime = await this.logManager.getLogMtime(slot.slot);
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
  private async runCheck(): Promise<void> {
    if (this.checkInFlight) {
      console.warn("[stuck] Skipping overlapping watchdog check");
      return;
    }
    this.checkInFlight = true;
    try {
      await this.checkAll();
    } catch (err) {
      console.error("[stuck] Check failed:", err);
    } finally {
      this.checkInFlight = false;
    }
  }

  start(): void {
    if (this.timer) return; // Already running
    this.timer = setInterval(() => {
      void this.runCheck();
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

// ─── Transient model-API stall policy (Rajiv 2026-10-03 23:26 IST: "Implement in stuck.ts") ───
/**
 * Transient model-API stall detection + nudge policy (pure, unit-tested).
 *
 * Rajiv 2026-10-03 23:19 IST (C0ALZJHGE49 / 1791049473.052849):
 * "Can mop detect these stalls and nudge the slots?"
 *
 * A slot stalls when its turn ENDS on a transient provider failure (stream
 * disconnect, overload, 5xx/529, connection reset) and then sits idle at the
 * prompt. Claude Code's own "Waiting for API response · will retry in Nm"
 * banner is an in-flight retry, NOT a stall.
 */

export const API_STALL_NUDGE_MESSAGE =
  "Your last turn ended on a transient model API error. Resume your current " +
  "assignment from where you stopped; commit and push progress first.";

export const API_STALL_GRACE_MS = 75_000;
export const API_STALL_RESTALL_WINDOW_MS = 10 * 60_000;
export const API_STALL_MAX_CONSECUTIVE_NUDGES = 3;

/** Explicit transient-failure signatures, matched only on an `API Error:` line. */
export const API_STALL_SIGNATURES: ReadonlyArray<{ name: string; pattern: RegExp }> = [
  { name: "stream_disconnected", pattern: /stream (?:error|disconnected|closed before)/i },
  { name: "servers_overloaded", pattern: /overloaded/i },
  { name: "http_5xx", pattern: /API Error:\s*(?:\(?\s*)?5\d\d\b/ },
  { name: "internal_server_error", pattern: /internal server error|server_error|bad gateway|service unavailable|gateway time-?out/i },
  { name: "connection_reset", pattern: /ECONNRESET|connection reset|socket hang up|connection error|ETIMEDOUT|EPIPE|premature close/i },
];

/** Claude Code's own retry banner: the turn is still alive. */
export const API_RETRY_BANNER = /Waiting for API response|will retry in|Retrying in \d/i;

/** A turn-ending API error line (the assistant bullet line, not a ⎿ tool result). */
const API_ERROR_LINE = /^\s*(?:[⏺●]\s*)?API Error:/;
/** Lines that prove the slot resumed after the error. */
const RESUMED_LINE = /^\s*(?:[⏺●]\s*\S|[❯>]\s+\S)/;

const TAIL_LINES = 25;
const COMPOSER_PROMPT_LINE = /^\s*[❯>](\s|$)/;

export interface ApiStallClassification {
  stalled: boolean;
  signature: string | null;
  reason:
    | "stall"
    | "no_api_error"
    | "non_transient_api_error"
    | "retry_in_progress"
    | "resumed_after_error";
  /** Stable-ish fingerprint of this stall (error text + error-line count). */
  fingerprint: string | null;
  errorText: string | null;
}

export function classifyApiStallTail(pane: string): ApiStallClassification {
  let all = pane.replace(/\r/g, "").split("\n");
  // Ignore the live composer (last prompt line and below) so text typed or
  // pasted into it never reads as resumed work or changes the fingerprint.
  for (let i = all.length - 1; i >= 0; i--) {
    if (COMPOSER_PROMPT_LINE.test(all[i])) { all = all.slice(0, i); break; }
  }
  const nonBlank = all.filter((l) => l.trim().length > 0);
  const tail = nonBlank.slice(-TAIL_LINES);
  const none = (reason: ApiStallClassification["reason"]): ApiStallClassification => ({
    stalled: false, signature: null, reason, fingerprint: null, errorText: null,
  });

  let idx = -1;
  for (let i = tail.length - 1; i >= 0; i--) {
    if (API_ERROR_LINE.test(tail[i])) { idx = i; break; }
  }
  if (idx < 0) return none("no_api_error");

  // Join wrapped continuation lines (indented, no bullet) into the error text.
  let errorText = tail[idx].trim();
  let j = idx + 1;
  while (j < tail.length && /^\s{2,}\S/.test(tail[j]) && !RESUMED_LINE.test(tail[j])) {
    errorText += " " + tail[j].trim();
    j++;
  }
  const after = tail.slice(idx + 1);
  if (after.some((l) => API_RETRY_BANNER.test(l)) || API_RETRY_BANNER.test(errorText)) {
    return none("retry_in_progress");
  }
  if (tail.slice(j).some((l) => RESUMED_LINE.test(l))) return none("resumed_after_error");

  const sig = API_STALL_SIGNATURES.find((s) => s.pattern.test(errorText));
  if (!sig) return none("non_transient_api_error");

  const errorCount = all.filter((l) => API_ERROR_LINE.test(l)).length;
  return {
    stalled: true,
    signature: sig.name,
    reason: "stall",
    fingerprint: `${errorCount}:${errorText}`,
    errorText,
  };
}

export type ApiStallAction =
  | { kind: "none"; reason: string }
  | { kind: "wait"; remainingMs: number }
  | { kind: "nudge"; consecutive: number }
  | { kind: "cap_alert"; consecutive: number };

export interface ApiStallSlotStatus {
  state: "clear" | "stall_pending" | "nudged" | "capped";
  signature: string | null;
  first_seen_at: string | null;
  last_nudge_at: string | null;
  consecutive_nudges: number;
  assignment_epoch: number | null;
}

interface SlotTrack {
  epoch: number | null;
  episode: { fingerprint: string; signature: string; firstSeenMs: number; nudged: boolean } | null;
  lastNudgeMs: number | null;
  consecutive: number;
  capped: boolean;
}

/**
 * Per-slot stall policy:
 * - one nudge per stall episode, after a grace period;
 * - a stall that starts within RESTALL_WINDOW of the previous nudge extends
 *   the consecutive chain; otherwise the chain resets;
 * - once MAX consecutive nudges each re-stalled, stop nudging and alert PM
 *   exactly once, until the slot recovers (a stall outside the window) or the
 *   assignment changes.
 */
export class ApiStallTracker {
  private slots = new Map<number, SlotTrack>();

  constructor(
    private readonly graceMs = API_STALL_GRACE_MS,
    private readonly restallWindowMs = API_STALL_RESTALL_WINDOW_MS,
    private readonly maxConsecutive = API_STALL_MAX_CONSECUTIVE_NUDGES,
  ) {}

  private track(slot: number, epoch: number | null): SlotTrack {
    let t = this.slots.get(slot);
    if (!t || t.epoch !== epoch) {
      t = { epoch, episode: null, lastNudgeMs: null, consecutive: 0, capped: false };
      this.slots.set(slot, t);
    }
    return t;
  }

  observe(
    slot: number,
    nowMs: number,
    input: { occupied: boolean; turnIdle: boolean; epoch: number | null; classification: ApiStallClassification },
  ): ApiStallAction {
    if (!input.occupied) {
      this.slots.delete(slot);
      return { kind: "none", reason: "not_occupied" };
    }
    const t = this.track(slot, input.epoch);
    if (!input.turnIdle) {
      t.episode = null;
      return { kind: "none", reason: "turn_active" };
    }
    const c = input.classification;
    if (!c.stalled || !c.fingerprint || !c.signature) {
      t.episode = null;
      return { kind: "none", reason: c.reason };
    }
    if (!t.episode || t.episode.fingerprint !== c.fingerprint) {
      // New stall episode.
      const restall = t.lastNudgeMs !== null && nowMs - t.lastNudgeMs <= this.restallWindowMs;
      if (!restall) {
        t.consecutive = 0;
        t.capped = false;
      }
      t.episode = { fingerprint: c.fingerprint, signature: c.signature, firstSeenMs: nowMs, nudged: false };
    }
    const ep = t.episode;
    if (ep.nudged) return { kind: "none", reason: "already_nudged" };
    if (t.capped) return { kind: "none", reason: "capped" };
    if (t.consecutive >= this.maxConsecutive) {
      t.capped = true;
      return { kind: "cap_alert", consecutive: t.consecutive };
    }
    const elapsed = nowMs - ep.firstSeenMs;
    if (elapsed < this.graceMs) return { kind: "wait", remainingMs: this.graceMs - elapsed };
    return { kind: "nudge", consecutive: t.consecutive + 1 };
  }

  /** Call only after the nudge was actually delivered. */
  recordNudge(slot: number, nowMs: number): void {
    const t = this.slots.get(slot);
    if (!t?.episode) return;
    t.episode.nudged = true;
    t.lastNudgeMs = nowMs;
    t.consecutive += 1;
  }

  status(slot: number): ApiStallSlotStatus {
    const t = this.slots.get(slot);
    const iso = (ms: number | null | undefined) => (ms == null ? null : new Date(ms).toISOString());
    if (!t) {
      return { state: "clear", signature: null, first_seen_at: null, last_nudge_at: null, consecutive_nudges: 0, assignment_epoch: null };
    }
    const state: ApiStallSlotStatus["state"] = t.capped
      ? "capped"
      : t.episode
        ? (t.episode.nudged ? "nudged" : "stall_pending")
        : "clear";
    return {
      state,
      signature: t.episode?.signature ?? null,
      first_seen_at: iso(t.episode?.firstSeenMs),
      last_nudge_at: iso(t.lastNudgeMs),
      consecutive_nudges: t.consecutive,
      assignment_epoch: t.epoch,
    };
  }
}
