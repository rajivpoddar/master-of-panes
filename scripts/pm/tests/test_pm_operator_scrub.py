from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
CALLER = ROOT / "shared-assets/claude/scripts/ci/request-label-gated-ci.sh"
ADAPTER = ROOT / "shared-assets/claude/scripts/ci/heydonna-cto-label-gated-ci.py"
RUNTIME = ROOT / "shared-assets/claude/control_plane/runtime_observation.py"
MANIFEST = ROOT / "shared-assets/manifest.json"
OLD_CALLER = Path("/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh")


class PmOperatorScrubTests(unittest.TestCase):
    def test_red_baseline_requires_retired_binary_and_green_uses_native_adapter(self) -> None:
        old = OLD_CALLER.read_text(encoding="utf-8")
        self.assertIn("pm-operator/current/bin/pm-operator", old)
        self.assertIn("pm_operator_missing", old)

        current = CALLER.read_text(encoding="utf-8")
        self.assertNotIn("pm-operator/current/bin/pm-operator", current)
        self.assertNotIn("pm-transition.sh", current)
        self.assertIn("heydonna-cto-label-gated-ci.py", current)
        for argument in ("--issue", "--head", "--base", "--checkout"):
            self.assertIn(argument, current)

    def test_native_dispatch_forwards_authoritative_tuple_once(self) -> None:
        head = "a" * 40
        base = "b" * 40
        with TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / "adapter.log"
            gh = root / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' '{\"number\":7639,\"headRefOid\":\""
                + head
                + "\",\"baseRefOid\":\""
                + base
                + "\",\"closingIssuesReferences\":[{\"number\":7638}]}'\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            adapter = root / "adapter"
            adapter.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" > \"$ADAPTER_LOG\"\n",
                encoding="utf-8",
            )
            adapter.chmod(0o755)
            env = dict(
                os.environ,
                GH_BIN=str(gh),
                CI_ADMISSION_ADAPTER=str(adapter),
                ADAPTER_LOG=str(log),
                RLGC_CHECKOUT=str(root),
            )
            completed = subprocess.run(
                [str(CALLER), "--pr", "7639"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            args = log.read_text(encoding="utf-8")
            self.assertEqual(args.count("--pr 7639"), 1)
            self.assertIn("--issue 7638", args)
            self.assertIn("--head " + head, args)
            self.assertIn("--base " + base, args)
            self.assertIn("--checkout " + str(root), args)

    def test_missing_adapter_stops_before_any_github_effect(self) -> None:
        head = "a" * 40
        base = "b" * 40
        with TemporaryDirectory() as temp:
            root = Path(temp)
            gh = root / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' '{\"number\":7639,\"headRefOid\":\""
                + head[:-1]
                + "0\",\"baseRefOid\":\""
                + base
                + "\",\"closingIssuesReferences\":[{\"number\":7638}]}'\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            env = dict(
                os.environ,
                GH_BIN=str(gh),
                CI_ADMISSION_ADAPTER=str(root / "missing"),
                RLGC_CHECKOUT=str(root),
            )
            completed = subprocess.run(
                [str(CALLER), "--pr", "7639"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("native_admission_adapter_missing", completed.stderr)

    def test_adapter_head_drift_refusal_is_not_retried(self) -> None:
        base = "b" * 40
        with TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / "adapter.log"
            gh = root / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' '{\"number\":7639,\"headRefOid\":\""
                + "c" * 40
                + "\",\"baseRefOid\":\""
                + base
                + "\",\"closingIssuesReferences\":[{\"number\":7638}]}'\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            adapter = root / "adapter"
            adapter.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$ADAPTER_LOG\"\n"
                "exit 23\n",
                encoding="utf-8",
            )
            adapter.chmod(0o755)
            env = dict(
                os.environ,
                GH_BIN=str(gh),
                CI_ADMISSION_ADAPTER=str(adapter),
                ADAPTER_LOG=str(log),
                RLGC_CHECKOUT=str(root),
            )
            completed = subprocess.run(
                [str(CALLER), "--pr", "7639"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(completed.returncode, 23)
            self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)
            self.assertIn("--head " + "c" * 40, log.read_text(encoding="utf-8"))

    def test_ambiguous_linked_issue_stops_before_adapter_effect(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            gh = root / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' '{\"number\":7639,\"headRefOid\":\""
                + "a" * 40
                + "\",\"baseRefOid\":\""
                + "b" * 40
                + "\",\"closingIssuesReferences\":[{\"number\":7638},{\"number\":7637}]}'\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            adapter = root / "adapter"
            adapter.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
            adapter.chmod(0o755)
            env = dict(
                os.environ,
                GH_BIN=str(gh),
                CI_ADMISSION_ADAPTER=str(adapter),
                RLGC_CHECKOUT=str(root),
            )
            completed = subprocess.run(
                [str(CALLER), "--pr", "7639"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("linked_issue_relationship_ambiguous", completed.stderr)

    def test_slot_ready_event_forwards_exact_tuple_once(self) -> None:
        head = "d" * 40
        base = "e" * 40
        with TemporaryDirectory() as temp:
            root = Path(temp)
            event = root / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "pr": 7639,
                        "issue": 7638,
                        "head": head,
                        "base": base,
                        "checkout": str(root),
                    }
                ),
                encoding="utf-8",
            )
            log = root / "adapter.log"
            adapter = root / "adapter"
            adapter.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" > \"$ADAPTER_LOG\"\n",
                encoding="utf-8",
            )
            adapter.chmod(0o755)
            env = dict(
                os.environ,
                CI_ADMISSION_ADAPTER=str(adapter),
                ADAPTER_LOG=str(log),
            )
            completed = subprocess.run(
                [str(CALLER), "--slot-ready-event", str(event)],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            args = log.read_text(encoding="utf-8")
            self.assertEqual(args.count("--pr 7639"), 1)
            self.assertIn("--issue 7638", args)
            self.assertIn("--head " + head, args)
            self.assertIn("--base " + base, args)
            self.assertIn("--checkout " + str(root), args)

    def test_retired_recovery_mode_is_a_zero_effect_refusal(self) -> None:
        with TemporaryDirectory() as temp:
            adapter = Path(temp) / "adapter"
            adapter.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
            adapter.chmod(0o755)
            env = dict(os.environ, CI_ADMISSION_ADAPTER=str(adapter))
            completed = subprocess.run(
                [str(CALLER), "--pr", "7639", "--recover-missing-edge"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("retired_recovery_mode_unsupported", completed.stderr)

    def test_manifest_mapping_and_mode(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        entry = next(
            item
            for item in manifest["entries"]
            if item["source_path"] == "claude/scripts/ci/request-label-gated-ci.sh"
        )
        self.assertEqual(
            entry["canonical_target"],
            "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ci/request-label-gated-ci.sh",
        )
        self.assertEqual(entry["mode"], 0o755)
        self.assertEqual(stat.S_IMODE(CALLER.stat().st_mode), 0o755)

        adapter_entry = next(
            item
            for item in manifest["entries"]
            if item["source_path"] == "claude/scripts/ci/heydonna-cto-label-gated-ci.py"
        )
        self.assertEqual(adapter_entry["sha256"], "f955e4e9cb92e74e2b40b9a07baee3beb2c852c57459e7e04a59f5e07501f43f")
        self.assertEqual(stat.S_IMODE(ADAPTER.stat().st_mode), 0o755)

        runtime_entry = next(
            item
            for item in manifest["entries"]
            if item["source_path"] == "claude/control_plane/runtime_observation.py"
        )
        self.assertEqual(
            runtime_entry["sha256"],
            hashlib.sha256(RUNTIME.read_bytes()).hexdigest(),
        )
        self.assertEqual(stat.S_IMODE(RUNTIME.stat().st_mode), 0o644)

    def test_active_mapped_sources_have_no_retired_dependency(self) -> None:
        for path in (CALLER, ADAPTER, RUNTIME):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("pm-operator", text)
            self.assertNotIn("pm_operator", text)
            self.assertNotIn("pm-transition.sh", text)
            self.assertNotIn("pm_transition", text)
        self.assertIn("RuntimeObservationAdapter", RUNTIME.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
