// Rajiv, DM D0AMF0XE6TS thread 1793731816.594829, ts 1793752982.460979:
// "We should implement the pm exit pending the same way. The additional step here
// will be to stop all subagents after save handoff".
// PM decisions: reuse the clear flow's save detection on the SAME two files;
// subagent stop is a PM-side step in the same pm_save_handoff obligation
// (variant exit) acknowledged via POST /exit-pending/subagents-stopped; /exit only
// after save AND ack; never blocks; downstream respawn/continuation unchanged.
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { promisify } from "node:util";
import Database from "better-sqlite3";
import ts from "typescript";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { ProcessHealthChecker } from "../src/health.js";
import {
  armPmClearAfterSavedHandoff,
  pmOpsHandoffObligationOps,
  requestPmHandoffClear,
  PM_HANDOFF_ACTION,
  PM_HANDOFF_REQUESTED_AT_KEY,
  type HandoffObligationOps,
} from "../src/pmHandoffClear.js";
import {
  ensurePmExitHandoffObligation,
  evaluatePmExitHandoffAtStop,
  exitHandoffRequestKey,
  recordPmExitSubagentsStopped,
  PM_EXIT_HANDOFF_OBLIGATION_KEY,
  PM_EXIT_HANDOFF_REQUESTED_AT_KEY,
  PM_HANDOFF_EXIT_VARIANT,
} from "../src/pmHandoffExit.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const PM_OPS_LEGACY = new URL("../scripts/pm/shared-assets/claude/scripts/pm-ops-legacy.py", import.meta.url).pathname;
const run = promisify(execFile);
const source = (name: string) => readFileSync(new URL(`../src/${name}.ts`, import.meta.url), "utf8");

function find(node: ts.Node, predicate: (n: ts.Node) => boolean): ts.Node | undefined {
  if (predicate(node)) return node;
  let result: ts.Node | undefined;
  ts.forEachChild(node, child => { result ??= find(child, predicate); });
  return result;
}
/** The real server route body, executed with the named dependencies in scope. */
function route(path: string, method: string, deps: Record<string, unknown>) {
  const tree = ts.createSourceFile("server.ts", source("server"), ts.ScriptTarget.Latest, true);
  const call = find(tree, n => ts.isCallExpression(n) && n.expression.getText(tree) === `app.${method}`
    && n.arguments[0]?.getText(tree) === JSON.stringify(path)) as ts.CallExpression;
  assert.ok(call, `route ${method} ${path}`);
  const js = ts.transpileModule(`const handler = ${call.arguments[1].getText(tree)};`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function(...Object.keys(deps), `${js}; return handler;`)(...Object.values(deps));
}
const ctx = (body: unknown) => ({ req: { json: async () => body }, json: (b: unknown, status = 200) => ({ body: b, status }) });

function fixture() {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-handoff-exit-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
  const files = [join(directory, "MEMORY-session-handoff-current.md"), join(directory, "pm-continuation-note.md")];
  const past = new Date(Date.now() - 3_600_000);
  for (const file of files) { writeFileSync(file, "old\n"); utimesSync(file, past, past); }
  const ledger = join(directory, "pm-ops.db");
  execFileSync("python3", [PM_OPS_LEGACY, "init"], { env: { ...process.env, PM_OPS_DB: ledger, HOME: directory } });
  { const con = new Database(ledger); con.prepare("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('obligations', 29000)").run(); con.close(); }
  const argv: string[][] = [];
  const execute = (file: string, args: string[], options: { timeout: number }) => {
    argv.push(args);
    return run(file, args, { ...options, env: { ...process.env, PM_OPS_DB: ledger, HOME: directory } });
  };
  const exitOps = pmOpsHandoffObligationOps(execute, ledger, PM_OPS_LEGACY, PM_HANDOFF_EXIT_VARIANT);
  const clearOps = pmOpsHandoffObligationOps(execute, ledger, PM_OPS_LEGACY);
  const rows = () => {
    const con = new Database(ledger, { readonly: true });
    try {
      return con.prepare("SELECT id, status, target_id, title, required_action, dedupe_group FROM obligations WHERE kind='pm_save_handoff' ORDER BY id").all() as
        { id: number; status: string; target_id: string; title: string; required_action: string; dedupe_group: string }[];
    } finally { con.close(); }
  };
  const save = (file: string) => { const future = new Date(Date.now() + 5_000); writeFileSync(file, "new\n"); utimesSync(file, future, future); };
  const saveAll = () => files.forEach(save);
  const sends: string[] = [];
  const relay = {
    observeSlotPane: async () => ({ paneId: "%40" }),
    deliverGuardedToSlot: async (_slot: number, text: string, guard: any) => {
      const refused = (await guard.recheck()) ?? guard.finalCheck();
      if (refused) return { outcome: "refused_pre_effect", reason: refused };
      sends.push(text); return { outcome: "delivered" };
    },
    injectToPM() {}, sendToSlot() { throw Error("must not send to a dev"); },
  };
  const processor = new HookProcessor(db, relay as any) as any;
  processor.pmExitHandoffOps = exitOps;
  processor.pmExitHandoffFiles = files;
  const post = route("/exit-pending", "post", { db });
  const ack = route("/exit-pending/subagents-stopped", "post", { db, recordPmExitSubagentsStopped });
  const arm = async () => {
    const res = await post(ctx({ enabled: true, pm_only: true, continuation_prompt: "Read resume. Resume PM." }));
    assert.equal(res.status, 200);
    return ensurePmExitHandoffObligation(db, exitOps);
  };
  const stop = (session = "pm") => processor.process(0, { type: "Stop", session_id: session });
  return { db, files, exitOps, clearOps, argv, rows, save, saveAll, sends, processor, post, ack, arm, stop, directory,
    close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); } };
}
const until = async (done: () => boolean) => {
  for (let i = 0; i < 100 && !done(); i++) await new Promise((r) => setTimeout(r, 100));
};

