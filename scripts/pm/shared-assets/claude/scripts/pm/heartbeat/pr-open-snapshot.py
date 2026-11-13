#!/usr/bin/env python3
"""Live open-PR snapshot shared by the hourly drive and the 3h heartbeat.

Gate color comes from live GitHub (`gh pr list` statusCheckRollup on the
current headRefOid). A red PR is "unowned" only when it has no MoP slot
owner, no slot:N label, and no PM-owned wait (CI still running, review hold,
or a capture/CI/review obligation owner). Read-only; takes no PR actions.

Output (JSON, or --text one line per PR):
  {number, head, title, draft, state, owner, next}
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import sys

_SCRIPTS = Path.home() / ".claude" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import gh_budget

_REVIEW_GATE_PATH = Path(__file__).resolve().parents[2] / "ci" / "pm-admit-12b.py"
_REVIEW_GATE_SPEC = importlib.util.spec_from_file_location("pm_admit_12b_shared", _REVIEW_GATE_PATH)
_REVIEW_GATE = importlib.util.module_from_spec(_REVIEW_GATE_SPEC)
_REVIEW_GATE_SPEC.loader.exec_module(_REVIEW_GATE)

REPO = "Scribie/heydonna-app"
# Shared with every other hook/heartbeat caller. A fresh file skips both the
# `gh pr list` GraphQL read and the review-thread query.
SNAPSHOT_TTL = gh_budget.DEFAULT_TTL
RED = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
PENDING = {"", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", "REQUESTED"}
# Required merge gates: the CI `test` aggregate job (ci.yml) and the E2E `e2e` job
# (e2e.yml). Branch protection is not readable on this plan, so these aggregate job
# names are the required-check source. Every other check (e.g. core-large-file-correctness)
# is advisory: it never makes a PR red and never suppresses a merge ask.
GATES = ("test", "e2e")
CODEX_LABEL = "pm-blocked:codex"
CODEX_AUTHOR = re.compile(r"codex", re.I)  # chatgpt-codex-connector and friends
CODEX_BLOCKING = re.compile(r"\bP[01]\b")  # "![P1 Badge]" etc.

MOP_URL = "http://127.0.0.1:3100/slots"
# Paths that never need real CI/E2E (Rajiv 2026-09-28/29): a PR touching only
# these gets a direct CI-exempt merge ask instead of sitting in the PM 12b queue.
CI_EXEMPT_PREFIXES = ("benchmarks/", "website/", "scripts/ci/hetzner/", "docs/", "docs-site/", "blog/")
DO_NOT_MERGE_RE = re.compile(r"do not merge", re.IGNORECASE)
# Rajiv 2026-10-01 13:17 IST (C0ALZJHGE49/1790840684.251169): "why are so many
# pr's stuck at pm 12b review? is there a workflow gap somewhere?" -- PRs whose
# 12b already BLOCKed (rework queued) or whose CI-exempt merge ask is already
# with PR Merges were re-reported as review-admit every pass. These states are
# now reported as such and never re-raise pr_admission until the head moves.
REWORK_QUEUED = "rework-queued (no slot)"
MERGE_ASK_PENDING = "merge-ask-pending"
REWORK_OWNER_RE = re.compile(r"rework|block|revise", re.IGNORECASE)

# --- Deterministic every-turn open-PR obligations (Rajiv 2026-09-30 00:58/01:00
# IST, thread C0ALZJHGE49/1790707604.948989: "the pr's should never get
# stuck" / "there's already an obligation system we have. reuse that instead
# of adding a new hook"). Each open, non-draft, non-pm-blocked, non-slot-owned
# PR gets up to three pm-ops obligation rows (one per concern below), created
# and resolved through the existing writer so the existing
# pm-ops-sync-stop-validator Stop hook surfaces them every PM turn like any
# other due obligation. No new hook, no new cadence.
PM_OPS_CLI = Path(
    __import__("os").environ.get("PM_OPS_CLI", str(Path.home() / ".claude/scripts/pm-ops.py"))
)
PM_OPS_DB = Path(
    __import__("os").environ.get(
        "PM_OPS_DB",
        str(
            Path.home()
            / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
        ),
    )
)
GREEN_MIN_DURATION_SECONDS = 5 * 60
OBLIGATION_KINDS = ("pr_admission", "pr_red_unowned", "pr_merge_ask")
# PM claims ownership of an in-flight PR obligation (e.g. right after
# launching ci-repair-agent on a red PR) with the one-liner documented in
# .claude/rules/20-buddhi-pm.md §12b -- --owner names the claimant and
# --suppress-until stops the existing pm-ops-sync-stop-validator Stop hook
# from re-surfacing the row while the claim is fresh. A claim is fresh for
# OWNER_CLAIM_TTL_SECONDS; while fresh, this producer additionally leaves the
# row alone (no upsert) instead of treating it as still-unowned, so it never
# clobbers the claim on the next hourly/3h/every-stop sync pass. See
# sync_pr_obligations().
OWNER_CLAIM_TTL_SECONDS = 60 * 60
# Open-row owners that are a PM wait even after suppress_until lapses.
# Generic "PM" and unrelated names are not a wait.
PM_WAIT_OWNER_RE = re.compile(
    r"capture|replay|(?:^|[^a-z])ci(?:[^a-z]|$)|review|12b|admission",
    re.IGNORECASE,
)


def _iso_duration_seconds(started: str, completed_at: str) -> float:
    try:
        def parse(value: str) -> "datetime.datetime":
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))

        return (parse(completed_at) - parse(started)).total_seconds()
    except Exception:
        return 0.0


def gate_state(pr: dict) -> dict:
    """Pure: required-gate (GATES) red/green-with-duration state for one PR."""
    checks = summarize_checks(pr.get("statusCheckRollup") or [])
    required_red = [n for n in checks["red"] if n in GATES]
    gate_durations_ok = set()
    for check in pr.get("statusCheckRollup") or []:
        name, value = _check_result(check)
        if name not in GATES or value != "SUCCESS":
            continue
        started, completed_at = check.get("startedAt"), check.get("completedAt")
        if isinstance(started, str) and isinstance(completed_at, str):
            if _iso_duration_seconds(started, completed_at) >= GREEN_MIN_DURATION_SECONDS:
                gate_durations_ok.add(name)
    green_ready = all(g in checks["green"] and g in gate_durations_ok for g in GATES)
    return {"required_red": required_red, "green_ready": green_ready}


def _obligation_upsert(*, kind: str, pr: int, head: str, title: str, action: str, owner: str | None = None) -> None:
    if not PM_OPS_CLI.is_file():
        return
    argv = [
        sys.executable, str(PM_OPS_CLI), "obligation-upsert",
        "--kind", kind,
        "--severity", "high",
        "--horizon", "hourly",
        "--target-type", "github_pr",
        "--target-id", str(pr),
        "--pr", str(pr),
        "--title", title[:200],
        "--action", action,
        "--evidence-json", json.dumps({"pm_stop_actionable": True, "head": head}),
    ]
    if owner:
        argv += ["--owner", owner]
    try:
        subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=20, check=False)
    except Exception:
        pass


def _existing_obligation(kind: str, pr: int, *, strict: bool = False) -> dict | None:
    """Read-only lookup of the current open (kind, pr) row, or None.

    Used only to detect an agent's ownership claim before deciding whether to
    upsert. Never mutates; failures return None (fail open -> treat as
    unclaimed, same as today).
    """
    if not PM_OPS_DB.is_file():
        if strict:
            raise OSError("ownership ledger unavailable")
        return None
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{PM_OPS_DB}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT id, pr, owner, updated_at, suppress_until, evidence_json FROM obligations
            WHERE status='open' AND kind=? AND pr=?
            ORDER BY id DESC LIMIT 1
            """,
            (kind, pr),
        ).fetchone()
        con.close()
    except Exception:
        if strict:
            raise
        return None
    return dict(row) if row else None


