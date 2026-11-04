// Rajiv, DM D0AMF0XE6TS thread 1793731816.594829, ts 1793732417.459339:
// "add the obligation to save memory and then automatically inject clear at next stop".
// CTO REVISE (C0ALZJHGE49 thread 1793736095.019189, ts 1793736515.197999): bind completion
// and ID publication to the original request, pin Stop authority before the first await,
// and make failed/lost-ACK obligation creation recoverable. Proof packet (a)(b)(c) below.
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { promisify } from "node:util";
import Database from "better-sqlite3";
import { Hono } from "hono";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { isPmClearUndelivered, makePmClearWriteFence, requestPmClearOnce, retryDeferredPmClearOnStop } from "../src/pmClearLatch.js";
import {
  armPmClearAfterSavedHandoff,
  failOpenUnobservedPmClear,
  handoffFilesSaved,
  handoffRequestKey,
  isPmHandoffPending,
  pmOpsHandoffObligationOps,
  reconcilePmHandoffObligation,
  requestPmHandoffClear,
  PM_HANDOFF_MAX_RECONCILE_ATTEMPTS,
  PM_HANDOFF_OBLIGATION_KEY,
  PM_HANDOFF_REQUESTED_AT_KEY,
  PM_HANDOFF_SESSION_KEY,
  type HandoffObligationOps,
} from "../src/pmHandoffClear.js";
import { registerPMSoftBoundary, type PMStopAuthority, type PMStopAuthorityPins } from "../src/pmSoftBoundary.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const PM_OPS_LEGACY = new URL("../scripts/pm/shared-assets/claude/scripts/pm-ops-legacy.py", import.meta.url).pathname;
const run = promisify(execFile);

/** Bounded poll for a ledger effect done by a python subprocess (fixed sleeps flaked under suite load). */
async function until(done: () => boolean, maxMs = 5_000): Promise<void> {
  for (let waited = 0; waited < maxMs && !done(); waited += 100) await new Promise((r) => setTimeout(r, 100));
}

function gate() {
  let open!: () => void;
  const promise = new Promise<void>((resolve) => { open = resolve; });
  return { promise, open };
}

/** MoP DB + handoff files + an ISOLATED real pm-ops ledger (pm-ops-legacy.py, tmp PM_OPS_DB). */
function fixture() {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-handoff-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
  const files = [join(directory, "MEMORY-session-handoff-current.md"), join(directory, "pm-continuation-note.md")];
  const past = new Date(Date.now() - 3_600_000);
  for (const file of files) { writeFileSync(file, "old\n"); utimesSync(file, past, past); }
  const ledger = join(directory, "pm-ops.db");
  // Real schema via the canonical writer, ids seeded to production-like values
  // (pm-ops-legacy exits with the row id, so id 2 would read as its refusal code 2).
  execFileSync("python3", [PM_OPS_LEGACY, "init"], { env: { ...process.env, PM_OPS_DB: ledger, HOME: directory } });
  { const con = new Database(ledger); con.prepare("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('obligations', 29000)").run(); con.close(); }
  const execute = (file: string, args: string[], options: { timeout: number }) =>
    run(file, args, { ...options, env: { ...process.env, PM_OPS_DB: ledger, HOME: directory } });
  const ops = pmOpsHandoffObligationOps(execute, ledger, PM_OPS_LEGACY);
  const rows = () => {
    try {
      const con = new Database(ledger, { readonly: true });
      try {
        return con.prepare("SELECT id, status, target_id FROM obligations WHERE kind='pm_save_handoff' ORDER BY id").all() as
          { id: number; status: string; target_id: string }[];
      } finally { con.close(); }
    } catch { return []; }
  };
  const save = (file: string) => { const future = new Date(Date.now() + 5_000); writeFileSync(file, "new\n"); utimesSync(file, future, future); };
  const saveAll = () => files.forEach(save);
  return { db, files, ops, ledger, rows, save, saveAll, directory,
    close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); } };
}
type Fixture = ReturnType<typeof fixture>;

const request = (f: Fixture, ops: HandoffObligationOps = f.ops, source = "op", nowMs = Date.now()) =>
  requestPmHandoffClear(f.db, { source, sessionId: "pm", nowMs, automatic: source.startsWith("session_age"), ops });

