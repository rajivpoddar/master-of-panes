import ast
import datetime
import subprocess
import types
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/spark-run-monitor.sh'
SOURCE = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
CONSTANTS = {'REMINDER_RENEW_BELOW_S', 'REMINDER_HORIZON_S', 'DOWN_REALERT_S'}


def load(names, **namespace):
    tree = ast.parse(SOURCE)
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', None) in CONSTANTS)]
    namespace.setdefault('datetime', datetime)
    exec(compile(ast.Module(body=body, type_ignores=[]), '<monitor>', 'exec'), namespace)
    return namespace


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


NOW = 1794990000.0
ROW = {'key': 'training-sindhu-r7-hourly', 'channel': 'C0ALZJHGE49', 'thread_ts': '1794326745.821609',
       'text': 'Training sindhu-r7-n5625: PM, post step/loss', 'interval_s': 3600, 'stop_obligation_id': None,
       'status': 'expired', 'until_at': iso(NOW - 3600), 'next_at': iso(NOW + 1800)}


def test_expired_reminder_is_revived_with_its_own_args_inside_the_seven_day_cap():
    ns = load({'iso_epoch', 'reminder_renewal'})
    argv = ns['reminder_renewal'](ROW, NOW)
    assert '--replace' not in argv and argv[argv.index('--first-at') + 1] == '+1m'
    assert argv[argv.index('--every') + 1] == '3600s' and '--text=' + ROW['text'] in argv
    until = datetime.datetime.fromisoformat(argv[argv.index('--until') + 1]).timestamp()
    assert 6 * 86400 < until - NOW < 7 * 86400


def test_active_reminder_renews_only_near_expiry_on_the_same_grid_and_never_revives_a_cancel():
    ns = load({'iso_epoch', 'reminder_renewal'})
    near = dict(ROW, status='active', until_at=iso(NOW + 2 * 86400))
    argv = ns['reminder_renewal'](near, NOW)
    assert '--replace' in argv and argv[argv.index('--first-at') + 1] == near['next_at']
    assert ns['reminder_renewal'](dict(near, until_at=iso(NOW + 6 * 86400)), NOW) is None
    assert ns['reminder_renewal'](dict(ROW, status='stopped'), NOW) is None
    assert ns['reminder_renewal'](None, NOW) is None


def test_alert_post_uses_alerts_identity_then_falls_back_and_plain_post_never_does():
    calls = []

    def fake_run(cmd, **kw):
        calls.append((kw.get('env') or {}).get('SLACK_SEND_AS'))
        return types.SimpleNamespace(returncode=1, stdout='', stderr='ratelimited')

    sub = types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
    ns = load({'post'}, os=__import__('os'), re=__import__('re'), sys=__import__('sys'), json=__import__('json'),
              time=types.SimpleNamespace(sleep=lambda s: None, gmtime=lambda: None, strftime=lambda *a: ''),
              subprocess=sub, a=types.SimpleNamespace(channel='C1', run_name='r'), SEND='send', redact=lambda s: s,
              ALERT_DIR='/nonexistent-alert-dir/x', POST_RETRY_DELAY_S=0)
    ns['post']('down', '1.2', alert=True)
    assert calls == ['alerts', 'alerts', None]
    calls.clear()
    ns['post']('stats', '1.2')
    assert calls == [None, None]
