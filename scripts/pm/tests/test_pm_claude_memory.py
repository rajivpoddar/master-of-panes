"""PM Claude RSS hourly line and the once-per-6h exit-pending arm.

Simulated RSS only. The arm opener is a fake; nothing calls MoP or tmux.
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/pm-claude-memory.py"
spec = importlib.util.spec_from_file_location("pm_claude_memory", PATH)
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)

COMPOSE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py"
compose_spec = importlib.util.spec_from_file_location("heartbeat_compose_memory", COMPOSE)
composer = importlib.util.module_from_spec(compose_spec)
compose_spec.loader.exec_module(composer)

GB = 1024 ** 3


def processes():
    # pane shell 10 -> PM claude 20 (2.4GB) -> subagent claude 21 (3GB). px-foo 30 is 1.6GB. px-small 31 is under.
    return {
        10: (1, 1000, "zsh"),
        20: (10, int(2.4 * GB / 1024), "/usr/local/bin/claude --model opus"),
        21: (20, int(3.0 * GB / 1024), "/usr/local/bin/claude --name subagent"),
        30: (1, int(1.6 * GB / 1024), "/usr/local/bin/claude --name px-foo --model opus"),
        31: (1, int(1.0 * GB / 1024), "/usr/local/bin/claude --name px-small"),
        32: (1, int(1.8 * GB / 1024), "/usr/bin/node --name px-not-claude"),
    }


def test_nearest_claude_is_the_pane_claude_not_the_nested_subagent():
    found = memory.nearest_claude(10, processes())
    assert found is not None
    assert found[0] == 20
    assert found[1] == int(2.4 * GB / 1024) * 1024


def test_px_list_keeps_only_claude_processes_at_or_over_1_5gb():
    rows = memory.px_over(processes())
    assert rows == [("px-foo", 30, int(1.6 * GB / 1024) * 1024)]


def test_line_includes_pm_rss_and_px_over_floor():
    line = memory.format_line((20, int(2.4 * GB / 1024) * 1024), memory.px_over(processes()), "below")
    assert line.startswith("*Claude RSS:* PM 2.40GB pid 20")
    assert "px-foo 1.60GB pid 30" in line
    assert "px-small" not in line
    assert "exit-pending" not in line


def test_threshold_arms_once_then_cooldown(tmp_path):
    state = tmp_path / "state.json"
    calls = []

    def opener(payload):
        calls.append(payload)
        return {"ok": True, "status": {"pm_only": True, "with_devs": False}}

    snap = {"pm": (20, 2 * GB), "px": []}
    now = datetime.fromtimestamp(1_700_000_000, timezone.utc)
    first = memory.hourly_line(
        arm=True, now=now, collect=lambda: snap, state_path=state,
        status_get=lambda: {"pending": False}, opener=opener, env={},
    )
    second = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000 + 3600, timezone.utc),
        collect=lambda: snap, state_path=state, status_get=lambda: {"pending": False},
        opener=opener, env={},
    )
    assert "exit-pending armed" in first
    assert calls[0]["pm_only"] is True and calls[0]["with_devs"] is False
    assert calls[0]["continuation_prompt"]
    assert len(calls) == 1
    assert "exit-pending cooldown" in second


def test_under_threshold_and_env_override_do_not_arm_or_call_mop(tmp_path):
    state = tmp_path / "state.json"
    called = []
    snap = {"pm": (20, 2 * GB - 1), "px": []}
    line = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000, timezone.utc),
        collect=lambda: snap, state_path=state, status_get=lambda: called.append("status"),
        opener=lambda _payload: called.append("arm"), env={},
    )
    assert "exit-pending" not in line
    assert called == []
    armed = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000, timezone.utc),
        collect=lambda: snap, state_path=state, status_get=lambda: {"pending": False},
        opener=lambda payload: called.append(payload), env={"MOP_PM_RSS_EXIT_BYTES": str(GB)},
    )
    assert "exit-pending armed" in armed
    assert called and called[0]["with_devs"] is False


def test_dry_run_over_threshold_does_not_call_mop(tmp_path):
    state = tmp_path / "state.json"
    called = []
    line = memory.hourly_line(
        arm=False, now=datetime.fromtimestamp(1_700_000_000, timezone.utc),
        collect=lambda: {"pm": (7, 3 * GB), "px": []}, state_path=state,
        status_get=lambda: called.append("status"), opener=lambda _p: called.append("arm"), env={},
    )
    assert "dry-run would arm" in line
    assert called == []
    assert not state.exists()


def test_opener_failure_is_fail_open_and_starts_the_cooldown(tmp_path):
    state = tmp_path / "state.json"

    def opener(_payload):
        raise TimeoutError("lost")

    now = datetime.fromtimestamp(1_700_000_000, timezone.utc)
    first = memory.hourly_line(
        arm=True, now=now, collect=lambda: {"pm": (4, 3 * GB), "px": []}, state_path=state,
        status_get=lambda: {"pending": False}, opener=opener, env={},
    )
    second = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000 + 10, timezone.utc),
        collect=lambda: {"pm": (4, 3 * GB), "px": []}, state_path=state,
        status_get=lambda: {"pending": False}, opener=opener, env={},
    )
    assert "uncertain" in first
    assert "cooldown" in second


def test_unreachable_mop_does_not_raise(tmp_path):
    def status_get():
        raise OSError("down")

    line = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000, timezone.utc),
        collect=lambda: {"pm": (4, 3 * GB), "px": []}, state_path=tmp_path / "state.json",
        status_get=status_get, opener=lambda _p: None, env={},
    )
    assert "MoP unreachable" in line


def test_compose_places_the_rss_line_on_1h_only():
    now = datetime.now(timezone.utc)
    line = "*Claude RSS:* PM 2.40GB pid 20; px over 1.5GB: none"
    hourly = composer.compose("1h", now, {"events": 0}, {}, [], memory=line)
    three = composer.compose("3h", now, {"events": 0}, {}, [], memory=line)
    assert line in hourly
    assert line not in three
    assert "*Claude RSS:*" not in composer.compose("1h", now, {"events": 0}, {}, [])
