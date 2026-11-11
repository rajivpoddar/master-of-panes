#!/usr/bin/env python3
"""Deterministic next-slot-work selector: priority first.

Rajiv, C0ALZJHGE49 thread 1793195119.674919, ts 1793202340.562609:
"academy, multi doc upload, speaker mapping sidebar are p2. how do we ensure
this priority is followed on slot assignments".

Ordering (hard rule):
  1. priority label: P0 < P1 < P2 < P3 < unknown
  2. ties: rework before repro before new_issue
  3. ties: oldest (createdAt) first, then issue number

A lower-priority pick is allowed only when no eligible higher-priority
candidate exists; the pick then carries `priority_reason` for the assignment
receipt. `--check <issue>` refuses (exit 3, PRIORITY_INVERSION) a PM pick that
skips a higher-priority eligible candidate.

Candidates (live GitHub, read-only):
  new_issue = open issue labelled status:todo (Ready Pool)
  rework    = open issue labelled status:qa-rework / pm-state:qa-failed-rework /
              pm-state:blocked-rework
  plus optional PM-supplied rows via --extra-json
    ([{"issue": N, "class": "rework|repro|new_issue", "priority": "P1", "pr": M}]).
Excluded: already on a slot (status:in-progress or slot:N), pm-blocked:*
(except P0 or dependency gates), status:blocked/deferred/backlog (except dependency-blocked),
pm-nonclaimable:tracking, umbrella, and Ready Pool claimable_slot_type values
that are not slot work.

The explicit Ready Pool header `merge_after: #N[, #M]` records merge ordering,
not an assignment gate (CTO C0ALZJHGE49 ts 1794385105.483169). `--check`
reports live open referenced issues in `merge_after_open`; malformed declarations
fail closed. Do not infer merge ordering from prose. Canonical blockers and native
blocked-by dependencies retain their existing gating semantics.

Dependency-blocked candidates of all priorities are listed in the pool. Their
referenced PR must be merged, or a referenced issue must have exactly one
authoritatively linked implementation PR that is merged. Issue closure, deploy
and verification are not dependency requirements (Rajiv 1793896514.652319).
Missing/ambiguous linkage stays gated. Other P0 gates remain gated. Offline
mode disables dependency lookups and leaves dependencies gated.

Dependency hell (Rajiv DM D0AMF0XE6TS thread 1793816247.442119, ts 1793855417.837589):
every dependency-gated issue's open blockers are followed transitively (batched GraphQL,
depth <= 6, deduped). `dependency_hell` flags CYCLE, DEEP (>= 3 open blockers deep or
truncated), STALE_ROOT (root has no assignee/slot:N/linked open PR, or idle >= 3d),
EXTERNAL_ROOT (pm-blocked:external/product/cto or partner/decision wording in the root's
ready-pool gate/wake/blockers), DEAD_ROOT (root closed without satisfying the condition) and
AGED (gated >= 7d: pm-blocked:dependency LabeledEvent time, else updatedAt as a lower bound).
`dependency_hotspots` lists each open root gating >= 3 pool issues once.
Dependency warnings stay in this selector's output. `--sync-obligations` is a
compatibility no-op; explicit follow-ups use the ordinary pm-ops writer.

Usage:
  ready_pool_select.py                 # ranked JSON, pick first
  ready_pool_select.py --sync-obligations   # compatibility flag; no ledger writes
  ready_pool_select.py --rework-only   # WIP cap hit: existing-PR work only
  ready_pool_select.py --check 11462   # validate a PM pick before mop-assign-slot

Exit codes: 0 ok/pick, 1 no pick, 3 PRIORITY_INVERSION,
4 not_eligible/dependency open/LOOKUP_FAILED/LOOKUP_TIMEOUT, 5 other gate,
6 PR_CAP. GitHub lookups use bounded timeouts and up to two retries on timeout.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import importlib.util
import json
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any
import os
import sys

_SCRIPTS = Path.home() / ".claude" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import gh_budget

REPO = "Scribie/heydonna-app"
POOL_TTL = gh_budget.DEFAULT_TTL
ASSOCIATED_PRS = ('closedByPullRequestsReferences(first:2,includeClosedPrs:true) '
                  '{ nodes { number state mergedAt } pageInfo { hasNextPage } }')
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
UNKNOWN_RANK = 9
CLASS_RANK = {"rework": 0, "repro": 1, "new_issue": 2}
REWORK_LABELS = {"status:qa-rework", "pm-state:qa-failed-rework", "pm-state:blocked-rework"}
# Shared snapshot for every caller (heartbeat, hourly, --check inversion scan).
# 60–120s: long enough that a collector timeout cannot stampede GitHub, short
# enough that a full ranking is not a five-minute-old pool. The issue passed to
# --check does not use this snapshot for its own dependency or merged-PR facts.
CANDIDATE_LABELS = ("status:todo", *tuple(sorted(REWORK_LABELS)), "pm-blocked:dependency")
SNAPSHOT_TTL = 90
_FRESH = "__fresh_issues__"


def pool_cache_name(kind: str) -> str:
    """Disk key is repo + the exact label set, so a different filter cannot reuse it."""
    return f"{kind}:{REPO}:{','.join(CANDIDATE_LABELS)}"
EXCLUDE_LABELS = {
    "status:in-progress", "status:blocked", "status:deferred", "status:backlog",
    "status:done", "pm-nonclaimable:tracking", "umbrella",
}
NON_SLOT_TYPES = {
    "none", "no", "no-slot", "no_slot", "not-claimable", "not_claimable", "unclaimable",
    "pm", "pm-direct", "pm_direct", "direct-main", "infra", "infrastructure", "docs",
    "docs-only", "documentation", "documentation-only", "rajiv", "external",
}
READY_POOL_RE = re.compile(r"<!--\s*ready-pool:\s*(.*?)-->", re.DOTALL | re.IGNORECASE)


@lru_cache(maxsize=1)
def contract_policy():
    """Use the promoter's pure contract validator; never invoke its writers."""
    path = Path(__file__).with_name("backlog-triage.py")
    spec = importlib.util.spec_from_file_location("ready_pool_contract_policy", path)
    if spec is None or spec.loader is None:
        raise ValueError("canonical Ready Pool contract policy unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CONTRACT_MEMO: dict[str, list[str]] = {}


def contract_reasons(issue: dict[str, Any]) -> list[str]:
    # Pure over the issue content, but each call runs the ICL validator; one run
    # used to re-validate every candidate ~8 times. Memoize on the content, and
    # reuse a stamp carried in the shared issue snapshot so the next process
    # does not pay the scan again.
    # Annotation keys (_dependency_comments, _implementation_merged, stamps) are not
    # inputs to the contract. Including them busted the memo every time a prefetch
    # attached comments and made every --check chunk re-scan the body.
    key = json.dumps(
        {k: v for k, v in issue.items() if not str(k).startswith("_")},
        sort_keys=True, default=str)
    if key not in _CONTRACT_MEMO:
        stamped = issue.get("_contract_reasons")
        if issue.get("_contract_key") == key and isinstance(stamped, list):
            _CONTRACT_MEMO[key] = list(stamped)
        else:
            normalized = {**issue, "labels": [{"name": name} for name in _labels(issue)]}
            _CONTRACT_MEMO[key] = contract_policy().ready_pool_contract_reasons(
                normalized, set(), allow_backlog="status:todo" not in _labels(issue),
                allow_dependencies=True,
            )
            issue["_contract_key"] = key
            issue["_contract_reasons"] = list(_CONTRACT_MEMO[key])
            sleep(0)  # let an in-flight dependency-hell fetch take the GIL
    return list(_CONTRACT_MEMO[key])


def _labels(issue: dict[str, Any]) -> set[str]:
    out = set()
    for item in issue.get("labels") or []:
        out.add(item.get("name", "") if isinstance(item, dict) else str(item))
    return out


def ready_pool_value(body: str, field: str) -> str:
    return contract_policy().ready_pool_metadata({"body": body or ""}).get(field, "")


def merge_after_refs(body: str) -> list[int]:
    """Parse only the last explicit Ready Pool header, without normalizing malformed input."""
    blocks = READY_POOL_RE.findall(body or "")
    if not blocks:
        return []
    block = blocks[-1]
    if block.strip().startswith("{"):
        try:
            data = json.loads(block)
        except ValueError:
            if re.search(r'"merge_after"', block, re.I):
                raise ValueError("malformed merge_after header") from None
            return []
        if not isinstance(data, dict) or "merge_after" not in data:
            return []
        lines = [data["merge_after"]] if isinstance(data["merge_after"], str) else [""]
    else:
        lines = re.findall(r"(?im)^[ \t]*merge_after\s*[:=][ \t]*([^\r\n]*)", block)
    if not lines:
        return []
    if len(lines) != 1 or not re.fullmatch(r"\s*#[1-9]\d*(?:\s*,\s*#[1-9]\d*)*\s*", lines[0]):
        raise ValueError("malformed merge_after: expected #N[, #M]")
    return list(dict.fromkeys(int(n) for n in re.findall(r"#(\d+)", lines[0])))


def merge_after_receipt(res: dict, issue: dict | None, offline: bool = False) -> dict:
    if issue is None:
        return res
    try:
        refs = merge_after_refs(issue.get("body") or "")
    except ValueError as exc:
        return {"status": "not_eligible", "issue": issue["number"], "reason": str(exc)}
    if refs and offline:
        return {"status": "LOOKUP_FAILED", "issue": issue["number"], "reason": "merge_after live lookup unavailable"}
    nodes = graphql_nodes(refs) if refs else {}
    if any(not isinstance(nodes.get(n), dict) or "mergedAt" in nodes[n]
           or nodes[n].get("state") not in {"OPEN", "CLOSED"} for n in refs):
        return {"status": "LOOKUP_FAILED", "issue": issue["number"], "reason": "merge_after issue lookup failed"}
    res["merge_after_open"] = [n for n in refs if nodes[n]["state"] == "OPEN"]
    return res


def dispatch_content_refusals(issue: dict[str, Any]) -> list[str]:
    """Screen authoritative Ready Pool fields, never historical prose or comments."""
    body = issue.get("body") or ""
    reasons = []
    claimable = ready_pool_value(body, "claimable_slot_type").lower()
    if claimable and claimable not in {"dev", "sync"}:
        reasons.append("claimable_slot_type_not_dev")
    for field in ("owner", "lane"):
        value = ready_pool_value(body, field)
        if re.match(r"(?i)^\s*(?:pm-hotfix|pm|cto)(?![a-z0-9])", value):
            reasons.append(f"{field}_reserved_lane")
    disposition = ready_pool_value(body, "disposition")
    if re.search(r"(?i)(?<![a-z0-9])(?:blocked|parked|deferred)[a-z0-9_]*", disposition):
        reasons.append("disposition_not_dispatchable")
    if re.search(r"(?i)\bneeds[_ -]investigation\b", disposition) and claimable not in {"dev", "sync"}:
        reasons.append("non_dev_needs_investigation")
    return reasons


def priority_rank(labels: set[str], body: str = "") -> int:
    """Label wins; Ready Pool `priority:` is the fallback; unknown sorts last."""
    found = sorted(PRIORITY_RANK[name] for name in labels if name in PRIORITY_RANK)
    if found:
        return found[0]
    meta = ready_pool_value(body, "priority").upper()
    return PRIORITY_RANK.get(meta, UNKNOWN_RANK)


def prio_name(rank: int) -> str:
    return f"P{rank}" if rank in PRIORITY_RANK.values() else "P?"


SUB_TO_BLOCKER = {"sub:waiting-on-issue": "pm-blocked:dependency", "sub:pm-gate": "pm-blocked:pm-gate",
                  "sub:external": "pm-blocked:external", "sub:decision": "pm-blocked:decision",
                  "sub:infra": "pm-blocked:infra"}


def declared_issue_holds(issue: dict[str, Any]) -> set[str]:
    raw = ready_pool_value(issue.get("body") or "", "holds")
    holds = set()
    for entry in re.split(r"[;,]", raw):
        value = entry.strip().lstrip("- ")
        if not value or value.lower() in {"none", "n/a", "-"}:
            continue
        sub = value if value.startswith("sub:") else "sub:" + value
        holds.add(value if value.startswith("pm-blocked:") else SUB_TO_BLOCKER.get(sub, "pm-blocked:" + value.removeprefix("sub:")))
    return holds


def issue_holds(issue: dict[str, Any]) -> set[str]:
    labels = _labels(issue)
    return (declared_issue_holds(issue)
            | {SUB_TO_BLOCKER[l] for l in labels & SUB_TO_BLOCKER.keys()}
            | {l for l in labels if l.startswith("pm-blocked:")})


def classify(issue: dict[str, Any], requested_class: str | None = None, *, light: bool = False) -> str | None:
    """Return the selection class; light mode is only a non-authoritative prefilter."""
    if not light and (not isinstance(issue.get("body"), str) or not issue["body"].strip() or dispatch_content_refusals(issue)):
        return None
    if not light:
        try:
            merge_after_refs(issue.get("body") or "")
        except ValueError:
            return None
    labels = _labels(issue)
    holds = issue_holds(issue)
    status = {l for l in labels if l.startswith("status:")}
    subs = {l for l in labels if l.startswith("sub:")}
    declared_holds = ready_pool_value(issue.get("body") or "", "holds")
    declared = declared_issue_holds(issue)
    mapped_subs = {SUB_TO_BLOCKER[sub] for sub in subs if sub in SUB_TO_BLOCKER}
    if declared_holds and mapped_subs and not mapped_subs <= declared:
        return None
    if issue.get("state", "OPEN").upper() != "OPEN" or len(status) != 1 or len(subs) > 1:
        return None
    current_status = next(iter(status))
    if subs and ((current_status == "status:todo" and subs != {"sub:waiting-on-issue"})
                 or (current_status == "status:blocked" and not subs <= {"sub:pm-gate", "sub:external", "sub:decision", "sub:infra"})
                 or (current_status == "status:backlog" and not subs <= {"sub:needs-contract", "sub:pm-owned"})
                 or current_status not in {"status:todo", "status:blocked", "status:backlog"}):
        return None
    if current_status == "status:blocked" and holds == {"pm-blocked:dependency"}:
        return None
    if current_status == "status:todo" and any(h != "pm-blocked:dependency" for h in holds):
        return None
    excluded = EXCLUDE_LABELS - ({"status:blocked"} if holds == {"pm-blocked:dependency"} else set())
    if labels & excluded or any(l.startswith("slot:") for l in labels):
        return None
    if holds and holds != {"pm-blocked:dependency"} and "P0" not in labels:
        return None
    if ready_pool_value(issue.get("body") or "", "claimable_slot_type").lower() in NON_SLOT_TYPES:
        return None
    if requested_class in {"rework", "repro"}:
        return requested_class
    if labels & REWORK_LABELS:
        return "rework"
    if current_status == "status:todo":
        return "new_issue"
    return None


def blocking_labels(labels: set[str], issue: dict[str, Any] | None = None) -> list[str]:
    return sorted(issue_holds(issue) if issue is not None else {l for l in labels if l.startswith("pm-blocked:")})


def is_gated(c: dict[str, Any]) -> bool:
    return bool(c.get("blocking_labels")) and not c.get("dependency_unblocked", False)


_DEP_RE = re.compile(r"(?i)(?:(pr|pull request|issue)\s+)?#(\d+)(?:\s+(merge|merges|merged|merging|closed|complete|completed))?")
_WAKE_REF_RE = re.compile(r"#(\d+)")
_WAKE_KINDS = (
    ('complete', re.compile(r"(?i)\b(complet\w*)\b")),
    ('merge', re.compile(r"(?i)\b(merg\w*|land\w*|ship\w*)\b")),
    ('closed', re.compile(r"(?i)\b(clos\w*|done|finished)\b")),
)


def _kind_from_condition(condition: str | None, prefix: str | None) -> str:
    condition = (condition or '').lower()
    if condition in ('merge', 'merges', 'merged', 'merging') or (not condition and (prefix or '').lower() in ('pr', 'pull request')):
        return 'merge'
    if condition in ('complete', 'completed'):
        return 'complete'
    if condition == 'closed':
        return 'closed'
    return 'default'


def parse_dependencies(blockers: str, wake: str) -> list[tuple[int, str]] | None:
    """`blockers` is a structured field and is matched whole; `wake_condition` is prose and is scanned for references.

    A malformed `blockers` value returns None so the issue stays visibly unroutable. Prose in
    `wake_condition` must never make a well-formed blockers list unparseable, which is what
    applying `fullmatch` to both fields did.
    """
    refs: dict[int, str] = {}
    text = (blockers or '').strip()
    if text.lower() not in ('', 'none', 'null', 'n/a', '[]'):
        # A parent/umbrella ref is context, not a dependency: it stays open by construction.
        text = re.sub(r'(?i)\b(?:part\s+of|parent|umbrella)\s*#\d+', '', text)
        text = re.sub(r'(?i)^blocked\s+on\s+', '', text).strip('[] ')
        loose = False
        for part in re.split(r'(?i)\s+and\s+|\s*,\s*|\s*;\s*', text):
            part = part.strip()
            if not part:
                continue
            match = _DEP_RE.fullmatch(part)
            if match:
                prefix, number, condition = match.groups()
                kind = _kind_from_condition(condition, prefix)
                number = int(number)
            else:
                loose = True
                found = _WAKE_REF_RE.findall(part)
                if not found:
                    continue
                kind = _kind_from_condition(None, None)
                for candidate, verb_re in _WAKE_KINDS:
                    if verb_re.search(part):
                        kind = candidate
                        break
                for raw in found:
                    refs.setdefault(int(raw), kind)
                continue
            old = refs.get(number)
            if old and old != kind and old not in ('closed', 'default') and kind not in ('closed', 'default'):
                return None
            if old != 'merge' and (kind not in ('closed', 'default') or not old or old == 'default'):
                refs[number] = kind
        if loose and not refs:
            # Prose naming no reference at all: genuinely unroutable, keep the issue visibly gated.
            return None
        return list(refs.items())

    # No structured blockers: read the prose wake condition for the references it names.
    wake = re.sub(r'(?i)\b(?:part\s+of|parent|umbrella)\s*:?[ \t]*#\d+', '', wake or '')
    for clause in re.split(r"(?i)\bthen\b|\s+and\s+|\s*,\s*|\s*;\s*", wake.strip()):
        for match in _WAKE_REF_RE.finditer(clause):
            kind = 'default'
            for candidate, verb_re in _WAKE_KINDS:
                if verb_re.search(clause):
                    kind = candidate
                    break
            refs.setdefault(int(match.group(1)), kind)
    return list(refs.items())


def prose_dependencies(text: str) -> list[tuple[int, str]]:
    """Extract only explicitly stated dependencies, not parent or incidental references."""
    text = re.sub(r'(?i)\b(?:part\s+of|parent|umbrella)\s*:?[ \t]*#\d+', '', text or '')
    refs = {}
    for line in text.splitlines():
        match = re.search(r'(?i)\b(?:blocked\s+(?:on|by)|depends?\s+on|dependencies?\s*:|blockers?\s*:|after)\s+([^\n.;]+)', line)
        if not match:
            continue
        clause = match.group(1)
        if (line[match.start():].lower().startswith('after')
                and re.search(r'(?i)\bmerge\s*$', line[:match.start()])):
            continue  # merge-order prose is never an assignment dependency
        if line[match.start():].lower().startswith('after') and not re.search(r'(?i)\b(?:merg\w*|clos\w*|land\w*)\b', clause):
            continue
        clause = re.sub(r'#(\d+)\s*[–—-]\s*#(\d+)', lambda m: ' '.join('#'+str(n) for n in range(int(m[1]), min(int(m[2]), int(m[1])+100)+1)), clause)
        for num in _WAKE_REF_RE.findall(clause):
            refs[int(num)] = 'closed'
    return list(refs.items())


_TOKEN_LOCK = threading.Lock()
_GITHUB_TOKEN: str | None = None


def _github_token() -> str:
    """Read gh's credential once. Callers must not log the return value."""
    global _GITHUB_TOKEN
    with _TOKEN_LOCK:
        if _GITHUB_TOKEN:
            return _GITHUB_TOKEN
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise OSError("gh auth token unavailable")
        _GITHUB_TOKEN = token
        return token


def graphql_http(query: str, variables: dict | None = None, timeout: int = 60,
                 deadline: float | None = None) -> dict:
    """One GraphQL round trip without a gh process per page. Falls back to gh on transport errors."""
    remaining = deadline - monotonic() if deadline is not None else timeout
    if remaining <= 0:
        raise TimeoutError("candidate snapshot deadline reached")
    body = json.dumps({"query": query, **({"variables": variables} if variables else {})}).encode()
    try:
        req = urllib.request.Request(
            "https://api.github.com/graphql",
            data=body,
            headers={
                "Authorization": "Bearer " + _github_token(),
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
                "User-Agent": "ready-pool-select",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=min(timeout, remaining)) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        cmd = ["gh", "api", "graphql", "-f", "query=" + query]
        for key, value in (variables or {}).items():
            if value is not None:
                cmd += ["-f", f"{key}={value}"]
        return json.loads(run_lookup(cmd, timeout=timeout, deadline=deadline).stdout)


def _paginate_issues(label: str, node_fields: str, deadline: float, page_size: int,
                     timeout: int, transform, *, order_by: bool, on_row=None) -> dict[int, dict]:
    """One label's cursor walk. Callers run a label per thread; each returns its own dict."""
    cursor, seen, expected, rows = None, set(), None, {}
    order = ",orderBy:{field:CREATED_AT,direction:ASC}" if order_by else ""
    while True:
        if deadline - monotonic() <= 0:
            raise TimeoutError("candidate snapshot deadline reached")
        query = (
            'query($cursor:String){repository(owner:"Scribie",name:"heydonna-app"){'
            f'issues(first:{page_size},after:$cursor,states:OPEN,labels:[{json.dumps(label)}]{order})'
            f'{{totalCount nodes{{{node_fields}}} pageInfo{{hasNextPage endCursor}}}}}}}}'
        )
        payload = graphql_http(query, {"cursor": cursor}, timeout=timeout, deadline=deadline)
        if payload.get("errors"):
            raise ValueError("candidate lookup failed")
        page = payload["data"]["repository"]["issues"]
        if expected is not None and expected != page["totalCount"]:
            raise ValueError("candidate population changed during pagination")
        expected = page["totalCount"]
        for raw in page["nodes"]:
            number = int(raw["number"])
            if number in seen:
                raise ValueError("duplicate candidate page")
            seen.add(number)
            rows[number] = transform(raw)
            if on_row is not None:
                on_row(rows[number])
        if not page["pageInfo"]["hasNextPage"]:
            if len(seen) != expected:
                raise ValueError("incomplete candidate pagination")
            return rows
        cursor = page["pageInfo"].get("endCursor")
        if not cursor:
            raise ValueError("missing candidate cursor")


def _load_label_union(timeout: int, node_fields: str, transform, *, page_size: int,
                      page_timeout: int, order_by: bool, on_row=None) -> list[dict[str, Any]]:
    deadline = monotonic() + timeout

    def one(label: str) -> dict[int, dict]:
        last = None
        for _ in range(2):
            try:
                return _paginate_issues(
                    label, node_fields, deadline, page_size, page_timeout, transform,
                    order_by=order_by, on_row=on_row)
            except ValueError as exc:
                last = exc
                if "changed during pagination" not in str(exc) and "duplicate candidate page" not in str(exc):
                    raise
        raise last

    with ThreadPoolExecutor(max_workers=len(CANDIDATE_LABELS)) as pool:
        parts = list(pool.map(one, CANDIDATE_LABELS))
    merged: dict[int, dict] = {}
    for part in parts:
        merged.update(part)
    return list(merged.values())


def run_lookup(cmd: list[str], timeout: int = 60, deadline: float | None = None) -> subprocess.CompletedProcess:
    for attempt in range(3):
        remaining = deadline - monotonic() if deadline is not None else timeout
        if remaining <= 0:
            raise TimeoutError('candidate snapshot deadline reached')
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=min(timeout, remaining), check=True)
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise
            delay = 2 ** attempt
            if deadline is not None and monotonic() + delay >= deadline:
                raise TimeoutError('candidate snapshot deadline reached')
            sleep(delay)
        except (OSError, subprocess.SubprocessError):
            if attempt:
                raise
    raise RuntimeError('unreachable')


def dependency_satisfaction(node: dict | None) -> tuple[bool, str]:
    """Merged implementation PR only; missing/ambiguous linkage stays unknown.

    GitHub's closing-PR relation is authoritative. Body mentions and historical
    partial PRs cannot invent that relation or disambiguate multiple linked PRs.
    """
    if not isinstance(node, dict):
        return False, "LOOKUP_FAILED"
    if "mergedAt" in node:
        merged, state = node.get("mergedAt"), node.get("state")
        if state not in {"OPEN", "CLOSED", "MERGED"} or (merged is not None and (not isinstance(merged, str) or _ts(merged) is None)):
            return False, "LOOKUP_FAILED"
        return bool(merged) and state == "MERGED", state
    if node.get("state") not in {"OPEN", "CLOSED"}:
        return False, "LOOKUP_FAILED"
    links = node.get("closedByPullRequestsReferences")
    if not isinstance(links, dict) or not isinstance(links.get("nodes"), list):
        return False, "LOOKUP_FAILED"
    if not isinstance(links.get("pageInfo"), dict):
        return False, "LOOKUP_FAILED"
    prs, more = links["nodes"], links["pageInfo"].get("hasNextPage")
    if more is not False:
        return False, "AMBIGUOUS_ASSOCIATED_PR" if more is True else "LOOKUP_FAILED"
    if not prs:
        return False, "NO_ASSOCIATED_PR"
    if len(prs) != 1:
        return False, "AMBIGUOUS_ASSOCIATED_PR"
    ok, state = dependency_satisfaction(prs[0])
    return ok, f"ASSOCIATED_PR_{state}"


_NATIVE_ERRORS = (OSError, subprocess.SubprocessError, TimeoutError, ValueError, TypeError)


def _native_fetch(number: int) -> list[tuple[int, str]]:
    cmd = ['gh', 'api', '--paginate', '--slurp',
           f'repos/{REPO}/issues/{number}/dependencies/blocked_by?per_page=100']
    pages = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True).stdout)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError('invalid native dependency response')
    refs = []
    for page in pages:
        for issue in page:
            if (not isinstance(issue, dict) or issue.get('repository_url', '').lower()
                    != f'https://api.github.com/repos/{REPO}'.lower()
                    or not isinstance(issue.get('number'), int)):
                raise ValueError('unsupported native dependency')
            refs.append((issue['number'], 'default'))
    return refs


