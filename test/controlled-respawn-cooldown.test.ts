import assert from "node:assert/strict";
import test from "node:test";
import { ProcessHealthChecker } from "../src/health.js";

test("an in-flight health tick cannot cross a controlled respawn fence", async () => {
  const events: string[] = [];
  const notifications: string[] = [];
  const checker = new ProcessHealthChecker(
    {
      logEvent: (_slot: number, event: string) => events.push(event),
      getExitPending: () => false,
    } as never,
    {
      injectToPM: (message: string) => notifications.push(message),
    } as never,
  );

  const internals = checker as unknown as {
    startTime: number;
    getPaneCommand: (slot: number) => Promise<string | null>;
    restartSlot: (slot: number) => Promise<boolean>;
  };
  internals.startTime = 0;
  let releasePaneRead: ((command: string) => void) | undefined;
  let paneReadStarted: (() => void) | undefined;
  const paneReadIsStarted = new Promise<void>((resolve) => {
    paneReadStarted = resolve;
  });
  internals.getPaneCommand = async (slot) => {
    if (slot !== 4) return null;
    paneReadStarted?.();
    return await new Promise<string>((resolve) => {
      releasePaneRead = resolve;
    });
  };
  let launches = 0;
  internals.restartSlot = async () => {
    launches += 1;
    return true;
  };

  const healthTick = checker.checkAll();
  await paneReadIsStarted;

  checker.markPmInitiatedRespawn(4);
  checker.completePmInitiatedRespawn(4);
  assert.equal(checker.isPmInitiatedRespawn(4), false);
  releasePaneRead?.("zsh");
  await healthTick;

  assert.equal(launches, 0);
  assert.deepEqual(events, []);
  assert.deepEqual(notifications, []);
});

test("an ordinary unfenced dead shell still launches exactly once", async () => {
  const events: string[] = [];
  const notifications: string[] = [];
  const checker = new ProcessHealthChecker(
    {
      logEvent: (_slot: number, event: string) => events.push(event),
      getExitPending: () => false,
      getSlot: () => ({ task: "focused proof" }),
    } as never,
    {
      injectToPM: (message: string) => notifications.push(message),
    } as never,
  );

  const internals = checker as unknown as {
    startTime: number;
    getPaneCommand: (slot: number) => Promise<string | null>;
    restartSlot: (slot: number) => Promise<boolean>;
    scheduleContinueInjection: (slot: number) => void;
  };
  internals.startTime = 0;
  internals.getPaneCommand = async (slot) => slot === 4 ? "zsh" : null;
  let launches = 0;
  internals.restartSlot = async () => {
    launches += 1;
    return true;
  };
  internals.scheduleContinueInjection = () => undefined;

  await checker.checkAll();

  assert.equal(launches, 1);
  assert.deepEqual(events, ["process_dead", "process_restarted"]);
  assert.equal(notifications.length, 1);
  assert.match(notifications[0], /slot 4 process died/);
});
