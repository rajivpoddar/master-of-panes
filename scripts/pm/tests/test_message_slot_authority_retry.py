"""message-slot's PM slot-authority read must survive a transient MoP stall.

2026-10-08 ~16:45Z: an 18.8s MoP event-loop stall made the single 4s
GET /slots/N return nothing, so PM sends failed with
exit=46 reason=mop_slot_authority_unavailable_use_native_mop_operator.
Hermetic: fake MoP server on an ephemeral port, stub send script, temp dirs.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "scripts/pm/shared-assets/claude/skills/message-slot/scripts/message-slot.sh"


def _server(bad_reads: int, payload: dict):
    state = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def do_GET(self):  # noqa: N802
            state["n"] += 1
            body = b"" if state["n"] <= bad_reads else json.dumps(payload).encode()
            self.send_response(200 if body else 503)
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def _run(tmp_path: Path, port: int) -> subprocess.CompletedProcess:
    send = tmp_path / "send.sh"
    send.write_text("#!/usr/bin/env bash\necho SENT \"$@\" >> %s\n" % (tmp_path / "sent.log"))
    send.chmod(0o755)
    env = dict(
        os.environ,
        MOP_BASE=f"http://127.0.0.1:{port}",
        SEND_SCRIPT=str(send),
        MESSAGE_SLOT_ARCHIVE_DIR=str(tmp_path / "archive"),
        PM_VERIFICATION_LEASE_DIR=str(tmp_path / "leases"),
        MESSAGE_SLOT_AUTHORITY_BACKOFF="0",
        MESSAGE_SLOT_AUTHORITY_TIMEOUT="2",
    )
    return subprocess.run(
        ["bash", str(SCRIPT), "6", "status check", "--force", "--from", "PM"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )


def test_transient_authority_outage_is_retried(tmp_path):
    srv, state = _server(2, {"occupied": True, "issue": 1, "pr": None, "assignment_epoch": 7})
    try:
        res = _run(tmp_path, srv.server_port)
    finally:
        srv.shutdown()
    assert res.returncode == 0, res.stderr
    assert "MESSAGE_SLOT_OK" in res.stdout
    assert state["n"] == 3
    assert "--force" in (tmp_path / "sent.log").read_text()


def test_persistent_authority_outage_still_fails_closed(tmp_path):
    srv, state = _server(99, {})
    try:
        res = _run(tmp_path, srv.server_port)
    finally:
        srv.shutdown()
    assert res.returncode == 46
    assert "mop_slot_authority_unavailable_use_native_mop_operator" in res.stderr
    assert state["n"] == 4
    assert not (tmp_path / "sent.log").exists()
