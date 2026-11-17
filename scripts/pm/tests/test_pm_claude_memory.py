"""PM Claude RSS hourly line and the once-per-6h exit-pending arm.

Simulated RSS only. The arm opener and the Alerts notifier are fakes; nothing calls MoP, tmux or Slack.
The shared deterministic trace is test/fixtures/pm-rss-arm-trace.json (client_steps).
Alert send custody (CTO REVISE of da450c5, C0ALZJHGE49 thread 1794923203.112819, ts 1794925523.128429): the
guarded send intent is persisted BEFORE every dispatch, including already-pending; an unconfirmed issued send
stays consumed/UNKNOWN for the window and is never resent. Added, NOT executed (MoP changes need no local proof).
Single winner (CTO REVISE of bb561de1, C0ALZJHGE49 thread 1794923203.112819, ts 1794926555.931559): the window is
claimed under a non-blocking lock with the state re-read and re-decided under it; one winning intent is persisted,
and only that winner arms or alerts. Two callers at the same expired window yield one notifier call.
"""

import fcntl
import importlib.util
import io
import json
import subprocess
import threading
import types
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/pm-claude-memory.py"
spec = importlib.util.spec_from_file_location("pm_claude_memory", PATH)
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)

COMPOSE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py"
compose_spec = importlib.util.spec_from_file_location("heartbeat_compose_memory", COMPOSE)
composer = importlib.util.module_from_spec(compose_spec)
compose_spec.loader.exec_module(composer)

GB = 1024 ** 3


def processes():
    # pane shell 10 -> PM claude 20 (2.4GB) -> subagent claude 21 (3GB). px-foo 30 is 1.6GB. px-small 31 is under.
    return {
        10: (1, 1000, "zsh"),
        20: (10, int(2.4 * GB / 1024), "/usr/local/bin/claude --model opus"),
        21: (20, int(3.0 * GB / 1024), "/usr/local/bin/claude --name subagent"),
        30: (1, int(1.6 * GB / 1024), "/usr/local/bin/claude --name px-foo --model opus"),
        31: (1, int(1.0 * GB / 1024), "/usr/local/bin/claude --name px-small"),
        32: (1, int(1.8 * GB / 1024), "/usr/bin/node --name px-not-claude"),
    }


def test_nearest_claude_is_the_pane_claude_not_the_nested_subagent():
    found = memory.nearest_claude(10, processes())
    assert found is not None
    assert found[0] == 20
    assert found[1] == int(2.4 * GB / 1024) * 1024


def test_px_list_keeps_only_claude_processes_at_or_over_1_5gb():
    rows = memory.px_over(processes())
    assert rows == [("px-foo", 30, int(1.6 * GB / 1024) * 1024)]


def test_line_includes_pm_rss_and_px_over_floor():
    line = memory.format_line((20, int(2.4 * GB / 1024) * 1024), memory.px_over(processes()), "below")
    assert line.startswith("*Claude RSS:* PM 2.40GB pid 20")
    assert "px-foo 1.60GB pid 30" in line
    assert "px-small" not in line
    assert "exit-pending" not in line


def test_threshold_arms_once_then_cooldown(tmp_path):
    state = tmp_path / "state.json"
    calls = []

    def opener(payload):
        calls.append(payload)
        return {"ok": True, "status": {"pm_only": True, "with_devs": False}}

    snap = {"pm": (20, 2 * GB), "px": []}
    now = datetime.fromtimestamp(1_700_000_000, timezone.utc)
    first = memory.hourly_line(
        arm=True, now=now, collect=lambda: snap, state_path=state,
        status_get=lambda: {"pending": False}, opener=opener, env={},
    )
    second = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000 + 3600, timezone.utc),
        collect=lambda: snap, state_path=state, status_get=lambda: {"pending": False},
        opener=opener, env={},
    )
    assert "exit-pending armed" in first
    assert calls[0]["pm_only"] is True and calls[0]["with_devs"] is False
    assert calls[0]["continuation_prompt"]
    assert len(calls) == 1
    assert "exit-pending cooldown" in second


