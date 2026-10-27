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
        c.executemany('INSERT INTO events VALUES (?,0,?,?)', [(1,'SessionStart','{"session_id":"s"}'),(2,'UserPromptSubmit','{}'),(3,'pm_status_busy','{}')])
    return base


def execute(home, dry, effects, race=None, phase='checkpoint'):
    code = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    code = code.replace("log = Path(f'/tmp/daily-pm-restart-{day}.log')", "log = state_dir / f'daily-pm-restart-{day}.log'")
    (home / '.claude/state').mkdir(parents=True, exist_ok=True)
    changed = False
    def run(argv, **kwargs):
        nonlocal changed
        effects.append(argv)
        text = '[{"number":13,"headRefOid":"abc"}]'
        if argv[:2] == ['tmux','display-message']:
            text = f"{'%41' if (changed and race == 'identity') or race == 'successor' else '%40'}|$1|100|{home}/Downloads/projects/heydonna-app|claude"
        elif argv[0] == 'ps':
            text = f'100 1 {os.getuid()} Tue Oct 27 00:00:00 2026 claude'
            if phase == 'checkpoint' or race == 'caller_still_running':
                text += f'\n101 100 {os.getuid()} Tue Oct 27 00:01:00 2026 bash daily-pm-restart.sh'
                text += f'\n102 101 {os.getuid()} Tue Oct 27 00:01:00 2026 python3 -'
                text += f'\n103 102 {os.getuid()} Tue Oct 27 00:01:00 2026 ps -axo'
            else:
                text += f'\n500 1 {os.getuid()} Tue Oct 27 00:00:00 2026 node mop-server'
                text += f'\n501 500 {os.getuid()} Tue Oct 27 00:01:00 2026 python3 -'
            if changed and race == 'tool': text += f'\n101 100 {os.getuid()} Tue Oct 27 00:00:00 2026 running-tool'
            if changed and race == 'birth': text = text.replace('00:00:00','01:00:00')
        elif argv[:2] == ['tmux','capture-pane']: text = 'Ready\n❯\n'
        elif argv[0] == 'python3' and 'stale-process-cleanup' in argv[1]:
            changed = True
            if race == 'completed_work':
                with sqlite3.connect(home / '.local/share/master-of-panes/data/mop.db') as c:
                    c.executemany('INSERT INTO events(slot,event_type,payload) VALUES(0,?,?)', [('UserPromptSubmit','{}'),('Stop','{}')])
        elif argv[:2] in (['tmux','if-shell'], ['tmux','respawn-pane']):
            saved, = (home / '.claude/state').glob('daily-pm-restart-*.json')
            assert json.loads(saved.read_text())['launch_intent'] is True
            if race == 'uncertain': raise TimeoutError('accepted response lost')
            text = 'PM_RESTART_REQUESTED'
        return SimpleNamespace(stdout=text, returncode=0)
    def url(request, **kwargs):
        if isinstance(request, str):
            if request.endswith('/pm-status'):
                return io.BytesIO(json.dumps({'pm_busy': phase == 'checkpoint' or race == 'busy', 'queue_count': 1 if race == 'queued' else 0}).encode())
            return io.BytesIO(json.dumps({'slots': [{'slot': 2, 'issue': 12, 'assignment_epoch': 8}]}).encode())
        effects.append(['exit-pending', request.data.decode()])
        return io.BytesIO(b'{"ok":true}')
    with patch.dict(os.environ, {'MOP_DB_PATH': str(home / '.local/share/master-of-panes/data/mop.db')}), \
         patch.object(Path, 'home', return_value=home), patch.object(sys, 'argv', ['restart'] + (['--dry-run'] if dry else ['--complete-on-idle'] if phase == 'idle' else [])), \
         patch('subprocess.run', side_effect=run), patch('urllib.request.urlopen', side_effect=url):
        exec(compile(code, str(SCRIPT), 'exec'), {'__name__': '__main__'})


def stop(home):
    with sqlite3.connect(home / '.local/share/master-of-panes/data/mop.db') as c:
        c.executemany('INSERT INTO events(slot,event_type,payload) VALUES(0,?,?)', [('PostToolUse','{"tool_name":"Bash","tool_input":{"command":"~/.claude/scripts/pm/daily-pm-restart.sh"}}'),('Stop','{}')])


def replacements(effects):
    return [e for e in effects if e[:2] in (['tmux','if-shell'],['tmux','respawn-pane'])]


