"""Focused proof for the adopted issue-contract-ledger enforcement closure.

Hermetic and FAIL-CLOSED: both registered wrappers are executed with synthetic
PreToolUse JSON, an adopted-parser/validator pair (or the live preimage), and
gh/validator shims. A wrapper that exits nonzero or prints nothing FAILS the
case, so a parser crash can never satisfy an ALLOW assertion. No live hook,
GitHub call, or issue mutation occurs.
"""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from unittest.mock import Mock
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
HOME_WRAPPER = ROOT / "scripts/pm/shared-assets/claude/hooks/block-invalid-issue-contract-ledger.sh"
APP_WRAPPER = ROOT / "scripts/pm/shared-assets/claude/hooks/heydonna-app-block-invalid-issue-contract-ledger.sh"
PARSER = ROOT / "scripts/pm/shared-assets/claude/scripts/issue-contract-ledger-hook.py"
VALIDATOR = ROOT / "scripts/pm/shared-assets/claude/scripts/validate-issue-contract-ledger.py"
POLICY = ROOT / "scripts/pm/shared-assets/claude/scripts/control_plane_issue_policy.py"

PROSE = ('python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-resolve --kind ci_rework --target-id 18218 '
         '--external-state "reworded prose mentioning gh issue edit 7945 --repo heydonna-app/heydonna-app as history only"')
VARIABLE = "gh issue edit $ISSUE --repo Scribie/heydonna-app --body-file /tmp/body.md"
QUOTED_TEMPLATE = 'gh issue edit "7925" --repo Scribie/heydonna-app --body-file {body}'


def shims(tmp: Path, *, validator_ok: bool, live_body: str = "plain body"):
    gh = tmp / "gh"
    payload = shlex.quote(json.dumps({"body": live_body, "labels": []}))
    gh.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' {payload}\n", encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    val = tmp / "validator.py"
    val.write_text("import json,sys\nprint(json.dumps({'ok': %s, 'errors': [] if %s else ['missing_contract_ledger']}))\n"
                   % (validator_ok, validator_ok), encoding="utf-8")
    return str(gh), str(val)


