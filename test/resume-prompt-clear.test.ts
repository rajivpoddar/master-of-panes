import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { DEFAULT_PM_CONTINUATION_PROMPT, HookProcessor } from "../src/hooks.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { findSessionStartClearEvent, waitForSessionStartClear } from "../src/sessionStartClearWait.js";

function withDb<T>(fn: (db: MoPDatabase) => T | Promise<T>): Promise<T> {
  const directory = mkdtempSync(join(tmpdir(), "mop-resume-prompt-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  return Promise.resolve(fn(db)).finally(() => {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  });
}

function fakeRelay(injected: string[]) {
  return {
    injectToPM: (message: string) => {
      injected.push(message);
      return true;
    },
  } as unknown as TmuxRelay;
}

// ─── db persistence: setResumePrompt / hasResumePrompt / claimResumePrompt ─

test("resume_prompt persists across a simulated clear-and-reconnect (new MoPDatabase handle on the same file)", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-resume-prompt-persist-"));
  try {
    const dbPath = join(directory, "mop.db");
    const first = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
    first.setResumePrompt(0, "continue reviewing PR #123");
    assert.equal(first.hasResumePrompt(0), true);
    first.close();

    // Simulate the clear-and-reconnect cycle: a fresh handle onto the same
    // durable file (this repo's requirement is survival across that cycle,
    // not process-restart survival).
    const reopened = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
    assert.equal(reopened.hasResumePrompt(0), true);
    assert.equal(reopened.claimResumePrompt(0), "continue reviewing PR #123");
    reopened.close();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("claimResumePrompt is consume-once: a second claim returns null and does not replay", () =>
  withDb((db) => {
    db.setResumePrompt(0, "resume: finish the migration");
    assert.equal(db.claimResumePrompt(0), "resume: finish the migration");
    assert.equal(db.claimResumePrompt(0), null);
    assert.equal(db.hasResumePrompt(0), false);
  }));

test("claimResumePrompt returns null when nothing was ever persisted", () =>
  withDb((db) => {
    assert.equal(db.claimResumePrompt(0), null);
    assert.equal(db.hasResumePrompt(0), false);
  }));

// ─── SessionStart source=clear injection (hooks.ts), consume-once ────────

test("SessionStart source=clear for slot 0 injects a persisted resume_prompt via relay.injectToPM and claims it", () =>
  withDb(async (db) => {
    db.setPendingClear(0);
    db.setResumePrompt(0, "resume: re-run the failing E2E spec");
    const injected: string[] = [];
    const processor = new HookProcessor(db, fakeRelay(injected));

    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-2" });

    assert.deepEqual(injected, ["resume: re-run the failing E2E spec"]);
    assert.equal(db.hasResumePrompt(0), false);
    assert.equal(db.hasPendingClear(0), false);
    const injectedEvents = db.getEvents(0, 5, "resume_prompt_injected");
    assert.equal(injectedEvents.length, 1);
    assert.match(injectedEvents[0].payload, /"chars":35/);
  }));

test("SessionStart source=clear consume-once: a later SessionStart source=clear never replays an already-injected prompt", () =>
  withDb(async (db) => {
    db.setPendingClear(0);
    db.setResumePrompt(0, "resume once only");
    const injected: string[] = [];
    const processor = new HookProcessor(db, fakeRelay(injected));

    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-a" });
    assert.deepEqual(injected, ["resume once only"]);

    // A later, unrelated /clear cycle (no new resume_prompt persisted) must
    // not resend the prior prompt.
    db.setPendingClear(0);
    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-b" });
    assert.deepEqual(injected, ["resume once only", DEFAULT_PM_CONTINUATION_PROMPT]);
    assert.equal(db.getEvents(0, 10, "resume_prompt_injected").length, 1);
  }));

test("SessionStart source=clear for slot 0 with no persisted resume_prompt injects the PM continuation prompt once (Rajiv 2026-10-04)", () =>
  withDb(async (db) => {
    db.setPendingClear(0);
    const injected: string[] = [];
    const processor = new HookProcessor(db, fakeRelay(injected));

    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-3" });

    assert.deepEqual(injected, [DEFAULT_PM_CONTINUATION_PROMPT]);
    assert.match(injected[0], /pm-continuation-note\.md/);
    assert.equal(db.getEvents(0, 5, "resume_prompt_injected").length, 0);
    assert.equal(db.getEvents(0, 5, "pm_continuation_injected").length, 1);
    // Existing clear-ack behavior is untouched.
    assert.equal(db.hasPendingClear(0), false);
    assert.equal(db.getEvents(0, 5, "clear_pending_executed").length, 1);
    assert.equal(db.getEvents(0, 5, "slot_cleared").length, 1);

    // Duplicate hook delivery for the same cleared session: no re-send.
    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-3" });
    assert.equal(injected.length, 1);

    // A later /clear (new session) gets its own continuation.
    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-4" });
    assert.equal(injected.length, 2);
  }));

test("an explicit resume_prompt replaces the default PM continuation (no double injection)", () =>
  withDb(async (db) => {
    db.setResumePrompt(0, "explicit resume");
    const injected: string[] = [];
    const processor = new HookProcessor(db, fakeRelay(injected));
    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-5" });
    assert.deepEqual(injected, ["explicit resume"]);
  }));

test("a dev slot SessionStart(source=clear) never injects the PM continuation", () =>
  withDb(async (db) => {
    const injected: string[] = [];
    const processor = new HookProcessor(db, fakeRelay(injected));
    await processor.process(2, { type: "SessionStart", source: "clear", session_id: "slot-2" });
    assert.deepEqual(injected, []);
  }));

test("a resume_prompt persisted for slot 0 is not injected by an unrelated slot's SessionStart(source=clear)", () =>
  withDb(async (db) => {
    db.setResumePrompt(0, "pm-only prompt");
    const injected: string[] = [];
    const processor = new HookProcessor(db, fakeRelay(injected));

    // Dev slot 1 also gets a /clear (e.g. mop-assign-slot new_issue path).
    await processor.process(1, { type: "SessionStart", source: "clear", session_id: "slot-1" });

    assert.deepEqual(injected, []);
    assert.equal(db.hasResumePrompt(0), true);
  }));

test("SessionStart source=compact for slot 0 is unaffected by a pending resume_prompt (no injection, existing compact behavior intact)", () =>
  withDb(async (db) => {
    db.setResumePrompt(0, "should not fire on compact");
    const injected: string[] = [];
    const processor = new HookProcessor(db, fakeRelay(injected));

    await processor.process(0, { type: "SessionStart", source: "compact", session_id: "pm-4" });

    assert.deepEqual(injected, []);
    assert.equal(db.hasResumePrompt(0), true);
    assert.equal(db.getEvents(0, 5, "session_start_compact").length, 1);
  }));

test("SessionStart source=compact for a dev slot injects nothing (continue-after-compaction removed, Rajiv 2026-10-01)", () =>
  withDb(async (db) => {
    const sent: string[] = [];
    const relay = {
      injectToPM: (m: string) => { sent.push(`pm:${m}`); return true; },
      sendToSlot: (s: number, m: string) => { sent.push(`${s}:${m}`); return true; },
      sendToSlotAsync: async (s: number, m: string) => { sent.push(`${s}:${m}`); return true; },
    } as unknown as TmuxRelay;
    const processor = new HookProcessor(db, relay);

    await processor.process(1, { type: "SessionStart", source: "compact", session_id: "slot-1-c" });

    assert.deepEqual(sent, []);
    assert.equal(db.getEvents(1, 5, "session_start_compact").length, 1);
  }));

// ─── bounded fallback poll: SessionStart never arrives ───────────────────

test("bounded fallback poll: if no SessionStart(source=clear) event ever lands, waitForSessionStartClear reports ready=false (fallback then claims+injects directly in server.ts)", () =>
  withDb(async (db) => {
    db.setResumePrompt(0, "fallback should catch this");
    const baseline = db.getEvents(0, 1, "SessionStart")[0]?.id ?? 0;

    let now = 0;
    const wait = async (ms: number) => {
      now += ms;
    };
    const result = await waitForSessionStartClear({
      getEvent: () => findSessionStartClearEvent(db, 0, baseline),
      wait,
      nowMs: () => now,
      timeoutMs: 5_000,
      pollMs: 1_000,
    });

    assert.equal(result.ready, false);
    assert.equal(result.waitedMs, 5_000);
    // The prompt is still claimable — this is exactly what the server.ts
    // fallback (armResumePromptFallback) uses to inject directly on timeout.
    assert.equal(db.claimResumePrompt(0), "fallback should catch this");
  }));

test("bounded fallback poll: an event that lands before the deadline is picked up (no fallback injection needed)", () =>
  withDb(async (db) => {
    db.setResumePrompt(0, "hook wins the race");
    const baseline = db.getEvents(0, 1, "SessionStart")[0]?.id ?? 0;

    let now = 0;
    const wait = async (ms: number) => {
      now += ms;
      if (now >= 2_000) {
        // Simulate the SessionStart hook arriving mid-poll and the hook path
        // claiming the prompt itself (as hooks.ts does).
        db.logEvent(0, "SessionStart", "SessionStart", null, { type: "SessionStart", source: "clear" });
      }
    };
    const result = await waitForSessionStartClear({
      getEvent: () => findSessionStartClearEvent(db, 0, baseline),
      wait,
      nowMs: () => now,
      timeoutMs: 10_000,
      pollMs: 1_000,
    });

    assert.equal(result.ready, true);
  }));
