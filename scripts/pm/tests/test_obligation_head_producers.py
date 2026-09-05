from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path


warnings.simplefilter("ignore", ResourceWarning)


ROOT = Path(__file__).resolve().parents[3]
WRITER = Path("/Users/rajiv/.claude/scripts/pm-ops.py")
READER = Path("/Users/rajiv/.claude/scripts/sakshi-heartbeat.py")
HEAD = "9163a885d2e13920095a853730ffd11e13378444"
OTHER_HEAD = "9a705cf97428758c1107c6d27a2bf4d040b13d1a"


def load_reader():
    spec = importlib.util.spec_from_file_location("installed_sakshi", READER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load installed reader: {READER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ObligationHeadProducerTests(unittest.TestCase):
    def test_canonical_callers_require_or_preserve_exact_head_contract(self) -> None:
        assets = ROOT / "scripts/pm/shared-assets/claude"
        ci_skill = (assets / "skills/ci-failure-investigation/SKILL.md").read_text()
        context_hook = (assets / "hooks/pm-context-injector.sh").read_text()
        idle_skill = (assets / "skills/pm-idle-notification/SKILL.md").read_text()

        self.assertIn("REFUSE ci_rework obligation", ci_skill)
        self.assertIn('--evidence "head_sha=${HEAD_SHA}"', ci_skill)
        self.assertIn('CURRENT_PR_HEAD="<fresh exact current PR head from the supported readback>"', ci_skill)
        self.assertIn("superseded_by_accepted_slot_rework", ci_skill)
        self.assertIn("superseded_by_newer_exact_head_slot_rework", ci_skill)
        self.assertIn("(kind,target_type,target_id,pr,issue)", ci_skill)
        self.assertIn("CI_EVENT_HEAD", context_hook)
        self.assertIn('head_sha=$CI_EVENT_HEAD', context_hook)
        self.assertIn("head_status=missing_or_conflicting", context_hook)
        self.assertIn('--evidence "head_sha=$HEAD"', idle_skill)

    def test_real_writer_to_temp_store_to_installed_reader(self) -> None:
        if not WRITER.is_file() or not READER.is_file():
            self.skipTest("installed producer/reader pair is not available in this environment")

        reader = load_reader()
        with tempfile.TemporaryDirectory(prefix="obligation-head-producer-") as tmp:
            db = Path(tmp) / "pm-ops.db"
            env = {**os.environ, "PM_OPS_DB": str(db)}

            def upsert(pr: int, *, evidence: list[str], kind: str = "ci_rework") -> None:
                command = [
                    "python3",
                    str(WRITER),
                    "obligation-upsert",
                    "--kind",
                    kind,
                    "--severity",
                    "high",
                    "--target-type",
                    "pr",
                    "--target-id",
                    str(pr),
                    "--pr",
                    str(pr),
                    "--owner",
                    "pm",
                    "--title",
                    f"CI rework for PR #{pr}",
                    "--action",
                    "Await the exact-head PM investigation report.",
                    "--blocker",
                    "current-head evidence required",
                ]
                for item in evidence:
                    command.extend(("--evidence", item))
                result = subprocess.run(command, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

            # The real installed writer retains the full head and repeated
            # invocation is the existing upsert/idempotency boundary.
            upsert(7629, evidence=["run=33968407703", f"head_sha={HEAD}"])
            upsert(7629, evidence=["run=33968407703", f"head_sha={HEAD}"])
            reader.PM_OPS_DB = db
            rows, error = reader._load_open_pr_continuations("7629", HEAD)
            self.assertIsNone(error)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["head"], HEAD)

            with sqlite3.connect(db) as connection:
                count = connection.execute(
                    "select count(*) from obligations where status='open' and pr=7629"
                ).fetchone()[0]
            self.assertEqual(count, 1)

            # A malformed historical sibling is retained, but the valid exact
            # head remains authoritative for the reader.
            upsert(7629, kind="ci_reconcile", evidence=["run=legacy", "head_sha=not-a-head"])
            rows, error = reader._load_open_pr_continuations("7629", HEAD)
            self.assertIsNone(error)
            self.assertEqual(rows[0]["head"], HEAD)

            # Missing, stale, and conflicting evidence fail closed and cannot
            # become current-head activity.
            upsert(7585, evidence=["run=missing-head"])
            rows, error = reader._load_open_pr_continuations("7585", OTHER_HEAD)
            self.assertEqual(rows, [])
            self.assertIn("no exact head binding", error or "")

            upsert(7622, evidence=["run=conflict", f"head={OTHER_HEAD}", f"head_sha={HEAD}"])
            rows, error = reader._load_open_pr_continuations("7622", HEAD)
            self.assertEqual(rows, [])
            self.assertIn("conflicting heads", error or "")

            with sqlite3.connect(db) as connection:
                historical = connection.execute(
                    "select count(*) from obligations where status='open'"
                ).fetchone()[0]
            self.assertEqual(historical, 4)

    def test_pm_context_caller_emits_bound_head_to_existing_writer(self) -> None:
        hook = ROOT / "scripts/pm/shared-assets/claude/hooks/pm-context-injector.sh"
        with tempfile.TemporaryDirectory(prefix="pm-context-producer-") as tmp:
            root = Path(tmp)
            calls = root / "calls.jsonl"
            fake_writer = root / "pm-ops.py"
            fake_writer.write_text(
                "import json, os, sys\n"
                f"open({str(calls)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            )
            fake_writer.chmod(0o755)
            payload = json.dumps(
                {
                    "prompt": f"# slack-channel C0AEY9CEC4D in thread 1788633837.228499 | CI failed https://github.com/heydonna-app/heydonna-app/pull/7629 {{\"headRefOid\": \"{HEAD}\"}}",
                    "cwd": "/Users/rajiv/Downloads/projects/heydonna-app",
                }
            )
            result = subprocess.run(
                ["bash", str(hook)],
                input=payload,
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "PM_OPS": str(fake_writer),
                    "HOME": str(root),
                    "LOG": str(root / "hook.log"),
                    "GH_CLI": str(root / "missing-gh"),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in calls.read_text().splitlines()]
            obligation = next(args for args in calls if args[0] == "obligation-upsert")
            self.assertIn("--evidence", obligation)
            self.assertIn(f"head_sha={HEAD}", obligation)

    def test_accepted_slot_rework_receipt_is_queued_and_nonexecuting(self) -> None:
        if not WRITER.is_file() or not READER.is_file():
            self.skipTest("installed producer/reader pair is not available in this environment")

        reader = load_reader()
        with tempfile.TemporaryDirectory(prefix="slot-rework-producer-") as tmp:
            db = Path(tmp) / "pm-ops.db"
            env = {**os.environ, "PM_OPS_DB": str(db)}

            def run(command: list[str]) -> str:
                result = subprocess.run(command, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            def slot_command(
                head: str,
                *,
                owner: str = "rescues",
                action: str = "Dispatch the accepted local reproduction packet once.",
                wake: str = "next healthy numbered-slot capacity",
                receipt: str = "cto-7629-rework-v1",
            ) -> list[str]:
                return [
                    "python3",
                    str(WRITER),
                    "obligation-upsert",
                    "--kind",
                    "slot_rework",
                    "--severity",
                    "high",
                    "--target-type",
                    "pr",
                    "--target-id",
                    "7629",
                    "--pr",
                    "7629",
                    "--owner",
                    owner,
                    "--title",
                    "Accepted exact-head rework for PR #7629",
                    "--action",
                    action,
                    "--blocker",
                    f"accepted CTO next-step receipt={receipt}",
                    "--dedupe-group",
                    f"slot_rework:7629:{head}:{receipt}",
                    "--evidence",
                    f"head_sha={head}",
                    "--evidence",
                    f"owner={owner}",
                    "--evidence",
                    f"action={action}",
                    "--evidence",
                    f"wake={wake}",
                    "--evidence",
                    f"source_receipt={receipt}",
                    "--print-id",
                ]

            # The actual writer key is (kind, target_type, target_id, pr,
            # issue), not dedupe_group. First retain a generic failed-run
            # ci_rework row, then add the accepted nonexecuting slot row. The
            # installed reader correctly refuses the contradictory open lanes
            # until the accepted transition resolves only that exact generic
            # row.
            generic = [
                "python3",
                str(WRITER),
                "obligation-upsert",
                "--kind",
                "ci_rework",
                "--severity",
                "high",
                "--target-type",
                "pr",
                "--target-id",
                "7629",
                "--pr",
                "7629",
                "--owner",
                "pm",
                "--title",
                "CI rework for PR #7629",
                "--action",
                "Await the exact-head PM investigation report.",
                "--blocker",
                "current-head evidence required",
                "--evidence",
                "run=33968407703",
                "--evidence",
                f"head_sha={HEAD}",
                "--print-id",
            ]
            generic_id = int(run(generic))
            slot = slot_command(HEAD)
            first_slot_id = int(run(slot))
            second_slot_id = int(run(slot))
            self.assertEqual(first_slot_id, second_slot_id)

            reader.PM_OPS_DB = db
            records, error = reader._load_open_pr_continuations("7629", HEAD)
            self.assertEqual(records, [])
            self.assertIn("contradict", error or "")

            run(
                [
                    "python3",
                    str(WRITER),
                    "obligation-resolve",
                    "--kind",
                    "ci_rework",
                    "--id",
                    str(generic_id),
                    "--reason",
                    "superseded_by_accepted_slot_rework",
                    "--external-state",
                    "accepted_nonexecuting_queue:cto-7629-rework-v1",
                ]
            )
            records, error = reader._load_open_pr_continuations("7629", HEAD)
            self.assertIsNone(error)
            self.assertEqual(records[0]["lane"], "rework")
            metadata = reader._continuation_motion_metadata(records)
            self.assertIsNotNone(metadata)
            self.assertEqual(metadata["motion_state"], "REPRO_REWORK_QUEUED")
            self.assertEqual(metadata["workflow_motion"], "none")
            self.assertEqual(metadata["owner"], "rescues")

            with sqlite3.connect(db) as connection:
                generic_row = connection.execute(
                    "select status, evidence_json, resolved_reason from obligations where id=?",
                    (generic_id,),
                ).fetchone()
                self.assertEqual(generic_row[0], "resolved")
                self.assertIn(HEAD, generic_row[1])
                self.assertEqual(generic_row[2], "superseded_by_accepted_slot_rework")
                count = connection.execute(
                    "select count(*) from obligations where status='open' and kind='slot_rework'"
                ).fetchone()[0]
            self.assertEqual(count, 1)

            # A later independently accepted head must not rewrite either the
            # failed-run row or the prior slot receipt. Resolve the old slot
            # row by its exact ID, then let the same writer key insert the new
            # open slot row with the later receipt.
            later_head = "f109414c02cc296510103fe2c090ce964e9b9dfb"
            run(
                [
                    "python3",
                    str(WRITER),
                    "obligation-resolve",
                    "--kind",
                    "slot_rework",
                    "--id",
                    str(first_slot_id),
                    "--reason",
                    "superseded_by_newer_exact_head_slot_rework",
                    "--external-state",
                    "accepted_nonexecuting_queue:cto-7629-rework-v2",
                ]
            )
            later_id = int(
                run(
                    slot_command(
                        later_head,
                        action="Dispatch the newer accepted reproduction packet once.",
                        wake="next healthy numbered-slot capacity after head refresh",
                        receipt="cto-7629-rework-v2",
                    )
                )
            )
            self.assertNotEqual(later_id, first_slot_id)
            records, error = reader._load_open_pr_continuations("7629", later_head)
            self.assertIsNone(error)
            self.assertEqual(records[0]["head"], later_head)
            self.assertEqual(records[0]["owner"], "rescues")
            old_records, old_error = reader._load_open_pr_continuations("7629", HEAD)
            self.assertEqual(old_records, [])
            self.assertIsNone(old_error)

            with sqlite3.connect(db) as connection:
                history = connection.execute(
                    """select id, status, evidence_json, resolved_reason
                       from obligations where kind='slot_rework' and pr=7629 order by id"""
                ).fetchall()
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0][0], first_slot_id)
            self.assertEqual(history[0][1], "resolved")
            self.assertIn(HEAD, history[0][2])
            self.assertEqual(history[1][0], later_id)
            self.assertEqual(history[1][1], "open")
            self.assertIn(later_head, history[1][2])

            # Missing, wrong, or placeholder owner/head evidence never becomes
            # a queued reader result; these are separate retained rows.
            bad = slot_command(HEAD, owner="unknown")
            bad.extend(("--target-id", "7585", "--pr", "7585", "--evidence", "head_sha=not-a-head"))
            result = subprocess.run(bad, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            records, error = reader._load_open_pr_continuations("7585", HEAD)
            self.assertEqual(records, [])
            self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
