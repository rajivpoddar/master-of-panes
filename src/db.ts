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
  reason?:
    | "expected_epoch_required"
    | "epoch_mismatch"
    | "invalid_repository_id"
    | "invalid_branch_ref"
    | "invalid_assignment_metadata"
    | "target_already_assigned"
    | "slot_already_occupied"
    | "slot_not_occupied"
    | "slot_already_free_unverifiable"
    | "expected_tuple_required"
    | "expected_session_required"
    | "session_mismatch"
    | "branch_mismatch"
    | "observed_tuple_mismatch";
  owner_slots?: number[];
  owner_conflicts?: Array<{
    slot: number;
    matching_fields: Array<"issue" | "pr" | "branch_ref">;
  }>;
}

interface BranchIdentity {
  branch: string | null;
  branchRef: string | null;
}

interface ExpectedAssignmentTuple {
  pr: number | null;
  branchRef: string;
  headSha: string | null;
}

/** Complete occupied identity used by the rebind/release CAS boundary. */
export interface AssignmentTupleInput {
  repository_id: string | number | null;
  issue: number | null;
  pr: number | null;
  branch: string | null;
  head_sha: string | null;
  work_kind: string | null;
  handoff_id: string | null;
  claimed_at: string | null;
}

export interface AssignmentTuple {
  repository_id: string;
  issue: number | null;
  pr: number | null;
  branch: string | null;
  branch_ref: string | null;
  head_sha: string | null;
  work_kind: string | null;
  handoff_id: string | null;
  claimed_at: string | null;
}

const ASSIGNMENT_WORK_KINDS = new Set([
  "implementation",
  "rework",
  "repro",
  "review",
]);

