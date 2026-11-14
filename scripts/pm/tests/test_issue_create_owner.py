"""Static regression cases for the single-chain permit owner (issue-create-owner.py).

Governing defect: the permit lifecycle was spread over parallel PreToolUse hooks (pre-audit wrote a
pending file, the ledger hook validated, a "last" finalizer consumed the permit BEFORE gh ran). No hook
order is guaranteed, each hook re-read the body at its own moment, and rtk /
rewrite-github-priority-label.py race on updatedInput. A consumed permit with no issue, or an issue with
a live permit, were both reachable.

Contract proven here (one owner, one process):
  1. the exact --body/--body-file bytes are held in ONE in-memory buffer that reaches every gate IN
     MEMORY: the audit gate on stdin, the ledger gates in MOP_ISSUE_CREATE_BODY_B64 with `--body-file -`.
     No file, scratch copy or temp directory exists at any point;
  2. the existing audit (source/verdict/permit) and ledger/tracker gates run sequentially on them;
  3. the permit binds the EXACT raw digest (trailing newlines and NUL bytes included);
  4. a rejecting gate writes nothing: no lock content, no in-flight record, no obligation, no state,
     no draft copy, no Slack cache, no log, no seal, no capture file;
  5. check + binding + the durable in-flight record happen under the ONE shared permit lock
     (`<permit>.lock`: the lock the mint and restore take), which is never nested or held across gh;
  6. gh gets the identical bytes on stdin; only a verified issue URL (gh output AND a GitHub readback
     of repo/number/title/body) consumes the permit; everything else stays reconciliation-only;
  7. the in-flight record is released only on a definite spawn failure, never by restore;
  8. ALL compound/wrapped raw creates are denied upfront; a simple raw create is parsed by a
     byte-preserving parser (every gh flag spelling, any quoting) that fails closed on anything
     ambiguous or unreadable; a simple raw create holding a permit is denied before any permit write;
  9. the guard DISCOVERS every PreToolUse Bash rewrite handler in the settings in force (not a fixed
     list) and dry-runs each on the owner invocation, letting it through only if none would rewrite,
     deny or block it (no live hook is modified or overwritten);
 10. existing consumed/state records (source 33762) are never touched.
Not covered by design: a hostile process running as the same user.

Hermetic: temp state/permit/consumed/inflight roots, a stub `gh` that records argv and stdin, stub
ledger hooks, no live Slack, GitHub or pm-ops. Committed but NOT executed under the MoP no-test-proof
directive.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
HOOKS = ROOT / "scripts/pm/shared-assets/claude/hooks"
OWNER = HOOKS / "issue-create-owner.py"
HELPER = HOOKS / "explore-issue-latch-guard.sh"
MANIFEST = ROOT / "scripts/pm/shared-assets/manifest.json"

CHANNEL = "D0CFYAP3ZK3"
THREAD = "1794136866.155859"
KEY = f"{CHANNEL}__{THREAD.replace('.', '_')}"
OBLIGATION_ID = "33762"
TITLE = "Owner test: filing with a valid permit"
BODY = f"Source: {CHANNEL}:{THREAD}\nDescription: owner chain body.\n"
URL = "https://github.com/Scribie/heydonna-app/issues/4242"
O = "/Users/rajiv/.claude/hooks/issue-create-owner.py"
G = 'gh issue create --title "t" --body-file /tmp/b.md'

_spec = importlib.util.spec_from_file_location("issue_create_owner", OWNER)
owner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(owner)  # type: ignore[union-attr]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _body_digest(body: str) -> str:
    """The permit binds the EXACT raw digest of the body bytes (nothing stripped)."""
    return _sha(body)


def _legacy_digest(body: str) -> str:
    """The retired `$(cat file)` digest: trailing newlines stripped. No longer accepted."""
    return _sha(body.rstrip("\n"))


FAKE_GH = """#!/bin/sh
d="$FAKE_GH_DIR"
if [ "$1" = "api" ]; then
  printf '%s\\n' "$@" > "$d/api-argv"
  echo call >> "$d/api-calls"
  case "${FAKE_API_MODE:-ok}" in
    ok) cat "$d/api.json" ;;
    mismatch) cat "$d/api-mismatch.json" ;;
    missing) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  esac
  exit 0
fi
printf '%s\\n' "$@" > "$d/argv"
cat > "$d/stdin"
echo call >> "$d/calls"
case "${FAKE_GH_MODE:-ok}" in
  ok) echo "https://github.com/Scribie/heydonna-app/issues/4242" ;;
  fail) echo "gh: boom" >&2; exit 1 ;;
  nourl) echo "created it, trust me" ;;
  twourls) echo "https://github.com/Scribie/heydonna-app/issues/1"; echo "https://github.com/Scribie/heydonna-app/issues/2" ;;
esac
"""

FAKE_LEDGER = """#!/bin/sh
cat > "$FAKE_LEDGER_DIR/payload"
printf '%s\\n' "$0" >> "$FAKE_LEDGER_DIR/order"
printf '%s' "$MOP_ISSUE_CREATE_BODY_B64" | python3 -c 'import base64, sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' > "$FAKE_LEDGER_DIR/seen-body"
printf '%s' "$MOP_ISSUE_CREATE_STAGE" > "$FAKE_LEDGER_DIR/seen-stage"
case "${FAKE_LEDGER_MODE:-ok}" in
  block) echo '{"decision":"block","message":"BLOCKED: fake ledger says no"}' ;;
  mutate) printf 'TAMPER' >> "$FAKE_MUTATE_FILE" ;;
esac
exit 0
"""


LOCK_PROBE = """#!/usr/bin/env python3
import fcntl
import sys

handle = open(sys.argv[1], "a")
try:
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("free")
except OSError:
    print("held")
