# Canonical LLM Capture Contract

This is the single canonical home for how LLM/STT capture is produced and
verified in HeyDonna. Planners, implementers, and reviewers all reference this
rule; do not restate or fork it.

## The contract

Canonical LLM capture runs the existing E2E tests against the application in
capture mode, recording the actual requests/responses made by that path. Reuse
the existing capture workflow, proxy and fixture store. Verification is the
subsequent genuine strict-replay E2E run. Do not invent direct provider/Modal
generators, standalone capture tests, synthetic request builders, or separate
manifest/hash/readback approval gates as substitutes or prerequisites.
Standalone fixture-maintenance tools remain separate and must not gate canonical
capture. If an actual application call is not captured, repair the existing
capture path at that boundary; do not create a parallel path.

## What this does NOT relax (operational safeguards remain)

These are safeguards, not extra acceptance tests, and stay in force:

- head-binding of capture and replay evidence to the exact latest head SHA;
- duplicate/first-write-wins fixture protection;
- customer-data privacy (fixtures stay in the repo/R2 fixture store, no
  `~/Downloads` fallback, no committed real customer content);
- truthful failed-run status (a failed or partial capture run reports as failed,
  never as green).

Capture green alone is not readiness: readiness is capture PLUS strict-replay
E2E on the latest head. STT fixture-key determinism gates remain valid as
fixture-correctness safeguards; they do not replace canonical capture or become
a separate release gate.
