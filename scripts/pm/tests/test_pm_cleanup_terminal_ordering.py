#!/usr/bin/env python3
"""Focused proof: closed-clean only after verified effects (obligation 18410).

Hermetic: FakeExternal holds in-memory GitHub/Slack state; mapping/receipt
paths are temp files. No network, no gh, no Slack, no slots.
"""

import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "shared-assets", "claude", "scripts", "pm-cleanup-pr.py")


def load():
    spec = importlib.util.spec_from_file_location("cleanup_under_test", os.path.realpath(SRC))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HEAD = "a" * 40
MERGE = "b" * 40
RESULTS = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
    else:
        RESULTS.append((name, True, ""))


class FakeExternal:
    def __init__(self, mod, *, pr_state="MERGED", refs="linked", issue_state="OPEN"):
        self.C = mod.CleanupError
        self.pr = {"number": 7801, "state": pr_state,
                   "mergeCommit": {"oid": MERGE}, "headRefOid": HEAD,
                   "closingIssuesReferences": ([{"number": 7701}] if refs == "linked"
                       else ([{"number": 9999}] if refs == "other" else [])),
                   "labels": [{"name": "ci-head:old"}, {"name": "status:in-progress"},
                              {"name": "priority:p0"}]}
        self.issue = {"number": 7701, "state": issue_state,
                      "stateReason": "COMPLETED" if issue_state == "CLOSED" else "REOPENED",
                      "labels": [{"name": "status:todo"}]}
        self.writes = []
        self.thread = [{"ts": "1789000000.000001", "user": "U0BNFGX2UAX",
                        "channel": "C0ALZJHGE49", "text": "root"}]
        self.fail_next_add = False

    def read_pr(self, request):
        return json.loads(json.dumps(self.pr))

    def read_issue(self, request):
        return json.loads(json.dumps(self.issue))

    def _mut(self, store, name, op, label=""):
        if op == "add" and self.fail_next_add:
            self.fail_next_add = False
            return  # applied nothing: readback will mismatch
        labels = store["labels"]
        if op == "add":
            if not any(l["name"] == label for l in labels):
                labels.append({"name": label})
        else:
            store["labels"] = [l for l in labels if l["name"] != label]
        self.writes.append((name, op, label))

    def add_pr_label(self, request, label):
        self._mut(self.pr, "pr", "add", label)

    def remove_pr_label(self, request, label):
        self._mut(self.pr, "pr", "remove", label)

    def add_issue_label(self, request, label):
        self._mut(self.issue, "issue", "add", label)

    def remove_issue_label(self, request, label):
        self._mut(self.issue, "issue", "remove", label)

    def close_issue(self, request):
        self.issue["state"] = "CLOSED"
        self.issue["stateReason"] = "COMPLETED"
        self.writes.append(("issue", "close", ""))

    def slack_auth(self, token):
        return {"ok": True, "user_id": "U0BNFGX2UAX"}

    def slack_replies(self, token, thread_ts):
        return {"ok": True, "messages": [dict(m) for m in self.thread]}

    def slack_post(self, token, thread_ts, text):
        self.thread.append({"ts": "1789000001.000001", "user": "U0BNFGX2UAX",
                            "channel": "C0ALZJHGE49", "thread_ts": thread_ts, "text": text})
        self.writes.append(("slack", "reply", thread_ts))
        return {"ok": True}


def ctx(mod):
    tmp = tempfile.mkdtemp(prefix="cleanup18410-")
    mapping = os.path.join(tmp, "mapping.json")
    receipt = os.path.join(tmp, "receipts.json")
    with open(mapping, "w") as f:
        json.dump({"m1": {"repository_id": "heydonna-app/heydonna-app", "repository": "heydonna-app/heydonna-app",
                           "issue": 7701, "pr": 7801, "head_sha": HEAD,
                           "thread_ts": "1789000000.000001", "status": "parent_created"}}, f)
    return mapping, receipt


def base_req(**kw):
    req = {"repository": "heydonna-app/heydonna-app", "pr": 7801, "issue": 7701,
           "head": HEAD, "merge_commit": MERGE, "cleanup_mode": "linked_issue"}
    req.update(kw)
    return req


