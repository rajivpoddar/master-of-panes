from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/pm/shared-assets/claude/scripts/mop-healthcheck.sh"


class HealthcheckStartupGraceTests(unittest.TestCase):
    """Run the real script against stub launchctl/ps/curl/nohup binaries."""

    def run_tick(self, *, loaded=True, pid="4242", etime="01:40", kickstart_ok=True, counter=0):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "calls.log"
            mop_dir = root / "mop"
            (mop_dir / "dist").mkdir(parents=True)

            def stub(name: str, body: str) -> None:
                path = bin_dir / name
                path.write_text("#!/bin/bash\n" + body)
                path.chmod(path.stat().st_mode | stat.S_IEXEC)

            pid_line = f"  pid = {pid}\\n" if pid else ""
            stub(
                "launchctl",
                f'''echo "launchctl $*" >> "{calls}"
case "$1" in
  print) [ "{int(loaded)}" = 1 ] && printf "service = x\\n{pid_line}" || exit 113 ;;
  kickstart) [ "{int(kickstart_ok)}" = 1 ] || exit 1 ;;
esac
''',
            )
            stub("ps", f'echo "{etime}"\n')
            stub("curl", "exit 7\n")
            stub("sleep", "exit 0\n")
            stub("lsof", "exit 0\n")
            stub("nohup", f'echo "nohup $*" >> "{calls}"\n')
            counter_file = root / "count"
            counter_file.write_text(f"{counter}\n")
            env = {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HOME": str(root),
                "MOP_HC_LOG_FILE": str(root / "hc.log"),
                "MOP_HC_COUNTER_FILE": str(counter_file),
                "MOP_HC_SERVER_LOG": str(root / "server.log"),
                "MOP_HC_DIR": str(mop_dir),
                "MOP_STARTUP_GRACE_SECONDS": "150",
            }
            subprocess.run(["/bin/bash", str(SCRIPT)], env=env, check=True, capture_output=True)
            time.sleep(1.0)  # the fallback server is spawned in the background
            log = calls.read_text() if calls.exists() else ""
            return log, int(counter_file.read_text().strip())

    def test_starting_child_within_grace_is_left_alone(self):
        log, counter = self.run_tick(etime="01:40", counter=2)
        self.assertNotIn("kickstart", log)
        self.assertNotIn("nohup", log)
        self.assertEqual(counter, 0)

    def test_expired_grace_gets_exactly_one_kickstart_and_no_competing_server(self):
        log, counter = self.run_tick(etime="03:20", counter=2)
        self.assertEqual(log.count("kickstart -k"), 1)
        self.assertNotIn("nohup", log)
        self.assertEqual(counter, 0)

    def test_launchd_absent_falls_back(self):
        log, _ = self.run_tick(loaded=False, counter=2)
        self.assertNotIn("kickstart", log)
        self.assertIn("nohup node dist/server.js", log)

    def test_launchd_start_failure_falls_back(self):
        log, _ = self.run_tick(etime="05:00", kickstart_ok=False, counter=2)
        self.assertEqual(log.count("kickstart -k"), 1)
        self.assertIn("nohup node dist/server.js", log)


if __name__ == "__main__":
    unittest.main()