const armLatch = (f: Fixture) => () => requestPmClearOnce({
  db: f.db, source: "pm_handoff_saved_stop", nowMs: Date.now(), staleAfterMs: 0, recentSuppressMs: 0,
  hasRecentClearEvent: false, laterLifecycleEvent: null, deferUntilStop: true, isPMBusy: () => false,
  send: async () => { throw new Error("arm must never send"); } });

/**
 * Real Hono + soft-boundary middleware + real TmuxRelay guarded PM clear writer
 * (writePMClearFenced + makePmClearWriteFence), pinned Stop authority, and an
 * optional barrier inside beforeClearStop.
 */
function stopHarness(f: Fixture, barrier?: () => Promise<void>, ops: HandoffObligationOps = f.ops) {
  let generation = 0;
  const commands: string[] = [];
  let composer = "", buffer = "";
  const sessionToken = () => String(f.db.getEvents(0, 1, "SessionStart")[0]?.id ?? 0);
  const pins: PMStopAuthorityPins = {
    snapshot: () => ({ generation, sessionToken: sessionToken() }),
    claim: (entry, sessionId) => entry.generation === generation && entry.sessionToken === sessionToken()
      ? { generation: entry.generation, sessionToken: entry.sessionToken, sessionId } : null,
    isCurrent: (a) => a.generation === generation && a.sessionToken === sessionToken(),
  };
  const relay = new TmuxRelay(DEFAULT_CONFIG, { pmRuntime: "claude", runShell: async (command: string) => {
    commands.push(command);
    if (command.includes("#{pane_id}|#{pane_current_command}")) return { stdout: "%1|claude\n", stderr: "" };
    if (command.includes("#{pane_pid}")) return { stdout: "321\n", stderr: "" };
    if (command.startsWith("ps -p 321")) return { stdout: "/opt/claude", stderr: "" };
    if (command.startsWith("tmux capture-pane")) return { stdout: `${"─".repeat(40)}\n❯ ${composer}\n${"─".repeat(40)}`, stderr: "" };
    const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(command);
    if (load) buffer = readFileSync(load[1]!, "utf8");
    if (command.startsWith("tmux paste-buffer")) composer = buffer;
    if (command.endsWith(" C-u") || command.endsWith(" Enter")) composer = "";
    return { stdout: "", stderr: "" };
  } });
  relay.setDatabase(f.db);
  const deliveries: Promise<unknown>[] = [];
  const app = new Hono();
  const boundary = registerPMSoftBoundary(app, f.db, relay, (authority?: PMStopAuthority) => {
    assert.ok(authority, "clear delivery always carries the pinned Stop authority");
    const delivery = retryDeferredPmClearOnStop({
      db: f.db, source: "pm_native_stop", nowMs: Date.now(), staleAfterMs: 0, recentSuppressMs: 0,
      hasRecentClearEvent: false, laterLifecycleEvent: null, backoffMs: 0, settleMs: 0,
      wait: async () => { throw new Error("no idle timer"); }, isCurrent: () => pins.isCurrent(authority!),
      isIdleProven: () => true,
      send: async ({ requestedAt }) => {
        const written = await boundary.withMaintenanceWriter(() => relay.writePMClearFenced({ paneId: "%1",
          check: makePmClearWriteFence({ db: f.db, requestedAt, generation: authority!.generation,
            sessionToken: authority!.sessionToken, currentGeneration: () => generation,
            currentSessionToken: sessionToken, idleProven: () => true }) }));
        return { success: written.ok, busy: written.busy, zeroEffect: written.zeroEffect };
      },
    }).finally(() => boundary.reconcileFailedMaintenance());
    deliveries.push(delivery);
    return delivery;
  }, async (sessionId, authority) => {
    if (barrier) await barrier();
    await armPmClearAfterSavedHandoff(f.db, { sessionId, ops, files: f.files,
      isCurrent: () => !!authority && pins.isCurrent(authority), requestClear: armLatch(f) });
  }, pins);
  app.post("/hooks/slot/0", async (c) => {
    const raw = await c.req.json();
    if (["UserPromptSubmit", "PreToolUse", "SessionStart"].includes(raw.hook_event_name)) generation++;
    if (raw.hook_event_name === "SessionStart") f.db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: raw.session_id });
    return c.json({});
  });
  const hook = (event: string, extra: Record<string, unknown> = {}) => app.request("/hooks/slot/0", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hook_event_name: event, session_id: "pm", mop_tool_tracking: 1, ...extra }) });
  const pastes = () => commands.filter((c) => c.startsWith("tmux paste-buffer")).length;
  const enters = () => commands.filter((c) => c.endsWith(" Enter")).length;
  return { hook, pastes, enters, settle: async () => { await Promise.all(deliveries); await new Promise((r) => setTimeout(r, 20)); },
    close: () => relay.setPMBusy(true) };
}

