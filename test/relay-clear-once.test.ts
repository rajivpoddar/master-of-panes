import assert from "node:assert/strict";
import test from "node:test";

import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const CHECKOUT = "/Users/rajiv/Downloads/projects/heydonna-app-3005";

function makeRunner(failOn: "paste" | "enter" | null = null) {
  const calls: string[] = [];
  const effects: string[] = [];
  const runShell = async (command: string) => {
    calls.push(command);
    if (command.includes("display-message")) return { stdout: `%55|${CHECKOUT}\n`, stderr: "", code: 0 };
    if (command.includes("rev-parse --show-toplevel")) return { stdout: `${CHECKOUT}\n`, stderr: "", code: 0 };
    if (failOn === "paste" && command.includes("paste-buffer")) throw new Error("paste failed");
    if (failOn === "enter" && command.includes("send-keys")) throw new Error("enter failed");
    if (command.includes("load-buffer")) effects.push("load-buffer", "paste-buffer", "send-keys");
    return { stdout: "", stderr: "", code: 0 };
  };
  return { calls, effects, runShell };
}

test("clear delivery runs the exact fence once before pane mutation", async () => {
  const runner = makeRunner();
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: runner.runShell });
  let fences = 0;
  const result = await relay.sendClearOnce(5, async () => { fences += 1; return false; });
  assert.equal(result.ok, false);
  assert.equal(result.effect_started, false);
  assert.equal(fences, 1);
  assert.equal(runner.calls.filter((call) => call.includes("load-buffer")).length, 0);
  assert.equal(runner.calls.filter((call) => call.includes("paste-buffer")).length, 0);
  assert.equal(runner.calls.filter((call) => call.includes("send-keys")).length, 0);
});

test("paste failure is ambiguous with one pane attempt and no retry", async () => {
  const runner = makeRunner("paste");
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: runner.runShell });
  const result = await relay.sendClearOnce(5, async () => true);
  assert.equal(result.ok, false);
  assert.equal(result.effect_started, true);
  assert.equal(runner.calls.filter((call) => call.includes("load-buffer")).length, 1);
  assert.deepEqual(runner.effects, []);
});

test("Enter failure is ambiguous with one submission and no retry", async () => {
  const runner = makeRunner("enter");
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: runner.runShell });
  const result = await relay.sendClearOnce(5, async () => true);
  assert.equal(result.ok, false);
  assert.equal(result.effect_started, true);
  assert.equal(runner.calls.filter((call) => call.includes("load-buffer")).length, 1);
  assert.deepEqual(runner.effects, []);
});

test("final fence has no file write, timer, or separate pane await before submission", async () => {
  const runner = makeRunner();
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: runner.runShell });
  const phases: string[] = [];
  let releaseFence!: () => void;
  let fenceStarted!: () => void;
  const fenceObserved = new Promise<void>((resolve) => { fenceStarted = resolve; });
  const pausedFence = new Promise<void>((resolve) => { releaseFence = resolve; });
  const pending = relay.sendClearOnce(5, async () => {
    phases.push("final-fence");
    fenceStarted();
    await pausedFence;
    phases.push("fence-passed");
    return true;
  });
  await fenceObserved;
  assert.deepEqual(phases, ["final-fence"]);
  assert.equal(runner.calls.filter((call) => call.includes("load-buffer")).length, 0);
  releaseFence();
  const result = await pending;
  assert.equal(result.ok, true);
  assert.deepEqual(phases, ["final-fence", "fence-passed"]);
  assert.equal(runner.calls.filter((call) => call.includes("load-buffer")).length, 1);
  assert.equal(runner.calls.filter((call) => call.includes("paste-buffer")).length, 1);
  assert.equal(runner.calls.filter((call) => call.includes("send-keys")).length, 1);
  assert.match(runner.calls.find((call) => call.includes("load-buffer")) ?? "", /&&/);
});
