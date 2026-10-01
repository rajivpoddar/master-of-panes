#!/usr/bin/env python3
"""Live open-PR snapshot shared by the hourly drive and the 3h heartbeat.

State comes only from live GitHub (`gh pr list` statusCheckRollup on the
current headRefOid). pm-ops rows and advisory pm-state labels are never used
to decide state. Read-only; takes no PR actions.

Output (JSON, or --text one line per PR):
  {number, head, title, draft, state, owner, next}
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = "heydonna-app/heydonna-app"
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
PM_12B_PASS_RE = re.compile(r"PM[ _-]?12b|PM_OPUS_REVIEW:\s*PASS", re.IGNORECASE)
PM_12B_BLOCK_RE = re.compile(r"PM_OPUS_REVIEW:\s*(BLOCK|REVISE|REQUEST_CHANGES)|PM[ _-]?12b[^\n]{0,40}\b(BLOCK|REVISE)\b", re.IGNORECASE)
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
        subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except Exception:
        pass


def _existing_obligation(kind: str, pr: int) -> dict | None:
    """Read-only lookup of the current open (kind, pr) row, or None.

    Used only to detect an agent's ownership claim before deciding whether to
    upsert. Never mutates; failures return None (fail open -> treat as
    unclaimed, same as today).
    """
    if not PM_OPS_DB.is_file():
        return None
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{PM_OPS_DB}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT owner, updated_at, suppress_until, evidence_json FROM obligations
            WHERE status='open' AND kind=? AND pr=?
            ORDER BY id DESC LIMIT 1
            """,
            (kind, pr),
        ).fetchone()
        con.close()
    except Exception:
        return None
    return dict(row) if row else None


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


def _latest_12b_verdict(pr: dict, head: str) -> str | None:
    """"PASS" / "BLOCK" from the LATEST head-bound PM 12b comment, else None.

    Comments are chronological (gh order); a later same-head PASS supersedes
    an earlier same-head BLOCK and vice versa (CTO REVISE 2026-10-01).
    """
    head_short = head[:7]
    verdict = None
    for c in pr.get("comments") or []:
        body = c.get("body") or ""
        if not head_short or head_short not in body:
            continue
        if PM_12B_BLOCK_RE.search(body):
            verdict = "BLOCK"
        elif PM_12B_PASS_RE.search(body):
            verdict = "PASS"
    return verdict


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


