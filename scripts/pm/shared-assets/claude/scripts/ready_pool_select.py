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

Dependency-blocked candidates of all priorities are listed in the pool. Their
referenced issues and PRs are checked live before picking or checking; unresolved
or unparseable dependencies remain gated. Other P0 gates remain gated. Offline
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
6 PR_CAP. GitHub lookups use bounded timeouts and one retry.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = "heydonna-app/heydonna-app"
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
UNKNOWN_RANK = 9
CLASS_RANK = {"rework": 0, "repro": 1, "new_issue": 2}
REWORK_LABELS = {"status:qa-rework", "pm-state:qa-failed-rework", "pm-state:blocked-rework"}
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


def contract_reasons(issue: dict[str, Any]) -> list[str]:
    issue = {**issue, "labels": [{"name": name} for name in _labels(issue)]}
    return contract_policy().ready_pool_contract_reasons(
        issue, set(), allow_backlog="status:todo" not in _labels(issue),
        allow_dependencies=True,
    )


def _labels(issue: dict[str, Any]) -> set[str]:
    out = set()
    for item in issue.get("labels") or []:
        out.add(item.get("name", "") if isinstance(item, dict) else str(item))
    return out


def ready_pool_value(body: str, field: str) -> str:
    m = READY_POOL_RE.search(body or "")
    if not m:
        return ""
    f = re.search(rf"(?mi)^\s*{re.escape(field)}\s*:\s*(.*?)\s*$", m.group(1))
    return f.group(1).strip() if f else ""


def priority_rank(labels: set[str], body: str = "") -> int:
    """Label wins; Ready Pool `priority:` is the fallback; unknown sorts last."""
    found = sorted(PRIORITY_RANK[name] for name in labels if name in PRIORITY_RANK)
    if found:
        return found[0]
    meta = ready_pool_value(body, "priority").upper()
    return PRIORITY_RANK.get(meta, UNKNOWN_RANK)


def prio_name(rank: int) -> str:
    return f"P{rank}" if rank in PRIORITY_RANK.values() else "P?"


def classify(issue: dict[str, Any]) -> str | None:
    """Return the selection class for an eligible issue, else None."""
    labels = _labels(issue)
    excluded = EXCLUDE_LABELS - ({"status:blocked"} if "pm-blocked:dependency" in labels else set())
    if labels & excluded or any(l.startswith("slot:") for l in labels):
        return None
    if blocking_labels(labels) and "P0" not in labels and "pm-blocked:dependency" not in labels:
        return None
    if ready_pool_value(issue.get("body") or "", "claimable_slot_type").lower() in NON_SLOT_TYPES:
        return None
    if labels & REWORK_LABELS:
        return "rework"
    if "status:todo" in labels or "pm-blocked:dependency" in labels:
        return "new_issue"
    return None


def blocking_labels(labels: set[str]) -> list[str]:
    return sorted(l for l in labels if l.startswith("pm-blocked:"))


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
        if line[match.start():].lower().startswith('after') and not re.search(r'(?i)\b(?:merg\w*|clos\w*|land\w*)\b', clause):
            continue
        clause = re.sub(r'#(\d+)\s*[–—-]\s*#(\d+)', lambda m: ' '.join('#'+str(n) for n in range(int(m[1]), min(int(m[2]), int(m[1])+100)+1)), clause)
        for num in _WAKE_REF_RE.findall(clause):
            refs[int(num)] = 'closed'
    return list(refs.items())


def run_lookup(cmd: list[str], timeout: int = 45) -> subprocess.CompletedProcess:
    for attempt in range(2):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        except (OSError, subprocess.SubprocessError):
            if attempt:
                raise
    raise RuntimeError('unreachable')


