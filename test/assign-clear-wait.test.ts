import assert from "node:assert/strict";
import test from "node:test";

import {
  clearSlotWhenIdle,
  type ClearWaitActivity,
  type ClearWaitDependencies,
} from "../src/assignmentClearWait.js";

const OPTS = { idleTimeoutMs: 10_000, ackTimeoutMs: 10_000, pollMs: 1_000 };

function fake(script: {
  activity: ClearWaitActivity[];
  ackAfterPolls?: number | null;
  sendOk?: boolean;
}) {
  let clock = 0;
  let activityIndex = 0;
  let events = 5;
  let clearSentAt: number | null = null;
  let ackPolls = 0;
  const log: string[] = [];
  const deps: ClearWaitDependencies = {
    getActivity: async () => {
      const value = script.activity[Math.min(activityIndex, script.activity.length - 1)];
      activityIndex += 1;
      log.push(`activity:${value}`);
      return value;
    },
    latestEventId: () => events,
    findClearAck: (_slot, afterId) => {
      if (clearSentAt === null) return null;
      ackPolls += 1;
      const need = script.ackAfterPolls;
      if (need === null || need === undefined) return null;
      if (ackPolls >= need) {
        events += 1;
        assert.ok(events > afterId);
        return events;
      }
      return null;
    },
    sendClear: async () => {
      log.push("send_clear");
      clearSentAt = clock;
      return script.sendOk === false ? { ok: false, detail: "pane gone" } : { ok: true };
    },
    sleep: async (ms) => {
      clock += ms;
    },
    now: () => clock,
  };
  return { deps, log };
}

test("waits for idle BEFORE sending /clear, then for SessionStart:clear + idle", async () => {
  const f = fake({ activity: ["active", "active", "idle", "active", "idle"], ackAfterPolls: 3 });
  const result = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(result.ok, true);
  const sendIndex = f.log.indexOf("send_clear");
  assert.equal(sendIndex, 3, "clear is sent only after the first idle observation");
  assert.deepEqual(f.log.slice(0, 3), ["activity:active", "activity:active", "activity:idle"]);
  assert.ok(f.log.slice(sendIndex + 1).includes("activity:idle"), "idle re-checked after the clear ack");
  if (result.ok) assert.ok(result.clear_ack_event_id > result.clear_marker_event_id);
});

test("slot never idle: typed clear_wait refusal and NO clear is sent", async () => {
  const f = fake({ activity: ["active"], ackAfterPolls: 1 });
  const result = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.step, "clear_wait");
    assert.match(result.reason, /slot_not_idle_before_clear/);
  }
  assert.ok(!f.log.includes("send_clear"));
});

test("clear never acknowledged: typed clear_wait refusal, never success", async () => {
  const f = fake({ activity: ["idle"], ackAfterPolls: null });
  const result = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.step, "clear_wait");
    assert.match(result.reason, /no_session_start_clear/);
  }
});

test("clear acknowledged but slot stays busy: clear_wait refusal", async () => {
  const f = fake({ activity: ["idle", "active"], ackAfterPolls: 1 });
  const result = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.reason, /slot_not_idle_after_clear/);
});

test("send failure is a clear_send result, not clear_wait", async () => {
  const f = fake({ activity: ["idle"], ackAfterPolls: 1, sendOk: false });
  const result = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.step, "clear_send");
});