def native_dependencies(number: int, cache: dict) -> list[tuple[int, str]] | None:
    """Read all native blockers once per run; stop requests after an API failure."""
    key = ('native', number)
    if key in cache:
        return cache[key]
    if cache.get(('native', 'unavailable')):
        return None
    try:
        refs = _native_fetch(number)
        cache[key] = refs
        return refs
    except _NATIVE_ERRORS:
        cache[('native', 'unavailable')] = True
        cache[key] = None
        print('native_deps=unavailable', file=sys.stderr)
        return None


def _blocked_by_refs(connection: dict | None) -> list[tuple[int, str]]:
    """Same tuples REST `/dependencies/blocked_by` produced: (number, 'default'), this repo only."""
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        raise ValueError("invalid native dependency response")
    page = connection.get("pageInfo")
    if not isinstance(page, dict) or page.get("hasNextPage") is not False:
        raise ValueError("incomplete native dependency page")
    refs = []
    for item in connection["nodes"]:
        repo = ((item or {}).get("repository") or {}).get("nameWithOwner") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("number"), int) or not isinstance(repo, str):
            raise ValueError("unsupported native dependency")
        if repo.lower() != REPO.lower():
            raise ValueError("unsupported native dependency")
        refs.append((item["number"], "default"))
    return refs


