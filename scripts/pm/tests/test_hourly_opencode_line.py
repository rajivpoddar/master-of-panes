"""Hourly *OpenCode Go:* line: cap with its source, live primary procs, latest-per-name run registrations, Muse selection records.
Counts only.

Directive: OpenCode Go pool line for the hourly composer (no cost, no rate, no quota). Revised per the CTO re-review
(C0ALZJHGE49 thread 1794932691.512539, ts 1794933244.444789):
  1. cap follows the launcher's precedence (nonempty inherited OPENCODE_PROXY_CAP, then the caps file, then 2) and labels the
     source; an unreadable file is a labelled fallback; an invalid inherited value is UNVERIFIED, never a confirmed number;
  2. live = PRIMARY-model processes only; run manifests are latest-per-name REGISTRATION evidence, not a launch count; unreadable,
     malformed, model-less or future-stamped manifests make the field PARTIAL with the observed minimum, never silent zero;
  3. Muse = logged selection records (primary and alias), no time-based launch grouping, zero is not proof of no use.
Fakes only: the ps text, the run registry, the caps file, the environment and the Muse log are temp files, dicts or strings;
nothing calls MoP, tmux, Slack or a provider.
Added, NOT executed (MoP changes need no local test proof; Rajiv DM D0AMF0XE6TS thread 1793979986.975849,
ts 1793983042.752819).
"""

import importlib.util
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py"
spec = importlib.util.spec_from_file_location("heartbeat_compose_opencode", COMPOSE)
composer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(composer)

HY4, DEEP, MUSE = composer.OPENCODE_MODELS
NOW = datetime.now(timezone.utc).replace(microsecond=0)

LIVE_LABEL = "live primary procs now (nested alias requests not counted): "
REG_LABEL = "run registrations last 1h (latest per run name, may include failed launches): "
MUSE_LABEL = "Muse selection records logged last 1h: "
MUSE_SUFFIX = "selection log only, not proof of use or of no use)"
BANNED = ("$", "%", "/h", "cost", "rate", "per hour")


def stamp(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def manifest(root, name, model, started):
    run = root / name
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"name": name, "model": model, "started_at": started}))


def log_line(seconds_ago, name, role="primary"):
    when = (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f"{when} EMERGENCY_MUSE role={role} name={name} type=hd-builder tier=MID "
            f"model=muse-spark-1.3-contributor reason=none\n")


def ps(*models):
    # The launcher wrapper embeds --model too; only the real `claude -p --name X --model <id>` process counts.
    rows = ["/usr/bin/login -pf rajiv",
            "/bin/bash /Users/rajiv/.claude/scripts/proxy-agent.sh --model muse-spark-1.3-contributor"]
    rows += [f"/opt/homebrew/bin/claude -p --name px-{i} --model {model}" for i, model in enumerate(models)]
    return "\n".join(rows)


def counts_text(pool, hy4=0, deep=0, muse=0):
    return f"pool {pool} · {HY4} {hy4} · {DEEP} {deep} · {MUSE} {muse}"


def muse_cell(total, primary, alias, *notes, partial=False):
    head = f"PARTIAL, observed minimum {total}" if partial else str(total)
    return f"{head} ({' · '.join([f'primary {primary}', f'alias {alias}', *notes])}; {MUSE_SUFFIX}"


def line_of(cap, live, registered, muse):
    return f"*OpenCode Go:* {cap} | {LIVE_LABEL}{live} | {REG_LABEL}{registered} | {MUSE_LABEL}{muse}"


# ---- cap: launcher precedence and source label ----

def test_cap_inherited_env_beats_the_file_and_names_its_source(tmp_path):
    caps = tmp_path / "caps"
    caps.write_text("OPENCODE_PROXY_CAP=3\n")
    assert composer.opencode_cap(caps, {"OPENCODE_PROXY_CAP": "5"}) == (5, "inherited env OPENCODE_PROXY_CAP")
    # An EMPTY inherited value is not an override: the launcher's `-z "${!_k:-}"` test falls through to the file.
    assert composer.opencode_cap(caps, {"OPENCODE_PROXY_CAP": ""}) == (3, "cap file")


