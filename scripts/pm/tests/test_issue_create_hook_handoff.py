"""Focused proof for the issue-create trap repair (CP repair 2026-09-20).

Governing defect (PM runtime, Rajiv directive 1789902323.668159): filing a valid
internal follow-up through the ledger hook's mandated --body-file flow was
double-blocked. Reproduction discriminator (see the module docstring table in
the candidate packet):

  (a) internal + existing LITERAL body file + exact marker  -> both hooks ALLOW
  (b) short marker <!-- internal-followup -->               -> audit blocks, ledger allows
  (c) customer-origin + marker                              -> audit blocks (permit required)
  (d) --body-file unreadable at hook time (missing file, or an unresolved
      shell variable)                                       -> BOTH hooks refused, with the
      audit hook reporting "requires prior Codex architecture review" - the wrong
      remedy - because BODY was empty and the marker lives inside the file.

The repair keeps every gate intent and gives the unreadable body-file hand-off
one typed refusal whose remedy satisfies both hooks.

Hermetic: temp state/permit/slack roots, /usr/bin/true as the pm-ops CLI, and a
dead MoP events URL so the Codex-evidence gate is deterministic. The live
ledger, GitHub, Slack and the installed hooks are never touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "hooks"
AUDIT_HOOK = Path(os.environ.get("AUDIT_HOOK_PATH", SHARED / "pre-issue-create-audit.sh"))
LEDGER_HOOK = SHARED / "block-invalid-issue-contract-ledger.sh"
REPO = "heydonna-app/heydonna-app"

VALID_BODY = """<!-- ready-pool:
priority: P2
lane: ci
ac_summary: Internal follow-up keeps the CI summary line truthful.
claimable_slot_type: dev
blockers: none
work_type: app-code
branch_slug: ci-summary-line
required_validation: Focused unit proof at the changed runtime control point.
owner: unassigned
next_action: Claim and implement the bounded CI summary fix.
wake_condition: A dev slot claims the issue or implementation returns a blocker.
disposition: PROMOTE
application_state: ready_for_dispatch
fingerprint: issue-trap-fixture
-->

## Problem

Internal engineering follow-up: the CI helper prints a stale summary line.

## Acceptance criteria

1. AC-1: the summary line matches the run outcome.
   - Surface: nonvisual
   - QA reachability: deterministic
   - Required proof: unit
   - QA scenario: Omit - nonvisual AC.
   - Screenshot exemption: Not applicable - nonvisual AC.

## Issue Contract Ledger

