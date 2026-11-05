# HeyDonna Issue and Ready Pool Contract

Use the current triage policy and the installed canonical validators in `~/.claude/scripts` as the source of truth.

The completed body should use this shape.

```markdown
<!-- ready-pool:
qa_proof_schema: 1
priority: P1
lane: editor
branch_slug: bounded-lowercase-slug
ac_summary: Observable production-path success in 140 characters or fewer.
claimable_slot_type: any
blockers: none
work_type: bug_fix
disposition: PROMOTE
application_state: ready
owner: slot-dispatch
next_action: PM selects the issue after live ownership and dependency checks.
wake_condition: immediately
required_validation: Focused unit/integration proof that executes the named runtime control point; add E2E only for a critical user workflow.
source_commit: <FETCHED_ORIGIN_MAIN_SHA>
runtime_symbol: path/to/file.ts::productionFunction
duplicate_search_receipt: Searched live issues and PRs by capability, failure mode, and runtime symbol; no canonical duplicate found.
overlapping_work_receipt: Inspected open PRs and active issues touching the runtime surface; no overlapping implementation or dependency found.
-->

## Problem

Describe the verified current behavior and user/product impact.

## Original directive and intended capability

Quote or tightly cite Rajiv's directive. State the intended capability without
narrowing it to a test or implementation detail.

## Production failure mode

Describe the production-shaped sequence that fails and its consequence.

## Runtime control point

Name the gate, state transition, retry boundary, validator decision, persisted
artifact, prompt, export branch, or other production authority that must change.
Explain why the affected case reaches it.

## Scope

- In scope: one coherent change.
- Out of scope: adjacent refactors, migrations, and policy decisions.

## Scribie integration contract impact

Choose exactly one:

- [x] N/A — this issue does not change Scribie-facing request fields, response
      fields, validation behavior, error codes, field precedence, or webhook
      behavior.
- [ ] Applies — this issue changes the Scribie integration contract.

- External-facing error code or response change: N/A
- Caller remediation steps: N/A
- Customer-notification owner, channel, and timing: N/A
- Compatibility or rollout requirement: N/A

## Acceptance criteria

Use at most three primary outcome ACs with stable IDs and proof fields. Negative constraints and proof remain mandatory and are not additional implementation scopes. Split actual oversized work rather than relabeling outcomes. Use the allowed values below.

1. AC-1: Positive observable production-path result.
   - Surface: visual | nonvisual
   - QA reachability: deterministic | nondeterministic | not-applicable
   - Required proof: screenshot | playwright | e2e | integration | unit | trace | telemetry | artifact | capture-replay
   - QA scenario: Exact state, action, and viewport for deterministic visual QA; omit for nonvisual ACs.
   - Screenshot exemption: Required only for nondeterministic visual behavior; explain why and name the alternative proof.
2. AC-2: State or data integrity invariant preserved.
   - Surface: nonvisual
   - QA reachability: not-applicable
   - Required proof: unit + integration
3. AC-3: Required error, retry, authorization, or stale-result behavior proven.
   - Surface: nonvisual
   - QA reachability: not-applicable
   - Required proof: unit

## Negative acceptance criteria

- Do not move the fix to a downstream post-processor or test-only seam.
- Do not broaden the change beyond the named runtime control point.
- Do not add a silent fallback or implicit status transition.

## Testing requirements

- Unit test for pure logic.
- Integration test for the affected Convex/API/Modal/editor boundary.
- E2E only when the critical user workflow cannot be proven below E2E.
- RED-on-revert when required to prove the regression guard.

## Issue Contract Ledger

| Contract row | Requirement |
| --- | --- |
| Positive AC | State the observable success case and production path. |
| Negative AC | State behavior that must remain unchanged and cases that must still fail closed. |
| Forbidden implementation | Name the wrong control point, silent fallback, test seam, or over-broad implementation that is not allowed. |
| Determinism | State whether behavior is deterministic or model-dependent and identify any fixture/artifact authority. |
| Required proof | Name the smallest tests/artifacts that execute the production control point, including RED-on-revert or live artifact proof when required. |

## Directive fidelity

- Directive fidelity: MATCHES
- Main architectural concern: State the primary risk and how the contract
  contains it.
- Testing decision: State why the selected unit/integration/E2E proof is the
  smallest sufficient plan.
```

## Visual QA receipt contract

For every deterministic visual AC, QA must post an exact-head, issue-body-bound
receipt on the PR. Evidence must be a GitHub attachment, R2 object, or named
PNG member of a GitHub Actions artifact. The merge gate downloads the bytes,
validates the PNG, and matches its SHA-256. Local `/tmp`, `file:`, and
`data:` references are invalid.

```html
<!-- qa-visual-proof: {"schema":"heydonna_qa_visual_proof","version":1,"verdict":"pass","pr":123,"issue":456,"head_sha":"<40-char SHA>","issue_body_sha256":"<64-char SHA256>","scenarios":[{"ac_id":"AC-1","artifact_kind":"github_attachment","artifact_url":"https://github.com/user-attachments/assets/<id>","sha256":"<PNG SHA256>","viewport":"1440x900","state":"Exact UI state shown","captured_at":"2026-08-03T00:00:00Z"}]} -->
```

## Promotion rules

`status:todo` admits bounded app-code/app-test work, including named dependencies at every priority. Assignment remains gated by live dependencies and ownership. Do not admit as claimable:

- internal control-plane repair;
- docs-only or infra/direct-main work;
- tracking/umbrella work;
- investigation or reproduction requests;
- external action;
- nondependency product, external, activation or unresolved authority holds;
- unresolved material product or acceptance decisions.

Canonical blockers in Ready Pool metadata are authoritative. Incidental prose
must not introduce additional dependency edges. Use `blockers: #N` or `[#N, #M]` plus `pm-blocked:dependency` for named waits. Retain the actual wake/capability requirement; admission does not authorize execution.

The `source_commit` must equal the fetched `origin/main` commit used for the
review. `runtime_symbol` must identify the production authority, not only a
test. The duplicate and overlap receipts must state what live surfaces were
searched and the outcome; bare `none`, `unknown`, or `not checked` is invalid.

## Priority rules

- P0: verified active critical incident.
- P1: bug or user-visible lag.
- P2: benchmark, agency, or workspace feature. Academy, multi-doc upload and speaker mapping/sidebar are P2; PRM remains dropped.
- P3: low-priority bounded improvement.

Never use `priority:p1`, `high-priority`, or other aliases.

No Codex architecture review or codex-arch-reviewed marker is required for promotion. For a named dependency, replace the sample blockers/wake with the exact reference and condition, use READY_GATED, and retain pm-blocked:dependency. The five ledger rows, safety constraints and required proof remain binding.