test("REST PM-only exit opens ONE pm_save_handoff exit obligation; /exit only after save AND subagents-stopped ack", async () => {
  const f = fixture();
  try {
    const id = await f.arm();
    assert.ok(id);
    const [row] = f.rows();
    assert.equal(f.rows().length, 1);
    assert.equal(row.status, "open");
    assert.equal(row.target_id, exitHandoffRequestKey(f.db.getConfig(PM_EXIT_HANDOFF_REQUESTED_AT_KEY)!));
    assert.match(row.required_action, /TaskStop/);
    assert.match(row.required_action, /\/exit-pending\/subagents-stopped/);
    assert.equal(row.dedupe_group, "pm_save_handoff:pm:exit");

    // Neither condition: Stop falls through, nothing sent, exit still pending.
    assert.deepEqual(await f.stop(), {});
    assert.deepEqual(f.sends, []);
    assert.equal(f.db.getExitStatus().cycled[0], false);

    // Save only (the SAME two files the clear flow watches): still waiting.
    f.saveAll();
    await f.stop();
    assert.deepEqual(f.sends, []);
    assert.equal(f.db.getExitStatus().cycled[0], false);

    // Ack: the next Stop sends exactly one /exit and resolves this request's row.
    const acked = await f.ack(ctx({}));
    assert.equal(acked.status, 200);
    assert.equal(acked.body.ok, true);
    await f.stop();
    await f.stop();
    assert.deepEqual(f.sends, ["/exit"]);
    assert.equal(f.db.getExitStatus().cycled[0], true);
    assert.equal(f.db.getExitStatus().pm_only, true, "downstream respawn + continuation unchanged");
    assert.equal(f.db.getEvents(0, 5, "exit_pending_triggered").length, 1);
    await until(() => f.rows()[0].status === "resolved");
    assert.equal(f.rows()[0].status, "resolved");
  } finally { f.close(); }
});

test("ack without the save keeps waiting; one file saved is not a save", async () => {
  const f = fixture();
  try {
    await f.arm();
    assert.equal((await f.ack(ctx({}))).status, 200);
    await f.stop();
    f.save(f.files[0]);
    await f.stop();
    assert.deepEqual(f.sends, []);
    assert.deepEqual(evaluatePmExitHandoffAtStop(f.db, { currentSessionId: "pm", ops: f.exitOps, files: f.files }),
      { kind: "waiting", saved: false, subagentsStopped: true });
    f.save(f.files[1]);
    await f.stop();
    assert.deepEqual(f.sends, ["/exit"]);
  } finally { f.close(); }
});

test("explicit resolve of this request's own row is a save signal (reused clear-flow detection); ack still required", async () => {
  const f = fixture();
  try {
    const id = await f.arm();
    await f.exitOps.resolveById(id!, "PM saved handoff");
    await f.stop();
    assert.deepEqual(f.sends, []);
    await f.ack(ctx({}));
    await f.stop();
    assert.deepEqual(f.sends, ["/exit"]);
  } finally { f.close(); }
});

