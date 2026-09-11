"""Focused contracts for the managed PM state-transition entrypoint.

Repairs SOURCE_AUTHORITY_ABSENT_FOR_STATE_TRANSITION_ENTRYPOINT: the versioned
``pm-state-replace.sh`` and the promotion-proof guard were unmanaged local files
whose guard guidance pointed at absent ``pm-operator``/``pm-transition``
entrypoints.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
SHARED = ROOT / "scripts" / "pm" / "shared-assets"
MANIFEST = SHARED / "manifest.json"
ENTRY_SOURCE = SHARED / "claude" / "scripts" / "pm-state-replace.sh"
GUARD_SOURCE = SHARED / "claude" / "hooks" / "pm-state-promotion-proof-guard.sh"

ENTRY_SHA = "3c98bb0a95f0e0ad42c40f975a993db401db2875720e8b14b5cdccb7be755907"
ENTRY_BLOB = "fed6ec0f5b877e506697bed00912274f08005481"
ENTRY_COMMIT = "5abb4ec486d27bb51460d287f9ce019494769977"
GUARD_LIVE_PREIMAGE = "a109e33d186de8de977dad2f942f9e913cbf1c07f0b0621ff6b064c2856ce1d1"

APP_ENTRY = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/pm-state-replace.sh"
USER_ENTRY = "/Users/rajiv/.claude/scripts/pm-state-replace.sh"
APP_GUARD = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks/pm-state-promotion-proof-guard.sh"
USER_GUARD = "/Users/rajiv/.claude/hooks/pm-state-promotion-proof-guard.sh"

MOCK_GH = textwrap.dedent(
    r'''
    #!/usr/bin/env bash
    set -uo pipefail
    args="$*"
    if [ "${1:-}" = "pr" ] && [ "${2:-}" = "view" ]; then
      printf '%s\n' "${MOCK_HEAD:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
      exit 0
    fi
    if [ "${1:-}" = "pr" ] && [ "${2:-}" = "ready" ]; then
      printf 'MUT:pr-ready\n' >>"${MOCK_MUT_LOG:?}"
      exit 0
    fi
    if [ "${1:-}" = "api" ]; then
      if [[ "$args" == *"/pulls/"* ]]; then
        printf '%s\n' "${MOCK_DRAFT:-false}"
        exit 0
      fi
      if [[ "$args" == *"--method DELETE"* ]]; then
        printf 'MUT:delete:%s\n' "$args" >>"${MOCK_MUT_LOG:?}"
        exit 0
      fi
      if [[ "$args" == *"--method POST"* ]]; then
        cat >/dev/null 2>&1 || true
        printf 'MUT:post:%s\n' "$args" >>"${MOCK_MUT_LOG:?}"
        : >"${MOCK_POSTED_FLAG:?}"
        exit 0
      fi
      if [[ "$args" == *"/labels/"* ]]; then
        exit 0
      fi
      if [[ "$args" == *"ci-head:"* ]]; then
        cat "${MOCK_MARKERS_FILE:?}"
        exit 0
      fi
      if [ "${MOCK_STATE_READ_FAIL:-0}" = "1" ]; then
        exit 1
      fi
      if [ -f "${MOCK_POSTED_FLAG:?}" ] && [ -n "${MOCK_POST_STATE_FILE:-}" ]; then
        cat "$MOCK_POST_STATE_FILE"
      else
        cat "${MOCK_STATE_FILE:?}"
      fi
      exit 0
    fi
    exit 0
    '''
).lstrip()


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "install_release_state_entry", ROOT / "scripts" / "install-release.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _entry(source_path):
    for entry in _manifest()["entries"]:
        if entry["source_path"] == source_path:
            return entry
    raise AssertionError("manifest entry missing: " + source_path)


def _targets(entry):
    return [entry["canonical_target"], *entry.get("additional_targets", [])]


def test_manifest_maps_entrypoint_and_guard_with_source_authority():
    entry = _entry("claude/scripts/pm-state-replace.sh")
    assert _targets(entry) == [APP_ENTRY, USER_ENTRY]
    assert entry["mode"] == 0o755
    assert entry["sha256"] == ENTRY_SHA
    assert entry["sha256"] == hashlib.sha256(ENTRY_SOURCE.read_bytes()).hexdigest()
    assert entry["source_authority"]["repository"] == "heydonna-app/heydonna-app"
    assert entry["source_authority"]["commit"] == ENTRY_COMMIT
    assert entry["source_authority"]["path"] == "scripts/pm/control-plane/pm-state-replace.sh"
    assert entry["source_authority"]["blob_sha"] == ENTRY_BLOB
    assert entry["source_authority"]["preimage_sha256"] is None
    assert entry["ownership_class"] == "shared-claude-pm-state-transition"

    guard = _entry("claude/hooks/pm-state-promotion-proof-guard.sh")
    assert _targets(guard) == [APP_GUARD, USER_GUARD]
    assert guard["mode"] == 0o755
    assert guard["sha256"] == hashlib.sha256(GUARD_SOURCE.read_bytes()).hexdigest()
    assert guard["source_authority"]["preimage_sha256"] == GUARD_LIVE_PREIMAGE
    assert guard["ownership_class"] == "shared-claude-pm-admission-guard"


def test_guard_guidance_redirects_to_installed_entrypoint_only():
    text = GUARD_SOURCE.read_text(encoding="utf-8")
    assert "pm-operator" not in text
    assert "/Users/rajiv/.claude/scripts/pm-state-replace.sh" in text
    for fence in (
        "raw writes to canonical promotion proofs are disabled",
        "is still draft",
        "cannot be promoted by an inline self-authored proof command",
        "PM_STATE_PROMOTION_PROOF=PM_OVERRIDE_WITH_PROOF",
        "pre-merge-current-head-ci-guard.sh",
        "CORE_E2E_CLASSIFICATION",
    ):
        assert fence in text, fence


def _seed_targets(root, mapping):
    paths = {}
    for name, target in mapping.items():
        path = root / target.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("stale-" + name + "\n").encode())
        path.chmod(0o755)
        paths[name] = path
    return paths


def test_installer_installs_all_four_targets_and_rolls_back(tmp_path):
    installer = _load_installer()
    release = tmp_path / "release"
    shutil.copytree(SHARED, release / "scripts" / "pm" / "shared-assets")
    targets = tmp_path / "targets"
    seeded = _seed_targets(
        targets,
        {
            "app_entry": APP_ENTRY,
            "user_entry": USER_ENTRY,
            "app_guard": APP_GUARD,
            "user_guard": USER_GUARD,
        },
    )
    rollback = tmp_path / "rollback"
    result = installer.install_shared_assets(release_dir=release, target_root=targets, rollback_bundle=rollback)
    assert result["status"] == "SHARED_ASSETS_INSTALLED"
    for name, path in seeded.items():
        expected = ENTRY_SOURCE if "entry" in name else GUARD_SOURCE
        assert path.read_bytes() == expected.read_bytes()
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert str(seeded["app_entry"]) in result["targets"]
    assert str(seeded["user_guard"]) in result["targets"]
    assert installer.check_shared_assets(release_dir=release, target_root=targets)["status"] == "SHARED_ASSETS_PASS"
    installer.restore_rollback_bundle(rollback)
    for name, path in seeded.items():
        assert path.read_bytes() == ("stale-" + name + "\n").encode()


def test_installer_refuses_symlinked_leaf_without_effect(tmp_path):
    installer = _load_installer()
    release = tmp_path / "release"
    shutil.copytree(SHARED, release / "scripts" / "pm" / "shared-assets")
    targets = tmp_path / "targets"
    seeded = _seed_targets(targets, {"app_entry": APP_ENTRY, "user_entry": USER_ENTRY})
    decoy = tmp_path / "decoy.sh"
    decoy.write_bytes(b"decoy\n")
    seeded["app_entry"].unlink()
    seeded["app_entry"].symlink_to(decoy)
    with pytest.raises(installer.InstallerError):
        installer.install_shared_assets(
            release_dir=release, target_root=targets, rollback_bundle=tmp_path / "rollback"
        )
    assert seeded["app_entry"].is_symlink()
    assert decoy.read_bytes() == b"decoy\n"
    assert seeded["user_entry"].read_bytes() == b"stale-user_entry\n"


def _gh_env(tmp_path, *, state, markers, **extra):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(MOCK_GH, encoding="utf-8")
    gh.chmod(0o755)
    mut_log = tmp_path / "mutations.log"
    mut_log.write_text("", encoding="utf-8")
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state) + "\n", encoding="utf-8")
    markers_file = tmp_path / "markers.json"
    markers_file.write_text(json.dumps(markers) + "\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": str(bindir) + ":" + os.environ.get("PATH", ""),
        "MOCK_MUT_LOG": str(mut_log),
        "MOCK_STATE_FILE": str(state_file),
        "MOCK_MARKERS_FILE": str(markers_file),
        "MOCK_POSTED_FLAG": str(tmp_path / "posted.flag"),
        "PM_STATE_TRANSITIONS_LOG": str(tmp_path / "transitions.log"),
        "PM_CI_CYCLE_RECEIPT_ROOT": str(tmp_path / "receipts"),
        "GH_REPO": "example/repo",
        **extra,
    }
    return env, mut_log


def _run_entry(args, env):
    return subprocess.run(["bash", str(ENTRY_SOURCE), *args], env=env, capture_output=True, text=True)


def _mutations(mut_log):
    return [line for line in mut_log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_unknown_suffix_refused_with_zero_label_effect(tmp_path):
    env, mut_log = _gh_env(tmp_path, state=["pm-state:pm-review-pending"], markers=[])
    result = _run_entry(["42", "not-a-real-state"], env)
    assert result.returncode == 2, result.stderr
    assert _mutations(mut_log) == []


def test_absent_state_read_refused_with_zero_label_effect(tmp_path):
    env, mut_log = _gh_env(
        tmp_path, state=["pm-state:pm-review-pending"], markers=[], MOCK_STATE_READ_FAIL="1"
    )
    result = _run_entry(["42", "merge-ready"], env)
    assert result.returncode == 1, result.stderr
    assert "cannot read current PM state labels" in result.stderr
    assert _mutations(mut_log) == []


def test_draft_pr_refused_with_zero_label_effect(tmp_path):
    env, mut_log = _gh_env(
        tmp_path, state=["pm-state:pm-review-pending"], markers=[], MOCK_DRAFT="true"
    )
    result = _run_entry(["42", "qa-passed-awaiting-ci"], env)
    assert result.returncode == 1, result.stderr
    assert "still draft" in result.stderr
    assert _mutations(mut_log) == []


def test_missing_proof_refused_by_gate_with_zero_label_effect(tmp_path):
    gate = tmp_path / "refusing-gate.py"
    gate.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    env, mut_log = _gh_env(
        tmp_path, state=["pm-state:pm-review-pending"], markers=[], CI_READY_GATE=str(gate)
    )
    result = _run_entry(["42", "qa-passed-awaiting-ci"], env)
    assert result.returncode == 1, result.stderr
    assert _mutations(mut_log) == []


def test_post_state_drift_refused(tmp_path):
    env, mut_log = _gh_env(tmp_path, state=["pm-state:pm-review-pending"], markers=[])
    post = tmp_path / "post-state.json"
    post.write_text(json.dumps(["merge-ready", "pm-state:stale-drift"]) + "\n", encoding="utf-8")
    env["MOCK_POST_STATE_FILE"] = str(post)
    result = _run_entry(["42", "merge-ready"], env)
    assert result.returncode == 1, result.stderr
    assert "VERIFY-FAILED" in result.stderr or "expected only merge-ready" in result.stderr
    assert any(m.startswith("MUT:post:") for m in _mutations(mut_log))


def test_clean_merge_ready_applies_exactly_one_state(tmp_path):
    env, mut_log = _gh_env(tmp_path, state=["pm-state:pm-review-pending"], markers=[])
    post = tmp_path / "post-state.json"
    post.write_text(json.dumps(["merge-ready"]) + "\n", encoding="utf-8")
    env["MOCK_POST_STATE_FILE"] = str(post)
    result = _run_entry(["42", "merge-ready"], env)
    assert result.returncode == 0, result.stderr
    muts = _mutations(mut_log)
    assert sum(m.startswith("MUT:post:") for m in muts) == 1
    assert sum(m.startswith("MUT:delete:") for m in muts) == 1
