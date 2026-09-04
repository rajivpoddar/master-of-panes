import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { StuckDetector } from "../src/stuck.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG, type HookPayload } from "../src/types.js";

function withDatabase(run: (db: MoPDatabase) => Promise<void>): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-active-turn-nudge-gate-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  return run(db).finally(() => {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  });
}

async function deliverHook(
  db: MoPDatabase,
  processor: HookProcessor,
  slot: number,
  payload: HookPayload,
): Promise<void> {
  if (payload.type === "UserPromptSubmit" && payload.session_id) {
    db.startAgentTurn(slot, payload.session_id);
  } else if (payload.type === "PreToolUse" || payload.type === "PostToolUse") {
    db.touchMeaningfulWork(slot, payload.session_id);
  }
  await processor.process(slot, payload);
  if ((payload.type === "Stop" || payload.type === "SessionEnd") && payload.session_id) {
    db.finishAgentTurn(slot, payload.session_id);
  }
}

function assignAndIdle(db: MoPDatabase): void {
  const result = db.assignSlot(
    2,
    "issue 7000",
    "github:heydonna-app/heydonna-app",
    7000,
    "fix/7000",
    7001,
    "a".repeat(40),
    0,
    "implementation",
    "handoff-7000",
  );
  assert.equal(result.ok, true);
  db.updateSlot(2, {
    idle: true,
    active_turn_id: null,
    active_turn_started_at: null,
    active_turn_state: "inactive",
  });
}

test("prompt lifecycle has one opener, telemetry-only tool hooks, and one closer", async () => {
  await withDatabase(async (db) => {
    assignAndIdle(db);
    const processor = new HookProcessor(db, {} as TmuxRelay);

    await deliverHook(db, processor, 2, {
      type: "PreToolUse",
      session_id: "session-a",
      tool_name: "Bash",
    });
    assert.equal(db.getSlot(2)?.active_turn_state, "inactive");
    assert.equal(db.getSlot(2)?.idle, true);

    await deliverHook(db, processor, 2, {
      type: "PostToolUse",
      session_id: "session-a",
      tool_name: "Bash",
    });
    assert.equal(db.getSlot(2)?.active_turn_state, "inactive");
    assert.equal(db.getSlot(2)?.idle, true);

    await deliverHook(db, processor, 2, {
      type: "UserPromptSubmit",
      session_id: "session-a",
    });
    assert.equal(db.getSlot(2)?.active_turn_state, "active");
    assert.equal(db.getSlot(2)?.idle, false);

    await deliverHook(db, processor, 2, {
      type: "PostToolUse",
      session_id: "session-a",
      tool_name: "Bash",
    });
    assert.equal(db.getSlot(2)?.active_turn_state, "active");
    assert.equal(db.getSlot(2)?.idle, false);

    await deliverHook(db, processor, 2, {
      type: "Stop",
      session_id: "session-a",
    });
    assert.equal(db.getSlot(2)?.active_turn_state, "inactive");
    assert.equal(db.getSlot(2)?.idle, true);

    await deliverHook(db, processor, 2, {
      type: "UserPromptSubmit",
      session_id: "session-b",
    });
    await deliverHook(db, processor, 2, {
      type: "SessionEnd",
      session_id: "session-b",
    });
    assert.equal(db.getSlot(2)?.active_turn_state, "inactive");
    assert.equal(db.getSlot(2)?.idle, true);
  });
});

test("a delivered occupied prompt opens the exact generation and blocks the next bucket", async () => {
  await withDatabase(async (db) => {
    assignAndIdle(db);
    const assigned = db.getSlot(2)!;
    const originalNow = Date.now;
    const assignedMs = Date.parse(assigned.assigned_at!);
    const sends: string[] = [];
    const relay = {
      sendToSlotAsync: async (_slot: number, command: string) => {
        sends.push(command);
        return true;
      },
    } as unknown as TmuxRelay;
    const detector = new StuckDetector(
      db,
      { getLogMtime: async () => null } as never,
      relay,
    );
    const processor = new HookProcessor(db, relay);

    try {
      assert.equal(
        db.openPromptDelivery(2, assigned.assignment_epoch + 1, assigned.assigned_at),
        false,
      );
      assert.equal(db.getSlot(2)?.active_turn_state, "inactive");
      assert.equal(db.getSlot(2)?.idle, true);

      Date.now = () => assignedMs + 30 * 60_000;
      await detector.checkIdleOccupied(assigned);
      assert.equal(sends.length, 1);
      assert.equal(db.getSlot(2)?.assignment_epoch, assigned.assignment_epoch);
      assert.equal(db.getSlot(2)?.active_turn_state, "active");
      assert.equal(db.getSlot(2)?.idle, false);

      await deliverHook(db, processor, 2, {
        type: "PostToolUse",
        session_id: "session-a",
        tool_name: "Bash",
      });
      assert.equal(db.getSlot(2)?.active_turn_state, "active");
      assert.equal(db.getSlot(2)?.idle, false);

      Date.now = () => assignedMs + 60 * 60_000;
      await detector.checkIdleOccupied(db.getSlot(2)!);
      assert.equal(sends.length, 1);
    } finally {
      Date.now = originalNow;
    }
  });
});
