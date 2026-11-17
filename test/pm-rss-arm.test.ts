// Conditional PM Claude RSS arm at the server boundary (CTO race finding).
// Rajiv DM D0AMF0XE6TS thread 1794921293.133799, ts 1794921896.970909 (track Claude
// memory in hourlies, set exit pending automatically) and ts 1794923900.734289
// (easiest trigger is an alerts message; remove the 04:00 restart).
// The deterministic trace in test/fixtures/pm-rss-arm-trace.json is replayed against
// the REAL POST /exit-pending route body with a fresh in-memory DB per step.
// Added, not executed: MoP changes need no local proof (Rajiv DM D0AMF0XE6TS
// thread 1793979986.975849, ts 1793983042.752819).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

import { MoPDatabase } from "../src/db.js";
import { evaluatePmRssArm, PM_RSS_ARM_CONDITION, pmRssArmOwner } from "../src/pmRssArm.js";
import { PM_HANDOFF_REQUESTED_AT_KEY } from "../src/pmHandoffClear.js";
import {
  ensurePmExitHandoffObligation,
  exitHandoffRequestKey,
  PM_EXIT_HANDOFF_REQUESTED_AT_KEY,
  PM_EXIT_TERMINAL_KEY,
} from "../src/pmHandoffExit.js";
import type { HandoffObligationOps } from "../src/pmHandoffClear.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { isPMCadenceTaskRetired } from "../src/pmCadence.js";

type Step = {
  name: string;
  setup: string[];
  request: Record<string, unknown>;
  expect: Record<string, unknown>;
};
const trace = JSON.parse(readFileSync(new URL("./fixtures/pm-rss-arm-trace.json", import.meta.url), "utf8")) as {
  server_steps: Step[];
};
const source = (name: string) => readFileSync(new URL(`../src/${name}.ts`, import.meta.url), "utf8");

