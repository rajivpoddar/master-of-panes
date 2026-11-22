"""Hourly *OpenCode Go:* section: a pipe table with a pool row (cap with its source, plan usage windows) and one row per
OpenCode model (live primary procs, latest-per-name run registrations, Muse selection records).

Rajiv C0ALZJHGE49 ts 1795323839.696369, 2026-11-22: "change the claude team and opencode burn section to tables". The sources
and the never-silent-zero semantics below are unchanged; only the rendering is a table, and it adds the plan usage windows
(monthly / weekly / rolling %) that the usage sampler records under opencode:go:*. No cost, no $ and no per-hour rate.

Directive: OpenCode Go pool line for the hourly composer (no cost, no rate, no quota). Revised per the CTO re-review
(C0ALZJHGE49 thread 1794932691.512539, ts 1794933244.444789):
  1. cap follows the launcher's precedence (nonempty inherited OPENCODE_PROXY_CAP, then the caps file, then 2) and labels the
     source; an unreadable file is a labelled fallback; an invalid inherited value is UNVERIFIED, never a confirmed number;
  2. live = PRIMARY-model processes only; run manifests are latest-per-name REGISTRATION evidence, not a launch count; unreadable,
     malformed, model-less or future-stamped manifests make the field PARTIAL with the observed minimum, never silent zero;
  3. Muse = logged selection records (primary and alias), no time-based launch grouping, zero is not proof of no use.
Invalid UTF-8 follow-up: a manifest or Muse log line that does not decode strictly is PARTIAL (never an exception, never repaired
into a normal-looking record); a process-table line with invalid UTF-8 no longer raises out of the live reader.
Fakes only: the ps text, the run registry, the caps file, the environment and the Muse log are temp files, dicts or strings;
nothing calls MoP, tmux, Slack or a provider.
Added, NOT executed (MoP changes need no local test proof; Rajiv DM D0AMF0XE6TS thread 1793979986.975849,
ts 1793983042.752819).
"""

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py"
spec = importlib.util.spec_from_file_location("heartbeat_compose_opencode", COMPOSE)
composer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(composer)

HY4, DEEP, GLM, MUSE = composer.OPENCODE_MODELS
NOW = datetime.now(timezone.utc).replace(microsecond=0)

POOL = "OpenCode Go pool"
MUSE_LABEL = "Muse selection records logged last 1h: "
MUSE_SUFFIX = "selection log only, not proof of use or of no use)"
# The Used column now carries plan-window percentages by request, so "%" is allowed there; a dollar figure, a cost word or a
# per-hour rate never is.
BANNED = ("$", "%/h", "cost", "per hour")
NO_SNAPSHOT = "no-usage-snapshots.jsonl"


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


def counts(pool, hy4=0, deep=0, muse=0, glm=0, floor=False):
    """Expected per-row count cells; `floor` is the lower-bound form ("≥N") used when a manifest could hide a registration."""
    mark = "≥" if floor else ""
    return {POOL: f"{mark}{pool}", HY4: f"{mark}{hy4}", DEEP: f"{mark}{deep}", GLM: f"{mark}{glm}", MUSE: f"{mark}{muse}"}


def muse_cell(total, primary, alias, *notes, partial=False):
    head = f"PARTIAL, observed minimum {total}" if partial else str(total)
    return f"{head} ({' · '.join([f'primary {primary}', f'alias {alias}', *notes])}; {MUSE_SUFFIX}"


def parse(section):
    """(head line, header cells, separator row, {row name: {header: cell}}, ordered row names) of an OpenCode Go section."""
    rows = section.splitlines()
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows[1:]]
    header, body = cells[0], cells[2:]
    return rows[0], header, rows[2], {r[0]: dict(zip(header, r)) for r in body}, [r[0] for r in body]


def render(now, **kw):
    # An absent usage snapshot keeps host state out of a test; pass snapshot= to exercise the Used column.
    kw.setdefault("snapshot", Path("/nonexistent") / NO_SNAPSHOT)
    return composer.opencode_line(now, **kw)