def test_under_threshold_and_env_override_do_not_arm_or_call_mop(tmp_path):
    state = tmp_path / "state.json"
    called = []
    snap = {"pm": (20, 2 * GB - 1), "px": []}
    line = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000, timezone.utc),
        collect=lambda: snap, state_path=state, status_get=lambda: called.append("status"),
        opener=lambda _payload: called.append("arm"), env={},
    )
    assert "exit-pending" not in line
    assert called == []
    armed = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000, timezone.utc),
        collect=lambda: snap, state_path=state, status_get=lambda: {"pending": False},
        opener=lambda payload: called.append(payload), env={"MOP_PM_RSS_EXIT_BYTES": str(GB)},
    )
    assert "exit-pending armed" in armed
    assert called and called[0]["with_devs"] is False


def test_dry_run_over_threshold_does_not_call_mop(tmp_path):
    state = tmp_path / "state.json"
    called = []
    line = memory.hourly_line(
        arm=False, now=datetime.fromtimestamp(1_700_000_000, timezone.utc),
        collect=lambda: {"pm": (7, 3 * GB), "px": []}, state_path=state,
        status_get=lambda: called.append("status"), opener=lambda _p: called.append("arm"), env={},
    )
    assert "dry-run would arm" in line
    assert called == []
    assert not state.exists()


def test_opener_failure_is_fail_open_and_starts_the_cooldown(tmp_path):
    state = tmp_path / "state.json"

    def opener(_payload):
        raise TimeoutError("lost")

    now = datetime.fromtimestamp(1_700_000_000, timezone.utc)
    first = memory.hourly_line(
        arm=True, now=now, collect=lambda: {"pm": (4, 3 * GB), "px": []}, state_path=state,
        status_get=lambda: {"pending": False}, opener=opener, env={},
    )
    second = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000 + 10, timezone.utc),
        collect=lambda: {"pm": (4, 3 * GB), "px": []}, state_path=state,
        status_get=lambda: {"pending": False}, opener=opener, env={},
    )
    assert "uncertain" in first
    assert "cooldown" in second


def test_unreachable_mop_does_not_raise(tmp_path):
    def status_get():
        raise OSError("down")

    line = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000, timezone.utc),
        collect=lambda: {"pm": (4, 3 * GB), "px": []}, state_path=tmp_path / "state.json",
        status_get=status_get, opener=lambda _p: None, env={},
    )
    assert "MoP unreachable" in line


def test_compose_places_the_rss_line_on_1h_only():
    now = datetime.now(timezone.utc)
    line = "*Claude RSS:* PM 2.40GB pid 20; px over 1.5GB: none"
    hourly = composer.compose("1h", now, {"events": 0}, {}, [], memory=line)
    three = composer.compose("3h", now, {"events": 0}, {}, [], memory=line)
    assert line in hourly
    assert line not in three
    assert "*Claude RSS:*" not in composer.compose("1h", now, {"events": 0}, {}, [])


TRACE = json.loads((Path(__file__).resolve().parents[3] / "test/fixtures/pm-rss-arm-trace.json").read_text())


