#!/usr/bin/env python3
"""Audit HeyDonna issue backlog and Ready Pool claimability.

Ready Pool membership is separate from assignment eligibility:
- Dependency-held app work stays in status:todo at every priority (Rajiv S113).
- The selector checks live dependencies before assignment; pool admission does
  not authorize execution of blocked work.
- status:backlog promotion candidates are surfaced to PM ops; the script does
  not auto-promote them in audit mode because that requires product/priority
  judgement.
- promote admits all eligible P0-P2 issues; runnable P3s fill only the remaining
  claimable deficit below the minimum of 10. No Codex arch review is required.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

CONTROL_PLANE_DIR = Path(__file__).resolve().parent
if str(CONTROL_PLANE_DIR) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE_DIR))

from issue_claimability import NON_DISPATCHABLE_CLAIMABLE_TYPES


REPO = os.environ.get("GH_REPO", "Scribie/heydonna-app")
PM_OPS = Path("/Users/rajiv/.claude/scripts/pm-ops.py")
PREFLIGHT_CHECK = Path("/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/handoff/scripts/preflight-check.sh")
ICL_VALIDATOR = Path(
    os.environ.get(
        "HEYDONNA_ICL_VALIDATOR",
        str(CONTROL_PLANE_DIR / "validate-issue-contract-ledger.py"),
    )
)
ICL_GRANDFATHER_SNAPSHOT = Path(
    "/Users/rajiv/Downloads/projects/heydonna-app/.claude/config/icl-grandfathered-body-sha256.json"
)
ICL_ENFORCEMENT_START = os.environ.get(
    "PM_ICL_ENFORCEMENT_START",
    "2026-07-18T19:55:00Z",
)
OUT_DIR = Path("/tmp")
GH = shutil.which("gh") or "/opt/homebrew/bin/gh"

STATUS_PREFIX = "status:"
IMPORTANT_LABELS = {
    "bug",
    "customer-feedback",
    "public-beta",
    "pmf",
    "post-beta",
}
READY_POOL_TARGET = int(os.environ.get("PM_READY_POOL_TARGET", "10") or "10")
HEARTBEAT_RECENT_HOURS = int(os.environ.get("PM_BACKLOG_HEARTBEAT_RECENT_HOURS", "72") or "72")
PROMOTER_REJECTION_TTL_SECONDS = int(os.environ.get("PM_BACKLOG_PROMOTER_REJECTION_TTL_SECONDS", "86400") or "86400")
REJECTION_COOLDOWN_REASONS = {
    "ARCH_REJECTED",
    "DUPLICATE_SUPERSEDED",
    "NEEDS_CTO",
    "TRACKING",
}
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
DEPRECATED_PRIORITY_ALIASES = {
    "high-priority": "P1",
    "priority:p0": "P0",
    "priority:p1": "P1",
    "priority:p2": "P2",
    "priority:p3": "P3",
}
REQUIRED_READY_POOL_FIELDS = [
    "priority",
    "lane",
    "branch_slug",
    "ac_summary",
    "claimable_slot_type",
    "blockers",
    "work_type",
]
BRANCH_SLUG_MAX_LENGTH = 40
BRANCH_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
BRANCH_SLUG_STOP_WORDS = {
    "a",
    "an",
    "and",
    "child",
    "current",
    "from",
    "in",
    "into",
    "make",
    "main",
    "of",
    "on",
    "per",
    "real",
    "reimplemented",
    "residual",
    "split",
    "the",
    "to",
    "v2",
    "with",
}
def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def gh_issue_list() -> list[dict[str, Any]]:
    proc = run(
        [
            GH,
            "issue",
            "list",
            "--repo",
            REPO,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,title,labels,body,createdAt,updatedAt",
        ]
    )
    return json.loads(proc.stdout)


def gh_issue_view(issue: int) -> dict[str, Any]:
    proc = run(
        [
            GH,
            "issue",
            "view",
            str(issue),
            "--repo",
            REPO,
            "--json",
            "number,title,labels,body,createdAt,updatedAt",
        ]
    )
    return json.loads(proc.stdout)


def labels(issue: dict[str, Any]) -> set[str]:
    return {
        str(label.get("name", ""))
        for label in issue.get("labels", [])
        if str(label.get("name", ""))
    }


def canonical_priority_labels(names: set[str]) -> list[str]:
    return sorted(name for name in names if name in PRIORITY_RANK)


def deprecated_priority_aliases(names: set[str]) -> list[str]:
    return sorted(name for name in names if name.lower() in DEPRECATED_PRIORITY_ALIASES)


def priority_label_drift(issue: dict[str, Any], meta: dict[str, str], *, require_label: bool) -> str:
    names = labels(issue)
    canonical = canonical_priority_labels(names)
    aliases = deprecated_priority_aliases(names)
    ready_pool_priority = clean_meta_value(meta.get("priority")).upper()

    if aliases:
        return "deprecated_priority_aliases:" + ",".join(aliases)
    if len(canonical) > 1:
        return "multiple_canonical_priority_labels:" + ",".join(canonical)
    if require_label and not canonical:
        return "missing_canonical_priority_label"
    if canonical and ready_pool_priority in PRIORITY_RANK and canonical[0] != ready_pool_priority:
        return f"ready_pool_priority_mismatch:label={canonical[0]}:ready_pool={ready_pool_priority}"
    return ""


def repairable_ready_pool_priority_drift(drift: str) -> bool:
    return drift.startswith("ready_pool_priority_mismatch:")


_OPEN_PR_NUMS: set[int] | None = None


def open_pr_num_set() -> set[int]:
    """Open PR numbers; a blocker naming an open PR is a live dependency.

    2026-11-02 promoter repair: open_nums held open ISSUES only, so
    `blockers: #12290` (open PR) read as resolved and #12516/#12304 were
    re-flagged resolved_dependency_requires_contract_repair every hourly.
    Lookup failures remain unknown; they cannot prove a dependency resolved.
    """
    global _OPEN_PR_NUMS
    if _OPEN_PR_NUMS is None:
        if os.environ.get("BACKLOG_TRIAGE_SKIP_OPEN_PRS") == "1":
            _OPEN_PR_NUMS = set()
        else:
            try:
                proc = run([GH, "pr", "list", "--repo", REPO, "--state", "open",
                            "--limit", "500", "--json", "number"])
                _OPEN_PR_NUMS = {int(row["number"]) for row in json.loads(proc.stdout)}
            except Exception as exc:
                raise RuntimeError("open_pr_dependency_lookup_failed") from exc
    return _OPEN_PR_NUMS


def issue_num_set(issues: list[dict[str, Any]]) -> set[int]:
    return {int(issue["number"]) for issue in issues} | open_pr_num_set()


def select_heartbeat_triage_issues(
    issues: list[dict[str, Any]],
    *,
    recent_hours: int = HEARTBEAT_RECENT_HOURS,
    now: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Limit heartbeat backlog hygiene to recent backlog plus live dependency waits.

    `updatedAt` is intentionally not used: label/body reconciliation can touch
    the entire historical backlog and make every old issue appear recent. Ready
    Pool/Todo validation belongs to the dispatch-readiness validator, not the
    heartbeat backlog sweep. Old dependency waits stay in scope because
    dependency closure can make them claimable without changing the issue.
    """
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    cutoff = current - dt.timedelta(hours=max(0, recent_hours))
    selected: list[dict[str, Any]] = []
    selected_reasons: dict[str, int] = {
        "recent_created_backlog": 0,
        "dependency_wait_recheck": 0,
    }
    skipped_historical: list[int] = []

    for issue in issues:
        names = labels(issue)
        status_labels = sorted(name for name in names if name.startswith(STATUS_PREFIX))
        if status_labels != ["status:backlog"]:
            skipped_historical.append(int(issue["number"]))
            continue

        created_at = parse_github_time(issue.get("createdAt"))
        if created_at and created_at >= cutoff.timestamp():
            selected.append(issue)
            selected_reasons["recent_created_backlog"] += 1
            continue

        if "pm-blocked:dependency" in names:
            selected.append(issue)
            selected_reasons["dependency_wait_recheck"] += 1
            continue

        skipped_historical.append(int(issue["number"]))

    return selected, {
        "surface": "heartbeat",
        "recent_hours": recent_hours,
        "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "input_open_issues": len(issues),
        "selected_issues": len(selected),
        "selected_reasons": selected_reasons,
        "skipped_historical_backlog": len(skipped_historical),
        "skipped_historical_issue_numbers": sorted(skipped_historical),
    }


