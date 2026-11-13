"""CTO REVISE 1794583885.992979: named 3h overflow and real Asks header."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).parents[1] / 'shared-assets/claude/scripts'

def test_real_asks_header_is_accepted():
    source = (ROOT / 'pm-hourly-post.sh').read_text()
    assert "grep -Eq '^(<@[A-Z0-9]+> *)?\\*Hourly'" in source
    assert re.match(r'^(<@[A-Z0-9]+> *)?\*Hourly', '<@U0BNFGX2UAX> *Hourly 1h*')

def test_long_3h_fold_names_every_route():
    spec = importlib.util.spec_from_file_location('revise4_composer', ROOT / 'pm/heartbeat/heartbeat-compose.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = [f'route_{i:03d}_' + 'x' * 90 for i in range(80)]
    result = module.compact_report(['*Routes:*'] + ['• ' + n + ' — 1 ev; owner PM' for n in names], 3, cadence='3h')
    result = module.cap_lines(result, module.MAX_LINE_CHARS - module.LINE_NOTE_RESERVE)
    assert all(n in result for n in names)
    assert all(len(line) <= module.MAX_LINE_CHARS - module.LINE_NOTE_RESERVE for line in result.splitlines())

def test_overlap_has_forced_kill_backstop():
    source = (ROOT / 'ci/pm-admit-ci.sh').read_text()
    assert 'timeout --kill-after=5 "$OVERLAP_TIMEOUT" python3 -I' in source
