import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { MoPDatabase, STALE_TURN_QUIESCENCE_MS } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const SLOT = 6;
const TURN = "b830d078-31e6-4549-a3d7-3581d767debe";
const ACTOR = "cto-test";
const REASON = "owning session interrupted; pane relaunched under a new session";

function withDatabase(run: (db: MoPDatabase) => void): void {
  const directory = mkdtempSync(join(tmpdir(), "mop-abandon-test-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    run(db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

function startStaleTurn(db: MoPDatabase, turnId: string = TURN): void {
  db.startAgentTurn(SLOT, turnId);
  const old = new Date(Date.now() - STALE_TURN_QUIESCENCE_MS - 60_000).toISOString();
  db.updateSlot(SLOT, { active_turn_started_at: old, last_meaningful_work_at: old });
}

function turnFields(db: MoPDatabase) {
  const slot = db.getSlot(SLOT)!;
  return {
    active_turn_id: slot.active_turn_id,
    active_turn_started_at: slot.active_turn_started_at,
    active_turn_state: slot.active_turn_state,
    idle: slot.idle,
  };
}

test("abandoning an orphaned turn clears it through the Stop transition and is idempotent", () => {
  withDatabase((db) => {
    const occupiedBefore = db.getSlot(SLOT)!.occupied;
    const epochBefore = db.getSlot(SLOT)!.assignment_epoch;
    startStaleTurn(db);
    const result = db.abandonTurn(SLOT, TURN, REASON, ACTOR);
    assert.equal(result.ok, true);
    assert.equal(result.conflict, false);
    assert.equal(result.idempotent, false);
    assert.deepEqual(turnFields(db), {
      active_turn_id: null,
      active_turn_started_at: null,
      active_turn_state: "inactive",
      idle: true,
    });
    // Assignment identity is untouched: only the turn is cleared.
    assert.equal(db.getSlot(SLOT)!.occupied, occupiedBefore);
    assert.equal(db.getSlot(SLOT)!.assignment_epoch, epochBefore);
    // Audit record names who/why.
    const events = db.getEvents(SLOT, 5).filter((event) => event.event_type === "agent_turn_abandoned");
    assert.equal(events.length, 1);
    const payload = JSON.parse(events[0].payload);
    assert.equal(payload.turn_id, TURN);
    assert.equal(payload.reason, REASON);
    assert.equal(payload.actor, ACTOR);
    // Repeat is idempotent, with no second audit event.
    const repeat = db.abandonTurn(SLOT, TURN, REASON, ACTOR);
    assert.equal(repeat.ok, true);
    assert.equal(repeat.idempotent, true);
    assert.equal(
      db.getEvents(SLOT, 10).filter((event) => event.event_type === "agent_turn_abandoned").length,
      1,
    );
  });
});

test("a replacement turn is never cleared by an old turn id", () => {
  withDatabase((db) => {
    startStaleTurn(db);
    const cleared = db.abandonTurn(SLOT, TURN, REASON, ACTOR);
    assert.equal(cleared.ok, true);
    const next = "9f8e7d6c-5b4a-3210-9876-fedcba987654";
    db.startAgentTurn(SLOT, next);
    const stale = db.abandonTurn(SLOT, TURN, REASON, ACTOR);
    assert.equal(stale.ok, false);
    assert.equal(stale.reason, "turn_mismatch");
    assert.equal(db.getSlot(SLOT)!.active_turn_id, next);
    assert.equal(db.getSlot(SLOT)!.active_turn_state, "active");
  });
});

test("a live turn with fresh activity is refused", () => {
  withDatabase((db) => {
    db.startAgentTurn(SLOT, TURN);
    db.touchMeaningfulWork(SLOT, TURN);
    const result = db.abandonTurn(SLOT, TURN, REASON, ACTOR);
    assert.equal(result.ok, false);
    assert.equal(result.reason, "turn_recently_active");
    assert.equal(db.getSlot(SLOT)!.active_turn_id, TURN);
    assert.equal(db.getSlot(SLOT)!.active_turn_state, "active");
  });
});

test("an indeterminate turn is refused", () => {
  withDatabase((db) => {
    startStaleTurn(db);
    db.finishAgentTurn(SLOT, "someone-else");
    assert.equal(db.getSlot(SLOT)!.active_turn_state, "indeterminate");
    const result = db.abandonTurn(SLOT, TURN, REASON, ACTOR);
    assert.equal(result.ok, false);
    assert.equal(result.reason, "turn_state_not_active");
    assert.equal(db.getSlot(SLOT)!.active_turn_id, TURN);
  });
});

test("an armed per-slot clear wins over abandon", () => {
  withDatabase((db) => {
    startStaleTurn(db);
    db.setPendingClear(SLOT);
    const result = db.abandonTurn(SLOT, TURN, REASON, ACTOR);
    assert.equal(result.ok, false);
    assert.equal(result.reason, "clear_pending_active");
    assert.equal(db.getSlot(SLOT)!.active_turn_id, TURN);
  });
});

test("invalid identity and unknown slots fail closed", () => {
  withDatabase((db) => {
    startStaleTurn(db);
    assert.equal(db.abandonTurn(999, TURN, REASON, ACTOR).reason, "unknown_slot");
    assert.equal(db.abandonTurn(SLOT, "  ", REASON, ACTOR).reason, "turn_id_invalid");
    assert.equal(db.abandonTurn(SLOT, TURN, "  ", ACTOR).reason, "reason_invalid");
    assert.equal(db.abandonTurn(SLOT, TURN, REASON, "  ").reason, "actor_invalid");
    assert.equal(db.getSlot(SLOT)!.active_turn_id, TURN);
  });
});

test("a normal Stop produces the identical turn end-state", () => {
  withDatabase((db) => {
    startStaleTurn(db, "stop-path-turn");
    db.finishAgentTurn(SLOT, "stop-path-turn");
    const viaStop = turnFields(db);
    startStaleTurn(db, "abandon-path-turn");
    const result = db.abandonTurn(SLOT, "abandon-path-turn", REASON, ACTOR);
    assert.equal(result.ok, true);
    const viaAbandon = turnFields(db);
    assert.deepEqual(viaAbandon, viaStop);
  });
});
