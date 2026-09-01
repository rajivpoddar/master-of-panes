/**
 * MoP PM Cadence Scheduler
 *
 * Owns PM scheduled cadence that used to be split across launchd scripts:
 * - 3h heartbeat: queues a normal PM prompt to run the canonical Sakshi
 *   Python producer directly
 * - daily morning brief: queues a normal PM prompt to invoke Skill(morning-brief)
 *
 * launchd remains the MoP watchdog only. These ticks are persisted in MoP DB
 * config keys so restarts do not double-fire within the same cadence bucket.
 */

import { execFile } from "node:child_process";
import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";

export type PMCadenceTaskName = "heartbeat" | "morning-brief";
export type PMCadenceTriggerReason = "scheduled" | "manual" | "boot";

type PMCadenceTask = {
  name: PMCadenceTaskName;
  label: string;
  configPrefix: string;
  commandDescription: string;
};

export type HeartbeatPreflightStage = "dry-run" | "launch-prompt" | "ready-pool-audit";

export type HeartbeatPreflightResult = {
  ok: boolean;
  stage: HeartbeatPreflightStage;
  returncode: number;
  output: string;
  error: string | null;
};

export type HeartbeatPreflightRunner = () => Promise<HeartbeatPreflightResult & { launch_prompt?: string }>;

export type PMCadenceRunResult = {
  task: PMCadenceTaskName;
  triggered: boolean;
  reason: PMCadenceTriggerReason;
  due_key: string;
  injected: boolean;
  queued: boolean;
  message: string;
  preflight: "not-run" | "passed" | "failed";
  preflight_stage: HeartbeatPreflightStage | null;
  preflight_error: string | null;
};

const CANONICAL_HEARTBEAT_PRODUCER = "/Users/rajiv/.claude/scripts/sakshi-heartbeat.py";
const HEARTBEAT_PREFLIGHT_STAGES: Array<{
  stage: HeartbeatPreflightStage;
  args: string[];
}> = [
  { stage: "dry-run", args: ["--dry-run"] },
  { stage: "launch-prompt", args: ["--launch-prompt"] },
  { stage: "ready-pool-audit", args: ["--ready-pool-audit"] },
];

function runHeartbeatProducerStage(
  stage: HeartbeatPreflightStage,
  args: string[],
): Promise<HeartbeatPreflightResult> {
  return new Promise((resolve) => {
    execFile(
      "python3",
      [CANONICAL_HEARTBEAT_PRODUCER, ...args],
      { timeout: 180_000, maxBuffer: 2 * 1024 * 1024 },
      (error, stdout, stderr) => {
        const returncode = error && typeof error.code === "number" ? error.code : error ? 1 : 0;
        resolve({
          ok: !error,
          stage,
          returncode,
          output: stdout,
          error: error ? (stderr.trim() || error.message).slice(0, 500) : null,
        });
      },
    );
  });
}

export async function runCanonicalHeartbeatPreflight(): Promise<HeartbeatPreflightResult & { launch_prompt: string }> {
  let launchPrompt = "";
  for (const { stage, args } of HEARTBEAT_PREFLIGHT_STAGES) {
    const result = await runHeartbeatProducerStage(stage, args);
    if (!result.ok) return { ...result, launch_prompt: "" };
    if (stage === "launch-prompt") {
      launchPrompt = result.output;
      if (!launchPrompt.trim()) {
        return {
          ...result,
          ok: false,
          returncode: 2,
          error: "canonical launch prompt was empty",
          launch_prompt: "",
        };
      }
    }
  }
  return {
    ok: true,
    stage: "ready-pool-audit",
    returncode: 0,
    output: "",
    error: null,
    launch_prompt: launchPrompt,
  };
}

const HEARTBEAT_TASK: PMCadenceTask = {
  name: "heartbeat",
  label: "3h heartbeat",
  configPrefix: "pm_cadence_heartbeat",
  commandDescription:
    "MoP: 3h heartbeat due\n\n" +
    "The canonical read-only Sakshi producer preflight must pass before launch. " +
    "Use the exact launch prompt appended by MoP; any nonzero producer result is UNKNOWN/NOT_CLEAR and stops this wake. " +
    "Session-age clear_due is report-only here.",
};

const MORNING_BRIEF_TASK: PMCadenceTask = {
  name: "morning-brief",
  label: "morning brief",
  configPrefix: "pm_cadence_morning_brief",
  commandDescription:
    "MoP: morning brief due\n\n" +
    "Invoke Skill(morning-brief) now. Launch the morning-brief background agent with run_in_background=true; the agent owns evidence gathering and Slack posting. Do not run /morning-brief inline.",
};

