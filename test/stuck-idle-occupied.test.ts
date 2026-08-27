import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import type { MoPDatabase } from "../src/db.js";
import type { LogManager } from "../src/logs.js";
import type { TmuxRelay } from "../src/relay.js";
import { StuckDetector } from "../src/stuck.js";
import type { EventLogEntry, SlotState } from "../src/types.js";

const NOW = Date.parse("2026-07-27T02:30:00.000Z");
const OLD_IDLE = "2026-07-27T02:24:00.000";
const NEW_IDLE_PROMPT = "2026-07-27T02:24:30.000";
const SESSION_ID = "session-2";

function expectedNudge(
  waitAgeMinutes: number,
  urgency: string,
  idleAnchor = OLD_IDLE,
): string {
  return (
    "Use Skill(pm-wait-nudge) now with slot=2 assignment_epoch=4 pr=7001 " +
    "issue=7000 branch=fix/7000 head=" + "a".repeat(40) +
    ` wait_started_at=${idleAnchor} wait_age_minutes=${waitAgeMinutes} ` +
    `urgency=${urgency}. Classify PM_WAIT vs LOCAL_CONTINUE. If ` +
    "LOCAL_CONTINUE, continue the exact unfinished phase NOW: edits → " +
    "affected tests → commit → push; do not end the turn without a new " +
    "head, a typed blocker, or a terminal receipt; classification-only or " +
    "\"will continue\" prose is a violation. API timeouts and interrupted " +
    "local work are not PM waits."
  );
}

function slot(overrides: Partial<SlotState> = {}): SlotState {
  return {
    slot: 2,
    address: "0:0.2",
    name: "Hasta",
    status: "active",
    occupied: true,
    session_id: "session-2",
    task: "issue 7000",
    issue: 7000,
    branch: "fix/7000",
    pr: 7001,
    head_sha: "a".repeat(40),
    assignment_epoch: 4,
    assigned_at: "2026-07-27T02:20:00.000Z",
    last_activity: "2026-07-27T02:29:59.000",
    dnd: false,
    idle: true,
    activity: null,
    active_turn_id: null,
    active_turn_started_at: null,
    active_turn_state: "inactive",
    last_meaningful_work_at: "2026-07-27T02:23:59.000Z",
    ...overrides,
  };
}

function freeSlot(overrides: Partial<SlotState> = {}): SlotState {
  return slot({
    status: "free",
    occupied: false,
    session_id: null,
    task: null,
    issue: null,
    branch: null,
    pr: null,
    head_sha: null,
    assigned_at: null,
    last_activity: OLD_IDLE,
    ...overrides,
  });
}

function installGate(result: Record<string, unknown>): { path: string; cleanup: () => void } {
  const directory = mkdtempSync(join(tmpdir(), "mop-free-slot-gate-"));
  const path = join(directory, "gate.py");
  const payload = Buffer.from(JSON.stringify(result), "utf8").toString("base64");
  writeFileSync(
    path,
    `#!/usr/bin/env python3\nimport base64\nprint(base64.b64decode("${payload}").decode())\n`,
  );
  chmodSync(path, 0o755);
  return { path, cleanup: () => rmSync(directory, { recursive: true, force: true }) };
}

interface Harness {
  detector: StuckDetector;
  events: EventLogEntry[];
  sends: string[];
  setSlotReads: (slots: SlotState[]) => void;
  setLogMtime: (date: Date | null) => void;
}

function harness(currentSlot: SlotState, allSlots: SlotState[] = [currentSlot]): Harness {
  let slotReads: SlotState[] = [];
  let logMtime: Date | null = new Date(NOW);
  let nextEventId = 2;
  const sends: string[] = [];
  const events: EventLogEntry[] = [{
    id: 1,
    timestamp: OLD_IDLE,
    slot: currentSlot.slot,
    event_type: "Stop",
    hook_type: "Stop",
    tool_name: null,
    payload: "{}",
    processed: false,
  }];

  const db = {
    getExitPending: () => false,
    hasPendingClear: () => false,
    hasRecentSubagentDispatch: () => null,
    getSlot: () => slotReads.shift() ?? currentSlot,
    getAllSlots: () => allSlots,
    getEvents: (_slot: number, limit: number, eventType?: string) =>
      events
        .filter((event) => !eventType || event.event_type === eventType)
        .sort((a, b) => b.id - a.id)
        .slice(0, limit),
    logEvent: (
      eventSlot: number,
      eventType: string,
      hookType: string | null,
      toolName: string | null,
      payload: Record<string, unknown>,
    ) => {
      events.push({
        id: nextEventId++,
        timestamp: new Date(NOW).toISOString().replace(/Z$/, ""),
        slot: eventSlot,
        event_type: eventType,
        hook_type: hookType,
        tool_name: toolName,
        payload: JSON.stringify(payload),
        processed: false,
      });
      return nextEventId - 1;
    },
  } as unknown as MoPDatabase;

  const relay = {
    getSlotActivityState: async () => {
      throw new Error("nudge path must not inspect terminal prompt state");
    },
    sendToSlotAsync: async (_slot: number, command: string) => {
      sends.push(command);
      return true;
    },
  } as unknown as TmuxRelay;
  const logManager = {
    getLogMtime: async () => logMtime,
  } as unknown as LogManager;

  return {
    detector: new StuckDetector(db, logManager, relay),
    events,
    sends,
    setSlotReads: (slots) => {
      slotReads = [...slots];
    },
    setLogMtime: (date) => {
      logMtime = date;
    },
  };
}

