---
name: qa-brief
description: Generate the exact-head QA brief consumed by the existing QA tester and readiness gate.
---

# QA brief contract

## Named scenarios and learning loop (Rajiv-approved 2026-11-10)

Read LESSONS.md before writing the brief. JSON names `scenario` + `parameters`, not free-form steps. Prefer `editor-takeover-two-tabs` for same-user takeover and `clone-sample-open-editor` for base setup. See scenarios/README.md for adapter contracts and invoke scenarios/run.py ON THE REMOTE BOX once; no LLM reasoning between steps. Resolve exact-head bindings before recording. Use bounded generic waits for readiness, element enabled, sync idle and head version change, never exact-text predicates. A final server content assertion is verification, not a wait for UI copy.

Clone the sample through an existing internal mutation on the owned disposable backend. Never guess its API, use another slot/production, or silently replace a named customer artifact. Missing bindings return SCENARIO_BINDING_MISSING. Creation/upload/audio ACs retain the real jobsheet + MP3 path. For two-tabs use ONE session/profile and two actual tabs, not a second profile.

When no scenario fits, record `scenario: null`, `scenario_miss_reason`, parameters and bounded steps, and append the miss to LESSONS.md. When a matching named scenario lacks `clone_internal` or `observe` bindings, named execution must still refuse with `SCENARIO_BINDING_MISSING`. Until the adapters exist, explicitly permit the existing canonical manual QA route at the SAME exact head and owned backend; label it `manual (scenario adapter missing)`, record the adapter gap as a lesson row, and never report a named-scenario PASS for the manual run. Retain every AC, required artifact, server readback and safety check; do not add a wrapper, gate or review chain.

Use capture-bg, wait for AGENT_BROWSER_READY within 2100s, and retain canonical synthetic auth, finite command timeouts, one diagnosed transient retry and controller-owned teardown. Never host browser QA. Every run emits command timing, session summary and cleanup receipts.

Every QA video must be reviewed through qa-video-review with its session summary and frames. Append one structured lesson row; recurring cause >=2 or idle_share >0.4 requires a SAME-review scenario/guidance patch. Verdict includes `LESSON_APPLIED: <file:change>` or `LESSON_NONE: <reason>`. Shared assets are MoP-managed: make and commit the learning patch/lesson in MoP sources, stage/activate/shared-install, restart and verify deployed copies. Never edit a release directory or leave live-only changes.

The implementing slot includes issue, PR, exact 40-hex head, full AC/proof inventory and privacy-safe exact-artifact locators in the brief. Scenario selection does not waive any AC. Evidence must map each AC to the real production path, exact head and required artifact.

Every brief, including manual routes, specifies an observable precondition assertion before each action under test and an observable end-state assertion after it. The driver checks the visible precondition before acting and checks the actual resulting state after the action; navigation or a click alone is not proof. At the first unmet assertion, stop the scenario, report a named failure (`PRECONDITION_NOT_ESTABLISHED` or `END_STATE_ASSERTION_FAILED`), and exit non-zero. Keep recording active through the final assertion, including a failing assertion, before controller-owned teardown.

Recovery and writer-move flows (retro #13357/#13435; CTO C0ALZJHGE49 thread 1794234970.206389, ts 1794578332.315349): the brief specifies the actual product transition and asserts its authoritative outcome, never button, banner or toast text alone. Reload saved asserts the saved SERVER head: after the action the head version and content read back from the server equal the saved version, and the editor shows that content. Same-user writer move asserts the lease handoff: the server lease/session/device now names the target context and the old context is fenced and publishes nothing. Use two independent browser contexts for the same user (see the remote-qa-runner run receipt), and take every UI state and element name from the shipped component at the exact head, not from a guessed id or an earlier banner.

The brief also supplies the runner's receipt fields: scenario, target route/dialog, which tab/context performs each action, storage scope, and each browser context's signed-in identity. The runner reads them back from the live browser (tab count, URL path and origin, hashed browser identity, storage) and records anything it cannot verify, or finds mismatching or malformed, as UNKNOWN/PARTIAL; it never refuses or crashes the run. Evidence is the readback, never the brief's claim. Two tabs in one profile are one browser, so they do not prove a different-browser or different-device AC: that brief names two independent browser contexts. Action markers are timed on the recording clock (`recording_s`, from the controller's `recording.started_ms`), or UNKNOWN when the clock is unavailable; never on the scenario clock (`elapsed_s`).

Before routing rework from a failed or blocked QA, record the FIRST failing boundary as `product`, `harness`, `oracle` or `UNKNOWN`, with the evidence for it (for example wrong tab or identity, stale token, wrong element id or a setup failure is harness/oracle, not product). `UNKNOWN` names the missing evidence.

The tester writes one exact-head evidence envelope and posts the durable
`qa-artifact-evidence` marker. Its `tester_verdict` and `codex_qa_verdict`
must both be unconditional `PASS`/`PASSED`/`APPROVE`/`APPROVED`; any error,
missing check, inaccessible artifact, wrong artifact, or conditional verdict
is a refusal. The marker is consumed by the installed
`qa-visual-proof-gate.py`; no PM-side override or conditional promotion is
valid.

The validator adds one bounded local parse/compare after the existing QA
execution. It does not make another production query or run a second review.