function payloadReferencesIssueExactly(value: unknown, issueNumber: number): boolean {
  if (typeof value !== "string") return false;
  const references = [
    ...value.matchAll(/#(\d+)\b/g),
    ...value.matchAll(/\bissue\s*#?\s*(\d+)\b/gi),
  ];
  return references.some((match) => Number(match[1]) === issueNumber);
}

function isCodexReviewerAgentEvent(
  eventType: string,
  toolName: string,
  payload: string,
  issueNumber: number,
): boolean {
  if (eventType !== "PostToolUse" || (toolName !== "Agent" && toolName !== "Task")) {
    return false;
  }
  try {
    const parsed = JSON.parse(payload) as { tool_input?: Record<string, unknown> };
    const input = parsed.tool_input;
    if (!input || input.subagent_type !== "codex-code-reviewer") return false;
    return payloadReferencesIssueExactly(input.description, issueNumber)
      || payloadReferencesIssueExactly(input.prompt, issueNumber);
  } catch {
    return false;
  }
}

export function normalizeRepositoryId(value: unknown): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const repositoryId = String(value).trim();
  if (
    !repositoryId
    || repositoryId.length > 255
    || /[\u0000-\u001f\u007f\s]/.test(repositoryId)
  ) {
    return null;
  }
  return repositoryId;
}

export function normalizeBranchIdentity(
  value: string | null | undefined
): BranchIdentity | null {
  if (value === null || value === undefined || value.trim() === "") {
    return { branch: null, branchRef: null };
  }

  const raw = value.trim();
  const branch = raw.startsWith("refs/heads/")
    ? raw.slice("refs/heads/".length)
    : raw;
  if (
    (raw.startsWith("refs/") && !raw.startsWith("refs/heads/"))
    || !branch
    || branch.startsWith("/")
    || branch.endsWith("/")
    || branch.endsWith(".")
    || branch.includes("..")
    || branch.includes("//")
    || branch.includes("@{")
    || branch.includes("\\")
    || /[\u0000-\u0020\u007f~^:?*\[]/.test(branch)
  ) {
    return null;
  }
  return { branch, branchRef: `refs/heads/${branch}` };
}

export function normalizeAssignmentTuple(
  value: AssignmentTupleInput,
): AssignmentTuple | null {
  const repositoryId = normalizeRepositoryId(value.repository_id);
  if (!repositoryId) return null;

  const issue = value.issue === null
    ? null
    : Number.isInteger(value.issue) && Number(value.issue) > 0
      ? Number(value.issue)
      : null;
  if (value.issue !== null && issue === null) return null;

  const pr = value.pr === null
    ? null
    : Number.isInteger(value.pr) && Number(value.pr) > 0
      ? Number(value.pr)
      : null;
  if (value.pr !== null && pr === null) return null;

  const branchIdentity = normalizeBranchIdentity(value.branch);
  if (!branchIdentity) return null;

  const headSha = value.head_sha === null
    ? null
    : typeof value.head_sha === "string" && /^[0-9a-f]{40}$/i.test(value.head_sha)
      ? value.head_sha
      : null;
  if (value.head_sha !== null && headSha === null) return null;

  const workKind = value.work_kind === null
    ? null
    : typeof value.work_kind === "string" && ASSIGNMENT_WORK_KINDS.has(value.work_kind.trim())
      ? value.work_kind.trim()
      : null;
  if (value.work_kind !== null && workKind === null) return null;

  const handoffId = value.handoff_id === null
    ? null
    : typeof value.handoff_id === "string" && value.handoff_id.trim()
      ? value.handoff_id.trim()
      : null;
  if (value.handoff_id !== null && handoffId === null) return null;

  const claimedAt = value.claimed_at === null
    ? null
    : typeof value.claimed_at === "string" && value.claimed_at.trim()
      ? value.claimed_at.trim()
      : null;
  if (value.claimed_at !== null && claimedAt === null) return null;

  if ((workKind === null) !== (handoffId === null)) return null;
  if (pr !== null && (branchIdentity.branchRef === null || headSha === null)) return null;
  if (issue === null && pr === null) return null;

  return {
    repository_id: repositoryId,
    issue,
    pr,
    branch: branchIdentity.branch,
    branch_ref: branchIdentity.branchRef,
    head_sha: headSha,
    work_kind: workKind,
    handoff_id: handoffId,
    claimed_at: claimedAt,
  };
}

export function slotAssignmentTuple(slot: SlotState): AssignmentTuple | null {
  if (!slot.repository_id) return null;
  return {
    repository_id: slot.repository_id,
    issue: slot.issue,
    pr: slot.pr,
    branch: slot.branch,
    branch_ref: slot.branch_ref,
    head_sha: slot.head_sha,
    work_kind: slot.work_kind,
    handoff_id: slot.handoff_id,
    claimed_at: slot.claimed_at,
  };
}

export function assignmentTupleMatches(
  left: AssignmentTuple | null,
  right: AssignmentTuple | null,
): boolean {
  if (!left || !right) return false;
  return left.repository_id === right.repository_id
    && left.issue === right.issue
    && left.pr === right.pr
    && left.branch === right.branch
    && left.branch_ref === right.branch_ref
    && left.head_sha === right.head_sha
    && left.work_kind === right.work_kind
    && left.handoff_id === right.handoff_id
    && left.claimed_at === right.claimed_at;
}

export class MoPDatabase {
  private db: Database.Database;

  constructor(private readonly config: MoPConfig) {
    // Ensure data directory exists
    mkdirSync(dirname(config.dbPath), { recursive: true });

    this.db = new Database(config.dbPath);
    this.db.pragma("journal_mode = WAL"); // Better concurrent read perf
    this.db.pragma("foreign_keys = ON");
    try {
      this.init();
    } catch (error) {
      this.db.close();
      throw error;
    }
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
      CREATE INDEX IF NOT EXISTS idx_events_slot_type_tool_time
        ON events(slot, event_type, tool_name, timestamp DESC, id DESC);

      CREATE TABLE IF NOT EXISTS slots (
        slot INTEGER PRIMARY KEY,
        address TEXT NOT NULL,
        name TEXT,
        status TEXT NOT NULL DEFAULT 'free',
        occupied INTEGER NOT NULL DEFAULT 0,
        session_id TEXT,
        task TEXT,
        repository_id TEXT,
        issue INTEGER,
        branch TEXT,
        branch_ref TEXT,
        pr INTEGER,
        head_sha TEXT,
        assignment_epoch INTEGER NOT NULL DEFAULT 0,
        assigned_at TEXT,
        work_kind TEXT,
        handoff_id TEXT,
        claimed_at TEXT,
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
    if (!columns.some((c) => c.name === "repository_id")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN repository_id TEXT");
    }
    if (!columns.some((c) => c.name === "branch_ref")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN branch_ref TEXT");
    }
    if (!columns.some((c) => c.name === "work_kind")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN work_kind TEXT");
    }
    if (!columns.some((c) => c.name === "handoff_id")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN handoff_id TEXT");
    }
    if (!columns.some((c) => c.name === "claimed_at")) {
      this.db.exec("ALTER TABLE slots ADD COLUMN claimed_at TEXT");
    }

    this.migrateAssignmentIdentity();

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

  private migrateAssignmentIdentity(): void {
    const legacyRepositoryId = this.configuredLegacyRepositoryId();
    const legacyRows = this.db.prepare(`
      SELECT slot, occupied, repository_id, branch, branch_ref
      FROM slots
      WHERE (occupied = 1 AND (repository_id IS NULL OR repository_id = ''))
         OR (branch IS NOT NULL AND branch <> '' AND (branch_ref IS NULL OR branch_ref = ''))
    `).all() as Array<{
      slot: number;
      occupied: number;
      repository_id: string | null;
      branch: string | null;
      branch_ref: string | null;
    }>;

    this.db.transaction(() => {
      for (const row of legacyRows) {
        const branchIdentity = normalizeBranchIdentity(row.branch);
        if (!branchIdentity) {
          throw new Error(
            `cannot migrate slot ${row.slot}: invalid legacy branch ${JSON.stringify(row.branch)}`
          );
        }
        if (branchIdentity.branchRef && !row.branch_ref) {
          this.db.prepare(`
            UPDATE slots SET branch = ?, branch_ref = ? WHERE slot = ?
          `).run(branchIdentity.branch, branchIdentity.branchRef, row.slot);
        }
        if (row.occupied === 1 && !row.repository_id) {
          if (!legacyRepositoryId) {
            throw new Error(
              "MOP_LEGACY_REPOSITORY_ID is required to migrate occupied legacy slots"
            );
          }
          this.db.prepare(`
            UPDATE slots SET repository_id = ? WHERE slot = ?
          `).run(legacyRepositoryId, row.slot);
        }
      }
    })();

    this.db.exec(`
      CREATE UNIQUE INDEX IF NOT EXISTS ux_slots_occupied_repository_issue
        ON slots(repository_id, issue)
        WHERE occupied = 1
          AND repository_id IS NOT NULL
          AND repository_id <> ''
          AND issue IS NOT NULL
          AND issue > 0;

      CREATE UNIQUE INDEX IF NOT EXISTS ux_slots_occupied_repository_pr
        ON slots(repository_id, pr)
        WHERE occupied = 1
          AND repository_id IS NOT NULL
          AND repository_id <> ''
          AND pr IS NOT NULL
          AND pr > 0;

      CREATE UNIQUE INDEX IF NOT EXISTS ux_slots_occupied_repository_branch_ref
        ON slots(repository_id, branch_ref)
        WHERE occupied = 1
          AND repository_id IS NOT NULL
          AND repository_id <> ''
          AND branch_ref IS NOT NULL
          AND branch_ref <> '';
    `);
  }

  private configuredLegacyRepositoryId(): string | null {
    if (this.config.legacyRepositoryId === null) return null;
    const repositoryId = normalizeRepositoryId(this.config.legacyRepositoryId);
    if (!repositoryId) {
      throw new Error("MOP_LEGACY_REPOSITORY_ID is invalid");
    }
    return repositoryId;
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
      "name", "session_id", "task", "last_activity", "dnd", "idle", "activity",
      "active_turn_id", "active_turn_started_at", "active_turn_state", "last_meaningful_work_at",
    ];
    this.updateSlotFields(slot, updates, allowedFields);
  }

  private updateAssignmentState(
    slot: number,
    updates: Partial<SlotState>
  ): void {
    this.updateSlotFields(slot, updates, [
      "status", "occupied", "session_id", "task", "repository_id", "issue",
      "branch", "branch_ref", "pr", "head_sha", "assignment_epoch",
      "assigned_at", "work_kind", "handoff_id", "claimed_at", "dnd", "idle", "activity", "active_turn_id",
      "active_turn_started_at", "active_turn_state",
    ]);
  }

  private updateSlotFields(
    slot: number,
    updates: Partial<SlotState>,
    allowedFields: string[]
  ): void {
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

  /**
   * Internal final clear invoked only after NativeSlotReleaseCoordinator has
   * completed delivery and validated the owning pane's reset acknowledgement.
   * The complete tuple, owning session, and exact epoch are compared in the
   * same transaction; there is no numbered-slot epoch-only clear operation.
   */
  commitNativeRelease(
    slot: number,
    expectedEpoch: number,
    expectedSessionId: string,
    expectedTupleInput: AssignmentTupleInput,
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
    if (typeof expectedSessionId !== "string" || expectedSessionId.trim() === "") {
      const current = this.getSlot(slot);
      return {
        ok: false,
        conflict: true,
        assignment_epoch: current?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "expected_session_required",
      };
    }
    const expectedTuple = normalizeAssignmentTuple(expectedTupleInput);
    if (!expectedTuple) {
      const current = this.getSlot(slot);
      return {
        ok: false,
        conflict: true,
        assignment_epoch: current?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "expected_tuple_required",
      };
    }

    return this.db.transaction((): SlotMutationResult => {
      const current = this.getSlot(slot);
      const epoch = current?.assignment_epoch ?? 0;
      if (!current) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "epoch_mismatch" };
      }
      if (!current.occupied) {
        return {
          ok: false,
          conflict: true,
          assignment_epoch: epoch,
          idempotent: false,
          reason: "slot_already_free_unverifiable",
        };
      }
      if (epoch !== expectedEpoch) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "epoch_mismatch" };
      }
      if (current.session_id !== expectedSessionId) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "session_mismatch" };
      }
      if (!assignmentTupleMatches(slotAssignmentTuple(current), expectedTuple)) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "observed_tuple_mismatch" };
      }

      this.updateAssignmentState(slot, {
        status: "free" as SlotStatus,
        occupied: false,
        session_id: null,
        task: null,
        repository_id: null,
        issue: null,
        branch: null,
        branch_ref: null,
        pr: null,
        head_sha: null,
        assigned_at: null,
        work_kind: null,
        handoff_id: null,
        claimed_at: null,
        dnd: false,
        idle: true,
        activity: null,
        active_turn_id: null,
        active_turn_started_at: null,
        active_turn_state: "inactive",
        assignment_epoch: epoch + 1,
      });
      return { ok: true, conflict: false, assignment_epoch: epoch + 1, idempotent: false };
    })();
  }

  assignSlot(
    slot: number,
    task: string,
    repositoryId: string | number | null,
    issue: number | null,
    branch: string | null,
    sessionId: string | null,
    pr: number | null = null,
    headSha: string | null = null,
    expectedEpoch?: number,
    allowIssueClaimAdoption = false,
    expectedCurrentTuple: ExpectedAssignmentTuple | null = null,
    workKind: string | null = null,
    handoffId: string | null = null,
  ): SlotMutationResult {
    const current = this.getSlot(slot);
    if (!Number.isInteger(expectedEpoch)) {
      return {
        ok: false,
        conflict: true,
        assignment_epoch: current?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "expected_epoch_required",
      };
    }
    const normalizedRepositoryId = normalizeRepositoryId(repositoryId);
    if (!normalizedRepositoryId) {
      return {
        ok: false,
        conflict: true,
        assignment_epoch: current?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "invalid_repository_id",
      };
    }
    const branchIdentity = normalizeBranchIdentity(branch);
    if (!branchIdentity) {
      return {
        ok: false,
        conflict: true,
        assignment_epoch: current?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "invalid_branch_ref",
      };
    }
    const normalizedIssue = Number.isInteger(issue) && Number(issue) > 0
      ? Number(issue)
      : null;
    const normalizedPr = Number.isInteger(pr) && Number(pr) > 0
      ? Number(pr)
      : null;
    const normalizedWorkKind = typeof workKind === "string" ? workKind.trim() : workKind;
    const normalizedHandoffId = typeof handoffId === "string" ? handoffId.trim() : handoffId;
    if (
      (workKind !== null && typeof workKind !== "string")
      || (handoffId !== null && typeof handoffId !== "string")
      || (normalizedWorkKind !== null && normalizedWorkKind.length === 0)
      || (normalizedWorkKind !== null && !ASSIGNMENT_WORK_KINDS.has(normalizedWorkKind))
      || (normalizedHandoffId !== null && normalizedHandoffId.length === 0)
      || ((normalizedWorkKind === null) !== (normalizedHandoffId === null))
    ) {
      return {
        ok: false,
        conflict: true,
        assignment_epoch: current?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "invalid_assignment_metadata",
      };
    }

    return this.db.transaction((): SlotMutationResult => {
      const current = this.getSlot(slot);
      const epoch = current?.assignment_epoch ?? 0;
      if (!current || epoch !== expectedEpoch) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "epoch_mismatch" };
      }
      const metadataMatches = normalizedWorkKind === null
        ? current.work_kind === null && current.handoff_id === null
        : current.work_kind === normalizedWorkKind
          && current.handoff_id === normalizedHandoffId;
      const idempotent = current.occupied
        && current.repository_id === normalizedRepositoryId
        && current.issue === normalizedIssue
        && current.pr === normalizedPr
        && current.branch_ref === branchIdentity.branchRef
        && current.head_sha === headSha
        && metadataMatches;
      const observedTupleMatches = expectedCurrentTuple !== null
        && current.pr === expectedCurrentTuple.pr
        && current.branch_ref === expectedCurrentTuple.branchRef
        && current.head_sha === expectedCurrentTuple.headSha;
      // A non-idempotent adoption is an issue-only -> PR identity bind: it may
      // start ONLY from an issue-only claim (pr === null). A PR-bound row can
      // be re-adopted only idempotently (same PR); a caller supplying a NEW PR
      // for an already PR-bound observed tuple must fail closed, never rewrite
      // the PR at unchanged epoch.
      const issueClaimAdoption = allowIssueClaimAdoption
        && current.occupied
        && current.pr === null
        && observedTupleMatches
        && current.repository_id === normalizedRepositoryId
        && current.issue === normalizedIssue
        && normalizedIssue !== null
        && normalizedPr !== null
        && branchIdentity.branchRef !== null
        && typeof headSha === "string"
        && /^[0-9a-f]{40}$/i.test(headSha)
        && !current.dnd;
      if (allowIssueClaimAdoption && !current.occupied) {
        return {
          ok: false,
          conflict: true,
          assignment_epoch: epoch,
          idempotent: false,
          reason: "slot_not_occupied",
        };
      }
      if (
        allowIssueClaimAdoption
        && current.occupied
        && !observedTupleMatches
      ) {
        return {
          ok: false,
          conflict: true,
          assignment_epoch: epoch,
          idempotent: false,
          reason: "observed_tuple_mismatch",
        };
      }
      if (current.occupied) {
        if (idempotent) {
          return {
            ok: true,
            conflict: false,
            assignment_epoch: epoch,
            idempotent: true,
          };
        }
        if (!issueClaimAdoption) {
          return {
            ok: false,
            conflict: true,
            assignment_epoch: epoch,
            idempotent: false,
            reason: "slot_already_occupied",
            owner_slots: [slot],
          };
        }
      }

      const owners = this.db.prepare(`
        SELECT slot, issue, pr, branch_ref
        FROM slots
        WHERE occupied = 1
          AND repository_id = ?
          AND slot != ?
          AND (
            (? IS NOT NULL AND pr = ?)
            OR (? IS NOT NULL AND issue = ?)
            OR (? IS NOT NULL AND branch_ref = ?)
          )
        ORDER BY slot
      `).all(
        normalizedRepositoryId,
        slot,
        normalizedPr, normalizedPr,
        normalizedIssue, normalizedIssue,
        branchIdentity.branchRef, branchIdentity.branchRef
      ) as Array<{
        slot: number;
        issue: number | null;
        pr: number | null;
        branch_ref: string | null;
      }>;
      if (owners.length > 0) {
        const ownerConflicts = owners.map((owner) => ({
          slot: owner.slot,
          matching_fields: [
            ...(normalizedIssue !== null && owner.issue === normalizedIssue
              ? ["issue" as const]
              : []),
            ...(normalizedPr !== null && owner.pr === normalizedPr
              ? ["pr" as const]
              : []),
            ...(branchIdentity.branchRef !== null
              && owner.branch_ref === branchIdentity.branchRef
              ? ["branch_ref" as const]
              : []),
          ],
        }));
        return {
          ok: false,
          conflict: true,
          assignment_epoch: epoch,
          idempotent: false,
          reason: "target_already_assigned",
          owner_slots: ownerConflicts.map((owner) => owner.slot),
          owner_conflicts: ownerConflicts,
        };
      }

      const nextEpoch = epoch + 1;
      // An issue-claim adoption is an identity bind (pr + head onto the same
      // slot/issue/branch), not a new assignment: the assignment epoch is
      // preserved so downstream pickup validation sees no tuple drift.
      const boundEpoch = issueClaimAdoption ? epoch : nextEpoch;
      const assignmentTime = new Date().toISOString();
      const claimedAt = issueClaimAdoption && current.claimed_at
        ? current.claimed_at
        : assignmentTime;
      const assignmentWorkKind = issueClaimAdoption && normalizedWorkKind === null
        ? current.work_kind
        : normalizedWorkKind;
      const assignmentHandoffId = issueClaimAdoption && normalizedHandoffId === null
        ? current.handoff_id
        : normalizedHandoffId;
      try {
        this.updateAssignmentState(slot, {
          status: "active" as SlotStatus,
          occupied: true,
          session_id: issueClaimAdoption ? current.session_id : sessionId,
          task,
          repository_id: normalizedRepositoryId,
          issue: normalizedIssue,
          branch: branchIdentity.branch,
          branch_ref: branchIdentity.branchRef,
          pr: normalizedPr,
          head_sha: headSha,
          assignment_epoch: boundEpoch,
          assigned_at: assignmentTime,
          work_kind: assignmentWorkKind,
          handoff_id: assignmentHandoffId,
          claimed_at: claimedAt,
          dnd: false,
        });
      } catch (error) {
        if (
          error instanceof Error
          && error.message.includes("UNIQUE constraint failed")
        ) {
          const racedOwners = this.db.prepare(`
            SELECT slot, issue, pr, branch_ref
            FROM slots
            WHERE occupied = 1
              AND repository_id = ?
              AND slot != ?
              AND (
                (? IS NOT NULL AND pr = ?)
                OR (? IS NOT NULL AND issue = ?)
                OR (? IS NOT NULL AND branch_ref = ?)
              )
            ORDER BY slot
          `).all(
            normalizedRepositoryId,
            slot,
            normalizedPr, normalizedPr,
            normalizedIssue, normalizedIssue,
            branchIdentity.branchRef, branchIdentity.branchRef
          ) as Array<{
            slot: number;
            issue: number | null;
            pr: number | null;
            branch_ref: string | null;
          }>;
          const ownerConflicts = racedOwners.map((owner) => ({
            slot: owner.slot,
            matching_fields: [
              ...(normalizedIssue !== null && owner.issue === normalizedIssue
                ? ["issue" as const]
                : []),
              ...(normalizedPr !== null && owner.pr === normalizedPr
                ? ["pr" as const]
                : []),
              ...(branchIdentity.branchRef !== null
                && owner.branch_ref === branchIdentity.branchRef
                ? ["branch_ref" as const]
                : []),
            ],
          }));
          return {
            ok: false,
            conflict: true,
            assignment_epoch: epoch,
            idempotent: false,
            reason: "target_already_assigned",
            owner_slots: ownerConflicts.map((owner) => owner.slot),
            owner_conflicts: ownerConflicts,
          };
        }
        throw error;
      }
      return {
        ok: true,
        conflict: false,
        assignment_epoch: boundEpoch,
        idempotent: false,
      };
    })();
  }

  adoptIssueClaimSlot(
    slot: number,
    task: string,
    repositoryId: string | number | null,
    issue: number | null,
    branch: string | null,
    pr: number | null,
    headSha: string | null,
    expectedCurrentPr: number | null,
    expectedCurrentBranchRef: string,
    expectedCurrentHeadSha: string | null,
    expectedEpoch?: number
  ): SlotMutationResult {
    return this.assignSlot(
      slot,
      task,
      repositoryId,
      issue,
      branch,
      null,
      pr,
      headSha,
      expectedEpoch,
      true,
      {
        pr: expectedCurrentPr,
        branchRef: expectedCurrentBranchRef,
        headSha: expectedCurrentHeadSha,
      },
    );
  }

  /**
   * Rebind an occupied assignment through the existing assignment authority.
   * The expected tuple is checked under the SQLite transaction; a successful
   * rebind advances the ownership epoch exactly once.  A retry after a lost
   * response is acknowledged only when the desired tuple is already present at
   * the next epoch.
   */
  rebindSlot(
    slot: number,
    expectedEpoch: number,
    expectedTupleInput: AssignmentTupleInput,
    desiredTupleInput: AssignmentTupleInput,
    task?: string | null,
  ): SlotMutationResult {
    const currentBeforeValidation = this.getSlot(slot);
    const expectedTuple = normalizeAssignmentTuple(expectedTupleInput);
    const desiredTuple = normalizeAssignmentTuple(desiredTupleInput);
    if (!expectedTuple || !desiredTuple) {
      return {
        ok: false,
        conflict: true,
        assignment_epoch: currentBeforeValidation?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "observed_tuple_mismatch",
      };
    }
    if (!Number.isInteger(expectedEpoch)) {
      return {
        ok: false,
        conflict: true,
        assignment_epoch: currentBeforeValidation?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "expected_epoch_required",
      };
    }
    if (expectedTuple.repository_id !== desiredTuple.repository_id) {
      return {
        ok: false,
        conflict: true,
        assignment_epoch: currentBeforeValidation?.assignment_epoch ?? 0,
        idempotent: false,
        reason: "invalid_repository_id",
      };
    }

    return this.db.transaction((): SlotMutationResult => {
      const current = this.getSlot(slot);
      const epoch = current?.assignment_epoch ?? 0;
      const currentTuple = current ? slotAssignmentTuple(current) : null;
      if (!current || !current.occupied) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "slot_not_occupied" };
      }

      // Exact replay after a committed write, including a client-side lost
      // response, must not issue a second assignment write.
      if (
        epoch === expectedEpoch + 1
        && assignmentTupleMatches(currentTuple, desiredTuple)
      ) {
        return { ok: true, conflict: false, assignment_epoch: epoch, idempotent: true };
      }
      if (epoch !== expectedEpoch) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "epoch_mismatch" };
      }
      if (!assignmentTupleMatches(currentTuple, expectedTuple)) {
        return { ok: false, conflict: true, assignment_epoch: epoch, idempotent: false, reason: "observed_tuple_mismatch" };
      }
      if (assignmentTupleMatches(currentTuple, desiredTuple)) {
        return { ok: true, conflict: false, assignment_epoch: epoch, idempotent: true };
      }

      const owners = this.db.prepare(`
        SELECT slot, issue, pr, branch_ref
        FROM slots
        WHERE occupied = 1
          AND repository_id = ?
          AND slot != ?
          AND (
            (? IS NOT NULL AND pr = ?)
            OR (? IS NOT NULL AND issue = ?)
            OR (? IS NOT NULL AND branch_ref = ?)
          )
        ORDER BY slot
      `).all(
        desiredTuple.repository_id,
        slot,
        desiredTuple.pr, desiredTuple.pr,
        desiredTuple.issue, desiredTuple.issue,
        desiredTuple.branch_ref, desiredTuple.branch_ref,
      ) as Array<{ slot: number; issue: number | null; pr: number | null; branch_ref: string | null }>;
      if (owners.length > 0) {
        return {
          ok: false,
          conflict: true,
          assignment_epoch: epoch,
          idempotent: false,
          reason: "target_already_assigned",
          owner_slots: owners.map((owner) => owner.slot),
          owner_conflicts: owners.map((owner) => ({
            slot: owner.slot,
            matching_fields: [
              ...(desiredTuple.issue !== null && owner.issue === desiredTuple.issue ? ["issue" as const] : []),
              ...(desiredTuple.pr !== null && owner.pr === desiredTuple.pr ? ["pr" as const] : []),
              ...(desiredTuple.branch_ref !== null && owner.branch_ref === desiredTuple.branch_ref ? ["branch_ref" as const] : []),
            ],
          })),
        };
      }

      try {
        this.updateAssignmentState(slot, {
          status: "active" as SlotStatus,
          occupied: true,
          task: task ?? current.task,
          repository_id: desiredTuple.repository_id,
          issue: desiredTuple.issue,
          branch: desiredTuple.branch,
          branch_ref: desiredTuple.branch_ref,
          pr: desiredTuple.pr,
          head_sha: desiredTuple.head_sha,
          assignment_epoch: epoch + 1,
          assigned_at: current.assigned_at,
          work_kind: desiredTuple.work_kind,
          handoff_id: desiredTuple.handoff_id,
          claimed_at: desiredTuple.claimed_at,
          dnd: current.dnd,
        });
      } catch (error) {
        if (error instanceof Error && error.message.includes("UNIQUE constraint failed")) {
          return {
            ok: false,
            conflict: true,
            assignment_epoch: epoch,
            idempotent: false,
            reason: "target_already_assigned",
          };
        }
        throw error;
      }

      return { ok: true, conflict: false, assignment_epoch: epoch + 1, idempotent: false };
    })();
  }

  /**
   * Synchronize the observed checkout head for the branch already owned by a
   * slot. This is not a new assignment: the ownership epoch and active turn
   * remain unchanged.
   */
  syncSlotCheckout(
    slot: number,
    branch: string,
    headSha: string,
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
        return {
          ok: false,
          conflict: true,
          assignment_epoch: epoch,
          idempotent: false,
          reason: "epoch_mismatch",
        };
      }
      if (!current.occupied) {
        return {
          ok: false,
          conflict: true,
          assignment_epoch: epoch,
          idempotent: false,
          reason: "slot_not_occupied",
        };
      }
      const branchIdentity = normalizeBranchIdentity(branch);
      if (!branchIdentity || current.branch_ref !== branchIdentity.branchRef) {
        return {
          ok: false,
          conflict: true,
          assignment_epoch: epoch,
          idempotent: false,
          reason: "branch_mismatch",
        };
      }

      const idempotent = current.head_sha === headSha;
      if (!idempotent) {
        this.updateAssignmentState(slot, { head_sha: headSha });
      }
      return {
        ok: true,
        conflict: false,
        assignment_epoch: epoch,
        idempotent,
      };
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
      this.updateSlot(slot, {
        active_turn_state: "indeterminate",
        idle: false,
      });
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
    enqueuedAt?: string,
  ): void {
    const stmt = this.db.prepare(`
      INSERT INTO pm_pending_events (slot, event_type, payload, enqueued_at)
      VALUES (?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%f', 'now')))
      ON CONFLICT(slot, event_type) DO UPDATE SET
        payload = excluded.payload,
        enqueued_at = excluded.enqueued_at
    `);
    stmt.run(slot, eventType, payload, enqueuedAt ?? null);
  }

  deletePendingPMEvent(slot: number, eventType: string, enqueuedAt?: string): number {
    const result = enqueuedAt === undefined
      ? this.db.prepare(`
          DELETE FROM pm_pending_events
          WHERE slot = ? AND event_type = ?
        `).run(slot, eventType)
      : this.db.prepare(`
          DELETE FROM pm_pending_events
          WHERE slot = ? AND event_type = ? AND enqueued_at = ?
        `).run(slot, eventType, enqueuedAt);
    return result.changes;
  }

  /**
   * Drain all queued PM-bound events. Slot-state transitions are coalesced to
   * the most relevant winner per slot; freeform/cadence/ops occurrences are
   * preserved independently because they carry distinct actionable content.
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
   *                          rows are retained as individual occurrences.
   *
   *   When a higher-priority signal exists for a slot, all lower-priority
   *   lower-priority slot-state rows are dropped. Within a tie, the
   *   most-recently-enqueued slot-state row wins; non-state rows are retained.
   *
   *   Free-form/cadence/ops rows do not mix with slot-state coalescing and
   *   remain queued even when a slot-state winner exists for that slot.
   *
   * Returns the deduped rows. Caller injects each row into PM.
   */
  drainPendingPMEvents(): Array<{ slot: number; event_type: string; payload: string | null; enqueued_at: string }> {
    const rows = this.db.prepare(`
      SELECT slot, event_type, payload, enqueued_at
      FROM pm_pending_events
      ORDER BY slot ASC, enqueued_at ASC
    `).all() as Array<{ slot: number; event_type: string; payload: string | null; enqueued_at: string }>;

    const slotStateTypes = new Set(["slot-blocked", "slot-idle", "slot-active", "check-slot"]);
    const isSlotState = (eventType: string): boolean => slotStateTypes.has(eventType);

    // Priority: lower number = higher priority among slot-state rows only.
    const priorityOf = (eventType: string): number => {
      if (eventType === "slot-blocked") return 1;
      if (eventType === "slot-idle") return 2;
      if (eventType === "slot-active") return 3;
      if (eventType === "check-slot") return 4;
      return Number.MAX_SAFE_INTEGER;
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
      const stateRows = slotRows.filter((row) => isSlotState(row.event_type));
      const otherRows = slotRows.filter((row) => !isSlotState(row.event_type));
      if (stateRows.length > 0) {
        stateRows.sort((a, b) => {
          const pa = priorityOf(a.event_type);
          const pb = priorityOf(b.event_type);
          if (pa !== pb) return pa - pb;
          return b.enqueued_at.localeCompare(a.enqueued_at);
        });
        out.push(stateRows[0]);
      }
      out.push(...otherRows);
    }

    // Keep selected rows until the relay records a successful submit. Only
    // superseded slot-state rows are intentionally deleted now; every
    // freeform/cadence/ops occurrence remains durable for delivery.
    const selected = new Set(out.map((row) => `${row.slot}\u0000${row.event_type}`));
    for (const row of rows) {
      if (isSlotState(row.event_type) && !selected.has(`${row.slot}\u0000${row.event_type}`)) {
        this.deletePendingPMEvent(row.slot, row.event_type, row.enqueued_at);
      }
    }
    return out;
  }

  /**
   * Read the latest durable delivery outcome for one queue occurrence. The
   * existing append-only event log is the recovery boundary: a started
   * occurrence with no terminal outcome is ambiguous after a crash and must
   * not replay its submit key. The query is exact and unbounded by the
   * diagnostic read horizon, so unrelated event volume cannot make an
   * ambiguous occurrence replayable.
   */
  getPMQueueDeliveryState(
    slot: number,
    eventType: string,
    message: string,
    enqueuedAt?: string,
  ): "none" | "started" | "deferred" | "ambiguous" | "delivered" {
    const rows = this.db.prepare(`
      SELECT id, event_type, payload
      FROM events
      WHERE slot = ?
        AND event_type IN ('pm_queue_delivery_started', 'pm_queue_delivery_deferred', 'pm_queue_delivered')
        AND json_extract(payload, '$.event_type') = ?
        AND json_extract(payload, '$.message') = ?
        AND (? IS NULL OR json_extract(payload, '$.enqueued_at') = ?)
      ORDER BY id DESC
      LIMIT 1
    `).all(slot, eventType, message, enqueuedAt ?? null, enqueuedAt ?? null) as Array<{
      event_type: string;
      payload: string;
    }>;
    if (rows.length === 0) return "none";
    const event = rows[0];
    if (event.event_type === "pm_queue_delivery_started") return "started";
    if (event.event_type === "pm_queue_delivered") return "delivered";
    try {
      const payload = JSON.parse(event.payload) as { ambiguous?: boolean };
      if (payload.ambiguous === true) return "ambiguous";
    } catch {
      // A malformed diagnostic is not evidence of delivery; retryable
      // deferred remains the conservative interpretation.
    }
    return "deferred";
  }

  hasPMQueueDelivery(slot: number, eventType: string, message: string, enqueuedAt?: string): boolean {
    return this.getPMQueueDeliveryState(slot, eventType, message, enqueuedAt) === "delivered";
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
      WHERE timestamp > strftime('%Y-%m-%dT%H:%M:%f', 'now', '-' || ? || ' minutes')
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

    // Read the small, indexed recent tool-event set, then parse Agent/Task
    // payloads so reviewer identity and issue binding are exact rather than a
    // forgeable prose substring. Existing Skill/Bash matching stays intact.
    const stmt = this.db.prepare(`
      SELECT slot, timestamp, event_type, tool_name, payload
      FROM events
      WHERE timestamp > strftime('%Y-%m-%dT%H:%M:%f', 'now', '-' || ? || ' minutes')
        AND tool_name IN ('Skill', 'Bash', 'Agent', 'Task')
      ORDER BY timestamp DESC
    `);

    const rows = stmt.all(windowMinutes) as Array<{
      slot: number;
      timestamp: string;
      event_type: string;
      tool_name: string;
      payload: string;
    }>;
    const row = rows.find((candidate) => {
      if (candidate.tool_name === "Skill") {
        return candidate.payload.includes(issueStr)
          && (/codex.*review/i.test(candidate.payload) || /zen.*review/i.test(candidate.payload));
      }
      if (candidate.tool_name === "Bash") {
        return candidate.payload.includes(issueStr)
          && (candidate.payload.includes("codex exec") || /codex-companion.*review/i.test(candidate.payload));
      }
      return isCodexReviewerAgentEvent(
        candidate.event_type,
        candidate.tool_name,
        candidate.payload,
        issueNumber,
      );
    });

    if (!row) {
      return { found: false, method: null, timestamp: null, slot: null, details: null };
    }

    // Determine method from the match
    let method = "unknown";
    const payload = row.payload.toLowerCase();
    if (row.tool_name === "Agent" || row.tool_name === "Task") method = "codex-agent";
    else if (payload.includes("codex-app")) method = "codex-app";
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
