from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/pm/shared-assets/claude/scripts/ready-pool-contract-repair.py"
SPEC = importlib.util.spec_from_file_location("ready_pool_contract_repair", SCRIPT)
assert SPEC and SPEC.loader
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


def test_runtime_helper_is_manifested_at_pm_operator_path() -> None:
    manifest = json.loads(
        (ROOT / "scripts/pm/shared-assets/manifest.json").read_text(encoding="utf-8")
    )
    source_path = "claude/scripts/ready-pool-contract-repair.py"
    entry = next(row for row in manifest["entries"] if row["source_path"] == source_path)
    assert entry["canonical_target"] == (
        "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ready-pool-contract-repair.py"
    )
    assert entry["mode"] == 493
    assert entry["sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    assert manifest["inventory"]["selected_count"] == len(manifest["entries"])
    source = SCRIPT.read_text(encoding="utf-8")
    assert "pm-transition-alert.sh" not in source
    assert "message-pm.sh" in source


def verified_message_run(cmd: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    assert cmd[0] == str(repair.PM_MESSAGE)
    assert cmd[1] == "--file"
    message_path = Path(cmd[2])
    return subprocess.CompletedProcess(
        cmd,
        0,
        f"MESSAGE_PM_SENT slot=0 verified=true bytes={len(message_path.read_bytes())}\n",
        "",
    )


def test_cto_decision_uses_supported_transport_and_keeps_durable_obligation() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        old_out_dir = repair.OUT_DIR
        repair.OUT_DIR = tmp
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if cmd[1:3] == ["issue", "view"]:
                return subprocess.CompletedProcess(cmd, 0, "pm-blocked:product\n", "")
            if cmd[1:3] == ["issue", "edit"]:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[0] == str(repair.PM_OPS):
                assert "ready_pool_cto_decision" in cmd
                assert any("Should the stored transcript be preserved?" in part for part in cmd)
                return subprocess.CompletedProcess(cmd, 0, "412\n", "")
            return verified_message_run(cmd, timeout=timeout)

        result = {
            "decision_kind": "product_decision",
            "decision_question": "Should the stored transcript be preserved?",
            "recommended_default": "Preserve it until explicit approval.",
            "evidence": ["candidate-body-sha256:abc123"],
        }
        try:
            with mock.patch.object(repair, "run", side_effect=fake_run):
                receipt, obligation_id = repair.escalate_cto_decision(
                    123, tmp / "result.json", result, 30
                )
        finally:
            repair.OUT_DIR = old_out_dir

        sent_message = next(
            Path(cmd[2]).read_bytes()
            for cmd in calls
            if cmd[0] == str(repair.PM_MESSAGE)
        )
        assert receipt == f"slot=0 verified=true bytes={len(sent_message)}"
        assert obligation_id == 412
        message = next(Path(cmd[2]).read_text() for cmd in calls if cmd[0] == str(repair.PM_MESSAGE))
        assert "Should the stored transcript be preserved?" in message
        assert "Preserve it until explicit approval." in message
        assert not any("pm-transition-alert.sh" in cmd[0] for cmd in calls)


def test_parked_notification_failure_is_typed_and_actionable() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        old_out_dir = repair.OUT_DIR
        repair.OUT_DIR = tmp

        def unavailable(cmd: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd, 75, "", "MESSAGE_PM_FAILED reason=transport_TimeoutError"
            )

        try:
            with mock.patch.object(repair, "run", side_effect=unavailable):
                try:
                    repair.escalate_parked_candidate(
                        123, tmp / "result.json", "DEPENDENCY_WAIT", "waiting for API owner", 30
                    )
                except repair.RepairError as exc:
                    text = str(exc)
                    assert "ready_pool_notification_failed" in text
                    assert "issue=123" in text
                    assert "message_file=" in text
                    assert "transport_TimeoutError" in text
                else:
                    raise AssertionError("failed PM notification was silently accepted")
        finally:
            repair.OUT_DIR = old_out_dir


def test_cto_notification_failure_retains_obligation_and_retry_path() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        old_out_dir = repair.OUT_DIR
        repair.OUT_DIR = tmp

        def unavailable(cmd: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
            if cmd[1:3] == ["issue", "view"] or cmd[1:3] == ["issue", "edit"]:
                return subprocess.CompletedProcess(cmd, 0, "pm-blocked:product\n", "")
            if cmd[0] == str(repair.PM_OPS):
                return subprocess.CompletedProcess(cmd, 0, "412\n", "")
            return subprocess.CompletedProcess(
                cmd, 75, "", "MESSAGE_PM_FAILED reason=transport_TimeoutError"
            )

        try:
            with mock.patch.object(repair, "run", side_effect=unavailable):
                try:
                    repair.escalate_cto_decision(
                        123,
                        tmp / "result.json",
                        {
                            "decision_kind": "product_decision",
                            "decision_question": "Should the stored transcript be preserved?",
                            "recommended_default": "Preserve it until explicit approval.",
                            "evidence": ["candidate-body-sha256:abc123"],
                        },
                        30,
                    )
                except repair.RepairError as exc:
                    text = str(exc)
                    assert "ready_pool_notification_failed" in text
                    assert "durable_obligation_id=412" in text
                    assert "message_file=" in text
                    assert "transport_TimeoutError" in text
                else:
                    raise AssertionError("failed CTO notification was silently accepted")
        finally:
            repair.OUT_DIR = old_out_dir


def run_empty_candidate_case(tmp: Path, *, notification_ok: bool) -> tuple[int, dict]:
    source = tmp / "promoter.json"
    source.write_text(
        json.dumps(
            {
                "repo": repair.REPO,
                "apply": False,
                "current_ready_pool_count": 0,
                "promotion_deficit": 10,
                "needs_ready_pool_metadata": [],
                "skipped": {},
                "would_promote": [],
            }
        ),
        encoding="utf-8",
    )
    old_values = {
        "OUT_DIR": repair.OUT_DIR,
        "ACTIVE_STATE": repair.ACTIVE_STATE,
        "LOCK_FILE": repair.LOCK_FILE,
        "DEBUG_LOG": repair.DEBUG_LOG,
    }
    repair.OUT_DIR = tmp
    repair.ACTIVE_STATE = tmp / "active.json"
    repair.LOCK_FILE = tmp / "repair.lock"
    repair.DEBUG_LOG = tmp / "debug.jsonl"
    try:
        def fake_run(cmd: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
            if cmd[0] == str(repair.PM_MESSAGE) and notification_ok:
                return verified_message_run(cmd, timeout=timeout)
            return subprocess.CompletedProcess(
                cmd, 75, "", "MESSAGE_PM_FAILED reason=transport_TimeoutError"
            )

        with mock.patch.object(repair, "live_ready_pool_count", return_value=0), mock.patch.object(
            repair, "apply_deterministic_repairs", return_value={"priority": [], "failed": []}
        ), mock.patch.object(repair, "record_rejections", return_value=""), mock.patch.object(
            repair, "run", side_effect=fake_run
        ):
            code = repair.main(["--source-manifest", str(source), "--apply"])
        aggregate = max(tmp.glob("pm-ready-pool-contract-fanout-*.json"), key=lambda p: p.stat().st_mtime)
        return code, json.loads(aggregate.read_text(encoding="utf-8"))
    finally:
        for name, value in old_values.items():
            setattr(repair, name, value)


def test_empty_candidate_exhaustion_completes_with_verified_notification() -> None:
    with tempfile.TemporaryDirectory() as raw:
        code, aggregate = run_empty_candidate_case(Path(raw), notification_ok=True)
        assert code == 0
        assert aggregate["candidate_exhausted"] is True
        assert aggregate["status"] == "complete"
        assert aggregate["exhaustion_notification_receipt"].startswith(
            "slot=0 verified=true bytes="
        )


def test_empty_candidate_notification_failure_returns_typed_blocker() -> None:
    with tempfile.TemporaryDirectory() as raw:
        code, aggregate = run_empty_candidate_case(Path(raw), notification_ok=False)
        assert code == 2
        assert aggregate["status"] == "incomplete"
        error = next(row for row in aggregate["results"] if row["status"] == "ERROR")
        assert error["reason"].startswith("ready_pool_notification_failed")
        assert "message_file=" in error["reason"]


if __name__ == "__main__":
    test_runtime_helper_is_manifested_at_pm_operator_path()
    test_cto_decision_uses_supported_transport_and_keeps_durable_obligation()
    test_parked_notification_failure_is_typed_and_actionable()
    test_cto_notification_failure_retains_obligation_and_retry_path()
    test_empty_candidate_exhaustion_completes_with_verified_notification()
    test_empty_candidate_notification_failure_returns_typed_blocker()
    print("ready-pool-contract-repair notifications: PASS")
