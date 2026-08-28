import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";
import { MoPDatabase } from "../src/db.js";
import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import { PM_TRANSITION_ASSIGNMENT_AUTHORITY, PM_TRANSITION_ASSIGNMENT_HEADER } from "../src/assignmentAuthority.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import {
  DEFAULT_DEV_SLOT_COUNT,
  DEV_SLOT_NAMES,
  DEV_SLOT_NUMBERS,
  RUNTIME_SLOT_NUMBERS,
  SLOT_RUNTIME_IDENTITIES,
  devSlots,
  isValidDevSlot,
  isValidRuntimeSlot,
} from "../src/slotConfig.js";
import { RESTART_COMMANDS } from "../src/health.js";
import { spawnSync } from "node:child_process";

test("the canonical bound enumerates PM plus six isolated dev slots", () => {
  assert.equal(DEFAULT_CONFIG.slotCount, 6);
  assert.deepEqual(DEV_SLOT_NUMBERS, [1, 2, 3, 4, 5, 6]);
  assert.deepEqual(RUNTIME_SLOT_NUMBERS, [0, 1, 2, 3, 4, 5, 6]);
  assert.deepEqual(devSlots(), [1, 2, 3, 4, 5, 6]);
  assert.equal(isValidDevSlot(1), true);
  assert.equal(isValidDevSlot(6), true);
  assert.equal(isValidDevSlot(0), false);
  assert.equal(isValidDevSlot(7), false);
  assert.equal(isValidRuntimeSlot(0), true);
  assert.equal(isValidRuntimeSlot(7), false);
});

test("S5/S6 runtime identities are isolated and launchable without provisioning them", () => {
  const legacyBindings = {
    1: ["dev:uncommon-buffalo-66", "heydonna-slot-1"],
    2: ["dev:optimistic-camel-445", "heydonna"],
    3: ["dev:handsome-finch-141", "heydonna-slot-3"],
    4: ["dev:knowing-orca-670", "heydonna-slot-4"],
  } as const;
  for (const slot of [1, 2, 3, 4] as const) {
    assert.equal(SLOT_RUNTIME_IDENTITIES[slot].provisioning, "preserve-live");
    assert.equal(SLOT_RUNTIME_IDENTITIES[slot].convexDeployment, undefined);
    assert.equal(SLOT_RUNTIME_IDENTITIES[slot].legacyConvexDeployment, legacyBindings[slot][0]);
    assert.equal(SLOT_RUNTIME_IDENTITIES[slot].legacyConvexProject, legacyBindings[slot][1]);
  }
  for (const slot of [5, 6]) {
    const identity = SLOT_RUNTIME_IDENTITIES[slot];
    assert.equal(identity.name, slot === 5 ? "Revati" : "Pushya");
    assert.equal(identity.checkoutPath, `/Users/rajiv/Downloads/projects/heydonna-app-300${slot}`);
    assert.equal(identity.convexDeployment, `heydonna-slot-${slot}`);
    assert.equal(identity.convexProject, `heydonna-slot-${slot}`);
    assert.equal(identity.provisioning, "create-isolated");
    assert.equal(identity.appPort, 3000 + slot);
    assert.equal(identity.browserSession, `slot${slot}`);
    assert.equal(identity.modalSuffix, `-slot${slot}`);
    assert.equal(identity.envPath, `/Users/rajiv/Downloads/projects/heydonna-app-300${slot}/.env.local`);
    assert.match(RESTART_COMMANDS[slot], new RegExp(`launch-slot-${slot}\\.sh`));
  }
  assert.notEqual(SLOT_RUNTIME_IDENTITIES[5].convexDeployment, SLOT_RUNTIME_IDENTITIES[6].convexDeployment);
  assert.notEqual(SLOT_RUNTIME_IDENTITIES[5].appPort, SLOT_RUNTIME_IDENTITIES[6].appPort);
});