// ── Existing controls (kept) ──────────────────────────────────────────────

test("control: a due clear opens ONE pm_save_handoff obligation and injects nothing before the save", async () => {
  const f = fixture();
  try {
    const first = await request(f);
    assert.equal(first.kind, "awaiting_handoff");
    assert.equal((await request(f, f.ops, "session_age_policy")).kind, "already_awaiting");
    assert.equal(f.rows().length, 1, "exactly one obligation row");
    assert.equal(f.rows()[0]!.target_id, handoffRequestKey((first as { requestedAt: string }).requestedAt));
    const h = stopHarness(f);
    await h.hook("Stop"); await h.settle();
    assert.equal(h.pastes(), 0, "Stop before the save injects nothing");
    assert.equal(f.db.hasPendingClear(0), false);
    h.close();
  } finally { f.close(); }
});

test("control: both mtimes must advance; the next Stop injects exactly one /clear and resolves its own row", async () => {
  const f = fixture();
  try {
    const a = await request(f) as { requestedAt: string };
    assert.equal(handoffFilesSaved(Date.parse(a.requestedAt), f.files), false);
    f.save(f.files[0]!);
    const h = stopHarness(f);
    await h.hook("Stop"); await h.settle();
    assert.equal(h.pastes(), 0, "one file is not enough");
    f.save(f.files[1]!);
    await h.hook("Stop"); await h.settle();
    assert.equal(h.pastes(), 1); assert.equal(h.enters(), 1);
    assert.equal(f.db.getConfig("pm_clear_delivery_state"), "awaiting_ack");
    await h.hook("Stop"); await h.settle();
    assert.equal(h.pastes(), 1, "ambiguous/possible-effect write is never resent");
    await until(() => f.rows()[0]?.status === "resolved");
    assert.equal(f.rows()[0]!.status, "resolved");
    h.close();
  } finally { f.close(); }
});

test("control: an explicit resolve of THIS request's row counts as saved", async () => {
  const f = fixture();
  try {
    await request(f);
    const id = f.db.getConfig(PM_HANDOFF_OBLIGATION_KEY)!;
    assert.ok(await f.ops.resolveById(id, "PM explicit resolve"));
    const { outcome } = await armPmClearAfterSavedHandoff(f.db, { sessionId: "pm", ops: f.ops, files: f.files, requestClear: armLatch(f) });
    assert.equal(outcome.kind, "ready");
    assert.equal(isPmClearUndelivered(f.db), true);
  } finally { f.close(); }
});

test("control: a written clear never acknowledged fails open after ONE attempt and is not re-requested", async () => {
  const f = fixture();
  try {
    const attempt = Date.now() - 10 * 60_000;
    f.db.setPendingClear(0);
    f.db.setConfig("pm_clear_requested_at", new Date(attempt - 1_000).toISOString());
    f.db.setConfig("pm_clear_stop_retry_at", new Date(attempt).toISOString());
    f.db.setConfig("pm_clear_delivery_state", "awaiting_ack");
    assert.equal(failOpenUnobservedPmClear(f.db, { nowMs: attempt + 30_000, sessionId: "pm" }), false);
    let sends = 0;
    const relay = { sendToSlot() { sends++; return true; } } as unknown as TmuxRelay;
    await new HookProcessor(f.db, relay).process(0, { type: "Stop", session_id: "pm" });
    assert.equal(f.db.hasPendingClear(0), false);
    assert.equal(f.db.getConfig("pm_clear_delivery_state"), "failed_open");
    assert.equal(f.db.getEvents(0, 10, "pm_clear_unobserved_fail_open").length, 1);
    assert.equal(sends, 0);
    assert.equal((await request(f, f.ops, "session_age_policy")).kind, "refused_failopen");
    assert.equal(f.rows().length, 0);
  } finally { f.close(); }
});

