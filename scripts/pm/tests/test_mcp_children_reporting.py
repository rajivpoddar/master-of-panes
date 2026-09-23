from __future__ import annotations

import importlib.util
import json
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
SCRIPT = REPO_ROOT / "scripts" / "pm" / "shared-assets" / "claude" / "scripts" / "heartbeat-session-age-clear.py"


def load_module():
    spec = importlib.util.spec_from_file_location("heartbeat_session_age_clear", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE_PS = """\
/Users/rajiv/.nvm/versions/node/v22.13.1/bin/node /Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/dist/mcp.js
/Users/rajiv/.nvm/versions/node/v22.13.1/bin/node /Users/rajiv/.claude/plugins/cache/rajiv-plugins/master-of-panes/current/dist/mcp.js
/usr/local/bin/claude
"""


class McpChildrenReportingTest(unittest.TestCase):
    def test_counts_only_mcp_children(self):
        module = load_module()
        real_run = module.subprocess.run
        try:
            module.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=FAKE_PS)  # type: ignore[method-assign]
            self.assertEqual(module._mcp_children_running(), 2)
        finally:
            module.subprocess.run = real_run  # type: ignore[method-assign]

    def test_fail_open_on_collection_error(self):
        module = load_module()
        real_run = module.subprocess.run
        try:
            def boom(*args, **kwargs):
                raise OSError("no ps")
            module.subprocess.run = boom  # type: ignore[method-assign]
            self.assertIsNone(module._mcp_children_running())
        finally:
            module.subprocess.run = real_run  # type: ignore[method-assign]

    def test_producer_emits_top_level_field(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"mcp_children_running": _mcp_children_running()', text)


if __name__ == "__main__":
    unittest.main()
