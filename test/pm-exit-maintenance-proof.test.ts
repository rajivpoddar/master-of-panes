// CTO REVISE proof packet (C0ALZJHGE49 thread 1793754459.969419, ts 1793754925.429599)
// for the pm_save_handoff `exit` variant (Rajiv D0AMF0XE6TS thread 1793731816.594829,
// ts 1793752982.460979), amended by Rajiv DM D0AMF0XE6TS thread 1793754554.496299,
// ts 1793758450.451269: "remove the acks and the gates. as long all subgents are
// stopped and resume point is saved, it should work." (supersedes the ACK/fence REVISE).
// One harness: real Hono + PM soft-boundary middleware (pinned
// pre-await Stop authority, ONE maintenance command per Stop) + real TmuxRelay guarded
// writers (clear: writePMClearFenced; exit: deliverGuardedToSlot) + real HookProcessor +
// an isolated actual pm-ops ledger (pm-ops-legacy.py, tmp PM_OPS_DB).
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { promisify } from "node:util";
import Database from "better-sqlite3";
import { Hono } from "hono";
import ts from "typescript";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor, type PmExitStopPin } from "../src/hooks.js";
import { ProcessHealthChecker } from "../src/health.js";
import { makePmClearWriteFence, requestPmClearOnce, retryDeferredPmClearOnStop } from "../src/pmClearLatch.js";
import {
  armPmClearAfterSavedHandoff,
  pmOpsHandoffObligationOps,
  requestPmHandoffClear,
  PM_HANDOFF_REQUESTED_AT_KEY,
  type HandoffObligationOps,
} from "../src/pmHandoffClear.js";
import {
  ensurePmExitHandoffObligation,
  exitHandoffRequestKey,
  selectPmStopMaintenance,
  PM_EXIT_HANDOFF_OBLIGATION_KEY,
  PM_EXIT_HANDOFF_OBLIGATION_STATE_KEY,
  PM_HANDOFF_EXIT_VARIANT,
} from "../src/pmHandoffExit.js";
import { registerPMSoftBoundary, type PMStopAuthority, type PMStopAuthorityPins } from "../src/pmSoftBoundary.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const PM_OPS_LEGACY = new URL("../scripts/pm/shared-assets/claude/scripts/pm-ops-legacy.py", import.meta.url).pathname;
const run = promisify(execFile);
const until = async (done: () => boolean, maxMs = 8_000) => {
  for (let waited = 0; waited < maxMs && !done(); waited += 50) await new Promise((r) => setTimeout(r, 50));
};