// ── (a) delayed A completion vs successor B ───────────────────────────────

test("(a) a delayed resolve of A never resolves successor B or authorizes B's unsaved clear", async () => {
  const f = fixture();
  try {
    const hold = gate();
    const delayed: HandoffObligationOps = { ...f.ops,
      resolveById: async (id, reason) => { await hold.promise; return f.ops.resolveById(id, reason); } };
    await request(f, delayed, "op", Date.now() - 60_000);
    const idA = f.db.getConfig(PM_HANDOFF_OBLIGATION_KEY)!;
    f.saveAll();
    const armed = await armPmClearAfterSavedHandoff(f.db, { sessionId: "pm", ops: delayed, files: f.files, requestClear: armLatch(f) });
    assert.equal(armed.outcome.kind, "ready");
    // A's clear is acknowledged; then B is requested while A's resolve is still in flight.
    f.db.clearPendingClear(0);
    for (const file of f.files) { const t = new Date(Date.now() - 30_000); utimesSync(file, t, t); }
    const b = await request(f);
    assert.equal(b.kind, "awaiting_handoff");
    const idB = f.db.getConfig(PM_HANDOFF_OBLIGATION_KEY)!;
    assert.notEqual(idB, idA, "B never reuses A's row");
    hold.open();
    // Poll (bounded) instead of a fixed 500ms: the resolve is a python subprocess
    // and flaked under full-suite load.
    await until(() => f.rows().find((r) => String(r.id) === idA)?.status === "resolved");
    const byId = Object.fromEntries(f.rows().map((r) => [String(r.id), r.status]));
    assert.equal(byId[idA], "resolved", "A's delayed completion resolved A only");
    assert.equal(byId[idB], "open", "no successor resolution");
    const later = await armPmClearAfterSavedHandoff(f.db, { sessionId: "pm", ops: f.ops, files: f.files, requestClear: armLatch(f) });
    assert.equal(later.outcome.kind, "waiting", "B is not authorized without its own save");
    assert.equal(f.db.hasPendingClear(0), false, "no unsaved clear");
  } finally { f.close(); }
});

test("(a) a stale returned ID for A is never published into successor B", async () => {
  const f = fixture();
  try {
    const hold = gate();
    const slowUpsert: HandoffObligationOps = { ...f.ops,
      upsert: async (key, evidence) => { await hold.promise; return f.ops.upsert(key, evidence); } };
    const pendingA = request(f, slowUpsert, "op", Date.now() - 60_000);
    // Operator re-request supersedes A while A's upsert is still in flight.
    for (const key of [PM_HANDOFF_REQUESTED_AT_KEY, PM_HANDOFF_SESSION_KEY]) f.db.setConfig(key, "");
    const b = await request(f) as { requestedAt: string; obligationId: string };
    hold.open();
    await pendingA;
    await until(() => f.rows().filter((r) => r.status === "open").length === 1
      && f.db.getEvents(0, 10, "pm_handoff_obligation_stale_id_dropped").length === 1);
    assert.equal(f.db.getConfig(PM_HANDOFF_OBLIGATION_KEY), b.obligationId, "B keeps its own id");
    assert.equal(f.db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY), b.requestedAt);
    assert.equal(f.db.getEvents(0, 10, "pm_handoff_obligation_stale_id_dropped").length, 1);
    const open = f.rows().filter((r) => r.status === "open");
    assert.deepEqual(open.map((r) => String(r.id)), [b.obligationId], "stale A row resolved; only B open");
  } finally { f.close(); }
});

// ── (b) failed and lost-ACK creation reconciliation ───────────────────────

test("(b) failed creation keeps requested_at and recovers by a keyed write proven absent", async () => {
  const f = fixture();
  try {
    const failing: HandoffObligationOps = { ...f.ops, upsert: async () => { throw new Error("pm-ops exit=1"); } };
    const a = await request(f, failing) as { requestedAt: string; obligationId: string | null };
    assert.equal(a.obligationId, null);
    assert.equal(f.rows().length, 0);
    assert.equal(f.db.getEvents(0, 10, "pm_handoff_obligation_uncertain").length, 1);
    assert.equal(await reconcilePmHandoffObligation(f.db, f.ops), "retried");
    assert.equal(f.db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY), a.requestedAt, "requested_at never reset");
    assert.equal(f.rows().length, 1);
    assert.equal(f.rows()[0]!.target_id, handoffRequestKey(a.requestedAt));
  } finally { f.close(); }
});

