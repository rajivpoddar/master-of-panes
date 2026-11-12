# Retarget an occupied slot's PR

When a closed PR is superseded by a replacement for the **same** occupied issue, use the release's `scripts/mop-retarget-pr.py` with `--slot`, `--issue`, `--epoch`, `--old-pr`, `--pr`, `--branch`, `--head` (40 hex characters), and `--reason`.

The command reads the complete current assignment and refuses a mismatched slot, issue, epoch or old PR. The server verifies the old PR is closed and the replacement is open at the supplied branch and head via `gh`; unavailable GitHub evidence refuses the operation. The server then compares the entire observed tuple and epoch atomically, updates only PR/branch/head metadata, and writes `slot_pr_retargeted` with both sides and the reason in the same transaction. No Ready Pool selection, message delivery, ownership transfer or clock reset occurs. If the call fails or its response is lost, read the slot and audit event before retrying.
