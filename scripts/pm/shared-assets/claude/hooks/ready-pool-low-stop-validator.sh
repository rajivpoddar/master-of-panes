#!/usr/bin/env bash
# Stop hook: if hourly ops flagged READY_POOL_LOW, require PM to run the
# backlog promoter before ending the turn.

set -euo pipefail

cat >/dev/null 2>&1 || true

python3 <<'PYEOF'
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

LOG = os.environ.get("READY_POOL_LOW_STOP_LOG") or "/tmp/ready-pool-low-stop-validator.log"
HOURLY_GLOB = os.environ.get("READY_POOL_LOW_HOURLY_GLOB") or "/tmp/mop-ops-audit-*.stdout"
PROMOTER_GLOB = os.environ.get("READY_POOL_LOW_PROMOTER_GLOB") or "/tmp/pm-backlog-promoter-*.json"
FANOUT_GLOB = os.environ.get("READY_POOL_LOW_FANOUT_GLOB") or "/tmp/pm-ready-pool-contract-fanout-*.json"
DETERMINISTIC_GLOB = os.environ.get("READY_POOL_LOW_DETERMINISTIC_GLOB") or "/tmp/pm-ready-pool-contract-repair-deterministic-*.json"
ACTIVE_REPAIR = os.environ.get("READY_POOL_REPAIR_ACTIVE_FILE") or "/tmp/pm-ready-pool-contract-repair-active.json"
LEGACY_REPAIR_SCAN = os.environ.get("READY_POOL_REPAIR_LEGACY_SCAN", "1") != "0"

try:
    MAX_HOURLY_AGE_SECONDS = int(os.environ.get("READY_POOL_LOW_STOP_MAX_AGE_SECONDS") or "7200")
except ValueError:
    MAX_HOURLY_AGE_SECONDS = 7200


def log(message: str) -> None:
    try:
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def emit_block(message: str) -> None:
    print(json.dumps({"decision": "block", "message": message, "reason": message}))


