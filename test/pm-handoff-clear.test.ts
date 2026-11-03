// Rajiv, DM D0AMF0XE6TS thread 1793731816.594829, ts 1793732417.459339:
// "add the obligation to save memory and then automatically inject clear at next stop".
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { isPmClearUndelivered, requestPmClearOnce, retryDeferredPmClearOnStop } from "../src/pmClearLatch.js";
import {
  armPmClearAfterSavedHandoff,
  failOpenUnobservedPmClear,
  handoffFilesSaved,
  isPmHandoffPending,
  requestPmHandoffClear,
  type HandoffObligationOps,
} from "../src/pmHandoffClear.js";
import { registerPMSoftBoundary } from "../src/pmSoftBoundary.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function fixture() {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-handoff-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
  const files = [join(directory, "MEMORY-session-handoff-current.md"), join(directory, "pm-continuation-note.md")];
  const past = new Date(Date.now() - 3_600_000);
  for (const file of files) { writeFileSync(file, "old\n"); utimesSync(file, past, past); }
  const calls = { upserts: [] as Record<string, unknown>[], resolves: [] as string[], explicit: false };
  const ops: HandoffObligationOps = {
    upsert: async (evidence) => { calls.upserts.push(evidence); return "4242"; },
    resolve: async (reason) => { calls.resolves.push(reason); return true; },
    isExplicitlyResolved: () => calls.explicit,
  };
  const save = (file: string) => { const future = new Date(Date.now() + 5_000); writeFileSync(file, "new\n"); utimesSync(file, future, future); };
  return { db, files, ops, calls, save, close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); } };
}

function fakeRelay(): TmuxRelay {
  return {
    setPMMaintenanceGate() {}, setPMBusy() { return { drained: 0 }; },
    isPMBusy: () => false, isPMComposerQueueEmpty: async () => true,
  } as unknown as TmuxRelay;
}

/** Wire the real soft-boundary Stop middleware with the saved-handoff arm and a counting Stop sender. */
function stopHarness(f: ReturnType<typeof fixture>) {
  let generation = 0;
  const sent: number[] = [];
  const app = new Hono();
  registerPMSoftBoundary(app, f.db, fakeRelay(), () => {
    const stopGeneration = ++generation;
    return retryDeferredPmClearOnStop({
      db: f.db, source: "pm_native_stop", nowMs: Date.now(), staleAfterMs: 0, recentSuppressMs: 0,
      hasRecentClearEvent: false, laterLifecycleEvent: null, backoffMs: 0, settleMs: 0,
      wait: async () => { throw new Error("no idle timer"); }, isCurrent: () => generation === stopGeneration,
      isIdleProven: () => true, send: async () => { sent.push(stopGeneration); return { success: true }; },
    });
  }, async (sessionId) => {
    await armPmClearAfterSavedHandoff(f.db, { sessionId, ops: f.ops, files: f.files, requestClear: () => requestPmClearOnce({
      db: f.db, source: "pm_handoff_saved_stop", nowMs: Date.now(), staleAfterMs: 0, recentSuppressMs: 0,
      hasRecentClearEvent: false, laterLifecycleEvent: null, deferUntilStop: true, isPMBusy: () => false,
      send: async () => { throw new Error("arm must never send"); } }) });
  });
  app.post("/hooks/slot/0", (c) => c.json({}));
  const hook = (event: string, extra: Record<string, unknown> = {}) => app.request("/hooks/slot/0", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hook_event_name: event, session_id: "pm", ...extra }) });
  return { hook, sent, bumpGeneration: () => { generation++; } };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