test("suppresses a gate recommendation for an actively owned PR", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: true,
    slot: 4,
    recommendation_kind: "slot_e2e",
    rework_packet_count: 0,
    rework_pr_count: 0,
    ready_pool_size: 1,
    recommended_obligation_id: 12,
    recommended_pr: 7468,
    recommended_issue: 7436,
    recommended_packet: "packet-7468",
    recommended_run_id: null,
    recommended_ci_url: null,
    recommended_action: "assign slot e2e",
    recommended_category: "numbered-slot-e2e",
    slot_dispatch_wedge_id: null,
    reason: "authoritative_numbered_slot_e2e_boundary_available",
  });
  Date.now = () => NOW + 6 * 60_000;
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const free = freeSlot({ slot: 4, assignment_epoch: 578 });
    const activeOwner = slot({ slot: 1, pr: 7468, assignment_epoch: 597 });
    const h = harness(free, [activeOwner, free]);
    h.events[0] = { ...h.events[0], event_type: "slot_released" };
    await h.detector.checkIdleFree(free);
    assert.deepEqual(h.sends, []);
    const failures = h.events.filter(
      (event) => event.event_type === "idle_free_assignment_gate_failed",
    );
    assert.equal(failures.length, 1);
    assert.match(JSON.parse(failures[0].payload).error, /occupied PR 7468/);
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});

test("fails closed for malformed or missing PR recommendations", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  Date.now = () => NOW + 6 * 60_000;
  const malformed: Array<[string, unknown]> = [
    ["string", "7468"],
    ["boolean", true],
    ["zero", 0],
    ["negative", -1],
    ["fractional", 7468.5],
    ["missing", undefined],
  ];
  try {
    for (const [label, recommendedPr] of malformed) {
      const result: Record<string, unknown> = {
        allowed: true,
        slot: 4,
        recommendation_kind: "slot_e2e",
        rework_packet_count: 0,
        rework_pr_count: 0,
        ready_pool_size: 0,
        recommended_obligation_id: 12,
        recommended_issue: 7436,
        recommended_packet: "packet-7468",
        recommended_run_id: null,
        recommended_ci_url: null,
        recommended_action: "assign slot e2e",
        slot_dispatch_wedge_id: null,
        reason: "authoritative_numbered_slot_e2e_boundary_available",
      };
      if (recommendedPr !== undefined) result.recommended_pr = recommendedPr;
      const gate = installGate(result);
      process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
      try {
        const candidate = freeSlot({ slot: 4 });
        const h = harness(candidate);
        h.events[0] = { ...h.events[0], event_type: "slot_released" };
        await h.detector.checkIdleFree(candidate);
        assert.deepEqual(h.sends, [], label);
        const failures = h.events.filter(
          (event) => event.event_type === "idle_free_assignment_gate_failed",
        );
        assert.equal(failures.length, 1, label);
        assert.match(JSON.parse(failures[0].payload).error, /recommended_pr/, label);
      } finally {
        gate.cleanup();
      }
    }
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
  }
});

test("the idle-occupied nudge carries the terminal LOCAL_CONTINUE directive", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const h = harness(slot());
    await h.detector.checkIdleOccupied(slot());

    assert.equal(h.sends.length, 1);
    assert.match(
      h.sends[0],
      /continue the exact unfinished phase NOW: edits → affected tests → commit → push/,
    );
    assert.match(
      h.sends[0],
      /do not end the turn without a new head, a typed blocker, or a terminal receipt/,
    );
    assert.match(
      h.sends[0],
      /classification-only or "will continue" prose is a violation/,
    );
    assert.doesNotMatch(h.sends[0], /resume the existing work now/);
  } finally {
    Date.now = originalNow;
  }
});

test("nudges an occupied idle dev slot once per idle episode", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const h = harness(slot());
    await h.detector.checkIdleOccupied(slot());
    await h.detector.checkIdleOccupied(slot());

    assert.deepEqual(h.sends, [
      expectedNudge(6, "REMINDER"),
    ]);
    const injected = h.events.filter(
      (event) => event.event_type === "idle_occupied_continue_injected",
    );
    assert.equal(injected.length, 1);
    assert.deepEqual(JSON.parse(injected[0].payload), {
      command: expectedNudge(6, "REMINDER"),
      assignment_epoch: 4,
      idle_anchor: OLD_IDLE,
      idle_anchor_source: "Stop",
      idle_age_ms: 360_000,
      wait_anchor: OLD_IDLE,
      wait_anchor_source: "Stop",
      wait_age_ms: 360_000,
      wait_age_minutes: 6,
      urgency: "REMINDER",
      turn_state: "inactive",
      issue: 7000,
      pr: 7001,
      branch: "fix/7000",
    });
  } finally {
    Date.now = originalNow;
  }
});

test("a matching idle prompt starts a new idle episode after an earlier nudge", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const h = harness(slot());
    h.events.push(
      {
        id: 2,
        timestamp: "2026-07-27T02:29:00.000",
        slot: 2,
        event_type: "idle_occupied_continue_injected",
        hook_type: "Stuck",
        tool_name: null,
        payload: JSON.stringify({
          assignment_epoch: 4,
          idle_anchor: OLD_IDLE,
        }),
        processed: false,
      },
      {
        id: 3,
        timestamp: NEW_IDLE_PROMPT,
        slot: 2,
        event_type: "idle_prompt_turn_finished",
        hook_type: "Notification",
        tool_name: null,
        payload: "{}",
        processed: false,
      },
    );

    await h.detector.checkIdleOccupied(slot());

    assert.deepEqual(h.sends, [
      expectedNudge(5, "REMINDER", NEW_IDLE_PROMPT),
    ]);
    const latest = h.events.filter(
      (event) => event.event_type === "idle_occupied_continue_injected",
    ).at(-1);
    assert.ok(latest);
    assert.equal(JSON.parse(latest.payload).idle_anchor, NEW_IDLE_PROMPT);
    assert.equal(JSON.parse(latest.payload).idle_anchor_source, "idle_prompt_turn_finished");
  } finally {
    Date.now = originalNow;
  }
});

