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
│   ├── status/SKILL.md       # /tmux-manager:status — show all slot status
│   ├── assign/SKILL.md       # /tmux-manager:assign N — allocate slot
│   ├── handoff/SKILL.md      # /tmux-manager:handoff N — hand off work to slot
│   └── monitor/SKILL.md      # /tmux-manager:monitor N GOAL — background supervisor
├── hooks/
│   └── hooks.json            # SessionEnd → auto-mark slot idle
├── scripts/
│   ├── assign-slot.sh        # Allocate slot, update state, spawn session
│   ├── get-slot-status.sh    # Read state files, output ASCII table
│   ├── send-to-slot.sh       # Forward message to a slot's tmux pane
│   ├── is-active.sh          # Check if a slot is idle or active
│   ├── capture-output.sh     # Capture recent output from a slot
│   └── update-slot-state.sh  # Clean up state on session end
└── README.md
```

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
- Default layout: session 0, window 0, panes 0-4
- Pane 0 (0:0.0) = PM/orchestrator
- Panes 1-4 (0:0.1 through 0:0.4) = dev slots
- Address format: `session:window.pane`

### Detecting Idle vs Active
The `is-active.sh` script works by:
1. Capture cursor position
2. Wait 2 seconds
3. Re-capture cursor position
4. If cursor moved → ACTIVE; if same → IDLE

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
