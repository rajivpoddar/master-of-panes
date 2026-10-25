from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/pm/shared-assets/claude/scripts/mop-healthcheck.sh"


class Sandbox:
    """One persistent sandbox (identity, counter, log, world state) across ticks.

    The real script runs against stub launchctl/ps/curl/lsof binaries whose
    behavior is read from state files, so a sequence of ticks models a real
    watchdog timeline. The child "age" advances 60s per tick.
    """

    def __init__(self, tmp: Path, *, loaded=True, list_ok=False, kickstart_ok=True, child_age=30, listener=False):
        self.root = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.calls = tmp / "calls.log"
        self.calls.write_text("")
        self.state = tmp / "state"
        self.state.mkdir()
        self.counter = tmp / "count"
        self.counter.write_text("0\n")
        self.log_file = tmp / "hc.log"
        releases = tmp / "releases"
        self.release_a = releases / "release-a"
        self.release_b = releases / "release-b"
        for release in (self.release_a, self.release_b):
            (release / "dist").mkdir(parents=True)
            (release / "dist" / "server.js").write_text("//\n")
        self.current = tmp / "current"
        os.symlink(self.release_b, self.current)
        self.plist = tmp / "server.plist"
        self.plist.write_text("<plist/>\n")
        self.set(loaded=loaded, list_ok=list_ok, kickstart_ok=kickstart_ok, age=child_age, listener=listener)
        sandbox_calls = self.calls

        def stub(name: str, body: str) -> None:
            path = self.bin / name
            path.write_text("#!/bin/bash\n" + body)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

        st = self.state
        stub(
            "launchctl",
            f'''echo "launchctl $*" >> "{sandbox_calls}"
case "$1" in
  print) [ "$(cat {st}/loaded)" = 1 ] && printf "service = x\\n\\tpid = 4242\\n" || exit 113 ;;
  list) [ "$(cat {st}/list_ok)" = 1 ] || exit 113 ;;
  kickstart) [ "$(cat {st}/kickstart_ok)" = 1 ] || exit 1; echo 0 > {st}/age ;;
  bootstrap) echo 1 > {st}/loaded ;;
esac
''',
        )
        stub("ps", f'a=$(cat {st}/age); printf "%02d:%02d\\n" $((a/60)) $((a%60))\n')
        stub("curl", "exit 7\n")
        stub("sleep", "exit 0\n")
        stub("lsof", f'echo "lsof $*" >> "{sandbox_calls}"; [ "$(cat {st}/listener)" = 1 ] && echo 999; exit 0\n')
        stub("nohup", f'echo "nohup $*" >> "{sandbox_calls}"\n')
        stub("kill", f'echo "kill $*" >> "{sandbox_calls}"\n')
        stub("pkill", f'echo "pkill $*" >> "{sandbox_calls}"\n')

    def set(self, *, loaded=None, list_ok=None, kickstart_ok=None, age=None, listener=None):
        for name, value in (("loaded", loaded), ("list_ok", list_ok), ("kickstart_ok", kickstart_ok),
                            ("age", age), ("listener", listener)):
            if value is not None:
                (self.state / name).write_text(f"{int(value)}\n")

    def tick(self) -> None:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            "MOP_HC_LOG_FILE": str(self.log_file),
            "MOP_HC_COUNTER_FILE": str(self.counter),
            "MOP_HC_CURRENT": str(self.current),
            "MOP_HC_PLIST": str(self.plist),
            "MOP_STARTUP_GRACE_SECONDS": "150",
        }
        subprocess.run(["/bin/bash", str(SCRIPT)], env=env, check=True, capture_output=True)
        age = int((self.state / "age").read_text())
        (self.state / "age").write_text(f"{age + 60}\n")

    def calls_text(self) -> str:
        return self.calls.read_text()

    def count(self, token: str) -> int:
        return sum(1 for line in self.calls_text().splitlines() if token in line)

    def log(self) -> str:
        return self.log_file.read_text() if self.log_file.exists() else ""


