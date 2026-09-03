#!/usr/bin/env bash
# Send one literal status message to the PM pane through MoP.
set -euo pipefail

usage() {
  printf 'Usage: %s [--file PATH | -- MESSAGE]\n' "$0" >&2
  exit 2
}

message=""
if [[ "${1:-}" == "--file" ]]; then
  [[ $# -eq 2 && -f "$2" ]] || { echo 'MESSAGE_PM_FAILED reason=file_not_found' >&2; exit 2; }
  message=$(<"$2") || { echo 'MESSAGE_PM_FAILED reason=message_read_failed' >&2; exit 2; }
else
  [[ "${1:-}" == "--" ]] && shift
  [[ $# -gt 0 ]] || usage
  message="$*"
fi

[[ -n "${message//[[:space:]]/}" ]] || { echo 'MESSAGE_PM_FAILED reason=empty_message' >&2; exit 2; }
[[ "$message" != '$ARGUMENTS' && "$message" != *'<complete message supplied'* ]] || {
  echo 'MESSAGE_PM_FAILED reason=unexpanded_message_placeholder' >&2
  exit 2
}
[[ "$message" != /* && "$message" != $'\033'* ]] || {
  echo 'MESSAGE_PM_FAILED reason=control_message_not_allowed' >&2
  exit 2
}

export MESSAGE_PM_BODY="$message"
python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

message = os.environ["MESSAGE_PM_BODY"]
raw_base = os.environ.get("MOP_BASE_URL", "http://127.0.0.1:3100")
parsed = urllib.parse.urlsplit(raw_base)
if (
    parsed.scheme != "http"
    or parsed.hostname != "127.0.0.1"
    or parsed.username is not None
    or parsed.password is not None
    or parsed.path not in ("", "/")
    or parsed.query
    or parsed.fragment
    or parsed.port is None
    or not 1 <= parsed.port <= 65535
):
    print("MESSAGE_PM_FAILED reason=non_loopback_endpoint", file=sys.stderr)
    raise SystemExit(2)
base = f"http://127.0.0.1:{parsed.port}"
payload = json.dumps(
    {"command": message, "force": True, "source": "message-pm"},
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")
request = urllib.request.Request(
    f"{base}/slots/0/send",
    data=payload,
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    method="POST",
)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("redirect_refused")

try:
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=12) as response:
        status = response.status
        raw = response.read()
except urllib.error.HTTPError as error:
    print(f"MESSAGE_PM_FAILED reason=http_{error.code}", file=sys.stderr)
    raise SystemExit(1)
except Exception as error:
    print(f"MESSAGE_PM_FAILED reason=transport_{type(error).__name__}", file=sys.stderr)
    raise SystemExit(75)

try:
    result = json.loads(raw.decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError):
    print("MESSAGE_PM_FAILED reason=invalid_json_receipt", file=sys.stderr)
    raise SystemExit(1)
expected_bytes = len(message.encode("utf-8"))
if not (
    status == 200
    and isinstance(result, dict)
    and result.get("success") is True
    and result.get("slot") == 0
    and result.get("verified") is True
    and result.get("bytes") == expected_bytes
):
    print("MESSAGE_PM_FAILED reason=unverified_delivery", file=sys.stderr)
    raise SystemExit(1)
print(f"MESSAGE_PM_SENT slot=0 verified=true bytes={expected_bytes}")
PY