@pytest.mark.parametrize("step", TRACE["client_steps"], ids=lambda step: step["name"])
def test_shared_client_trace(step, tmp_path):
    state = tmp_path / "state.json"
    if step.get("state"):
        state.write_text(json.dumps(step["state"]))
    snap = step["snapshot"]
    pm = None if snap["pm_rss_bytes"] is None else (20, snap["pm_rss_bytes"])
    px = None if snap["px"] is None else [tuple(row) for row in snap["px"]]
    payloads, alerts, at_dispatch = [], [], []

    def opener(payload):
        payloads.append(payload)
        kind = step.get("opener", "ok")
        if kind == "owner_preserved":
            raise memory.OwnerPreserved("owner_state_preserved")
        if kind == "timeout":
            raise TimeoutError("lost")
        return {"ok": True, "status": {"pm_only": True, "with_devs": False}}

    def notifier(text):
        alerts.append(text)
        # The guarded send intent must already be on disk when the dispatch happens (CTO REVISE of da450c5).
        at_dispatch.append(json.loads(state.read_text()) if state.exists() else None)
        kind = step["notifier"]
        if kind == "raise":
            raise RuntimeError("slack down")
        return kind == "ok"

    def status_get():
        if step.get("status") == "down":
            raise OSError("down")
        return {"pending": step.get("status") == "pending"}

    line = memory.hourly_line(
        arm=step["arm"], now=datetime.fromtimestamp(step.get("now_epoch", 1_700_000_000), timezone.utc),
        collect=lambda: {"pm": pm, "px": px}, state_path=state, status_get=status_get,
        opener=opener, env={}, cooldown_s=step.get("cooldown_s", memory.COOLDOWN_S),
        notifier=notifier if "notifier" in step else None,
    )
    expect = step["expect"]
    for text in expect.get("contains", []):
        assert text in line, (step["name"], line)
    for text in expect.get("not_contains", []):
        assert text not in line, (step["name"], line)
    assert len(payloads) == expect["opener_calls"]
    assert len(alerts) == expect["alerts"]
    for key, value in expect.get("payload", {}).items():
        assert payloads[0][key] == value
    for text in expect.get("alert_contains", []):
        assert text in alerts[0]
    for text in expect.get("alert_not_contains", []):
        assert text not in alerts[0]
    saved = json.loads(state.read_text()) if state.exists() else {}
    if "state_exists" in expect:
        assert state.exists() is expect["state_exists"]
    if "state_status" in expect:
        assert saved["status"] == expect["state_status"]
    if "state_has_last_attempt_epoch" in expect:
        assert ("last_attempt_epoch" in saved) is expect["state_has_last_attempt_epoch"]
    if "state_last_attempt_epoch" in expect:
        assert saved["last_attempt_epoch"] == expect["state_last_attempt_epoch"]
    if "state_alert" in expect:
        assert saved["alert"] == expect["state_alert"]
    for key, value in expect.get("state_at_dispatch", {}).items():
        assert at_dispatch[0] is not None and at_dispatch[0][key] == value, (step["name"], at_dispatch)


def test_live_snapshot_reports_unavailable_when_ps_fails_and_pm_unknown_when_only_tmux_fails(monkeypatch):
    def failing_ps(args, **_kwargs):
        raise OSError("ps missing")

    monkeypatch.setattr(memory.subprocess, "run", failing_ps)
    assert memory.live_snapshot() == {"pm": None, "px": None}

    table = "10 1 1000 zsh\n30 1 1677721 /usr/local/bin/claude --name px-foo --model opus\n"

    def ps_ok_tmux_fails(args, **_kwargs):
        if args[0] == "ps":
            return types.SimpleNamespace(stdout=table, returncode=0)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(memory.subprocess, "run", ps_ok_tmux_fails)
    snap = memory.live_snapshot()
    assert snap["pm"] is None
    assert [row[0] for row in snap["px"]] == ["px-foo"]

    monkeypatch.setattr(memory.subprocess, "run", lambda args, **_k: types.SimpleNamespace(stdout="", returncode=0))
    assert memory.live_snapshot() == {"pm": None, "px": None}, "an unreadable (empty) table is unavailable, not none"


