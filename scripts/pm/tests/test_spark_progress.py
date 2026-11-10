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
    assert 'steps last hour 5' in result
    assert 'IST)' in result


def test_stalled_interval_and_no_tqdm_fallback():
    data = {'total': 200, 'samples': [[0, 100], [900, 100]]}
    result = progress.spark_progress_render(data, 900)
    assert 'ETA n/a' in result
    assert 'NO STEP PROGRESS in 900s' in result


def test_persists_across_restart_and_resets_when_step_decreases(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, 'SPARK_PROGRESS_STATE_DIR', tmp_path)
    progress.spark_progress_update('run', 'host', '/log', 100, 200, 1000)
    result = progress.spark_progress_update('run', 'host', '/log', 102, 200, 1600)
    assert 'sec/step 300.0' in result
    assert 'steps last hour 2' in result
    assert 'sec/step n/a' in progress.spark_progress_update('run', 'host', '/log', 1, 200, 1700)


def test_unknown_when_no_samples():
    assert 'ETA n/a' in progress.spark_progress_render({'samples': []})
