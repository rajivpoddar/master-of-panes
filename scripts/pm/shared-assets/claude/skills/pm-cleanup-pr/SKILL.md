---
name: pm-cleanup-pr
description: Finalize one already-merged HeyDonna PR and linked issue through the direct journaled cleanup caller.
---

Pipe one complete JSON object to the manifest-installed `pm-cleanup-pr.py`
exactly once after the canonical merge terminal. It requires repository, PR,
issue, exact PR head, and merge commit. It verifies the merged/head/link
tuple against live GitHub, preserves unrelated labels, removes only stale
CI/cleanup/slot/status lifecycle labels, sets `pm-state:closed-clean` on the
PR and `status:done` on the linked issue, and never releases or changes a
MoP slot. A missing/stale tuple or any uncertain/started effect is a typed
non-retryable outcome.

There is no transition-thread mapping in the post-cutover control plane, and
the caller must never fabricate one. For a merged PR with a linked issue,
attest the canonical merge terminal explicitly:

```json
{"repository": "heydonna-app/heydonna-app", "pr": 7740, "issue": 7739,
 "head": "<40-hex>", "merge_commit": "<40-hex>",
 "thread_reply": false, "post_merge_terminal": true}
```

With `post_merge_terminal: true` (which requires `thread_reply: false`),
the caller skips mapping resolution, verifies the exact merge commit, head,
and unique linked issue live, strips the stale labels, sets the terminal
labels, and journal-closes the linked issue as `COMPLETED` only when it is
still open — all readback-verified and idempotent under one receipt key. It
never posts to Slack and never invents a thread mapping. Use plain
`thread_reply: false` without the attestation only when the linked issue is
already `CLOSED`/`COMPLETED`; a requested thread reply still fails closed
without a mapping.

For a merged PR with no linked closing issue, use the explicit
`cleanup_mode=merged_pr_issue_less` contract with `issue=null` and omit
`thread_ts` (and omit `thread_reply`: it defaults to true and explicit
`false` is refused in this mode). Supply the repository, PR number, full PR
head, and merge commit. The caller requires an authoritative merged PR
readback with zero linked closing issues, then performs only journaled
per-label PR cleanup and adds `pm-state:closed-clean`; it does not read or
close an issue and does not post to Slack. Do not use this mode for a PR
that has any linked issue.