test("a due clear opens ONE pm_save_handoff obligation and injects nothing before the save", async () => {
  const f = fixture();
  try {
    const first = await requestPmHandoffClear(f.db, { source: "http_clear_endpoint", sessionId: "pm", nowMs: Date.now(), automatic: false, ops: f.ops });
    assert.equal(first.kind, "awaiting_handoff");
    const again = await requestPmHandoffClear(f.db, { source: "session_age_policy", sessionId: "pm", nowMs: Date.now(), automatic: true, ops: f.ops });
    assert.equal(again.kind, "already_awaiting");
    assert.equal(f.calls.upserts.length, 1, "exactly one obligation upsert");
    assert.equal(f.calls.upserts[0]!.pm_stop_actionable, 1);
    assert.equal(f.db.hasPendingClear(0), false, "no clear latch before the save");

    const h = stopHarness(f);
    await h.hook("Stop");
    await settle();
    assert.equal(h.sent.length, 0, "Stop before the save injects nothing");
    assert.equal(isPmHandoffPending(f.db), true);
    assert.equal(f.db.hasPendingClear(0), false);
  } finally { f.close(); }
});

test("the obligation resolves only when BOTH handoff mtimes advance past the request", async () => {
  const f = fixture();
  try {
    const requested = await requestPmHandoffClear(f.db, { source: "op", sessionId: "pm", nowMs: Date.now(), automatic: false, ops: f.ops });
    const requestedMs = Date.parse((requested as { requestedAt: string }).requestedAt);
    assert.equal(handoffFilesSaved(requestedMs, f.files), false);
    f.save(f.files[0]!);
    assert.equal(handoffFilesSaved(requestedMs, f.files), false, "one file is not enough");
    const h = stopHarness(f);
    await h.hook("Stop");
    await settle();
    assert.equal(h.sent.length, 0);
    assert.equal(f.calls.resolves.length, 0);
    f.save(f.files[1]!);
    assert.equal(handoffFilesSaved(requestedMs, f.files), true);
    await h.hook("Stop");
    await settle();
    assert.equal(f.calls.resolves.length, 1, "MoP resolves the obligation once both files are saved");
    assert.match(f.calls.resolves[0]!, /handoff files saved/);
    assert.equal(isPmHandoffPending(f.db), false);
  } finally { f.close(); }
});

test("an explicit resolve also counts as saved", async () => {
  const f = fixture();
  try {
    await requestPmHandoffClear(f.db, { source: "op", sessionId: "pm", nowMs: Date.now(), automatic: false, ops: f.ops });
    f.calls.explicit = true;
    const { outcome } = await armPmClearAfterSavedHandoff(f.db, { sessionId: "pm", ops: f.ops, files: f.files,
      requestClear: async () => ({ kind: "deferred_busy" }) });
    assert.deepEqual(outcome.kind, "ready");
    assert.equal((outcome as { via: string }).via, "explicit_resolve");
  } finally { f.close(); }
});

test("after the save, the NEXT PM Stop injects /clear immediately (no idle timer)", async () => {
  const f = fixture();
  try {
    await requestPmHandoffClear(f.db, { source: "op", sessionId: "pm", nowMs: Date.now(), automatic: false, ops: f.ops });
    f.save(f.files[0]!); f.save(f.files[1]!);
    const h = stopHarness(f);
    await h.hook("Stop");
    await settle();
    assert.equal(h.sent.length, 1, "this Stop sent exactly one /clear");
    assert.equal(f.db.getConfig("pm_clear_delivery_state"), "awaiting_ack");
    await h.hook("Stop");
    await settle();
    assert.equal(h.sent.length, 1, "a later Stop never resends");
  } finally { f.close(); }
});

