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

    def test_mixed_group_keeps_non_mop_commands_and_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.local.json")
            mop_old = '/Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/scripts/hook-relay.sh Stop'
            orig = {"hooks": {
                "Stop": [{"matcher": "keep-matcher", "hooks": [
                    {"type": "command", "command": "pm-stop-validator.sh", "timeout": 8},
                    {"type": "command", "command": mop_old, "timeout": 5}]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "safety.sh"}]}],
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "audit.sh"},
                                                {"type": "command", "command": "bash \"/old/master-of-panes/current/scripts/hook-relay.sh\" UserPromptSubmit"}]}],
            }}
            with open(p, "w") as f:
                json.dump(orig, f)
            s1 = self.run_it(p)
            s2 = self.run_it(p)
            self.assertEqual(s1, s2)
            stop0 = s1["hooks"]["Stop"][0]
            self.assertEqual(stop0["matcher"], "keep-matcher")
            self.assertEqual(stop0["hooks"], [{"type": "command", "command": "pm-stop-validator.sh", "timeout": 8}])
            self.assertEqual(s1["hooks"]["PreToolUse"][0], orig["hooks"]["PreToolUse"][0])
            for ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
                self.assertEqual(sum("hook-relay.sh" in h["command"] for g in s1["hooks"][ev] for h in g["hooks"]), 1)
            ups = [h["command"] for g in s1["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
            self.assertIn("audit.sh", ups)
            self.assertFalse(any("/old/" in c for c in ups))
            stop = [h["command"] for g in s1["hooks"]["Stop"] for h in g["hooks"]]
            self.assertEqual(sum("hook-relay.sh" in c for c in stop), 1)

    def test_dev_slot_settings_keep_other_relays_but_not_stop_relay(self):
        with tempfile.TemporaryDirectory() as d:
            slot = os.path.join(d, "heydonna-app-3002", ".claude")
            os.makedirs(slot)
            p = os.path.join(slot, "settings.json")
            orig = {"env": {"HEYDONNA_SLOT_STOP_GATED": "1"}, "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": 'bash "/old/master-of-panes/current/scripts/hook-relay.sh" Stop'},
                {"type": "command", "command": "keep-stop"},
            ]}]}}
            with open(p, "w") as f:
                json.dump(orig, f)
            s1 = self.run_it(p)
            s2 = self.run_it(p)
            self.assertEqual(s1, s2)
            stop = [h["command"] for g in s1["hooks"]["Stop"] for h in g["hooks"]]
            self.assertIn("keep-stop", stop)
            self.assertFalse(any("hook-relay.sh" in c for c in stop))
            self.assertTrue(any("--cleanup-session" in c for c in stop))
            ups = [h["command"] for g in s1["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
            self.assertTrue(any("hook-relay.sh\" UserPromptSubmit" in c for c in ups))
            pm = os.path.join(d, "heydonna-app", ".claude")
            os.makedirs(pm)
            pm_settings = os.path.join(pm, "settings.json")
            with open(pm_settings, "w") as f:
                json.dump({}, f)
            pm_out = self.run_it(pm_settings)
            pm_stop = [h["command"] for g in pm_out["hooks"]["Stop"] for h in g["hooks"]]
            self.assertTrue(any("hook-relay.sh\" Stop" in c for c in pm_stop))

    def test_dev_gate_repair_preserves_hooks_and_missing_gate_fails_loud(self):
        with tempfile.TemporaryDirectory() as d:
            slot = os.path.join(d, "heydonna-app-3001", ".claude")
            os.makedirs(slot)
            path = os.path.join(slot, "settings.json")
            original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}}
            with open(path, "w") as f:
                json.dump(original, f)
            def run(*args):
                return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)
            self.assertNotEqual(run(path).returncode, 0)
            self.assertNotEqual(run("--check-slot-stop-gates", d).returncode, 0)
            self.assertEqual(run(path, "--ensure-slot-stop-gate").returncode, 0)
            with open(path) as f:
                repaired = json.load(f)
            self.assertEqual(repaired["hooks"], original["hooks"])
            self.assertEqual(repaired["env"]["HEYDONNA_SLOT_STOP_GATED"], "1")
            for slot_num in range(2, 5):
                other = os.path.join(d, f"heydonna-app-300{slot_num}", ".claude")
                os.makedirs(other)
                with open(os.path.join(other, "settings.json"), "w") as f:
                    json.dump({"env": {"HEYDONNA_SLOT_STOP_GATED": "1"}}, f)
            self.assertEqual(run("--check-slot-stop-gates", d).returncode, 0)
            pm = os.path.join(d, "heydonna-app", ".claude", "settings.json")
            self.assertNotEqual(run(pm, "--ensure-slot-stop-gate").returncode, 0)

    def test_mode_retained_and_new_files_created_0600(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.local.json")
            with open(p, "w") as f:
                json.dump({}, f)
            os.chmod(p, 0o600)
            self.run_it(p)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            os.chmod(p, 0o640)
            with open(p) as f:
                data = json.load(f)
            data["enabledPlugins"]["master-of-panes@rajiv-plugins"] = True
            with open(p, "w") as f:
                json.dump(data, f)
            self.run_it(p)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)
            fresh = os.path.join(d, "new.json")
            self.run_it(fresh)
            self.assertEqual(os.stat(fresh).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