def snapshot_file(tmp_path, rows):
    path = tmp_path / "usage-snapshots.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def usage_row(window, pct=20, ago=600, reset_in=3600, status="ok"):
    return {"key": f"opencode:go:{window}", "status": status, "pct": pct, "ts": NOW.timestamp() - ago,
            "reset": NOW.timestamp() + reset_in}


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
    live = composer.opencode_live(ps(HY4, DEEP, DEEP, GLM, "claude-sonnet-5-5", "grok-4.7", MUSE))
    assert live == {HY4: 1, DEEP: 2, GLM: 1, MUSE: 1}  # an explicit glm-5.3 run takes a pool slot and is counted


# ---- run registrations in the last hour ----

def test_registrations_count_only_opencode_models_registered_inside_the_window(tmp_path):
    manifest(tmp_path, "px-a", HY4, stamp(60))
    manifest(tmp_path, "px-b", DEEP, stamp(3000))
    manifest(tmp_path, "px-c", DEEP, stamp(3500))
    manifest(tmp_path, "px-d", MUSE, stamp(3599))
    manifest(tmp_path, "px-glm", GLM, stamp(120))  # an explicit GLM run is a pool registration
    manifest(tmp_path, "px-soon", HY4, stamp(-30))  # a few seconds past now is inside the skew allowance
    manifest(tmp_path, "px-old", HY4, stamp(3700))  # registered before the window
    manifest(tmp_path, "px-claude", "claude-sonnet-5-5", stamp(30))  # a different named model
    (tmp_path / "stray.lock").write_text("")  # not a run directory
    (tmp_path / "px-empty").mkdir()  # run directory without a manifest
    counts, partial = composer.opencode_registrations(NOW, tmp_path)
    assert counts == {HY4: 2, DEEP: 2, GLM: 1, MUSE: 1}
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
    assert counts == {HY4: 1, DEEP: 0, GLM: 0, MUSE: 0}
    assert partial == 5


def test_invalid_utf8_manifest_is_partial_not_an_exception_and_not_counted(tmp_path):
    run = tmp_path / "px-badutf8"
    run.mkdir()
    (run / "manifest.json").write_bytes(b'{"model": "' + HY4.encode() + b'", "started_at": "' + stamp(10).encode()
                                        + b'", "note": "\xc3\x28"}')  # \xc3\x28 is an invalid two-byte sequence
    manifest(tmp_path, "px-ok", DEEP, stamp(10))
    counts, partial = composer.opencode_registrations(NOW, tmp_path)
    assert counts == {HY4: 0, DEEP: 1, GLM: 0, MUSE: 0}  # the undecodable record is not trusted into the counts ...
    assert partial == 1  # ... and is reported as PARTIAL rather than dropped


def test_an_undecodable_manifest_older_than_the_window_is_not_partial(tmp_path):
    run = tmp_path / "px-old"
    run.mkdir()
    old = run / "manifest.json"
    old.write_bytes(b"\xff\xfe not utf-8")
    long_ago = (NOW - timedelta(hours=5)).timestamp()
    os.utime(old, (long_ago, long_ago))  # last modified long before the window: cannot hold an in-window registration
    assert composer.opencode_registrations(NOW, tmp_path) == ({HY4: 0, DEEP: 0, GLM: 0, MUSE: 0}, 0)


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


def test_muse_log_with_invalid_utf8_does_not_raise_and_is_not_repaired_into_a_record(tmp_path):
    log = tmp_path / "muse.log"
    bad_in_window = log_line(60, "px-bad").encode().replace(b"px-bad", b"px-\xc3\x28")
    bad_before_window = log_line(3700, "px-old").encode().replace(b"px-old", b"px-\xc3\x28")
    log.write_bytes(log_line(30, "px-ok").encode() + bad_in_window + bad_before_window)
    counts, malformed, absent = composer.muse_selection_records(NOW, log)
    assert counts == {"primary": 1, "alias": 0, "other": 0}  # the in-window undecodable line is NOT counted as a record
    assert (malformed, absent) == (1, False)  # it is PARTIAL; the undecodable line stamped before the window is not