class HealthcheckTests(unittest.TestCase):
    def sandbox(self, **kwargs) -> Sandbox:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Sandbox(Path(tmp.name), **kwargs)

    def test_script_has_no_bare_server_or_port_kill(self):
        text = SCRIPT.read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"(?m)^\s*(?:nohup|kill|pkill)\b|\$\(.*\bkill\b|plugins/cache", code))

    def test_finite_grace_then_exactly_one_kickstart_across_ticks(self):
        box = self.sandbox(child_age=0)  # freshly started child that never binds
        for _ in range(8):
            box.tick()
        # Ticks at child ages 0,60,120 are inside the 150s grace; failures are counted
        # only afterwards (180,240,300 -> threshold) and one kickstart restarts the child.
        self.assertEqual(box.count("kickstart"), 1)
        self.assertIn("STARTING", box.log())
        self.assertEqual(box.count("nohup") + box.count("bootstrap") + box.count("kill"), 0)

    def test_loaded_service_existing_child_start_failure_forbids_kill_and_nohup(self):
        box = self.sandbox(kickstart_ok=False, child_age=900)
        box.counter.write_text("2\n")
        box.tick()
        self.assertEqual(box.count("kickstart"), 1)
        self.assertEqual(box.count("nohup") + box.count("kill") + box.count("pkill"), 0)
        self.assertEqual(box.count("bootstrap"), 0)
        self.assertEqual(box.count("lsof"), 0, "no port inspection/kill path while launchd owns the service")
        self.assertIn("RECOVERY_FAILED", box.log())

    def test_permanent_start_failure_is_bounded_with_one_owner_across_ticks(self):
        box = self.sandbox(kickstart_ok=False, child_age=900)
        for _ in range(12):
            box.tick()
        # One kickstart attempt per 3-tick window: 4 in 12 ticks, each reported, never worked around.
        self.assertEqual(box.count("kickstart"), 4)
        self.assertEqual(box.log().count("RECOVERY_FAILED"), 4)
        self.assertEqual(box.count("nohup") + box.count("bootstrap") + box.count("kill") + box.count("pkill"), 0)
        self.assertEqual((box.state / "loaded").read_text().strip(), "1")

    def test_owner_absent_fallback_targets_the_pointer_selected_release(self):
        box = self.sandbox(loaded=False, list_ok=False)
        box.counter.write_text("2\n")
        box.tick()
        self.assertEqual(box.count("bootstrap"), 1)
        self.assertIn(str(box.plist), box.calls_text())
        self.assertIn(str(box.release_b.resolve()), box.log(), "fallback names the exact release the pointer selects")
        self.assertNotIn(str(box.release_a.resolve()), box.log())
        self.assertEqual(box.count("nohup") + box.count("kill"), 0)

    def test_owner_absent_fallback_follows_a_repointed_release(self):
        box = self.sandbox(loaded=False)
        box.current.unlink()
        os.symlink(box.release_a, box.current)
        box.counter.write_text("2\n")
        box.tick()
        self.assertIn(str(box.release_a.resolve()), box.log())
        self.assertNotIn(str(box.release_b.resolve()), box.log())

    def test_owner_absent_fallback_refuses_without_proven_absence_or_a_clean_port(self):
        unproven = self.sandbox(loaded=False, list_ok=True)  # print fails but list knows the label
        unproven.counter.write_text("2\n")
        unproven.tick()
        self.assertEqual(unproven.count("bootstrap") + unproven.count("nohup"), 0)

        foreign = self.sandbox(loaded=False, listener=True)
        foreign.counter.write_text("2\n")
        foreign.tick()
        self.assertEqual(foreign.count("bootstrap") + foreign.count("nohup") + foreign.count("kill"), 0)

        broken = self.sandbox(loaded=False)
        (broken.release_b / "dist" / "server.js").unlink()
        broken.counter.write_text("2\n")
        broken.tick()
        self.assertEqual(broken.count("bootstrap") + broken.count("nohup"), 0)


if __name__ == "__main__":
    unittest.main()
