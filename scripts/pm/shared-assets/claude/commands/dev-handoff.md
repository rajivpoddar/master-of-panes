<!-- caveman-boundary: inserted 2026-04-19 -->
> **Caveman boundary:** Output from this skill goes to **dev slots (precise handoff instructions; caveman fragments break one-shot precision)**. Write full, professional, grammatical English. If caveman mode is active in the session, treat this skill as an Auto-Clarity exception (see `~/.claude/plugins/marketplaces/caveman/rules/caveman-activate.md`). No fragments, no dropped articles, no abbreviations.

Hand off a NEW GitHub issue to a dev slot. Arguments: slot number and issue number (e.g., "1 1682").

For rework (existing PR), use `/rework-handoff`. For QA, use `/qa-handoff`. `/handoff` auto-routes.

Runs as a background Task agent so PM stays event-driven.

## Immediate Actions (PM main thread)

1. Before launching the handoff agent, check target slot N's session age using `/tmp/sakshi-heartbeat.json` or run:
   `bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/handoff/scripts/session-age-preflight.sh N`
2. If the slot is stale and between work units, PM runs MoP logged clear first:
   `bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh --require-terminal N`.
   Wait for a fresh slot JSONL/effective_start, then continue.
3. Create task: "Handoff #ISSUE to slot N — background agent"
4. Launch background Task agent (see prompt below) with `run_in_background: true`
5. PM is free — continue processing other events

## Background Agent Prompt