def evaluate_dependency(c: dict[str, Any], cache: dict, offline: bool = False) -> None:
    issue_body = c.pop('issue_body', '')
    comment_text = c.pop('comment_text', '')
    # Canonical blockers are authoritative even when an advisory label is missing.
    if str(c.get("blockers") or "").strip().lower() not in {"", "none", "null", "n/a", "[]"}:
        c["blocking_labels"] = sorted(set(c.get("blocking_labels") or []) | {"pm-blocked:dependency"})
    if "pm-blocked:dependency" not in c.get("blocking_labels", []):
        return
    refs = parse_dependencies(c.get("blockers", ""), c.get("wake_condition", ""))
    from_body = False
    if not refs and "pm-blocked:dependency" in c.get("blocking_labels", []):
        refs = prose_dependencies(issue_body + '\n' + comment_text)
        from_body = bool(refs)
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
    for number, kind in refs:
        key = (number, kind)
        if key not in cache:
            try:
                cmd = ["gh", "api", f"repos/Scribie/heydonna-app/issues/{number}"]
                p = run_lookup(cmd)
                data = json.loads(p.stdout)
                if not isinstance(data, dict) or data.get('state') not in ('open', 'closed'):
                    raise ValueError('invalid issue state')
                is_pr = 'pull_request' in data
                if is_pr:
                    p = run_lookup(['gh', 'pr', 'view', str(number), '-R', 'Scribie/heydonna-app', '--json', 'mergedAt,state'])
                    pr = json.loads(p.stdout)
                    if not isinstance(pr, dict) or pr.get('state') not in ('OPEN', 'CLOSED', 'MERGED') or 'mergedAt' not in pr or (pr['mergedAt'] is not None and (not isinstance(pr['mergedAt'], str) or not pr['mergedAt'])):
                        raise ValueError('invalid PR state')
                    satisfied = bool(pr['mergedAt']) if kind in ('merge', 'complete', 'default') else pr['state'] in ('CLOSED', 'MERGED')
                    state = pr['state']
                else:
                    if data.get('state_reason') not in (None, 'completed', 'not_planned', 'reopened'):
                        raise ValueError('invalid issue completion state')
                    state = data['state'].upper()
                    satisfied = kind != 'merge' and state == 'CLOSED' and (kind not in ('complete', 'default') or data.get('state_reason') == 'completed')
                cache[key] = (satisfied, state)
            except TimeoutError:
                raise
            except (OSError, subprocess.SubprocessError, ValueError, TypeError):
                cache[key] = (False, "LOOKUP_FAILED")
        satisfied, state = cache[key]
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


def graphql_nodes(numbers: list[int], comments: bool = False) -> dict[int, dict]:
    result = {}
    for start in range(0, len(numbers), 50):
        batch = numbers[start:start + 50]
        fields = '... on Issue { number state stateReason comments(last: 20) { nodes { body } } }' if comments else '... on Issue { number state stateReason } ... on PullRequest { number state mergedAt }'
        aliases = ' '.join(f'i{n}: issueOrPullRequest(number:{n}) {{ {fields} }}' for n in batch)
        query = '{ repository(owner:"Scribie",name:"heydonna-app") { ' + aliases + ' } }'
        try:
            payload = json.loads(run_lookup(['gh', 'api', 'graphql', '-f', 'query=' + query]).stdout)
            nodes = payload['data']['repository']
            if payload.get('errors') or not isinstance(nodes, dict):
                raise ValueError('GraphQL errors')
            result.update({n: nodes.get(f'i{n}') for n in batch})
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            result.update({n: None for n in batch})
    return result


