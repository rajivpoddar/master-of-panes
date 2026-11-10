import importlib.util
from pathlib import Path


PATH = Path(__file__).parents[1] / 'shared-assets/claude/scripts/pm/heartbeat/heartbeat-compose.py'
spec = importlib.util.spec_from_file_location('spark_progress', PATH)
progress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(progress)


def test_measured_rate_prefers_hour_window_and_ist():
    data = {'total': 200, 'samples': [[0, 100], [3600, 110], [5400, 115]]}
    result = progress.spark_progress_render(data, 5400)
    assert 'sec/step 360.0' in result
    assert 'steps in last 30m: 5' in result
    assert 'IST)' in result


def test_stalled_interval_and_no_tqdm_fallback():
    data = {'total': 200, 'samples': [[0, 100], [900, 100]]}
    result = progress.spark_progress_render(data, 900)
    assert 'ETA n/a' in result
    assert 'NO STEP PROGRESS in 900s' in result


def test_persists_across_restart_and_resets_when_step_decreases(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, 'SPARK_PROGRESS_STATE_DIR', tmp_path)
    identity = {'log_id': '1:2', 'size': 100, 'container': 'container-start'}
    progress.spark_progress_update('run', 'host', '/log', 100, 200, 1000, identity=identity)
    result = progress.spark_progress_update('run', 'host', '/log', 102, 200, 1600, identity=identity)
    assert 'sec/step 300.0' in result
    assert 'steps in last 10m: 2' in result
    assert 'sec/step n/a' in progress.spark_progress_update('run', 'host', '/log', 1, 200, 1700, identity=identity)
    changed = dict(identity, log_id='1:3')
    assert 'sec/step n/a' in progress.spark_progress_update('run', 'host', '/log', 2, 200, 1800, identity=changed)
    assert 'sec/step n/a' in progress.spark_progress_update('run', 'host', '/log', 3, 200, 1900, identity=dict(changed, container='new-start'))


def test_advance_then_stall_and_partial_window():
    result = progress.spark_progress_render({'total': 5625, 'samples': [[0, 3916], [1200, 3920], [2220, 3920]]})
    assert 'ETA n/a' in result
    assert 'NO STEP PROGRESS in 1020s' in result
    assert 'steps in last 37m: 4' in result
    full = progress.spark_progress_render({'total': 5625, 'samples': [[0, 3916], [3600, 3920]]})
    assert 'steps in last hour: 4' in full


def test_optimizer_identity_excludes_loading_and_micro_steps():
    import ast
    source = (PATH.parents[2] / 'spark-run-monitor.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'parse')
    import re
    namespace = {'re': re, 'num': r'(-?\d+(?:\.\d+)?(?:e-?\d+)?)'}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<parse>', 'exec'), namespace)
    parse = namespace['parse']
    loading = [' 46%|x| 311/677 [01:00<02:00, 3.0it/s]', '100%|x| 677/677 [03:00<00:00, 3.0it/s]', 'micro_step 3923/5625']
    assert parse(loading, 5625)['step'] is None
    assert parse(['70%|x| 3922/5625 [57:09<1:16:20, 2.69s/it]'], 5625)['step'] is None
    trainer = "70%|x| 3922/5625 [57:09<1:16:20, 2.69s/it]{'loss': '0.1362', 'grad_norm': '18.24'}"
    assert parse([trainer] + loading, 5625)['step'] == 3922
    assert parse([trainer], 677)['step'] is None


def test_unknown_when_no_samples():
    assert 'ETA n/a' in progress.spark_progress_render({'samples': []})