def active_claim(row: dict | None, now: "datetime.datetime | None" = None) -> dict | None:
    """Pure: {owner, until} when an open obligation row has a non-empty owner and a
    suppress_until still in the future (PM bg-flow ownership, e.g. captures/agents)."""
    if not row:
        return None
    owner = (row.get("owner") or "").strip()
    until_raw = row.get("suppress_until")
    if not owner or not isinstance(until_raw, str) or not until_raw:
        return None
    try:
        until = datetime.datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {"owner": owner, "until": until} if until > now else None


def pm_wait_from_row(row: dict | None, now: "datetime.datetime | None" = None) -> dict | None:
    """Pure: a PM-owned wait on an open obligation row, or None.

    A live suppress_until claim (any non-PM owner) is a wait. After that window
    lapses, an owner that still names CI, review, or capture is a wait too.
    Generic owner "PM" is not a wait.
    """
    if not row:
        return None
    live = active_claim(row, now)
    if live and live["owner"].lower() != "pm":
        return live
    owner = (row.get("owner") or "").strip()
    if not owner or owner.lower() == "pm":
        return None
    if owner.lower().startswith("slot:") or PM_WAIT_OWNER_RE.search(owner):
        return {"owner": owner, "until": None}
    return None


def red_is_unowned(*, slot_label: bool, mop_owned: bool, pm_wait: bool) -> bool:
    """Unowned iff no MoP slot owner, no slot:N label, and no PM-owned wait."""
    return not slot_label and not mop_owned and not pm_wait


def _claim_owner_label(claim: dict) -> str:
    until = claim.get("until")
    if until is not None:
        return f"owner: {claim['owner']} (until {until.astimezone().strftime('%H:%M')})"
    return f"owner: {claim['owner']}"


