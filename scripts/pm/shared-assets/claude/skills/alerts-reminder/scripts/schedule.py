#!/usr/bin/env python3
"""Schedule one PM-targeted reminder with the existing HeyDonna Alerts bot."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


ALERTS_USER = "U0AJZTN7SM6"
ALERTS_BOT = "B0AJ3HSC2PQ"
TEAM = "TERRYEMM3"
PM_MENTION = "<@U0ALEAYCAUT>"
KEYCHAIN_SERVICE = "com.heydonna.support-email-slack.alerts"
STATE = Path.home() / ".claude/control_plane/alerts-reminder-schedules.json"


def emit(status: str, **fields: object) -> None:
    print(json.dumps({"status": status, **fields}, sort_keys=True))


def slack_call(token: str, method: str, fields: dict[str, object]) -> dict:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        "https://slack.com/api/" + method,
        data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def persist(records: dict) -> None:
    temporary = STATE.with_name(f".{STATE.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(records, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, STATE)
        directory = os.open(STATE.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--thread-ts", required=True)
    parser.add_argument("--post-at", required=True, type=int)
    parser.add_argument("--text-file", required=True, type=Path)
    parser.add_argument("--dedup-key", required=True)
    args = parser.parse_args()

    if not re.fullmatch(r"[CDG][A-Z0-9]+", args.channel):
        emit("BLOCKED", reason="invalid_channel")
        return 2
    if not re.fullmatch(r"\d+\.\d+", args.thread_ts):
        emit("BLOCKED", reason="invalid_thread_ts")
        return 2
    if args.post_at < int(time.time()) + 30:
        emit("BLOCKED", reason="due_time_not_future")
        return 2
    if not args.text_file.is_absolute() or not args.text_file.is_file():
        emit("BLOCKED", reason="missing_absolute_text_file")
        return 2
    text = args.text_file.read_text(encoding="utf-8")
    if not text.strip() or PM_MENTION not in text or not args.dedup_key.strip():
        emit("BLOCKED", reason="missing_text_pm_mention_or_dedup")
        return 2

    payload = {"channel": args.channel, "thread_ts": args.thread_ts,
               "post_at": args.post_at, "text": text}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    credential = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", "rajiv",
         "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True, text=True,
    )
    if credential.returncode or not credential.stdout.strip():
        emit("BLOCKED", reason="alerts_credential_unavailable")
        return 2
    token = credential.stdout.strip()
    try:
        identity = slack_call(token, "auth.test", {})
    except Exception:
        emit("BLOCKED", reason="alerts_identity_unavailable")
        return 2
    if not identity.get("ok") or identity.get("user_id") != ALERTS_USER or identity.get("bot_id") != ALERTS_BOT or identity.get("team_id") != TEAM:
        emit("BLOCKED", reason="alerts_identity_mismatch")
        return 2

    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = STATE.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = json.loads(STATE.read_text()) if STATE.exists() else {}
        prior = records.get(args.dedup_key)
        if prior:
            if prior.get("sha256") != digest:
                emit("BLOCKED", reason="dedup_payload_mismatch", dedup_key=args.dedup_key)
                return 2
            if prior.get("status") == "scheduled":
                emit("ALREADY_SCHEDULED", dedup_key=args.dedup_key,
                     scheduled_message_id=prior["scheduled_message_id"])
                return 0
            emit("BLOCKED", reason="prior_attempt_requires_reconciliation", dedup_key=args.dedup_key)
            return 2

        records[args.dedup_key] = {"status": "attempting", "sha256": digest,
                                   "channel": args.channel, "thread_ts": args.thread_ts,
                                   "post_at": args.post_at}
        persist(records)
        try:
            response = slack_call(token, "chat.scheduleMessage", payload)
        except Exception:
            records[args.dedup_key]["status"] = "uncertain"
            persist(records)
            emit("UNCERTAIN", dedup_key=args.dedup_key)
            return 3

        if not response.get("ok"):
            records[args.dedup_key]["status"] = "rejected"
            records[args.dedup_key]["error"] = response.get("error", "unknown")
            persist(records)
            emit("BLOCKED", reason="slack_rejected", error=records[args.dedup_key]["error"],
                 dedup_key=args.dedup_key)
            return 2
        scheduled_id = response.get("scheduled_message_id")
        message = response.get("message") or {}
        if (not scheduled_id or response.get("channel") != args.channel
                or int(response.get("post_at", -1)) != args.post_at
                or message.get("text") != text or message.get("bot_id") != ALERTS_BOT):
            records[args.dedup_key]["status"] = "uncertain"
            persist(records)
            emit("UNCERTAIN", reason="schedule_receipt_mismatch", dedup_key=args.dedup_key)
            return 3

        records[args.dedup_key].update(status="scheduled", scheduled_message_id=scheduled_id)
        persist(records)
        emit("SCHEDULED", dedup_key=args.dedup_key, scheduled_message_id=scheduled_id,
             channel=args.channel, thread_ts=args.thread_ts, post_at=args.post_at,
             sender=ALERTS_USER)
        return 0


if __name__ == "__main__":
    sys.exit(main())
