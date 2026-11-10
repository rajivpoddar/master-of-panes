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

import hashlib
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
REPO = "Scribie/heydonna-app"

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


# The ledger hook preserves a blocked control-plane draft under
# $HOME/.claude/control-plane-artifacts/blocked-issues and upserts a REAL
# pm-ops obligation unless overridden. Unisolated runs left 383 drafts and live
# obligations (e.g. 31134, PM 2026-11-07). Every hook run below is pinned to
# tmp_path, and any write to the live directory fails the test.
LIVE_CP_ARTIFACTS = Path.home() / ".claude" / "control-plane-artifacts" / "blocked-issues"


def _live_artifacts() -> list[str]:
    return sorted(p.name for p in LIVE_CP_ARTIFACTS.iterdir()) if LIVE_CP_ARTIFACTS.is_dir() else []


@pytest.fixture(autouse=True)
def _never_touch_live_control_plane_artifacts():
    before = _live_artifacts()
    yield
    assert _live_artifacts() == before, f"test wrote into the LIVE {LIVE_CP_ARTIFACTS}"


def _env(tmp_path: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    curl = fake_bin / "curl"
    curl.write_text("#!/bin/sh\nprintf '%s\\n' '{\"ok\":false,\"events\":[]}'\n")
    curl.chmod(0o755)
    pm_ops = tmp_path / "pm-ops-recorder.py"
    pm_ops.write_text(
        "import sys\n"
        f"open({str(tmp_path / 'pm-ops-calls.log')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "print(4242)\n"
    )
    pm_ops.chmod(0o755)
    return dict(
        os.environ,
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        BLOCKED_CP_ISSUE_ARTIFACTS=str(tmp_path / "cp-artifacts"),
        PM_OPS=str(pm_ops),
        EXPLORE_ISSUE_LATCH_GUARD=str(SHARED / "explore-issue-latch-guard.sh"),
        EXPLORE_ISSUE_ENV_LOCAL=str(tmp_path / "no-credentials.env"),
        SLACK_BOT_TOKEN="",
        EXPLORE_ISSUE_STATE_DIR=str(tmp_path / "state"),
        EXPLORE_ISSUE_PERMIT_DIR=str(tmp_path / "permit"),
        EXPLORE_ISSUE_CONSUMED_PERMIT_DIR=str(tmp_path / "consumed"),
        EXPLORE_ISSUE_SLACK_CACHE_DIR=str(tmp_path / "slack"),
        EXPLORE_ISSUE_FALLBACK_LOG=str(tmp_path / "fallback.log"),
        EXPLORE_ISSUE_PM_OPS_CLI="/usr/bin/true",
        EXPLORE_ISSUE_MOP_EVENTS_URL="http://127.0.0.1:9/events",
    )


def _run(hook: Path, command: str, env: dict[str, str], *, unprivileged: bool = False) -> tuple[int, str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp"})
    options = {"user": 65534} if unprivileged and os.geteuid() == 0 else {}
    proc = subprocess.run(["bash", str(hook)], input=payload, capture_output=True, text=True, env=env, timeout=60, **options)
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
    assert "Source identity" in message
    assert "No permit was consumed" in message


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
    if os.geteuid() == 0:
        # Root bypasses mode000. Exercise real permission denial as nobody;
        # make only these disposable parent directories traversable.
        for directory in [tmp_path, *tmp_path.parents]:
            if directory == Path("/tmp"):
                break
            directory.chmod(directory.stat().st_mode | 0o111)
    body.chmod(0o000)
    try:
        cmd = _create_cmd(str(body))
        rc, out = _run(AUDIT_HOOK, cmd, env, unprivileged=True)
        assert rc == 0
        message = _decision(out)["message"]
        assert "requires prior Codex architecture review" not in message
        assert "cannot be read when this hook runs" in message
        assert "existing file the hook may not read" in message
        ledger_rc, ledger_out = _run(LEDGER_HOOK, cmd, env, unprivileged=True)
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
    # The preserved draft and the obligation upsert stay inside tmp_path.
    preserved = list((tmp_path / "cp-artifacts").glob("*-fix-control-plane-release-CLI-summary.md"))
    assert len(preserved) == 1 and str(preserved[0]) in message
    assert "obligation-upsert" in (tmp_path / "pm-ops-calls.log").read_text()


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


@pytest.mark.parametrize("source_root,allowed", [("1794207884.242379", True), ("1794207885.242379", False)])
def test_reply_timestamp_uses_root_permit_not_unrelated_root(tmp_path, source_root, allowed):
    env = dict(_env(tmp_path), EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX="^C0AGWPQFKHA$")
    title = "customer bug report"
    reply = "1794208507.857479"
    text = (VALID_BODY + f"\nReporter reply: {reply}\n"
            + f"Source: https://scribiers.slack.com/archives/C0AGWPQFKHA/p{reply.replace('.', '')}?thread_ts={source_root}&cid=C0AGWPQFKHA\n")
    cache = tmp_path / "slack"
    cache.mkdir()
    (cache / f"{source_root}.json").write_text(json.dumps({"ok": True, "channel": "C0AGWPQFKHA", "messages": [
        {"ts": source_root}, {"ts": reply, "thread_ts": source_root},
    ]}))
    body = _body(tmp_path, "reply.md", text)
    permit_dir = tmp_path / "permit"
    permit_dir.mkdir()
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    (permit_dir / "C0AGWPQFKHA__1794207884_242379.permit.json").write_text(json.dumps({
        "channel": "C0AGWPQFKHA", "thread_ts": "1794207884.242379",
        "body_sha256": digest(text.rstrip("\n")), "title_digest": digest(title),
    }))
    rc, out = _run(AUDIT_HOOK, _create_cmd(str(body), title=title), env)
    assert rc == 0
    if allowed:
        assert out == ""
        assert not list(permit_dir.glob("*.json"))
    else:
        assert "Customer-origin" in _decision(out)["message"]
        assert list(permit_dir.glob("*.json"))


def test_explicit_source_wins_over_unrelated_quoted_permalink(tmp_path):
    env = dict(_env(tmp_path), EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX="^C0AGWPQFKHA$")
    title = "customer bug report"
    text = (VALID_BODY + "\nSource: C0AGWPQFKHA:1794207884.242379\n"
            + "> unrelated: https://scribiers.slack.com/archives/C0AGWPQFKHA/p1794208507857479?thread_ts=1794207885.242379\n")
    body = _body(tmp_path, "source.md", text)
    permits = tmp_path / "permit"
    permits.mkdir()
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    (permits / "C0AGWPQFKHA__1794207884_242379.permit.json").write_text(json.dumps({
        "channel": "C0AGWPQFKHA", "thread_ts": "1794207884.242379",
        "body_sha256": digest(text.rstrip("\n")), "title_digest": digest(title),
    }))
    assert _run(AUDIT_HOOK, _create_cmd(str(body), title=title), env) == (0, "")
    assert not list(permits.glob("*.json"))
    assert not (tmp_path / "pm-ops-calls.log").exists()


@pytest.mark.parametrize("source", ["Source: C0AGWPQFKHA", "Source: C0AGWPQFKHA:1794207884.242379\nSource: C0AGWPQFKHA:1794207885.242379", "Source: 1794207884.242379"])
def test_unresolved_source_neither_consumes_permit_nor_upserts(tmp_path, source):
    env = dict(_env(tmp_path), EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX="^C0AGWPQFKHA$", EXPLORE_ISSUE_PM_OPS_CLI=str(tmp_path / "pm-ops-recorder.py"))
    body = _body(tmp_path, "ambiguous.md", VALID_BODY + "\n" + source + "\n")
    permits = tmp_path / "permit"
    permits.mkdir()
    permit = permits / "C0AGWPQFKHA__1794207884_242379.permit.json"
    permit.write_text("{}")
    rc, out = _run(AUDIT_HOOK, _create_cmd(str(body)), env)
    assert rc == 0
    assert "Source identity" in _decision(out)["message"]
    assert permit.exists()
    assert not (tmp_path / "pm-ops-calls.log").exists()
    assert not (tmp_path / "state").exists()


def test_non_internal_originator_still_classifies_without_any_prose(tmp_path):
    """Originator identity remains a real signal: a non-internal Slack user id
    in the cached thread still refuses even when the body says nothing."""
    env = _env(tmp_path)
    body = _body(tmp_path, "body-userid.md", VALID_BODY + "\n<!-- audit-bypass: internal-followup -->\n\nSource: C0AGWPQFKHA:1785932534.656769\n")
    cache_dir = tmp_path / "slack"
    cache_dir.mkdir(exist_ok=True)
    # The hook looks up one cache file per (channel, ts); write the fixture the
    # same way the Slack API would have.
    cmd = _create_cmd(str(body))
    # Seed the exact cache key; a hermetic run has no Slack transport/token
    # whose response would otherwise create this file as a side effect.
    (cache_dir / "1785932534.656769.json").write_text(
        json.dumps({"ok": True, "messages": [{"user": "U0CUSTOMER01"}]}), encoding="utf-8"
    )
    rc, out = _run(AUDIT_HOOK, cmd, env)
    assert rc == 0
    message = _decision(out)["message"]
    assert "Customer-origin" in message
    assert "non-internal user-id U0CUSTOMER01" in message


@pytest.mark.parametrize(
    "originator",
    [
        {"user": "U0BNFGX2UAX"},
        {"bot_id": "B0BNMPL5EN6"},
    ],
    ids=["cto-user", "cto-bot"],
)
def test_cto_source_user_and_bot_ids_classify_as_internal(tmp_path, originator):
    env = dict(_env(tmp_path), EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX="^C0ALZJHGE49$")
    source_ts = "1785932534.656769"
    body = _body(
        tmp_path,
        "body-cto.md",
        VALID_BODY + f"\n<!-- audit-bypass: internal-followup -->\n\nSource: C0ALZJHGE49:{source_ts}\n",
    )
    cache_dir = tmp_path / "slack"
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / f"{source_ts}.json").write_text(
        json.dumps({"ok": True, "messages": [originator]}), encoding="utf-8"
    )

    rc, out = _run(AUDIT_HOOK, _create_cmd(str(body)), env)

    assert (rc, out) == (0, "")


@pytest.mark.parametrize("source,allowed", [
    ("", False),
    ("Origin: internal", False),
    ("Source: none (internal) — maintenance follow-up", False),
    ("Source: none (internal)", False),
    ("Source: C0ALZJHGE49:1794257214.316859\nSource: docs/plans/foo.md", False),
])
def test_high_priority_bypass_requires_single_explicit_source(tmp_path, source, allowed):
    env = _env(tmp_path)
    body = _body(tmp_path, "priority.md", VALID_BODY + "\n<!-- audit-bypass: internal-followup -->\n" + source)
    rc, out = _run(AUDIT_HOOK, _create_cmd(str(body)) + " --label priority:P1", env)
    assert rc == 0
    assert (out == "") == allowed
    if "docs/plans" in source:
        # Live hook: more than one Source line is INVALID, not a separate MULTIPLE code.
        assert "ambiguous" in _decision(out)["message"]


def test_alerts_root_is_internal_even_with_pm_reply(tmp_path):
    env = _env(tmp_path)
    root = "1794257214.316859"
    cache = tmp_path / "slack"
    cache.mkdir()
    (cache / f"{root}.json").write_text(json.dumps({"ok": True, "messages": [
        {"ts": root, "user": "U0AJZTN7SM6", "bot_id": "B0AJ3HSC2PQ"},
        {"ts": "1794258103.810189", "thread_ts": root, "user": "U0ALEAYCAUT"},
    ]}))
    body = _body(tmp_path, "slack-root.md", VALID_BODY + f"\nSource: C0ALZJHGE49:{root}\n<!-- audit-bypass: internal-followup -->")
    assert _run(AUDIT_HOOK, _create_cmd(str(body)) + " --label priority:P1", env) == (0, "")


@pytest.mark.parametrize("gh_rc,allowed", [(0, True), (1, False)])
def test_github_comment_requires_the_comment_to_read_back(tmp_path, gh_rc, allowed):
    # Live hook: a single heydonna-app comment URL is a source after `gh api` reads the id.
    # Author and issue matching are no longer done in the hook.
    env = _env(tmp_path)
    gh = tmp_path / "bin" / "gh"
    gh.write_text(f"#!/bin/sh\nexit {gh_rc}\n")
    gh.chmod(0o755)
    body = _body(tmp_path, "comment.md", VALID_BODY + "\nSource: https://github.com/Scribie/heydonna-app/issues/12000#issuecomment-123\n<!-- audit-bypass: internal-followup -->")
    rc, out = _run(AUDIT_HOOK, _create_cmd(str(body)) + " --label priority:P1", env)
    assert rc == 0
    assert (out == "") == allowed
    if not allowed:
        assert "GitHub comment" in _decision(out)["message"]
