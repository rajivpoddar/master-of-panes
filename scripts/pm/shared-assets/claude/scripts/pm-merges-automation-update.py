#!/usr/bin/env python3
"""Render a metadata-preserving scheduler update for the PM hourly task.

This adapter never writes automation state.  It validates the active TOML
identity and emits the exact semantic payload consumed by the supported
automation_update boundary; scheduler-owned timestamps remain untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path


REQUIRED = {
    "id": "pr-merges-residency-heartbeat",
    "kind": "heartbeat",
    "name": "PR Merges hourly open-PR audit",
    "status": "ACTIVE",
    "rrule": "FREQ=HOURLY;INTERVAL=1;BYMINUTE=12",
    "target_thread_id": "01a0324b-68e0-7491-988f-e7e1549f16f7",
}


def render(automation_path: Path, prompt_path: Path) -> dict[str, object]:
    try:
        config = tomllib.loads(automation_path.read_text(encoding="utf-8"))
        prompt = prompt_path.read_text(encoding="utf-8")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("automation_source_invalid") from exc
    for key, expected in REQUIRED.items():
        if config.get(key) != expected:
            raise ValueError(f"automation_metadata_mismatch:{key}")
    if not prompt.strip() or "pm-terminal-continuity.py" not in prompt:
        raise ValueError("prompt_empty")
    notification_policy = config.get("notification_policy", config.get("notificationPolicy"))
    if notification_policy not in (None, "failed_runs_only"):
        raise ValueError("automation_notification_policy_invalid")
    prior_prompt = config.get("prompt")
    if not isinstance(prior_prompt, str) or not prior_prompt.strip():
        raise ValueError("automation_prior_prompt_missing")
    metadata = {
        "id": config["id"],
        "kind": config["kind"],
        "name": config["name"],
        "status": config["status"],
        "rrule": config["rrule"],
        "targetThreadId": config["target_thread_id"],
        "notificationPolicy": notification_policy,
        "created_at": config.get("created_at"),
        "updated_at": config.get("updated_at"),
    }
    return {
        "mode": "update",
        "kind": "heartbeat",
        "id": REQUIRED["id"],
        "name": REQUIRED["name"],
        "status": REQUIRED["status"],
        "rrule": REQUIRED["rrule"],
        "targetThreadId": REQUIRED["target_thread_id"],
        "notificationPolicy": notification_policy,
        "prompt": prompt,
        "preserve": ["id", "kind", "name", "status", "rrule", "targetThreadId", "created_at", "updated_at"],
        "rollback_preimage": {
            "automation_path": str(automation_path),
            "metadata": metadata,
            "prompt": prior_prompt,
            "prompt_sha256": hashlib.sha256(prior_prompt.encode("utf-8")).hexdigest(),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("automation_toml", type=Path)
    parser.add_argument("prompt_template", type=Path)
    args = parser.parse_args(argv)
    try:
        result = render(args.automation_toml, args.prompt_template)
    except ValueError as exc:
        print(json.dumps({"status": "REFUSED", "error_class": str(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