"""


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.permits = tmp_path / "permits"
        self.consumed = tmp_path / "consumed"
        self.inflight = tmp_path / "inflight"
        self.state = tmp_path / "state"
        self.work = tmp_path / "work"
        self.gh_dir = tmp_path / "gh"
        self.ledger_dir = tmp_path / "ledger"
        self.bin = tmp_path / "bin"
        self.tmp = tmp_path / "tmp"
        for directory in (self.gh_dir, self.ledger_dir, self.bin, self.tmp):
            directory.mkdir()
        self.permit_lock = self.permits / f"{KEY}.permit.json.lock"
        self.gh = self._script(self.bin / "gh", FAKE_GH)
        self.ledger_a = self._script(tmp_path / "ledger-a.sh", FAKE_LEDGER)
        self.ledger_b = self._script(tmp_path / "ledger-b.sh", FAKE_LEDGER)
        self.body_file = tmp_path / "issue_body.md"

    @staticmethod
    def _script(path: Path, text: str) -> Path:
        path.write_text(text)
        path.chmod(0o755)
        return path

    def env(self, **extra: str) -> dict[str, str]:
        env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            ISSUE_CREATE_OWNER_GH_BIN=str(self.gh),
            ISSUE_CREATE_OWNER_LEDGER_HOOKS=str(self.ledger_a),
            ISSUE_CREATE_OWNER_WORK_DIR=str(self.work),
            ISSUE_CREATE_OWNER_LOCK_WAIT="1",
            ISSUE_CREATE_OWNER_READBACK_DELAY="0",
            TMPDIR=str(self.tmp),
            ISSUE_CREATE_OWNER_POST_SWEEP_HOOK="",
            ISSUE_CREATE_OWNER_POST_LINK_HOOK="",
            EXPLORE_ISSUE_LATCH_GUARD=str(HELPER),
            EXPLORE_ISSUE_STATE_DIR=str(self.state),
            EXPLORE_ISSUE_PERMIT_DIR=str(self.permits),
            EXPLORE_ISSUE_CONSUMED_PERMIT_DIR=str(self.consumed),
            EXPLORE_ISSUE_RECONCILED_PERMIT_DIR=str(self.root / "reconciled"),
            EXPLORE_ISSUE_INFLIGHT_DIR=str(self.inflight),
            EXPLORE_ISSUE_VERDICT_DIR=str(self.root / "verdicts"),
            EXPLORE_ISSUE_SLACK_CACHE_DIR=str(self.root / "slack"),
            EXPLORE_ISSUE_ENV_LOCAL=str(self.root / "no-credentials.env"),
            EXPLORE_ISSUE_FALLBACK_LOG=str(self.root / "fallback.log"),
            EXPLORE_ISSUE_PM_OPS_CLI="/usr/bin/true",
            SLACK_BOT_TOKEN="",
            FAKE_GH_DIR=str(self.gh_dir),
            FAKE_LEDGER_DIR=str(self.ledger_dir),
        )
        env.pop("EXPLORE_ISSUE_SOURCE_MESSAGE_TS", None)
        env.update(extra)
        return env

    def write_body(self, body: str = BODY) -> None:
        self.body_file.write_bytes(body.encode())

    def write_permit(self, body: str = BODY, title: str = TITLE) -> Path:
        self.permits.mkdir(exist_ok=True)
        path = self.permits / f"{KEY}.permit.json"
        path.write_text(json.dumps({
            "channel": CHANNEL,
            "thread_ts": THREAD,
            "source_message_ts": THREAD,
            "body_sha256": _body_digest(body),
            "title_digest": _sha(title),
            "obligation_id": OBLIGATION_ID,
            "minted_at": "2026-11-14T10:00:00Z",
        }))
        return path

    def write_api(self, body: str = BODY, title: str = TITLE, number: int = 4242,
                  repo: str = "Scribie/heydonna-app", name: str = "api.json") -> None:
        (self.gh_dir / name).write_text(json.dumps({
            "number": number,
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{repo}",
            "title": title,
            "body": body,
            "state": "open",
        }))

    def api_calls(self) -> int:
        calls = self.gh_dir / "api-calls"
        return len(calls.read_text().splitlines()) if calls.exists() else 0

    def write_verdict(self, text: str) -> None:
        verdicts = self.root / "verdicts"
        verdicts.mkdir(exist_ok=True)
        (verdicts / f"{KEY}.verdict.txt").write_text(text)

    def run_owner(self, *flags: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        args = list(flags) or ["--title", TITLE, "--body-file", str(self.body_file)]
        return subprocess.run(
            [sys.executable, str(OWNER), "create", *args],
            capture_output=True, text=True, env=env or self.env(), cwd=str(self.root), timeout=180,
        )

    def gh_calls(self) -> int:
        calls = self.gh_dir / "calls"
        return len(calls.read_text().splitlines()) if calls.exists() else 0

    def inflight_files(self) -> list[Path]:
        return sorted(self.inflight.glob("*.inflight.json")) if self.inflight.exists() else []

    def consumed_files(self) -> list[Path]:
        return sorted(self.consumed.glob(f"{KEY}.permit.consumed.*.json")) if self.consumed.exists() else []

    def nothing_written(self) -> None:
        """A rejected gate writes nothing: no in-flight, consumed, pending, state, capture, scratch or log."""
        assert (self.permits / f"{KEY}.permit.json").exists(), "permit must be intact"
        assert not self.inflight_files(), "no in-flight record may exist"
        assert not self.consumed_files(), "nothing may be consumed"
        assert not (self.root / "pending").exists()
        assert not self.work.exists(), "no capture file, seal or log directory may be created"
        assert not list(self.tmp.iterdir()), "the ledger scratch copy must be gone"
        assert not self.state.exists(), "no obligation/state record may be written by a gate"
        assert not (self.root / "slack").exists(), "no Slack cache may be written by a gate"
        assert not (self.root / "fallback.log").exists(), "no diagnostic log may be written by a gate"
        assert self.gh_calls() == 0, "gh must not have been started"
        if self.permit_lock.exists():
            assert not self.permit_lock.read_text().strip(), "the permit lock carries no binding yet"


@pytest.fixture()
def world(tmp_path: Path) -> World:
    value = World(tmp_path)
    value.write_body()
    value.write_permit()
    value.write_api()
    return value


# ---------------------------------------------------------------------------
# 1 + 2 + 5 + 6: capture once, sequential gates on the same bytes, stdin, consume on verified URL
# ---------------------------------------------------------------------------

def test_happy_path_feeds_identical_captured_bytes_and_consumes_on_verified_url(world: World):
    original = world.body_file.read_bytes()
    # The ledger gate rewrites the ORIGINAL file while it runs: gh must still get the captured bytes.
    env = world.env(FAKE_MUTATE_FILE=str(world.body_file), FAKE_LEDGER_MODE="mutate")
    result = world.run_owner(env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == URL
    assert "FILED" in result.stderr and URL in result.stderr
    assert world.body_file.read_bytes() == original + b"TAMPER", "test premise: the original file was changed"
    assert (world.gh_dir / "stdin").read_bytes() == original, "gh must receive the captured bytes on stdin"
    argv = (world.gh_dir / "argv").read_text().splitlines()
    assert argv[:2] == ["issue", "create"]
    assert argv[argv.index("--body-file") + 1] == "-", "gh must read the body from stdin"
    assert argv[argv.index("--title") + 1] == TITLE
    assert world.gh_calls() == 1
    # The ledger gate saw the exact buffer in memory: `--body-file -`, never a path, never a file.
    payload = json.loads((world.ledger_dir / "payload").read_text())
    command = payload["tool_input"]["command"]
    assert command.endswith("--body-file -") and str(world.body_file) not in command
    assert (world.ledger_dir / "seen-body").read_bytes() == original, "ledger gate must see the exact buffer"
    assert (world.ledger_dir / "seen-stage").read_text() == "audit"
    assert not list(world.tmp.iterdir()), "no scratch file or directory exists, even transiently"
    # Permit consumed once, URL recorded; in-flight released after the durable consumed record.
    assert not (world.permits / f"{KEY}.permit.json").exists()
    consumed = world.consumed_files()
    assert len(consumed) == 1
    record = json.loads(consumed[0].read_text())
    assert record["issue_url"] == URL and record["issue_number"] == 4242
    assert record["consumed_by"] == "issue-create-owner"
    assert record["obligation_id"] == OBLIGATION_ID
    assert record["readback"] == "verified"
    assert not world.inflight_files()
    # The returned issue was read back from GitHub by repo and number before the permit was consumed.
    assert (world.gh_dir / "api-argv").read_text().splitlines() == ["api", "repos/Scribie/heydonna-app/issues/4242"]


def test_trailing_newlines_and_nul_are_part_of_the_exact_digest_and_reach_gh_unchanged(world: World):
    exact = f"Source: {CHANNEL}:{THREAD}\nBody with a NUL \x00 inside.\n\n\n".encode()
    world.body_file.write_bytes(exact)
    world.write_permit(exact.decode())
    world.write_api(exact.decode())
    assert _legacy_digest(exact.decode()) != _body_digest(exact.decode()), "test premise"
    result = world.run_owner()
    assert result.returncode == 0, result.stderr
    assert (world.gh_dir / "stdin").read_bytes() == exact
    assert (world.ledger_dir / "seen-body").read_bytes() == exact
    record = json.loads(world.consumed_files()[0].read_text())
    assert record["body_sha256"] == hashlib.sha256(exact).hexdigest()


def test_a_permit_minted_with_the_retired_stripped_digest_is_refused(world: World):
    legacy = world.write_permit()
    data = json.loads(legacy.read_text())
    data["body_sha256"] = _legacy_digest(BODY)
    legacy.write_text(json.dumps(data))
    result = world.run_owner()
    assert result.returncode == 2, result.stderr
    assert "REFUSED NO_GH_START" in result.stderr
    world.nothing_written()


def test_priority_label_alias_is_canonicalised_by_the_owner(world: World):
    result = world.run_owner(
        "--title", TITLE, "--body-file", str(world.body_file), "--label", "priority:p1,status:todo",
    )
    assert result.returncode == 0, result.stderr
    argv = (world.gh_dir / "argv").read_text().splitlines()
    assert argv[argv.index("--label") + 1] == "P1,status:todo"


def test_inline_body_is_captured_exactly(world: World):
    inline = f"Source: {CHANNEL}:{THREAD}\nInline body, no file.\n"
    world.write_permit(inline)
    world.write_api(inline)
    result = world.run_owner("--title", TITLE, "--body", inline)
    assert result.returncode == 0, result.stderr
    assert (world.gh_dir / "stdin").read_bytes() == inline.encode()


def test_ledger_hooks_run_sequentially_on_the_same_payload(world: World):
    env = world.env(ISSUE_CREATE_OWNER_LEDGER_HOOKS=os.pathsep.join([str(world.ledger_a), str(world.ledger_b)]))
    result = world.run_owner(env=env)
    assert result.returncode == 0, result.stderr
    order = (world.ledger_dir / "order").read_text().splitlines()
    assert order == [str(world.ledger_a), str(world.ledger_b)]


# ---------------------------------------------------------------------------
# 4: rejected gates never write a lock, an in-flight record or permit state
# ---------------------------------------------------------------------------

def test_rejected_audit_gate_writes_nothing_and_never_starts_gh(world: World):
    fix_body = f"Status: FIX\nSource: {CHANNEL}:{THREAD}\nCause: CODE_DEFECT\nDescription: no verdict file exists.\n"
    world.write_body(fix_body)
    world.write_permit(fix_body)
    result = world.run_owner()
    assert result.returncode == 2, result.stderr
    assert "REFUSED NO_GH_START" in result.stderr and "ABSENT" in result.stderr
    world.nothing_written()


def test_rejected_ledger_gate_writes_nothing_and_never_starts_gh(world: World):
    result = world.run_owner(env=world.env(FAKE_LEDGER_MODE="block"))
    assert result.returncode == 2, result.stderr
    assert "REFUSED NO_GH_START" in result.stderr and "fake ledger says no" in result.stderr
    world.nothing_written()


def test_rejected_customer_origin_gate_writes_no_obligation_or_state(world: World):
    """The audit's durable-block path (obligation upsert + OPEN state) is suppressed for this chain."""
    (world.permits / f"{KEY}.permit.json").unlink()
    recorder = world._script(world.root / "pm-ops-recorder.sh", '#!/bin/sh\necho "$@" >> "$PM_OPS_RECORD"\necho 4242\n')
    record = world.root / "pm-ops-calls.log"
    env = world.env(
        EXPLORE_ISSUE_CUSTOMER_CHANNELS_REGEX=f"^{CHANNEL}$",
        EXPLORE_ISSUE_PM_OPS_CLI=str(recorder),
        PM_OPS_RECORD=str(record),
    )
    result = world.run_owner(env=env)
    assert result.returncode == 2, result.stderr
    assert "has no valid READY_TO_FILE permit" in result.stderr, "the BLOCK decision is unchanged"
    assert not record.exists(), "no obligation upsert"
    assert not world.state.exists(), "no OPEN state record"
    assert world.gh_calls() == 0 and not world.inflight_files() and not world.consumed_files()
    assert not world.work.exists() and not list(world.tmp.iterdir())


