/**
 * MoP PM Cadence Scheduler
 *
 * Owns PM scheduled cadence that used to be split across launchd scripts:
 * - 3h heartbeat: queues a normal PM prompt to invoke Skill(heartbeat-tasks)
 * - daily morning brief: queues a normal PM prompt to invoke Skill(morning-brief)
 * - daily growth report (10:30 local): queues a PM prompt to invoke Skill(growth-daily)
 * - 1h hourly heartbeat (at :13 local): queues a normal PM prompt to invoke
 *   Skill(hourly-heartbeat). It replaces the PM session-only "HOURLY OPEN-PR
 *   DRIVE" cron (Rajiv 2026-09-29 11:02 IST, thread C0ALZJHGE49/1790659381.537339).
 *
 * launchd remains the MoP watchdog only. These ticks are persisted in MoP DB
 * config keys so restarts do not double-fire within the same cadence bucket.
 */

import { closeSync, fstatSync, openSync, readSync, readFileSync } from "node:fs";

import { homedir } from "node:os";
import { join } from "node:path";

import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";

export type PMCadenceTaskName = "heartbeat" | "morning-brief" | "hourly-heartbeat" | "growth-daily" | "lifecycle-email" | "daily-pm-restart" | "rtb-split-update";
export type PMCadenceTriggerReason = "scheduled" | "manual" | "boot";

type PMCadenceTask = {
  name: PMCadenceTaskName;
  label: string;
  configPrefix: string;
  commandDescription: string;
};

type PMCadenceRunResult = {
  task: PMCadenceTaskName;
  triggered: boolean;
  reason: PMCadenceTriggerReason;
  due_key: string;
  injected: boolean;
  queued: boolean;
  message: string;
};

const HEARTBEAT_TASK: PMCadenceTask = {
  name: "heartbeat",
  label: "3h heartbeat",
  configPrefix: "pm_cadence_heartbeat",
  commandDescription: "MoP: run Skill(heartbeat-tasks) now with a background agent.",
};

const MORNING_BRIEF_TASK: PMCadenceTask = {
  name: "morning-brief",
  label: "morning brief",
  configPrefix: "pm_cadence_morning_brief",
  commandDescription:
    "MoP: morning brief due\n\n" +
    "Invoke Skill(morning-brief) now. Launch the morning-brief background agent with run_in_background=true; the agent owns evidence gathering and Slack posting. Do not run /morning-brief inline.",
};

// Rajiv C0ALZJHGE49 thread 1793299570.404429: every 30 minutes until stopped.
// Normal PM prompt via the hourly-heartbeat delivery path, never a slash command.
const RTB_SPLIT_UPDATE_TASK: PMCadenceTask = {
  name: "rtb-split-update",
  label: "remote-box split update (:13/:43 local)",
  configPrefix: "pm_cadence_rtb_split_update",
  commandDescription: "MoP: post the 30-min remote-box split update to C0ALZJHGE49 thread 1793299570.404429 via a bg agent: RTB candidate state, pool usage, cx33 status, next step.",
};

const HOURLY_HEARTBEAT_TASK: PMCadenceTask = {
  name: "hourly-heartbeat",
  label: "1h heartbeat",
  configPrefix: "pm_cadence_hourly_heartbeat",
  commandDescription: "MoP: run Skill(hourly-heartbeat) now with a background agent.",
};

// Rajiv C0C7U35LBKR thread 1792821492.101599, ts 1792823200.845199: "add it at
// 10:30 am each day". Fires once per local (IST) day from 10:30, like morning-brief.
const GROWTH_DAILY_TASK: PMCadenceTask = {
  name: "growth-daily",
  label: "growth daily",
  configPrefix: "pm_cadence_growth_daily",
  commandDescription:
    "MoP: growth report due. Invoke Skill(growth-daily) now: launch the growth background agent with run_in_background=true (Mondays: Skill(growth-weekly)). Do not run it inline.",
};