test("(b) lost-ACK creation is adopted by lookup, never blindly replayed", async () => {
  const f = fixture();
  try {
    let upserts = 0;
    const lostAck: HandoffObligationOps = { ...f.ops,
      upsert: async (key, evidence) => { upserts++; await f.ops.upsert(key, evidence); throw new Error("timeout after write"); } };
    const a = await request(f, lostAck) as { requestedAt: string };
    assert.equal(f.rows().length, 1, "the write did land");
    const spy: HandoffObligationOps = { ...f.ops, upsert: async () => { throw new Error("must not replay an uncertain write"); } };
    assert.equal(await reconcilePmHandoffObligation(f.db, spy), "adopted");
    assert.equal(upserts, 1);
    assert.equal(f.db.getConfig(PM_HANDOFF_OBLIGATION_KEY), String(f.rows()[0]!.id));
    assert.equal(f.db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY), a.requestedAt);
    assert.equal(f.rows().length, 1, "no duplicate row");
  } finally { f.close(); }
});

test("(b) an unreadable ledger never leaves a permanent phantom awaiting request", async () => {
  const f = fixture();
  try {
    const dark: HandoffObligationOps = { upsert: async () => null, resolveById: async () => false,
      lookup: () => ({ status: "unknown", error: "unable to open database file" }) };
    await request(f, dark);
    const h = stopHarness(f);
    const requestedAt = f.db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY);
    for (let i = 0; i < PM_HANDOFF_MAX_RECONCILE_ATTEMPTS; i++) {
      assert.equal(await reconcilePmHandoffObligation(f.db, dark), "unknown");
      assert.equal(f.db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY), requestedAt, "bounded retries keep requested_at");
    }
    assert.equal(await reconcilePmHandoffObligation(f.db, dark), "abandoned");
    assert.equal(isPmHandoffPending(f.db), false, "no permanent phantom request");
    assert.equal(f.db.getEvents(0, 10, "pm_handoff_request_abandoned").length, 1);
    await h.hook("Stop"); await h.settle();
    assert.equal(h.pastes(), 0, "abandonment never clears");
    assert.equal((await request(f)).kind, "awaiting_handoff", "a fresh request works after abandonment");
    h.close();
  } finally { f.close(); }
});

// ── single-flight creation (CTO review ts 1793737660.035519) ──────────────

test("single-flight: concurrent Stop, re-request and reconcile never start a second writer while the original upsert is active", async () => {
  const f = fixture();
  try {
    const hold = gate();
    let upserts = 0;
    let lookups = 0;
    const counted: HandoffObligationOps = {
      upsert: async (key, evidence) => {
        upserts++;
        if (upserts === 1) await hold.promise; // ORIGINAL write held before its DB write
        return f.ops.upsert(key, evidence);
      },
      resolveById: f.ops.resolveById,
      lookup: (key) => { lookups++; return f.ops.lookup(key); },
    };
    const original = request(f, counted);
    await new Promise((r) => setTimeout(r, 20));
    const requestedAt = f.db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY);
    assert.equal(f.rows().length, 0, "original write is held before the DB");

    const h = stopHarness(f, undefined, counted);
    const concurrent = await Promise.all([
      h.hook("Stop"), h.hook("Stop"),
      request(f, counted, "session_age_policy"), request(f, counted),
      reconcilePmHandoffObligation(f.db, counted), reconcilePmHandoffObligation(f.db, counted),
      ...Array.from({ length: PM_HANDOFF_MAX_RECONCILE_ATTEMPTS + 2 }, () => reconcilePmHandoffObligation(f.db, counted)),
    ]);
    await h.settle();
    assert.equal(upserts, 1, "no second writer while the original is active");
    assert.equal(lookups, 0, "an in-flight write is never judged by an empty lookup");
    assert.equal(f.db.getEvents(0, 10, "pm_handoff_request_abandoned").length, 0, "no abandonment while active");
    assert.equal(f.db.getConfig(PM_HANDOFF_REQUESTED_AT_KEY), requestedAt, "requested_at preserved");
    assert.equal((concurrent[2] as { kind: string }).kind, "already_awaiting");
    assert.equal(concurrent[4], "writer_active");
    assert.equal(h.pastes(), 0);

    hold.open();
    const settled = await original as { obligationId: string };
    await new Promise((r) => setTimeout(r, 50));
    assert.equal(f.rows().length, 1, "exactly one durable row after settlement");
    assert.equal(f.db.getConfig(PM_HANDOFF_OBLIGATION_KEY), settled.obligationId);
    assert.equal(await reconcilePmHandoffObligation(f.db, counted), "created");

    f.saveAll();
    await h.hook("Stop"); await h.settle();
    assert.equal(h.pastes(), 1, "the saved request clears once");
    await until(() => f.rows().filter((r) => r.status === "open").length === 0);
    assert.deepEqual(f.rows().filter((r) => r.status === "open"), [], "no stale open sibling after completion");
    h.close();
  } finally { f.close(); }
});