```
You are a handoff agent. Deliver GitHub issue #ISSUE to dev slot N.
Working directory: /Users/rajiv/Downloads/projects/heydonna-app

Run these steps in order. Stop on any failure and report it.

STEP 1 — Session-age pre-handoff guard:
  PM should already have cleared stale free/standby sessions before launching this agent.
  Verify the target slot is acceptable:

    bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/handoff/scripts/session-age-preflight.sh N

  Exit 0 → continue.
  Exit 1 → STOP. Report ONLY:
    "BLOCKED_STALE_SESSION_PRE_HANDOFF: slot N session is stale. PM must run
     MoP logged clear (`bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts/mop-clear-slot.sh --require-terminal N`),
     wait for fresh JSONL/effective_start proof, then rerun handoff."
    Do not build or deliver the handoff.

STEP 2 — Fetch issue and detect subagent:
  TITLE=$(gh issue view ISSUE --json title -q '.title')
  LABELS=$(gh issue view ISSUE --json labels -q '.labels[].name' | tr '\n' ',')
  # Route: editor/prosemirror/tiptap → editor-specialist
  # Route: docx/lxml/python-docx → ai-pipeline-specialist
  # Route: ai/proofread/gemini/modal → ai-pipeline-specialist
  # Route: test/coverage/mutation → integration-test-specialist
  # Route: pagination/template/layout → pagination-template-specialist
  # Default → fullstack-dev

STEP 2.4 — REWORK_TARGET detection (MANDATORY):
  Per Rajiv directive 2026-05-16 10:45 IST thread `1778907856.776679`:
  "add rework target. update pr-mark-ready". When `/dev-handoff` is called against an
  issue that already has an OPEN PR (either because the router was bypassed, the slot was
  reassigned to overflow, or the existing PR pointer was missed), the handoff MUST tell the
  slot to rework the existing PR — NOT create a new branch / new PR.

  2.4a. Grep issue body for explicit PR pointers:
      BODY=$(gh issue view ISSUE --json body --jq '.body')
      BODY_PR=$(echo "$BODY" | grep -oE '(PR ?#|pulls/)[0-9]+' | grep -oE '[0-9]+' | head -1)

  2.4b. Search OPEN PRs by issue reference:
      SEARCH_PR=$(gh pr list --search "in:title #ISSUE OR in:body #ISSUE" \
        --state open --json number,headRefName,title --limit 5)
      EXISTING_PR=$(echo "$SEARCH_PR" | jq -r '.[0].number // empty')
      EXISTING_BRANCH=$(echo "$SEARCH_PR" | jq -r '.[0].headRefName // empty')

  2.4c. If BODY_PR or EXISTING_PR set:
      Resolve to a single (PR, BRANCH) pair (prefer BODY_PR if it matches a row in SEARCH_PR;
      otherwise use first row of SEARCH_PR). Export:
        REWORK_TARGET="$EXISTING_PR"
        REWORK_BRANCH="$EXISTING_BRANCH"
      STEP 4c injects a REWORK_TARGET block into the handoff body; the atomic
      assignment call later delivers that branch instruction.

      Recommended escalation: if router should have caught this (i.e. PR exists), prefer
      `/rework-handoff` instead. Continue with `/dev-handoff` only if PM explicitly intended
      this path (e.g. overflow slot assignment for an issue with an in-flight PR).

  2.4d. If neither pointer found: REWORK_TARGET unset, continue with new-branch flow.

  **Why this gate exists:** PR #4445 (2026-05-15) — slot 2 (Hasta) was directed to REWORK
  #4445 on the gemini-flash + capture-extend scope, but the slot created TWO NEW PRs
  (slack-alert, capture-extend) instead of pushing to the existing #4445 branch. Rajiv
  flagged twice (02:46 PM + 03:41 PM): "why is hasta creating a new pr for slack alert.
  this gemini flash pr has to be reworked" / "why is hasta creating a new pr for extended
  catpure? 4445 has to be reworked for it". Fragmenting fix surface across 3-4 PRs forces
  re-stacking, re-review, ordered merges. The handoff body never carried a REWORK_TARGET
  constraint — slot defaulted to new-branch.

  Reference: `feedback_handoff_rework_target_constraint_on_existing_pr` 2026-05-16.

STEP 2.5 — Issue-spec consistency preflight (MANDATORY — BLOCKS HANDOFF):
  Per feedback_issue_body_must_be_rewritten_on_scope_change_not_appended_as_comments
  (Rajiv directive 2026-05-12 15:37 IST). Run:

    bash /Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/handoff/scripts/preflight-check.sh ISSUE

  Exit 0 → continue to STEP 3.
  Exit 1 → STOP. Report ONLY this:
    "Preflight: issue body stale + N PM-correction comments newer than body.
     Per Rajiv directive 2026-05-12 15:37 IST, PM must rewrite issue body via
     `gh issue edit ISSUE --body-file <path>` before handoff. Reference:
     feedback_issue_body_must_be_rewritten_on_scope_change_not_appended_as_comments.
     Handoff BLOCKED."
    Then EXIT. Do not build handoff file. Do not run arch review. Do not deliver.

  **Why this gate exists:** PR #4364 (issue had body §2 saying `cand1: gpt-audio-mini,
  cand3: MiMo` while PM-correction comment said `Gemini → GPT Audio → MiMo` ladder).
  Body never edited. Slot defaulted to "body is contract, comments supplementary"
  → shipped inverted ladder order across 10 plan rounds + 9 PR review rounds.
  Codex APPROVED each round because it graded against the (stale) body.

  **Override (escape hatch — explicit Rajiv confirmation only):**
    HANDOFF_BYPASS_STALE_BODY=1 <handoff command>

  **PM recovery path when gate fires:**
    1. gh issue view ISSUE --json body --jq .body > /tmp/issue-ISSUE-body-pre-$(date +%Y-%m-%d).md
    2. Compose new body reflecting the corrected scope.
    3. gh issue edit ISSUE --body-file <new-body-path>
    4. Post audit comment with the original body content + link to correction trail.
    5. Re-run the handoff command.

STEP 3 — Codex architecture review (MANDATORY — BLOCKS HANDOFF):
  3a. Check if issue already has label "codex-arch-reviewed":
      HAS_LABEL=$(gh issue view ISSUE --json labels -q '[.labels[].name] | any(. == "codex-arch-reviewed")')
      If "true": skip to STEP 4.

  3b. Run `/codex-app-arch-review` skill (preferred) or fall back:
      - Primary: invoke the `codex-app-arch-review` skill with the full
        arch review template (6 criteria + domain constraints). Writes marker file.
      - Fallback: if the primary skill is unavailable (rate-limited, timeout, error), read the arch review template at
        `/Users/rajiv/.claude/skills/codex-architecture-review/templates/arch-review-prompt.txt`
        and run via `codex exec` piped mode.

  3c. Build the review context:
      - ISSUE_NUMBER: from args
      - SYMPTOM_OR_REQUEST: from issue body (the user's reported problem)
      - DIAGNOSIS_OR_APPROACH: from issue body (the proposed fix/approach)
      - AFFECTED_CODE: read relevant code files that the issue references

  3d. Act on verdict — THIS IS A HARD GATE, NOT A SUGGESTION:
      CONFIRMED → add label "codex-arch-reviewed", post findings as issue comment, continue to STEP 4
      NEEDS_DEEPER_INVESTIGATION → **HARD STOP. Do NOT proceed to STEP 4.** Report ONLY this:
        "Arch review: NEEDS_DEEPER_INVESTIGATION — [paste exact Codex concerns]. Handoff BLOCKED. PM must review."
        Then EXIT. Do not build handoff file. Do not deliver to slot. Do not update labels.
      MISDIAGNOSED → **HARD STOP. Do NOT proceed to STEP 4.** Report ONLY this:
        "Arch review: MISDIAGNOSED — [paste exact Codex concerns]. Handoff BLOCKED. Do NOT hand off."
        Then EXIT. Do not build handoff file. Do not deliver to slot. Do not update labels.
      Timeout (no output after 10 min) → add label "codex-arch-reviewed", note "timed out" in handoff file, continue

  **Why this gate exists and why you MUST NOT bypass it:**
  - #3161 was handed off without arch review → 3 rework cycles (wrong diagnosis)
  - #2973 created parallel infrastructure instead of reusing existing (Codex caught it)
  - #3220 on Mar 31: agent received NEEDS_DEEPER_INVESTIGATION but continued anyway.
    Rajiv caught it: "how come the handoff completed before codex arch review completed?"
    The agent treated the stop gate as a warning. It is NOT a warning. It is a STOP.
    Including findings in the handoff does NOT substitute for PM review of the concerns.
    (Rajiv directive 2026-03-31: "fix the handoff agent")

STEP 4 — Build handoff file:
  4a. Read the workflow template: cat /Users/rajiv/Downloads/projects/heydonna-app/.claude/skills/handoff/templates/workflow-prefix.md
  4b. Fetch the issue: gh issue view ISSUE --json number,title,state,url,body,labels,comments
  4c. Build /tmp/slot-delivery-ISSUE.md by concatenating IN THIS EXACT ORDER:
    1. Natural greeting from Dhruva (one line):
       Slot 1 = Rohini, Slot 2 = Hasta, Slot 3 = Ashwini, Slot 4 = Chitra
       Example: "Hey Rohini, here's a P0 for you."
    1b. **REWORK_TARGET block (if REWORK_TARGET set from STEP 2.4)** — prepend these
        two lines BEFORE the workflow-prefix.md content:

          **REWORK_TARGET=PR-$REWORK_TARGET on branch `$REWORK_BRANCH`** — DO NOT create a new PR.
          `git checkout $REWORK_BRANCH` + push changes to extend that PR. Do NOT run `/review-and-pr` (creates new PR).

        Include this block directly in the handoff file if the template uses the
        `{{REWORK_TARGET_BLOCK}}` placeholder.
    2. The FULL workflow-prefix.md content (copy verbatim — do NOT summarize or omit steps)
    3. Subagent recommendation: "Recommended subagent: [type]" (TEXT ONLY — never a command). This is the mandatory Phase 3 implementation specialist; the slot must use it after Codex plan review and before Codex code review.
    4. Codex arch review findings (if any) under "## Architecture Review Notes"
    5. Full issue content (title, URL, body, labels, comments)
    5a. **E2E Classification gate** — read the issue body for the `## Fix Classification` section. Append a "## Test Coverage Directive" block to the handoff:
        - If HAPPY-PATH → "Test coverage: E2E spec assertion + unit/integration coverage. Specify the exact spec file from the issue's `## Test Coverage` section."
        - If EDGE-CASE / ERROR-PATH → "Test coverage: unit + integration + Python wire tests. **DO NOT add an E2E assertion for this fix.** Rajiv directive 2026-05-02 13:55 IST: E2E is happy-path only; edge cases and error paths use unit/integration. Plan-agent must enforce this in Phase 1 and qa-tester must enforce it in Phase 6."
        - If issue lacks classification → flag as "## Test Coverage Directive: classification missing — slot must classify in Phase 1 plan; default to unit/integration unless the fix is a clear happy-path restoration."
    6. **Agent-Browser Devtools Testing** (include for CSSPagination, DOCX export, template rendering issues):
       "## Devtools Testing Note
       For CSS pagination or rendering issues, use agent-browser with devtools to inspect DOM state.
       Launch `agent-browser` to open a headed browser, navigate to the editor, and use devtools
       to verify section wrappers, pagination passes, computed styles, and DOM measurement results.
       This replaces the need for manual Rajiv devtools inspection."
    7. Signoff: "— Dhruva"

  CRITICAL: The workflow prefix contains Phase 1-4 with checkpoints. If the handoff file
  is missing these phases, the slot won't know to use plan-agent or /review-and-pr.
  Always include the FULL template — never paraphrase it. If a compact/slot-claim
  handoff is used, it must still include the explicit workflow chain:
  foreground `plan-agent`, Codex plan review, selected area implementation
  specialist, Codex code review, foreground `qa-tester`, Codex QA review. No
  native Plan Mode, no self-authored main-pane plan, no main-pane implementation
  in place of the selected specialist, and `PLAN_AGENT_REQUIRED` /
  `IMPLEMENTATION_SPECIALIST_REQUIRED` if the required agent cannot run. Stop
  after each agent + reviewer pair, not between the agent and its review:
  plan-agent → Codex plan review → STOP; implementation specialist → Codex code
  review → STOP; qa-tester → Codex QA review → STOP.

  CRITICAL: NEVER send `claude --profile`, `claude --model`, or any command that starts
  a new Claude Code process to a slot. The slot already has an active session. The handoff
  is delivered as literal task-file content by the atomic assignment command below.