// Rajiv approved templates: C0ALZJHGE49 ts 1793088244.818019.
const LIFECYCLE_EMAIL_TASK: PMCadenceTask = {
  name: "lifecycle-email",
  label: "lifecycle email (weekdays 10:00 ET)",
  configPrefix: "pm_cadence_lifecycle_email",
  commandDescription: "MoP: lifecycle email due. Invoke Skill(lifecycle-email-processing) now. Launch the lifecycle-email-agent background agent with run_in_background=true; preserve the selector send_allowed gate. Do not run it inline.",
};

export function lifecycleEmailApproved(root = join(homedir(), ".claude/skills/lifecycle-email-processing/templates")): boolean {
  return ["D0", "D1", "D3", "D7", "FIRST_FILE_READY"].every((touch) => {
    try {
      return /^STATUS:\s*APPROVED(?:\s|$)/m.test(readFileSync(join(root, `${touch}.txt`), "utf8"));
    } catch {
      return false;
    }
  });
}

export function lifecycleEmailParts(now: Date): { day: string; hour: number; weekday: string } {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", hourCycle: "h23", weekday: "short",
  }).formatToParts(now).map((p) => [p.type, p.value]));
  return { day: `${parts.year}-${parts.month}-${parts.day}-ET`, hour: Number(parts.hour), weekday: parts.weekday };
}

export function isLifecycleEmailWindow(now: Date): boolean {
  const et = lifecycleEmailParts(now);
  return et.weekday !== "Sat" && et.weekday !== "Sun" && et.hour >= 10;
}

const DAILY_PM_RESTART_TASK: PMCadenceTask = {
  name: "daily-pm-restart",
  label: "daily PM restart (04:00 IST)",
  configPrefix: "pm_cadence_daily_pm_restart",
  commandDescription: "MoP: run Skill(daily-pm-restart)",
};

export function dailyPMRestartParts(now: Date): { day: string; hour: number } {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", hourCycle: "h23",
  }).formatToParts(now).map((p) => [p.type, p.value]));
  return { day: `${parts.year}-${parts.month}-${parts.day}-IST`, hour: Number(parts.hour) };
}

export function isDailyPMRestartWindow(now: Date): boolean {
  // No midday boot catch-up: only the 04:00 hour can enqueue a new occurrence.
  return dailyPMRestartParts(now).hour === 4;
}

const TASKS: Record<PMCadenceTaskName, PMCadenceTask> = {
  heartbeat: HEARTBEAT_TASK,
  "morning-brief": MORNING_BRIEF_TASK,
  "hourly-heartbeat": HOURLY_HEARTBEAT_TASK,
  "rtb-split-update": RTB_SPLIT_UPDATE_TASK,
  "growth-daily": GROWTH_DAILY_TASK,
  "lifecycle-email": LIFECYCLE_EMAIL_TASK,
  "daily-pm-restart": DAILY_PM_RESTART_TASK,
};

const CFG_GLOBAL_PAUSED = "pm_cadence_paused";
const LOCAL_MORNING_BRIEF_HOUR = 10;
const GROWTH_DAILY_HOUR = 10;
const GROWTH_DAILY_MINUTE = 30;
// Offset from the top of the hour so the hourly post never races the 3h heartbeat.
const HOURLY_HEARTBEAT_MINUTE = 13;

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function localDayKey(now: Date): string {
  return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
}

function heartbeatDueKey(now: Date): string {
  // Local 3h buckets: 00-02, 03-05, 06-08, 09-11, 12-14, 15-17, 18-20, 21-23.
  return `${localDayKey(now)}:${Math.floor(now.getHours() / 3)}`;
}

export function hourlyHeartbeatDueKey(now: Date): string {
  return `${localDayKey(now)}:${pad2(now.getHours())}`;
}

export function isHourlyHeartbeatWindow(now: Date): boolean {
  return now.getMinutes() >= HOURLY_HEARTBEAT_MINUTE;
}

