---
name: qa-brief
description: Generate the exact-head QA brief consumed by the existing QA tester and readiness gate.
---

# QA brief contract

The implementing slot generates one JSON brief from the issue's structured
acceptance contract. It contains the issue number, PR number, complete 40-hex
head, required proof types, and the privacy-safe identity/locator of any named
customer artifact. The brief is evidence planning, not evidence.

Use the existing QA tester and existing QA-review path. For every required
criterion, name the production path and the evidence row. A visual surface
requires an exact customer artifact screenshot; an export contract requires
inspection of the emitted customer DOCX/OOXML; a named file, template, or
artifact must be that exact artifact, not a seeded substitute. Do not invent an
artifact identity from prose outside the acceptance contract.

## Browser scenario setup

Default to cloning the canonical sample project/file first, then run the
scenario on that clone. The remote capture setup's `--seed-project medium`
provisions the Frazier sample for the synthetic admin; use the dashboard's
**Try a sample deposition** action (implemented by
`api.projects.cloneSampleProject`) to create the working clone. Record the
source sample and resulting clone in the brief. Create a fresh project through
the jobsheet + MP3 flow only when an AC tests creation, upload, or processing.
If the scenario requires a state the sample cannot provide, state the precise
missing state and use the smallest applicable setup path.

Avoid getting stuck: use `capture-bg`, wait for `AGENT_BROWSER_READY` on the
box, then start browser commands; each readiness wait is bounded at 2100s and
browser commands must have a finite timeout. Re-snapshot after navigation
before using refs. On auth redirect, verify synthetic auth once and stop with
`AUTH_REDIRECT` if still signed out (never live Clerk login). If editor-ready
does not appear by its bounded wait, capture the visible state and stop with
`EDITOR_READY_TIMEOUT`. For Radix menus, follow the relevant `agent-browser-*`
Radix skill and re-snapshot after opening; for hidden file inputs use the
known input selector with `upload`, not a visible-file-input search. One
retry after a diagnosed transient is the limit; otherwise return a typed
blocker with the last screenshot/log evidence, not a hanging run.

The tester writes one exact-head evidence envelope and posts the durable
`qa-artifact-evidence` marker. Its `tester_verdict` and `codex_qa_verdict`
must both be unconditional `PASS`/`PASSED`/`APPROVE`/`APPROVED`; any error,
missing check, inaccessible artifact, wrong artifact, or conditional verdict
is a refusal. The marker is consumed by the installed
`qa-visual-proof-gate.py`; no PM-side override or conditional promotion is
valid.

The validator adds one bounded local parse/compare after the existing QA
execution. It does not make another production query or run a second review.
