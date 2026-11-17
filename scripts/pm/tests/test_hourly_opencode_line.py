"""Hourly *OpenCode Go:* line: pool cap, live now / launched last 1h per model id, Muse emergency launches. Counts only.

Directive: OpenCode Go pool line for the hourly composer (cap from the proxy caps file, default 2; the three pool model ids;
emergency Muse Spark launches from the launcher's log; no cost and no rate). Fakes only: the ps text, the run registry, the caps
file and the Muse log are temp files or strings; nothing calls MoP, tmux, Slack or a provider.
Added, NOT executed (MoP changes need no local test proof; Rajiv DM D0AMF0XE6TS thread 1793979986.975849,
ts 1793983042.752819).
"""

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py"
spec = importlib.util.spec_from_file_location("heartbeat_compose_opencode", COMPOSE)
composer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(composer)

HY4, DEEP, MUSE = composer.OPENCODE_MODELS
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def stamp(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def manifest(root, name, model, started):
    run = root / name
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"name": name, "model": model, "started_at": started}))


def log_line(seconds_ago, name, role="primary"):
    when = (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f"{when} EMERGENCY_MUSE role={role} name={name} type=hd-builder tier=MID "
            f"model=muse-spark-1.3-contributor reason=none\n")


def ps(*models):
    # The launcher wrapper embeds --model too; only the real `claude -p --name X --model <id>` process counts.
    rows = ["/usr/bin/login -pf rajiv",
            "/bin/bash /Users/rajiv/.claude/scripts/proxy-agent.sh --model muse-spark-1.3-contributor"]
    rows += [f"/opt/homebrew/bin/claude -p --name px-{i} --model {model}" for i, model in enumerate(models)]
    return "\n".join(rows)


# ---- cap ----

def test_cap_defaults_to_two_when_the_file_is_missing_or_has_no_valid_line(tmp_path):
    assert composer.opencode_cap(tmp_path / "missing") == (2, True)
    caps = tmp_path / "caps"
    caps.write_text("GROK_PROXY_CAP=6\nOPENCODE_PROXY_CAP=abc\nOPENCODE_PROXY_CAP=\n")
    assert composer.opencode_cap(caps) == (2, True)


def test_cap_is_the_first_valid_opencode_line_like_the_launcher(tmp_path):
    caps = tmp_path / "caps"
    caps.write_text("CODEX_PROXY_CAP=10\nOPENCODE_PROXY_CAP=x\nOPENCODE_PROXY_CAP=4\nOPENCODE_PROXY_CAP=9\n")
    assert composer.opencode_cap(caps) == (4, False)


# ---- live ----

def test_live_counts_only_real_claude_processes_per_opencode_model():
    live = composer.opencode_live(ps(HY4, DEEP, DEEP, "claude-sonnet-5-5", "grok-4.7", MUSE))
    assert live == {HY4: 1, DEEP: 2, MUSE: 1}


# ---- launches in the last hour ----

def test_launches_count_only_opencode_models_started_inside_the_hour(tmp_path):
    manifest(tmp_path, "px-a", HY4, stamp(60))
    manifest(tmp_path, "px-b", DEEP, stamp(3000))
    manifest(tmp_path, "px-c", DEEP, stamp(3500))
    manifest(tmp_path, "px-d", MUSE, stamp(3599))
    manifest(tmp_path, "px-old", HY4, stamp(3700))  # started before the window
    manifest(tmp_path, "px-claude", "claude-sonnet-5-5", stamp(30))  # not an OpenCode model
    (tmp_path / "stray.lock").write_text("")  # not a run directory
    (tmp_path / "px-empty").mkdir()  # run directory without a manifest
    counts, unreadable = composer.opencode_launches(NOW, tmp_path)
    assert counts == {HY4: 1, DEEP: 2, MUSE: 1}
    assert unreadable == 0


def test_unreadable_or_incomplete_manifests_are_counted_not_dropped(tmp_path):
    (tmp_path / "px-bad").mkdir()
    (tmp_path / "px-bad" / "manifest.json").write_text("{not json")
    (tmp_path / "px-list").mkdir()
    (tmp_path / "px-list" / "manifest.json").write_text("[]")
    manifest(tmp_path, "px-nostart", DEEP, "")  # OpenCode model with no usable started_at
    manifest(tmp_path, "px-ok", HY4, stamp(10))
    counts, unreadable = composer.opencode_launches(NOW, tmp_path)
    assert counts == {HY4: 1, DEEP: 0, MUSE: 0}
    assert unreadable == 3


def test_an_unlistable_registry_raises_instead_of_reading_zero(tmp_path):
    with pytest.raises(OSError):
        composer.opencode_launches(NOW, tmp_path / "nope")


# ---- Muse emergency log ----

def test_muse_log_missing_is_zero_and_unreadable_raises(tmp_path):
    assert composer.muse_emergency_launches(NOW, tmp_path / "none.log") == 0
    with pytest.raises(OSError):
        composer.muse_emergency_launches(NOW, tmp_path)  # a directory, not a log


def test_muse_primary_and_alias_lines_of_one_launch_count_once(tmp_path):
    log = tmp_path / "muse.log"
    log.write_text(log_line(120, "px-one", "primary") + log_line(120, "px-one", "haiku")
                   + log_line(119, "px-one", "sonnet") + log_line(119, "px-one", "opus")
                   + log_line(300, "px-two", "primary"))
    assert composer.muse_emergency_launches(NOW, log) == 2