def prefetch_dependencies(issues: list[dict], cache: dict) -> None:
    missing = []
    for issue in issues:
        if 'pm-blocked:dependency' not in _labels(issue):
            continue
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
        state = node.get('state', '')
        if 'mergedAt' in node:
            satisfied = bool(node['mergedAt']) if kind in ('merge', 'complete', 'default') else state in ('CLOSED', 'MERGED')
        else:
            satisfied = kind != 'merge' and state == 'CLOSED' and (kind not in ('complete', 'default') or node.get('stateReason') == 'COMPLETED')
        cache[(number, kind)] = (satisfied, state or 'LOOKUP_FAILED')


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
        "blocking_labels": blocking_labels(labels),
        "issue_body": body if "pm-blocked:dependency" in labels else "",
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
    gated = {c["issue"] for c in gated_p0_candidates(issues, rework_only, cache, offline)}
    for issue in issues:
        cls = classify(issue)
        if cls:
            if cls == "new_issue" and contract_reasons(issue):
                continue
            c = candidate(issue, cls)
            evaluate_dependency(c, cache, offline)
            if not is_gated(c):
                best[c["issue"]] = c
    for row in extra or []:
        cls = row.get("class", "rework")
        if cls == "new_issue":
            # A PM-supplied row cannot bypass the live issue contract.
            source = next((i for i in issues if int(i["number"]) == int(row["issue"])), None)
            if source is None or classify(source) != "new_issue" or contract_reasons(source):
                continue
            c = candidate(source, cls)
            evaluate_dependency(c, cache, offline)
            if is_gated(c):
                continue
        else:
            rank = PRIORITY_RANK.get(str(row.get("priority", "")).upper(), UNKNOWN_RANK)
            c = {"issue": int(row["issue"]), "class": cls, "priority": prio_name(rank), "priority_rank": rank,
                 "created_at": row.get("created_at", ""), "title": row.get("title", ""),
                 **({"pr": row["pr"]} if row.get("pr") else {})}
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


def load_open_issues(timeout: int = 120) -> list[dict[str, Any]]:
    proc = run_lookup(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open", "--limit", "1000",
         "--json", "number,title,labels,body,createdAt"], timeout=timeout,
    )
    return json.loads(proc.stdout or "[]")


def load_open_prs(timeout: int = 120) -> list[dict[str, Any]]:
    proc = run_lookup(
        ["gh", "pr", "list", "--repo", REPO, "--state", "open", "--limit", "1000",
         "--json", "number,isDraft,body,updatedAt,closingIssuesReferences"], timeout=timeout,
    )
    return json.loads(proc.stdout)


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
        "dep_label_at": dep_at,
    }


def graphql_details(numbers: list[int]) -> tuple[dict[int, dict | None], int]:
    """Batched detail lookup (25/batch, one retry). Partial GraphQL data is kept: a
    deleted/transferred number comes back null with an error but must not void its batch."""
    result: dict[int, dict | None] = {}
    failures = 0
    for start in range(0, len(numbers), 25):
        batch = numbers[start:start + 25]
        aliases = " ".join(f"i{n}: issueOrPullRequest(number:{n}) {{ {_ISSUE_DETAIL} {_PR_DETAIL} }}" for n in batch)
        query = '{ repository(owner:"Scribie",name:"heydonna-app") { ' + aliases + " } }"
        repo = None
        for _ in range(2):
            try:
                p = subprocess.run(["gh", "api", "graphql", "-f", "query=" + query],
                                   capture_output=True, text=True, timeout=60)
                repo = (json.loads(p.stdout or "{}").get("data") or {}).get("repository")
                if isinstance(repo, dict):
                    break
            except (OSError, subprocess.SubprocessError, ValueError):
                repo = None
        if not isinstance(repo, dict):
            failures += 1
            result.update({n: None for n in batch})
            continue
        for n in batch:
            result[n] = normalize_node(repo.get(f"i{n}"))
    return result, failures


def node_satisfied(node: dict | None, kind: str) -> bool:
    """Same satisfaction semantics as prefetch_dependencies()."""
    if not node:
        return False
    if node["is_pr"]:
        return bool(node["merged_at"]) if kind in ("merge", "complete", "default") else node["state"] in ("CLOSED", "MERGED")
    return kind != "merge" and node["state"] == "CLOSED" and (
        kind not in ("complete", "default") or node.get("state_reason") == "COMPLETED")


