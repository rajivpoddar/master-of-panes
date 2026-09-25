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
    restartSlot: (slot: number) => Promise<{ success: boolean; reason: string }>;
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
    return { success: true, reason: "agent boot verified (claude)" };
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
    restartSlot: (slot: number) => Promise<{ success: boolean; reason: string }>;
    scheduleContinueInjection: (slot: number) => void;
  };
  internals.startTime = 0;
  internals.getPaneCommand = async (slot) => slot === 4 ? "zsh" : null;
  let launches = 0;
  internals.restartSlot = async () => {
    launches += 1;
    return { success: true, reason: "agent boot verified (claude)" };
  };
  internals.scheduleContinueInjection = () => undefined;

  await checker.checkAll();

  assert.equal(launches, 1);
  assert.deepEqual(events, ["process_dead", "process_restarted"]);
  assert.equal(notifications.length, 1);
  assert.match(notifications[0], /slot 4 process died/);
});

test("launcher failure is logged and queued without counting a restart success", async () => {
  const events: string[] = [];
  const notifications: string[] = [];
  const checker = new ProcessHealthChecker(
    {
      logEvent: (_slot: number, event: string) => events.push(event),
      getExitPending: () => false,
    } as never,
    { injectToPM: (message: string) => notifications.push(message) } as never,
  );
  const internals = checker as unknown as {
    startTime: number;
    getPaneCommand: (slot: number) => Promise<string | null>;
    restartSlot: (slot: number) => Promise<{ success: boolean; reason: string }>;
    restartCounts: Map<number, { count: number; windowStart: number }>;
  };
  internals.startTime = 0;
  internals.getPaneCommand = async (slot) => slot === 4 ? "zsh" : null;
  internals.restartSlot = async () => ({ success: false, reason: "launcher exited nonzero" });

  await checker.checkAll();

  assert.deepEqual(events, ["process_dead", "process_restart_failed"]);
  assert.equal(internals.restartCounts.get(4)?.count, 0);
  assert.equal(notifications.length, 1);
  assert.match(notifications[0] ?? "", /restart failed/);
});

test("agent boot is the success boundary", async () => {
  const events: string[] = [];
  const checker = new ProcessHealthChecker(
    { logEvent: (_slot: number, event: string) => events.push(event) } as never,
    {} as never,
  );
  const internals = checker as unknown as {
    getPaneCommand: (slot: number) => Promise<string | null>;
    waitForAgentBoot: (slot: number, timeoutMs?: number) => Promise<string | null>;
    AGENT_BOOT_POLL_MS: number;
  };
  let reads = 0;
  internals.AGENT_BOOT_POLL_MS = 0;
  internals.getPaneCommand = async () => ++reads < 3 ? "zsh" : "claude";

  assert.equal(await internals.waitForAgentBoot(0, 100), "claude");
  assert.deepEqual(events, ["agent_booted"]);
});

test("only verified restarts consume the hourly crash limit", async () => {
  const events: string[] = [];
  const checker = new ProcessHealthChecker(
    { logEvent: (_slot: number, event: string) => events.push(event), getExitPending: () => false } as never,
    { injectToPM: () => true } as never,
  );
  const internals = checker as unknown as {
    startTime: number;
    getPaneCommand: (slot: number) => Promise<string | null>;
    restartSlot: (slot: number) => Promise<{ success: boolean; reason: string }>;
    isRestartSuppressed: () => boolean;
    restartCounts: Map<number, { count: number; windowStart: number }>;
  };
  internals.startTime = 0;
  internals.isRestartSuppressed = () => false;
  internals.getPaneCommand = async (slot) => slot === 4 ? "zsh" : null;
  internals.restartSlot = async () => ({ success: true, reason: "agent boot verified (claude)" });

  await checker.checkAll();
  await checker.checkAll();
  await checker.checkAll();
  const fourthResultStart = events.length;
  await checker.checkAll();

  assert.equal(internals.restartCounts.get(4)?.count, 3);
  assert.equal(events.slice(fourthResultStart).includes("process_dead"), false);
});