def _assemble_blocked_by(number: int, connection: dict) -> dict:
    """Follow blockedBy cursors so a page cap cannot drop a blocker."""
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        raise ValueError("invalid native dependency response")
    nodes = list(connection["nodes"])
    page = connection.get("pageInfo") if isinstance(connection.get("pageInfo"), dict) else {}
    hops = 0
    while page.get("hasNextPage") is True:
        hops += 1
        cursor = page.get("endCursor")
        if hops > 5 or not cursor:
            raise ValueError("incomplete native dependency page")
        query = (
            '{repository(owner:"Scribie",name:"heydonna-app"){issue(number:%d){'
            'blockedBy(first:100,after:%s){pageInfo{hasNextPage endCursor}'
            'nodes{number repository{nameWithOwner}}}}}}'
        ) % (number, json.dumps(cursor))
        payload = graphql_http(query, timeout=60)
        issue = ((payload.get("data") or {}).get("repository") or {}).get("issue")
        nxt = issue.get("blockedBy") if isinstance(issue, dict) else None
        if not isinstance(nxt, dict) or not isinstance(nxt.get("nodes"), list):
            raise ValueError("invalid native dependency response")
        nodes.extend(nxt["nodes"])
        page = nxt.get("pageInfo") if isinstance(nxt.get("pageInfo"), dict) else {}
    if page.get("hasNextPage") is not False:
        raise ValueError("incomplete native dependency page")
    return {"nodes": nodes, "pageInfo": {"hasNextPage": False}}


def _native_refs_blob(payload: dict) -> dict:
    if isinstance(payload.get("refs"), dict):
        return payload["refs"]
    return {key: value for key, value in payload.items() if key not in {"refs", "states"}}


def _apply_cached_states(cache: dict, states: dict) -> None:
    for raw, value in states.items():
        if not isinstance(raw, str):
            continue
        if raw.startswith("state|"):
            cache[("state", int(raw.split("|", 1)[1]))] = value
            continue
        number_s, sep, kind = raw.partition("|")
        if sep:
            cache[(int(number_s), kind)] = tuple(value) if isinstance(value, list) else value


def _native_disk_load(cache: dict) -> None:
    if os.environ.get("GH_BUDGET_DISABLE") == "1":
        return
    payload, age = gh_budget.peek(pool_cache_name("ready-pool-native"))
    if not isinstance(payload, dict) or age is None or age > SNAPSHOT_TTL:
        return
    for key, refs in _native_refs_blob(payload).items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        if ("native", number) in cache or not isinstance(refs, list):
            continue
        cache[("native", number)] = [tuple(item) for item in refs]
    if isinstance(payload.get("states"), dict):
        _apply_cached_states(cache, payload["states"])


def _native_disk_store(cache: dict) -> None:
    if os.environ.get("GH_BUDGET_DISABLE") == "1" or cache.get(("native", "unavailable")):
        return
    refs, states = {}, {}
    payload, age = gh_budget.peek(pool_cache_name("ready-pool-native"))
    if isinstance(payload, dict) and age is not None and age <= SNAPSHOT_TTL:
        refs.update(_native_refs_blob(payload))
        if isinstance(payload.get("states"), dict):
            states.update(payload["states"])
    for key, value in cache.items():
        if isinstance(key, tuple) and len(key) == 2 and key[0] == "native" and isinstance(key[1], int) and isinstance(value, list):
            refs[str(key[1])] = value
        elif isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int) and isinstance(value, tuple):
            states[f"{key[0]}|{key[1]}"] = list(value)
        elif isinstance(key, tuple) and key[0] == "state" and isinstance(key[1], int):
            states[f"state|{key[1]}"] = value
    if not refs and not states:
        return
    try:
        gh_budget.store(pool_cache_name("ready-pool-native"), {"refs": refs, "states": states})
    except OSError:
        pass


def _prefetch_native_batch(batch: list[int]) -> dict[int, list[tuple[int, str]] | None]:
    aliases = " ".join(
        f'i{n}: issue(number:{n}){{blockedBy(first:100){{pageInfo{{hasNextPage endCursor}}'
        f'nodes{{number repository{{nameWithOwner}}}}}}}}'
        for n in batch
    )
    query = '{ repository(owner:"Scribie",name:"heydonna-app") { ' + aliases + " } }"
    payload = graphql_http(query, timeout=60)
    repo = (payload.get("data") or {}).get("repository")
    if not isinstance(repo, dict):
        raise ValueError("native batch failed")
    out: dict[int, list[tuple[int, str]] | None] = {}
    for number in batch:
        raw = repo.get(f"i{number}")
        if not isinstance(raw, dict) or not isinstance(raw.get("blockedBy"), dict):
            out[number] = None
            continue
        try:
            out[number] = _blocked_by_refs(_assemble_blocked_by(number, raw["blockedBy"]))
        except ValueError:
            out[number] = None
    return out


def _store_native_satisfaction(cache: dict) -> None:
    """One batched state lookup for every native blocker. Per-candidate lookups were a round trip each."""
    pending = []
    for key, refs in cache.items():
        if not (isinstance(key, tuple) and len(key) == 2 and key[0] == "native" and isinstance(refs, list)):
            continue
        for number, kind in refs:
            if (number, kind) not in cache:
                pending.append(number)
    pending = list(dict.fromkeys(pending))
    if not pending:
        return
    try:
        nodes = graphql_nodes(pending)
    except TimeoutError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        nodes = {number: None for number in pending}
    for number in pending:
        node = nodes.get(number) if isinstance(nodes, dict) else None
        if isinstance(node, dict):
            cache[("state", number)] = node.get("state")
        try:
            cache[(number, "default")] = dependency_satisfaction(node if isinstance(node, dict) else None)
        except (ValueError, TypeError):
            cache[(number, "default")] = (False, "LOOKUP_FAILED")


def prefetch_native(numbers: list[int], cache: dict, workers: int = 8, *, persist: bool = True) -> None:
    """Fill native blockers with batched GraphQL `blockedBy` (same cache entries as the REST path).

    One REST call per candidate was ~1.2s. `blockedBy` returns the same (number, repo) set;
    a failed number still latches `unavailable` and stores None, and a cross-repo blocker
    is still an error rather than a silently dropped dependency.
    """
    _native_disk_load(cache)
    if cache.get(("native", "unavailable")):
        return
    todo = [n for n in dict.fromkeys(numbers) if ("native", n) not in cache]
    failed = False
    if todo:
        batches = [todo[start:start + 50] for start in range(0, len(todo), 50)]
        try:
            with ThreadPoolExecutor(max_workers=min(workers, len(batches), 4)) as pool:
                parts = list(pool.map(_prefetch_native_batch, batches))
        except _NATIVE_ERRORS:
            for number in todo:
                cache[("native", number)] = None
            cache[("native", "unavailable")] = True
            print("native_deps=unavailable", file=sys.stderr)
            return
        for part in parts:
            for number, refs in part.items():
                cache[("native", number)] = refs
                failed = failed or refs is None
    if failed:
        cache[("native", "unavailable")] = True
        print("native_deps=unavailable", file=sys.stderr)
        return
    _store_native_satisfaction(cache)
    if persist:
        _native_disk_store(cache)