def _obligation_resolve(*, kind: str, pr: int, reason: str) -> None:
    if not PM_OPS_CLI.is_file():
        return
    try:
        subprocess.run(
            [
                sys.executable, str(PM_OPS_CLI), "obligation-resolve",
                "--kind", kind,
                "--target-type", "github_pr",
                "--target-id", str(pr),
                "--pr", str(pr),
                "--reason", reason,
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except Exception:
        pass  # best-effort: no matching open row is not an error here


def sync_pr_obligations(prs: list[dict], mop_owned_prs: set[int]) -> list[dict]:
    """Create/resolve pr_admission|pr_red_unowned|pr_merge_ask obligations.

    Fails open: any gh/pm-ops error must never raise; a skipped sync just
    means the Stop hook's next due-obligation pass is unchanged, never a
    block storm. Returns the list of {pr, kind, action} rows actually
    upserted, for callers that want to report what was created.
    """
    created: list[dict] = []
    seen_open_prs: set[int] = set()
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
                _obligation_resolve(kind=kind, pr=number, reason="owned_by_slot_or_blocked_or_draft")
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
                _obligation_resolve(kind="pr_admission", pr=number, reason="ci_exempt_merge_ask")
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
            _obligation_resolve(kind="pr_admission", pr=number, reason="ci-head_matches_current_head")

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
            _obligation_resolve(kind="pr_red_unowned", pr=number, reason="required_gates_not_red")

        if admitted and gates["green_ready"] and not gates["required_red"]:
            if _is_fresh_owner_claim(_existing_obligation("pr_merge_ask", number)):
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
            _obligation_resolve(kind="pr_merge_ask", pr=number, reason="not_green_or_not_admitted")

    _resolve_stale_pr_obligations(seen_open_prs)
    return created


def _resolve_stale_pr_obligations(seen_open_prs: set[int]) -> None:
    """Resolve pr_* obligations for PRs no longer open (merged/closed).

    Read-only DB lookup to find candidates; the actual resolve still goes
    through the canonical obligation-resolve CLI. Best-effort: any failure
    here is silently skipped, never raised.
    """
    db_path = Path(
        __import__("os").environ.get(
            "PM_OPS_DB",
            str(
                Path.home()
                / ".claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
            ),
        )
    )
    if not db_path.is_file():
        return
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in OBLIGATION_KINDS)
        rows = con.execute(
            f"""
            SELECT DISTINCT pr FROM obligations
            WHERE status='open' AND kind IN ({placeholders}) AND pr IS NOT NULL
            """,
            OBLIGATION_KINDS,
        ).fetchall()
        con.close()
    except Exception:
        return
    for row in rows:
        try:
            pr_number = int(row["pr"])
        except Exception:
            continue
        if pr_number in seen_open_prs:
            continue
        for kind in OBLIGATION_KINDS:
            _obligation_resolve(kind=kind, pr=pr_number, reason="pr_no_longer_open")


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


def classify_pr(pr: dict, mop_owned_prs: set[int] | None = None, holds: dict[int, str] | None = None) -> dict:
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
        if owner == "PM":
            state += ", unowned"
    elif gates_pending or (admitted and not gates_green and checks["pending"]):
        state, nxt = "CI running", "CI running" if stale_admission else "await terminal"
    elif gates_green:
        state = "CI+E2E green"
        if not admitted:
            nxt = "not admitted: no merge ask"
        else:
            # Admission already transferred ownership to PM; a lingering slot:* label is
            # stale pre-admission ownership, not live authority, so it must not suppress
            # the merge ask — emit the ask and a cleanup action for the stale label.
            nxt = "CTO merge ask"
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
    if advisory_red:
        state += " (advisory red: " + ",".join(advisory_red[:3]) + ")"
    if codex and codex["outdated"]:
        state += f" (codex P1 outdated x{codex['outdated']} — owner confirm)"
    return {
        "number": pr.get("number"),
        "head": head[:7],
        "head_full": head,
        "title": (pr.get("title") or "")[:60],
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
    proc = subprocess.run(
        [
            "gh", "pr", "list", "--repo", REPO, "--state", "open", "--limit", "100",
            "--json", "number,headRefOid,title,isDraft,labels,statusCheckRollup,files,body,comments",
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {proc.stderr.strip()[:200]}")
    prs = json.loads(proc.stdout)
    # --sync-obligations runs under the Stop hook's 8s budget and never reads review threads.
    threads = fetch_review_threads() if with_threads else None
    for pr in prs:
        pr["reviewThreads"] = None if threads is None else threads.get(pr.get("number"), [])
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
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
        help="Create/resolve pr_admission|pr_red_unowned|pr_merge_ask pm-ops "
             "obligations for open PRs (see sync_pr_obligations).",
    )
    args = parser.parse_args()
    mop_owned_prs = fetch_mop_owned_prs()
    raw_prs = fetch_open_prs(with_threads=not args.sync_obligations)
    if args.sync_obligations:
        created = sync_pr_obligations(raw_prs, mop_owned_prs)
        print(json.dumps({"obligations_synced": created}, indent=2))
        return 0
    holds = {}
    for pr in raw_prs:
        number = pr.get("number")
        if isinstance(number, int):
            hold = admission_hold(pr, {k: _existing_obligation(k, number) for k in ("pr_admission", "pr_merge_ask")})
            if hold:
                holds[number] = hold
    rows = sorted(
        (classify_pr(pr, mop_owned_prs, holds) for pr in raw_prs),
        key=lambda r: -int(r["number"]),
    )
    if args.text:
        print("\n".join(format_line(r) for r in rows))
        for r in rows:
            for a in r["actions"]:
                print(f"ACTION: {a}")
    else:
        print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