def test_live_arm_maps_only_the_servers_owner_preserved_409(monkeypatch):
    def respond(code, body):
        def fake(_request, timeout=0):
            raise urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(json.dumps(body).encode()))
        return fake

    monkeypatch.setattr(memory.urllib.request, "urlopen", respond(409, {"ok": False, "reason": "owner_state_preserved"}))
    with pytest.raises(memory.OwnerPreserved):
        memory.live_arm({"enabled": True})
    monkeypatch.setattr(memory.urllib.request, "urlopen", respond(409, {"ok": False, "reason": "below_threshold"}))
    with pytest.raises(urllib.error.HTTPError):
        memory.live_arm({"enabled": True})
    monkeypatch.setattr(memory.urllib.request, "urlopen", respond(500, {}))
    with pytest.raises(urllib.error.HTTPError):
        memory.live_arm({"enabled": True})


def test_post_alert_posts_as_the_alerts_bot_with_the_body_on_stdin(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(args=args, stdin=kwargs["input"], env=kwargs["env"])
        return types.SimpleNamespace(returncode=0, stdout="OK ts=1794924000.100000 channel=C0ALZJHGE49\n")

    monkeypatch.setattr(memory.subprocess, "run", fake_run)
    assert memory.post_alert("hello", sender="/fake/slack-send.sh") is True
    assert seen["args"] == ["/fake/slack-send.sh", "-c", "C0ALZJHGE49", "-f"]
    assert seen["stdin"] == "hello"
    assert seen["env"]["SLACK_SEND_AS"] == "alerts"
    monkeypatch.setattr(memory.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="OK ts=1.2 channel=C0OTHER\n"))
    assert memory.post_alert("hello", sender="/fake") is False, "a receipt for another channel is not delivery"
    monkeypatch.setattr(memory.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no sender")))
    assert memory.post_alert("hello", sender="/fake") is False


def test_injected_opener_without_notifier_can_never_reach_slack(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "post_alert", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not post")))
    line = memory.hourly_line(
        arm=True, now=datetime.fromtimestamp(1_700_000_000, timezone.utc),
        collect=lambda: {"pm": (20, 3 * GB), "px": []}, state_path=tmp_path / "state.json",
        status_get=lambda: {"pending": False}, opener=lambda _p: {"ok": True, "status": {"pm_only": True, "with_devs": False}}, env={},
    )
    assert "exit-pending armed" in line and "alert" not in line


T0 = 1_700_000_000
RSS = int(2.4 * GB)
ARMED_OK = {"ok": True, "status": {"pm_only": True, "with_devs": False}}


def arm(state, *, now_epoch, notifier, status=lambda: {"pending": True}, opener=None):
    """One hourly decision at an RSS over the threshold. Every collaborator is a fake; nothing reaches MoP or Slack."""
    return memory.arm_if_needed(
        rss_bytes=RSS, now_epoch=now_epoch, state_path=state, threshold=2 * GB,
        status_get=status, opener=opener or (lambda _payload: ARMED_OK), notifier=notifier,
    )


def never_opens(_payload):
    raise AssertionError("the already-pending branch must not POST /exit-pending")


def test_already_pending_persists_the_send_intent_before_the_dispatch(tmp_path):
    state = tmp_path / "state.json"
    seen = []

    def notifier(_text):
        seen.append(json.loads(state.read_text()))
        return True

    note = arm(state, now_epoch=T0, notifier=notifier, opener=never_opens)
    assert note == "already_pending_alerted"
    assert len(seen) == 1
    assert seen[0]["status"] == "already_pending"
    assert seen[0]["alert"] == "issued"
    assert seen[0]["last_attempt_epoch"] == T0
    assert json.loads(state.read_text())["alert"] == "posted"


def test_accepted_send_with_a_lost_receipt_stays_consumed_and_is_not_resent_in_the_window(tmp_path):
    state = tmp_path / "state.json"
    sent = []

    def lost_receipt(text):
        sent.append(text)
        return False  # Slack accepted the message; the receipt never arrived

    assert arm(state, now_epoch=T0, notifier=lost_receipt, opener=never_opens) == "already_pending_unconfirmed"
    assert json.loads(state.read_text())["alert"] == "issued", "unconfirmed stays consumed/UNKNOWN, never recorded as unsent"
    # A second invocation and a restart both read only the state file: the window is consumed.
    assert arm(state, now_epoch=T0 + 3600, notifier=lost_receipt, opener=never_opens) == "cooldown"
    assert arm(state, now_epoch=T0 + memory.COOLDOWN_S - 1, notifier=lost_receipt, opener=never_opens) == "cooldown"
    assert len(sent) == 1
    # The next policy-due window is a separate reminder, not a retry of the uncertain send.
    assert arm(state, now_epoch=T0 + memory.COOLDOWN_S, notifier=lost_receipt, opener=never_opens) == "already_pending_unconfirmed"
    assert len(sent) == 2


def test_a_raising_sender_is_unconfirmed_and_consumed(tmp_path):
    state = tmp_path / "state.json"
    calls = []

    def boom(_text):
        calls.append(1)
        raise TimeoutError("sender timed out after dispatch")

    assert arm(state, now_epoch=T0, notifier=boom, opener=never_opens) == "already_pending_unconfirmed"
    assert arm(state, now_epoch=T0 + 3600, notifier=boom, opener=never_opens) == "cooldown"
    assert len(calls) == 1
    assert json.loads(state.read_text())["alert"] == "issued"


def test_state_write_failure_after_an_ok_receipt_does_not_repeat(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    sent = []
    real_atomic = memory._atomic

    def fail_after_dispatch(path, payload):
        if sent:
            raise OSError("disk full")
        real_atomic(path, payload)

    monkeypatch.setattr(memory, "_atomic", fail_after_dispatch)
    notifier = lambda text: sent.append(text) or True  # noqa: E731
    assert arm(state, now_epoch=T0, notifier=notifier, opener=never_opens) == "already_pending_alerted"
    assert json.loads(state.read_text())["alert"] == "issued", "the persisted intent still holds the window"
    assert arm(state, now_epoch=T0 + 3600, notifier=notifier, opener=never_opens) == "cooldown"
    assert len(sent) == 1


def test_unwritable_intent_skips_the_alert_and_fails_open(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    state = blocker / "state.json"
    sent = []
    note = arm(state, now_epoch=T0, notifier=lambda text: sent.append(text) or True, opener=never_opens)
    assert note == "already_pending_skipped"
    assert sent == [], "no intent on disk means nothing is dispatched"
    assert not state.exists()
    line = memory.format_line((20, RSS), [], note)
    assert "exit-pending already pending; alert skipped" in line


def test_arm_path_skips_the_alert_when_only_the_intent_write_fails(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    real_atomic = memory._atomic

    def fail_intent(path, payload):
        if "alert" in payload:
            raise OSError("read-only after the arm")
        real_atomic(path, payload)

    monkeypatch.setattr(memory, "_atomic", fail_intent)
    opened, sent = [], []
    note = arm(
        state, now_epoch=T0, status=lambda: {"pending": False},
        opener=lambda payload: opened.append(payload) or ARMED_OK, notifier=lambda text: sent.append(text) or True,
    )
    assert note == "armed_skipped"
    assert len(opened) == 1 and sent == []
    assert json.loads(state.read_text())["status"] == "attempted", "the pre-POST throttle intent still holds the window"
    assert arm(state, now_epoch=T0 + 3600, notifier=lambda text: sent.append(text) or True) == "cooldown"
    assert sent == []


def test_alert_never_touches_an_existing_exit_pending_episode(tmp_path):
    """Episode cb42059c is pending with an UNKNOWN outcome: the alert path posts nothing to MoP and arms nothing."""
    state = tmp_path / "state.json"
    arms = []
    sent = []
    note = arm(
        state, now_epoch=T0, status=lambda: {"pending": True, "episode": "cb42059c-60de-4aa1-b7a4-18799b05d06b"},
        opener=lambda payload: arms.append(payload), notifier=lambda text: sent.append(text) or False,
    )
    assert note == "already_pending_unconfirmed"
    assert arms == [], "no POST /exit-pending, no clear, no re-arm of the existing episode"
    assert len(sent) == 1


# ---- Single-winner claim (CTO REVISE of bb561de1, ts 1794926555.931559). Added, NOT executed. ----


def test_two_callers_at_the_same_expired_window_yield_one_notifier_call(tmp_path):
    """Caller A has read the expired window and the pending status when caller B runs to completion."""
    state = tmp_path / "state.json"
    sent, inner = [], []
    notifier = lambda text: sent.append(text) or True  # noqa: E731

    def status_while_rival_completes():
        if not inner:
            inner.append(arm(state, now_epoch=T0, notifier=notifier, opener=never_opens))
        return {"pending": True}

    outer = arm(state, now_epoch=T0, notifier=notifier, status=status_while_rival_completes, opener=never_opens)
    assert inner == ["already_pending_alerted"]
    assert outer == "cooldown", "the stale caller re-reads the window under the lock and finds it consumed"
    assert len(sent) == 1
    saved = json.loads(state.read_text())
    assert saved["alert"] == "posted" and saved["last_attempt_epoch"] == T0


def test_racing_callers_on_the_arm_path_open_and_alert_once(tmp_path):
    state = tmp_path / "state.json"
    opened, sent, inner = [], [], []
    opener = lambda payload: opened.append(payload) or ARMED_OK  # noqa: E731
    notifier = lambda text: sent.append(text) or True  # noqa: E731

    def status_while_rival_completes():
        if not inner:
            inner.append(arm(state, now_epoch=T0, status=lambda: {"pending": False}, opener=opener, notifier=notifier))
        return {"pending": False}

    outer = arm(state, now_epoch=T0, status=status_while_rival_completes, opener=opener, notifier=notifier)
    assert inner == ["armed_alerted"]
    assert outer == "cooldown"
    assert len(opened) == 1, "the loser never POSTs /exit-pending"
    assert len(sent) == 1


def test_concurrent_callers_released_together_produce_exactly_one_winner(tmp_path):
    """Real threads, each on its own lock file description, all past the unlocked pre-check before any claims."""
    state = tmp_path / "state.json"
    callers = 4
    barrier = threading.Barrier(callers, timeout=10)
    sent, results = [], []
    guard = threading.Lock()

    def status_at_the_barrier():
        barrier.wait()  # every caller has read the expired window before any caller claims it
        return {"pending": True}

    def notifier(text):
        with guard:
            sent.append(text)
        return True

    def caller():
        note = arm(state, now_epoch=T0, notifier=notifier, status=status_at_the_barrier, opener=never_opens)
        with guard:
            results.append(note)

    threads = [threading.Thread(target=caller) for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert len(results) == callers
    assert results.count("already_pending_alerted") == 1, results
    assert all(note in ("cooldown", "contended") for note in results if note != "already_pending_alerted"), results
    assert len(sent) == 1
    assert json.loads(state.read_text())["alert"] == "posted"


def test_a_caller_that_cannot_take_the_claim_lock_arms_nothing_and_sends_nothing(tmp_path):
    state = tmp_path / "state.json"
    sent, opened = [], []
    notifier = lambda text: sent.append(text) or True  # noqa: E731
    opener = lambda payload: opened.append(payload) or ARMED_OK  # noqa: E731
    with state.with_name("state.json.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # another run is claiming the same window
        pending = arm(state, now_epoch=T0, notifier=notifier, opener=never_opens)
        fresh = arm(state, now_epoch=T0, status=lambda: {"pending": False}, opener=opener, notifier=notifier)
    assert pending == "contended" and fresh == "contended"
    assert sent == [] and opened == []
    assert not state.exists(), "a contended claim persists nothing"
    assert "exit-pending claimed by another run" in memory.format_line((20, RSS), [], pending)
    # With the lock free the next caller wins the same window: contention never consumed it.
    assert arm(state, now_epoch=T0 + 60, notifier=notifier, opener=never_opens) == "already_pending_alerted"
    assert len(sent) == 1


def test_claim_rereads_under_the_lock_and_preserves_a_rivals_unknown_attempt(tmp_path):
    state = tmp_path / "state.json"
    rival = {
        "attempt": "rival", "last_attempt_epoch": T0 - 5, "last_rss_bytes": RSS,
        "status": "already_pending", "alert": "issued",
    }

    def status_after_the_rival_claims():
        state.write_text(json.dumps(rival))  # the rival won after this caller's unlocked pre-check
        return {"pending": True}

    sent = []
    note = arm(state, now_epoch=T0, notifier=lambda text: sent.append(text) or True,
               status=status_after_the_rival_claims, opener=never_opens)
    assert note == "cooldown"
    assert sent == [], "the rival's issued send stays consumed/UNKNOWN and is not resent"
    assert json.loads(state.read_text()) == rival, "its timestamp and UNKNOWN attempt are not reset"


def test_unreadable_or_unpersistable_claim_skips_the_alert_and_arms_nothing(tmp_path):
    sent, opened = [], []
    notifier = lambda text: sent.append(text) or True  # noqa: E731
    opener = lambda payload: opened.append(payload) or ARMED_OK  # noqa: E731
    unreadable = tmp_path / "state.json"
    unreadable.mkdir()  # exists but cannot be read as a file: the window cannot be proven
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    for state in (unreadable, blocker / "state.json"):
        assert arm(state, now_epoch=T0, notifier=notifier, opener=never_opens) == "already_pending_skipped"
        assert arm(state, now_epoch=T0, status=lambda: {"pending": False}, opener=opener, notifier=notifier) == "uncertain"
    assert sent == [] and opened == [], "no claim on disk means nothing is armed and nothing is dispatched"
    assert unreadable.is_dir(), "the unreadable state is left untouched"


def test_winning_claim_is_durable_before_the_arm_post_and_carries_an_attempt_id(tmp_path):
    state = tmp_path / "state.json"
    at_post = []

    def opener(_payload):
        at_post.append(json.loads(state.read_text()))
        return ARMED_OK

    assert arm(state, now_epoch=T0, status=lambda: {"pending": False}, opener=opener,
               notifier=lambda _text: True) == "armed_alerted"
    assert len(at_post) == 1
    assert at_post[0]["status"] == "attempted" and at_post[0]["last_attempt_epoch"] == T0
    assert at_post[0]["attempt"], "the winning intent names its attempt"
    assert json.loads(state.read_text())["attempt"] == at_post[0]["attempt"], "later writes keep the same attempt"


def test_a_later_due_window_alerts_once_when_two_callers_race_it(tmp_path):
    """The UNKNOWN send of the first window is never retried; the next policy-due window is one new reminder."""
    state = tmp_path / "state.json"
    sent, inner = [], []

    def lost_receipt(text):
        sent.append(text)
        return False

    assert arm(state, now_epoch=T0, notifier=lost_receipt, opener=never_opens) == "already_pending_unconfirmed"
    assert json.loads(state.read_text())["alert"] == "issued"
    due = T0 + memory.COOLDOWN_S

    def status_while_rival_completes():
        if not inner:
            inner.append(arm(state, now_epoch=due, notifier=lost_receipt, opener=never_opens))
        return {"pending": True}

    outer = arm(state, now_epoch=due, notifier=lost_receipt, status=status_while_rival_completes, opener=never_opens)
    assert inner == ["already_pending_unconfirmed"]
    assert outer == "cooldown"
    assert len(sent) == 2, "one reminder per policy-due window, never a second send in the same window"
    saved = json.loads(state.read_text())
    assert saved["last_attempt_epoch"] == due and saved["alert"] == "issued"