test("ack endpoint: 409 without a pending exit or on episode mismatch; re-arming discards an earlier ack", async () => {
  const f = fixture();
  try {
    assert.equal((await f.ack(ctx({}))).status, 409);
    await f.arm();
    assert.equal((await f.ack(ctx({ episode: "other" }))).status, 409);
    assert.equal((await f.ack(ctx({ episode: 5 }))).status, 400);
    assert.equal((await f.ack(ctx({ episode: f.db.getExitStatus().episode }))).status, 200);
    assert.ok(f.db.getExitStatus().subagents_stopped_at);
    // A new request (new episode) never inherits the old ack.
    await f.post(ctx({ enabled: true, pm_only: true }));
    assert.equal(f.db.getExitStatus().subagents_stopped_at, null);
    f.saveAll();
    await f.stop();
    assert.deepEqual(f.sends, []);
    // Disabling clears the handoff entirely.
    await f.post(ctx({ enabled: false }));
    assert.equal(f.db.getExitStatus().handoff_requested_at, null);
    assert.equal((await f.ack(ctx({}))).status, 409);
  } finally { f.close(); }
});

test("PM session change cancels the exit request (as the clear flow does) and resolves its row; no /exit", async () => {
  const f = fixture();
  try {
    await f.arm();
    f.saveAll();
    await f.ack(ctx({}));
    f.db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm2", source: "clear" });
    await f.stop("pm2");
    assert.deepEqual(f.sends, []);
    assert.equal(f.db.getExitStatus().pending, false);
    assert.equal(f.db.getEvents(0, 1, "pm_exit_handoff_cancelled").length, 1);
    await until(() => f.rows()[0].status === "resolved");
    assert.equal(f.rows()[0].status, "resolved");
  } finally { f.close(); }
});

test("new PM input after the ready Stop refuses the /exit write without holding the input", async () => {
  const f = fixture();
  try {
    await f.arm();
    f.saveAll();
    await f.ack(ctx({}));
    f.processor.relay.observeSlotPane = async () => {
      f.db.logEvent(0, "UserPromptSubmit", "UserPromptSubmit", null, {});
      return { paneId: "%40" };
    };
    await f.stop();
    assert.deepEqual(f.sends, []);
    assert.equal(f.db.getEvents(0, 1, "UserPromptSubmit").length, 1);
  } finally { f.close(); }
});

test("waiting never blocks: no Stop decision, no input/tool gating, hooks return {} for every PM event", async () => {
  const f = fixture();
  try {
    await f.arm();
    for (const type of ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]) {
      const response = await f.processor.process(0, { type, session_id: "pm", tool_name: "Bash", tool_input: {} });
      assert.equal(response?.decision, undefined, `${type} must not be gated`);
    }
    assert.deepEqual(f.sends, []);
  } finally { f.close(); }
});

test("lost obligation write: the exit stays pending and the next waiting Stop retries by request key (one row)", async () => {
  const f = fixture();
  try {
    let fail = true;
    const flaky: HandoffObligationOps = { ...f.exitOps, upsert: async (k, e) => { if (fail) throw Error("boom"); return f.exitOps.upsert(k, e); } };
    f.processor.pmExitHandoffOps = flaky;
    await f.post(ctx({ enabled: true, pm_only: true }));
    assert.equal(await ensurePmExitHandoffObligation(f.db, flaky), null);
    assert.equal(f.db.getExitStatus().pending, true);
    fail = false;
    await f.stop();
    await until(() => !!f.db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY));
    assert.equal(f.rows().length, 1);
    assert.ok(f.db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY));
    await ensurePmExitHandoffObligation(f.db, flaky);
    assert.equal(f.rows().length, 1, "idempotent per request");
  } finally { f.close(); }
});

test("full sequence: saved+acked exit -> watchdog boot -> continuation -> dev-only exits (downstream unchanged)", async () => {
  const f = fixture();
  try {
    await f.arm();
    f.saveAll();
    await f.ack(ctx({}));
    await f.stop();
    assert.deepEqual(f.sends, ["/exit"]);
    const messages: string[] = [];
    const health = new ProcessHealthChecker(f.db, {
      deliverGuardedToSlot: async (_s: number, text: string) => { messages.push(text); return { outcome: "delivered" }; },
    } as any) as any;
    await health.deliverPMRestartContinuation("%40");
    assert.deepEqual(messages, ["Read resume. Resume PM."]);
    const status = f.db.getExitStatus();
    assert.equal(status.pm_only, false);
    assert.equal(status.dev_only, true);
    assert.equal(status.handoff_requested_at, null, "dev-only episode needs no PM handoff");
  } finally { f.close(); }
});

