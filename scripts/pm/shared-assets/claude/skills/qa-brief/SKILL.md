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

The tester writes one exact-head evidence envelope and posts the durable
`qa-artifact-evidence` marker. Its `tester_verdict` and `codex_qa_verdict`
must both be unconditional `PASS`/`PASSED`/`APPROVE`/`APPROVED`; any error,
missing check, inaccessible artifact, wrong artifact, or conditional verdict
is a refusal. The marker is consumed by the installed
`qa-visual-proof-gate.py`; no PM-side override or conditional promotion is
valid.

The validator adds one bounded local parse/compare after the existing QA
execution. It does not make another production query or run a second review.
