import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function makeDb() {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-resume-prompt-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  return { db, directory };
}

test("SessionStart:clear injects a persisted resume prompt via submitToPM and clears the config", async () => {
  const { db, directory } = makeDb();
  try {
    db.setPendingClear(0);
    db.setConfig("pm_clear_resume_prompt", "resume: obligation 123 is open, PR #456 awaiting CI admission");
    db.setConfig("pm_clear_resume_prompt_requested_at", new Date().toISOString());

    const submitted: string[] = [];
    const relay = {
      async submitToPM(message: string) {
        submitted.push(message);
        return { ok: true, submitKey: "Enter", ambiguous: false };
      },
    } as unknown as TmuxRelay;
    const processor = new HookProcessor(db, relay);

    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-fresh" });

    assert.deepEqual(submitted, ["resume: obligation 123 is open, PR #456 awaiting CI admission"]);
    assert.equal(db.getConfig("pm_clear_resume_prompt"), "");
    const injected = db.getEvents(0, 10, "resume_prompt_injected");
    assert.equal(injected.length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("SessionStart:clear with no persisted resume prompt is unchanged (no injection, no new event)", async () => {
  const { db, directory } = makeDb();
  try {
    db.setPendingClear(0);

    const submitted: string[] = [];
    const relay = {
      async submitToPM(message: string) {
        submitted.push(message);
        return { ok: true, submitKey: "Enter", ambiguous: false };
      },
    } as unknown as TmuxRelay;
    const processor = new HookProcessor(db, relay);

    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-fresh" });

    assert.deepEqual(submitted, []);
    assert.equal(db.getEvents(0, 10, "resume_prompt_injected").length, 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a failed injection retains the resume prompt for retry instead of losing it", async () => {
  const { db, directory } = makeDb();
  try {
    db.setPendingClear(0);
    db.setConfig("pm_clear_resume_prompt", "resume: keep me");
    db.setConfig("pm_clear_resume_prompt_requested_at", new Date().toISOString());

    const relay = {
      async submitToPM() {
        return { ok: false, submitKey: "Enter", ambiguous: false };
      },
    } as unknown as TmuxRelay;
    const processor = new HookProcessor(db, relay);

    await processor.process(0, { type: "SessionStart", source: "clear", session_id: "pm-fresh" });

    assert.equal(db.getConfig("pm_clear_resume_prompt"), "resume: keep me");
    assert.equal(db.getEvents(0, 10, "resume_prompt_injection_failed").length, 1);

    // A later retry (e.g. the bounded fallback poller, or the direct helper)
    // succeeds and consumes the retained prompt exactly once.
    const relay2 = {
      async submitToPM(message: string) {
        return { ok: true, submitKey: "Enter", ambiguous: false, delivered: message };
      },
    } as unknown as TmuxRelay;
    const processor2 = new HookProcessor(db, relay2);
    const ok = await processor2.injectPendingResumePromptIfAny();
    assert.equal(ok, true);
    assert.equal(db.getConfig("pm_clear_resume_prompt"), "");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
