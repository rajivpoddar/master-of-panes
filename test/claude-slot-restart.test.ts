import assert from "node:assert/strict";
import test from "node:test";
import {
  CLAUDE_SLOT_SCRIPTS_DIR,
  RESTART_COMMANDS,
} from "../src/health.js";

test("numbered slot auto-restarts use the persistent Claude Code launchers", () => {
  assert.equal(CLAUDE_SLOT_SCRIPTS_DIR, "/Users/rajiv/.claude/scripts");
  for (const slot of [1, 2, 3, 4]) {
    assert.equal(
      RESTART_COMMANDS[slot],
      `bash ${CLAUDE_SLOT_SCRIPTS_DIR}/launch-slot-${slot}.sh --continue`,
    );
    assert.doesNotMatch(RESTART_COMMANDS[slot], /-omp\.sh/);
  }
});

test("PM restart remains on its tracked Claude Code launcher", () => {
  assert.equal(
    RESTART_COMMANDS[0],
    "bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/launch-pm.sh --continue",
  );
});
