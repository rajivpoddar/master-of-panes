---
name: codex-stdio-send-message
description: Queue one exact message in an existing Codex Desktop task through app-server stdio. Use when a task-to-task handoff must run without renderer-mediated `codex_app` messaging.
---

# Codex Stdio Send Message

Use the bundled helper for an explicitly authorized task-to-task delivery when
renderer-mediated Codex app tools are unsafe or have stalled. The helper queues
the user-visible message once with `thread/queue/add` and stops. It does not
start, resume, create, inspect, interrupt, or wait for a task. A successful
queue/add is durable delegation ownership: never create a second owner for that
dedup key.

## Required tuple

Obtain all three values before sending:

- Exact destination Codex task ID.
- Exact message text.
- A stable, event-specific deduplication key. Reuse the same key for the same
  logical delivery; never generate a second key to retry an uncertain send.

## Send

Run:

```bash
python3 /Users/rajiv/.codex/skills/codex-stdio-send-message/scripts/send_message.py \
  --thread-id '<destination-task-id>' \
  --dedup-key '<stable-event-key>' \
  --message '<exact-message>'
```

For multiline or shell-sensitive text, put the exact content in an existing
absolute-path file and use `--message-file <path>` instead of `--message`.
Do not create a message file unless the current task authorizes filesystem
writes.

The helper launches the Codex Desktop app-server over stdio, performs the
required initialization handshake, and queues exactly once. It never calls
renderer-mediated `codex_app` tools.

## Interpret the receipt

- `status=queued_for_task_consumption`, exit 0: `thread/queue/add` succeeded.
  The submission is durable and owns this dedup key for task consumption.
  Report `threadId`, `queuedSubmissionId`, and `clientUserMessageId`; do not
  retry queue/add or create another owner. This proves durable acceptance, not
  recipient start or completion.
- `status=unavailable`, exit 2: app-server definitely rejected the request or
  failed before queue submission. Report the exact error and stop unless the
  user authorized another transport.
- `status=uncertain`, exit 3: the queue request was written but no authoritative
  response arrived. Do not retry, change the deduplication key, or fall back to
  another transport; report the uncertainty for reconciliation.
- Exit 4: local input or configuration was invalid; correct only that local
  invocation and rerun with the same deduplication key.

Do not poll the destination. For a required return message, use this same skill
rather than `send_message_to_thread`, `read_thread`, or another renderer-mediated
Codex app tool.