def test_missing_required_gate_fails_closed(world: World):
    env = world.env(ISSUE_CREATE_OWNER_LEDGER_HOOKS=str(world.root / "no-such-ledger.sh"))
    result = world.run_owner(env=env)
    assert result.returncode == 2, result.stderr
    assert "gate is missing" in result.stderr
    world.nothing_written()


def test_a_permit_that_does_not_match_the_captured_bytes_is_refused(world: World):
    world.write_permit("different body than the file holds\n")
    result = world.run_owner()
    assert result.returncode == 2, result.stderr
    assert "REFUSED NO_GH_START" in result.stderr
    world.nothing_written()


def test_no_permit_means_the_owner_refuses(world: World):
    (world.permits / f"{KEY}.permit.json").unlink()
    result = world.run_owner()
    assert result.returncode == 2, result.stderr
    assert "permit" in result.stderr
    assert world.gh_calls() == 0 and not world.inflight_files()


@pytest.mark.parametrize("flags", [
    ["--title", TITLE, "--body-file", "BODY", "--web"],
    ["--title", TITLE, "--body-file", "BODY", "--body", "also inline"],
    ["--title", TITLE, "--body-file", "-"],
    ["--body-file", "BODY"],
    ["--title", TITLE],
    ["--title", TITLE, "--body", "   \n"],
    ["--title", TITLE, "--body-file", "BODY", "stray-positional"],
])
def test_bad_flags_are_refused_before_any_write(world: World, flags):
    resolved = [str(world.body_file) if item == "BODY" else item for item in flags]
    result = world.run_owner(*resolved)
    assert result.returncode == 2, result.stderr
    assert "REFUSED NO_GH_START" in result.stderr
    world.nothing_written()


# ---------------------------------------------------------------------------
# 5 + 6 + 7: durable in-flight before gh; UNKNOWN is reconciliation-only; release only on no-start
# ---------------------------------------------------------------------------

