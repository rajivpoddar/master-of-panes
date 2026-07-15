#!/usr/bin/env node
/**
 * Smoke test for slot-idle staleness gate (db.ts methods).
 *
 * Mirrors the existing check-slot "skip when idle between tool calls" gate
 * but for the /slot-idle injection path. Verifies that:
 *   1. hasRecentSubagentDispatch() returns truthy when Task fired with no later Stop
 *   2. hasRecentSubagentDispatch() returns null when a Stop event closed the Task
 *   3. hasRecentSubagentDispatch() returns null when Task is older than window
 *   4. getLastToolFire() returns the latest PostToolUse within the window
 *   5. background Agent dispatch stays active across Stop until TaskStop
 *   6. getLastVisibleSlotState() tracks PM-visible idle/active state only for
 *      the current occupied assignment
 *
 * Run: node scripts/test-slot-idle-staleness-gate.mjs
 *
 * Uses an isolated in-memory MoPDatabase via better-sqlite3 :memory: path
 * so it does not touch the production mop.db.
 *
 * Rajiv directive 2026-05-05: PM nudge interrupted slot 4 plan-agent because
 * the JSONL classifier captured "subagent_active=false" 43s before the Task
 * actually dispatched. Gate must catch this race in MoP itself.
 */

import { MoPDatabase } from "../dist/db.js";
import Database from "better-sqlite3";
import { rmSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

let pass = 0;
let fail = 0;

function assert(name, cond, detail = "") {
  if (cond) {
    console.log(`  ok ${name}`);
    pass++;
  } else {
    console.log(`  FAIL ${name}${detail ? "  -- " + detail : ""}`);
    fail++;
  }
}

const tmpDir = mkdtempSync(join(tmpdir(), "mop-gate-test-"));
const dbPath = join(tmpDir, "test.db");

try {
  const db = new MoPDatabase({ dbPath });

  // Open a parallel raw handle on the same DB file for test-only direct INSERTs.
  // (Avoids exposing MoPDatabase.db privately.)
  const raw = new Database(dbPath);

  // ─── Test 1: Task fired with no later Stop → gate fires ───
  // Insert a synthetic Task PostToolUse 5s ago with no Stop.
  const insertEvent = (slot, type, tool, offsetSec, payload = "{}") => {
    raw.prepare(
      `INSERT INTO events (timestamp, slot, event_type, tool_name, payload)
       VALUES (strftime('%Y-%m-%dT%H:%M:%f', 'now', ? || ' seconds'), ?, ?, ?, ?)`
    ).run(`-${offsetSec}`, slot, type, tool, payload);
  };
  const assignSlot = (slot, offsetSec) => {
    raw.prepare(
      `INSERT INTO slots (slot, address, status, occupied, assigned_at)
       VALUES (?, ?, 'active', 1, strftime('%Y-%m-%dT%H:%M:%f', 'now', ? || ' seconds'))`
    ).run(slot, `test:${slot}`, `-${offsetSec}`);
  };

  // Slot 1: Task fired 5s ago, no Stop → subagent active
  insertEvent(1, "PostToolUse", "Task", 5);
  const r1 = db.hasRecentSubagentDispatch(1, 90);
  assert("slot 1 — Task 5s ago, no Stop → gate fires", r1 !== null, JSON.stringify(r1));

  // Slot 2: Task fired 5s ago, then Stop 1s ago → subagent closed
  insertEvent(2, "PostToolUse", "Task", 5);
  insertEvent(2, "Stop", null, 1);
  const r2 = db.hasRecentSubagentDispatch(2, 90);
  assert("slot 2 — Task 5s ago + Stop 1s ago → gate does NOT fire", r2 === null, JSON.stringify(r2));

  // Slot 3: Task fired 200s ago (outside 90s window) → no gate
  insertEvent(3, "PostToolUse", "Task", 200);
  const r3 = db.hasRecentSubagentDispatch(3, 90);
  assert("slot 3 — Task 200s ago (outside window) → gate does NOT fire", r3 === null, JSON.stringify(r3));

  // ─── Test 4: getLastToolFire ───
  // Slot 4: Bash 10s ago → recent-tool gate
  insertEvent(4, "PostToolUse", "Bash", 10);
  const r4 = db.getLastToolFire(4, 15);
  assert("slot 4 — Bash 10s ago, window=15s → recent-tool gate fires", r4 !== null && r4.tool === "Bash", JSON.stringify(r4));

  // Slot 4 again: window=5s → should miss the 10s-old Bash
  const r4b = db.getLastToolFire(4, 5);
  assert("slot 4 — Bash 10s ago, window=5s → recent-tool gate misses", r4b === null, JSON.stringify(r4b));

  // ─── Test 6: Empty slot → null ───
  const r5 = db.hasRecentSubagentDispatch(99, 90);
  assert("slot 99 (empty) — no events → gate does NOT fire", r5 === null);

  const bgAgentPayload = JSON.stringify({
    type: "PostToolUse",
    tool_name: "Agent",
    tool_input: { run_in_background: true, description: "background review" },
  });
  const fgAgentPayload = JSON.stringify({
    type: "PostToolUse",
    tool_name: "Agent",
    tool_input: { run_in_background: false, description: "foreground review" },
  });

  insertEvent(5, "PostToolUse", "Agent", 5, bgAgentPayload);
  insertEvent(5, "Stop", null, 1);
  const r6 = db.hasRecentSubagentDispatch(5, 90);
  assert("slot 5 — background Agent + later Stop still gates", r6 !== null && r6.toolName === "Agent", JSON.stringify(r6));

  insertEvent(6, "PostToolUse", "Agent", 5, bgAgentPayload);
  insertEvent(6, "PostToolUse", "TaskStop", 1, JSON.stringify({ type: "PostToolUse", tool_name: "TaskStop" }));
  const r7 = db.hasRecentSubagentDispatch(6, 90);
  assert("slot 6 — background Agent + TaskStop closes gate", r7 === null, JSON.stringify(r7));

  insertEvent(7, "PostToolUse", "Agent", 5, fgAgentPayload);
  const r8 = db.hasRecentSubagentDispatch(7, 90);
  assert("slot 7 — foreground Agent does NOT gate", r8 === null, JSON.stringify(r8));

  const r9 = db.getLastVisibleSlotState(8);
  assert("slot 8 — no visible slot state → null", r9 === null, JSON.stringify(r9));

  assignSlot(9, 30);
  insertEvent(9, "slot_idle_notified", null, 5);
  const r10 = db.getLastVisibleSlotState(9);
  assert("slot 9 — last visible state idle", r10 !== null && r10.state === "idle", JSON.stringify(r10));

  assignSlot(10, 30);
  insertEvent(10, "slot_idle_notified", null, 10);
  insertEvent(10, "slot_active_notified", null, 5);
  const r11 = db.getLastVisibleSlotState(10);
  assert("slot 10 — latest visible state active", r11 !== null && r11.state === "active", JSON.stringify(r11));

  insertEvent(11, "slot_idle_notified", null, 30);
  assignSlot(11, 5);
  const r12 = db.getLastVisibleSlotState(11);
  assert("slot 11 — prior-assignment idle is ignored", r12 === null, JSON.stringify(r12));

  insertEvent(12, "slot_idle_notified", null, 30);
  assignSlot(12, 5);
  insertEvent(12, "slot_idle_notified", null, 1);
  const r13 = db.getLastVisibleSlotState(12);
  assert("slot 12 — current-assignment idle is visible", r13 !== null && r13.state === "idle", JSON.stringify(r13));
} catch (err) {
  console.error("test crashed:", err.stack || err.message);
  fail++;
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
