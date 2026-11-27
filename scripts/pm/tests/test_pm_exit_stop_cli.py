"""The installed CLI and Stop runner are the two user-facing PM exit paths."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets/claude"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ASSETS / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PmExitPathsTest(unittest.TestCase):
    def test_cli_arms_pm_by_default_and_requires_explicit_dev_scope(self):
        cli = load("test_mop_cli_exit", "scripts/mop.py")
        for flags, expected in (
            (["--on"], {"enabled": True, "pm_only": True}),
            (["--on", "--dev-only"], {"enabled": True, "dev_only": True}),
            (["--off"], {"enabled": False}),
        ):
            with self.subTest(flags=flags), patch.object(cli, "http_json", return_value=(200, {})) as post, redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["exit-pending", *flags]), 0)
                self.assertEqual(post.call_args.args[2], expected)

    def test_stop_runner_skips_unrelated_children_and_only_blocks_on_owned_row(self):
        hook = load("test_mop_pm_exit_stop", "hooks/pm-stop-group-runner.py")
        for row in (None, "44723"):
            stdout = io.StringIO()
            with self.subTest(row=row), patch.object(sys, "argv", ["runner", "obligations"]), \
                    patch.object(sys, "stdin", io.StringIO('{"session_id":"pm"}')), redirect_stdout(stdout), \
                    patch.object(hook, "debug_log"), patch.object(hook, "load_groups", return_value={"obligations": ()}), \
                    patch.object(hook, "pm_exit_stop", return_value={"pending": True, "block_obligation_id": row}) as stop, \
                    patch.object(hook, "run_child") as child, patch.object(hook, "ThreadPoolExecutor") as pool:
                self.assertEqual(hook.main(), 0)
                stop.assert_called_once_with("pm")
                child.assert_not_called()
                pool.assert_not_called()
            if row is None:
                self.assertEqual(stdout.getvalue(), "")
            else:
                output = json.loads(stdout.getvalue())
                self.assertEqual(output["decision"], "block")
                self.assertIn("#44723", output["reason"])


if __name__ == "__main__":
    unittest.main()