def test_inflight_record_is_durable_before_gh_and_binds_source_invocation_digest(world: World):
    """gh sees the in-flight record already on disk, bound to source / invocation / digest."""
    gh_probe = world.bin / "gh"
    world._script(gh_probe, FAKE_GH.replace(
        'echo call >> "$d/calls"',
        'echo call >> "$d/calls"\ncp "$FAKE_INFLIGHT_DIR/$FAKE_KEY.inflight.json" "$d/inflight-during-gh"',
    ))
    env = world.env(FAKE_INFLIGHT_DIR=str(world.inflight), FAKE_KEY=KEY)
    result = world.run_owner(env=env)
    assert result.returncode == 0, result.stderr
    during = json.loads((world.gh_dir / "inflight-during-gh").read_text())
    assert during["state"] == "IN_FLIGHT"
    assert during["key"] == KEY
    assert (during["channel"], during["thread_ts"]) == (CHANNEL, THREAD)
    assert during["body_sha256"] == hashlib.sha256(BODY.encode()).hexdigest(), "the exact raw digest"
    assert "permit_body_sha256" not in during and "capture_path" not in during
    assert during["title_digest"] == _sha(TITLE)
    assert len(during["invocation"]) == 32 and during["argv_digest"]
    # The binding was written under the ONE shared permit lock (`<permit>.lock`), not a private lock.
    lock = json.loads(world.permit_lock.read_text())
    assert lock["invocation"] == during["invocation"] and lock["key"] == KEY
    assert lock["body_sha256"] == during["body_sha256"] and lock["title_digest"] == during["title_digest"]
    assert not list(world.inflight.glob("*.owner.lock")), "no second, private owner lock"


def test_gh_failure_leaves_permit_unconsumed_and_source_reconciliation_only(world: World):
    result = world.run_owner(env=world.env(FAKE_GH_MODE="fail"))
    assert result.returncode == 5, result.stderr
    assert "RECONCILIATION-ONLY" in result.stderr
    assert (world.permits / f"{KEY}.permit.json").exists(), "an UNKNOWN create must not silently burn the permit"
    assert not world.consumed_files()
    records = world.inflight_files()
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["state"] == "RETURNED" and record["gh_rc"] == 1
    # Re-entry is refused by the audit gate (status 3) and never reaches gh again.
    again = world.run_owner(env=world.env(FAKE_GH_MODE="ok"))
    assert again.returncode == 2, again.stderr
    assert "already consumed" in again.stderr
    assert world.gh_calls() == 1


@pytest.mark.parametrize("mode", ["nourl", "twourls"])
def test_gh_exit_zero_without_exactly_one_verified_url_is_unknown(world: World, mode: str):
    result = world.run_owner(env=world.env(FAKE_GH_MODE=mode))
    assert result.returncode == 5, result.stderr
    assert (world.permits / f"{KEY}.permit.json").exists()
    assert not world.consumed_files()
    assert len(world.inflight_files()) == 1


def _assert_unknown_after_gh_returned(world: World, readback_prefix: str) -> dict:
    assert (world.permits / f"{KEY}.permit.json").exists(), "an unverified create must not burn the permit"
    assert not world.consumed_files()
    records = world.inflight_files()
    assert len(records) == 1, "the in-flight record stays: reconciliation-only"
    record = json.loads(records[0].read_text())
    assert record["issue_url_candidate"] == URL and "issue_url" not in record
    assert record["readback"].startswith(readback_prefix), record["readback"]
    return record


def test_readback_unavailable_keeps_reconciliation_only(world: World):
    result = world.run_owner(env=world.env(FAKE_API_MODE="missing"))
    assert result.returncode == 5, result.stderr
    assert "RECONCILIATION-ONLY" in result.stderr and URL in result.stderr
    assert world.api_calls() == 3, "bounded retries for read-after-write lag"
    _assert_unknown_after_gh_returned(world, "unavailable")
    again = world.run_owner(env=world.env(FAKE_API_MODE="ok"))
    assert again.returncode == 2 and world.gh_calls() == 1, "never create again for an UNKNOWN source"


@pytest.mark.parametrize("field,write", [
    ("title", lambda w: w.write_api(title="A different title", name="api-mismatch.json")),
    ("body", lambda w: w.write_api(body="Some other body entirely\n", name="api-mismatch.json")),
    ("number", lambda w: w.write_api(number=7, name="api-mismatch.json")),
    ("repository_url", lambda w: w.write_api(repo="evil/else", name="api-mismatch.json")),
])
def test_readback_identity_mismatch_keeps_reconciliation_only(world: World, field, write):
    write(world)
    result = world.run_owner(env=world.env(FAKE_API_MODE="mismatch"))
    assert result.returncode == 5, result.stderr
    assert "did not verify" in result.stderr
    record = _assert_unknown_after_gh_returned(world, "mismatch")
    assert field in record["readback"] or field == "repository_url"


def test_readback_that_is_a_pull_request_is_a_mismatch(world: World):
    data = json.loads((world.gh_dir / "api.json").read_text())
    data["pull_request"] = {"url": "x"}
    (world.gh_dir / "api-mismatch.json").write_text(json.dumps(data))
    result = world.run_owner(env=world.env(FAKE_API_MODE="mismatch"))
    assert result.returncode == 5, result.stderr
    _assert_unknown_after_gh_returned(world, "mismatch: the number is a pull request")


def test_readback_tolerates_crlf_and_surrounding_whitespace_only(world: World):
    world.write_api(body="\n" + BODY.replace("\n", "\r\n") + "\n\n")
    result = world.run_owner()
    assert result.returncode == 0, result.stderr
    assert world.consumed_files()


def test_compare_issue_requires_the_requested_repo(world: World):
    spec = owner.Spec()
    spec.title, spec.repo = TITLE, "Scribie/heydonna-app"
    good = json.loads((world.gh_dir / "api.json").read_text())
    assert owner.compare_issue(good, URL, spec, BODY.encode()) == (True, "verified")
    spec.repo = "heydonna-app/heydonna-app"
    ok, detail = owner.compare_issue(good, URL, spec, BODY.encode())
    assert not ok and "requested repo" in detail


def test_url_for_a_foreign_repo_is_not_verified(world: World, monkeypatch):
    monkeypatch.setenv("ISSUE_CREATE_OWNER_REPO_RE", r"someone/else")
    spec = owner.Spec()
    assert owner.verified_url(0, URL.encode(), spec) is None


def test_verified_url_requires_zero_exit_and_a_single_canonical_line():
    spec = owner.Spec()
    assert owner.verified_url(0, (URL + "\n").encode(), spec) == URL
    assert owner.verified_url(1, (URL + "\n").encode(), spec) is None
    assert owner.verified_url(0, b"", spec) is None
    assert owner.verified_url(0, f"warning\n{URL}\n".encode(), spec) == URL
    assert owner.verified_url(0, f"{URL}\ntrailing text\n".encode(), spec) is None
    assert owner.verified_url(0, f"{URL}\n{URL}\n".encode(), spec) == URL
    assert owner.verified_url(0, (URL.replace("4242", "1") + "\n" + URL + "\n").encode(), spec) is None


