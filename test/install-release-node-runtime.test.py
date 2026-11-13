"""Release staging must use launchd's Node and load its native dependency."""
import importlib.util
import plistlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("installer_node", Path(__file__).parents[1] / "scripts/install-release.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ServiceRuntimeTests(unittest.TestCase):
    def test_service_runtime_selected_and_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True)
            node22 = home / "node22"
            node24 = home / "node24"
            node22.touch()
            node24.touch()
            (agents / "service.plist").write_bytes(plistlib.dumps({"ProgramArguments": [str(node22), "dist/server.js"]}))
            with patch.object(m.Path, "home", return_value=home):
                self.assertEqual(m.service_node("service"), str(node22))
                with self.assertRaises(m.InstallerError):
                    m.service_node("service", str(node24))

    def test_native_check_opens_in_memory_database_with_pinned_node(self):
        with patch.object(m, "_run") as run:
            m.verify_native_runtime(Path("/release"), "/node22")
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "/node22")
        self.assertIn("new D(':memory:').close()", args[0][2])
        self.assertEqual(kwargs["cwd"], Path("/release"))