def test_muse_same_name_apart_is_two_launches_and_old_or_garbage_lines_are_ignored(tmp_path):
    log = tmp_path / "muse.log"
    log.write_text(log_line(1800, "px-a") + log_line(600, "px-a")  # 20 minutes apart: two launches
                   + log_line(3700, "px-old")  # outside the hour
                   + "not a log line\n"
                   + "2026-13-45T99:99:99Z EMERGENCY_MUSE name=px-bad\n"  # unparsable stamp
                   + log_line(-600, "px-future"))  # stamped well after now
    assert composer.muse_emergency_launches(NOW, log) == 2


# ---- the line ----

def test_line_is_counts_only_with_cap_live_launched_and_muse(tmp_path):
    caps = tmp_path / "caps"
    caps.write_text("OPENCODE_PROXY_CAP=3\n")
    runs = tmp_path / "runs"
    runs.mkdir()
    manifest(runs, "px-a", HY4, stamp(60))
    manifest(runs, "px-b", DEEP, stamp(600))
    manifest(runs, "px-c", DEEP, stamp(900))
    log = tmp_path / "muse.log"
    log.write_text(log_line(200, "px-m"))
    line = composer.opencode_line(NOW, caps=caps, root=runs, log=log, ps_text=ps(DEEP, MUSE))
    assert line == ("*OpenCode Go:* cap 3 · live now/launched last 1h: pool 2/3 · hy4-flash-free 0/1 · "
                    "deepseek-v4.1-flash 1/2 · muse-spark-1.3-contributor 1/0 · Muse emergency launches last 1h: 1")
    for banned in ("$", "%", "/h", "cost", "rate", "per hour"):
        assert banned not in line.lower()


def test_line_marks_the_default_cap_and_zero_counts(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    line = composer.opencode_line(NOW, caps=tmp_path / "caps", root=runs, log=tmp_path / "muse.log", ps_text=ps())
    assert line == ("*OpenCode Go:* cap 2 (default) · live now/launched last 1h: pool 0/0 · hy4-flash-free 0/0 · "
                    "deepseek-v4.1-flash 0/0 · muse-spark-1.3-contributor 0/0 · Muse emergency launches last 1h: 0")


def test_line_notes_unreadable_manifests(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "px-bad").mkdir()
    (runs / "px-bad" / "manifest.json").write_text("{")
    line = composer.opencode_line(NOW, caps=tmp_path / "caps", root=runs, log=tmp_path / "muse.log", ps_text=ps())
    assert line.endswith(" · 1 run manifests unreadable")


def test_each_unreadable_source_reads_unavailable_never_zero(monkeypatch, tmp_path):
    def no_ps(*args, **kwargs):
        raise OSError("ps unavailable")

    monkeypatch.setattr(subprocess, "run", no_ps)
    log_dir = tmp_path / "log-is-a-directory"
    log_dir.mkdir()
    line = composer.opencode_line(NOW, caps=tmp_path / "caps", root=tmp_path / "no-runs", log=log_dir)
    assert line == ("*OpenCode Go:* cap 2 (default) · live now/launched last 1h: pool UNAVAILABLE/UNAVAILABLE · "
                    "hy4-flash-free UNAVAILABLE/UNAVAILABLE · deepseek-v4.1-flash UNAVAILABLE/UNAVAILABLE · "
                    "muse-spark-1.3-contributor UNAVAILABLE/UNAVAILABLE · Muse emergency launches last 1h: UNAVAILABLE")


def test_a_live_failure_does_not_hide_the_launch_counts(monkeypatch, tmp_path):
    def no_ps(*args, **kwargs):
        raise subprocess.TimeoutExpired("ps", 5)

    monkeypatch.setattr(subprocess, "run", no_ps)
    runs = tmp_path / "runs"
    runs.mkdir()
    manifest(runs, "px-a", HY4, stamp(60))
    line = composer.opencode_line(NOW, caps=tmp_path / "caps", root=runs, log=tmp_path / "muse.log")
    assert "pool UNAVAILABLE/1" in line
    assert "hy4-flash-free UNAVAILABLE/1" in line
    assert "Muse emergency launches last 1h: 0" in line


# ---- placement in the hourly report ----

def test_compose_places_the_line_under_the_burn_block_on_1h_only():
    now = datetime.now(timezone.utc)
    line = "*OpenCode Go:* cap 2 (default) · live now/launched last 1h: pool 0/0"
    burn = ("*Burn:* pace to Claude · Codex; headroom = budget - burn, %/h; mode is per pool\n"
            "Claude pool: burn 0.10 → OK → Claude routes: NORMAL",
            {"Claude": "NORMAL", "Codex": "NORMAL"})
    hourly = composer.compose("1h", now, {"events": 0}, {}, [], burn=burn, opencode=line).splitlines()
    three = composer.compose("3h", now, {"events": 0}, {}, [], opencode=line)
    assert line in hourly
    assert hourly.index(line) > hourly.index("Claude pool: burn 0.10 → OK → Claude routes: NORMAL")
    assert line not in three
    assert "*OpenCode Go:*" not in composer.compose("1h", now, {"events": 0}, {}, [])