| Contract row | Requirement |
| --- | --- |
| Positive AC | The summary line matches the run outcome (AC-1). |
| Negative AC | No product behavior change. |
| Forbidden implementation | Editing generated artifacts. |
| Determinism | Deterministic string fix. |
| Required proof | Unit test at the summary control point (AC-1). |
"""


def _env(tmp_path: Path) -> dict[str, str]:
    return dict(
        os.environ,
        EXPLORE_ISSUE_STATE_DIR=str(tmp_path / "state"),
        EXPLORE_ISSUE_PERMIT_DIR=str(tmp_path / "permit"),
        EXPLORE_ISSUE_CONSUMED_PERMIT_DIR=str(tmp_path / "consumed"),
        EXPLORE_ISSUE_SLACK_CACHE_DIR=str(tmp_path / "slack"),
        EXPLORE_ISSUE_FALLBACK_LOG=str(tmp_path / "fallback.log"),
        EXPLORE_ISSUE_PM_OPS_CLI="/usr/bin/true",
        EXPLORE_ISSUE_MOP_EVENTS_URL="http://127.0.0.1:9/events",
    )


def _run(hook: Path, command: str, env: dict[str, str]) -> tuple[int, str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"})
    proc = subprocess.run(["bash", str(hook)], input=payload, capture_output=True, text=True, env=env, timeout=60)
    return proc.returncode, proc.stdout.strip()


def _decision(stdout: str) -> dict:
    assert stdout, "hook must always emit a decision when it refuses"
    return json.loads(stdout)


def _body(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _create_cmd(body_file: str, title: str = "internal follow-up: stale CI summary line") -> str:
    return f'gh issue create --repo {REPO} --title "{title}" --body-file {body_file} --label status:todo'


def test_internal_followup_with_literal_body_file_and_exact_marker_is_allowed_by_both_hooks(tmp_path):
    env = _env(tmp_path)
    body = _body(tmp_path, "body-ok.md", VALID_BODY + "\n<!-- audit-bypass: internal-followup -->\n")
    cmd = _create_cmd(str(body))
    assert _run(AUDIT_HOOK, cmd, env) == (0, "")
    assert _run(LEDGER_HOOK, cmd, env) == (0, "")


def test_short_marker_is_refused_by_the_audit_hook_only(tmp_path):
    env = _env(tmp_path)
    body = _body(tmp_path, "body-short.md", VALID_BODY + "\n<!-- internal-followup -->\n")
    cmd = _create_cmd(str(body))
    rc, out = _run(AUDIT_HOOK, cmd, env)
    assert rc == 0
    message = _decision(out)["message"]
    assert "requires prior Codex architecture review" in message
    assert "<!-- audit-bypass: internal-followup -->" in message
    assert _run(LEDGER_HOOK, cmd, env) == (0, "")


def test_customer_origin_with_marker_still_requires_the_explore_issue_permit(tmp_path):
    # The customer signal is STRUCTURED: the source is a channel that the
    # operator/CTO has named as a customer-facing channel. Body prose is not a
    # classification signal any more.
    env = dict(_env(tmp_path), EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX="^C0AGWPQFKHA$")
    body = _body(
        tmp_path,
        "body-customer.md",
        VALID_BODY + "\n<!-- audit-bypass: internal-followup -->\n\nSource: C0AGWPQFKHA\n",
    )
    cmd = _create_cmd(str(body), title="customer bug report")
    rc, out = _run(AUDIT_HOOK, cmd, env)
    assert rc == 0
    message = _decision(out)["message"]
    assert "Customer-origin" in message and "explore-issue" in message
    # The marker never releases a customer-origin create: whether the refusal
    # comes from the resolvable-thread branch ("audit-bypass marker is NOT
    # accepted for customer-origin") or the empty-thread branch, the caller is
    # told to run Skill(explore-issue) and mint the one-use permit.
    assert "permit" in message


def test_unreadable_body_file_gets_one_typed_remedy_from_the_audit_hook(tmp_path):
    env = _env(tmp_path)
    missing = tmp_path / "not-written-yet.md"
    rc, out = _run(AUDIT_HOOK, _create_cmd(str(missing)), env)
    assert rc == 0
    message = _decision(out)["message"]
    # The wrong-remedy Codex gate must NOT be what the caller is told.
    assert "requires prior Codex architecture review" not in message
    assert "cannot be read when this hook runs" in message
    assert "LITERAL path" in message and "do not create the body file inside the same Bash call" in message
    # The ledger hook refuses the same command for the same underlying reason.
    ledger_rc, ledger_out = _run(LEDGER_HOOK, _create_cmd(str(missing)), env)
    assert ledger_rc == 0
    assert "valid Issue Contract Ledger" in _decision(ledger_out)["message"]


def test_shell_variable_body_file_path_gets_the_same_typed_remedy(tmp_path):
    env = _env(tmp_path)
    body = _body(tmp_path, "body-var.md", VALID_BODY + "\n<!-- audit-bypass: internal-followup -->\n")
    cmd = f'BODY_FILE={body}; ' + _create_cmd('"$BODY_FILE"')
    rc, out = _run(AUDIT_HOOK, cmd, env)
    assert rc == 0
    message = _decision(out)["message"]
    assert "cannot be read when this hook runs" in message
    assert "unresolved shell variable" in message
    ledger_rc, ledger_out = _run(LEDGER_HOOK, cmd, env)
    assert ledger_rc == 0
    assert "valid Issue Contract Ledger" in _decision(ledger_out)["message"]


def test_existing_but_unreadable_body_file_gets_the_same_typed_remedy(tmp_path):
    """PM's real shape: the path exists but the hook cannot read it (mode 000).

    The ledger parser classifies any OSError from open() as
    body_file_unreadable_at_hook_time, so a missing path and an unreadable path
    are ONE classification and must produce ONE remedy from the audit hook.
    """
    env = _env(tmp_path)
    body = _body(tmp_path, "body-unreadable.md", VALID_BODY + "\n<!-- audit-bypass: internal-followup -->\n")
    body.chmod(0o000)
    try:
        cmd = _create_cmd(str(body))
        rc, out = _run(AUDIT_HOOK, cmd, env)
        assert rc == 0
        message = _decision(out)["message"]
        assert "requires prior Codex architecture review" not in message
        assert "cannot be read when this hook runs" in message
        assert "existing file the hook may not read" in message
        ledger_rc, ledger_out = _run(LEDGER_HOOK, cmd, env)
        assert ledger_rc == 0
        assert "valid Issue Contract Ledger" in _decision(ledger_out)["message"]
    finally:
        body.chmod(0o600)


def test_inline_marker_without_a_body_file_keeps_the_ledger_requirement(tmp_path):
    env = _env(tmp_path)
    cmd = (
        f'gh issue create --repo {REPO} --title "internal follow-up: stale CI summary line" '
        '--body "internal <!-- audit-bypass: internal-followup -->" --label status:todo'
    )
    assert _run(AUDIT_HOOK, cmd, env) == (0, "")
    rc, out = _run(LEDGER_HOOK, cmd, env)
    assert rc == 0
    assert "valid Issue Contract Ledger" in _decision(out)["message"]


def test_control_plane_draft_keeps_the_existing_control_plane_refusal(tmp_path):
    env = _env(tmp_path)
    body = _body(
        tmp_path,
        "cp-draft.md",
        VALID_BODY.replace("ci-summary-line", "cp-cli-parity").replace(
            "Internal engineering follow-up: the CI helper prints a stale summary line.",
            "Internal control-plane follow-up: the MoP release CLI prints a stale control-plane summary line.",
        ).replace("lane: ci", "lane: control-plane")
        + "\n<!-- audit-bypass: internal-followup -->\n",
    )
    cmd = _create_cmd(str(body), title="fix control-plane release CLI summary")
    rc, out = _run(LEDGER_HOOK, cmd, env)
    assert rc == 0
    message = _decision(out)["message"]
    assert "control-plane" in message.lower()


LEDGER_BOILERPLATE_BODY = VALID_BODY + """
## Customer / integration impact