function rtbSplitUpdateDueKey(now: Date): string {
  // Shift the half-hour buckets by 13 minutes; before :13 belongs to the
  // previous hour's :43 occurrence (including across local midnight).
  const shifted = new Date(now.getTime() - 13 * 60 * 1000);
  const minute = Math.floor(shifted.getMinutes() / 30) * 30 + 13;
  return `${localDayKey(shifted)}:${pad2(shifted.getHours())}:${pad2(minute)}`;
}

function morningBriefDueKey(now: Date): string {
  return localDayKey(now);
}

function isMorningBriefWindow(now: Date): boolean {
  return now.getHours() >= LOCAL_MORNING_BRIEF_HOUR;
}

function growthDailyDueKey(now: Date): string {
  return localDayKey(now);
}

export function isGrowthDailyWindow(now: Date): boolean {
  return now.getHours() * 60 + now.getMinutes() >= GROWTH_DAILY_HOUR * 60 + GROWTH_DAILY_MINUTE;
}

function configKey(task: PMCadenceTask, key: string): string {
  return `${task.configPrefix}_${key}`;
}

function cadenceEventType(taskName: PMCadenceTaskName, dueKey: string): string {
  return `cadence-${taskName}-${dueKey}`;
}

export class PMCadenceScheduler {
  private db: MoPDatabase;
  private relay: TmuxRelay;
  private timer: NodeJS.Timeout | null = null;
  private bootCatchupTimer: NodeJS.Timeout | null = null;
  private running: boolean = false;

  private readonly CHECK_INTERVAL_MS: number = parseInt(
    process.env.MOP_PM_CADENCE_CHECK_INTERVAL_MS ?? `${60 * 1000}`,
    10
  );
  private readonly BOOT_CATCHUP_DELAY_MS: number = parseInt(
    process.env.MOP_PM_CADENCE_BOOT_DELAY_MS ?? `${45 * 1000}`,
    10
  );