test("keeps cumulative wait age when a nudge turn ends in PM_WAIT", async () => {
  const originalNow = Date.now;
  Date.now = () => Date.parse("2026-07-27T02:36:01.000Z");
  try {
    const h = harness(slot());
    h.events.push(
      {
        id: 2,
        timestamp: "2026-07-27T02:30:00.000",
        slot: 2,
        event_type: "idle_occupied_continue_injected",
        hook_type: "Stuck",
        tool_name: null,
        payload: JSON.stringify({
          assignment_epoch: 4,
          idle_anchor: OLD_IDLE,
        }),
        processed: false,
      },
      {
        id: 3,
        timestamp: "2026-07-27T02:30:05.000",
        slot: 2,
        event_type: "UserPromptSubmit",
        hook_type: "UserPromptSubmit",
        tool_name: null,
        payload: JSON.stringify({ session_id: SESSION_ID }),
        processed: false,
      },
      {
        id: 4,
        timestamp: "2026-07-27T02:30:30.000",
        slot: 2,
        event_type: "Stop",
        hook_type: "Stop",
        tool_name: null,
        payload: JSON.stringify({
          session_id: SESSION_ID,
          transcript: "PM_WAIT_NUDGE_RESULT classification=PM_WAIT action=reminded_pm waiting=6m urgency=REMINDER",
        }),
        processed: false,
      },
      {
        id: 5,
        timestamp: "2026-07-27T02:31:00.000",
        slot: 2,
        event_type: "idle_prompt_turn_finished",
        hook_type: "Notification",
        tool_name: null,
        payload: JSON.stringify({ notification_session_id: SESSION_ID }),
        processed: false,
      },
    );

    await h.detector.checkIdleOccupied(slot());

    assert.deepEqual(h.sends, [expectedNudge(12, "REMINDER")]);
    const latest = h.events.filter(
      (event) => event.event_type === "idle_occupied_continue_injected",
    ).at(-1);
    assert.ok(latest);
    assert.deepEqual(JSON.parse(latest.payload), {
      command: expectedNudge(12, "REMINDER"),
      assignment_epoch: 4,
      idle_anchor: "2026-07-27T02:31:00.000",
      idle_anchor_source: "idle_prompt_turn_finished",
      idle_age_ms: 301_000,
      wait_anchor: OLD_IDLE,
      wait_anchor_source: "pm_wait_nudge_carry",
      wait_age_ms: 721_000,
      wait_age_minutes: 12,
      urgency: "REMINDER",
      turn_state: "inactive",
      issue: 7000,
      pr: 7001,
      branch: "fix/7000",
    });
  } finally {
    Date.now = originalNow;
  }
});

test("keeps the original wait start across repeated PM_WAIT nudge turns", async () => {
  const originalNow = Date.now;
  Date.now = () => Date.parse("2026-07-27T02:42:01.000Z");
  try {
    const h = harness(slot());
    h.events.push(
      {
        id: 2,
        timestamp: "2026-07-27T02:30:00.000",
        slot: 2,
        event_type: "idle_occupied_continue_injected",
        hook_type: "Stuck",
        tool_name: null,
        payload: JSON.stringify({
          assignment_epoch: 4,
          idle_anchor: OLD_IDLE,
          wait_anchor: OLD_IDLE,
          wait_anchor_source: "Stop",
        }),
        processed: false,
      },
      {
        id: 3,
        timestamp: "2026-07-27T02:30:05.000",
        slot: 2,
        event_type: "UserPromptSubmit",
        hook_type: "UserPromptSubmit",
        tool_name: null,
        payload: JSON.stringify({ session_id: SESSION_ID }),
        processed: false,
      },
      {
        id: 4,
        timestamp: "2026-07-27T02:30:30.000",
        slot: 2,
        event_type: "Stop",
        hook_type: "Stop",
        tool_name: null,
        payload: JSON.stringify({
          session_id: SESSION_ID,
          transcript: "PM_WAIT_NUDGE_RESULT classification=PM_WAIT action=reminded_pm waiting=6m urgency=REMINDER",
        }),
        processed: false,
      },
      {
        id: 5,
        timestamp: "2026-07-27T02:31:00.000",
        slot: 2,
        event_type: "idle_prompt_turn_finished",
        hook_type: "Notification",
        tool_name: null,
        payload: JSON.stringify({ notification_session_id: SESSION_ID }),
        processed: false,
      },
      {
        id: 6,
        timestamp: "2026-07-27T02:36:00.000",
        slot: 2,
        event_type: "idle_occupied_continue_injected",
        hook_type: "Stuck",
        tool_name: null,
        payload: JSON.stringify({
          assignment_epoch: 4,
          idle_anchor: "2026-07-27T02:31:00.000",
          wait_anchor: OLD_IDLE,
          wait_anchor_source: "Stop",
        }),
        processed: false,
      },
      {
        id: 7,
        timestamp: "2026-07-27T02:36:05.000",
        slot: 2,
        event_type: "UserPromptSubmit",
        hook_type: "UserPromptSubmit",
        tool_name: null,
        payload: JSON.stringify({ session_id: SESSION_ID }),
        processed: false,
      },
      {
        id: 8,
        timestamp: "2026-07-27T02:36:30.000",
        slot: 2,
        event_type: "Stop",
        hook_type: "Stop",
        tool_name: null,
        payload: JSON.stringify({
          session_id: SESSION_ID,
          transcript: "PM_WAIT_NUDGE_RESULT classification=PM_WAIT action=reminded_pm waiting=12m urgency=REMINDER",
        }),
        processed: false,
      },
      {
        id: 9,
        timestamp: "2026-07-27T02:37:00.000",
        slot: 2,
        event_type: "idle_prompt_turn_finished",
        hook_type: "Notification",
        tool_name: null,
        payload: JSON.stringify({ notification_session_id: SESSION_ID }),
        processed: false,
      },
    );

    await h.detector.checkIdleOccupied(slot());

    assert.deepEqual(h.sends, [expectedNudge(18, "FOLLOW_UP")]);
    const latest = h.events.filter(
      (event) => event.event_type === "idle_occupied_continue_injected",
    ).at(-1);
    assert.ok(latest);
    assert.deepEqual(JSON.parse(latest.payload), {
      command: expectedNudge(18, "FOLLOW_UP"),
      assignment_epoch: 4,
      idle_anchor: "2026-07-27T02:37:00.000",
      idle_anchor_source: "idle_prompt_turn_finished",
      idle_age_ms: 301_000,
      wait_anchor: OLD_IDLE,
      wait_anchor_source: "Stop",
      wait_age_ms: 1_081_000,
      wait_age_minutes: 18,
      urgency: "FOLLOW_UP",
      turn_state: "inactive",
      issue: 7000,
      pr: 7001,
      branch: "fix/7000",
    });
  } finally {
    Date.now = originalNow;
  }
});

