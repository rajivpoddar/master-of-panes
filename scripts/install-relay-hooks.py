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


def apply(settings, scripts_dir):
    hooks = settings.setdefault("hooks", {})
    for ev, entry in desired_hooks(scripts_dir).items():
        groups = [g for g in hooks.get(ev, [])
                  if not any(scripts_dir in h.get("command", "") or MARK in h.get("command", "")
                             for h in g.get("hooks", []))]
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
    tmp = a.settings + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new, f, indent=2)
        f.write("\n")
    os.replace(tmp, a.settings)
    print(f"updated {a.settings}")


if __name__ == "__main__":
    main()