function find(node: ts.Node, predicate: (n: ts.Node) => boolean): ts.Node | undefined {
  if (predicate(node)) return node;
  let result: ts.Node | undefined;
  ts.forEachChild(node, child => { result ??= find(child, predicate); });
  return result;
}
/** The real server route handler, executed with its named dependencies. */
function route(path: string, deps: Record<string, unknown>) {
  const source = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const tree = ts.createSourceFile("server.ts", source, ts.ScriptTarget.Latest, true);
  const call = find(tree, n => ts.isCallExpression(n) && n.expression.getText(tree) === "app.post"
    && n.arguments[0]?.getText(tree) === JSON.stringify(path)) as ts.CallExpression;
  assert.ok(call, path);
  const js = ts.transpileModule(`const handler = ${call.arguments[1].getText(tree)};`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function(...Object.keys(deps), `${js}; return handler;`)(...Object.values(deps));
}
const ctx = (body: unknown) => ({ req: { json: async () => body }, json: (b: unknown, status = 200) => ({ body: b, status }) });

function harness() {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-exit-proof-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const files = [join(directory, "MEMORY-session-handoff-current.md"), join(directory, "pm-continuation-note.md")];
  const past = new Date(Date.now() - 3_600_000);
  for (const file of files) { writeFileSync(file, "old\n"); utimesSync(file, past, past); }
  const ledger = join(directory, "pm-ops.db");
  execFileSync("python3", [PM_OPS_LEGACY, "init"], { env: { ...process.env, PM_OPS_DB: ledger, HOME: directory } });
  { const con = new Database(ledger); con.prepare("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('obligations', 31000)").run(); con.close(); }
  const execute = (file: string, args: string[], options: { timeout: number }) =>
    run(file, args, { ...options, env: { ...process.env, PM_OPS_DB: ledger, HOME: directory } });
  const exitOps = pmOpsHandoffObligationOps(execute, ledger, PM_OPS_LEGACY, PM_HANDOFF_EXIT_VARIANT);
  const clearOps = pmOpsHandoffObligationOps(execute, ledger, PM_OPS_LEGACY);
  const rows = () => {
    const con = new Database(ledger, { readonly: true });
    try {
      return con.prepare("SELECT id, status, target_id FROM obligations WHERE kind='pm_save_handoff' ORDER BY id").all() as
        { id: number; status: string; target_id: string }[];
    } finally { con.close(); }
  };
  const saveAll = () => { const future = new Date(Date.now() + 5_000); for (const file of files) { writeFileSync(file, "new\n"); utimesSync(file, future, future); } };

  // Fake tmux pane: composer emulation; every paste records the pasted text.
  let generation = 0;
  let composer = "", buffer = "";
  let busy = false; // PM mid-turn: Claude renders the busy chevron (153;153;153)
  let queuedBanner = false; // Claude's native queued-input banner below an EMPTY composer
  let swallowEnter = false;
  let ctrlU = 0;
  const pasted: string[] = [];
  let enters = 0;
  let escapes = 0;
  let observeBarrier: (() => Promise<void>) | null = null;
  let onPaste: (() => void) | null = null;
  const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude", runShell: async (command: string) => {
    if (command.includes("#{pane_id}|#{pane_current_command}")) return { stdout: "%1|claude\n", stderr: "" };
    if (command.includes("#{pane_id}|#{pane_current_path}")) return { stdout: "%1|/Users/rajiv/Downloads/projects/heydonna-app\n", stderr: "" };
    if (command.includes("rev-parse --show-toplevel")) return { stdout: "/Users/rajiv/Downloads/projects/heydonna-app\n", stderr: "" };
    if (command.includes("#{pane_pid}")) return { stdout: "321\n", stderr: "" };
    if (command.startsWith("ps -p 321")) return { stdout: "/opt/claude", stderr: "" };
    if (command.startsWith("tmux capture-pane")) {
      const chevron = busy && command.includes(" -e ") ? "\x1b[38;2;153;153;153m❯\x1b[39m" : "❯";
      const banner = queuedBanner ? "\n  ⏵ press up to edit queued messages" : "";
      return { stdout: `${"─".repeat(40)}\n${chevron} ${composer}\n${"─".repeat(40)}${banner}`, stderr: "" };
    }
    if (command.endsWith(" Escape")) escapes++;
    const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(command);
    if (load) buffer = readFileSync(load[1]!, "utf8");
    if (command.startsWith("tmux paste-buffer")) { composer = buffer; pasted.push(buffer); onPaste?.(); }
    if (command.endsWith(" C-u")) { ctrlU++; composer = ""; }
    if (command.endsWith(" Enter")) { enters++; if (!swallowEnter) composer = ""; }
    return { stdout: "", stderr: "" };
  } } as any);
  relay.setDatabase(db);
  const observe = relay.observeSlotPane.bind(relay);
  relay.observeSlotPane = async (slot: number, lines?: number) => {
    if (observeBarrier) await observeBarrier();
    return observe(slot, lines);
  };
  const sessionToken = () => String(db.getEvents(0, 1, "SessionStart")[0]?.id ?? 0);
  const pins: PMStopAuthorityPins = {
    snapshot: () => ({ generation, sessionToken: sessionToken() }),
    claim: (entry, sessionId) => entry.generation === generation && entry.sessionToken === sessionToken()
      ? { generation: entry.generation, sessionToken: entry.sessionToken, sessionId } : null,
    isCurrent: (a) => a.generation === generation && a.sessionToken === sessionToken(),
  };
  const processor = new HookProcessor(db, relay) as any;
  processor.pmExitHandoffOps = exitOps;
  processor.pmExitHandoffFiles = files;
  const deliveries: Promise<unknown>[] = [];
  const app = new Hono();
  const boundary = registerPMSoftBoundary(app, db, relay, (authority?: PMStopAuthority) => {
    const delivery = retryDeferredPmClearOnStop({
      db, source: "pm_native_stop", nowMs: Date.now(), staleAfterMs: 0, recentSuppressMs: 0,
      hasRecentClearEvent: false, laterLifecycleEvent: null, backoffMs: 0, settleMs: 0,
      wait: async () => { throw new Error("no idle timer"); }, isCurrent: () => pins.isCurrent(authority!),
      isIdleProven: () => true,
      send: async ({ requestedAt }) => {
        const written = await boundary.withMaintenanceWriter(() => relay.writePMClearFenced({ paneId: "%1",
          check: makePmClearWriteFence({ db, requestedAt, generation: authority!.generation,
            sessionToken: authority!.sessionToken, currentGeneration: () => generation,
            currentSessionToken: sessionToken, idleProven: () => true }) }));
        return { success: written.ok, busy: written.busy, zeroEffect: written.zeroEffect };
      },
    }).finally(() => boundary.reconcileFailedMaintenance());
    deliveries.push(delivery);
    return delivery;
  }, async (sessionId, authority) => {
    await armPmClearAfterSavedHandoff(db, { sessionId, ops: clearOps, files,
      isCurrent: () => !!authority && pins.isCurrent(authority),
      requestClear: () => requestPmClearOnce({
        db, source: "pm_handoff_saved_stop", nowMs: Date.now(), staleAfterMs: 0, recentSuppressMs: 0,
        hasRecentClearEvent: false, laterLifecycleEvent: null, deferUntilStop: true, isPMBusy: () => false,
        send: async () => { throw new Error("arm must never send"); } }) });
  }, pins, (sessionId, workPin) => selectPmStopMaintenance(db, { sessionId, workPin, ops: exitOps, files }));
  // Same shape as the server's main hook route: the middleware's pin reaches HookProcessor.
  app.post("/hooks/slot/0", async (c) => {
    const raw = await c.req.json();
    if (["UserPromptSubmit", "PreToolUse", "SessionStart"].includes(raw.hook_event_name)) generation++;
    const pin = (c as unknown as { get(key: string): PmExitStopPin | undefined }).get("pmExitStopPin");
    const { hook_event_name, ...rest } = raw;
    return c.json(await processor.process(0, { type: hook_event_name, ...rest }, pin) ?? {});
  });
  let toolSeq = 0;
  /** A hook whose request body is held open: the middleware snapshots, then awaits body parsing. */
  const holdHook = (event: string, extra: Record<string, unknown> = {}) => {
    let release!: () => void;
    const text = JSON.stringify({ hook_event_name: event, session_id: "pm", mop_tool_tracking: 1, ...extra });
    const body = new ReadableStream<Uint8Array>({
      start(controller) { release = () => { controller.enqueue(new TextEncoder().encode(text)); controller.close(); }; },
    });
    const response = app.request("/hooks/slot/0", {
      method: "POST", headers: { "Content-Type": "application/json" }, body, duplex: "half",
    } as RequestInit);
    return { response, release: () => release() };
  };
  const hook = async (event: string, extra: Record<string, unknown> = {}) => {
    const send = (name: string, body: Record<string, unknown>) => app.request("/hooks/slot/0", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hook_event_name: name, session_id: "pm", mop_tool_tracking: 1, ...body }) });
    if (event !== "PreToolUse") return send(event, extra);
    // A real tool call: identified PreToolUse admission, then its PostToolUse.
    const tool_use_id = `toolu_${++toolSeq}`;
    const response = await send("PreToolUse", { tool_use_id, ...extra });
    const decision = await response.clone().json();
    assert.notEqual(decision?.hookSpecificOutput?.permissionDecision, "deny", "PM tools are never gated");
    await send("PostToolUse", { tool_use_id, ...extra });
    return response;
  };
  const post = route("/exit-pending", { db });
  const status = () => db.getExitStatus();
  const arm = async () => {
    assert.equal((await post(ctx({ enabled: true, pm_only: true, continuation_prompt: "Read resume. Resume PM." }))).status, 200);
    return ensurePmExitHandoffObligation(db, exitOps);
  };
  const exits = () => pasted.filter((p) => p === "/exit").length;
  const clears = () => pasted.filter((p) => p === "/clear").length;
  return {
    db, files, exitOps, clearOps, rows, saveAll, relay, processor, hook, post, arm, status, exits, clears,
    pasted, enters: () => enters, escapes: () => escapes, ctrlU: () => ctrlU, holdHook, setBusy: (b: boolean) => { busy = b; },
    setQueuedBanner: (b: boolean) => { queuedBanner = b; }, setSwallowEnter: (b: boolean) => { swallowEnter = b; },
    setComposer: (text: string) => { composer = text; }, setOnPaste: (f: (() => void) | null) => { onPaste = f; }, setObserveBarrier: (b: (() => Promise<void>) | null) => { observeBarrier = b; },
    settle: async () => { await Promise.all(deliveries); await new Promise((r) => setTimeout(r, 30)); },
    close: () => { relay.setPMBusy(true); db.close(); rmSync(directory, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 }); },
  };
}
type H = ReturnType<typeof harness>;
async function boot(h: H, session = "pm", source = "startup") {
  await h.hook("SessionStart", { session_id: session, source, cwd: "/Users/rajiv/Downloads/projects/heydonna-app" });
}

