#!/usr/bin/env python3
"""Read workflow requirements from the canonical main classifier, never PR policy."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = "heydonna-app/heydonna-app"
REPO_ROOT = "/Users/rajiv/Downloads/projects/heydonna-app"


def requirements(result, head):
    """Only an exact-head JSON boolean false can remove the E2E requirement."""
    return {"head": head, "ci_required": True,
            "e2e_required": not (isinstance(result, dict)
                                  and result.get("head") == head
                                  and result.get("e2e_required") is False)}


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=120).stdout


def classify(number, head):
    """Pin both classifier files to one main SHA; missing/bad output requires E2E."""
    try:
        main = json.loads(command("gh", "api", f"repos/{REPO}/git/ref/heads/main"))["object"]["sha"]
        with tempfile.TemporaryDirectory(prefix="mop-classifier-") as directory:
            root = Path(directory)
            for name in ("change_scope.py", "change-scope-rules.json"):
                blob = json.loads(command("gh", "api",
                    f"repos/{REPO}/contents/scripts/ci/{name}?ref={main}"))
                (root / name).write_bytes(base64.b64decode(blob["content"]))
            result = json.loads(command(sys.executable, str(root / "change_scope.py"),
                "--rules", str(root / "change-scope-rules.json"),
                "--repo-root", REPO_ROOT, "--repo", REPO, "--pr", str(number),
                "--expected-head", head))
        return requirements(result, head)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return requirements(None, head)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    print(json.dumps(classify(args.pr, args.head), sort_keys=True))