function find(node: ts.Node, predicate: (n: ts.Node) => boolean): ts.Node | undefined {
  if (predicate(node)) return node;
  let result: ts.Node | undefined;
  ts.forEachChild(node, (child) => { result ??= find(child, predicate); });
  return result;
}
/** The real server route body, executed with the named dependencies in scope. */
function route(path: string, method: string, deps: Record<string, unknown>) {
  const tree = ts.createSourceFile("server.ts", source("server"), ts.ScriptTarget.Latest, true);
  const call = find(tree, (n) => ts.isCallExpression(n) && n.expression.getText(tree) === `app.${method}`
    && n.arguments[0]?.getText(tree) === JSON.stringify(path)) as ts.CallExpression;
  assert.ok(call, `route ${method} ${path}`);
  const js = ts.transpileModule(`const handler = ${call.arguments[1].getText(tree)};`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function(...Object.keys(deps), `${js}; return handler;`)(...Object.values(deps));
}
const ctx = (body: unknown) => ({ req: { json: async () => body }, json: (b: unknown, status = 200) => ({ body: b, status }) });

/** In-memory obligation ledger: one row per request key, like pm-ops obligation-upsert. */
function fakeOps() {
  const rows: { key: string; evidence: Record<string, unknown> }[] = [];
  const ops: HandoffObligationOps = {
    upsert: async (key, evidence) => {
      const existing = rows.findIndex((row) => row.key === key);
      if (existing >= 0) return String(existing + 1);
      rows.push({ key, evidence });
      return String(rows.length);
    },
    resolveById: async () => true,
    lookup: (key) => {
      const index = rows.findIndex((row) => row.key === key);
      return index < 0 ? { status: "absent" }
        : { status: "found", id: String(index + 1), open: true,
          requestedAt: String(rows[index].evidence.requested_at ?? ""), sessionId: String(rows[index].evidence.session_id ?? "") };
    },
  };
  return { rows, ops };
}

function applySetup(db: MoPDatabase, name: string, probes: { maintenance: boolean; held: boolean; throws: boolean }, post: (b: unknown) => Promise<unknown>) {
  switch (name) {
    case "rss_arm":
      return post({ enabled: true, pm_only: true, with_devs: false, continuation_prompt: "resume",
        condition: "pm_rss", rss_bytes: 2576980378, threshold_bytes: 2147483648 });
    case "exit_pending_with_devs": db.setExitPending(true, false, true, "daily", true, true); return;
    case "exit_pending_dev_only": db.setExitPending(true, true); return;
    case "refused_terminal":
      db.setConfig(PM_EXIT_TERMINAL_KEY, JSON.stringify({
        episode: "refused-1", outcome: "refused_pre_effect", reason: "test", continuation_prompt: "kept", with_devs: false,
        at: new Date(0).toISOString(), pinned_work: 0, session_id: "pm", obligation_id: null,
      }));
      return;
    case "pm_clear_pending": db.setPendingClear(0); return;
    case "clear_handoff_requested": db.setConfig(PM_HANDOFF_REQUESTED_AT_KEY, new Date(1_700_000_000_000).toISOString()); return;
    case "maintenance_pending": probes.maintenance = true; return;
    case "boundary_held": probes.held = true; return;
    case "probe_throws": probes.throws = true; return;
    default: throw new Error(`unknown setup ${name}`);
  }
}

for (const step of trace.server_steps) {
  test(`trace: ${step.name}`, async () => {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
    try {
      db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
      const probes = { maintenance: false, held: false, throws: false };
      const pmRssArmProbes = {
        pendingMaintenance: () => { if (probes.throws) throw new Error("lookup failed"); return probes.maintenance ? { kind: "daily" } : null; },
        boundaryHeld: () => probes.held,
      };
      const post = route("/exit-pending", "post", { db, evaluatePmRssArm, pmRssArmProbes, PM_RSS_ARM_CONDITION });
      const { rows, ops } = fakeOps();
      const arm = async (body: unknown) => {
        const res = await post(ctx(body)) as { status: number; body: Record<string, any> };
        if (res.status === 200) await ensurePmExitHandoffObligation(db, ops);
        return res;
      };
      for (const name of step.setup) await applySetup(db, name, probes, arm);
      const before = JSON.stringify(db.getExitStatus());
      const beforeEpisode = db.getExitStatus().episode;
      const obligationsBefore = rows.length;

      const res = await arm(step.request) as { status: number; body: Record<string, any> };
      const e = step.expect as Record<string, any>;
      assert.equal(res.status, e.status, step.name);
      if (e.reason) assert.equal(res.body.reason, e.reason);
      if (e.conditional) assert.equal(res.body.conditional, e.conditional);
      if (e.owner_kind) assert.equal(res.body.owner.kind, e.owner_kind);
      if (typeof e.exit_pending === "boolean") assert.equal(db.getExitStatus().pending, e.exit_pending);
      if (typeof e.pm_only === "boolean") assert.equal(db.getExitStatus().pm_only, e.pm_only);
      if (typeof e.with_devs === "boolean") assert.equal(db.getExitStatus().with_devs, e.with_devs);
      if (e.state_unchanged) assert.equal(JSON.stringify(db.getExitStatus()), before, "a refusal mutates nothing");
      if (e.episode_changed) assert.notEqual(db.getExitStatus().episode, beforeEpisode);
      if (e.terminal_kept) assert.equal(db.getExitStatus().exit_terminal?.episode, "refused-1");
      assert.equal(rows.length, e.obligations, "exactly the expected number of PM housekeeping obligations");
      if (res.status === 200 && e.conditional) {
        // The single obligation is the existing pm_save_handoff exit row for this request.
        const requestedAt = db.getConfig(PM_EXIT_HANDOFF_REQUESTED_AT_KEY)!;
        assert.equal(rows[0].key, exitHandoffRequestKey(requestedAt));
        assert.equal(rows[0].evidence.variant, "exit");
        assert.equal(rows[0].evidence.pm_stop_actionable, 1);
      }
      if (res.status !== 200) {
        assert.equal(rows.length, obligationsBefore, "a refusal opens no obligation");
        const events = db.getEvents(0, 20, "pm_rss_arm_refused");
        if (step.request.condition === "pm_rss" && e.reason === "owner_state_preserved") assert.ok(events.length >= 1);
      }
    } finally { db.close(); }
  });
}

test("evaluatePmRssArm validates before it reads owner state, and an owner lookup failure preserves", () => {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  try {
    const none = { pendingMaintenance: () => null, boundaryHeld: () => false };
    const decision = evaluatePmRssArm(db, { condition: "pm_rss", enabled: true, pm_only: true, rss_bytes: 3, threshold_bytes: 2 }, none);
    assert.equal(decision.ok, true);
    assert.equal(pmRssArmOwner(db, none), null);
    const broken = { pendingMaintenance: () => { throw new Error("x"); }, boundaryHeld: () => false };
    assert.equal(pmRssArmOwner(db, broken)?.kind, "owner_state_unknown");
    const recover = evaluatePmRssArm(db, { condition: "pm_rss", enabled: true, pm_only: true, rss_bytes: 3, threshold_bytes: 2, recover_episode: "e" }, none);
    assert.equal(recover.ok, false);
  } finally { db.close(); }
});

test("the owner check and the arm are one synchronous step (no await between them)", () => {
  const text = source("server");
  const start = text.indexOf("const rssArm = body.condition === undefined");
  const end = text.indexOf("db.setExitPending(body.enabled", start);
  assert.ok(start > 0 && end > start);
  const between = text.slice(start, end).replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  // The only await after the evaluation is the with_devs dev-session probe, which the
  // rss arm refuses before it can be entered.
  assert.equal(between.match(/\bawait\b/g)?.length, 1);
  assert.match(between, /if \(withDevs\) \{\s*for \(const slot of devSlots\(DEFAULT_DEV_SLOT_COUNT\)\) \{\s*const command = await/);
});

test("rule 50: the RSS arm adds no gate, no hold and no UserPromptSubmit change", () => {
  const code = source("pmRssArm").replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  assert.doesNotMatch(code, /UserPromptSubmit|permissionDecision|"deny"|setPMBusy|isAdmissionFenced|setConfig|setExitPending/);
});

test("the daily 04:00 restart is retired in the cadence scheduler; no other task is", () => {
  assert.equal(isPMCadenceTaskRetired("daily-pm-restart"), true);
  for (const task of ["heartbeat", "morning-brief", "hourly-heartbeat", "growth-daily", "lifecycle-email", "rtb-split-update"] as const) {
    assert.equal(isPMCadenceTaskRetired(task), false, task);
  }
});
