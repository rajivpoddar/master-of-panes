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
import type { EventLogEntry, MoPConfig, OpsJobRecord, OpsJobStatus, SlotState, SlotStatus } from "./types.js";

export interface SlotMutationResult {
  ok: boolean;
  conflict: boolean;
  assignment_epoch: number;
  idempotent: boolean;
  reason?: "expected_epoch_required" | "epoch_mismatch" | "target_already_assigned";
  owner_slots?: number[];
}

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
      CREATE INDEX IF NOT EXISTS idx_events_slot_id ON events(slot, id DESC);
      CREATE INDEX IF NOT EXISTS idx_events_type_id ON events(event_type, id DESC);
      CREATE INDEX IF NOT EXISTS idx_events_slot_type_id ON events(slot, event_type, id DESC);

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
        head_sha TEXT,
        assignment_epoch INTEGER NOT NULL DEFAULT 0,
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

    if (!columns.some((c) => c.name === "head_sha")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN head_sha TEXT");
    }
    if (!columns.some((c) => c.name === "assignment_epoch")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN assignment_epoch INTEGER NOT NULL DEFAULT 0");
    }
    if (!columns.some((c) => c.name === "active_turn_id")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN active_turn_id TEXT");
    }
    if (!columns.some((c) => c.name === "active_turn_started_at")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN active_turn_started_at TEXT");
    }
    if (!columns.some((c) => c.name === "active_turn_state")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN active_turn_state TEXT NOT NULL DEFAULT 'inactive'");
    }
    if (!columns.some((c) => c.name === "last_meaningful_work_at")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN last_meaningful_work_at TEXT");
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

    this.db.exec(`
      CREATE TABLE IF NOT EXISTS ops_jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        reason TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now')),
        started_at TEXT,
        finished_at TEXT,
        pid INTEGER,
        exit_code INTEGER,
        decision TEXT,
        result_reason TEXT,
        payload_bytes INTEGER,
        error TEXT,
        stdout_path TEXT,
        trace_path TEXT
      );

      CREATE INDEX IF NOT EXISTS idx_ops_jobs_kind_status_created
        ON ops_jobs(kind, status, created_at DESC);
      CREATE INDEX IF NOT EXISTS idx_ops_jobs_kind_finished
        ON ops_jobs(kind, finished_at DESC);
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

    // id is monotonic and avoids filtered timestamp scans plus temp sorting.
    sql += " ORDER BY id DESC LIMIT ?";
    params.push(limit);

    return this.db.prepare(sql).all(...params) as EventLogEntry[];
  }

  /**
   * Bound the operational event log. MoP events are diagnostics, not durable
   * product records; keeping the newest rows and a short time window prevents
   * synchronous hook/API reads from starving the event loop indefinitely.
   */
  pruneEvents(maxRows: number = 200_000, maxAgeDays: number = 14): number {
    const safeMaxRows = Math.max(1_000, Math.floor(maxRows));
    const safeMaxAgeDays = Math.max(1, Math.floor(maxAgeDays));
    const cutoff = this.db
      .prepare("SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?")
      .get(safeMaxRows - 1) as { id: number } | undefined;

    const result = cutoff
      ? this.db.prepare(`
          DELETE FROM events
          WHERE id < ?
             OR timestamp < strftime('%Y-%m-%dT%H:%M:%f', 'now', ?)
        `).run(cutoff.id, `-${safeMaxAgeDays} days`)
      : this.db.prepare(`
          DELETE FROM events
          WHERE timestamp < strftime('%Y-%m-%dT%H:%M:%f', 'now', ?)
        `).run(`-${safeMaxAgeDays} days`);

    this.db.pragma("wal_checkpoint(PASSIVE)");
    this.db.pragma("optimize");
    return result.changes;
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
      "branch", "pr", "head_sha", "assigned_at", "last_activity", "dnd", "idle", "activity",
      "active_turn_id", "active_turn_started_at", "active_turn_state", "last_meaningful_work_at",
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

  releaseSlot(slot: number, expectedEpoch?: number): SlotMutationResult {
    if (!Number.isInteger(expectedEpoch)) {
      const current = this.getSlot(slot);
      return {
        ok: false,
        conflict: true,
        assignment_epoch: current?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "expected_epoch_required",
      };
    }

    return this.db.transaction((): SlotMutationResult => {
      const current = this.getSlot(slot);
      const epoch = current?.assignment_epoch ?? 0;
      if (!current || epoch !== expectedEpoch) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "epoch_mismatch" };
      }
      if (!current.occupied) {
        return { ok: true, conflict: false, assignment_epoch: epoch, idempotent: true };
      }
      this.updateSlot(slot, {
        status: "free" as SlotStatus,
        occupied: false,
        session_id: null,
        task: null,
        issue: null,
        branch: null,
        pr: null,
        head_sha: null,
        assigned_at: null,
        dnd: false,
        idle: true,
        activity: null,
        active_turn_id: null,
        active_turn_started_at: null,
        active_turn_state: "inactive",
      });
      return { ok: true, conflict: false, assignment_epoch: epoch, idempotent: false };
    })();
  }

  assignSlot(
    slot: number,
    task: string,
    issue: number | null,
    branch: string | null,
    sessionId: string | null,
    pr: number | null = null,
    headSha: string | null = null,
    expectedEpoch?: number
  ): SlotMutationResult {
    if (!Number.isInteger(expectedEpoch)) {
      const current = this.getSlot(slot);
      return {
        ok: false,
        conflict: true,
        assignment_epoch: current?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "expected_epoch_required",
      };
    }

    return this.db.transaction((): SlotMutationResult => {
      const current = this.getSlot(slot);
      const epoch = current?.assignment_epoch ?? 0;
      if (!current || epoch !== expectedEpoch) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "epoch_mismatch" };
      }
      const normalizedBranch = branch?.trim() || null;
      const ownerSlots = this.db.prepare(`
        SELECT slot
        FROM slots
        WHERE occupied = 1
          AND slot != ?
          AND (
            (? IS NOT NULL AND pr = ?)
            OR (? IS NOT NULL AND issue = ?)
            OR (? IS NOT NULL AND branch = ?)
          )
        ORDER BY slot
      `).all(
        slot,
        pr, pr,
        issue, issue,
        normalizedBranch, normalizedBranch
      ) as Array<{ slot: number }>;
      if (ownerSlots.length > 0) {
        return {
          ok: false,
          conflict: true,
          assignment_epoch: epoch,
          idempotent: false,
          reason: "target_already_assigned",
          owner_slots: ownerSlots.map((owner) => owner.slot),
        };
      }
      const idempotent = current.occupied
        && current.issue === issue
        && current.pr === pr
        && current.branch === branch
        && current.head_sha === headSha;
      const nextEpoch = idempotent ? epoch : epoch + 1;
      this.updateSlot(slot, {
        status: "active" as SlotStatus,
        occupied: true,
        session_id: sessionId,
        task,
        issue,
        branch,
        pr,
        head_sha: headSha,
        assigned_at: idempotent ? current.assigned_at : new Date().toISOString(),
        dnd: false,
      });
      if (!idempotent) {
        this.db.prepare("UPDATE slots SET assignment_epoch = ? WHERE slot = ?").run(nextEpoch, slot);
      }
      return { ok: true, conflict: false, assignment_epoch: nextEpoch, idempotent };
    })();
  }

  startAgentTurn(slot: number, turnId: string): void {
    const now = new Date().toISOString();
    this.updateSlot(slot, {
      active_turn_id: turnId,
      active_turn_started_at: now,
      active_turn_state: "active",
      last_meaningful_work_at: now,
      idle: false,
    });
  }

  touchMeaningfulWork(slot: number, turnId?: string | null): void {
    const current = this.getSlot(slot);
    const now = new Date().toISOString();
    this.updateSlot(slot, {
      active_turn_id: turnId ?? current?.active_turn_id ?? null,
      active_turn_state: "active",
      last_meaningful_work_at: now,
      idle: false,
    });
  }

  finishAgentTurn(slot: number, turnId?: string | null): void {
    const current = this.getSlot(slot);
    if (!current) return;
    if (turnId && current.active_turn_id && turnId !== current.active_turn_id) {
      this.updateSlot(slot, { active_turn_state: "indeterminate" });
      return;
    }
    this.updateSlot(slot, {
      active_turn_id: null,
      active_turn_started_at: null,
      active_turn_state: "inactive",
      idle: true,
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

  deletePendingPMEvent(slot: number, eventType: string): number {
    const result = this.db.prepare(`
      DELETE FROM pm_pending_events
      WHERE slot = ? AND event_type = ?
    `).run(slot, eventType);
    return result.changes;
  }

  /**
   * Drain all queued PM-bound events. Returns AT MOST ONE row per slot —
   * the most-relevant single notification — so PM sees one summary signal
   * per slot rather than a stream of intermediate state transitions.
   *
   * Coalesce policy (Rajiv directive 2026-05-13 14:13 IST thread
   * `1778661820.586119`: "send the last one when pm goes idle after stop
   * hook fire"):
   *
   *   Per-slot priority (highest first):
   *     1. `slot-blocked` — terminal blocker, PM must intervene
   *     2. `slot-idle`     — terminal turn-end, PM acts
   *     3. `slot-active`   — informational state-change
   *     4. `check-slot`    — periodic 5-min wellness ping
   *     5. `freeform-*`    — escalation comments, plan-approval-needed,
   *                          compact warning, scheduled-task. Free-form
   *                          rows are ALSO collapsed to one per slot;
   *                          the most-recently-enqueued wins.
   *
   *   When a higher-priority signal exists for a slot, all lower-priority
   *   rows for that slot are dropped. Within a tie (same priority bucket)
   *   the most-recently-enqueued row wins.
   *
   *   Free-form rows (`freeform-<hash>`) DO NOT mix with slot-state rows —
   *   they are emitted as a separate notification per slot ONLY IF no
   *   slot-state signal exists for that slot. This preserves escalation
   *   visibility when the slot has nothing else queued.
   *
   * Returns the deduped rows. Caller injects each row into PM.
   */
  drainPendingPMEvents(): Array<{ slot: number; event_type: string; payload: string | null; enqueued_at: string }> {
    const rows = this.db.prepare(`
      SELECT slot, event_type, payload, enqueued_at
      FROM pm_pending_events
      ORDER BY slot ASC, enqueued_at ASC
    `).all() as Array<{ slot: number; event_type: string; payload: string | null; enqueued_at: string }>;

    // Priority: lower number = higher priority. Free-form bucket sits below
    // slot-state rows; within free-form, most-recent wins.
    const priorityOf = (eventType: string): number => {
      if (eventType === "slot-blocked") return 1;
      if (eventType === "slot-idle") return 2;
      if (eventType === "slot-active") return 3;
      if (eventType === "check-slot") return 4;
      // freeform-<hash> or any other custom event_type — only emit when no
      // slot-state row exists. Most-recently-enqueued wins within bucket.
      return 5;
    };

    // Group by slot, pick winner per slot.
    type Row = typeof rows[number];
    const bySlot = new Map<number, Row[]>();
    for (const row of rows) {
      const arr = bySlot.get(row.slot) ?? [];
      arr.push(row);
      bySlot.set(row.slot, arr);
    }

    const out: Row[] = [];
    const slots = [...bySlot.keys()].sort((a, b) => a - b);
    for (const slot of slots) {
      const slotRows = bySlot.get(slot)!;
      // Sort: priority ASC (higher prio first), then enqueued_at DESC
      // (most-recent first within same priority).
      slotRows.sort((a, b) => {
        const pa = priorityOf(a.event_type);
        const pb = priorityOf(b.event_type);
        if (pa !== pb) return pa - pb;
        return b.enqueued_at.localeCompare(a.enqueued_at);
      });
      out.push(slotRows[0]);
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

  // ─── Ops Jobs ──────────────────────────────────────────────

  createOpsJob(job: OpsJobRecord): void {
    this.db.prepare(`
      INSERT INTO ops_jobs (
        id, kind, reason, status, created_at, started_at, finished_at,
        pid, exit_code, decision, result_reason, payload_bytes, error,
        stdout_path, trace_path
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      job.id,
      job.kind,
      job.reason,
      job.status,
      job.created_at,
      job.started_at,
      job.finished_at,
      job.pid,
      job.exit_code,
      job.decision,
      job.result_reason,
      job.payload_bytes,
      job.error,
      job.stdout_path,
      job.trace_path,
    );
  }

  updateOpsJob(id: string, updates: Partial<Omit<OpsJobRecord, "id">>): void {
    const allowedFields = [
      "kind", "reason", "status", "created_at", "started_at", "finished_at",
      "pid", "exit_code", "decision", "result_reason", "payload_bytes",
      "error", "stdout_path", "trace_path",
    ];
    const sets: string[] = [];
    const values: unknown[] = [];
    for (const [key, value] of Object.entries(updates)) {
      if (!allowedFields.includes(key)) continue;
      sets.push(`${key} = ?`);
      values.push(value);
    }
    if (sets.length === 0) return;
    values.push(id);
    this.db.prepare(`UPDATE ops_jobs SET ${sets.join(", ")} WHERE id = ?`).run(...values);
  }

  getOpsJob(id: string): OpsJobRecord | null {
    const row = this.db.prepare("SELECT * FROM ops_jobs WHERE id = ?").get(id);
    return row ? normalizeOpsJob(row as Record<string, unknown>) : null;
  }

  getRunningOpsJob(kind: string): OpsJobRecord | null {
    const row = this.db.prepare(`
      SELECT * FROM ops_jobs
      WHERE kind = ? AND status = 'running'
      ORDER BY started_at DESC, created_at DESC
      LIMIT 1
    `).get(kind);
    return row ? normalizeOpsJob(row as Record<string, unknown>) : null;
  }

  getQueuedOpsJob(kind: string, reason?: string): OpsJobRecord | null {
    const row = reason
      ? this.db.prepare(`
          SELECT * FROM ops_jobs
          WHERE kind = ? AND reason = ? AND status = 'queued'
          ORDER BY created_at ASC
          LIMIT 1
        `).get(kind, reason)
      : this.db.prepare(`
          SELECT * FROM ops_jobs
          WHERE kind = ? AND status = 'queued'
          ORDER BY created_at ASC
          LIMIT 1
        `).get(kind);
    return row ? normalizeOpsJob(row as Record<string, unknown>) : null;
  }

  getNextQueuedOpsJob(kind: string): OpsJobRecord | null {
    return this.getQueuedOpsJob(kind);
  }

  getLatestOpsJob(kind: string): OpsJobRecord | null {
    const row = this.db.prepare(`
      SELECT * FROM ops_jobs
      WHERE kind = ?
      ORDER BY created_at DESC
      LIMIT 1
    `).get(kind);
    return row ? normalizeOpsJob(row as Record<string, unknown>) : null;
  }

  getLatestCompletedOpsJob(kind: string): OpsJobRecord | null {
    const row = this.db.prepare(`
      SELECT * FROM ops_jobs
      WHERE kind = ? AND status NOT IN ('queued', 'running')
      ORDER BY COALESCE(finished_at, created_at) DESC
      LIMIT 1
    `).get(kind);
    return row ? normalizeOpsJob(row as Record<string, unknown>) : null;
  }

  listOpsJobs(kind: string, limit: number = 10): OpsJobRecord[] {
    const rows = this.db.prepare(`
      SELECT * FROM ops_jobs
      WHERE kind = ?
      ORDER BY created_at DESC
      LIMIT ?
    `).all(kind, limit) as Record<string, unknown>[];
    return rows.map(normalizeOpsJob);
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

  getLastVisibleSlotState(
    slotNum: number
  ): { state: "idle" | "active"; timestamp: string; eventType: string } | null {
    const row = this.db.prepare(`
      SELECT e.event_type, e.timestamp FROM events e
      JOIN slots s ON s.slot = e.slot
      WHERE e.slot = ?
        AND s.occupied = 1
        AND s.assigned_at IS NOT NULL
        AND julianday(e.timestamp) >= julianday(s.assigned_at)
        AND e.event_type IN ('slot_idle_notified', 'slot_active_notified')
      ORDER BY e.timestamp DESC, e.id DESC
      LIMIT 1
    `).get(slotNum) as { event_type: string; timestamp: string } | undefined;

    if (!row) return null;
    return {
      state: row.event_type === "slot_idle_notified" ? "idle" : "active",
      timestamp: row.timestamp,
      eventType: row.event_type,
    };
  }

  /**
   * Detect whether a slot has dispatched a subagent in the last `windowSec`
   * seconds and that subagent has not been closed yet.
   *
   * Used by the slot-idle staleness gate (mirroring check-slot's idle-skip):
   * between the IDLE_DEBOUNCE_MS window opening and the timer firing, the slot
   * may have transitioned active again (e.g. plan-agent fired). The MoP
   * idle flag on the SlotState is point-in-time and lags real activity by up
   * to one debounce window. The events table is the source of truth.
   *
   * Returns the latest subagent dispatch timestamp if a recent unclosed dispatch
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
  ): { taskTs: string; lastStopTs: string | null; toolName?: string } | null {
    // Most recent Task/Agent dispatches within the window.
    // Use strftime() not datetime() so the cutoff has the same 'YYYY-MM-DDTHH:MM:SS.fff'
    // shape as stored timestamps — datetime() returns 'YYYY-MM-DD HH:MM:SS' (space, no
    // fraction) which lexicographically sorts BELOW any stored 'T...' timestamp,
    // making every row "in window".
    const taskStmt = this.db.prepare(`
      SELECT timestamp, tool_name, payload FROM events
      WHERE slot = ?
        AND event_type IN ('PostToolUse', 'PreToolUse')
        AND tool_name IN ('Task', 'Agent')
        AND timestamp > strftime('%Y-%m-%dT%H:%M:%f', 'now', '-' || ? || ' seconds')
      ORDER BY timestamp DESC
      LIMIT 20
    `);
    const taskRows = taskStmt.all(slotNum, windowSec) as Array<{
      timestamp: string;
      tool_name: string;
      payload: string;
    }>;
    if (taskRows.length === 0) return null;

    const stopStmt = this.db.prepare(`
      SELECT timestamp FROM events
      WHERE slot = ?
        AND event_type = 'Stop'
        AND timestamp > ?
      ORDER BY timestamp DESC
      LIMIT 1
    `);

    const agentCloseStmt = this.db.prepare(`
      SELECT timestamp FROM events
      WHERE slot = ?
        AND timestamp > ?
        AND (
          (event_type = 'PostToolUse' AND tool_name = 'TaskStop')
          OR event_type = 'subagent_completed'
        )
      ORDER BY timestamp DESC
      LIMIT 1
    `);

    for (const taskRow of taskRows) {
      if (taskRow.tool_name === "Agent") {
        let runInBackground = false;
        try {
          const payload = JSON.parse(taskRow.payload) as { tool_input?: { run_in_background?: unknown } };
          runInBackground = payload.tool_input?.run_in_background === true;
        } catch {
          runInBackground = false;
        }
        if (!runInBackground) continue;

        // A normal Stop after Agent dispatch only means the parent prompt is idle;
        // the background agent remains active until TaskStop/subagent_completed.
        const closeRow = agentCloseStmt.get(slotNum, taskRow.timestamp) as { timestamp: string } | undefined;
        if (!closeRow) return { taskTs: taskRow.timestamp, lastStopTs: null, toolName: "Agent" };
        continue;
      }

      // Foreground Task dispatches are closed by the next Stop.
      const stopRow = stopStmt.get(slotNum, taskRow.timestamp) as { timestamp: string } | undefined;
      if (!stopRow) return { taskTs: taskRow.timestamp, lastStopTs: null, toolName: "Task" };
    }

    return null;
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

function normalizeOpsJob(row: Record<string, unknown>): OpsJobRecord {
  return {
    id: String(row.id),
    kind: String(row.kind),
    reason: String(row.reason),
    status: String(row.status) as OpsJobStatus,
    created_at: String(row.created_at),
    started_at: row.started_at === null || row.started_at === undefined ? null : String(row.started_at),
    finished_at: row.finished_at === null || row.finished_at === undefined ? null : String(row.finished_at),
    pid: row.pid === null || row.pid === undefined ? null : Number(row.pid),
    exit_code: row.exit_code === null || row.exit_code === undefined ? null : Number(row.exit_code),
    decision: row.decision === null || row.decision === undefined ? null : String(row.decision),
    result_reason: row.result_reason === null || row.result_reason === undefined ? null : String(row.result_reason),
    payload_bytes: row.payload_bytes === null || row.payload_bytes === undefined ? null : Number(row.payload_bytes),
    error: row.error === null || row.error === undefined ? null : String(row.error),
    stdout_path: row.stdout_path === null || row.stdout_path === undefined ? null : String(row.stdout_path),
    trace_path: row.trace_path === null || row.trace_path === undefined ? null : String(row.trace_path),
  };
}