STEP 5 — Safety check for uncommitted work:
  Design intent: regenerated-file drift (Convex code-gen) is normal and harmless.
  The actual blockers are (a) real uncommitted code edits the slot has not pushed, and
  (b) local commits on the slot's branch that are not yet on origin (diverged unpushed work).

  5a. Porcelain check — filter out known regenerated files before deciding:
      cd /Users/rajiv/Downloads/projects/heydonna-app-300N
      DIRTY=$(git status --porcelain \
        | grep -vE '^[ MARCU?!]+ convex/_generated/(api\.d\.ts|api\.js|dataModel\.d\.ts|server\.d\.ts|server\.js)$' \
        | head -5)
      If DIRTY is non-empty: STOP and report
        "Slot N has uncommitted non-regenerated work: $DIRTY"
      Do NOT fix it. If DIRTY is empty (only regenerated files dirty, or no
      output at all), the tree is clean enough to continue.

  5b. Branch-divergence check — only block on UNPUSHED local commits:
      BRANCH=$(git -C /Users/rajiv/Downloads/projects/heydonna-app-300N branch --show-current)
      if [ "$BRANCH" != "main" ]; then
        UNPUSHED=$(git -C /Users/rajiv/Downloads/projects/heydonna-app-300N \
          log --oneline "origin/$BRANCH..HEAD" 2>/dev/null | head -5)
        # If origin/$BRANCH doesn't exist, list main..HEAD commits not on origin/main
        if [ -z "$(git -C /Users/rajiv/Downloads/projects/heydonna-app-300N rev-parse --verify "origin/$BRANCH" 2>/dev/null)" ]; then
          UNPUSHED=$(git -C /Users/rajiv/Downloads/projects/heydonna-app-300N \
            log --oneline main..HEAD | head -5)
        fi
        If UNPUSHED is non-empty: STOP and report
          "Slot N has unpushed local commits on $BRANCH: $UNPUSHED"
        Do NOT fix it.
      fi
