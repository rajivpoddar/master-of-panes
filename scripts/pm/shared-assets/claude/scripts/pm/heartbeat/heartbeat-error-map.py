#!/usr/bin/env python3
"""Map Axiom error codes to open issues/PRs, pm-ops obligations and investigations.

Input: the JSON emitted by `axiom-activity-report.py --errors-by-code` (file or
stdin). Output: JSON {code: {status: mapped|NEW, refs: [...]}} for the top codes.

Read-only. pm-ops.db is opened normally with PRAGMA query_only=ON (a -readonly
open fails on the WAL journal).

Usage:
  python3 scripts/axiom-activity-report.py --hours 3 --errors-by-code --compare > /tmp/ax.json
  python3 scripts/pm/heartbeat/heartbeat-error-map.py --axiom-json /tmp/ax.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = "Scribie/heydonna-app"
REPO_ROOT = Path(os.environ.get("HEYDONNA_REPO", str(Path.home() / "Downloads/projects/heydonna-app")))
PM_OPS_DB = Path(
    os.environ.get(
        "PM_OPS_DB",
        str(Path.home() / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"),
    )
)
# Codes too generic to search for; they always map to nothing useful.
GENERIC_CODES = {"unknown", "error", ""}


KNOWN_CODES_PATH = Path(os.environ.get("HEARTBEAT_KNOWN_CODES", str(Path(__file__).resolve().parent / "heartbeat-known-codes.json")))


def load_known_codes(path: Path = KNOWN_CODES_PATH) -> dict:
    """Optional code -> ref map (known causes that have no issue/obligation yet)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def classify_mapping(code: str, issues: list[dict], obligations: list[dict], investigations: list[str],
                     known: dict | None = None) -> dict:
    """Pure: decide mapped vs NEW and build ordered refs (issue/PR first, then known-code ref)."""
    refs: list[str] = []
    for item in issues:
        kind = "PR" if item.get("is_pr") else "issue"
        refs.append(f"#{item['number']} ({kind})")
    for ob in obligations:
        label = f"obl {ob['id']}"
        if ob.get("pr"):
            label += f" PR#{ob['pr']}"
        elif ob.get("issue"):
            label += f" #{ob['issue']}"
        refs.append(label)
    for path in investigations:
        refs.append(Path(path).name)
    if known and code in known:
        refs.append(known[code])
    return {"code": code, "status": "mapped" if refs else "NEW", "refs": refs}


def search_github(code: str) -> list[dict]:
    query = f'repo:{REPO} is:open "{code}"'
    proc = subprocess.run(
        ["gh", "api", "-X", "GET", "search/issues", "-f", f"q={query}", "-f", "per_page=5"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh search failed: {proc.stderr.strip()[:200]}")
    items = json.loads(proc.stdout).get("items", [])
    return [{"number": i["number"], "is_pr": "pull_request" in i, "title": i.get("title", "")} for i in items]


def search_pm_ops(code: str, db: Path = PM_OPS_DB) -> list[dict]:
    if not db.exists():
        raise RuntimeError(f"pm-ops.db missing at {db}")
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA query_only=ON")
        like = f"%{code}%"
        rows = conn.execute(
            "SELECT id, pr, issue, title FROM obligations WHERE status='open' AND ("
            "target_id LIKE ? OR title LIKE ? OR required_action LIKE ? OR blocker LIKE ? OR evidence_json LIKE ?)"
            " ORDER BY id DESC LIMIT 3",
            (like, like, like, like, like),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "pr": r[1], "issue": r[2], "title": r[3]} for r in rows]


def search_investigations(code: str, root: Path = REPO_ROOT / "docs/investigations") -> list[str]:
    if not root.is_dir():
        return []
    hits = []
    for path in sorted(root.glob("*.md"), reverse=True):
        try:
            if code in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(str(path))
        except OSError:
            continue
        if len(hits) >= 2:
            break
    return hits


def map_codes(codes: list[str]) -> tuple[dict, list[str]]:
    result: dict[str, dict] = {}
    failures: list[str] = []
    known = load_known_codes()
    for code in codes:
        if code in GENERIC_CODES:
            result[code] = {"code": code, "status": "generic", "refs": []}
            continue
        issues, obligations = [], []
        try:
            issues = search_github(code)
        except Exception as exc:  # noqa: BLE001 - surface as footer failure, keep mapping
            failures.append(f"gh search {code}: {exc}")
        try:
            obligations = search_pm_ops(code)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"pm-ops {code}: {exc}")
        result[code] = classify_mapping(code, issues, obligations, search_investigations(code), known)
    return result, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--axiom-json", help="axiom --errors-by-code JSON (default: stdin)")
    parser.add_argument("--code", action="append", default=[], help="map an explicit code (repeatable)")
    args = parser.parse_args()
    codes = list(args.code)
    if not codes:
        raw = Path(args.axiom_json).read_text() if args.axiom_json else sys.stdin.read()
        codes = [row["code"] for row in json.loads(raw).get("top", [])]
    mapping, failures = map_codes(codes)
    print(json.dumps({"mapping": mapping, "failures": failures}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