def test_spawn_failure_releases_the_inflight_record_and_leaves_the_permit_intact(world: World):
    env = world.env(ISSUE_CREATE_OWNER_GH_BIN=str(world.root / "no-such-gh"))
    result = world.run_owner(env=env)
    assert result.returncode == 4, result.stderr
    assert "NO_GH_START" in result.stderr and "released" in result.stderr
    assert (world.permits / f"{KEY}.permit.json").exists()
    assert not world.inflight_files() and not world.consumed_files()
    # Definitely no gh start: the same permit is usable again.
    retry = world.run_owner()
    assert retry.returncode == 0, retry.stderr
    assert world.gh_calls() == 1


def test_release_is_only_reachable_from_the_spawn_failure_path():
    source = OWNER.read_text()
    assert source.count("release_inflight(") == 2, "one definition and exactly one call site"
    spawn = source.index("[gh_bin] + gh_args")
    call = source.index("release_inflight(inflight_path, invocation, permit_file, lock_wait)")
    except_clause = source.rindex("except OSError as exc:", 0, call)
    assert spawn < except_clause < call


def test_source_order_inflight_before_gh_and_consume_after_verified_url():
    source = OWNER.read_text()
    inflight_write = source.index("atomic_write_json(inflight_path, record)")
    popen = source.index("[gh_bin] + gh_args")
    verified = source.index("candidate = None if timed_out else verified_url(")
    readback = source.index("readback_issue(gh_bin, candidate, spec, spec.body)")
    consume = source.index("consume_under_lock(marker, record, url, inflight_path, lock_wait)")
    assert inflight_write < popen < verified < readback < consume
    # Every gate runs before the shared permit lock is taken and before the in-flight write.
    audit = source.index("run_hook(audit_script")
    ledger = source.index("run_ledger_stage(spec, cwd, command_words)")
    lock = source.index("with PermitLock(permit_file, lock_wait) as lock:")
    assert audit < ledger < lock < inflight_write
    # check + binding + in-flight are inside that one lock block; gh is outside it.
    assert lock < source.index("lock.bind(record)") < inflight_write < popen
    # The permit lock is the file the mint and restore take: `<permit file>.lock`.
    assert 'self.path = permit_file + ".lock"' in source
    # No second lock and no seal/capture machinery.
    for forbidden in ("owner.lock", "check_seal", "cmd_seal", ".seal", "capture_path", "mkdtemp", "rmtree"):
        assert forbidden not in source, forbidden
    import inspect

    ledger_stage = inspect.getsource(owner.run_ledger_stage)
    for forbidden in ("open(", "write(", "mkdtemp", "os.open", "--body-file\", body_path"):
        assert forbidden not in ledger_stage, forbidden
    assert "MOP_ISSUE_CREATE_BODY_B64" in ledger_stage