test("healthy flow: save -> natural Stop -> one /exit -> continuation -> dev-only (no ACK)", async () => {
  const h = harness();
  try {
    await boot(h);
    const id = await h.arm();
    assert.ok(id);
    await h.hook("Stop");
    assert.equal(h.exits(), 0, "nothing before the save");
    h.saveAll();
    await h.hook("Stop");
    await h.hook("Stop");
    assert.equal(h.exits(), 1, "exactly one /exit");
    assert.equal(h.clears(), 0);
    assert.equal(h.status().cycled[0], true);
    await until(() => h.rows()[0]?.status === "resolved");
    assert.equal(h.rows()[0]!.status, "resolved");
    const messages: string[] = [];
    const health = new ProcessHealthChecker(h.db, {
      deliverGuardedToSlot: async (_s: number, text: string) => { messages.push(text); return { outcome: "delivered" }; },
    } as any) as any;
    await health.deliverPMRestartContinuation("%1");
    assert.deepEqual(messages, ["Read resume. Resume PM."]);
    assert.equal(h.status().dev_only, true);
  } finally { h.close(); }
});

test("saved handoff still refuses a different PM session at delivery", async () => {
  const h = harness();
  try {
    await boot(h);
    await h.arm();
    h.saveAll();
    h.setObserveBarrier(async () => { h.setObserveBarrier(null); await boot(h, "successor"); });
    await h.hook("Stop");
    assert.equal(h.exits(), 0);
    assert.equal(h.db.getExitStatus().exit_terminal?.reason, "pm_owner_changed");
  } finally { h.close(); }
});

