#!/usr/bin/env python3
"""Build one fail-closed snapshot of unhandled CTO Slack fallback events."""

import argparse
import hashlib
import datetime
import fcntl
import json
import os
import re
import tempfile
import time
import uuid
import math
from pathlib import Path
from typing import Any


DEFAULT_EVENTS_FILE = Path("/tmp/cto-slack-events.json")
DEFAULT_ACK_FILE = Path("/tmp/cto-slack-ack.json")
DEFAULT_OUTPUT_FILE = Path("/tmp/cto-slack-relay-snapshot.json")
DEFAULT_HEARTBEAT_FILE = Path("/tmp/cto-slack-relay-heartbeat")
DEFAULT_CLAIMS_FILE = Path("/tmp/cto-slack-relay-claims.json")
DEFAULT_LOCK_FILE = Path("/tmp/cto-slack-relay.lock")
DEFAULT_CLAIM_TTL_SECONDS = 120.0
DEFAULT_DELIVERY_RECEIPTS_FILES = (
    Path("/tmp/cto-ipc-delivery-receipts.jsonl"),
    Path("/tmp/cto-app-server-delivery-receipts.jsonl"),
)
ROUTE_TABLE_BEGIN = "<!-- CTO_SLACK_ROUTE_TABLE_V2"
ROUTE_TABLE_END = "CTO_SLACK_ROUTE_TABLE_END -->"
CTO_MENTION = re.compile(r"<@(?P<user_id>[^|>\r\n]+)(?:\|[^>\r\n]*)?>")


class SnapshotError(RuntimeError):
    """The durable relay state could not be interpreted safely."""


