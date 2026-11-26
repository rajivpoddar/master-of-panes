"""Launcher memory admission with stubbed macOS commands; no model launch."""
import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).parents[1] / "shared-assets/claude/scripts/proxy-agent.sh"
GATE = LAUNCHER.read_text().split("proxy_agent_memory_gate() {", 1)[1].split("\n}\n", 1)[0]


@pytest.mark.parametrize("free, used, minimum, maximum, rc, message", [
    (86, "0.00M", "", "", 0, "ALLOWED reason=MEMORY_PRESSURE free=86 swap_used=0"),
    (20, "1.00G", "", "", 0, "ALLOWED reason=MEMORY_PRESSURE free=20 swap_used=1024"),
    (19, "0.25M", "", "", 75, "REFUSED reason=MEMORY_PRESSURE free=19 swap_used=0.25"),
    (86, "1024.01M", "", "", 75, "REFUSED reason=MEMORY_PRESSURE free=86 swap_used=1024.01"),
    (26, "768K", "27", "1", 75, "REFUSED reason=MEMORY_PRESSURE free=26 swap_used=0.75"),
    (26, "768K", "26", "0.5", 75, "REFUSED reason=MEMORY_PRESSURE free=26 swap_used=0.75"),
    (26, "768K", "26", "0.75", 0, "ALLOWED reason=MEMORY_PRESSURE free=26 swap_used=0.75"),
    (None, "0.25M", "", "", 0, "WARN reason=MEMORY_PRESSURE_UNAVAILABLE free=unknown swap_used=0.25 action=allow"),
    (86, "garbage", "", "", 0, "WARN reason=MEMORY_PRESSURE_UNAVAILABLE free=86 swap_used=unknown action=allow"),
    (19, "", "", "", 75, "REFUSED reason=MEMORY_PRESSURE free=19 swap_used=unknown"),
    (None, "1024.01M", "", "", 75, "REFUSED reason=MEMORY_PRESSURE free=unknown swap_used=1024.01"),
    (None, "", "", "", 0, "WARN reason=MEMORY_PRESSURE_UNAVAILABLE free=unknown swap_used=unknown action=allow"),
])
def test_memory_gate(tmp_path, free, used, minimum, maximum, rc, message):
    run = subprocess.run(
        ["/bin/bash", "-euc", "set -o pipefail\nproxy_agent_memory_gate() {" + GATE + "\n}\nproxy_agent_memory_gate"],
        env={**probe_env(tmp_path, free, used), "PROXY_AGENT_DRY_RUN": "1",
             "PROXY_AGENT_MIN_FREE_PCT": minimum, "PROXY_AGENT_MAX_SWAP_MB": maximum},
        capture_output=True, text=True, check=False,
    )
    assert run.returncode == rc, run.stderr
    assert run.stderr == f"proxy-agent: {message}\n"
    assert not run.stdout


def probe_env(tmp_path, free, used):
    stubs = {
        "memory_pressure": '[[ $# == 1 && $1 == -Q && -n $FREE ]] || exit 1\n'
                           'printf "System-wide memory free percentage: %s%%\\n" "$FREE"\n',
        "sysctl": '[[ $# == 2 && $1 == -n && $2 == vm.swapusage && -n $USED ]] || exit 1\n'
                  'printf "total = 4096.00M  used = %s  free = 0.00M  (encrypted)\\n" "$USED"\n',
    }
    for name, script in stubs.items():
        path = tmp_path / name
        path.write_text("#!/bin/bash\n" + script)
        path.chmod(0o755)
    return {"PATH": f"{tmp_path}{os.pathsep}/usr/bin:/bin", "FREE": "" if free is None else str(free), "USED": used}


@pytest.mark.parametrize("free, expect_preemption", [(19, False), (20, True)])
def test_p0_checks_memory_before_preempting(tmp_path, free, expect_preemption):
    scripts = tmp_path / ".claude/scripts"
    scripts.mkdir(parents=True)
    (scripts / "proxy-run-state.py").write_text("# No live run state\n")
    helper = scripts / "proxy-preempt.py"
    calls = tmp_path / "preempt-called"
    # Exit at the full pool in the control case, before any model probes or launch.
    helper.write_text(f"from pathlib import Path\nPath({str(calls)!r}).touch()\nraise SystemExit(3)\n")
    brief = tmp_path / "brief.md"
    brief.write_text("PRIORITY: P0\n")
    run = subprocess.run(
        ["/bin/bash", str(LAUNCHER), "hd-builder", str(brief), "--cwd", str(tmp_path), "--model", "gpt-6.1-terra"],
        env={**probe_env(tmp_path, free, "0.00M"), "HOME": str(tmp_path), "CODEX_PROXY_CAP": "1",
             "PROXY_AGENT_STUB_POOL_LIVE": "1", "PROXY_AGENT_PREEMPT": "1", "PROXY_AGENT_SANDBOX": "0",
             "PROXY_AGENT_DISK_MIN_FREE_KIB": "0"},
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert run.returncode == 75, run.stderr
    assert calls.exists() == expect_preemption, run.stderr
    expected_reason = "(codex pool_cap)" if expect_preemption else "REFUSED reason=MEMORY_PRESSURE free=19 swap_used=0"
    assert expected_reason in run.stderr
