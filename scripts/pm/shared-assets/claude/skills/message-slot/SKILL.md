---
name: message-slot
description: |
  Send a short operational message to a dev slot through the MoP slot gateway.
  Use for PM-to-slot or slot-to-slot nudges, corrections, approvals, status
  requests, and short non-handoff directives. NOT for issue assignment,
  rework/handoff packets, Slack, raw UI controls, C-c/Escape, /clear, or formal
  handoff bodies.
---

# message-slot

Short PM-to-slot or slot-to-slot communication. This is the slot-directed
counterpart to `message-pm`.

## Purpose

Use `message-slot` when a slot needs a concise instruction or relay that is not
a formal work assignment:

- Plan approval or rejection summary.
- "Run the Codex review before proceeding."
- "Stop and read marker file X verbatim."
- "Status please: post current PR, branch, test state."
- Slot-to-slot context relay routed through MoP when PM explicitly wants that.

## Not For

- Full issue assignment or rework packets: use `Skill(direct-assign)`.
- Formal multiline handoff bodies.
- Slack messages.
- Raw UI controls such as Escape, C-c, `/clear`, or `/compact`: use
  `interrupt-slot` or `tmux-slot-command`.
- PM-bound updates from a slot: use `message-pm`.

## Canonical Command

```bash
bash ~/.claude/skills/message-slot/scripts/message-slot.sh <slot> '<message>'
bash ~/.claude/skills/message-slot/scripts/message-slot.sh <slot> --file /tmp/short-direction.md --force
cat /tmp/short-direction.md | bash ~/.claude/skills/message-slot/scripts/message-slot.sh --slot <slot> -f --force
```

Examples:

```bash
bash ~/.claude/skills/message-slot/scripts/message-slot.sh 3 'Plan approved. Proceed with implementation.' --force
bash ~/.claude/skills/message-slot/scripts/message-slot.sh 2 'Status please: PR, branch, tests, exact blocker.'
bash ~/.claude/skills/message-slot/scripts/message-slot.sh 4 'slot 2 says the fixture key is 6b7810ec; verify before rerun.' --from 'slot 2 (Hasta)'
```

For file-backed content, always use `--file`; do not pass
`"$(cat /tmp/file.md)"`, because non-shell slot transports may deliver that
literal command text to the slot. `--file` archives the body and sends the slot
a short `FILE_PACKET` pointer with the archive path, source filename, bytes,
sha256, and a bounded content preview; the slot must read the archive path with
the Read tool instead of treating the preview as the full instruction body.

## Flags

- `--force`: send immediately; use for plan approvals, urgent corrections, or
  when the target is blocked waiting on the message.
- `--wait`: after sending, wait for the slot to return to an idle prompt.
- `--from <label>`: override the sender prefix. If omitted, the script infers
  `PM` from the PM repo and `slot N (Name)` from slot `.env.local` or cwd.
- `--file <path>`: copy a readable file to `/tmp/pm-delivered-archive`, compute
  bytes + sha256, include a bounded preview, and send a short
  `FILE_PACKET path=... source=... preview=...` instruction. This is
  file-reference delivery, not tmux body paste. Do not use this for formal
  handoff packets.
- `-f`: read stdin, archive it, compute bytes + sha256, include a bounded
  preview, and send the same `FILE_PACKET path=... source=stdin preview=...`
  instruction. Use this only with a pipe/redirect, e.g.
  `cat /tmp/short-direction.md | ... -f --force`.
- `--allow-command`: allow a message that begins with `/`. Default rejects
  slash-command-shaped text because most slot directives should use the
  dedicated control skills.
- `--dry-run`: print the exact proof line without sending.

## Output Contract

The script always emits one send-attempt proof line:

```text
MESSAGE_SLOT_OK slot:N from="<sender>" force=<0|1> wait=<0|1> mode=<message|file-ref> ts=<UTC_ISO>
MESSAGE_SLOT_FAILED slot:N from="<sender>" exit=<code> reason=<summary> ts=<UTC_ISO>
```

`MESSAGE_SLOT_OK` proves the message was handed to the local sender. It is not,
by itself, receipt proof that the slot saw or acted on the text.

For urgent rework, plan approval, model restart, or file-reference delivery, PM must
also verify receipt by one of:

- MoP `/events?slot=N` shows the delivery event plus a subsequent same-slot
  pickup/response event tied to this prompt boundary;
- an explicit slot reply or MoP acknowledgement tied to this prompt boundary;
- using `--wait` and then checking MoP events or final pane state show the slot
  consumed the message.

If the slot does not acknowledge the `FILE_PACKET` path+sha256 within 30 seconds,
retry with a plain MoP/send reminder that names the exact archive path and next
action, then verify the fallback landed. MoP owns buffer-paste delivery and may
chunk large payloads; do not impose a local byte limit. Do not record the work as
dispatched until receipt is verified.

Use both send proof and receipt proof in hourly ops audit proof blocks or PM
status notes.

## Safety Rules

1. Target slot must be `1`, `2`, `3`, `4`, `5`, or `6`.
2. Messages are prefixed automatically as `[UTC_ISO_TIMESTAMP] <sender> -> slot N:`.
3. Slash-command-looking messages are rejected by default.
4. File-reference mode rejects empty files and slash-command-looking files by default.
5. Delivery delegates to `~/.claude/skills/tmux-slot-command/scripts/send-to-slot.sh`
   so MoP owns INSERT mode, delivery, and idle/force behavior.