test("inbound between save and Stop does not refuse exit or alter inbound custody", async () => {
  const h = harness();
  try {
    await boot(h);
    await h.arm();
    h.saveAll();
    h.db.enqueuePendingPMEvent(1, "handoff", "inbound");
    const response = await h.hook("UserPromptSubmit", { prompt: "new work" });
    assert.equal(response.status, 200);
    await h.hook("Stop");
    assert.equal(h.exits(), 1);
    assert.equal(h.db.peekPendingPMEvents().length, 1, "exit does not change inbound delivery");
    assert.equal(h.db.getEvents(0, 5, "pm_exit_refused_terminal").length, 0);
  } finally { h.close(); }
});

test("Rajiv proof: request -> save -> PM Stop on a BUSY pane while a new prompt arrives -> exactly one /exit, continuation kept, no ACK", async () => {
  const h = harness();
  try {
    await boot(h);
    await h.arm();
    h.saveAll();
    // The PM Stop hook keeps the pane busy (due-obligation feedback) and new input lands mid-write.
    h.setBusy(true);
    h.setObserveBarrier(async () => { h.setObserveBarrier(null); await h.hook("UserPromptSubmit", { prompt: "slack event" }); });
    await h.hook("Stop");
    assert.equal(h.exits(), 1, "exactly one /exit written");
    assert.equal(h.enters(), 1);
    assert.equal(h.escapes(), 0, "never Escape into a busy PM turn");
    const triggered = JSON.parse(h.db.getEvents(0, 1, "exit_pending_triggered")[0]!.payload);
    assert.equal(triggered.delivery, "delivered");
    assert.equal(h.db.getEvents(0, 5, "pm_exit_refused_terminal").length, 0);
    assert.equal(h.status().continuation_prompt, "Read resume. Resume PM.", "continuation preserved");
    assert.equal(h.status().exit_terminal, null);
    // Exactly once: later Stops and new work never resend.
    await h.hook("PreToolUse", { tool_name: "Agent", tool_input: {} });
    await h.hook("Stop");
    await h.hook("Stop");
    assert.equal(h.exits(), 1);
    assert.equal(h.db.getEvents(0, 10, "exit_pending_triggered").length, 1);
    // No ACK anywhere in the flow.
    assert.equal(h.db.getEvents(0, 10, "pm_exit_subagents_stopped").length, 0);
    const health = new ProcessHealthChecker(h.db, {
      deliverGuardedToSlot: async (_s: number, text: string) => { assert.equal(text, "Read resume. Resume PM."); return { outcome: "delivered" }; },
    } as any) as any;
    await boot(h, "pm-next", "resume");
    await health.deliverPMRestartContinuation("%1");
    assert.equal(h.status().dev_only, true, "dev_only armed after PM continuation");
  } finally { h.close(); }
});

