"""The hourly report always surfaces both remote pools or a visible failure."""
import importlib.util
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py"
SPEC = importlib.util.spec_from_file_location("heartbeat_remote_boxes", SOURCE)
composer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(composer)

# The list command reports browser first, followed by test and queue rows.
LIST = """BOX                          TYPE     OWNER            PR     AGE_MIN  TTL_MIN  BRANCH
boxes: 0/20
== browser pool (cx43, rtb-pool-NN, target 8) ==
POOL_BOX               STATE      OWNER                    TTL_MIN  LEASE_ALIAS/TICKET             PRIO
rtb-pool-02-115935     expired    qa-spec                  -        ticket                         P-
rtb-pool-01-115933     leased     qa-spec                  135      ticket                         P-
rtb-pool-05-124524     broken     qa-spec                  -        ticket                         P-
rtb-pool-03-095944     free       -                        -        -                              -
pool: 1 free / 8, 0 queued
== test pool (cx33, rtb-tpool-NN, target 12) ==
POOL_BOX               STATE      OWNER                    TTL_MIN  LEASE_ALIAS/TICKET             PRIO
rtb-tpool-01-190156    leased     slot1                    59       ticket                         P1
rtb-tpool-03-190441    releasing  slot8                    -        ticket                         P1
rtb-tpool-04-190600    free       -                        -        -                              -
queue:ddf05518b0ca     waiting    hotfix                   -        rtb-tpool-04-190600            P1
pool: 1 free / 12, 1 queued
"""


def test_real_list_format_both_pools(monkeypatch):
    monkeypatch.setattr(composer.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=LIST))
    line = composer.remote_boxes_line()
    assert "test pool 1 free / 12 (1 leased; 1 releasing; 0 expired; 0 broken)" in line
    assert "browser pool 1 free / 8 (1 leased; 0 releasing; 1 expired: rtb-pool-02-115935; 1 broken: rtb-pool-05-124524)" in line


def test_failure_is_rendered_in_hourly(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 20)
    monkeypatch.setattr(composer.subprocess, "run", timeout)
    line = composer.remote_boxes_line()
    assert line == "*Remote boxes:* unavailable (UNAVAILABLE (timeout 20s))"
    post = composer.compose("1h", datetime.now(timezone.utc), None, {}, [], training=("*Training:* no run active", []), remote_boxes=line)
    assert "\n\n" + line in post


def test_after_slots_with_one_blank_line():
    line = "*Remote boxes:* test pool 1 free / 12"
    post = composer.compose("1h", datetime.now(timezone.utc), None, {}, [], slots={"slots": []}, include_slots=True,
                            training=("*Training:* no run active", []), remote_boxes=line)
    assert "*Slots" not in post and "\n\n" + line + "\n\n" in post


def test_invalid_or_incomplete_output_is_unavailable(monkeypatch):
    for output in (LIST.replace("pool: 1 free / 12", "pool: 9 free / 12"), LIST.split("== test pool")[0], LIST.replace("releasing", "newstate")):
        monkeypatch.setattr(composer.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=output))
        assert composer.remote_boxes_line().startswith("*Remote boxes:* unavailable (")
