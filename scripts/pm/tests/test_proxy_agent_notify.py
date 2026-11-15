"""Focused proof for proxy-agent-notify.py and its proxy-agent.sh hooks (Rajiv DM
D0AMF0XE6TS thread 1794720837.453299, ts 1794720957.200479).

Covers canonical sources under scripts/pm/shared-assets/claude/scripts/:
  - launch line  "<role> started on #N (PR #M, head <sha8>, model <m>, run <name>)";
  - stop line    "<role> <DONE|HANDOFF|BLOCKED> on #N: <first 200 chars>" (+ plan link for an
                 hd-planner HANDOFF that names a committed plan);
  - thread lookup precedence is delegated to pm-ops (stubbed); the customer channel is never
    a target even if the lookup returns it;
  - FAIL-OPEN: every lookup/post failure exits 0 and never raises;
  - posts go out as the Alerts bot (SLACK_SEND_AS=alerts) with the body on stdin, never `-f path`;
  - proxy-agent.sh parses (`bash -n`) and carries both hooks for the four hd-* roles only.

Hermetic: slack-send, gh, pm-ops and proxy-observe are stubs wired through the helper's
PROXY_NOTIFY_* env overrides. No live Slack, GitHub or DB.
"""
from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
ASSETS = ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts"
NOTIFY = ASSETS / "proxy-agent-notify.py"
PROXY_AGENT = ASSETS / "proxy-agent.sh"

DEV = "C0ALZJHGE49"
CUSTOMER = "C0A56RX6FNW"
THREAD = "1794296562.075779"
HEAD40 = "0123456789abcdef0123456789abcdef01234567"
PLAN = "docs/plans/issue-13671-agent-lane-slack-updates.md"


