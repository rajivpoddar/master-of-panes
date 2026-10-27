"""Daily restart runs real snapshot code against isolated state and mocked effects."""
import io
import json
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
    return base


def execute(home, dry, effects):
    code = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    def run(argv, **kwargs):
        effects.append(argv)
        return SimpleNamespace(stdout='[{"number":13,"headRefOid":"abc"}]', returncode=0)
    def url(request, **kwargs):
        if isinstance(request, str):
            return io.BytesIO(json.dumps({'slots': [{'slot': 2, 'issue': 12, 'assignment_epoch': 8}]}).encode())
        effects.append(['exit-pending', request.data.decode()])
        return io.BytesIO(b'{"ok":true}')
    with patch.object(Path, 'home', return_value=home), patch.object(sys, 'argv', ['restart'] + (['--dry-run'] if dry else [])), \
         patch('subprocess.run', side_effect=run), patch('urllib.request.urlopen', side_effect=url):
        exec(compile(code, str(SCRIPT), 'exec'), {'__name__': '__main__'})


def test_dry_run_snapshots_but_does_not_restart_or_write_resume(tmp_path, capsys):
    base = environment(tmp_path)
    effects = []
    execute(tmp_path, True, effects)
    assert not list((base / 'memory').glob('*0400.md'))
    assert len(effects) == 1 and effects[0][:3] == ['gh', 'pr', 'list']
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
    assert effects[-2][0] == 'exit-pending'
    assert effects[-1][:5] == ['tmux', 'respawn-pane', '-k', '-t', '0:0.0']
    assert 'launch-pm-cliproxy.sh' in effects[-1][-1]
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
    assert effects == []
    assert not list((base / 'memory').glob('*0400.md'))
    assert '(old.md)' in (base / 'memory/MEMORY.md').read_text()