test("CTO REVISE ts 1793760137.229639: Stop body held while a new prompt is admitted + native queued banner -> exactly one /exit", async () => {
  const h = harness();
  try {
    await boot(h);
    await h.arm();
    h.saveAll();
    h.setBusy(true);
    h.setQueuedBanner(true); // native banner, EMPTY composer, before paste and before Enter
    const held = h.holdHook("Stop");
    await new Promise((r) => setTimeout(r, 20)); // middleware has pinned and is awaiting the body
    await h.hook("UserPromptSubmit", { prompt: "slack event admitted before selection" });
    held.release();
    await held.response;
    assert.equal(h.db.getEvents(0, 5, "pm_stop_maintenance_selected").length, 1, "exit selected despite the failed clear claim");
    assert.equal(h.exits(), 1, "exactly one /exit submission");
    assert.equal(h.enters(), 1);
    assert.equal(h.escapes(), 0, "zero Escape");
    assert.equal(h.ctrlU(), 0, "zero C-u");
    const triggered = JSON.parse(h.db.getEvents(0, 1, "exit_pending_triggered")[0]!.payload);
    assert.equal(triggered.delivery, "delivered", "confirmed by the composer clearing, not by the pre-existing banner");
    assert.equal(h.status().continuation_prompt, "Read resume. Resume PM.", "continuation retained");
    assert.equal(h.status().exit_terminal, null);
    assert.equal(h.db.getEvents(0, 5, "pm_exit_subagents_stopped").length, 0, "no ACK");
    // No work voiding and no resend.
    await h.hook("PreToolUse", { tool_name: "Agent", tool_input: {} });
    await h.hook("Stop");
    await h.hook("Stop");
    assert.equal(h.exits(), 1);
    assert.equal(h.db.getEvents(0, 10, "exit_pending_triggered").length, 1);
  } finally { h.close(); }
});

test("native queued banner negatives: a foreign draft is never touched; a pre-existing banner is never proof of submit", async () => {
  const draft = harness();
  try {
    await boot(draft);
    await draft.arm();
    draft.saveAll();
    draft.setQueuedBanner(true);
    draft.setComposer("PM is typing a draft");
    await draft.hook("Stop");
    assert.equal(draft.exits(), 0, "no paste over a foreign draft");
    assert.equal(draft.enters(), 0);
    assert.equal(draft.ctrlU(), 0);
    assert.equal(draft.escapes(), 0);
    assert.equal(JSON.parse(draft.db.getEvents(0, 1, "exit_pending_triggered")[0]!.payload).delivery, "refused_pre_effect");
  } finally { draft.close(); }
  const swallowed = harness();
  try {
    await boot(swallowed);
    await swallowed.arm();
    swallowed.saveAll();
    swallowed.setQueuedBanner(true);
    swallowed.setSwallowEnter(true); // /exit stays in the composer under a pre-existing banner
    await swallowed.hook("Stop");
    const delivery = JSON.parse(swallowed.db.getEvents(0, 1, "exit_pending_triggered")[0]!.payload).delivery;
    assert.equal(delivery, "uncertain", "pre-existing banner never confirms this submit");
    assert.equal(swallowed.status().exit_terminal, null, "uncertain stays consumed, no terminal");
    assert.equal(swallowed.ctrlU(), 0);
    await swallowed.hook("Stop");
    assert.equal(swallowed.db.getEvents(0, 10, "exit_pending_triggered").length, 1, "no resend");
  } finally { swallowed.close(); }
});

test("PM work, a subagent launch or new input after the save never voids readiness", async () => {
  const h = harness();
  try {
    await boot(h);
    await h.arm();
    h.saveAll();
    await h.hook("PreToolUse", { tool_name: "Agent", tool_input: { description: "new subagent" } });
    await h.hook("UserPromptSubmit", { prompt: "new input" });
    await h.hook("Stop");
    assert.equal(h.exits(), 1);
  } finally { h.close(); }
});