test("versioned S5/S6 launch mappings are explicit and preserve the existing launcher library", () => {
  const manifest = JSON.parse(readFileSync(new URL("../scripts/pm/shared-assets/manifest.json", import.meta.url), "utf8")) as {
    entries: Array<{ source_path: string; canonical_target: string; mode: number }>;
  };
  for (const slot of [5, 6]) {
    const entry = manifest.entries.find((item) => item.source_path === `claude/scripts/launch-slot-${slot}.sh`);
    assert.ok(entry);
    assert.equal(entry?.canonical_target, `/Users/rajiv/.claude/scripts/launch-slot-${slot}.sh`);
    assert.equal(entry?.mode, 493);
  }
  for (const name of ["pushya", "revati"]) {
    const entry = manifest.entries.find((item) => item.source_path === `claude/dev-slot-rules/22-slot-${name}.md`);
    assert.ok(entry);
    assert.equal(entry?.canonical_target, `/Users/rajiv/.claude/dev-slot-rules/22-slot-${name}.md`);
    assert.equal(entry?.mode, 420);
  }

  const launcher = readFileSync(
    new URL("../scripts/pm/shared-assets/claude/scripts/launch-dev-slot-claude.sh", import.meta.url),
    "utf8",
  );
  assert.match(launcher, /5\) SLOT_NAME="Revati"/);
  assert.match(launcher, /6\) SLOT_NAME="Pushya"/);
  assert.match(launcher, /CLAUDE_CODE_SUBAGENT_MODEL="\$SPARK_MODEL"/);
  assert.match(launcher, /\.config\/ornith15\/api-key/);
  assert.match(launcher, /20-buddhi-dev\.md/);
  assert.match(launcher, /22-slot-revati\.md/);
  assert.match(launcher, /22-slot-pushya\.md/);
  const firstPreflight = launcher.indexOf("preflight_rule_link \"$DEV_SLOT_RULES/20-buddhi-dev.md\"");
  const lastPreflight = launcher.indexOf("preflight_rule_link \"$PROJECT_RULES/21-lessons.md\"");
  const firstMutation = launcher.indexOf("repair_rule_link \"$DEV_SLOT_RULES/20-buddhi-dev.md\"");
  assert.ok(firstPreflight >= 0);
  assert.ok(lastPreflight > firstPreflight);
  assert.ok(firstMutation > lastPreflight);
});

test("six-slot DB initialization adds S5/S6 without rewriting existing state", () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-six-slot-db-"));
  const dbPath = join(directory, "mop.db");
  const legacy = new MoPDatabase({ ...DEFAULT_CONFIG, slotCount: 4, dbPath });
  legacy.updateSlot(1, { task: "preserve me" });
  const legacyEpoch = legacy.getSlot(1)!.assignment_epoch;
  legacy.close();
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
  try {
    const after = db.getSlot(1)!;
    assert.equal(after.task, "preserve me");
    assert.equal(after.assignment_epoch, legacyEpoch);
    assert.deepEqual(db.getAllSlots().map((slot) => slot.slot), [1, 2, 3, 4, 5, 6]);
    assert.equal(db.getSlot(5)?.occupied, false);
    assert.equal(db.getSlot(6)?.occupied, false);
    assert.equal(db.getSlot(5)?.name, "Revati");
    assert.equal(db.getSlot(6)?.name, "Pushya");
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("all six slots have stable human names", () => {
  assert.deepEqual(DEV_SLOT_NAMES, {
    1: "Rohini",
    2: "Hasta",
    3: "Ashwini",
    4: "Chitra",
    5: "Revati",
    6: "Pushya",
  });
});

test("configured bounds reject invalid migration counts while production stays fixed at six", () => {
  assert.throws(() => devSlots(0));
  assert.throws(() => devSlots(7));
  assert.equal(DEFAULT_DEV_SLOT_COUNT, 6);
});

test("pane configuration refuses seven before any tmux operation", () => {
  const home = mkdtempSync(join(tmpdir(), "mop-seven-pane-home-"));
  const paneState = join(home, ".claude", "tmux-panes");
  mkdirSync(paneState, { recursive: true });
  writeFileSync(join(paneState, "config.json"), JSON.stringify({ panes: {
    manager: "0:0.0",
    dev: ["0:0.1", "0:0.2", "0:0.3", "0:0.4", "0:0.5", "0:0.6", "0:0.7"],
  } }));
  try {
    const result = spawnSync("/bin/bash", ["-c", `source ${JSON.stringify(join(process.cwd(), "scripts/pane-lib.sh"))}; load_config`], {
      env: { ...process.env, HOME: home }, encoding: "utf8",
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /between 1 and 6/);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("assignment HTTP accepts S6 and refuses S7 before mutation", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-six-slot-route-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    const app = new Hono();
    registerAssignmentRoute(app, db);
    const body = {
      task: "S6 bound fixture",
      repository_id: "github:heydonna-app/heydonna-app",
      issue: 600,
      pr: 600,
      branch: "fix/s6-bound",
      head_sha: "a".repeat(40),
      work_kind: "implementation",
      handoff_id: "s6-bound",
      expected_epoch: 0,
    };
    const headers = {
      "content-type": "application/json",
      [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY,
    };
    const accepted = await app.request("/slots/6/assign", { method: "POST", headers, body: JSON.stringify(body) });
    assert.equal(accepted.status, 200);
    assert.equal(db.getSlot(6)?.pr, 600);
    const refused = await app.request("/slots/7/assign", { method: "POST", headers, body: JSON.stringify(body) });
    assert.equal(refused.status, 400);
    assert.equal(db.getSlot(7), undefined);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
