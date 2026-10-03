import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "install-relay-hooks.py")
PLUGIN_HOOKS = os.path.join(HERE, "..", "hooks", "hooks.json")


class InstallRelayHooksTest(unittest.TestCase):
    def run_it(self, path):
        subprocess.run([sys.executable, SCRIPT, path, "--scripts-dir", "/x/master-of-panes/current/scripts"],
                       check=True, capture_output=True)
        with open(path) as f:
            return json.load(f)

    def test_covers_every_plugin_hook_event_disables_plugin_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.local.json")
            orig = {"permissions": {"allow": ["x"]},
                    "enabledPlugins": {"master-of-panes@rajiv-plugins": True, "other": True},
                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}}
            with open(p, "w") as f:
                json.dump(orig, f)
            s1 = self.run_it(p)
            s2 = self.run_it(p)
            self.assertEqual(s1, s2)
            self.assertTrue(os.path.exists(p + ".bak-noplugin"))
            self.assertFalse(s1["enabledPlugins"]["master-of-panes@rajiv-plugins"])
            self.assertTrue(s1["enabledPlugins"]["other"])
            self.assertEqual(s1["permissions"], orig["permissions"])
            with open(PLUGIN_HOOKS) as f:
                plugin_events = set(json.load(f)["hooks"].keys())
            for ev in plugin_events:
                cmds = [h["command"] for g in s1["hooks"][ev] for h in g["hooks"]]
                self.assertTrue(any("hook-relay.sh\" " + ev in c for c in cmds), ev)
                self.assertFalse(any("CLAUDE_PLUGIN_ROOT" in c for c in cmds))
            stop = [h["command"] for g in s1["hooks"]["Stop"] for h in g["hooks"]]
            self.assertIn("keep-me", stop)
            self.assertTrue(any("--cleanup-session" in c for c in stop))
            self.assertEqual(len(stop), 3)


if __name__ == "__main__":
    unittest.main()
