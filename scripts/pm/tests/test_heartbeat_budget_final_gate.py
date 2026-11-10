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