def fetch_live_claims(prs: list[dict]) -> dict[int, dict]:
    """Read-only: bind canonical live claims to the fetched PR's exact head.

    Owner text alone, an old-head claim, or a failed lookup cannot establish
    live ownership. Keep that uncertainty visible instead of dispatching again.
    """
    claims: dict[int, dict] = {}
    for pr in prs:
        number = pr.get("number")
        if not isinstance(number, int):
            continue
        unknown = None
        for kind in OBLIGATION_KINDS:
            try:
                row = _existing_obligation(kind, number, strict=True)
                claim = pm_wait_from_row(row)
                if not claim:
                    continue
                evidence = json.loads(row.get("evidence_json") or "{}")
                recorded_head = evidence.get("head") if isinstance(evidence, dict) else None
                if (isinstance(recorded_head, str) and re.fullmatch(r"[0-9a-f]{40}", recorded_head)
                        and recorded_head == pr.get("headRefOid") and active_claim(row)):
                    claims[number] = {**claim, "head": recorded_head, "row_id": row["id"]}
                    break
                unknown = {"owner": row.get("owner"), "ownership_unknown": True,
                           "reason": "claim head missing/mismatched or live claim window expired"}
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                unknown = {"owner": "unknown", "ownership_unknown": True,
                           "reason": "ownership evidence unavailable or malformed"}
        else:
            if unknown:
                claims[number] = unknown
    return claims


def _is_fresh_owner_claim(row: dict | None) -> bool:
    """True when `row` records a non-PM owner claimed within the TTL."""
    if not row:
        return False
    owner = (row.get("owner") or "").strip()
    if not owner or owner.lower() == "pm":
        return False
    updated_at = row.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        return False
    try:
        parsed = datetime.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        age = (datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds()
    except Exception:
        return False
    return 0 <= age <= OWNER_CLAIM_TTL_SECONDS


def _row_holds_head(row: dict | None, head: str) -> bool:
    """True when a non-PM owner's open row still holds the PR at this head.

    A row recording a head holds only that head (a new head re-raises). A row
    without a head holds while its suppress_until is in the future.
    """
    if not row:
        return False
    owner = (row.get("owner") or "").strip()
    if not owner or owner.lower() == "pm":
        return False
    try:
        row_head = (json.loads(row.get("evidence_json") or "{}") or {}).get("head") or ""
    except Exception:
        row_head = ""
    if row_head:
        return bool(head) and row_head == head
    until = row.get("suppress_until")
    if not isinstance(until, str) or not until:
        return False
    try:
        parsed = datetime.datetime.fromisoformat(until.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return False
    return parsed > datetime.datetime.now(datetime.timezone.utc)


def _authorized_12b_reviewer() -> str:
    """Resolve the currently configured gh account; never guess a reviewer login."""
    try:
        result = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                                capture_output=True, text=True, timeout=15)
        login = result.stdout.strip()
        return login if result.returncode == 0 and login else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _latest_12b_verdict(pr: dict, head: str, authorized_login: str | None = None) -> str | None:
    """Use the admission helper's canonical marker and reviewed-head rules."""
    if authorized_login is None:
        authorized_login = pr.get("_authorized_12b_reviewer") or _authorized_12b_reviewer()
    try:
        return _REVIEW_GATE.reviewed_12b_verdict(
            pr.get("comments") or [], head, authorized_login, REPO, pr.get("number")
        )
    except Exception:
        # Missing authority or unavailable carry-forward evidence never grants 12b.
        return None


def _has_12b_block(pr: dict, head: str) -> bool:
    return _latest_12b_verdict(pr, head) == "BLOCK"


def _exempt_merge_eligible(pr: dict) -> bool:
    """Local path exemption alone is not enough: a RED required gate keeps the red disposition."""
    return _is_ci_exempt(pr) and not gate_state(pr)["required_red"]


def _is_ci_exempt(pr: dict) -> bool:
    files = [f.get("path", "") for f in (pr.get("files") or [])]
    return bool(files) and all(any(f.startswith(p) for p in CI_EXEMPT_PREFIXES) for f in files)


def admission_hold(pr: dict, rows: dict | None = None) -> str | None:
    """REWORK_QUEUED / MERGE_ASK_PENDING when an un-admitted PR must not be
    re-raised as review-admit at its current head, else None.

    `rows` maps obligation kind -> latest open row for this PR (or None).
    """
    rows = rows or {}
    head = pr.get("headRefOid") or ""
    adm = rows.get("pr_admission")
    verdict = _latest_12b_verdict(pr, head)
    if verdict == "BLOCK":
        return REWORK_QUEUED
    if verdict != "PASS" and adm and REWORK_OWNER_RE.search(adm.get("owner") or "") and _row_holds_head(adm, head):
        return REWORK_QUEUED
    if _exempt_merge_eligible(pr) and (
        _row_holds_head(rows.get("pr_merge_ask"), head) or _row_holds_head(adm, head)
    ):
        return MERGE_ASK_PENDING
    return None


def merge_ask_next(pr: dict) -> str:
    """Project the existing CI reconciliation disposition; green checks alone
    prove CI, not product readiness. Missing evidence is unknown, not a hold
    invented by this reporter or a new merge gate.
    """
    head = pr.get("headRefOid") or ""
    if DO_NOT_MERGE_RE.search(pr.get("body") or ""):
        return "blocked: DO NOT MERGE in PR body"
    if _has_12b_block(pr, head):
        return REWORK_QUEUED
    receipt = pr.get("ci_reconciliation")
    if not isinstance(receipt, dict) or not re.fullmatch(r"[0-9a-f]{40}", head):
        return "PM readiness unverified"
    if str(receipt.get("pr")) != str(pr.get("number")) or receipt.get("head_sha") != head:
        return "PM readiness unverified"
    if receipt.get("status") == "resolved":
        if receipt.get("resolution") == "blocked":
            return "PM readiness blocked: " + str(receipt.get("blocked_reason") or "unspecified")
        if receipt.get("resolution") == "merge_ready":
            return "CTO merge ask"
    return "PM readiness unverified"


def _read_ci_reconciliation(pr: dict) -> dict | None:
    """Read the sentinel written by ci-success-reconciliation.py, never mutate it."""
    directory = Path(__import__("os").environ.get("CI_SUCCESS_SENTINEL_DIR", "/tmp"))
    try:
        receipt = json.loads((directory / f"pm-required-ci-reconcile-{pr['number']}.json").read_text())
    except (OSError, ValueError, KeyError):
        return None
    return receipt if isinstance(receipt, dict) else None


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_rows(kind: str, pr: int) -> list[dict]:
    """Read-only: open (kind, pr) rows with their fingerprint. Failure -> []."""
    if not PM_OPS_DB.is_file():
        return []
    try:
        import hashlib
        import sqlite3

        con = sqlite3.connect(f"file:{PM_OPS_DB}?mode=ro", uri=True, timeout=2)
        rows = con.execute(
            "SELECT id, updated_at, evidence_json FROM obligations WHERE status='open' AND kind=? AND pr=? ORDER BY id",
            (kind, pr),
        ).fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for row_id, updated_at, evidence in rows:
        try:
            head = (json.loads(evidence or "{}") or {}).get("head") or ""
        except Exception:
            head = ""
        out.append({"id": int(row_id), "updated_at": updated_at or "", "head": head,
                    "evidence_sha256": hashlib.sha256((evidence or "").encode("utf-8")).hexdigest()})
    return out


def _obligation_resolve(*, kind: str, pr: int, reason: str, head: str | None = None,
                        observed_at: str | None = None) -> list[str]:
    """Resolve each open (kind, pr) row by exact id through
    obligation-resolve --if-unclaimed, bound to the observation that justified it.

    `observed_at` is when the live PR state (at classified `head`) was read. A row
    written at or after that instant carries a newer observation (e.g. a
    canonical upsert that installed head H2 or new evidence) and is skipped as
    skipped_changed. The row's (updated_at, evidence sha256) fingerprint is also
    passed to pm-ops, which re-checks it, the claim and open status in one
    BEGIN IMMEDIATE transaction, so an upsert between this read and the write is
    never closed on stale evidence. Best-effort; returns per-row outcomes."""
    if not PM_OPS_CLI.is_file():
        return []
    outcomes: list[str] = []
    for row in _open_rows(kind, pr):
        if observed_at and row["updated_at"] >= observed_at:
            outcomes.append("skipped_changed")
            continue
        try:
            argv = [
                sys.executable, str(PM_OPS_CLI), "obligation-resolve",
                "--kind", kind,
                "--id", str(row["id"]),
                "--if-unclaimed",
                "--expect-updated-at", row["updated_at"],
                "--expect-evidence-sha256", row["evidence_sha256"],
                "--reason", reason + (f" @head {head[:12]}" if head else ""),
            ]
            proc = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=20, check=False)
            outcomes.append(((proc.stdout or "").strip().splitlines() or ["error"])[-1] if proc.returncode == 0 else "error")
        except Exception:
            outcomes.append("error")  # best-effort: the row stays open
    return outcomes