test("a zero-effect refusal (PM draft in the composer) ends in a durable terminal; recovery or a fresh request re-arms", async () => {
  const h = harness();
  try {
    await boot(h);
    await h.arm();
    h.saveAll();
    // A foreign draft is never overwritten: proven zero-effect refusal.
    h.setObserveBarrier(async () => { h.setObserveBarrier(null); h.setComposer("PM draft"); });
    await h.hook("Stop");
    assert.equal(h.exits(), 0, "no paste");
    assert.equal(h.enters(), 0, "no Enter");
    const triggered = JSON.parse(h.db.getEvents(0, 1, "exit_pending_triggered")[0]!.payload);
    assert.equal(triggered.delivery, "refused_pre_effect");
    assert.equal(h.db.getEvents(0, 1, "pm_exit_custody_released_zero_effect").length, 1, "proven zero effect releases custody");
    assert.equal(h.status().cycled[0], true, "the attempt record is kept (not erased)");
    const refused = h.status();
    assert.equal(refused.pending, false, "episode is not left indefinitely active/cycled/waiting");
    assert.equal(refused.continuation_delivery, "exit_refused");
    assert.equal(refused.continuation_prompt, "Read resume. Resume PM.", "continuation retained");
    const terminal = refused.exit_terminal as Record<string, unknown>;
    assert.equal(terminal.outcome, "refused_pre_effect");
    assert.equal(terminal.reason, "composer_not_empty");
    await until(() => h.rows().some((r) => r.target_id === `pm-dhruva:exit-refused:${terminal.episode}`));
    const refusedRow = h.rows().find((r) => r.target_id === `pm-dhruva:exit-refused:${terminal.episode}`);
    assert.equal(refusedRow?.status, "open", "actionable refused obligation for the owner");
    h.setComposer("");
    await h.hook("Stop");
    await h.hook("Stop");
    const health = new ProcessHealthChecker(h.db, {
      deliverGuardedToSlot: async () => { throw new Error("continuation must not be sent without a delivered exit + boot"); },
    } as any) as any;
    await health.deliverPMRestartContinuation("%1");
    await health.checkExitPending();
    assert.equal(h.exits(), 0, "zero paste after the refusal");
    assert.equal(h.db.getEvents(0, 10, "exit_pending_triggered").length, 1, "no autonomous resend");
    // Same-owner recovery edge.
    assert.equal((await h.post(ctx({ enabled: true, pm_only: true, recover_episode: "other" }))).status, 409);
    const recover = await h.post(ctx({ enabled: true, pm_only: true, recover_episode: terminal.episode }));
    assert.equal(recover.status, 200);
    await ensurePmExitHandoffObligation(h.db, h.exitOps);
    const rearmed = h.status();
    assert.equal(rearmed.pending, true);
    assert.notEqual(rearmed.episode, terminal.episode);
    assert.equal(rearmed.continuation_prompt, "Read resume. Resume PM.");
    assert.equal(rearmed.exit_terminal, null);
    await until(() => h.rows().find((r) => r.target_id === `pm-dhruva:exit-refused:${terminal.episode}`)?.status === "resolved");
    h.saveAll();
    await h.hook("Stop");
    assert.equal(h.exits(), 1, "owner-recovered episode exits once after the save");
  } finally { h.close(); }
});