  constructor(db: MoPDatabase, relay: TmuxRelay) {
    this.db = db;
    this.relay = relay;
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => {
      void this.tick("scheduled");
    }, this.CHECK_INTERVAL_MS);
    this.bootCatchupTimer = setTimeout(() => {
      this.bootCatchupTimer = null;
      void this.tick("boot");
    }, this.BOOT_CATCHUP_DELAY_MS);
    console.log(
      `[pm-cadence] Scheduler started — check interval ${this.CHECK_INTERVAL_MS}ms, ` +
        `morning brief hour ${LOCAL_MORNING_BRIEF_HOUR}:00 local`
    );
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.bootCatchupTimer) {
      clearTimeout(this.bootCatchupTimer);
      this.bootCatchupTimer = null;
    }
  }

  async tick(reason: PMCadenceTriggerReason): Promise<PMCadenceRunResult[]> {
    if (this.running) {
      return [];
    }
    this.running = true;
    try {
      const results: PMCadenceRunResult[] = [];
      const heartbeat = await this.runIfDue("heartbeat", reason);
      if (heartbeat) results.push(heartbeat);
      const morningBrief = await this.runIfDue("morning-brief", reason);
      if (morningBrief) results.push(morningBrief);
      const hourly = await this.runIfDue("hourly-heartbeat", reason);
      if (hourly) results.push(hourly);
      const rtb = await this.runIfDue("rtb-split-update", reason);
      if (rtb) results.push(rtb);
      const growth = await this.runIfDue("growth-daily", reason);
      if (growth) results.push(growth);
      const lifecycle = await this.runIfDue("lifecycle-email", reason);
      if (lifecycle) results.push(lifecycle);
      const restart = await this.runIfDue("daily-pm-restart", reason);
      if (restart) results.push(restart);
      return results;
    } finally {
      this.running = false;
    }
  }

  async runManual(taskName: PMCadenceTaskName): Promise<PMCadenceRunResult> {
    return this.triggerTask(taskName, "manual", this.currentDueKey(taskName), true);
  }

  setPaused(paused: boolean, taskName?: PMCadenceTaskName): void {
    if (taskName) {
      this.db.setConfig(configKey(TASKS[taskName], "paused"), paused ? "true" : "false");
      return;
    }
    this.db.setConfig(CFG_GLOBAL_PAUSED, paused ? "true" : "false");
  }

  getStatus(): {
    paused: boolean;
    running: boolean;
    check_interval_ms: number;
    boot_catchup_delay_ms: number;
    local_morning_brief_hour: number;
    tasks: Array<{
      task: PMCadenceTaskName;
      label: string;
      paused: boolean;
      last_due_key: string | null;
      last_run_ts: string | null;
      last_run_reason: string | null;
      last_injected: string | null;
      current_due_key: string;
      currently_due: boolean;
      command: string;
    }>;
  } {
    const globalPaused = this.db.getConfig(CFG_GLOBAL_PAUSED) === "true";
    return {
      paused: globalPaused,
      running: this.running,
      check_interval_ms: this.CHECK_INTERVAL_MS,
      boot_catchup_delay_ms: this.BOOT_CATCHUP_DELAY_MS,
      local_morning_brief_hour: LOCAL_MORNING_BRIEF_HOUR,
      tasks: (Object.keys(TASKS) as PMCadenceTaskName[]).map((taskName) => {
        const task = TASKS[taskName];
        const lastDueKey = this.db.getConfig(configKey(task, "last_due_key"));
        const currentDueKey = this.currentDueKey(taskName);
        const taskPaused = this.isTaskPaused(taskName);
        return {
          task: taskName,
          label: task.label,
          paused: globalPaused || taskPaused,
          last_due_key: lastDueKey,
          last_run_ts: this.db.getConfig(configKey(task, "last_run_ts")),
          last_run_reason: this.db.getConfig(configKey(task, "last_run_reason")),
          last_injected: this.db.getConfig(configKey(task, "last_injected")),
          current_due_key: currentDueKey,
          currently_due: this.isTaskDue(taskName, new Date(), lastDueKey) && !globalPaused && !taskPaused,
          command: task.commandDescription,
        };
      }),
    };
  }

  private async runIfDue(taskName: PMCadenceTaskName, reason: PMCadenceTriggerReason): Promise<PMCadenceRunResult | null> {
    if (this.db.getConfig(CFG_GLOBAL_PAUSED) === "true" || this.isTaskPaused(taskName)) {
      return null;
    }

    const task = TASKS[taskName];
    const now = new Date();
    const dueKey = this.currentDueKey(taskName, now);
    const lastDueKey = this.db.getConfig(configKey(task, "last_due_key"));
    if (lastDueKey === null && reason !== "manual" && (taskName === "heartbeat" || taskName === "hourly-heartbeat" || taskName === "rtb-split-update")) {
      // Hourly: a startup before :13 must not suppress this hour's :13 run. Seed a
      // pre-window marker that differs from the hour's due key so :13 still fires.
      const seedKey = taskName === "hourly-heartbeat" && !isHourlyHeartbeatWindow(now) ? `${dueKey}:pre` : dueKey;
      this.seedCurrentBucket(taskName, reason, seedKey);
      return null;
    }
    if (!this.isTaskDue(taskName, now, lastDueKey)) {
      return null;
    }
    const delivered = this.db.hasPMQueueDelivery(
      0,
      cadenceEventType(taskName, dueKey),
      TASKS[taskName].commandDescription,
    );
    if (delivered) {
      this.db.setConfig(configKey(task, "last_due_key"), dueKey);
      this.db.logEvent(0, "pm_cadence_delivery_reconciled", null, null, {
        task: taskName,
        due_key: dueKey,
        event_type: cadenceEventType(taskName, dueKey),
      });
      return null;
    }
    return this.triggerTask(taskName, reason, dueKey, false);
  }

  private seedCurrentBucket(
    taskName: PMCadenceTaskName,
    reason: PMCadenceTriggerReason,
    dueKey: string,
  ): void {
    const task = TASKS[taskName];
    const ts = new Date().toISOString();
    this.db.setConfig(configKey(task, "last_due_key"), dueKey);
    this.db.setConfig(configKey(task, "last_run_ts"), ts);
    this.db.setConfig(configKey(task, "last_run_reason"), `seed-${reason}`);
    this.db.setConfig(configKey(task, "last_injected"), "false");
    this.db.logEvent(0, "pm_cadence_seeded", null, null, {
      task: taskName,
      label: task.label,
      reason,
      due_key: dueKey,
      injected: false,
    });
    console.log(`[pm-cadence] seeded task=${taskName} reason=${reason} due_key=${dueKey} injected=false`);
  }

  private async triggerTask(
    taskName: PMCadenceTaskName,
    reason: PMCadenceTriggerReason,
    dueKey: string,
    manual: boolean,
  ): Promise<PMCadenceRunResult> {
    const task = TASKS[taskName];
    if (taskName === "lifecycle-email" && !lifecycleEmailApproved()) {
      return { task: taskName, triggered: false, reason, due_key: dueKey,
        injected: false, queued: false, message: "Templates not all APPROVED; selector send_allowed=false" };
    }
    let message = task.commandDescription;

    const eventType = cadenceEventType(taskName, dueKey);
    let injected = false;
    let queued = false;

    // submitToPM persists the exact occurrence before entering the shared
    // serialized boundary. Its observation-bound key is Enter only for an
    // explicit idle; busy/unknown uses C-q immediately. Awaiting the result
    // keeps the due key tied to actual delivery rather than enqueueing.
    const submitted = await this.relay.submitToPM(message, eventType);
    injected = submitted.ok && submitted.queued !== true;
    queued = !injected;

    const ts = new Date().toISOString();
    if (injected) {
      this.db.setConfig(configKey(task, "last_due_key"), dueKey);
      // The durable relay journal already owns this occurrence's replay
      // decision. Do not arm a second, raw transcript-based submit path.
      if (!this.db.hasPMQueueDelivery(0, eventType, message)) {
        armDeliveryVerification(this.db, taskName, dueKey, ts);
      }
    }
    this.db.setConfig(configKey(task, "last_run_ts"), ts);
    this.db.setConfig(configKey(task, "last_run_reason"), reason);
    this.db.setConfig(configKey(task, "last_injected"), injected ? "true" : "false");
    this.db.logEvent(0, "pm_cadence_triggered", null, null, {
      task: taskName,
      label: task.label,
      reason,
      due_key: dueKey,
      injected,
      queued,
      manual,
      message,
      event_type: eventType,
      delivery_mode: queued ? "queued-normal-prompt" : "direct-submit",
    });
    console.log(
      `[pm-cadence] triggered task=${taskName} reason=${reason} due_key=${dueKey} ` +
        `injected=${injected} queued=${queued}`
    );

    return {
      task: taskName,
      triggered: true,
      reason,
      due_key: dueKey,
      injected,
      queued,
      message,
    };
  }

  private isTaskPaused(taskName: PMCadenceTaskName): boolean {
    return this.db.getConfig(configKey(TASKS[taskName], "paused")) === "true";
  }

  private currentDueKey(taskName: PMCadenceTaskName, now = new Date()): string {
    if (taskName === "rtb-split-update") return rtbSplitUpdateDueKey(now);
    if (taskName === "daily-pm-restart") return dailyPMRestartParts(now).day;
    if (taskName === "lifecycle-email") return lifecycleEmailParts(now).day;
    if (taskName === "heartbeat") return heartbeatDueKey(now);
    if (taskName === "hourly-heartbeat") return hourlyHeartbeatDueKey(now);
    if (taskName === "growth-daily") return growthDailyDueKey(now);
    return morningBriefDueKey(now);
  }

  private isTaskDue(taskName: PMCadenceTaskName, now: Date, lastDueKey: string | null): boolean {
    if (taskName === "rtb-split-update") return rtbSplitUpdateDueKey(now) !== lastDueKey;
    if (taskName === "daily-pm-restart") {
      return isDailyPMRestartWindow(now) && dailyPMRestartParts(now).day !== lastDueKey;
    }
    if (taskName === "lifecycle-email") {
      return isLifecycleEmailWindow(now) && lifecycleEmailParts(now).day !== lastDueKey;
    }
    if (taskName === "heartbeat") {
      return heartbeatDueKey(now) !== lastDueKey;
    }
    if (taskName === "hourly-heartbeat") {
      return isHourlyHeartbeatWindow(now) && hourlyHeartbeatDueKey(now) !== lastDueKey;
    }
    if (taskName === "growth-daily") {
      return isGrowthDailyWindow(now) && growthDailyDueKey(now) !== lastDueKey;
    }
    if (!isMorningBriefWindow(now)) {
      return false;
    }
    return morningBriefDueKey(now) !== lastDueKey;
  }
}