def test_busy_permit_lock_refuses_without_starting_gh_or_writing_in_flight(world: World):
    with open(world.permit_lock, "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = world.run_owner()
    assert result.returncode == 3, result.stderr
    assert "stayed busy" in result.stderr
    assert world.gh_calls() == 0
    assert not world.inflight_files() and not world.consumed_files()
    assert (world.permits / f"{KEY}.permit.json").exists()


def test_the_lock_is_not_held_while_gh_runs_or_consume_would_deadlock(world: World):
    """gh itself observes the permit lock FREE: the owner released it after the in-flight write."""
    probe = world._script(world.root / "lockprobe.py", LOCK_PROBE)
    world._script(world.bin / "gh", FAKE_GH.replace(
        'echo call >> "$d/calls"',
        'echo call >> "$d/calls"\npython3 "$FAKE_LOCK_PROBE" "$FAKE_PERMIT_LOCK" > "$d/lock-state"',
    ))
    result = world.run_owner(env=world.env(FAKE_PERMIT_LOCK=str(world.permit_lock), FAKE_LOCK_PROBE=str(probe)))
    assert result.returncode == 0, result.stderr
    assert (world.gh_dir / "lock-state").read_text().strip() == "free"
    assert world.consumed_files(), "the consume step re-took the same lock without deadlocking"


def test_mint_restore_and_owner_all_use_the_one_permit_lock_file():
    helper = HELPER.read_text()
    assert 'path + ".lock"' in helper, "mint (atomic write) and legacy consume lock <path>.lock"
    assert 'permit_file + ".lock"' in helper, "restore locks the permit file's lock, not the consumed file's"
    assert 'found + ".lock"' not in helper and 'found_consumed + ".lock"' not in helper


def test_post_create_hooks_are_replayed_with_a_synthesized_payload(world: World):
    record = world.root / "post-record.json"
    sweep = world._script(world.root / "post-sweep.sh", '#!/bin/sh\ncat > "$POST_RECORD"\n')
    env = world.env(ISSUE_CREATE_OWNER_POST_SWEEP_HOOK=str(sweep), POST_RECORD=str(record))
    result = world.run_owner(
        "--title", TITLE, "--body-file", str(world.body_file), "--label", "status:todo", env=env,
    )
    assert result.returncode == 0, result.stderr
    for _ in range(100):
        if record.exists() and record.stat().st_size:
            break
        time.sleep(0.1)
    payload = json.loads(record.read_text())
    assert payload["tool_name"] == "Bash" and payload["hook_event_name"] == "PostToolUse"
    assert payload["tool_input"]["command"].startswith("gh issue create ")
    assert "status:todo" in payload["tool_input"]["command"]
    assert payload["tool_response"] == {"stdout": URL + "\n", "stderr": "", "exit_code": 0}


# ---------------------------------------------------------------------------
# 10: existing records (source 33762) are preserved
# ---------------------------------------------------------------------------

def test_existing_consumed_and_state_records_are_never_touched(world: World):
    world.consumed.mkdir()
    consumed = world.consumed / f"{KEY}.permit.consumed.20261113T210948Z.json"
    consumed.write_text(json.dumps({"obligation_id": OBLIGATION_ID, "body_sha256": "legacy"}))
    world.state.mkdir()
    state = world.state / f"{KEY}.json"
    state.write_text(json.dumps({"state": "READY_TO_FILE", "obligation_id": OBLIGATION_ID, "channel": CHANNEL,
                                 "thread_ts": THREAD}))
    before = (consumed.read_bytes(), state.read_bytes())
    result = world.run_owner()
    assert result.returncode == 2, result.stderr
    assert "already consumed" in result.stderr
    assert (consumed.read_bytes(), state.read_bytes()) == before
    assert world.gh_calls() == 0 and not world.inflight_files()
    assert (world.permits / f"{KEY}.permit.json").exists()


# ---------------------------------------------------------------------------
# 7 (shapes): classification used by the PreToolUse guard
# ---------------------------------------------------------------------------

CLASSIFY_CASES = [
    ("ls -la", "none"),
    ("gh issue list --label x", "none"),
    ("gh issue comment 5 --body 'run gh issue create later'", "none"),
    ('echo "gh issue create"', "none"),
    ("bash scripts/foo.sh", "none"),
    (G, "raw_simple"),
    ("/opt/homebrew/bin/gh issue create --title t --body-file /tmp/b.md", "raw_simple"),
    (f"cd /tmp && {G}", "raw_compound"),
    (f"{G}; echo done", "raw_compound"),
    (f"{G} | cat", "raw_compound"),
    (f"({G})", "raw_compound"),
    (f"{G} \\\n  --label status:todo", "raw_simple"),
    (f"env A=1 {G}", "raw_wrapped"),
    (f"A=1 {G}", "raw_wrapped"),
    (f"rtk {G}", "raw_wrapped"),
    (f"time {G}", "raw_wrapped"),
    (f"xargs {G}", "raw_wrapped"),
    (f"bash -c '{G}'", "raw_wrapped"),
    (f"bash -lc '{G}'", "raw_wrapped"),
    (f'eval "{G}"', "raw_wrapped"),
    (f"python3 {O} create --title t --body-file /tmp/b.md", "owner_simple"),
    (f"{O} create --title t --body-file /tmp/b.md --label status:todo", "owner_simple"),
    (f"python3 {O} create --title t --body-file /tmp/b.md && echo ok", "owner_unsafe"),
    (f"python3 {O} create --title t --body-file /tmp/b.md 2>&1", "owner_unsafe"),
    (f"python3 {O} create --title t --body-file /tmp/b.md | tee /tmp/o", "owner_unsafe"),
    (f"env X=1 python3 {O} create --title t --body-file /tmp/b.md", "owner_unsafe"),
    (f"bash -c 'python3 {O} create --title t'", "owner_unsafe"),
    (f"python3 {O} create --title t --body-file /tmp/b.md; {G}", "owner_unsafe"),
]


@pytest.mark.parametrize("command,kind", CLASSIFY_CASES)
def test_classify(command: str, kind: str):
    assert owner.classify(command) == kind


def test_classify_cli_reads_the_hook_payload():
    proc = subprocess.run(
        [sys.executable, str(OWNER), "classify"],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": G}}),
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0 and json.loads(proc.stdout) == {"kind": "raw_simple"}


# ---------------------------------------------------------------------------
# 8 + 9: rewriting hooks preserve the owner command
# ---------------------------------------------------------------------------

FAKE_REWRITE_NOOP = """#!/bin/sh
cat > /dev/null
exit 0
"""

FAKE_REWRITE_ALTER = """#!/bin/sh
cat > /dev/null
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":{"command":"rtk changed"}}}'
exit 0
"""

FAKE_REWRITE_DENY = """#!/bin/sh
cat > /dev/null
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"no"}}'
exit 0
"""

FAKE_REWRITE_CRASH = """#!/bin/sh
cat > /dev/null
exit 3
"""

OWNER_COMMAND = f"python3 {O} create --title t --body-file /tmp/b.md --label priority:p1"
# An owner invocation whose inline body mentions every pattern the live rewriters key on.
HOSTILE_OWNER = (
    f"python3 {O} create --title t --label priority:p1 "
    "--body 'gh issue create --label priority:p1; modal app list; send-to-slot.sh 1 /tmp/brief.md; "
    "agent-browser open x; npx convex dev'"
)


def _hooks(tmp_path: Path, **scripts: str) -> str:
    paths = []
    for name, text in scripts.items():
        path = tmp_path / f"{name}.sh"
        path.write_text(text)
        path.chmod(0o755)
        paths.append(str(path))
    return os.pathsep.join(paths)


def _preserve(command: str, hooks: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if hooks is not None:
        env["ISSUE_CREATE_OWNER_REWRITER_HOOKS"] = hooks
    payload = json.dumps({"tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": command}})
    return subprocess.run([sys.executable, str(OWNER), "preserve"], input=payload,
                          capture_output=True, text=True, env=env, timeout=120)


def test_owner_command_has_no_gh_issue_text_for_a_rewriter_to_match():
    assert "gh issue" not in OWNER_COMMAND and "issue create" not in OWNER_COMMAND
    # The owner applies the priority-label canonicalisation itself, so nothing is lost.
    spec = owner.parse_spec(["--title", "t", "--body", "x", "--label", "priority:p1"])
    assert spec.extras == [("--label", "P1")]


def test_preserve_is_silent_when_every_rewriter_is_a_noop(tmp_path):
    hooks = _hooks(tmp_path, a=FAKE_REWRITE_NOOP, b=FAKE_REWRITE_NOOP)
    result = _preserve(OWNER_COMMAND, hooks)
    assert (result.returncode, result.stdout) == (0, "")


@pytest.mark.parametrize("script", [FAKE_REWRITE_ALTER, FAKE_REWRITE_DENY, FAKE_REWRITE_CRASH])
def test_preserve_blocks_when_any_rewriter_would_alter_deny_or_fail(tmp_path, script):
    hooks = _hooks(tmp_path, quiet=FAKE_REWRITE_NOOP, loud=script)
    result = _preserve(OWNER_COMMAND, hooks)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert "loud.sh" in decision["message"] and "quiet.sh" not in decision["message"]
    assert "Nothing ran and nothing was written" in decision["message"]


def test_preserve_fails_closed_when_a_rewriter_cannot_be_run(tmp_path):
    result = _preserve(OWNER_COMMAND, str(tmp_path / "missing-rewriter.py"))
    assert json.loads(result.stdout)["decision"] == "block"


def test_the_guard_blocks_an_owner_invocation_a_rewriter_would_change_and_passes_a_clean_one(tmp_path):
    clean = _hooks(tmp_path, quiet=FAKE_REWRITE_NOOP)
    loud = _hooks(tmp_path, loud=FAKE_REWRITE_ALTER)
    audit = HOOKS / "pre-issue-create-audit.sh"
    payload = json.dumps({"tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": OWNER_COMMAND}})
    passed = subprocess.run(["bash", str(audit)], input=payload, capture_output=True, text=True, timeout=60,
                            env=dict(os.environ, ISSUE_CREATE_OWNER_REWRITER_HOOKS=clean))
    assert (passed.returncode, passed.stdout) == (0, "")
    blocked = subprocess.run(["bash", str(audit)], input=payload, capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, ISSUE_CREATE_OWNER_REWRITER_HOOKS=loud))
    assert blocked.returncode == 0
    assert json.loads(blocked.stdout)["decision"] == "block" and "loud.sh" in blocked.stdout


# ---- discovery: the handler set comes from the settings in force, never from a fixed list ----------------

REWRITER_WITH_UPDATED_INPUT = """#!/bin/sh
# emits updatedInput
cat > /dev/null
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":{"command":"rtk changed"}}}'
"""

NOOP_THAT_CAN_REWRITE = """#!/bin/sh
# may emit updatedInput for other commands, but not for this one
cat > /dev/null
exit 0
"""

BLOCKER_WITHOUT_REWRITES = """#!/bin/sh
cat > /dev/null
touch "$BLOCKER_RAN"
exit 2
"""


def _script(directory: Path, name: str, text: str) -> Path:
    target = directory / name
    target.write_text(text)
    target.chmod(0o755)
    return target