def t1_linked_effects_before_clean(mod):
    mapping, receipt = ctx(mod)
    ext = FakeExternal(mod)
    out = mod.run(base_req(), mapping_path=__import__("pathlib").Path(mapping),
                  receipt_path=__import__("pathlib").Path(receipt), external=ext,
                  cto_slack_token="x")
    assert out == {**out, "success": True, "status": "completed"}, out
    assert "issue_residue" not in out, out
    assert ext.issue["state"] == "CLOSED" and ext.issue["stateReason"] == "COMPLETED"
    pr_names = [l["name"] for l in ext.pr["labels"]]
    assert "pm-state:closed-clean" in pr_names and "status:done" in [l["name"] for l in ext.issue["labels"]]
    assert "priority:p0" in pr_names


def t2_issue_less_label_only(mod):
    from pathlib import Path
    _, receipt = ctx(mod)
    ext = FakeExternal(mod, refs="empty")
    out = mod.run(base_req(cleanup_mode="merged_pr_issue_less", issue=None),
                  mapping_path=Path("/nonexistent/mapping.json"),
                  receipt_path=Path(receipt), external=ext, cto_slack_token="x")
    assert out["status"] == "completed", out
    assert [l["name"] for l in ext.pr["labels"]].count("pm-state:closed-clean") == 1
    assert not any(w[0] == "issue" or w[0] == "slack" for w in ext.writes), ext.writes


def t3_named_unlinked_residue(mod):
    from pathlib import Path
    _, receipt = ctx(mod)
    ext = FakeExternal(mod, refs="empty")
    out = mod.run(base_req(), mapping_path=Path("/nonexistent/mapping.json"),
                  receipt_path=Path(receipt), external=ext, cto_slack_token="x")
    assert out["status"] == "completed" and out.get("issue_residue") == "named-issue-unreconciled", out
    assert "pm-state:closed-clean" in [l["name"] for l in ext.pr["labels"]]
    assert ext.issue["state"] == "OPEN", ext.issue
    assert not any(w[0] == "issue" or w[0] == "slack" for w in ext.writes), ext.writes
    # retry is idempotent and preserves the residue + unrelated labels
    out2 = mod.run(base_req(), mapping_path=Path("/nonexistent/mapping.json"),
                   receipt_path=Path(receipt), external=ext, cto_slack_token="x")
    assert out2.get("idempotent") is True and out2.get("issue_residue") == "named-issue-unreconciled", out2
    assert "priority:p0" in [l["name"] for l in ext.pr["labels"]]


def t4_authoritative_disagreement_fail_closed(mod):
    from pathlib import Path
    mapping, receipt = ctx(mod)
    ext = FakeExternal(mod, refs="other")
    try:
        mod.run(base_req(issue_authoritative=True), mapping_path=Path(mapping),
                receipt_path=Path(receipt), external=ext, cto_slack_token="x")
    except mod.CleanupError as exc:
        assert exc.reason == "linked_issue_mismatch", exc.reason
    else:
        raise AssertionError("authoritative disagreement must fail closed")
    assert not any(w[0] == "issue" for w in ext.writes), ext.writes
    assert "pm-state:closed-clean" not in [l["name"] for l in ext.pr["labels"]]


def t5_failure_no_clean_terminal(mod):
    from pathlib import Path
    _, receipt = ctx(mod)
    ext = FakeExternal(mod, refs="empty")
    ext.fail_next_add = True
    try:
        mod.run(base_req(), mapping_path=Path("/nonexistent/mapping.json"),
                receipt_path=Path(receipt), external=ext, cto_slack_token="x")
    except mod.CleanupError as exc:
        assert exc.reason == "cleanup_ambiguous", exc.reason
    else:
        raise AssertionError("failed effect must not emit a clean terminal")
    assert "pm-state:closed-clean" not in [l["name"] for l in ext.pr["labels"]]
    writes_after_fail = list(ext.writes)
    # Retry is idempotent by design (started effects stay ambiguous, never
    # re-executed): still no clean terminal, zero new effects, labels kept.
    ext.fail_next_add = False
    try:
        mod.run(base_req(), mapping_path=Path("/nonexistent/mapping.json"),
                receipt_path=Path(receipt), external=ext, cto_slack_token="x")
    except mod.CleanupError as exc2:
        assert exc2.reason == "cleanup_ambiguous", exc2.reason
    else:
        raise AssertionError("retry must not emit a clean terminal either")
    assert ext.writes == writes_after_fail, ext.writes
    assert "priority:p0" in [l["name"] for l in ext.pr["labels"]]
    assert "pm-state:closed-clean" not in [l["name"] for l in ext.pr["labels"]]


MOD = load()
for _n, _f in sorted([(k, v) for k, v in list(globals().items())
                      if len(k) > 1 and k[0] == "t" and k[1].isdigit()]):
    check(_n, lambda _f=_f: _f(MOD))
failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else " :: " + detail))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