def test_cap_invalid_inherited_value_is_unestablished_not_a_number(tmp_path):
    caps = tmp_path / "caps"
    caps.write_text("OPENCODE_PROXY_CAP=3\n")
    cap, label = composer.opencode_cap(caps, {"OPENCODE_PROXY_CAP": "abc"})
    assert cap is None and "invalid" in label and "enforces no OpenCode cap" in label
    assert "abc" not in label  # never echo an inherited value into the report


def test_cap_file_is_the_first_valid_line_and_skips_an_unterminated_final_line(tmp_path):
    caps = tmp_path / "caps"
    caps.write_text("CODEX_PROXY_CAP=10\nOPENCODE_PROXY_CAP=x\nOPENCODE_PROXY_CAP=4\nOPENCODE_PROXY_CAP=9\n")
    assert composer.opencode_cap(caps, {}) == (4, "cap file")
    caps.write_text("OPENCODE_PROXY_CAP=7")  # no trailing newline: the launcher's `while read` loop never processes it
    assert composer.opencode_cap(caps, {}) == (2, "default: key absent")


def test_cap_default_and_fallback_labels(tmp_path):
    assert composer.opencode_cap(tmp_path / "missing", {}) == (2, "default: key absent, no cap file")
    caps = tmp_path / "caps"
    caps.write_text("GROK_PROXY_CAP=6\n")
    assert composer.opencode_cap(caps, {}) == (2, "default: key absent")
    caps.write_text("OPENCODE_PROXY_CAP=abc\nOPENCODE_PROXY_CAP=\n")
    assert composer.opencode_cap(caps, {}) == (2, "fallback: cap file value invalid")
    unreadable = tmp_path / "caps-dir"
    unreadable.mkdir()
    assert composer.opencode_cap(unreadable, {}) == (2, "fallback: cap file unreadable")


# ---- live ----

def test_live_counts_only_real_primary_claude_processes_per_opencode_model():
    live = composer.opencode_live(ps(HY4, DEEP, DEEP, "claude-sonnet-5-5", "grok-4.7", MUSE))
    assert live == {HY4: 1, DEEP: 2, MUSE: 1}


# ---- run registrations in the last hour ----

def test_registrations_count_only_opencode_models_registered_inside_the_window(tmp_path):
    manifest(tmp_path, "px-a", HY4, stamp(60))
    manifest(tmp_path, "px-b", DEEP, stamp(3000))
    manifest(tmp_path, "px-c", DEEP, stamp(3500))
    manifest(tmp_path, "px-d", MUSE, stamp(3599))
    manifest(tmp_path, "px-soon", HY4, stamp(-30))  # a few seconds past now is inside the skew allowance
    manifest(tmp_path, "px-old", HY4, stamp(3700))  # registered before the window
    manifest(tmp_path, "px-claude", "claude-sonnet-5-5", stamp(30))  # a different named model
    (tmp_path / "stray.lock").write_text("")  # not a run directory
    (tmp_path / "px-empty").mkdir()  # run directory without a manifest
    counts, partial = composer.opencode_registrations(NOW, tmp_path)
    assert counts == {HY4: 2, DEEP: 2, MUSE: 1}
    assert partial == 0


def test_unreadable_malformed_modelless_or_future_manifests_are_partial_not_dropped(tmp_path):
    (tmp_path / "px-bad").mkdir()
    (tmp_path / "px-bad" / "manifest.json").write_text("{not json")
    (tmp_path / "px-list").mkdir()
    (tmp_path / "px-list" / "manifest.json").write_text("[]")
    manifest(tmp_path, "px-nostart", DEEP, "")  # OpenCode model with no usable started_at
    manifest(tmp_path, "px-nomodel", None, stamp(10))  # in-window record whose model metadata is missing
    manifest(tmp_path, "px-future", MUSE, stamp(-3600))  # stamped an hour after the report: outside the window
    manifest(tmp_path, "px-ok", HY4, stamp(10))
    counts, partial = composer.opencode_registrations(NOW, tmp_path)
    assert counts == {HY4: 1, DEEP: 0, MUSE: 0}
    assert partial == 5