def test_legacy_unfinished_state_is_preserved_without_new_attempt(tmp_path):
    environment(tmp_path)
    effects = []
    execute(tmp_path,False,effects)
    saved, = (tmp_path / '.claude/state').glob('daily-pm-restart-*.json')
    saved.write_text('{"cleanup":true,"launch_intent":false}')
    before = saved.read_bytes()
    effects.clear()
    execute(tmp_path,False,effects)
    assert effects == [] and saved.read_bytes() == before


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
    assert not replacements(effects) and not any(e[0] == 'exit-pending' for e in effects)
    saved, = (tmp_path / '.claude/state').glob('daily-pm-restart-*.json')
    assert json.loads(saved.read_text())['handoff_status'] == 'pending'
    assert not json.loads(saved.read_text()).get('launch_intent')
    stop(tmp_path)
    execute(tmp_path, False, effects, phase='idle')
    replacement, = [e for e in effects if e[:2] == ['tmux','if-shell']]
    assert next(i for i,e in enumerate(effects) if e[0] == 'exit-pending') < effects.index(replacement)
    assert replacement[4] == '%40'
    assert 'respawn-pane -k -t %40' in replacement[-2]
    assert 'launch-pm-cliproxy.sh' in replacement[-2]
    before = list(effects)
    execute(tmp_path, False, effects, phase='idle')
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


@pytest.mark.parametrize('race', ['identity', 'completed_work', 'busy', 'birth', 'tool', 'caller_still_running', 'queued'])
def test_fresh_pm_identity_and_activity_refuse_replacement(tmp_path, race):
    environment(tmp_path)
    effects = []
    execute(tmp_path, False, effects)
    stop(tmp_path)
    with pytest.raises(RuntimeError): execute(tmp_path, False, effects, race, phase='idle')
    assert not any(e[:2] == ['tmux','if-shell'] or e[:2] == ['tmux','respawn-pane'] for e in effects)


def test_accepted_replacement_response_loss_cannot_retry(tmp_path):
    environment(tmp_path)
    effects = []
    execute(tmp_path,False,effects)
    stop(tmp_path)
    with pytest.raises(TimeoutError): execute(tmp_path, False, effects, 'uncertain', phase='idle')
    before = list(effects)
    execute(tmp_path, False, effects, phase='idle')
    assert effects == before
    saved, = (tmp_path / '.claude/state').glob('daily-pm-restart-*.json')
    assert json.loads(saved.read_text())['replacement_status'] == 'attempted_uncertain'


@pytest.mark.parametrize('race', ['successor','completed_before_idle','tool_after_checkpoint','posttool_without_pre','status_start_stop'])
def test_idle_handoff_refuses_successor_or_intervening_work(tmp_path,race):
    environment(tmp_path)
    effects = []
    execute(tmp_path,False,effects)
    stop(tmp_path)
    with sqlite3.connect(tmp_path / '.local/share/master-of-panes/data/mop.db') as c:
        if race == 'completed_before_idle':
            c.executemany('INSERT INTO events(slot,event_type,payload) VALUES(0,?,?)',[('UserPromptSubmit','{}'),('Stop','{}')])
        if race == 'tool_after_checkpoint':
            c.executemany('INSERT INTO events(slot,event_type,payload) VALUES(0,?,?)',[('PreToolUse','{}'),('PostToolUse','{}'),('Stop','{}')])
        if race == 'posttool_without_pre':
            c.executemany('INSERT INTO events(slot,event_type,payload) VALUES(0,?,?)',[('PostToolUse','{}'),('Stop','{}')])
        if race == 'status_start_stop':
            c.executemany('INSERT INTO events(slot,event_type,payload) VALUES(0,?,?)',[('pm_status_busy','{}'),('pm_status_idle_drained','{}'),('Stop','{}')])
    execute(tmp_path,False,effects,race,phase='idle')
    assert not replacements(effects)
    saved, = (tmp_path / '.claude/state').glob('daily-pm-restart-*.json')
    assert json.loads(saved.read_text())['handoff_status'] == 'refused_successor_or_work'


if __name__ == '__main__':
    # Cross-language fixture: real MoP relay owns the idle callback process.
    home = Path(sys.argv[1]); phase = sys.argv[2]
    if phase == 'checkpoint': environment(home)
    if phase == 'stop': stop(home)
    if phase != 'stop':
        history = home / 'fixture-effects.json'
        effects = json.loads(history.read_text()) if history.exists() else []
        execute(home,False,effects,phase=phase)
        history.write_text(json.dumps(effects))
        (home/'fixture-parent.json').write_text(json.dumps({'parent_pid':os.getppid()}))
