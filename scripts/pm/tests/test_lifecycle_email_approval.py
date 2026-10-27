"""The production selector send_allowed gate requires every approved template."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/growth/lifecycle-drip.py'


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