test("resets wait age when normal work stops before a later PM_WAIT result", async () => {
  const originalNow = Date.now;
  Date.now = () => Date.parse("2026-07-27T02:36:01.000Z");
  try {
    const h = harness(slot());
    h.events.push(
      {
        id: 2,
        timestamp: "2026-07-27T02:30:00.000",
        slot: 2,
        event_type: "idle_occupied_continue_injected",
        hook_type: "Stuck",
        tool_name: null,
        payload: JSON.stringify({
          assignment_epoch: 4,
          idle_anchor: OLD_IDLE,
        }),
        processed: false,
      },
      {
        id: 3,
        timestamp: "2026-07-27T02:30:05.000",
        slot: 2,
        event_type: "UserPromptSubmit",
        hook_type: "UserPromptSubmit",
        tool_name: null,
        payload: JSON.stringify({ session_id: SESSION_ID }),
        processed: false,
      },
      {
        id: 4,
        timestamp: "2026-07-27T02:30:20.000",
        slot: 2,
        event_type: "Stop",
        hook_type: "Stop",
        tool_name: null,
        payload: JSON.stringify({
          session_id: SESSION_ID,
          transcript: "Finished another local validation step.",
        }),
        processed: false,
      },
      {
        id: 5,
        timestamp: "2026-07-27T02:30:30.000",
        slot: 2,
        event_type: "Stop",
        hook_type: "Stop",
        tool_name: null,
        payload: JSON.stringify({
          session_id: SESSION_ID,
          transcript: "PM_WAIT_NUDGE_RESULT classification=PM_WAIT action=reminded_pm waiting=6m urgency=REMINDER",
        }),
        processed: false,
      },
      {
        id: 6,
        timestamp: "2026-07-27T02:31:00.000",
        slot: 2,
        event_type: "idle_prompt_turn_finished",
        hook_type: "Notification",
        tool_name: null,
        payload: JSON.stringify({ notification_session_id: SESSION_ID }),
        processed: false,
      },
    );

    await h.detector.checkIdleOccupied(slot());

    assert.deepEqual(h.sends, [
      expectedNudge(5, "REMINDER", "2026-07-27T02:31:00.000"),
    ]);
  } finally {
    Date.now = originalNow;
  }
});

test("resets wait age when the prior nudge turn resumes local work", async () => {
  const originalNow = Date.now;
  Date.now = () => Date.parse("2026-07-27T02:36:01.000Z");
  try {
    const h = harness(slot());
    h.events.push(
      {
        id: 2,
        timestamp: "2026-07-27T02:30:00.000",
        slot: 2,
        event_type: "idle_occupied_continue_injected",
        hook_type: "Stuck",
        tool_name: null,
        payload: JSON.stringify({
          assignment_epoch: 4,
          idle_anchor: OLD_IDLE,
        }),
        processed: false,
      },
      {
        id: 3,
        timestamp: "2026-07-27T02:30:30.000",
        slot: 2,
        event_type: "Stop",
        hook_type: "Stop",
        tool_name: null,
        payload: JSON.stringify({
          transcript: "PM_WAIT_NUDGE_RESULT classification=LOCAL_CONTINUE action=resumed waiting=6m urgency=REMINDER",
        }),
        processed: false,
      },
      {
        id: 4,
        timestamp: "2026-07-27T02:31:00.000",
        slot: 2,
        event_type: "idle_prompt_turn_finished",
        hook_type: "Notification",
        tool_name: null,
        payload: "{}",
        processed: false,
      },
    );

    await h.detector.checkIdleOccupied(slot());

    assert.deepEqual(h.sends, [
      expectedNudge(5, "REMINDER", "2026-07-27T02:31:00.000"),
    ]);
  } finally {
    Date.now = originalNow;
  }
});

test("raises urgency with the occupied idle age", async () => {
  const originalNow = Date.now;
  try {
    const cases = [
      { minutes: 15, urgency: "FOLLOW_UP" },
      { minutes: 30, urgency: "URGENT" },
      { minutes: 60, urgency: "ESCALATION" },
    ];
    for (const item of cases) {
      Date.now = () => Date.parse(OLD_IDLE + "Z") + item.minutes * 60_000;
      const h = harness(slot());
      await h.detector.checkIdleOccupied(slot());
      assert.equal(h.sends.length, 1);
      assert.match(h.sends[0], new RegExp(`wait_age_minutes=${item.minutes}`));
      assert.match(h.sends[0], new RegExp(`urgency=${item.urgency}`));
    }
  } finally {
    Date.now = originalNow;
  }
});

