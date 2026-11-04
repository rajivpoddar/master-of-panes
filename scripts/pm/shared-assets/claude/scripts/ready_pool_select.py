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

Usage:
  ready_pool_select.py                 # ranked JSON, pick first
  ready_pool_select.py --rework-only   # WIP cap hit: existing-PR work only
  ready_pool_select.py --check 11462   # validate a PM pick before mop-assign-slot

Exit codes: 0 ok/pick, 1 no pick, 3 PRIORITY_INVERSION,
4 not_eligible/dependency open/LOOKUP_FAILED/LOOKUP_TIMEOUT, 5 other gate,
6 PR_CAP. --check has a 30-second total deadline; timeout emits JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
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


def parse_dependencies(blockers: str, wake: str) -> list[tuple[int, str]] | None:
    """Parse the entire supported condition, never just its recognizable fragments."""
    refs: dict[int, str] = {}
    pattern = re.compile(r"(?i)(?:(pr|pull request|issue)\s+)?#(\d+)(?:\s+(merge|merges|merged|merging|closed|complete|completed))?")
    for text in (blockers, wake):
        text = (text or '').strip()
        if text.lower() in ('', 'none', 'n/a', '[]'):
            continue
        text = re.sub(r'(?i)^blocked\s+on\s+', '', text)
        text = text.strip('[] ')
        parts = re.split(r'(?i)\s+and\s+|\s*,\s*', text)
        for part in parts:
            match = pattern.fullmatch(part.strip())
            if not match:
                return None
            prefix, number, condition = match.groups()
            condition = (condition or '').lower()
            kind = 'merge' if condition in ('merge', 'merges', 'merged', 'merging') or (not condition and (prefix or '').lower() in ('pr', 'pull request')) else 'complete' if condition in ('complete', 'completed') else 'closed'
            number = int(number)
            old = refs.get(number)
            if old and old != kind and old != 'closed' and kind != 'closed':
                return None
            if old != 'merge' and (kind != 'closed' or not old):
                refs[number] = kind
    return list(refs.items())


def evaluate_dependency(c: dict[str, Any], cache: dict, offline: bool = False) -> None:
    if "pm-blocked:dependency" not in c.get("blocking_labels", []):
        return
    refs = parse_dependencies(c.get("blockers", ""), c.get("wake_condition", ""))
    if not refs:
        c["dependency_reason"] = "unparsed_wake_condition"
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
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
                data = json.loads(p.stdout)
                if not isinstance(data, dict) or data.get('state') not in ('open', 'closed'):
                    raise ValueError('invalid issue state')
                is_pr = 'pull_request' in data
                if is_pr:
                    p = subprocess.run(['gh', 'pr', 'view', str(number), '-R', 'Scribie/heydonna-app', '--json', 'mergedAt,state'], capture_output=True, text=True, timeout=30, check=True)
                    pr = json.loads(p.stdout)
                    if not isinstance(pr, dict) or pr.get('state') not in ('OPEN', 'CLOSED', 'MERGED') or 'mergedAt' not in pr or (pr['mergedAt'] is not None and (not isinstance(pr['mergedAt'], str) or not pr['mergedAt'])):
                        raise ValueError('invalid PR state')
                    satisfied = bool(pr['mergedAt']) if kind in ('merge', 'complete') else pr['state'] in ('CLOSED', 'MERGED')
                    state = pr['state']
                else:
                    if data.get('state_reason') not in (None, 'completed', 'not_planned', 'reopened'):
                        raise ValueError('invalid issue completion state')
                    state = data['state'].upper()
                    satisfied = kind != 'merge' and state == 'CLOSED' and (kind != 'complete' or data.get('state_reason') == 'completed')
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
    else:
        c["dependency_reason"] = "other_blocking_labels"


def candidate(issue: dict[str, Any], cls: str) -> dict[str, Any]:
    labels = _labels(issue)
    body = issue.get("body") or ""
    rank = priority_rank(labels, body)
    wake = ready_pool_value(body, "wake_condition")
    blockers = ready_pool_value(body, "blockers")
    if not wake and not blockers:
        match = re.search(r'(?im)^\s*(?:[-*]\s*)?blocked\s+on\s+([^\n.]+)', body)
        wake = match.group(0).strip().lstrip('-* ').strip() if match else ''
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
            c = candidate(issue, cls)
            evaluate_dependency(c, cache, offline)
            if not is_gated(c):
                best[c["issue"]] = c
    for row in extra or []:
        cls = row.get("class", "rework")
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
    proc = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open", "--limit", "1000",
         "--json", "number,title,labels,body,createdAt"],
        capture_output=True, text=True, errors="replace", timeout=timeout, check=True,
    )
    return json.loads(proc.stdout or "[]")


def load_open_prs(timeout: int = 120) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--state", "open", "--limit", "1000",
         "--json", "number,isDraft,body"],
        capture_output=True, text=True, errors="replace", timeout=timeout, check=True,
    )
    return json.loads(proc.stdout)


def owned_issues(prs: list[dict[str, Any]]) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    """Conservatively exclude Part of references when remaining ACs are uncertain."""
    owned: dict[int, list[int]] = {}
    uncertain: dict[int, list[int]] = {}
    for pr in prs:
        body = pr.get("body") or ""
        for match in re.finditer(r"\b(closes|part\s+of)\s+#(\d+)\b", body, re.IGNORECASE):
            issue = int(match.group(2))
            if match.group(1).lower() != "closes":
                # An explicit remaining-AC statement means this is a partial slice.
                if re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:remaining|outstanding)\s+(?:acceptance criteria|ACs?)\s*:\s*(?!none\b|no\b|n/?a\b|$)\S", body):
                    continue
                uncertain.setdefault(issue, []).append(int(pr["number"]))
            owned.setdefault(issue, []).append(int(pr["number"]))
    return owned, uncertain


def selftest() -> int:
    from unittest.mock import patch
    assert parse_dependencies('[#10]', 'PR #10 merged') == [(10, 'merge')]
    assert parse_dependencies('', '#10 closed and Rajiv approval') is None
    assert parse_dependencies('Other/repo#10', '') is None
    assert parse_dependencies('', 'parent #10') is None
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
    def stub(cmd, **kwargs):
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
        assert rank_candidates(issues)[0]['issue'] == 2
        assert rank_candidates(issues, rework_only=True) == []
        assert rank_candidates([]) == []
    print('selftest: PASS')
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
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.check is not None:
        def deadline(_signum, _frame):
            raise TimeoutError('LOOKUP_TIMEOUT')
        signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, 30)
    cache: dict = {}

    issues = json.load(open(args.issues_json)) if args.issues_json else load_open_issues()
    extra = json.load(open(args.extra_json)) if args.extra_json else []
    prs = load_open_prs()
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
    for issue in issues:
        if "pm-blocked:dependency" not in _labels(issue):
            continue
        cls = classify(issue)
        if not cls:
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
        "ranked": ranked[: args.limit],
        "pool_count": len({c["issue"] for c in ranked + gated + all_dependencies}),
        "dependency_gated_count": len(dependency_gated),
        "dependency_gated": dependency_gated,
        "now_unblocked_count": len(unblocked),
        "now_unblocked": unblocked,
        "gated_p0_count": len(gated_p0),
        "gated_p0": gated_p0,
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
