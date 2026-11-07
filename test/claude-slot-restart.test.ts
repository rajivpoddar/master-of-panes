import assert from "node:assert/strict";
import test from "node:test";
import {
  CLAUDE_SLOT_SCRIPTS_DIR,
  CLAUDE_SLOT_RUNTIME_ENV,
  RESTART_COMMANDS,
} from "../src/health.js";

test("numbered slot auto-restarts use the persistent Claude Code launchers", () => {
  assert.equal(CLAUDE_SLOT_SCRIPTS_DIR, "/Users/rajiv/.claude/scripts");
  for (const slot of [1, 2, 3, 4, 5, 6]) {
    assert.equal(
      RESTART_COMMANDS[slot],
      `env ${CLAUDE_SLOT_RUNTIME_ENV} bash ${CLAUDE_SLOT_SCRIPTS_DIR}/launch-slot-${slot}.sh --continue`,
    );
    assert.doesNotMatch(RESTART_COMMANDS[slot], /-omp\.sh/);
  }
});

test("numbered slot auto-restarts preserve the Ornith context/output contract", () => {
  assert.match(CLAUDE_SLOT_RUNTIME_ENV, /DEV_SLOT_SPARK_MODEL=ornith-1\.5-35b-a3b/);
  assert.match(CLAUDE_SLOT_RUNTIME_ENV, /DEV_SLOT_SPARK_MAX_CONTEXT_TOKENS=240000/);
  assert.match(CLAUDE_SLOT_RUNTIME_ENV, /DEV_SLOT_SPARK_MAX_OUTPUT_TOKENS=32000/);
  assert.match(CLAUDE_SLOT_RUNTIME_ENV, /DEV_SLOT_SPARK_MAX_THINKING_TOKENS=2048/);
  assert.match(CLAUDE_SLOT_RUNTIME_ENV, /MAX_THINKING_TOKENS=2048/);
  assert.doesNotMatch(CLAUDE_SLOT_RUNTIME_ENV, /MAX_OUTPUT_TOKENS=2048(?:\s|$)/);
});

// Rajiv DM D0AMF0XE6TS thread 1793979986.975849, ts 1793989226.288339: "switching models with
// continue is not probably a good idea. MoP should run the right launcher and then inject the
// continuation prompt". The PM keeps CLIProxy routing and starts fresh with the configured model.
test("PM restart keeps CLIProxy routing and launches fresh with the default opus model", () => {
  assert.equal(
    RESTART_COMMANDS[0],
    "bash /Users/rajiv/.claude/scripts/launch-pm.sh --via cliproxy --model claude-opus-5-5",
  );
});