test("uses authoritative inactive turn state when the legacy idle flag is stale", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const candidate = slot({ idle: false, active_turn_state: "inactive" });
    const h = harness(candidate);
    await h.detector.checkIdleOccupied(candidate);
    assert.deepEqual(h.sends, [expectedNudge(6, "REMINDER")]);
  } finally {
    Date.now = originalNow;
  }
});

test("does not compete with a specialized recovery in the same idle episode", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const h = harness(slot());
    h.events.push({
      id: 2,
      timestamp: "2026-07-27T02:25:00.000",
      slot: 2,
      event_type: "compact_dispatched",
      hook_type: null,
      tool_name: null,
      payload: "{}",
      processed: false,
    });
    await h.detector.checkIdleOccupied(slot());
    assert.deepEqual(h.sends, []);
  } finally {
    Date.now = originalNow;
  }
});

test("requires current ownership and an inactive turn", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const guarded = [
      slot({ slot: 0 }),
      slot({ occupied: false }),
      slot({ dnd: true }),
      slot({ active_turn_state: "active" }),
    ];
    for (const candidate of guarded) {
      const h = harness(candidate);
      await h.detector.checkIdleOccupied(candidate);
      assert.deepEqual(h.sends, []);
    }
  } finally {
    Date.now = originalNow;
  }
});

test("suppresses continuation when DND is enabled at the delivery boundary", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const initial = slot();
    const h = harness(initial);
    h.setSlotReads([
      initial,
      slot({ dnd: true }),
    ]);

    await h.detector.checkIdleOccupied(initial);

    assert.deepEqual(h.sends, []);
    const suppressed = h.events.filter(
      (event) => event.event_type === "continue_suppressed_slot_state",
    );
    assert.equal(suppressed.length, 1);
    assert.equal(JSON.parse(suppressed[0].payload).reason, "dnd");
  } finally {
    Date.now = originalNow;
  }
});

test("nudges a free idle slot once per urgency tier with the total free time", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: true,
    slot: 2,
    recommendation_kind: "rework",
    rework_packet_count: 3,
    rework_pr_count: 2,
    ready_pool_size: 3,
    recommended_obligation_id: 91,
    recommended_pr: 7001,
    recommended_issue: 7000,
    recommended_packet: "/tmp/rework-7001.md",
    recommended_action: "assign rework",
    slot_dispatch_wedge_id: 93,
    reason: "packet_backed_rework_available",
  });
  Date.now = () => NOW;
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const candidate = freeSlot();
    const h = harness(candidate);
    h.events[0] = { ...h.events[0], event_type: "slot_released" };
    await h.detector.checkIdleFree(candidate);
    await h.detector.checkIdleFree(candidate);

    assert.equal(h.sends.length, 1);
    assert.match(h.sends[0], /mode=FREE_WAIT_ASSIGNMENT slot=2/);
    assert.match(h.sends[0], /wait_started_at=2026-07-27T02:24:00.000/);
    assert.match(h.sends[0], /wait_age_minutes=6 urgency=REMINDER/);
    assert.match(h.sends[0], /recommendation_kind=rework/);
    assert.match(h.sends[0], /rework_packet_count=3 rework_pr_count=2 ready_pool_size=3/);
    assert.match(h.sends[0], /recommended_pr=7001 recommended_issue=7000/);
    assert.equal(
      h.events.filter((event) => event.event_type === "idle_free_assignment_nudge_injected").length,
      1,
    );
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});

test("MOP_PM_WAIT_NUDGES_DISABLED=1 suppresses occupied and free PM wait nudges", async () => {
  const previous = process.env.MOP_PM_WAIT_NUDGES_DISABLED;
  process.env.MOP_PM_WAIT_NUDGES_DISABLED = "1";
  try {
    const occupied = harness(slot());
    await occupied.detector.checkIdleOccupied(slot());
    assert.deepEqual(occupied.sends, []);

    const free = harness(freeSlot());
    await free.detector.checkIdleFree(freeSlot());
    assert.deepEqual(free.sends, []);
  } finally {
    if (previous === undefined) {
      delete process.env.MOP_PM_WAIT_NUDGES_DISABLED;
    } else {
      process.env.MOP_PM_WAIT_NUDGES_DISABLED = previous;
    }
  }
});

test("does not wake a free slot when the Ready Pool gate is closed", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: false,
    slot: 2,
    recommendation_kind: null,
    rework_packet_count: 0,
    rework_pr_count: 0,
    ready_pool_size: 0,
    recommended_obligation_id: null,
    recommended_pr: null,
    recommended_issue: null,
    recommended_packet: null,
    recommended_action: null,
    slot_dispatch_wedge_id: null,
    reason: "no_ready_pool_obligation",
  });
  Date.now = () => NOW;
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const candidate = freeSlot();
    const h = harness(candidate);
    h.events[0] = { ...h.events[0], event_type: "slot_released" };
    await h.detector.checkIdleFree(candidate);
    assert.deepEqual(h.sends, []);
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});

test("fails closed when the deleted external Ready Pool gate is unavailable", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  Date.now = () => NOW + 6 * 60_000;
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = join(
    tmpdir(),
    "pm-wait-nudge-deleted-ready-pool-assignment-gate.py",
  );
  try {
    const candidate = freeSlot({ slot: 1 });
    const h = harness(candidate);
    h.events[0] = { ...h.events[0], event_type: "slot_released" };
    await h.detector.checkIdleFree(candidate);
    assert.deepEqual(h.sends, []);
    const failures = h.events.filter(
      (event) => event.event_type === "idle_free_assignment_gate_failed",
    );
    assert.equal(failures.length, 1);
    assert.match(JSON.parse(failures[0].payload).error, /Command failed|var[/]folders/);
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
  }
});

