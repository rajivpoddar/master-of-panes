---
name: heydonna-file-promote-issue
description: File one bounded HeyDonna app-code or app-test issue and admit it to the Ready Pool, with directive fidelity, live duplicate and ownership checks, a complete contract and verified routing. Supports named dependency-held work; excludes operational-only work and unresolved product decisions.
---

# File and Promote a HeyDonna Issue

Follow the current [triage policy](/Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/pm-triage/skill.md) and repository AGENTS.md. User authorization and newer explicit directives govern routine choices; escalate material changes to product behavior, acceptance, authority or cost. Do not introduce a fresh approval hop for work already authorized.

Create one independently shippable app-code or executable app-test/CI-producer issue, targeted at half a day or less. Internal control-plane, operational configuration, docs-only, tracking, investigation-only and external-action work stays with its actual operational owner. A stale infra label does not turn an app/CI producer repair into operational work.

`status:todo` means Ready Pool membership. Named dependency-held app work belongs there at every priority; it remains unassignable until the live dependency checks pass. Other product/external/activation holds remain nonclaimable. Admission needs no Codex architecture review, review marker or architecture label. Implementation-plan, code-review and release requirements remain in force.

## Establish the contract

Read the original directive, complete issue/comment and relevant PR evidence, current-main code and live MoP ownership. Compare intended capability, production failure and actual runtime control point. Use the [minimum-solution ladder](/Users/rajiv/.codex/skills/_shared/minimum-solution-ladder.md) to keep the repair bounded. For legal prompt changes preserve AGENTS.md's artifact-derived examples and production-path proof.

Search live issues and PRs by capability, failure mode, symbols and identifiers; read plausible matches. Reuse the actual delivery issue when one already owns the work. Do not create competing work for an active PR or slot. Administrative closure or an archived operational record does not prove delivery or clear a capability gate.

Fetch origin/main without changing the checkout. Inspect the fetched commit and record `source_commit`, runtime symbol and concrete duplicate/overlap search receipts. The bundled validator loads the installed canonical triage validators from `~/.claude/scripts`; product source evidence binds to the fetched app main, not retired app-repository copies of the control plane.

## Draft and route

Use [references/issue-contract.md](references/issue-contract.md). Keep at most three independently implementable primary ACs, with stable IDs and per-AC Surface, QA reachability and Required proof. Keep negative requirements, forbidden implementations and proof mandatory; moving them outside the primary list never waives them or makes oversized work small. Split genuinely broader work and preserve the remaining parent requirements.

Preserve deterministic screenshot scenarios and durable visual evidence where applicable. Choose exactly one Scribie impact checkbox and complete its four details. Complete the five-row ICL. Use one canonical priority matching metadata:

- P0: verified active critical incident.
- P1: bugs, user-visible lag and user/data/export harm.
- P2: benchmarks and agency/workspace features; Academy, multi-doc upload and speaker mapping/sidebar are P2. PRM is dropped for now.
- P3: low-priority bounded work. Runnable P3 work refills only a claimable deficit below ten; named dependency-held membership applies at every priority.

Promote every eligible P0/P1/P2 regardless of pool size. Ten claimable unassigned issues is a floor.

Ready Pool fields: priority, lane, branch_slug, ac_summary (<=140 characters), claimable_slot_type, blockers, work_type, owner, next_action, wake_condition, disposition, application_state and required_validation. Preserve source_commit, runtime_symbol and duplicate/overlap receipts. Branch slugs are <=40 lowercase alphanumeric/hyphen characters.

Use `blockers: none` for unblocked work. For a named dependency use `#N` or `[#N, #M]`, retain the actual capability/proof wake and add `pm-blocked:dependency`. Do not replace a genuine product/external gate with a numeric dependency or none. `disposition: READY_GATED` is suitable for a dependency-held child. PM alone assigns numbered slots.

## Validate and create once

Write a named body file in a separate file-edit step before creation. Run:

```bash
python3 /Users/rajiv/.codex/skills/heydonna-file-promote-issue/scripts/validate_candidate.py \
  --repo-root /Users/rajiv/Downloads/projects/heydonna-app \
  --title '<issue title>' --priority P1 --body-file /tmp/heydonna-issue-<slug>.md
```

Require ok=true. Recheck live duplicates, ownership, dependencies and the source-main evidence immediately before creation. Reconcile any relevant drift; do not infer completion or rerun an uncertain write. Verify required labels exist. Then create once using the pre-existing body file:

```bash
gh issue create --repo Scribie/heydonna-app \
  --title '<issue title>' --body-file /tmp/heydonna-issue-<slug>.md \
  --label status:todo --label P1
```

Add existing kind/area labels as appropriate and `pm-blocked:dependency` for named dependencies. Do not add codex-arch-reviewed. Do not create backlog and then perform a redundant promotion. Record intent before the POST; on timeout or response loss, reconcile read-only rather than issuing another create.

## Verify the live result

Read back the full body and labels: OPEN, one status:todo, one matching priority, exact validated body, no active slot/PR conflict, and retained dependency gates. Save the live body and rerun the bundled validator. Validate the ICL with `python3 ~/.claude/scripts/validate-issue-contract-ledger.py --body-file <file> --require-qa-proof --json`.

Use `backlog-triage.py audit-ready-pool` for membership and contract results. Use `check-dispatch-readiness --issues N` for unblocked execution, and `ready_pool_select.py --check N` immediately before PM assignment. An unresolved dependency is a valid held member, not a failed promotion. Other genuine holds stay outside claimable work. Existing Todo metadata repair uses the canonical guarded `repair-ready-pool` route; incomplete ACs/ICL must first be repaired from source.

Keep the actual issue/child as the work contract. Create PM operational obligations only for real operational actions with owner/action/wake; do not recreate retired routine ready_pool or triage_retained_scope inventory mirrors. Sync with `python3 ~/.claude/scripts/pm-ops.py sync --write --no-live --reason pm-triage`.

Return the issue URL, priority, branch, source/runtime and duplicate evidence, verified contract result, and whether it is currently claimable or dependency-held. Report unknown product facts precisely and retain all gates; no dry run alone is completion.