STEP 6 — Atomically assign and deliver the handoff:
  This operation commits slot ownership, delivers the task file, and updates
  issue labels together. Do not separately reset the slot, reserve the issue,
  or deliver the handoff through another path.

    python3 /Users/rajiv/.claude/scripts/mop-assign-slot.py \
      --slot N --issue ISSUE --repo-id 992731533 \
      --task-file /tmp/slot-delivery-ISSUE.md

  Require the typed `status:assigned` JSON terminal and exit 0. On refusal or
  non-zero exit, STOP and report the exact JSON/stderr; do not use another
  assignment or delivery path.

  ANTI-PATTERN — NEVER DO THESE:
  - NEVER run `cat FILE | claude --resume`; this starts a new process, not the slot's session.
  - NEVER run `cd heydonna-app && git checkout`; that is the PM's clone, not the slot's.
  - NEVER improvise delivery; the atomic assignment command above is the only delivery path.
  - The slot already has an active Claude session. The assignment command delivers the
    formal task file through the sanctioned path; do not bypass it.

STEP 7 — Verify assignment labels:
  The atomic assignment call has already updated the issue labels. Verify:

    gh issue view ISSUE --json labels --jq '[.labels[].name] | sort | join(",")'

  Required labels: `status:in-progress` and `slot:N`. Forbidden labels:
  `status:todo` and `status:in-review`.

  If verification fails, STOP and report `HANDOFF_RESERVATION_DRIFT` with the
  assignment terminal; do not retry through a separate state path.

STEP 8 — Verify delivery:
  sleep 3
  tmux capture-pane -t 0:0.N -p -S -5 | tail -5

Report: "Handoff complete: #ISSUE to slot N (subagent: <detected>, arch review: <status>)" or failure details.
```

## After Agent Completes

1. Check the result — if success, note in working memory
2. If failure (especially arch review MISDIAGNOSED/NEEDS_INVESTIGATION), investigate before retrying
3. **Slot monitor starts automatically** — MoP injects `/slot-active N` when slot starts working → PM runs `/loop 5m /check-slot N`
4. **MANDATORY: Update `docs/pm/todo.md`** — reflect new slot assignment

ARGUMENTS: $ARGUMENTS
