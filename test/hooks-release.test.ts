import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

test("a stale PM-direction Stop cannot reclaim a released slot", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-release-stop-test-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.assignSlot(3, "PR rework", 6245, "fix/6245", "turn-a", 6411, "a".repeat(40), 0);
    db.finishAgentTurn(3, "turn-a");
    db.releaseSlot(3, 1);
    assert.equal(db.getSlot(3)?.occupied, false);

    const processor = new HookProcessor(db, {} as TmuxRelay);
    await processor.process(3, {
      type: "Stop",
      session_id: "turn-a",
      transcript: "Work is complete. Need PM direction before the next step.",
    });

    const slot = db.getSlot(3);
    assert.equal(slot?.occupied, false);
    assert.equal(slot?.task, null);
    assert.equal(slot?.issue, null);
    assert.equal(slot?.pr, null);
    assert.equal(slot?.assignment_epoch, 1);
    const events = db.getEvents(3, 10, "stale_pm_direction_after_release");
    assert.equal(events.length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
