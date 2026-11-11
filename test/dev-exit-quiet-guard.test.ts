import assert from "node:assert/strict";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { devExitQuietRefusal } from "../src/relay.js";

const pane = "output\n────────\n❯ \n────────\n  ? for shortcuts\n";

for (const c of [
  { name: "background marker", elapsed: 60_000, capture: `← 2 agents\n${pane}`, expected: 0 },
  { name: "under sixty seconds", elapsed: 59_999, capture: pane, expected: 0 },
  { name: "quiet and clear", elapsed: 60_000, capture: pane, expected: 1 },
  { name: "capture error", elapsed: 60_000, capture: null, expected: 0 },
]) {
  test(`dev exit quiet guard: ${c.name}`, () => {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
    try {
      db.updateSlot(1, { idle: false, active_turn_state: "active" });
      db.updateSlot(1, { idle: true, active_turn_state: "inactive" });
      const start = Date.now();
      let sends = 0;
      let attempted = false;
      const send = () => {
        if (!attempted && !devExitQuietRefusal(db.exitQuietReady(1, start + c.elapsed), c.capture)) {
          attempted = true;
          sends++;
        }
      };
      send(); send();
      assert.equal(sends, c.expected);
    } finally { db.close(); }
  });
}

test("busy turn resets the quiet observation", () => {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  try {
    db.updateSlot(1, { idle: true, active_turn_state: "inactive" });
    assert.equal(db.exitQuietReady(1, Date.now() + 60_000), true);
    db.updateSlot(1, { idle: false, active_turn_state: "active" });
    db.updateSlot(1, { idle: true, active_turn_state: "inactive" });
    assert.equal(db.exitQuietReady(1), false);
  } finally { db.close(); }
});
