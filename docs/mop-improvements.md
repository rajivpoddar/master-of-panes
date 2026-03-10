# Spec: MoP Improvements — Hooks Expansion, Pane Logging, Stuck Detection

**Author:** Dhruva (PM)
**Date:** 2026-03-10
**Status:** Implemented (all 4 phases live)
**Source:** Rajiv directive in Slack thread 1773116295.196179

---

## Problem

MoP currently uses 4 of 8 available Claude Code hooks. The unused hooks represent missed opportunities for real-time state management. Additionally, all pane output inspection depends on `tmux capture-pane` (lossy, polling-based) and `is-active.sh` (ANSI chevron color parsing — hacky, 10s timeout). These limitations cause:

1. **Blind spots** — PM only knows when a slot *stops* (Stop hook), not when it *starts working* or *what it's doing*
2. **Lost output** — `capture-pane` only returns the visible scrollback buffer; content that scrolled past is gone
3. **No stuck detection** — if a slot hangs mid-execution, PM discovers it hours later during heartbeat
4. **Tmux dependency** — `is-active.sh` parses ANSI escape codes for chevron color, which is fragile and slow

## Goals

1. **Use all 8 hooks** for comprehensive state tracking
2. **Eliminate `tmux capture-pane`** with persistent pane logging via `pipe-pane`
3. **Detect stuck slots** automatically via log mtime monitoring
4. **Enrich slot state** with activity type (testing, committing, verifying) without tmux parsing

---

## Pillar 1: Hooks Expansion

### Current Hook Usage (4 of 8)

| Hook | Handler | What it does |
|------|---------|-------------|
| **Stop** | `handleStop()` | Detects idle → notifies PM, auto-releases post-PR |
| **PostToolUse** | `handlePostToolUse()` | Detects plan-ready (Write to `plans/*.md`) |
| **PreToolUse** | `handlePreToolUse()` | Pass-through (placeholder) |
| **Notification** | `handleNotification()` | Logs notification type (no routing) |

### New Hook Handlers

#### 1.1 PostToolUse — Activity Tracking (Priority: HIGH)

**Problem:** PM only knows a slot is "active" (occupied=true, idle=false). It doesn't know *what* the slot is doing.

**Solution:** Parse PostToolUse events for Bash/Write/Edit tool calls and classify activity:

```typescript
// In handlePostToolUse(), after existing plan-ready detection:

if (payload.tool_name === "Bash") {
  const cmd = (payload.tool_input as Record<string, string>)?.command ?? "";
  const activity = classifyBashCommand(cmd);
  if (activity) {
    this.db.updateSlot(slotNum, { activity });
    this.db.logEvent(slotNum, `activity_${activity}`, "PostToolUse", "Bash", {
      command: cmd.slice(0, 200), // Truncate for storage
      activity,
    });
  }
}

if (payload.tool_name === "Write" || payload.tool_name === "Edit") {
  const filePath = (payload.tool_input as Record<string, string>)?.file_path ?? "";
  this.db.updateSlot(slotNum, { activity: "coding" });
  this.db.logEvent(slotNum, "activity_coding", "PostToolUse", payload.tool_name, {
    file: filePath,
  });
}
```

**Activity classifier:**

```typescript
function classifyBashCommand(cmd: string): string | null {
  // Test patterns (order matters — most specific first)
  if (/vitest\s+run|bun\s+run\s+test/.test(cmd)) return "testing";
  if (/tsc\s+--noEmit/.test(cmd)) return "type_checking";
  if (/bun\s+lint|eslint/.test(cmd)) return "linting";
  if (/git\s+commit/.test(cmd)) return "committing";
  if (/git\s+push/.test(cmd)) return "pushing";
  if (/git\s+(checkout|branch|switch)/.test(cmd)) return "branching";
  if (/gh\s+pr\s+create/.test(cmd)) return "creating_pr";
  if (/modal\s+deploy/.test(cmd)) return "deploying_modal";
  if (/npx\s+convex\s+deploy/.test(cmd)) return "deploying_convex";
  // Generic coding signals
  if (/sg\s+--lang|grep|find/.test(cmd)) return "exploring";
  return null; // Don't classify every command — only significant ones
}
```

**Schema change** — add `activity` column to slots table:

```sql
ALTER TABLE slots ADD COLUMN activity TEXT;
-- Values: testing, type_checking, linting, committing, pushing, branching,
--         creating_pr, deploying_modal, deploying_convex, exploring, coding, null
```

