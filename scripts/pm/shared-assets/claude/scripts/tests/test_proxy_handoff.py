"""Tests for ~/.claude/scripts/proxy-handoff.py, the RESULT "handoff" field in proxy-agent-notify.py and pm-handoff-fresh.py.

Run this file only:  python3 -m pytest ~/.claude/scripts/tests/test_proxy_handoff.py -q
(local PM tooling tests; rule 46 does not apply to them). Nothing here touches Slack, the pm-ops DB or the real handoffs directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ph = load("proxy_handoff", "proxy-handoff.py")
notify = load("proxy_agent_notify", "proxy-agent-notify.py")
fresh = load("pm_handoff_fresh", "pm-handoff-fresh.py")

NEXT = ("Launch hd-proof-qa on PR #14692 at head 7e08919f014b3e62c92f6eaddc9448b22727f07b and run the wizard QA spec "
        "tests/e2e/specs/qa-tests/13956-upload-document-labels.spec.ts on the remote QA box with --on-miss")
DONT1 = "Don't loop on wizard-test navigation: reuse the established wizard test ids."
DONT2 = "Don't escalate a P0 relaunch with --rung 3: it reaches Sol."


def handoff(next_item: str = NEXT, donts: tuple[str, ...] = (DONT1, DONT2), extra_done: str = "") -> str:
    dont_lines = "\n".join(f"- {d}" for d in donts)
    return (f"# HANDOFF issue #14679\nupdated: 2000-01-01T00:00:00Z by draft\n\n## DONE\n- plan pushed 444e609e{extra_done}\n\n"
            f"## OPEN\n- QA spec pending\n\n## NEXT\n- {next_item}\n\n## DECIDED + WHY\n- frontend only (rule 57)\n\n## DON'T\n{dont_lines}\n")


@pytest.fixture()
def hdir(tmp_path, monkeypatch):
    path = tmp_path / "handoffs"
    monkeypatch.setenv("PROXY_HANDOFF_DIR", str(path))
    for key in ("PROXY_AGENT_ISSUE", "PROXY_HANDOFF", "PROXY_HANDOFF_TYPES", "PROXY_AGENT_NAME"):
        monkeypatch.delenv(key, raising=False)
    return path


@pytest.fixture()
def run(capsys, tmp_path):
    def _run(*argv: str, text: str | None = None):
        if text is not None:
            src = tmp_path / f"draft-{abs(hash(text))}.md"
            src.write_text(text)
            argv = (*argv, "--from", str(src))
        rc = ph.main(list(argv))
        return rc, capsys.readouterr().out
    return _run


def brief(tmp_path: Path, body: str, name: str = "brief.md") -> str:
    path = tmp_path / name
    path.write_text(body)
    return str(path)


# ---- write / read round trip ------------------------------------------------------------------------------------------------------
def test_write_read_round_trip(hdir, run):
    rc, out = run("write", "--issue", "14679", "--by", "lane-a", text=handoff())
    assert rc == 0 and "HANDOFF_WRITTEN" in out
    rc, body = run("read", "--issue", "14679")
    assert rc == 0
    assert "by lane-a" in body and "2000-01-01" not in body  # the helper stamps updated:, the draft's stamp is replaced
    _pre, sections, order = ph.parse(body)
    assert order == list(ph.SECTIONS)
    assert ph.items(sections["NEXT"])[0] == NEXT
    assert [ph.norm(i) for i in ph.dont_items(body)] == [ph.norm(DONT1), ph.norm(DONT2)]
    assert run("validate", "--issue", "14679")[1].strip() == "HANDOFF_OK"
    rc, again = run("write", "--issue", "14679", "--by", "lane-b", text=body)  # rewriting its own output is stable (no blank-line growth)
    assert rc == 0
    assert run("read", "--issue", "14679")[1].split("\n", 2)[2] == body.split("\n", 2)[2]


@pytest.mark.parametrize("mutate,reason", [
    (lambda t: t.replace("## OPEN\n- QA spec pending\n\n", ""), "missing_section:OPEN"),
    (lambda t: t.replace("- " + NEXT, "- UNSET: later"), "next_unset"),
    (lambda t: t.replace("- QA spec pending", ""), "empty_section:OPEN"),
    (lambda t: t + "\nBearer abcdefghijklmnopqrstuvwxyz0123456789\n", "secret_like_token"),
    (lambda t: t.replace("## OPEN", "## TEMP").replace("## DONE", "## OPEN").replace("## TEMP", "## DONE"), "section_order"),
])
def test_validate_refuses_bad_drafts(hdir, run, mutate, reason):
    rc, out = run("write", "--issue", "14679", text=mutate(handoff()))
    assert rc == 2 and reason in out
    assert not (hdir / "issue-14679.md").exists()


# ---- successor injection -------------------------------------------------------------------------------------------------------------
def test_successor_injection_carries_the_handoff(hdir, run, tmp_path, capsys):
    run("write", "--issue", "14679", "--by", "lane-a", text=handoff())
    path = brief(tmp_path, "MODE: IMPLEMENT\nISSUE: 14679\nPR: UNKNOWN\nGOAL: pinned goal\n")
    block, status = ph.build_injection(path, "hd-builder", "proxy-hd-builder-14679d")
    assert status.startswith("present issue=14679") and "next=set" in status
    assert "HANDOFF FOR ISSUE #14679" in block
    assert NEXT in block and DONT1 in block and DONT2 in block  # the DON'T section reaches the successor
    assert "RESTATE GOAL:" in block and "RESTATE NEXT:" in block
    assert "restate-check --issue 14679" in block and "write --issue 14679" in block and "add-dont --issue 14679" in block
    assert '"handoff": "' + str(hdir / "issue-14679.md") + '"' in block
    rc = ph.main(["inject", "--brief", path, "--type", "hd-builder", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("PROXY_AGENT_HANDOFF present issue=14679") and "HANDOFF CONTRACT" in out


def test_first_lane_gets_a_contract_without_a_file(hdir, tmp_path):
    block, status = ph.build_injection(brief(tmp_path, "ISSUE: 14700\n"), "hd-planner", "n")
    assert status.startswith("absent issue=14700")
    assert "none yet (you are the first lane" in block and "HANDOFF CONTRACT" in block


def test_injection_is_skipped_when_not_eligible(hdir, tmp_path, monkeypatch):
    path = brief(tmp_path, "ISSUE: 14679\n")
    assert ph.build_injection(path, "pm-admission-reviewer", "n") == ("", "skip reason=type_not_eligible type=pm-admission-reviewer")
    assert ph.build_injection(brief(tmp_path, "no refs here\n", "norefs.md"), "hd-builder", "n")[1] == "skip reason=no_issue_ref"
    monkeypatch.setenv("PROXY_HANDOFF", "0")
    assert ph.build_injection(path, "hd-builder", "n")[0] == ""
    monkeypatch.delenv("PROXY_HANDOFF")
    monkeypatch.setenv("PROXY_HANDOFF_TYPES", "hd-builder")
    assert ph.build_injection(path, "hd-planner", "n")[0] == "" and ph.build_injection(path, "hd-builder", "n")[0] != ""


def test_inject_fails_open(hdir, tmp_path, capsys):
    assert ph.main(["inject", "--brief", str(tmp_path / "missing.md"), "--type", "hd-builder"]) == 0
    assert capsys.readouterr().out == ""


def test_large_handoff_keeps_next_and_every_dont(hdir):
    big = handoff(extra_done="\n" + "\n".join(f"- old detail line {i} " + "x" * 80 for i in range(400)))
    rendered = ph.render_for_injection(big)
    assert len(rendered) < len(big) and NEXT in rendered and DONT1 in rendered and DONT2 in rendered
    assert "older lines cut" in rendered


# ---- DON'T entries are preserved -----------------------------------------------------------------------------------------------------
def test_dont_is_never_dropped_without_pm_approval(hdir, run):
    assert run("write", "--issue", "14679", text=handoff())[0] == 0
    before = (hdir / "issue-14679.md").read_text()
    rc, out = run("write", "--issue", "14679", text=handoff(donts=(DONT1,)))  # drops DONT2
    assert rc == 3 and "dont_dropped" in out and "--rung 3" in out
    assert (hdir / "issue-14679.md").read_text() == before
    rc, out = run("write", "--issue", "14679", "--approve-drop", "not-a-slack-ts", "--drop", DONT2, text=handoff(donts=(DONT1,)))
    assert rc == 3
    rc, _ = run("write", "--issue", "14679", text=handoff(donts=(DONT1 + " Rewritten differently.", DONT2)))  # extension keeps the prefix
    assert rc == 0
    rc, _ = run("write", "--issue", "14679", text=handoff(donts=("Reworded: dont loop on wizard tests", DONT2)))  # rewording is a drop
    assert rc == 3


def test_dont_drop_with_recorded_approval(hdir, run):
    run("write", "--issue", "14679", text=handoff())
    rc, out = run("write", "--issue", "14679", "--by", "pm", "--approve-drop", "1795443398.969229", "--drop", DONT2[:30], text=handoff(donts=(DONT1,)))
    assert rc == 0 and "dropped_with_approval=1" in out
    log = (hdir / "issue-14679.drops.log").read_text()
    assert "approved_ts=1795443398.969229" in log and "--rung 3" in log
    assert ph.norm(DONT2) not in [ph.norm(i) for i in ph.dont_items((hdir / "issue-14679.md").read_text())]


def test_add_dont_appends_and_is_idempotent(hdir, run):
    run("write", "--issue", "14679", text=handoff())
    assert run("add-dont", "--issue", "14679", "Ornith", "lanes", "must", "not", "post", "unverifiable", "box", "results")[0] == 0
    items = ph.dont_items((hdir / "issue-14679.md").read_text())
    assert len(items) == 3 and items[-1].startswith("Ornith lanes must not post")
    assert "HANDOFF_UNCHANGED" in run("add-dont", "--issue", "14679", "Ornith lanes must not post unverifiable box results")[1]
    assert len(ph.dont_items((hdir / "issue-14679.md").read_text())) == 3
    assert run("validate", "--issue", "14679")[1].strip() == "HANDOFF_OK"


def test_add_dont_creates_a_skeleton_that_forces_a_next_step(hdir, run):
    assert run("add-dont", "--issue", "14700", "never", "retry", "the", "same", "wizard", "click")[0] == 0
    rc, out = run("validate", "--issue", "14700")
    assert rc == 2 and "next_unset" in out
    assert ph.first_next((hdir / "issue-14700.md").read_text()) is None


# ---- restate check -------------------------------------------------------------------------------------------------------------------
def test_restate_check_accepts_a_faithful_restate_and_rejects_a_mismatch(hdir, run):
    run("write", "--issue", "14679", text=handoff())
    faithful = ("RESTATE NEXT: launch hd-proof-qa for PR #14692 at head 7e08919f and run the QA spec "
                "13956-upload-document-labels.spec.ts on the remote QA box with --on-miss")
    rc, out = run("restate-check", "--issue", "14679", "--next", faithful)
    assert rc == 0 and out.startswith("RESTATE_OK")
    rc, out = run("restate-check", "--issue", "14679", "--next", "RESTATE NEXT: rebase the branch and rewrite the wizard tests")
    assert rc == 4 and out.startswith("RESTATE_MISMATCH")
    rc, out = run("restate-check", "--issue", "14679", "--next", "launch hd-proof-qa and run the QA spec on the remote QA box with --on-miss")
    assert rc == 4 and "missing=" in out and "14692" in out  # right words, but the PR and head identifiers are gone
    rc, out = run("restate-check", "--issue", "14679", "--next", "  ")
    assert rc == 4 and "no_restate" in out


def test_restate_check_without_a_handoff_does_not_block(hdir, run):
    assert run("restate-check", "--issue", "14999", "--next", "anything")[1].startswith("NO_HANDOFF")
    run("add-dont", "--issue", "14998", "x", "y", "z")  # skeleton, NEXT unset
    rc, out = run("restate-check", "--issue", "14998", "--next", "anything")
    assert rc == 0 and "next_unset" in out


def test_restate_check_reads_the_first_restate_from_the_lane_stream(hdir, run, tmp_path):
    run("write", "--issue", "14679", text=handoff())
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": "RESTATE GOAL: g\nRESTATE NEXT: " + NEXT}]}}
    stream = tmp_path / "lane.jsonl"
    stream.write_text(json.dumps({"type": "system"}) + "\n" + json.dumps(event) + "\n")
    rc, out = run("restate-check", "--issue", "14679", "--jsonl", str(stream))
    assert rc == 0 and out.startswith("RESTATE_OK")
    stream.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "starting work"}]}}) + "\n")
    assert run("restate-check", "--issue", "14679", "--jsonl", str(stream))[0] == 4


# ---- key resolution ------------------------------------------------------------------------------------------------------------------
def test_key_resolution(hdir, tmp_path, monkeypatch, run):
    assert ph.resolve_key("ISSUE: 14577\nPR: 14682\n")[0] == "14577"
    assert ph.resolve_key("PR: 14682\n")[0] == "14682"  # no link yet: the PR number itself
    block, _ = ph.build_injection(brief(tmp_path, "ISSUE: #14577\nPR: 14682\n"), "hd-proof-qa", "n")  # both lines: the link is recorded
    assert (hdir / "pr-14682.issue").read_text().strip() == "14577"
    assert ph.resolve_key("PR: 14682\n")[0] == "14577"  # a PR-only brief now finds the issue's handoff
    monkeypatch.setenv("PROXY_AGENT_ISSUE", "#15000")
    assert ph.resolve_key("ISSUE: 14577\n")[0] == "15000"
    monkeypatch.delenv("PROXY_AGENT_ISSUE")
    assert ph.resolve_key("nothing\n")[0] is None
    assert run("link", "--pr", "14613", "--issue", "14612")[1].startswith("HANDOFF_LINKED")
    assert ph.resolve_key("PR: 14613\n")[0] == "14612"


# ---- RESULT block "handoff" field ----------------------------------------------------------------------------------------------------
def result_text(extra: dict | None = None) -> str:
    block = {"lane": "l", "type": "hd-builder", "issue": 14679, "pr": 14692, "head": "a" * 40, "verdict": "HANDOFF",
             "proof_links": [], "next_stage": None, "notes": "n"}
    block.update(extra or {})
    return "work\nRESULT_BEGIN\n" + json.dumps(block) + "\nRESULT_END\nHANDOFF: ready"


def test_result_block_handoff_field(hdir, run):
    run("write", "--issue", "14679", text=handoff())
    path = str(hdir.resolve() / "issue-14679.md")
    found = notify.structured_result(result_text({"handoff": path}), "l", "", "", "")
    assert found["status"] == "STRUCTURED"
    assert found["block"]["handoff"] == path and found["block"]["handoff_status"] == "ok"
    legacy = notify.structured_result(result_text(), "l", "", "", "")  # the field is optional: older lanes stay STRUCTURED
    assert legacy["status"] == "STRUCTURED" and legacy["block"]["handoff"] is None and legacy["block"]["handoff_status"] == "missing"
    outside = notify.structured_result(result_text({"handoff": "/etc/passwd"}), "l", "", "", "")
    assert outside["status"] == "STRUCTURED" and outside["block"]["handoff"] is None
    assert outside["block"]["handoff_status"] == "invalid_path"
    gone = notify.structured_result(result_text({"handoff": str(hdir.resolve() / "issue-99999.md")}), "l", "", "", "")
    assert gone["block"]["handoff_status"] == "missing"
    (hdir / "issue-14680.md").write_text("# not a handoff\n")
    bad = notify.structured_result(result_text({"handoff": str(hdir.resolve() / "issue-14680.md")}), "l", "", "", "")
    assert bad["block"]["handoff_status"] == "invalid"
    other = notify.structured_result(result_text({"type": "pm-admission-reviewer"}), "l", "", "", "")
    assert other["block"]["handoff_status"] == "n/a"


# ---- pm-handoff-fresh ----------------------------------------------------------------------------------------------------------------
NOTE_OK = "# GOAL\n- x\n## DONE\n- x\n## OPEN\n- x\n## NEXT\n- x\n## DECIDED + WHY\n- x\n## DON'T\n- x\n"


def test_pm_handoff_fresh_states(tmp_path, capsys):
    state = tmp_path / "hourly.json"
    note = tmp_path / "note.md"
    state.write_text(json.dumps({"channel": "C", "ts": "1795441441.525659", "recorded_at": 1795441548}))
    note.write_text(NOTE_OK)
    os.utime(note, (1795441441.5 + 600, 1795441441.5 + 600))
    assert fresh.check(note, state)[0] == "HANDOFF_FRESH"
    os.utime(note, (1795441441.5 - 600, 1795441441.5 - 600))
    status, detail = fresh.check(note, state)
    assert status == "HANDOFF_STALE" and "older_than_hourly_parent" in detail
    note.write_text("# GOAL\n- x\n## DONE\n- x\n")
    os.utime(note, (1795441441.5 + 600, 1795441441.5 + 600))
    status, detail = fresh.check(note, state)
    assert status == "HANDOFF_STALE" and "shape_missing:OPEN,NEXT,DECIDED + WHY,DON'T" in detail
    state.write_text("not json")
    assert fresh.check(note, state)[0] == "HANDOFF_UNKNOWN"
    assert fresh.check(tmp_path / "missing.md", state)[0] == "HANDOFF_UNKNOWN"
    # never a gate: the default exit code is 0 even when stale or unknown; --exit-code opts into 1 and 2
    assert fresh.main(["--note", str(note), "--state", str(state)]) == 0
    assert fresh.main(["--note", str(note), "--state", str(state), "--exit-code"]) == 2
    capsys.readouterr()