def read_file(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def latest_file(pattern: str):
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def pid_is_live(pid: int) -> bool:
    if pid < 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def active_repair_coordinator():
    """Return live running proof; stale/dead leases never satisfy the gate."""
    try:
        data = json.loads(read_file(ACTIVE_REPAIR))
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("schema") == "pm-ready-pool-contract-repair-active/v1":
        try:
            pid = int(data.get("pid") or 0)
            heartbeat = float(data.get("heartbeat_at_epoch") or 0)
            started = float(data.get("started_at_epoch") or 0)
        except (TypeError, ValueError):
            pid = 0
            heartbeat = 0
            started = 0
        source = str(data.get("source_manifest") or "")
        if (
            data.get("status") == "running"
            and pid_is_live(pid)
            and os.path.isfile(source)
            and 0 <= time.time() - heartbeat <= 180
            and 0 <= time.time() - started <= 21600
        ):
            return {
                "mode": "lease",
                "pid": pid,
                "source": source,
                "processed": count_value(data, "processed_count"),
                "candidates": count_value(data, "candidate_count"),
            }

    # Backward compatibility for a coordinator launched before lease support.
    # Accept only the exact apply command with a readable source manifest.
    if LEGACY_REPAIR_SCAN:
        try:
            listing = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
        except Exception:
            listing = ""
        command_re = re.compile(
            r"^\s*(\d+)\s+.*?/\.claude/scripts/ready-pool-contract-repair\.py\s+"
            r"--source-manifest\s+(\S+)\s+--apply(?:\s|$)"
        )
        for line in listing.splitlines():
            match = command_re.search(line)
            if not match:
                continue
            pid = int(match.group(1))
            source = match.group(2)
            if pid_is_live(pid) and os.path.isfile(source):
                return {"mode": "legacy_process", "pid": pid, "source": source, "processed": 0, "candidates": 0}
    return None


def int_field(text: str, name: str, default: int = 0) -> int:
    m = re.search(rf"\b{name}=([0-9]+)\b", text)
    if not m:
        return default
    try:
        return int(m.group(1))
    except ValueError:
        return default


def list_len(data: dict, key: str) -> int:
    value = data.get(key)
    return len(value) if isinstance(value, list) else 0


def count_value(data: dict, key: str) -> int:
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    value = counts.get(key, data.get(key))
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    try:
        return int(value)
    except Exception:
        return 0


def source_manifest_for_review(data: dict):
    if not data.get("review_result"):
        return None, None
    path = str(data.get("dry_run_manifest") or "")
    if not path or not os.path.isfile(path):
        return path or None, None
    try:
        decoded = json.loads(read_file(path))
    except Exception:
        return path, None
    return path, decoded if isinstance(decoded, dict) else None


def promotion_manifest_closes_issue(path: str, issue: int) -> bool:
    if not path or not os.path.isfile(path):
        return False
    try:
        data = json.loads(read_file(path))
    except Exception:
        return False
    if not isinstance(data, dict) or not bool(data.get("apply") or data.get("applied")):
        return False
    promoted = data.get("promoted")
    if not isinstance(promoted, list):
        promoted = data.get("promotion_results")
    if not isinstance(promoted, list):
        return False
    for row in promoted:
        if not isinstance(row, dict) or not row.get("ok"):
            continue
        try:
            row_issue = int(row.get("issue"))
        except Exception:
            continue
        if row_issue != issue:
            continue
        labels = row.get("live_labels")
        ready_pool = row.get("live_ready_pool")
        if not isinstance(labels, list) or not isinstance(ready_pool, dict):
            continue
        priority_labels = [label for label in labels if str(label) in {"P0", "P1", "P2", "P3"}]
        if "status:todo" not in labels or len(priority_labels) != 1:
            continue
        if any(str(label).startswith(("pm-blocked:", "pm-cleanup:")) and label != "pm-blocked:dependency" for label in labels):
            continue
        required = ("priority", "claimable_slot_type", "ac_summary")
        if any(not str(ready_pool.get(key) or "").strip() for key in required):
            continue
        if not str(ready_pool.get("lane") or ready_pool.get("work_type") or "").strip():
            continue
        blockers = str(ready_pool.get("blockers") or "").strip().lower()
        if blockers not in {"", "none", "null", "[]"} and not re.fullmatch(r"\s*\[?\s*#\d+(?:\s*,\s*#\d+)*\s*\]?\s*", blockers):
            continue
        if str(ready_pool.get("priority") or "").strip() != priority_labels[0]:
            continue
        return True
    return False


def completed_contract_fanout(source_path: str):
    source_real = os.path.realpath(source_path)
    source_mtime = os.path.getmtime(source_path) if os.path.isfile(source_path) else 0
    candidates = sorted(glob.glob(FANOUT_GLOB) + glob.glob(DETERMINISTIC_GLOB), key=os.path.getmtime, reverse=True)
    for path in candidates:
        if os.path.getmtime(path) < source_mtime:
            continue
        try:
            data = json.loads(read_file(path))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("schema") == "pm-ready-pool-contract-repair-deterministic/v1":
            if data.get("status") != "complete" or not data.get("apply"):
                continue
            if os.path.realpath(str(data.get("source_manifest") or "")) != source_real:
                continue
            if any(data.get(key) for key in ("failed", "needs_contract", "blocked_contract")):
                continue
            scan_path = str(data.get("final_scan_manifest") or "")
            try:
                scan = json.loads(read_file(scan_path))
            except Exception:
                continue
            if scan.get("degraded") or scan.get("apply") or scan.get("would_promote") or scan.get("needs_ready_pool_metadata"):
                continue
            if all(promotion_manifest_closes_issue(str(row.get("promotion_manifest") or ""), int(row.get("issue") or 0))
                   for row in data.get("promoted") or []):
                return path
            continue
        if data.get("schema") != "pm-ready-pool-contract-fanout/v1" or data.get("status") != "complete":
            continue
        if os.path.realpath(str(data.get("source_manifest") or "")) != source_real:
            continue
        results = data.get("results")
        if data.get("candidate_exhausted") and not data.get("target_reached"):
            if not str(data.get("exhaustion_slack_ts") or "").strip():
                continue
        window_size = count_value(data, "window_size")
        if not isinstance(results, list) or not results or window_size < 1 or len(results) != window_size:
            continue
        valid = True
        seen = set()
        for row in results:
            if not isinstance(row, dict):
                valid = False
                break
            try:
                issue = int(row.get("issue"))
            except Exception:
                valid = False
                break
            if issue in seen:
                valid = False
                break
            seen.add(issue)
            status = str(row.get("status") or "")
            if status not in {"PROMOTED", "PARKED", "NEEDS_CTO", "NEEDS_RAJIV", "EXTERNAL_ACTION"}:
                valid = False
                break
            result_file = str(row.get("result_file") or "")
            if not result_file or not os.path.isfile(result_file):
                valid = False
                break
            if status == "PROMOTED":
                promotion_manifest = str(row.get("promotion_manifest") or "")
                if not promotion_manifest_closes_issue(promotion_manifest, issue):
                    valid = False
                    break
            if status in {"NEEDS_CTO", "NEEDS_RAJIV", "EXTERNAL_ACTION"} and not str(row.get("slack_ts") or "").strip():
                valid = False
                break
        if valid:
            return path
    return None


def review_result_closes(data: dict):
    source_path, source = source_manifest_for_review(data)
    if source is None:
        return False, "review_source_missing_or_unreadable", None
    needs_metadata = max(
        list_len(source, "needs_ready_pool_metadata"),
        count_value(source, "needs_ready_pool_metadata"),
    )
    if needs_metadata == 0:
        return True, "no_contract_candidates", None
    fanout_path = completed_contract_fanout(source_path)
    if fanout_path:
        return True, "contract_fanout_complete", fanout_path
    return False, f"contract_candidates_unresolved:{needs_metadata}", None


def apply_reaches_target(data: dict):
    target = count_value(data, "ready_pool_target") or 10
    current = count_value(data, "current_ready_pool_count")
    rows = data.get("promoted")
    if isinstance(rows, list):
        # Membership can increase while claimable capacity remains unchanged.
        promoted = sum(
            1 for row in rows
            if isinstance(row, dict) and row.get("ok")
            and not row.get("dependency_gated")
            and not (row.get("candidate") or {}).get("open_dependencies")
            and "pm-blocked:dependency" not in (row.get("live_labels") or [])
        )
    else:
        promoted = 0  # counts without per-issue proof cannot establish capacity
    return current + promoted >= target, current, promoted, target


hourly_path = latest_file(HOURLY_GLOB)
if not hourly_path:
    log("allow no_hourly_artifact")
    sys.exit(0)

hourly_mtime = os.path.getmtime(hourly_path)
hourly_age = time.time() - hourly_mtime
if MAX_HOURLY_AGE_SECONDS > 0 and hourly_age > MAX_HOURLY_AGE_SECONDS:
    log(f"allow stale_hourly_artifact path={hourly_path} age_s={int(hourly_age)}")
    sys.exit(0)

try:
    hourly_text = read_file(hourly_path)
except Exception as exc:
    log(f"allow unreadable_hourly path={hourly_path} error={exc}")
    sys.exit(0)

match = re.search(r"READY_POOL_LOW[^\n]*", hourly_text)
if not match:
    log(f"allow no_ready_pool_low path={hourly_path}")
    sys.exit(0)

ready_line = match.group(0)
clean_todo = int_field(ready_line, "clean_todo")
threshold = int_field(ready_line, "threshold", 10)
total_todo = int_field(ready_line, "total_status_todo")
assigned_todo = int_field(ready_line, "assigned_todo")
blocked_todo = int_field(ready_line, "blocked_todo")

active_repair = active_repair_coordinator()
if active_repair:
    log(
        f"allow active_repair mode={active_repair['mode']} pid={active_repair['pid']} "
        f"source={active_repair['source']} processed={active_repair['processed']} "
        f"candidates={active_repair['candidates']}"
    )
    sys.exit(0)

newer_promoters = [
    path
    for path in glob.glob(PROMOTER_GLOB)
    if os.path.getmtime(path) > hourly_mtime
]
newer_promoters.sort(key=os.path.getmtime, reverse=True)

# Closure via completed review-result: a review-result manifest with zero remaining
# accepts is valid closure even if a later dry-run superseded it by mtime. The
# promoter's own final slot-dispatch-sweep always writes a fresh candidate-bearing
# dry-run AFTER its accepted=0 review-result, so keying only on newest-by-mtime makes
# the gate unsatisfiable once the backlog is genuinely exhausted (all rejected / held /
# accepts already in status:todo). Scan all newer manifests for a completed review with
# nothing left to promote.
for _rp in newer_promoters:
    try:
        _rd = json.loads(read_file(_rp))
    except Exception:
        continue
    if not isinstance(_rd, dict):
        continue
    if _rd.get("degraded"):
        continue
    if _rd.get("review_result") and max(list_len(_rd, "accepted"), count_value(_rd, "accepted")) == 0:
        _closes, _reason, _fanout = review_result_closes(_rd)
        if _closes:
            log(f"allow review_result_no_accepts_among_newer path={_rp} reason={_reason} fanout={_fanout or 'none'}")
            sys.exit(0)
        log(f"skip_nonterminal_review_result path={_rp} reason={_reason}")
    # A target-restoring apply is closure even if a later re-scan/dry-run (the
    # promoter's trailing slot-dispatch-sweep) shows promoted=0. A partial apply is
    # not closure; PM must continue or produce reviewed exhaustion proof.
    if max(list_len(_rd, "promoted"), count_value(_rd, "promoted")) > 0:
        _reached, _current, _promoted, _target = apply_reaches_target(_rd)
        if _reached:
            log(
                f"allow apply_promoted_among_newer path={_rp} current={_current} "
                f"promoted={_promoted} target={_target}"
            )
            sys.exit(0)
        log(
            f"skip_partial_apply path={_rp} current={_current} promoted={_promoted} "
            f"target={_target}"
        )
    # A completed review that promoted nothing NEW and had zero apply failures is
    # exhaustion closure: accepted candidates were already status:todo (not_backlog)
    # or every candidate was rejected. This is distinct from an apply FAILURE
    # (promotion_failed>0), which must still block.
    if (
        _rd.get("review_result")
        and count_value(_rd, "promoted") == 0
        and count_value(_rd, "promotion_failed") == 0
    ):
        _closes, _reason, _fanout = review_result_closes(_rd)
        if _closes:
            log(
                f"allow review_result_exhausted_no_new_promotions path={_rp} "
                f"reason={_reason} fanout={_fanout or 'none'}"
            )
            sys.exit(0)
        log(f"skip_nonterminal_exhaustion_result path={_rp} reason={_reason}")

if not newer_promoters:
    message = (
        "[READY_POOL_LOW_PROMOTER_REQUIRED] Latest hourly ops flagged READY_POOL_LOW "
        f"(clean_todo={clean_todo}, threshold={threshold}, total_status_todo={total_todo}, "
        f"assigned_todo={assigned_todo}, blocked_todo={blocked_todo}) in {hourly_path}. "
        "No newer /tmp/pm-backlog-promoter-*.json exists. PM must launch "
        "Agent(subagent_type=\"pm-backlog-promoter\", run_in_background=true, prompt=\"mode=refill-ready-pool\"), "
        "repair contracts in the same run, promote all eligible P0-P2 and P3 deficit refill, then use canonical direct-assign; no architecture review is required."
    )
    log(f"block no_newer_promoter hourly={hourly_path}")
    emit_block(message)
    sys.exit(0)

promoter_path = None
promoter = None
for candidate_path in newer_promoters:
    try:
        candidate = json.loads(read_file(candidate_path))
    except Exception as exc:
        message = (
            "[READY_POOL_LOW_PROMOTER_REQUIRED] Latest hourly ops flagged READY_POOL_LOW, "
            f"but the newer promoter manifest is unreadable: {candidate_path} ({exc}). "
            "Rerun the background pm-backlog-promoter agent in refill-ready-pool mode and finish with a valid manifest before stopping."
        )
        log(f"block unreadable_promoter path={candidate_path} error={exc}")
        emit_block(message)
        sys.exit(0)
    if not isinstance(candidate, dict):
        log(f"skip auxiliary_promoter_manifest path={candidate_path} type={type(candidate).__name__}")
        continue
    promoter_path = candidate_path
    promoter = candidate
    break

if promoter is None or promoter_path is None:
    message = (
        "[READY_POOL_LOW_PROMOTER_REQUIRED] Latest hourly ops flagged READY_POOL_LOW, "
        "but every newer pm-backlog-promoter JSON file is an auxiliary manifest rather than "
        "an object-shaped promoter result. Rerun the background pm-backlog-promoter agent in "
        "refill-ready-pool mode before stopping."
    )
    log("block no_object_promoter_manifest")
    emit_block(message)
    sys.exit(0)

apply_mode = bool(promoter.get("apply") or promoter.get("applied"))
selected = max(list_len(promoter, "selected"), count_value(promoter, "selected"))
would_promote = max(list_len(promoter, "would_promote"), count_value(promoter, "would_promote"))
needs_metadata = max(list_len(promoter, "needs_ready_pool_metadata"), count_value(promoter, "needs_ready_pool_metadata"))
auto_promotable = count_value(promoter, "auto_promotable")
promoted = max(list_len(promoter, "promoted"), count_value(promoter, "promoted"))
promotion_failed = max(list_len(promoter, "promotion_failed"), count_value(promoter, "promotion_failed"))
accepted = max(list_len(promoter, "accepted"), count_value(promoter, "accepted"))
rejected = max(list_len(promoter, "rejected"), count_value(promoter, "rejected"))

if promoter.get("degraded"):
    message = (
        "[READY_POOL_LOW_PROMOTER_REQUIRED] PM ran the backlog promoter after hourly READY_POOL_LOW, "
        f"but the newer manifest is degraded and is not closure ({promoter_path}: "
        f"stage={promoter.get('stage','unknown')} error={promoter.get('error','unknown')}). "
        "Fix the promoter failure or rerun the background pm-backlog-promoter agent successfully before stopping."
    )
    log(f"block degraded_promoter path={promoter_path}")
    emit_block(message)
    sys.exit(0)

if promoter.get("review_result"):
    if accepted == 0:
        closes, close_reason, fanout_path = review_result_closes(promoter)
        if closes:
            log(
                f"allow promoter_review_result_no_accepts hourly={hourly_path} "
                f"promoter={promoter_path} rejected={rejected} reason={close_reason} "
                f"fanout={fanout_path or 'none'}"
            )
            sys.exit(0)
        message = (
            "[READY_POOL_CONTRACT_FANOUT_REQUIRED] PM rejected all immediately promotable candidates, "
            f"but the source manifest still has unresolved Ready Pool contract candidates "
            f"({promoter_path}: {close_reason}). Parent PM must run: python3 "
            "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/ready-pool-contract-repair.py "
            f"--source-manifest {promoter_path} --apply . The coordinator launches the bounded "
            "deterministic contract repair, validates hash-pinned bodies, "
            "promotes completed contracts in the same run and continues through all eligible P0-P2 or "
            "genuine exhaustion. Every issue must finish as PROMOTED with verified apply proof, "
            "PARKED, NEEDS_CTO, or EXTERNAL_ACTION. Post each decision/action to its transition "
            "thread and write a complete "
            "pm-ready-pool-contract-fanout/v1 aggregate before stopping."
        )
        log(f"block review_result_contract_candidates path={promoter_path} reason={close_reason}")
        emit_block(message)
        sys.exit(0)
    message = (
        "[READY_POOL_LOW_PROMOTER_REQUIRED] PM reviewed backlog promoter candidates after hourly READY_POOL_LOW, "
        f"but the review-result manifest still has accepted={accepted} candidate(s) ({promoter_path}). "
        "Apply the accepted promotions with backlog-triage.py promote --apply, then use canonical direct-assign before stopping."
    )
    log(f"block review_result_has_accepts path={promoter_path} accepted={accepted}")
    emit_block(message)
    sys.exit(0)

if apply_mode:
    if promoted == 0:
        message = (
            "[READY_POOL_LOW_PROMOTER_REQUIRED] PM ran the backlog promoter after hourly READY_POOL_LOW, "
            f"but the apply manifest changed nothing ({promoter_path}: selected={selected}, "
            f"promoted=0, promotion_failed={promotion_failed}). "
            "This is not closure for a low Ready Pool. PM must rerun the background pm-backlog-promoter agent, "
            "write a review-result manifest if all candidates are rejected, or apply at least one verified promotion and reuse canonical direct-assign."
        )
        log(f"block apply_no_effect promoter={promoter_path} selected={selected} failed={promotion_failed}")
        emit_block(message)
        sys.exit(0)
    if promotion_failed and promoted == 0:
        message = (
            "[READY_POOL_LOW_PROMOTER_REQUIRED] PM ran the backlog promoter after hourly READY_POOL_LOW, "
            f"but the apply manifest reports promotion_failed={promotion_failed} and promoted=0 ({promoter_path}). "
            "Fix the promoter failure or rerun the background pm-backlog-promoter agent successfully before stopping."
        )
        log(f"block apply_failed promoter={promoter_path} failed={promotion_failed}")
        emit_block(message)
        sys.exit(0)
    reached, current, promoted_count, apply_target = apply_reaches_target(promoter)
    if not reached:
        message = (
            "[READY_POOL_LOW_PROMOTER_REQUIRED] PM applied some backlog promotions, "
            f"but the clean Ready Pool is still below target ({promoter_path}: "
            f"current_before={current}, promoted={promoted_count}, target={apply_target}). "
            "Continue the bounded promoter/contract-repair windows until the target is restored, "
            "or write a reviewed exhaustion result with completed contract-fanout proof before stopping."
        )
        log(
            f"block partial_apply hourly={hourly_path} promoter={promoter_path} "
            f"current={current} promoted={promoted_count} target={apply_target}"
        )
        emit_block(message)
        sys.exit(0)
    log(
        f"allow promoter_apply_target_reached hourly={hourly_path} promoter={promoter_path} "
        f"current={current} promoted={promoted_count} target={apply_target} failed={promotion_failed}"
    )
    sys.exit(0)

if selected or would_promote or needs_metadata or auto_promotable:
    message = (
        "[READY_POOL_LOW_PROMOTER_REQUIRED] PM only has a dry-run promoter manifest after hourly READY_POOL_LOW "
        f"({promoter_path}: selected={selected}, would_promote={would_promote}, "
        f"needs_ready_pool_metadata={needs_metadata}, auto_promotable={auto_promotable}). "
        "PM applies valid candidates without an architecture-review prerequisite. If contract candidates remain, "
        "parent PM must run: python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/"
        f"ready-pool-contract-repair.py --source-manifest {promoter_path} --apply . Then run "
        "canonical direct-assign before stopping."
    )
    log(f"block dry_run_has_candidates promoter={promoter_path} selected={selected} needs={needs_metadata}")
    emit_block(message)
    sys.exit(0)

log(f"allow promoter_no_candidates hourly={hourly_path} promoter={promoter_path}")
PYEOF
