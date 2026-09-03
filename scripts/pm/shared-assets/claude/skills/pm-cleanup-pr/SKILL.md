---
name: pm-cleanup-pr
description: Finalize one already-merged HeyDonna PR and linked issue through the direct journaled cleanup caller.
---

Pipe one complete JSON object to the manifest-installed `pm-cleanup-pr.py`
exactly once after the canonical merge terminal. It requires repository, PR,
issue, exact PR head, merge commit, and the existing canonical transition
thread mapping. It verifies the merged/head/link tuple, preserves unrelated
labels, removes only stale CI/cleanup/slot/status lifecycle labels, sets
`pm-state:closed-clean`, closes the linked issue as `COMPLETED` with
`status:done`, and posts one exact cleanup reply in that existing thread.

The caller reads only `SLACK_CTO_BOT_TOKEN`, verifies Abhijit CTO
(`U0BNFGX2UAX`) and exact thread readback, journals every external effect, and
never releases or changes a MoP slot. A missing/stale tuple, mapping, bot
identity, or any uncertain/started effect is a typed non-retryable outcome.