**Impact:** `mop_slot_status` and `mop_all_slots` now return `activity` field. PM heartbeat and idle handler get richer context. Eliminates need for `tmux capture-pane` to guess what a slot is doing.

#### 1.2 SubagentStop — Background Agent Completion Routing (Priority: HIGH)

**Problem:** When a slot's background agent (code-explorer, editor-specialist, etc.) completes, there's no hook-based notification. PM discovers it via task notification or manually checks.

**Note:** SubagentStop is NOT currently a standard Claude Code hook. It would need to be implemented as a custom Notification hook variant, or detected via PostToolUse on the Agent tool's return. For now, we implement this as **PostToolUse detection of Agent tool completion**:

```typescript
// In handlePostToolUse():
if (payload.tool_name === "Agent" && payload.tool_output) {
  // Agent tool returned — a subagent completed
  this.db.logEvent(slotNum, "subagent_completed", "PostToolUse", "Agent", {
    output_preview: (payload.tool_output ?? "").slice(0, 500),
  });

  // Notify PM that a subagent finished in this slot
  this.relay.notifySubagentComplete(slotNum);
}
```

**New relay method:**

```typescript
notifySubagentComplete(slotNum: number): void {
  const time = new Date().toLocaleTimeString("en-US", { hour12: false });
  const slot = this.db.getSlot(slotNum);
  const taskPart = slot?.task ? ` — ${truncate(slot.task, 40)}` : "";

  const comment = `# subagent completed in slot ${slotNum}${taskPart} | ${time}`;
  // Don't auto-trigger a slash command — just inform PM
  this.injectToPM(comment);
}
```

**Impact:** PM gets notified when a slot's background agent finishes, enabling faster reaction to plan-ready, PR creation, or CI results.

#### 1.3 PreCompact — State Persistence Before Memory Loss (Priority: MEDIUM)

**Problem:** When a slot's context compacts, it may lose track of undelivered notifications, mid-transition state, or debugging context.

**Note:** PreCompact is NOT currently a standard Claude Code hook. This would need to be a Notification hook variant or a new hook type. For now, we implement this as **Notification hook handling for `autocompact` notification type**:

```typescript
// In handleNotification():
if (payload.notification_type === "autocompact") {
  const slot = this.db.getSlot(slotNum);

  // Persist critical state that might be lost
  this.db.logEvent(slotNum, "pre_compact", "Notification", null, {
    task: slot?.task,
    issue: slot?.issue,
    branch: slot?.branch,
    activity: slot?.activity,
    context_warning: true,
  });

  // If slot is mid-task with no PR yet, warn PM
  if (slot?.occupied && !slot.pr) {
    const comment = `# slot ${slotNum} compacting — mid-task, no PR yet | issue: #${slot.issue || "?"}`;
    this.relay.injectToPM(comment);
  }
}
```

**Impact:** PM gets early warning when slots are running low on context, enabling proactive intervention (send `/compact` guidance, split work, or collect progress before context loss).

#### 1.4 UserPromptSubmit — PM Command Guard (Priority: LOW)

**Problem:** PM sends a command to a slot (via `mop_send_to_slot`) while MoP is simultaneously auto-releasing or reassigning the slot. Race condition.

**Note:** UserPromptSubmit fires when a user submits a prompt, but MoP hooks fire on *slot* panes (not PM pane). This hook would only be useful if installed on the PM pane itself. For slot-side, we rely on the existing `mop_send_to_slot` MCP tool which already checks `is-active.sh` before sending.

**Decision:** Defer to Phase 2. The existing `force` flag on `mop_send_to_slot` handles the critical case (plan approvals). The race condition is theoretical — no production incidents from it.

### Hook Installation

Each new handler requires a corresponding hook entry in the slot's `.claude/settings.json`. The existing `install-slot-hooks.sh` script must be updated to register the HTTP POST endpoint for all hook types:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "type": "command",
        "command": "curl -s -X POST http://localhost:3100/hooks/slot/$SLOT_NUM -H 'Content-Type: application/json' -d \"$HOOK_PAYLOAD\""
      }
    ],
    "Stop": [{ "type": "command", "command": "..." }],
    "Notification": [{ "type": "command", "command": "..." }]
  }
}
```

**Current state:** Hooks are already installed via HTTP POST to MoP server. The new handlers are purely server-side — no new hook script installation needed. The slot's hook configuration already sends ALL PostToolUse, Stop, and Notification events to MoP. We just need to add logic in `HookProcessor` to handle more event types within the existing hooks.

