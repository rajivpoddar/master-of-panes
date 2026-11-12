"""The parent slot and the companion may use different linked worktrees."""
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / "shared-assets/claude/hooks/validate-codex-review-agent-result.py"
spec = importlib.util.spec_from_file_location("review_validator", SOURCE)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def test_checkout_binding_preserves_strict_head_check(tmp_path, capsys):
    marker = tmp_path / "marker"
    primary = "/Users/example/heydonna-app-3001"
    worktree = "/Users/example/worktree"
    live = "a" * 40
    payload = {"cwd": primary, "tool_input": {"subagent_type": "codex-code-reviewer", "prompt": "PR #123"},
               "tool_response": {"agentType": "codex-code-reviewer", "text": str(marker)}}
    marker.write_text(f"MARKER_PROVENANCE: codex-review-companion\nCHECKOUT: {worktree}\nHEAD_SHA: {live}\n")
    heads = {primary: "b" * 40, worktree: live}
    def command(args, **kwargs):
        if args[0] == "git":
            return "/repo/.git\n" if "--git-common-dir" in args else heads[args[2]] + "\n"
        assert kwargs["cwd"] == worktree
        return live + "\n"
    with patch.object(validator, "marker_target", return_value=(marker, "123")), \
         patch.object(validator.subprocess, "check_output", side_effect=command), \
         patch("sys.stdin", io.StringIO(json.dumps(payload))):
        validator.main()
    assert capsys.readouterr().out == ""
    heads[worktree] = "c" * 40
    with patch.object(validator, "marker_target", return_value=(marker, "123")), \
         patch.object(validator.subprocess, "check_output", side_effect=command), \
         patch("sys.stdin", io.StringIO(json.dumps(payload))):
        validator.main()
    assert "CODEX_REVIEW_MARKER_HEAD_MISMATCH" in capsys.readouterr().out


def test_unrelated_checkout_is_rejected(tmp_path, capsys):
    marker = tmp_path / "marker"
    marker.write_text("MARKER_PROVENANCE: codex-review-companion\nCHECKOUT: /other/checkout\nHEAD_SHA: " + "a" * 40)
    payload = {"cwd": "/Users/example/heydonna-app-3001", "tool_input": {"subagent_type": "codex-code-reviewer", "prompt": "PR #123"},
               "tool_response": {"agentType": "codex-code-reviewer", "text": str(marker)}}
    def command(args, **kwargs):
        return ("/other/.git" if args[2] == "/other/checkout" else "/repo/.git") + "\n"
    with patch.object(validator, "marker_target", return_value=(marker, "123")), \
         patch.object(validator.subprocess, "check_output", side_effect=command), \
         patch("sys.stdin", io.StringIO(json.dumps(payload))):
        validator.main()
    assert "CODEX_REVIEW_HEAD_UNVERIFIED" in capsys.readouterr().out
