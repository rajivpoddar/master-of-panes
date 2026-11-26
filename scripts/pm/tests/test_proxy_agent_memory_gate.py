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
])
def test_memory_gate(tmp_path, free, used, minimum, maximum, rc, message):
    pressure = tmp_path / "memory_pressure"
    pressure.write_text('#!/bin/bash\n[[ $# == 1 && $1 == -Q && -n $FREE ]] || exit 1\n'
                        'printf "System-wide memory free percentage: %s%%\\n" "$FREE"\n')
    swap = tmp_path / "sysctl"
    swap.write_text('#!/bin/bash\n[[ $# == 2 && $1 == -n && $2 == vm.swapusage ]] || exit 1\n'
                    'printf "total = 4096.00M  used = %s  free = 0.00M  (encrypted)\\n" "$USED"\n')
    for cmd in (pressure, swap):
        cmd.chmod(0o755)
    run = subprocess.run(
        ["/bin/bash", "-euc", "set -o pipefail\nproxy_agent_memory_gate() {" + GATE + "\n}\nproxy_agent_memory_gate"],
        env={"PATH": f"{tmp_path}{os.pathsep}/usr/bin:/bin", "FREE": "" if free is None else str(free),
             "USED": used, "PROXY_AGENT_DRY_RUN": "1", "PROXY_AGENT_MIN_FREE_PCT": minimum,
             "PROXY_AGENT_MAX_SWAP_MB": maximum},
        capture_output=True, text=True, check=False,
    )
    assert run.returncode == rc, run.stderr
    assert run.stderr == f"proxy-agent: {message}\n"
    assert not run.stdout