// ─── Delivery verification (Rajiv 2026-10-16, DM D0AMF0XE6TS thread
// 1792121896.574029: "why are these getting dropped?"). The 08:13 IST hourly
// inject on 2026-10-16 reported injected=true but never reached the PM
// transcript, and the due_key was already marked done.
//
// CTO REVISE (C0ALZJHGE49 thread 1792122734.658139, ts 1792123311.071369):
// - Only a user-role message or a queued-command/queue-enqueue record in the
//   SAME PM session counts as acceptance; assistant/tool quotations never do.
// - Absence is proof only when the Stop belongs to the bound session and the
//   read window provably covers the inject time. Anything else stays PENDING
//   and is NOT re-sent.
// - The re-send uses the pane-id-pinned, composer-fenced writer and refuses
//   Enter unless PM idle is proven in the same session. ───

const VERIFY_GRACE_MS = 5_000;
const TRANSCRIPT_TAIL_BYTES = 8 * 1024 * 1024;
const REDELIVER_IDLE_RETRY_MS = parseInt(process.env.MOP_PM_CADENCE_REDELIVER_RETRY_MS ?? "35000", 10);

type PendingVerify = {
  due_key: string;
  injected_at: string;
  retried: boolean;
  /** Re-send claimed synchronously and its write is still in flight. */
  inflight?: boolean;
  /** Proven pre-paste zero-effect refusals already released (max 1). */
  zero_effect_releases?: number;
  session_id?: string | null;
};