test("new input cancels the injection; the next Stop delivers", async () => {
  const f = fixture();
  try {
    f.db.setPendingClear(0);
    f.db.setConfig("pm_clear_requested_at", new Date().toISOString());
    f.db.setConfig("pm_clear_delivery_state", "deferred_busy");
    let sends = 0;
    const cancelled = await retryDeferredPmClearOnStop({
      db: f.db, source: "pm_native_stop", nowMs: Date.now(), staleAfterMs: 0, recentSuppressMs: 0,
      hasRecentClearEvent: false, laterLifecycleEvent: null, backoffMs: 0, settleMs: 0, wait: async () => {},
      isCurrent: () => false, // a UserPromptSubmit/PreToolUse bumped the generation
      isIdleProven: () => true, send: async () => { sends++; return { success: true }; },
    });
    assert.equal(cancelled.kind, "skipped");
    assert.equal(sends, 0, "new input cancels the write");
    assert.equal(isPmClearUndelivered(f.db), true, "intent kept for the next Stop");

    // Session change cancels a not-yet-armed handoff entirely.
    f.db.clearPendingClear(0);
    await requestPmHandoffClear(f.db, { source: "op", sessionId: "pm", nowMs: Date.now(), automatic: false, ops: f.ops });
    f.save(f.files[0]!); f.save(f.files[1]!);
    const { outcome } = await armPmClearAfterSavedHandoff(f.db, { sessionId: "pm-new", ops: f.ops, files: f.files,
      requestClear: async () => { throw new Error("must not arm after a session change"); } });
    assert.equal(outcome.kind, "cancelled_session_change");
    assert.equal(isPmHandoffPending(f.db), false);
  } finally { f.close(); }
});

test("a written clear never acknowledged fails open after ONE attempt and is not re-requested", async () => {
  const f = fixture();
  try {
    const attempt = Date.now() - 10 * 60_000;
    f.db.setPendingClear(0);
    f.db.setConfig("pm_clear_requested_at", new Date(attempt - 1_000).toISOString());
    f.db.setConfig("pm_clear_stop_retry_at", new Date(attempt).toISOString());
    f.db.setConfig("pm_clear_delivery_state", "awaiting_ack");

    // Inside the bound: still pending (the Stop right after /clear can precede SessionStart:clear).
    assert.equal(failOpenUnobservedPmClear(f.db, { nowMs: attempt + 30_000, sessionId: "pm" }), false);
    assert.equal(f.db.hasPendingClear(0), true);

    let sends = 0;
    const relay = { sendToSlot() { sends++; return true; } } as unknown as TmuxRelay;
    await new HookProcessor(f.db, relay).process(0, { type: "Stop", session_id: "pm" });
    assert.equal(f.db.hasPendingClear(0), false, "pending flag cleared");
    assert.equal(f.db.getConfig("pm_clear_delivery_state"), "failed_open");
    assert.equal(f.db.getEvents(0, 10, "pm_clear_unobserved_fail_open").length, 1, "audit row written");
    assert.equal(sends, 0, "fail-open never resends");

    const again = await requestPmHandoffClear(f.db, { source: "session_age_policy", sessionId: "pm", nowMs: Date.now(), automatic: true, ops: f.ops });
    assert.equal(again.kind, "refused_failopen", "no automatic re-request for the same session");
    assert.equal(f.calls.upserts.length, 0);
  } finally { f.close(); }
});

test("rule 50: no UserPromptSubmit change and no input gate in the handoff path", () => {
  const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  const handoff = read("../src/pmHandoffClear.ts");
  const code = handoff.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  assert.doesNotMatch(code, /UserPromptSubmit/);
  assert.doesNotMatch(code, /permissionDecision|"deny"|setPMBusy|isAdmissionFenced|maintenance/i);
  for (const hook of [
    "../scripts/pm/shared-assets/claude/hooks/pm-self-clear-stop.sh",
    "../scripts/pm/shared-assets/claude/hooks/pm-ops-sync-stop-validator.sh",
    "../scripts/pm/shared-assets/claude/scripts/heartbeat-session-age-clear.py",
  ]) {
    assert.doesNotMatch(read(hook), /UserPromptSubmit/, `${hook} must not touch UserPromptSubmit`);
  }
  // The legacy self-clear Stop consumer can no longer emit a block.
  assert.doesNotMatch(read("../scripts/pm/shared-assets/claude/hooks/pm-self-clear-stop.sh"), /"decision": "block"/);
  // The PM UserPromptSubmit relay stays exactly the existing MoP hook-relay entry.
  const settingsPath = "../scripts/pm/shared-assets/claude/settings.json";
  try {
    const settings = JSON.parse(read(settingsPath));
    const ups = JSON.stringify(settings.hooks?.UserPromptSubmit ?? []);
    assert.doesNotMatch(ups, /handoff|save_handoff|pm-self-clear/);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
});
