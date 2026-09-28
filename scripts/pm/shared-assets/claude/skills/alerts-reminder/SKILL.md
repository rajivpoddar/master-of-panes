---
name: alerts-reminder
description: PM-only scheduling of one-off operational Slack reminders as HeyDonna Alerts. Use for future reminders with a fixed due time; ordinary PM replies continue through slack-message.
---

# Alerts reminders

Schedule future operational reminders with Slack's existing scheduler through
`scripts/schedule.py`. It uses the existing HeyDonna Alerts Keychain credential
and verifies Slack user `U0AJZTN7SM6` before scheduling. Do not use PM's
`SLACK_BOT_TOKEN` or the ordinary `slack-send.sh` for these reminders.

Prepare the exact message in a UTF-8 file, including `<@U0ALEAYCAUT>`. Preserve
the source channel, parent `thread_ts`, due time, and text. Use one stable dedup
key for the obligation; keep it on any retry or reconciliation. For example:

```bash
python3 /Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/alerts-reminder/scripts/schedule.py \
  --channel "$SOURCE_CHANNEL_ID" --thread-ts "$SOURCE_THREAD_TS" \
  --post-at "$DUE_EPOCH" --text-file /absolute/path/to/reminder.txt \
  --dedup-key "pm-ops:<obligation>:reminder"
```

The helper reserves the key before calling Slack and returns the accepted
`scheduled_message_id`. A repeat with the same key and payload returns that
receipt without scheduling again; changed payload or uncertain acceptance
stops. Do not re-key, blindly retry, or repost a reminder that has fired.

This applies only to automated operational reminders. Human PM progress,
status, and answers still use the PM Slack sender. Scheduling does not change
the underlying bench or slot execution.