- [x] N/A — this issue does not change Scribie-facing request fields.
- External-facing error code or response change: N/A
- Caller remediation steps: N/A
- Customer-notification owner, channel, and timing: N/A
- Reported by: PM triage (internal follow-up)

<!-- audit-bypass: internal-followup -->
"""


def test_internal_followup_with_ledger_boilerplate_is_not_customer_origin(tmp_path):
    """The reported false positive: our OWN ledger/template boilerplate used to
    satisfy the customer keyword scan, so an internal follow-up was classified
    customer-origin and demanded a READY_TO_FILE permit it never needed.
    Classification is structured now: template text can never classify."""
    env = _env(tmp_path)
    body = _body(tmp_path, "body-boilerplate.md", LEDGER_BOILERPLATE_BODY)
    cmd = _create_cmd(str(body), title="internal follow-up: ledger boilerplate must not classify")
    assert _run(AUDIT_HOOK, cmd, env) == (0, ""), "boilerplate must not make an internal follow-up customer-origin"
    assert _run(LEDGER_HOOK, cmd, env) == (0, "")


def test_customer_channel_source_still_classifies_after_the_boilerplate_fix(tmp_path):
    """The same guard, same body shape, but a REAL customer-source channel must
    still be refused (the real gate is not weakened)."""
    env = dict(_env(tmp_path), EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX="^C0AGWPQFKHA$")
    body = _body(
        tmp_path,
        "body-boilerplate-customer.md",
        LEDGER_BOILERPLATE_BODY + "\nSource: C0AGWPQFKHA:1785932534.656769\n",
    )
    cmd = _create_cmd(str(body), title="internal follow-up: ledger boilerplate must not classify")
    rc, out = _run(AUDIT_HOOK, cmd, env)
    assert rc == 0
    message = _decision(out)["message"]
    assert "Customer-origin" in message and "explore-issue" in message
    assert "customer source channel" in message


def test_non_internal_originator_still_classifies_without_any_prose(tmp_path):
    """Originator identity remains a real signal: a non-internal Slack user id
    in the cached thread still refuses even when the body says nothing."""
    env = _env(tmp_path)
    body = _body(tmp_path, "body-userid.md", VALID_BODY + "\n<!-- audit-bypass: internal-followup -->\n\nSource: 1785932534.656769\n")
    cache_dir = tmp_path / "slack"
    cache_dir.mkdir(exist_ok=True)
    # The hook looks up one cache file per (channel, ts); write the fixture the
    # same way the Slack API would have.
    import glob
    cmd = _create_cmd(str(body))
    # Warm the lookup once so the cache path used by the hook exists.
    _run(AUDIT_HOOK, cmd, env)
    for path in glob.glob(str(cache_dir / "*")):
        Path(path).write_text(json.dumps({"ok": True, "messages": [{"user": "U0CUSTOMER01"}]}), encoding="utf-8")
    rc, out = _run(AUDIT_HOOK, cmd, env)
    assert rc == 0
    message = _decision(out)["message"]
    assert "Customer-origin" in message
    assert "non-internal user-id U0CUSTOMER01" in message
