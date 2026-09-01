#!/usr/bin/env python3
"""Stage, atomically activate, check, and roll back a MoP release.

This is deployment plumbing only.  It owns no MoP slot, session, or PM state.
The only persistent files it creates are immutable release payloads and a
rollback bundle for files explicitly supplied by the inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA = "master_of_panes_release"
MANIFEST = "RELEASE_MANIFEST.json"
ROLLBACK_MANIFEST = "ROLLBACK_MANIFEST.json"
SHARED_ASSET_MANIFEST = "scripts/pm/shared-assets/manifest.json"
PROTECTED_NAMES = {".git"}
# These files are part of the running service contract, not optional payload.
# Keep the list deliberately narrow so release packaging remains additive.
REQUIRED_RUNTIME_FILES = {
    "dist/server.js": 0o644,
    "scripts/release-slot-reset-and-ack.py": 0o755,
}


class InstallerError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_record(path: Path, relative: str) -> dict[str, Any]:
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return {"path": relative, "kind": "symlink", "mode": mode, "target": os.readlink(path)}
    if not stat.S_ISREG(metadata.st_mode):
        raise InstallerError(f"unsupported non-file payload: {path}")
    return {"path": relative, "kind": "file", "mode": mode, "sha256": sha256(path)}


def _safe_relative(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or any(part in PROTECTED_NAMES for part in candidate.parts):
        raise InstallerError(f"unsafe relative path: {value}")
    return candidate


def _run(
    command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=True)
    except FileNotFoundError as exc:
        raise InstallerError(f"required command is unavailable: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()[-1:] or ["command failed"]
        raise InstallerError(f"command failed: {' '.join(command)}: {detail[0]}") from exc


def assert_clean_source(repo: Path, candidate: str) -> str:
    observed = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    if observed != candidate:
        raise InstallerError(f"source candidate drift: expected={candidate} actual={observed}")
    dirty = _run(["git", "status", "--porcelain=v1"], cwd=repo).stdout
    if dirty:
        raise InstallerError("source repository is dirty")
    return _run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo).stdout.strip()


def _copy_payload(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        os.symlink(os.readlink(source), destination)
    elif stat.S_ISREG(metadata.st_mode):
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise InstallerError(f"tracked path is not a file or symlink: {source}")


def _tracked_paths(repo: Path) -> list[str]:
    output = _run(["git", "ls-files", "-z"], cwd=repo).stdout
    return [value for value in output.split("\0") if value]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600 if "ROLLBACK" in path.name else 0o644)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _payload_records(root: Path, paths: Iterable[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative in sorted(paths):
        safe = _safe_relative(relative)
        path = root / safe
        if path.exists() or path.is_symlink():
            records.append(file_record(path, relative))
    return records


def _verify_required_runtime_files(root: Path, records: Iterable[dict[str, Any]] | None = None) -> None:
    """Refuse a release that cannot service the running MoP contract."""
    indexed = {record.get("path"): record for record in records or []}
    for relative, expected_mode in REQUIRED_RUNTIME_FILES.items():
        path = root / _safe_relative(relative)
        if not path.is_file() or path.is_symlink():
            raise InstallerError(f"required runtime file is missing or unsafe: {path}")
        observed = file_record(path, relative)
        if observed["mode"] != expected_mode:
            raise InstallerError(
                f"required runtime file mode mismatch: {path} expected={oct(expected_mode)} actual={oct(observed['mode'])}"
            )
        if records is not None and indexed.get(relative) != observed:
            raise InstallerError(f"required runtime file is absent from release manifest: {path}")


def stage_release(
    *,
    repo: Path,
    candidate: str,
    base: str,
    patch_id: str,
    release_root: Path,
    bun: str = "bun",
    node_bin: str | None = None,
) -> dict[str, Any]:
    tree = assert_clean_source(repo, candidate)
    if not node_bin:
        raise InstallerError("stage requires --node-bin for the pinned native runtime rebuild")
    release_root.mkdir(parents=True, exist_ok=True)
    release_dir = release_root / candidate
    if release_dir.exists() or release_dir.is_symlink():
        manifest_path = release_dir / MANIFEST
        if not manifest_path.is_file():
            raise InstallerError(f"release path exists without manifest: {release_dir}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("commit") != candidate or manifest.get("tree") != tree:
            raise InstallerError("existing release manifest does not bind the candidate")
        return {"status": "ALREADY_STAGED", "release_dir": str(release_dir), "manifest": manifest}

    temporary = Path(tempfile.mkdtemp(prefix=f".{candidate}.", dir=release_root))
    try:
        for relative in _tracked_paths(repo):
            safe = _safe_relative(relative)
            _copy_payload(repo / safe, temporary / safe)
        # Native dependencies are rebuilt explicitly below with the pinned
        # runtime.  Letting Bun run lifecycle scripts here can select the
        # host Node ABI before that check, producing an unusable release.
        _run([bun, "install", "--frozen-lockfile", "--ignore-scripts"], cwd=temporary)
        _run([bun, "run", "build"], cwd=temporary)
        node_modules = temporary / "node_modules"
        node_path = Path(node_bin).resolve(strict=True)
        npm_bin = node_path.with_name("npm")
        if not npm_bin.is_file():
            raise InstallerError(f"matching npm binary is missing beside node: {npm_bin}")
        environment = dict(os.environ)
        environment["PATH"] = str(node_path.parent) + os.pathsep + environment.get("PATH", "")
        _run([str(npm_bin), "rebuild", "better-sqlite3", "--no-audit", "--no-fund"], cwd=temporary, env=environment)
        tracked = _tracked_paths(repo)
        generated = [str(path.relative_to(temporary)) for path in (temporary / "dist").rglob("*") if path.is_file()]
        dependencies = [
            str(path.relative_to(temporary))
            for path in node_modules.rglob("*")
            if path.is_file() or path.is_symlink()
        ]
        _verify_required_runtime_files(temporary)
        files = _payload_records(temporary, [*tracked, *generated, *dependencies])
        shared_metadata: dict[str, Any] | None = None
        if (temporary / SHARED_ASSET_MANIFEST).is_file():
            shared = _load_shared_manifest(temporary)
            shared_metadata = {
                "manifest": SHARED_ASSET_MANIFEST,
                "sha256": sha256(temporary / SHARED_ASSET_MANIFEST),
                "entry_count": len(shared["entries"]),
            }
        manifest = {
            "schema": SCHEMA,
            "version": 1,
            "commit": candidate,
            "tree": tree,
            "base": base,
            "stable_patch_id": patch_id,
            "source_repo": str(repo),
            "files": files,
        }
        if shared_metadata is not None:
            manifest["shared_assets"] = shared_metadata
        _write_json(temporary / MANIFEST, manifest)
        os.replace(temporary, release_dir)
        temporary = Path()
        return {"status": "STAGED", "release_dir": str(release_dir), "manifest": manifest}
    finally:
        if temporary != Path() and temporary.exists():
            shutil.rmtree(temporary)


def verify_staged(*, repo: Path, release_dir: Path, candidate: str) -> dict[str, Any]:
    tree = assert_clean_source(repo, candidate)
    manifest_path = release_dir / MANIFEST
    if not manifest_path.is_file():
        raise InstallerError(f"staged manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("commit") != candidate or manifest.get("tree") != tree:
        raise InstallerError("staged manifest candidate/tree mismatch")
    _verify_required_runtime_files(release_dir, manifest.get("files", []))
    shared_manifest_path = release_dir / SHARED_ASSET_MANIFEST
    if shared_manifest_path.is_file():
        shared = _load_shared_manifest(release_dir)
        metadata = manifest.get("shared_assets")
        expected = {
            "manifest": SHARED_ASSET_MANIFEST,
            "sha256": sha256(shared_manifest_path),
            "entry_count": len(shared["entries"]),
        }
        if metadata != expected:
            raise InstallerError("staged shared asset metadata mismatch")
    for record in manifest.get("files", []):
        path = release_dir / _safe_relative(record["path"])
        if not (path.exists() or path.is_symlink()):
            raise InstallerError(f"staged file missing: {path}")
        observed = file_record(path, record["path"])
        if observed != record:
            raise InstallerError(f"staged digest/mode/link mismatch: {path}")
    return manifest


def _load_shared_manifest(release_dir: Path) -> dict[str, Any]:
    """Validate the deterministic, additive shared-operational mapping."""
    path = release_dir / SHARED_ASSET_MANIFEST
    if not path.is_file() or path.is_symlink():
        raise InstallerError(f"shared asset manifest missing: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallerError(f"shared asset manifest is not valid JSON: {path}") from exc
    if manifest.get("schema") != "mop_shared_operational_assets" or manifest.get("version") != 1:
        raise InstallerError("unsupported shared asset manifest schema")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or entries != sorted(entries, key=lambda item: item.get("source_path", "")):
        raise InstallerError("shared asset entries must be a deterministic sorted list")
    targets: set[str] = set()
    sources: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise InstallerError("shared asset entry is not an object")
        source = entry.get("source_path")
        target = entry.get("canonical_target")
        additional_targets = entry.get("additional_targets", [])
        digest = entry.get("sha256")
        mode = entry.get("mode")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or not isinstance(additional_targets, list)
            or not all(isinstance(value, str) for value in additional_targets)
            or not isinstance(digest, str)
        ):
            raise InstallerError("shared asset entry is incomplete")
        source_path = release_dir / Path(SHARED_ASSET_MANIFEST).parent / _safe_relative(source)
        if not source_path.is_file() or source_path.is_symlink():
            raise InstallerError(f"shared asset source is not a regular file: {source}")
        if source in sources:
            raise InstallerError(f"duplicate shared asset source: {source}")
        sources.add(source)
        for target_value in [target, *additional_targets]:
            target_path = Path(target_value)
            if not target_path.is_absolute() or target_path in {Path("/"), Path.home()} or ".." in target_path.parts:
                raise InstallerError(f"shared asset target is too broad: {target_value}")
            if target_value in targets:
                raise InstallerError(f"duplicate shared asset target: {target_value}")
            targets.add(target_value)
        if not isinstance(mode, int) or mode < 0 or mode > 0o777:
            raise InstallerError(f"invalid shared asset mode: {target}")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise InstallerError(f"invalid shared asset digest: {target}")
        if sha256(source_path) != digest or stat.S_IMODE(source_path.stat().st_mode) != mode:
            raise InstallerError(f"shared asset source digest/mode mismatch: {source}")
        if entry.get("dependency_status") != "closed" or entry.get("dependencies") != []:
            raise InstallerError(f"shared asset dependency closure is unresolved: {source}")
    compatibility = manifest.get("rollback_compatibility", [])
    if not isinstance(compatibility, list):
        raise InstallerError("rollback compatibility entries must be a list")
    compatibility_targets: set[str] = set()
    for entry in compatibility:
        if not isinstance(entry, dict):
            raise InstallerError("rollback compatibility entry is not an object")
        source = entry.get("source_path")
        target = entry.get("canonical_target")
        digest = entry.get("sha256")
        mode = entry.get("mode")
        if not isinstance(source, str) or not isinstance(target, str) or not isinstance(digest, str):
            raise InstallerError("rollback compatibility entry is incomplete")
        source_path = release_dir / Path(SHARED_ASSET_MANIFEST).parent / _safe_relative(source)
        if not source_path.is_file() or source_path.is_symlink():
            raise InstallerError(f"rollback compatibility source is not a regular file: {source}")
        target_path = Path(target)
        if not target_path.is_absolute() or target_path in {Path("/"), Path.home()} or ".." in target_path.parts:
            raise InstallerError(f"rollback compatibility target is too broad: {target}")
        if target in targets or target in compatibility_targets:
            raise InstallerError(f"duplicate shared asset target: {target}")
        compatibility_targets.add(target)
        if not isinstance(mode, int) or mode < 0 or mode > 0o777:
            raise InstallerError(f"invalid rollback compatibility mode: {target}")
        preimage_modes = entry.get("preimage_modes", [mode])
        if (
            not isinstance(preimage_modes, list)
            or not preimage_modes
            or any(not isinstance(value, int) or value < 0 or value > 0o777 for value in preimage_modes)
            or sorted(set(preimage_modes)) != preimage_modes
            or mode not in preimage_modes
        ):
            raise InstallerError(f"invalid rollback compatibility preimage modes: {target}")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise InstallerError(f"invalid rollback compatibility digest: {target}")
        if sha256(source_path) != digest or stat.S_IMODE(source_path.stat().st_mode) != mode:
            raise InstallerError(f"rollback compatibility source digest/mode mismatch: {source}")
    inventory = manifest.get("inventory")
    if not isinstance(inventory, dict) or inventory.get("selected_count") != len(entries):
        raise InstallerError("shared asset inventory count mismatch")
    return manifest


def _shared_target_path(target: str, target_root: Path | None) -> Path:
    path = Path(target)
    if target_root is None:
        return path
    if not target_root.is_absolute() or target_root in {Path("/"), Path.home()}:
        raise InstallerError(f"shared asset target root is too broad: {target_root}")
    return target_root / path.relative_to("/")


def _shared_entry_targets(entry: dict[str, Any]) -> list[str]:
    """Return the one source payload's explicitly mapped canonical targets."""
    return [entry["canonical_target"], *entry.get("additional_targets", [])]