def parse_github_time(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except Exception:
        return 0.0


def recent_promoter_rejections() -> dict[int, dict[str, Any]]:
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    rejected: dict[int, dict[str, Any]] = {}
    paths = {
        *glob.glob(str(OUT_DIR / "pm-backlog-promoter-*-result.json")),
        *glob.glob(str(OUT_DIR / "pm-backlog-promoter-rejections-*.json")),
    }
    for path_str in paths:
        path = Path(path_str)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if PROMOTER_REJECTION_TTL_SECONDS > 0 and now - mtime > PROMOTER_REJECTION_TTL_SECONDS:
            continue
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(decoded, list):
            rows = decoded
            accepted: list[Any] = []
            manifest_rows: dict[int, dict[str, Any]] = {}
        elif isinstance(decoded, dict) and decoded.get("review_result"):
            rows = decoded.get("rejected")
            accepted = decoded.get("accepted") or []
            manifest_rows = {}
            manifest_path = Path(str(decoded.get("dry_run_manifest") or ""))
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    manifest = {}
                candidate_groups: list[Any] = [
                    manifest.get("selected"),
                    manifest.get("would_promote"),
                    manifest.get("needs_ready_pool_metadata"),
                ]
                skipped = manifest.get("skipped")
                if isinstance(skipped, dict):
                    candidate_groups.extend(skipped.values())
                for group in candidate_groups:
                    if not isinstance(group, list):
                        continue
                    for candidate in group:
                        if not isinstance(candidate, dict):
                            continue
                        try:
                            number = int(candidate.get("number"))
                        except Exception:
                            continue
                        manifest_rows[number] = candidate
        else:
            continue
        accepted_set = {int(num) for num in accepted if str(num).isdigit()}
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                issue = int(row.get("issue"))
            except Exception:
                continue
            if issue in accepted_set:
                continue
            existing = rejected.get(issue)
            if existing and float(existing.get("mtime", 0)) >= mtime:
                continue
            manifest_row = manifest_rows.get(issue, {})
            rejected[issue] = {
                "mtime": mtime,
                "path": str(path),
                "reason": row.get("reason") or "rejected",
                "detail": row.get("detail") or "",
                "rejection_class": str(row.get("rejection_class") or row.get("reason") or "").upper(),
                "body_sha256": str(row.get("body_sha256") or manifest_row.get("body_sha256") or ""),
                "labels_fingerprint": str(
                    row.get("labels_fingerprint") or manifest_row.get("labels_fingerprint") or ""
                ),
                "dependency_fingerprint": str(
                    row.get("dependency_fingerprint")
                    or manifest_row.get("dependency_fingerprint")
                    or ""
                ),
            }
    return rejected


def ready_pool_blocks(body: str) -> list[str]:
    return re.findall(r"<!--\s*ready-pool:(.*?)-->", body or "", re.S | re.I)


def clean_meta_value(value: Any) -> str:
    cleaned = str(value or "").strip().strip("`\"'").strip()
    return cleaned.rstrip(";,").strip()


def validate_branch_slug(value: Any) -> str:
    slug = clean_meta_value(value)
    if not slug:
        raise ValueError("missing_branch_slug")
    if not BRANCH_SLUG_RE.fullmatch(slug):
        raise ValueError("invalid_branch_slug:" + slug)
    if len(slug) > BRANCH_SLUG_MAX_LENGTH:
        raise ValueError(
            f"branch_slug_exceeds_{BRANCH_SLUG_MAX_LENGTH}_chars:{len(slug)}"
        )
    return slug


def concise_branch_slug(title: Any) -> str:
    normalized = str(title or "")
    normalized = re.sub(
        r"^(?:fix|feat|test|chore|docs|refactor|perf|ci)(?:\([^)]*\))?\s*:\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"#\d+", " ", normalized)
    normalized = re.sub(r"\bq\s*/\s*a\b", " qa ", normalized, flags=re.IGNORECASE)
    tokens = re.findall(r"[a-z0-9]+", normalized.lower())
    tokens = [
        token
        for token in tokens
        if not token.isdigit() and token not in BRANCH_SLUG_STOP_WORDS
    ]
    if not tokens:
        raise ValueError("missing_branch_slug_source")

    selected: list[str] = []
    for token in tokens[:5]:
        candidate = "-".join([*selected, token])
        if len(candidate) > BRANCH_SLUG_MAX_LENGTH:
            break
        selected.append(token)
    if not selected:
        raise ValueError("missing_branch_slug_source")
    return validate_branch_slug("-".join(selected))


def normalize_ready_pool_key(key: Any) -> str:
    return re.sub(r"[\s-]+", "_", str(key or "").strip().lower())


def normalize_ready_pool_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        if not value:
            return "none"
        return ",".join(clean_meta_value(item) for item in value)
    if isinstance(value, dict):
        if not value:
            return "none"
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return clean_meta_value(value)


def add_ready_pool_aliases(meta: dict[str, str]) -> dict[str, str]:
    if "blockers" not in meta:
        for key in ("blocker", "blocked_by", "depends_on", "depends", "dependencies", "dependency"):
            if key in meta:
                meta["blockers"] = meta[key]
                break
    if "claimable_slot_type" not in meta:
        for key in ("claimable_type", "slot_type", "claimable_slots", "claimable_slot"):
            if key in meta:
                meta["claimable_slot_type"] = meta[key]
                break
    if "work_type" not in meta and "type" in meta:
        meta["work_type"] = meta["type"]
    return meta


def parse_ready_pool_block(block: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    stripped = (block or "").strip()

    if stripped.startswith("{"):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            for key, value in decoded.items():
                meta[normalize_ready_pool_key(key)] = normalize_ready_pool_value(value)
            return add_ready_pool_aliases(meta)

    for line in block.splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_ -]*)\s*[:=]\s*(.*?)\s*$", line)
        if not match:
            continue
        key = normalize_ready_pool_key(match.group(1))
        meta[key] = clean_meta_value(match.group(2))

    for match in re.finditer(
        r"\b([A-Za-z_][A-Za-z0-9_ -]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s]+)",
        block,
    ):
        key = normalize_ready_pool_key(match.group(1))
        meta[key] = clean_meta_value(match.group(2))
    return add_ready_pool_aliases(meta)


def ready_pool_metadata(issue: dict[str, Any]) -> dict[str, str]:
    blocks = ready_pool_blocks(issue.get("body") or "")
    if not blocks:
        return {}
    return parse_ready_pool_block(blocks[-1])


def issue_body_sha256(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def primary_acceptance_criterion(body: str) -> str:
    """Return the first explicit AC bullet without inventing a replacement."""
    heading = re.search(
        r"(?im)^#{1,4}\s*(?:acceptance criteria|acceptance criterion|acs?)(?:\s*\([^\n)]*\))?\s*:?[ \t]*$",
        body or "",
    )
    if not heading:
        return ""
    tail = (body or "")[heading.end() : heading.end() + 4000]
    for line in tail.splitlines():
        if re.match(r"^\s*#{1,4}\s+", line):
            break
        bullet = re.match(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)(.*\S)\s*$", line)
        if bullet:
            return re.sub(r"\s+", " ", bullet.group(1).strip())[:140]
    return ""


DEPENDENCY_METADATA_KEYS = (
    "blockers",
    "blocker",
    "blocked_by",
    "depends_on",
    "depends",
    "dependencies",
    "dependency",
)


def ready_pool_dependency_contract(body: str) -> tuple[bool, set[int]]:
    """Return whether Ready Pool declares blockers and its canonical refs."""
    deps: set[int] = set()
    declared = False

    for block in ready_pool_blocks(body):
        meta = parse_ready_pool_block(block)
        for key in DEPENDENCY_METADATA_KEYS:
            if key not in meta:
                continue
            declared = True
            value = clean_meta_value(meta.get(key))
            if value.lower() in {"", "null", "none", "[]"}:
                continue
            deps.update(int(num) for num in re.findall(r"#?(\d{3,6})", value))
    return declared, deps


def extract_deps_from_ready_pool(body: str) -> set[int]:
    return ready_pool_dependency_contract(body)[1]


def extract_deps_from_lines(body: str) -> set[int]:
    deps: set[int] = set()
    for line in (body or "").splitlines():
        normalized = line.strip()
        if not normalized:
            continue

        structured = re.match(
            r"^[\s>*-]*(?:\*\*)?"
            r"(?:depends[- ]on|blocked[- ]by|blocked[- ]on|blocker|predecessor)"
            r"(?:\*\*)?\b",
            normalized,
            flags=re.I,
        )
        dispatch_order = (
            re.search(r"dispatch order", normalized, flags=re.I)
            and re.search(r"blocked\s+on", normalized, flags=re.I)
        )
        if structured or dispatch_order:
            deps.update(int(num) for num in re.findall(r"#(\d{3,6})", normalized))
    return deps


def extract_deps_from_title(title: str) -> set[int]:
    deps: set[int] = set()
    for match in re.finditer(
        r"\b(?:blocked\s+(?:on|by)|depends?\s+on|dependency\s+(?:on|blocked\s+by)|after)\s+#(\d{3,6})\b",
        title or "",
        flags=re.I,
    ):
        deps.add(int(match.group(1)))
    return deps


def open_dependencies(issue: dict[str, Any], open_nums: set[int]) -> list[int]:
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    declared, canonical_refs = ready_pool_dependency_contract(body)
    prose_refs = extract_deps_from_lines(body) | extract_deps_from_title(title)
    # An explicit non-empty Ready Pool contract is authoritative. Do not union
    # umbrella, parent, or successor references from prose into the dependency
    # graph. When metadata explicitly says no blockers, retain prose as a
    # fail-closed fallback so a contradictory body cannot be dispatched.
    refs = canonical_refs if declared and canonical_refs else prose_refs
    return sorted(num for num in refs if num in open_nums)


def has_ready_pool(body: str) -> bool:
    return bool(re.search(r"<!--\s*ready-pool:", body or "", re.I))


def has_release_gate(names: set[str]) -> bool:
    return len(names & {"public-beta", "post-beta"}) == 1


def is_important(names: set[str]) -> bool:
    return any(name in {"P0", "P1", "P2"} for name in names) or bool(IMPORTANT_LABELS & names)


def is_tracker(issue: dict[str, Any]) -> bool:
    meta = ready_pool_metadata(issue)
    return bool(labels(issue) & {"umbrella", "epic", "tracking", "pm-nonclaimable:tracking"}) or (
        clean_meta_value(meta.get("work_type")).lower() == "tracking"
        or clean_meta_value(meta.get("disposition")).upper() == "TRACKING_ONLY"
    )


def normalized_priority(issue: dict[str, Any], meta: dict[str, str]) -> str:
    explicit = clean_meta_value(meta.get("priority")).upper()
    if explicit in PRIORITY_RANK:
        return explicit

    names = labels(issue)
    for priority in ("P0", "P1", "P2", "P3"):
        if priority in names:
            return priority
    aliases = deprecated_priority_aliases(names)
    if aliases:
        return DEPRECATED_PRIORITY_ALIASES[aliases[0].lower()]
    if "bug" in names:
        return "P1"
    return "P3"


def normalized_claimable_type(meta: dict[str, str]) -> str:
    return clean_meta_value(
        meta.get("claimable_slot_type")
        or meta.get("claimable_type")
        or meta.get("slot_type")
        or meta.get("lane")
    ).lower()


def normalized_lane(issue: dict[str, Any], meta: dict[str, str]) -> str:
    lane = clean_meta_value(meta.get("lane") or meta.get("work_type"))
    if lane:
        return lane
    for name in sorted(labels(issue)):
        if name.startswith("lane:"):
            return name.split(":", 1)[1]
    return ""


def metadata_blockers_clear(meta: dict[str, str]) -> bool:
    value = clean_meta_value(
        meta.get("blockers")
        or meta.get("depends_on")
        or meta.get("depends")
        or meta.get("blocked_by")
    ).lower()
    return value in {"", "null", "none", "[]"}


def has_dependency_contract(meta: dict[str, str]) -> bool:
    """A named dependency can gate assignment without preventing membership.

    Unparseable/product/external waits are not converted into dependency refs.
    The selector remains the live authority for whether each ref is satisfied.
    """
    value = clean_meta_value(meta.get("blockers"))
    return bool(re.fullmatch(r"\s*\[?\s*#\d+(?:\s*,\s*#\d+)*\s*\]?\s*", value))


def has_pm_blocker_label(names: set[str]) -> bool:
    return any(name.startswith("pm-blocked:") or name.startswith("pm-cleanup:") for name in names)


def missing_ready_pool_dispatch_fields(meta: dict[str, str]) -> list[str]:
    missing: list[str] = []
    priority = clean_meta_value(meta.get("priority")).upper()
    if priority not in PRIORITY_RANK:
        missing.append("priority")
    for field in ("lane", "ac_summary", "claimable_slot_type", "work_type"):
        if not clean_meta_value(meta.get(field)):
            missing.append(field)
    try:
        validate_branch_slug(meta.get("branch_slug"))
    except ValueError:
        missing.append("branch_slug")
    # `blockers=` is valid and means no blocker; the field itself must exist so
    # PM's dispatch path parses the same Ready Pool contract the promoter saw.
    if "blockers" not in meta:
        missing.append("blockers")
    return missing


def has_ready_pool_dispatch_contract(meta: dict[str, str]) -> bool:
    return not missing_ready_pool_dispatch_fields(meta)


# Runnability gate (2026-10-28): the promoter put umbrellas, done/PM-parked
# issues and a recurrence tracker into status:todo. Refuse promotion when
# (a) merged PRs reference the issue and no remaining AC is named after them,
# (b) open `Part of #N` child issues exist (promote the child instead), or
# (c) the latest park/unpark PM comment says PARKED / not runnable / dropped.
# 2026-10-31 gap fix (Rajiv D0AMF0XE6TS thread 1793425202.193379): bare "dropped" matched
# technical prose ("dropped `words`", #11770) and silently parked live P1s. Park needs an
# explicit PM marker; "re-queued"/"wake condition has fired" count as unpark (#11791).
PARK_COMMENT_RE = re.compile(r"\bPM PARKED\b|\bPARKED\b|\bnot runnable\b|\bPM (?:park|hold)\b", re.I)
UNPARK_COMMENT_RE = re.compile(
    r"\bUN-?PARKED\b|\bunpark(?:ed|ing)?\b|\brunnable again\b|\bre-?queued\b|\bwake condition (?:has )?fired\b",
    re.I,
)
REMAINING_AC_RE = re.compile(r"\bremaining\s+(?:AC|ACs|acceptance criteri(?:on|a)|slices?|work)\b\s*[:#-]?\s*\S", re.I)


def _ref_re(issue_number: int, verbs: str) -> re.Pattern[str]:
    return re.compile(rf"\b(?:{verbs})\s*:?\s+#{issue_number}\b", re.I)


# 2026-11-02 (#12089/#12090): "This PR must not close #12089" counted as a close.
# A closing keyword directly preceded by a negation ("not", "never", "n't") is not a close.
_NEGATED_PREFIX_RE = re.compile(r"(?:\bnot|\bnever|n't|n’t)\s+$", re.I)


def _has_unnegated_ref(pattern: re.Pattern[str], text: str) -> bool:
    return any(not _NEGATED_PREFIX_RE.search(text[max(0, m.start() - 20):m.start()])
               for m in pattern.finditer(text))


def runnability_refusals(
    issue_number: int,
    body: str,
    comments: list[dict[str, Any]],
    merged_prs: list[dict[str, Any]],
    child_issues: list[dict[str, Any]],
) -> list[str]:
    """Pure decision: reasons an otherwise-clean issue is not runnable."""
    reasons: list[str] = []
    closes = _ref_re(issue_number, "close[sd]?|fix(?:e[sd])?|resolve[sd]?")
    part_of = _ref_re(issue_number, "part of|refs?")
    closing = [pr for pr in merged_prs if _has_unnegated_ref(closes, pr.get("body") or "")]
    partial = [pr for pr in merged_prs if part_of.search(pr.get("body") or "")]
    if closing:
        reasons.append("merged_pr_closes_issue:" + ",".join(f"#{pr['number']}" for pr in closing))
    elif partial:
        last_merge = max(str(pr.get("mergedAt") or "") for pr in partial)
        texts = [c.get("body") or "" for c in comments if str(c.get("createdAt") or "") > last_merge]
        if not any(REMAINING_AC_RE.search(t) for t in texts):
            reasons.append(
                "merged_part_of_prs_without_named_remaining_ac:"
                + ",".join(f"#{pr['number']}" for pr in partial)
            )
    child_re = re.compile(
        rf"\b(?:part of|slice\s+\w+\s+of|parents?\s*:?(?:\s+#\d+\s*(?:,|and))*)\s+#{issue_number}\b", re.I
    )
    children = sorted(
        int(c["number"]) for c in child_issues
        if int(c["number"]) != issue_number and child_re.search(c.get("body") or "")
    )
    if children:
        reasons.append("umbrella_open_children:promote_child_instead:" + ",".join(f"#{n}" for n in children))
    latest = None
    for comment in sorted(comments, key=lambda c: str(c.get("createdAt") or "")):
        text = comment.get("body") or ""
        if UNPARK_COMMENT_RE.search(text):
            latest = None
        elif PARK_COMMENT_RE.search(text):
            latest = comment
    if latest is not None:
        reasons.append("latest_pm_comment_parked:" + str(latest.get("createdAt") or ""))
    return reasons


def issue_runnability_refusals(issue_number: int) -> list[str]:
    try:
        view = json.loads(run([GH, "issue", "view", str(issue_number), "--repo", REPO,
                               "--json", "body,comments"]).stdout)
        prs = json.loads(run([GH, "pr", "list", "--repo", REPO, "--state", "merged", "--limit", "100",
                              "--search", f"{issue_number} in:body",
                              "--json", "number,body,mergedAt"]).stdout)
        kids = json.loads(run([GH, "issue", "list", "--repo", REPO, "--state", "open", "--limit", "100",
                               "--search", f"{issue_number} in:body",
                               "--json", "number,body"]).stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        return [f"runnability_check_failed:{exc}"]
    return runnability_refusals(issue_number, view.get("body") or "", view.get("comments") or [], prs, kids)


def handoff_preflight_ok(issue_number: int) -> tuple[bool, str]:
    if not PREFLIGHT_CHECK.exists():
        return True, "preflight_missing"
    proc = run([str(PREFLIGHT_CHECK), str(issue_number)], check=False)
    if proc.returncode == 0:
        return True, ""
    text = "\n".join(part for part in (proc.stderr, proc.stdout) if part)
    reason = ""
    for line in text.splitlines():
        if "[BLOCK]" in line:
            reason = line.strip()
            break
    return False, reason or f"preflight_exit_{proc.returncode}"


def issue_contract_ledger_errors(
    body: str, *, require_qa_proof: bool = False
) -> list[str]:
    """Use the same deterministic ICL validator as create/promotion hooks."""
    if not ICL_VALIDATOR.exists():
        return ["icl_validator_missing"]
    command = [str(ICL_VALIDATOR), "--json"]
    if require_qa_proof:
        command.append("--require-qa-proof")
    proc = subprocess.run(
        command,
        input=body or "",
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ["icl_validator_failed"]
    return [str(reason) for reason in payload.get("errors", [])]


def icl_enforcement_applies(issue: dict[str, Any]) -> bool:
    """Grandfather only exact pre-hook body hashes, not later label/comment noise."""
    created = str(issue.get("createdAt") or "")
    if created >= ICL_ENFORCEMENT_START:
        return True
    try:
        snapshot = json.loads(ICL_GRANDFATHER_SNAPSHOT.read_text(encoding="utf-8"))
        expected = str(snapshot.get("issues", {}).get(str(issue.get("number"))) or "")
    except (OSError, json.JSONDecodeError):
        return True
    return not expected or issue_body_sha256(issue.get("body") or "") != expected


def is_non_dispatchable_contract(issue: dict[str, Any], meta: dict[str, str]) -> bool:
    names = labels(issue)
    claimable = normalized_claimable_type(meta)
    work_type = clean_meta_value(meta.get("work_type")).lower()
    disposition = clean_meta_value(meta.get("disposition")).lower()
    title = str(issue.get("title") or "").strip().lower()
    body = str(issue.get("body") or "").lower()
    docs_only = (
        work_type in {"docs", "docs-only", "documentation", "documentation-only"}
        or "documentation" in names
        or title.startswith(("docs:", "docs(", "documentation:"))
        or "docs-only" in body
        or "documentation-only" in body
    )
    return (
        claimable in NON_DISPATCHABLE_CLAIMABLE_TYPES
        or work_type
        in {
            "investigation",
            "external",
            "external_dependency",
            "infra",
            "infrastructure",
            "tracking",
            "deferred",
        }
        or disposition
        in {
            "investigation_required",
            "external_required",
            "infra_required",
            "tracking_only",
            "deferred",
        }
        or any(
            name.startswith("pm-blocked:") or name.startswith("needs-")
            for name in names
        )
        or docs_only
        or "tracking only" in body
        or "tracking-only" in body
        or "not claimable" in body
        or "not-claimable" in body
        or bool({"epic", "tracking"} & names)
        or "pm-nonclaimable:tracking" in names
    )


def labels_fingerprint(issue: dict[str, Any]) -> str:
    relevant = sorted(
        name for name in labels(issue)
        if name.startswith(("status:", "pm-blocked:", "pm-cleanup:"))
        or name in PRIORITY_RANK
        or name.lower() in DEPRECATED_PRIORITY_ALIASES
        or name in {"epic", "tracking", "pm-nonclaimable:tracking"}
    )
    return hashlib.sha256("\n".join(relevant).encode("utf-8")).hexdigest()


def dependency_fingerprint(deps: list[int]) -> str:
    return hashlib.sha256(",".join(str(dep) for dep in sorted(deps)).encode("utf-8")).hexdigest()


def rejection_still_applies(issue: dict[str, Any], deps: list[int], rejection: dict[str, Any]) -> bool:
    """Only durable terminal decisions cool down, and only for unchanged evidence."""
    rejection_class = str(rejection.get("rejection_class") or "").upper()
    if rejection_class not in REJECTION_COOLDOWN_REASONS:
        return False
    expected_body = str(rejection.get("body_sha256") or "")
    expected_labels = str(rejection.get("labels_fingerprint") or "")
    expected_deps = str(rejection.get("dependency_fingerprint") or "")
    if not all((expected_body, expected_labels, expected_deps)):
        return False
    return (
        expected_body == issue_body_sha256(issue.get("body") or "")
        and expected_labels == labels_fingerprint(issue)
        and expected_deps == dependency_fingerprint(deps)
    )


def promoter_row(
    issue: dict[str, Any],
    reason: str,
    *,
    meta: dict[str, str] | None = None,
    deps: list[int] | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    metadata = meta or {}
    row = {
        "number": int(issue["number"]),
        "title": issue.get("title") or "",
        "labels": sorted(labels(issue)),
        "reason": reason,
        "priority": priority or normalized_priority(issue, metadata),
        "lane": normalized_lane(issue, metadata),
        "branch_slug": clean_meta_value(metadata.get("branch_slug"))
        or concise_branch_slug(issue.get("title") or ""),
        "ac_summary": clean_meta_value(metadata.get("ac_summary"))
        or primary_acceptance_criterion(issue.get("body") or ""),
        "claimable_slot_type": normalized_claimable_type(metadata),
        "work_type": clean_meta_value(metadata.get("work_type")) or "implementation",
        "open_dependencies": deps or [],
        "canonical_blocked_by": sorted(deps or []),
        "dependency_fingerprint": dependency_fingerprint(deps or []),
        "updatedAt": issue.get("updatedAt"),
        "body_sha256": issue_body_sha256(issue.get("body") or ""),
        "labels_fingerprint": labels_fingerprint(issue),
    }
    if metadata:
        row["ready_pool"] = metadata
    return row


def classify_promoter(
    issues: list[dict[str, Any]],
    open_nums: set[int],
    issue_filter: set[int] | None = None,
    *,
    force_recheck: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    candidates: list[dict[str, Any]] = []
    recent_rejections = recent_promoter_rejections()
    pr_owned = open_pr_issue_numbers()
    skipped: dict[str, list[dict[str, Any]]] = {
        "not_backlog": [],
        "status_hygiene": [],
        "blocked_label": [],
        "open_dependency": [],
        "missing_ready_pool_contract": [],
        "metadata_blocker": [],
        "non_dispatchable": [],
        "priority_label_drift": [],
        "recently_rejected": [],
        "handoff_preflight_failed": [],
        "not_runnable": [],
    }

    for issue in issues:
        number = int(issue["number"])
        if issue_filter and number not in issue_filter:
            continue

        names = labels(issue)
        status_labels = sorted(name for name in names if name.startswith(STATUS_PREFIX))
        # A backlog issue with additional status labels is repairable hygiene,
        # not a reason to make the issue permanently invisible. Keep requiring
        # the canonical backlog label, then evaluate every substantive gate.
        # Promotion removes the stale status labels only after exact-body
        # review and all other dispatch-readiness checks pass.
        dependency_hold = (
            "pm-blocked:dependency" in names
            or has_dependency_contract(ready_pool_metadata(issue))
        )
        held_intake = dependency_hold and status_labels in (["status:blocked"], ["status:deferred"])
        if "status:backlog" not in status_labels and not held_intake:
            key = "status_hygiene" if len(status_labels) != 1 else "not_backlog"
            skipped[key].append(promoter_row(issue, f"status_labels={','.join(status_labels) or 'none'}"))
            continue

        meta = ready_pool_metadata(issue)
        deps = open_dependencies(issue, open_nums)
        if number in pr_owned or any(re.fullmatch(r"slot:\d+", name) for name in names):
            skipped["non_dispatchable"].append(promoter_row(issue, "already_owned", meta=meta, deps=deps))
            continue
        rejection = recent_rejections.get(number)
        if rejection and not force_recheck and rejection_still_applies(issue, deps, rejection):
            row = promoter_row(issue, f"recent_terminal_rejection:{rejection.get('rejection_class') or 'rejected'}", meta=meta, deps=deps)
            row["review_result_manifest"] = rejection.get("path")
            row["review_detail"] = rejection.get("detail") or ""
            skipped["recently_rejected"].append(row)
            continue

        dependency_blocker = "pm-blocked:dependency" in names
        remaining_pm_blockers = names - {"pm-blocked:dependency"}

        # S113: preserve named dependency waits in the pool at every priority.
        # Strip only the dependency label when judging the implementation contract.
        if is_tracker(issue):
            skipped["non_dispatchable"].append(promoter_row(issue, "tracking_or_non_dispatchable", meta=meta, deps=deps))
        elif dependency_blocker and (
            # The resolved pm-blocked:dependency label is exactly what is being
            # cleared here; judging dispatchability WITH it made every resolved
            # dependency wait non-dispatchable, so valid P0 children (#12183)
            # were routed to contract repair forever. Judge the contract alone.
            is_non_dispatchable_contract(
                {**issue, "labels": [l for l in issue.get("labels", []) if l.get("name") != "pm-blocked:dependency"]},
                meta,
            )
            or not meta
            or not has_ready_pool_dispatch_contract(meta)
            or (not metadata_blockers_clear(meta) and not has_dependency_contract(meta))
        ):
            skipped["missing_ready_pool_contract"].append(
                promoter_row(issue, "dependency_requires_contract_repair", meta=meta, deps=deps)
            )
        elif has_pm_blocker_label(remaining_pm_blockers):
            skipped["blocked_label"].append(promoter_row(issue, "pm_blocker_label", meta=meta, deps=deps))
        elif is_non_dispatchable_contract(
            # deps is empty here, so a pm-blocked:dependency label is resolved
            # and is removed at apply; it must not mark the contract tracking.
            {**issue, "labels": [l for l in issue.get("labels", []) if l.get("name") != "pm-blocked:dependency"]},
            meta,
        ):
            skipped["non_dispatchable"].append(promoter_row(issue, "tracking_or_non_dispatchable", meta=meta, deps=deps))
        elif not meta or not has_ready_pool_dispatch_contract(meta):
            missing = missing_ready_pool_dispatch_fields(meta)
            skipped["missing_ready_pool_contract"].append(
                promoter_row(
                    issue,
                    "missing_ready_pool_fields:" + ",".join(missing or REQUIRED_READY_POOL_FIELDS),
                    meta=meta,
                    deps=deps,
                )
            )
        elif not metadata_blockers_clear(meta) and not has_dependency_contract(meta):
            skipped["metadata_blocker"].append(promoter_row(issue, "metadata_blocker", meta=meta, deps=deps))
        else:
            priority = normalized_priority(issue, meta)
            drift = priority_label_drift(issue, meta, require_label=False)
            if drift:
                row = promoter_row(issue, "priority_label_drift:" + drift, meta=meta, deps=deps, priority=priority)
                if repairable_ready_pool_priority_drift(drift) or drift.startswith((
                    "deprecated_priority_aliases:",
                    "multiple_canonical_priority_labels:",
                    "missing_canonical_priority_label",
                )):
                    row["reason"] = "ready_pool_metadata_repair:" + drift
                    skipped["missing_ready_pool_contract"].append(row)
                else:
                    skipped["priority_label_drift"].append(row)
                continue
            preflight_ok, preflight_reason = handoff_preflight_ok(number)
            if not preflight_ok:
                skipped["handoff_preflight_failed"].append(
                    promoter_row(
                        issue,
                        f"handoff_preflight_failed:{preflight_reason}",
                        meta=meta,
                        deps=deps,
                        priority=priority,
                    )
                )
            else:
                candidates.append(promoter_row(issue, "clean_ready_pool", meta=meta, deps=deps, priority=priority))

    # Runnability gate runs only on rows that could be promoted (gh cost).
    for queue in (candidates, skipped["missing_ready_pool_contract"]):
        kept = []
        for row in queue:
            refusals = issue_runnability_refusals(int(row["number"]))
            if refusals:
                row["reason"] = "not_runnable:" + ";".join(refusals)
                row["runnability_refusals"] = refusals
                skipped["not_runnable"].append(row)
            else:
                kept.append(row)
        queue[:] = kept

    candidates.sort(key=lambda row: (PRIORITY_RANK.get(str(row["priority"]), 9), int(row["number"])))
    for rows in skipped.values():
        rows.sort(key=lambda row: (PRIORITY_RANK.get(str(row["priority"]), 9), -int(row["number"])))
    return candidates, skipped


def select_promotion_rows(rows: list[dict[str, Any]], current_claimable: int) -> list[dict[str, Any]]:
    """Admit every P0-P2; admit runnable P3s only to refill the claimable floor."""
    higher = [row for row in rows if row.get("priority") in {"P0", "P1", "P2"}]
    held_p3 = [row for row in rows if row.get("priority") == "P3" and row.get("open_dependencies")]
    remaining = max(0, READY_POOL_TARGET - current_claimable - sum(
        not row.get("open_dependencies") for row in higher
    ))
    p3 = [row for row in rows if row.get("priority") == "P3" and not row.get("open_dependencies")]
    return higher + held_p3 + p3[:remaining]


def is_clean_ready_pool_todo(issue: dict[str, Any], open_nums: set[int]) -> bool:
    return not dispatch_readiness_reasons(issue, open_nums, allow_backlog=False)


def dispatch_readiness_reasons(
    issue: dict[str, Any],
    open_nums: set[int],
    *,
    allow_backlog: bool,
    allow_dependencies: bool = False,
) -> list[str]:
    """Check assignment by default; promotion/audit opt into dependency-held membership."""
    names = labels(issue)
    status_labels = sorted(name for name in names if name.startswith(STATUS_PREFIX))
    allowed_status = ["status:backlog"] if allow_backlog else ["status:todo"]
    reasons: list[str] = []
    meta = ready_pool_metadata(issue)
    dependency_hold = "pm-blocked:dependency" in names or has_dependency_contract(meta)
    repairable_backlog_hygiene = allow_backlog and (
        "status:backlog" in status_labels
        or (allow_dependencies and dependency_hold and status_labels in (["status:blocked"], ["status:deferred"]))
    )
    if status_labels != allowed_status and not repairable_backlog_hygiene:
        reasons.append("status_labels=" + (",".join(status_labels) or "none"))
        return reasons
    if not meta or not has_ready_pool_dispatch_contract(meta):
        missing = missing_ready_pool_dispatch_fields(meta)
        reasons.append("missing_ready_pool_fields:" + ",".join(missing or REQUIRED_READY_POOL_FIELDS))
    deps = open_dependencies(issue, open_nums)
    if deps and not allow_dependencies:
        reasons.append("open_dependency:" + ",".join(str(dep) for dep in deps))
    readiness_blocker_labels = names
    if allow_dependencies or not deps:
        readiness_blocker_labels = names - {"pm-blocked:dependency"}
    if has_pm_blocker_label(readiness_blocker_labels):
        reasons.append("pm_blocker_label")
    if meta and not metadata_blockers_clear(meta) and not has_dependency_contract(meta):
        reasons.append("metadata_blocker")
    contract_issue = issue
    if allow_dependencies or not deps:
        # A resolved dependency label is cleared at apply; judge the contract alone.
        contract_issue = {**issue, "labels": [l for l in issue.get("labels", []) if l.get("name") != "pm-blocked:dependency"]}
    if is_tracker(issue) or is_non_dispatchable_contract(contract_issue, meta):
        reasons.append("tracking_or_non_dispatchable")
    if any(re.fullmatch(r"slot:\d+", name) for name in names):
        reasons.append("already_owned")
    drift = priority_label_drift(issue, meta, require_label=not allow_backlog)
    if drift:
        reasons.append("priority_label_drift:" + drift)
    for error in issue_contract_ledger_errors(
        issue.get("body") or "", require_qa_proof=True
    ):
        reasons.append("issue_contract:" + error)
    if reasons:
        return reasons
    preflight_ok, preflight_reason = handoff_preflight_ok(int(issue["number"]))
    if not preflight_ok:
        reasons.append("handoff_preflight_failed:" + preflight_reason)
    return reasons


def open_pr_issue_numbers() -> set[int]:
    """Issue numbers referenced by an open PR (closing refs or Closes/Part of/Refs in body)."""
    proc = run(
        [GH, "pr", "list", "--repo", REPO, "--state", "open", "--limit", "300",
         "--json", "number,body,closingIssuesReferences"],
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("open_pr_ownership_lookup_failed")
    found: set[int] = set()
    for pr in json.loads(proc.stdout or "[]"):
        for ref in pr.get("closingIssuesReferences") or []:
            if ref.get("number"):
                found.add(int(ref["number"]))
        for m in re.finditer(r"(?i)\b(?:closes|fixes|resolves|part of|refs?)\s+#(\d+)", pr.get("body") or ""):
            found.add(int(m.group(1)))
    return found


def is_claimable_ready_pool_todo(
    issue: dict[str, Any], open_nums: set[int], pr_issues: set[int]
) -> bool:
    """Pool count includes only claimable status:todo issues (Rajiv 2026-10-11)."""
    if not is_clean_ready_pool_todo(issue, open_nums):
        return False
    if int(issue["number"]) in pr_issues:
        return False
    for name in labels(issue):
        if (
            re.fullmatch(r"slot:\d+", name)
            or (name.startswith("pm-blocked:") and name != "pm-blocked:dependency")
            or name.startswith("pm-nonclaimable")
        ):
            return False
    return True


def clean_ready_pool_todo_count(issues: list[dict[str, Any]], open_nums: set[int]) -> int:
    pr_issues = open_pr_issue_numbers()
    return sum(1 for issue in issues if is_claimable_ready_pool_todo(issue, open_nums, pr_issues))


def compute_promotion_deficit(
    issues: list[dict[str, Any]],
    open_nums: set[int],
    issue_filter: set[int] | None,
) -> tuple[int, int]:
    current_ready = clean_ready_pool_todo_count(issues, open_nums)
    return current_ready, max(0, READY_POOL_TARGET - current_ready)


PARKING_PARENT_KEYS = {
    "parent",
    "umbrella",
    "depends_on",
    "blocked_on",
    "blocked_by",
    "blocker",
    "blockers",
    "dependency",
    "dependencies",
    "parent_blocked_on",
    "parent_or_blocked_on",
}
PARKING_WAKE_KEYS = {
    "unblock_condition",
    "unblocks_when",
    "promote_when",
    "ready_when",
    "wake_condition",
}
PARKING_OWNER_KEYS = {
    "owner",
    "next_reviewer",
    "pm_owner",
    "assignee",
    "owner_next_reviewer",
}


def parking_metadata_entries(issue: dict[str, Any]) -> dict[str, list[str]]:
    """Extract explicit parking fields from canonical and common Markdown forms."""
    body = str(issue.get("body") or "")
    entries: dict[str, list[str]] = {}

    def add_entry(key: str, value: str) -> None:
        normalized_key = normalize_ready_pool_key(key).replace("_/_", "_")
        normalized_value = clean_meta_value(value)
        if normalized_key and normalized_value and normalized_value not in {"---", ":---", "---:"}:
            entries.setdefault(normalized_key, []).append(normalized_value)

    for key, value in ready_pool_metadata(issue).items():
        add_entry(key, value)

    for raw_line in body.splitlines():
        line = raw_line.strip()
        line = re.sub(r"^(?:>\s*)+", "", line)
        line = re.sub(r"^(?:[-+*]\s+|\d+[.)]\s+)", "", line)
        line = line.replace("**", "").replace("__", "").replace("`", "").strip()
        if not line:
            continue

        if line.startswith("|") and line.endswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) >= 2:
                add_entry(cells[0], " | ".join(cells[1:]))
            continue

        match = re.match(r"^([^:]{1,80}?)\s*:\s*(\S.*)$", line)
        if match:
            add_entry(match.group(1), match.group(2))

    return entries


def parking_metadata_missing(issue: dict[str, Any]) -> list[str]:
    entries = parking_metadata_entries(issue)
    title = str(issue.get("title") or "")
    missing: list[str] = []

    parent_present = any(entries.get(key) for key in PARKING_PARENT_KEYS)
    if not parent_present:
        parent_present = bool(
            re.search(
                r"(?i)\b(?:blocked\s+(?:on|by)|depends?\s+on|child\s+of|follow-up\s+to)\s+#\d+\b",
                title,
            )
        )
    if not parent_present:
        missing.append("parent_or_blocker")

    wake_present = any(entries.get(key) for key in PARKING_WAKE_KEYS)
    if not wake_present:
        wake_present = any(
            re.search(
                r"(?i)\b(?:unblock|wake)\s+condition\s*:\s*\S",
                value,
            )
            for values in entries.values()
            for value in values
        )
    if not wake_present:
        missing.append("unblock_condition")

    if not any(entries.get(key) for key in PARKING_OWNER_KEYS):
        missing.append("owner_or_next_reviewer")
    return missing


def is_ready_gated_p0(names: set[str], meta: dict[str, str]) -> bool:
    """P0 Ready Pool item whose gate the assigner checks (ready_pool_select.py gated_p0)."""
    return "P0" in names and clean_meta_value(meta.get("disposition")).upper() == "READY_GATED"


def classify(
    issues: list[dict[str, Any]],
    open_nums: set[int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if open_nums is None:
        open_nums = issue_num_set(issues)
    buckets: dict[str, list[dict[str, Any]]] = {
        "todo_blocked_dependency": [],
        "todo_dependency_gated": [],
        "todo_ready_gated_p0": [],
        "todo_priority_label_drift": [],
        "todo_missing_ready_pool": [],
        "todo_dispatch_readiness_failed": [],
        "backlog_promote_candidate": [],
        "backlog_metadata_missing": [],
        "status_hygiene": [],
        "missing_release_gate": [],
        "valid_backlog_blocked": [],
    }

    for issue in issues:
        names = labels(issue)
        status_labels = sorted(name for name in names if name.startswith(STATUS_PREFIX))
        deps = open_dependencies(issue, open_nums)
        row = {
            "number": int(issue["number"]),
            "title": issue.get("title") or "",
            "labels": sorted(names),
            "status_labels": status_labels,
            "open_dependencies": deps,
            "updatedAt": issue.get("updatedAt"),
        }

        if len(status_labels) != 1:
            row["reason"] = f"status_count={len(status_labels)}"
            buckets["status_hygiene"].append(row)

        if not has_release_gate(names):
            buckets["missing_release_gate"].append(row)

        status = status_labels[0] if len(status_labels) == 1 else ""
        body = issue.get("body") or ""
        meta = ready_pool_metadata(issue)

        if status == "status:todo":
            priority_drift = priority_label_drift(issue, meta, require_label=True)
            if priority_drift:
                drift_row = dict(row)
                drift_row["reason"] = "priority_label_drift:" + priority_drift
                if meta:
                    drift_row["ready_pool"] = meta
                buckets["todo_priority_label_drift"].append(drift_row)
            if deps:
                # Rajiv: "the P0's can still be added to the ready pool, right? the gates
                # can be checked while assigning" -> stays status:todo, never migrated.
                row["reason"] = "dependency_gated"
                row["gate"] = clean_meta_value(meta.get("gate"))
                buckets["todo_dependency_gated"].append(dict(row))
                if is_ready_gated_p0(names, meta):
                    buckets["todo_ready_gated_p0"].append(dict(row))
            missing_ready_pool = missing_ready_pool_dispatch_fields(meta)
            if missing_ready_pool:
                row["reason"] = (
                    "missing_ready_pool_frontmatter"
                    if not has_ready_pool(body)
                    else "missing_ready_pool_fields:" + ",".join(missing_ready_pool)
                )
                row["missing_ready_pool_fields"] = missing_ready_pool
                buckets["todo_missing_ready_pool"].append(row)
            else:
                readiness = dispatch_readiness_reasons(issue, open_nums, allow_backlog=False, allow_dependencies=True)
                if readiness:
                    failed_row = dict(row)
                    failed_row["reason"] = "dispatch_readiness_failed"
                    failed_row["readiness_reasons"] = readiness
                    buckets["todo_dispatch_readiness_failed"].append(failed_row)
        elif status == "status:backlog":
            missing = parking_metadata_missing(issue)
            non_dispatchable = is_non_dispatchable_contract(issue, meta)
            if deps:
                blocked_row = dict(row)
                blocked_row["reason"] = "dependency_blocked"
                buckets["valid_backlog_blocked"].append(blocked_row)
            elif non_dispatchable:
                blocked_row = dict(row)
                blocked_row["reason"] = "non_dispatchable_contract"
                buckets["valid_backlog_blocked"].append(blocked_row)
            if missing:
                row["reason"] = "missing_" + ",".join(missing)
                row["missing"] = missing
                buckets["backlog_metadata_missing"].append(row)
            elif (
                not deps
                and not non_dispatchable
                and is_important(names)
                and not is_tracker(issue)
            ):
                row["reason"] = "important_unblocked_backlog"
                buckets["backlog_promote_candidate"].append(row)

    for key in buckets:
        buckets[key].sort(key=lambda item: (item.get("updatedAt") or "", item["number"]), reverse=True)
    return buckets


def pm_ops_upsert(
    kind: str,
    severity: str,
    title: str,
    action: str,
    issue: int,
    evidence: str,
    *,
    horizon: str = "daily",
) -> None:
    if not PM_OPS.exists():
        print(f"WARN pm-ops missing: {PM_OPS}", file=sys.stderr)
        return
    cmd = [
        "python3",
        str(PM_OPS),
        "obligation-upsert",
        "--kind",
        kind,
        "--severity",
        severity,
        "--horizon",
        horizon,
        "--target-type",
        "issue",
        "--target-id",
        str(issue),
        "--issue",
        str(issue),
        "--owner",
        "pm",
        "--title",
        title,
        "--action",
        action,
        "--evidence",
        evidence,
    ]
    run(cmd)


def promotion_failure(row: dict[str, Any], reason: str, **extra: Any) -> dict[str, Any]:
    result = {"ok": False, "reason": reason, "issue": int(row["number"]), "candidate": row}
    result.update(extra)
    return result


def ready_pool_defaults_block(row: dict[str, Any]) -> str:
    priority = clean_meta_value(row.get("priority")).upper()
    if priority not in PRIORITY_RANK:
        priority = "P3"
    lane = clean_meta_value(row.get("lane")) or "general"
    branch_slug = validate_branch_slug(row.get("branch_slug"))
    ac_summary = clean_meta_value(row.get("ac_summary"))
    if not ac_summary:
        raise ValueError("missing_explicit_acceptance_criterion")
    if len(ac_summary) > 140:
        raise ValueError("acceptance_criterion_exceeds_140_chars")
    claimable = clean_meta_value(row.get("claimable_slot_type")) or "any"
    blockers = clean_meta_value(row.get("blockers")) or "none"
    work_type = clean_meta_value(row.get("work_type")) or "implementation"
    return "\n".join(
        [
            "<!-- ready-pool:",
            f"priority: {priority}",
            f"lane: {lane}",
            f"branch_slug: {branch_slug}",
            f"ac_summary: {ac_summary}",
            f"claimable_slot_type: {claimable}",
            f"blockers: {blockers}",
            f"work_type: {work_type}",
            "-->",
        ]
    )


def replace_ready_pool_block(body: str, block: str) -> str:
    """Replace all stale Ready Pool blocks with one canonical block."""
    stripped = re.sub(r"(?is)\n*<!--\s*ready-pool:.*?-->\s*", "\n", body or "").strip()
    heading = re.match(r"(?s)^(\s*#\s+[^\n]+\n)", stripped)
    if heading:
        return stripped[: heading.end()] + "\n" + block + "\n\n" + stripped[heading.end() :].lstrip()
    if stripped:
        return block + "\n\n" + stripped + "\n"
    return block + "\n"


def ensure_ready_pool_metadata(issue: int, row: dict[str, Any]) -> dict[str, Any]:
    live = gh_issue_view(issue)
    live_meta = ready_pool_metadata(live)
    expected_priority = clean_meta_value(row.get("priority")).upper()
    if expected_priority not in PRIORITY_RANK:
        expected_priority = normalized_priority(live, live_meta)
    live_priority = clean_meta_value(live_meta.get("priority")).upper()
    missing = missing_ready_pool_dispatch_fields(live_meta)
    repair_reasons = list(missing)
    if live_meta and expected_priority in PRIORITY_RANK and live_priority != expected_priority:
        repair_reasons.append(f"priority_mismatch:{live_priority or 'none'}->{expected_priority}")
    if live_meta and not repair_reasons:
        return {"changed": False, "ready_pool": live_meta}

    body = live.get("body") or ""
    new_body = replace_ready_pool_block(body, ready_pool_defaults_block(row))
    tmp = OUT_DIR / f"pm-backlog-promoter-ready-pool-{issue}.md"
    tmp.write_text(new_body, encoding="utf-8")
    run([GH, "issue", "edit", str(issue), "--repo", REPO, "--body-file", str(tmp)])

    refreshed = gh_issue_view(issue)
    refreshed_meta = ready_pool_metadata(refreshed)
    missing_after = missing_ready_pool_dispatch_fields(refreshed_meta)
    if missing_after:
        raise RuntimeError("ready_pool_metadata_repair_failed:" + ",".join(missing_after))
    refreshed_priority = clean_meta_value(refreshed_meta.get("priority")).upper()
    if expected_priority in PRIORITY_RANK and refreshed_priority != expected_priority:
        raise RuntimeError(f"ready_pool_priority_repair_failed:expected={expected_priority}:actual={refreshed_priority}")
    return {"changed": True, "ready_pool": refreshed_meta, "body_file": str(tmp), "repair_reasons": repair_reasons}


def read_ready_pool_repair_proposals(path: str) -> dict[int, dict[str, Any]]:
    decoded = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(decoded, dict):
        decoded = decoded.get("repairs")
    if not isinstance(decoded, list):
        raise ValueError("--metadata-json must be a list or an object with a repairs list")
    proposals: dict[int, dict[str, Any]] = {}
    for row in decoded:
        if not isinstance(row, dict) or not row.get("issue"):
            raise ValueError("every repair proposal must be an object with an issue number")
        issue = int(row["issue"])
        if issue in proposals:
            raise ValueError(f"duplicate repair proposal for issue {issue}")
        proposals[issue] = row
    return proposals


def validate_ready_pool_repair_proposal(proposal: dict[str, Any]) -> dict[str, str]:
    metadata = {
        field: clean_meta_value(proposal.get(field))
        for field in REQUIRED_READY_POOL_FIELDS
    }
    missing = [field for field in REQUIRED_READY_POOL_FIELDS if field not in proposal]
    missing.extend(
        field
        for field in ("priority", "lane", "branch_slug", "ac_summary", "claimable_slot_type", "work_type")
        if not metadata.get(field) and field not in missing
    )
    if missing:
        raise ValueError("missing_repair_fields:" + ",".join(missing))
    if metadata["priority"].upper() not in PRIORITY_RANK:
        raise ValueError("invalid_priority:" + metadata["priority"])
    metadata["priority"] = metadata["priority"].upper()
    metadata["branch_slug"] = validate_branch_slug(metadata["branch_slug"])
    if len(metadata["ac_summary"]) > 140:
        raise ValueError("acceptance_criterion_exceeds_140_chars")
    if metadata["claimable_slot_type"].lower() in NON_DISPATCHABLE_CLAIMABLE_TYPES:
        raise ValueError("non_dispatchable_claimable_slot_type:" + metadata["claimable_slot_type"])
    if not metadata_blockers_clear(metadata) and not has_dependency_contract(metadata):
        raise ValueError("status_todo_repair_has_blockers:" + metadata["blockers"])
    review_proof = clean_meta_value(proposal.get("review_proof"))
    if len(review_proof) < 8:
        raise ValueError("missing_review_proof")
    body_sha = clean_meta_value(proposal.get("expected_body_sha256"))
    if not re.fullmatch(r"[0-9a-f]{64}", body_sha):
        raise ValueError("missing_or_invalid_expected_body_sha256")
    return metadata


def repair_todo_ready_pool_issue(
    issue: dict[str, Any],
    proposal: dict[str, Any],
    open_nums: set[int],
) -> dict[str, Any]:
    number = int(issue["number"])
    names = labels(issue)
    status_labels = sorted(name for name in names if name.startswith(STATUS_PREFIX))
    current_meta = ready_pool_metadata(issue)
    current_missing = missing_ready_pool_dispatch_fields(current_meta)
    if status_labels != ["status:todo"]:
        return promotion_failure(issue, "not_status_todo", status_labels=status_labels)
    if not current_missing:
        return {
            "ok": True,
            "changed": False,
            "issue": number,
            "reason": "already_valid",
            "live_ready_pool": current_meta,
        }
    if has_pm_blocker_label(names - {"pm-blocked:dependency"}):
        return promotion_failure(issue, "pm_blocker_label", labels=sorted(names))
    deps = open_dependencies(issue, open_nums)
    try:
        metadata = validate_ready_pool_repair_proposal(proposal)
    except ValueError as exc:
        return promotion_failure(issue, "invalid_repair_proposal", error=str(exc))

    old_body = issue.get("body") or ""
    old_sha = issue_body_sha256(old_body)
    if old_sha != clean_meta_value(proposal.get("expected_body_sha256")):
        return promotion_failure(
            issue,
            "stale_issue_body",
            expected_body_sha256=proposal.get("expected_body_sha256"),
            actual_body_sha256=old_sha,
        )

    block = ready_pool_defaults_block(metadata)
    new_body = replace_ready_pool_block(old_body, block)
    tmp = OUT_DIR / f"pm-ready-pool-repair-{number}.md"
    tmp.write_text(new_body, encoding="utf-8")
    try:
        run([GH, "issue", "edit", str(number), "--repo", REPO, "--body-file", str(tmp)])
        priority_labels_to_remove = sorted(
            label
            for label in names
            if label.lower() in DEPRECATED_PRIORITY_ALIASES
            or (label in PRIORITY_RANK and label != metadata["priority"])
        )
        label_cmd = [GH, "issue", "edit", str(number), "--repo", REPO]
        if metadata["priority"] not in names:
            label_cmd.extend(["--add-label", metadata["priority"]])
        if priority_labels_to_remove:
            label_cmd.extend(["--remove-label", ",".join(priority_labels_to_remove)])
        if len(label_cmd) > 6:
            run(label_cmd)
        refreshed = gh_issue_view(number)
    except subprocess.CalledProcessError as exc:
        return promotion_failure(
            issue,
            "gh_repair_failed",
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )

    refreshed_names = labels(refreshed)
    refreshed_status = sorted(name for name in refreshed_names if name.startswith(STATUS_PREFIX))
    refreshed_meta = ready_pool_metadata(refreshed)
    missing_after = missing_ready_pool_dispatch_fields(refreshed_meta)
    priority_after = canonical_priority_labels(refreshed_names)
    if refreshed_status != ["status:todo"] or missing_after or priority_after != [metadata["priority"]]:
        return promotion_failure(
            issue,
            "repair_verification_failed",
            status_labels=refreshed_status,
            missing_ready_pool_fields=missing_after,
            priority_labels=priority_after,
            live_ready_pool=refreshed_meta,
        )

    try:
        pm_ops_upsert(
            "ready_pool",
            "high",
            f"#{number} Ready Pool contract repaired",
            f"Dispatch #{number} through Skill(direct-assign); keep queued if no eligible slot is free.",
            number,
            (
                "ready-pool-contract-repair "
                f"review_proof={clean_meta_value(proposal.get('review_proof'))} "
                f"body_sha256={issue_body_sha256(refreshed.get('body') or '')}"
            ),
            horizon="hourly",
        )
    except subprocess.CalledProcessError as exc:
        return promotion_failure(
            issue,
            "pm_ops_upsert_failed_after_github_repair",
            changed=True,
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )
    return {
        "ok": True,
        "changed": True,
        "issue": number,
        "reason": "repaired",
        "missing_before": current_missing,
        "body_sha256_before": old_sha,
        "body_sha256_after": issue_body_sha256(refreshed.get("body") or ""),
        "live_labels": sorted(refreshed_names),
        "live_ready_pool": refreshed_meta,
        "review_proof": clean_meta_value(proposal.get("review_proof")),
    }


def write_ready_pool_repair_manifest(
    candidates: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    applied: bool,
) -> Path:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = OUT_DIR / f"pm-ready-pool-repair-{ts}.json"
    superseded = [result for result in results if result.get("reason") == "superseded_not_status_todo"]
    repaired = [result for result in results if result.get("ok") and result not in superseded]
    failed = [result for result in results if not result.get("ok")]
    payload = {
        "repo": REPO,
        "generated_at": ts,
        "mode": "repair_ready_pool",
        "apply": applied,
        "counts": {
            "repair_required": len(candidates),
            "repaired": len(repaired),
            "superseded": len(superseded),
            "repair_failed": len(failed),
        },
        "repair_required": candidates,
        "repaired": repaired,
        "superseded": superseded,
        "repair_failed": failed,
        "next_command": (
            "dispatch the Ready Pool with Skill(direct-assign)"
            if applied and not failed and repaired
            else "refresh live Ready Pool state; no repair remains"
            if applied and not failed and superseded
            else "launch Agent(subagent_type=pm-backlog-promoter, run_in_background=true, mode=repair-ready-pool)"
        ),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def repair_ready_pool_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Repair incomplete Ready Pool contracts on existing status:todo issues.")
    parser.add_argument("--dry-run", action="store_true", help="inspect only; default behavior")
    parser.add_argument("--apply", action="store_true", help="apply agent-reviewed Ready Pool metadata")
    parser.add_argument("--metadata-json", help="agent-produced repair proposals; required with --apply")
    parser.add_argument("--issues", nargs="+", type=int, required=True, help="status:todo issue numbers")
    args = parser.parse_args(argv)
    if args.apply and not args.metadata_json:
        parser.error("--apply requires --metadata-json")
    if args.apply:
        print(
            "ERROR: in-place repair of status:todo is disabled. Reconcile the issue to "
            "status:backlog, repair its contract, record Codex architecture approval, "
            "then use promote --apply with --review-result-manifest.",
            file=sys.stderr,
        )
        return 2

    try:
        all_issues = gh_issue_list()
        open_nums = issue_num_set(all_issues)
        by_number = {int(issue["number"]): issue for issue in all_issues}
        selected = [by_number[number] for number in args.issues if number in by_number]
        missing_issues = sorted(set(args.issues) - set(by_number))
        if missing_issues:
            raise ValueError("open_issue_not_found:" + ",".join(str(issue) for issue in missing_issues))
    except Exception as exc:
        manifest = write_ready_pool_repair_manifest([], [{"ok": False, "reason": "discovery_failed", "error": str(exc)}], applied=bool(args.apply))
        print("PM_READY_POOL_REPAIR_DEGRADED")
        print(f"manifest={manifest}")
        print(f"error={exc}", file=sys.stderr)
        return 2

    candidates: list[dict[str, Any]] = []
    for issue in selected:
        status_labels = sorted(name for name in labels(issue) if name.startswith(STATUS_PREFIX))
        if status_labels != ["status:todo"]:
            continue
        meta = ready_pool_metadata(issue)
        missing = missing_ready_pool_dispatch_fields(meta)
        if missing:
            candidates.append(
                {
                    "issue": int(issue["number"]),
                    "title": issue.get("title") or "",
                    "updatedAt": issue.get("updatedAt"),
                    "expected_body_sha256": issue_body_sha256(issue.get("body") or ""),
                    "missing_ready_pool_fields": missing,
                    "current_ready_pool": meta,
                    "primary_ac_candidate": primary_acceptance_criterion(issue.get("body") or ""),
                    "labels": sorted(labels(issue)),
                }
            )

    results: list[dict[str, Any]] = []
    if args.apply:
        try:
            proposals = read_ready_pool_repair_proposals(args.metadata_json)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifest = write_ready_pool_repair_manifest(candidates, [{"ok": False, "reason": "proposal_read_failed", "error": str(exc)}], applied=True)
            print("PM_READY_POOL_REPAIR_FAILED")
            print(f"manifest={manifest}")
            print(f"error={exc}", file=sys.stderr)
            return 2
        for issue in selected:
            number = int(issue["number"])
            status_labels = sorted(name for name in labels(issue) if name.startswith(STATUS_PREFIX))
            if status_labels != ["status:todo"]:
                results.append(
                    {
                        "ok": True,
                        "changed": False,
                        "issue": number,
                        "reason": "superseded_not_status_todo",
                        "status_labels": status_labels,
                    }
                )
                continue
            proposal = proposals.get(number)
            if proposal is None:
                results.append(promotion_failure(issue, "missing_repair_proposal"))
                continue
            results.append(repair_todo_ready_pool_issue(issue, proposal, open_nums))

    manifest = write_ready_pool_repair_manifest(candidates, results, applied=bool(args.apply))
    print("PM_READY_POOL_REPAIR")
    print(f"manifest={manifest}")
    superseded_count = sum(1 for row in results if row.get("reason") == "superseded_not_status_todo")
    repaired_count = sum(1 for row in results if row.get("ok") and row.get("reason") != "superseded_not_status_todo")
    print(f"repair_required={len(candidates)} repaired={repaired_count} superseded={superseded_count} repair_failed={sum(1 for row in results if not row.get('ok'))}")
    for candidate in candidates:
        print(
            f"- #{candidate['issue']} missing={','.join(candidate['missing_ready_pool_fields'])} "
            f"body_sha256={candidate['expected_body_sha256']} title={candidate['title']}"
        )
    if not args.apply:
        print("next=PM background Agent(pm-backlog-promoter) mode=repair-ready-pool must write --metadata-json and apply")
        return 0

    failed = [result for result in results if not result.get("ok")]
    repaired = [
        result
        for result in results
        if result.get("ok") and result.get("reason") != "superseded_not_status_todo"
    ]
    if failed:
        print("ERROR: one or more Ready Pool repairs failed; dispatch sweep skipped", file=sys.stderr)
        return 2
    if repaired:
        if PM_OPS.exists():
            run(
                [
                    "python3",
                    str(PM_OPS),
                    "sync",
                    "--write",
                    "--no-live",
                    "--reason",
                    "ready-pool-contract-repair",
                ]
            )
        run_slot_dispatch_sweep()
    return 0


def promote_backlog_issue(
    row: dict[str, Any],
    open_nums: set[int],
    *,
    add_ready_pool_defaults: bool = False,
) -> dict[str, Any]:
    issue = int(row["number"])
    canonical_priority = clean_meta_value(row.get("priority")).upper()
    if canonical_priority not in PRIORITY_RANK:
        return promotion_failure(row, "invalid_canonical_priority", priority=row.get("priority"))

    metadata_repair: dict[str, Any] = {"changed": False}
    if add_ready_pool_defaults:
        try:
            metadata_repair = ensure_ready_pool_metadata(issue, row)
        except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
            return promotion_failure(row, "ready_pool_metadata_repair_failed", error=str(exc))

    try:
        admission_issue = gh_issue_view(issue)
    except subprocess.CalledProcessError as exc:
        return promotion_failure(row, "gh_issue_view_failed_before_admission", error=str(exc))
    readiness_reasons = dispatch_readiness_reasons(
        admission_issue,
        open_nums,
        allow_backlog=True,
        allow_dependencies=True,
    )
    if not readiness_reasons:
        readiness_reasons = [f"not_runnable:{r}" for r in issue_runnability_refusals(issue)]
    if not readiness_reasons and issue in open_pr_issue_numbers():
        readiness_reasons = ["already_owned_by_open_pr"]
    if not add_ready_pool_defaults and row.get("body_sha256") != issue_body_sha256(admission_issue.get("body") or ""):
        readiness_reasons.append("stale_issue_body_before_promotion")
    if readiness_reasons:
        return promotion_failure(
            row,
            "dispatch_readiness_failed_before_promotion",
            readiness_reasons=readiness_reasons,
        )

    admission_status_labels = sorted(
        name for name in labels(admission_issue) if name.startswith(STATUS_PREFIX)
    )
    try:
        labels_to_remove = list(admission_status_labels)
        if (
            "pm-blocked:dependency" in labels(admission_issue)
            and not open_dependencies(admission_issue, open_nums)
        ):
            labels_to_remove.append("pm-blocked:dependency")
        run(
            [
                GH,
                "issue",
                "edit",
                str(issue),
                "--repo",
                REPO,
                "--remove-label",
                ",".join(labels_to_remove),
                "--add-label",
                f"status:todo,{canonical_priority}",
            ]
        )
    except subprocess.CalledProcessError as exc:
        return promotion_failure(
            row,
            "gh_issue_edit_failed",
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )

    try:
        live = gh_issue_view(issue)
    except subprocess.CalledProcessError as exc:
        return promotion_failure(
            row,
            "gh_issue_view_failed_after_edit",
            returncode=exc.returncode,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )

    live_labels = labels(live)
    priority_labels_to_remove = sorted(
        label
        for label in live_labels
        if label.lower() in DEPRECATED_PRIORITY_ALIASES
        or (label in PRIORITY_RANK and label != canonical_priority)
    )
    if priority_labels_to_remove:
        try:
            run(
                [
                    GH,
                    "issue",
                    "edit",
                    str(issue),
                    "--repo",
                    REPO,
                    "--remove-label",
                    ",".join(priority_labels_to_remove),
                ]
            )
            live = gh_issue_view(issue)
            live_labels = labels(live)
        except subprocess.CalledProcessError as exc:
            return promotion_failure(
                row,
                "priority_alias_cleanup_failed",
                returncode=exc.returncode,
                stdout=exc.stdout,
                stderr=exc.stderr,
                attempted_remove=priority_labels_to_remove,
                live_labels=sorted(live_labels),
            )

    status_labels = sorted(name for name in live_labels if name.startswith(STATUS_PREFIX))
    if status_labels != ["status:todo"]:
        return promotion_failure(
            row,
            "label_verification_failed",
            live_labels=sorted(live_labels),
            status_labels=status_labels,
        )

    live_priority_labels = canonical_priority_labels(live_labels)
    live_priority_aliases = deprecated_priority_aliases(live_labels)
    if live_priority_labels != [canonical_priority] or live_priority_aliases:
        return promotion_failure(
            row,
            "priority_label_verification_failed",
            expected_priority=canonical_priority,
            live_priority_labels=live_priority_labels,
            live_priority_aliases=live_priority_aliases,
            live_labels=sorted(live_labels),
        )

    live_meta = ready_pool_metadata(live)
    missing = missing_ready_pool_dispatch_fields(live_meta)
    if missing:
        return promotion_failure(
            row,
            "ready_pool_verification_failed",
            missing_ready_pool_fields=missing,
            live_labels=sorted(live_labels),
            live_ready_pool=live_meta,
        )
    live_ready_pool_priority = clean_meta_value(live_meta.get("priority")).upper()
    if live_ready_pool_priority != canonical_priority:
        return promotion_failure(
            row,
            "ready_pool_priority_mismatch_after_edit",
            expected_priority=canonical_priority,
            live_ready_pool_priority=live_ready_pool_priority,
            live_labels=sorted(live_labels),
            live_ready_pool=live_meta,
        )

    final_readiness = dispatch_readiness_reasons(live, open_nums, allow_backlog=False, allow_dependencies=True)
    if final_readiness:
        rollback_status_labels = sorted(
            name for name in labels(live) if name.startswith(STATUS_PREFIX)
        )
        run(
            [
                GH,
                "issue",
                "edit",
                str(issue),
                "--repo",
                REPO,
                "--remove-label",
                ",".join(rollback_status_labels),
                "--add-label",
                ",".join(admission_status_labels),
            ],
            check=False,
        )
        return promotion_failure(
            row,
            "post_promotion_dispatch_readiness_failed_rolled_back",
            readiness_reasons=final_readiness,
        )

    pm_ops_upsert(
        "ready_pool",
        "high",
        f"#{issue} promoted from backlog to Ready Pool",
        (
            f"Check #{issue}'s dependencies live before Skill(direct-assign). If no slot is free, keep it queued "
            "as status:todo with explicit ownership instead of returning it to backlog."
        ),
        issue,
        (
            "backlog-promoter:clean_ready_pool "
            f"priority={row.get('priority','')} lane={row.get('lane','')} "
            f"claimable_slot_type={row.get('claimable_slot_type','')}"
        ),
        horizon="hourly",
    )
    return {
        "ok": True,
        "issue": issue,
        "candidate": row,
        "canonical_priority": canonical_priority,
        "metadata_repair": metadata_repair,
        "removed_priority_labels": priority_labels_to_remove,
        "live_labels": sorted(live_labels),
        "live_ready_pool": live_meta,
        "dependency_gated": bool(open_dependencies(live, open_nums)),
        "verified_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def write_promoter_manifest(
    candidates: list[dict[str, Any]],
    skipped: dict[str, list[dict[str, Any]]],
    selected: list[dict[str, Any]],
    *,
    applied: bool,
    current_ready_pool_count: int = 0,
    promotion_deficit: int = 0,
    metadata_candidates: list[dict[str, Any]] | None = None,
    promotion_results: list[dict[str, Any]] | None = None,
) -> Path:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = OUT_DIR / f"pm-backlog-promoter-{ts}.json"
    results = promotion_results or []
    promoted = [result for result in results if result.get("ok")]
    promotion_failed = [result for result in results if not result.get("ok")]
    metadata_candidates = metadata_candidates or []
    payload = {
        "repo": REPO,
        "generated_at": ts,
        "apply": applied,
        "ready_pool_target": READY_POOL_TARGET,
        "current_ready_pool_count": current_ready_pool_count,
        "promotion_deficit": promotion_deficit,
        "counts": {
            "auto_promotable": len(candidates),
            "needs_ready_pool_metadata": len(metadata_candidates),
            "selected": len(selected),
            "promoted": len(promoted),
            "promotion_failed": len(promotion_failed),
            "skipped": {key: len(value) for key, value in skipped.items()},
        },
        "selected": selected,
        "would_promote": selected if not applied else [],
        "needs_ready_pool_metadata": metadata_candidates,
        "promoted": promoted,
        "promotion_failed": promotion_failed,
        "next_command": (
            "Agent(subagent_type=pm-backlog-promoter,run_in_background=true,"
            f"mode=refill-ready-pool)  # repair contracts and promote every eligible P0-P2; refill P3 only below {READY_POOL_TARGET}"
        ),
        "skipped": skipped,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_rejected_json(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    decoded = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(decoded, list):
        return [row for row in decoded if isinstance(row, dict)]
    if isinstance(decoded, dict):
        rejected = decoded.get("rejected")
        if isinstance(rejected, list):
            return [row for row in rejected if isinstance(row, dict)]
    raise ValueError("--rejected-json must be a JSON list or an object with a rejected list")


def write_promoter_review_result_manifest(
    *,
    dry_run_manifest: str,
    accepted: list[int],
    rejected: list[dict[str, Any]],
    notes: str,
) -> Path:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = OUT_DIR / f"pm-backlog-promoter-{ts}-result.json"
    payload = {
        "repo": REPO,
        "generated_at": ts,
        "apply": False,
        "review_result": True,
        "dry_run_manifest": dry_run_manifest,
        "accepted": accepted,
        "rejected": rejected,
        "counts": {
            "accepted": len(accepted),
            "rejected": len(rejected),
            "promoted": 0,
        },
        "notes": notes,
        "next_command": (
            "/Users/rajiv/.claude/scripts/backlog-triage.py promote --apply "
            f"--review-result-manifest {path} --issues <accepted_issue...>"
            if accepted
            else "none; no accepted backlog promotion candidates"
        ),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_review_approval(path: str) -> tuple[set[int], dict[int, str]]:
    review_path = Path(path)
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    if not payload.get("review_result") or payload.get("repo") != REPO:
        raise ValueError("invalid_review_result_manifest")
    accepted = {int(number) for number in payload.get("accepted") or []}
    dry_path = Path(str(payload.get("dry_run_manifest") or ""))
    if not dry_path.is_file():
        raise ValueError("review_source_manifest_missing")
    dry_payload = json.loads(dry_path.read_text(encoding="utf-8"))
    if dry_payload.get("repo") != REPO or dry_payload.get("apply"):
        raise ValueError("invalid_review_source_manifest")
    hashes: dict[int, str] = {}
    rows: list[dict[str, Any]] = []
    for key in ("selected", "would_promote", "needs_ready_pool_metadata"):
        value = dry_payload.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    for row in rows:
        number = row.get("number")
        body_sha = str(row.get("body_sha256") or "")
        if number is not None and re.fullmatch(r"[0-9a-f]{64}", body_sha):
            hashes[int(number)] = body_sha
    return accepted, hashes


def write_promoter_degraded_manifest(*, stage: str, error: str, applied: bool) -> Path:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = OUT_DIR / f"pm-backlog-promoter-{ts}.json"
    payload = {
        "repo": REPO,
        "generated_at": ts,
        "apply": applied,
        "degraded": True,
        "stage": stage,
        "error": error,
        "ready_pool_target": READY_POOL_TARGET,
        "counts": {
            "auto_promotable": 0,
            "needs_ready_pool_metadata": 0,
            "selected": 0,
            "promoted": 0,
            "promotion_failed": 0,
            "skipped": {},
        },
        "selected": [],
        "would_promote": [],
        "needs_ready_pool_metadata": [],
        "promoted": [],
        "promotion_failed": [],
        "next_command": "rerun background Agent(pm-backlog-promoter) mode=refill-ready-pool; this degraded manifest is not closure",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def print_promoter_summary(
    candidates: list[dict[str, Any]],
    skipped: dict[str, list[dict[str, Any]]],
    selected: list[dict[str, Any]],
    manifest: Path,
    *,
    applied: bool,
    current_ready_pool_count: int = 0,
    promotion_deficit: int = 0,
    metadata_candidates: list[dict[str, Any]] | None = None,
    promotion_results: list[dict[str, Any]] | None = None,
) -> None:
    results = promotion_results or []
    promoted = [result for result in results if result.get("ok")]
    promotion_failed = [result for result in results if not result.get("ok")]
    metadata_candidates = metadata_candidates or []
    counts = {
        "auto_promotable": len(candidates),
        "needs_ready_pool_metadata": len(metadata_candidates),
        "selected": len(selected),
        "promoted": len(promoted),
        "promotion_failed": len(promotion_failed),
        "skipped": {key: len(value) for key, value in skipped.items()},
    }
    print("PM_BACKLOG_PROMOTER")
    print("counts=" + json.dumps(counts, sort_keys=True))
    print(
        f"ready_pool_current={current_ready_pool_count} target={READY_POOL_TARGET} "
        f"deficit={promotion_deficit} scanned_clean={len(selected)} "
        f"metadata_repair_candidates={len(metadata_candidates)}"
    )
    print(f"manifest={manifest}")
    if selected:
        print("SELECTED")
        for row in selected:
            print(
                f"- #{row['number']} priority={row.get('priority','')} "
                f"lane={row.get('lane','')} claimable={row.get('claimable_slot_type','')} "
                f"title={row['title']}"
            )
    if metadata_candidates:
        print("NEEDS_READY_POOL_METADATA")
        for row in metadata_candidates:
            print(
                f"- #{row['number']} priority={row.get('priority','')} "
                f"reason={row.get('reason','missing_ready_pool_contract')} title={row['title']}"
            )
    print(f"applied={str(applied).lower()}")
    if promoted:
        print("PROMOTED_VERIFIED")
        for result in promoted:
            print(f"- #{result['issue']} labels={','.join(result.get('live_labels') or [])}")
    if promotion_failed:
        print("PROMOTION_FAILED")
        for result in promotion_failed:
            print(f"- #{result['issue']} reason={result.get('reason','unknown')}")
    if not applied:
        print(
            "next=Agent(subagent_type=pm-backlog-promoter,run_in_background=true,mode=refill-ready-pool) "
            f"(no architecture-review prerequisite; promote all eligible P0-P2)"
        )


def run_slot_dispatch_sweep() -> None:
    # The slot-dispatch-sweep helper is RETIRED (Rajiv directive 2026-09-21).
    # Ready Pool dispatch is PM-owned and runs through Skill(direct-assign)
    # (MoP slot REST POST /slots/:n/assign); this promoter only repairs the
    # GitHub Ready Pool contract and hands the queue to PM.
    print(
        "next=PM dispatches the repaired Ready Pool through Skill(direct-assign); "
        "no slot-dispatch-sweep helper exists (retired 2026-09-21).",
        file=sys.stderr,
    )


def promote_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Promote all eligible P0-P2 issues and refill the remaining claimable deficit from P3.")
    parser.add_argument("--dry-run", action="store_true", help="explicit no-op mode; default behavior")
    parser.add_argument("--apply", action="store_true", help="move selected backlog issues to status:todo and run dispatch sweep")
    parser.add_argument(
        "--defer-reconcile",
        action="store_true",
        help="apply label/body changes only; caller owns one final PM sync/dispatch after a batch",
    )
    parser.add_argument(
        "--review-result",
        action="store_true",
        help="write an optional hash-pinned PM decision manifest without changing GitHub labels",
    )
    parser.add_argument("--dry-run-manifest", help="dry-run manifest reviewed before --review-result")
    parser.add_argument("--accepted", nargs="*", type=int, default=[], help="PM-accepted issue numbers for optional --review-result")
    parser.add_argument("--rejected-json", help="JSON file listing rejected/parked candidates for --review-result")
    parser.add_argument("--notes", default="", help="review-result or degraded-manifest notes")
    parser.add_argument("--issues", nargs="*", type=int, help="optional issue number filter")
    parser.add_argument(
        "--force-recheck",
        action="store_true",
        help="explicitly reconsider unchanged terminal rejections; use only after reviewed evidence changed",
    )
    parser.add_argument(
        "--review-result-manifest",
        help="optional hash-pinned PM candidate decision manifest; no architecture review required",
    )
    parser.add_argument(
        "--add-ready-pool-defaults",
        action="store_true",
        help="for evidence-grounded metadata-repair candidates, append default Ready Pool metadata before promoting",
    )
    args = parser.parse_args(argv)

    if args.review_result:
        if not args.dry_run_manifest:
            parser.error("--review-result requires --dry-run-manifest")
        rejected = read_rejected_json(args.rejected_json)
        manifest = write_promoter_review_result_manifest(
            dry_run_manifest=args.dry_run_manifest,
            accepted=args.accepted or [],
            rejected=rejected,
            notes=args.notes,
        )
        print("PM_BACKLOG_PROMOTER_REVIEW_RESULT")
        print(f"manifest={manifest}")
        print(f"accepted={len(args.accepted or [])}")
        print(f"rejected={len(rejected)}")
        if args.accepted:
            print(
                "next=/Users/rajiv/.claude/scripts/backlog-triage.py promote --apply "
                f"--review-result-manifest {manifest} --issues "
                + " ".join(str(i) for i in args.accepted)
            )
        else:
            print("next=none; no accepted backlog promotion candidates")
        return 0

    issue_filter = set(args.issues or []) or None
    try:
        all_issues = gh_issue_list()
        open_nums = issue_num_set(all_issues)
        current_ready_pool_count, promotion_deficit = compute_promotion_deficit(all_issues, open_nums, issue_filter)
        candidates, skipped = classify_promoter(
            all_issues,
            open_nums,
            issue_filter,
            force_recheck=args.force_recheck,
        )
    except Exception as exc:
        manifest = write_promoter_degraded_manifest(
            stage="candidate_discovery",
            error=str(exc),
            applied=bool(args.apply),
        )
        print("PM_BACKLOG_PROMOTER_DEGRADED")
        print(f"manifest={manifest}")
        print(f"error={exc}", file=sys.stderr)
        return 2
    # 10 is a FLOOR, never a ceiling (Rajiv 2026-10-03 / 2026-10-11): a full
    # pool must not hide eligible candidates (e.g. a P0 while pool=17, 0 P0).
    metadata_candidates: list[dict[str, Any]] = [
        row for row in (skipped.get("missing_ready_pool_contract") or [])
        if row.get("priority") != "P3" or row.get("open_dependencies") or promotion_deficit > 0
    ]
    selected: list[dict[str, Any]] = list(candidates)
    if args.add_ready_pool_defaults:
        selected = selected + list(metadata_candidates)
    considered = list(selected)
    selected = select_promotion_rows(selected, current_ready_pool_count)
    selected_numbers = {int(row["number"]) for row in selected}
    skipped["p3_refill_not_needed"] = [row for row in considered if int(row["number"]) not in selected_numbers]

    promotion_results: list[dict[str, Any]] = []
    if args.apply:
        # Codex arch review removed (Rajiv 2026-10-11): manifest optional.
        if args.review_result_manifest:
            try:
                accepted_issues, reviewed_body_hashes = load_review_approval(args.review_result_manifest)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"ERROR: review approval is invalid: {exc}", file=sys.stderr)
                return 2
            selected_numbers = {int(row["number"]) for row in selected}
            unapproved = sorted(selected_numbers - accepted_issues)
            stale_or_unpinned = sorted(
                int(row["number"])
                for row in selected
                if reviewed_body_hashes.get(int(row["number"])) != row.get("body_sha256")
            )
            if unapproved or stale_or_unpinned:
                print(
                    "ERROR: promotion admission proof failed "
                    f"unapproved={unapproved} stale_or_unpinned={stale_or_unpinned}",
                    file=sys.stderr,
                )
                return 2
        # All P0-P2 proceed even above ten. P3 selection already respects the deficit.
        for row in selected:
            promotion_results.append(
                promote_backlog_issue(
                    row,
                    open_nums,
                    add_ready_pool_defaults=args.add_ready_pool_defaults,
                )
            )
        promoted = [result for result in promotion_results if result.get("ok")]
        promotion_failed = [result for result in promotion_results if not result.get("ok")]
        manifest = write_promoter_manifest(
            candidates,
            skipped,
            selected,
            applied=True,
            current_ready_pool_count=current_ready_pool_count,
            promotion_deficit=promotion_deficit,
            metadata_candidates=metadata_candidates,
            promotion_results=promotion_results,
        )
        print_promoter_summary(
            candidates,
            skipped,
            selected,
            manifest,
            applied=True,
            current_ready_pool_count=current_ready_pool_count,
            promotion_deficit=promotion_deficit,
            metadata_candidates=metadata_candidates,
            promotion_results=promotion_results,
        )
        if promoted and PM_OPS.exists() and not args.defer_reconcile:
            run(["python3", str(PM_OPS), "sync", "--write", "--reason", "backlog-promoter"])
        if promotion_failed:
            print(f"ERROR: promotion verification failed for {len(promotion_failed)} issue(s); dispatch sweep skipped", file=sys.stderr)
            return 2
        if promoted and not args.defer_reconcile:
            run_slot_dispatch_sweep()
    else:
        manifest = write_promoter_manifest(
            candidates,
            skipped,
            selected,
            applied=False,
            current_ready_pool_count=current_ready_pool_count,
            promotion_deficit=promotion_deficit,
            metadata_candidates=metadata_candidates,
        )
        print_promoter_summary(
            candidates,
            skipped,
            selected,
            manifest,
            applied=False,
            current_ready_pool_count=current_ready_pool_count,
            promotion_deficit=promotion_deficit,
            metadata_candidates=metadata_candidates,
        )

    return 0


def migrate_blocked_todo(row: dict[str, Any]) -> None:
    """Compatibility no-op: S113 retains dependency-held Todos at every priority."""
    return


def migrate_dependency_blocked_todos(buckets: dict[str, list[dict[str, Any]]]) -> None:
    """Apply only the dependency migration selected by the legacy CLI flag."""
    for row in buckets["todo_blocked_dependency"]:
        migrate_blocked_todo(row)


def upsert_triage_obligations(
    buckets: dict[str, list[dict[str, Any]]], issue_filter: set[int] | None
) -> None:
    for row in buckets["todo_priority_label_drift"]:
        if issue_filter and int(row["number"]) not in issue_filter:
            continue
        issue = int(row["number"])
        pm_ops_upsert(
            "ready_pool",
            "high",
            f"#{issue} has non-canonical Ready Pool priority labels",
            (
                "Repair GitHub priority to exactly one P0/P1/P2/P3 label matching "
                "Ready Pool priority, remove deprecated aliases, then dispatch with Skill(direct-assign)."
            ),
            issue,
            f"backlog-triage:{row.get('reason','priority_label_drift')}",
            horizon="hourly",
        )

    for row in buckets["backlog_promote_candidate"]:
        if issue_filter and int(row["number"]) not in issue_filter:
            continue
        issue = int(row["number"])
        pm_ops_upsert(
            "priority_review",
            "high",
            f"#{issue} looks like unblocked backlog that may need promotion",
            "Review against Rajiv priority, add/repair ready-pool frontmatter, then move to status:todo or document the blocker.",
            issue,
            "backlog-triage:important_unblocked_backlog",
        )

    for row in buckets["backlog_metadata_missing"]:
        if issue_filter and int(row["number"]) not in issue_filter:
            continue
        issue = int(row["number"])
        pm_ops_upsert(
            "backlog",
            "medium",
            f"#{issue} backlog parking contract is incomplete",
            "Add parent/blocker, unblock condition, and owner/next reviewer, or promote to status:todo if it is runnable now.",
            issue,
            f"backlog-triage:{row.get('reason','metadata_missing')}",
        )


def write_manifest(
    buckets: dict[str, list[dict[str, Any]]],
    *,
    scope: dict[str, Any] | None = None,
) -> Path:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"pm-backlog-triage-{ts}.json"
    counts = {key: len(value) for key, value in buckets.items()}
    payload = {
        "repo": REPO,
        "generated_at": ts,
        "counts": counts,
        "buckets": buckets,
    }
    if scope:
        payload["scope"] = scope
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def print_summary(
    buckets: dict[str, list[dict[str, Any]]],
    path: Path,
    *,
    scope: dict[str, Any] | None = None,
) -> None:
    counts = {key: len(value) for key, value in buckets.items()}
    print("PM_BACKLOG_TRIAGE")
    print("counts=" + json.dumps(counts, sort_keys=True))
    if scope:
        print("scope=" + json.dumps(scope, sort_keys=True))
    print(f"manifest={path}")

    sections = [
        ("TODO_BLOCKED_DEPENDENCY", "todo_blocked_dependency"),
        ("TODO_DEPENDENCY_GATED", "todo_dependency_gated"),
        ("TODO_READY_GATED_P0", "todo_ready_gated_p0"),
        ("TODO_PRIORITY_LABEL_DRIFT", "todo_priority_label_drift"),
        ("TODO_MISSING_READY_POOL", "todo_missing_ready_pool"),
        ("TODO_DISPATCH_READINESS_FAILED", "todo_dispatch_readiness_failed"),
        ("BACKLOG_PROMOTE_CANDIDATE", "backlog_promote_candidate"),
        ("BACKLOG_METADATA_MISSING", "backlog_metadata_missing"),
        ("STATUS_HYGIENE", "status_hygiene"),
    ]
    for heading, key in sections:
        rows = buckets[key]
        if not rows:
            continue
        print(heading)
        for row in rows:
            deps = ",".join(f"#{dep}" for dep in row.get("open_dependencies", [])) or "none"
            print(f"- #{row['number']} deps={deps} reason={row.get('reason','')} title={row['title']}")


def check_dispatch_readiness_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Validate the status:todo dispatchability invariant.")
    parser.add_argument("--issues", nargs="+", type=int, required=True)
    parser.add_argument(
        "--allow-backlog",
        action="store_true",
        help="validate a backlog issue for admission before status:todo is applied",
    )
    args = parser.parse_args(argv)

    all_issues = gh_issue_list()
    open_nums = issue_num_set(all_issues)
    by_number = {int(issue["number"]): issue for issue in all_issues}
    failures: list[dict[str, Any]] = []
    for number in args.issues:
        issue = by_number.get(number)
        if issue is None:
            failures.append({"issue": number, "reasons": ["open_issue_not_found"]})
            continue
        reasons = dispatch_readiness_reasons(issue, open_nums, allow_backlog=args.allow_backlog)
        if reasons:
            failures.append({"issue": number, "reasons": reasons})

    if failures:
        print("DISPATCH_READINESS_FAILED " + json.dumps(failures, sort_keys=True))
        return 13
    print("DISPATCH_READINESS_OK issues=" + ",".join(str(number) for number in args.issues))
    return 0


def audit_ready_pool_main(argv: list[str]) -> int:
    """Audit the complete live Todo pool without applying mutations.

    This is intentionally independent of the heartbeat backlog intake window:
    old Todo rows remain scheduler inputs and must retain a valid Ready Pool
    contract even when no backlog issue was created recently.
    """
    parser = argparse.ArgumentParser(
        description="Audit every open status:todo issue against the Ready Pool invariant."
    )
    parser.parse_args(argv)

    all_issues = gh_issue_list()  # gh_issue_list is exhaustive up to 1000 rows.
    open_nums = issue_num_set(all_issues)
    pr_owned = open_pr_issue_numbers()
    todos = [
        issue
        for issue in all_issues
        if sorted(name for name in labels(issue) if name.startswith(STATUS_PREFIX))
        == ["status:todo"]
    ]
    contract_rows: dict[int, dict[str, Any]] = {}
    dependency_rows: list[dict[str, Any]] = []
    clean_issues: list[int] = []
    gated_p0_issues: list[int] = []
    for issue in todos:
        number = int(issue["number"])
        names = labels(issue)
        meta = ready_pool_metadata(issue)
        reasons: list[str] = []
        if number in pr_owned or any(re.fullmatch(r"slot:\d+", name) for name in names):
            reasons.append("already_owned_reconcile_lifecycle")
        missing = missing_ready_pool_dispatch_fields(meta)
        if missing:
            reasons.append("missing_ready_pool_fields:" + ",".join(missing))
        drift = priority_label_drift(issue, meta, require_label=True)
        if drift:
            reasons.append("priority_label_drift:" + drift)
        # S113: dependency-held membership applies to every priority.
        contract_issue = {**issue, "labels": [l for l in issue.get("labels", []) if l.get("name") != "pm-blocked:dependency"]}
        if has_pm_blocker_label(names - {"pm-blocked:dependency"}):
            reasons.append("pm_blocker_label")
        if meta and not metadata_blockers_clear(meta) and not has_dependency_contract(meta):
            reasons.append("metadata_blocker")
        if is_tracker(issue) or is_non_dispatchable_contract(contract_issue, meta):
            reasons.append("tracking_or_non_dispatchable")
        if icl_enforcement_applies(issue):
            for error in issue_contract_ledger_errors(issue.get("body") or ""):
                reasons.append("issue_contract_ledger:" + error)

        deps = open_dependencies(issue, open_nums)
        if deps:
            dependency_rows.append(
                {
                    "issue": number,
                    "title": issue.get("title") or "",
                    "open_dependencies": deps,
                    "reason": "open_dependency",
                }
            )
        if reasons:
            contract_rows[number] = {
                "issue": number,
                "title": issue.get("title") or "",
                "reasons": reasons,
            }
        if not reasons and deps and is_ready_gated_p0(names, meta):
            gated_p0_issues.append(number)
        if not reasons and not deps:
            clean_issues.append(number)

    clean_issues.sort()
    contract_defects = sorted(contract_rows.values(), key=lambda row: row["issue"])
    gated_dependencies = sorted(dependency_rows, key=lambda row: row["issue"])
    dependency_defects: list[dict[str, Any]] = []  # compatibility: waits are not defects

    manifest = {
        "schema": "pm-ready-pool-audit/v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "all-open-status-todo",
        "open_issue_scan_limit": 1000,
        "todo_count": len(todos),
        "clean_todo_count": len(clean_issues),
        "clean_issues": clean_issues,
        "ready_gated_p0": sorted(gated_p0_issues),
        "contract_defects": contract_defects,
        "dependency_defects": dependency_defects,
        "dependency_gated": gated_dependencies,
        "dependency_gated_count": len(gated_dependencies),
    }
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = OUT_DIR / f"pm-ready-pool-audit-{ts}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        "READY_POOL_AUDIT "
        f"todo={len(todos)} clean={len(clean_issues)} dependency_gated={len(gated_dependencies)} ready_gated_p0={len(gated_p0_issues)} "
        f"contract_defects={len(contract_defects)} "
        f"dependency_defects={len(dependency_defects)}"
    )
    print(f"manifest={path}")
    if contract_defects:
        issue_list = ",".join(f"#{row['issue']}" for row in contract_defects)
        print(f"READY_POOL_CONTRACT_REPAIR_REQUIRED issues={issue_list}")
        for row in contract_defects:
            print(f"- #{row['issue']} reasons={';'.join(row['reasons'])} title={row['title']}")
    if dependency_defects:
        issue_list = ",".join(f"#{row['issue']}" for row in dependency_defects)
        print(f"TODO_BLOCKED_DEPENDENCY issues={issue_list}")
    if not contract_defects and not dependency_defects:
        print("READY_POOL_AUDIT_CLEAR")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "promote":
        return promote_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "repair-ready-pool":
        return repair_ready_pool_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "check-dispatch-readiness":
        return check_dispatch_readiness_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "audit-ready-pool":
        return audit_ready_pool_main(sys.argv[2:])

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="explicit no-op mode; default behavior")
    parser.add_argument("--apply", action="store_true", help="write safe PM ops updates; does not promote backlog to todo")
    parser.add_argument(
        "--migrate-blocked-todos",
        action="store_true",
        help=(
            "deprecated compatibility flag; dependency-held Todos remain in the Ready Pool (S113)"
        ),
    )
    parser.add_argument(
        "--promote-limit",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--issues", nargs="*", type=int, help="optional issue number filter")
    parser.add_argument(
        "--surface",
        choices=("full", "heartbeat"),
        default="full",
        help="full planning audit or heartbeat-scoped recent intake audit",
    )
    parser.add_argument(
        "--recent-hours",
        type=int,
        default=HEARTBEAT_RECENT_HOURS,
        help="creation-age window for --surface heartbeat (default: 72)",
    )
    args = parser.parse_args()

    issue_filter = set(args.issues or []) or None
    all_issues = gh_issue_list()
    open_nums = issue_num_set(all_issues)
    issues = all_issues
    scope: dict[str, Any] | None = None
    if issue_filter:
        issues = [issue for issue in issues if int(issue["number"]) in issue_filter]
    elif args.surface == "heartbeat":
        issues, scope = select_heartbeat_triage_issues(
            issues,
            recent_hours=args.recent_hours,
        )

    buckets = classify(issues, open_nums)
    manifest = write_manifest(buckets, scope=scope)
    print_summary(buckets, manifest, scope=scope)

    if args.apply:
        if args.migrate_blocked_todos:
            migrate_dependency_blocked_todos(buckets)
        upsert_triage_obligations(buckets, issue_filter)
        if PM_OPS.exists():
            # The triage pass above already read live GitHub state and wrote the
            # affected obligations. Rendering that ledger must not start a
            # second, repository-wide live issue scan: slot-claim invokes this
            # path synchronously for one issue, so the broad sync can hold a
            # released slot command open for several minutes. Keep the render
            # bounded to the freshly reconciled ledger.
            run(
                [
                    "python3",
                    str(PM_OPS),
                    "sync",
                    "--write",
                    "--no-live",
                    "--reason",
                    "backlog-triage",
                ]
            )
        print("applied=true")
    else:
        print("applied=false")
        print("next=/Users/rajiv/.claude/scripts/backlog-triage.py --apply")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
