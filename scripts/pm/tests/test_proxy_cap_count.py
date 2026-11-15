import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/proxy-cap-count.py'
spec = importlib.util.spec_from_file_location('proxy_cap_count', MODULE)
proxy_cap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy_cap)


class ProxyCapCountTest(unittest.TestCase):
    def test_only_long_running_types_count_for_both_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roles = ['hd-planner', 'hd-builder', 'hd-proof-qa', 'pm-hotfix',
                     'pm-proof-qa', 'hd-investigator', 'superproofer',
                     'ci-repair-agent', 'pm-ops-tooling', 'pm-admission-reviewer']
            lines = []
            for index, role in enumerate(roles):
                name = f'px-run-{index}'
                (root / name).mkdir()
                (root / name / 'manifest.json').write_text(json.dumps({'type': role}))
                model = 'claude-sonnet-5-5' if index % 2 == 0 else 'gpt-6.1-terra'
                lines.append(f'/opt/homebrew/bin/claude -p --name {name} --model {model}')
            processes = '\n'.join(lines)
            self.assertEqual(proxy_cap.count_processes(processes, root), 7)
            self.assertEqual(proxy_cap.count_processes(processes, root, True), 4)

    def test_missing_registry_metadata_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                proxy_cap.count_processes('/bin/claude -p --name px-missing --model claude-x', Path(tmp))


if __name__ == '__main__':
    unittest.main()