def test_an_unlistable_registry_raises_instead_of_reading_zero(tmp_path):
    with pytest.raises(OSError):
        composer.opencode_registrations(NOW, tmp_path / "nope")


# ---- Muse selection records ----

def test_muse_log_missing_is_zero_records_flagged_absent_and_unreadable_raises(tmp_path):
    assert composer.muse_selection_records(NOW, tmp_path / "none.log") == ({"primary": 0, "alias": 0, "other": 0}, 0, True)
    with pytest.raises(OSError):
        composer.muse_selection_records(NOW, tmp_path)  # a directory, not a log


def test_muse_records_are_one_per_logged_line_with_no_dedupe(tmp_path):
    log = tmp_path / "muse.log"
    log.write_text(log_line(120, "px-one", "primary") + log_line(120, "px-one", "haiku")
                   + log_line(119, "px-one", "sonnet") + log_line(119, "px-one", "opus")
                   + log_line(300, "px-two", "primary") + log_line(299, "px-two", "primary"))
    counts, malformed, absent = composer.muse_selection_records(NOW, log)
    assert counts == {"primary": 3, "alias": 3, "other": 0}
    assert (malformed, absent) == (0, False)


def test_muse_old_lines_are_skipped_and_bad_or_future_lines_are_malformed(tmp_path):
    log = tmp_path / "muse.log"
    log.write_text(log_line(1800, "px-a") + log_line(600, "px-a")
                   + log_line(3700, "px-old")  # outside the hour: skipped
                   + "not a log line\n"  # carries no marker: ignored
                   + "2026-13-45T99:99:99Z EMERGENCY_MUSE name=px-bad\n"  # unparsable stamp: malformed
                   + "EMERGENCY_MUSE no stamp\n"  # marker without a stamp: malformed
                   + log_line(-600, "px-future"))  # stamped well after now: malformed
    counts, malformed, absent = composer.muse_selection_records(NOW, log)
    assert counts == {"primary": 2, "alias": 0, "other": 0}
    assert malformed == 3


