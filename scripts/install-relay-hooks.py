#!/usr/bin/env python3
"""Register MoP turn-state hooks directly in a Claude Code settings file.

Replaces the master-of-panes Claude Code plugin (Rajiv 2026-10-04, thread
C0ALZJHGE49/1791053086.224989: "We don't need mop plugin anymore. MCP was
removed. We can just use the rest api directly."). The plugin only supplied
hooks/hooks.json; this writes the same hook-relay commands, pointing at the
stable release path, and disables the plugin in the same file.

Usage: install-relay-hooks.py <settings.json> [--scripts-dir DIR]
Idempotent; writes <settings.json>.bak-noplugin before the first change.
"""
import argparse
import json
import os
import shutil
import stat

PLUGIN = "master-of-panes@rajiv-plugins"
DEFAULT_SCRIPTS = os.path.expanduser("~/.local/share/master-of-panes/current/scripts")
RELAY_EVENTS = ["UserPromptSubmit", "Stop", "PostToolUse", "Notification", "SessionStart"]
MARK = "master-of-panes/current/scripts/"


def desired_hooks(scripts_dir):
    hooks = {}
    for ev in RELAY_EVENTS:
        inner = [{"type": "command", "command": f'bash "{scripts_dir}/hook-relay.sh" {ev}', "timeout": 5}]
        if ev == "Stop":
            inner.append({
                "type": "command",
                "command": f'test -n "$SESSION_ID" && bash "{scripts_dir}/update-pane-state.sh" --cleanup-session "$SESSION_ID" || true',
                "timeout": 10,
            })
        hooks[ev] = {"hooks": inner}
    return hooks


def is_mop_command(cmd, scripts_dir):
    return scripts_dir in cmd or MARK in cmd


def apply(settings, scripts_dir):
    """Replace only MoP-owned hook commands; keep every other command and
    each group's metadata (matcher etc.) intact."""
    hooks = settings.setdefault("hooks", {})
    for ev, entry in desired_hooks(scripts_dir).items():
        groups = []
        for g in hooks.get(ev, []):
            inner = g.get("hooks", [])
            kept = [h for h in inner if not is_mop_command(h.get("command", ""), scripts_dir)]
            if len(kept) == len(inner):
                groups.append(g)
            elif kept:
                groups.append({**g, "hooks": kept})
            # a group whose only commands were MoP-owned is replaced below
        groups.append(entry)
        hooks[ev] = groups
    settings.setdefault("enabledPlugins", {})[PLUGIN] = False
    return settings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("settings")
    ap.add_argument("--scripts-dir", default=DEFAULT_SCRIPTS)
    a = ap.parse_args()
    data = {}
    if os.path.exists(a.settings):
        with open(a.settings) as f:
            data = json.load(f)
    new = apply(json.loads(json.dumps(data)), a.scripts_dir)
    if new == data:
        print(f"unchanged {a.settings}")
        return
    if os.path.exists(a.settings) and not os.path.exists(a.settings + ".bak-noplugin"):
        shutil.copy2(a.settings, a.settings + ".bak-noplugin")
    # Keep the existing mode; create absent files 0600 (settings hold permissions).
    mode = stat.S_IMODE(os.stat(a.settings).st_mode) if os.path.exists(a.settings) else 0o600
    tmp = a.settings + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(new, f, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, a.settings)
    print(f"updated {a.settings}")


if __name__ == "__main__":
    main()