test("a refused episode never blocks a fresh pm_only request with continuation_prompt (a38bee66 recovery)", async () => {
  const h = harness();
  try {
    await boot(h);
    await h.arm();
    h.saveAll();
    h.setObserveBarrier(async () => { h.setObserveBarrier(null); h.setComposer("PM draft"); });
    await h.hook("Stop");
    const a = h.status().exit_terminal as Record<string, unknown>;
    assert.equal(a.outcome, "refused_pre_effect");
    h.setComposer("");
    // A fresh request, no recover_episode.
    const fresh = await h.post(ctx({ enabled: true, pm_only: true, continuation_prompt: "Fresh continuation." }));
    assert.equal(fresh.status, 200);
    await ensurePmExitHandoffObligation(h.db, h.exitOps);
    assert.equal(h.status().exit_terminal, null);
    assert.equal(h.status().pending, true);
    await until(() => h.rows().find((r) => r.target_id === `pm-dhruva:exit-refused:${a.episode}`)?.status === "resolved");
    h.setBusy(true);
    h.saveAll();
    await h.hook("Stop");
    assert.equal(h.exits(), 1);
    assert.equal(h.status().continuation_prompt, "Fresh continuation.");
    // An old pin reaching HookProcessor after newer work still exits once (no staleness gate).
    const h2 = harness();
    try {
      await boot(h2);
      await h2.arm();
      h2.saveAll();
      const { latestPmWork, latestPmBoot } = await import("../src/pmHandoffExit.js");
      const oldPin: PmExitStopPin = { work: latestPmWork(h2.db), boot: latestPmBoot(h2.db).id, selected: true };
      await h2.hook("UserPromptSubmit", { prompt: "newer work" });
      await h2.processor.process(0, { type: "Stop", session_id: "pm" }, oldPin);
      assert.equal(h2.exits(), 1);
      assert.equal(h2.db.getEvents(0, 1, "pm_exit_stop_stale").length, 0);
    } finally { h2.close(); }
  } finally { h.close(); }
});

test("delayed recovery of refused episode A after a newer dev_only/global episode B: 409, B byte-identical, zero paste/Enter", async () => {
  for (const newer of [{ enabled: true, dev_only: true }, { enabled: true }]) {
    const h = harness();
    try {
      await boot(h);
      await h.arm();
      h.saveAll();
      h.setObserveBarrier(async () => { h.setObserveBarrier(null); h.setComposer("PM draft"); });
      await h.hook("Stop");
      h.setComposer("");
      const a = h.status().exit_terminal as Record<string, unknown>;
      assert.equal(a.outcome, "refused_pre_effect");
      // A normal newer episode B through the real route; terminal A survives.
      assert.equal((await h.post(ctx(newer))).status, 200);
      const before = JSON.stringify(h.status());
      const pastesBefore = h.pasted.length, entersBefore = h.enters();
      assert.ok(h.status().exit_terminal, "old terminal A still recorded");
      const late = await h.post(ctx({ enabled: true, pm_only: true, recover_episode: a.episode }));
      assert.equal(late.status, 409);
      assert.equal(JSON.stringify(h.status()), before, "B's episode/mode/continuation/cycle/attempt state unchanged");
      await h.hook("Stop");
      assert.equal(h.pasted.filter((p) => p === "/exit").length, 0);
      assert.equal(h.pasted.length, pastesBefore, "zero paste");
      assert.equal(h.enters(), entersBefore, "zero Enter");
    } finally { h.close(); }
  }
});

test("control: a possible/uncertain /exit effect stays consumed (custody kept, no terminal, no resend)", async () => {
  const h = harness();
  try {
    await boot(h);
    await h.arm();
    h.saveAll();
    // Foreign text joins the composer after the paste and before Enter: the effect was attempted.
    h.setOnPaste(() => { h.setOnPaste(null); h.setComposer("/exit plus a PM draft"); });
    await h.hook("Stop");
    const triggered = JSON.parse(h.db.getEvents(0, 1, "exit_pending_triggered")[0]!.payload);
    assert.equal(triggered.delivery, "uncertain");
    assert.equal(h.exits(), 1);
    assert.equal(h.enters(), 0);
    const status = h.status();
    assert.equal(status.pending, true);
    assert.equal(status.cycled[0], true);
    assert.equal(status.exit_terminal, null, "uncertain is never treated as proven no-effect");
    assert.equal(selectPmStopMaintenance(h.db, { ops: h.exitOps }), "exit_custody");
    await h.hook("Stop");
    assert.equal(h.exits(), 1, "no resend");
  } finally { h.close(); }
});

