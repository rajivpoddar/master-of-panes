# tmux-slot-manager

A Claude Code plugin for managing parallel dev sessions across tmux panes.

## Installation

```bash
# Use as a plugin
claude --plugin-dir /path/to/claude-tmux-manager
```

## Setup

On first use, run the setup command to configure your tmux layout:

```
/tmux-manager:setup
```

This creates `~/.claude/tmux-slots/config.json` with your settings:
- Number of dev slots (default: 4)
- tmux pane prefix (default: `0:0`)
- Manager pane (default: `0:0.0`)
- State directory (default: `~/.claude/tmux-slots`)

If you skip setup, the plugin works with defaults (4 slots at `0:0.1` through `0:0.4`).

## Slot Layout (Default)

| Pane    | Role                  |
|---------|-----------------------|
| `0:0.0` | PM / orchestrator     |
| `0:0.1` | Dev slot 1            |
| `0:0.2` | Dev slot 2            |
| `0:0.3` | Dev slot 3            |
| `0:0.4` | Dev slot 4            |

Custom layouts are configured via `/tmux-manager:setup`.

## Slash Commands

| Command                  | Description                                      |
|--------------------------|--------------------------------------------------|
| `/tmux-manager:setup`    | Configure slots, pane layout, and state directory |
| `/tmux-manager:status`   | Show all slot status as ASCII table              |
| `/tmux-manager:assign`   | Allocate a slot with task and branch             |
| `/tmux-manager:handoff`  | Send a task instruction to a slot                |
| `/tmux-manager:monitor`  | Launch background supervisor for a slot          |

## Scripts

| Script               | Purpose                                           |
|----------------------|---------------------------------------------------|
| `assign-slot.sh`     | Allocate slot, update JSON state                  |
| `get-slot-status.sh` | Read state files, render ASCII table              |
| `send-to-slot.sh`    | Send text to slot via tmux send-keys              |
| `is-active.sh`       | Detect active/idle via chevron color + content     |
| `capture-output.sh`  | Capture recent pane output (ghost-text-safe)      |
| `run-and-wait.sh`    | Send command and block until completion            |
| `update-slot-state.sh` | Release slot, set session, cleanup                |
| `slot-lib.sh`        | Shared library (locking, validation, jq helpers)  |

## GHOST_TEXT_WARNING

**Ghost text (autocomplete predictions) in Claude Code tmux sessions is indistinguishable from real input when reading pane output with `tmux capture-pane -p`.**

This is not a hypothetical risk. Three real incidents occurred in February 2026:

### Incident 1: Accidental PR Merge
Ghost text `merge PR #1223` appeared at an idle slot's prompt. A PM agent read it as an action being taken. Result: a 59-file track-changes refactor was merged without code review.

### Incident 2: False Status Report
Ghost text `fix them all` appeared at slot 4's prompt. A PM agent reported "slot 4 is executing: fix them all." The slot was completely idle.

### Incident 3: Unauthorized Command Trigger
Ghost text `/review-and-pr` appeared at slot 2's prompt. A PM agent sent `/review-and-pr` to the slot, triggering PR creation while the developer wasn't done.

### Hard Rules

1. **NEVER send commands to slots based on prompt-line text** — only when explicitly asked by a human
2. **NEVER read the `❯` prompt line as actionable** — anything on or after it could be ghost text
3. **NEVER report prompt-line text as "what slot is doing"** — only report output *above* the prompt

### How This Plugin Handles It

- **`is-active.sh`** — Uses chevron *color* detection (ANSI codes) and content *hashing*, never reads prompt-line text. Immune to ghost text.
- **`capture-output.sh`** — Strips the `❯` prompt line and everything below it by default. Use `--raw` only for debugging.
- **`get-slot-status.sh`** — Activity detection delegates to `is-active.sh`. Never reads or reports prompt-line content.
- **`slot-lib.sh`** — Provides `strip_prompt_line` function used by capture scripts.
- **`send-to-slot.sh`** — Reads INSERT/NORMAL mode from the status bar, not from prompt-line text. Does not interpret ghost text.

### Safe Patterns

```bash
# SAFE: Activity check (color-based, no text interpretation)
is-active.sh 1 && echo "ACTIVE" || echo "IDLE"

# SAFE: Capture output (auto-strips prompt line)
capture-output.sh 1 20

# SAFE: Report status (JSON state + color detection)
get-slot-status.sh --live

# UNSAFE: Raw capture includes ghost text — for debugging only
capture-output.sh 1 --raw

# UNSAFE: Never trust text on the ❯ line
tmux capture-pane -t 0:0.1 -p | tail -3   # May contain ghost text!
```

### Detecting Ghost Text in Raw Output

If you need to distinguish ghost text from real input, use ANSI capture:

```bash
# Ghost text has dim/gray ANSI codes (^[[2m or ^[[90m)
tmux capture-pane -e -t 0:0.1 -p | tail -10 | cat -v
```

## License

MIT
