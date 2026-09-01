#!/usr/bin/env python3
"""Render complete visible Slack message content from Block Kit API responses."""

from __future__ import annotations

import json
import sys
from typing import Any


def text_object(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    text = value.get("text")
    return text if isinstance(text, str) else ""


def inline_element(element: dict[str, Any]) -> str:
    kind = element.get("type", "")
    if kind == "text":
        return str(element.get("text", ""))
    if kind == "link":
        url = str(element.get("url", ""))
        label = str(element.get("text") or url)
        return f"<{url}|{label}>" if url else label
    if kind == "user":
        return f"<@{element.get('user_id', '')}>"
    if kind == "channel":
        return f"<#{element.get('channel_id', '')}>"
    if kind == "usergroup":
        return f"<!subteam^{element.get('usergroup_id', '')}>"
    if kind == "broadcast":
        return f"<!{element.get('range', '')}>"
    if kind == "emoji":
        return f":{element.get('name', '')}:"
    if kind == "date":
        timestamp = element.get("timestamp", "")
        fmt = element.get("format", "")
        fallback = element.get("fallback", "")
        link = element.get("url")
        link_part = f"^{link}" if link else ""
        return f"<!date^{timestamp}^{fmt}{link_part}|{fallback}>"
    return f"[unsupported:{kind or 'unknown'}]"


def rich_element(element: dict[str, Any], depth: int = 0) -> str:
    kind = element.get("type", "")
    children = element.get("elements")
    if kind == "rich_text_section":
        return "".join(
            inline_element(child)
            for child in children or []
            if isinstance(child, dict)
        )
    if kind == "rich_text_list":
        indent = "  " * depth
        ordered = element.get("style") == "ordered"
        lines = []
        for index, child in enumerate(children or [], start=1):
            body = rich_element(child, depth + 1) if isinstance(child, dict) else ""
            marker = f"{index}." if ordered else "-"
            lines.append(f"{indent}{marker} {body}")
        return "\n".join(lines)
    if kind == "rich_text_quote":
        body = "".join(
            inline_element(child)
            for child in children or []
            if isinstance(child, dict)
        )
        return "\n".join(f"> {line}" for line in body.splitlines() or [""])
    if kind == "rich_text_preformatted":
        body = "".join(
            inline_element(child)
            for child in children or []
            if isinstance(child, dict)
        )
        return f"```\n{body}\n```"
    return inline_element(element)


def action_element(element: dict[str, Any]) -> str:
    kind = str(element.get("type", "unknown"))
    label = text_object(element.get("text"))
    url = element.get("url")
    value = element.get("value")
    details = [part for part in (label, str(url or ""), str(value or "")) if part]
    return f"[{kind}: {' | '.join(details)}]" if details else f"[{kind}]"


def render_block(block: dict[str, Any]) -> str:
    kind = block.get("type", "")
    if kind == "rich_text":
        return "\n".join(
            rich_element(element)
            for element in block.get("elements", [])
            if isinstance(element, dict)
        )
    if kind == "section":
        parts = [text_object(block.get("text"))]
        parts.extend(text_object(field) for field in block.get("fields", []))
        return "\n".join(part for part in parts if part)
    if kind == "header":
        return text_object(block.get("text"))
    if kind == "context":
        return " ".join(
            text_object(element)
            if element.get("type") in {"plain_text", "mrkdwn"}
            else render_block(element)
            for element in block.get("elements", [])
            if isinstance(element, dict)
        )
    if kind == "divider":
        return "---"
    if kind == "image":
        title = text_object(block.get("title"))
        alt = str(block.get("alt_text", ""))
        url = str(block.get("image_url", ""))
        return " ".join(part for part in (title, alt, url) if part)
    if kind == "actions":
        return " ".join(
            action_element(element)
            for element in block.get("elements", [])
            if isinstance(element, dict)
        )
    if kind == "input":
        label = text_object(block.get("label"))
        element = block.get("element")
        control = action_element(element) if isinstance(element, dict) else ""
        return " ".join(part for part in (label, control) if part)
    return f"[unsupported:{kind or 'unknown'}]"


def render_blocks(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        rendered
        for block in blocks
        if isinstance(block, dict)
        if (rendered := render_block(block)).strip()
    )


def render_attachment(attachment: dict[str, Any]) -> str:
    block_body = render_blocks(attachment.get("blocks"))
    if block_body:
        return block_body
    parts = [
        str(attachment.get(key, ""))
        for key in ("pretext", "title", "text", "fallback")
    ]
    for field in attachment.get("fields", []):
        if isinstance(field, dict):
            parts.extend((str(field.get("title", "")), str(field.get("value", ""))))
    return "\n".join(part for part in parts if part)


def render_message(message: dict[str, Any]) -> str:
    body = render_blocks(message.get("blocks"))
    if not body:
        body = str(message.get("text", ""))
    attachments = [
        render_attachment(attachment)
        for attachment in message.get("attachments", [])
        if isinstance(attachment, dict)
    ]
    attachments = [attachment for attachment in attachments if attachment]
    if attachments:
        body = "\n".join([body, *attachments]) if body else "\n".join(attachments)
    return body


def main() -> int:
    payload = json.load(sys.stdin)
    if not payload.get("ok"):
        error = payload.get("error", "slack_api_error")
        print(f"Slack API error: {error}", file=sys.stderr)
        return 2

    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        print("Slack API response has no messages list", file=sys.stderr)
        return 3

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if index:
            print("\n---")
        identity = message.get("user") or message.get("bot_id") or "unknown"
        print(
            f"ts={message.get('ts', '')} "
            f"thread_ts={message.get('thread_ts', '')} "
            f"from={identity}"
        )
        print(render_message(message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