test("both intents ready: exactly ONE command (exit); clear intent kept; no lost continuation through a delayed SessionStart", async () => {
  const h = harness();
  try {
    await boot(h);
    const clear = await requestPmHandoffClear(h.db, { source: "op", sessionId: "pm", nowMs: Date.now() - 60_000, automatic: false, ops: h.clearOps });
    assert.equal(clear.kind, "awaiting_handoff");
    await h.arm();
    h.saveAll(); // satisfies BOTH requests
    await h.hook("Stop");
    await h.settle();
    assert.equal(h.exits(), 1);
    assert.equal(h.clears(), 0, "clear not dispatched");
    assert.equal(h.db.hasPendingClear(0), false, "clear not armed");
    assert.ok(h.db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY), "losing clear intent kept, not reset");
    // Duplicate Stops while SessionStart is delayed: exit custody holds the clear.
    await h.hook("Stop");
    await h.hook("Stop");
    await h.settle();
    assert.equal(h.exits(), 1);
    assert.equal(h.clears(), 0);
    // Delayed respawn boot; the continuation is still delivered.
    await boot(h, "pm-next", "resume");
    const messages: string[] = [];
    const health = new ProcessHealthChecker(h.db, {
      deliverGuardedToSlot: async (_s: number, text: string) => { messages.push(text); return { outcome: "delivered" }; },
    } as any) as any;
    await health.deliverPMRestartContinuation("%1");
    assert.deepEqual(messages, ["Read resume. Resume PM."]);
    assert.equal(h.status().dev_only, true);
  } finally { h.close(); }
});

test("a latched clear keeps custody: the ready exit yields (not consumed), exactly one /clear; exit follows in the new session", async () => {
  const h = harness();
  try {
    await boot(h);
    await requestPmHandoffClear(h.db, { source: "op", sessionId: "pm", nowMs: Date.now() - 60_000, automatic: false, ops: h.clearOps });
    h.saveAll();
    await h.hook("Stop"); // exit not pending: the saved clear is armed and written at this Stop
    await h.settle();
    assert.equal(h.clears(), 1);
    assert.equal(h.db.hasPendingClear(0), true, "clear latch awaiting SessionStart:clear");
    await h.arm();
    h.saveAll();
    await h.hook("Stop");
    await h.settle();
    assert.equal(h.clears(), 1, "never resent");
    assert.equal(h.exits(), 0, "exit yielded to the latched clear");
    assert.equal(h.status().cycled[0], false, "exit not consumed; continuation not stranded");
    assert.ok(h.status().handoff_requested_at, "exit intent kept");
    // The clear completes; the saved exit then fires once in the new session (no ACK).
    await boot(h, "pm-cleared", "clear");
    await h.hook("Stop", { session_id: "pm-cleared" });
    await h.settle();
    assert.equal(h.exits(), 1);
    assert.equal(h.clears(), 1);
  } finally { h.close(); }
});

test("obligation custody: accepted upsert with lost ACK, then unknown lookup -> no reissue; exact completed row adopted", async () => {
  const h = harness();
  try {
    await boot(h);
    let upserts = 0;
    let lookupUnknown = true;
    const lossy: HandoffObligationOps = {
      upsert: async (key, evidence) => { upserts++; await h.exitOps.upsert(key, evidence); return null; }, // accepted, ACK lost
      resolveById: h.exitOps.resolveById,
      lookup: (key) => lookupUnknown ? { status: "unknown", error: "ledger busy" } : h.exitOps.lookup(key),
    };
    h.processor.pmExitHandoffOps = lossy;
    assert.equal((await h.post(ctx({ enabled: true, pm_only: true }))).status, 200);
    lookupUnknown = false;
    // First ensure proves absence and writes once; the id is lost.
    assert.equal(await ensurePmExitHandoffObligation(h.db, lossy), null);
    assert.equal(h.db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_STATE_KEY), "uncertain");
    assert.equal(h.rows().length, 1, "the write was accepted");
    // Unknown lookup: never treated as absence, never reissued (also at a waiting Stop).
    lookupUnknown = true;
    assert.equal(await ensurePmExitHandoffObligation(h.db, lossy), null);
    await h.hook("Stop");
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(upserts, 1);
    assert.equal(h.rows().length, 1);
    // The PM completes that exact row; the next ensure adopts it (completed) instead of replacing it.
    const row = h.rows()[0]!;
    assert.equal(row.target_id, exitHandoffRequestKey(h.status().handoff_requested_at!));
    await h.exitOps.resolveById(String(row.id), "PM saved handoff");
    lookupUnknown = false;
    assert.equal(await ensurePmExitHandoffObligation(h.db, lossy), String(row.id));
    assert.equal(h.db.getConfig(PM_EXIT_HANDOFF_OBLIGATION_KEY), String(row.id));
    assert.equal(upserts, 1);
    assert.equal(h.rows().length, 1);
    // The adopted completed row is this request's explicit-resolve save evidence.
    await h.hook("Stop");
    assert.equal(h.exits(), 1);
  } finally { h.close(); }
});
