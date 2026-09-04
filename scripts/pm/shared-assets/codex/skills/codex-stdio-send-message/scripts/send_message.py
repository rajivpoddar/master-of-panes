#!/usr/bin/env python3
"""Queue one exact message in an existing Codex Desktop task over stdio."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO


DEFAULT_CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_MESSAGE_BYTES = 256_000
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class RpcError(RuntimeError):
    """A definite JSON-RPC rejection."""

    def __init__(self, code: Any, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"app-server-rpc-error code={code} message={message}")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Queue one exact message in an existing Codex Desktop task."
    )
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--dedup-key", required=True)
    message_group = parser.add_mutually_exclusive_group(required=True)
    message_group.add_argument("--message")
    message_group.add_argument("--message-file")
    parser.add_argument("--codex-bin", default=DEFAULT_CODEX_BIN)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args()


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)


def read_message(args: argparse.Namespace) -> str:
    if args.message is not None:
        message = args.message
    elif args.message_file == "-":
        message = sys.stdin.read()
    else:
        message_path = Path(args.message_file).expanduser()
        if not message_path.is_absolute():
            raise ValueError("message-file-must-be-absolute")
        message = message_path.read_text(encoding="utf-8")
    if not message.strip():
        raise ValueError("message-must-not-be-empty")
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError(f"message-exceeds-{MAX_MESSAGE_BYTES}-bytes")
    return message


def stable_client_user_message_id(dedup_key: str) -> str:
    digest = bytearray(
        hashlib.sha256(f"codex-stdio-send-message:{dedup_key}".encode()).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    hex_value = digest.hex()
    return (
        f"{hex_value[:8]}-{hex_value[8:12]}-{hex_value[12:16]}-"
        f"{hex_value[16:20]}-{hex_value[20:]}"
    )


def write_json(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stream.flush()


def read_response(
    process: subprocess.Popen[str],
    selector: selectors.BaseSelector,
    request_id: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"app-server-request-timeout id={request_id}")
        if not selector.select(remaining):
            raise TimeoutError(f"app-server-request-timeout id={request_id}")
        assert process.stdout is not None
        line = process.stdout.readline()
        if line == "":
            raise RuntimeError(
                f"app-server-exited code={process.poll()} while-waiting-for={request_id}"
            )
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") != request_id:
            continue
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message", "request-failed")
            raise RpcError(code, message)
        return payload


def main() -> int:
    args = parse_args()
    process: subprocess.Popen[str] | None = None
    queue_attempted = False
    selector: selectors.BaseSelector | None = None
    try:
        if not TASK_ID_RE.fullmatch(args.thread_id):
            raise ValueError("invalid-thread-id")
        if not args.dedup_key or len(args.dedup_key) > 512:
            raise ValueError("dedup-key-must-be-1-to-512-characters")
        if args.timeout_seconds <= 0 or args.timeout_seconds > 120:
            raise ValueError("timeout-seconds-must-be-greater-than-0-and-at-most-120")
        codex_bin = Path(args.codex_bin).expanduser()
        if not codex_bin.is_absolute() or not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
            raise ValueError("codex-bin-must-be-an-executable-absolute-path")
        message = read_message(args)
        client_message_id = stable_client_user_message_id(args.dedup_key)

        process = subprocess.Popen(
            [str(codex_bin), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        assert process.stdin is not None and process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)

        write_json(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex-stdio-send-message",
                        "version": "1.0.0",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                    },
                },
            },
        )
        read_response(process, selector, 1, args.timeout_seconds)
        write_json(
            process.stdin,
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        )

        queue_attempted = True
        write_json(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "thread/queue/add",
                "params": {
                    "threadId": args.thread_id,
                    "input": [{"type": "text", "text": message, "text_elements": []}],
                    "clientUserMessageId": client_message_id,
                },
            },
        )
        response = read_response(process, selector, 2, args.timeout_seconds)
        result = response.get("result")
        queued = result.get("queuedSubmission") if isinstance(result, dict) else None
        if not isinstance(queued, dict):
            raise RuntimeError("queue-response-missing-queued-submission")
        submission_id = queued.get("id")
        returned_client_id = queued.get("clientUserMessageId")
        if not isinstance(submission_id, str) or not submission_id:
            raise RuntimeError("queue-response-missing-submission-id")
        if returned_client_id != client_message_id:
            raise RuntimeError("queue-response-mismatched-client-message-id")
        emit(
            {
                "status": "queued_for_task_consumption",
                "threadId": args.thread_id,
                "queuedSubmissionId": submission_id,
                "clientUserMessageId": client_message_id,
                "queueAccepted": True,
                "executionOwnership": "queued",
            }
        )
        return 0
    except ValueError as error:
        emit({"status": "invalid", "error": str(error)})
        return 4
    except RpcError as error:
        emit({"status": "unavailable", "error": str(error)})
        return 2
    except (BrokenPipeError, OSError, RuntimeError, TimeoutError) as error:
        status = "uncertain" if queue_attempted else "unavailable"
        emit({"status": status, "error": str(error)})
        return 3 if queue_attempted else 2
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
