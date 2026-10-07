"""CTO P1 on 071144d: installed activity probes must bind to the
checkout-verified pane id, never 0:0.$SLOT, and fail closed (exit 2)."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ASSETS = ROOT / "scripts/pm/shared-assets"
SCRIPTS = ASSETS / "claude/skills/tmux-slot-command/scripts"
P = "/Users/rajiv/Downloads/projects/heydonna-app-300{}".format

ACTIVE = "\x1b[38;2;153;153;153m❯ \x1b[0m"
IDLE = "❯ "


def _fake_tmux(tmp: Path, panes: dict[str, tuple[str, str]], active: set[str]) -> dict[str, str]:
    """panes: address -> (pane_id, cwd). Captures render active/idle chevrons."""
    table = tmp / "panes.json"
    table.write_text(json.dumps({"panes": panes, "active": sorted(active)}))
    tmux = tmp / "tmux"
    tmux.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"d=json.load(open({str(table)!r}))\n"
        "a=sys.argv[1:]; t=a[a.index('-t')+1] if '-t' in a else ''\n"
        "panes=d['panes']\n"
        "def find(t):\n"
        "    if t in panes: return panes[t]\n"
        "    for v in panes.values():\n"
        "        if v[0]==t: return v\n"
        "if a[0]=='display-message':\n"
        "    p=find(t)\n"
        "    if not p: sys.exit(1)\n"
        "    print(p[0]+'|'+p[1])\n"
        "elif a[0]=='list-panes':\n"
        "    print('\\n'.join(v[0]+'|'+v[1] for v in panes.values()))\n"
        "elif a[0]=='capture-pane':\n"
        "    p=find(t)\n"
        "    if not p or not t.startswith('%'): sys.exit(1)\n"
        f"    print('x\\n'+({ACTIVE!r} if p[0] in d['active'] else {IDLE!r}))\n"
    )
    tmux.chmod(tmux.stat().st_mode | stat.S_IEXEC)
    # Do not inspect live numbered-slot checkouts in this offline identity proof.
    git = tmp / "git"
    git.write_text('#!/bin/bash\nprintf "%s\\n" "$2"\n')
    git.chmod(git.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "MOP_TMUX_BIN": str(tmux)}


LIVE = {"0:0.6": ("%155", P(7)), "0:0.7": ("%7", P(6))}


def _active(slot: int, env, *extra: str) -> int:
    return subprocess.run([str(SCRIPTS / "is-active.sh"), str(slot), "--fast", *extra], env=env, capture_output=True).returncode


def _pane(slot: int, env, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPTS / "slot-pane.sh"), str(slot), *extra], env=env, capture_output=True, text=True)


def test_s6_active_s7_idle_binds_to_s6_pane(tmp_path):
    env = _fake_tmux(tmp_path, LIVE, {"%7"})
    assert _pane(6, env).stdout.strip() == "%7"
    assert _pane(7, env).stdout.strip() == "%155"
    assert _active(6, env) == 0  # S6 active -> refuse /exit
    assert _active(7, env) == 1


def test_inverse_mapping(tmp_path):
    env = _fake_tmux(tmp_path, LIVE, {"%155"})
    assert _active(6, env) == 1
    assert _active(7, env) == 0


def test_missing_pane_fails_closed(tmp_path):
    env = _fake_tmux(tmp_path, {"0:0.6": LIVE["0:0.6"]}, set())
    assert _pane(6, env).returncode == 2
    assert _active(6, env) == 2  # unknown, never idle


def test_rebound_pinned_pane_fails_closed(tmp_path):
    env = _fake_tmux(tmp_path, {"0:0.6": LIVE["0:0.6"], "0:0.7": ("%7", P(5))}, set())
    assert _pane(6, env, "--pane-id", "%7").returncode == 2
    assert _active(6, env, "--pane-id", "%7") == 2
    dup = _fake_tmux(tmp_path, {"0:0.7": ("%7", P(5)), "0:0.8": ("%8", P(6)), "0:0.9": ("%9", P(6))}, set())
    assert _pane(6, dup).returncode == 2


def test_send_to_slot_treats_unknown_as_active():
    text = (SCRIPTS / "send-to-slot.sh").read_text()
    assert '[ $? -ne 1 ]' in text


def test_no_index_formula_in_shared_pane_probes():
    formula = re.compile(r'0:0\.\$|0:0\.\$\{|0:0\.%s|"0:0\." *\+')
    for path in (ASSETS / "claude/skills").rglob("*.sh"):
        assert not formula.search(path.read_text()), path


def test_probes_selected_into_manifest():
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    sources = {e["source_path"] for e in manifest["entries"]}
    for rel in (
        "claude/skills/tmux-slot-command/scripts/slot-pane.sh",
        "claude/skills/tmux-slot-command/scripts/is-active.sh",
        "claude/skills/tmux-slot-command/scripts/run-and-wait.sh",
        "claude/skills/tmux-pane-screenshot/scripts/pane-screenshot.sh",
        "claude/skills/pane-video/scripts/record-pane.sh",
    ):
        assert rel in sources, rel
