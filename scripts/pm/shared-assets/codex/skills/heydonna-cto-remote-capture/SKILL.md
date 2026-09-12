---
name: heydonna-cto-remote-capture
description: Admit one verified exact-head HeyDonna strict-fixture capture from a fixture-miss terminal tuple, with no PM state and no legacy capture-required CI receipt.
---

# HeyDonna CTO remote capture admission

Self-contained CTO admission edge for one canonical capture. It consumes a verified **fixture-miss
terminal tuple** directly and has no dependency on PM state, labels, retry budgets, affected-test
receipts, or the retired `capture-required` CI verdict. Use only this tool plus `gh`, Git and Python's
standard library. Never invoke `heydonna-app/scripts/pm/control-plane/**` or any
`request-budgeted-remote-capture` script — those PM-side requester paths are retired.

## Terminal artifact

A small JSON file describing the terminal that proved the fixture miss:

```json
{
  "pr_number": 7644,
  "head_sha": "<40-hex live PR head>",
  "head_branch": "<live PR branch>",
  "source_run": 34617909102,
  "source_attempt": 1,
  "control_sha": "<40-hex current main>",
  "capture_profile": "auto-process-only",
  "cause": "SHARED_STRICT_FIXTURE_CORPUS_MISS_...",
  "fixture_miss": true
}
```

`fixture_miss` must be exactly `true`; anything else is non-fixture evidence and is refused.

## Fences (all read-only)

1. Terminal shape + fixture-miss evidence.
2. Live PR fence — PR must be OPEN and its live `headRefOid` must equal `head_sha`.
3. Source-run fence — the source run's `headSha` must equal `head_sha`.
4. Duplicate fence — refuse when the same exact head already has an active or a successful run of the
   canonical capture workflow.

Dry-run is the default and never mutates anything: on success it prints `READY_TO_CAPTURE`.

## Run

```bash
python3 scripts/admit_capture.py --terminal TERMINAL.json
python3 scripts/admit_capture.py --terminal TERMINAL.json --apply
```

`--apply` dispatches exactly one run of the canonical workflow, using the workflow's own input names
(`pr_number`, `head_sha`, `head_branch`, `control_sha`, `capture_profile`). It is idempotent: re-running
after a launch hits the duplicate fence instead of dispatching again.

Exit codes: `0` admitted/dispatched · `2` refused with a typed reason · `3` invalid input.

## Boundaries

This tool does not change the capture workflow, profiles, selectors, fixtures, or strict-replay
behaviour, and it never edits app product code. Rollback is deleting the installed skill directory.
