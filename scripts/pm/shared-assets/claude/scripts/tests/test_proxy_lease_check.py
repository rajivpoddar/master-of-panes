"""Lane lease check (Rajiv D0AMF0XE6TS thread 1795436469.682079, ts 1795438159.967189): proxy-lease-check.py lists live lanes with no OBJECTIVE
progress (pushed non-merge commit, lane PR comment, GitHub-confirmed receipt) for more than 60 minutes as STALLED, with a promotion packet, and kills nothing.
Hermetic: fake ps text, fake gh (fixture by API path), real git against local bare remotes, recording pm-ops stub.
Run: /opt/homebrew/bin/pytest -q -p no:cacheprovider tests/test_proxy_lease_check.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).parents[1] / 'proxy-lease-check.py'
REPO = 'heydonna-app/heydonna-app'
NAME = 'proxy-hd-builder-14461t'
NOW = '2026-11-23T12:00:00Z'
GH = '''#!/usr/bin/env python3
import json, os, sys
data = json.load(open(os.environ['FAKE_GH']))
path = sys.argv[2]
val = data.get(path)
if val == 'ERROR':
    print('gh: HTTP 502', file=sys.stderr)
    sys.exit(1)
for row in (val or []):
    print(json.dumps(row))
'''
STUB = '''import json, os, sys
open(os.environ['STUB_LOG'], 'a').write(json.dumps(sys.argv[1:]) + "\\n")
'''


class Lab:
    def __init__(self, tmp):
        self.tmp = tmp
        (tmp / 'bin').mkdir()
        (tmp / 'bin' / 'gh').write_text(GH)
        (tmp / 'bin' / 'gh').chmod(0o755)
        (tmp / 'stub.py').write_text(STUB)
        self.gh = {}
        self.ps = []
        self.runs = tmp / 'runs'
        self.runs.mkdir()
        self.env = {**os.environ, 'PROXY_LEASE_GH': str(tmp / 'bin' / 'gh'), 'FAKE_GH': str(tmp / 'gh.json'), 'PROXY_LEASE_PS': str(tmp / 'ps.txt'),
                    'PROXY_AGENT_RUN_STATE_DIR': str(self.runs), 'PROXY_AGENT_PM_OPS_CLI': str(tmp / 'stub.py'), 'STUB_LOG': str(tmp / 'calls.jsonl')}

    def lane(self, name=NAME, started='2026-11-23T10:20:00Z', pr='14470', issue='14461', type='hd-builder', out=None, **extra):
        self.ps.append(f'  {4000 + len(self.ps)} 1 /opt/homebrew/bin/claude -p --name {name} --model claude-sonnet-5-5 --max-turns 200')
        (self.runs / name).mkdir(exist_ok=True)
        mf = {'name': name, 'type': type, 'role': type, 'started_at': started, 'lease_started_at': started, 'model': 'claude-sonnet-5-5',
              'out': str(out or self.tmp / f'{name}.md'), **extra}
        if pr:
            mf['pr'] = pr
        if issue:
            mf['issue'] = issue
        (self.runs / name / 'manifest.json').write_text(json.dumps(mf))

    def pr(self, commits=(), comments=(), number='14470', branch='fix/14461-x', head='a' * 40, issue_comments=()):
        base = f'repos/{REPO}'
        self.gh[f'{base}/pulls/{number}'] = [{'head': head, 'branch': branch}]
        self.gh[f'{base}/pulls/{number}/commits'] = [{'sha': s, 'at': at, 'parents': p} for s, at, p in commits]
        self.gh[f'{base}/issues/{number}/comments'] = [{'at': at, 'url': f'https://github.com/{REPO}/pull/{number}#issuecomment-{i}', 'body': body}
                                                        for i, (at, body) in enumerate(comments, 100)]
        self.gh[f'{base}/issues/14461/comments'] = [{'at': at, 'url': 'https://github.com/x', 'body': body} for at, body in issue_comments]

    def run(self, *args, env=None):
        (self.tmp / 'ps.txt').write_text('\n'.join(self.ps) + '\n')
        (self.tmp / 'gh.json').write_text(json.dumps(self.gh))
        done = subprocess.run([sys.executable, '-I', str(TOOL), '--now', NOW, *args], env={**self.env, **(env or {})}, capture_output=True, text=True)
        assert done.returncode in (0, 3), done.stderr
        return done

    def report(self, *args):
        return json.loads(self.run('--json', *args).stdout)

    def calls(self):
        p = self.tmp / 'calls.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def only(report):
    (lane,) = report['lanes']
    return lane


def test_a_pushed_commit_after_the_lane_started_renews_the_lease(lab):
    lab.lane()
    lab.pr(commits=[('c1', '2026-11-23T11:50:00Z', 1)])
    lane = only(lab.report())
    assert lane['status'] == 'OK' and lane['basis'] == 'commit' and lane['age_min'] == 10 and lane['last_renewal'] == '2026-11-23T11:50:00Z'


def test_a_lane_with_only_a_merge_commit_and_a_pm_comment_is_stalled_with_a_promotion_packet(lab):
    lab.lane()
    lab.pr(commits=[('old', '2026-11-23T09:00:00Z', 1), ('merge', '2026-11-23T11:55:00Z', 2)],  # before the lane started / a main merge: not progress
           comments=[('2026-11-23T11:56:00Z', 'PM 12b: PASS at abc')])  # no lane marker: not the lane's comment
    lane = only(lab.report())
    assert lane['status'] == 'STALLED' and lane['basis'] == 'launch' and lane['age_min'] == 100 and lane['last_renewal'] == '2026-11-23T10:20:00Z'
    assert lane['last_commit'] == 'none' and lane['last_comment'] == 'none' and lane['branch'] == 'fix/14461-x'
    assert lane['packet']['lane'] == NAME and lane['packet']['pr'] == '14470' and lane['packet']['issue'] == '14461' and lane['packet']['head'] == 'a' * 40
    text = lab.run().stdout
    assert f'PROXY_LEASE lane={NAME} status=STALLED age_min=100 threshold_min=60' in text and 'packet: lane=' in text and 'worktree=UNKNOWN' in text
    assert 'PROXY_LEASE_SUMMARY lanes=1 ok=0 stalled=1 unavailable=0 threshold_min=60' in text and 'hand off one tier up' in text


def test_the_exit_code_is_zero_unless_fail_on_stalled_is_asked_for(lab):
    lab.lane()
    lab.pr()
    assert lab.run().returncode == 0 and lab.run('--fail-on-stalled').returncode == 3
    assert lab.run('--threshold-min', '200').returncode == 0 and only(lab.report('--threshold-min', '200'))['status'] == 'OK'


@pytest.mark.parametrize('body', [NAME + ' pushed', 'hd-builder: pushed 3 commits', '**hd-builder** update'])
def test_a_pr_comment_by_the_lane_renews_it(lab, body):
    lab.lane()
    lab.pr(comments=[('2026-11-23T11:45:00Z', body)])
    lane = only(lab.report())
    assert lane['status'] == 'OK' and lane['basis'] == 'comment' and lane['age_min'] == 15


def test_a_comment_from_someone_else_or_before_the_lane_started_does_not_renew(lab):
    lab.lane()
    lab.pr(comments=[('2026-11-23T11:45:00Z', 'looks good to me'), ('2026-11-23T09:00:00Z', NAME + ' old comment')])
    assert only(lab.report())['status'] == 'STALLED'


def test_an_unreadable_source_is_unavailable_with_its_reason_never_a_guessed_stall(lab):
    lab.lane()
    lab.pr()
    lab.gh[f'repos/{REPO}/pulls/14470/commits'] = 'ERROR'
    lane = only(lab.report())
    assert lane['status'] == 'UNAVAILABLE' and 'gh_api_commits:gh: HTTP 502' in lane['reason'] and lane['basis'] == 'launch'
    assert 'UNAVAILABLE reason=gh_api_commits' in lab.run().stdout


def test_a_lane_with_no_pr_no_issue_and_no_worktree_is_stalled_with_that_stated(lab):
    lab.lane(pr=None, issue=None)
    lane = only(lab.report())
    assert lane['status'] == 'STALLED' and lane['reason'] == 'no_pr_no_branch_resolved' and lane['packet']['worktree'] == 'UNKNOWN'


def git(*a, cwd=None):
    return subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def worktree_lane(lab, *, dirty=False, unpushed=1):
    """A lane with no PR yet whose transcript names its lane-clone checkout: real git, a local bare origin."""
    origin = lab.tmp / 'origin.git'
    git('init', '-q', '--bare', str(origin))
    wt = lab.tmp / 'wt'
    git('init', '-q', '-b', 'fix/14461-x', str(wt))
    git('remote', 'add', 'origin', str(origin), cwd=wt)
    (wt / 'a').write_text('1')
    git('add', 'a', cwd=wt)
    git('commit', '-q', '-m', 'one', cwd=wt)
    git('push', '-q', 'origin', 'HEAD:refs/heads/fix/14461-x', cwd=wt)
    for i in range(unpushed):
        (wt / f'u{i}').write_text('u')
        git('add', '.', cwd=wt)
        git('commit', '-q', '-m', f'local {i}', cwd=wt)
    if dirty:
        (wt / 'a').write_text('changed')
    out = lab.tmp / 'lane.md'
    event = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': f'lane-clone.sh clone {wt} origin/fix/14461-x'}}]}}
    out.with_suffix('.jsonl').write_text(json.dumps(event) + '\n')
    lab.lane(pr=None, out=out, started='2020-01-01T00:00:00Z')  # started long ago, so the real commit time is "after the lane started"
    lab.gh[f'repos/{REPO}/pulls?head=heydonna-app:fix/14461-x&state=open'] = []
    lab.gh[f'repos/{REPO}/issues/14461/comments'] = []
    return wt


def test_the_packet_reports_the_lanes_worktree_branch_head_dirty_and_unpushed_state(lab):
    wt = worktree_lane(lab, dirty=True, unpushed=2)
    p = only(lab.report('--now', '2999-01-01T00:00:00Z'))['packet']
    assert p['worktree'] == str(wt) and p['branch'] == 'fix/14461-x' and p['head'] == git('rev-parse', 'HEAD', cwd=wt)
    assert p['dirty'] == 1 and p['unpushed'] == 2 and p['remote_head'] != 'UNKNOWN' and p['remote_head'] != p['head']


def test_a_lane_without_a_pr_is_renewed_by_the_branch_tip_it_pushed(lab):
    worktree_lane(lab)
    lane = only(lab.report('--now', '2999-01-01T00:00:00Z', '--threshold-min', '99999999'))
    assert lane['basis'] == 'commit' and lane['ref'].startswith('fix/14461-x@') and lane['last_commit'] != 'none'


def test_a_receipt_renews_only_when_github_confirms_the_comment(lab):
    lab.lane()
    lab.pr()
    rec = lab.runs / NAME / 'receipts'
    rec.mkdir()
    url = f'https://github.com/{REPO}/pull/14470#issuecomment-555'
    (rec / 'a.json').write_text(json.dumps({'ref': url}))
    (rec / 'b.json').write_text(json.dumps({'ref': 'slack ts 1795.123 trust me'}))  # not verifiable: a self-claim, ignored
    lab.gh[f'repos/{REPO}/issues/comments/555'] = [{'at': '2026-11-23T11:30:00Z', 'url': url}]
    lane = only(lab.report())
    assert lane['status'] == 'OK' and lane['basis'] == 'receipt' and lane['ref'] == url and lane['age_min'] == 30
    lab.gh[f'repos/{REPO}/issues/comments/555'] = []  # GitHub has no such comment: no renewal
    assert only(lab.report())['status'] == 'STALLED'


def test_only_lanes_this_launcher_registered_are_checked(lab):
    lab.lane()
    lab.pr(commits=[('c1', '2026-11-23T11:50:00Z', 1)])
    lab.ps.append('  9999 1 /opt/homebrew/bin/claude -p --name px-someone-elses-session --model claude-sonnet-5-5')
    assert [x['lane'] for x in lab.report()['lanes']] == [NAME]


def test_nothing_is_written_unless_record_is_asked_for_and_then_only_the_lease_fields(lab):
    lab.lane()
    lab.pr(commits=[('c1', '2026-11-23T11:50:00Z', 1)])
    lab.run()
    assert lab.calls() == []
    lab.run('--record')
    (argv,) = lab.calls()
    pairs = dict(zip(argv[1::2], argv[2::2]))
    assert argv[0] == 'obligation-upsert' and pairs['--kind'] == 'proxy_run' and pairs['--target-id'] == NAME and pairs['--issue'] == '14461'
    ev = json.loads(pairs['--evidence-json'])
    assert set(ev) == {'lease_renewed_at', 'lease_basis', 'lease_ref', 'lease_state', 'lease_checked_at'} and ev['lease_state'] == 'OK' and ev['lease_basis'] == 'commit'


def test_the_tool_cannot_stop_anything():
    src = TOOL.read_text()
    for banned in ('os.kill', 'import signal', 'SIGTERM', 'killpg', 'pkill', 'tmux', 'terminate()'):
        assert banned not in src, banned
