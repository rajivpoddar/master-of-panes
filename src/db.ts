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

    // PM busy-queue table — coalesce-on-key (slot, event_type) so the latest
    // enqueue per (slot, event_type) wins via INSERT OR REPLACE. Drained on
    // PM Stop transition.
    //
    // Rajiv directive 2026-05-06 11:18 IST: queue slot-idle/active/check-slot
    // events to PM while PM is busy (mid-tool/turn), drain on PM stop hook.
    // Mirrors pm_pending_clears semantics but for PM-pane slash-command relays.
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS pm_pending_events (
        slot INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT,
        enqueued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now')),
        PRIMARY KEY (slot, event_type)
      );
    `);

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

  /**
   * Dedup helper for notifyEscalation — returns the most recent
   * `escalation` event row for (slot, issueNum) within the timestamp
   * window, or undefined if none. Payload match uses LIKE on the
   * JSON-serialized `"issue":N` substring (cheap; payload is < 500 chars).
   */
  getRecentEscalation(
    slot: number,
    issueNum: number,
    sinceIso: string
  ): { id: number; timestamp: string } | undefined {
    return this.db
      .prepare(
        `SELECT id, timestamp FROM events
         WHERE slot = ? AND event_type = 'escalation'
           AND timestamp > ?
           AND payload LIKE ?
         ORDER BY id DESC LIMIT 1`
      )
      .get(slot, sinceIso, `%"issue":${issueNum}%`) as
      | { id: number; timestamp: string }
      | undefined;
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

  // ─── Clear Pending ─────────────────────────────────────

  /**
   * Set pending clear for a specific slot.
   * When the slot next goes idle, handleStop will send /clear.
   */
  setPendingClear(slot: number): void {
    this.setConfig(`clear_pending_${slot}`, "true");
  }

  /**
   * Check if a specific slot has a pending clear.
   */
  hasPendingClear(slot: number): boolean {
    return this.getConfig(`clear_pending_${slot}`) === "true";
  }

  /**
   * Clear the pending clear flag for a slot (after /clear sent).
   */
  clearPendingClear(slot: number): void {
    this.setConfig(`clear_pending_${slot}`, "false");
  }

  /**
   * Get all pending clear statuses.
   */
  getClearPendingStatus(): Record<number, boolean> {
    const status: Record<number, boolean> = {};
    for (let i = 0; i <= 4; i++) {
      status[i] = this.hasPendingClear(i);
    }
    return status;
  }

  /**
   * Clear all pending clear flags.
   */
  clearAllPendingClears(): void {
    for (let i = 0; i <= 4; i++) {
      this.clearPendingClear(i);
    }
  }

  // ─── PM Pending Events Queue ─────────────────────────────
  //
  // Holds slot-idle / slot-active / check-slot relays (and any other PM-bound
  // injectToPM calls) when the PM pane is busy. Drained on PM Stop hook.
  //
  // Coalesce semantics: PRIMARY KEY (slot, event_type) means a fresher event
  // of the same shape replaces the older one — we only ever want the latest
  // signal per (slot, event_type) by the time PM drains the queue.
  //
  // Rajiv directive 2026-05-06 11:18 IST.

  enqueuePendingPMEvent(
    slot: number,
    eventType: string,
    payload: string | null = null,
  ): void {
    const stmt = this.db.prepare(`
      INSERT INTO pm_pending_events (slot, event_type, payload, enqueued_at)
      VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%f', 'now'))
      ON CONFLICT(slot, event_type) DO UPDATE SET
        payload = excluded.payload,
        enqueued_at = excluded.enqueued_at
    `);
    stmt.run(slot, eventType, payload);
  }

  /**
   * Drain all queued PM-bound events. Returns rows ordered by slot,
   * with cross-event-type coalescing applied: per slot, if a slot-idle
   * is queued alongside a check-slot or slot-active, the terminal idle
   * signal wins (drop check-slot/slot-active). If slot-active and
   * check-slot coexist, slot-active wins. Caller is responsible for
   * actually injecting these into the PM pane and then calling
   * clearAllPendingPMEvents() (or the rows are deleted as part of the
   * drain transaction below).
   */
  drainPendingPMEvents(): Array<{ slot: number; event_type: string; payload: string | null; enqueued_at: string }> {
    const rows = this.db.prepare(`
      SELECT slot, event_type, payload, enqueued_at
      FROM pm_pending_events
      ORDER BY slot ASC, enqueued_at ASC
    `).all() as Array<{ slot: number; event_type: string; payload: string | null; enqueued_at: string }>;

    // Coalesce per slot: idle > active > check-slot. Other event_types pass
    // through untouched.
    const bySlot = new Map<number, Map<string, typeof rows[number]>>();
    for (const row of rows) {
      let m = bySlot.get(row.slot);
      if (!m) { m = new Map(); bySlot.set(row.slot, m); }
      m.set(row.event_type, row);
    }

    const out: typeof rows = [];
    const slots = [...bySlot.keys()].sort((a, b) => a - b);
    for (const slot of slots) {
      const m = bySlot.get(slot)!;
      const hasIdle = m.has("slot-idle");
      const hasActive = m.has("slot-active");
      // `check-slot` is the lowest-priority signal — always dropped if
      // either slot-idle or slot-active is queued for the same slot.
      const dropCheck = hasIdle || hasActive;
      // `slot-active` is dropped if `slot-idle` is queued for the same slot
      // (idle is terminal — slot finished its turn).
      const dropActive = hasIdle;

      for (const [eventType, row] of m) {
        if (eventType === "check-slot" && dropCheck) continue;
        if (eventType === "slot-active" && dropActive) continue;
        out.push(row);
      }
    }

    // Delete drained rows.
    this.db.exec("DELETE FROM pm_pending_events");
    return out;
  }

  /** Returns count of rows currently queued. */
  getPendingPMEventCount(): number {
    const row = this.db.prepare("SELECT COUNT(*) AS n FROM pm_pending_events").get() as { n: number };
    return row?.n ?? 0;
  }

  /** Inspect (without draining). Used by /pm-status GET for diagnostics. */
  peekPendingPMEvents(): Array<{ slot: number; event_type: string; payload: string | null; enqueued_at: string }> {
    return this.db.prepare(`
      SELECT slot, event_type, payload, enqueued_at
      FROM pm_pending_events
      ORDER BY slot ASC, enqueued_at ASC
    `).all() as Array<{ slot: number; event_type: string; payload: string | null; enqueued_at: string }>;
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

  /**
   * Detect whether a slot has dispatched a Task subagent in the last `windowSec`
   * seconds AND has not yet emitted a terminal Stop event after that dispatch.
   *
   * Used by the slot-idle staleness gate (mirroring check-slot's idle-skip):
   * between the IDLE_DEBOUNCE_MS window opening and the timer firing, the slot
   * may have transitioned active again (e.g. plan-agent fired). The MoP
   * idle flag on the SlotState is point-in-time and lags real activity by up
   * to one debounce window. The events table is the source of truth.
   *
   * Returns the latest Task dispatch timestamp if a recent unclosed dispatch
   * exists; null otherwise.
   *
   * Rajiv directive 2026-05-05: PM nudge interrupted slot 4's plan-agent
   * because the classifier captured JSONL delta BEFORE the Task fired but
   * the PM didn't process the notification until 43s later — by which time
   * the subagent was already running.
   */
  hasRecentSubagentDispatch(
    slotNum: number,
    windowSec: number = 60
  ): { taskTs: string; lastStopTs: string | null } | null {
    // Most recent Task dispatch within the window.
    // Use strftime() not datetime() so the cutoff has the same 'YYYY-MM-DDTHH:MM:SS.fff'
    // shape as stored timestamps — datetime() returns 'YYYY-MM-DD HH:MM:SS' (space, no
    // fraction) which lexicographically sorts BELOW any stored 'T...' timestamp,
    // making every row "in window".
    const taskStmt = this.db.prepare(`
      SELECT timestamp FROM events
      WHERE slot = ?
        AND event_type IN ('PostToolUse', 'PreToolUse')
        AND tool_name = 'Task'
        AND timestamp > strftime('%Y-%m-%dT%H:%M:%f', 'now', '-' || ? || ' seconds')
      ORDER BY timestamp DESC
      LIMIT 1
    `);
    const taskRow = taskStmt.get(slotNum, windowSec) as { timestamp: string } | undefined;
    if (!taskRow) return null;

    // Any Stop event strictly AFTER that Task dispatch closes the subagent.
    const stopStmt = this.db.prepare(`
      SELECT timestamp FROM events
      WHERE slot = ?
        AND event_type = 'Stop'
        AND timestamp > ?
      ORDER BY timestamp DESC
      LIMIT 1
    `);
    const stopRow = stopStmt.get(slotNum, taskRow.timestamp) as { timestamp: string } | undefined;

    // If a Stop fired after the Task, the subagent is closed → no gate.
    if (stopRow) return null;

    return { taskTs: taskRow.timestamp, lastStopTs: null };
  }

  /**
   * Generic recent-tool detector: was ANY tool fired in the last `windowSec`
   * seconds? Used as a secondary staleness gate for slot-idle when the slot
   * is mid-tool-call but happens to be momentarily idle between PostToolUse
   * and the next PreToolUse.
   *
   * Returns the latest tool tuple or null.
   */
  getLastToolFire(
    slotNum: number,
    windowSec: number = 30
  ): { tool: string; timestamp: string } | null {
    // strftime() (not datetime()) for the same lexicographic shape reason as
    // hasRecentSubagentDispatch above.
    const stmt = this.db.prepare(`
      SELECT tool_name, timestamp FROM events
      WHERE slot = ?
        AND event_type = 'PostToolUse'
        AND tool_name IS NOT NULL
        AND tool_name != ''
        AND timestamp > strftime('%Y-%m-%dT%H:%M:%f', 'now', '-' || ? || ' seconds')
      ORDER BY timestamp DESC
      LIMIT 1
    `);
    const row = stmt.get(slotNum, windowSec) as
      | { tool_name: string; timestamp: string }
      | undefined;
    if (!row) return null;
    return { tool: row.tool_name, timestamp: row.timestamp };
  }

  // ─── Review Status (unforgeable gate) ───────────────────

  /**
   * Check if a code review was run for a given issue number.
   * Searches the events table for Skill invocations matching review patterns.
   * This is unforgeable — only actual tool invocations logged by MoP hooks
   * can create these entries. Slots cannot write to the events table.
   *
   * @param issueNumber - GitHub issue number
   * @param windowMinutes - How far back to search (default 60 min)
   * @returns Review status with method, timestamp, and verdict if found
   */
  findReviewEvent(
    issueNumber: number,
    windowMinutes: number = 60
  ): { found: boolean; method: string | null; timestamp: string | null; slot: number | null; details: string | null } {
    const issueStr = String(issueNumber);

    // Search for Skill tool calls that match review patterns
    // Covers: codex-app-code-review, codex-review, zen-code-review, zen-review
    const stmt = this.db.prepare(`
      SELECT slot, timestamp, tool_name, payload
      FROM events
      WHERE timestamp > datetime('now', '-' || ? || ' minutes')
        AND (
          (tool_name = 'Skill' AND (
            payload LIKE '%codex%review%' OR
            payload LIKE '%zen%review%'
          ))
          OR
          (tool_name = 'Bash' AND (
            payload LIKE '%codex exec%' OR
            payload LIKE '%codex-companion%review%'
          ))
        )
        AND payload LIKE ?
      ORDER BY timestamp DESC
      LIMIT 1
    `);

    const row = stmt.get(windowMinutes, `%${issueStr}%`) as {
      slot: number; timestamp: string; tool_name: string; payload: string;
    } | undefined;

    if (!row) {
      return { found: false, method: null, timestamp: null, slot: null, details: null };
    }

    // Determine method from the match
    let method = "unknown";
    const payload = row.payload.toLowerCase();
    if (payload.includes("codex-app")) method = "codex-app";
    else if (payload.includes("zen")) method = "zen";
    else if (payload.includes("codex exec")) method = "codex-cli";
    else if (payload.includes("codex-companion")) method = "codex-plugin";
    else if (payload.includes("codex")) method = "codex";

    return {
      found: true,
      method,
      timestamp: row.timestamp,
      slot: row.slot,
      details: `tool=${row.tool_name}, matched in events DB`,
    };
  }

  close(): void {
    this.db.close();
  }
}