// ─── Clear-flow regression (release 9e3ce9dc behaviour must be unchanged) ───

test("clear flow: default obligation argv is byte-identical to 9e3ce9dc (title, action, dedupe group, target)", async () => {
  const f = fixture();
  try {
    await f.clearOps.upsert("pm-dhruva:2026-11-04T00:00:00.000Z", { requested_at: "2026-11-04T00:00:00.000Z" });
    const args = f.argv.at(-1)!;
    const at = (flag: string) => args[args.indexOf(flag) + 1];
    assert.equal(at("--kind"), "pm_save_handoff");
    assert.equal(at("--target-id"), "pm-dhruva:2026-11-04T00:00:00.000Z");
    assert.equal(at("--title"), "Save PM handoff before MoP injects /clear");
    assert.equal(at("--action"), PM_HANDOFF_ACTION);
    assert.equal(at("--dedupe-group"), "pm_save_handoff:pm");
    assert.doesNotMatch(PM_HANDOFF_ACTION, /TaskStop|subagents/);
  } finally { f.close(); }
});

test("clear flow: a pending exit handoff never satisfies, consumes or cancels a clear request, and vice versa", async () => {
  const f = fixture();
  try {
    // Clear requested first, then the exit armed: separate keys and rows.
    const clear = await requestPmHandoffClear(f.db, { source: "op", sessionId: "pm", nowMs: Date.now(), automatic: false, ops: f.clearOps });
    assert.equal(clear.kind, "awaiting_handoff");
    await f.arm();
    assert.equal(f.rows().length, 2);
    assert.notEqual(f.rows()[0].target_id, f.rows()[1].target_id);
    // Ack alone affects neither flow.
    await f.ack(ctx({}));
    let cleared = 0;
    const armed = await armPmClearAfterSavedHandoff(f.db, {
      sessionId: "pm", ops: f.clearOps, files: f.files,
      requestClear: async () => { cleared++; return { kind: "deferred_busy" }; },
    });
    assert.equal(armed.outcome.kind, "waiting");
    assert.equal(cleared, 0);
    // The clear's save detection is unchanged: the saved files arm it exactly once.
    f.saveAll();
    const ready = await armPmClearAfterSavedHandoff(f.db, {
      sessionId: "pm", ops: f.clearOps, files: f.files,
      requestClear: async () => { cleared++; return { kind: "deferred_busy" }; },
    });
    assert.equal(ready.outcome.kind, "ready");
    assert.equal(cleared, 1);
    assert.equal(f.db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY), "");
    // The exit request is untouched by the clear consuming its own request.
    assert.ok(f.db.getConfig(PM_EXIT_HANDOFF_REQUESTED_AT_KEY));
    assert.equal(f.db.getExitStatus().pm_only, true);
  } finally { f.close(); }
});

test("direct (non-REST) PM-only arming keeps the unconditioned single-Stop /exit", async () => {
  const f = fixture();
  try {
    f.db.setExitPending(true, false, true, "resume");
    assert.equal(f.db.getExitStatus().handoff_requested_at, null);
    await f.stop();
    assert.deepEqual(f.sends, ["/exit"]);
  } finally { f.close(); }
});

test("rule 50 source pins: no UserPromptSubmit or tool gating added by the exit handoff", () => {
  const exitSrc = source("pmHandoffExit");
  assert.doesNotMatch(exitSrc, /UserPromptSubmit|PreToolUse|decision:\s*"block"|setTimeout|setInterval/);
  const hooks = source("hooks");
  const start = hooks.indexOf("a REST-armed PM exit waits");
  assert.ok(start > 0);
  const block = hooks.slice(start, hooks.indexOf("exit_pending_triggered", start));
  assert.doesNotMatch(block, /decision|block:/);
});

test("daily-pm-restart script and skill describe save -> TaskStop -> subagents-stopped ack -> /exit", () => {
  const root = new URL("../scripts/pm/shared-assets/claude/", import.meta.url);
  for (const file of ["scripts/pm/daily-pm-restart.sh", "skills/daily-pm-restart/SKILL.md"]) {
    const text = readFileSync(new URL(file, root), "utf8");
    assert.match(text, /pm_save_handoff/, file);
    assert.match(text, /TaskStop/, file);
    assert.match(text, /\/exit-pending\/subagents-stopped/, file);
  }
});
