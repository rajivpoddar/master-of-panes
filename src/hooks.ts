/**
 * MoP Hook Processor — Business logic for incoming Claude Code HTTP hooks
 *
 * Receives hook payloads, logs them, detects significant events (slot idle,
 * plan ready), updates slot state, and relays notifications to PM.
 */

import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { execShell, sleep } from "./asyncCommand.js";
import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";
import type { HookPayload, HookResponse, SlotState } from "./types.js";

const PM_CLEAR_RETRY_SUPPRESS_MS = parseInt(
  process.env.MOP_PM_CLEAR_RETRY_SUPPRESS_MS ?? `${60 * 1000}`,
  10,
);
const PM_CLEAR_REQUESTED_AT_KEY = "pm_clear_requested_at";

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
  private pendingActiveTimers = new Map<number, ReturnType<typeof setTimeout>>();

  /** Idle debounce delay: 60 seconds (Rajiv directive 2026-05-15 12:02 IST thread `1778825746.293759`:
   * bumped from 30s → 60s to reduce duplicate slot-notification injection. Modern slots run longer
   * atomic tool sequences than 2026-04-01 baseline; 30s window fired during mid-sequence "pause"
   * causing 5+ FIRINGs in 30min on healthy slots. See feedback memo dated 2026-05-15.) */
  private static readonly IDLE_DEBOUNCE_MS = 60_000;
  private static readonly ACTIVE_DEBOUNCE_MS = 60_000;

  /**
   * Slot-idle staleness gate window — after the IDLE_DEBOUNCE_MS timer fires,
   * suppress /slot-idle if a Task/Agent subagent dispatched within this many
   * seconds and has not closed. Background Agent dispatches are closed by
   * TaskStop/subagent_completed, not by the parent prompt's Stop.
   *
   * Set to 60m because background Codex reviews can outlive the parent prompt
   * by many minutes; a visible idle notification during that window is false.
   */
  private static readonly IDLE_STALENESS_GATE_SEC = 60 * 60;

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
   * Codex/gh/tmux work for up to 30s per active slot; doing that inside this
   * inside this process starves /health and the hourly ops scheduler.
   */
  private static readonly CHECK_SLOT_BG_ENABLED =
    process.env.MOP_CHECK_SLOT_BG_ENABLED === "1";

  private static readonly SLOT_STATE_DIRECT_DEDUP_MS = 45_000;
  /**
   * Plain "MoP: slot N idle/active" PM injects are now opt-in only.
   * PM-wait reminder/check-slot inserts are also opt-in. Dev slots now use
   * message-pm for explicit PM-bound status and ESCALATION bodies for hard
   * blocks; background "still waiting for PM" pings add duplicate PM noise.
   */
  private static readonly SLOT_STATE_INJECTS_ENABLED =
    process.env.MOP_SLOT_STATE_INJECTS_ENABLED === "1";
  private static readonly PM_WAIT_REMINDERS_ENABLED =
    process.env.MOP_PM_WAIT_REMINDERS_ENABLED === "1";
  private static readonly WAITING_PM_CHECK_SLOT_INJECTS_ENABLED =
    process.env.MOP_WAITING_PM_CHECK_SLOT_INJECTS_ENABLED === "1";
  private lastDirectSlotStateNotificationAt = new Map<string, number>();
  private static readonly PM_WAIT_REMINDER_INTERVAL_MS = 5 * 60 * 1000;
  private static readonly PM_WAIT_REMINDER_SWEEP_MS = 60 * 1000;
  private static readonly PM_WAIT_ACTIVITIES = new Set([
    "awaiting_plan_approval",
    "waiting_for_pm_action",
    "waiting_for_pm_direction",
  ]);
  private pmWaitReminderTimers = new Map<number, ReturnType<typeof setTimeout>>();
  private pmWaitStartedAt = new Map<number, number>();
  private pmWaitReminderSweepTimer: ReturnType<typeof setInterval> | null = null;
  private promisedActionContinueAt = new Map<number, number>();
  private static readonly PROMISED_ACTION_CONTINUE_DEDUP_MS = 5 * 60 * 1000;
  private static readonly SLOT_PROMISE_AUDIT_SCRIPT =
    "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/slot-promised-action-audit.py";

  constructor(
    private db: MoPDatabase,
    private relay: TmuxRelay
  ) {
    if (HookProcessor.PM_WAIT_REMINDERS_ENABLED) {
      this.startPmWaitReminderSweep();
    }
  }

  private getPollMonitorSkipReason(slot: Pick<SlotState, "task" | "branch" | "activity">): string | null {
    const text = [slot.task, slot.branch, slot.activity]
      .filter((v): v is string => typeof v === "string" && v.length > 0)
      .join(" ");
    if (!text) return null;

    const patterns: Array<[RegExp, string]> = [
      [/\bci[-_ ]?watch\b/i, "ci-watch task"],
      [/\bwatch(?:ing|er)?\b/i, "watch task"],
      [/\bmonitor(?:ing|s)?\b/i, "monitor task"],
      [/\bpoll(?:ing|er)?\b/i, "poll task"],
      [/\bheartbeat\b/i, "heartbeat monitor task"],
      [/\bcheck\s+back\b/i, "check-back monitor task"],
      [/\bkeep\s+an\s+eye\b/i, "monitor task"],
    ];
    for (const [pattern, reason] of patterns) {
      if (pattern.test(text)) return reason;
    }
    return null;
  }

  private injectSlotStateDirect(
    slot: SlotState,
    state: "idle" | "active",
    source: string,
    toolName: string | null,
    payload: Record<string, unknown> = {}
  ): boolean {
    const eventBase = state === "idle" ? "slot_idle" : "slot_active";
    const pollMonitorReason = this.getPollMonitorSkipReason(slot);
    if (pollMonitorReason) {
      console.log(
        `[${state}-debug] slot ${slot.slot} suppress=${pollMonitorReason.replace(/\s+/g, "-")} task=${slot.task ?? "undefined"}`
      );
      this.db.logEvent(slot.slot, `${eventBase}_suppressed_poll_monitor`, source, toolName, {
        ...payload,
        reason: pollMonitorReason,
        task: slot.task,
        branch: slot.branch,
        activity: slot.activity,
      });
      return false;
    }

    if (state === "active") {
      const lastVisible = this.db.getLastVisibleSlotState(slot.slot);
      if (lastVisible?.state !== "idle") {
        this.db.logEvent(slot.slot, "slot_active_suppressed_no_visible_idle", source, toolName, {
          ...payload,
          reason: "last PM-visible slot state was not idle",
          last_visible_state: lastVisible?.state ?? null,
          last_visible_event: lastVisible?.eventType ?? null,
          last_visible_ts: lastVisible?.timestamp ?? null,
        });
        return false;
      }
    }

    const key = `${slot.slot}:${state}`;
    const nowMs = Date.now();
    const lastAt = this.lastDirectSlotStateNotificationAt.get(key);
    if (lastAt !== undefined && nowMs - lastAt < HookProcessor.SLOT_STATE_DIRECT_DEDUP_MS) {
      this.db.logEvent(slot.slot, `${eventBase}_suppressed_direct_dedup`, source, toolName, {
        ...payload,
        reason: "recent-direct-state-notification",
        age_ms: nowMs - lastAt,
        dedup_ms: HookProcessor.SLOT_STATE_DIRECT_DEDUP_MS,
      });
      return false;
    }
    this.lastDirectSlotStateNotificationAt.set(key, nowMs);

    const message = `MoP: slot ${slot.slot} ${state}`;
    if (state === "idle") {
      this.startPmWaitReminder(slot.slot, nowMs, true);
      if (HookProcessor.SLOT_STATE_INJECTS_ENABLED) {
        this.relay.notifySlotIdle(slot, true);
      } else {
        this.db.logEvent(slot.slot, "slot_idle_plain_inject_suppressed", source, toolName, {
          ...payload,
          reason: "plain slot-state PM injects disabled",
          pm_wait_reminder_enabled: HookProcessor.PM_WAIT_REMINDERS_ENABLED,
          task: slot.task,
          branch: slot.branch,
          issue: slot.issue,
        });
      }
    } else {
      this.stopPmWaitReminder(slot.slot, "slot-active");
      if (HookProcessor.SLOT_STATE_INJECTS_ENABLED) {
        this.relay.injectToPMDirect(message);
      } else {
        this.db.logEvent(slot.slot, "slot_active_plain_inject_suppressed", source, toolName, {
          ...payload,
          reason: "plain slot-state PM injects disabled",
          task: slot.task,
          branch: slot.branch,
          issue: slot.issue,
        });
      }
    }
    this.db.logEvent(slot.slot, `${eventBase}_notified`, source, toolName, {
      ...payload,
      relay_path: `${source}.direct.${state}`,
      notification_type: `slot-${state}`,
      pm_injected: HookProcessor.SLOT_STATE_INJECTS_ENABLED,
      task: slot.task,
      branch: slot.branch,
      issue: slot.issue,
      direct: true,
    });
    return true;
  }

  private startPmWaitReminder(slotNum: number, startedAt = Date.now(), reset = false): void {
    if (!HookProcessor.PM_WAIT_REMINDERS_ENABLED) {
      this.stopPmWaitReminder(slotNum, "pm-wait-reminders-disabled");
      this.db.logEvent(slotNum, "slot_idle_pm_wait_reminder_suppressed", "Timer", null, {
        reason: "pm-wait-reminders-disabled",
        started_at: new Date(startedAt).toISOString(),
        reset,
      });
      return;
    }

    const existing = this.pmWaitReminderTimers.get(slotNum);
    if (existing) {
      if (!reset) return;
      clearTimeout(existing);
      this.pmWaitReminderTimers.delete(slotNum);
    }
    this.pmWaitStartedAt.set(slotNum, startedAt);

    const schedule = (): void => {
      const timer = setTimeout(() => {
        if (this.sendPmWaitReminder(slotNum)) {
          schedule();
        }
      }, HookProcessor.PM_WAIT_REMINDER_INTERVAL_MS);
      if (timer.unref) timer.unref();
      this.pmWaitReminderTimers.set(slotNum, timer);
    };

    schedule();
    this.db.logEvent(slotNum, "slot_idle_pm_wait_reminder_started", "Timer", null, {
      interval_ms: HookProcessor.PM_WAIT_REMINDER_INTERVAL_MS,
      started_at: new Date(startedAt).toISOString(),
      reset,
    });

    if (Date.now() - startedAt >= HookProcessor.PM_WAIT_REMINDER_INTERVAL_MS) {
      const immediate = setTimeout(() => {
        this.sendPmWaitReminder(slotNum);
      }, 0);
      if (immediate.unref) immediate.unref();
    }
  }

  private slotWaitStartedAt(slot: Pick<SlotState, "last_activity">): number {
    const parsed = Date.parse(slot.last_activity);
    return Number.isFinite(parsed) ? parsed : Date.now();
  }

  private sendPmWaitReminder(slotNum: number): boolean {
    const slot = this.db.getSlot(slotNum);
    const waitingForPm =
      typeof slot?.activity === "string" &&
      HookProcessor.PM_WAIT_ACTIVITIES.has(slot.activity);
    if (!slot || !slot.occupied || (!slot.idle && !waitingForPm)) {
      this.stopPmWaitReminder(slotNum, "slot-no-longer-waiting");
      return false;
    }
    const pollMonitorReason = this.getPollMonitorSkipReason(slot);
    if (pollMonitorReason) {
      this.stopPmWaitReminder(slotNum, pollMonitorReason);
      return false;
    }
    const subagent = this.db.hasRecentSubagentDispatch(
      slotNum,
      HookProcessor.IDLE_STALENESS_GATE_SEC
    );
    if (subagent) {
      this.db.logEvent(slotNum, "slot_idle_pm_wait_reminder_suppressed_subagent_active", "Timer", null, {
        reason: "subagent_active",
        task_dispatch_ts: subagent.taskTs,
        tool_name: subagent.toolName ?? null,
        window_sec: HookProcessor.IDLE_STALENESS_GATE_SEC,
        task: slot.task,
        branch: slot.branch,
        issue: slot.issue,
      });
      return true;
    }

    const startedAt = this.pmWaitStartedAt.get(slotNum) ?? Date.now();
    const waitedMin = Math.max(1, Math.floor((Date.now() - startedAt) / 60_000));
    const isPlanApproval = slot.activity === "awaiting_plan_approval";
    const issuePart = slot.issue ? ` #${slot.issue}` : "";
    const message = isPlanApproval
      ? `MoP: slot ${slotNum} awaiting plan approval${issuePart} (${waitedMin}m)\n` +
        `\n` +
        `Run approve-plan for slot ${slotNum}, or send "2" so MoP routes through the approval endpoint. This is a WAITING_PM_ACTION re-fire; PM must approve/reject the plan or explicitly keep the wait open.`
      : `MoP: slot ${slotNum} idle — still waiting for PM input (${waitedMin}m)\n` +
        `\n` +
        `Run Skill(slot-idle) with arg ${slotNum} now. This is a WAITING_PM_ACTION re-fire; PM must either send a directive via mop_send_to_slot or explicitly mark the wait as valid.`;
    this.relay.injectToPMDirect(message);
    this.db.logEvent(slotNum, "slot_idle_pm_wait_reminder", "Timer", null, {
      waited_ms: Date.now() - startedAt,
      interval_ms: HookProcessor.PM_WAIT_REMINDER_INTERVAL_MS,
      task: slot.task,
      branch: slot.branch,
      issue: slot.issue,
      dnd: slot.dnd,
      direct: true,
    });
    return true;
  }

  private stopPmWaitReminder(slotNum: number, reason: string): void {
    const timer = this.pmWaitReminderTimers.get(slotNum);
    if (timer) {
      clearTimeout(timer);
      this.pmWaitReminderTimers.delete(slotNum);
      this.db.logEvent(slotNum, "slot_idle_pm_wait_reminder_stopped", "Timer", null, {
        reason,
      });
    }
    this.pmWaitStartedAt.delete(slotNum);
  }

  private async sendClearViaMopSendPath(slotNum: number, source: string): Promise<boolean> {
    try {
      const port = parseInt(process.env.MOP_PORT ?? "3100", 10);
      const res = await fetch(`http://127.0.0.1:${port}/slots/${slotNum}/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          command: "/clear",
          force: true,
          allow_pm_clear: slotNum === 0,
          source,
        }),
      });
      const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
      return res.ok && data.success === true;
    } catch {
      return false;
    }
  }

  private parseMoPIsoMs(value: string | null | undefined): number | null {
    if (!value) return null;
    const trimmed = value.trim();
    const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(trimmed) ? trimmed : `${trimmed}Z`;
    const ts = Date.parse(normalized);
    return Number.isFinite(ts) ? ts : null;
  }

  private isRecentIso(value: string | null, windowMs: number): boolean {
    const ts = this.parseMoPIsoMs(value);
    return ts !== null && Date.now() - ts >= 0 && Date.now() - ts < windowMs;
  }

  private hasRecentPmClearSend(windowMs: number): boolean {
    const cutoff = Date.now() - windowMs;
    return this.db
      .getEvents(0, 30)
      .some((event) => {
        if (
          ![
            "send_allowed_pm_clear_control_command",
            "clear_pending_queued",
            "clear_pending_pm_retry_sent",
          ].includes(event.event_type)
        ) {
          return false;
        }

        const ts = this.parseMoPIsoMs(event.timestamp);
        if (ts === null || ts < cutoff) return false;

        if (event.event_type === "send_allowed_pm_clear_control_command") {
          try {
            const payload = JSON.parse(event.payload) as { command?: unknown };
            return payload.command === "/clear";
          } catch {
            return false;
          }
        }

        return true;
      });
  }

  private detectPmDirectionRequest(transcript?: string): { summary: string; reason: string; issue: number | null } | null {
    const text = (transcript ?? "").trim();
    if (!text) return null;

    const issueMatch = /\b(?:PR|#)\s*#?(\d{3,6})\b/i.exec(text);
    const issue = issueMatch ? parseInt(issueMatch[1], 10) : null;
    const lines = text
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    const tail = lines.slice(-10).join(" ");

    const questionPatterns: Array<[RegExp, string]> = [
      [/\bwant me to\b/i, "slot asked whether to proceed"],
      [/\bshould I\b/i, "slot asked whether to proceed"],
      [/\bshall I\b/i, "slot asked whether to proceed"],
      [/\bdo you want me to\b/i, "slot asked whether to proceed"],
      [/\bwould you like me to\b/i, "slot asked whether to proceed"],
      [/\bneed(?:s)? PM (?:direction|decision|input)\b/i, "slot requested PM direction"],
      [/\bawaiting PM\b|\bwaiting for PM\b/i, "slot is waiting for PM"],
      [/\bconfirm(?: whether)?\b/i, "slot requested confirmation"],
      [/\bor\b.{0,120}\bsufficient\b/i, "slot asked PM to choose sufficiency"],
    ];

    for (const [pattern, reason] of questionPatterns) {
      if (pattern.test(tail) || pattern.test(text)) {
        return {
          summary: this.extractPmDirectionSummary(text),
          reason,
          issue,
        };
      }
    }

    if (/\bVerdict:\s*\*\*?FAIL\b/i.test(text) || /\bFAIL\s*\((?:critical|blocker|major)\)/i.test(text)) {
      if (/\b(correct fix needs|root cause|blocker|critical|requires|must)\b/i.test(text)) {
        return {
          summary: this.extractPmDirectionSummary(text, "critical QA fail needs PM dispatch/rework direction"),
          reason: "critical QA fail stopped at idle",
          issue,
        };
      }
    }

    return null;
  }

  private extractPmDirectionSummary(text: string, fallback = "PM direction needed"): string {
    const compact = text.replace(/\s+/g, " ").trim();
    const question = compact.match(/([^.!?]{0,220}\?)/);
    const marker = compact.match(/((?:Correct fix needs|However, Rajiv's directive|Given the local E2E|Root cause:|QA complete\. Verdict:)[^.!?]{0,260}[.!?]?)/i);
    const picked = (question?.[1] ?? marker?.[1] ?? fallback).trim();
    return picked.replace(/\s+/g, " ").slice(0, 220);
  }

  private startPmWaitReminderSweep(): void {
    if (this.pmWaitReminderSweepTimer) return;
    const run = async (): Promise<void> => {
      for (const slot of this.db.getAllSlots()) {
        if (!slot.occupied || this.getPollMonitorSkipReason(slot)) {
          this.stopPmWaitReminder(slot.slot, "slot-not-remindable");
          continue;
        }

        this.startCheckSlotTimer(slot.slot);

        let active = true;
        try {
          active = await this.relay.isSlotActive(slot.slot);
        } catch {
          active = true;
        }

        if (active) {
          const waitingForPm =
            typeof slot.activity === "string" &&
            HookProcessor.PM_WAIT_ACTIVITIES.has(slot.activity);
          if (waitingForPm) {
            this.startPmWaitReminder(slot.slot, this.slotWaitStartedAt(slot));
            continue;
          }
          if (slot.idle) {
            this.db.updateSlot(slot.slot, {
              idle: false,
              last_activity: new Date().toISOString(),
            });
          }
          this.stopPmWaitReminder(slot.slot, "slot-active-sweep");
          continue;
        }

        const subagent = this.db.hasRecentSubagentDispatch(
          slot.slot,
          HookProcessor.IDLE_STALENESS_GATE_SEC
        );
        if (subagent) {
          if (slot.idle) {
            this.db.updateSlot(slot.slot, {
              idle: false,
              last_activity: new Date().toISOString(),
            });
          }
          this.stopPmWaitReminder(slot.slot, "subagent-active-sweep");
          this.db.logEvent(slot.slot, "slot_idle_reconcile_suppressed_subagent_active", "Timer", null, {
            reason: "subagent_active",
            task_dispatch_ts: subagent.taskTs,
            tool_name: subagent.toolName ?? null,
            window_sec: HookProcessor.IDLE_STALENESS_GATE_SEC,
          });
          continue;
        }

        if (!slot.idle) {
          this.db.updateSlot(slot.slot, {
            idle: true,
            last_activity: new Date().toISOString(),
          });
          this.db.logEvent(slot.slot, "slot_idle_reconciled_from_pane", "Timer", null, {
            reason: "pm-wait-reminder-sweep",
          });
        }
        this.startPmWaitReminder(slot.slot, this.slotWaitStartedAt(slot));
      }
    };

    void run();
    this.pmWaitReminderSweepTimer = setInterval(() => {
      void run();
    }, HookProcessor.PM_WAIT_REMINDER_SWEEP_MS);
    if (this.pmWaitReminderSweepTimer.unref) this.pmWaitReminderSweepTimer.unref();
  }

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

    const timer = setInterval(async () => {
      const now = new Date().toISOString().slice(11, 19);
      // Only fire if slot is occupied.
      // Do NOT check idle — slots are idle between tool calls (every few seconds).
      // The timer should survive through normal idle↔active cycling.
      // Only stop when truly unoccupied (released). DND suppresses normal
      // PM-visible slot state noise, but must not suppress stuck/wait reminders.
      const currentSlot = this.db.getSlot(slotNum);
      console.log(`[check-slot] ${now} Slot ${slotNum} timer tick — occupied=${currentSlot?.occupied}, idle=${currentSlot?.idle}, dnd=${currentSlot?.dnd}, task=${currentSlot?.task?.slice(0,30)}`);
      if (!currentSlot || !currentSlot.occupied) {
        console.log(`[check-slot] ${now} Slot ${slotNum} STOPPING timer — occupied=${currentSlot?.occupied}, dnd=${currentSlot?.dnd}`);
        this.stopCheckSlotTimer(slotNum);
        return;
      }

      const pollMonitorReason = this.getPollMonitorSkipReason(currentSlot);
      if (pollMonitorReason) {
        console.log(`[check-slot] ${now} Slot ${slotNum} SKIPPED — ${pollMonitorReason}`);
        this.db.logEvent(slotNum, "check_slot_skipped", "Timer", null, {
          reason: pollMonitorReason,
          interval_ms: HookProcessor.CHECK_SLOT_INTERVAL_MS,
          task: currentSlot.task,
          branch: currentSlot.branch,
        });
        return;
      }

      // Skip firing if slot is momentarily idle (between tool calls), but keep timer alive
      if (currentSlot.idle) {
        console.log(`[check-slot] ${now} Slot ${slotNum} SKIP tick — idle between tool calls, timer stays alive`);
        this.startPmWaitReminder(slotNum, this.slotWaitStartedAt(currentSlot));
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
      // Default behavior: if the marker is absent, fall through to inject. If the
      // bg-script itself fails/times out, write diagnostics but do not notify PM;
      // a broken classifier should not create back-to-back slot noise.
      // (Rajiv directive 2026-05-08 20:34 IST — gate at MoP server, not PM-side.)
      const checkFile = `/tmp/slot-${slotNum}-check.txt`;
      const bgScript = `${process.env.HOME}/.claude/scripts/check-slot-bg.sh`;
      let bgOutput = "";
      let bgFailed = false;
      try {
        const result = await execShell(`bash ${bgScript} ${slotNum}`, {
          timeout: 30_000,
          maxBuffer: 1024 * 1024,
        });
        bgOutput = result.stdout;
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

      if (bgFailed) {
        console.log(`[check-slot] ${now} Slot ${slotNum} SKIPPED — bg-script failed; wrote ${checkFile}`);
        this.db.logEvent(slotNum, "check_slot_skipped", "Timer", null, {
          check_file: checkFile,
          reason: "bg-script-failed",
          interval_ms: HookProcessor.CHECK_SLOT_INTERVAL_MS,
          bg_failed: true,
        });
        return;
      }

      // Decide based on bg-script INJECT_DECISION marker.
      // skip → log + return (timer keeps running for next tick).
      // inject → fire injection.
      const skipMatch = bgOutput.match(/INJECT_DECISION:skip(?:\s+REASON:([^\n]*))?/);
      if (skipMatch) {
        const reason = (skipMatch[1] || "unspecified").trim().slice(0, 120);
        console.log(`[check-slot] ${now} Slot ${slotNum} SKIPPED — bg-script INJECT_DECISION:skip REASON:${reason}`);
        this.db.logEvent(slotNum, "check_slot_skipped", "Timer", null, {
          check_file: checkFile,
          reason,
          interval_ms: HookProcessor.CHECK_SLOT_INTERVAL_MS,
        });
        return;
      }

      console.log(`[check-slot] ${now} Slot ${slotNum} FIRING — bg-script INJECT_DECISION:inject`);

      // Inject only the check-slot signal. The bg-script output remains in
      // /tmp/slot-N-check.txt and the event log; PM should not receive the
      // full classifier payload inline.
      // Rajiv directive 2026-05-29: "disable this full check slot bg result
      // inject as well. just inject check slot message and time."
      // Rajiv directive 2026-05-22 22:23 IST thread `1779468118.901709`:
      // "inject is a string instead of a command." Switched from slash
      // command `/check-slot N` to message prefix `MoP: check slot N`.
      // PM-side pm-context-injector.sh recognizes the prefix and emits
      // [MoP_SLOT_NOTIFICATION] system-reminder hinting Skill(check-slot).
      const payload = bgOutput.trimEnd() || `STATUS:ERROR reason=empty-output slot=${slotNum}`;
      // Derive a one-line <reason> from the bg-script summary so PM sees
      // the headline status without re-parsing the payload. Order of
      // preference: WARNING line > NEXT_ACTION line > HEALTH line > STATUS
      // line > fallback "routine timer". Strips the marker prefix so the
      // reason reads cleanly inline.
      let reason = "routine timer";
      const warn = payload.match(/^WARNING:([^\n]+)/m);
      const next = payload.match(/^NEXT_ACTION:([^\n]+)/m);
      const health = payload.match(/^HEALTH:([^\n]+)/m);
      const status = payload.match(/^STATUS:([^\n]+)/m);
      if (warn) reason = `WARNING:${warn[1].trim()}`;
      else if (next) reason = `NEXT_ACTION:${next[1].trim()}`;
      else if (health) reason = `HEALTH:${health[1].trim()}`;
      else if (status) reason = `STATUS:${status[1].trim()}`;
      reason = reason.replace(/\s+/g, " ").slice(0, 120);
      if (/^STATUS:WAITING_PM_ACTION\b/m.test(payload)) {
        if (!HookProcessor.WAITING_PM_CHECK_SLOT_INJECTS_ENABLED) {
          this.db.updateSlot(slotNum, {
            status: "active",
            occupied: true,
            idle: true,
            activity: "waiting_for_pm_action",
          });
          this.stopPmWaitReminder(slotNum, "waiting-pm-check-slot-injects-disabled");
          this.db.logEvent(slotNum, "check_slot_waiting_pm_action_suppressed", "Timer", null, {
            check_file: checkFile,
            reason,
            task: currentSlot.task,
            branch: currentSlot.branch,
            issue: currentSlot.issue,
          });
          console.log(`[check-slot] ${now} Slot ${slotNum} SKIPPED — WAITING_PM_ACTION inserts disabled`);
          return;
        }
        this.db.updateSlot(slotNum, {
          status: "active",
          occupied: true,
          idle: true,
          activity: "waiting_for_pm_action",
        });
        this.startPmWaitReminder(slotNum);
        this.db.logEvent(slotNum, "slot_waiting_pm_action_from_check_slot", "Timer", null, {
          check_file: checkFile,
          reason,
          task: currentSlot.task,
          branch: currentSlot.branch,
          issue: currentSlot.issue,
        });
      }
      this.relay.injectToPM(`MoP: check slot ${slotNum}`);
      console.log(`[check-slot] Slot ${slotNum} — injected short MoP check-slot message (reason="${reason.slice(0,60)}", payload ${payload.length} chars kept in ${checkFile})`);
      this.db.logEvent(slotNum, "check_slot_triggered", "Timer", null, {
        check_file: checkFile,
        interval_ms: HookProcessor.CHECK_SLOT_INTERVAL_MS,
        bg_failed: bgFailed,
        reason,
        payload_chars: payload.length,
        inline: false,
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
  private async pollForPlanApprovalPrompt(
    slotNum: number,
    pending: { issueNum: number; planFile: string; isRevision?: boolean },
    attempt: number,
  ): Promise<void> {
    const MAX_ATTEMPTS = 12; // 12 × 5s = 60s
    const POLL_INTERVAL_MS = 5_000;

    // MUST use tmux capture-pane directly — NOT relay.captureOutput() which prefers
    // log-based output (pipe-pane). The plan approval prompt is rendered by Claude Code's
    // TUI directly to the terminal, NOT to stdout — so the log file never captures it.
    // (Bug fix 2026-03-18: polling timed out every time because captureOutput returned
    // log content without the TUI prompt.)
    let output = "";
    try {
      const raw = await execShell(`tmux capture-pane -t 0:0.${slotNum} -p -S -20`, { timeout: 5_000 });
      output = raw.stdout;
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
      void this.pollForPlanApprovalPrompt(slotNum, pending, attempt + 1);
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
   * Cancel pending active notification timer for a slot.
   * Called when a Stop/idle-prompt lands during the active debounce window.
   */
  cancelPendingActiveTimer(slotNum: number): boolean {
    const existing = this.pendingActiveTimers.get(slotNum);
    if (existing) {
      clearTimeout(existing);
      this.pendingActiveTimers.delete(slotNum);
      return true;
    }
    return false;
  }

  private startActiveDebounceTimer(slotNum: number, toolName: string | null): void {
    if (this.pendingActiveTimers.has(slotNum)) return;

    const timer = setTimeout(async () => {
      this.pendingActiveTimers.delete(slotNum);
      const currentSlot = this.db.getSlot(slotNum);
      if (!currentSlot || currentSlot.idle || currentSlot.dnd) {
        this.db.logEvent(slotNum, "slot_active_cancelled", "Timer", toolName, {
          reason: currentSlot?.idle ? "slot became idle during active debounce window" : "slot not active",
          debounce_ms: HookProcessor.ACTIVE_DEBOUNCE_MS,
        });
        return;
      }

      const lastVisible = this.db.getLastVisibleSlotState(slotNum);
      if (lastVisible?.state !== "idle") {
        this.db.logEvent(slotNum, "slot_active_cancelled", "Timer", toolName, {
          reason: "last PM-visible slot state was not idle",
          debounce_ms: HookProcessor.ACTIVE_DEBOUNCE_MS,
          last_visible_state: lastVisible?.state ?? null,
          last_visible_event: lastVisible?.eventType ?? null,
          last_visible_ts: lastVisible?.timestamp ?? null,
        });
        return;
      }

      const captureFile = `/tmp/slot-${slotNum}-active-capture.txt`;
      try {
        const capture = await execShell(
          `tmux capture-pane -t 0:0.${slotNum} -p -S -15`,
          { timeout: 5_000 }
        );
        writeFileSync(captureFile, capture.stdout);
      } catch {
        writeFileSync(captureFile, "[capture failed]");
      }

      this.injectSlotStateDirect(currentSlot, "active", "PostToolUse", toolName, {
        capture_file: captureFile,
        debounce_ms: HookProcessor.ACTIVE_DEBOUNCE_MS,
      });
    }, HookProcessor.ACTIVE_DEBOUNCE_MS);
    if (timer.unref) timer.unref();
    this.pendingActiveTimers.set(slotNum, timer);
    this.db.logEvent(slotNum, "slot_active_debounce_started", "PostToolUse", toolName, {
      debounce_ms: HookProcessor.ACTIVE_DEBOUNCE_MS,
    });
  }

  private shellQuote(value: string): string {
    return `'${value.replace(/'/g, `'\\''`)}'`;
  }

  private async maybeContinuePromisedActionMiss(
    slotNum: number,
    currentSlot: SlotState,
    source: "Stop" | "Notification",
    payload?: HookPayload
  ): Promise<boolean> {
    if (currentSlot.dnd || !currentSlot.occupied) return false;

    const transcriptPath = payload?.transcript_path;
    if (!transcriptPath) {
      this.db.logEvent(slotNum, "slot_promised_action_scan_skipped", source, null, {
        reason: "missing_transcript_path",
      });
      return false;
    }

    const nowMs = Date.now();
    const lastAt = this.promisedActionContinueAt.get(slotNum);
    if (lastAt !== undefined && nowMs - lastAt < HookProcessor.PROMISED_ACTION_CONTINUE_DEDUP_MS) {
      this.db.logEvent(slotNum, "slot_promised_action_continue_suppressed", source, null, {
        reason: "dedup",
        age_ms: nowMs - lastAt,
        dedup_ms: HookProcessor.PROMISED_ACTION_CONTINUE_DEDUP_MS,
        transcript_path: transcriptPath,
      });
      return true;
    }

    let stdout = "";
    try {
      const cwd = payload?.cwd ?? `/Users/rajiv/Downloads/projects/heydonna-app-300${slotNum}`;
      const cmd = [
        "python3",
        this.shellQuote(HookProcessor.SLOT_PROMISE_AUDIT_SCRIPT),
        "stop",
        "--transcript",
        this.shellQuote(transcriptPath),
        "--cwd",
        this.shellQuote(cwd),
      ].join(" ");
      const result = await execShell(cmd, { timeout: 3_000, maxBuffer: 16 * 1024 });
      stdout = result.stdout.trim();
    } catch (err) {
      this.db.logEvent(slotNum, "slot_promised_action_scan_failed", source, null, {
        error: String(err),
        transcript_path: transcriptPath,
      });
      return false;
    }

    if (!stdout.startsWith("BLOCK\t")) return false;

    this.promisedActionContinueAt.set(slotNum, nowMs);
    const reason = stdout.slice("BLOCK\t".length).slice(0, 1000);
    this.relay.sendToSlot(slotNum, "continue your work", true);
    this.db.logEvent(slotNum, "slot_promised_action_continue_injected", source, null, {
      reason,
      command: "continue your work",
      transcript_path: transcriptPath,
      task: currentSlot.task,
      branch: currentSlot.branch,
      issue: currentSlot.issue,
      dedup_ms: HookProcessor.PROMISED_ACTION_CONTINUE_DEDUP_MS,
    });
    console.log(`[idle-debug] slot ${slotNum} promised-action miss detected — injected continue your work`);
    return true;
  }

  private startIdleDebounceTimer(slotNum: number, slot: SlotState, source: "Stop" | "Notification", payload?: HookPayload): void {
    this.cancelPendingIdleTimer(slotNum);

    const idleTimer = setTimeout(async () => {
      this.pendingIdleTimers.delete(slotNum);
      const currentSlot = this.db.getSlot(slotNum);
      console.log(`[idle-debug] slot ${slotNum} debounce fired: occupied=${currentSlot?.occupied}, idle=${currentSlot?.idle}, dnd=${currentSlot?.dnd}, task=${currentSlot?.task ?? 'undefined'}`);
      if (currentSlot?.idle) {
        const subagent = this.db.hasRecentSubagentDispatch(
          slotNum,
          HookProcessor.IDLE_STALENESS_GATE_SEC
        );
        if (subagent) {
          console.log(
            `[idle-debug] slot ${slotNum} STALENESS GATE — subagent ${subagent.toolName ?? "tool"} dispatched at ${subagent.taskTs} is still active; suppressing /slot-idle`
          );
          this.db.logEvent(slotNum, "slot_idle_suppressed_subagent_active", "Timer", null, {
            relay_path: `${source}.debounce.gate`,
            reason: "subagent_active_post_debounce",
            task_dispatch_ts: subagent.taskTs,
            tool_name: subagent.toolName ?? null,
            window_sec: HookProcessor.IDLE_STALENESS_GATE_SEC,
          });
          return;
        }

        const lastTool = this.db.getLastToolFire(
          slotNum,
          HookProcessor.IDLE_RECENT_TOOL_GATE_SEC
        );
        if (lastTool) {
          console.log(
            `[idle-debug] slot ${slotNum} STALENESS GATE — last tool ${lastTool.tool}@${lastTool.timestamp} within ${HookProcessor.IDLE_RECENT_TOOL_GATE_SEC}s; suppressing /slot-idle`
          );
          this.db.logEvent(slotNum, "slot_idle_suppressed_recent_tool", "Timer", null, {
            relay_path: `${source}.debounce.gate`,
            reason: "recent_tool_post_debounce",
            last_tool: lastTool.tool,
            last_tool_ts: lastTool.timestamp,
            window_sec: HookProcessor.IDLE_RECENT_TOOL_GATE_SEC,
          });
          return;
        }

        if (await this.maybeContinuePromisedActionMiss(slotNum, currentSlot, source, payload)) {
          return;
        }

        const lastVisible = this.db.getLastVisibleSlotState(slotNum);
        if (lastVisible?.state === "idle") {
          console.log(
            `[idle-debug] slot ${slotNum} already PM-visible idle since ${lastVisible.timestamp}; suppressing duplicate /slot-idle`
          );
          this.db.logEvent(slotNum, "slot_idle_suppressed_already_visible", "Timer", null, {
            relay_path: `${source}.debounce.gate`,
            reason: "last PM-visible slot state was already idle",
            last_visible_state: lastVisible.state,
            last_visible_event: lastVisible.eventType,
            last_visible_ts: lastVisible.timestamp,
          });
          return;
        }

        console.log(`[idle-debug] slot ${slotNum} still idle after debounce — preparing /slot-idle relay`);
        const captureFile = `/tmp/slot-${slotNum}-idle-capture.txt`;
        try {
          const capture = await execShell(
            `tmux capture-pane -t 0:0.${slotNum} -p -S -30`,
            { timeout: 5_000 }
          );
          writeFileSync(captureFile, capture.stdout);
        } catch {
          writeFileSync(captureFile, "[capture failed]");
        }

        console.log(`[idle-debug] slot ${slotNum} direct slot-idle notification candidate`);
        this.injectSlotStateDirect(currentSlot, "idle", source, null, {
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

    this.db.logEvent(slotNum, "slot_idle_debounce_started", source, null, {
      task: slot.task,
      branch: slot.branch,
      debounce_ms: HookProcessor.IDLE_DEBOUNCE_MS,
    });
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
  async process(slotNum: number, payload: HookPayload): Promise<HookResponse> {
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

    const isIdlePromptNotification =
      payload.type === "Notification" &&
      payload.notification_type === "idle_prompt";
    const isPermissionPromptNotification =
      payload.type === "Notification" &&
      payload.notification_type === "permission_prompt";

    // Update last_activity and mark busy/idle from runtime state.
    // SessionStart / PreCompact / PostCompact don't change idle flag — they're
    // lifecycle events that don't reflect work-in-progress state.
    const lifecycleEvents = new Set([
      "SessionStart",
      "PreCompact",
      "PostCompact",
    ]);
    if (!lifecycleEvents.has(payload.type)) {
      const idle = payload.type === "Stop" || isIdlePromptNotification || isPermissionPromptNotification;
      this.db.updateSlot(slotNum, {
        last_activity: new Date().toISOString(),
        idle,
      });
      if (!idle) {
        this.stopPmWaitReminder(slotNum, `${payload.type}-active`);
      }
    } else {
      this.db.updateSlot(slotNum, {
        last_activity: new Date().toISOString(),
      });
    }

    // Route by hook type
    switch (payload.type) {
      case "Stop":
        return await this.handleStop(slotNum, payload);
      case "PostToolUse":
        return await this.handlePostToolUse(slotNum, payload, wasIdle);
      case "PreToolUse":
        return this.handlePreToolUse(slotNum, payload);
      case "Notification":
        return await this.handleNotification(slotNum, payload);
      case "UserPromptSubmit":
        // User sent a message — slot is becoming active. Handle like PostToolUse
        // for idle→active transition detection. (Added 2026-03-18)
        return await this.handlePostToolUse(slotNum, payload, wasIdle);
      case "SessionStart":
        return await this.handleSessionStart(slotNum, payload);
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
  private async handleSessionStart(slotNum: number, payload: HookPayload): Promise<HookResponse> {
    const source = payload.source ?? "";
    debugLog(
      `[hooks] SessionStart slot=${slotNum} source=${source || "(none)"}`
    );
    if (slotNum === 0) {
      if (source === "clear") {
        const confirmedAt = new Date().toISOString();
        const wasPending = this.db.hasPendingClear(0);
        this.db.releaseSlot(0);
        this.db.clearPendingClear(0);
        this.db.setConfig("pm_clear_confirmed_at", confirmedAt);
        this.db.logEvent(0, "clear_pending_executed", "SessionStart", null, {
          name: "PM",
          source,
          was_pending: wasPending,
          confirmed_at: confirmedAt,
          delivery: "mop_send_to_slot",
          reason: "PM SessionStart source=clear acknowledged requested /clear",
        });
        this.db.logEvent(0, "slot_cleared", "SessionStart", null, {
          name: "PM",
          cleared_at: confirmedAt,
          immediate: false,
          confirmed: true,
          via: "SessionStart:clear",
          delivery: "mop_send_to_slot",
        });
        debugLog(`[hooks] SessionStart:clear slot=0 (PM) — clear acknowledged`);
        return {};
      }
      // PM pane — compact sessions are now passive from MoP's perspective.
      // Record the compact event for auditing/dedup, but do not auto-inject
      // "continue your work" into the PM pane anymore.
      if (source === "compact") {
        this.db.logEvent(0, "session_start_compact", "SessionStart", null, {
          source,
          via: "no_auto_continue",
        });
        debugLog(`[hooks] SessionStart:compact slot=0 (PM) — no auto-continue`);
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

  private async handleStop(slotNum: number, payload: HookPayload): Promise<HookResponse> {
    // PM (slot 0) is not represented in the slots table, so handle its pending
    // clear latch before the normal slot-row lookup. Without this, a PM clear
    // request can be logged as queued but never retried when PM reaches Stop.
    if (slotNum === 0 && this.db.hasPendingClear(0)) {
      try {
        const requestedAt = this.db.getConfig(PM_CLEAR_REQUESTED_AT_KEY);
        if (
          this.isRecentIso(requestedAt, PM_CLEAR_RETRY_SUPPRESS_MS) ||
          this.hasRecentPmClearSend(PM_CLEAR_RETRY_SUPPRESS_MS)
        ) {
          this.db.logEvent(0, "clear_pending_pm_retry_suppressed", "Stop", null, {
            name: "PM",
            reason: "PM clear is already pending and a /clear was recently sent; suppressing duplicate retry",
            requested_at: requestedAt,
            suppress_window_ms: PM_CLEAR_RETRY_SUPPRESS_MS,
          });
          return {};
        }

        const sent = await this.sendClearViaMopSendPath(0, "pm_clear_pending_stop_hook");
        if (!sent) {
          throw new Error("MoP send path failed for PM queued /clear");
        }
        this.db.logEvent(0, "clear_pending_pm_retry_sent", "Stop", null, {
          name: "PM",
          reason: "PM reached Stop while clear_pending_0=true; resent /clear through MoP clear path",
        });
      } catch (err) {
        this.db.logEvent(0, "clear_pending_failed", "Stop", null, {
          name: "PM",
          error: String(err),
        });
      }
      return {};
    }

    const slot = this.db.getSlot(slotNum);
    if (!slot) return {};

    // Skip if DND — slot is under Rajiv's control
    if (slot.dnd) {
      this.db.logEvent(slotNum, "stop_skipped_dnd", "Stop", null, {
        reason: "slot is DND",
      });
      return {};
    }

    const hadPendingActive = this.cancelPendingActiveTimer(slotNum);
    if (hadPendingActive) {
      this.db.logEvent(slotNum, "slot_active_debounce_cancelled", "Stop", null, {
        reason: "slot became idle during active debounce window",
        debounce_ms: HookProcessor.ACTIVE_DEBOUNCE_MS,
      });
    }

    // Check if slot was awaiting plan approval.
    // The plan approval prompt (numbered choices) renders AFTER Stop fires,
    // so we poll for it before notifying PM. (Rajiv directive 2026-03-18)
    if (slot.activity === "awaiting_plan_approval") {
      const pending = this.pendingPlanReady.get(slotNum);
      if (pending) {
        // Start polling for the plan approval prompt before notifying PM.
        this.pendingPlanReady.delete(slotNum);
        void this.pollForPlanApprovalPrompt(slotNum, pending, 0);
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

    const pmDirection = this.detectPmDirectionRequest(payload.transcript);
    if (pmDirection) {
      const taskLabel = slot.task ?? `PM direction needed${pmDirection.issue ? ` #${pmDirection.issue}` : ""}`;
      if (!slot.occupied) {
        this.db.assignSlot(
          slotNum,
          taskLabel,
          pmDirection.issue,
          slot.branch,
          payload.session_id ?? slot.session_id,
          slot.pr,
          slot.head_sha,
          slot.assignment_epoch
        );
      }
      this.db.updateSlot(slotNum, {
        status: "active",
        occupied: true,
        idle: true,
        task: taskLabel,
        issue: slot.issue ?? pmDirection.issue,
        activity: "waiting_for_pm_direction",
      });

      const recent = this.db.getEvents(slotNum, 1, "slot_pm_direction_needed")[0];
      const recentMs = recent ? Date.parse(`${recent.timestamp}Z`) : NaN;
      const suppressDirect = Number.isFinite(recentMs) && Date.now() - recentMs < 3 * 60_000;
      const pmWaitDirectEnabled = HookProcessor.PM_WAIT_REMINDERS_ENABLED;
      if (pmWaitDirectEnabled && !suppressDirect) {
        this.relay.injectToPMDirect(
          `MoP: slot ${slotNum} idle — waiting for PM direction: ${pmDirection.summary}`
        );
      }
      this.startPmWaitReminder(slotNum, Date.now(), true);
      this.db.logEvent(slotNum, "slot_pm_direction_needed", "Stop", null, {
        summary: pmDirection.summary,
        reason: pmDirection.reason,
        issue: pmDirection.issue,
        task: taskLabel,
        was_occupied: slot.occupied,
        direct_injected: pmWaitDirectEnabled && !suppressDirect,
        suppressed_reason: pmWaitDirectEnabled ? (suppressDirect ? "recent-duplicate" : null) : "pm-wait-reminders-disabled",
      });
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
    // If this slot has a pending clear (from a MoP clear command), send /clear now
    // that the slot is idle, then release it. Similar pattern to exit_pending.
    // Rajiv directive 2026-04-04: "if idle trigger immediately or wait till next idle"
    if (this.db.hasPendingClear(slotNum)) {
      try {
        const sent = await this.sendClearViaMopSendPath(slotNum, "clear_pending_stop_hook");
        if (!sent) {
          throw new Error("MoP send path failed for queued /clear");
        }

        // Release slot state (slots 1-4 only)
        if (slotNum >= 1 && slotNum <= 4) {
          this.db.releaseSlot(slotNum, slot.assignment_epoch);
        }

        this.db.clearPendingClear(slotNum);
        this.db.logEvent(slotNum, "clear_pending_executed", "Stop", null, {
          name: slot.name,
          reason: "Slot went idle — executing queued /clear from MoP clear command",
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
    this.startIdleDebounceTimer(slotNum, slot, "Stop", payload);

    // Auto-release slot if POST-PR (a PR exists for current branch).
    // Frees the slot immediately — PM still gets notification for CI watch/labels.
    if (slot.branch && slot.branch !== "main") {
      try {
        const prResult = await execShell(
          `gh pr list --head "${slot.branch}" --json number --jq '.[0].number'`,
          { timeout: 10_000 }
        );
        const prNum = prResult.stdout.trim();

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

  private async handlePostToolUse(slotNum: number, payload: HookPayload, wasIdle?: boolean): Promise<HookResponse> {
    // ─── Active Notification (idle → active transition) ──────
    // First PostToolUse after a Stop means the slot became active.
    // Notify PM so they know not to send new work to this slot.
    // Uses wasIdle from process() — captured BEFORE updateSlot clears idle flag.
    const slot = this.db.getSlot(slotNum);
    if (
      slot?.activity &&
      HookProcessor.PM_WAIT_ACTIVITIES.has(slot.activity)
    ) {
      this.stopPmWaitReminder(slotNum, "slot-resumed-from-pm-wait");
      this.db.updateSlot(slotNum, { activity: null });
    }
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
        // Active notifications are debounced just like idle notifications.
        // A quick tool blip after an idle prompt must not produce an immediate
        // idle→active PM-visible flip. Stop/idle-prompt cancels this timer.
        this.startActiveDebounceTimer(slotNum, payload.tool_name ?? null);
      }

      // ─── Start check-slot periodic timer ────────────────
      // Rajiv directive 2026-04-03: MoP drives check-slot instead of PM cron loop
      // Only start timer if slot is occupied (has a task). Released slots in auto-compact
      // cycles trigger active transitions but should NOT get timers. DND does not stop
      // watchdogs; it only suppresses normal slot-state notifications.
      if (slot.occupied && !this.getPollMonitorSkipReason(slot)) {
        this.startCheckSlotTimer(slotNum);
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
          this.db.assignSlot(slotNum, taskLabel, issueNum, null, null, null, null, slot?.assignment_epoch ?? 0);
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

    // ─── Native Plan Mode Guard ───────────────────────────────
    // Dev slots must use plan-agent + codex-plan-reviewer. If EnterPlanMode
    // leaks past Claude PreToolUse settings, record it as a workflow violation.

    if (payload.tool_name === "EnterPlanMode") {
      const slot = this.db.getSlot(slotNum);
      const reason =
        "PLAN_MODE_VIOLATION: native Claude Plan Mode is blocked for dev slots; use foreground plan-agent, then codex-plan-reviewer.";
      this.db.updateSlot(slotNum, { activity: "plan_mode_violation" });
      this.db.logEvent(slotNum, "plan_mode_violation", "PostToolUse", "EnterPlanMode", {
        reason,
        issue: slot?.issue,
        task: slot?.task,
        branch: slot?.branch,
      });
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
        this.db.assignSlot(slotNum, taskLabel, issueNum, null, null, null, null, slot?.assignment_epoch ?? 0);
        this.db.logEvent(slotNum, "auto_assigned_ask_user", "PostToolUse", "AskUserQuestion", {
          issue: issueNum,
          reason: "AskUserQuestion detected but slot not occupied",
        });
      }
    }

    return {};
  }

  // ─── PreToolUse Hook ───────────────────────────────────

  private handlePreToolUse(slotNum: number, payload: HookPayload): HookResponse {
    if (payload.tool_name === "EnterPlanMode") {
      const slot = this.db.getSlot(slotNum);
      const reason =
        "PLAN_MODE_BLOCKED: Dev slots must not use native Claude Plan Mode / EnterPlanMode. Use foreground plan-agent, then codex-plan-reviewer.";
      this.db.updateSlot(slotNum, { activity: "plan_mode_violation" });
      this.db.logEvent(slotNum, "plan_mode_blocked_pretool", "PreToolUse", "EnterPlanMode", {
        reason,
        issue: slot?.issue,
        task: slot?.task,
        branch: slot?.branch,
      });
      return { blocked: true, reason, message: reason };
    }

    return {};
  }

  // ─── Notification Hook ─────────────────────────────────

  private async handleNotification(slotNum: number, payload: HookPayload): Promise<HookResponse> {
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
      const hadPendingActive = this.cancelPendingActiveTimer(slotNum);
      if (hadPendingActive) {
        this.db.logEvent(slotNum, "slot_active_debounce_cancelled", "Notification", null, {
          reason: "idle_prompt during active debounce window",
          debounce_ms: HookProcessor.ACTIVE_DEBOUNCE_MS,
        });
      }

      if (slot?.occupied) {
        // idle_prompt is a strong idle signal, but it still has to dwell before
        // becoming a PM-visible state transition. The debounce callback applies
        // the same subagent/recent-tool gates as Stop.
        this.startIdleDebounceTimer(slotNum, slot, "Notification", payload);
      } else {
        console.log(`[idle-debug] slot ${slotNum} Notification(idle_prompt) — skipping (occupied=${slot?.occupied}, dnd=${slot?.dnd})`);
      }
    }

    if (notifType === "permission_prompt") {
      const hadPendingActive = this.cancelPendingActiveTimer(slotNum);
      if (hadPendingActive) {
        this.db.logEvent(slotNum, "slot_active_debounce_cancelled", "Notification", null, {
          reason: "permission_prompt during active debounce window",
          debounce_ms: HookProcessor.ACTIVE_DEBOUNCE_MS,
        });
      }

      if (slot?.occupied && !slot.dnd) {
        const recentEvents = this.db.getEvents(slotNum, 25);
        const recentNativePlanMode = recentEvents.some(
          (event) => event.tool_name === "EnterPlanMode" || event.event_type === "plan_mode_violation"
        );
        if (recentNativePlanMode) {
          const reason =
            "PLAN_MODE_PERMISSION_PROMPT_SUPPRESSED: native Claude Plan Mode prompt leaked through; do not start PM plan approval flow. Slot must exit and use foreground plan-agent + codex-plan-reviewer.";
          this.db.updateSlot(slotNum, { activity: "plan_mode_violation" });
          this.db.logEvent(slotNum, "plan_mode_permission_prompt_suppressed", "Notification", null, {
            notification_type: notifType,
            issue: slot.issue,
            task: slot.task,
            branch: slot.branch,
            reason,
          });
          return {};
        }

        this.clearPlanApprovalTimer(slotNum);
        this.db.updateSlot(slotNum, {
          status: "active",
          occupied: true,
          idle: true,
          activity: "awaiting_plan_approval",
        });
        this.relay.notifyPlanApprovalNeeded(slotNum, slot.issue ?? 0);
        this.startPlanApprovalTimer(slotNum, slot.issue ?? 0);
        this.startPmWaitReminder(slotNum, Date.now(), true);
        this.db.logEvent(slotNum, "slot_plan_approval_from_permission_prompt", "Notification", null, {
          notification_type: notifType,
          issue: slot.issue,
          task: slot.task,
          branch: slot.branch,
          direct: true,
        });
      } else {
        console.log(`[idle-debug] slot ${slotNum} Notification(permission_prompt) — skipping (occupied=${slot?.occupied}, dnd=${slot?.dnd})`);
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
