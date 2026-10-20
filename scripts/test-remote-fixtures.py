#!/usr/bin/env python3
"""Materialize managed test dependencies in an EMPTY disposable Linux sandbox.

Never use the workstation's installed files, database or environment. Legacy
absolute-path consumers see a compatibility symlink into the synthetic HOME;
refuse an existing /Users/rajiv so this cannot overwrite a real installation.
Run the requested test command with its own synthetic HOME (not the shell's).
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("requires a disposable Linux container; host setup refused")
    missing = [tool for tool in ("node", "npm", "git", "sqlite3", "jq", "tmux") if not shutil.which(tool)]
    if missing:
        raise SystemExit("required test tools missing: " + ", ".join(missing))
    alias = Path("/Users/rajiv")
    root = args.root.resolve()
    if root.exists() or alias.exists() or alias.is_symlink():
        raise SystemExit("fixture root or legacy HOME already exists; refusing overwrite")
    repo = Path(__file__).resolve().parents[1]
    subprocess.run(["git", "cat-file", "-e", "c33c295c46c471de233e3420523bfc2e96c9e760^{commit}"],
                   cwd=repo, check=True)
    root.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("fixture_installer", repo / "scripts/install-release.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.install_shared_assets(release_dir=repo, target_root=root, rollback_bundle=root / "rollback")
    home = root / "Users/rajiv"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(home, target_is_directory=True)
    app = home / "Downloads/projects/heydonna-app"
    app.mkdir(parents=True, exist_ok=True)
    # Fake token is read only by send tests with a fake curl transport.
    (app / ".env.local").write_text("SLACK_BOT_TOKEN=fixture-not-a-credential\n")
    for number in range(1, 9):
        (app.parent / f"heydonna-app-300{number}").mkdir(parents=True, exist_ok=True)
    db = home / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as connection:
        connection.execute("create table obligations (id integer,kind text,status text,pr integer,issue integer,slot integer,owner text,title text,required_action text,blocker text,evidence_json text,updated_at text,created_at text)")
    # Unmanaged guard is an explicit historical fixture, never read from HOME.
    guard = home / ".claude/hooks/force-dev-slot-foreground-agent.py"
    shutil.copyfile(repo / "test/fixtures/foreground-agent-guard.py", guard)
    guard.chmod(0o755)
    # The old app alias is consumed by a guard-parity test, not a live sender.
    sender = home / ".claude/skills/slack-message/scripts/slack-send.sh"
    old_sender = app / ".claude/skills/slack-message-real/scripts/slack-send.sh"
    old_sender.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sender, old_sender)
    # Path-specific Node probes use the actual installed test runtime.
    node = shutil.which("node")
    if not node:
        raise SystemExit("node required")
    node_alias = Path("/opt/homebrew/bin/node")
    node_alias.parent.mkdir(parents=True, exist_ok=True)
    if node_alias.exists():
        raise SystemExit("unexpected preexisting Node alias")
    node_alias.symlink_to(node)
    # Resolver tests require an executable fallback, never an authenticated CLI.
    codex = node_alias.with_name("codex")
    codex.write_text("#!/bin/sh\necho 'fixture CLI must not be invoked' >&2\nexit 64\n")
    codex.chmod(0o755)
    environment = {**os.environ, "HOME": str(home), "MOP_TEST_FIXTURE_ROOT": str(root),
                   "MOP_NODE": node, "PYTHONDONTWRITEBYTECODE": "1"}
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("test command required after --")
    print(json.dumps({"fixture_root": str(root), "synthetic_home": str(home),
                      "command": command}), flush=True)
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