test("does not send a free-slot nudge after concurrent assignment", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: true,
    slot: 2,
    recommendation_kind: "todo",
    rework_packet_count: 0,
    rework_pr_count: 0,
    ready_pool_size: 1,
    recommended_obligation_id: 101,
    recommended_pr: null,
    recommended_issue: 7000,
    recommended_packet: null,
    recommended_action: "assign todo",
    slot_dispatch_wedge_id: null,
    reason: "ready_pool_work_available",
  });
  Date.now = () => NOW;
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const candidate = freeSlot();
    const h = harness(candidate);
    h.events[0] = { ...h.events[0], event_type: "slot_released" };
    h.setSlotReads([slot({ assignment_epoch: 5 })]);
    await h.detector.checkIdleFree(candidate);
    assert.deepEqual(h.sends, []);
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});

test("an idempotent release replay keeps the t0 anchor and the FOLLOW_UP wait_started_at", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: true,
    slot: 2,
    recommendation_kind: "rework",
    rework_packet_count: 3,
    rework_pr_count: 2,
    ready_pool_size: 3,
    recommended_obligation_id: 91,
    recommended_pr: 7001,
    recommended_issue: 7000,
    recommended_packet: "/tmp/rework-7001.md",
    recommended_action: "assign rework",
    slot_dispatch_wedge_id: 93,
    reason: "packet_backed_rework_available",
  });
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const candidate = freeSlot();
    const h = harness(candidate);
    // Real occupied->free release at t0 carries the anchor.
    h.events[0] = {
      ...h.events[0],
      event_type: "slot_released",
      payload: JSON.stringify({ assignment_epoch: 4, idempotent: false }),
    };

    Date.now = () => Date.parse(OLD_IDLE + "Z") + 6 * 60_000;
    await h.detector.checkIdleFree(candidate);

    // A later release pass on the already-free slot logs a NEW slot_released
    // event marked idempotent. It must not become the wait anchor.
    h.events.push({
      id: 99,
      timestamp: "2026-07-27T02:34:00.000",
      slot: 2,
      event_type: "slot_released",
      hook_type: null,
      tool_name: null,
      payload: JSON.stringify({ assignment_epoch: 4, idempotent: true }),
      processed: false,
    });

    Date.now = () => Date.parse(OLD_IDLE + "Z") + 10 * 60_000;
    await h.detector.checkIdleFree(candidate);
    Date.now = () => Date.parse(OLD_IDLE + "Z") + 16 * 60_000;
    await h.detector.checkIdleFree(candidate);

    assert.equal(h.sends.length, 2);
    assert.match(h.sends[0], /wait_age_minutes=6 urgency=REMINDER/);
    assert.match(h.sends[1], /wait_age_minutes=16 urgency=FOLLOW_UP/);
    for (const command of h.sends) {
      assert.match(command, new RegExp(`wait_started_at=${OLD_IDLE}`));
    }
    const injected = h.events.filter(
      (event) => event.event_type === "idle_free_assignment_nudge_injected",
    );
    assert.equal(injected.length, 2);
    for (const event of injected) {
      assert.equal(JSON.parse(event.payload).free_anchor, OLD_IDLE);
    }
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});

test("a real release restarts the free-wait anchor", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: true,
    slot: 2,
    recommendation_kind: "rework",
    rework_packet_count: 3,
    rework_pr_count: 2,
    ready_pool_size: 3,
    recommended_obligation_id: 91,
    recommended_pr: 7001,
    recommended_issue: 7000,
    recommended_packet: "/tmp/rework-7001.md",
    recommended_action: "assign rework",
    slot_dispatch_wedge_id: 93,
    reason: "packet_backed_rework_available",
  });
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const candidate = freeSlot();
    const h = harness(candidate);
    h.events[0] = {
      ...h.events[0],
      event_type: "slot_released",
      payload: JSON.stringify({ assignment_epoch: 4, idempotent: false }),
    };

    Date.now = () => Date.parse(OLD_IDLE + "Z") + 6 * 60_000;
    await h.detector.checkIdleFree(candidate);

    // A NEW occupied->free transition (assign, work, real release) at
    // t0 + 10m legitimately starts a fresh free episode.
    h.events.push({
      id: 99,
      timestamp: "2026-07-27T02:34:00.000",
      slot: 2,
      event_type: "slot_released",
      hook_type: null,
      tool_name: null,
      payload: JSON.stringify({ assignment_epoch: 4, idempotent: false }),
      processed: false,
    });

    Date.now = () => Date.parse(OLD_IDLE + "Z") + 16 * 60_000;
    await h.detector.checkIdleFree(candidate);

    assert.equal(h.sends.length, 2);
    assert.match(h.sends[0], /wait_age_minutes=6 urgency=REMINDER/);
    assert.match(h.sends[0], new RegExp(`wait_started_at=${OLD_IDLE}`));
    assert.match(h.sends[1], /wait_age_minutes=6 urgency=REMINDER/);
    assert.match(h.sends[1], /wait_started_at=2026-07-27T02:34:00.000/);
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});

test("nudges a free idle slot after a slot_cleared marker (no slot_released)", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: true,
    slot: 2,
    recommendation_kind: "rework",
    rework_packet_count: 3,
    rework_pr_count: 2,
    ready_pool_size: 3,
    recommended_obligation_id: 91,
    recommended_pr: 7001,
    recommended_issue: 7000,
    recommended_packet: "/tmp/rework-7001.md",
    recommended_action: "assign rework",
    slot_dispatch_wedge_id: 93,
    reason: "packet_backed_rework_available",
  });
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const candidate = freeSlot();
    const h = harness(candidate);
    // Slot became free via a clear, not a release. The clear must establish
    // the free-wait anchor so the assignment nudge fires.
    h.events[0] = {
      ...h.events[0],
      event_type: "slot_cleared",
      payload: JSON.stringify({ assignment_epoch: 4, idempotent: false }),
    };
    Date.now = () => Date.parse(OLD_IDLE + "Z") + 6 * 60_000;
    await h.detector.checkIdleFree(candidate);

    assert.equal(h.sends.length, 1);
    assert.match(h.sends[0], /mode=FREE_WAIT_ASSIGNMENT slot=2/);
    assert.match(h.sends[0], /wait_started_at=2026-07-27T02:24:00.000/);
    assert.match(h.sends[0], /wait_age_minutes=6 urgency=REMINDER/);
    assert.equal(
      h.events.filter((event) => event.event_type === "idle_free_assignment_nudge_injected").length,
      1,
    );
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});