---

## Pillar 2: Pane Logging

### Current State

All pane output inspection uses `tmux capture-pane -t 0:0.N -p -S -30`:
- **`captureOutput()` in relay.ts** — captures last N lines for PM queries
- **`is-active.sh`** — parses ANSI chevron color to detect idle/busy
- **`mop_capture_output` MCP tool** — exposes capture to PM

**Limitations:**
- Only visible scrollback buffer (default 2000 lines)
- Content that scrolled past is permanently lost
- ANSI parsing is fragile (relies on specific shell prompt theme)
- Each capture is a point-in-time snapshot requiring polling

### Solution: `tmux pipe-pane`

`pipe-pane` streams ALL pane output to a file in real-time. It captures everything — tool output, commands, errors — without polling.

#### 2.1 Enable Logging

Add to slot setup (in `install-slot-hooks.sh` or MoP server startup):

```bash
# Enable pane logging for all 4 slots
for i in 1 2 3 4; do
  LOG="/tmp/slot-${i}.log"
  touch "$LOG"
  tmux pipe-pane -t "0:0.${i}" -o "cat >> ${LOG}"
done
```

**`-o` flag:** Only captures output (not input). This means we get tool outputs, command results, and error messages — but not the raw keystrokes PM sends to slots. This is the right granularity.

#### 2.2 Log Rotation

Pane logs grow unboundedly. Implement rotation in MoP server:

```typescript
// New: LogManager class in src/logs.ts
export class LogManager {
  private readonly MAX_LOG_SIZE = 100 * 1024; // 100KB per slot
  private readonly LOG_DIR = "/tmp";

  getLogPath(slot: number): string {
    return `${this.LOG_DIR}/slot-${slot}.log`;
  }

  /**
   * Read last N bytes from slot log.
   * More reliable than capture-pane — never loses content.
   */
  tailLog(slot: number, bytes: number = 4096): string {
    const logPath = this.getLogPath(slot);
    try {
      const stat = statSync(logPath);
      const start = Math.max(0, stat.size - bytes);
      const fd = openSync(logPath, "r");
      const buf = Buffer.alloc(Math.min(bytes, stat.size));
      readSync(fd, buf, 0, buf.length, start);
      closeSync(fd);
      return buf.toString("utf-8");
    } catch {
      return "";
    }
  }

  /**
   * Rotate log if over MAX_LOG_SIZE.
   * Keeps last 50KB, discards the rest.
   */
  rotateIfNeeded(slot: number): void {
    const logPath = this.getLogPath(slot);
    try {
      const stat = statSync(logPath);
      if (stat.size > this.MAX_LOG_SIZE) {
        const tail = this.tailLog(slot, 50 * 1024);
        writeFileSync(logPath, tail);
      }
    } catch { /* file doesn't exist — ok */ }
  }

  /**
   * Get log last-modified time (for stuck detection).
   */
  getLogMtime(slot: number): Date | null {
    try {
      return statSync(this.getLogPath(slot)).mtime;
    } catch {
      return null;
    }
  }

  /**
   * Initialize pipe-pane for all slots.
   */
  enableLogging(slotCount: number): void {
    for (let i = 1; i <= slotCount; i++) {
      const logPath = this.getLogPath(i);
      try {
        // Touch log file
        writeFileSync(logPath, "", { flag: "a" });
        // Enable pipe-pane (idempotent — if already piping, this is a no-op)
        execSync(`tmux pipe-pane -t "0:0.${i}" -o 'cat >> ${logPath}'`, {
          timeout: 5000,
        });
      } catch (err) {
        console.error(`[logs] Failed to enable logging for slot ${i}:`, err);
      }
    }
  }
}
```

#### 2.3 Replace `captureOutput()` in relay.ts

```typescript
// Before (tmux capture-pane):
captureOutput(slotNum: number, lines = 30): { output: string; activity: "busy" | "idle" } {
  const raw = execSync(`tmux capture-pane -t 0:0.${slotNum} -p -S -${lines}`, ...);
  ...
}

// After (log file tail):
captureOutput(slotNum: number, bytes = 4096): { output: string; activity: "busy" | "idle" } {
  const output = this.logManager.tailLog(slotNum, bytes);
  const activity = this.isSlotActiveFromLog(slotNum) ? "busy" : "idle";
  return { output, activity };
}
```

#### 2.4 Replace `is-active.sh` with Log-Based Detection

