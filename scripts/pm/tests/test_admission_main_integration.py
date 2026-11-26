"""Exercise both admission CLIs against a local Git remote, with no GitHub writes."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets"
PM = ASSETS / "claude/scripts/ci/pm-admit-ci.sh"
CTO = ASSETS / "codex/skills/heydonna-cto-label-gated-ci/scripts/admit.py"
PR = "11173"
PR_REF = f"refs/pull/{PR}/head"
REPO = "Scribie/heydonna-app"


def git(*args):
    return subprocess.check_output([os.environ["REAL_GIT"], *args], text=True).strip()


def record(*args):
    with open(os.environ["WRITES"], "a") as log:
        log.write(json.dumps(args) + "\n")


def stub_git(args):
    if args[:1] == ["-C"]:
        args[1] = os.environ["FIXTURE_CLONE"]
    if args[:3] == ["remote", "add", "origin"]:
        args[3] = os.environ["FIXTURE_ORIGIN"]
    if "merge-tree" in args and os.environ["SCENARIO"] == "probe_error":
        sys.exit(128)
    if args[:1] == ["push"]:
        record("integration", git("rev-parse", "HEAD"))
    real = os.environ["REAL_GIT"]
    os.execv(real, [real, *args])


def stub_gh(args):
    origin, scenario = os.environ["FIXTURE_ORIGIN"], os.environ["SCENARIO"]
    head = git("-C", origin, "rev-parse", PR_REF if os.environ["LANE"] == "pm" else "topic")
    main = git("-C", origin, "rev-parse", "main")
    reported_conflict = scenario == "reported_conflict"
    if args[:2] == ["pr", "view"]:
        field = args[args.index("--json") + 1]
        values = {
            "state": "OPEN", "headRefOid": head, "baseRefName": "main",
            "headRefOid,mergeable": f"{head} {'CONFLICTING' if reported_conflict else 'MERGEABLE'}",
            "closingIssuesReferences": "1",
            # Malformed label is deliberate: on refusal even cleanup must not write.
            "labels": '{"labels":[{"name":"ci-head:"}]}',
            "labels,closingIssuesReferences": '{"labels":[{"name":"P2"}],"closingIssuesReferences":[]}',
        }
        print(values[field])
    elif args[:3] == ["api", "-X", "PUT"]:
        record("update-branch", args[-1])
        if scenario == "conflict" or reported_conflict or args[-1] != f"expected_head_sha={head}":
            sys.exit(1)
        if scenario != "unconfirmed":
            git("-C", origin, "switch", "--quiet", "topic")
            git("-C", origin, "merge", "--quiet", "--no-edit", main)
            new_head = git("-C", origin, "rev-parse", "HEAD")
            git("-C", origin, "update-ref", PR_REF, new_head, head)
            record("integration", new_head)
    elif args[:2] in (["pr", "edit"], ["label", "create"]):
        audit = Path.home() / ".codex" / "admission-cap-overrides.jsonl"
        if args[:2] == ["label", "create"] and audit.is_file():
            record("audit-before-label", json.loads(audit.read_text()))
        record(*args)
    elif args[0] == "api":
        path = next(arg for arg in args if arg.startswith("repos/"))
        if path.endswith(f"/pulls/{PR}"):
            print(json.dumps({"number": int(PR), "state": "open", "draft": False,
                              "mergeable": not reported_conflict, "base": {"ref": "main"},
                              "head": {"sha": head, "ref": "topic", "repo": {"full_name": REPO}},
                              "labels": []}))
        elif path.endswith("/git/ref/heads/main"):
            print(json.dumps({"object": {"sha": main}}))
        elif "/compare/" in path:
            base = git("-C", origin, "merge-base", main, head)
            print(json.dumps({"merge_base_commit": {"sha": base}}))
        elif "/actions/workflows/" in path:
            print('[{"workflow_runs":[]}]')
        elif path.endswith(f"/issues/{PR}/events?per_page=100"):
            rounds = int(os.environ["ADMISSION_ROUNDS"])
            if rounds < 0:
                sys.exit(1)
            label = {"name": f"ci-head:{head}"}
            print(json.dumps([[
                {"id": index * 2 + i + 1, "event": event, "label": label}
                for index in range(rounds) for i, event in enumerate(("labeled", "unlabeled"))
            ]]))
        elif path.endswith(f"/issues/{PR}/labels"):
            labels = json.load(sys.stdin)["labels"]
            record("add-ci-label", *labels)
            print(json.dumps([{"name": label} for label in labels]))
        else:
            sys.exit(1)
    else:
        sys.exit(1)


class AdmissionMainIntegrationTests(unittest.TestCase):
    def run_cli(self, lane, scenario, no_update, *, rounds=0, override=None, wrong_head=False,
                audit_available=True, apply=True):
        with tempfile.TemporaryDirectory(prefix="mop-admission-fixture-") as directory:
            root = Path(directory)
            origin, checkout, bin_dir = (root / name for name in ("origin", "checkout", "bin"))
            bin_dir.mkdir()
            (root / ".claude/logs").mkdir(parents=True)
            if audit_available:
                (root / ".codex").mkdir()
            writes = root / "writes"
            writes.touch()
            env = dict(os.environ, HOME=directory, TMPDIR=directory,
                       REAL_GIT=shutil.which("git"), FIXTURE_ORIGIN=str(origin),
                       FIXTURE_CLONE=str(checkout), WRITES=str(writes), SCENARIO=scenario,
                       LANE=lane, GIT_ALLOW_PROTOCOL="file", GIT_CONFIG_NOSYSTEM="1",
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="Fixture",
                       GIT_AUTHOR_EMAIL="fixture@example.invalid", GIT_COMMITTER_NAME="Fixture",
                       GIT_COMMITTER_EMAIL="fixture@example.invalid", PM_ADMIT_RETRY_SLEEP="0",
                       ADMISSION_ROUNDS=str(rounds))

            def local_git(*args):
                return subprocess.check_output([env["REAL_GIT"], *args], env=env, text=True).strip()

            local_git("init", "--quiet", "-b", "main", str(origin))
            shared = origin / "shared.txt"
            middle = "\n".join(f"unchanged {n}" for n in range(12)) + "\n"
            shared.write_text("first\n" + middle + "last\n")
            local_git("-C", str(origin), "add", ".")
            local_git("-C", str(origin), "commit", "-qm", "base")
            local_git("-C", str(origin), "switch", "-qc", "topic")
            if scenario in ("overlap", "unconfirmed"):
                shared.write_text("first\n" + middle + "topic last\n")
            elif scenario == "conflict":
                shared.write_text("topic first\n" + middle + "last\n")
            else:
                (origin / "topic.txt").write_text("disjoint\n")
            local_git("-C", str(origin), "add", ".")
            local_git("-C", str(origin), "commit", "-qm", "topic")
            old = local_git("-C", str(origin), "rev-parse", "HEAD")
            local_git("-C", str(origin), "update-ref", PR_REF, old)
            local_git("-C", str(origin), "switch", "-q", "main")
            shared.write_text("main first\n" + middle + "last\n")
            local_git("-C", str(origin), "commit", "-qam", "main moved")
            main = local_git("-C", str(origin), "rev-parse", "HEAD")
            local_git("clone", "-q", str(origin), str(checkout))
            for name in ("git", "gh", "sleep"):
                shim = bin_dir / name
                target = (f'exec {shlex.quote(sys.executable)} -I {shlex.quote(__file__)} {name} "$@"'
                          if name != "sleep" else "exit 0")
                shim.write_text(f"#!/bin/sh\n{target}\n")
                shim.chmod(0o700)
            env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
            if lane == "pm":
                # Stub only the independent 12b check; execute the unmodified admission CLI bytes.
                shutil.copyfile(PM, root / PM.name)
                shutil.copyfile(PM.with_name("pm-admit-cap.py"), root / "pm-admit-cap.py")
                (root / "pm-admit-12b.py").write_text("print('12B_GATE: PASS fixture')\n")
                cmd = ["bash", str(root / PM.name), PR]
            else:
                cmd = [sys.executable, "-I", str(CTO), "--pr", PR, "--head", old]
                if apply:
                    cmd += ["--apply"]
                if override is not None:
                    cmd += ["--cap-override", override, "--cap-override-head", main if wrong_head else old]
            if no_update:
                cmd += ["--no-update"]
            result = subprocess.run(cmd, env=env, cwd=root, text=True, capture_output=True, timeout=30)
            final = local_git("-C", str(origin), "rev-parse", PR_REF if lane == "pm" else "topic")
            effects = [json.loads(line) for line in writes.read_text().splitlines()]
            if final != old:
                local_git("-C", str(origin), "merge-base", "--is-ancestor", old, final)
                local_git("-C", str(origin), "merge-base", "--is-ancestor", main, final)
            return result, old, final, effects

    def test_cto_cap_refusals_do_not_write(self):
        approval = "cto:1700000000.000001"
        cases = [
            ("disjoint", "cap_reached", {"rounds": 2}),
            ("overlap", "cap_reached", {"rounds": 2}),  # Refuse before a branch push too.
            ("disjoint", "cap_reached", {"rounds": 2, "apply": False}),
            ("disjoint", "ADMISSION_CAP_OVERRIDE_HEAD_MISMATCH",
             {"rounds": 2, "override": approval, "wrong_head": True}),
            ("disjoint", "UNAVAILABLE", {"rounds": -1}),
            ("disjoint", "UNAVAILABLE", {"rounds": -1, "override": approval}),
            ("disjoint", "ADMISSION_CAP_OVERRIDE_AUDIT_UNAVAILABLE",
             {"rounds": 2, "override": approval, "audit_available": False}),
        ]
        for scenario, reason, kwargs in cases:
            with self.subTest(scenario=scenario, reason=reason, options=kwargs):
                result, old, final, effects = self.run_cli("cto", scenario, False, **kwargs)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                receipt = json.loads(result.stdout)
                self.assertEqual(receipt["status"], "REFUSED")
                self.assertIn(reason, receipt["reason"])
                if reason == "cap_reached":
                    self.assertIn("rounds=2 cap=2", receipt["reason"])
                self.assertEqual(final, old)
                self.assertEqual(effects, [])

    def test_cto_pinned_override_is_audited_before_writing(self):
        approval = "rajiv:1700000000.000002"
        result, old, final, effects = self.run_cli("cto", "disjoint", False, rounds=2, override=approval)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ADMISSION_REQUESTED")
        self.assertEqual(final, old)
        self.assertEqual(len(effects), 3, effects)
        name, audit = effects[0]
        self.assertEqual(name, "audit-before-label")
        self.assertEqual({k: v for k, v in audit.items() if k != "ts"},
                         {"repo": REPO, "pr": int(PR), "head": old,
                          "admission-cap-override": approval, "rounds": 2})
        self.assertEqual(effects[1][:3], ["label", "create", f"ci-head:{old}"])
        self.assertEqual(effects[2], ["add-ci-label", f"ci-head:{old}"])

    def test_admission_only_labels_a_head_with_a_clean_or_confirmed_plan(self):
        for lane in ("pm", "cto"):
            for scenario in ("disjoint", "overlap", "conflict", "reported_conflict", "probe_error", "unconfirmed"):
                if lane == "cto" and scenario == "unconfirmed":
                    continue  # Only the GitHub update endpoint accepts asynchronously.
                for no_update in (False, True):
                    with self.subTest(lane=lane, scenario=scenario, no_update=no_update):
                        result, old, final, effects = self.run_cli(lane, scenario, no_update)
                        output = result.stdout + result.stderr
                        labels = [e for e in effects if e[:2] in (["label", "create"], ["pr", "edit"])
                                  or e[0] == "add-ci-label"]
                        if scenario == "disjoint" or (scenario == "overlap" and not no_update):
                            self.assertEqual(result.returncode, 0, output)
                            added = [e[-1] if e[0] == "add-ci-label" else e[e.index("--add-label") + 1]
                                     for e in labels if e[0] == "add-ci-label" or "--add-label" in e]
                            self.assertEqual(added, [f"ci-head:{final}"], effects)
                            if scenario == "disjoint":
                                self.assertEqual(final, old, effects)
                                self.assertEqual(effects, labels)  # No integration attempt.
                            else:
                                self.assertNotEqual(final, old, effects)
                                self.assertIn(["integration", final], effects)
                                self.assertLess(effects.index(["integration", final]), effects.index(labels[0]))
                        else:
                            self.assertEqual(result.returncode, 2, output)
                            self.assertRegex(output, {
                                "overlap": "--no-update",
                                "conflict": "conflict|CONFLICT",
                                "reported_conflict": "conflict|CONFLICT",
                                "probe_error": "probe failed|MAIN_MERGE_PROBE_FAILED",
                                "unconfirmed": "merge not confirmed|--no-update",
                            }[scenario])
                            self.assertEqual(final, old, effects)
                            self.assertEqual(labels, [], effects)
                            if no_update:
                                self.assertEqual(effects, [])


if __name__ == "__main__":
    if sys.argv[1:2] == ["git"]:
        stub_git(sys.argv[2:])
    elif sys.argv[1:2] == ["gh"]:
        stub_gh(sys.argv[2:])
    else:
        unittest.main()
