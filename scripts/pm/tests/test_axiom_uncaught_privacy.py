import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "shared-assets/claude/scripts/axiom-activity-report.py"
FIXTURE = ROOT / "tests/fixtures/axiom-uncaught-sanitized.json"


def test_uncaught_fixture_redaction_and_nested_partial():
    spec = importlib.util.spec_from_file_location("axiom_activity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = json.loads(FIXTURE.read_text())
    assert result["matches"][0]["data"]["meta"]
    assert result["status"]["isPartial"] is False
    # The retained response was complete; this modified copy is synthetic.
    result["status"]["isPartial"] = True
    result["matches"][0]["data"].update({"userId": "private-user", "fileId": "private-file"})
    result["matches"][0]["data"]["meta"].update({"errorStack": "private-stack", "errorMessage": "private-message"})
    output = io.StringIO()
    with patch.object(sys, "argv", ["report", "--hours", "1", "--uncaught-errors"]), \
         patch.object(module, "load_token", return_value="unused"), \
         patch.object(module, "run_apl_query", return_value=result), \
         contextlib.redirect_stdout(output):
        try:
            module.main()
        except SystemExit as error:
            assert error.code == 1
        else:
            raise AssertionError("partial response must exit nonzero")
    text = output.getvalue()
    assert json.loads(text)["isPartial"] is True
    for secret in ("private-user", "private-file", "private-stack", "private-message"):
        assert secret not in text