# ---- fixture table (static/stub inputs; one row per CTO-named case) ----
# Row keys: env (inherited environment), caps (None = no file, "dir" = unreadable path, else file text), runs (ordered manifest
# writes; a repeated name overwrites, like a later start/resume), log (None = no file, else Muse log lines), ps (live primaries).
# Expected cells: cap, live, reg, muse.
FIXTURES = [
    dict(id="inherited-cap-override-beats-file", env={"OPENCODE_PROXY_CAP": "5"}, caps="OPENCODE_PROXY_CAP=3\n",
         cap="cap 5 (inherited env OPENCODE_PROXY_CAP)"),
    dict(id="inherited-cap-invalid-is-unverified", env={"OPENCODE_PROXY_CAP": "abc"}, caps="OPENCODE_PROXY_CAP=3\n",
         cap="cap UNVERIFIED (inherited env OPENCODE_PROXY_CAP invalid; launcher enforces no OpenCode cap)"),
    dict(id="empty-inherited-cap-falls-through-to-file", env={"OPENCODE_PROXY_CAP": ""}, caps="OPENCODE_PROXY_CAP=4\n",
         cap="cap 4 (cap file)"),
    dict(id="unreadable-cap-file-falls-back-to-2", env={}, caps="dir", cap="cap 2 (fallback: cap file unreadable)"),
    dict(id="inherited-override-wins-over-unreadable-file", env={"OPENCODE_PROXY_CAP": "6"}, caps="dir",
         cap="cap 6 (inherited env OPENCODE_PROXY_CAP)"),
    dict(id="absent-key-is-default", env={}, caps="GROK_PROXY_CAP=6\n", cap="cap 2 (default: key absent)"),
    dict(id="missing-cap-file-is-default", env={}, caps=None, cap="cap 2 (default: key absent, no cap file)"),
    dict(id="invalid-file-value-is-fallback", env={}, caps="OPENCODE_PROXY_CAP=abc\n",
         cap="cap 2 (fallback: cap file value invalid)"),
    dict(id="unterminated-final-cap-line-is-skipped", env={}, caps="OPENCODE_PROXY_CAP=7", cap="cap 2 (default: key absent)"),
    dict(id="bad-registrations-are-partial-with-observed-minimum", env={}, caps=None,
         runs=[("px-bad", "{not json"), ("px-nomodel", {"started_at": stamp(60)}),
               ("px-future", {"model": DEEP, "started_at": stamp(-3600)}),
               ("px-nostart", {"model": MUSE}), ("px-ok", {"model": HY4, "started_at": stamp(30)})],
         reg=("PARTIAL, observed minimum " + counts_text(1, hy4=1)
              + " (4 manifests unreadable, malformed or missing model/start time)")),
    dict(id="overwritten-registration-is-latest-per-name-and-failed-launch-still-counts", env={}, caps=None,
         runs=[("px-a", {"model": MUSE, "started_at": stamp(1800)}),  # first registration ...
               ("px-a", {"model": DEEP, "started_at": stamp(5)}),  # ... replaced by a later start/resume of the same name
               ("px-fail", {"model": HY4, "started_at": stamp(40), "rc": "UNAVAILABLE", "reason": "launch_failed"})],
         reg=counts_text(2, hy4=1, deep=1)),
    dict(id="unused-muse-alias-selection-is-a-record-not-use", env={}, caps=None, ps=(DEEP,),
         log=[log_line(90, "px-x", "haiku")],
         live=counts_text(1, deep=1), reg=counts_text(0), muse=muse_cell(1, 0, 1)),
    dict(id="same-name-records-inside-60s-are-not-deduped", env={}, caps=None,
         log=[log_line(120, "px-one", "primary"), log_line(120, "px-one", "haiku"),
              log_line(119, "px-one", "sonnet"), log_line(119, "px-one", "opus")],
         muse=muse_cell(4, 1, 3)),
    dict(id="empty-muse-log-is-zero-records-not-proof", env={}, caps=None, log=[], muse=muse_cell(0, 0, 0)),
    dict(id="malformed-and-future-muse-lines-are-partial", env={}, caps=None,
         log=[log_line(1800, "px-a"), "2026-13-45T99:99:99Z EMERGENCY_MUSE name=px-bad\n", log_line(-600, "px-future"),
              log_line(3700, "px-old"), "not a log line\n"],
         muse=muse_cell(1, 1, 0, "2 malformed log lines", partial=True)),
]


@pytest.mark.parametrize("case", FIXTURES, ids=[case["id"] for case in FIXTURES])
def test_fixture_table(tmp_path, case):
    caps = tmp_path / "caps"
    if case["caps"] == "dir":
        caps.mkdir()
    elif case["caps"] is not None:
        caps.write_text(case["caps"])
    runs = tmp_path / "runs"
    runs.mkdir()
    for name, payload in case.get("runs", []):
        run = runs / name
        run.mkdir(exist_ok=True)
        (run / "manifest.json").write_text(payload if isinstance(payload, str) else json.dumps(payload))
    log = tmp_path / "muse.log"
    if case.get("log") is not None:
        log.write_text("".join(case["log"]))
    line = composer.opencode_line(NOW, caps=caps, root=runs, log=log, ps_text=ps(*case.get("ps", ())), environ=case["env"])
    expected = line_of(case.get("cap", "cap 2 (default: key absent, no cap file)"),
                       case.get("live", counts_text(0)), case.get("reg", counts_text(0)),
                       case.get("muse", muse_cell(0, 0, 0, "log file absent")))
    assert line == expected
    assert "\n" not in line
    for banned in BANNED:
        assert banned not in line.lower()


# ---- the line ----

