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
export type HookType = "PreToolUse" | "PostToolUse" | "Notification" | "Stop";

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
  /** Notification type (for Notification hooks) */
  notification_type?: string;
  /** Stop reason (for Stop hooks) */
  stop_reason?: string;
  /** Transcript of the conversation (for Stop hooks) */
  transcript?: string;
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