const CFG_PM_STOP_SESSION = "pm_cadence_last_stop_session";

function pendingKey(taskName: PMCadenceTaskName): string {
  return configKey(TASKS[taskName], "pending_verify");
}

function armDeliveryVerification(
  db: MoPDatabase,
  taskName: PMCadenceTaskName,
  dueKey: string,
  injectedAt: string,
  retried = false,
  sessionId: string | null = db.getConfig(CFG_PM_STOP_SESSION),
): void {
  const pending: PendingVerify = { due_key: dueKey, injected_at: injectedAt, retried, session_id: sessionId };
  db.setConfig(pendingKey(taskName), JSON.stringify(pending));
}

function readTail(path: string, maxBytes: number): { text: string; truncated: boolean } | null {
  let fd: number | null = null;
  try {
    fd = openSync(path, "r");
    const size = fstatSync(fd).size;
    const start = Math.max(0, size - maxBytes);
    const buf = Buffer.alloc(size - start);
    readSync(fd, buf, 0, buf.length, start);
    return { text: buf.toString("utf8"), truncated: start > 0 };
  } catch {
    return null;
  } finally {
    if (fd !== null) closeSync(fd);
  }
}

type TranscriptRecord = {
  type?: string;
  operation?: string;
  timestamp?: string;
  sessionId?: string;
  content?: unknown;
  message?: { role?: string; content?: unknown };
  attachment?: { type?: string; prompt?: unknown };
};

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => (part && typeof part === "object" && (part as { type?: string }).type === "text"
        ? String((part as { text?: unknown }).text ?? "")
        : ""))
      .join("\n");
  }
  return "";
}