// ── (c) beforeClearStop barrier: new input, completed work, new boot ──────

for (const scenario of ["new-input", "completed-work", "new-boot"] as const) {
  test(`(c) a Stop held at the beforeClearStop barrier cannot adopt newer work: ${scenario}`, async () => {
    const f = fixture();
    try {
      await request(f);
      f.saveAll();
      const hold = gate();
      let barrierOn = true;
      const h = stopHarness(f, async () => { if (barrierOn) { barrierOn = false; await hold.promise; } });
      const oldStop = h.hook("Stop");
      await new Promise((r) => setTimeout(r, 20));
      if (scenario === "new-input") await h.hook("UserPromptSubmit");
      if (scenario === "completed-work") { await h.hook("UserPromptSubmit"); await h.hook("PreToolUse", { tool_use_id: "t1" }); await h.hook("PostToolUse", { tool_use_id: "t1" }); }
      if (scenario === "new-boot") await h.hook("SessionStart", { source: "startup", session_id: "pm-next" });
      hold.open();
      await oldStop; await h.settle();
      assert.equal(h.pastes(), 0, "no stale paste");
      assert.equal(h.enters(), 0, "no stale Enter");
      assert.equal(f.db.hasPendingClear(0), false, "the old Stop armed nothing");
      if (scenario === "new-boot") {
        await h.hook("Stop", { session_id: "pm-next" }); await h.settle();
        assert.equal(h.pastes(), 0, "a new session cancels the handoff, never clears");
        assert.equal(isPmHandoffPending(f.db), false);
      } else {
        assert.equal(isPmHandoffPending(f.db), true, "intent kept for the next current Stop");
        await h.hook("Stop"); await h.settle();
        assert.equal(h.pastes(), 1, "the next current Stop injects once");
        assert.equal(h.enters(), 1);
      }
      h.close();
    } finally { f.close(); }
  });
}

test("rule 50: no UserPromptSubmit change and no input gate in the handoff path", () => {
  const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  const code = read("../src/pmHandoffClear.ts").replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  assert.doesNotMatch(code, /UserPromptSubmit/);
  assert.doesNotMatch(code, /permissionDecision|"deny"|setPMBusy|isAdmissionFenced|maintenance/i);
  for (const hook of [
    "../scripts/pm/shared-assets/claude/hooks/pm-self-clear-stop.sh",
    "../scripts/pm/shared-assets/claude/hooks/pm-ops-sync-stop-validator.sh",
    "../scripts/pm/shared-assets/claude/scripts/heartbeat-session-age-clear.py",
  ]) {
    assert.doesNotMatch(read(hook), /UserPromptSubmit/, `${hook} must not touch UserPromptSubmit`);
  }
  assert.doesNotMatch(read("../scripts/pm/shared-assets/claude/hooks/pm-self-clear-stop.sh"), /"decision": "block"/);
  // Completion is by exact row id only; never by shared kind/target.
  assert.match(code, /"obligation-resolve", "--kind", PM_HANDOFF_OBLIGATION_KIND,\s*"--id", id/);
  assert.doesNotMatch(code, /obligation-resolve[^\n]*--target-id/);
});
