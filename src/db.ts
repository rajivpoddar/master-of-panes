/**
 * MoP SQLite Database — Event log and slot state persistence
 *
 * Two tables:
 * - events: Append-only log of all hook events (the "chitta" replacement)
 * - slots: Current state of each dev slot (replaces pane-N.json files)
 */

import Database from "better-sqlite3";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import type { EventLogEntry, MoPConfig, SlotState, SlotStatus } from "./types.js";

export class MoPDatabase {
  private db: Database.Database;

  constructor(config: MoPConfig) {
    // Ensure data directory exists
    mkdirSync(dirname(config.dbPath), { recursive: true });

    this.db = new Database(config.dbPath);
    this.db.pragma("journal_mode = WAL"); // Better concurrent read perf
    this.db.pragma("foreign_keys = ON");
    this.init();
  }

  private init(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now')),
        slot INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        hook_type TEXT,
        tool_name TEXT,
        payload TEXT NOT NULL DEFAULT '{}',
        processed INTEGER NOT NULL DEFAULT 0
      );

      CREATE INDEX IF NOT EXISTS idx_events_slot ON events(slot);
      CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
      CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC);

      CREATE TABLE IF NOT EXISTS slots (
        slot INTEGER PRIMARY KEY,
        address TEXT NOT NULL,
        name TEXT,
        status TEXT NOT NULL DEFAULT 'free',
        occupied INTEGER NOT NULL DEFAULT 0,
        session_id TEXT,
        task TEXT,
        issue INTEGER,
        branch TEXT,
        pr INTEGER,
        assigned_at TEXT,
        last_activity TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now')),
        dnd INTEGER NOT NULL DEFAULT 0,
        idle INTEGER NOT NULL DEFAULT 1
      );
    `);

    // Migration: add name column if missing (for existing databases)
    const columns = this.db.prepare("PRAGMA table_info(slots)").all() as Array<{ name: string }>;
    if (!columns.some((c) => c.name === "name")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN name TEXT");
    }

    // Migration: add idle column if missing
    if (!columns.some((c) => c.name === "idle")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN idle INTEGER NOT NULL DEFAULT 1");
    }

    // Migration: add activity column if missing
    if (!columns.some((c) => c.name === "activity")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN activity TEXT");
    }

    // Initialize config KV table
    this.initConfig();

    // Seed slot rows if they don't exist
    const insertSlot = this.db.prepare(`
      INSERT OR IGNORE INTO slots (slot, address)
      VALUES (?, ?)
    `);

    for (let i = 1; i <= 4; i++) {
      insertSlot.run(i, `0:0.${i}`);
    }
  }

  // ─── Event Log ───────────────────────────────────────────

  logEvent(
    slot: number,
    eventType: string,
    hookType: string | null,
    toolName: string | null,
    payload: Record<string, unknown>
  ): number {
    const stmt = this.db.prepare(`
      INSERT INTO events (slot, event_type, hook_type, tool_name, payload)
      VALUES (?, ?, ?, ?, ?)
    `);
    const result = stmt.run(slot, eventType, hookType, toolName, JSON.stringify(payload));
    return Number(result.lastInsertRowid);
  }

  getEvents(
    slot?: number,
    limit: number = 50,
    eventType?: string
  ): EventLogEntry[] {
    let sql = "SELECT * FROM events WHERE 1=1";
    const params: unknown[] = [];

    if (slot !== undefined) {
      sql += " AND slot = ?";
      params.push(slot);
    }
    if (eventType) {
      sql += " AND event_type = ?";
      params.push(eventType);
    }

    sql += " ORDER BY timestamp DESC LIMIT ?";
    params.push(limit);

    return this.db.prepare(sql).all(...params) as EventLogEntry[];
  }

  markProcessed(eventId: number): void {
    this.db.prepare("UPDATE events SET processed = 1 WHERE id = ?").run(eventId);
  }

  // ─── Slot State ──────────────────────────────────────────

  getSlot(slot: number): SlotState | undefined {
    const row = this.db
      .prepare("SELECT * FROM slots WHERE slot = ?")
      .get(slot) as (Record<string, unknown> & { dnd: number; occupied: number; idle: number }) | undefined;

    if (!row) return undefined;

    return {
      ...row,
      dnd: Boolean(row.dnd),
      occupied: Boolean(row.occupied),
      idle: Boolean(row.idle),
    } as unknown as SlotState;
  }

  getAllSlots(): SlotState[] {
    const rows = this.db
      .prepare("SELECT * FROM slots ORDER BY slot")
      .all() as Array<Record<string, unknown> & { dnd: number; occupied: number; idle: number }>;

    return rows.map((row) => ({
      ...row,
      dnd: Boolean(row.dnd),
      occupied: Boolean(row.occupied),
      idle: Boolean(row.idle),
    })) as unknown as SlotState[];
  }

  updateSlot(slot: number, updates: Partial<SlotState>): void {
    const allowedFields = [
      "name", "status", "occupied", "session_id", "task", "issue",
      "branch", "pr", "assigned_at", "last_activity", "dnd", "idle", "activity",
    ];

    const sets: string[] = [];
    const values: unknown[] = [];

    for (const [key, value] of Object.entries(updates)) {
      if (!allowedFields.includes(key)) continue;
      sets.push(`${key} = ?`);
      // Convert booleans to integers for SQLite
      values.push(typeof value === "boolean" ? (value ? 1 : 0) : value);
    }

    if (sets.length === 0) return;

    // Always update last_activity
    sets.push("last_activity = strftime('%Y-%m-%dT%H:%M:%f', 'now')");

    values.push(slot);
    this.db.prepare(`UPDATE slots SET ${sets.join(", ")} WHERE slot = ?`).run(...values);
  }

  releaseSlot(slot: number): void {
    this.updateSlot(slot, {
      status: "free" as SlotStatus,
      occupied: false,
      session_id: null,
      task: null,
      issue: null,
      branch: null,
      pr: null,
      assigned_at: null,
      dnd: false,
      idle: true,
      activity: null,
    });
  }

  assignSlot(
    slot: number,
    task: string,
    issue: number | null,
    branch: string | null,
    sessionId: string | null
  ): void {
    this.updateSlot(slot, {
      status: "active" as SlotStatus,
      occupied: true,
      session_id: sessionId,
      task,
      issue,
      branch,
      assigned_at: new Date().toISOString(),
      dnd: false,
    });
  }

  // ─── Config (KV Store) ──────────────────────────────────

  private initConfig(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now'))
      )
    `);
  }

  getConfig(key: string): string | null {
    const row = this.db
      .prepare("SELECT value FROM config WHERE key = ?")
      .get(key) as { value: string } | undefined;
    return row?.value ?? null;
  }

  setConfig(key: string, value: string): void {
    this.db.prepare(`
      INSERT INTO config (key, value, updated_at)
      VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%f', 'now'))
      ON CONFLICT(key) DO UPDATE SET
        value = excluded.value,
        updated_at = excluded.updated_at
    `).run(key, value);
  }

  // ─── Exit Pending ──────────────────────────────────────

  getExitPending(): boolean {
    return this.getConfig("exit_pending") === "true";
  }

  setExitPending(enabled: boolean): void {
    this.setConfig("exit_pending", enabled ? "true" : "false");
    if (enabled) {
      // Reset all slot exit-cycled tracking when enabling
      for (let i = 0; i <= 4; i++) {
        this.setConfig(`exit_cycled_${i}`, "false");
      }
    }
  }

  markSlotExitCycled(slot: number): void {
    this.setConfig(`exit_cycled_${slot}`, "true");
  }

  getExitStatus(): { pending: boolean; cycled: Record<number, boolean> } {
    const pending = this.getExitPending();
    const cycled: Record<number, boolean> = {};
    for (let i = 0; i <= 4; i++) {
      cycled[i] = this.getConfig(`exit_cycled_${i}`) === "true";
    }
    return { pending, cycled };
  }

  // ─── Queries ─────────────────────────────────────────────

  getSlotHistory(slot: number, limit: number = 20): EventLogEntry[] {
    return this.getEvents(slot, limit);
  }

  getRecentActivity(minutes: number = 60): EventLogEntry[] {
    const stmt = this.db.prepare(`
      SELECT * FROM events
      WHERE timestamp > datetime('now', '-' || ? || ' minutes')
      ORDER BY timestamp DESC
    `);
    return stmt.all(minutes) as EventLogEntry[];
  }

  close(): void {
    this.db.close();
  }
}