test("nudges a free idle slot with no free marker using a stable created watermark", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: true,
    slot: 2,
    recommendation_kind: "rework",
    rework_packet_count: 3,
    rework_pr_count: 2,
    ready_pool_size: 3,
    recommended_obligation_id: 91,
    recommended_pr: 7001,
    recommended_issue: 7000,
    recommended_packet: "/tmp/rework-7001.md",
    recommended_action: "assign rework",
    slot_dispatch_wedge_id: 93,
    reason: "packet_backed_rework_available",
  });
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const candidate = freeSlot();
    const h = harness(candidate);
    // No slot_released and no slot_cleared marker: the slot row is free but
    // MoP never recorded a free transition. The detector must still nudge by
    // writing one stable idle_free_anchor_created watermark on first
    // observation, then measuring the wait from it.
    Date.now = () => NOW;
    await h.detector.checkIdleFree(candidate);
    assert.equal(h.sends.length, 0); // just created the watermark; not yet 5m
    const created = h.events.filter((event) => event.event_type === "idle_free_anchor_created");
    assert.equal(created.length, 1);
    assert.equal(JSON.parse(created[0].payload).assignment_epoch, 4);

    // 10 minutes after the watermark, the nudge fires with a stable anchor.
    Date.now = () => NOW + 10 * 60_000;
    await h.detector.checkIdleFree(candidate);
    assert.equal(h.sends.length, 1);
    assert.match(h.sends[0], /mode=FREE_WAIT_ASSIGNMENT slot=2/);
    assert.match(h.sends[0], /wait_started_at=2026-07-27T02:30:00.000/);
    assert.match(h.sends[0], /wait_age_minutes=10 urgency=REMINDER/);
    assert.equal(
      h.events.filter((event) => event.event_type === "idle_free_assignment_nudge_injected").length,
      1,
    );
    // The watermark is not recreated on later checks (stable anchor + dedup).
    assert.equal(
      h.events.filter((event) => event.event_type === "idle_free_anchor_created").length,
      1,
    );
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});

test("escalates a free-slot PM reminder without resetting the release anchor", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: true,
    slot: 2,
    recommendation_kind: "todo",
    rework_packet_count: 0,
    rework_pr_count: 0,
    ready_pool_size: 1,
    recommended_obligation_id: 101,
    recommended_pr: null,
    recommended_issue: 7000,
    recommended_packet: null,
    recommended_action: "assign todo",
    slot_dispatch_wedge_id: null,
    reason: "ready_pool_work_available",
  });
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const candidate = freeSlot();
    const h = harness(candidate);
    h.events[0] = { ...h.events[0], event_type: "slot_released" };

    Date.now = () => Date.parse(OLD_IDLE + "Z") + 6 * 60_000;
    await h.detector.checkIdleFree(candidate);
    Date.now = () => Date.parse(OLD_IDLE + "Z") + 10 * 60_000;
    await h.detector.checkIdleFree(candidate);
    // Idempotent release replay at t0 + 10m must not reset the anchor mid-tier.
    h.events.push({
      id: 99,
      timestamp: "2026-07-27T02:34:00.000",
      slot: 2,
      event_type: "slot_released",
      hook_type: null,
      tool_name: null,
      payload: JSON.stringify({ assignment_epoch: 4, idempotent: true }),
      processed: false,
    });
    Date.now = () => Date.parse(OLD_IDLE + "Z") + 16 * 60_000;
    await h.detector.checkIdleFree(candidate);
    Date.now = () => Date.parse(OLD_IDLE + "Z") + 31 * 60_000;
    await h.detector.checkIdleFree(candidate);
    Date.now = () => Date.parse(OLD_IDLE + "Z") + 61 * 60_000;
    await h.detector.checkIdleFree(candidate);

    assert.equal(h.sends.length, 4);
    assert.match(h.sends[0], /wait_age_minutes=6 urgency=REMINDER/);
    assert.match(h.sends[1], /wait_age_minutes=16 urgency=FOLLOW_UP/);
    assert.match(h.sends[2], /wait_age_minutes=31 urgency=URGENT/);
    assert.match(h.sends[3], /wait_age_minutes=61 urgency=ESCALATION/);
    for (const command of h.sends) {
      assert.match(command, /wait_started_at=2026-07-27T02:24:00.000/);
    }
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});

test("does not nudge an active slot when stale log mtime lacks a hook boundary", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const candidate = slot({
      active_turn_state: "active",
      idle: false,
      active_turn_started_at: null,
    });
    const h = harness(candidate);
    h.setLogMtime(new Date(Date.parse(OLD_IDLE + "Z")));
    await h.detector.checkIdleOccupied(candidate);

    assert.equal(h.sends.length, 0);
    const wedged = h.events.filter(
      (event) => event.event_type === "idle_occupied_wedge_detected",
    );
    assert.equal(wedged.length, 0);
    assert.equal(h.events.filter(
      (event) => event.event_type === "idle_occupied_continue_injected",
    ).length, 0);
  } finally {
    Date.now = originalNow;
  }
});

