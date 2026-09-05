#!/usr/bin/env python3
"""Production-shaped proof for the CTO admission adapter."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "shared-assets/claude/scripts/ci/heydonna-cto-label-gated-ci.py"
MANIFEST = ROOT / "shared-assets/manifest.json"
HEAD = "1e7422943c65f2e4c976582c53f842452ebf5de7"
BASE = "e10acd01b72cd906ffe188947176caa4577e9d01"


class AdapterContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.checkout = self.dir / "checkout"
        self.checkout.mkdir()
        (self.checkout / ".git-head").write_text(HEAD, encoding="utf-8")
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.state = self.dir / "state.json"
        self.issue_body = "Current issue contract with visual and substantive proof."
        self.state.write_text(json.dumps({
            "body": self.issue_body,
            "labels": ["unrelated", "ci-head:old"],
            "workflow": "not_started",
            "linked_issue": 7554,
            "post_mode": "success",
            "post_calls": 0,
            "delete_calls": 0,
            "reason": "",
            "latest_run_id": 123,
        }), encoding="utf-8")
        self.gh = self.bin / "gh"
        self.gh.write_text(self._gh_script(), encoding="utf-8")
        self.gh.chmod(0o755)
        git = self.bin / "git"
        git.write_text("#!/bin/sh\nif [ \"$1\" = \"-C\" ]; then cat \"$2/.git-head\"; else exit 2; fi\n", encoding="utf-8")
        git.chmod(0o755)
        self.old_path = os.environ.get("PATH", "")
        self.old_validator_env = os.environ.get("HEYDONNA_ISSUE_CONTRACT_LEDGER_VALIDATOR")
        os.environ["PATH"] = f"{self.bin}:{self.old_path}"
        self.readiness = self.dir / "readiness.py"
        self.readiness.write_text(self._gate_script(), encoding="utf-8")
        self.visual = self.dir / "visual.py"
        self.visual.write_text("import json; print(json.dumps({'ok': True, 'pr': 7622, 'head_sha': '" + HEAD + "'}))\n", encoding="utf-8")
        self.validator = self.dir / "validate-issue-contract-ledger.py"
        self.validator.write_text("def validate(body, require_qa_proof=True): return ['must-not-be-used']\ndef parse_ac_proof_contract(body): return []\n", encoding="utf-8")
        self.module = self._load_module()
        self.module.READINESS_GATE = self.readiness
        self.module.READINESS_GATE_SHA256 = hashlib.sha256(self.readiness.read_bytes()).hexdigest()
        self.module.VISUAL_GATE = self.visual
        self.module.VISUAL_GATE_SHA256 = hashlib.sha256(self.visual.read_bytes()).hexdigest()
        self.module.ISSUE_CONTRACT_VALIDATOR = self.validator
        self.module.ISSUE_CONTRACT_VALIDATOR_SHA256 = hashlib.sha256(self.validator.read_bytes()).hexdigest()
        self.module.EFFECT_RECEIPT_ROOT = self.dir / "receipts"

    def tearDown(self) -> None:
        os.environ["PATH"] = self.old_path
        if self.old_validator_env is None:
            os.environ.pop("HEYDONNA_ISSUE_CONTRACT_LEDGER_VALIDATOR", None)
        else:
            os.environ["HEYDONNA_ISSUE_CONTRACT_LEDGER_VALIDATOR"] = self.old_validator_env
        self.temp.cleanup()

    def _load_module(self):
        spec = importlib.util.spec_from_file_location("cto_admission", ADAPTER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _gate_script(self) -> str:
        return f'''#!/usr/bin/env python3
import json, pathlib
s=json.loads(pathlib.Path({str(self.state)!r}).read_text())
ok=s["workflow"] == "not_started"
reasons=[] if ok else [s.get("reason") or "workflow_state_"+s["workflow"]]
print(json.dumps({{"ok":ok,"status":"pass" if ok else "blocked","headRefOid":{HEAD!r},"reasons":reasons,"artifacts":{{"change_scope":{{"head":{HEAD!r},"ci_required":True,"e2e_required":True,"control_plane_only":False}},"workflows":{{"state":s["workflow"],"bad_run_id":s.get("latest_run_id")}},"latest_ci_verdict":{{"run_id":s.get("latest_run_id")}}}}}}))
raise SystemExit(0 if ok else 1)
'''

    def _gh_script(self) -> str:
        return f'''#!/usr/bin/env python3
import json, pathlib, sys
p=pathlib.Path({str(self.state)!r}); s=json.loads(p.read_text()); args=sys.argv[1:]
if args[:2] == ["pr", "view"]:
 print(json.dumps({{"number":7622,"headRefOid":{HEAD!r},"state":"OPEN","isDraft":False,"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","headRefName":"feature","closingIssuesReferences":[{{"number":s["linked_issue"]}}],"labels":[{{"name":x}} for x in s["labels"]]}}))
elif args[:2] == ["issue", "view"]:
 print(json.dumps({{"number":7554,"body":s["body"],"state":"OPEN"}}))
elif args[:2] == ["run", "view"]:
 print(json.dumps({{"databaseId":123,"headSha":{HEAD!r},"event":"pull_request","status":"completed","conclusion":"failure","workflowName":"CI","attempt":2}}))
elif args[:2] == ["api", "--method"] and args[2] == "POST":
 s["post_calls"]+=1
 if s["post_mode"] != "fail": s["labels"]=sorted(set(s["labels"]) | {{"pm-state:qa-passed-awaiting-ci", "ci-head:{HEAD}"}})
 p.write_text(json.dumps(s)); print("{{}}")
 raise SystemExit(1 if s["post_mode"] == "loss" or s["post_mode"] == "fail" else 0)
elif args[:2] == ["api", "--method"] and args[2] == "DELETE":
 s["delete_calls"]+=1; label=args[-1].rsplit("/",1)[-1]
 if s["post_mode"] != "delete_loss": s["labels"]=[x for x in s["labels"] if x != label]
 p.write_text(json.dumps(s)); print("{{}}")
 raise SystemExit(1 if s["post_mode"] == "delete_loss" else 0)
else:
 print("unsupported", file=sys.stderr); raise SystemExit(2)
'''

    def _args(self, **extra: str):
        import argparse
        values = dict(pr=7622, issue=7554, head=HEAD, base=BASE, checkout=str(self.checkout), gh=str(self.gh), source_run_id=None, source_run_attempt=None, causal_log=None, local_log=None)
        values.update(extra)
        return argparse.Namespace(**values)

    def _main(self, **extra):
        args = self._args(**extra)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = self.module._checkout_head(args)
                pr = self.module._live_pr(args)
                body = self.module._issue_body(args)
                sha = hashlib.sha256(body.encode()).hexdigest()
                reentry = bool(args.source_run_id or args.source_run_attempt or args.causal_log or args.local_log)
                self.module._visual_proof(args, sha)
                self.module._gate(args, reentry=reentry)
                if reentry:
                    self.module._local_reentry_proof(args)
                result = self.module._effect(args, self.module._labels(pr), sha)
                return 0, result, err.getvalue()
            except Exception as exc:
                return 13, None, err.getvalue() + str(exc)

    def _state(self):
        return json.loads(self.state.read_text())

    def test_authoritative_relationship_visual_gate_and_one_trigger(self) -> None:
        code, result, error = self._main()
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "ADMITTED")
        state = self._state()
        self.assertEqual(state["post_calls"], 1)
        self.assertIn("unrelated", state["labels"])
        self.assertIn(f"ci-head:{HEAD}", state["labels"])
        self.assertNotIn("ci-head:old", state["labels"])
        source = ADAPTER.read_text(encoding="utf-8")
        self.assertIn("qa-passed-awaiting-ci`` is the paid CI/E2E trigger", source)
        self.assertNotIn("never starts CI", source)

    def test_wrong_linked_issue_and_stale_visual_are_zero_effect(self) -> None:
        state = self._state(); state["linked_issue"] = 999; self.state.write_text(json.dumps(state))
        code, _, error = self._main()
        self.assertEqual(code, 13)
        self.assertIn("linked_issue_relationship", error)
        self.assertEqual(self._state()["post_calls"], 0)

    def test_workflow_states_and_reentry_proof(self) -> None:
        for state in ("in_progress", "green", "failed"):
            data = self._state(); data["workflow"] = state; self.state.write_text(json.dumps(data))
            code, _, _ = self._main()
            self.assertEqual(code, 13)
        data = self._state(); data["workflow"] = "failed"; data["reason"] = "current_head_ci_or_e2e_failed_use_rerun_not_label_trigger run=123 sealed_preflight_retired=true"; self.state.write_text(json.dumps(data))
        causal = self.dir / "causal.txt"; local = self.dir / "local.txt"
        causal.write_text(f"PR=7622\nHEAD={HEAD}\nSOURCE_RUN_ID=123\nSOURCE_RUN_ATTEMPT=2\nCAUSAL_CLASS=INFRASTRUCTURE\n")
        local.write_text(f"LOCAL_REPRO_PR=7622\nLOCAL_REPRO_HEAD={HEAD}\nLOCAL_REPRO_SOURCE_RUN_ID=123\nLOCAL_REPRO_SOURCE_RUN_ATTEMPT=2\nLOCAL_REPRO_RESULT=PASS\nLOCAL_REPRO_EXIT=0\n")
        code, result, error = self._main(source_run_id="123", source_run_attempt="2", causal_log=str(causal), local_log=str(local))
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "ADMITTED")

    def test_replay_and_concurrency_are_single_flight(self) -> None:
        code, _, error = self._main(); self.assertEqual(code, 0, error)
        code, result, error = self._main(); self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "ALREADY_ADMITTED")
        self.assertEqual(self._state()["post_calls"], 1)

        data = self._state(); data["labels"] = ["unrelated", "ci-head:old"]; data["post_calls"] = 0; self.state.write_text(json.dumps(data))
        args = self._args(); before = ["ci-head:old", "unrelated"]
        self.module._receipt_path(args).unlink()
        results: list[object] = []
        def worker() -> None:
            try:
                results.append(("ok", self.module._effect(args, before, hashlib.sha256(self.issue_body.encode()).hexdigest())))
            except Exception as exc:
                results.append(("refused", str(exc)))
        threads = [threading.Thread(target=worker) for _ in range(2)]
        [thread.start() for thread in threads]; [thread.join() for thread in threads]
        self.assertEqual(self._state()["post_calls"], 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(item[0] == "ok" for item in results), 2)
        self.assertEqual(sum(item[1]["status"] == "ADMITTED" for item in results if item[0] == "ok"), 1)
        self.assertEqual(sum(item[1]["status"] == "ALREADY_ADMITTED" for item in results if item[0] == "ok"), 1)

    def test_response_loss_and_delete_failure_never_repost(self) -> None:
        data = self._state(); data["post_mode"] = "fail"; data["labels"] = ["unrelated"]; self.state.write_text(json.dumps(data))
        code, result, error = self._main(); self.assertEqual(code, 13)
        self.assertIsNone(result)
        code, result, error = self._main(); self.assertEqual(code, 13)
        self.assertIn("effect_unresolved_no_conclusive_reconciliation", error)
        self.assertEqual(self._state()["post_calls"], 1)
        receipt = json.loads(self.module._receipt_path(self._args()).read_text())
        self.assertEqual(receipt["state"], "unresolved")

        data = self._state(); data["post_mode"] = "loss"; data["labels"] = ["unrelated"]; data["post_calls"] = 0; self.state.write_text(json.dumps(data))
        self.module._receipt_path(self._args()).unlink()
        code, _, _ = self._main(); self.assertEqual(code, 13)
        code, _, error = self._main(); self.assertEqual(code, 13)
        self.assertIn("effect_unresolved_observed_target", error)
        self.assertEqual(self._state()["post_calls"], 1)

        data = self._state(); data["post_mode"] = "delete_loss"; data["labels"] = ["unrelated", "ci-head:old"]; data["post_calls"] = 0; self.state.write_text(json.dumps(data))
        self.module._receipt_path(self._args()).unlink()
        code, _, _ = self._main(); self.assertEqual(code, 13)
        code, _, error = self._main(); self.assertEqual(code, 13)
        self.assertIn("effect_unresolved_observed_target", error)
        self.assertEqual(self._state()["post_calls"], 1)

    def test_reentry_rejects_latest_requires_rework_over_older_failed_run(self) -> None:
        data = self._state(); data["workflow"] = "failed"; data["latest_run_id"] = 456; data["reason"] = "current_head_ci_verdict_requires_rework classification=PRODUCT_REGRESSION run=456"; self.state.write_text(json.dumps(data))
        causal = self.dir / "causal.txt"; local = self.dir / "local.txt"
        causal.write_text(f"PR=7622\nHEAD={HEAD}\nSOURCE_RUN_ID=123\nSOURCE_RUN_ATTEMPT=2\nCAUSAL_CLASS=INFRASTRUCTURE\n")
        local.write_text(f"LOCAL_REPRO_PR=7622\nLOCAL_REPRO_HEAD={HEAD}\nLOCAL_REPRO_SOURCE_RUN_ID=123\nLOCAL_REPRO_SOURCE_RUN_ATTEMPT=2\nLOCAL_REPRO_RESULT=PASS\nLOCAL_REPRO_EXIT=0\n")
        code, _, error = self._main(source_run_id="123", source_run_attempt="2", causal_log=str(causal), local_log=str(local))
        self.assertEqual(code, 13)
        self.assertIn("reentry_reason_not_authorized", error)
        self.assertEqual(self._state()["post_calls"], 0)

    def test_issue_body_drift_after_proof_is_zero_effect(self) -> None:
        args = self._args()
        pr = self.module._live_pr(args)
        body = self.module._issue_body(args)
        body_sha = hashlib.sha256(body.encode()).hexdigest()
        self.module._visual_proof(args, body_sha)
        self.module._gate(args, reentry=False)
        data = self._state(); data["body"] = "changed after proof"; self.state.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "issue_body_changed_before_effect"):
            self.module._effect(args, self.module._labels(pr), body_sha)
        self.assertEqual(self._state()["post_calls"], 0)

    def test_validator_override_is_ignored_and_pinned(self) -> None:
        os.environ["HEYDONNA_ISSUE_CONTRACT_LEDGER_VALIDATOR"] = str(self.dir / "malicious.py")
        env = self.module._clean_env()
        self.assertNotIn("HEYDONNA_ISSUE_CONTRACT_LEDGER_VALIDATOR", env)
        self.module._trusted_asset(self.module.ISSUE_CONTRACT_VALIDATOR, self.module.ISSUE_CONTRACT_VALIDATOR_SHA256, "issue_contract_validator")

    def test_manifest_parity_and_executable_mode(self) -> None:
        manifest = json.loads(MANIFEST.read_text())
        entry = next(item for item in manifest["entries"] if item["source_path"] == "claude/scripts/ci/heydonna-cto-label-gated-ci.py")
        self.assertEqual(entry["sha256"], hashlib.sha256(ADAPTER.read_bytes()).hexdigest())
        self.assertEqual(entry["mode"], 0o755)
        self.assertEqual(stat.S_IMODE(ADAPTER.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
