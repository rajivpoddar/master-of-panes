import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import Database from "better-sqlite3";
import { MoPDatabase } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

function withDatabase(
  run: (db: MoPDatabase, path: string) => void,
): void {
  const directory = mkdtempSync(join(tmpdir(), "mop-responsive-"));
  const path = join(directory, "mop.db");
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: path,
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  try {
    run(db, path);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

test("recent subagent lookup uses the covering event index", () => {
  withDatabase((_db, path) => {
    const raw = new Database(path);
    try {
      const indexes = raw.prepare(`
        SELECT name FROM sqlite_master
        WHERE type = 'index'
      `).all() as Array<{ name: string }>;
      assert.ok(
        indexes.some(({ name }) => name === "idx_events_slot_type_tool_time"),
      );

      const plan = raw.prepare(`
        EXPLAIN QUERY PLAN
        SELECT timestamp, tool_name, payload FROM events
        WHERE slot = ?
          AND event_type IN ('PostToolUse', 'PreToolUse')
          AND tool_name IN ('Task', 'Agent')
          AND timestamp > strftime('%Y-%m-%dT%H:%M:%f', 'now', '-' || ? || ' seconds')
        ORDER BY timestamp DESC
        LIMIT 20
      `).all(1, 3600) as Array<{ detail: string }>;
      assert.match(
        plan.map(({ detail }) => detail).join("\n"),
        /idx_events_slot_type_tool_time/,
      );
    } finally {
      raw.close();
    }
  });
});

test("minute windows exclude same-day rows outside the requested interval", () => {
  withDatabase((db, path) => {
    db.logEvent(1, "old_activity", "PostToolUse", "Bash", {});
    const recentId = db.logEvent(1, "recent_activity", "PostToolUse", "Bash", {});
    const oldReviewId = db.logEvent(1, "review_old", "PreToolUse", "Skill", {
      skill: "codex-code-review",
      issue: 99991,
    });

    const raw = new Database(path);
    try {
      raw.prepare(`
        UPDATE events
        SET timestamp = strftime('%Y-%m-%dT%H:%M:%f', 'now', '-3 hours')
        WHERE id IN (
          SELECT id FROM events
          WHERE id != ?
        )
      `).run(recentId);
      raw.prepare(`
        UPDATE events
        SET timestamp = strftime('%Y-%m-%dT%H:%M:%f', 'now', '-3 hours')
        WHERE id = ?
      `).run(oldReviewId);
    } finally {
      raw.close();
    }

    const recent = db.getRecentActivity(60);
    assert.deepEqual(recent.map(({ event_type }) => event_type), ["recent_activity"]);
    assert.equal(db.findReviewEvent(99991, 60).found, false);
  });
});

test("recurring and request-path modules avoid synchronous runtime file IO", () => {
  const files = [
    "logs.ts",
    "relay.ts",
    "server.ts",
    "stuck.ts",
    "hooks.ts",
  ];
  const forbidden =
    /\b(?:readdirSync|statSync|readFileSync|writeFileSync|appendFileSync|openSync|readSync|closeSync|unlinkSync)\b/;
  for (const file of files) {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, forbidden, file);
  }

  const opsAudit = readFileSync(
    new URL("../src/opsAudit.ts", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(
    opsAudit,
    /\b(?:appendFileSync|writeFileSync)\b/,
    "opsAudit.ts",
  );
});
