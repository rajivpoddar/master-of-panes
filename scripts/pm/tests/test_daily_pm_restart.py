"""Daily restart runs real snapshot code against isolated state and mocked effects."""
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import pytest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/pm/daily-pm-restart.sh'


def environment(tmp_path):
    base = tmp_path / '.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app'
    (base / 'state').mkdir(parents=True)
    (base / 'memory').mkdir()
    (base / 'memory/MEMORY.md').write_text('- [RESUME PROMPT old](old.md)\n- Keep this entry\n')
    connection = sqlite3.connect(base / 'state/pm-ops.db')
    connection.execute('CREATE TABLE obligations (id,kind,owner,title,required_action,blocker,issue,pr,slot,status)')
    connection.execute("INSERT INTO obligations VALUES (1,'repair','agent-one','Fix','Resume exact packet',NULL,12,13,2,'open')")
    connection.commit()
    connection.close()
    mop = tmp_path / '.local/share/master-of-panes/data/mop.db'
    mop.parent.mkdir(parents=True)
    with sqlite3.connect(mop) as c:
        c.execute('CREATE TABLE events (id INTEGER PRIMARY KEY, slot, event_type, payload)')
        c.executemany('INSERT INTO events VALUES (?,0,?,?)', [(1,'SessionStart','{"session_id":"s"}'),(2,'UserPromptSubmit','{}'),(3,'Stop','{}')])
    return base


def execute(home, dry, effects, race=None):
    code = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    code = code.replace("log = Path(f'/tmp/daily-pm-restart-{day}.log')", "log = state_dir / f'daily-pm-restart-{day}.log'")
    (home / '.claude/state').mkdir(parents=True, exist_ok=True)
    changed = False
    def run(argv, **kwargs):
        nonlocal changed
        effects.append(argv)
        text = '[{"number":13,"headRefOid":"abc"}]'
        if argv[:2] == ['tmux','display-message']:
            text = f"{'%41' if changed and race == 'identity' else '%40'}|$1|100|{home}/Downloads/projects/heydonna-app|claude"
        elif argv[0] == 'ps':
            text = f'100 1 {os.getuid()} Tue Oct 27 00:00:00 2026 claude'
            if changed and race == 'tool': text += f'\n101 100 {os.getuid()} Tue Oct 27 00:00:00 2026 running-tool'
            if changed and race == 'birth': text = text.replace('00:00:00','01:00:00')
        elif argv[:2] == ['tmux','capture-pane']: text = 'Ready\n❯\n'
        elif argv[0] == 'python3' and 'stale-process-cleanup' in argv[1]:
            changed = True
            if race == 'completed_work':
                with sqlite3.connect(home / '.local/share/master-of-panes/data/mop.db') as c:
                    c.executemany('INSERT INTO events VALUES (?,0,?,?)', [(4,'UserPromptSubmit','{}'),(5,'Stop','{}')])
        elif argv[:2] in (['tmux','if-shell'], ['tmux','respawn-pane']):
            saved, = (home / '.claude/state').glob('daily-pm-restart-*.json')
            assert json.loads(saved.read_text())['launch_intent'] is True
            if race == 'uncertain': raise TimeoutError('accepted response lost')
            text = 'PM_RESTART_REQUESTED'
        return SimpleNamespace(stdout=text, returncode=0)
    def url(request, **kwargs):
        if isinstance(request, str):
            if request.endswith('/pm-status'):
                return io.BytesIO(json.dumps({'pm_busy': race == 'busy', 'queue_count': 0}).encode())
            return io.BytesIO(json.dumps({'slots': [{'slot': 2, 'issue': 12, 'assignment_epoch': 8}]}).encode())
        effects.append(['exit-pending', request.data.decode()])
        return io.BytesIO(b'{"ok":true}')
    with patch.dict(os.environ, {'MOP_DB_PATH': str(home / '.local/share/master-of-panes/data/mop.db')}), \
         patch.object(Path, 'home', return_value=home), patch.object(sys, 'argv', ['restart'] + (['--dry-run'] if dry else [])), \
         patch('subprocess.run', side_effect=run), patch('urllib.request.urlopen', side_effect=url):
        exec(compile(code, str(SCRIPT), 'exec'), {'__name__': '__main__'})


def test_dry_run_snapshots_but_does_not_restart_or_write_resume(tmp_path, capsys):
    base = environment(tmp_path)
    effects = []
    execute(tmp_path, True, effects)
    assert not list((base / 'memory').glob('*0400.md'))
    assert [e for e in effects if e[:3] == ['gh','pr','list']]
    assert not any(e[:2] == ['tmux','if-shell'] or e[0] == 'exit-pending' for e in effects)
    output = capsys.readouterr().out
    assert 'open_obligations=1; slots=1; open_prs=1' in output
    assert 'SAFE_POINT' in output and 'PM_RELAUNCH' in output
    assert '(old.md)' in (base / 'memory/MEMORY.md').read_text()


def test_apply_preserves_state_orders_safe_exits_and_is_idempotent(tmp_path):
    base = environment(tmp_path)
    effects = []
    execute(tmp_path, False, effects)
    resume, = (base / 'memory').glob('*0400.md')
    text = resume.read_text()
    assert 'agent-one' in text and 'assignment_epoch' in text and 'abc' in text
    assert resume.name in (base / 'memory/MEMORY.md').read_text()
    assert 'Keep this entry' in (base / 'memory/MEMORY.md').read_text()
    replacement, = [e for e in effects if e[:2] == ['tmux','if-shell']]
    assert next(i for i,e in enumerate(effects) if e[0] == 'exit-pending') < effects.index(replacement)
    assert replacement[4] == '%40'
    assert 'respawn-pane -k -t %40' in replacement[-2]
    assert 'launch-pm-cliproxy.sh' in replacement[-2]
    before = list(effects)
    execute(tmp_path, False, effects)
    assert effects == before


def test_unknown_database_refuses_before_any_restart_effect(tmp_path):
    base = environment(tmp_path)
    db = base / 'state/pm-ops.db'
    db.rename(db.with_suffix('.parked'))
    effects = []
    with pytest.raises(sqlite3.OperationalError):
        execute(tmp_path, False, effects)
    assert not any(e[0] == 'exit-pending' or e[:2] == ['tmux','if-shell'] for e in effects)
    assert not list((base / 'memory').glob('*0400.md'))
    assert '(old.md)' in (base / 'memory/MEMORY.md').read_text()


@pytest.mark.parametrize('race', ['identity', 'completed_work', 'busy', 'birth', 'tool'])
def test_fresh_pm_identity_and_activity_refuse_replacement(tmp_path, race):
    environment(tmp_path)
    effects = []
    with pytest.raises(RuntimeError): execute(tmp_path, False, effects, race)
    assert not any(e[:2] == ['tmux','if-shell'] or e[:2] == ['tmux','respawn-pane'] for e in effects)


def test_accepted_replacement_response_loss_cannot_retry(tmp_path):
    environment(tmp_path)
    effects = []
    with pytest.raises(TimeoutError): execute(tmp_path, False, effects, 'uncertain')
    before = list(effects)
    execute(tmp_path, False, effects)
    assert effects == before
    saved, = (tmp_path / '.claude/state').glob('daily-pm-restart-*.json')
    assert json.loads(saved.read_text())['replacement_status'] == 'attempted_uncertain'
