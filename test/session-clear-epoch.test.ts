import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";

test("session-clear intent fences assignment at the same free epoch", () => {
  const root = mkdtempSync(join(tmpdir(), "mop-session-clear-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(root, "mop.db") });
  try {
    const started = "2020-01-01T00:00:00.000Z";
    db.updateSlot(1, {
      session_id: "old-session",
      session_started_at: started,
      idle: true,
      active_turn_id: null,
      active_turn_state: "inactive",
      dnd: false,
    });
    const claimed = db.claimSessionClearIntent(1, 0, "old-session", started, "clear-1");
    assert.equal(claimed.ok, true);
    const assignment = db.assignSlot(1, "new work", "repo", 99, "fix/99", 199, "a".repeat(40), 0, "rework", "handoff", true);
    assert.equal(assignment.reason, "session_clear_in_progress");
    assert.equal(db.getSlot(1)?.occupied, false);
    assert.equal(db.clearSessionClearIntent(1, "clear-1"), true);
    assert.equal(db.assignSlot(1, "new work", "repo", 99, "fix/99", 199, "a".repeat(40), 0, "rework", "handoff", true).ok, true);
  } finally {
    db.close();
    rmSync(root, { recursive: true, force: true });
  }
});
