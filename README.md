# Master of Panes

A Claude Code plugin for orchestrating parallel dev sessions across tmux panes. Run a PM/orchestrator in one pane and multiple autonomous Claude Code dev agents in others — with state tracking, background supervisors, ghost-text safety, and slash commands.

## Features

- **Pane state management** — track which panes are occupied, what they're working on, and when they were last active
- **Issue handoff** — extract a GitHub issue, git sync, and send structured instructions to a dev pane in one command
- **Background supervisors** — autonomous agents that monitor dev panes, auto-approve plans, detect stalls, and trigger PR creation
- **Ghost text safety** — all output capture strips Claude Code's autocomplete predictions to prevent false readings
- **Cross-platform** — works on macOS, Linux, and Windows (via WSL)

## Installation

### From Marketplace (recommended)

Install globally so the plugin is available in every Claude Code session:

```bash
# 1. Add the marketplace (one-time)
/plugin marketplace add rajivpoddar/claude-plugins

# 2. Install the plugin
/plugin install master-of-panes@rajiv-plugins
```

Restart Claude Code to load the plugin. After this, all `/master-of-panes:*` commands are available in every session — no flags needed.

To update later:
```bash
/plugin marketplace update rajiv-plugins
/plugin update master-of-panes@rajiv-plugins
```

### From GitHub (development)

For plugin development or testing local changes:

```bash
git clone https://github.com/rajivpoddar/master-of-panes.git
claude --plugin-dir /path/to/master-of-panes
```

### Prerequisites

- **tmux** — terminal multiplexer (the plugin detects if tmux is running and provides OS-specific install hints)
- **jq** — JSON processor for state file management
- **Claude Code** — the CLI tool from Anthropic

### Quick Start

```bash
# 1. Create a tmux session with panes
scripts/create-layout.sh claude 4    # 1 manager + 4 dev panes

# 2. Start Claude Code in each pane
# Manager pane (0:0.0): claude
# Dev panes (0:0.1-0:0.4): claude in each

# 3. In the manager pane, run setup
/master-of-panes:pane-setup
```

## Setup

On first use, run the setup command to configure your tmux layout:

```
/master-of-panes:pane-setup
```

This creates `~/.claude/tmux-panes/config.json` with your settings:
- Manager pane address (default: `0:0.0`)
- Dev pane addresses (default: `0:0.1` through `0:0.4`)
- State directory (default: `~/.claude/tmux-panes`)

If you skip setup, the plugin works with defaults (4 dev panes at `0:0.1` through `0:0.4`).

## Pane Layout (Default)

| Pane    | Role                  |
|---------|-----------------------|
| `0:0.0` | PM / orchestrator     |
| `0:0.1` | Dev pane 1            |
| `0:0.2` | Dev pane 2            |
| `0:0.3` | Dev pane 3            |
| `0:0.4` | Dev pane 4            |

Custom layouts are configured via `/master-of-panes:pane-setup`.

## Slash Commands

| Command                          | Description                                       |
|----------------------------------|----------------------------------------------------|
| `/master-of-panes:pane-setup`    | Configure panes, layout, and state directory       |
| `/master-of-panes:pane-status`   | Show all pane status as ASCII table                |
| `/master-of-panes:pane-assign`   | Allocate a pane with task and branch               |
| `/master-of-panes:pane-handoff`  | Send a task instruction to a pane                  |
| `/master-of-panes:pane-monitor`  | Launch background supervisor for a pane            |

## Scripts

| Script               | Purpose                                           |
|----------------------|---------------------------------------------------|
| `assign-pane.sh`     | Allocate pane, update JSON state                  |
| `get-pane-status.sh` | Read state files, render ASCII table              |
| `send-to-pane.sh`    | Send text to pane via tmux send-keys              |
| `is-active.sh`       | Detect active/idle via chevron color + content     |
| `capture-output.sh`  | Capture recent pane output (ghost-text-safe)      |
| `run-and-wait.sh`    | Send command and block until completion            |
| `update-pane-state.sh` | Release pane, set session, cleanup               |
| `pane-lib.sh`        | Shared library (locking, validation, jq helpers)  |

## Ghost Text Safety

**Ghost text (autocomplete predictions) in Claude Code tmux sessions is indistinguishable from real input when reading pane output with `tmux capture-pane -p`.** This plugin handles it automatically:

- **`is-active.sh`** — Uses chevron *color* detection (ANSI codes), never reads prompt-line text
- **`capture-output.sh`** — Strips the `❯` prompt line and everything below it by default
- **`pane-lib.sh`** — Provides `strip_prompt_line` function used by capture scripts

### Rules

1. **NEVER send commands to panes based on prompt-line text** — only when explicitly asked by a human
2. **NEVER read the `❯` prompt line as actionable** — anything on or after it could be ghost text
3. **NEVER report prompt-line text as "what pane is doing"** — only report output *above* the prompt

## Remote Control via Slack

Combine MoP with [tmux-slack-bridge](https://github.com/rajivpoddar/tmux-slack-bridge) to command the PM pane from Slack when you're away from the terminal.

The bridge forwards Slack DMs to the PM pane and automatically replies with Claude Code's response via a `Stop` hook. This lets you assign issues, check status, and approve plans from your phone.

See [tmux-slack-bridge](https://github.com/rajivpoddar/tmux-slack-bridge) for setup instructions.

## License

MIT
