import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/pm/heartbeat'


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_oversized_report_keeps_every_section_and_unavailable():
    composer = load('heartbeat-compose')
    lines = ['Heartbeat']
    for section in range(12):
        lines += ['', f'*Section {section}:*', '• UNAVAILABLE (read timeout)']
        lines += [f'• item {item}' for item in range(20)]
    for limit in (20, 24):
        text = composer.compact_report(lines, limit)
        assert len(text.splitlines()) <= limit
        assert all(f'*Section {section}:*' in text for section in range(12))
        assert text.count('UNAVAILABLE') == 12
        assert '+20 more' in text


def test_slow_refresh_does_not_starve_next_channel_or_in_app(monkeypatch, capsys):
    import json
    module = load('support-inbox-snapshot')
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.sys, 'argv', ['support-inbox-snapshot'])
    monkeypatch.setattr(module, 'CHANNELS', {'a': 'A', 'b': 'B'})
    monkeypatch.setattr(module, 'load_env', lambda _: 'test-token')
    monkeypatch.setattr(module, 'load_slack_reply_skips', lambda: {})
    monkeypatch.setattr(module, 'load_open_state', lambda: {})
    monkeypatch.setattr(module, 'load_carry_cursor', lambda _: 0)
    monkeypatch.setattr(module, 'save_carry_cursor', lambda *args: None)
    monkeypatch.setattr(module, 'save_open_state', lambda *args: None)
    monkeypatch.setattr(module, 'tracked_roots', lambda *args: {})
    monkeypatch.setattr(module, 'apply_skips', lambda threads, *args: threads)
    monkeypatch.setattr(module, 'select_items', lambda threads, *args: threads)
    reads = []

    def channel(token, channel, *args):
        assert module.remaining_budget() > 0
        reads.append(channel)
        return [{'ts': '1', 'channel': channel}]

    def refresh(threads, *args, **kwargs):
        clock[0] = kwargs['deadline'] + 0.1
        raise module.PartialChannel('refresh timeout', threads)

    def in_app(*args):
        assert module.remaining_budget() > 0
        reads.append('in-app')
        return [{'title': 'retained'}]

    monkeypatch.setattr(module, 'fetch_channel', channel)
    monkeypatch.setattr(module, 'carry_forward', refresh)
    monkeypatch.setattr(module, 'fetch_in_app', in_app)
    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert reads == ['a', 'b', 'in-app']
    assert len(result['slack']) == 2
    assert result['in_app'] == [{'title': 'retained'}]
    assert len(result['failures']) == 2
    assert all('partial' in failure for failure in result['failures'])