```typescript
/**
 * Check if slot is active based on log mtime.
 * If log was modified in last 5 seconds, slot is actively producing output.
 */
isSlotActiveFromLog(slotNum: number): boolean {
  const mtime = this.logManager.getLogMtime(slotNum);
  if (!mtime) return false;
  const ageMs = Date.now() - mtime.getTime();
  return ageMs < 5000; // Active if log modified in last 5 seconds
}
```

**Fallback:** Keep `is-active.sh` as a secondary check. If log-based detection returns "idle" but `is-active.sh` returns "ACTIVE", trust `is-active.sh` (the slot may be waiting for user input with no output).

#### 2.5 MCP Tool Update

Update `mop_capture_output` to use log file:

```typescript
// MCP tool handler:
mop_capture_output(slot: number, lines: number): { output: string; activity: string } {
  // Use log manager instead of tmux capture-pane
  const bytes = lines * 120; // Approximate bytes per line
  return relay.captureOutput(slot, bytes);
}
```

---

## Pillar 3: Stuck Detection

### Current State

No automated stuck detection. PM discovers stuck slots during 3h heartbeat checks by visually inspecting tmux panes. Slots can be stuck for hours before detection.

### Solution: Log Mtime Watchdog

#### 3.1 Stuck Detection Logic

A slot is "stuck" when:
- `occupied = true` (assigned to a task)
- `idle = false` (not at prompt)
- `dnd = false` (not under Rajiv's control)
- Log file mtime > 5 minutes ago (no output produced)

```typescript
// New: StuckDetector class in src/stuck.ts
export class StuckDetector {
  private readonly STUCK_THRESHOLD_MS = 5 * 60 * 1000; // 5 minutes
  private readonly CHECK_INTERVAL_MS = 60 * 1000; // Check every minute

  constructor(
    private db: MoPDatabase,
    private logManager: LogManager,
    private relay: TmuxRelay
  ) {}

  /**
   * Check all slots for stuck state.
   * Called periodically from the server's setInterval.
   */
  checkAll(): void {
    const slots = this.db.getAllSlots();
    for (const slot of slots) {
      if (!slot.occupied || slot.idle || slot.dnd) continue;

      const mtime = this.logManager.getLogMtime(slot.slot);
      if (!mtime) continue;

      const ageMs = Date.now() - mtime.getTime();
      if (ageMs > this.STUCK_THRESHOLD_MS) {
        this.handleStuck(slot, ageMs);
      }
    }
  }

  private handleStuck(slot: SlotState, silenceMs: number): void {
    const minutes = Math.round(silenceMs / 60000);

    // Check if we already notified recently (prevent spam)
    const recentNotifications = this.db.getEvents(slot.slot, 1, "stuck_detected");
    if (recentNotifications.length > 0) {
      const lastNotified = new Date(recentNotifications[0].timestamp);
      if (Date.now() - lastNotified.getTime() < 10 * 60 * 1000) {
        return; // Already notified within 10 minutes
      }
    }

    // Log the stuck event
    this.db.logEvent(slot.slot, "stuck_detected", null, null, {
      task: slot.task,
      issue: slot.issue,
      silence_minutes: minutes,
    });

    // Notify PM
    const comment = `# slot ${slot.slot} may be stuck — no output for ${minutes}min | task: ${slot.task || "unknown"}`;
    this.relay.injectToPM(comment);
  }

  /**
   * Start the periodic check.
   */
  start(): NodeJS.Timeout {
    return setInterval(() => this.checkAll(), this.CHECK_INTERVAL_MS);
  }
}
```

#### 3.2 Server Integration

In `server.ts` / `main.ts`:

```typescript
// After initializing db, relay, processor:
const logManager = new LogManager();
const stuckDetector = new StuckDetector(db, logManager, relay);

// Enable pane logging on startup
logManager.enableLogging(config.slotCount);

// Start stuck detection
const stuckTimer = stuckDetector.start();

// Log rotation on 10-minute interval
setInterval(() => {
  for (let i = 1; i <= config.slotCount; i++) {
    logManager.rotateIfNeeded(i);
  }
}, 10 * 60 * 1000);