def _settings(directory: Path, name: str, matchers: dict, **extra) -> Path:
    document = {"hooks": {"PreToolUse": [
        {"matcher": matcher, "hooks": [{"type": "command", "command": command} for command in commands]}
        for matcher, commands in matchers.items()
    ]}, **extra}
    target = directory / name
    target.write_text(json.dumps(document))
    return target


def _preserve_with(command: str, **env: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash", "cwd": env.pop("PAYLOAD_CWD", "/tmp"), "tool_input": {"command": command}})
    return subprocess.run([sys.executable, str(OWNER), "preserve"], input=payload, capture_output=True,
                          text=True, env=dict(os.environ, **env), timeout=120)


def test_preserve_discovers_registered_bash_handlers_and_runs_nothing_else(tmp_path):
    rewriter = _script(tmp_path, "direct-rewriter.sh", REWRITER_WITH_UPDATED_INPUT)
    inner = _script(tmp_path, "inner-rewriter.sh", REWRITER_WITH_UPDATED_INPUT)
    wrapper = _script(tmp_path, "wrapper.sh", f"#!/bin/sh\nexec bash {inner}\n")  # reached only through the wrapper
    noop = _script(tmp_path, "noop.sh", NOOP_THAT_CAN_REWRITE)
    blocker = _script(tmp_path, "blocker.sh", BLOCKER_WITHOUT_REWRITES)
    other_tool = _script(tmp_path, "write-tool-rewriter.sh", REWRITER_WITH_UPDATED_INPUT)
    settings = _settings(tmp_path, "settings.json", {
        "Bash": [f"bash {rewriter}", f"bash {wrapper}", f"bash {noop}", f"bash {blocker}"],
        "Write": [f"bash {other_tool}"],
    })
    ran = tmp_path / "blocker-ran"
    result = _preserve_with(OWNER_COMMAND, ISSUE_CREATE_OWNER_SETTINGS_FILES=str(settings), BLOCKER_RAN=str(ran))
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    message = decision["message"]
    assert "direct-rewriter.sh" in message and "wrapper.sh" in message, message
    assert "noop.sh" not in message and "blocker.sh" not in message and "write-tool-rewriter.sh" not in message
    assert not ran.exists(), "a hook that cannot emit updatedInput is not a rewrite handler and must not be run"


def test_preserve_is_silent_when_every_discovered_handler_is_a_noop(tmp_path):
    noop = _script(tmp_path, "noop.sh", NOOP_THAT_CAN_REWRITE)
    settings = _settings(tmp_path, "settings.json", {"Bash": [f"bash {noop}"], "*": [f"bash {noop}"]})
    result = _preserve_with(OWNER_COMMAND, ISSUE_CREATE_OWNER_SETTINGS_FILES=str(settings))
    assert (result.returncode, result.stdout) == (0, "")


def test_preserve_fails_closed_when_a_settings_file_cannot_be_read(tmp_path):
    broken = tmp_path / "settings.json"
    broken.write_text("{not json")
    result = _preserve_with(OWNER_COMMAND, ISSUE_CREATE_OWNER_SETTINGS_FILES=str(broken))
    assert json.loads(result.stdout)["decision"] == "block"
    assert "could not be read" in result.stdout


def test_preserve_never_dry_runs_the_guard_itself(tmp_path):
    audit = HOOKS / "pre-issue-create-audit.sh"
    settings = _settings(tmp_path, "settings.json", {"Bash": [f"bash {audit}", f"bash {OWNER}"]})
    result = _preserve_with(OWNER_COMMAND, ISSUE_CREATE_OWNER_SETTINGS_FILES=str(settings))
    assert (result.returncode, result.stdout) == (0, "")


def test_preserve_discovers_project_level_and_plugin_handlers(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    project = tmp_path / "proj"
    (project / "sub").mkdir(parents=True)
    (project / ".claude").mkdir()
    project_rewriter = _script(project, "project-rewriter.sh", REWRITER_WITH_UPDATED_INPUT)
    _settings(project / ".claude", "settings.json", {"Bash": [f"bash {project_rewriter}"]})
    plugin_root = home / ".claude" / "plugins" / "cache" / "mkt" / "plug" / "1.0.0"
    (plugin_root / "hooks").mkdir(parents=True)
    _script(plugin_root, "plugin-rewriter.sh", REWRITER_WITH_UPDATED_INPUT)
    _settings(plugin_root / "hooks", "hooks.json", {"Bash": ["bash ${CLAUDE_PLUGIN_ROOT}/plugin-rewriter.sh"]})
    (home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {"plug@mkt": True}}))
    result = _preserve_with(OWNER_COMMAND, HOME=str(home), PAYLOAD_CWD=str(project / "sub"))
    decision = json.loads(result.stdout)
    assert "project-rewriter.sh" in decision["message"], decision["message"]
    assert "plugin-rewriter.sh" in decision["message"], decision["message"]


@pytest.mark.skipif(not (Path.home() / ".claude" / "hooks" / "rewrite-github-priority-label.py").is_file(),
                    reason="live settings absent")
def test_discovery_on_the_live_settings_finds_every_live_handler_not_a_fixed_subset():
    handlers, problems = owner.discover_rewrite_handlers("/tmp")
    labels = {label for label, _ in handlers}
    assert not problems, problems
    for known in ("rewrite-github-priority-label.py", "rewrite-agent-browser-session.py",
                  "rewrite-convex-dev-cmd.sh", "rewrite-modal-app-list-cmd.sh",
                  "rewrite-send-to-slot-file.py", "rtk-rewrite-worktree-aware.sh"):
        assert known in labels, (known, sorted(labels))
    # Anything else registered that can emit updatedInput is covered too (e.g. the rescue sandbox).
    assert len(labels) >= 6


# ---- the byte-preserving raw parser -----------------------------------------------------------------------

def _extract(command: str, cwd: str = "/tmp") -> dict:
    return owner.extract_raw_create(command, cwd)


def _body_of(result: dict) -> bytes:
    import base64

    return base64.b64decode(result["body_b64"])


@pytest.mark.parametrize("command,title,body", [
    ("gh issue create -t T -b B", "T", b"B"),
    ("gh issue create --title=T --body=B", "T", b"B"),
    ("gh issue create -tT -bB", "T", b"B"),
    ("gh issue create -t=T -b=B", "T", b"B"),
    ("gh issue create --title 'a b' --body 'c d'", "a b", b"c d"),
    ('gh issue create --title "a b" -b "c d"', "a b", b"c d"),
    ("gh issue create -t T -b 'multi\nline\n'", "T", b"multi\nline\n"),
    ("gh issue create -t T -b B -l x -a y -m z -p w -R o/r -T tpl --recover r -w -e", "T", b"B"),
    ("/opt/homebrew/bin/gh issue create -t T -b B", "T", b"B"),
])
def test_extract_reads_every_gh_spelling_exactly(command, title, body):
    result = _extract(command)
    assert result["ok"] is True, result
    assert result["title"] == title and _body_of(result) == body and result["has_body"] is True