def sync_pr_obligations(prs: list[dict], mop_owned_prs: set[int], observed_at: str | None = None) -> list[dict]:
    """Create/resolve pr_admission|pr_red_unowned|pr_merge_ask obligations.

    Fails open: any gh/pm-ops error must never raise; a skipped sync just
    means the Stop hook's next due-obligation pass is unchanged, never a
    block storm. Returns the list of {pr, kind, action} rows actually
    upserted, for callers that want to report what was created.
    """
    created: list[dict] = []
    seen_open_prs: set[int] = set()
    # When the PR list was read; resolves are bound to this observation.
    observed_at = observed_at or _utc_stamp()
    for pr in prs:
        number = pr.get("number")
        head = pr.get("headRefOid") or ""
        if not isinstance(number, int) or not head:
            continue
        seen_open_prs.add(number)
        labels = [label.get("name", "") for label in pr.get("labels", [])]
        blocked = any(x.startswith("pm-blocked:") for x in labels)
        owned = bool(pr.get("isDraft")) or blocked or number in mop_owned_prs
        title = (pr.get("title") or "")[:60]

        if owned:
            # Owned/blocked/draft: an in-flight obligation for this PR is no
            # longer PM's to chase; resolve any that are still open.
            for kind in OBLIGATION_KINDS:
                _obligation_resolve(kind=kind, pr=number, reason="owned_by_slot_or_blocked_or_draft", head=head, observed_at=observed_at)
            continue

        admitted = f"ci-head:{head}" in labels
        gates = gate_state(pr)

        if not admitted:
            adm_row = _existing_obligation("pr_admission", number)
            hold = admission_hold(pr, {"pr_admission": adm_row, "pr_merge_ask": _existing_obligation("pr_merge_ask", number)})
            rework_claim_released = (
                adm_row is not None
                and REWORK_OWNER_RE.search(adm_row.get("owner") or "")
                and _latest_12b_verdict(pr, head) == "PASS"
            )
            if hold or (_is_fresh_owner_claim(adm_row) and not rework_claim_released):
                pass  # 12b BLOCK / rework queued / merge ask pending / fresh claim at this head
            elif _is_ci_exempt(pr) and gates["required_red"]:
                pass  # red exempt PR: pr_red_unowned below owns it; merge ask resolved below
            elif _is_ci_exempt(pr):
                # CI-exempt paths need a direct merge ask, not 12b admission.
                _obligation_resolve(kind="pr_admission", pr=number, reason="ci_exempt_merge_ask", head=head, observed_at=observed_at)
                _obligation_upsert(
                    kind="pr_merge_ask", pr=number, head=head,
                    title=f"PR #{number} CI-exempt, no merge ask: {title}",
                    action="send the CI-exempt exact-head merge ask now; then claim with --owner pr-merges",
                )
                created.append({"pr": number, "kind": "pr_merge_ask"})
            else:
                _obligation_upsert(
                    kind="pr_admission", pr=number, head=head, title=f"PR #{number} not admitted: {title}",
                    action="launch pm-admission-reviewer now (PASS -> pm-admit-ci.sh same turn)",
                    owner="PM",
                )
                created.append({"pr": number, "kind": "pr_admission"})
        else:
            _obligation_resolve(kind="pr_admission", pr=number, reason="ci-head_matches_current_head", head=head, observed_at=observed_at)

        if gates["required_red"]:
            if _is_fresh_owner_claim(_existing_obligation("pr_red_unowned", number)):
                pass  # e.g. ci-repair-agent already claimed this PR/head
            else:
                _obligation_upsert(
                    kind="pr_red_unowned", pr=number, head=head,
                    title=f"PR #{number} red at {head[:12]}: {title}",
                    action="ci-repair-agent / e2e-failure-investigator / slot repro",
                )
                created.append({"pr": number, "kind": "pr_red_unowned"})
        else:
            _obligation_resolve(kind="pr_red_unowned", pr=number, reason="required_gates_not_red", head=head, observed_at=observed_at)

        if admitted and gates["green_ready"] and not gates["required_red"]:
            if merge_ask_next(pr) != "CTO merge ask":
                _obligation_resolve(kind="pr_merge_ask", pr=number, reason="readiness_not_merge_ready", head=head, observed_at=observed_at)
            elif _is_fresh_owner_claim(_existing_obligation("pr_merge_ask", number)):
                pass
            else:
                _obligation_upsert(
                    kind="pr_merge_ask", pr=number, head=head,
                    title=f"PR #{number} green, no merge ask: {title}",
                    action="send the exact-head merge ask to CTO now",
                )
                created.append({"pr": number, "kind": "pr_merge_ask"})
        elif not admitted and _is_ci_exempt(pr) and not gates["required_red"]:
            pass  # CI-exempt merge ask row is owned by the not-admitted branch above
        else:
            _obligation_resolve(kind="pr_merge_ask", pr=number, reason="not_green_or_not_admitted", head=head, observed_at=observed_at)

    _resolve_stale_pr_obligations(seen_open_prs)
    return created


