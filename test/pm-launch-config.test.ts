// Rajiv DM D0AMF0XE6TS thread 1794015303.639509, ts 1794018254.090169: "for direct claude,
// it has to use launch-pm.sh". MoP config pm_launcher (cliproxy|direct) selects the PM
// launcher; the fresh-start + continuation contract is identical for both. Also: only
// active non-DND dev slots belong to a with_devs (04:00) episode (S5-S8 decommissioned).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
import { MoPDatabase } from "../src/db.js";
import {
  AGENT_COMMANDS, pmConfiguredLauncher, pmFreshLaunchCommand, pmLaunchPlan, PM_LAUNCHER_CONFIG_KEY, PM_MODEL_CONFIG_KEY,
  RESTART_COMMANDS,
} from "../src/health.js";
import { devSlots, DEFAULT_DEV_SLOT_COUNT } from "../src/slotConfig.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const database = () => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
const SCRIPTS = "/Users/rajiv/.claude/scripts";

function route(path: string, method: string, deps: Record<string, unknown>) {
  const source = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const tree = ts.createSourceFile("server.ts", source, ts.ScriptTarget.Latest, true);
  let call: ts.CallExpression | undefined;
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === `app.${method}`
        && node.arguments[0]?.getText(tree) === JSON.stringify(path)) call = node;
    ts.forEachChild(node, visit);
  };
  visit(tree); assert.ok(call, `${method} ${path}`);
  const js = ts.transpileModule(`const handler = ${call.arguments[1].getText(tree)};`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function(...Object.keys(deps), `${js}; return handler;`)(...Object.values(deps));
}
const context = (body: unknown) => ({ req: { json: async () => body }, json: (b: unknown, status = 200) => ({ body: b, status }) });

test("launcher contract: cliproxy default; direct uses launch-pm.sh; deepseek stays on the proxy", () => {
  assert.equal(pmFreshLaunchCommand("opus", "direct"), `bash ${SCRIPTS}/launch-pm.sh --via direct --model claude-opus-5-5`);
  assert.equal(pmFreshLaunchCommand("claude-opus-5-5", "direct"), `bash ${SCRIPTS}/launch-pm.sh --via direct --model claude-opus-5-5`);
  assert.equal(pmFreshLaunchCommand("deepseek", "direct"), `bash ${SCRIPTS}/launch-pm.sh --via cliproxy --model deepseek`);
  assert.equal(pmFreshLaunchCommand("opus"), `bash ${SCRIPTS}/launch-pm.sh --via cliproxy --model claude-opus-5-5`);
  assert.equal(RESTART_COMMANDS[0], pmFreshLaunchCommand("opus", "cliproxy"));
  for (const launcher of ["cliproxy", "direct"] as const) assert.doesNotMatch(pmFreshLaunchCommand("opus", launcher), /--continue/);
  const db = database();
  try {
    assert.equal(pmConfiguredLauncher(db), "cliproxy");
    db.setConfig(PM_LAUNCHER_CONFIG_KEY, "bogus");
    assert.equal(pmConfiguredLauncher(db), "cliproxy");
    db.setConfig(PM_LAUNCHER_CONFIG_KEY, "direct");
    assert.deepEqual(pmLaunchPlan(db), { launcher: "direct", model: "opus", command: `bash ${SCRIPTS}/launch-pm.sh --via direct --model claude-opus-5-5` });
  } finally { db.close(); }
});

test("POST /pm-launch-config switches the backend for the NEXT restart and validates input", async () => {
  const db = database();
  try {
    const deps = { db, pmLaunchPlan, PM_LAUNCHER_CONFIG_KEY, PM_MODEL_CONFIG_KEY };
    const post = route("/pm-launch-config", "post", deps);
    const get = route("/pm-launch-config", "get", deps);
    assert.equal(get(context(null)).body.launcher, "cliproxy");
    const ok = await post(context({ launcher: "direct" }));
    assert.equal(ok.status, 200);
    assert.equal(ok.body.command, `bash ${SCRIPTS}/launch-pm.sh --via direct --model claude-opus-5-5`);
    assert.equal(ok.body.effective, "next PM restart");
    assert.equal(db.getConfig(PM_LAUNCHER_CONFIG_KEY), "direct");
    for (const bad of [{}, { launcher: "proxy" }, { model: "x; touch /tmp/pwn" }, { model: 5 }]) {
      assert.equal((await post(context(bad))).status, 400, JSON.stringify(bad));
    }
    assert.equal(db.getConfig(PM_LAUNCHER_CONFIG_KEY), "direct", "a refused request mutates nothing");
    assert.equal((await post(context({ launcher: "cliproxy", model: "deepseek" }))).body.command,
      `bash ${SCRIPTS}/launch-pm.sh --via cliproxy --model deepseek`);
    assert.equal(db.getEvents(0, 5, "pm_launch_config_set").length, 2);
  } finally { db.close(); }
});

test("with_devs arming exempts DND and non-running dev slots (decommissioned S5-S8); live devs stay armed", async () => {
  const db = database();
  try {
    db.updateSlot(3, { occupied: true, dnd: true });
    for (const slot of [1, 2, 3]) db.logEvent(slot, "SessionStart", "SessionStart", null, { session_id: `s${slot}` });
    const running = new Set([1, 2, 3, 4]);
    const healthChecker = { getPaneCommandPublic: async (slot: number) => running.has(slot) ? (slot === 2 ? "2.1.312" : "claude") : null };
    const post = route("/exit-pending", "post", { db, healthChecker, devSlots, DEFAULT_DEV_SLOT_COUNT, AGENT_COMMANDS });
    assert.equal((await post(context({ enabled: true, pm_only: true, with_devs: true, continuation_prompt: "resume" }))).status, 200);
    const status = db.getExitStatus();
    assert.equal(status.with_devs, true);
    assert.deepEqual([0, 1, 2].map((slot) => status.cycled[slot]), [false, false, false], "PM and live devs armed");
    assert.deepEqual([3, 4, 5, 6, 7, 8].map((slot) => status.cycled[slot]), [true, true, true, true, true, true],
      "DND, session-unknown and non-running exempt");
    const reasons = [3, 4, 5].map((slot) => JSON.parse(db.getEvents(slot, 1, "exit_pending_slot_exempt")[0].payload).reason);
    assert.deepEqual(reasons, ["dnd", "session_unknown", "no_running_agent"]);
    // Each armed slot is bound to its arm-time session/epoch.
    assert.equal(db.exitArmCheck(1, "s1"), "match");
    assert.equal(db.exitArmCheck(2, "s2"), "match");
    assert.equal(db.exitArmCheck(1, "later"), "session_changed");
    // A plain pm_only arm probes nothing and exempts nothing.
    const quiet = route("/exit-pending", "post", {
      db, healthChecker: { getPaneCommandPublic: async () => { throw new Error("must not probe"); } },
      devSlots, DEFAULT_DEV_SLOT_COUNT, AGENT_COMMANDS,
    });
    for (let slot = 1; slot <= 8; slot++) db.setConfig(`exit_cycled_${slot}`, "false");
    assert.equal((await quiet(context({ enabled: true, pm_only: true }))).status, 200);
    for (let slot = 1; slot <= 8; slot++) assert.equal(db.getExitStatus().cycled[slot], false);
  } finally { db.close(); }
});