const TASKS: Record<PMCadenceTaskName, PMCadenceTask> = {
  heartbeat: HEARTBEAT_TASK,
  "morning-brief": MORNING_BRIEF_TASK,
};

const CFG_GLOBAL_PAUSED = "pm_cadence_paused";
const LOCAL_MORNING_BRIEF_HOUR = 10;

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

function morningBriefDueKey(now: Date): string {
  return localDayKey(now);
}

function isMorningBriefWindow(now: Date): boolean {
  return now.getHours() >= LOCAL_MORNING_BRIEF_HOUR;
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
  private readonly heartbeatPreflight: HeartbeatPreflightRunner | null;

  private readonly CHECK_INTERVAL_MS: number = parseInt(
    process.env.MOP_PM_CADENCE_CHECK_INTERVAL_MS ?? `${60 * 1000}`,
    10
  );
  private readonly BOOT_CATCHUP_DELAY_MS: number = parseInt(
    process.env.MOP_PM_CADENCE_BOOT_DELAY_MS ?? `${45 * 1000}`,
    10
  );

  constructor(
    db: MoPDatabase,
    relay: TmuxRelay,
    options: { heartbeatPreflight?: HeartbeatPreflightRunner | null } = {},
  ) {
    this.db = db;
    this.relay = relay;
    this.heartbeatPreflight = options.heartbeatPreflight ?? null;
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
    if (lastDueKey === null && reason !== "manual" && taskName === "heartbeat") {
      this.seedCurrentBucket(taskName, reason, dueKey);
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
    let message = task.commandDescription;

    const eventType = cadenceEventType(taskName, dueKey);
    let injected = false;
    let queued = false;
    let preflight: PMCadenceRunResult["preflight"] = "not-run";
    let preflightStage: HeartbeatPreflightStage | null = null;
    let preflightError: string | null = null;

    if (taskName === "heartbeat" && this.heartbeatPreflight) {
      const result = await this.heartbeatPreflight();
      preflightStage = result.stage;
      if (!result.ok) {
        preflight = "failed";
        preflightError = result.error ?? `producer exited ${result.returncode}`;
        const failure = `HEARTBEAT_PREFLIGHT_FAILED stage=${result.stage} ` +
          `exit=${result.returncode}: ${preflightError}`;
        const ts = new Date().toISOString();
        this.db.setConfig(configKey(task, "last_run_ts"), ts);
        this.db.setConfig(configKey(task, "last_run_reason"), `preflight-failed:${result.stage}`);
        this.db.setConfig(configKey(task, "last_injected"), "false");
        this.db.logEvent(0, "pm_cadence_preflight_failed", null, null, {
          task: taskName,
          reason,
          due_key: dueKey,
          stage: result.stage,
          returncode: result.returncode,
          error: preflightError,
          healthy: false,
        });
        console.error(`[pm-cadence] ${failure}`);
        return {
          task: taskName,
          triggered: false,
          reason,
          due_key: dueKey,
          injected: false,
          queued: false,
          message: failure,
          preflight,
          preflight_stage: preflightStage,
          preflight_error: preflightError,
        };
      }
      preflight = "passed";
      message = `${task.commandDescription}\n\n${result.launch_prompt ?? result.output}`;
    }

    // submitToPM persists the exact occurrence before entering the shared
    // serialized boundary. Its observation-bound key is Enter only for an
    // explicit idle; busy/unknown uses C-q immediately. Awaiting the result
    // keeps the due key tied to actual delivery rather than enqueueing.
    const submitted = await this.relay.submitToPM(message, eventType);
    injected = submitted.ok;
    queued = !injected;

    const ts = new Date().toISOString();
    if (injected) {
      this.db.setConfig(configKey(task, "last_due_key"), dueKey);
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
      preflight,
      preflight_stage: preflightStage,
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
      preflight,
      preflight_stage: preflightStage,
      preflight_error: preflightError,
    };
  }

  private isTaskPaused(taskName: PMCadenceTaskName): boolean {
    return this.db.getConfig(configKey(TASKS[taskName], "paused")) === "true";
  }

  private currentDueKey(taskName: PMCadenceTaskName, now = new Date()): string {
    return taskName === "heartbeat" ? heartbeatDueKey(now) : morningBriefDueKey(now);
  }

  private isTaskDue(taskName: PMCadenceTaskName, now: Date, lastDueKey: string | null): boolean {
    if (taskName === "heartbeat") {
      return heartbeatDueKey(now) !== lastDueKey;
    }
    if (!isMorningBriefWindow(now)) {
      return false;
    }
    return morningBriefDueKey(now) !== lastDueKey;
  }
}
