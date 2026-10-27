"""The production selector send_allowed gate requires every approved template."""
import importlib.util
import contextlib
import fcntl
import io
import json
import multiprocessing
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/growth/lifecycle-drip.py'


def load(home):
    spec = importlib.util.spec_from_file_location('drip_delivery', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE = home / 'drip.jsonl'
    module.OPTOUT = home / 'optout.jsonl'
    module.TEMPLATES = home / 'templates'
    return module


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026,10,27,14,tzinfo=timezone.utc)


def setup_mail(home):
    module = load(home)
    module.TEMPLATES.mkdir()
    for touch in module.TOUCHES:
        (module.TEMPLATES / f'{touch}.txt').write_text('STATUS: APPROVED\n')
    (home / 'body.txt').write_text("Personalized synthetic fixture. Reply 'stop'.")


def mail_entry(home, mode='ok', result='result.json'):
    module = load(home)
    argv = ['drip','send','--user-id','synthetic-user','--email','fixture@example.invalid',
            '--touch','FIRST_FILE_READY','--subject','Fixture','--body-file',str(home/'body.txt')]
    def gmail(command, **kwargs):
        assert command[:3] == ['gws','gmail','+send']
        # The actual writer must have fsynced intent BEFORE reaching Gmail.
        state = module.read_jsonl(module.STATE)
        assert state[-1]['event'] == 'attempt'
        with (home/'accepted.jsonl').open('a') as out:
            fcntl.flock(out,fcntl.LOCK_EX)
            out.write('{"id":"fixture-gmail-id"}\n');out.flush();os.fsync(out.fileno())
        if mode == 'lost': raise TimeoutError('accepted; no response')
        if mode == 'crash': os._exit(87)
        return SimpleNamespace(stdout='{"id":"fixture-gmail-id"}',returncode=0)
    output = io.StringIO()
    with patch.object(module,'datetime',Clock),patch.object(sys,'argv',argv), \
            patch.object(module.subprocess,'run',side_effect=gmail),contextlib.redirect_stdout(output):
        module.main()
    (home/result).write_text(output.getvalue())


def test_actual_send_entry_is_durable_and_concurrent_once(tmp_path):
    setup_mail(tmp_path)
    ctx = multiprocessing.get_context('fork')
    workers = [ctx.Process(target=mail_entry,args=(tmp_path,'ok',f'worker-{i}.json')) for i in range(2)]
    for p in workers:p.start()
    for p in workers:p.join(10);assert p.exitcode == 0
    assert len((tmp_path/'accepted.jsonl').read_text().splitlines()) == 1
    rows = load(tmp_path).read_jsonl(tmp_path/'drip.jsonl')
    assert [r['event'] for r in rows] == ['attempt','sent']
    assert rows[0]['attempt_id'] == rows[1]['attempt_id']
    assert rows[1]['gmail_id'] == 'fixture-gmail-id'


def test_accepted_lost_ack_and_restart_keep_attempt_without_resend(tmp_path):
    setup_mail(tmp_path)
    mail_entry(tmp_path,'lost')
    before = (tmp_path/'drip.jsonl').read_bytes()
    mail_entry(tmp_path)
    assert json.loads((tmp_path/'result.json').read_text())['status'] == 'UNCERTAIN'
    assert (tmp_path/'drip.jsonl').read_bytes() == before
    assert len((tmp_path/'accepted.jsonl').read_text().splitlines()) == 1
    module = load(tmp_path)
    attempt = module.read_jsonl(module.STATE)[0]
    argv = ['drip','record-sent','--user-id','synthetic-user','--email','fixture@example.invalid',
            '--touch','FIRST_FILE_READY','--gmail-id','fixture-gmail-id','--attempt-id',attempt['attempt_id']]
    with patch.object(sys,'argv',argv),patch.object(module.subprocess,'run',side_effect=AssertionError('reconcile sent')):
        module.main()
    mail_entry(tmp_path)
    assert json.loads((tmp_path/'result.json').read_text())['duplicate'] is True
    assert len((tmp_path/'accepted.jsonl').read_text().splitlines()) == 1


def test_accepted_process_crash_then_new_process_refuses_replay(tmp_path):
    setup_mail(tmp_path)
    ctx = multiprocessing.get_context('fork')
    first = ctx.Process(target=mail_entry,args=(tmp_path,'crash'))
    first.start();first.join(10);assert first.exitcode == 87
    second = ctx.Process(target=mail_entry,args=(tmp_path,))
    second.start();second.join(10);assert second.exitcode == 0
    assert json.loads((tmp_path/'result.json').read_text())['status'] == 'UNCERTAIN'
    assert len((tmp_path/'accepted.jsonl').read_text().splitlines()) == 1


def test_failed_or_skipped_batch_keeps_first_file_ready_recoverable(tmp_path):
    module = load(tmp_path)
    now = Clock.now()
    uid = 'synthetic-user'
    module.append_jsonl(module.STATE, {'event':'run','ts':(now-timedelta(days=1)).isoformat(),'dry_run':False})
    def convex(table, limit):
        return {'users':[{'userId':uid,'email':'fixture@example.invalid','_creationTime':(now-timedelta(days=10)).timestamp()*1000}],
                'transcripts':[{'userId':uid,'_id':'f','_creationTime':1,'lastAutoProcessCompletedAt':(now-timedelta(days=5)).timestamp()*1000}]
               }.get(table,[])
    with patch.object(module,'convex',side_effect=convex),patch.object(module,'axiom_activity',return_value=({},{})), \
            patch.object(module,'template_status',return_value={t:'APPROVED' for t in module.TOUCHES}):
        with patch.object(module,'gmail_recent_manual_recipients',return_value={'fixture@example.invalid'}):
            skipped = module.select(now,False)
            assert skipped['skipped'][0]['touch'] == 'FIRST_FILE_READY'
        with patch.object(module,'gmail_recent_manual_recipients',return_value=set()):
            for _ in range(2):
                selected = module.select(now,False)
                assert selected['records'][0]['touch'] == 'FIRST_FILE_READY'


def test_selector_send_allowed_requires_every_template_and_weekday(tmp_path):
    spec = importlib.util.spec_from_file_location('lifecycle_drip', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def select(iso='2026-10-27T14:00:00+00:00'):
        with patch.object(module, 'TEMPLATES', tmp_path), patch.object(module, 'read_jsonl', return_value=[]), \
             patch.object(module, 'convex', return_value=[]), patch.object(module, 'axiom_activity', return_value=({}, {})), \
             patch.object(module, 'gmail_recent_manual_recipients', return_value=set()), \
             patch.object(module, 'append_jsonl', side_effect=AssertionError('dry-run wrote state')):
            return module.select(datetime.fromisoformat(iso), True)['meta']['send_allowed']
    assert select() is False
    for touch in module.TOUCHES:
        (tmp_path / f'{touch}.txt').write_text('STATUS: APPROVED (Rajiv cited)\n')
    assert select() is True
    for touch in module.TOUCHES:
        (tmp_path / f'{touch}.txt').write_text('STATUS: DRAFT_PENDING_RAJIV\n')
        assert select() is False, touch
        (tmp_path / f'{touch}.txt').write_text('STATUS: APPROVED\n')
    assert select('2026-10-31T14:00:00+00:00') is False