// Clean up on shutdown
process.on("SIGINT", () => {
  clearInterval(stuckTimer);
  db.close();
  process.exit(0);
});
```

#### 3.3 Heartbeat Integration

The stuck detector runs independently of the PM heartbeat. Heartbeat can query stuck events:

```typescript
// In heartbeat, query MoP event log:
// GET /events?type=stuck_detected&limit=5
// → Shows any slots that got stuck since last heartbeat
```

---

## Implementation Plan

### Phase 1: Core (do first — highest ROI)

| Step | Change | File | Effort |
|------|--------|------|--------|
| 1a | Add `activity` column to slots table | `src/db.ts` | 5 min |
| 1b | PostToolUse activity classifier | `src/hooks.ts` | 20 min |
| 1c | PostToolUse Agent completion detection | `src/hooks.ts` | 10 min |
| 1d | `notifySubagentComplete()` relay method | `src/relay.ts` | 5 min |
| 1e | Return `activity` in MCP slot queries | `src/mcp.ts` | 5 min |

**Total Phase 1:** ~45 min

### Phase 2: Pane Logging (eliminates tmux dependency)

| Step | Change | File | Effort |
|------|--------|------|--------|
| 2a | Create `LogManager` class | `src/logs.ts` (new) | 30 min |
| 2b | Enable `pipe-pane` on server startup | `src/server.ts` | 5 min |
| 2c | Replace `captureOutput()` to use log files | `src/relay.ts` | 15 min |
| 2d | Add log-based `isSlotActive` fallback | `src/relay.ts` | 10 min |
| 2e | Update `mop_capture_output` MCP tool | `src/mcp.ts` | 5 min |
| 2f | Log rotation on interval | `src/server.ts` | 5 min |

**Total Phase 2:** ~70 min

### Phase 3: Stuck Detection

| Step | Change | File | Effort |
|------|--------|------|--------|
| 3a | Create `StuckDetector` class | `src/stuck.ts` (new) | 20 min |
| 3b | Wire up periodic check in server | `src/server.ts` | 5 min |
| 3c | Add `stuck_detected` event type to queries | `src/db.ts` | 5 min |

**Total Phase 3:** ~30 min

### Phase 4: Notification Enhancements (nice-to-have)

| Step | Change | File | Effort |
|------|--------|------|--------|
| 4a | Handle `autocompact` notification | `src/hooks.ts` | 10 min |
| 4b | Enrich Notification handler with routing | `src/hooks.ts` | 15 min |

**Total Phase 4:** ~25 min

---

## Files Changed

| File | Status | Changes |
|------|--------|---------|
| `src/hooks.ts` | Modified | PostToolUse activity classifier, Agent completion detection, Notification routing |
| `src/relay.ts` | Modified | `notifySubagentComplete()`, replace `captureOutput()` with log-based, `isSlotActiveFromLog()` |
| `src/db.ts` | Modified | `activity` column migration, `stuck_detected` event queries |
| `src/types.ts` | Modified | `activity` field on `SlotState` |
| `src/server.ts` | Modified | Wire up LogManager, StuckDetector, log rotation interval |
| `src/logs.ts` | **New** | `LogManager` — pipe-pane setup, log tail, rotation, mtime queries |
| `src/stuck.ts` | **New** | `StuckDetector` — periodic stuck check, PM notification |
| `src/mcp.ts` | Modified | Return `activity` in slot queries, update `mop_capture_output` |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| `pipe-pane` log grows unbounded | Disk fills up | Log rotation every 10 min, cap at 100KB |
| Activity classifier misclassifies commands | Wrong status displayed | Classifier is best-effort; `null` for unknown commands |
| Stuck detector false positives | PM interrupted unnecessarily | 5-min threshold + 10-min re-notify cooldown |
| Log file permissions | MoP can't read slot logs | All run as same user (`rajiv`), `/tmp/` is world-writable |
| `pipe-pane` not idempotent | Multiple pipes to same file | Check with `tmux show -p -t PANE pipe-pane` before enabling |

---

## Success Criteria

1. **Activity tracking:** `mop_all_slots()` shows `activity: "testing"` when slot runs vitest
2. **No more capture-pane:** All `tmux capture-pane` calls replaced with log file reads
3. **Stuck detection:** PM gets notified within 6 minutes of a slot going silent
4. **Agent completion:** PM sees `# subagent completed in slot N` when background agent finishes
5. **No regressions:** Existing idle detection, plan-ready detection, and auto-release still work

---

## Out of Scope

- **UserPromptSubmit hook** — deferred to Phase 2 (PM-side hook, different installation model)
- **Structured event log** (`/tmp/slot-N-events.jsonl`) — the SQLite events table already serves this purpose
- **Real-time slot dashboard UI** — MCP tools are sufficient for PM orchestration
- **Cross-slot coordination** — handled by PM, not by MoP directly

---

*Dhruva, 2026-03-10*