def run(wrapper: Path, parser: Path, validator: str, gh: str, tmp: Path, command: str) -> dict:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = dict(os.environ,
               ISSUE_CONTRACT_LEDGER_HOOK_PARSER=str(parser),
               ISSUE_CONTRACT_LEDGER_VALIDATOR=validator,
               ISSUE_CONTRACT_LEDGER_GH_BIN=gh,
               ISSUE_CONTRACT_LEDGER_TEMPLATE=str(tmp / "template.md"))
    # FAIL CLOSED: the parser itself must exit 0. A crash here (nonzero) would
    # otherwise be swallowed by the wrapper's `|| true` and read as a silent
    # ALLOW, so it is asserted before the wrapper runs.
    parser_run = subprocess.run(["python3", str(parser)], input="", capture_output=True, text=True, timeout=30,
                                env=dict(env, CMD_TEXT=command))
    assert parser_run.returncode == 0, f"parser crashed rc={parser_run.returncode}: {parser_run.stderr[:300]}"
    done = subprocess.run(["bash", str(wrapper)], input=payload, env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, f"wrapper exit {done.returncode}: {done.stderr[:300]}"
    assert "Traceback" not in done.stderr, f"wrapper stderr traceback: {done.stderr[:300]}"
    if not done.stdout.strip():
        # The wrapper prints nothing on ALLOW; the parser exit above proved this
        # is a real ALLOW rather than a swallowed crash.
        return {"decision": "allow"}
    text = done.stdout[done.stdout.index("{"):]
    return json.loads(text)


def blocked(result: dict) -> bool:
    return result.get("decision") == "block"


def test_policy_closure_has_no_unowned_imports() -> None:
    for path in (PARSER, POLICY, VALIDATOR):
        assert path.is_file(), path
    for line in POLICY.read_text(encoding="utf-8").splitlines() + VALIDATOR.read_text(encoding="utf-8").splitlines():
        if line.startswith(("import ", "from ")):
            mod = line.split()[1].split(".")[0]
            assert mod in {"__future__", "argparse", "json", "os", "re", "subprocess", "sys"}, line


def test_both_wrappers_allow_quoted_prose(tmp_path) -> None:
    gh, val = shims(tmp_path, validator_ok=True)
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        assert not blocked(run(wrapper, PARSER, val, gh, tmp_path, PROSE)), f"{wrapper.name} blocked prose"


def test_both_wrappers_type_the_variable_target(tmp_path) -> None:
    gh, val = shims(tmp_path, validator_ok=True)
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        out = run(wrapper, PARSER, val, gh, tmp_path, VARIABLE)
        assert blocked(out) and "literal issue number" in out.get("message", ""), out


def body_file(tmp_path: Path) -> str:
    body = tmp_path / "body.md"
    body.write_text("## Issue Contract Ledger\ncomplete\n", encoding="utf-8")
    return str(body)


def test_both_wrappers_accept_quoted_literal(tmp_path) -> None:
    gh, val = shims(tmp_path, validator_ok=True)
    command = QUOTED_TEMPLATE.format(body=body_file(tmp_path))
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        assert not blocked(run(wrapper, PARSER, val, gh, tmp_path, command)), "quoted literal must work"


def test_genuine_mutation_still_blocks(tmp_path) -> None:
    gh, val = shims(tmp_path, validator_ok=False)
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        out = run(wrapper, PARSER, val, gh, tmp_path, QUOTED_TEMPLATE.format(body=body_file(tmp_path)))
        assert blocked(out) and "Issue Contract Ledger" in out.get("message", ""), out


@pytest.mark.parametrize("repo", ["Scribie/heydonna-app", "scribie/heydonna-app", "SCRIBIE/HEYDONNA-APP", "sCrIbIe/HeyDonna-App"])
@pytest.mark.parametrize("mutation", ["-X POST", "-f title=synthetic-review -f body=invalid"])
def test_both_wrappers_block_raw_rest_issue_create_case_insensitively(tmp_path, repo, mutation) -> None:
    gh, val = shims(tmp_path, validator_ok=True)
    command = f"gh api repos/{repo}/issues {mutation}"
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        out = run(wrapper, PARSER, val, gh, tmp_path, command)
        assert blocked(out) and "unsupported_raw_api_issue_create" in out.get("message", ""), out


@pytest.mark.parametrize("command", [
    "gh api repos/Scribie/heydonna-app/issues -X GET",
    "gh api repos/unrelated/project/issues -X POST -f title=synthetic-review -f body=invalid",
])
def test_both_wrappers_leave_other_rest_operations_alone(tmp_path, command) -> None:
    gh, val = shims(tmp_path, validator_ok=True)
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        assert not blocked(run(wrapper, PARSER, val, gh, tmp_path, command))


@pytest.mark.parametrize("operation", ["create --title synthetic-review", "edit 7925"])
@pytest.mark.parametrize("validator_ok", [False, True])
def test_both_wrappers_preserve_ordinary_issue_validation(tmp_path, operation, validator_ok) -> None:
    gh, val = shims(tmp_path, validator_ok=validator_ok)
    command = f"gh issue {operation} --repo Scribie/heydonna-app --body-file {body_file(tmp_path)}"
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        out = run(wrapper, PARSER, val, gh, tmp_path, command)
        assert blocked(out) == (not validator_ok), out


READY_HEADER = "<!-- ready-pool:\r\npriority: P1\r\nwork_type: bug\r\n-->"
LEDGER = (
    "## Issue Contract Ledger\n"
    "- **Positive AC:** Reload shows the saved speaker name.\n"
    "- **Negative AC:** Other files retain their speaker names.\n"
    "- **Forbidden implementation:** Do not change the workspace speaker directory.\n"
    "- **Determinism:** The saved speaker name is deterministic.\n"
    "- **Required proof:** Unit test the saved-speaker lookup.\n"
)
REAL_ISSUE = (
    "<!-- qa-proof-schema: 1 -->\n\n"
    "## Problem\nThe saved transcript shows an outdated speaker name.  \n\n"
    "## Acceptance criteria\n1. AC-1: Reload shows the corrected name ‘Speaker 2’.\n"
    "   - Surface: nonvisual\n   - QA reachability: deterministic\n   - Required proof: unit\n\n"
    "## Out of scope\nDo not rename labels on other transcripts.\n\n"
    + LEDGER + "\n"
).replace("\n", "\r\n")
RULE_54_STUB = "## Problem\nMissing status: label, violating rule 54.\n\n" + LEDGER


@pytest.mark.parametrize("old,new", [
    (REAL_ISSUE, READY_HEADER + "\r\n\r\n" + REAL_ISSUE),
    (REAL_ISSUE + READY_HEADER + "\r\n", REAL_ISSUE + READY_HEADER.replace("P1", "P2") + "\r\n"),
    (RULE_54_STUB, READY_HEADER + "\r\n\r\n" + RULE_54_STUB),
])
def test_label_writer_accepts_only_header_changes_without_rewriting_issue(tmp_path, old, new):
    gh, _ = shims(tmp_path, validator_ok=True, live_body=old)
    path = tmp_path / "ready-pool.md"
    path.write_bytes(new.encode("utf-8"))
    command = QUOTED_TEMPLATE.format(body=path) + " --add-label status:backlog"
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        assert not blocked(run(wrapper, PARSER, str(VALIDATOR), gh, tmp_path, command))


@pytest.mark.parametrize("old,new,reason", [
    (REAL_ISSUE, RULE_54_STUB, "ready_pool_edit_changes_existing_issue_text"),
    (REAL_ISSUE, READY_HEADER + "\r\n\r\n" + REAL_ISSUE.rstrip(), "ready_pool_edit_changes_existing_issue_text"),
    (REAL_ISSUE + "<!-- ready-pool:\npriority: P1\n", REAL_ISSUE + READY_HEADER, "ready_pool_header_unparseable"),
    (REAL_ISSUE, REAL_ISSUE + "```md\n" + READY_HEADER + "\n```", "ready_pool_header_unparseable"),
])
def test_label_writer_skips_and_reports_invalid_changes(tmp_path, old, new, reason):
    gh, val = shims(tmp_path, validator_ok=True, live_body=old)
    path = tmp_path / "candidate.md"
    path.write_bytes(new.encode("utf-8"))
    command = QUOTED_TEMPLATE.format(body=path) + " --add-label status:backlog"
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        out = run(wrapper, PARSER, val, gh, tmp_path, command)
        assert blocked(out) and reason in out["message"] and "SKIP #7925" in out["message"], out


@pytest.mark.parametrize("second_target", [" 7930", " -- 7930", " https://github.com/Scribie/heydonna-app/issues/7930"])
@pytest.mark.parametrize("label_flag", ["", " --add-label status:backlog"])
def test_body_edit_cannot_apply_verified_header_to_a_second_issue(tmp_path, second_target, label_flag):
    gh, _ = shims(tmp_path, validator_ok=True, live_body=REAL_ISSUE)
    candidate = tmp_path / "ready-pool.md"
    candidate.write_bytes((READY_HEADER + "\r\n\r\n" + REAL_ISSUE).encode("utf-8"))
    single = f"gh issue edit 7925 --repo Scribie/heydonna-app -F {candidate}{label_flag}"
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        assert not blocked(run(wrapper, PARSER, str(VALIDATOR), gh, tmp_path, single))
        out = run(wrapper, PARSER, str(VALIDATOR), gh, tmp_path, single + second_target)
        assert blocked(out) and "exactly one literal issue number" in out["message"], out


def test_routine_lane_cannot_replace_body_by_dropping_the_label_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_AGENT_NAME", "proxy-routine-synthetic")
    gh, val = shims(tmp_path, validator_ok=True, live_body=REAL_ISSUE)
    path = tmp_path / "stub.md"
    path.write_text(RULE_54_STUB, encoding="utf-8")
    command = f'gh issue edit 7925 --repo Scribie/heydonna-app -F {path}'
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        out = run(wrapper, PARSER, val, gh, tmp_path, command)
        assert blocked(out) and "ready_pool_edit_changes_existing_issue_text" in out["message"], out


def run_raw(wrapper: Path, env_extra: dict, payload: str):
    env = dict(os.environ, **env_extra)
    return subprocess.run(["bash", str(wrapper)], input=payload, env=env, capture_output=True, text=True, timeout=30)


def decision_of(done, label: str) -> dict:
    assert done.returncode == 0, f"{label}: wrapper exit {done.returncode}: {done.stderr[:200]}"
    assert done.stdout.strip(), f"{label}: wrapper must never be silent"
    return json.loads(done.stdout[done.stdout.index("{"):])


def literal_command(tmp_path: Path) -> str:
    return "gh issue edit 7925 --repo Scribie/heydonna-app --body-file " + body_file(tmp_path)


def test_crashing_parser_blocks_both_wrappers(tmp_path) -> None:
    gh, val = shims(tmp_path, validator_ok=True)
    broken = tmp_path / "broken_parser.py"
    broken.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": literal_command(tmp_path)}})
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        done = run_raw(wrapper, {"ISSUE_CONTRACT_LEDGER_HOOK_PARSER": str(broken),
                                 "ISSUE_CONTRACT_LEDGER_VALIDATOR": val,
                                 "ISSUE_CONTRACT_LEDGER_GH_BIN": gh}, payload)
        out = decision_of(done, f"{wrapper.name} crashing parser")
        assert out.get("decision") == "block", out
        assert "parser failed" in out.get("message", ""), out
        assert "no issue was mutated" in out.get("message", ""), out


def test_malformed_input_blocks_both_wrappers(tmp_path) -> None:
    """jq exits nonzero on invalid JSON, which is the jq-nonzero path.

    A PATH-only shim cannot simulate a missing jq because both wrappers
    prepend the standard bin dirs to PATH; malformed JSON exercises the same
    captured-status branch with the real jq.
    """
    gh, val = shims(tmp_path, validator_ok=True)
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        done = run_raw(wrapper, {"ISSUE_CONTRACT_LEDGER_HOOK_PARSER": str(PARSER),
                                 "ISSUE_CONTRACT_LEDGER_VALIDATOR": val,
                                 "ISSUE_CONTRACT_LEDGER_GH_BIN": gh}, "not-json")
        out = decision_of(done, f"{wrapper.name} malformed input")
        assert out.get("decision") == "block", out
        assert "could not be classified" in out.get("message", ""), out
        assert "no issue was mutated" in out.get("message", ""), out


def test_rc0_empty_parser_output_still_allows(tmp_path) -> None:
    gh, val = shims(tmp_path, validator_ok=True)
    quiet = tmp_path / "quiet_parser.py"
    quiet.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello"}})
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        done = run_raw(wrapper, {"ISSUE_CONTRACT_LEDGER_HOOK_PARSER": str(quiet),
                                 "ISSUE_CONTRACT_LEDGER_VALIDATOR": val,
                                 "ISSUE_CONTRACT_LEDGER_GH_BIN": gh}, payload)
        assert done.returncode == 0 and done.stdout.strip() == "", f"{wrapper.name}: genuine non-mutation must ALLOW"


@pytest.mark.parametrize("template", [
    "gh issue edit 7925 7930 --body-file {body}",                 # two targets
    "gh issue edit $N --body-file {body}",                        # variable target
    "gh issue edit 7925 $EXTRA --body-file {body}",               # $VAR may add a target or flag
    "gh issue edit 7925 --body-file {body}*",                     # glob
    'gh issue edit 7925 --body "$(cat {body})"',                  # command substitution
    "gh issue edit 7925 --body-file {body}; echo done",           # chained ;
    "gh issue edit 7925 --body-file {body} && echo done",         # chained &&
    "gh issue edit 7925 --body-file - <<'EOF'\nbody\nEOF",        # here-doc body
    "gh issue edit 7925 -F{body}",                                # attached short value
    "FOO=1 gh issue edit 7925 --body-file {body}",                # env indirection
    'bash -c "gh issue edit 7925 --body-file {body}"',            # interpreter indirection
    "gh api -X PATCH repos/Scribie/heydonna-app/issues/7925 -f body=x",  # raw API body write
    "gh issue edit 7925 $FLAGS",                                  # expansion may hide --body
])
def test_non_literal_issue_body_writes_are_refused(tmp_path, template):
    """CTO REVISE of acc2caaa (ts 1795753582.188919): bind the checked argv to gh's argv."""
    gh, val = shims(tmp_path, validator_ok=True, live_body=REAL_ISSUE)
    command = template.format(body=body_file(tmp_path))
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        out = run(wrapper, PARSER, val, gh, tmp_path, command)
        assert blocked(out) and "issue body write refused" in out["message"], out


@pytest.mark.parametrize("command", [
    "gh issue edit 7925 --add-label status:todo",
    "gh issue edit 7925 --add-label status:todo && gh issue view 7925",
    'gh issue edit 7925 --add-label "$LABEL"',
])
def test_label_only_edits_are_not_body_writes(tmp_path, command):
    gh, val = shims(tmp_path, validator_ok=True, live_body=REAL_ISSUE)
    for wrapper in (HOME_WRAPPER, APP_WRAPPER):
        out = run(wrapper, PARSER, val, gh, tmp_path, command)
        assert "issue body write refused" not in out.get("message", ""), out

@pytest.mark.parametrize("wrapper", [HOME_WRAPPER, APP_WRAPPER])
@pytest.mark.parametrize("variant,expected", [
    ("audit", True), ("audit_first", True), ("canonical", True), ("malformed", False), ("duplicate", False),
])
def test_label_transition_distinguishes_repair_audit(tmp_path, wrapper, variant, expected):
    audit = "<!-- ready-pool-repair:\r\nreason: prior audit\r\n-->\r\n"
    header = READY_HEADER + "\r\n"
    body = header + REAL_ISSUE
    if variant == "audit":
        body += audit
    elif variant == "audit_first":
        body = audit + body
    elif variant == "malformed":
        body += "<!-- ready-pool:\r\npriority: P1\r\n"
    elif variant == "duplicate":
        body += header
    gh, val = shims(tmp_path, validator_ok=True, live_body=body)
    result = run(wrapper, PARSER, val, gh, tmp_path,
                 "gh issue edit 14494 --repo Scribie/heydonna-app --add-label status:todo --remove-label status:done")
    writer = Mock()
    if not blocked(result):
        writer()
    assert (writer.call_count == 1) is expected, result
    if not expected:
        assert "ready_pool_header_unparseable" in result["message"]


if __name__ == "__main__":
    unittest.main()