test("does not nudge an indeterminate slot when stale log mtime lacks a hook boundary", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const candidate = slot({
      active_turn_state: "indeterminate",
      idle: false,
      active_turn_started_at: null,
    });
    const h = harness(candidate);
    h.setLogMtime(new Date(Date.parse(OLD_IDLE + "Z")));
    await h.detector.checkIdleOccupied(candidate);

    assert.equal(h.sends.length, 0);
    assert.equal(h.events.filter(
      (event) => event.event_type === "idle_occupied_continue_injected",
    ).length, 0);
  } finally {
    Date.now = originalNow;
  }
});

test("does not nudge an active slot with a fresh JSONL write", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const candidate = slot({
      active_turn_state: "active",
      idle: false,
      active_turn_started_at: null,
    });
    const h = harness(candidate);
    h.setLogMtime(new Date(NOW - 1_000));
    await h.detector.checkIdleOccupied(candidate);
    assert.deepEqual(h.sends, []);
  } finally {
    Date.now = originalNow;
  }
});

test("requires strictly more than five minutes and dedupes the idle episode", async () => {
  const originalNow = Date.now;
  const baseMs = Date.parse(OLD_IDLE + "Z");
  try {
    const candidate = slot();
    const h = harness(candidate);
    Date.now = () => baseMs + 5 * 60_000;
    await h.detector.checkIdleOccupied(candidate);
    assert.deepEqual(h.sends, []);

    Date.now = () => baseMs + 5 * 60_000 + 1;
    await h.detector.checkIdleOccupied(candidate);
    await h.detector.checkIdleOccupied(candidate);
    assert.equal(h.sends.length, 1);
  } finally {
    Date.now = originalNow;
  }
});

test("a UserPromptSubmit suppresses an old episode until a later Stop starts a new one", async () => {
  const originalNow = Date.now;
  const baseMs = Date.parse(OLD_IDLE + "Z");
  try {
    const h = harness(slot());
    Date.now = () => baseMs + 6 * 60_000;
    await h.detector.checkIdleOccupied(slot());
    assert.equal(h.sends.length, 1);

    const active = slot({
      active_turn_state: "active",
      active_turn_started_at: "2026-07-27T02:30:01.000Z",
      idle: false,
    });
    Date.now = () => baseMs + 7 * 60_000;
    await h.detector.checkIdleOccupied(active);
    assert.equal(h.sends.length, 1);

    h.events.push({
      id: 20,
      timestamp: "2026-07-27T02:31:00.000",
      slot: 2,
      event_type: "UserPromptSubmit",
      hook_type: "UserPromptSubmit",
      tool_name: null,
      payload: JSON.stringify({ session_id: SESSION_ID }),
      processed: false,
    }, {
      id: 21,
      timestamp: "2026-07-27T02:31:01.000",
      slot: 2,
      event_type: "Stop",
      hook_type: "Stop",
      tool_name: null,
      payload: JSON.stringify({ session_id: SESSION_ID }),
      processed: false,
    });
    Date.now = () => baseMs + 13 * 60_000 + 1;
    await h.detector.checkIdleOccupied(slot());
    assert.equal(h.sends.length, 2);
    assert.match(h.sends[1], /wait_started_at=2026-07-27T02:31:01.000/);
  } finally {
    Date.now = originalNow;
  }
});

test("re-fires the occupied nudge when urgency advances on the same anchor", async () => {
  const originalNow = Date.now;
  Date.now = () => NOW;
  try {
    const h = harness(slot());
    h.events.push({
      id: 50,
      timestamp: "2026-07-27T02:26:00.000",
      slot: 2,
      event_type: "idle_occupied_continue_injected",
      hook_type: "Stuck",
      tool_name: null,
      payload: JSON.stringify({
        command: expectedNudge(6, "REMINDER"),
        assignment_epoch: 4,
        idle_anchor: OLD_IDLE,
        wait_anchor: OLD_IDLE,
        urgency: "REMINDER",
      }),
      processed: false,
    });

    Date.now = () => Date.parse(OLD_IDLE + "Z") + 30 * 60_000;
    await h.detector.checkIdleOccupied(slot());

    assert.equal(h.sends.length, 1);
    assert.match(h.sends[0], /urgency=URGENT/);
  } finally {
    Date.now = originalNow;
  }
});

test("does not nudge a free slot when hook state is active despite stale log mtime", async () => {
  const originalNow = Date.now;
  const originalGate = process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
  const gate = installGate({
    allowed: true,
    slot: 2,
    recommendation_kind: "rework",
    rework_packet_count: 1,
    rework_pr_count: 1,
    ready_pool_size: 1,
    recommended_obligation_id: 91,
    recommended_pr: 7001,
    recommended_issue: 7000,
    recommended_packet: "/tmp/rework-7001.md",
    recommended_action: "assign rework",
    slot_dispatch_wedge_id: null,
    reason: "packet_backed_rework_available",
  });
  Date.now = () => NOW;
  process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = gate.path;
  try {
    const candidate = freeSlot({
      active_turn_state: "active",
      idle: false,
      active_turn_started_at: null,
    });
    const h = harness(candidate);
    h.events[0] = { ...h.events[0], event_type: "slot_released" };
    h.setLogMtime(new Date(Date.parse(OLD_IDLE + "Z")));
    await h.detector.checkIdleFree(candidate);

    assert.equal(h.sends.length, 0);
    const wedged = h.events.filter(
      (event) => event.event_type === "idle_free_wedge_detected",
    );
    assert.equal(wedged.length, 0);
  } finally {
    Date.now = originalNow;
    if (originalGate === undefined) delete process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE;
    else process.env.MOP_FREE_SLOT_ASSIGNMENT_GATE = originalGate;
    gate.cleanup();
  }
});
