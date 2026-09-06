from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[3]
SOURCE = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "stale-process-cleanup.py"
MANIFEST = ROOT / "scripts" / "pm" / "shared-assets" / "manifest.json"


def _module():
    spec = importlib.util.spec_from_file_location("stale_process_cleanup", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state(**overrides):
    state = {
        "slot": 3,
        "status": "free",
        "occupied": 0,
        "idle": 1,
        "dnd": 0,
        "activity": None,
        "issue": None,
        "task": None,
        "repository_id": None,
        "branch": None,
        "branch_ref": None,
        "pr": None,
        "head_sha": None,
        "work_kind": None,
        "handoff_id": None,
        "claimed_at": None,
        "active_turn_state": "inactive",
        "active_turn_id": None,
        "last_activity": "2026-09-07T00:00:00Z",
        "root": "/Users/rajiv/Downloads/projects/heydonna-app-3003",
    }
    state.update(overrides)
    return state


def test_age_alone_never_authorizes_slot_backend_cleanup() -> None:
    module = _module()
    assert "super-secret" not in module.sanitize_command("convex dev --token super-secret")
    assert "<redacted>" in module.sanitize_command("convex dev --token super-secret")
    assert module.stale_owner_evidence(_state()) == "mop_free_idle_inactive_no_assignment"
    for state in (
        _state(status="active", occupied=1, idle=0),
        _state(status="held", occupied=0, idle=1),
        _state(dnd=1),
        _state(active_turn_state="active", active_turn_id="turn-1"),
        _state(active_turn_state="indeterminate"),
        _state(activity="running tests"),
        _state(task="retained task"),
        _state(repository_id="heydonna-app/heydonna-app"),
        _state(branch_ref="refs/heads/fix/7635"),
        _state(head_sha="a" * 40),
        _state(work_kind="repro"),
        _state(handoff_id="handoff-1"),
        _state(claimed_at="2026-09-07T00:00:00Z"),
        _state(active_turn_state="inactive", active_turn_id="turn-1"),
        _state(active_turn_state=None),
        _state(issue="#7635"),
        _state(state_missing=True),
    ):
        assert module.stale_owner_evidence(state) is None


def test_slot_authority_schema_is_required_before_cleanup() -> None:
    module = _module()
    short_row = "|".join(["3", "free", "0", "1", "0"] + [""] * 13)
    short = subprocess.CompletedProcess([], 0, short_row + "\n", "")
    with mock.patch.object(module, "run", return_value=short):
        assert module.slot_states() == {}


def test_active_old_convex_watch_is_preserved() -> None:
    module = _module()
    ps_all = subprocess.CompletedProcess([], 0, "3003 1 501 03:42:00 /usr/bin/convex dev\n", "")
    ps_one = subprocess.CompletedProcess([], 0, "3003 1 501 /usr/bin/convex dev\n", "")
    sqlite = subprocess.CompletedProcess([], 0, "3|active|1|0|0|running tests|#7635|task|repo|fix/7635|refs/heads/fix/7635|7635|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|repro|handoff|2026-09-07T00:00:00Z|active|turn-1|2026-09-07T00:00:00Z\n", "")

    def fake_run(args, timeout=5):
        if args[0] == "sqlite3":
            return sqlite
        return ps_all if "-axo" in args else ps_one

    with mock.patch.object(module, "run", side_effect=fake_run), mock.patch.object(
        module, "get_cwd", return_value="/Users/rajiv/Downloads/projects/heydonna-app-3003"
    ):
        result = module.collect_processes(60, 360, 30)
    assert len(result["candidates"]) == 0
    assert result["rows"][0]["skip_reason"] == "no positive stale-owner evidence"


def test_unknown_or_held_slot_state_is_preserved() -> None:
    module = _module()
    row = {
        "pid": 3003,
        "ppid": 1,
        "uid": 501,
        "category": "convex",
        "age_seconds": 9999,
        "cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3003",
        "start_time": "Mon Sep  7 00:00:00 2026",
        "associated_slot": 3,
        "owner_proof": None,
        "command_identity": ("convex", "/usr/bin/convex"),
    }
    with mock.patch.object(module, "revalidate_candidate", return_value=(False, "slot_owner_evidence_changed")), mock.patch.object(
        module.os, "kill"
    ) as kill:
        result = module.kill_candidates([row], 0)
    assert result == [
        {
            "pid": 3003,
            "category": "convex",
            "term_sent": False,
            "kill_sent": False,
            "status": "identity_or_owner_refused",
            "reason": "slot_owner_evidence_changed",
        }
    ]
    kill.assert_not_called()


def test_proven_orphan_is_revalidated_before_term_and_kill() -> None:
    module = _module()
    row = {
        "pid": 3003,
        "ppid": 1,
        "uid": 501,
        "category": "convex",
        "cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3003",
        "start_time": "Mon Sep  7 00:00:00 2026",
        "associated_slot": 3,
        "owner_proof": "mop_free_idle_inactive_no_assignment",
        "command_identity": ("convex", "/usr/bin/convex"),
    }
    with mock.patch.object(module, "revalidate_candidate", return_value=(True, "identity_and_owner_match")), mock.patch.object(
        module.os, "kill"
    ) as kill, mock.patch.object(module, "alive", return_value=False), mock.patch.object(module.time, "sleep"):
        result = module.kill_candidates([row], 0)
    assert result[0]["status"] == "terminated"
    assert result[0]["term_sent"] is True
    assert result[0]["kill_sent"] is False
    kill.assert_called_once_with(3003, module.signal.SIGTERM)


def test_pid_identity_change_refuses_escalation() -> None:
    module = _module()
    row = {
        "pid": 3003,
        "ppid": 1,
        "uid": 501,
        "category": "convex",
        "cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3003",
        "start_time": "Mon Sep  7 00:00:00 2026",
        "associated_slot": 3,
        "owner_proof": "mop_free_idle_inactive_no_assignment",
        "command_identity": ("convex", "/usr/bin/convex"),
    }
    with mock.patch.object(module, "revalidate_candidate", side_effect=[(True, "identity_and_owner_match"), (False, "process_identity_changed:start_time")]), mock.patch.object(
        module.os, "kill"
    ) as kill, mock.patch.object(module, "alive", return_value=True), mock.patch.object(module.time, "sleep"):
        result = module.kill_candidates([row], 0)
    assert result[0]["status"] == "identity_or_owner_refused_before_kill"
    assert result[0]["reason"] == "process_identity_changed:start_time"
    assert [call.args for call in kill.call_args_list] == [(3003, module.signal.SIGTERM)]


def test_executable_shape_change_refuses_before_term() -> None:
    module = _module()
    row = {
        "pid": 3003,
        "ppid": 1,
        "uid": 501,
        "category": "convex",
        "command_identity": ("convex", "/usr/bin/convex"),
        "cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3003",
        "start_time": "Mon Sep  7 00:00:00 2026",
        "associated_slot": 3,
        "owner_proof": "mop_free_idle_inactive_no_assignment",
    }
    changed = {"pid": 3003, "ppid": 1, "uid": 501, "category": None,
               "command_identity": None, "start_time": row["start_time"], "cwd": row["cwd"]}
    with mock.patch.object(module, "_process_snapshot", return_value=changed), mock.patch.object(
        module, "slot_states", return_value={3: _state()}
    ), mock.patch.object(module.os, "kill") as kill:
        result = module.kill_candidates([row], 0)
    assert result[0]["status"] == "identity_or_owner_refused"
    assert result[0]["reason"] == "process_category_changed"
    kill.assert_not_called()


def test_executable_shape_change_refuses_before_kill() -> None:
    module = _module()
    row = {
        "pid": 3003,
        "ppid": 1,
        "uid": 501,
        "category": "convex",
        "command_identity": ("convex", "/usr/bin/convex"),
        "cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3003",
        "start_time": "Mon Sep  7 00:00:00 2026",
        "associated_slot": 3,
        "owner_proof": "mop_free_idle_inactive_no_assignment",
    }
    before = {"pid": 3003, "ppid": 1, "uid": 501, "category": "convex",
              "command_identity": row["command_identity"], "start_time": row["start_time"], "cwd": row["cwd"]}
    after = {**before, "category": "nextjs", "command_identity": ("nextjs", "/usr/bin/node")}
    with mock.patch.object(module, "_process_snapshot", side_effect=[before, after]), mock.patch.object(
        module, "slot_states", return_value={3: _state()}
    ), mock.patch.object(module.os, "kill") as kill, mock.patch.object(module, "alive", return_value=True), mock.patch.object(
        module.time, "sleep"
    ):
        result = module.kill_candidates([row], 0)
    assert result[0]["status"] == "identity_or_owner_refused_before_kill"
    assert result[0]["reason"] == "process_category_changed"
    assert [call.args for call in kill.call_args_list] == [(3003, module.signal.SIGTERM)]


def test_owner_authority_change_refuses_before_kill() -> None:
    module = _module()
    row = {
        "pid": 3003,
        "ppid": 1,
        "uid": 501,
        "category": "convex",
        "command_identity": ("convex", "/usr/bin/convex"),
        "cwd": "/Users/rajiv/Downloads/projects/heydonna-app-3003",
        "start_time": "Mon Sep  7 00:00:00 2026",
        "associated_slot": 3,
        "owner_proof": "mop_free_idle_inactive_no_assignment",
    }
    snapshot = {"pid": 3003, "ppid": 1, "uid": 501, "category": "convex",
                "command_identity": row["command_identity"], "start_time": row["start_time"], "cwd": row["cwd"]}
    with mock.patch.object(module, "_process_snapshot", return_value=snapshot), mock.patch.object(
        module, "slot_states", side_effect=[{3: _state()}, {3: _state(status="active", occupied=1, idle=0, active_turn_state="active", active_turn_id="turn-2")}]
    ), mock.patch.object(module.os, "kill") as kill, mock.patch.object(module, "alive", return_value=True), mock.patch.object(
        module.time, "sleep"
    ):
        result = module.kill_candidates([row], 0)
    assert result[0]["status"] == "identity_or_owner_refused_before_kill"
    assert result[0]["reason"] == "slot_owner_evidence_changed"
    assert [call.args for call in kill.call_args_list] == [(3003, module.signal.SIGTERM)]


def test_manifest_maps_source_with_exact_digest_and_mode() -> None:
    module = _module()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["entries"] if item["source_path"] == "claude/scripts/stale-process-cleanup.py")
    assert entry["canonical_target"] == "/Users/rajiv/.claude/scripts/stale-process-cleanup.py"
    assert entry["mode"] == 0o755
    assert entry["sha256"] == __import__("hashlib").sha256(SOURCE.read_bytes()).hexdigest()
    assert module.OUT_JSON == Path("/tmp/stale-process-cleanup-latest.json")
