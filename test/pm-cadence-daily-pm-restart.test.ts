import assert from "node:assert/strict";
import test from "node:test";
import { dailyPMRestartParts, isDailyPMRestartWindow, PMCadenceScheduler } from "../src/pmCadence.js";

test("daily restart window is explicitly 04:00 IST regardless of host timezone", () => {
  assert.equal(isDailyPMRestartWindow(new Date("2026-10-27T22:29:59Z")), false);
  assert.equal(isDailyPMRestartWindow(new Date("2026-10-27T22:30:00Z")), true);
  assert.equal(isDailyPMRestartWindow(new Date("2026-10-27T23:29:59Z")), true);
  assert.equal(isDailyPMRestartWindow(new Date("2026-10-27T23:30:00Z")), false);
  assert.equal(isDailyPMRestartWindow(new Date("2026-10-28T08:00:00Z")), false);
  assert.equal(dailyPMRestartParts(new Date("2026-10-27T22:30:00Z")).day, "2026-10-28-IST");
});

// RETIRED (Rajiv DM D0AMF0XE6TS thread 1794921293.133799, ts 1794923900.734289:
// "remove 4am restart"): the 04:00 IST occurrence never fires. The task definition
// stays so persisted keys, status and the pause/run routes keep working.
test("daily restart is retired: never scheduled, never at boot, never manual, queued occurrence dropped; status stays readable", async () => {
  const config = new Map<string, string>();
  const messages: string[] = [];
  const db = { getConfig: (k: string) => config.get(k) ?? null,
    setConfig: (k: string, v: string) => config.set(k, v), logEvent: () => {},
    hasPMQueueDelivery: () => false };
  const relay = { submitToPM: async (message: string) => { messages.push(message); return { ok: true }; } };
  const scheduler = new PMCadenceScheduler(db as never, relay as never);
  const status = () => scheduler.getStatus().tasks.find(t => t.task === "daily-pm-restart")!;
  assert.equal(status().command, "MoP: run Skill(daily-pm-restart)");
  assert.equal(status().retired, true);
  assert.equal(status().currently_due, false);
  assert.match(status().label, /RETIRED/);
  assert.equal(scheduler.getStatus().tasks.filter(t => t.retired).length, 1, "only daily-pm-restart is retired");
  // Pause toggles stay harmless and readable.
  scheduler.setPaused(true, "daily-pm-restart");
  assert.equal(status().paused, true);
  scheduler.setPaused(false, "daily-pm-restart");
  // Manual run is refused with a reason; nothing is submitted and no key is written.
  const manual = await scheduler.runManual("daily-pm-restart");
  assert.equal(manual.triggered, false);
  assert.match(manual.message, /retired/);
  assert.deepEqual(messages, []);
  assert.equal(config.has("pm_cadence_daily_pm_restart_last_due_key"), false);
  // Scheduled and boot evaluation of the task do nothing and write nothing, even inside the old window.
  const runIfDue = (scheduler as unknown as { runIfDue: (t: string, r: string, s?: Date) => Promise<unknown> }).runIfDue.bind(scheduler);
  assert.equal(await runIfDue("daily-pm-restart", "scheduled"), null);
  assert.equal(await runIfDue("daily-pm-restart", "boot", new Date("2026-11-17T22:45:00Z")), null);
  assert.deepEqual(messages, []);
  assert.equal(config.has("pm_cadence_daily_pm_restart_last_due_key"), false);
  assert.equal(config.has("pm_cadence_daily_pm_restart_last_run_ts"), false);
  // An occurrence queued before retirement is dropped at the delivery boundary.
  assert.deepEqual(scheduler.revalidateQueuedReminder("cadence-daily-pm-restart-2026-11-18-IST", new Date("2026-11-17T22:45:00Z")),
    { drop: "cadence-task-retired" });
});