/** The user/queue-acceptance text of a record, or null for any other record. */
function acceptedPromptText(record: TranscriptRecord): string | null {
  if (record.type === "user" && record.message?.role === "user") {
    // tool_result blocks are user-role but are tool output, not a prompt.
    return textOf(record.message.content);
  }
  if (record.type === "queue-operation" && record.operation === "enqueue") return textOf(record.content);
  if (record.type === "attachment" && record.attachment?.type === "queued_command") {
    return textOf(record.attachment.prompt);
  }
  return null;
}

export type TranscriptVerdict = "accepted" | "absent" | "incomplete";

/**
 * accepted: a same-session user/queue record carrying the prompt at/after the inject.
 * absent: the window provably covers the inject time and no such record exists.
 * incomplete: the read window may not cover the inject; never a basis to re-send.
 */
export function classifyTranscript(
  tail: { text: string; truncated: boolean },
  prompt: string,
  sinceMs: number,
  sessionId: string,
): TranscriptVerdict {
  const needle = prompt.split("\n")[0];
  let earliestMs = Number.POSITIVE_INFINITY;
  let sawSessionRecord = false;
  for (const line of tail.text.split("\n")) {
    if (!line.trim()) continue;
    let record: TranscriptRecord;
    try {
      record = JSON.parse(line) as TranscriptRecord;
    } catch {
      continue;
    }
    if (record.sessionId !== sessionId) continue;
    const ts = record.timestamp ? Date.parse(record.timestamp) : NaN;
    if (!Number.isFinite(ts)) continue;
    sawSessionRecord = true;
    earliestMs = Math.min(earliestMs, ts);
    if (ts < sinceMs - VERIFY_GRACE_MS) continue;
    const text = acceptedPromptText(record);
    if (text !== null && text.trim().startsWith(needle)) return "accepted";
  }
  if (!sawSessionRecord) return "incomplete";
  // A truncated tail must still start before the inject to prove absence.
  // Absence needs temporal coverage regardless of file size: a replaced or
  // compacted session file whose same-session records start after the
  // inject cannot prove the prompt was never accepted (CTO REVISE of 02aa84e4).
  if (earliestMs > sinceMs) return "incomplete";
  return "absent";
}

type RedeliverRelay = Pick<TmuxRelay, "writePMPromptFenced" | "isPMIdleProven">;

/**
 * Called on every PM Stop with that Stop's session_id and transcript_path.
 */
