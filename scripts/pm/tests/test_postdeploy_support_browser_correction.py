"""Offline proof for C0ALZJHGE49/1791727949.359799; no live cleanup or APIs."""

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


support = load("support_correction", ASSETS / "pm/heartbeat/support-inbox-snapshot.py")
compose = load("compose_correction", ASSETS / "pm/heartbeat/heartbeat-compose.py")
cleanup = load("cleanup_correction", ASSETS / "stale-process-cleanup.py")


def msg(ts, text, user="CUSTOMER"):
    return {"user": user, "ts": str(ts), "text": text}


@pytest.mark.parametrize("text", ["thanks", "ok", "great", "Perfect, thanks a lot", ""])
def test_bare_ack_or_empty_does_not_close_unresolved_ask(text):
    item = support.classify_thread([msg(1000, "export blank"), msg(1100, text)], 5000)
    assert item["state"] == "needs_reply"
    assert item["answered_by"] is None and item["answered_at"] is None
    item["channel"] = "#heydonna-feedback"
    lines, tldr = compose.support_block({"slack": [item]}, "3h")
    assert "NEEDS REPLY" in "\n".join(lines) and "need a reply" in tldr


@pytest.mark.parametrize("text", ["Thanks. Found it", "It worked!", "Resolved."])
def test_explicit_requester_resolution_closes_without_team_attribution(text):
    item = support.classify_thread([msg(1000, "export blank"), msg(1100, text)], 5000)
    assert item["state"] == "answered"
    assert item["answered_by"] is None and item["answered_at"] == 1100.0
    item["channel"] = "#heydonna-feedback"
    lines, tldr = compose.support_block({"slack": [item]}, "3h")
    assert "resolved by requester" in "\n".join(lines)
    assert "answered by" not in "\n".join(lines)
    assert tldr == "support resolved"


@pytest.mark.parametrize("last", ["Thanks. Found it, but export is still blank", "", "thanks"])
def test_reopen_wins_even_with_later_ack(last):
    messages = [msg(1000, "export blank"), msg(1100, "Thanks. Found it"),
                msg(1200, "Still blank; please check again"), msg(1300, last)]
    assert support.classify_thread(messages, 5000)["state"] == "needs_reply"


def test_bare_ack_after_team_reply_remains_team_answered():
    item = support.classify_thread([msg(1000, "export blank"),
                                   msg(1100, "Fixed.", "U0ALEAYCAUT"), msg(1200, "thanks")], 5000)
    assert item["state"] == "answered" and item["answered_by"] == "PM"


def browser_row(pid, ppid, daemon=False, slot=1):
    return {"pid": pid, "ppid": ppid, "uid": os.getuid(), "category": "agent-browser",
            "age_seconds": 18000, "age": "5h", "cwd": "/fake/heydonna-app-3001",
            "start_time": "fixture-start", "command_identity": ("agent-browser", str(pid)),
            "command": "agent-browser-darwin-arm64" if daemon else "Google Chrome for Testing Helper",
            "is_daemon": daemon, "cpu_centis": 100, "associated_slot": slot,
            "slot_free_idle": False, "eligible": False, "skip_reason": "held_or_preserved_slot_checkout",
            "policy": None}


def browser_tree():
    # Actual selection includes a descendant and same-cwd reparented helper.
    return [browser_row(100, 1, True), browser_row(101, 100), browser_row(102, 1)]


def test_flat_cpu_browser_tree_with_live_session_is_not_eligible():
    rows = browser_tree()
    with patch.object(cleanup, "slot_has_live_session", return_value=True), patch.object(
        cleanup, "run", side_effect=AssertionError("no real process observation")
    ):
        cleanup.apply_idle_orphan_policy(rows, {100: 8000}, 120, 30)
    assert not any(row["eligible"] for row in rows)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_live_session_at_signal_boundary_vetoes_daemon_and_helpers(index):
    rows = browser_tree()
    with patch.object(cleanup, "slot_has_live_session", return_value=False):
        cleanup.apply_idle_orphan_policy(rows, {100: 8000}, 120, 30)
    assert all(row["eligible"] for row in rows)
    row = rows[index]
    with patch.object(cleanup, "slot_has_live_session", return_value=True), patch.object(
        cleanup, "_process_snapshot", return_value=dict(row)
    ), patch.object(cleanup, "cpu_times", return_value={100: 100}), patch.object(
        cleanup.os, "kill"
    ) as kill, patch.object(cleanup.time, "sleep"), patch.object(cleanup, "alive", return_value=False), patch.object(
        cleanup, "run", side_effect=AssertionError("no real command")
    ):
        results = cleanup._kill_members([row], 0)
    kill.assert_not_called()
    assert results[0]["outcome"] == "refused" and results[0]["reason"] == "slot_session_alive"


def test_proven_idle_browser_without_live_session_remains_eligible():
    rows = browser_tree()
    with patch.object(cleanup, "slot_has_live_session", return_value=False):
        cleanup.apply_idle_orphan_policy(rows, {100: 8000}, 120, 30)
        for row in rows:
            with patch.object(cleanup, "_process_snapshot", return_value=dict(row)), patch.object(
                cleanup, "cpu_times", return_value={100: 100}
            ):
                assert cleanup.revalidate_candidate(row)[0]
    assert all(row["eligible"] for row in rows)
