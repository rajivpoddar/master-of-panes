# PM/Kimi3 Review-Cap Policy (Highest Priority)

This policy supersedes any older rule, skill, handoff, or prompt that sends a
plan/code review cap directly to CTO or asks for another review after a PM
rescue patch.

When `PLAN_REVIEW_CAP_REACHED` or `CODE_REVIEW_CAP_REACHED` occurs:

1. The slot stops ordinary review/rework but keeps ownership of the issue, PR,
   branch, and worktree. Do not release, reassign, or add `pm-blocked:cto`.
2. Report the cap through the existing supported `Skill(message-pm)` or direct
   MoP PM delivery path, preserving the exact marker and review result. PM
   prepares one immutable rescue ledger entry, then delegates exactly one
   `pm-kimi3-pr-rescue` against the frozen source with `isolation="worktree"`
   and this typed prompt:
   ```text
   PM_KIMI3_RESCUE_REQUEST
   ledger_entry_path: <absolute immutable ledger path>
   live_slot_worktree: <absolute owning slot checkout>
   END_PM_KIMI3_RESCUE_REQUEST
   ```
   Never hand-compose `PM_FABLE_RESCUE_MOP_BINDING`; the installed launch hook
   derives that legacy authority block, every output path, and the ledger
   digest deterministically. The typed runner creates its own detached
   exact-head checkout. The live slot worktree remains read-only reference
   evidence and must never be the rescue's execution checkout.
3. MoP must contain the matching real PM-side `PostToolUse:Agent` event for
   that request ID, exact issue/PR head or plan SHA, and predeclared packet and
   patch paths. A slot-local receipt or prose claim is not authority.
4. On a validated `PATCH_READY`, apply exactly the named patch after
   `git apply --check`. On a validated `NO_PATCH_REQUIRED`, keep the exact
   source unchanged and never fabricate an empty patch. In either case, run
   only the named affected unit/integration tests and continue to the next
   non-review phase. Do not run Codex, PM Claude, plan, code, QA-review,
   TypeScript, lint, build, Playwright, capture, CI, or E2E as rescue review or
   proof unless the packet's affected unit/integration command itself invokes
   a required unit/integration harness.
5. The Kimi3 patch is terminal review adjudication. Any adaptive edit,
   additional path, stale head/plan SHA, patch mismatch, application failure,
   or affected-proof failure invalidates the bypass and counts as PM rescue
   failure.
6. PM may invoke `cto-rescue-pr` or `cto-rescue-issue` for a review-cap reason
   only with the MoP-validated exact-source Kimi3 `FAILED` packet supplied
   through `--pm-rescue-proof`.
   - If the isolated Agent dies before writing its packet, PM may supply the
     canonical `PM_FABLE_RESCUE_FAILURE_PACKET` only when it is bound to that
     same isolated Agent event and a digest-verified terminal transport-error
     artifact.
   - If the live head drifts during rescue, the failure packet remains bound to
     the original source tuple and records the exact current live head. Drift
     makes the patch non-actionable; it must not make CTO escalation
     unreachable.

No retry with Opus, Sonnet, Codex, another slot, or another ordinary review is
allowed between PM rescue and CTO escalation.
