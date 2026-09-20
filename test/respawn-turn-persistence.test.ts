import assert from "node:assert/strict";
import test from "node:test";

import {
  evaluateRespawnTurnPersistence,
  respawnTurnPersistenceRemedy,
} from "../src/respawnTurnGuard.js";

const TURN_ID = "b7bebc16-8dbd-48c9-941e-4ebf6d432875";

test("respawn success is conditional on the inherited turn actually settling", () => {
  const pending = evaluateRespawnTurnPersistence(TURN_ID, { active_turn_id: TURN_ID, active_turn_state: "active" });
  assert.equal(pending.persisted, true);
  assert.equal(pending.turn_id, TURN_ID);

  const cleared = evaluateRespawnTurnPersistence(TURN_ID, { active_turn_id: null, active_turn_state: "inactive" });
  assert.equal(cleared.persisted, false);

  const replaced = evaluateRespawnTurnPersistence(TURN_ID, { active_turn_id: "new-turn", active_turn_state: "active" });
  assert.equal(replaced.persisted, false, "a replacement turn is the new session's business, not a stale record");

  const none = evaluateRespawnTurnPersistence(null, { active_turn_id: null, active_turn_state: "inactive" });
  assert.equal(none.persisted, false);

  const remedy = respawnTurnPersistenceRemedy(6, TURN_ID);
  assert.match(remedy, /abandon-turn/);
  assert.match(remedy, new RegExp(TURN_ID));
  assert.match(remedy, /idempotent/);
});
