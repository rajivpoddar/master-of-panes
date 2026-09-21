#!/usr/bin/env python3
"""Bounded, strictly read-only preview of one accepted Codex thread queue.

Model-neutral by construction: this is an ordinary shell entrypoint that speaks
the app-server JSON-RPC framing directly. It has no codex_app tool dependency,
no Responses API use, no OpenAI credential read, and it never calls a model or a
provider. Any routed non-OpenAI model can run it the same way a native OpenAI
model can.

READ-ONLY ALLOWLIST: initialize, initialized, thread/queue/list. It never adds,
starts, resumes, reorders, updates, deletes, claims or acknowledges anything, and
never touches thread/model configuration or interrupts active work. A peek is
not consumption, execution ownership, or new authority.

The outbound method list is reported in every terminal so the read-only claim is
auditable rather than asserted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TextIO

THREAD_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
DEFAULT_CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"
ALLOWED_METHODS = ("initialize", "initialized", "thread/queue/list")
MAX_BODY_CHARS = 200_000
MAX_LINE_BYTES = 8_000_000


class RpcError(RuntimeError):
    def __init__(self, code: Any, message: str) -> None:
        super().__init__(f"rpc-error code={code} message={message}")
        self.code = code
        self.message = message


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="queue_preview.py",
        description="Read-only, bounded preview of one Codex thread queue (model-neutral).",
    )
    parser.add_argument("--thread-id", required=True, help="Exact Codex task/thread id")
    parser.add_argument("--limit", type=int, default=20, help="Page size, 1..100 (default 20)")
    parser.add_argument("--cursor", default=None, help="Opaque page cursor from a previous call")
    parser.add_argument("--timeout-seconds", type=float, default=15.0, help="Bounded RPC timeout")
    parser.add_argument("--preview-chars", type=int, default=200, help="Per-entry preview cap, 0..2000")
    parser.add_argument("--codex-bin", default=DEFAULT_CODEX_BIN, help="Absolute path to the codex binary")
    return parser.parse_args()


def emit(payload: dict[str, Any], exit_code: int) -> int:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()
    return exit_code


def write_json(stream: Any, payload: dict[str, Any]) -> None:
    stream.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
    stream.flush()


def _read_complete_lines(
    process: subprocess.Popen,
    selector: selectors.BaseSelector,
    deadline: float,
    buffer: bytearray,
) -> None:
    """Read raw chunks under the deadline until at least one line completes.

    The buffer is owned by the CALLER and persists across calls, so a line
    delivered in several chunks still assembles. Every read is a bounded raw
    read gated on selector readiness, so a server that flushes half a line and
    then stalls cannot block past the deadline the way a text readline() would.
    """

    completed = False
    while not completed:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("app-server-read-deadline-exceeded")
        if not selector.select(remaining):
            raise TimeoutError("app-server-read-deadline-exceeded")
        assert process.stdout is not None
        chunk = os.read(process.stdout.fileno(), 65_536)
        if not chunk:
            raise RuntimeError(f"app-server-exited code={process.poll()} while-reading")
        buffer.extend(chunk)
        if len(buffer) > MAX_LINE_BYTES:
            raise RuntimeError("app-server-response-exceeds-bounded-line-buffer")
        completed = b"\n" in buffer


def read_response(
    process: subprocess.Popen,
    selector: selectors.BaseSelector,
    request_id: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    buffer = bytearray()
    while True:
        _read_complete_lines(process, selector, deadline, buffer)
        while b"\n" in buffer:
            raw, _, rest = buffer.partition(b"\n")
            buffer[:] = rest
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") != request_id:
                continue
            error = payload.get("error")
            if isinstance(error, dict):
                raise RpcError(error.get("code"), str(error.get("message", "request-failed")))
            return payload


def entry_text(entry: dict[str, Any]) -> str:
    raw = entry.get("input")
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def shape_entry(entry: dict[str, Any], preview_chars: int) -> dict[str, Any]:
    text = entry_text(entry)
    body = text[:MAX_BODY_CHARS]
    preview = body[:preview_chars] if preview_chars > 0 else ""
    return {
        "id": entry.get("id"),
        "clientUserMessageId": entry.get("clientUserMessageId"),
        "input_chars": len(text),
        "preview": preview,
        "preview_truncated": len(body) > len(preview) or len(text) > len(body),
        "body_truncated": len(text) > MAX_BODY_CHARS,
    }


def main() -> int:
    args = parse_args()
    methods_sent: list[str] = []
    validation = (
        THREAD_ID_RE.fullmatch(args.thread_id or "") is None
        or not (1 <= args.limit <= 100)
        or not (0 <= args.preview_chars <= 2000)
        or not (0 < args.timeout_seconds <= 120)
    )
    if validation:
        return emit(
            {
                "status": "queue_preview_invalid_args",
                "error": "require an exact thread id, limit 1..100, preview-chars 0..2000, timeout 0..120s",
                "methods_sent": methods_sent,
            },
            2,
        )
    codex_bin = Path(args.codex_bin).expanduser()
    if not codex_bin.is_absolute() or not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
        return emit(
            {
                "status": "queue_preview_unavailable",
                "error": "codex-bin-must-be-an-executable-absolute-path",
                "codex_bin": str(codex_bin),
                "methods_sent": methods_sent,
            },
            2,
        )

    process: subprocess.Popen[str] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        process = subprocess.Popen(
            [str(codex_bin), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            bufsize=0,
        )
        assert process.stdin is not None and process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)

        methods_sent.append("initialize")
        write_json(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex-stdio-send-message", "version": "1.1.0"},
                    "capabilities": {"experimentalApi": True, "requestAttestation": False},
                },
            },
        )
        read_response(process, selector, 1, args.timeout_seconds)
        methods_sent.append("initialized")
        write_json(process.stdin, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

        params: dict[str, Any] = {"threadId": args.thread_id, "limit": args.limit}
        if args.cursor:
            params["cursor"] = args.cursor
        methods_sent.append("thread/queue/list")
        write_json(
            process.stdin,
            {"jsonrpc": "2.0", "id": 2, "method": "thread/queue/list", "params": params},
        )
        response = read_response(process, selector, 2, args.timeout_seconds)
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            return emit(
                {
                    "status": "queue_preview_malformed",
                    "error": "thread/queue/list result did not contain a data array",
                    "thread_id": args.thread_id,
                    "methods_sent": methods_sent,
                },
                3,
            )
        raw_entries = result["data"]
        if any(
            not isinstance(entry, dict)
            or ("id" in entry and not isinstance(entry.get("id"), str))
            for entry in raw_entries
        ):
            return emit(
                {
                    "status": "queue_preview_malformed",
                    "error": "thread/queue/list returned an invalid entry; refusing to report an empty queue",
                    "thread_id": args.thread_id,
                    "invalid_entries": sum(1 for e in raw_entries if not isinstance(e, dict)),
                    "methods_sent": methods_sent,
                },
                3,
            )
        data = raw_entries
        next_cursor = result.get("nextCursor") if isinstance(result.get("nextCursor"), str) else None
        return emit(
            {
                "status": "queue_preview_ok",
                "thread_id": args.thread_id,
                "limit": args.limit,
                "cursor": args.cursor,
                "nextCursor": next_cursor,
                "has_more": next_cursor is not None,
                "count": len(data),
                "entries": [shape_entry(e, args.preview_chars) for e in data],
                "read_only": True,
                "methods_sent": methods_sent,
            },
            0,
        )
    except TimeoutError as error:
        return emit(
            {"status": "queue_preview_timeout", "error": str(error), "thread_id": args.thread_id,
             "methods_sent": methods_sent}, 3,
        )
    except RpcError as error:
        return emit(
            {"status": "queue_preview_unavailable", "error": error.message, "code": error.code,
             "thread_id": args.thread_id, "methods_sent": methods_sent}, 3,
        )
    except Exception as error:  # noqa: BLE001 - never render an error as an empty queue
        return emit(
            {"status": "queue_preview_unavailable", "error": f"{type(error).__name__}: {error}",
             "thread_id": args.thread_id, "methods_sent": methods_sent}, 3,
        )
    finally:
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
                process.terminate()
                try:
                    process.wait(timeout=3)
                except Exception:
                    process.kill()
                    process.wait(timeout=3)
            except Exception:
                pass
        if selector is not None:
            selector.close()


if __name__ == "__main__":
    raise SystemExit(main())
