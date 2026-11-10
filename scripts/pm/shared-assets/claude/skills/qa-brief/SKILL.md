---
name: qa-brief
description: Generate the exact-head QA brief consumed by the existing QA tester and readiness gate.
---

# QA brief contract

## Named scenarios and learning loop (Rajiv-approved 2026-11-10)

Read LESSONS.md before writing the brief. JSON names `scenario` + `parameters`, not free-form steps. Prefer `editor-takeover-two-tabs` for same-user takeover and `clone-sample-open-editor` for base setup. See scenarios/README.md for adapter contracts and invoke scenarios/run.py ON THE REMOTE BOX once; no LLM reasoning between steps. Resolve exact-head bindings before recording. Use bounded generic waits for readiness, element enabled, sync idle and head version change, never exact-text predicates. A final server content assertion is verification, not a wait for UI copy.

Clone the sample through an existing internal mutation on the owned disposable backend. Never guess its API, use another slot/production, or silently replace a named customer artifact. Missing bindings return SCENARIO_BINDING_MISSING. Creation/upload/audio ACs retain the real jobsheet + MP3 path. For two-tabs use ONE session/profile and two actual tabs, not a second profile.

Free-form steps are allowed ONLY when no scenario fits: record `scenario: null`, `scenario_miss_reason`, parameters and bounded steps, and append the miss to LESSONS.md. Missing adapters for a matching scenario are blockers, not an excuse to bypass the library.

Use capture-bg, wait for AGENT_BROWSER_READY within 2100s, and retain canonical synthetic auth, finite command timeouts, one diagnosed transient retry and controller-owned teardown. Never host browser QA. Every run emits command timing, session summary and cleanup receipts.

Every QA video must be reviewed through qa-video-review with its session summary and frames. Append one structured lesson row; recurring cause >=2 or idle_share >0.4 requires a SAME-review scenario/guidance patch. Verdict includes `LESSON_APPLIED: <file:change>` or `LESSON_NONE: <reason>`. Shared assets are MoP-managed: make and commit the learning patch/lesson in MoP sources, stage/activate/shared-install, restart and verify deployed copies. Never edit a release directory or leave live-only changes.

The implementing slot includes issue, PR, exact 40-hex head, full AC/proof inventory and privacy-safe exact-artifact locators in the brief. Scenario selection does not waive any AC. Evidence must map each AC to the real production path, exact head and required artifact.

The tester writes one exact-head evidence envelope and posts the durable
`qa-artifact-evidence` marker. Its `tester_verdict` and `codex_qa_verdict`
must both be unconditional `PASS`/`PASSED`/`APPROVE`/`APPROVED`; any error,
missing check, inaccessible artifact, wrong artifact, or conditional verdict
is a refusal. The marker is consumed by the installed
`qa-visual-proof-gate.py`; no PM-side override or conditional promotion is
valid.

The validator adds one bounded local parse/compare after the existing QA
execution. It does not make another production query or run a second review.
