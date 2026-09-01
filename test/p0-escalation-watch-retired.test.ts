import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import Database from "better-sqlite3";

import { P0EscalationWatcher } from "../src/p0EscalationWatch.js";
import { PMCadenceScheduler } from "../src/pmCadence.js";

class FakeMopDb {
  private readonly config = new Map<string, string>();
  readonly events: Array<{ type: string; data: Record<string, unknown> }> = [];

  getConfig(key: string): string | null {
    return this.config.get(key) ?? null;
  }

  setConfig(key: string, value: string): void {
    this.config.set(key, value);
  }

  logEvent(_slot: number, type: string, _eventType: string | null, _toolName: string | null, data: Record<string, unknown>): void {
    this.events.push({ type, data });
  }
}

class FakeRelay {
  injections: string[] = [];

  async submitToPM(message: string): Promise<{ ok: boolean }> {
    this.injections.push(message);
    return { ok: true };
  }
}

async function withPmOpsDb(run: (path: string) => Promise<void>): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-p0-watch-test-"));
  const path = join(directory, "pm-ops.db");
  try {
    const db = new Database(path);
    db.exec(`
      CREATE TABLE obligations (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        title TEXT,
        required_action TEXT,
        evidence_json TEXT,
        next_review_at TEXT,
        suppress_until TEXT,
        status TEXT NOT NULL,
        kind TEXT NOT NULL
      );
      INSERT INTO obligations
        (id, created_at, updated_at, target_type, target_id, title,
         required_action, evidence_json, next_review_at, suppress_until,
         status, kind)
      VALUES
        (14909, '2026-08-25T00:00:00.000Z', '2026-08-25T00:00:00.000Z',
         'slack-thread', '7024', 'P0 test obligation', 'review', '{}',
         NULL, NULL, 'open', 'p0_escalation');
    `);
    db.close();
    await run(path);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("boot and scheduled diagnostics never inject P0 prompts", async () => {
  await withPmOpsDb(async (pmOpsDb) => {
    const mopDb = new FakeMopDb();
    const relay = new FakeRelay();
    process.env.MOP_PM_OPS_DB = pmOpsDb;
    const watcher = new P0EscalationWatcher(mopDb as never, relay as never);

    watcher.start();
    assert.equal(watcher.getStatus().running, false);
    assert.equal(watcher.getStatus().boot_pending, false);
    assert.equal(watcher.getStatus().automatic_scheduling, false);

    const result = await watcher.tick("scheduled");
      assert.equal(result.due, 1);
      assert.equal(result.injected, 0);
      assert.equal(relay.injections.length, 0);
      watcher.stop();
  });
});

test("manual diagnostics remain read-only and heartbeat ownership is readable", async () => {
  await withPmOpsDb(async (pmOpsDb) => {
    const mopDb = new FakeMopDb();
    const relay = new FakeRelay();
    process.env.MOP_PM_OPS_DB = pmOpsDb;
    const watcher = new P0EscalationWatcher(mopDb as never, relay as never);
    const result = await watcher.tick("manual");
    assert.equal(result.due, 1);
    assert.equal(result.injected, 0);
    assert.equal(relay.injections.length, 0);

    const cadence = new PMCadenceScheduler(mopDb as never, relay as never);
    const heartbeat = cadence.getStatus().tasks.find((task) => task.task === "heartbeat");
    assert.ok(heartbeat);
    assert.equal(heartbeat.label, "3h heartbeat");
    assert.equal(heartbeat.command.includes("/Users/rajiv/.claude/scripts/sakshi-heartbeat.py --dry-run"), true);
    assert.equal(heartbeat.command.includes("/Users/rajiv/.claude/scripts/sakshi-heartbeat.py --launch-prompt"), true);
    assert.equal(heartbeat.command.includes("/Users/rajiv/.claude/scripts/sakshi-heartbeat.py --ready-pool-audit"), true);
    assert.equal(heartbeat.command.includes("heartbeat-tasks"), false);
    assert.equal(heartbeat.command.includes("pm-operator"), false);
  });
});