STALE_RECHECK_CAP = 50


def _live_pr_state(pr: int) -> str | None:
    """Live PR state (OPEN/CLOSED/MERGED) or None when the read fails.

    REST pulls, not `gh pr view`: a simple state read was spending the shared
    GraphQL budget (up to STALE_RECHECK_CAP per sync).
    """
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{REPO}/pulls/{pr}"],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
        if proc.returncode != 0:
            return None
        doc = json.loads(proc.stdout)
        if doc.get("number") != pr:
            return None
        if doc.get("merged") is True:
            return "MERGED"
        state = str(doc.get("state") or "").upper()
        return state if state in ("OPEN", "CLOSED") else None
    except Exception:
        return None


def _resolve_stale_pr_obligations(seen_open_prs: set[int]) -> None:
    """Resolve pr_* obligations for PRs proven no longer open (merged/closed).

    `gh pr list --limit 100` can truncate, so absence from the list is NOT
    proof: each candidate PR is re-read live (at most STALE_RECHECK_CAP per
    run) and resolved only when gh says CLOSED/MERGED. A failed read or an
    OPEN PR keeps its rows; PRs whose rows are all actively claimed are not
    re-read. Writes go through obligation-resolve --if-unclaimed.
    """
    if not PM_OPS_DB.is_file():
        return
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{PM_OPS_DB}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in OBLIGATION_KINDS)
        rows = con.execute(
            f"""
            SELECT pr, owner, suppress_until FROM obligations
            WHERE status='open' AND kind IN ({placeholders}) AND pr IS NOT NULL
            ORDER BY pr
            """,
            OBLIGATION_KINDS,
        ).fetchall()
        con.close()
    except Exception:
        return
    candidates: dict[int, bool] = {}
    for row in rows:
        try:
            pr_number = int(row["pr"])
        except Exception:
            continue
        if pr_number in seen_open_prs:
            continue
        unclaimed = active_claim(dict(row)) is None
        candidates[pr_number] = candidates.get(pr_number, False) or unclaimed
    checked = 0
    for pr_number, any_unclaimed in sorted(candidates.items()):
        if not any_unclaimed:
            continue
        if checked >= STALE_RECHECK_CAP:
            break
        checked += 1
        observed = _utc_stamp()
        if _live_pr_state(pr_number) not in ("CLOSED", "MERGED"):
            continue
        for kind in OBLIGATION_KINDS:
            _obligation_resolve(kind=kind, pr=pr_number, reason="pr_no_longer_open", observed_at=observed)