export async function verifyPMCadenceDelivery(
  db: MoPDatabase,
  relay: RedeliverRelay,
  stop: { sessionId?: string; transcriptPath?: string },
  now: Date = new Date(),
  idleRetryMs: number = REDELIVER_IDLE_RETRY_MS,
): Promise<void> {
  const stopSession = stop.sessionId ?? null;
  if (stopSession) db.setConfig(CFG_PM_STOP_SESSION, stopSession);
  for (const taskName of Object.keys(TASKS) as PMCadenceTaskName[]) {
    const raw = db.getConfig(pendingKey(taskName));
    if (!raw) continue;
    let pending: PendingVerify;
    try {
      pending = JSON.parse(raw) as PendingVerify;
    } catch {
      db.setConfig(pendingKey(taskName), "");
      continue;
    }
    if (pending.inflight) continue;
    const injectedMs = Date.parse(pending.injected_at);
    if (!Number.isFinite(injectedMs) || now.getTime() - injectedMs < VERIFY_GRACE_MS) continue;
    // Wrong-session or unknown-session Stops are never evidence.
    if (!stopSession || !stop.transcriptPath) continue;
    if (pending.session_id && pending.session_id !== stopSession) continue;
    if (!stop.transcriptPath.endsWith(`${stopSession}.jsonl`)) continue;
    const tail = readTail(stop.transcriptPath, TRANSCRIPT_TAIL_BYTES);
    if (tail === null) continue;

    const task = TASKS[taskName];
    const details = { task: taskName, due_key: pending.due_key, session_id: stopSession };
    const verdict = classifyTranscript(tail, task.commandDescription, injectedMs, stopSession);
    if (verdict === "accepted") {
      db.setConfig(pendingKey(taskName), "");
      db.logEvent(0, "pm_cadence_delivery_verified", "Stop", null, { ...details, retried: pending.retried });
      continue;
    }
    if (verdict === "incomplete") continue;
    if (pending.retried) {
      db.setConfig(pendingKey(taskName), "");
      db.logEvent(0, "pm_cadence_delivery_unverified", "Stop", null, details);
      continue;
    }
    // Bind to the proven session before any write (synchronous; no await
    // between the read above and the claim inside redeliverFenced).
    db.setConfig(pendingKey(taskName), JSON.stringify({ ...pending, session_id: stopSession }));
    const sent = await redeliverFenced(db, relay, taskName, pending.due_key, stopSession);
    if (!sent && idleRetryMs > 0) {
      // One bounded retry after the idle debounce window, same fence.
      setTimeout(() => {
        void redeliverFenced(db, relay, taskName, pending.due_key, stopSession);
      }, idleRetryMs).unref?.();
    }
  }
}

async function redeliverFenced(
  db: MoPDatabase,
  relay: RedeliverRelay,
  taskName: PMCadenceTaskName,
  dueKey: string,
  sessionId: string,
): Promise<boolean> {
  const raw = db.getConfig(pendingKey(taskName));
  if (!raw) return false;
  const pending = JSON.parse(raw) as PendingVerify;
  if (pending.retried || pending.inflight || pending.due_key !== dueKey) return false;
  // Claim synchronously, before the first await: at most one submission.
  const claimed: PendingVerify = { ...pending, retried: true, inflight: true, session_id: sessionId };
  db.setConfig(pendingKey(taskName), JSON.stringify(claimed));
  const check = () => {
    if (!relay.isPMIdleProven()) return { reason: "pm_idle_not_proven", busy: true };
    if (db.getConfig(CFG_PM_STOP_SESSION) !== sessionId) return { reason: "pm_session_changed", busy: false };
    return null;
  };
  const task = TASKS[taskName];
  const result = await relay.writePMPromptFenced(task.commandDescription, check);
  const releases = pending.zero_effect_releases ?? 0;
  if (!result.ok && result.zeroEffect && releases < 1) {
    // Proven pre-paste, zero-effect refusal: release the claim for one retry.
    db.setConfig(pendingKey(taskName), JSON.stringify({
      ...pending, retried: false, inflight: false, session_id: sessionId, zero_effect_releases: releases + 1,
    }));
  } else {
    // Submitted, post-paste refusal (even if guarded cleanup removed the text),
    // or a second zero-effect refusal: CONSUMED, never re-sent.
    const injectedAt = result.ok ? new Date().toISOString() : pending.injected_at;
    db.setConfig(pendingKey(taskName), JSON.stringify({
      ...pending, injected_at: injectedAt, retried: true, inflight: false, session_id: sessionId,
    }));
  }
  db.logEvent(0, result.ok ? "pm_cadence_redelivered" : "pm_cadence_redeliver_refused", "Stop", null, {
    task: taskName,
    due_key: dueKey,
    session_id: sessionId,
    reason: result.reason ?? null,
    zero_effect: result.zeroEffect,
    consumed: result.ok || !result.zeroEffect || releases >= 1,
  });
  return result.ok;
}
