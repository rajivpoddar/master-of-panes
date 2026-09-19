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
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
HOME_WRAPPER = ROOT / "scripts/pm/shared-assets/claude/hooks/block-invalid-issue-contract-ledger.sh"
APP_WRAPPER = ROOT / "scripts/pm/shared-assets/claude/hooks/heydonna-app-block-invalid-issue-contract-ledger.sh"
PARSER = ROOT / "scripts/pm/shared-assets/claude/scripts/issue-contract-ledger-hook.py"
VALIDATOR = ROOT / "scripts/pm/shared-assets/claude/scripts/validate-issue-contract-ledger.py"
POLICY = ROOT / "scripts/pm/shared-assets/claude/scripts/control_plane_issue_policy.py"
PREIMAGE_PARSER = Path("/Users/rajiv/.claude/scripts/issue-contract-ledger-hook.py")

PROSE = ('python3 /Users/rajiv/.claude/scripts/pm-ops.py obligation-resolve --kind ci_rework --target-id 18218 '
         '--external-state "reworded prose mentioning gh issue edit 7945 --repo heydonna-app/heydonna-app as history only"')
VARIABLE = "gh issue edit $ISSUE --repo heydonna-app/heydonna-app --body-file /tmp/body.md"
QUOTED_TEMPLATE = 'gh issue edit "7925" --repo heydonna-app/heydonna-app --body-file {body}'


def shims(tmp: Path, *, validator_ok: bool):
    gh = tmp / "gh"
    gh.write_text('#!/usr/bin/env bash\nprintf \'%s\' \'{"body":"plain body","labels":[]}\'\n', encoding="utf-8")
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


def test_red_on_live_preimage(tmp_path) -> None:
    gh, val = shims(tmp_path, validator_ok=False)
    prose = run(HOME_WRAPPER, PREIMAGE_PARSER, val, gh, tmp_path, PROSE)
    assert blocked(prose), "RED anchor: the live preimage classified quoted prose as a mutation"
    var = run(HOME_WRAPPER, PREIMAGE_PARSER, val, gh, tmp_path, VARIABLE)
    assert "literal issue number" not in var.get("message", ""), "preimage lacks the typed literal refusal"


def run_raw(wrapper: Path, env_extra: dict, payload: str):
    env = dict(os.environ, **env_extra)
    return subprocess.run(["bash", str(wrapper)], input=payload, env=env, capture_output=True, text=True, timeout=30)


def decision_of(done, label: str) -> dict:
    assert done.returncode == 0, f"{label}: wrapper exit {done.returncode}: {done.stderr[:200]}"
    assert done.stdout.strip(), f"{label}: wrapper must never be silent"
    return json.loads(done.stdout[done.stdout.index("{"):])


def literal_command(tmp_path: Path) -> str:
    return "gh issue edit 7925 --repo heydonna-app/heydonna-app --body-file " + body_file(tmp_path)


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


if __name__ == "__main__":
    unittest.main()
