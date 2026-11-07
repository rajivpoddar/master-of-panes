// Single PM launcher (Rajiv DM D0AMF0XE6TS thread 1794015303.639509, ts 1794019433.856239:
// "can we combine the launcher script into one and make cliproxy or direct a param or something?").
// Tracked copies of the deployed ~/.claude/scripts launchers (deployed copies are authoritative, S19).
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { pmFreshLaunchCommand, RESTART_COMMANDS } from "../src/health.js";

const dir = fileURLToPath(new URL("../scripts/pm/launchers/", import.meta.url));
const launcher = join(dir, "launch-pm.sh");
const shim = join(dir, "launch-pm-cliproxy.sh");

test("tracked launchers parse; the cliproxy launcher is a one-line shim onto launch-pm.sh --via cliproxy", () => {
  for (const file of [launcher, shim]) execFileSync("bash", ["-n", file]);
  const lines = readFileSync(shim, "utf8").split("\n").filter((line) => line.trim() && !line.startsWith("#"));
  assert.deepEqual(lines, ['exec bash "$(dirname "${BASH_SOURCE[0]}")/launch-pm.sh" --via cliproxy "$@"']);
  const source = readFileSync(launcher, "utf8");
  assert.match(source, /^VIA=cliproxy$/m, "default stays cliproxy");
  assert.match(source, /exec claude --model "\$MODEL" --dangerously-skip-permissions/);
});

test("an unknown --via refuses before any launch", () => {
  const result = spawnSync("bash", [launcher, "--via", "bogus"], { encoding: "utf8" });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /--via must be cliproxy or direct/);
});

test("every MoP PM launch passes an explicit --via (never direct by omission)", () => {
  for (const launch of ["cliproxy", "direct"] as const) {
    for (const model of ["opus", "deepseek", "claude-opus-5-5"]) {
      assert.match(pmFreshLaunchCommand(model, launch), /\/launch-pm\.sh --via (cliproxy|direct) --model /);
    }
  }
  assert.match(RESTART_COMMANDS[0], /launch-pm\.sh --via cliproxy --model claude-opus-5-5$/);
});

test("--via direct: no proxy env, args passed through (stub claude)", { skip: !existsSync("/Users/rajiv/Downloads/projects/heydonna-app") }, () => {
  const bin = mkdtempSync(join(tmpdir(), "mop-launcher-stub-"));
  try {
    const stub = join(bin, "claude");
    writeFileSync(stub, '#!/bin/sh\necho "ARGS:$*"\nenv | grep -E "^(ANTHROPIC_|API_TIMEOUT_MS|CLAUDE_CODE_MAX_CONTEXT)" | cut -d= -f1\n');
    chmodSync(stub, 0o755);
    const out = execFileSync("bash", [launcher, "--via", "direct", "--model", "opus", "--extra"], {
      encoding: "utf8",
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, ANTHROPIC_BASE_URL: "http://inherited", ANTHROPIC_API_KEY: "x", API_TIMEOUT_MS: "1" },
    });
    assert.equal(out.trim(), "ARGS:--model claude-opus-5-5 --dangerously-skip-permissions --extra");
  } finally {
    rmSync(bin, { recursive: true, force: true }); // Exact generated fixture only.
  }
});
