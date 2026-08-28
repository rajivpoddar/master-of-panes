from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
RUNNER = SHARED / "claude" / "scripts" / "mop-slot-respawn.sh"


def test_canonical_runner_delegates_the_complete_six_slot_respawn_to_mop() -> None:
    body = RUNNER.read_text(encoding="utf-8")
    for slot in range(1, 7):
        assert f"heydonna-app-300{slot}) SLOT={slot}" in body
        assert f'"$MOP_BASE_URL/slots/$SLOT/respawn"' in body
    assert "tmux send-keys" not in body
    assert "launch-slot-" not in body
    assert ".idle == true and .active_turn_state == \"inactive\"" in body
    assert "already has a pending MoP respawn" in body
    assert 'if [[ $# -ne 0 ]]' in body
    assert '--data \'{"continue_session":true}\'' in body


def test_each_slot_installs_the_same_thin_skill_and_canonical_runner_wrapper() -> None:
    manifest = json.loads((SHARED / "manifest.json").read_text(encoding="utf-8"))
    by_target = {entry["canonical_target"]: entry for entry in manifest["entries"]}
    skill_bodies: set[bytes] = set()
    wrapper_bodies: set[bytes] = set()

    runner_target = "/Users/rajiv/.claude/scripts/mop-slot-respawn.sh"
    assert runner_target in by_target

    for slot in range(1, 7):
        source_root = SHARED / "claude" / "slot-skills" / f"slot{slot}" / "respawn"
        skill_bodies.add((source_root / "SKILL.md").read_bytes())
        wrapper_bodies.add((source_root / "scripts" / "respawn.sh").read_bytes())
        target_root = f"/Users/rajiv/Downloads/projects/heydonna-app-300{slot}/.claude/skills/respawn"
        assert f"{target_root}/SKILL.md" in by_target
        assert f"{target_root}/scripts/respawn.sh" in by_target

    assert len(skill_bodies) == 1
    assert len(wrapper_bodies) == 1
    assert wrapper_bodies.pop() == (
        b'#!/bin/bash\nexec /Users/rajiv/.claude/scripts/mop-slot-respawn.sh "$@"\n'
    )


def test_runner_waits_for_idle_then_posts_one_controlled_respawn() -> None:
    requests: list[tuple[str, str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(("GET", self.path, b""))
            body = b'{"idle":true,"active_turn_state":"inactive"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            payload = self.rfile.read(length)
            requests.append(("POST", self.path, payload))
            body = b'{"status":"RESPAWN_STARTED"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    lock = Path("/tmp/mop-slot-respawn-1.lock")
    log = Path("/tmp/mop-slot-respawn-1.log")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "heydonna-app-3001"
            checkout.mkdir()
            env = dict(os.environ)
            env["MOP_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
            result = subprocess.run(
                ["bash", str(RUNNER)],
                cwd=checkout,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            assert "Respawn queued through MoP for slot 1" in result.stdout

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any(
                method == "POST" for method, _path, _body in requests
            ):
                time.sleep(0.05)

        assert requests.count(("GET", "/slots/1", b"")) == 1
        assert requests.count(
            ("POST", "/slots/1/respawn", b'{"continue_session":true}')
        ) == 1
        assert "MOP_RESPAWN_ACCEPTED slot=1" in log.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        log.unlink(missing_ok=True)
        lock.rmdir() if lock.exists() else None


def test_runner_treats_a_conflicting_respawn_as_a_terminal_refusal() -> None:
    requests: list[tuple[str, str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(("GET", self.path, b""))
            body = b'{"idle":true,"active_turn_state":"inactive"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            payload = self.rfile.read(length)
            requests.append(("POST", self.path, payload))
            body = b'{"error":"respawn already in progress"}'
            self.send_response(409)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    lock = Path("/tmp/mop-slot-respawn-1.lock")
    log = Path("/tmp/mop-slot-respawn-1.log")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "heydonna-app-3001"
            checkout.mkdir()
            env = dict(os.environ)
            env["MOP_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
            result = subprocess.run(
                ["bash", str(RUNNER)],
                cwd=checkout,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (
                lock.exists() or not log.exists() or "status=409" not in log.read_text()
            ):
                time.sleep(0.05)

        time.sleep(0.6)
        assert requests.count(("GET", "/slots/1", b"")) == 1
        assert requests.count(
            ("POST", "/slots/1/respawn", b'{"continue_session":true}')
        ) == 1
        assert "MOP_RESPAWN_REFUSED slot=1 status=409" in log.read_text(encoding="utf-8")
        assert not lock.exists()
    finally:
        server.shutdown()
        server.server_close()
        log.unlink(missing_ok=True)
        lock.rmdir() if lock.exists() else None
