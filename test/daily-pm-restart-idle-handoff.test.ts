import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import type { MoPDatabase } from "../src/db.js";

const fixture = fileURLToPath(new URL("../scripts/pm/tests/test_daily_pm_restart.py", import.meta.url));
const python = process.env.MOP_FIXTURE_PYTHON ?? "python3";

type IdleBoundary = {
  drainTimer: ReturnType<typeof setTimeout> | null;
  drainGeneration: number;
  runDrainCheck(delayMs: number, generation: number): Promise<void>;
};

async function stopAndDrain(relay: TmuxRelay): Promise<void> {
  const wasBusy = relay.isPMBusy();
  relay.setPMBusy(false); // the real /pm-status Stop contract
  assert.equal(relay.isPMBusy(), wasBusy, "Stop does not change the proven state before debounce");
  const boundary = relay as unknown as IdleBoundary;
  if (boundary.drainTimer) clearTimeout(boundary.drainTimer);
  await boundary.runDrainCheck(30_000, boundary.drainGeneration);
}

test("documented busy skill checkpoints with caller tools; MoP Stop/idle replaces once outside Claude", async () => {
  const home = mkdtempSync(join(tmpdir(), "mop-pm-checkpoint-"));
  const call = (phase: string) => execFileSync(python, [fixture, home, phase], { encoding: "utf8" });
  let launches = 0;
  const relay = new TmuxRelay(DEFAULT_CONFIG, { pmIdleHandoff: async () => {
    assert.equal(relay.isPMIdleProven(), true);
    const state = readdirSync(join(home, ".claude/state")).find(n => /^daily-pm-restart-.*\.json$/.test(n))!;
    if (JSON.parse(readFileSync(join(home, ".claude/state", state), "utf8")).handoff_status !== "pending") return;
    launches += 1;
    call("idle"); // Node/MoP is the worker's real OS parent, not target Claude.
  } });
  relay.drainPMQueue = async () => 0; // fake queue/tmux boundary; no native delivery
  try {
    relay.setPMBusy(true); // real UserPromptSubmit start contract
    call("checkpoint"); // fixture contains Claude -> Bash -> Python -> ps
    assert.equal(relay.isPMBusy(), true);
    let effects = JSON.parse(readFileSync(join(home, "fixture-effects.json"), "utf8")) as string[][];
    assert.equal(effects.some(a => a[1] === "respawn-pane" || a[0] === "mop-respawn"), false);
    assert.deepEqual(JSON.parse(effects.find(a => a[0] === "exit-pending")![1]), { enabled: true, dev_only: true });
    assert.equal(launches, 0);
    call("stop"); // own Bash completion + terminal Stop, no new prompt/tool
    await stopAndDrain(relay);
    effects = JSON.parse(readFileSync(join(home, "fixture-effects.json"), "utf8"));
    assert.equal(effects.filter(a => a[0] === "mop-respawn").length, 1);
    assert.deepEqual(JSON.parse(effects.find(a => a[0] === "mop-respawn")![1]), { continue_session: true });
    const send = effects.find(a => a[0] === "mop-send")!;
    assert.ok(effects.indexOf(send) > effects.findIndex(a => a[0] === "mop-respawn"));
    assert.match(JSON.parse(send[1]).command, /Read .*0400/);
    assert.equal(effects.some(a => a[0] === "tmux" && a[1] === "respawn-pane"), false);
    assert.equal(JSON.parse(readFileSync(join(home, "fixture-parent.json"), "utf8")).parent_pid, process.pid);
    await stopAndDrain(relay);
    assert.equal(launches, 1, "retained requested state cannot replay on another Stop");
  } finally { rmSync(home, { recursive: true, force: true }); }
});

for (const scenario of ["queued", "drained_new_work", "new_busy_during_drain"] as const) {
  test(`idle handoff refuses ${scenario}`, async () => {
    let launches = 0;
    const relay = new TmuxRelay(DEFAULT_CONFIG, { pmIdleHandoff: async () => { launches += 1; } });
    relay.setDatabase({
      getPendingPMEventCount: () => scenario === "queued" ? 1 : 0,
      logEvent: () => 1,
    } as unknown as MoPDatabase);
    relay.drainPMQueue = async () => {
      if (scenario === "new_busy_during_drain") relay.setPMBusy(true);
      return scenario === "drained_new_work" ? 1 : 0;
    };
    relay.setPMBusy(true);
    await stopAndDrain(relay);
    assert.equal(launches, 0);
  });
}

test("server wires idle continuation through MoP execFile with the bound service DB", () => {
  const source = readFileSync(fileURLToPath(new URL("../src/server.ts", import.meta.url)), "utf8");
  assert.match(source, /new TmuxRelay\(config, \{ pmIdleHandoff:/);
  assert.match(source, /execFile\("\/bin\/bash"/);
  assert.match(source, /"--complete-on-idle"/);
  assert.match(source, /MOP_DB_PATH: resolve\(config\.dbPath\)/);
});
