# CLAUDE.md — tmux-slot-manager

A Claude Code plugin for managing parallel dev sessions across tmux panes.

## What This Is

A reusable Claude Code plugin that provides:
- Slash commands for slot management (`/tmux-manager:status`, `:assign`, `:handoff`, `:monitor`)
- JSON state files for tracking slot occupancy (`~/.claude/tmux-slots/`)
- SessionEnd hooks for auto-cleanup when sessions finish
- Shell scripts for tmux orchestration (send-keys, capture-pane, is-active)

## Design Principles

1. **No project-specific references** — this is generic, works with any Claude Code project
2. **Shell scripts do tmux work** — Claude Code can't manage tmux directly, scripts bridge the gap
3. **JSON state files** — reliable tracking in `~/.claude/tmux-slots/slot-N.json`
4. **Skills provide the UI** — slash commands invoke shell scripts and format output
5. **Hooks provide automation** — SessionEnd cleans up stale slots

## Plugin Structure

```
claude-tmux-manager/
├── .claude-plugin/
│   └── plugin.json           # Plugin manifest
├── skills/
│   ├── setup/SKILL.md        # /tmux-manager:setup — configure slots and layout
│   ├── status/SKILL.md       # /tmux-manager:status — show all slot status
│   ├── assign/SKILL.md       # /tmux-manager:assign N — allocate slot
│   ├── handoff/SKILL.md      # /tmux-manager:handoff N — hand off work to slot
│   └── monitor/SKILL.md      # /tmux-manager:monitor N GOAL — background supervisor
├── hooks/
│   └── hooks.json            # Stop → auto-mark slot idle
├── scripts/
│   ├── slot-lib.sh           # Shared library (config, locking, validation)
│   ├── assign-slot.sh        # Allocate slot, update state
│   ├── get-slot-status.sh    # Read state files, output ASCII table
│   ├── send-to-slot.sh       # Forward message to a slot's tmux pane
│   ├── is-active.sh          # Check if a slot is idle or active
│   ├── capture-output.sh     # Capture recent output from a slot
│   ├── run-and-wait.sh       # Send command and block until completion
│   └── update-slot-state.sh  # Clean up state on session end
└── README.md
```

## Configuration

`~/.claude/tmux-slots/config.json` (created by `/tmux-manager:setup`):
```json
{
  "slots": 4,
  "pane_prefix": "0:0",
  "manager_pane": "0:0.0",
  "state_dir": "~/.claude/tmux-slots"
}
```

If no config exists, defaults are used (4 slots, pane prefix `0:0`). All scripts read config via `load_config()` in `slot-lib.sh`.

## State File Format

`~/.claude/tmux-slots/slot-N.json`:
```json
{
  "slot": 1,
  "occupied": false,
  "pane": "0:0.1",
  "session_id": null,
  "task": null,
  "branch": null,
  "assigned_at": null,
  "last_activity": null
}
```

## Existing Reference Implementation

These ad-hoc scripts in the heydonna project serve as the reference:
- `~/.claude/skills/tmux-slot-command/scripts/send-to-slot.sh` — sends text to a slot pane via tmux send-keys
- `~/.claude/skills/tmux-slot-command/scripts/is-active.sh` — checks cursor movement to detect idle/active
- `~/.claude/skills/slot-handoff-supervisor/scripts/handoff-and-supervise.sh` — hands off issues and monitors

Extract the generic parts, remove heydonna-specific references (issue labels, branch naming, Slack channels, Codex reviews).

## Key Technical Details

### tmux Pane Addressing
- Configurable via `config.json` (`pane_prefix` and `manager_pane`)
- Default layout: session 0, window 0, panes 0-4
- Pane 0 (0:0.0) = PM/orchestrator
- Panes 1-N (0:0.1 through 0:0.N) = dev slots
- Address format: `session:window.pane`
- Scripts derive session/window from `PANE_PREFIX`: `TMUX_SESSION="${PANE_PREFIX%%:*}"`

### Detecting Idle vs Active
The `is-active.sh` script works by:
1. Chevron color: gray ❯ (38;2;153;153;153) = ACTIVE, white ❯ = IDLE
2. Content hashing: captures pane twice 1.5s apart, compares MD5
3. Exit codes: 0=active, 1=idle, 2=error

### Sending Messages to Slots
The `send-to-slot.sh` script:
1. Waits for slot to become IDLE (polls every 5s, timeout 10min)
2. Sends text via `tmux send-keys -t <pane> '<escaped-text>'`
3. Waits 300ms then sends Enter (delay needed for Claude Code to register input)

### SessionEnd Hook
When a Claude Code session ends in a slot pane:
1. Hook fires with session context
2. Script finds which slot file has that session_id
3. Marks slot as `occupied: false`
4. Clears session_id, task, branch

## Development Commands

```bash
# Test the plugin locally
claude --plugin-dir /Users/rajiv/Downloads/projects/claude-tmux-manager

# Run a specific script
bash scripts/get-slot-status.sh
bash scripts/is-active.sh 1

# Initialize state directory
mkdir -p ~/.claude/tmux-slots
```

## What NOT to Include

- No project-specific issue/PR workflows
- No Slack channel IDs or bot tokens
- No GitHub label conventions
- No code review integrations (Codex, etc.)
- No QA report formats
- No branch naming conventions (keep generic)