def native_candidates(issues: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[int]:
    """Issues evaluate_dependency() can run on. Contract rejects are included so this
    stays off the contract-validator critical path; rank still skips them before evaluating."""
    out = []
    for issue in issues:
        cls = classify(issue)
        if not cls:
            continue
        if cls == "new_issue" and issue.get("_implementation_merged"):
            continue
        out.append(int(issue["number"]))
    return out + [int(r["issue"]) for r in extra or []]


def evaluate_dependency(c: dict[str, Any], cache: dict, offline: bool = False) -> None:
    issue_body = c.pop('issue_body', '')
    comment_text = c.pop('comment_text', '')
    # Canonical blockers are authoritative even when an advisory label is missing.
    if str(c.get("blockers") or "").strip().lower() not in {"", "none", "null", "n/a", "[]"}:
        c["blocking_labels"] = sorted(set(c.get("blocking_labels") or []) | {"pm-blocked:dependency"})
    fresh = (not offline) and int(c.get("issue") or -1) in set(cache.get(_FRESH) or ())
    fresh_done = ("fresh_done", int(c.get("issue") or -1))
    if fresh and not cache.get(fresh_done):
        # The shared snapshot must not satisfy, or clear, the issue being assigned.
        # One live read per process: later chunks in the same --check reuse it.
        try:
            native = _native_fetch(int(c["issue"]))
        except _NATIVE_ERRORS:
            native = None
        cache[("native", c["issue"])] = native
        if not parse_dependencies(c.get("blockers", ""), c.get("wake_condition", "")):
            node = graphql_nodes([int(c["issue"])], comments=True).get(int(c["issue"]))
            comment_nodes = node.get("comments") if isinstance(node, dict) else None
            if isinstance(comment_nodes, dict) and isinstance(comment_nodes.get("nodes"), list):
                comment_text = "\n".join(x.get("body", "") for x in comment_nodes["nodes"] if isinstance(x, dict))
            else:
                comment_text = ""
        cache[fresh_done] = True
    elif fresh:
        native = cache.get(("native", c["issue"]))
    else:
        native = native_dependencies(c['issue'], cache) if not offline and c.get('issue') else []
    if native is None:
        c['blocking_labels'] = sorted(set(c.get('blocking_labels') or []) | {'pm-blocked:dependency'})
        c['dependency_reason'] = 'LOOKUP_FAILED'
        return
    if native:
        c['blocking_labels'] = sorted(set(c.get('blocking_labels') or []) | {'pm-blocked:dependency'})
    if "pm-blocked:dependency" not in c.get("blocking_labels", []):
        return
    refs = parse_dependencies(c.get("blockers", ""), c.get("wake_condition", ""))
    malformed = refs is None
    from_body = False
    if not refs:
        refs = prose_dependencies(issue_body + '\n' + comment_text)
        from_body = bool(refs)
    if malformed and not refs:
        c['dependency_reason'] = 'no_dependency_declared'
        return
    refs = refs or []
    known = {n for n, _ in refs}
    refs.extend((n, kind) for n, kind in (native or []) if n not in known)
    if fresh and cache.get(fresh_done) is True and not cache.get(("fresh_states", int(c.get("issue") or -1))):
        for number, kind in refs:
            cache.pop((number, kind), None)
            cache.pop(("state", number), None)
            cache.pop(("native_state", number), None)
        cache[("fresh_states", int(c.get("issue") or -1))] = True
    if not refs:
        # The label says blocked, but nothing is named. Do not assign (unverifiable) and do not
        # hide it as unparseable prose: this is a stale label an operator must re-queue.
        c["dependency_reason"] = "no_dependency_declared"
        return
    c["dependencies"] = [{"number": n, "kind": kind} for n, kind in refs]
    if offline:
        c["dependency_reason"] = "dependency_check_disabled"
        return
    unresolved = []
    # One GraphQL round for every uncached blocker, not one query per number.
    pending = [number for number, kind in refs if (number, kind) not in cache]
    if pending and not offline:
        try:
            nodes = graphql_nodes(pending)
        except TimeoutError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            nodes = {number: None for number in pending}
        for number in pending:
            node = nodes.get(number) if isinstance(nodes, dict) else None
            if isinstance(node, dict):
                cache[("state", number)] = node.get("state")
    for number, kind in refs:
        key = (number, kind)
        if key not in cache:
            node = None
            if pending and not offline:
                node = nodes.get(number) if isinstance(nodes, dict) else None
            try:
                cache[key] = dependency_satisfaction(node)
            except (ValueError, TypeError):
                cache[key] = (False, "LOOKUP_FAILED")
        satisfied, state = cache[key]
        if satisfied and number in {n for n, _ in (native or [])}:
            native_state_key = ('native_state', number)
            if native_state_key not in cache:
                known = cache.get(("state", number))
                if known is None and not offline:
                    try:
                        node = graphql_nodes([number]).get(number)
                    except TimeoutError:
                        raise
                    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
                        node = None
                    known = node.get("state") if isinstance(node, dict) else None
                    if isinstance(node, dict):
                        cache[("state", number)] = known
                cache[native_state_key] = known == 'CLOSED'
            if not cache[native_state_key]:
                satisfied, state = False, 'NATIVE_BLOCKER_NOT_CLOSED_OR_LOOKUP_FAILED'
        if not satisfied:
            unresolved.append({"number": number, "kind": kind, "state": state})
    c["open_dependencies"] = unresolved
    if unresolved:
        c["dependency_reason"] = "LOOKUP_FAILED" if any(item["state"] == "LOOKUP_FAILED" for item in unresolved) else "open_or_unverified_dependency"
    elif set(c["blocking_labels"]) == {"pm-blocked:dependency"}:
        c["dependency_unblocked"] = True
        if from_body:
            c["dependency_reason"] = "dependency_resolved_from_body"
    else:
        c["dependency_reason"] = "other_blocking_labels"


def graphql_nodes(numbers: list[int], comments: bool = False, candidate_details: bool = False) -> dict[int, dict]:
    if numbers and os.environ.get("GH_BUDGET_DISABLE") != "1" and gh_budget.graphql_low():
        gh_budget.note_stale(
            f"graphql remaining {gh_budget.graphql_remaining()} < {gh_budget.LOW_WATER}; skipped a GraphQL node lookup")
        return {n: None for n in numbers}
    result = {}
    for start in range(0, len(numbers), 50):
        batch = numbers[start:start + 50]
        fields = ('... on Issue { number state stateReason comments(last: 20) { nodes { body } } }'
                  if comments else '... on Issue { number state stateReason ' + ASSOCIATED_PRS
                  + (' title body createdAt labels(first:100){nodes{name} pageInfo{hasNextPage}}' if candidate_details else '')
                  + ' } ... on PullRequest { number state mergedAt }')
        aliases = ' '.join(f'i{n}: issueOrPullRequest(number:{n}) {{ {fields} }}' for n in batch)
        query = '{ repository(owner:"Scribie",name:"heydonna-app") { ' + aliases + ' } }'
        try:
            # gh process startup dominated small batches. Tests pin GH_BUDGET_DISABLE
            # so the subprocess stub still sees the query.
            if os.environ.get("GH_BUDGET_DISABLE") == "1":
                payload = json.loads(run_lookup(['gh', 'api', 'graphql', '-f', 'query=' + query]).stdout)
            else:
                payload = graphql_http(query)
            nodes = payload['data']['repository']
            if payload.get('errors') or not isinstance(nodes, dict):
                raise ValueError('GraphQL errors')
            result.update({n: nodes.get(f'i{n}') for n in batch})
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            result.update({n: None for n in batch})
    return result


def _dump_dep_cache(issues: list[dict], cache: dict) -> dict:
    comments = {str(int(i["number"])): i.get("_dependency_comments", "")
                for i in issues if "_dependency_comments" in i}
    states = {}
    for key, value in cache.items():
        if isinstance(key, tuple) and len(key) == 2 and isinstance(key[0], int):
            states[f"{key[0]}|{key[1]}"] = list(value) if isinstance(value, tuple) else value
        elif isinstance(key, tuple) and key and key[0] == "state" and isinstance(key[1], int):
            states[f"state|{key[1]}"] = value
    return {"comments": comments, "states": states}


def _apply_dep_payload(issues: list[dict], cache: dict, payload: dict) -> None:
    comments = payload.get("comments") or {}
    for issue in issues:
        key = str(int(issue["number"]))
        if key in comments:
            issue["_dependency_comments"] = comments[key]
    for raw, value in (payload.get("states") or {}).items():
        if raw.startswith("state|"):
            cache[("state", int(raw.split("|", 1)[1]))] = value
            continue
        number_s, sep, kind = raw.partition("|")
        if not sep:
            continue
        cache[(int(number_s), kind)] = tuple(value) if isinstance(value, list) else value


def prefetch_dependencies(issues: list[dict], cache: dict, *, snapshot: bool = False) -> None:
    """Batched dependency lookup.

    Only the full candidate snapshot is written to the shared TTL cache. A --check of
    one issue must not replace that snapshot with a partial result; it may read it,
    then live-refresh the issue being assigned.
    """
    if os.environ.get("GH_BUDGET_DISABLE") == "1":
        _prefetch_dependencies_uncached(issues, cache)
        return
    name = pool_cache_name("ready-pool-deps")
    if snapshot:
        try:
            payload, _live = gh_budget.serve(name, SNAPSHOT_TTL, lambda: _capture_dependencies(issues))
        except LookupError:
            gh_budget.note_stale(
                f"graphql remaining {gh_budget.graphql_remaining()} < {gh_budget.LOW_WATER} "
                "and no ready-pool dependency cache")
            return
    else:
        payload, age = gh_budget.peek(name)
        if not (isinstance(payload, dict) and payload.get("complete") and age is not None and age <= SNAPSHOT_TTL):
            _prefetch_dependencies_uncached(issues, cache)
            return
    if isinstance(payload, dict) and payload.get("complete"):
        _apply_dep_payload(issues, cache, payload)


def _capture_dependencies(issues: list[dict]) -> dict:
    local: dict = {}
    _prefetch_dependencies_uncached(issues, local)
    payload = _dump_dep_cache(issues, local)
    payload["complete"] = True
    return payload


def _prefetch_dependencies_uncached(issues: list[dict], cache: dict) -> None:
    missing = []
    for issue in issues:
        if 'pm-blocked:dependency' not in issue_holds(issue) or '_dependency_comments' in issue:
            continue  # already prefetched (fast --check re-evaluates the picked issue per chunk)
        c = candidate(issue, 'new_issue')
        if not parse_dependencies(c['blockers'], c['wake_condition']):
            missing.append(int(issue['number']))
    for issue in issues:
        if int(issue['number']) in missing:
            issue['_dependency_comments'] = ''
    nodes = graphql_nodes(missing, comments=True)
    for issue in issues:
        node = nodes.get(int(issue['number']))
        if node:
            issue['_dependency_comments'] = '\n'.join(x.get('body', '') for x in node['comments']['nodes'])
    refs = set()
    for issue in issues:
        c = candidate(issue, classify(issue) or 'new_issue')
        if 'pm-blocked:dependency' not in c['blocking_labels'] and not c['blockers']:
            continue
        parsed = parse_dependencies(c['blockers'], c['wake_condition']) or prose_dependencies(c['issue_body'] + '\n' + c['comment_text'])
        refs.update(parsed)
    states = graphql_nodes(sorted({n for n, _ in refs}))
    for number, kind in refs:
        node = states.get(number)
        if not node:
            cache[(number, kind)] = (False, 'LOOKUP_FAILED')
            continue
        cache[(number, kind)] = dependency_satisfaction(node)
        if isinstance(node, dict):
            cache[("state", number)] = node.get("state")


def candidate(issue: dict[str, Any], cls: str) -> dict[str, Any]:
    labels = _labels(issue)
    body = issue.get("body") or ""
    rank = priority_rank(labels, body)
    wake = ready_pool_value(body, "wake_condition")
    blockers = ready_pool_value(body, "blockers")
    return {
        "issue": int(issue["number"]),
        "class": cls,
        "priority": prio_name(rank),
        "priority_rank": rank,
        "created_at": issue.get("createdAt") or "",
        "title": (issue.get("title") or "")[:100],
        "gate": ready_pool_value(body, "gate"),
        "blockers": blockers,
        "wake_condition": wake,
        "blocking_labels": blocking_labels(labels, issue),
        "issue_body": body if "pm-blocked:dependency" in issue_holds(issue) else "",
        "comment_text": issue.get("_dependency_comments", ""),
        **({"pr": issue["pr"]} if issue.get("pr") else {}),
    }


def sort_key(c: dict[str, Any]) -> tuple:
    return (c["priority_rank"], CLASS_RANK.get(c["class"], 9), c.get("created_at") or "~", c["issue"])


def rank_candidates(issues: list[dict[str, Any]], extra: list[dict[str, Any]] | None = None,
                    rework_only: bool = False, cache: dict | None = None,
                    offline: bool = False) -> list[dict[str, Any]]:
    """Pure: assignable (ungated) candidates in the mandatory order (deduped by issue,
    best class kept). Gated P0s are excluded here; see gated_p0_candidates()."""
    best: dict[int, dict[str, Any]] = {}
    cache = cache if cache is not None else {}
    if not offline:
        prefetch_dependencies(issues, cache)
    gated = {c["issue"] for c in gated_p0_candidates(issues, rework_only, cache, offline)}
    for issue in issues:
        cls = classify(issue)
        if cls:
            if cls == "new_issue" and issue.get("_implementation_merged"):
                continue
            if cls == "new_issue" and contract_reasons(issue):
                continue
            c = candidate(issue, cls)
            evaluate_dependency(c, cache, offline)
            if not is_gated(c):
                best[c["issue"]] = c
    for row in extra or []:
        cls = row.get("class", "rework")
        source = next((i for i in issues if int(i["number"]) == int(row["issue"])), None)
        if source is None or classify(source, cls) is None:
            continue
        if cls == "new_issue":
            # A PM-supplied row cannot bypass the live issue contract.
            if classify(source) != "new_issue" or contract_reasons(source) or source.get("_implementation_merged"):
                continue
        c = candidate(source, cls)
        if row.get("pr"):
            c["pr"] = row["pr"]
        evaluate_dependency(c, cache, offline)
        if is_gated(c):
            continue
        if c["issue"] in gated:  # PM row cannot ungate a pm-blocked P0
            continue
        old = best.get(c["issue"])
        if old is None or sort_key(c) < sort_key(old):
            best[c["issue"]] = c
    out = list(best.values())
    if rework_only:
        out = [c for c in out if c["class"] in ("rework", "repro")]
    return sorted(out, key=sort_key)


def gated_p0_candidates(issues: list[dict[str, Any]], rework_only: bool = False,
                        cache: dict | None = None, offline: bool = False) -> list[dict[str, Any]]:
    """Pure: P0s in the pool that carry pm-blocked:*; the assigner checks the gate."""
    out = []
    cache = cache if cache is not None else {}
    for issue in issues:
        cls = classify(issue)
        if not cls:
            continue
        if cls == "new_issue" and issue.get("_implementation_merged"):
            continue
        if cls == "new_issue" and contract_reasons(issue):
            continue
        c = candidate(issue, cls)
        evaluate_dependency(c, cache, offline)
        if is_gated(c) and (not rework_only or cls in ("rework", "repro")):
            out.append(c)
    return sorted(out, key=sort_key)


def priority_reason(pick: dict[str, Any], ranked: list[dict[str, Any]]) -> str:
    """Receipt line for the assignment; non-empty when the pick is below P1."""
    if pick["priority_rank"] <= 1:
        return ""
    higher = [c for c in ranked if c["priority_rank"] <= 1]
    if higher:  # cannot happen for ranked[0]; guard for --check of a non-head pick
        return ""
    return (f"priority_reason: {pick['priority']} {pick['class']} #{pick['issue']} taken because "
            f"no ready P0/P1 candidate exists ({len(ranked)} eligible checked)")


def check_pick(issue_no: int, ranked: list[dict[str, Any]],
               gated: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Pure: allow the pick only if no eligible candidate sorts strictly better on priority,
    or (same priority) on class. Age within a tier is advisory. A gated P0 pick returns
    GATED; gated P0s never count as `better` (they are not assignable)."""
    g = next((c for c in gated or [] if c["issue"] == issue_no), None)
    if g is not None:
        return {"status": "DEPENDENCY_OPEN" if "pm-blocked:dependency" in g.get("blocking_labels", []) else "GATED", "issue": issue_no, "pick": g, "gate": g.get("gate", ""),
                "wake_condition": g.get("wake_condition", ""),
                "blocking_labels": g.get("blocking_labels", []),
                "reason": g.get("dependency_reason", "blocked"),
                "open_dependencies": g.get("open_dependencies", [])}
    pick = next((c for c in ranked if c["issue"] == issue_no), None)
    if pick is None:
        return {"status": "not_eligible", "issue": issue_no,
                "reason": "issue is not an eligible Ready Pool/rework candidate"}
    better = [c for c in ranked
              if (c["priority_rank"], CLASS_RANK.get(c["class"], 9))
              < (pick["priority_rank"], CLASS_RANK.get(pick["class"], 9))]
    if better:
        b = better[0]
        return {"status": "PRIORITY_INVERSION", "issue": issue_no, "pick": pick, "should_take": b,
                "reason": f"#{issue_no} is {pick['priority']} {pick['class']} but "
                          f"#{b['issue']} ({b['priority']} {b['class']}) is ready"}
    return {"status": "ok", "issue": issue_no, "pick": pick,
            "priority_reason": priority_reason(pick, ranked)}


def priority_inversions(slots: list[dict[str, Any]], issue_prio: dict[int, int],
                        ranked: list[dict[str, Any]]) -> list[str]:
    """Pure: occupied slots on a P2-or-lower lane while a P0 is ready and unassigned."""
    held = {int(s["issue"]) for s in slots if s.get("occupied") and s.get("issue")}
    ready_p0 = [c for c in ranked if c["priority_rank"] == 0 and c["issue"] not in held]
    if not ready_p0:
        return []
    out = []
    for s in sorted(slots, key=lambda s: s.get("slot", 0)):
        if not s.get("occupied") or not s.get("issue"):
            continue
        rank = issue_prio.get(int(s["issue"]), UNKNOWN_RANK)
        if rank in (2, 3):  # unknown priority (closed/unlabelled lane) is not flagged
            out.append(f"PRIORITY_INVERSION S{s.get('slot')} on #{s['issue']} ({prio_name(rank)}) "
                       f"while P0 #{ready_p0[0]['issue']} is ready and unassigned")
    return out


def candidate_node(node: dict) -> dict:
    labels = node.get("labels") or {}
    if not isinstance(labels, dict) or (labels.get("pageInfo") or {}).get("hasNextPage") is not False:
        raise ValueError("incomplete candidate labels")
    ok, state = dependency_satisfaction(node)
    if state == "LOOKUP_FAILED":
        raise ValueError("candidate implementation association unavailable")
    return {**node, "labels": labels["nodes"], "_implementation_merged": ok}


def _load_open_issues_uncached(timeout: int = 240) -> list[dict[str, Any]]:
    """Complete candidate-label union. Labels are fetched together; each label is pages of 100.

    Contract checks run as each page lands, overlapping the remaining round trips.
    """
    fields = ('number title body state createdAt labels(first:100){nodes{name} pageInfo{hasNextPage}} '
              + ASSOCIATED_PRS)
    return _load_label_union(timeout, fields, candidate_node, page_size=100, page_timeout=60, order_by=True)


def load_open_issues(timeout: int = 240) -> list[dict[str, Any]]:
    """Candidate snapshot. Shared SNAPSHOT_TTL cache keyed by repo + label set.
    Below the GraphQL floor a stale cache is served; a miss stays UNAVAILABLE."""
    if os.environ.get("GH_BUDGET_DISABLE") == "1":
        return _load_open_issues_uncached(timeout)
    try:
        issues, _live = gh_budget.serve(
            pool_cache_name("ready-pool-open-issues"), SNAPSHOT_TTL, lambda: _load_open_issues_uncached(timeout))
    except LookupError as exc:
        raise ValueError(str(exc)) from None
    return issues


def _load_open_prs_uncached(timeout: int = 30) -> list[dict[str, Any]]:
    proc = run_lookup(
        ["gh", "pr", "list", "--repo", REPO, "--state", "open", "--limit", "1000",
         "--json", "number,isDraft,body,updatedAt,closingIssuesReferences"], timeout=timeout,
    )
    return json.loads(proc.stdout)


def load_open_prs(timeout: int = 30, *, fresh: bool = False) -> list[dict[str, Any]]:
    """`gh pr list` is GraphQL. The ranking snapshot is shared; --check passes fresh=True
    so the issue being assigned is not excluded or kept by a stale open-PR list."""
    if fresh or os.environ.get("GH_BUDGET_DISABLE") == "1":
        return _load_open_prs_uncached(timeout)
    try:
        prs, _live = gh_budget.serve(
            pool_cache_name("ready-pool-open-prs"), SNAPSHOT_TTL, lambda: _load_open_prs_uncached(timeout))
    except LookupError as exc:
        raise ValueError(str(exc)) from None
    return prs


def owned_issues(prs: list[dict[str, Any]]) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    """Conservatively exclude Part of references when remaining ACs are uncertain."""
    owned: dict[int, list[int]] = {}
    uncertain: dict[int, list[int]] = {}
    for pr in prs:
        for ref in pr.get("closingIssuesReferences") or []:
            if ref.get("number"):
                owned.setdefault(int(ref["number"]), []).append(int(pr["number"]))
        body = pr.get("body") or ""
        for match in re.finditer(r"\b(closes?|fix(?:es)?|resolves?|part\s+of|refs?)\s+#(\d+)\b", body, re.IGNORECASE):
            if re.search(r"(?:\bnot|\bnever|n't|n’t)\s+$", body[max(0, match.start() - 20):match.start()], re.I):
                continue
            issue = int(match.group(2))
            if match.group(1).lower() in {"part of", "ref", "refs"}:
                uncertain.setdefault(issue, []).append(int(pr["number"]))
            owned.setdefault(issue, []).append(int(pr["number"]))
    return owned, uncertain


# ---------------------------------------------------------------------------
# Dependency hell (Rajiv DM D0AMF0XE6TS thread 1793816247.442119, ts 1793855417.837589:
# "it should warn the pm on issues which are stuck in a dependency hell as well. pm has to
# take care of it."). Read-only analysis over the dependency-gated pool; --sync-obligations
# mirrors it into pm-ops `dependency_hell` obligations (owner PM -> hourly "Pending PM actions").
# ---------------------------------------------------------------------------
DEP_HELL_MAX_DEPTH = 6
DEEP_THRESHOLD = 3          # open blockers on the longest chain
STALE_DAYS = 3              # root idle this long (or ownerless) -> STALE_ROOT
AGED_DAYS = 7               # gated this long -> AGED
FANOUT_THRESHOLD = 3        # one open root gating this many pool issues -> hotspot
EXTERNAL_LABELS = {"pm-blocked:external", "pm-blocked:product", "pm-blocked:cto"}
EXTERNAL_TEXT_RE = re.compile(
    r"(?i)\b(partner|daya|abi|vendor|rajiv|cto|decision|approv\w*|sign-?off|external)\b")
NONE_VALUES = {"", "none", "null", "n/a", "[]"}
DEP_HELL_STATE = Path.home() / ".claude" / "state" / "ready_pool_dependency_hell.json"
PM_OPS = Path.home() / ".claude" / "scripts" / "pm-ops.py"
_ISSUE_DETAIL = ('... on Issue { number state stateReason title updatedAt body '
                 + ASSOCIATED_PRS + ' '
                 'assignees(first:5){nodes{login}} labels(first:50){nodes{name}} '
                 'timelineItems(itemTypes:[LABELED_EVENT], last:40)'
                 '{nodes{... on LabeledEvent{createdAt label{name}}}} }')
_PR_DETAIL = ('... on PullRequest { number state mergedAt title updatedAt '
              'assignees(first:5){nodes{login}} labels(first:50){nodes{name}} }')


def _ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _age_days(value: str, now: datetime) -> float | None:
    t = _ts(value)
    return None if t is None else (now - t).total_seconds() / 86400


def normalize_node(raw: dict | None) -> dict | None:
    if not raw or not raw.get("number"):
        return None
    dep_at = ""
    for ev in ((raw.get("timelineItems") or {}).get("nodes") or []):
        if ev and (ev.get("label") or {}).get("name") == "pm-blocked:dependency":
            dep_at = max(dep_at, ev.get("createdAt") or "")
    return {
        "number": int(raw["number"]),
        "is_pr": "mergedAt" in raw,
        "state": raw.get("state") or "",
        "state_reason": raw.get("stateReason"),
        "merged_at": raw.get("mergedAt"),
        "title": (raw.get("title") or "")[:100],
        "updated_at": raw.get("updatedAt") or "",
        "assignees": [a.get("login", "") for a in (raw.get("assignees") or {}).get("nodes") or [] if a],
        "labels": sorted(l.get("name", "") for l in (raw.get("labels") or {}).get("nodes") or [] if l),
        "body": raw.get("body") or "",
        "closedByPullRequestsReferences": raw.get("closedByPullRequestsReferences"),
        "dep_label_at": dep_at,
    }


def _graphql_detail_batch(batch: list[int]) -> tuple[dict[int, dict | None], int]:
    aliases = " ".join(f"i{n}: issueOrPullRequest(number:{n}) {{ {_ISSUE_DETAIL} {_PR_DETAIL} }}" for n in batch)
    query = '{ repository(owner:"Scribie",name:"heydonna-app") { ' + aliases + " } }"
    repo = None
    for _ in range(2):
        try:
            payload = graphql_http(query, timeout=60)
            repo = (payload.get("data") or {}).get("repository")
            if isinstance(repo, dict):
                break
        except (OSError, ValueError, TimeoutError):
            repo = None
    if not isinstance(repo, dict):
        return {n: None for n in batch}, 1
    return {n: normalize_node(repo.get(f"i{n}")) for n in batch}, 0


def graphql_details(numbers: list[int]) -> tuple[dict[int, dict | None], int]:
    """Batched detail lookup (25/batch, one retry, batches in flight together).
    Partial GraphQL data is kept: a deleted/transferred number comes back null
    with an error but must not void its batch."""
    batches = [numbers[start:start + 25] for start in range(0, len(numbers), 25)]
    result: dict[int, dict | None] = {}
    failures = 0
    if len(batches) <= 1:
        parts = [_graphql_detail_batch(batch) for batch in batches]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
            parts = list(pool.map(_graphql_detail_batch, batches))
    for part, failed in parts:
        result.update(part)
        failures += failed
    return result, failures


def node_satisfied(node: dict | None, kind: str) -> bool:
    """Same satisfaction semantics as prefetch_dependencies()."""
    if not node:
        return False
    raw = {"state": node["state"], "mergedAt": node["merged_at"]} if node["is_pr"] else node
    return dependency_satisfaction(raw)[0]


def declared_deps(node: dict | None) -> list[tuple[int, str]]:
    """Declared blockers of an OPEN issue node, mirroring evaluate_dependency() (structured
    `blockers:` or a normalized dependency hold; prose body fallback). Comments are not read
    for transitive nodes. PRs and closed nodes are leaves."""
    if not node or node["is_pr"] or node["state"] != "OPEN" or node_satisfied(node, "default"):
        return []
    body = node["body"]
    dependency_hold = "pm-blocked:dependency" in issue_holds(node)
    blockers = ready_pool_value(body, "blockers")
    if blockers.strip().lower() in NONE_VALUES and not dependency_hold:
        return []
    refs = parse_dependencies(blockers, ready_pool_value(body, "wake_condition"))
    if not refs and dependency_hold:
        refs = prose_dependencies(body)
    return refs or []


def fetch_dependency_graph(gated: list[dict[str, Any]], max_depth: int = DEP_HELL_MAX_DEPTH
                           ) -> tuple[dict[int, dict | None], int]:
    """Level-by-level batched fetch of every node reachable through open blockers (deduped)."""
    gated_edges = {c["issue"]: [(d["number"], d["kind"]) for d in c.get("open_dependencies") or []] for c in gated}
    nodes: dict[int, dict | None] = {}
    failures = 0
    frontier = set(gated_edges) | {n for edges in gated_edges.values() for n, _ in edges}
    for _ in range(max_depth + 1):
        todo = sorted(n for n in frontier if n not in nodes)
        if not todo:
            break
        got, f = graphql_details(todo)
        failures += f
        nodes.update(got)
        frontier = set()
        for n in todo:
            deps = gated_edges[n] if n in gated_edges else declared_deps(nodes.get(n))
            frontier.update(d for d, _ in deps if d not in nodes)
    return nodes, failures


def _open_edges(n: int, nodes: dict, gated_edges: dict) -> list[int]:
    if n in gated_edges:
        return [d for d, _ in gated_edges[n]]
    return [d for d, k in declared_deps(nodes.get(n)) if not node_satisfied(nodes.get(d), k)]


def _walk(start: int, nodes: dict, gated_edges: dict, max_depth: int) -> dict[str, Any]:
    """Bounded DFS over open blockers: best path per leaf root, cycles, truncation."""
    leaves: dict[int, list[int]] = {}
    cycles: list[list[int]] = []
    truncated = False
    stack = [[start]]
    steps = 0
    while stack:
        path = stack.pop()
        steps += 1
        if steps > 5000:
            truncated = True
            break
        n = path[-1]
        nxt = _open_edges(n, nodes, gated_edges)
        if n != start and (not nxt or len(path) - 1 >= max_depth):
            truncated = truncated or bool(nxt)
            if len(path) > len(leaves.get(n, [])):
                leaves[n] = path
            continue
        for d in nxt:
            if d in path:
                cycles.append(path + [d])
            else:
                stack.append(path + [d])
    return {"leaves": leaves, "cycles": cycles, "truncated": truncated}


def assess_root(n: int, nodes: dict, owned: dict[int, list[int]], pr_updated: dict[int, str],
                now: datetime) -> dict[str, Any]:
    node = nodes.get(n)
    if node is None:
        return {"number": n, "state": "LOOKUP_FAILED", "owner": "unknown", "last_activity": "", "flags": [], "title": ""}
    labels = set(node["labels"])
    linked = sorted(p for p in owned.get(n, []) if p != n)
    owners = ["@" + a for a in node["assignees"]] + sorted(l for l in labels if re.fullmatch(r"slot:\d+", l))
    owners += ([f"PR #{n}"] if node["is_pr"] and node["state"] == "OPEN" else []) + [f"PR #{p}" for p in linked]
    activity = max([node["updated_at"]] + [pr_updated.get(p, "") for p in linked])
    flags, detail = [], []
    if node["state"] != "OPEN":
        flags.append("DEAD_ROOT")  # closed without satisfying the declared condition: never wakes
        detail.append(f"closed {node['state_reason'] or node['state']} without satisfying the dependency")
    else:
        idle = _age_days(activity, now)
        if not owners:
            detail.append("no live owner")
        if idle is not None and idle >= STALE_DAYS:
            detail.append(f"idle {idle:.1f}d")
        if detail:
            flags.append("STALE_ROOT")
        body = node["body"]
        text = " ".join(ready_pool_value(body, f) for f in ("gate", "wake_condition", "blockers"))
        if labels & EXTERNAL_LABELS or EXTERNAL_TEXT_RE.search(text):
            flags.append("EXTERNAL_ROOT")
            detail.append("waits on " + (", ".join(sorted(labels & EXTERNAL_LABELS)) or "partner/decision"))
    return {"number": n, "state": node["state"], "title": node["title"], "owner": ", ".join(owners) or "none",
            "last_activity": activity, "flags": flags, "detail": "; ".join(detail)}


_NEXT_STEP = {
    "CYCLE": "break the cycle (drop or re-point one blocker)",
    "DEAD_ROOT": "re-scope: root closed without satisfying the dependency",
    "STALE_ROOT": "unblock root (assign slot/pm-hotfix or owner)",
    "EXTERNAL_ROOT": "chase the partner/decision on the root or re-scope",
    "DEEP": "re-scope/split so the chain is shorter",
    "AGED": "unblock root or re-scope",
}


def dependency_hell_report(gated: list[dict[str, Any]], nodes: dict, owned: dict[int, list[int]],
                           pr_updated: dict[int, str], now: datetime,
                           max_depth: int = DEP_HELL_MAX_DEPTH) -> tuple[list[dict], list[dict]]:
    """Pure: (dependency_hell, dependency_hotspots) for the dependency-gated pool.

    AGED uses the last `pm-blocked:dependency` LabeledEvent time from the issue timeline. When
    there is none (gated via `blockers:` only, or lookup failed) it uses the issue updatedAt,
    which is a LOWER bound on the gated age (adding the label also bumps updatedAt), so AGED
    there can under-report but never over-report. updatedAt also moves on bot/label edits, so
    STALE_ROOT idle detection is conservative in the same direction."""
    gated_edges = {c["issue"]: [(d["number"], d["kind"]) for d in c.get("open_dependencies") or []] for c in gated}
    root_cache: dict[int, dict] = {}

    def root(n: int) -> dict:
        if n not in root_cache:
            root_cache[n] = assess_root(n, nodes, owned, pr_updated, now)
        return root_cache[n]

    walks, fan = {}, {}
    for c in gated:
        w = _walk(c["issue"], nodes, gated_edges, max_depth)
        walks[c["issue"]] = w
        for r in w["leaves"]:
            fan.setdefault(r, []).append(c)
    hotspots = []
    for r, cs in fan.items():
        if len(cs) >= FANOUT_THRESHOLD:
            info = root(r)
            cs = sorted(cs, key=lambda c: (c["priority_rank"], c["issue"]))
            hotspots.append({"root": r, "root_title": info.get("title", ""), "root_owner": info["owner"],
                             "root_last_activity": info["last_activity"], "root_flags": info["flags"],
                             "count": len(cs), "best_priority": cs[0]["priority"], "_rank": cs[0]["priority_rank"],
                             "gated_issues": [c["issue"] for c in cs]})
    hotspots.sort(key=lambda h: (h.pop("_rank"), -h["count"], h["root"]))
    fan_count = {r: len(cs) for r, cs in fan.items()}

    hell = []
    for c in gated:
        w = walks[c["issue"]]
        reasons: list[str] = []
        leaves = w["leaves"]
        depth = max((len(p) - 1 for p in leaves.values()), default=0)
        if w["cycles"]:
            reasons.append("CYCLE")
        if depth >= DEEP_THRESHOLD or w["truncated"]:
            reasons.append("DEEP")
        flagged = {r: root(r)["flags"] for r in leaves}
        for flag in ("STALE_ROOT", "EXTERNAL_ROOT", "DEAD_ROOT"):
            if any(flag in f for f in flagged.values()):
                reasons.append(flag)
        node = nodes.get(c["issue"]) or {}
        since, source = node.get("dep_label_at", ""), "label_event"
        if not since:
            since, source = node.get("updated_at", ""), "updated_at_lower_bound"
        aged = _age_days(since, now)
        if aged is not None and aged >= AGED_DAYS:
            reasons.append("AGED")
        if not reasons:
            continue
        if leaves:
            primary = max(leaves, key=lambda r: (len(leaves[r]), bool(flagged[r]), fan_count.get(r, 0), -r))
            chain = leaves[primary]
        elif w["cycles"]:
            chain = w["cycles"][0]
            primary = chain[-1]
        else:
            chain, primary = [c["issue"]], None
        info = root(primary) if primary is not None else {"owner": "", "last_activity": "", "detail": "", "state": ""}
        entry = {
            "issue": c["issue"], "priority": c["priority"], "priority_rank": c["priority_rank"],
            "title": c.get("title", ""), "reasons": reasons, "chain": "→".join(f"#{n}" for n in chain),
            "depth": depth, "root_blocker": primary, "root_owner": info["owner"],
            "root_last_activity": info["last_activity"], "root_state": info["state"],
            "root_detail": info.get("detail", ""), "fanout": fan_count.get(primary, 0) if primary else 0,
            "gated_since": since, "gated_since_source": source,
            "gated_days": round(aged, 1) if aged is not None else None,
        }
        if w["cycles"]:
            entry["cycle"] = "→".join(f"#{n}" for n in w["cycles"][0])
        if len(leaves) > 1:
            entry["other_roots"] = sorted(r for r in leaves if r != primary)
        hell.append(entry)
    hell.sort(key=lambda e: (e["priority_rank"], -e["fanout"], e["issue"]))
    return hell, hotspots


def obligation_targets(hell: list[dict], hotspots: list[dict]) -> dict[int, dict[str, str]]:
    """One obligation per fanout hotspot root; the report lists remain unchanged."""
    targets: dict[int, dict[str, str]] = {}
    for h in hotspots:
        issues = ', '.join('#' + str(n) for n in h['gated_issues'][:10])
        remaining = len(h['gated_issues']) - 10
        if remaining > 0:
            issues += f", +{remaining} more"
        text = (f"FANOUT: open root #{h['root']} gates {h['count']} pool issues "
                f"({issues}); owner {h['root_owner']}; next: unblock root #{h['root']} first")
        sev = "high" if h["best_priority"] in ("P0", "P1") else "normal"
        targets[h["root"]] = {"severity": sev, "title": f"Dependency hotspot #{h['root']}: gates {h['count']}", "action": text[:600]}
    return targets


def sync_obligations(targets: dict[int, dict[str, str]], complete: bool,
                     runner=subprocess.run, state_path: Path = DEP_HELL_STATE) -> dict[str, Any]:
    """Upsert every current target; resolve targets this script upserted before that are no
    longer in dependency hell. Resolution is skipped when the analysis was incomplete (lookup
    failures) so a GitHub outage cannot clear real obligations."""
    try:
        prev = set(json.loads(state_path.read_text()).get("targets", []))
    except (OSError, ValueError, AttributeError):
        prev = set()
    upserted, failed, resolved, already_closed = 0, [], 0, 0
    for n, t in sorted(targets.items()):
        cmd = ["python3", str(PM_OPS), "obligation-upsert", "--kind", "dependency_hell",
               "--target-type", "github_issue", "--target-id", str(n), "--issue", str(n),
               "--owner", "PM", "--severity", t["severity"], "--title", t["title"], "--action", t["action"]]
        p = runner(cmd, capture_output=True, text=True, timeout=60)
        if p.returncode == 0:
            upserted += 1
        else:
            failed.append({"issue": n, "op": "upsert", "stderr": (p.stderr or "")[-200:]})
    keep = set(targets)
    if complete:
        for n in sorted(prev - keep):
            cmd = ["python3", str(PM_OPS), "obligation-resolve", "--kind", "dependency_hell",
                   "--target-type", "github_issue", "--target-id", str(n), "--issue", str(n), "--reason", "cleared"]
            p = runner(cmd, capture_output=True, text=True, timeout=60)
            if p.returncode == 0:
                resolved += 1
            elif "matched no" in (p.stderr or "") + (p.stdout or ""):
                already_closed += 1
            else:
                failed.append({"issue": n, "op": "resolve", "stderr": (p.stderr or "")[-200:]})
                keep.add(n)
    else:
        keep |= prev
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"targets": sorted(keep), "updated_at": datetime.now(timezone.utc).isoformat()}))
    except OSError as exc:
        failed.append({"op": "state", "stderr": str(exc)})
    return {"upserted": upserted, "resolved": resolved, "already_closed": already_closed,
            "resolve_skipped": not complete, "failed": failed}


def _dep_hell_selftest() -> None:
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    fresh, old5, old8, old10 = "2026-11-04T12:00:00Z", "2026-10-31T00:00:00Z", "2026-10-28T00:00:00Z", "2026-10-26T00:00:00Z"

    def node(n, state="OPEN", labels=(), blockers="", assignees=("dev",), updated=fresh, is_pr=False,
             reason=None, dep_at=""):
        body = f"<!-- ready-pool:\nblockers: {blockers}\n-->" if blockers else ""
        return {"number": n, "is_pr": is_pr, "state": state, "state_reason": reason, "merged_at": None,
                "title": f"t{n}", "updated_at": updated, "assignees": list(assignees), "labels": list(labels),
                "body": body, "dep_label_at": dep_at}

    def gate(n, deps, prio=2):
        return {"issue": n, "priority": f"P{prio}", "priority_rank": prio, "title": f"g{n}",
                "open_dependencies": [{"number": d, "kind": k, "state": "OPEN"} for d, k in deps]}

    nodes = {n: node(n) for n in (100, 200, 300, 310, 400, 500, 501, 502, 700, 800, 900)}
    nodes.update({
        101: node(101, labels=["pm-blocked:dependency"], blockers="#102"),
        102: node(102, labels=["pm-blocked:dependency"], blockers="#101"),
        201: node(201, blockers="#202"), 202: node(202, blockers="#203"), 203: node(203),
        301: node(301, assignees=()),                       # ownerless root
        311: node(311, updated=old5),                       # owned but idle 5d
        401: node(401, labels=["pm-blocked:external"]),
        402: node(402, blockers="", labels=[]),
        503: node(503),
        600: node(600, dep_at=old10), 601: node(601),
        610: node(610, updated=old8), 611: node(611),
        701: node(701), 801: node(801, assignees=(), updated=old5),
        901: node(901, state="CLOSED", reason="NOT_PLANNED"),
    })
    nodes[700]["dep_label_at"] = fresh
    gated = [gate(100, [(101, "default")], 1), gate(200, [(201, "default")]), gate(300, [(301, "closed")]),
             gate(310, [(311, "closed")]), gate(400, [(401, "closed")]), gate(500, [(503, "closed")]),
             gate(501, [(503, "closed")]), gate(502, [(503, "closed")]), gate(600, [(601, "closed")]),
             gate(610, [(611, "closed")]), gate(700, [(701, "closed")]), gate(800, [(801, "closed")]),
             gate(900, [(901, "complete")])]
    owned = {801: [9001]}
    hell, hotspots = dependency_hell_report(gated, nodes, owned, {9001: fresh}, now)
    by = {e["issue"]: e for e in hell}
    assert "CYCLE" in by[100]["reasons"] and by[100]["cycle"].startswith("#100→#101→#102→#101"), by.get(100)
    assert by[200]["reasons"] == ["DEEP"] and by[200]["chain"] == "#200→#201→#202→#203" and by[200]["root_blocker"] == 203
    assert by[300]["reasons"] == ["STALE_ROOT"] and by[300]["root_owner"] == "none"
    assert by[310]["reasons"] == ["STALE_ROOT"] and "idle" in by[310]["root_detail"]
    assert by[400]["reasons"] == ["EXTERNAL_ROOT"]
    assert by[600]["reasons"] == ["AGED"] and by[600]["gated_since_source"] == "label_event"
    assert by[610]["reasons"] == ["AGED"] and by[610]["gated_since_source"] == "updated_at_lower_bound"
    assert by[900]["reasons"] == ["DEAD_ROOT"]
    assert 700 not in by and 800 not in by, "fresh owned root / linked-PR owner must not flag"
    assert not {500, 501, 502} & set(by), "fanout alone is a hotspot, not per-issue noise"
    assert hotspots and hotspots[0]["root"] == 503 and hotspots[0]["count"] == 3 and hotspots[0]["gated_issues"] == [500, 501, 502]
    assert hell[0]["issue"] == 100, "P1 sorts first"
    targets = obligation_targets(hell, hotspots)
    assert set(targets) == {503} and "#500, #501, #502" in targets[503]["action"]
    assert targets[503]["severity"] == "normal"
    large = {**hotspots[0], "best_priority": "P1", "count": 12, "gated_issues": list(range(20, 32))}
    rolled = obligation_targets(hell, [large])[503]
    assert rolled["severity"] == "high" and "#29, +2 more" in rolled["action"] and "#30" not in rolled["action"]
    # depth bound + truncation
    chain = {n: node(n, blockers=f"#{n + 1}") for n in range(1000, 1010)}
    w = _walk(999, chain, {999: [(1000, "default")]}, DEP_HELL_MAX_DEPTH)
    assert w["truncated"] and max(len(p) - 1 for p in w["leaves"].values()) == DEP_HELL_MAX_DEPTH
    # batched level-wise fetch with dedupe
    calls = []

    def fake_details(nums):
        calls.append(list(nums))
        return {n: nodes.get(n) for n in nums}, 0
    with patch_attr("graphql_details", fake_details):
        got, failures = fetch_dependency_graph([gated[0], gated[1]])
    assert failures == 0 and {101, 102, 201, 202, 203} <= set(got) and len(calls) <= 4
    assert sum(len(c) for c in calls) == len({n for c in calls for n in c}), "nodes fetched once"
    # obligation sync: upsert + resolve cleared; incomplete analysis never resolves
    import tempfile
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    with tempfile.TemporaryDirectory() as tmp:
        st = Path(tmp) / "s.json"
        st.write_text(json.dumps({"targets": [42, 503]}))
        r = sync_obligations({503: targets[503]}, False, fake_run, st)
        assert r["upserted"] == 1 and r["resolved"] == 0 and "obligation-resolve" not in seen
        r = sync_obligations({503: targets[503]}, True, fake_run, st)
        assert r["resolved"] == 1 and seen.count("obligation-resolve") == 1
        assert json.loads(st.read_text())["targets"] == [503]


class patch_attr:
    """Tiny module-attribute patch for the offline selftest."""
    def __init__(self, name, value):
        self.name, self.value = name, value

    def __enter__(self):
        self.old = globals()[self.name]
        globals()[self.name] = self.value

    def __exit__(self, *exc):
        globals()[self.name] = self.old


def selftest() -> int:
    # Disk cache must not poison or skip the stubbed lookups.
    os.environ["GH_BUDGET_DISABLE"] = "1"
    from unittest.mock import patch
    assert parse_dependencies('[#10]', 'PR #10 merged') == [(10, 'default')]
    assert parse_dependencies('', '#10 closed and Rajiv approval') == [(10, 'closed')]
    assert prose_dependencies('Part of #55. blocked on #10 and #11') == [(10, 'closed'), (11, 'closed')]
    assert prose_dependencies('blocked on #12073–#12076') == [(n, 'closed') for n in range(12073, 12077)]
    assert prose_dependencies('parent #99; umbrella #98; Part of #97') == []
    assert prose_dependencies('after #10 merges') == [(10, 'closed')]
    for state, expected in [('CLOSED', True), ('OPEN', False)]:
        c = {'blocking_labels': ['pm-blocked:dependency'], 'blockers': '', 'wake_condition': '', 'issue_body': 'blocked by #10'}
        evaluate_dependency(c, {(10, 'closed'): (expected, state)})
        assert bool(c.get('dependency_unblocked')) == expected
        if expected:
            assert c['dependency_reason'] == 'dependency_resolved_from_body'
        else:
            assert c['open_dependencies'][0]['number'] == 10
    external = {'blocking_labels': ['pm-blocked:external'], 'issue_body': 'blocked on #10'}
    evaluate_dependency(external, {})
    assert is_gated(external) and not external.get('dependencies')
    assert parse_dependencies('', 'Blocked on #10 closed') == [(10, 'closed')]
    issue = {'number': 1, 'body': 'Related #99', 'labels': ['P1', 'pm-blocked:dependency']}
    assert not candidate(issue, 'new_issue')['wake_condition']
    states = {
        10: {'state': 'closed', 'state_reason': 'completed'},
        11: {'state': 'open', 'state_reason': None},
        12: {'state': 'closed', 'state_reason': 'not_planned'},
        13: {'state': 'closed', 'pull_request': {}},
        14: {'state': 'closed', 'pull_request': {}},
        15: {'state': 'bad'},
    }
    real_run = subprocess.run
    def stub(cmd, **kwargs):
        if any(str(part).endswith("validate-issue-contract-ledger.py") for part in cmd):
            return real_run(cmd, **kwargs)
        if cmd[1] == 'api':
            number = int(cmd[-1].split('/')[-1])
            if number == 16:
                raise OSError('unavailable')
            data = {'data': {'repository': {f'i{number}': states[number]}}}
        else:
            number = int(cmd[3])
            data = {'state': 'MERGED' if number == 13 else 'CLOSED', 'mergedAt': '2026-01-01T00:00:00Z' if number == 13 else None}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), '')
    with patch('subprocess.run', side_effect=stub):
        def check(text):
            c = {'blocking_labels': ['pm-blocked:dependency'], 'blockers': '', 'wake_condition': text}
            evaluate_dependency(c, {})
            return c
        assert check('#10 closed').get('dependency_unblocked')
        assert check('#10 completed').get('dependency_unblocked')
        assert not check('#11 closed').get('dependency_unblocked')
        assert check('#12 closed').get('dependency_unblocked')
        assert not check('#12 completed').get('dependency_unblocked')
        assert check('PR #13 merges').get('dependency_unblocked')
        assert not check('PR #14 merged').get('dependency_unblocked')
        assert check('#14 closed').get('dependency_unblocked')
        assert check('#15 closed')['open_dependencies'][0]['state'] == 'LOOKUP_FAILED'
        assert check('#16 closed')['open_dependencies'][0]['state'] == 'LOOKUP_FAILED'
        issues = [{'number': 1, 'labels': ['P0', 'pm-blocked:dependency'], 'body': '<!-- ready-pool:\nblockers: #11\n-->'}, {'number': 2, 'labels': ['P1', 'status:todo'], 'body': ''}]
        assert rank_candidates(issues) == [], "incomplete contracts cannot become picks"
        assert rank_candidates(issues, rework_only=True) == []
        assert rank_candidates([]) == []
    _dep_hell_selftest()
    print('selftest: PASS (dependency parsing + dependency hell: cycle, deep, stale root, external root, dead root, fanout, aged, depth bound, batched fetch, obligation sync)')
    return 0


CHECK_EXIT = {"ok": 0, "PRIORITY_INVERSION": 3, "DEPENDENCY_OPEN": 4, "GATED": 5, "PR_CAP": 6}


def check_decision(args, issues, extra, ranked, gated, rework_only, open_pr_count, cache) -> dict[str, Any]:
    """The --check verdict over an evaluated population (shared by full and fast paths)."""
    # Preserve the existing gated P0 response even at the cap.
    all_gated = gated_p0_candidates(issues, cache=cache, offline=args.no_dependency_check)
    requested_gated = next((c for c in all_gated if c["issue"] == args.check), None)
    uncapped = rank_candidates(issues, extra, cache=cache, offline=args.no_dependency_check)
    requested = next((c for c in uncapped if c["issue"] == args.check), None)
    if requested_gated:
        res = check_pick(args.check, ranked, [requested_gated])
    elif rework_only and requested and requested["class"] == "new_issue":
        res = {"status": "PR_CAP", "issue": args.check, "reason": "PR_CAP: new_issue is not permitted in rework-only mode", "open_pr_count": open_pr_count}
    else:
        res = check_pick(args.check, ranked, gated)
    if res["status"] == "ok" and args.selection_class and res["pick"]["class"] != args.selection_class:
        res = {"status": "not_eligible", "issue": args.check, "reason": "selection_class_mismatch"}
    return res


def evaluate_check(args, issues, extra, prs, cache) -> dict[str, Any]:
    """main()'s --check pipeline over a given issue population, unchanged."""
    if not args.no_dependency_check:
        prefetch_dependencies(issues, cache)
        prefetch_native(native_candidates(issues, extra), cache)
    open_pr_count = sum(not pr["isDraft"] for pr in prs)
    rework_only = args.rework_only or open_pr_count > 25
    owned, _ = owned_issues(prs)
    eligible_issues = [i for i in issues if classify(i) != "new_issue" or int(i["number"]) not in owned]
    eligible_extra = [r for r in extra if r.get("class", "rework") != "new_issue" or int(r["issue"]) not in owned]
    ranked = rank_candidates(eligible_issues, eligible_extra, rework_only, cache, args.no_dependency_check)
    gated = gated_p0_candidates(eligible_issues, rework_only, cache, args.no_dependency_check)
    res = check_decision(args, issues, extra, ranked, gated, rework_only, open_pr_count, cache)
    res["_rework_only"] = rework_only
    return res


def _light_row(raw: dict) -> dict:
    labels = raw.get("labels") or {}
    if (labels.get("pageInfo") or {}).get("hasNextPage") is not False:
        raise ValueError("incomplete candidate labels")
    return {**raw, "labels": labels["nodes"]}


def _load_light_candidates_uncached(timeout: int = 60) -> list[dict[str, Any]]:
    """Number/labels/createdAt only. Labels run together, 100 per page."""
    fields = "number state createdAt labels(first:100){nodes{name} pageInfo{hasNextPage}}"
    return _load_label_union(timeout, fields, _light_row, page_size=100, page_timeout=30, order_by=False)


def load_light_candidates(timeout: int = 60) -> list[dict[str, Any]]:
    """Label scan used by every --check. Shared with the full ranking's label set."""
    if os.environ.get("GH_BUDGET_DISABLE") == "1":
        return _load_light_candidates_uncached(timeout)
    try:
        rows, _live = gh_budget.serve(
            pool_cache_name("ready-pool-light"), SNAPSHOT_TTL, lambda: _load_light_candidates_uncached(timeout))
    except LookupError as exc:
        raise ValueError(str(exc)) from None
    return rows


def _cached_checked_issue(number: int) -> dict | None:
    payload, age = gh_budget.peek(pool_cache_name("ready-pool-open-issues"))
    if not isinstance(payload, list) or age is None or age > SNAPSHOT_TTL:
        return None
    if not isinstance(payload, list):
        return None
    for issue in payload:
        if isinstance(issue, dict) and int(issue.get("number") or 0) == number:
            return issue
    return None


def _warm_contracts(issues: list[dict[str, Any]]) -> None:
    """Fill the contract memo while GitHub round trips are in flight. Rank still decides."""
    for issue in issues:
        if classify(issue) == "new_issue":
            contract_reasons(issue)


def _issue_from_detail(raw: dict | None, number: int) -> dict | None:
    if raw is None:
        raise ValueError(f"candidate #{number} lookup failed")
    if isinstance(raw.get("labels"), list):
        return raw
    if "mergedAt" in raw:
        return None
    return candidate_node(raw)


def _fresh_snapshot() -> list | None:
    """Full candidate bodies from the shared TTL, or None when the snapshot is stale."""
    if os.environ.get("GH_BUDGET_DISABLE") == "1":
        return None
    payload, age = gh_budget.peek(pool_cache_name("ready-pool-open-issues"))
    if isinstance(payload, list) and age is not None and age <= SNAPSHOT_TTL:
        return payload
    return None


def fast_check(args) -> dict[str, Any]:
    """--check N without loading every issue body.

    The checked issue's dependency blockers and merged-PR association are always
    live (`__fresh_issues__`). The rest of the pool is the shared short-TTL
    snapshot, used only to see whether something sorts strictly better.
    Bodies for that inversion scan are fetched in sort order, 8 at a time,
    and the scan stops at the first eligible better candidate.
    """
    cache: dict = {_FRESH: {int(args.check)}}
    extra = []
    if args.selection_class and args.selection_class != "new_issue":
        extra.append({"issue": args.check, "class": args.selection_class, "pr": args.pr})
    low = os.environ.get("GH_BUDGET_DISABLE") != "1" and gh_budget.graphql_low()
    snap = _fresh_snapshot()
    snap_by = {int(row["number"]): row for row in snap} if snap else {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        light_fut = None if snap is not None else pool.submit(load_light_candidates)
        pr_fut = pool.submit(lambda: load_open_prs(fresh=True))
        if low:
            pr_fut.result()
            rem = gh_budget.graphql_remaining()
            return {"status": "LOOKUP_FAILED", "issue": args.check, "stale": True,
                    "reason": f"UNAVAILABLE: graphql remaining {rem} < {gh_budget.LOW_WATER}; "
                              "refusing a stale assignment check"}
        node = graphql_nodes([args.check], candidate_details=True).get(args.check)
        prs = pr_fut.result()
        if node is None:
            return {"status": "LOOKUP_FAILED", "issue": args.check, "reason": "issue lookup failed"}
        if "mergedAt" in node:
            return {"status": "not_eligible", "issue": args.check,
                    "reason": "issue is not an eligible Ready Pool/rework candidate"}
        picked = candidate_node(node)
        res = evaluate_check(args, [picked], extra, prs, cache)
        rework_only = res.pop("_rework_only")
        res = merge_after_receipt(res, picked, args.no_dependency_check)
        if res["status"] != "ok":
            return res
        light_rows = snap if snap is not None else light_fut.result()
    pick = res["pick"]
    pick_key = (pick["priority_rank"], CLASS_RANK.get(pick["class"], 9))
    labelled = []
    unlabelled = []
    for light in light_rows:
        if int(light["number"]) == args.check or classify(light, light=True) is None:
            continue
        labels = _labels(light)
        ranks = [PRIORITY_RANK[name] for name in labels if name in PRIORITY_RANK]
        cls = "rework" if labels & REWORK_LABELS else "new_issue"
        if rework_only and cls == "new_issue":
            continue
        if not ranks or (min(ranks), CLASS_RANK[cls]) < pick_key:
            if ranks:
                labelled.append((min(ranks), CLASS_RANK[cls], light.get("createdAt") or "~", int(light["number"])))
            else:
                unlabelled.append(int(light["number"]))
    labelled.sort()
    events = [((rank, cls_rank, created, number), "id", number)
              for rank, cls_rank, created, number in labelled]
    if unlabelled:
        raws = {} if snap_by else graphql_nodes(unlabelled, candidate_details=True)
        for number in unlabelled:
            issue = snap_by.get(number) if snap_by else _issue_from_detail(raws.get(number), number)
            if issue is None:
                continue
            cls = classify(issue)
            if cls is None:
                continue
            row = candidate(issue, cls)
            if (row["priority_rank"], CLASS_RANK.get(cls, 9)) < pick_key:
                events.append((sort_key(row), "issue", issue))
    events.sort(key=lambda item: item[0])
    evaluated = 0
    eligible_higher = 0
    for start in range(0, len(events), 8):
        chunk_events = events[start:start + 8]
        ids = [item[2] for item in chunk_events if item[1] == "id" and item[2] not in snap_by]
        raws = graphql_nodes(ids, candidate_details=True) if ids else {}
        chunk = []
        for kind, payload in ((item[1], item[2]) for item in chunk_events):
            if kind == "issue":
                chunk.append(payload)
                eligible_higher += 1
                continue
            issue = snap_by.get(payload) if payload in snap_by else _issue_from_detail(raws.get(payload), payload)
            if issue is None:
                continue
            cls = classify(issue)
            if cls is None:
                continue
            row = candidate(issue, cls)
            if (row["priority_rank"], CLASS_RANK.get(cls, 9)) < pick_key:
                chunk.append(issue)
                eligible_higher += 1
        evaluated += len(chunk_events)
        if not chunk:
            continue
        verdict = evaluate_check(args, [picked, *chunk], extra, prs, cache)
        verdict.pop("_rework_only", None)
        if verdict["status"] != "ok":
            verdict["fast_path"] = {"higher_candidates": eligible_higher, "evaluated": evaluated}
            verdict["merge_after_open"] = res["merge_after_open"]
            return verdict
    if res.get("priority_reason"):
        res["priority_reason"] = (f"priority_reason: {pick['priority']} {pick['class']} #{pick['issue']} taken because "
                                  f"no ready P0/P1 candidate exists ({eligible_higher} higher-priority candidates checked)")
    res["fast_path"] = {"higher_candidates": eligible_higher, "evaluated": evaluated}
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rework-only", action="store_true", help="WIP cap hit: existing-PR rework/repro only")
    ap.add_argument("--extra-json", help="PM-supplied rework/repro candidate rows (JSON list)")
    ap.add_argument("--issues-json", help="use this saved `gh issue list` JSON instead of live GitHub")
    ap.add_argument("--check", type=int, help="validate a PM pick; exit 3 on PRIORITY_INVERSION")
    ap.add_argument("--selection-class", choices=tuple(CLASS_RANK), help="assignment caller's existing work class")
    ap.add_argument("--pr", type=int, help="assignment caller's existing PR binding")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--no-dependency-check", action="store_true", help="leave dependencies gated without live lookups")
    ap.add_argument("--selftest", action="store_true", help="check dependency parsing without network access")
    ap.add_argument("--sync-obligations", action="store_true",
                    help="compatibility flag; dependency warnings do not mirror into pm-ops")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.selection_class and args.check is None:
        raise ValueError("selection-class requires --check")
    if args.check is not None and not args.issues_json and not args.extra_json:
        res = fast_check(args)
        if gh_budget.stale_info():
            res.setdefault("stale", True)
            res.setdefault("stale_reason", gh_budget.stale_info())
        print(json.dumps(res, sort_keys=True))
        return CHECK_EXIT.get(res["status"], 4)
    cache: dict = {}

    issues = json.load(open(args.issues_json)) if args.issues_json else load_open_issues()
    extra = json.load(open(args.extra_json)) if args.extra_json else []
    if args.selection_class:
        if args.check is None:
            raise ValueError("selection-class requires --check")
        if args.selection_class != "new_issue":
            extra.append({"issue": args.check, "class": args.selection_class, "pr": args.pr})
    missing_extra = sorted({int(r["issue"]) for r in extra} - {int(i["number"]) for i in issues})
    for node in graphql_nodes(missing_extra, candidate_details=True).values():
        if isinstance(node, dict) and "mergedAt" not in node:
            issues.append(candidate_node(node))
    prs = load_open_prs()
    prefetch_pool = None
    if not args.no_dependency_check:
        dep_box: dict = {}
        nat_box: dict = {}
        prefetch_pool = ThreadPoolExecutor(max_workers=2)
        deps = prefetch_pool.submit(prefetch_dependencies, issues, dep_box, snapshot=True)
        native = prefetch_pool.submit(prefetch_native, native_candidates(issues, extra), nat_box)
        deps.result()
        native.result()
        cache.update(dep_box)
        cache.update(nat_box)
        prefetch_pool.shutdown(wait=True)
    open_pr_count = sum(not pr["isDraft"] for pr in prs)
    rework_only = args.rework_only or open_pr_count > 25
    owned, uncertain = owned_issues(prs)
    # Existing PRs own new work, not rework/repro on that same PR.
    eligible_issues = [i for i in issues if classify(i) != "new_issue" or int(i["number"]) not in owned]
    eligible_extra = [r for r in extra if r.get("class", "rework") != "new_issue" or int(r["issue"]) not in owned]
    # GraphQL for dependency hell overlaps the contract scan inside rank. The fetch
    # walks every gated issue; contract rejects are dropped before the report.
    hell_pool = None
    hell_fetch = None
    if not args.no_dependency_check:
        hell_input = []
        for issue in eligible_issues:
            cls = classify(issue)
            if not cls or (cls == "new_issue" and issue.get("_implementation_merged")):
                continue
            row = candidate(issue, cls)
            evaluate_dependency(row, cache, False)
            if is_gated(row) and "pm-blocked:dependency" in row.get("blocking_labels", []):
                hell_input.append(row)
        hell_pool = ThreadPoolExecutor(max_workers=1)
        hell_fetch = hell_pool.submit(fetch_dependency_graph, hell_input)
    ranked = rank_candidates(eligible_issues, eligible_extra, rework_only, cache, args.no_dependency_check)
    gated = gated_p0_candidates(eligible_issues, rework_only, cache, args.no_dependency_check)
    if os.environ.get("GH_BUDGET_DISABLE") != "1":
        try:
            gh_budget.store(pool_cache_name("ready-pool-open-issues"), issues)
        except OSError:
            pass
    contract_defects = [
        {"issue": int(i["number"]), "reasons": reasons}
        for i in issues if classify(i) == "new_issue"
        if (reasons := contract_reasons(i))
    ]
    all_dependencies = []
    for issue in eligible_issues:
        cls = classify(issue)
        if not cls:
            continue
        if cls == "new_issue" and contract_reasons(issue):
            continue
        c = candidate(issue, cls)
        evaluate_dependency(c, cache, args.no_dependency_check)
        if 'pm-blocked:dependency' in c.get('blocking_labels', []):
            all_dependencies.append(c)
    dependency_gated = [c for c in all_dependencies if is_gated(c)]
    unblocked = [c for c in all_dependencies if c.get("dependency_unblocked")]
    gated_p0 = [c for c in gated if c["priority_rank"] == 0]

    if args.check is not None:
        res = check_decision(args, issues, extra, ranked, gated, rework_only, open_pr_count, cache)
        checked = next ( (i for i in issues if int(i["number"]) == args.check), None)
        res = merge_after_receipt(res, checked, args.no_dependency_check)
        print(json.dumps(res, sort_keys=True))
        return CHECK_EXIT.get(res["status"], 4)

    dependency_hell, dependency_hotspots, hell_meta = [], [], {"skipped": "offline"}
    if not args.no_dependency_check:
        started = datetime.now(timezone.utc)
        try:
            nodes, failures = hell_fetch.result() if hell_fetch is not None else fetch_dependency_graph(dependency_gated)
        finally:
            if hell_pool is not None:
                hell_pool.shutdown(wait=True)
        pr_updated = {int(pr["number"]): pr.get("updatedAt") or "" for pr in prs}
        dependency_hell, dependency_hotspots = dependency_hell_report(
            dependency_gated, nodes, owned, pr_updated, started)
        lookup_failed = failures or any(d.get("state") == "LOOKUP_FAILED" for c in dependency_gated
                                        for d in c.get("open_dependencies") or [])
        hell_meta = {"nodes_fetched": len(nodes), "batch_failures": failures,
                     "complete": not failures, "lookup_failed_dependencies": bool(lookup_failed),
                     "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1)}
    if args.sync_obligations:
        hell_meta["sync"] = {"skipped": "routine dependency tracking stays in the selector; no obligations written or resolved"}

    pick = ranked[0] if ranked else None
    print(json.dumps({
        "schema": "ready-pool-select/v1",
        "rule": "priority P0>P1>P2>P3, then rework>repro>new_issue, then oldest; unresolved gates are never picked",
        "rework_only": rework_only,
        "open_pr_count": open_pr_count,
        "uncertain_part_of_exclusions": uncertain,
        "pick": pick,
        "priority_reason": priority_reason(pick, ranked) if pick else "",
        "eligible_count": len(ranked),
        "contract_defect_count": len(contract_defects),
        "contract_defects": contract_defects,
        "ranked": ranked[: args.limit],
        "pool_count": len({c["issue"] for c in ranked + gated + all_dependencies}),
        "dependency_gated_count": len(dependency_gated),
        "dependency_gated": dependency_gated,
        "needs_dependency_declaration": [{"number": c["issue"], "priority": c["priority"], "title": c["title"]} for c in dependency_gated if c.get("dependency_reason") == "no_dependency_declared"],
        "now_unblocked_count": len(unblocked),
        "now_unblocked": unblocked,
        "gated_p0_count": len(gated_p0),
        "gated_p0": gated_p0,
        "dependency_hell_count": len(dependency_hell),
        "dependency_hell": dependency_hell,
        "dependency_hotspots": dependency_hotspots,
        "dependency_hell_meta": hell_meta,
        **({"stale": True, "stale_reason": gh_budget.stale_info()} if gh_budget.stale_info() else {}),
    }, indent=2, sort_keys=True))
    return 0 if pick else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TimeoutError:
        print(json.dumps({'status': 'not_eligible', 'reason': 'LOOKUP_TIMEOUT'}))
        sys.exit(4)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'not_eligible', 'reason': 'LOOKUP_FAILED', 'detail': str(exc)}))
        sys.exit(4)
