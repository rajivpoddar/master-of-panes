from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import zlib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
SOURCE = ROOT / "scripts/pm/shared-assets/claude/scripts/qa-visual-proof-gate.py"
HEAD = "a" * 40
CHANNEL = "C0ALZJHGE49"
THREAD = "1790477819.230219"
FILE_ID = "FTEST12345"


def load_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "scripts/ci").mkdir(parents=True)
    (repo / "scripts/ci/change_scope.py").write_text("# fixture\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    spec = importlib.util.spec_from_file_location("qa_visual_proof_gate_slack_asset", SOURCE)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def png_bytes() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind)
        crc = zlib.crc32(data, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x00\x00\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def receipt(screenshot: bytes) -> dict:
    digest = hashlib.sha256(screenshot).hexdigest()
    return {
        "schema": "heydonna_qa_visual_proof",
        "version": 2,
        "artifact_kind": "slack",
        "pr": 77,
        "issue": 88,
        "head_sha": HEAD,
        "issue_body_sha256": hashlib.sha256(b"current issue body").hexdigest(),
        "slack_channel": CHANNEL,
        "slack_thread_ts": THREAD,
        "scenarios": [{"ac_id": "AC-1", "slack_file_id": FILE_ID, "file_name": "ac-1.png", "sha256": digest}],
    }


def install_slack_harness(monkeypatch, gate, screenshot: bytes, *, missing_file: bool = False):
    digest = hashlib.sha256(screenshot).hexdigest()
    calls = []

    def api(method, token, payload):
        assert token == "synthetic-token"
        calls.append(method)
        if method == "auth.test":
            return {"ok": True, "user_id": gate.SLACK_CTO_USER_ID}
        if method == "conversations.replies":
            return {
                "ok": True,
                "messages": [
                    {"ts": THREAD, "text": "proof thread"},
                    {
                        "ts": "1790477820.230219",
                        "thread_ts": THREAD,
                        "text": f"UI proof for PR #77 at head `{HEAD[:9]}` for layout review.\n• AC-1: ac-1.png (sha256 {digest[:12]}…)",
                        "files": [{"id": FILE_ID}],
                    },
                ],
                "response_metadata": {"next_cursor": ""},
            }
        if method == "files.info":
            if missing_file:
                raise gate.SlackProofError("slack_api_error:files.info:file_not_found")
            return {"ok": True, "file": {"id": FILE_ID, "name": "ac-1.png", "mimetype": "image/png", "size": len(screenshot), "url_private_download": "https://files.slack.com/files-pri/T/F/ac-1.png"}}
        raise AssertionError(f"unexpected API call {method} {payload}")

    monkeypatch.setattr(gate, "load_cto_slack_token", lambda: "synthetic-token")
    monkeypatch.setattr(gate, "slack_api_call", api)
    monkeypatch.setattr(gate, "download_slack_file_bytes", lambda url, token: screenshot)
    return calls


def test_shared_gate_reads_slack_file_bytes_and_binds_thread(monkeypatch, tmp_path) -> None:
    gate = load_gate(monkeypatch, tmp_path)
    screenshot = png_bytes()
    calls = install_slack_harness(monkeypatch, gate, screenshot)
    assert gate.verify_slack_receipt(
        receipt(screenshot), pr=77, issue=88, head=HEAD,
        issue_body_sha=hashlib.sha256(b"current issue body").hexdigest(),
        required_ac_ids=["AC-1"], verify_remote=True
    ) == []
    assert calls == ["auth.test", "conversations.replies", "files.info"]


def test_shared_gate_refuses_stale_head_before_slack_read(monkeypatch, tmp_path) -> None:
    gate = load_gate(monkeypatch, tmp_path)
    screenshot = png_bytes()
    calls = install_slack_harness(monkeypatch, gate, screenshot)
    assert gate.verify_slack_receipt(
        receipt(screenshot), pr=77, issue=88, head="b" * 40,
        issue_body_sha=hashlib.sha256(b"current issue body").hexdigest(),
        required_ac_ids=["AC-1"], verify_remote=True
    ) == ["receipt_head_mismatch"]
    assert calls == []


def test_shared_gate_refuses_same_head_body_edit_before_slack_read(monkeypatch, tmp_path) -> None:
    gate = load_gate(monkeypatch, tmp_path)
    screenshot = png_bytes()
    calls = install_slack_harness(monkeypatch, gate, screenshot)
    assert gate.verify_slack_receipt(
        receipt(screenshot), pr=77, issue=88, head=HEAD,
        issue_body_sha=hashlib.sha256(b"edited issue body").hexdigest(),
        required_ac_ids=["AC-1"], verify_remote=True
    ) == ["receipt_issue_body_mismatch"]
    assert calls == []


def test_shared_gate_refuses_wrong_issue_before_slack_read(monkeypatch, tmp_path) -> None:
    gate = load_gate(monkeypatch, tmp_path)
    screenshot = png_bytes()
    calls = install_slack_harness(monkeypatch, gate, screenshot)
    assert gate.verify_slack_receipt(
        receipt(screenshot), pr=77, issue=89, head=HEAD,
        issue_body_sha=hashlib.sha256(b"current issue body").hexdigest(),
        required_ac_ids=["AC-1"], verify_remote=True
    ) == ["receipt_issue_mismatch"]
    assert calls == []


def test_shared_gate_refuses_missing_slack_file_readback(monkeypatch, tmp_path) -> None:
    gate = load_gate(monkeypatch, tmp_path)
    screenshot = png_bytes()
    install_slack_harness(monkeypatch, gate, screenshot, missing_file=True)
    assert gate.verify_slack_receipt(
        receipt(screenshot), pr=77, issue=88, head=HEAD,
        issue_body_sha=hashlib.sha256(b"current issue body").hexdigest(),
        required_ac_ids=["AC-1"], verify_remote=True
    ) == ["slack_api_error:files.info:file_not_found"]
