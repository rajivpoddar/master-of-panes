# CLAUDE.md — Master of Panes

A Claude Code plugin for managing parallel dev sessions across tmux panes.

## What This Is

A reusable Claude Code plugin that provides:
- Slash commands for pane management (`/master-of-panes:pane-status`, `:pane-assign`, `:pane-handoff`, `:pane-monitor`)
- JSON state files for tracking pane occupancy (`~/.claude/tmux-panes/`)
- SessionEnd hooks for auto-cleanup when sessions finish
- Shell scripts for tmux orchestration (send-keys, capture-pane, is-active)

## Design Principles

1. **No project-specific references** — this is generic, works with any Claude Code project
2. **Shell scripts do tmux work** — Claude Code can't manage tmux directly, scripts bridge the gap
3. **JSON state files** — reliable tracking in `~/.claude/tmux-panes/pane-N.json`
4. **Skills provide the UI** — slash commands invoke shell scripts and format output
5. **Hooks provide automation** — SessionEnd cleans up stale panes

## Plugin Structure

```
master-of-panes/
├── .claude-plugin/
│   └── plugin.json           # Plugin manifest
├── skills/
│   ├── pane-setup/SKILL.md   # /master-of-panes:pane-setup — configure panes and layout
│   ├── pane-status/SKILL.md  # /master-of-panes:pane-status — show all pane status
│   ├── pane-assign/SKILL.md  # /master-of-panes:pane-assign N — allocate pane
│   ├── pane-handoff/SKILL.md # /master-of-panes:pane-handoff N — hand off work to pane
│   └── pane-monitor/SKILL.md # /master-of-panes:pane-monitor N GOAL — background supervisor
├── hooks/
│   └── hooks.json            # Stop → auto-mark pane idle
├── scripts/
│   ├── pane-lib.sh           # Shared library (config, locking, validation)
│   ├── assign-pane.sh        # Allocate pane, update state
│   ├── get-pane-status.sh    # Read state files, output ASCII table
│   ├── send-to-pane.sh       # Forward message to a dev pane
│   ├── is-active.sh          # Check if a pane is idle or active
│   ├── capture-output.sh     # Capture recent output from a pane
│   ├── run-and-wait.sh       # Send command and block until completion
│   └── update-pane-state.sh  # Clean up state on session end
└── README.md
```

## Configuration

`~/.claude/tmux-panes/config.json` (created by `/master-of-panes:pane-setup`):
```json
{
  "panes": {
    "manager": "0:0.0",
    "dev": ["0:0.1", "0:0.2", "0:0.3", "0:0.4"]
  },
  "state_dir": "~/.claude/tmux-panes"
}
```

If no config exists, defaults are used (4 dev panes at `0:0.1`-`0:0.4`, manager at `0:0.0`). All scripts read config via `load_config()` in `pane-lib.sh`.

## State File Format

`~/.claude/tmux-panes/pane-N.json`:
```json
{
  "pane": 1,
  "address": "0:0.1",
  "occupied": false,
  "session_id": null,
  "task": null,
  "branch": null,
  "assigned_at": null,
  "last_activity": null
}
```

## Existing Reference Implementation

These ad-hoc scripts in the heydonna project serve as the reference:
- `~/.claude/skills/tmux-pane-command/scripts/send-to-pane.sh` — sends text to a pane via tmux send-keys
- `~/.claude/skills/tmux-pane-command/scripts/is-active.sh` — checks cursor movement to detect idle/active
- `~/.claude/skills/pane-handoff-supervisor/scripts/handoff-and-supervise.sh` — hands off issues and monitors

Extract the generic parts, remove heydonna-specific references (issue labels, branch naming, Slack channels, Codex reviews).

## Key Technical Details

### tmux Pane Addressing
- Configurable via `config.json` (`panes.manager` and `panes.dev[]`)
- Default layout: session 0, window 0, panes 0-4
- Pane 0 (0:0.0) = PM/orchestrator
- Panes 1-N (0:0.1 through 0:0.N) = dev panes
- Address format: `session:window.pane`
- Each dev pane has an explicit address in the config — no prefix derivation needed

### Detecting Idle vs Active
The `is-active.sh` script works by:
1. Chevron color: gray ❯ (38;2;153;153;153) = ACTIVE, white ❯ = IDLE
2. Content hashing: captures pane twice 1.5s apart, compares MD5
3. Exit codes: 0=active, 1=idle, 2=error

### Sending Messages to Panes
The `send-to-pane.sh` script:
1. Waits for pane to become IDLE (polls every 5s, timeout 10min)
2. Sends text via `tmux send-keys -t <pane> '<escaped-text>'`
3. Waits 300ms then sends Enter (delay needed for Claude Code to register input)

### SessionEnd Hook
When a Claude Code session ends in a dev pane:
1. Hook fires with session context
2. Script finds which pane state file has that session_id
3. Marks pane as `occupied: false`
4. Clears session_id, task, branch

## Development Commands

```bash
# Test the plugin locally
claude --plugin-dir /path/to/master-of-panes

# Run a specific script
bash scripts/get-pane-status.sh
bash scripts/is-active.sh 1

# Initialize state directory
mkdir -p ~/.claude/tmux-panes
```

## What NOT to Include

- No project-specific issue/PR workflows
- No Slack channel IDs or bot tokens
- No GitHub label conventions
- No code review integrations (Codex, etc.)
- No QA report formats
- No branch naming conventions (keep generic)
