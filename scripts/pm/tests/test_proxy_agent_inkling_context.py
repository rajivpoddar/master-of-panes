"""Inkling preflight: an oversized assembled prompt refuses without starting Claude."""
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).parents[1] / 'shared-assets/claude/scripts/proxy-agent.sh'


@pytest.mark.parametrize('brief_size,expected_rc,receipt', [
    (16, 0, 'INKLING_CONTEXT_OK'),
    (240_000, 2, 'inkling_context_budget'),
])
def test_inkling_prompt_budget_accounts_for_input_and_reserved_output(tmp_path, brief_size, expected_rc, receipt):
    source = SCRIPT.read_text()
    definition = source.split('proxy_agent_inkling_budget() {', 1)[1].split('\n}\n', 1)[0]
    agent, prompt = tmp_path / 'agent.md', tmp_path / 'prompt.md'
    agent.write_bytes(b'A' * 1024)
    prompt.write_bytes(b'B' * brief_size)
    command = 'proxy_agent_inkling_budget() {' + definition + '\n}\nproxy_agent_inkling_budget 262144 32000 "$@"'
    result = subprocess.run(['/bin/bash', '-euc', command, 'fixture', str(agent), str(prompt)], capture_output=True, text=True)
    assert result.returncode == expected_rc
    assert receipt in result.stderr
    assert 'output_reserve=32000 max_model_len=262144' in result.stderr
