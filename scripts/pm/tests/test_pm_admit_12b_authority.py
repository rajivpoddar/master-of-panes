"""12b comment authority and production-wrapper conflict refusal tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
CI = ROOT / "scripts/pm/shared-assets/claude/scripts/ci"
GATE_PATH = CI / "pm-admit-12b.py"
spec = importlib.util.spec_from_file_location("pm_admit_12b_authority", GATE_PATH)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
HEAD = "a" * 40
OLD = "b" * 40


def _evaluate(author):
    comment = {"body": f"## PM 12b: PASS at {OLD}", "user": {"login": author},
               "created_at": "2026-11-12T00:00:00Z", "id": 1}
    original_api = gate.api
    try:
        gate.api = lambda repo, endpoint, pages=False: [comment] if "issues/123/comments" in endpoint else {}
        gate.authorized_reviewer_login = lambda: "pm-reviewer"
        gate.exempt = lambda *args: False
        gate.main_merges_only = lambda *args: True
        return gate.evaluate("owner/repo", 123, HEAD)
    finally:
        gate.api = original_api


def test_pass_carry_forward_requires_authenticated_pm_author_and_reviewed_sha():
    assert _evaluate("untrusted-user")[0] is False
    assert _evaluate("pm-reviewer")[0] is True


def test_production_wrapper_refuses_conflicting_main_merge(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(f'''#!{sys.executable}
import json, sys
args = sys.argv[1:]
joined = " ".join(args)
if args[:2] == ["pr", "view"] and "--json headRefOid" in joined:
    print("{HEAD}")
elif args[:2] == ["pr", "view"] and "--json state" in joined:
    print("OPEN")
elif args[:2] == ["api", "user"]:
    print("pm-reviewer")
elif args[:1] == ["api"] and "issues/11173/comments?per_page=100" in joined:
    print(json.dumps([{{"body":"## PM 12b: PASS at {OLD}","user":{{"login":"pm-reviewer"}},"created_at":"2026-11-12T00:00:00Z","id":1}}]))
elif len(args) > 1 and args[0] == "api" and "compare/" in args[1]:
    print(json.dumps({{"status":"ahead"}}))
else:
    sys.exit(1)
''')
    gh.chmod(0o755)
    git = bindir / "git"
    git.write_text(f'''#!{sys.executable}
import sys
args = sys.argv[1:]
if args[:2] == ["-C", "/Users/rajiv/Downloads/projects/heydonna-app"]:
    args = args[2:]
if args[:2] == ["rev-parse", "--verify"]:
    print(args[2].removesuffix("^{{commit}}"))
elif args[:2] == ["merge-base", "--is-ancestor"]:
    sys.exit(0)
elif args[:1] == ["rev-list"]:
    print("c" * 40)
elif args[:3] == ["show", "-s", "--format=%P"]:
    print("{OLD} " + "d" * 40)
elif args[:2] == ["merge-tree", "--write-tree"]:
    print("CONFLICT (content): merge conflict in src/example.ts", file=sys.stderr)
    sys.exit(1)
else:
    sys.exit(1)
''')
    git.chmod(0o755)
    (bindir / "python3").symlink_to(sys.executable)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", PM_ADMIT_RETRY_SLEEP="0")
    wrapper = CI / "pm-admit-ci.sh"
    result = subprocess.run(["bash", str(wrapper), "--check-12b-only", "11173"],
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "REFUSE: 12b_missing_or_stale" in result.stdout
    assert "ADMITTED" not in result.stdout
