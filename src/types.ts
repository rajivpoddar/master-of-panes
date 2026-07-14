/**
 * MoP (Master of Panes) — Core type definitions
 *
 * Three concerns:
 * 1. HTTP hook events from Claude Code slots
 * 2. Slot state tracking
 * 3. MCP tool interfaces for PM queries
 */

// ─── Claude Code HTTP Hook Events ────────────────────────────

/** Claude Code hook event types */
export type HookType =
  | "PreToolUse"
  | "PostToolUse"
  | "Notification"
  | "Stop"
  | "UserPromptSubmit"
  | "SubagentStop"
  | "PreCompact"
  | "PostCompact"
  | "SessionStart"
  | "SessionEnd";

/** Incoming HTTP hook payload from Claude Code */
export interface HookPayload {
  /** Which hook fired */
  type: HookType;
  /** The tool that was used (for PreToolUse/PostToolUse) */
  tool_name?: string;
  /** Tool input parameters */
  tool_input?: Record<string, unknown>;
  /** Tool output (for PostToolUse) */
  tool_output?: string;
  /** Session ID of the Claude Code instance */
  session_id?: string;
  /** Current working directory of the Claude Code instance */
  cwd?: string;
  /** Path to the session JSONL transcript */
  transcript_path?: string;
  /** Notification type (for Notification hooks) */
  notification_type?: string;
  /** Stop reason (for Stop hooks) */
  stop_reason?: string;
  /** Transcript of the conversation (for Stop hooks) */
  transcript?: string;
  /** SessionStart source: "startup" | "resume" | "clear" | "compact" */
  source?: string;
  /** PreCompact / PostCompact trigger: "manual" | "auto" */
  trigger?: string;
  /** PostCompact summary text */
  compact_summary?: string;
}

/** Response to Claude Code hook — can modify behavior */
export interface HookResponse {
  /** Whether to block the tool call (PreToolUse only) */
  blocked?: boolean;
  /** Reason for blocking */
  reason?: string;
  /** Message to inject into conversation */
  message?: string;
}

// ─── Slot State ──────────────────────────────────────────────

export type SlotStatus = "free" | "active" | "dnd";

export interface SlotState {
  /** Slot number (1-4) */
  slot: number;
  /** Tmux pane address */
  address: string;
  /** Human-readable name for this slot (e.g., "Rohini", "Hasta") */
  name: string | null;
  /** Current status */
  status: SlotStatus;
  /** Whether the slot is occupied */
  occupied: boolean;
  /** Claude Code session ID */
  session_id: string | null;
  /** Current task description */
  task: string | null;
  /** GitHub issue number */
  issue: number | null;
  /** Git branch name */
  branch: string | null;
  /** PR number if one exists */
  pr: number | null;
  /** Full commit SHA for the assigned PR head, when applicable */
  head_sha: string | null;
  /** Monotonic ownership generation; new assignment tuples increment it */
  assignment_epoch: number;
  /** When the slot was assigned */
  assigned_at: string | null;
  /** Last activity timestamp */
  last_activity: string;
  /** Do not disturb flag */
  dnd: boolean;
  /** Whether the slot is idle (at prompt) vs busy (executing) */
  idle: boolean;
  /** Current activity type (testing, coding, committing, etc.) — null if unknown */
  activity: string | null;
  /** Durable hook-derived turn identifier */
  active_turn_id: string | null;
  /** When the current agent turn started */
  active_turn_started_at: string | null;
  /** Hook-derived turn state; indeterminate is fail-closed for release */
  active_turn_state: "active" | "inactive" | "indeterminate";
  /** Last event proven to represent meaningful work rather than notification noise */
  last_meaningful_work_at: string | null;
}

// ─── Event Log ───────────────────────────────────────────────

export interface EventLogEntry {
  id: number;
  timestamp: string;
  slot: number;
  event_type: string;
  hook_type: HookType | null;
  tool_name: string | null;
  payload: string; // JSON string
  processed: boolean;
}

// ─── Ops Jobs ───────────────────────────────────────────────

export type OpsJobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "skipped"
  | "failed"
  | "timed_out"
  | "cancelled";

export interface OpsJobRecord {
  id: string;
  kind: string;
  reason: string;
  status: OpsJobStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  pid: number | null;
  exit_code: number | null;
  decision: string | null;
  result_reason: string | null;
  payload_bytes: number | null;
  error: string | null;
  stdout_path: string | null;
  trace_path: string | null;
}

// ─── MCP Tool Results ────────────────────────────────────────

export interface AllSlotsResult {
  slots: SlotState[];
  summary: string;
}

export interface SlotHistoryResult {
  slot: number;
  events: EventLogEntry[];
}

// ─── Server Config ───────────────────────────────────────────

export interface MoPConfig {
  /** HTTP server port for hook receiver */
  httpPort: number;
  /** MCP server transport type */
  mcpTransport: "stdio" | "sse";
  /** SQLite database path */
  dbPath: string;
  /** PM tmux pane address */
  pmPaneAddress: string;
  /** Number of dev slots */
  slotCount: number;
}

export const DEFAULT_CONFIG: MoPConfig = {
  httpPort: 3100,
  mcpTransport: "stdio",
  dbPath: "./data/mop.db",
  pmPaneAddress: "0:0.0",
  slotCount: 4,
};