def test_line_is_counts_only_with_cap_source_live_registrations_and_muse_selections(tmp_path):
    caps = tmp_path / "caps"
    caps.write_text("OPENCODE_PROXY_CAP=3\n")
    runs = tmp_path / "runs"
    runs.mkdir()
    manifest(runs, "px-a", HY4, stamp(60))
    manifest(runs, "px-b", DEEP, stamp(600))
    manifest(runs, "px-c", DEEP, stamp(900))
    log = tmp_path / "muse.log"
    log.write_text(log_line(200, "px-m"))
    line = composer.opencode_line(NOW, caps=caps, root=runs, log=log, ps_text=ps(DEEP, MUSE), environ={})
    assert line == line_of("cap 3 (cap file)", counts_text(2, deep=1, muse=1), counts_text(3, hy4=1, deep=2),
                           muse_cell(1, 1, 0))
    for banned in BANNED:
        assert banned not in line.lower()


def test_each_unreadable_source_fails_independently_and_never_reads_zero(monkeypatch, tmp_path):
    def no_ps(*args, **kwargs):
        raise OSError("ps unavailable")

    monkeypatch.setattr(subprocess, "run", no_ps)
    log_dir = tmp_path / "log-is-a-directory"
    log_dir.mkdir()
    line = composer.opencode_line(NOW, caps=tmp_path / "caps", root=tmp_path / "no-runs", log=log_dir, environ={})
    assert line == ("*OpenCode Go:* cap 2 (default: key absent, no cap file) | " + LIVE_LABEL + "UNAVAILABLE | "
                    + REG_LABEL + "UNAVAILABLE | " + MUSE_LABEL + "UNAVAILABLE")


def test_a_live_failure_does_not_hide_the_registration_counts(monkeypatch, tmp_path):
    def no_ps(*args, **kwargs):
        raise subprocess.TimeoutExpired("ps", 5)

    monkeypatch.setattr(subprocess, "run", no_ps)
    runs = tmp_path / "runs"
    runs.mkdir()
    manifest(runs, "px-a", HY4, stamp(60))
    line = composer.opencode_line(NOW, caps=tmp_path / "caps", root=runs, log=tmp_path / "muse.log", environ={})
    assert LIVE_LABEL + "UNAVAILABLE" in line
    assert REG_LABEL + counts_text(1, hy4=1) in line
    assert MUSE_LABEL + muse_cell(0, 0, 0, "log file absent") in line


def test_a_cap_source_failure_reads_unverified_and_leaves_the_other_fields(monkeypatch, tmp_path):
    def broken_cap(*args, **kwargs):
        raise RuntimeError("cap source broke")

    monkeypatch.setattr(composer, "opencode_cap", broken_cap)
    runs = tmp_path / "runs"
    runs.mkdir()
    line = composer.opencode_line(NOW, caps=tmp_path / "caps", root=runs, log=tmp_path / "muse.log", ps_text=ps(), environ={})
    assert line.startswith("*OpenCode Go:* cap UNVERIFIED (cap source failed) | " + LIVE_LABEL + counts_text(0))


# ---- placement in the hourly report ----

def test_compose_places_the_line_under_the_burn_block_on_1h_only():
    now = datetime.now(timezone.utc)
    line = "*OpenCode Go:* cap 2 (default: key absent) | " + LIVE_LABEL + counts_text(0)
    burn = ("*Burn:* pace to Claude · Codex; headroom = budget - burn, %/h; mode is per pool\n"
            "Claude pool: burn 0.10 → OK → Claude routes: NORMAL",
            {"Claude": "NORMAL", "Codex": "NORMAL"})
    hourly = composer.compose("1h", now, {"events": 0}, {}, [], burn=burn, opencode=line).splitlines()
    three = composer.compose("3h", now, {"events": 0}, {}, [], opencode=line)
    assert line in hourly
    assert hourly.index(line) > hourly.index("Claude pool: burn 0.10 → OK → Claude routes: NORMAL")
    assert line not in three
    assert "*OpenCode Go:*" not in composer.compose("1h", now, {"events": 0}, {}, [])
