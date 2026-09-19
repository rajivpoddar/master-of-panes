#!/usr/bin/env python3
"""Focused hermetic tests for the admit.py fetch-timeout verified-local escape.

No network, no gh, no filesystem mutation outside temp dirs. `command` is
stubbed per-test; local-object proof uses a real throwaway git repo fixture.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path):
    spec = importlib.util.spec_from_file_location("admit_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CANDIDATE = os.path.join(HERE, "admit.py")
PREIMAGE = "/Users/rajiv/.codex/skills/heydonna-cto-label-gated-ci/scripts/admit.py"

HEAD = "a" * 40
MAIN = "b" * 40
OTHER = "c" * 40
BRANCH = "fix/x"


def make_repo():
    d = tempfile.mkdtemp(prefix="admit-fixture-")
    subprocess.run(["git", "init", "--quiet", d], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    open(os.path.join(d, "f"), "w").write("1")
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "--quiet", "-m", "one"], check=True)
    open(os.path.join(d, "f"), "w").write("2")
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "--quiet", "-m", "two"], check=True)
    h = subprocess.run(["git", "-C", d, "rev-parse", "HEAD~1"], capture_output=True, text=True, check=True).stdout.strip()
    m = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return d, h, m


class Scripted:
    """Stub for admit.command with per-test scripting + full call log."""

    def __init__(self, mod, fetch="timeout", ls_main=None, ls_head=None, rest=""):
        self.mod = mod
        self.calls = []
        self.fetch = fetch  # "timeout" | "refuse" | "ok" | "timeout-twice"
        self.ls_main = ls_main
        self.ls_head = ls_head
        self.rest = rest
        self.fetch_count = 0

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs.get("timeout")))
        argv = list(args)
        if argv[:2] == ["git", "fetch"]:
            self.fetch_count += 1
            if argv[3] != "origin":
                return ""  # local-store fetch: hermetic success
            if self.fetch == "timeout" or (self.fetch == "timeout-twice"):
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            if self.fetch == "refuse":
                raise self.mod.Refusal("fetch failed: connection reset")
            return ""
        if argv[:2] == ["git", "ls-remote"]:
            ref = argv[3]
            if "main" in ref:
                if self.ls_main == "timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
                if self.ls_main == "ambiguous":
                    return "d" * 40 + "\trefs/heads/main\n" + "e" * 40 + "\trefs/heads/main2\n"
                return (self.ls_main or "") + "\trefs/heads/main\n"
            if isinstance(self.ls_head, Exception):
                raise self.ls_head
            if self.ls_head == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return (self.ls_head or "") + f"\trefs/heads/{BRANCH}\n"
        return self.rest

    def ls_remote_calls(self):
        return [c for c in self.calls if list(c[0])[:2] == ["git", "ls-remote"]]

    def network_fetches(self):
        return [c for c in self.calls if list(c[0])[:2] == ["git", "fetch"] and list(c[0])[3] == "origin"]


RESULTS = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
    else:
        RESULTS.append((name, True, ""))


def expect_refusal(mod, fn, fragment):
    try:
        fn()
    except mod.Refusal as exc:
        assert fragment in str(exc), f"want {fragment!r} in {exc}"
        return str(exc)
    raise AssertionError(f"expected Refusal containing {fragment!r}")


def with_mod(path, fn):
    mod = load(path)
    saved = dict(os.environ)
    try:
        fn(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def t1_timeout_verified_local_reaches_integration(mod):
    d, h, m = make_repo()
    git_dir = os.path.join(d, ".git")
    s = Scripted(mod, fetch="timeout", ls_main=m, ls_head=h)
    mod.command = s
    mod._same_repo_objects_dir = lambda: os.path.join(git_dir, "objects")
    mod._is_partial_clone = lambda: False
    mod._reuseable_alternate = lambda *a: None
    out = mod._fetch_head_and_main("/tmp/checkout", h, m, BRANCH)
    assert out == ""
    assert len(s.ls_remote_calls()) == 2, s.calls
    local = [c for c in s.calls if list(c[0])[:2] == ["git", "fetch"] and list(c[0])[3] == git_dir]
    assert len(local) == 1, s.calls


def t2_timeout_missing_object_refuses(mod):
    d, h, m = make_repo()
    os.environ["ADMIT_FETCH_TIMEOUT_S"] = "60"
    os.environ["ADMIT_TARGETED_FETCH_TIMEOUT_S"] = "60"
    s = Scripted(mod, fetch="timeout", ls_main=m, ls_head=h)
    mod.command = s
    mod._same_repo_objects_dir = lambda: os.path.join(d, ".git", "objects")
    mod._is_partial_clone = lambda: False
    mod._reuseable_alternate = lambda *a: None
    expect_refusal(mod, lambda: mod._fetch_head_and_main("/tmp/checkout", "d" * 40, m, BRANCH),
                   "FETCH_TIMEOUT_EXCEEDED")
    assert s.ls_remote_calls() == [], s.calls  # refused before any remote proof


def t2b_timeout_missing_object_resumes_network(mod):
    d, h, m = make_repo()
    s = Scripted(mod, fetch="timeout-twice", ls_main=m, ls_head=h)
    calls = {"n": 0}

    def fake(*args, **kwargs):
        argv = list(args)
        if argv[:2] == ["git", "fetch"]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return ""  # resumed network fetch succeeds: envelope preserved
        return Scripted.__call__(s, *args, **kwargs)

    mod.command = fake
    mod._same_repo_objects_dir = lambda: os.path.join(d, ".git", "objects")
    mod._is_partial_clone = lambda: False
    mod._reuseable_alternate = lambda *a: None
    assert mod._fetch_head_and_main("/tmp/checkout", "d" * 40, m, BRANCH) == ""
    assert calls["n"] == 2


def t3_timeout_head_drift_refuses(mod):
    d, h, m = make_repo()
    git_dir = os.path.join(d, ".git")
    s = Scripted(mod, fetch="timeout", ls_main=m, ls_head=OTHER)
    mod.command = s
    mod._same_repo_objects_dir = lambda: os.path.join(git_dir, "objects")
    mod._is_partial_clone = lambda: False
    mod._reuseable_alternate = lambda *a: None
    expect_refusal(mod, lambda: mod._fetch_head_and_main("/tmp/checkout", h, m, BRANCH),
                   "REMOTE_HEAD_DRIFT")
    assert not [c for c in s.calls if list(c[0])[:2] == ["git", "fetch"] and list(c[0])[3] == git_dir]


def t3b_timeout_main_drift_refuses(mod):
    d, h, m = make_repo()
    git_dir = os.path.join(d, ".git")
    s = Scripted(mod, fetch="timeout", ls_main=OTHER, ls_head=h)
    mod.command = s
    mod._same_repo_objects_dir = lambda: os.path.join(git_dir, "objects")
    mod._is_partial_clone = lambda: False
    mod._reuseable_alternate = lambda *a: None
    expect_refusal(mod, lambda: mod._fetch_head_and_main("/tmp/checkout", h, m, BRANCH),
                   "REMOTE_MAIN_DRIFT")


def t4_remote_uncertainty_refuses(mod):
    d, h, m = make_repo()
    git_dir = os.path.join(d, ".git")
    for ls_main, ls_head in (("timeout", h), ("ambiguous", h)):
        s = Scripted(mod, fetch="timeout", ls_main=ls_main, ls_head=ls_head)
        mod.command = s
        mod._same_repo_objects_dir = lambda: os.path.join(git_dir, "objects")
        mod._is_partial_clone = lambda: False
        mod._reuseable_alternate = lambda *a: None
        expect_refusal(mod, lambda: mod._fetch_head_and_main("/tmp/checkout", h, m, BRANCH),
                       "REMOTE_VERIFICATION_UNCERTAIN")


def t5_normal_fetch_unchanged(mod):
    d, h, m = make_repo()
    s = Scripted(mod, fetch="ok")
    mod.command = s
    mod._same_repo_objects_dir = lambda: None
    assert mod._fetch_head_and_main("/tmp/checkout", h, m, BRANCH) == ""
    assert s.ls_remote_calls() == [], s.calls
    assert len(s.network_fetches()) == 1


def t6_nontimeout_failure_propagates(mod):
    s = Scripted(mod, fetch="refuse")
    mod.command = s
    mod._same_repo_objects_dir = lambda: None
    expect_refusal(mod, lambda: mod._fetch_head_and_main("/tmp/checkout", HEAD, MAIN, BRANCH),
                   "fetch failed")
    assert s.ls_remote_calls() == [], s.calls


def t7_partial_clone_falls_through_to_network(mod):
    d, h, m = make_repo()
    s = Scripted(mod, fetch="timeout-twice", ls_main=m, ls_head=h)
    calls = {"n": 0}

    def fake(*args, **kwargs):
        if list(args)[:2] == ["git", "fetch"]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.TimeoutExpired(list(args), kwargs.get("timeout"))
            return ""
        return Scripted.__call__(s, *args, **kwargs)

    mod.command = fake
    mod._same_repo_objects_dir = lambda: os.path.join(d, ".git", "objects")
    mod._is_partial_clone = lambda: True  # commits present but blobs may be missing
    assert mod._fetch_head_and_main("/tmp/checkout", h, m, BRANCH) == ""
    assert s.ls_remote_calls() == [], s.calls  # never entered verification


def t8_duplicate_semantics_unchanged(mod):
    pr = {"number": 7941, "head": {"sha": HEAD, "ref": BRANCH,
          "repo": {"full_name": "heydonna-app/heydonna-app"}},
          "state": "open", "draft": False, "base": {"ref": "main"},
          "labels": [{"name": f"ci-head:{HEAD}"}]}
    run = {"id": 1, "head_sha": HEAD, "event": "pull_request", "head_branch": BRANCH,
           "status": "in_progress", "conclusion": None}
    jobs = {"jobs": [{"name": "classify-change-scope", "conclusion": "success"}]}

    def fake(*args, **kwargs):
        argv = list(args)
        import json as _json
        if argv[-1] == "repos/heydonna-app/heydonna-app/pulls/7941":
            return _json.dumps(pr)
        if argv[-1] == "repos/heydonna-app/heydonna-app/git/ref/heads/main":
            return _json.dumps({"object": {"sha": MAIN}})
        if argv[-1].startswith("repos/heydonna-app/heydonna-app/compare/"):
            return _json.dumps({"merge_base_commit": {"sha": MAIN}})
        if "workflows" in argv[-1]:
            return _json.dumps([{"workflow_runs": [run]}])
        if "actions/runs" in argv[-1]:
            return _json.dumps(jobs)
        raise AssertionError(argv)

    mod.command = fake
    out = mod.admit(7941, HEAD, apply=True)
    assert out["status"] == "ALREADY_ADMITTED" and out["head"] == HEAD, out


def with_env(fn):
    saved = dict(os.environ)
    os.environ.pop("ADMIT_FETCH_TIMEOUT_S", None)
    os.environ.pop("ADMIT_TARGETED_FETCH_TIMEOUT_S", None)
    try:
        return fn(CAND_MOD)
    finally:
        os.environ.clear()
        os.environ.update(saved)


CAND_MOD = load(CANDIDATE)
for _n, _f in sorted([(k, v) for k, v in list(globals().items())
                      if k.startswith("t") and callable(v) and k != "t9_red_on_preimage"]):
    check(_n, lambda _f=_f: with_env(_f))


def t9_red_on_preimage():
    mod = load(PREIMAGE)
    assert not hasattr(mod, "_verified_local_fetch"), "preimage must lack the escape"
    assert not hasattr(mod, "_remote_oid"), "preimage must lack remote verification"
    assert not hasattr(mod, "_local_objects_ready"), "preimage must lack local proof"
    import inspect
    assert len(inspect.signature(mod._fetch_head_and_main).parameters) == 3
    s = Scripted(mod, fetch="timeout")
    mod.command = s
    mod._same_repo_objects_dir = lambda: "/nonexistent/objects"
    try:
        mod._fetch_head_and_main("/tmp/checkout", HEAD, MAIN, BRANCH)
    except TypeError:
        pass  # 3-param signature rejects branch: no verification path exists
    except subprocess.TimeoutExpired:
        pass  # timeout propagates raw: no ls-remote verification attempted
    else:
        raise AssertionError("preimage should not succeed on timeout")
    assert s.ls_remote_calls() == [], s.calls


check("t9_red_on_preimage", t9_red_on_preimage)

failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else " :: " + detail))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
