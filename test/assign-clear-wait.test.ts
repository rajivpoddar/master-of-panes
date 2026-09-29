import assert from "node:assert/strict";
import test from "node:test";

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import {
  clearSlotWhenIdle,
  persistentClearPending,
  type ClearWaitActivity,
  type ClearWaitDependencies,
} from "../src/assignmentClearWait.js";

const OPTS = { idleTimeoutMs: 10_000, ackTimeoutMs: 10_000, pollMs: 1_000 };

/**
 * A fake pane. Each /clear is queued; `ackAfterPolls` ack-polls after a send,
 * the pane emits one SessionStart:clear for it (null = never).
 */
function fake(script: {
  activity: ClearWaitActivity[] | (() => ClearWaitActivity);
  ackAfterPolls?: number | null;
  sendOk?: boolean;
}) {
  let clock = 0;
  let activityIndex = 0;
  let nextEventId = 5;
  const acks: number[] = [];
  const log: string[] = [];
  let polls = 0;
  const state = {
    ackPolicy: script.ackAfterPolls,
    /** Emit a SessionStart:clear right now (e.g. a stale ack from an earlier attempt). */
    emitAck: () => {
      nextEventId += 1;
      acks.push(nextEventId);
      log.push(`ack:${nextEventId}`);
    },
  };
  const deps: ClearWaitDependencies = {
    getActivity: async () => {
      const value = typeof script.activity === "function"
        ? script.activity()
        : script.activity[Math.min(activityIndex, script.activity.length - 1)];
      activityIndex += 1;
      log.push(`activity:${value}`);
      return value;
    },
    latestEventId: () => nextEventId,
    countClearAcks: (_slot, afterId) => {
      polls += 1;
      if (state.ackPolicy !== null && state.ackPolicy !== undefined && polls === state.ackPolicy) {
        state.emitAck();
      }
      const newer = acks.filter((id) => id > afterId);
      return { count: newer.length, lastId: newer.length ? Math.max(...newer) : 0 };
    },
    pending: new Map(),
    sendClear: async () => {
      log.push("send_clear");
      nextEventId += 1;
      polls = 0;
      return script.sendOk === false ? { ok: false, detail: "pane gone" } : { ok: true };
    },
    sleep: async (ms) => {
      clock += ms;
    },
    now: () => clock,
  };
  return { deps, log, state };
}

test("waits for idle BEFORE sending /clear, then for SessionStart:clear + idle", async () => {
  const f = fake({ activity: ["active", "active", "idle", "active", "idle"], ackAfterPolls: 3 });
  const result = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(result.ok, true);
  const sendIndex = f.log.indexOf("send_clear");
  assert.deepEqual(f.log.slice(0, 3), ["activity:active", "activity:active", "activity:idle"]);
  assert.equal(sendIndex, 3, "clear is sent only after the first idle observation");
  assert.ok(f.log.slice(sendIndex + 1).includes("activity:idle"), "idle re-checked after the clear ack");
  if (result.ok) assert.ok(result.clear_ack_event_id > result.clear_marker_event_id);
  assert.equal(f.deps.pending.size, 0);
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
  assert.equal(f.deps.pending.get(4)?.outstanding, 1, "the unacked clear stays outstanding");
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

test("P1: a stale ack from attempt 1 arriving during attempt 2 settles attempt 1 only", async () => {
  const f = fake({ activity: ["idle"], ackAfterPolls: null });
  const first = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(first.ok, false, "attempt 1 times out with its clear still queued");

  // Attempt 2: attempt 1's late ack lands while attempt 2 is settling.
  f.state.ackPolicy = 2;
  const send = f.deps.sendClear;
  f.deps.sendClear = async (slot) => {
    f.state.ackPolicy = null; // attempt 2's own clear never acks in this window
    return send(slot);
  };
  const second = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(second.ok, false, "the stale ack must not satisfy attempt 2");
  if (!second.ok) assert.match(second.reason, /no_session_start_clear/);
  const sendsAfterStaleAck = f.log.slice(f.log.findIndex((e) => e.startsWith("ack:")));
  assert.ok(sendsAfterStaleAck.includes("send_clear"), "attempt 2 sends its clear only after attempt 1 settled");
  // No success -> the caller never delivers, so no packet can be wiped.
});

test("P1: a stale ack arriving AFTER attempt 2's clear still cannot satisfy attempt 2", async () => {
  const f = fake({ activity: ["idle"], ackAfterPolls: null });
  assert.equal((await clearSlotWhenIdle(4, f.deps, OPTS)).ok, false);

  // Attempt 1 never acks during settle (slot idle) -> attempt 2 sends; then
  // exactly ONE ack arrives (attempt 1's, late). Two are required.
  f.state.ackPolicy = null;
  let sends = 0;
  const origSend = f.deps.sendClear;
  f.deps.sendClear = async (slot) => {
    sends += 1;
    const r = await origSend(slot);
    f.state.ackPolicy = 1; // one ack arrives after this send
    return r;
  };
  const policyOnce = f.deps.countClearAcks;
  let emitted = false;
  f.deps.countClearAcks = (slot, afterId) => {
    const r = policyOnce(slot, afterId);
    if (f.state.ackPolicy === 1 && !emitted) emitted = true;
    if (emitted) f.state.ackPolicy = null;
    return r;
  };
  const second = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(sends, 1);
  assert.equal(second.ok, false, "one late ack for two outstanding clears is not enough");
  assert.equal(f.deps.pending.get(4)?.outstanding, 2);

  // Attempt 2's own ack finally lands -> attempt 3 settles, then clears cleanly.
  f.state.emitAck();
  f.deps.sendClear = origSend;
  f.deps.countClearAcks = policyOnce;
  f.state.ackPolicy = 1;
  const third = await clearSlotWhenIdle(4, f.deps, OPTS);
  assert.equal(third.ok, true);
});

test("P1: an outstanding clear persisted before a MoP restart is honored after it (no duplicate /clear)", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-clear-pending-"));
  const dbPath = join(directory, "mop.db");
  try {
    // Before restart: /clear sent, pane never acks within the window.
    const before = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
    const f = fake({ activity: ["idle"], ackAfterPolls: null });
    f.deps.pending = persistentClearPending(before);
    assert.equal((await clearSlotWhenIdle(4, f.deps, OPTS)).ok, false);
    before.close();
    assert.equal(f.log.filter((e) => e === "send_clear").length, 1);

    // Simulated restart: fresh process state, same SQLite file. The first
    // clear is still queued on a busy pane, so a retry must NOT send again.
    const after = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
    try {
      f.deps.pending = persistentClearPending(after);
      assert.equal(f.deps.pending.get(4)?.outstanding, 1, "marker reloaded from SQLite");
      f.deps.getActivity = async () => "active";
      const retry = await clearSlotWhenIdle(4, f.deps, OPTS);
      assert.equal(retry.ok, false);
      if (!retry.ok) assert.match(retry.reason, /prior_clear_unsettled/);
      assert.equal(f.log.filter((e) => e === "send_clear").length, 1, "no duplicate /clear after restart");

      // The ack then lands: the persisted marker settles and is cleared.
      f.state.emitAck();
      f.deps.getActivity = async () => "idle";
      f.state.ackPolicy = 1;
      const settled = await clearSlotWhenIdle(4, f.deps, OPTS);
      assert.equal(settled.ok, true);
      assert.equal(persistentClearPending(after).get(4), undefined, "marker removed on ack");
    } finally {
      after.close();
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
