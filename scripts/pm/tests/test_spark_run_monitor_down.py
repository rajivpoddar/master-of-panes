import ast
import datetime
import subprocess
import types
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/spark-run-monitor.sh'
SOURCE = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
CONSTANTS = {'REMINDER_RENEW_BELOW_S', 'REMINDER_HORIZON_S', 'DOWN_REALERT_S', 'AUTH_REFUSAL_ERRORS'}


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


def test_expired_or_stopped_reminder_is_not_revived():
    ns = load({'iso_epoch', 'reminder_extend_argv'})
    assert ns['reminder_extend_argv'](ROW, NOW) is None  # expired
    assert ns['reminder_extend_argv'](dict(ROW, status='stopped'), NOW) is None
    assert ns['reminder_extend_argv'](dict(ROW, status='active', until_at=iso(NOW - 60)), NOW) is None  # lapsed
    assert ns['reminder_extend_argv'](None, NOW) is None


def test_active_reminder_extends_only_the_cutoff_near_expiry():
    ns = load({'iso_epoch', 'reminder_extend_argv'})
    near = dict(ROW, status='active', until_at=iso(NOW + 2 * 86400))
    argv = ns['reminder_extend_argv'](near, NOW)
    assert argv[:3] == ['--key', ROW['key'], '--until']
    assert '--replace' not in argv and '--text' not in ' '.join(argv) and '--every' not in argv
    until = datetime.datetime.fromisoformat(argv[3]).timestamp()
    assert 6 * 86400 < until - NOW < 7 * 86400
    assert ns['reminder_extend_argv'](dict(near, until_at=iso(NOW + 6 * 86400)), NOW) is None


def test_resolution_requires_fresh_identity_or_terminal_and_a_confirmed_ledger_write():
    ns = load({'fresh_trainer_progress', 'may_upsert_down', 'may_resolve_down', 'may_announce_resolution'})
    ident = {'log_id': '1:2', 'container': 'abc:2026'}
    assert ns['fresh_trainer_progress'](None, 10, ident) is False
    assert ns['fresh_trainer_progress']((10, '1:2', 'abc:2026'), 10, ident) is False  # stale step
    assert ns['fresh_trainer_progress']((10, '1:2', 'abc:2026'), 11, {'log_id': '9:9', 'container': 'abc:2026'}) is False
    assert ns['fresh_trainer_progress']((10, '1:2', 'abc:2026'), 11, None) is False  # running/NA with no identity
    assert ns['fresh_trainer_progress']((10, '1:2', 'abc:2026'), 11, ident) is True
    closed = {'id': 36382, 'named_closed': True, 'ambiguous': False, 'hold': False}
    assert ns['may_upsert_down'](closed, False) is False
    assert ns['may_upsert_down']({'id': 36382, 'named_closed': False, 'ambiguous': False, 'hold': False}, False) is False
    assert ns['may_upsert_down']({'id': 36382, 'named_closed': False, 'ambiguous': False, 'hold': False}, None) is False
    assert ns['may_upsert_down']({'id': 36382, 'named_closed': False, 'ambiguous': False, 'hold': False}, True) is True
    healthy = {'id': 36382, 'named_closed': False, 'ambiguous': False, 'hold': False}
    assert ns['may_resolve_down'](healthy, True, False, False, False) is False  # running alone is not progress
    assert ns['may_resolve_down'](healthy, True, False, True, True) is False  # stop was issued this iteration
    assert ns['may_resolve_down'](healthy, True, True, False, False) is True
    assert ns['may_resolve_down'](healthy, True, False, True, False) is True
    assert ns['may_announce_resolution'](0, False) is True
    assert ns['may_announce_resolution'](0, True) is False
    assert ns['may_announce_resolution'](1, False) is False


def test_alert_post_is_one_shot_and_never_falls_back_to_the_default_identity():
    calls = []

    def fake_run(cmd, **kw):
        calls.append((kw.get('env') or {}).get('SLACK_SEND_AS'))
        return types.SimpleNamespace(returncode=1, stdout='', stderr='SLACK_SEND_REFUSED channel=C1 thread=1.2 error=not_in_channel')

    sub = types.SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired)
    ns = load({'classify_post', 'post'}, os=__import__('os'), re=__import__('re'), sys=__import__('sys'), json=__import__('json'),
              time=types.SimpleNamespace(sleep=lambda s: None, gmtime=lambda: None, strftime=lambda *a: ''),
              subprocess=sub, a=types.SimpleNamespace(channel='C0ALZJHGE49', run_name='r'), SEND='send', redact=lambda s: s,
              ALERT_DIR='/nonexistent-alert-dir/x')
    assert ns['post']('down', '1.2', alert=True) is None
    assert calls == ['alerts']
    calls.clear()

    def time_out(cmd, **kw):
        calls.append((kw.get('env') or {}).get('SLACK_SEND_AS'))
        raise subprocess.TimeoutExpired(cmd, 120)

    ns['subprocess'] = types.SimpleNamespace(run=time_out, TimeoutExpired=subprocess.TimeoutExpired)
    assert ns['post']('down', '1.2', alert=True) is None
    assert calls == ['alerts']  # uncertain attempt is consumed; no second identity
    calls.clear()
    ns['subprocess'] = sub
    ns['post']('stats', '1.2')
    assert calls == [None]
