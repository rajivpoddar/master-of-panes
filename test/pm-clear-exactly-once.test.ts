import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

test("repeated PM Stop events never resend an already-latched clear", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-clear-once-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.setPendingClear(0);
    db.setConfig("pm_clear_requested_at", "2026-01-01T00:00:00.000Z");

    const sent: string[] = [];
    const relay = {
      sendToSlot(_slot: number, command: string) {
        sent.push(command);
        return true;
      },
    } as TmuxRelay;
    const processor = new HookProcessor(db, relay);

    await processor.process(0, { type: "Stop", session_id: "pm-turn" });
    await processor.process(0, { type: "Stop", session_id: "pm-turn" });

    assert.deepEqual(sent, []);
    assert.equal(db.hasPendingClear(0), true);
    const held = db.getEvents(0, 10, "clear_pending_duplicate_suppressed");
    assert.equal(held.length, 2);
    assert.ok(held.every((event) => event.payload.includes('"via":"hook_stop"')));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("ordinary PM Stop only finishes the PM session and never enters slot lifecycle", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-stop-observation-only-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.startAgentTurn(0, "pm-turn");
    const processor = new HookProcessor(db, {} as TmuxRelay);

    await processor.process(0, { type: "UserPromptSubmit", session_id: "pm-turn" });
    await processor.process(0, { type: "Stop", session_id: "pm-turn" });
    db.finishAgentTurn(0, "pm-turn");

    const slot = db.getSlot(0)!;
    assert.equal(slot.active_turn_id, null);
    assert.equal(slot.active_turn_state, "inactive");
    assert.equal(slot.idle, true);
    assert.equal(db.getEvents(0, 10, "pm_stop_observed").length, 1);
    for (const eventType of [
      "slot_idle_debounce_started",
      "slot_active_debounce_started",
      "slot_idle_notified",
      "slot_active_notified",
      "auto_released_post_pr",
    ]) {
      assert.equal(db.getEvents(0, 10, eventType).length, 0, eventType);
    }
    db.close();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("PM status Stop cannot contain a pending-clear resend path", () => {
  const source = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const hooks = readFileSync(new URL("../src/hooks.ts", import.meta.url), "utf8");
  const start = source.indexOf('app.post("/pm-status"');
  const end = source.indexOf('app.get("/pm-status"', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);

  const route = source.slice(start, end);
  assert.doesNotMatch(route, /sendClearViaMopSendPath/);
  assert.doesNotMatch(route, /clear_pending_pm_retry_sent/);
  assert.match(route, /clear_pending_duplicate_suppressed/);
  assert.doesNotMatch(hooks, /sendClearViaMopSendPath/);
  assert.match(hooks, /clear_pending_retired/);
});