def _exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Harness:
    """Stub toolchain in tmp_path; `run` invokes the canonical helper with it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.slack_out = tmp_path / "slack.out"
        self.pm_args = tmp_path / "pm-args.txt"
        self.slack = _exe(tmp_path / "slack-send.sh", (
            "#!/bin/sh\n"
            f'{{ echo "ARGS: $*"; echo "AS: ${{SLACK_SEND_AS:-}}"; echo "BODY:"; cat; }} >> "{self.slack_out}"\n'
            '[ -n "$SLACK_STUB_FAIL" ] && exit 1\n'
            '[ -n "$SLACK_STUB_NO_RECEIPT" ] && exit 0\n'
            'echo "OK ts=1794720999.000100 channel=$2"\n'
        ))
        self.pm_ops = tmp_path / "pm-ops-stub.py"
        self.pm_ops.write_text(
            "import os, sys\n"
            f'open(r"{self.pm_args}", "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
            'if os.environ.get("PM_STUB_RC", "0") != "0":\n'
            '    print("NONE"); sys.exit(int(os.environ["PM_STUB_RC"]))\n'
            'print(os.environ.get("PM_STUB_OUT", ""))\n'
        )
        self.gh = _exe(tmp_path / "gh-stub.sh", (
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *closingIssuesReferences*) echo \'{"closingIssuesReferences":[{"number":13671}]}\';;\n'
            f'  *headRefOid*) echo \'{{"headRefOid":"{HEAD40}"}}\';;\n'
            '  api*) echo "{\\"path\\":\\"${GH_STUB_API_PATH:-}\\"}";;\n'
            "  *) echo '{}';;\n"
            "esac\n"
        ))
        self.observe = tmp_path / "observe-stub.py"
        self.observe.write_text(
            "import os, sys\n"
            'head = os.environ.get("OBSERVE_STUB_HEAD", "")\n'
            "if not head: sys.exit(1)\n"
            'print("last_pushed_head=" + head)\n'
        )

    def brief(self, text: str) -> Path:
        path = self.tmp / "brief.md"
        path.write_text(text)
        return path

    def run(self, mode: str, *, role: str = "hd-builder", brief: Path | str,
            jsonl: str = "", out: str = "", model: str = "luna", name: str = "px-test-1",
            **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            PROXY_NOTIFY_SLACK_SEND=str(self.slack),
            PROXY_NOTIFY_PM_OPS=str(self.pm_ops),
            PROXY_NOTIFY_GH=str(self.gh),
            PROXY_NOTIFY_OBSERVE=str(self.observe),
            PM_STUB_OUT=f"{DEV}\t{THREAD}\tissue-body",
        )
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-I", str(NOTIFY), mode, "--type", role, "--brief", str(brief),
             "--name", name, "--model", model, "--out", out, "--jsonl", jsonl, "--rc", "0"],
            env=env, capture_output=True, text=True,
        )

    def posts(self) -> list[dict[str, str]]:
        if not self.slack_out.exists():
            return []
        posts = []
        for chunk in self.slack_out.read_text().split("ARGS: ")[1:]:
            args, rest = chunk.split("\nAS: ", 1)
            sender, body = rest.split("\nBODY:\n", 1)
            posts.append({"args": args, "as": sender, "body": body})
        return posts


def jsonl_result(path: Path, text: str) -> Path:
    import json

    path.write_text(json.dumps({"type": "result", "result": text}) + "\n")
    return path


def test_sources_present_and_bash_syntax_ok() -> None:
    assert NOTIFY.is_file() and PROXY_AGENT.is_file()
    assert os.access(NOTIFY, os.X_OK), "helper must keep mode 0755 (manifest mode 493)"
    assert subprocess.run(["bash", "-n", str(PROXY_AGENT)], capture_output=True).returncode == 0
    ast.parse(NOTIFY.read_text(), filename=str(NOTIFY))  # syntax check without writing a __pycache__


def test_proxy_agent_wires_both_hooks_for_the_four_hd_roles_only() -> None:
    text = PROXY_AGENT.read_text()
    assert text.count("proxy-agent-notify.py") == 2, "exactly one launch hook and one stop hook"
    assert " launch " in text and " stop " in text
    for role in ("hd-planner", "hd-builder", "hd-proof-qa", "hd-investigator"):
        assert role in text
    # Deployed mitigations must survive the hook insertion: the fail-closed Claude count and
    # no sol on the TOP ladder (Rajiv C0ALZJHGE49 ts 1794720589.429929), plus the stop relay.
    assert "proxy_agent_live_claude_count()" in text and "Fail CLOSED" in text
    assert 'TOP) models="claude-opus-5-5 grok-4.7 gpt-6.1-terra"' in text
    assert "proxy-agent-stop.sh" in text


def test_launch_message_with_pr_and_head(tmp_path) -> None:
    h = Harness(tmp_path)
    brief = h.brief("MODE: IMPLEMENT\nISSUE: #13671\nPR: #13700\nHEAD: abcdef1234567890abcdef1234567890abcdef12\n")
    done = h.run("launch", brief=brief)
    assert done.returncode == 0, done.stderr
    posts = h.posts()
    assert len(posts) == 1
    assert posts[0]["body"].strip() == "hd-builder started on #13671 (PR #13700, head abcdef12, model luna, run px-test-1)"
    assert posts[0]["as"] == "alerts", "must post as the Alerts bot, never the PM token"
    assert posts[0]["args"].strip() == f"-c {DEV} -t {THREAD} -f", "body goes on stdin; never -f <path>"


def test_launch_message_without_pr_or_head_omits_them(tmp_path) -> None:
    h = Harness(tmp_path)
    done = h.run("launch", role="hd-planner", brief=h.brief("ISSUE: 13671\n"), model="")
    assert done.returncode == 0, done.stderr
    assert h.posts()[0]["body"].strip() == "hd-planner started on #13671 (model auto, run px-test-1)"


def test_launch_head_falls_back_to_gh_pr_head(tmp_path) -> None:
    h = Harness(tmp_path)
    done = h.run("launch", role="hd-proof-qa", brief=h.brief("PR: 13700\n"))
    assert done.returncode == 0, done.stderr
    body = h.posts()[0]["body"].strip()
    # PR-only brief: issue resolved through closingIssuesReferences, head through headRefOid.
    assert body == f"hd-proof-qa started on #13671 (PR #13700, head {HEAD40[:8]}, model luna, run px-test-1)"


def test_pm_ops_lookup_receives_issue_and_pr(tmp_path) -> None:
    h = Harness(tmp_path)
    h.run("launch", brief=h.brief("ISSUE: 13671\nPR: 13700\n"))
    args = h.pm_args.read_text().strip()
    assert args == "issue-thread get --issue 13671 --format tsv --pr 13700"


def test_no_thread_means_no_post_and_exit_zero(tmp_path) -> None:
    h = Harness(tmp_path)
    done = h.run("launch", brief=h.brief("ISSUE: 13671\n"), PM_STUB_RC="1")
    assert done.returncode == 0
    assert h.posts() == []
    assert "no_thread" in done.stderr


def test_customer_channel_is_never_a_target(tmp_path) -> None:
    h = Harness(tmp_path)
    done = h.run("launch", brief=h.brief("ISSUE: 13671\n"), PM_STUB_OUT=f"{CUSTOMER}\t{THREAD}\tissue-body")
    assert done.returncode == 0
    assert h.posts() == [], "C0A56RX6FNW must never receive a post"
    assert "thread_refused" in done.stderr


def test_malformed_lookup_output_is_refused(tmp_path) -> None:
    h = Harness(tmp_path)
    for bad in ("", "NONE", f"{DEV}\tnot-a-ts\tx", f"bad-channel\t{THREAD}\tx"):
        done = h.run("launch", brief=h.brief("ISSUE: 13671\n"), PM_STUB_OUT=bad)
        assert done.returncode == 0
    assert h.posts() == []


def test_non_hd_roles_and_briefs_without_refs_are_ignored(tmp_path) -> None:
    h = Harness(tmp_path)
    assert h.run("launch", role="resume", brief=h.brief("ISSUE: 13671\n")).returncode == 0
    assert h.run("launch", role="pm-hotfix", brief=h.brief("ISSUE: 13671\n")).returncode == 0
    assert h.run("launch", brief=h.brief("MODE: heartbeat\nno references\n")).returncode == 0
    assert h.posts() == []


def test_fail_open_on_every_failure_mode(tmp_path) -> None:
    h = Harness(tmp_path)
    brief = h.brief("ISSUE: 13671\n")
    cases = [
        h.run("launch", brief=tmp_path / "missing-brief.md"),
        h.run("launch", brief=brief, SLACK_STUB_FAIL="1"),
        h.run("launch", brief=brief, SLACK_STUB_NO_RECEIPT="1"),
        h.run("launch", brief=brief, PROXY_NOTIFY_SLACK_SEND=str(tmp_path / "does-not-exist")),
        h.run("launch", brief=brief, PROXY_NOTIFY_PM_OPS=str(tmp_path / "does-not-exist.py")),
        h.run("stop", brief=brief, jsonl=str(tmp_path / "none.jsonl"), out=str(tmp_path / "none.out")),
        h.run("launch", brief=brief, PROXY_NOTIFY_BUDGET_S="0"),
    ]
    for done in cases:
        assert done.returncode == 0, f"helper must always exit 0: {done.stderr}"
        assert "Traceback" not in done.stderr


def test_unconfirmed_delivery_is_logged_not_raised(tmp_path) -> None:
    h = Harness(tmp_path)
    done = h.run("launch", brief=h.brief("ISSUE: 13671\n"), SLACK_STUB_NO_RECEIPT="1")
    assert done.returncode == 0
    assert "delivery=unknown" in done.stderr


def test_stop_message_strips_marker_and_posts_status(tmp_path) -> None:
    h = Harness(tmp_path)
    jsonl = jsonl_result(tmp_path / "run.jsonl", "working...\nDONE: implemented and pushed abc1234; 3 files changed\n")
    done = h.run("stop", role="hd-builder", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl))
    assert done.returncode == 0, done.stderr
    assert h.posts()[0]["body"].strip() == "hd-builder DONE on #13671: implemented and pushed abc1234; 3 files changed"


def test_stop_message_uses_the_last_marker_and_all_three_statuses(tmp_path) -> None:
    h = Harness(tmp_path)
    for status in ("DONE", "BLOCKED", "HANDOFF"):
        (tmp_path / "slack.out").unlink(missing_ok=True)
        jsonl = jsonl_result(tmp_path / "run.jsonl", f"DONE: early draft\n{status}: final answer\n")
        h.run("stop", role="hd-investigator", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl))
        assert h.posts()[0]["body"].strip() == f"hd-investigator {status} on #13671: final answer"


def test_stop_message_clips_to_200_chars(tmp_path) -> None:
    h = Harness(tmp_path)
    jsonl = jsonl_result(tmp_path / "run.jsonl", "HANDOFF: " + "x" * 500)
    h.run("stop", role="hd-builder", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl))
    body = h.posts()[0]["body"].strip()
    assert body == "hd-builder HANDOFF on #13671: " + "x" * 200 + "…"


def test_stop_message_redacts_secrets(tmp_path) -> None:
    h = Harness(tmp_path)
    secret = "supersecretvalue123456"
    jsonl = jsonl_result(
        tmp_path / "run.jsonl",
        f"BLOCKED: token={secret} and Bearer abc.def.ghi and ghp_AAAAAAAAAAAAAAAA and env {secret}\n",
    )
    h.run("stop", role="hd-builder", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl),
          SOME_API_TOKEN=secret)
    body = h.posts()[0]["body"]
    assert secret not in body
    assert "abc.def.ghi" not in body and "ghp_AAAA" not in body
    assert "[REDACTED]" in body


def test_stop_message_is_slack_safe(tmp_path) -> None:
    h = Harness(tmp_path)
    jsonl = jsonl_result(tmp_path / "run.jsonl", "DONE: ran `rm -rf` <@U123> & more\n")
    h.run("stop", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl))
    body = h.posts()[0]["body"]
    assert "`" not in body, "no backticks in the posted text"
    assert "<@U123>" not in body, "no live mentions from agent text"
    assert "&lt;@U123&gt; &amp; more" in body


def test_stop_falls_back_to_out_file_and_skips_without_marker(tmp_path) -> None:
    h = Harness(tmp_path)
    out = tmp_path / "run.out"
    out.write_text("noise\nDONE: from the rendered out file\n")
    h.run("stop", brief=h.brief("ISSUE: 13671\n"), jsonl=str(tmp_path / "absent.jsonl"), out=str(out))
    assert h.posts()[0]["body"].strip() == "hd-builder DONE on #13671: from the rendered out file"
    (tmp_path / "slack.out").unlink()
    out.write_text("finished with no terminal marker at all\n")
    done = h.run("stop", brief=h.brief("ISSUE: 13671\n"), jsonl=str(tmp_path / "absent.jsonl"), out=str(out))
    assert done.returncode == 0 and h.posts() == []
    assert "no_terminal_marker" in done.stderr


def test_planner_handoff_with_plan_includes_path_and_verified_blob_link(tmp_path) -> None:
    h = Harness(tmp_path)
    jsonl = jsonl_result(tmp_path / "run.jsonl", f"HANDOFF: plan committed at {PLAN} head {HEAD40}\n")
    done = h.run("stop", role="hd-planner", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl),
                 GH_STUB_API_PATH=PLAN)
    assert done.returncode == 0, done.stderr
    body = h.posts()[0]["body"].strip()
    assert body.startswith("hd-planner HANDOFF on #13671: plan committed at ")
    assert f" Plan: {PLAN}" in body
    assert f"<https://github.com/Scribie/heydonna-app/blob/{HEAD40}/{PLAN}|plan at {HEAD40[:8]}>" in body


def test_planner_handoff_uses_observed_pushed_head_over_text(tmp_path) -> None:
    h = Harness(tmp_path)
    other = "f" * 40
    jsonl = jsonl_result(tmp_path / "run.jsonl", f"HANDOFF: plan {PLAN} (earlier head {other})\n")
    h.run("stop", role="hd-planner", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl),
          GH_STUB_API_PATH=PLAN, OBSERVE_STUB_HEAD=HEAD40)
    assert f"/blob/{HEAD40}/{PLAN}|plan at {HEAD40[:8]}>" in h.posts()[0]["body"]


def test_planner_handoff_unverified_plan_gets_path_but_no_link(tmp_path) -> None:
    h = Harness(tmp_path)
    jsonl = jsonl_result(tmp_path / "run.jsonl", f"HANDOFF: plan {PLAN} head {HEAD40}\n")
    h.run("stop", role="hd-planner", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl),
          GH_STUB_API_PATH="docs/plans/some-other-file.md")
    body = h.posts()[0]["body"]
    assert f"Plan: {PLAN}" in body
    assert "blob" not in body, "never claim a blob link the plan is not at"


def test_plan_link_is_planner_handoff_only(tmp_path) -> None:
    h = Harness(tmp_path)
    jsonl = jsonl_result(tmp_path / "run.jsonl", f"DONE: wrote {PLAN} head {HEAD40}\n")
    h.run("stop", role="hd-planner", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl), GH_STUB_API_PATH=PLAN)
    h.run("stop", role="hd-builder", brief=h.brief("ISSUE: 13671\n"), jsonl=str(jsonl), GH_STUB_API_PATH=PLAN)
    for post in h.posts():
        assert "Plan:" not in post["body"] and "blob" not in post["body"]