def fetch_mop_owned_prs() -> set[int]:
    """PR numbers a live MoP slot is actively reworking. Best-effort: MoP down -> empty set."""
    try:
        with urllib.request.urlopen(MOP_URL, timeout=3) as resp:
            data = json.load(resp)
    except Exception:
        return set()
    owned = set()
    for slot in data.get("slots", []):
        pr = slot.get("pr")
        if isinstance(pr, int):
            owned.add(pr)
    return owned


def _check_result(check: dict) -> tuple[str, str]:
    name = check.get("name") or check.get("context") or "?"
    value = (check.get("conclusion") or check.get("state") or "").upper()
    if "exemption" in (check.get("workflowName") or "").lower():
        value = "PLACEHOLDER"  # CI-exemption stub: never counts as a real green gate
    if not check.get("conclusion") and (check.get("status") or "").upper() not in ("", "COMPLETED"):
        value = ""
    return name, value


def summarize_checks(rollup: list[dict]) -> dict:
    """Pure: collapse duplicate-named checks (placeholder SKIPPED + real run)."""
    by_name: dict[str, set[str]] = {}
    for check in rollup or []:
        name, value = _check_result(check)
        by_name.setdefault(name, set()).add(value)
    red = sorted(n for n, v in by_name.items() if v & RED)
    pending = sorted(n for n, v in by_name.items() if n not in red and v & PENDING)
    green = sorted(n for n, v in by_name.items() if n not in red and n not in pending and "SUCCESS" in v)
    return {"red": red, "pending": pending, "green": green}


def codex_threads(threads: list[dict] | None) -> dict | None:
    """Pure: count unresolved Codex-bot P0/P1 review threads (live vs isOutdated).

    `threads` items: {isResolved, isOutdated, author, body} (first comment).
    None means the live read failed -> unknown.
    """
    if threads is None:
        return None
    live = outdated = 0
    for t in threads:
        if t.get("isResolved") or not CODEX_AUTHOR.search(t.get("author") or ""):
            continue
        if not CODEX_BLOCKING.search(t.get("body") or ""):
            continue
        if t.get("isOutdated"):
            outdated += 1
        else:
            live += 1
    return {"live": live, "outdated": outdated}