def test_extract_reads_body_file_bytes_exactly_through_every_flag_spelling(tmp_path):
    exact = b"Source: x\n\nbody with NUL \x00 inside\n\n\n"
    (tmp_path / "b.md").write_bytes(exact)
    for flag in ("-F b.md", "-Fb.md", "-F=b.md", "--body-file b.md", "--body-file=b.md", "-F 'b.md'", f"-F {tmp_path / 'b.md'}"):
        result = _extract(f"gh issue create -t T {flag}", cwd=str(tmp_path))
        assert result["ok"] is True, (flag, result)
        assert _body_of(result) == exact, flag
        assert result["has_body"] is True


def test_extract_without_a_body_flag_reports_no_body():
    result = _extract("gh issue create -t T")
    assert result["ok"] is True and result["has_body"] is False and result["body_b64"] == ""


@pytest.mark.parametrize("command,klass", [
    ("gh issue create -t x -b y --frobnicate", "ambiguous"),
    ("gh issue create -t x -t y -b z", "ambiguous"),
    ("gh issue create -t x -b y -b z", "ambiguous"),
    ("gh issue create -t x -F /tmp/a -F /tmp/b", "ambiguous"),
    ("gh issue create -t x -b y -F /tmp/b.md", "ambiguous"),
    ("gh issue create -t x -b y stray", "ambiguous"),
    ("gh issue create -t x -b y --", "ambiguous"),
    ("gh issue create -t", "ambiguous"),
    ("gh issue create --web=1", "ambiguous"),
    ('gh issue create -t "$T" -b y', "expansion"),
    ("gh issue create -t x -b \"${B}\"", "expansion"),
    ('gh issue create -t x -b "$(cat /tmp/b.md)"', "expansion"),
    ('gh issue create -t x -b "`cat /tmp/b.md`"', "expansion"),
    ("gh issue create -t x -F /tmp/b*.md", "expansion"),
    ("gh issue create -t x -F /tmp/b?.md", "expansion"),
    ("gh issue create -t x -F /tmp/[ab].md", "expansion"),
    ("gh issue create -t x -b {a,b}", "expansion"),
    ("gh issue create -t x -b {1..3}", "expansion"),
    ("gh issue create -t x -b $'a\\nb'", "expansion"),
    ("gh issue create -t x -F -", "unreadable"),
    ("gh issue create -t x -F /nonexistent/never.md", "unreadable"),
    ("cd /tmp && gh issue create -t x -b y", "shape"),
    ("gh issue create -t x -b y; echo", "shape"),
    ("env A=1 gh issue create -t x -b y", "shape"),
    ("gh issue create -t 'unterminated", "shape"),
])
def test_extract_fails_closed_on_anything_ambiguous_or_unreadable(command, klass):
    result = _extract(command)
    assert result["ok"] is False and result["error_class"] == klass, result


@pytest.mark.parametrize("text,expanded", [
    ("echo $X", True), ('echo "${X}"', True), ('echo "$(x)"', True), ("echo `x`", True), ("echo *.md", True),
    ("echo file?", True), ("echo [abc]", True), ("echo {a,b}", True), ("echo {1..3}", True), ("echo $'x'", True),
    ("echo '$X'", False), ('echo "price 5$"', False), ("echo a\\$b", False), ('echo "a b"', False),
    ("echo '*'", False), ('echo "*"', False), ("echo {a}", False), ("echo plain words", False),
])
def test_unquoted_expansion_scanner(text, expanded):
    assert owner.has_unquoted_expansion(text) is expanded


LIVE_HOOKS = Path.home() / ".claude" / "hooks"


@pytest.mark.skipif(not (LIVE_HOOKS / "rewrite-github-priority-label.py").is_file(), reason="live rewriters absent")
def test_the_real_live_rewriters_are_proven_noops_for_a_clean_owner_command_and_blocked_for_a_hostile_one():
    assert (_preserve(OWNER_COMMAND).returncode, _preserve(OWNER_COMMAND).stdout) == (0, "")
    hostile = _preserve(HOSTILE_OWNER)
    assert json.loads(hostile.stdout)["decision"] == "block"
    assert "rewrite-github-priority-label.py" in hostile.stdout


@pytest.mark.skipif(shutil.which("rtk") is None, reason="rtk not installed")
def test_rtk_leaves_the_owner_command_alone():
    untouched = subprocess.run(["rtk", "rewrite", OWNER_COMMAND], capture_output=True, text=True, timeout=30)
    assert untouched.returncode == 1 and untouched.stdout == "", "rtk must not rewrite the owner command"


def test_no_live_rewriter_is_managed_or_overwritten_by_this_change():
    manifest = json.loads(MANIFEST.read_text())
    sources = {entry["source_path"] for entry in manifest["entries"]}
    assert not [name for name in sources if "/rewrite-" in name or name.endswith("rtk-rewrite-worktree-aware.sh")]
    assert not [root for root in manifest["inventory"]["source_roots"] if "rewrite-" in root]
    shared = ROOT / "scripts/pm/shared-assets/claude/hooks"
    assert not list(shared.glob("rewrite-*")) and not (shared / "rtk-rewrite-worktree-aware.sh").exists()


# ---------------------------------------------------------------------------
# Deployment inventory: every changed or new deployed script is in the manifest at its real digest
# ---------------------------------------------------------------------------

DEPLOYED = {
    "claude/hooks/block-invalid-issue-contract-ledger.sh": "/Users/rajiv/.claude/hooks/block-invalid-issue-contract-ledger.sh",
    "claude/hooks/heydonna-app-block-invalid-issue-contract-ledger.sh": "/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks/block-invalid-issue-contract-ledger.sh",
    "claude/scripts/issue-contract-ledger-hook.py": "/Users/rajiv/.claude/scripts/issue-contract-ledger-hook.py",
    "claude/hooks/issue-create-owner.py": "/Users/rajiv/.claude/hooks/issue-create-owner.py",
    "claude/hooks/pre-issue-create-audit.sh": "/Users/rajiv/.claude/hooks/pre-issue-create-audit.sh",
    "claude/hooks/explore-issue-latch-guard.sh": "/Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh",
    "claude/hooks/explore-issue-permit-consume-gate.sh": "/Users/rajiv/.claude/hooks/explore-issue-permit-consume-gate.sh",
    "claude/skills/explore-issue/SKILL.md": "/Users/rajiv/.claude/skills/explore-issue/SKILL.md",
}


def test_manifest_lists_every_changed_asset_at_its_current_digest_and_mode():
    manifest = json.loads(MANIFEST.read_text())
    entries = manifest["entries"]
    assert entries == sorted(entries, key=lambda item: item["source_path"])
    assert manifest["inventory"]["selected_count"] == len(entries)
    by_source = {entry["source_path"]: entry for entry in entries}
    for source, target in DEPLOYED.items():
        entry = by_source[source]
        path = ROOT / "scripts/pm/shared-assets" / source
        assert entry["canonical_target"] == target
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest(), source
        assert entry["mode"] == (path.stat().st_mode & 0o777), source
    assert by_source["claude/hooks/issue-create-owner.py"]["mode"] == 0o755
    assert "~/.claude/hooks/issue-create-owner.py" in manifest["inventory"]["source_roots"]