def declared_deps(node: dict | None) -> list[tuple[int, str]]:
    """Declared blockers of an OPEN issue node, mirroring evaluate_dependency() (structured
    `blockers:` or a pm-blocked:dependency label; prose body fallback). Comments are not read
    for transitive nodes. PRs and closed nodes are leaves."""
    if not node or node["is_pr"] or node["state"] != "OPEN":
        return []
    body, labels = node["body"], set(node["labels"])
    blockers = ready_pool_value(body, "blockers")
    if blockers.strip().lower() in NONE_VALUES and "pm-blocked:dependency" not in labels:
        return []
    refs = parse_dependencies(blockers, ready_pool_value(body, "wake_condition"))
    if not refs and "pm-blocked:dependency" in labels:
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
            data = states[number]
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rework-only", action="store_true", help="WIP cap hit: existing-PR rework/repro only")
    ap.add_argument("--extra-json", help="PM-supplied rework/repro candidate rows (JSON list)")
    ap.add_argument("--issues-json", help="use this saved `gh issue list` JSON instead of live GitHub")
    ap.add_argument("--check", type=int, help="validate a PM pick; exit 3 on PRIORITY_INVERSION")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--no-dependency-check", action="store_true", help="leave dependencies gated without live lookups")
    ap.add_argument("--selftest", action="store_true", help="check dependency parsing without network access")
    ap.add_argument("--sync-obligations", action="store_true",
                    help="compatibility flag; dependency warnings do not mirror into pm-ops")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    cache: dict = {}

    issues = json.load(open(args.issues_json)) if args.issues_json else load_open_issues()
    extra = json.load(open(args.extra_json)) if args.extra_json else []
    prs = load_open_prs()
    contract_defects = [
        {"issue": int(i["number"]), "reasons": reasons}
        for i in issues if classify(i) == "new_issue"
        if (reasons := contract_reasons(i))
    ]
    if not args.no_dependency_check:
        prefetch_dependencies(issues, cache)
    open_pr_count = sum(not pr["isDraft"] for pr in prs)
    rework_only = args.rework_only or open_pr_count > 25
    owned, uncertain = owned_issues(prs)
    # Existing PRs own new work, not rework/repro on that same PR.
    eligible_issues = [i for i in issues if classify(i) != "new_issue" or int(i["number"]) not in owned]
    blocked_issue_numbers = {int(i["number"]) for i in issues if blocking_labels(_labels(i))}
    eligible_extra = [r for r in extra if int(r["issue"]) not in blocked_issue_numbers
                      and (r.get("class", "rework") != "new_issue" or int(r["issue"]) not in owned)]
    ranked = rank_candidates(eligible_issues, eligible_extra, rework_only, cache, args.no_dependency_check)
    gated = gated_p0_candidates(eligible_issues, rework_only, cache, args.no_dependency_check)
    all_dependencies = []
    for issue in eligible_issues:
        if "pm-blocked:dependency" not in _labels(issue) and ready_pool_value(issue.get("body") or "", "blockers").strip().lower() in {"", "none", "null", "n/a", "[]"}:
            continue
        cls = classify(issue)
        if not cls:
            continue
        if cls == "new_issue" and contract_reasons(issue):
            continue
        c = candidate(issue, cls)
        evaluate_dependency(c, cache, args.no_dependency_check)
        all_dependencies.append(c)
    dependency_gated = [c for c in all_dependencies if is_gated(c)]
    unblocked = [c for c in all_dependencies if c.get("dependency_unblocked")]
    gated_p0 = [c for c in gated if c["priority_rank"] == 0]

    if args.check is not None:
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
        print(json.dumps(res, sort_keys=True))
        return {"ok": 0, "PRIORITY_INVERSION": 3, "DEPENDENCY_OPEN": 4, "GATED": 5, "PR_CAP": 6}.get(res["status"], 4)

    dependency_hell, dependency_hotspots, hell_meta = [], [], {"skipped": "offline"}
    if not args.no_dependency_check:
        started = datetime.now(timezone.utc)
        nodes, failures = fetch_dependency_graph(dependency_gated)
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
    }, indent=2, sort_keys=True))
    return 0 if pick else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TimeoutError:
        print(json.dumps({'status': 'not_eligible', 'reason': 'LOOKUP_TIMEOUT'}))
        sys.exit(4)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(json.dumps({'status': 'not_eligible', 'reason': 'LOOKUP_FAILED', 'detail': str(exc)}))
        sys.exit(4)
