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

test("daily restart uses persisted normal PM cadence and honors per-task pause", async () => {
  const config = new Map<string, string>();
  const messages: string[] = [];
  const db = { getConfig: (k: string) => config.get(k) ?? null,
    setConfig: (k: string, v: string) => config.set(k, v), logEvent: () => {} };
  const relay = { submitToPM: async (message: string) => { messages.push(message); return { ok: true }; } };
  const scheduler = new PMCadenceScheduler(db as never, relay as never);
  assert.equal(scheduler.getStatus().tasks.find(t => t.task === "daily-pm-restart")!.command, "MoP: run Skill(daily-pm-restart)");
  scheduler.setPaused(true, "daily-pm-restart");
  assert.equal(scheduler.getStatus().tasks.find(t => t.task === "daily-pm-restart")!.paused, true);
  scheduler.setPaused(false, "daily-pm-restart");
  await scheduler.runManual("daily-pm-restart");
  assert.deepEqual(messages, ["MoP: run Skill(daily-pm-restart)"]);
  assert.match(config.get("pm_cadence_daily_pm_restart_last_due_key")!, /-IST$/);
});