def test_live_reader_tolerates_invalid_utf8_in_the_process_table(monkeypatch):
    raw = (b"/opt/homebrew/bin/claude -p --name px-0 --model " + DEEP.encode() + b" --note \xff\xfe\n"
           b"/usr/bin/odd-tool \xc3\x28\n"
           b"/opt/homebrew/bin/claude -p --name px-1 --model " + MUSE.encode() + b"\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=raw, stderr=b""))
    assert composer.opencode_live() == {HY4: 0, DEEP: 1, GLM: 0, MUSE: 1}


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
         reg=counts(1, hy4=1, floor=True), reg_partial=4),
    dict(id="overwritten-registration-is-latest-per-name-and-failed-launch-still-counts", env={}, caps=None,
         runs=[("px-a", {"model": MUSE, "started_at": stamp(1800)}),  # first registration ...
               ("px-a", {"model": DEEP, "started_at": stamp(5)}),  # ... replaced by a later start/resume of the same name
               ("px-fail", {"model": HY4, "started_at": stamp(40), "rc": "UNAVAILABLE", "reason": "launch_failed"})],
         reg=counts(2, hy4=1, deep=1)),
    dict(id="explicit-glm-run-is-counted-in-the-pool", env={}, caps=None, ps=(GLM,),
         runs=[("px-glm", {"model": GLM, "started_at": stamp(30)})],
         live=counts(1, glm=1), reg=counts(1, glm=1)),
    dict(id="unused-muse-alias-selection-is-a-record-not-use", env={}, caps=None, ps=(DEEP,),
         log=[log_line(90, "px-x", "haiku")],
         live=counts(1, deep=1), reg=counts(0), muse=muse_cell(1, 0, 1)),
    dict(id="same-name-records-inside-60s-are-not-deduped", env={}, caps=None,
         log=[log_line(120, "px-one", "primary"), log_line(120, "px-one", "haiku"),
              log_line(119, "px-one", "sonnet"), log_line(119, "px-one", "opus")],
         muse=muse_cell(4, 1, 3)),
    dict(id="empty-muse-log-is-zero-records-not-proof", env={}, caps=None, log=[], muse=muse_cell(0, 0, 0)),
    dict(id="malformed-and-future-muse-lines-are-partial", env={}, caps=None,
         log=[log_line(1800, "px-a"), "2026-13-45T99:99:99Z EMERGENCY_MUSE name=px-bad\n", log_line(-600, "px-future"),
              log_line(3700, "px-old"), "not a log line\n"],
         muse=muse_cell(1, 1, 0, "2 malformed or undecodable log lines", partial=True)),
    dict(id="invalid-utf8-manifest-is-partial-not-silent-and-not-a-crash", env={}, caps=None,
         runs=[("px-badutf8", b'{"model": "hy4-flash-free", "started_at": "' + stamp(30).encode() + b'", "note": "\xff\xfe"}'),
               ("px-ok", {"model": HY4, "started_at": stamp(30)})],
         reg=counts(1, hy4=1, floor=True), reg_partial=1),
    dict(id="invalid-utf8-muse-lines-are-partial-unless-stamped-before-the-window", env={}, caps=None,
         log=[log_line(1800, "px-a"),  # decodes: counted
              log_line(120, "px-b").encode().replace(b"px-b", b"px-\xff"),  # in-window, bad byte in a field: PARTIAL
              (NOW - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ").encode()
              + b" EMERGENCY_M\xffSE role=primary name=px-c\n",  # in-window, the marker itself is corrupt: PARTIAL
              b"\xff\xfe no stamp, undecodable\n",  # cannot be placed in time: PARTIAL
              log_line(3700, "px-old").encode().replace(b"px-old", b"px-\xff"),  # undecodable but stamped before the window
              log_line(60, "px-d", "haiku")],  # decodes: counted
         muse=muse_cell(2, 1, 1, "3 malformed or undecodable log lines", partial=True)),
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
        manifest_file = run / "manifest.json"
        if isinstance(payload, bytes):
            manifest_file.write_bytes(payload)
        else:
            manifest_file.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    log = tmp_path / "muse.log"
    if case.get("log") is not None:
        log.write_bytes(b"".join(row if isinstance(row, bytes) else row.encode() for row in case["log"]))
    section = render(NOW, caps=caps, root=runs, log=log, ps_text=ps(*case.get("ps", ())), environ=case["env"])
    head, header, separator, by, names = parse(section)
    assert header == composer.OPENCODE_TABLE_HEADER
    assert names == [POOL, HY4, DEEP, GLM, MUSE]
    live, reg = case.get("live", counts(0)), case.get("reg", counts(0))
    for name in names:
        assert (by[name]["Live now"], by[name]["Registered last 1h"]) == (live[name], reg[name]), name
    expected_pool_note = case.get("cap", "cap 2 (default: key absent, no cap file)")
    if case.get("reg_partial"):
        n = case["reg_partial"]
        expected_pool_note += (f"; registrations PARTIAL, observed minimum ({n} manifest{'' if n == 1 else 's'} "
                               "unreadable, undecodable, malformed or missing model/start time)")
    assert by[POOL]["Note"] == expected_pool_note
    assert by[MUSE]["Note"] == MUSE_LABEL + case.get("muse", muse_cell(0, 0, 0, "log file absent"))
    assert all(by[name]["Note"] == "—" for name in (HY4, DEEP, GLM))
    for banned in BANNED:
        assert banned not in section.lower()


# ---- the table ----

def test_section_is_a_pipe_table_with_one_pool_row_then_one_row_per_model(tmp_path):
    caps = tmp_path / "caps"
    caps.write_text("OPENCODE_PROXY_CAP=3\n")
    runs = tmp_path / "runs"
    runs.mkdir()
    manifest(runs, "px-a", HY4, stamp(60))
    manifest(runs, "px-b", DEEP, stamp(600))
    manifest(runs, "px-c", DEEP, stamp(900))
    log = tmp_path / "muse.log"
    log.write_text(log_line(200, "px-m"))
    section = render(NOW, caps=caps, root=runs, log=log, ps_text=ps(DEEP, MUSE), environ={})
    rows = section.splitlines()
    assert rows[0].startswith("*OpenCode Go:*")
    assert rows[1] == "| " + " | ".join(composer.OPENCODE_TABLE_HEADER) + " |"
    assert rows[2] == "|" + "|".join("---" for _ in composer.OPENCODE_TABLE_HEADER) + "|"
    for row in rows[1:2] + rows[3:]:  # one cell per header column, or the native table would shift its columns
        assert len(row.strip().strip("|").split("|")) == len(composer.OPENCODE_TABLE_HEADER), row
    _, _, _, by, names = parse(section)
    assert names == [POOL, HY4, DEEP, GLM, MUSE]
    live, reg = counts(2, deep=1, muse=1), counts(3, hy4=1, deep=2)
    for name in names:
        assert (by[name]["Live now"], by[name]["Registered last 1h"]) == (live[name], reg[name])
    assert by[POOL]["Note"] == "cap 3 (cap file)"
    assert by[MUSE]["Note"] == MUSE_LABEL + muse_cell(1, 1, 0)
    for banned in BANNED:
        assert banned not in section.lower()


def test_each_unreadable_source_fails_independently_and_never_reads_zero(monkeypatch, tmp_path):
    def no_ps(*args, **kwargs):
        raise OSError("ps unavailable")

    monkeypatch.setattr(subprocess, "run", no_ps)
    log_dir = tmp_path / "log-is-a-directory"
    log_dir.mkdir()
    section = render(NOW, caps=tmp_path / "caps", root=tmp_path / "no-runs", log=log_dir, environ={})
    _, _, _, by, names = parse(section)
    assert names == [POOL, HY4, DEEP, GLM, MUSE]
    for name in names:
        assert (by[name]["Live now"], by[name]["Registered last 1h"]) == ("UNAVAILABLE", "UNAVAILABLE"), name
    assert by[POOL]["Used"] == "UNAVAILABLE"
    assert by[POOL]["Note"] == "cap 2 (default: key absent, no cap file)"
    assert by[MUSE]["Note"] == MUSE_LABEL + "UNAVAILABLE"


def test_a_live_failure_does_not_hide_the_registration_counts(monkeypatch, tmp_path):
    def no_ps(*args, **kwargs):
        raise subprocess.TimeoutExpired("ps", 5)

    monkeypatch.setattr(subprocess, "run", no_ps)
    runs = tmp_path / "runs"
    runs.mkdir()
    manifest(runs, "px-a", HY4, stamp(60))
    section = render(NOW, caps=tmp_path / "caps", root=runs, log=tmp_path / "muse.log", environ={})
    _, _, _, by, _ = parse(section)
    assert by[POOL]["Live now"] == by[HY4]["Live now"] == "UNAVAILABLE"
    assert (by[POOL]["Registered last 1h"], by[HY4]["Registered last 1h"], by[DEEP]["Registered last 1h"]) == ("1", "1", "0")
    assert by[MUSE]["Note"] == MUSE_LABEL + muse_cell(0, 0, 0, "log file absent")


def test_a_cap_source_failure_reads_unverified_and_leaves_the_other_fields(monkeypatch, tmp_path):
    def broken_cap(*args, **kwargs):
        raise RuntimeError("cap source broke")

    monkeypatch.setattr(composer, "opencode_cap", broken_cap)
    runs = tmp_path / "runs"
    runs.mkdir()
    section = render(NOW, caps=tmp_path / "caps", root=runs, log=tmp_path / "muse.log", ps_text=ps(), environ={})
    _, _, _, by, _ = parse(section)
    assert by[POOL]["Note"] == "cap UNVERIFIED (cap source failed)"
    assert (by[POOL]["Live now"], by[POOL]["Registered last 1h"]) == ("0", "0")


def test_a_source_failure_in_the_usage_reader_reads_unavailable_not_an_exception(monkeypatch, tmp_path):
    def broken_usage(*args, **kwargs):
        raise RuntimeError("usage reader broke")

    monkeypatch.setattr(composer, "opencode_usage", broken_usage)
    runs = tmp_path / "runs"
    runs.mkdir()
    section = render(NOW, caps=tmp_path / "caps", root=runs, log=tmp_path / "muse.log", ps_text=ps(), environ={})
    _, _, _, by, _ = parse(section)
    assert by[POOL]["Used"] == "UNAVAILABLE"
    assert by[POOL]["Live now"] == "0"  # the other columns are unaffected


# ---- plan usage windows (the Used column) ----

def test_usage_windows_render_percent_and_reset_per_window_newest_sample_wins(tmp_path):
    snap = snapshot_file(tmp_path, [
        usage_row("monthly", pct=11, ago=3000), usage_row("monthly", pct=17, ago=600, reset_in=40 * 86400),
        usage_row("weekly", pct=34, reset_in=2 * 86400), usage_row("rolling", pct=23, reset_in=3600),
        {"key": "claude:a@example.test:weekly", "status": "ok", "pct": 99, "ts": NOW.timestamp()},  # another account
    ])
    used = composer.opencode_usage(NOW, snap)
    parts = used.split(" · ")
    assert [p.split(" ")[0] for p in parts] == ["monthly", "weekly", "rolling"]
    assert parts[0].startswith("monthly 17% (resets ") and parts[0].endswith(" IST)")
    assert parts[1].startswith("weekly 34% (resets ") and parts[2].startswith("rolling 23% (resets ")
    assert "99" not in used and "11%" not in used


def test_usage_window_without_a_fresh_percentage_reads_stale_or_n_a_never_zero(tmp_path):
    snap = snapshot_file(tmp_path, [
        usage_row("monthly", pct=17, ago=7300),  # sampled longer ago than USAGE_SAMPLE_MAX_AGE
        usage_row("weekly", pct=34, reset_in=-60),  # its reset has already passed: the percentage is from a closed window
        usage_row("rolling", status="error"),  # sampler could not read this window
    ])
    assert composer.opencode_usage(NOW, snap) == "monthly stale · weekly stale · rolling n/a"


def test_usage_window_missing_from_the_snapshot_is_n_a_and_no_row_at_all_is_unavailable(tmp_path):
    assert composer.opencode_usage(NOW, snapshot_file(tmp_path, [usage_row("weekly", pct=5)])).startswith("monthly n/a · weekly 5%")
    assert composer.opencode_usage(NOW, snapshot_file(tmp_path, [{"key": "claude:x:weekly", "ts": NOW.timestamp()}])) == "UNAVAILABLE"
    assert composer.opencode_usage(NOW, tmp_path / "missing.jsonl") == "UNAVAILABLE"
    assert composer.opencode_usage(NOW, tmp_path) == "UNAVAILABLE"  # a directory: unreadable, not an exception


def test_usage_reader_skips_malformed_lines_and_rejects_out_of_range_or_non_numeric_percentages(tmp_path):
    path = tmp_path / "usage-snapshots.jsonl"
    rows = ["{not json opencode:go:", json.dumps(["opencode:go:weekly"]),
            json.dumps(usage_row("monthly", pct=140)), json.dumps(usage_row("weekly", pct=True)),
            json.dumps(usage_row("rolling", pct="23"))]
    path.write_text("\n".join(rows) + "\n")
    assert composer.opencode_usage(NOW, path) == "monthly n/a · weekly n/a · rolling n/a"


def test_the_pool_row_carries_the_usage_windows_and_the_model_rows_do_not(tmp_path):
    snap = snapshot_file(tmp_path, [usage_row("monthly", pct=17), usage_row("weekly", pct=34), usage_row("rolling", pct=23)])
    runs = tmp_path / "runs"
    runs.mkdir()
    section = render(NOW, caps=tmp_path / "caps", root=runs, log=tmp_path / "muse.log", ps_text=ps(), environ={}, snapshot=snap)
    _, _, _, by, _ = parse(section)
    assert by[POOL]["Used"].startswith("monthly 17% (resets ") and "weekly 34%" in by[POOL]["Used"]
    assert all(by[name]["Used"] == "—" for name in (HY4, DEEP, GLM, MUSE))
    assert "$" not in section and "%/h" not in section


# ---- placement in the hourly report ----

def test_compose_places_the_section_under_the_burn_block_on_1h_only():
    now = datetime.now(timezone.utc)
    section = render(now, ps_text=ps(), environ={}, root=Path("/nonexistent/runs"), log=Path("/nonexistent/muse.log"))
    burn = ("*Burn:* pace to Claude · Codex; headroom = budget - burn, %/h; mode is per pool\n"
            "| Pool / account | State |\n|---|---|\n| Claude pool | OK · routes NORMAL |",
            {"Claude": "NORMAL", "Codex": "NORMAL"})
    hourly = composer.compose("1h", now, {"events": 0}, {}, [], burn=burn, opencode=section).splitlines()
    three = composer.compose("3h", now, {"events": 0}, {}, [], opencode=section)
    head = section.splitlines()[0]
    assert head in hourly
    assert hourly.index(head) > hourly.index("| Claude pool | OK · routes NORMAL |")
    assert head not in three
    assert "*OpenCode Go:*" not in composer.compose("1h", now, {"events": 0}, {}, [])


def test_compose_keeps_one_native_table_and_turns_the_opencode_table_into_plain_rows():
    now = datetime.now(timezone.utc)
    section = render(now, ps_text=ps(), environ={}, root=Path("/nonexistent/runs"), log=Path("/nonexistent/muse.log"))
    out = composer.compose("1h", now, {"events": 0}, {}, [], opencode=section).splitlines()
    # With no Burn table above it, the OpenCode table is the first table in the post and stays native.
    assert "| " + " | ".join(composer.OPENCODE_TABLE_HEADER) + " |" in out
    pace = ["| Account | Used |", "|---|---|", "| a | 1% |"]
    rewritten = composer.one_native_table([*pace, *section.splitlines()])
    assert rewritten[:3] == pace
    assert not any(row.startswith("|") for row in rewritten[3:])  # Slack allows one native table block per message
    assert any(row.startswith(f"{POOL} — Live now: 0") for row in rewritten)
    assert any(row.startswith(f"{HY4} — Live now: 0") for row in rewritten)
