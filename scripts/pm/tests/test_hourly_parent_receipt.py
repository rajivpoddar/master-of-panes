"""Retained, offline fixtures for hourly parent receipt custody."""
import os
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "shared-assets" / "claude"
SKILL = ASSETS / "skills/hourly-heartbeat/SKILL.md"
SENDER = ASSETS / "skills/slack-message/scripts/slack-send.sh"
PARENT = "OK ts=1794321992.050059 channel=C0ALZJHGE49"
CHILD = "OK ts=1794321993.050059 channel=C0ALZJHGE49"


@pytest.mark.parametrize("case", ["parent-only", "children", "child-failure", "ambiguous"])
def test_parent_custody_with_retained_sender_fixtures(tmp_path, case):
    # Execute the actual documented claim/registration shell, substituting only
    # the sender, input, bucket and registration paths; no Slack access.
    text = SKILL.read_text()
    block = text.split('  ```bash\n', 1)[1].split('  ```', 1)[0]
    sender = tmp_path / "sender"
    sender.write_text("#!/bin/bash\n" + {
        "parent-only": f"printf '%s\\n' '{PARENT}'\n",
        "children": f"printf '%s\\n' '{PARENT}'\nprintf '%s\\n' '{CHILD}' >&2\n",
        "child-failure": f"printf '%s\\n' '{PARENT}'\nprintf '%s\\n' '{CHILD}' >&2\nexit 7\n",
        "ambiguous": "exit 1\n",
    }[case])
    sender.chmod(0o755)
    registrar = tmp_path / "register"
    registrar.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$HOME/registered"\n')
    registrar.chmod(0o755)
    bucket = tmp_path / "bucket"
    bucket.write_text("2026-11-10:16\n")
    body = tmp_path / "body"
    body.write_text("fixture")
    block = (block.replace('/tmp/heartbeat-1h-bucket', str(bucket))
             .replace('/tmp/heartbeat-1h-post.txt', str(body))
             .replace('/Users/rajiv/.claude/scripts/slack-send.sh', str(sender))
             .replace('/Users/rajiv/.claude/scripts/hourly-thread-ts', str(registrar)))
    env = dict(os.environ, HOME=str(tmp_path))
    result = subprocess.run(["bash", "-c", block], env=env, capture_output=True, text=True)
    claim = tmp_path / '.claude/hourly-heartbeat-posts/2026-11-10:16'
    assert claim.is_dir()
    if case == "ambiguous":
        assert result.returncode != 0 and not (tmp_path / 'registered').exists()
        assert 'HOURLY_POST_UNCERTAIN' in result.stderr
    else:
        assert result.returncode == 0
        assert (claim / 'receipt').read_text().strip() == PARENT
        assert (tmp_path / 'registered').read_text().splitlines() == [
            '--set', '1794321992.050059', '--bucket', '2026-11-10:16', '--receipt', PARENT]
        if case in ('children', 'child-failure'):
            assert CHILD in (claim / 'errors').read_text()
    # A consumed or uncertain bucket is never re-sent.
    again = subprocess.run(["bash", "-c", block], env=env, capture_output=True, text=True)
    assert again.returncode != 0 and 'HOURLY_POST_UNCERTAIN' in again.stderr


def test_sender_keeps_all_three_child_hooks_and_separates_receipts():
    text = SENDER.read_text()
    assert "('pr-status-report.py', 'tracked-features-report.py', 'issue-lifecycle-report.py')" in text
    assert "result = subprocess.run([sys.executable, report, '--channel', channel, '--thread', ts], stdout=sys.stderr)" in text
    assert 'HOURLY_REPLY_FAILED parent_ts={ts}; retry {name} --thread {ts}' in text