def _validate_compatibility_preimage(target: Path, entry: dict[str, Any]) -> None:
    """Only an absent or exact validator may be replaced during rollback."""
    if not (target.exists() or target.is_symlink()):
        return
    if target.is_dir() or target.is_symlink():
        raise InstallerError(f"rollback compatibility target drift: {target}")
    record = file_record(target, str(target))
    preimage_modes = entry.get("preimage_modes", [entry["mode"]])
    expected = {"path": str(target), "kind": "file", "sha256": entry["sha256"]}
    if (
        record.get("path") != expected["path"]
        or record.get("kind") != expected["kind"]
        or record.get("sha256") != expected["sha256"]
        or record.get("mode") not in preimage_modes
    ):
        raise InstallerError(f"rollback compatibility target drift: {target}")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise InstallerError(f"cannot open directory for fsync: {path}") from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_shared_assets(
    *,
    release_dir: Path,
    target_root: Path | None,
    rollback_bundle: Path,
    fail_after: int | None = None,
) -> dict[str, Any]:
    """Atomically install only manifest-listed files; never prune unlisted files."""
    manifest = _load_shared_manifest(release_dir)
    target_records = [
        (entry, target_name, _shared_target_path(target_name, target_root))
        for entry in manifest["entries"]
        for target_name in _shared_entry_targets(entry)
    ]
    targets = [target for _, _, target in target_records]
    for entry in manifest.get("rollback_compatibility", []):
        _validate_compatibility_preimage(_shared_target_path(entry["canonical_target"], target_root), entry)
    rollback = create_rollback_bundle(targets, rollback_bundle)
    compatibility = manifest.get("rollback_compatibility", [])
    if compatibility:
        rollback_manifest_path = rollback_bundle / ROLLBACK_MANIFEST
        rollback_manifest = json.loads(rollback_manifest_path.read_text(encoding="utf-8"))
        compatibility_records: list[dict[str, Any]] = []
        compatibility_root = rollback_bundle / "compatibility"
        compatibility_root.mkdir(mode=0o700)
        for index, entry in enumerate(compatibility):
            source = release_dir / Path(SHARED_ASSET_MANIFEST).parent / _safe_relative(entry["source_path"])
            payload = compatibility_root / f"{index:04d}"
            _copy_payload(source, payload)
            os.chmod(payload, entry["mode"])
            _fsync_directory(compatibility_root)
            compatibility_records.append(
                {
                    "path": str(_shared_target_path(entry["canonical_target"], target_root)),
                    "kind": "file",
                    "mode": entry["mode"],
                    "preimage_modes": entry.get("preimage_modes", [entry["mode"]]),
                    "sha256": entry["sha256"],
                    "payload": str(payload.relative_to(rollback_bundle)),
                }
            )
        rollback_manifest["compatibility_entries"] = compatibility_records
        _write_json(rollback_manifest_path, rollback_manifest)
    staged: list[Path] = []
    replaced = 0
    try:
        for index, (entry, target_name, target) in enumerate(target_records):
            source = release_dir / Path(SHARED_ASSET_MANIFEST).parent / _safe_relative(entry["source_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.shared.{os.getpid()}.{index}.tmp")
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
            staged.append(temporary)
            with source.open("rb") as source_handle, temporary.open("xb") as destination:
                shutil.copyfileobj(source_handle, destination)
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(temporary, entry["mode"])
            os.replace(temporary, target)
            _fsync_directory(target.parent)
            replaced += 1
            observed = file_record(target, target_name)
            expected = {"path": target_name, "kind": "file", "mode": entry["mode"], "sha256": entry["sha256"]}
            if observed != expected:
                raise InstallerError(f"shared asset target readback mismatch: {target}")
            if fail_after is not None and replaced >= fail_after:
                raise InstallerError("injected shared asset installation failure")
        return {
            "status": "SHARED_ASSETS_INSTALLED",
            "count": len(targets),
            "targets": [str(path) for path in targets],
            "rollback_bundle": str(rollback_bundle),
        }
    except Exception as exc:
        restore_rollback_bundle(rollback_bundle)
        raise InstallerError(f"shared asset installation failed and baseline restored: {exc}") from exc
    finally:
        for temporary in staged:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()


def check_shared_assets(*, release_dir: Path, target_root: Path | None) -> dict[str, Any]:
    manifest = _load_shared_manifest(release_dir)
    checked: list[str] = []
    for entry in manifest["entries"]:
        for target_name in _shared_entry_targets(entry):
            target = _shared_target_path(target_name, target_root)
            expected = {"path": target_name, "kind": "file", "mode": entry["mode"], "sha256": entry["sha256"]}
            if not target.is_file() or target.is_symlink() or file_record(target, target_name) != expected:
                raise InstallerError(f"shared asset target parity mismatch: {target}")
            checked.append(str(target))
    return {"status": "SHARED_ASSETS_PASS", "count": len(checked), "targets": checked}


def _inventory_paths(inventory: Path, disposition: str, installed_roots: list[Path]) -> list[Path]:
    if not installed_roots:
        raise InstallerError("at least one narrow --installed-root is required")
    for root in installed_roots:
        if not root.is_absolute() or root == Path("/") or root == Path.home():
            raise InstallerError(f"installed root is too broad: {root}")
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    paths: list[Path] = []
    for item in payload.get("items", []):
        if item.get("disposition") != disposition:
            continue
        value = item.get("absolute_current_path")
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute() or path == Path("/") or path.is_dir():
            raise InstallerError(f"inventory target is not an explicit file/symlink: {value}")
        if not any(path == root or root in path.parents for root in installed_roots):
            continue
        paths.append(path)
    return sorted(set(paths))


def _bundle_target(bundle: Path, index: int, target: Path) -> Path:
    return bundle / "payload" / f"{index:04d}"


def create_rollback_bundle(targets: list[Path], bundle: Path) -> dict[str, Any]:
    if bundle.exists():
        raise InstallerError(f"rollback bundle already exists: {bundle}")
    bundle.mkdir(mode=0o700, parents=True)
    (bundle / "payload").mkdir(mode=0o700)
    entries: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        if target.is_dir() and not target.is_symlink():
            raise InstallerError(f"rollback target must be a file or symlink: {target}")
        if not (target.exists() or target.is_symlink()):
            entries.append({"path": str(target), "present": False})
            continue
        record = file_record(target, str(target))
        payload = _bundle_target(bundle, index, target)
        _copy_payload(target, payload)
        os.chmod(payload, record["mode"])
        entries.append({**record, "path": str(target), "present": True, "payload": str(payload.relative_to(bundle))})
    manifest = {"schema": "master_of_panes_rollback", "version": 1, "entries": entries}
    _write_json(bundle / ROLLBACK_MANIFEST, manifest)
    return manifest


def restore_rollback_bundle(bundle: Path) -> dict[str, Any]:
    manifest = json.loads((bundle / ROLLBACK_MANIFEST).read_text(encoding="utf-8"))
    staged_compatibility: list[tuple[dict[str, Any], Path, Path]] = []
    try:
        compatibility_entries = manifest.get("compatibility_entries", [])
        for index, entry in enumerate(compatibility_entries):
            target = Path(entry["path"])
            _validate_compatibility_preimage(target, entry)
            payload = bundle / entry["payload"]
            expected_payload = {"path": str(target), "kind": "file", "mode": entry["mode"], "sha256": entry["sha256"]}
            if not payload.is_file() or payload.is_symlink() or file_record(payload, str(target)) != expected_payload:
                raise InstallerError(f"rollback compatibility payload mismatch: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.rollback.{os.getpid()}.{index}.tmp")
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
            _copy_payload(payload, temporary)
            os.chmod(temporary, entry["mode"])
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            if file_record(temporary, str(target)) != expected_payload:
                raise InstallerError(f"rollback compatibility staging mismatch: {target}")
            staged_compatibility.append((entry, target, temporary))
        for entry, target, temporary in staged_compatibility:
            os.replace(temporary, target)
            _fsync_directory(target.parent)
            expected = {key: entry[key] for key in ("path", "kind", "mode", "sha256", "target") if key in entry}
            if file_record(target, entry["path"]) != expected:
                raise InstallerError(f"rollback compatibility verification failed: {target}")
    finally:
        for _, _, temporary in staged_compatibility:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
    for entry in manifest["entries"]:
        target = Path(entry["path"])
        if not entry.get("present"):
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    raise InstallerError(f"cannot remove directory during rollback: {target}")
                target.unlink()
            continue
        payload = bundle / entry["payload"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                raise InstallerError(f"cannot restore over directory: {target}")
            target.unlink()
        _copy_payload(payload, target)
        if entry["kind"] == "file":
            os.chmod(target, entry["mode"])
        expected = {key: entry[key] for key in ("path", "kind", "mode", "sha256", "target") if key in entry}
        if file_record(target, entry["path"]) != expected:
            raise InstallerError(f"rollback verification failed: {target}")
    return manifest


def atomic_switch(current: Path, release_dir: Path, expected_old: Path) -> None:
    if not current.is_symlink():
        raise InstallerError(f"current release is not a symlink: {current}")
    try:
        observed = current.resolve(strict=True)
        expected = expected_old.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InstallerError(f"current release target is missing: {current}") from exc
    if observed != expected:
        raise InstallerError(f"current release drift: expected={expected_old} actual={observed}")
    temporary = current.with_name(f".{current.name}.switch.{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink(str(release_dir), temporary)
    os.replace(temporary, current)
    if current.resolve(strict=True) != release_dir.resolve(strict=True):
        raise InstallerError("atomic release switch readback failed")


def _default_restart(label: str) -> None:
    uid = str(os.getuid())
    _run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])


def _default_health(url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(15):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read().decode("utf-8", errors="replace")
                if response.status != 200:
                    raise InstallerError(f"health status={response.status}")
                return {"status": response.status, "body": body, "attempt": attempt + 1}
        except (OSError, InstallerError) as exc:
            last_error = exc
            if attempt < 14:
                time.sleep(1)
    raise InstallerError(f"health unavailable after bounded retry: {last_error}")


def _default_canary(url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(10):
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/slots", timeout=5) as response:
                body = response.read().decode("utf-8", errors="replace")
                if response.status != 200:
                    raise InstallerError(f"slot canary status={response.status}")
                value = json.loads(body)
                if not isinstance(value, (list, dict)):
                    raise InstallerError("slot canary is not structured JSON")
                return {"status": response.status, "slots": value, "attempt": attempt + 1}
        except (OSError, ValueError, InstallerError) as exc:
            last_error = exc
            if attempt < 9:
                time.sleep(1)
    raise InstallerError(f"slot canary unavailable after bounded retry: {last_error}")


def delete_after_readiness(targets: list[Path], rollback: dict[str, Any]) -> None:
    by_path = {entry["path"]: entry for entry in rollback["entries"]}
    for target in targets:
        if target.is_dir() and not target.is_symlink():
            raise InstallerError(f"refusing directory deletion: {target}")
        if target.exists() or target.is_symlink():
            entry = by_path.get(str(target))
            if not entry or not entry.get("present"):
                raise InstallerError(f"target appeared after rollback capture: {target}")
            expected = {key: entry[key] for key in ("path", "kind", "mode", "sha256", "target") if key in entry}
            if file_record(target, str(target)) != expected:
                raise InstallerError(f"DELETE target drifted after rollback capture: {target}")
            target.unlink()


def activate(
    *,
    release_dir: Path,
    current: Path,
    expected_old: Path,
    delete_targets: list[Path],
    rollback_bundle: Path,
    restart: Callable[[], None],
    health: Callable[[], dict[str, Any]],
    canary: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if not release_dir.is_dir() or release_dir.is_symlink():
        raise InstallerError(f"release is not an immutable directory: {release_dir}")
    _verify_required_runtime_files(release_dir)
    rollback = create_rollback_bundle(delete_targets, rollback_bundle)
    switched = False
    try:
        atomic_switch(current, release_dir, expected_old)
        switched = True
        restart()
        health_result = health()
        canary_result = canary()
        delete_after_readiness(delete_targets, rollback)
        return {
            "status": "ACTIVATED",
            "release_dir": str(release_dir),
            "current": str(current),
            "health": health_result,
            "canary": canary_result,
            "rollback_bundle": str(rollback_bundle),
            "deleted": [str(path) for path in delete_targets],
        }
    except Exception as exc:
        if switched:
            atomic_switch(current, expected_old, release_dir)
            restore_rollback_bundle(rollback_bundle)
            restart()
            health()
        raise InstallerError(f"activation failed and baseline restored: {exc}") from exc


def check_install(
    *, release_dir: Path, current: Path, delete_targets: list[Path], keep_targets: list[Path], rollback_bundle: Path
) -> dict[str, Any]:
    if current.resolve(strict=True) != release_dir.resolve(strict=True):
        raise InstallerError("current pointer does not select requested release")
    missing = [str(path) for path in delete_targets if path.exists() or path.is_symlink()]
    absent_keep = [str(path) for path in keep_targets if not (path.exists() or path.is_symlink())]
    if missing:
        raise InstallerError(f"DELETE targets remain: {missing[:3]}")
    if absent_keep:
        raise InstallerError(f"KEEP targets are absent: {absent_keep[:3]}")
    rollback_manifest = rollback_bundle / ROLLBACK_MANIFEST
    if not rollback_manifest.is_file():
        raise InstallerError("rollback bundle manifest is missing")
    return {
        "status": "PASS",
        "release_dir": str(release_dir),
        "current": str(current),
        "delete_absent": len(delete_targets),
        "keep_present": len(keep_targets),
        "rollback_bundle": str(rollback_bundle),
    }


def _check_launchd(*, current: Path, service: str) -> dict[str, Any]:
    output = _run(["launchctl", "print", f"gui/{os.getuid()}/{service}"]).stdout
    if f"working directory = {current}" not in output:
        raise InstallerError("launchd working directory does not use current release")
    if "state = running" not in output or "pid = " not in output:
        raise InstallerError("launchd service is not running")
    pid = next(
        (line.split("=", 1)[1].strip() for line in output.splitlines() if line.strip().startswith("pid = ")),
        None,
    )
    return {"service": service, "pid": pid, "working_directory": str(current), "state": "running"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stage", "activate", "check", "shared-install", "shared-check"))
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--candidate")
    parser.add_argument("--base")
    parser.add_argument("--patch-id")
    parser.add_argument("--node-bin")
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--expected-old", type=Path)
    parser.add_argument("--rollback-bundle", type=Path, required=True)
    parser.add_argument("--shared-assets-root", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--installed-root", type=Path, action="append", default=[])
    parser.add_argument("--service", default="com.heydonna.mop-server")
    parser.add_argument("--health-url", default="http://127.0.0.1:3100/health")
    parser.add_argument("--canary-url", default="http://127.0.0.1:3100")
    args = parser.parse_args(argv)
    try:
        if args.mode == "stage":
            if not args.repo or not args.candidate or not args.base or not args.patch_id:
                raise InstallerError("stage requires --repo, --candidate, --base, and --patch-id")
            result = stage_release(repo=args.repo, candidate=args.candidate, base=args.base, patch_id=args.patch_id, release_root=args.release_root, node_bin=args.node_bin)
        elif args.mode in {"shared-install", "shared-check"}:
            release_dir = args.release_root / args.candidate if args.candidate else None
            if release_dir is None:
                raise InstallerError("shared asset modes require --candidate")
            if args.mode == "shared-install":
                result = install_shared_assets(
                    release_dir=release_dir,
                    target_root=args.shared_assets_root,
                    rollback_bundle=args.rollback_bundle,
                )
            else:
                result = check_shared_assets(release_dir=release_dir, target_root=args.shared_assets_root)
        else:
            if not args.candidate:
                raise InstallerError("activate/check requires --candidate")
            release_dir = args.release_root / args.candidate
            repo = args.repo
            if args.mode == "activate":
                if not args.expected_old or not args.inventory or not args.repo:
                    raise InstallerError("activate requires --repo, --expected-old, and explicit --inventory")
                delete_targets = _inventory_paths(args.inventory, "DELETE", args.installed_root)
                verify_staged(repo=args.repo, release_dir=release_dir, candidate=args.candidate)
                result = activate(release_dir=release_dir, current=args.current, expected_old=args.expected_old, delete_targets=delete_targets, rollback_bundle=args.rollback_bundle, restart=lambda: _default_restart(args.service), health=lambda: _default_health(args.health_url), canary=lambda: _default_canary(args.canary_url))
            else:
                if not args.inventory:
                    raise InstallerError("check requires explicit --inventory")
                delete_targets = _inventory_paths(args.inventory, "DELETE", args.installed_root)
                keep_targets = _inventory_paths(args.inventory, "KEEP_IN_HEYDONNA_APP", args.installed_root)
                result = check_install(release_dir=release_dir, current=args.current, delete_targets=delete_targets, keep_targets=keep_targets, rollback_bundle=args.rollback_bundle)
                result["launchd"] = _check_launchd(current=args.current, service=args.service)
                result["health"] = _default_health(args.health_url)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except InstallerError as exc:
        print(json.dumps({"status": "REFUSED", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
