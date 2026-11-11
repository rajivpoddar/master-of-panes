import assert from "node:assert/strict";
import test from "node:test";
import type Database from "better-sqlite3";
import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";

// Use an in-memory database: no user files or live service state are touched.
test("startup retention reads a covering index and preserves exact gapped-ID/age cutoff", () => {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const raw = (db as unknown as { db: Database.Database }).db;
  try {
    const insert = raw.prepare("INSERT INTO events (id, slot, event_type, payload, timestamp) VALUES (?, ?, 'Stop', ?, ?)");
    const now = new Date().toISOString();
    raw.transaction(() => {
      for (let i = 1; i <= 1100; i++) insert.run(i * 2, 1 + (i % 4), "x".repeat(4096), now);
      insert.run(2202, 1, "old", "2000-01-01T00:00:00.000");
    })();
    const originalPrepare = raw.prepare.bind(raw);
    let cutoffPlan: Array<{ detail: string }> = [];
    raw.prepare = ((sql: string) => {
      if (sql.startsWith("SELECT id FROM events")) {
        cutoffPlan = originalPrepare(`EXPLAIN QUERY PLAN ${sql}`).all(999) as Array<{ detail: string }>;
      }
      return originalPrepare(sql);
    }) as typeof raw.prepare;
    assert.equal(db.pruneEvents(1000, 14), 102);
    assert.ok(cutoffPlan.some(row => row.detail.includes("USING COVERING INDEX idx_events_slot")),
      JSON.stringify(cutoffPlan));
    const rows = originalPrepare("SELECT id FROM events ORDER BY id").all() as Array<{ id: number }>;
    assert.deepEqual(rows.map(row => row.id), Array.from({ length: 999 }, (_, i) => (i + 102) * 2));
  } finally {
    db.close();
  }
});