def classify_pr(pr: dict, mop_owned_prs: set[int] | None = None, holds: dict[int, str] | None = None,
                claims: dict[int, dict] | None = None) -> dict:
    """Pure: one PR -> state/owner/next from live head checks, labels, body and comments.

    Priority order for `next` (Rajiv 2026-09-29: too many PRs were reported as
    "waiting for PM review" when most were actually slot rework, CI-in-progress,
    blocked, CI-exempt, or already 12b-passed):
      1. draft -> slot rework (draft)
      2. pm-state:qa-failed-rework / pm-blocked:codex / MoP-owned rework -> slot rework
      3. stale ci-head + runs in progress -> CI running
      4. "DO NOT MERGE" / blocked precondition in body -> blocked: <reason>
      5. CI-exempt paths only (benchmarks/, website/, scripts/ci/hetzner/, docs) -> CI-exempt merge ask
      6. PM 12b PASS comment on the current head -> 12b done -> admit
      7. otherwise -> PM 12b review
    """
    mop_owned_prs = mop_owned_prs or set()
    head = pr.get("headRefOid") or ""
    labels = [label.get("name", "") for label in pr.get("labels", [])]
    slots = [x.split(":", 1)[1] for x in labels if x.startswith("slot:")]
    admitted = f"ci-head:{head}" in labels
    # PM owns the CI/E2E terminal of an admitted head (Rajiv 2026-09-28); a slot label
    # only names the owner before admission.
    owner = "PM" if admitted or not slots else f"S{slots[0]}"
    stale_admission = any(x.startswith("ci-head:") for x in labels) and not admitted
    # The Codex wait comes from live review threads, never the pm-blocked:codex label.
    codex = codex_threads(pr.get("reviewThreads"))
    has_codex_label = CODEX_LABEL in labels
    blocked = [x.split(":", 1)[1] for x in labels
               if x.startswith("pm-blocked:") and (x != CODEX_LABEL or codex is None)]
    actions: list[str] = []
    if codex and codex["live"]:
        blocked.insert(0, f"codex P1 x{codex['live']}")
    if has_codex_label and codex is not None and codex["live"] == 0:
        actions.append(f"stale pm-blocked:codex label PR#{pr.get('number')}@{head} (0 live Codex P0/P1 threads) -> remove label")
    checks = summarize_checks(pr.get("statusCheckRollup") or [])
    gates_green = all(g in checks["green"] for g in GATES)

    required_red = [n for n in checks["red"] if n in GATES]
    advisory_red = [n for n in checks["red"] if n not in GATES]
    gates_pending = any(g in checks["pending"] for g in GATES)

    is_slot_rework = (
        "qa-failed-rework" in labels
        # A PM 12b BLOCK (pm-state:blocked-rework) must never yield a merge ask
        # (2026-09-30: the hourly asked CTO to merge the blocked #8525).
        or "blocked-rework" in labels
        or "pm-state:blocked-rework" in labels
        or "codex" in blocked
        or pr.get("number") in mop_owned_prs
    )

    if pr.get("isDraft"):
        state, nxt = "draft", "slot rework (draft)"
    elif is_slot_rework:
        state = f"held ({','.join(blocked)})" if blocked else "rework in progress"
        nxt = "slot rework"
    elif blocked:
        # A pm-blocked hold wins over every CI state: a blocked PR never gets a merge ask.
        state, nxt = f"held ({','.join(blocked)})", f"{owner} / {blocked[0]} wait"
    elif required_red:
        state = "red: " + ",".join(required_red)
        e2e = any("e2e" in n for n in required_red)
        nxt = "e2e-failure-investigator" if e2e else "ci-repair-agent"
        # Unowned means nobody is on it: no MoP slot, no slot:N label, and no
        # PM wait (CI still running, review hold, or a capture/CI/review claim).
        number = pr.get("number")
        claim_now = (claims or {}).get(number)
        review_hold = bool((holds or {}).get(number)) or _has_12b_block(pr, head)
        if claim_now and claim_now.get("ownership_unknown"):
            state += ", ownership unknown"
            owner = "ownership unknown"
            nxt = "reconcile ownership evidence"
            actions.append(f"PR#{number}@{head} -> reconcile ownership evidence before dispatch; preserve incident custody")
        elif red_is_unowned(
            slot_label=bool(slots),
            mop_owned=number in mop_owned_prs,
            pm_wait=bool(claim_now) or gates_pending or review_hold,
        ):
            state += ", unowned"
            actions.append(f"PR#{number}@{head} -> {nxt}; check existing incident custody before dispatch")
    elif gates_pending or (admitted and not gates_green and checks["pending"]):
        state, nxt = "CI running", "CI running" if stale_admission else "await terminal"
    elif gates_green:
        state = "CI+E2E green"
        if not admitted:
            nxt = "not admitted: no merge ask"
        else:
            # Admission transfers ownership to PM, not product-proof readiness.
            # Keep CI green while reporting the existing exact-head disposition.
            nxt = (holds or {}).get(pr.get("number")) or merge_ask_next(pr)
            if slots:
                actions.append(
                    f"stale slot:{slots[0]} label on admitted+green PR#{pr.get('number')}@{head} -> remove label"
                )
    elif admitted and not required_red and not any(g in checks["pending"] for g in GATES):
        # Admitted head with a required gate absent or reported SKIPPED (matches none of
        # red/pending/green): this is an admitted head awaiting or lacking terminal checks,
        # not an un-admitted PR — never turn it into a re-admission request.
        state = "admitted: gate missing/SKIPPED"
        nxt = "await terminal (gate not run)"
    else:
        state = "stale admission" if stale_admission else "not admitted"
        body = pr.get("body") or ""
        files = [f.get("path", "") for f in (pr.get("files") or [])]
        comments = pr.get("comments") or []
        dnm = DO_NOT_MERGE_RE.search(body)
        ci_exempt = bool(files) and all(
            any(f.startswith(p) for p in CI_EXEMPT_PREFIXES) for f in files
        )
        head_short = head[:7]
        has_12b_pass = _latest_12b_verdict(pr, head) == "PASS"
        ci_exempt = ci_exempt and not gate_state(pr)["required_red"]
        hold = (holds or {}).get(pr.get("number")) or (REWORK_QUEUED if _has_12b_block(pr, head) else None)
        if dnm:
            nxt = "blocked: DO NOT MERGE in PR body"
        elif hold:
            nxt = hold
        elif ci_exempt:
            nxt = "CI-exempt -> merge ask"
        elif has_12b_pass:
            nxt = "12b done -> admit"
        else:
            nxt = "PM 12b review"
    claim = (claims or {}).get(pr.get("number"))
    if not claim and nxt in {
        "PM 12b review", "12b done -> admit", "not admitted: no merge ask",
        "CI-exempt -> merge ask", "CTO merge ask",
    }:
        # The existing report consumer drives this exact-head next edge. No
        # obligation mirror is required, and an existing ask must not resend.
        actions.append(
            f"PR#{pr.get('number')}@{head} -> {nxt}; reconcile existing owner/ask custody first"
        )
    if claim and not claim.get("ownership_unknown") and (
        state.endswith(", unowned")
        or state.startswith("red:")
        or state in ("not admitted", "stale admission")
    ):
        # Owned by a PM wait (live suppress_until claim, or a CI/review/capture owner).
        if state.endswith(", unowned"):
            state = state[: -len(", unowned")]
        owner = _claim_owner_label(claim)
        if state.startswith("red:"):
            nxt = "await existing owner terminal"
    if advisory_red:
        state += " (advisory red: " + ",".join(advisory_red[:3]) + ")"
    if codex and codex["outdated"]:
        state += f" (codex P1 outdated x{codex['outdated']} — owner confirm)"
    return {
        "number": pr.get("number"),
        "head": head[:7],
        "head_full": head,
        "title": (pr.get("title") or "")[:60],
        "isDraft": bool(pr.get("isDraft")),
        "draft": bool(pr.get("isDraft")),
        "admitted": admitted,
        "state": state,
        "owner": owner,
        "next": nxt,
        "actions": actions,
    }


