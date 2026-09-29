import assert from "node:assert/strict";
import test from "node:test";

import { getPmWaitNudgeIntervalMs } from "../src/stuck.js";

const ENV_KEY = "MOP_PM_WAIT_NUDGE_INTERVAL_MS";

function withEnv(value: string | undefined, fn: () => void): void {
  const previous = process.env[ENV_KEY];
  if (value === undefined) delete process.env[ENV_KEY];
  else process.env[ENV_KEY] = value;
  try {
    fn();
  } finally {
    if (previous === undefined) delete process.env[ENV_KEY];
    else process.env[ENV_KEY] = previous;
  }
}

test("getPmWaitNudgeIntervalMs honors a valid env override", () => {
  withEnv("300000", () => {
    assert.equal(getPmWaitNudgeIntervalMs(), 300_000);
  });
});

test("getPmWaitNudgeIntervalMs falls back to the 30-minute default when unset", () => {
  withEnv(undefined, () => {
    assert.equal(getPmWaitNudgeIntervalMs(), 30 * 60 * 1000);
  });
});

test("getPmWaitNudgeIntervalMs falls back to the 30-minute default when empty", () => {
  withEnv("", () => {
    assert.equal(getPmWaitNudgeIntervalMs(), 30 * 60 * 1000);
  });
});

test("getPmWaitNudgeIntervalMs falls back to the 30-minute default on non-numeric input", () => {
  withEnv("not-a-number", () => {
    assert.equal(getPmWaitNudgeIntervalMs(), 30 * 60 * 1000);
  });
});

test("getPmWaitNudgeIntervalMs falls back to the 30-minute default on a zero or negative value", () => {
  withEnv("0", () => {
    assert.equal(getPmWaitNudgeIntervalMs(), 30 * 60 * 1000);
  });
  withEnv("-5000", () => {
    assert.equal(getPmWaitNudgeIntervalMs(), 30 * 60 * 1000);
  });
});

test("getPmWaitNudgeIntervalMs floors a too-small positive value at 60000ms", () => {
  withEnv("1000", () => {
    assert.equal(getPmWaitNudgeIntervalMs(), 60_000);
  });
});

test("getPmWaitNudgeIntervalMs passes through a value exactly at the floor", () => {
  withEnv("60000", () => {
    assert.equal(getPmWaitNudgeIntervalMs(), 60_000);
  });
});
