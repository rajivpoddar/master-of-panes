#!/usr/bin/env bash
# Stop hook: dev slots must notify PM before ending terminal turns.
#
# Applies only in heydonna-app-300N slot clones. It does not affect the PM
# heydonna-app clone or unrelated projects.
#
# If this is an occupied dev slot, require a recent real message-pm delivery
# before allowing the turn to close. Plain prose such as "message-pm: ..." is
# not a delivery. Terminal-looking prose remains a fallback when MoP state is
# unavailable or has already flipped idle before this hook runs.

set -euo pipefail

INPUT=$(cat 2>/dev/null || true)
CWD_NOW=$(pwd 2>/dev/null || echo "")

case "$CWD_NOW" in
  */heydonna-app-300[1-6]|*/heydonna-app-300[1-6]/*) ;;
  *) exit 0 ;;
esac

STOP_HOOK_ACTIVE=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    value = json.loads(sys.stdin.read() or "{}").get("stop_hook_active", False)
except Exception:
    value = False
print("true" if value is True else "false")
' 2>/dev/null || printf 'false')
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

TRANSCRIPT_PATH=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read() or "{}")
    print(d.get("transcript_path") or d.get("transcriptPath") or "")
except Exception:
    print("")
' 2>/dev/null || true)

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

RESULT=$(python3 - "$TRANSCRIPT_PATH" "$CWD_NOW" <<'PYEOF'
import json
from datetime import datetime, timezone
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import urllib.request

path, cwd = sys.argv[1], sys.argv[2]

events = []
try:
    with open(path) as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except Exception:
                continue
except Exception:
    print("ALLOW")
    raise SystemExit

assistant_texts = []
recent_raw = []
last_prompt_pos = 0

def is_real_user_prompt(obj):
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    if (
        obj.get("type") != "user"
        and msg.get("role") != "user"
    ) or obj.get("isMeta"):
        return False
    content = msg.get("content", obj.get("content"))
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return True
        return False
    return False

for pos, obj in enumerate(events):
    if is_real_user_prompt(obj):
        last_prompt_pos = pos

recent_events = events[last_prompt_pos:]
for obj in recent_events:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    recent_raw.append(raw)
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    role = msg.get("role") or obj.get("role") or obj.get("type")
    if role == "assistant" or obj.get("type") == "assistant":
        content = msg.get("content", obj.get("content"))
        if isinstance(content, str):
            assistant_texts.append(content)
        elif isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text") or "")
            if parts:
                assistant_texts.append("\n".join(parts))

last_text = assistant_texts[-1] if assistant_texts else ""
window = "\n".join(recent_raw)
last_prompt_ts = ""
if 0 <= last_prompt_pos < len(events):
    last_prompt_ts = str(events[last_prompt_pos].get("timestamp") or "")

slot = "unknown"
m = re.search(r"heydonna-app-300([1-6])", cwd)
if m:
    slot = m.group(1)
session_id = os.path.basename(path).removesuffix(".jsonl")
RECENT_PM_DELIVERY_GRACE_SECONDS = int(
    os.environ.get("SLOT_PM_NOTIFY_RECENT_GRACE_SECONDS", "300")
)

issue = "unknown"
branch = os.popen("git branch --show-current 2>/dev/null").read().strip()
m = re.search(r"([0-9]{3,5})", branch or "")
if m:
    issue = m.group(1)

def has_message_pm_delivery(text):
    sent_ok = r"(?:(?:✓|\\u2713) Sent to PM \(slot 0\) via HTTP:|SLOT_PM_SENT:)"
    has_mop_slot0 = (
        re.search(r"mcp__plugin_master-of-panes_mop__mop_send_to_slot", text)
        and re.search(r'"slot"\s*:\s*0', text)
    )
    has_attributed_mop_slot0 = (
        re.search(r'"attributionMcpTool"\s*:\s*"mop_send_to_slot"', text)
        and re.search(sent_ok, text)
    )
    has_fallback_send = (
        re.search(r"/slots/0/send", text)
        and re.search(r"force", text)
    )
    has_tool_result_send = re.search(
        r'"type":"tool_result".{0,2000}' + sent_ok,
        text,
    )
    return bool(
        has_mop_slot0
        or has_attributed_mop_slot0
        or has_fallback_send
        or has_tool_result_send
    )

def event_tool_uses(obj):
    containers = []
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    containers.append(msg.get("content"))
    containers.append(obj.get("content"))
    for content in containers:
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                yield item

def event_has_pm_delivery(obj):
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if has_message_pm_delivery(raw):
        return True
    for tool_use in event_tool_uses(obj):
        name = str(tool_use.get("name") or "")
        tool_input = tool_use.get("input")
        if tool_use_is_pm_delivery(name, tool_input):
            return True
    return False

def curl_fallback_targets_pm(command):
    return bool(curl_fallback_pm_texts(command))

def tool_use_is_pm_delivery(name, tool_input):
    if not isinstance(name, str):
        return False
    if "mop_send_to_slot" in name and tool_input_targets_pm(tool_input):
        return True
    if (
        name == "Bash"
        and isinstance(tool_input, dict)
        and curl_fallback_targets_pm(tool_input.get("command"))
    ):
        return True
    return False

def event_has_meaningful_work_tool(obj):
    for tool_use in event_tool_uses(obj):
        name = str(tool_use.get("name") or "").lower()
        if not name:
            continue
        if "mop_send_to_slot" in name or "master-of-panes" in name:
            continue
        if (
            name == "bash"
            and isinstance(tool_use.get("input"), dict)
            and curl_fallback_targets_pm(tool_use["input"].get("command"))
        ):
            continue
        if name in {"skill", "taskupdate", "todowrite", "exit_plan_mode"}:
            continue
        return True
    return False

def last_meaningful_work_marker():
    marker = (-1, "")
    for pos, obj in enumerate(recent_events):
        if event_has_meaningful_work_tool(obj):
            marker = (pos, str(obj.get("timestamp") or ""))
    return marker

def open_continuation_obligation(slot_id):
    db_path = os.environ.get("PM_OPS_DB") or os.path.expanduser(
        "~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db"
    )
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT id, updated_at, pr, issue, slot, required_action, evidence_json
            FROM obligations
            WHERE status='open'
              AND kind='slot_same_turn_continuation'
              AND COALESCE(target_type,'')='slot'
              AND COALESCE(target_id,'')=?
              AND COALESCE(slot, ?) = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (str(slot_id), int(slot_id), int(slot_id)),
        ).fetchone()
        con.close()
    except Exception:
        return None
    return dict(row) if row else None

def resolve_continuation_obligation(row, resolution):
    pm_ops = "/Users/rajiv/.claude/scripts/pm-ops.py"
    if not os.path.exists(pm_ops):
        return
    cmd = [
        sys.executable,
        pm_ops,
        "obligation-resolve",
        "--kind",
        "slot_same_turn_continuation",
        "--target-type",
        "slot",
        "--target-id",
        str(slot),
        "--slot",
        str(slot),
        "--reason",
        resolution,
        "--external-state",
        f"transcript={path}",
    ]
    if row.get("pr") is not None:
        cmd.extend(["--pr", str(row["pr"])])
    if row.get("issue") is not None:
        cmd.extend(["--issue", str(row["issue"])])
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
    except Exception:
        pass

def continuation_obligation_block_reason():
    row = open_continuation_obligation(slot)
    if not row:
        return ""
    try:
        evidence = json.loads(row.get("evidence_json") or "{}")
    except Exception:
        evidence = {}

    current_branch = branch or ""
    current_head = os.popen("git rev-parse HEAD 2>/dev/null").read().strip()
    expected_branch = str(evidence.get("branch") or "")
    expected_head = str(evidence.get("head_sha") or evidence.get("head") or "")

    if expected_branch and current_branch and expected_branch != current_branch:
        resolve_continuation_obligation(row, "slot branch changed after continuation obligation")
        return ""
    if expected_head and current_head and expected_head != current_head:
        resolve_continuation_obligation(row, "slot head advanced after continuation obligation")
        return ""

    baseline = parse_iso_ts(row.get("updated_at"))
    for obj in events:
        event_ts = parse_iso_ts(obj.get("timestamp"))
        if baseline and (event_ts is None or event_ts <= baseline):
            continue
        if event_has_meaningful_work_tool(obj):
            resolve_continuation_obligation(row, "meaningful foreground work observed after PM continuation")
            return ""

    action = row.get("required_action") or "Run the assigned foreground implementation/review action now."
    return (
        "[SLOT_CONTINUATION_REQUIRED] PM assigned a same-turn continuation "
        f"obligation. obligation={row['id']} slot={slot} "
        f"pr={row.get('pr') or 'unknown'} issue={row.get('issue') or issue} "
        f"head={expected_head or current_head or 'unknown'}. No meaningful foreground "
        f"work occurred after the obligation was created. Required action: {action} "
        "Do not send PM another status-only message; message-pm, MoP calls, task-list "
        "updates, and continuation prose do not satisfy this obligation."
    )

def has_fresh_transcript_pm_delivery():
    last_work_pos, _ = last_meaningful_work_marker()
    last_delivery_pos = -1
    for pos, obj in enumerate(recent_events):
        if event_has_pm_delivery(obj):
            last_delivery_pos = pos
    return last_delivery_pos >= 0 and last_delivery_pos > last_work_pos

def event_ts_after_prompt(ts):
    if not last_prompt_ts or not ts:
        return True
    return str(ts) >= last_prompt_ts

def parse_iso_ts(value):
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def payload_matches_current_session(payload):
    if not isinstance(payload, dict):
        return False
    payload_session = payload.get("session_id")
    payload_transcript = payload.get("transcript_path")
    if payload_session:
        return str(payload_session) == session_id
    if payload_transcript:
        return os.path.abspath(str(payload_transcript)) == os.path.abspath(path)
    # Older MoP rows may not include session fields. Only use them if the
    # timestamp is after this stop window's last prompt.
    return True

def tool_input_targets_pm(tool_input):
    if not isinstance(tool_input, dict):
        return False
    target = tool_input.get("slot")
    return str(target) == "0"

def curl_fallback_pm_texts(command):
    texts = []
    if not isinstance(command, str) or "/slots/0/send" not in command:
        return texts

    def resolve_curl_data(value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value.startswith("@") or value == "@-":
            return value
        payload_path = value[1:]
        if not os.path.isabs(payload_path):
            payload_path = os.path.join(cwd, payload_path)
        try:
            with open(payload_path, encoding="utf-8") as payload_file:
                return payload_file.read()
        except (OSError, UnicodeError):
            return ""

    def add_payload(value):
        if not isinstance(value, str) or not value.strip():
            return
        value = resolve_curl_data(value)
        if not value:
            return
        try:
            payload = json.loads(value)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        if str(payload.get("force")).lower() not in {"true", "1"}:
            return
        msg = payload.get("command") or payload.get("message") or payload.get("text")
        if isinstance(msg, str) and msg.strip():
            texts.append(msg.strip())

    try:
        argv = shlex.split(command)
    except Exception:
        argv = []

    for i, arg in enumerate(argv):
        if arg in {"-d", "--data", "--data-raw", "--data-binary"} and i + 1 < len(argv):
            add_payload(argv[i + 1])
        elif arg.startswith("--data="):
            add_payload(arg.split("=", 1)[1])
        elif arg.startswith("--data-raw="):
            add_payload(arg.split("=", 1)[1])
        elif arg.startswith("--data-binary="):
            add_payload(arg.split("=", 1)[1])

    # Some fallback forms build JSON through python from a shell MSG variable.
    # The raw HTTP response proves delivery, but Codex proof must validate the
    # delivered body itself; recover the message from the assignment when safe.
    if not texts and re.search(r"\bforce\b", command):
        msg_match = re.search(
            r'(?:^|\n)\s*MSG="(?P<msg>.*?)"',
            command,
            re.S,
        )
        if msg_match:
            msg = msg_match.group("msg").strip()
            if msg:
                texts.append(msg)

    return texts

def mop_db_pm_delivery_texts(slot_id):
    texts = []
    rows = mop_db_posttool_rows(slot_id)
    for row, event_payload in rows:
        if event_payload is None:
            event_payload = {}
        tool_name = row.get("tool_name") or event_payload.get("tool_name")
        tool_input = event_payload.get("tool_input")
        if (
            tool_name == "mcp__plugin_master-of-panes_mop__mop_send_to_slot"
            and tool_input_targets_pm(tool_input)
        ):
            value = tool_input.get("command") or tool_input.get("message") or tool_input.get("text")
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
        if (
            tool_name == "Skill"
            and isinstance(tool_input, dict)
            and str(tool_input.get("skill")) == "message-pm"
        ):
            value = tool_input.get("args") or tool_input.get("message") or tool_input.get("text")
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
    return texts

def mop_db_posttool_rows(slot_id, enforce_prompt_boundary=True):
    payload = os.environ.get("SLOT_PM_NOTIFY_TEST_EVENTS_JSON", "")
    if not payload:
        try:
            url = f"http://localhost:3100/events?slot={slot_id}&limit=120&type=PostToolUse"
            with urllib.request.urlopen(url, timeout=1.25) as resp:
                payload = resp.read().decode("utf-8", "replace")
        except Exception:
            return []
    try:
        data = json.loads(payload or "{}")
    except Exception:
        return []
    rows = data.get("events", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    rows = sorted(
        (row for row in rows if isinstance(row, dict)),
        key=lambda row: str(row.get("timestamp") or ""),
    )
    filtered = []
    for row in rows:
        if enforce_prompt_boundary and not event_ts_after_prompt(row.get("timestamp")):
            continue
        raw_payload = row.get("payload") or "{}"
        try:
            event_payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except Exception:
            event_payload = {}
        if not payload_matches_current_session(event_payload):
            continue
        filtered.append((row, event_payload))
    return filtered

def row_is_pm_delivery(row, event_payload):
    if event_payload is None:
        event_payload = {}
    tool_name = row.get("tool_name") or event_payload.get("tool_name")
    tool_input = event_payload.get("tool_input")
    if tool_use_is_pm_delivery(tool_name, tool_input):
        return True
    if (
        tool_name == "Skill"
        and isinstance(tool_input, dict)
        and str(tool_input.get("skill")) == "message-pm"
    ):
        return True
    return False

def has_mop_db_pm_delivery(slot_id):
    return any(row_is_pm_delivery(row, event_payload) for row, event_payload in mop_db_posttool_rows(slot_id))

def has_fresh_mop_db_pm_delivery(slot_id):
    _, last_work_ts = last_meaningful_work_marker()
    for row, event_payload in mop_db_posttool_rows(slot_id):
        ts = str(row.get("timestamp") or "")
        if last_work_ts and ts <= last_work_ts:
            continue
        if row_is_pm_delivery(row, event_payload):
            return True
    return False

def has_recent_mop_db_pm_delivery(slot_id):
    if RECENT_PM_DELIVERY_GRACE_SECONDS <= 0:
        return False
    cutoff = datetime.now(timezone.utc).timestamp() - RECENT_PM_DELIVERY_GRACE_SECONDS
    for row, event_payload in reversed(mop_db_posttool_rows(slot_id, enforce_prompt_boundary=False)):
        if not row_is_pm_delivery(row, event_payload):
            continue
        ts = parse_iso_ts(row.get("timestamp"))
        if ts is None:
            continue
        return ts.timestamp() >= cutoff
    return False

def recent_pm_message_texts():
    texts = []

    def add_text(value):
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())

    def add_sent_status_texts(value):
        if not isinstance(value, str):
            return
        for line in value.splitlines():
            m = re.search(
                r"(?:(?:✓|\\u2713) Sent to PM \(slot 0\) via HTTP:|SLOT_PM_SENT:)\s*(.+)$",
                line,
            )
            if m:
                add_text(m.group(1))

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("type") == "tool_result":
                content = obj.get("content")
                if isinstance(content, str):
                    add_sent_status_texts(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            add_sent_status_texts(item.get("text"))
                        elif isinstance(item, str):
                            add_sent_status_texts(item)
            if tool_input_targets_pm(obj):
                add_text(obj.get("command") or obj.get("message") or obj.get("text"))
            name = obj.get("name") or obj.get("tool_name") or obj.get("attributionMcpTool")
            tool_input = obj.get("input") or obj.get("tool_input")
            if (
                isinstance(name, str)
                and name == "Skill"
                and isinstance(tool_input, dict)
                and str(tool_input.get("skill")) == "message-pm"
            ):
                add_text(tool_input.get("args") or tool_input.get("message") or tool_input.get("text"))
            if (
                isinstance(name, str)
                and ("mop_send_to_slot" in name or name == "Skill")
                and tool_input_targets_pm(tool_input)
            ):
                add_text(tool_input.get("command") or tool_input.get("message") or tool_input.get("text"))
            if (
                isinstance(name, str)
                and name == "Bash"
                and isinstance(tool_input, dict)
            ):
                for text in curl_fallback_pm_texts(tool_input.get("command")):
                    add_text(text)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for obj in recent_events:
        walk(obj)
    texts.extend(mop_db_pm_delivery_texts(slot))

    # Slots often summarize the PM send in their final assistant text. Include
    # only the last few assistant texts so skill examples loaded earlier in the
    # transcript cannot trip this check.
    texts.extend(assistant_texts[-3:])

    deduped = []
    seen = set()
    for text in texts:
        if text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped

NEGATIVE_VERDICTS = {
    "REVISE",
    "REQUEST_CHANGES",
    "REJECT",
    "UNKNOWN",
    "NEEDS_REVISION",
    "NEEDS_DEEPER_INVESTIGATION",
    "MISDIAGNOSED",
}
POSITIVE_VERDICTS = {"APPROVE", "APPROVE_PENDING_CI"}

def has_positive_codex_claim(text):
    if not text:
        return False
    if re.search(r"\bnot\s+(?:a\s+)?(?:codex\s+)?approv", text, re.I):
        return False
    if re.search(r"\bcodex\s+(?:approve|approved|approve_pending_ci)\b", text, re.I):
        return True
    if re.search(r"\bcodex\s+review\s+complete\b", text, re.I) and re.search(
        r"\b(?:ready|qa\s+passed|awaiting\s+ci|awaiting-ci|apply\s+pm-state:qa-passed-awaiting-ci)\b",
        text,
        re.I,
    ):
        return True
    if "codex" in text.lower() and re.search(
        r"\b(?:plan\s+(?:approved|complete|ready)|pr\s+(?:ready|approved)|qa\s+passed\s+awaiting\s+ci)\b",
        text,
        re.I,
    ):
        return True
    return False

def marker_paths_for_text(text):
    paths = re.findall(r"/tmp/codex-app-(?:plan|code)-review-\d+\.txt", text)
    if paths:
        return list(dict.fromkeys(paths))

    candidates = []
    pr_matches = re.findall(r"\bPR\s*#?\s*(\d{3,6})\b", text, flags=re.I)
    issue_matches = re.findall(r"\bissue\s*#?\s*(\d{3,6})\b", text, flags=re.I)
    if "plan" in text.lower() and issue_matches:
        candidates.extend(f"/tmp/codex-app-plan-review-{n}.txt" for n in issue_matches)
    if pr_matches:
        candidates.extend(f"/tmp/codex-app-code-review-{n}.txt" for n in pr_matches)
    for n in re.findall(r"#(\d{3,6})\b", text):
        if "plan" in text.lower():
            candidates.append(f"/tmp/codex-app-plan-review-{n}.txt")
        if "pr" in text.lower() or "qa" in text.lower() or "ci" in text.lower():
            candidates.append(f"/tmp/codex-app-code-review-{n}.txt")
    return [p for p in dict.fromkeys(candidates) if os.path.exists(p)]

def marker_field(marker_text, name):
    m = re.search(rf"^{re.escape(name)}:\s*([A-Z0-9_\-]+)\s*$", marker_text, re.I | re.M)
    return m.group(1).strip().upper() if m else ""

def text_names_verdict(text, field, verdict):
    return bool(re.search(rf"\b{re.escape(field)}\s*[:=]\s*{re.escape(verdict)}\b", text, re.I))

def has_codex_marker_proof_status(text):
    return bool(
        marker_paths_for_text(text)
        and re.search(r"\bCOMPANION_VERDICT\s*[:=]", text, re.I)
        and re.search(r"\bFINAL_REVIEWER_VERDICT\s*[:=]", text, re.I)
    )

def has_negative_codex_status(text):
    if not marker_paths_for_text(text):
        return False
    if not re.search(r"\b(?:COMPANION_VERDICT|FINAL_REVIEWER_VERDICT)\s*[:=]\s*(?:REVISE|REQUEST_CHANGES|REJECT|UNKNOWN|NEEDS_REVISION|NEEDS_DEEPER_INVESTIGATION|MISDIAGNOSED)\b", text, re.I):
        return False
    return bool(re.search(r"\b(?:standing by|adjudicat\w*|PM-Claude|PM_CLAUDE|PM-Opus|PM_OPUS|no action|blocked|rework|scope-conflation|request_changes)\b", text, re.I))

def negative_reported_marker_paths(texts):
    cleared = set()
    for text in texts:
        if has_positive_codex_claim(text):
            continue
        if not has_negative_codex_status(text):
            continue
        if not re.search(r"\bCOMPANION_VERDICT\s*[:=]", text, re.I):
            continue
        if not re.search(r"\bFINAL_REVIEWER_VERDICT\s*[:=]", text, re.I):
            continue
        for marker_path in marker_paths_for_text(text):
            cleared.add(marker_path)
    return cleared

def has_codex_terminal_status(text):
    if not text:
        return False
    if has_positive_codex_claim(text):
        return False
    has_pr_context = bool(
        marker_paths_for_text(text)
        or re.search(r"\b(?:PR|issue)\s*#?\s*\d{3,6}\b", text, re.I)
        or re.search(r"#\d{3,6}\b", text)
    )
    if not has_pr_context:
        return False
    has_codex_context = bool(
        re.search(r"\b(?:codex|COMPANION_VERDICT|FINAL_REVIEWER_VERDICT|PM_ADJUDICATION_REQUIRED|zen-review|zen\s+APPROVE)\b", text, re.I)
        or re.search(r"/tmp/(?:codex-app|zen)-code-review-\d+\.txt", text)
    )
    has_terminal_language = bool(re.search(
        r"\b(?:PM_ADJUDICATION_REQUIRED|stand(?:ing)?\s+down|standby|standing\s+by|"
        r"holding|PM\s+owns|PM\s+directive|blocked|rework|codex\s+402|"
        r"insufficient\s+balance|hook\s+loop|deadlock|corrupted\s+marker|"
        r"awaiting\s+new\s+assignment|waiting\s+for\s+new\s+work)\b",
        text,
        re.I,
    ))
    return has_codex_context and has_terminal_language

def codex_verdict_block_reason():
    def validate_positive_text(text):
        marker_paths = marker_paths_for_text(text)
        if not marker_paths:
            return (
                "[SLOT_CODEX_VERDICT_PROOF_REQUIRED] Positive Codex/ready claim "
                "without a /tmp/codex-app-* marker path. Include marker path, "
                "MARKER_PROVENANCE, TIMESTAMP, COMPANION_VERDICT, and "
                "FINAL_REVIEWER_VERDICT, or retract the approval claim."
            )

        for marker_path in marker_paths:
            try:
                with open(marker_path) as f:
                    marker_text = f.read()
            except Exception as exc:
                return f"[SLOT_CODEX_VERDICT_MARKER_UNREADABLE] {marker_path}: {exc}"

            if "$(date +%s)" in marker_text:
                return (
                    "[SLOT_CODEX_VERDICT_MARKER_INVALID] Marker contains literal "
                    f"'$(date +%s)' and is not trustworthy: {marker_path}"
                )

            provenance = marker_field(marker_text, "MARKER_PROVENANCE")
            top_verdict = marker_field(marker_text, "VERDICT")
            companion_verdict = marker_field(marker_text, "COMPANION_VERDICT") or top_verdict
            final_verdict = marker_field(marker_text, "FINAL_REVIEWER_VERDICT") or companion_verdict
            timestamp = marker_field(marker_text, "TIMESTAMP")

            if provenance != "CODEX-REVIEW-COMPANION":
                return (
                    "[SLOT_CODEX_VERDICT_PROVENANCE_REQUIRED] Positive Codex/ready "
                    f"claim relies on marker without MARKER_PROVENANCE: "
                    f"codex-review-companion: {marker_path}. Rerun the reviewer "
                    "with the current companion or report PM_ADJUDICATION_REQUIRED."
                )
            if not timestamp or not timestamp.isdigit():
                return (
                    "[SLOT_CODEX_VERDICT_TIMESTAMP_REQUIRED] Positive Codex/ready "
                    f"claim relies on marker without numeric TIMESTAMP: {marker_path}"
                )
            if companion_verdict in NEGATIVE_VERDICTS or final_verdict in NEGATIVE_VERDICTS:
                return (
                    "[SLOT_CODEX_VERDICT_NEGATIVE] Positive Codex/ready claim "
                    f"contradicts marker {marker_path}: "
                    f"COMPANION_VERDICT={companion_verdict or 'missing'} "
                    f"FINAL_REVIEWER_VERDICT={final_verdict or 'missing'}."
                )

            body_negative = re.search(
                r"Companion verdict:\s*(REQUEST_CHANGES|REVISE|REJECT|UNKNOWN|NEEDS_REVISION|NEEDS_DEEPER_INVESTIGATION|MISDIAGNOSED)",
                marker_text,
                re.I,
            )
            if body_negative:
                return (
                    "[SLOT_CODEX_VERDICT_ADJUDICATION_REQUIRED] Positive "
                    f"Codex/ready claim contradicts marker body in {marker_path}: "
                    f"Companion verdict: {body_negative.group(1).upper()}. "
                    "Report the companion verdict verbatim and ask PM to adjudicate."
                )

            if companion_verdict not in POSITIVE_VERDICTS or final_verdict not in POSITIVE_VERDICTS:
                return (
                    "[SLOT_CODEX_VERDICT_UNKNOWN] Positive Codex/ready claim "
                    f"relies on unsupported marker verdicts in {marker_path}: "
                    f"COMPANION_VERDICT={companion_verdict or 'missing'} "
                    f"FINAL_REVIEWER_VERDICT={final_verdict or 'missing'}."
                )
            if marker_path not in text:
                return (
                    "[SLOT_CODEX_VERDICT_PROOF_REQUIRED] Positive Codex/ready "
                    f"claim must name the marker path verbatim: {marker_path}"
                )
            if not text_names_verdict(text, "COMPANION_VERDICT", companion_verdict):
                return (
                    "[SLOT_CODEX_VERDICT_PROOF_REQUIRED] Positive Codex/ready "
                    f"claim must include exact COMPANION_VERDICT={companion_verdict} "
                    f"from {marker_path}."
                )
            if not text_names_verdict(text, "FINAL_REVIEWER_VERDICT", final_verdict):
                return (
                    "[SLOT_CODEX_VERDICT_PROOF_REQUIRED] Positive Codex/ready "
                    f"claim must include exact FINAL_REVIEWER_VERDICT={final_verdict} "
                    f"from {marker_path}."
                )
            return ""
        return ""

    texts = recent_pm_message_texts()
    negative_reported_markers = negative_reported_marker_paths(texts + [window])
    first_reason = ""
    for text in reversed(texts):
        positive_claim = has_positive_codex_claim(text)
        marker_paths = marker_paths_for_text(text)
        if positive_claim and marker_paths and all(
            marker_path in negative_reported_markers for marker_path in marker_paths
        ):
            continue
        if has_codex_terminal_status(text):
            return ""
        if has_negative_codex_status(text) and not positive_claim:
            return ""
        if not positive_claim and not has_codex_marker_proof_status(text):
            continue
        reason = validate_positive_text(text)
        if not reason:
            return ""
        if not first_reason:
            first_reason = reason
    return first_reason

PROMISE_AUDIT = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/slot-promised-action-audit.py"

def promised_action_block_reason():
    if not os.path.exists(PROMISE_AUDIT):
        return ""
    try:
        import subprocess
        proc = subprocess.run(
            [sys.executable, PROMISE_AUDIT, "stop", "--transcript", path, "--cwd", cwd],
            text=True,
            capture_output=True,
            timeout=2.0,
        )
    except Exception:
        return ""
    out = (proc.stdout or "").strip()
    if out.startswith("BLOCK\t"):
        return out.split("\t", 1)[1]
    return ""

def read_mop_slot_state_for_ready_check(slot_id):
    payload = os.environ.get("SLOT_PM_NOTIFY_TEST_SLOTS_JSON", "")
    if not payload:
        try:
            with urllib.request.urlopen("http://localhost:3100/slots", timeout=1.25) as resp:
                payload = resp.read().decode("utf-8", "replace")
        except Exception:
            return None
    try:
        data = json.loads(payload or "{}")
    except Exception:
        return None
    slots = data.get("slots", data) if isinstance(data, dict) else data
    if not isinstance(slots, list):
        return None
    return next(
        (
            item
            for item in slots
            if isinstance(item, dict) and str(item.get("slot")) == str(slot_id)
        ),
        None,
    )

def successful_completion_claim(text):
    if not isinstance(text, str) or not text.strip():
        return False
    if re.search(
        r"(?is)\b(?:ESCALATION|blocked|cannot continue|request_changes|revise|reject)\b",
        text,
    ):
        return False
    return bool(
        re.search(
            r"(?is)(?:\ball\s+\d+\s+.*\bclosed\b|\bimplementation complete\b|"
            r"\btask complete\b|\bQA\s+(?:pass|passed)\b|\bready for (?:PM|review|approval)\b|"
            r"\bcomplete for this session\b|\ball tests pass(?:ed)?\b)",
            text,
        )
    )

def no_push_test_only_completion(text):
    """Allow an explicitly completed local test-only turn without retired proof tooling."""
    if not isinstance(text, str) or not text.strip():
        return False
    # This is a narrow exception for a completed local test-only turn, not a
    # keyword waiver.  Keep mixed PR/readiness claims and unresolved work on
    # the normal current-head slot-ready path.
    if re.search(
        r"(?is)\b(?:not|non)\s+(?:a\s+)?test[- ]only\b|"
        r"\b(?:unfinished|incomplete|pending|in[- ]progress)\b|"
        r"\b(?:not|never)\s+(?:complete|completed|finished|done)\b|"
        r"\b(?:implementation|production|feature|pull\s+request|PR)\b|"
        r"\bready\s+for\s+(?:PM|review|approval)\b",
        text,
    ):
        return False
    completed_scope = re.search(
        r"(?is)\b(?:task|turn|work|commit|checks?|tests?)\b[^.?!]{0,120}"
        r"\b(?:complete|completed|finished|done|passed)\b",
        text,
    )
    local_test_only = re.search(r"(?is)\btest[- ]only\b", text)
    no_push_or_local = re.search(
        r"(?is)\b(?:no[- ]push(?:ed)?\s+(?:was\s+)?(?:required|requested|needed)|"
        r"not pushed|without pushing|local[- ]only)\b",
        text,
    )
    return bool(completed_scope and local_test_only and no_push_or_local)

def current_head_ready_packet(slot_id, pr, head):
    event_dir = os.environ.get("SLOT_READY_EVENT_DIR", "/tmp/slot-ready-events")
    try:
        names = os.listdir(event_dir)
    except OSError:
        return ""
    for name in names:
        if not name.endswith(".json"):
            continue
        event_path = os.path.join(event_dir, name)
        try:
            with open(event_path, encoding="utf-8") as f:
                event = json.load(f)
        except Exception:
            continue
        if str(event.get("slot") or "") != str(slot_id):
            continue
        if pr and str(event.get("pr") or "") != str(pr):
            continue
        event_head = str(event.get("head_sha") or event.get("headRefOid") or "")
        if head and event_head != head:
            continue
        if str(event.get("status") or "pending") not in {"pending", "consumed"}:
            continue
        return event_path
    return ""

has_recent_pm_delivery = has_recent_mop_db_pm_delivery(slot)
continuation_reason = continuation_obligation_block_reason()
if continuation_reason:
    print("BLOCK\t" + continuation_reason)
    raise SystemExit

has_fresh_transcript_delivery = has_fresh_transcript_pm_delivery()
has_any_pm_delivery = (
    has_message_pm_delivery(window)
    or has_fresh_transcript_delivery
    or has_mop_db_pm_delivery(slot)
    or has_recent_pm_delivery
)
has_fresh_pm_delivery = has_fresh_transcript_delivery or has_fresh_mop_db_pm_delivery(slot)

if has_any_pm_delivery:
    codex_reason = codex_verdict_block_reason()
    if codex_reason:
        print("BLOCK\t" + codex_reason)
        raise SystemExit
    promise_reason = promised_action_block_reason()
    if promise_reason:
        print("BLOCK\t" + promise_reason)
        raise SystemExit
    completion_texts = [last_text] + recent_pm_message_texts()
    completion_text = next(
        (text for text in completion_texts if successful_completion_claim(text)),
        "",
    )
    if completion_text:
        ready_slot_state = read_mop_slot_state_for_ready_check(slot)
        ready_pr = ready_slot_state.get("pr") if isinstance(ready_slot_state, dict) else None
        if ready_pr not in (None, "", "null", "unknown"):
            ready_head = os.popen("git rev-parse HEAD 2>/dev/null").read().strip()
            ready_packet = current_head_ready_packet(slot, ready_pr, ready_head)
            if not ready_packet and not no_push_test_only_completion(completion_text):
                print(
                    "BLOCK\t[SLOT_READY_PACKET_UNAVAILABLE] Successful PR completion "
                    "cannot be closed without authoritative current-head slot-ready evidence; "
                    "the retired slot-submit-ready helper is unavailable and is not invoked. "
                    f"slot={slot} pr={ready_pr} issue={issue} "
                    f"head={ready_head or 'unknown'}. Preserve the work and report the "
                    "missing current-head evidence to PM."
                )
                raise SystemExit
    if has_fresh_pm_delivery or has_recent_pm_delivery:
        print("ALLOW")
        raise SystemExit

def read_mop_slot_state(slot_id):
    payload = os.environ.get("SLOT_PM_NOTIFY_TEST_SLOTS_JSON", "")
    if not payload:
        try:
            with urllib.request.urlopen("http://localhost:3100/slots", timeout=1.25) as resp:
                payload = resp.read().decode("utf-8", "replace")
        except Exception:
            return None
    try:
        data = json.loads(payload or "{}")
    except Exception:
        return None
    slots = data.get("slots", data) if isinstance(data, dict) else data
    if not isinstance(slots, list):
        return None
    for item in slots:
        if not isinstance(item, dict):
            continue
        if str(item.get("slot")) == str(slot_id):
            return item
    return None

slot_state = read_mop_slot_state(slot)
slot_occupied = None
slot_dnd = None
mop_issue = None
mop_branch = None
if isinstance(slot_state, dict):
    slot_occupied = bool(slot_state.get("occupied"))
    slot_dnd = bool(slot_state.get("dnd"))
    mop_issue = slot_state.get("issue")
    mop_branch = slot_state.get("branch")

terminal_re = re.compile(
    r"(?is)("
    r"\bSTOP\b|"
    r"\bdone\.\s+here(?:'s|\s+is)\s+(?:the\s+)?summary\b|"
    r"\bescalat(?:e|ing|ed)\b|"
    r"\bstopping\b|"
    r"\bdone with\b|"
    r"\btask complete\b|"
    r"\bimplementation complete\b|"
    r"\bfinished(?:\s+with)?\s+#?\d+\b|"
    r"\bplan\s+v?\d+\b.*\b(?:pushed|ready|at)\b|"
    r"\bplan\b.*\b(?:pushed|ready for PM|awaiting PM|PM approval)\b|"
    r"\bround\s+\d+\b.*\b(?:past|exceed(?:ed|ing)?)\b.*\bcap\b|"
    r"\bpast\s+3-round\s+cap\b|"
    r"\bexceed(?:ed|ing)?\s+(?:the\s+)?(?:review\s+)?round\s+cap\b|"
    r"\bPR\s*#?\d+\s+(?:ready|created|opened)|"
    r"\bdraft PR\b|"
    r"\bQA\s+(?:passed|pass)\b|"
    r"\bready for (?:PM|review|mark-ready|approval)|"
    r"\bawait(?:ing)? (?:PM|approval|guidance)|"
    r"\bwaiting for (?:PM|approval|guidance)|"
    r"\brequest(?:ing|s)? PM\b|"
    r"\bPM direction\b|"
    r"\bPM\s+adjudication\s+(?:needed|required)\b|"
    r"\b(?:Codex|companion)\s+(?:returned\s+)?(?:REJECT|REVISE|REQUEST_CHANGES)\b|"
    r"\bblocked\b|"
    r"\bcannot continue\b|"
    r"\bneed(?:s)? PM\b"
    r")"
)

terminal_match = bool(terminal_re.search(last_text))
active_feature_branch = bool(re.search(r"(^|[/_-])[0-9]{3,5}([/_-]|$)", branch or ""))

enforce_reasons = []
if os.environ.get("SLOT_PM_NOTIFY_FORCE_ENFORCE") == "1":
    enforce_reasons.append("pi_native_stop")
if slot_occupied is True:
    enforce_reasons.append("mop_occupied_slot")
if terminal_match:
    enforce_reasons.append("terminal_text")
if slot_occupied is None and active_feature_branch and last_text.strip():
    enforce_reasons.append("mop_unknown_feature_branch")

if not enforce_reasons:
    print("ALLOW")
    raise SystemExit

snippet = re.sub(r"\s+", " ", last_text).strip()[:280]
reason = (
    "[SLOT_PM_NOTIFY_REQUIRED] Dev slot is trying to stop without a "
    "PM delivery. "
    f"slot={slot} issue={mop_issue or issue} branch={branch or 'unknown'} "
    f"mop_issue={mop_issue or 'unknown'} mop_branch={mop_branch or 'unknown'} "
    f"source={'+'.join(enforce_reasons)} occupied={slot_occupied} dnd={slot_dnd} "
    f"latest='{snippet}'"
)
print("BLOCK\t" + reason)
PYEOF
)

if [ "$RESULT" = "ALLOW" ]; then
  exit 0
fi

REASON=${RESULT#BLOCK	}
REASON="$REASON" python3 - <<'PYEOF'
import json
import os
import re

reason = os.environ.get("REASON", "")
if "[SLOT_CONTINUATION_REQUIRED]" in reason:
    reason += (
        "\n\nRequired next action: perform the assigned foreground work now. "
        "Do not invoke Skill(message-pm) again unless a concrete external blocker "
        "exists; in that case send an ESCALATION with the blocker proof."
    )
elif "[SLOT_PROMISE_ACTION_REQUIRED]" in reason:
    reason += (
        "\n\nRequired next action: run the promised foreground tool/action now. "
        "If the promise was wrong, invoke Skill(message-pm) and send PM a "
        "corrected status that explicitly retracts the promise and gives the "
        "real blocker/state proof."
    )
else:
    reason += (
        "\n\nRequired next action: send PM a concise terminal status using "
        "Skill(message-pm) or direct MoP PM delivery with prefix 'slot N (Name): ...'. "
        "For true blocks, begin the message body with 'ESCALATION:'. Include "
        "PR/issue, branch/head SHA, validation result, and exact next PM action. "
        "If the status relies on Codex review, include marker path, "
        "MARKER_PROVENANCE, TIMESTAMP, COMPANION_VERDICT, and "
        "FINAL_REVIEWER_VERDICT exactly from the marker. Then stop."
    )
print(json.dumps({"decision": "block", "message": reason, "reason": reason}))
PYEOF
exit 2