def format_line(row: dict) -> str:
    return f"#{row['number']} {row['head']} — {row['state']} — {row['owner']} — {row['next']}"


def fetch_open_prs(with_threads: bool = True) -> list[dict]:
    """Open PRs. Fresh shared cache for 300s; below the GraphQL floor, the last cache with a stale marker."""
    key = "pr-open-snapshot" if with_threads else "pr-open-snapshot-no-threads"
    try:
        prs, _live = gh_budget.serve(key, SNAPSHOT_TTL, lambda: _fetch_open_prs_live(with_threads))
    except LookupError as exc:
        raise RuntimeError(str(exc)) from None
    return prs


def _fetch_open_prs_live(with_threads: bool = True) -> list[dict]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        threads_future = pool.submit(fetch_review_threads) if with_threads else None
        # Every field is consumed by classification, ownership or review checks.
        cmd = [
            "gh", "pr", "list", "--repo", REPO, "--state", "open", "--limit", "200",
            "--json", "number,headRefOid,title,isDraft,labels,statusCheckRollup,files,body,comments",
        ]
        error = "unknown error"
        for attempt in range(3):
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
                if proc.returncode == 0:
                    prs = json.loads(proc.stdout)
                    if not isinstance(prs, list):
                        raise ValueError("expected a PR list")
                    break
                error = proc.stderr.strip()[:200] or f"exit {proc.returncode}"
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                error = str(exc)[:200]
            if attempt < 2:
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"UNAVAILABLE: gh pr list failed: {error}")
        threads = threads_future.result() if threads_future else None
    for pr in prs:
        pr["reviewThreads"] = None if threads is None else threads.get(pr.get("number"), [])
        pr["ci_reconciliation"] = _read_ci_reconciliation(pr)
    return prs


THREADS_QUERY = """query($owner:String!,$name:String!,$after:String){
  repository(owner:$owner,name:$name){pullRequests(states:OPEN,first:25,after:$after){
    pageInfo{hasNextPage endCursor}
    nodes{number reviewThreads(first:100){nodes{isResolved isOutdated
      comments(first:1){nodes{author{login} body}}}}}}}}"""


def fetch_review_threads() -> dict[int, list[dict]] | None:
    """Live review threads per open PR; None on failure (caller falls back to the label)."""
    owner, name = REPO.split("/")
    out: dict[int, list[dict]] = {}
    after = None
    try:
        while True:
            cmd = ["gh", "api", "graphql", "-f", f"query={THREADS_QUERY}",
                   "-F", f"owner={owner}", "-F", f"name={name}"]
            if after:
                cmd += ["-F", f"after={after}"]
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
            if proc.returncode != 0:
                return None
            page = json.loads(proc.stdout)["data"]["repository"]["pullRequests"]
            for node in page["nodes"]:
                rows = []
                for t in node["reviewThreads"]["nodes"]:
                    first = (t["comments"]["nodes"] or [{}])[0]
                    rows.append({"isResolved": t["isResolved"], "isOutdated": t["isOutdated"],
                                 "author": ((first.get("author") or {}).get("login") or ""),
                                 "body": first.get("body") or ""})
                out[node["number"]] = rows
            if not page["pageInfo"]["hasNextPage"]:
                return out
            after = page["pageInfo"]["endCursor"]
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Live open-PR snapshot")
    parser.add_argument("--text", action="store_true")
    parser.add_argument(
        "--sync-obligations", action="store_true",
        help="Compatibility no-op: routine PR tracking stays in GitHub and the PR report.",
    )
    args = parser.parse_args()
    if args.sync_obligations:
        # Historical callers must not recreate mirrors or resolve an explicit
        # incident, admission hold, or pending handoff with the same PR/kind.
        print(json.dumps({"obligations_synced": []}))
        return 0
    mop_owned_prs = fetch_mop_owned_prs()
    raw_prs = fetch_open_prs()
    claims = fetch_live_claims(raw_prs)
    holds = {}
    for pr in raw_prs:
        number = pr.get("number")
        if isinstance(number, int):
            hold = admission_hold(pr, {k: _existing_obligation(k, number) for k in ("pr_admission", "pr_merge_ask")})
            if hold:
                holds[number] = hold
    rows = sorted(
        (classify_pr(pr, mop_owned_prs, holds, claims) for pr in raw_prs),
        key=lambda r: -int(r["number"]),
    )
    stale = gh_budget.stale_info()
    if stale:
        for row in rows:
            row["stale"] = True
            row["stale_reason"] = stale
    if args.text:
        if stale:
            print(f"STALE: {stale}")
        print("\n".join(format_line(r) for r in rows))
        for r in rows:
            for a in r["actions"]:
                print(f"ACTION: {a}")
    else:
        print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