def require_route(value: Any, scope: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SnapshotError(f"{scope} must be an object")
    route: dict[str, str] = {}
    for field in ("project", "destination_thread_id", "consumer_sop_path"):
        field_value = value.get(field)
        if not isinstance(field_value, str) or not field_value:
            raise SnapshotError(f"{scope}.{field} must be a non-empty string")
        route[field] = field_value
    return route


def extract_route_table(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SnapshotError(f"unreadable route SOP: {path}: {error}") from error
    before, marker, remainder = text.partition(ROUTE_TABLE_BEGIN)
    del before
    if not marker:
        raise SnapshotError(f"route SOP has no {ROUTE_TABLE_BEGIN} marker")
    payload, marker, after = remainder.partition(ROUTE_TABLE_END)
    del after
    if not marker:
        raise SnapshotError(f"route SOP has no {ROUTE_TABLE_END} marker")
    try:
        table = json.loads(payload.strip())
    except json.JSONDecodeError as error:
        raise SnapshotError(f"route SOP table is invalid JSON: {error}") from error
    if not isinstance(table, dict) or table.get("version") != 2:
        raise SnapshotError("route SOP table must be a version 2 object")
    exact = table.get("exact_channel_routes")
    if not isinstance(exact, dict):
        raise SnapshotError("route SOP exact_channel_routes must be an object")
    normalized_exact: dict[str, dict[str, str]] = {}
    for channel, route in exact.items():
        if not isinstance(channel, str) or not channel:
            raise SnapshotError("route SOP exact channel must be a non-empty string")
        normalized_exact[channel] = require_route(
            route, f"route SOP exact_channel_routes.{channel}"
        )
        if "admitted_after_epoch" in route:
            for field in ("admitted_after_epoch", "admitted_before_epoch"):
                value = route.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
                    raise SnapshotError(f"route SOP {channel}.{field} must be a positive epoch or null")
                normalized_exact[channel][field] = value
    mention_only = table.get("mention_only_channels")
    if not isinstance(mention_only, dict):
        raise SnapshotError("route SOP mention_only_channels must be an object")
    normalized_mentions: dict[str, str] = {}
    for channel, user_id in mention_only.items():
        if not isinstance(channel, str) or not channel:
            raise SnapshotError("route SOP mention-only channel must be a non-empty string")
        if not isinstance(user_id, str) or not user_id:
            raise SnapshotError(f"route SOP mention_only_channels.{channel} must be a non-empty user ID")
        normalized_mentions[channel] = user_id
    return {
        "version": 2,
        "exact_channel_routes": normalized_exact,
        "mention_only_channels": normalized_mentions,
        "direct_message_route": require_route(
            table.get("direct_message_route"), "route SOP direct_message_route"
        ),
        "default_route": require_route(table.get("default_route"), "route SOP default_route"),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def rendered_text_has_user_mention(text: str, user_id: str) -> bool:
    without_quoted_lines = "\n".join(
        line for line in text.splitlines() if not re.match(r"^\s*>", line)
    )
    without_code = re.sub(r"```[\s\S]*?```", "", without_quoted_lines)
    without_code = re.sub(r"`[^`\r\n]*`", "", without_code)
    return any(match.group("user_id") == user_id for match in CTO_MENTION.finditer(without_code))


def block_has_user_mention(block: Any, user_id: str) -> bool:
    if not isinstance(block, dict):
        return False
    block_type = block.get("type") if isinstance(block.get("type"), str) else ""
    style = block.get("style") if isinstance(block.get("style"), dict) else {}
    if block_type in {"rich_text_quote", "blockquote", "rich_text_preformatted"} or style.get("code") is True:
        return False
    if block_type == "user" and block.get("user_id") == user_id:
        return True
    text_block = block.get("text")
    if isinstance(text_block, dict) and block_has_user_mention(text_block, user_id):
        return True
    if block_type == "mrkdwn" and isinstance(text_block, str):
        if rendered_text_has_user_mention(text_block, user_id):
            return True
    elements = block.get("elements")
    return isinstance(elements, list) and any(
        block_has_user_mention(element, user_id) for element in elements
    )


def event_has_current_user_mention(event: dict[str, Any], user_id: str) -> bool:
    evidence = event.get("source_evidence")
    if isinstance(evidence, dict):
        attestation = evidence.get("explicit_cto_mention")
        if isinstance(attestation, bool):
            return attestation
    tuple_value = event.get("exact_tuple")
    if not isinstance(tuple_value, dict):
        return False
    text = tuple_value.get("text")
    if isinstance(text, str) and text:
        return rendered_text_has_user_mention(text, user_id)
    blocks = tuple_value.get("blocks")
    if isinstance(blocks, list) and any(block_has_user_mention(block, user_id) for block in blocks):
        return True
    # For legacy envelopes, Slack's app_mention event itself is authoritative
    # only when no conflicting visible text or blocks were retained.
    return (
        isinstance(evidence, dict)
        and evidence.get("slack_event_type") == "app_mention"
        and not text
        and not blocks
    )


def channel_override_applies(event: dict[str, Any], route: dict[str, Any]) -> bool:
    """A temporal override changes new admissions only, never queued history."""
    if "admitted_after_epoch" not in route:
        return True
    after = route["admitted_after_epoch"]
    if after is None:  # Source candidate is inert until the scoped install stamps it.
        return False
    try:
        source_epoch = float(event["exact_tuple"]["ts"])
        queued_epoch = datetime.datetime.fromisoformat(event["queued_at"].replace("Z", "+00:00"))
        if queued_epoch.tzinfo is None or not math.isfinite(source_epoch):
            return False
        admission = queued_epoch.timestamp()
    except (KeyError, ValueError, TypeError, AttributeError):
        return False
    before = route.get("admitted_before_epoch")
    return source_epoch > after and admission > after and (before is None or max(source_epoch, admission) <= before)


def event_eligible_for_route(event: dict[str, Any], table: dict[str, Any]) -> bool:
    tuple_value = event.get("exact_tuple")
    if not isinstance(tuple_value, dict):
        return True
    channel = tuple_value.get("channel")
    if not isinstance(channel, str):
        return True
    mention_user = table["mention_only_channels"].get(channel)
    override = table["exact_channel_routes"].get(channel)
    if override and not channel_override_applies(event, override):
        return True  # Pre-cutover events retain their prior eligibility and route.
    if override and "admitted_after_epoch" in override:
        # Growth requires actual current-source mention text/blocks, never the
        # legacy empty app_mention fallback or a preview/parent attestation.
        text = tuple_value.get("text")
        blocks = tuple_value.get("blocks")
        return (isinstance(text, str) and rendered_text_has_user_mention(text, mention_user)) or (
            isinstance(blocks, list) and any(block_has_user_mention(block, mention_user) for block in blocks)
        )
    return not mention_user or event_has_current_user_mention(event, mention_user)


def route_event(
    event: dict[str, Any], table: dict[str, Any], route_sop_path: Path
) -> dict[str, Any] | None:
    tuple_value = event.get("exact_tuple")
    if not isinstance(tuple_value, dict):
        raise SnapshotError("event has no exact_tuple object")
    channel = tuple_value.get("channel")
    if not isinstance(channel, str) or not channel:
        raise SnapshotError("event exact_tuple.channel must be a non-empty string")
    if not event_eligible_for_route(event, table):
        return None
    source_sop = str(route_sop_path)
    if event.get("sop_path") != source_sop:
        raise SnapshotError(f"event {event.get('dedup_key')} has unexpected relay SOP path")
    wake_text = event.get("wake_text")
    if not isinstance(wake_text, str):
        raise SnapshotError(f"event {event.get('dedup_key')} has no wake_text")
    first_line, separator, remainder = wake_text.partition("\n")
    if first_line != f"SOP path: {source_sop}":
        raise SnapshotError(f"event {event.get('dedup_key')} wake_text has wrong SOP pointer")

    exact_routes = table["exact_channel_routes"]
    source_evidence = event.get("source_evidence")
    slack_event_type = (
        source_evidence.get("slack_event_type")
        if isinstance(source_evidence, dict)
        else None
    )
    if channel in exact_routes and channel_override_applies(event, exact_routes[channel]):
        selected = exact_routes[channel]
        route_match = "exact_channel"
    elif slack_event_type == "message.im":
        selected = table["direct_message_route"]
        route_match = "direct_message"
    else:
        selected = table["default_route"]
        route_match = "default"
    routed = dict(event)
    routed["relay_route"] = {
        **selected,
        "match": route_match,
        "source_channel": channel,
        "classifier_sop_path": source_sop,
        "classifier_sop_sha256": table["sha256"],
    }
    routed["routed_wake_text"] = (
        f"SOP path: {selected['consumer_sop_path']}" + separator + remainder
    )
    if channel in exact_routes and "admitted_after_epoch" in exact_routes[channel] and channel_override_applies(event, exact_routes[channel]):
        routed["routed_wake_text"] += "\n\nFrozen source envelope (conversation data):\n" + json.dumps(event, ensure_ascii=False)
    return routed


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SnapshotError(f"missing state file: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise SnapshotError(f"unreadable state file: {path}: {error}") from error


def extract_events(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        events = value
    elif isinstance(value, dict) and isinstance(value.get("events"), list):
        events = value["events"]
    else:
        raise SnapshotError("events state must be a list or an object with an events list")

    if not all(isinstance(event, dict) for event in events):
        raise SnapshotError("every event must be an object")
    return events


def extract_handled(value: Any) -> set[str]:
    if not isinstance(value, dict) or not isinstance(value.get("handled"), list):
        raise SnapshotError("ack state must be an object with a handled list")
    if not all(isinstance(item, str) for item in value["handled"]):
        raise SnapshotError("every handled entry must be a string")
    return set(value["handled"])


def receipt_handled_keys(paths: list[Path]) -> set[str]:
    """Read existing acceptance/unknown receipts; never retry or rewrite them."""
    handled: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            rows = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise SnapshotError(f"unreadable delivery receipts: {path}: {error}") from error
        for row in rows:
            if not row.strip():
                continue
            try:
                receipt = json.loads(row)
            except json.JSONDecodeError as error:
                raise SnapshotError(f"unreadable delivery receipt in {path}") from error
            if not isinstance(receipt, dict):
                raise SnapshotError(f"delivery receipt must be an object: {path}")
            key = receipt.get("receipt_key")
            if isinstance(key, str) and receipt.get("status") in {"delivered", "queued", "uncertain"}:
                handled.add(key)
    return handled


def extract_claims(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise SnapshotError("claims state must be an object")
    claims: dict[str, dict[str, Any]] = {}
    for key, claim in value.items():
        if not isinstance(key, str) or not key:
            raise SnapshotError("every claim key must be a non-empty string")
        if not isinstance(claim, dict) or not isinstance(
            claim.get("expires_at_epoch"), (int, float)
        ):
            raise SnapshotError(f"claim {key} has no numeric expires_at_epoch")
        normalized: dict[str, Any] = {"expires_at_epoch": float(claim["expires_at_epoch"])}
        if "owner" in claim:
            if not isinstance(claim["owner"], str) or not claim["owner"]:
                raise SnapshotError(f"claim {key} has an invalid owner")
            normalized["owner"] = claim["owner"]
        claims[key] = normalized
    return claims


def pending_events(
    events: list[dict[str, Any]],
    handled: set[str],
    claims: dict[str, dict[str, float]],
    now_epoch: float,
    requested_key: str | None,
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for event in events:
        dedup_key = event.get("dedup_key")
        fingerprint = event.get("fingerprint")
        if not isinstance(dedup_key, str) or not dedup_key:
            raise SnapshotError("event has no valid dedup_key")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise SnapshotError(f"event {dedup_key} has no valid fingerprint")
        if requested_key is not None and dedup_key != requested_key:
            continue
        if dedup_key in handled or fingerprint in handled:
            continue
        claim = claims.get(dedup_key)
        if claim and claim["expires_at_epoch"] > now_epoch:
            continue
        pending.append(event)
    return pending


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_heartbeat(path: Path) -> None:
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{timestamp}\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS_FILE)
    parser.add_argument("--ack-file", type=Path, default=DEFAULT_ACK_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--heartbeat-file", type=Path, default=DEFAULT_HEARTBEAT_FILE)
    parser.add_argument("--claims-file", type=Path, default=DEFAULT_CLAIMS_FILE)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--delivery-receipts-file", type=Path, action="append", default=None)
    parser.add_argument(
        "--route-sop",
        type=Path,
        required=True,
        help="Authoritative SOP containing the machine-readable project route table",
    )
    parser.add_argument("--key", default=None, help="Claim only this exact dedup key")
    claim_action = parser.add_mutually_exclusive_group()
    claim_action.add_argument("--renew-key", default=None, help="Renew this active claim lease")
    claim_action.add_argument("--release-key", default=None, help="Release this active claim lease")
    parser.add_argument("--claim-owner", default=None, help="Opaque owner token fencing claim updates")
    parser.add_argument(
        "--claim-ttl-seconds",
        type=float,
        default=DEFAULT_CLAIM_TTL_SECONDS,
        help="Lease duration preventing concurrent IPC and heartbeat delivery",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Freeze at most this many oldest pending events for one relay tick",
    )
    args = parser.parse_args()

    if (args.renew_key or args.release_key) and args.key:
        parser.error("claim renewal/release cannot be combined with --key")
    if (args.renew_key or args.release_key) and not args.claim_owner:
        parser.error("claim renewal/release requires --claim-owner")

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.claim_ttl_seconds <= 0:
        parser.error("--claim-ttl-seconds must be positive")

    if args.renew_key or args.release_key:
        action_key = args.renew_key or args.release_key
        try:
            write_heartbeat(args.heartbeat_file)
            args.lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(args.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with os.fdopen(lock_fd, "r+") as lock_handle:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                    handled = extract_handled(read_json(args.ack_file))
                    try:
                        claims = extract_claims(read_json(args.claims_file))
                    except SnapshotError as error:
                        if not args.claims_file.exists():
                            claims = {}
                        else:
                            raise error
                    if action_key in handled:
                        print(json.dumps({"status": "already-handled", "key": action_key}))
                        return 0
                    if args.renew_key:
                        if action_key not in claims:
                            raise SnapshotError(f"cannot renew missing claim: {action_key}")
                        if claims[action_key].get("owner") != args.claim_owner:
                            raise SnapshotError(f"claim owner mismatch: {action_key}")
                        claims[action_key] = {
                            "expires_at_epoch": time.time() + args.claim_ttl_seconds,
                            "owner": args.claim_owner,
                        }
                    else:
                        if action_key in claims and claims[action_key].get("owner") != args.claim_owner:
                            raise SnapshotError(f"claim owner mismatch: {action_key}")
                        claims.pop(action_key, None)
                    write_json_atomic(args.claims_file, claims)
                    print(json.dumps({
                        "status": "renewed" if args.renew_key else "released",
                        "key": action_key,
                    }))
            except OSError as error:
                raise SnapshotError(f"relay claim lock failed: {error}") from error
        except SnapshotError as error:
            print(f"ACTION_REQUIRED {error}", file=os.sys.stderr)
            return 2
        return 0

    # Invalidate any snapshot from a prior tick before interpreting mutable
    # queue/SOP state. A failed invocation must never leave deliverable stale
    # routing output behind for the direct IPC consumer.
    try:
        write_json_atomic(args.output, [])
    except OSError as error:
        print(f"ACTION_REQUIRED cannot invalidate relay snapshot: {error}", file=os.sys.stderr)
        return 2

    try:
        write_heartbeat(args.heartbeat_file)
        args.lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(args.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with os.fdopen(lock_fd, "r+") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                events = extract_events(read_json(args.events_file))
                handled = extract_handled(read_json(args.ack_file))
                # Scope the added receipt fence to temporal channel overrides;
                # all other channels keep the prior snapshot behavior.
                route_table = extract_route_table(args.route_sop)
                scoped_events = [event for event in events
                    if isinstance(event.get("exact_tuple"), dict)
                    and "admitted_after_epoch" in route_table["exact_channel_routes"].get(event["exact_tuple"].get("channel"), {})]
                receipt_keys = receipt_handled_keys(args.delivery_receipts_file or list(DEFAULT_DELIVERY_RECEIPTS_FILES)) if scoped_events else set()
                receipt_suppressed = {event.get("dedup_key") for event in scoped_events if event.get("dedup_key") in receipt_keys}
                try:
                    claims = extract_claims(read_json(args.claims_file))
                except SnapshotError as error:
                    if not args.claims_file.exists():
                        claims = {}
                    else:
                        raise error

                now_epoch = time.time()
                claims = {
                    key: claim
                    for key, claim in claims.items()
                    if key not in handled and claim["expires_at_epoch"] > now_epoch
                }
                pending = pending_events(
                    events,
                    handled | receipt_suppressed,
                    claims,
                    now_epoch,
                    args.key,
                )
                eligible = [
                    event for event in pending
                    if event_eligible_for_route(event, route_table)
                ]
                if args.limit is not None:
                    eligible = eligible[: args.limit]
                pending = [
                    route_event(event, route_table, args.route_sop) for event in eligible
                ]
                if any(event is None for event in pending):
                    raise SnapshotError("route eligibility changed while freezing snapshot")
                expires_at = now_epoch + args.claim_ttl_seconds
                claim_owner = args.claim_owner or f"relay-{os.getpid()}-{uuid.uuid4().hex}"
                for event in pending:
                    claims[str(event["dedup_key"])] = {
                        "expires_at_epoch": expires_at,
                        "owner": claim_owner,
                    }
                write_json_atomic(args.claims_file, claims)
                write_json_atomic(args.output, pending)
        except OSError as error:
            raise SnapshotError(f"relay claim lock failed: {error}") from error
    except SnapshotError as error:
        try:
            write_json_atomic(args.output, [])
        except OSError as invalidate_error:
            print(
                f"ACTION_REQUIRED {error}; snapshot invalidation failed: {invalidate_error}",
                file=os.sys.stderr,
            )
            return 2
        print(f"ACTION_REQUIRED {error}", file=os.sys.stderr)
        return 2

    print(json.dumps({"pending_count": len(pending), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
