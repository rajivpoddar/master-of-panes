---
name: explore-issue
description: |
  PM investigates a reported problem by launching a background agent that:
  (1) explores the codebase to diagnose root cause, (2) runs Codex architecture
  review to validate the diagnosis, (3) files a GitHub issue with correct labels.
  Use when: Rajiv reports a bug or requests investigation with "explore-issue:" prefix,
  or a customer-origin issue create was denied and a durable
  explore_issue_required pm-ops obligation is open for a Slack source.
  Arguments: quoted description of the problem, optional Slack channel/thread TS.
  One obligation per background agent — NEVER batch unrelated Slack sources.
  NOT for: implementing fixes (hand off to slots), reviewing plans (use pm-plan-approval),
  creating PRs (use review-and-pr).
author: PM Agent
version: 2.0.0
date: 2026-08-05
---

# Explore Issue

PM-driven investigation skill. Launches a background agent that explores the codebase,
diagnoses root cause, validates the diagnosis with Codex Architecture Review, and files
a GitHub issue — all without blocking the PM.

The pre-issue-create-audit hook no longer uses a session latch. Denied customer-origin
creates atomically upsert ONE durable pm-ops obligation per Slack source
(kind=explore_issue_required, severity=high, horizon=hourly, owner=pm,
target_type=slack_thread, target_id=<CHANNEL>:<THREAD_TS>,
dedupe_group=explore-issue:<CHANNEL>:<THREAD_TS>) with evidence binding channel, root
thread_ts, canonical permalink, title/body path, original+current body SHA, reason and
required_at. This skill drives that obligation through the state machine:

```
OPEN -> IN_PROGRESS -> READY_TO_FILE -> RESOLVED_FILED
                         |
                         +-> (WAITING_EXTERNAL)
OPEN -> ... -> RESOLVED_NO_ISSUE
```

READY_TO_FILE mints a one-use permit bound to channel/thread_ts/final body SHA/title
digest. Only that permit allows exactly one create, and only through the single create
owner (`issue-create-owner.py create ...`): it captures the body bytes once, runs the
audit and ledger/tracker gates on them, records durable in-flight state before gh,
feeds gh the identical bytes on stdin and consumes the permit only on a verified issue
URL (gh output plus a GitHub readback of repo/number/title/body). A raw
`gh issue create` for a permit-holding source is denied before any permit
write. Resolution requires the returned issue number/URL (RESOLVED_FILED) or an
explicit typed no-issue disposition (RESOLVED_NO_ISSUE). No issue number is required
before creation.

When an issue is a child of a feature, pass `--milestone` with the parent feature's milestone; never file a tracker issue (Rajiv C0ALZJHGE49 ts 1793976431.298289). Include this requirement in the background agent's filing instructions.

## Observation admission

A confirmed customer or Abi symptom, including a confirmed artifact difference, is
enough to file an `INVESTIGATION` issue even when the cause and control point are
`UNKNOWN` or a clearly labelled `HYPOTHESIS`. Preserve the observed evidence, the
evidence limits, and the source thread; do not invent a root cause or call the issue
implementation-ready. An unconfirmed report remains unfiled until the existing
substantive issue-contract checks establish a reportable observation. A duplicate
reconciles to the existing issue. Never acquire a customer query, artifact download,
capture, or other external evidence action merely to record a confirmed observation;
such work requires its own explicit authority. Only causal proof may promote the
record to a `FIX`/`CONFIRMED` claim and an implementation-ready disposition.

## When NOT to Use

- Implementing fixes — hand off to dev slots after the issue is filed
- Reviewing implementation plans — use `pm-plan-approval`
- Issues already filed — use `handoff` to assign to a slot
- Quick questions that don't need code exploration — answer directly

## Trigger

Rajiv sends a message with `explore-issue:` prefix, often with a screenshot or description,
OR a pre-issue-create-audit block surfaces an open explore_issue_required obligation.

## One Source Per Agent

A background agent may carry EXACTLY ONE obligation/source. Never batch unrelated
Slack sources into one agent, one obligation, or one completion. Each source gets its
own obligation (dedupe_group=explore-issue:<CHANNEL>:<THREAD_TS>), its own agent, and
its own resolution. Launching one agent per source keeps the state machine and permit
bound to a single channel/root/message.

## Quick Start

0. **OBLIGATION READ (MANDATORY when skill is invoked from a pre-issue-create-audit BLOCK):**
   Before parsing the problem description, read the per-source state record and the
   durable obligation. The state record is at:

   ```
   /tmp/heydonna-explore-issue-state/<CHANNEL>__<THREAD_TS>.json
   ```

   If the root record is terminal and a DIFFERENT later reply reports a new
   problem, its key is `<CHANNEL>__<ROOT_TS>__<REPLY_TS>.json` (dots become
   underscores). NEVER overwrite or reopen a terminal root. The record retains
   `thread_ts=<ROOT_TS>` and `source_message_ts=<REPLY_TS>`; the obligation target
   and dedupe suffix become `<CHANNEL>:<ROOT_TS>:<REPLY_TS>`. A replay of the
   original source remains terminal; it does not create a new intake.

   In every helper shell for a reply intake, set this BEFORE any read, transition,
   permit mint or resolution (use the original source message timestamp for an
   existing root intake, or the root timestamp for a legacy root record):

   ```bash
   export EXPLORE_ISSUE_SOURCE_MESSAGE_TS="<SOURCE_MESSAGE_TS>"
   source /Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh
   # Select ONCE at intake; when resuming, use the key of the supplied STATE_FILE.
   export EXPLORE_ISSUE_SELECTED_KEY="$(basename "<STATE_FILE>" .json)"
   explore_issue_state_file "<CHANNEL>" "<ROOT_TS>"
   ```

   For fresh intake without a supplied STATE_FILE, select once with
   `explore_issue_source_key "<CHANNEL>" "<ROOT_TS>"` and export that result as
   EXPLORE_ISSUE_SELECTED_KEY before the first read or side effect. Carry this exact
   selected key, STATE_FILE and obligation_id into the agent brief and EVERY later
   helper shell (including mint, create and resolution). Never run the selector
   again: root closure or reopening cannot change an in-flight intake's target.
   The helper initially chooses the reply key only when the root is terminal and
   belongs to a different source message. Preserve this selector throughout the intake.
   Never guess the source from the newest state file or replace root thread_ts
   with reply_ts. Use the verified reply permalink as the single `Source:` line
   in the final issue body, keeping `thread_ts` in its query equal to the root.

   Where `<CHANNEL>__<THREAD_TS>` is the Slack source key (dot in thread_ts replaced
   with underscore), e.g. `C0AGWPQFKHA__1785932534_656769.json`. The block message names
   the exact state file and source. To find the matching record:

   ```bash
   explore_issue_state_file "<CHANNEL>" "<ROOT_TS>"
   ```

   If the state record exists, READ it and PRESERVE the original Slack/body context
   (`channel`, `thread_ts`, `source_message_ts`, `body_file`, `reason`, `original_body_sha256`,
   `current_body_sha256`) and pass it through to the agent prompt verbatim. The
   investigation MUST use the same source intel that triggered the gate — don't fabricate
   a different scenario. Record `OBLIGATION_SOURCE=<CHANNEL>:<THREAD_TS>` and
   `STATE_FILE=<state file>` for the transition steps.

   If no state record exists but the create was denied, write one at launch via the
   agent (IN_PROGRESS) after upserting the obligation (see below).

1. Parse the problem description from Rajiv's message
2. **DECLARATION GATE (MANDATORY — write this before Step 3):**
   ```
   EXPLORE: [1-line problem description]
   SOURCE: [who reported, which channel/thread]
   EXISTING CHECK: [searched issues? list results or "none found"]
   EXPLORATION SCOPE: [what code paths to trace]
   DESIGN INTENT: [is the observed behavior by design or a bug? check code comments]
   ARTIFACT GATE (causal/FIX only): [for separately authorized DOCX/export/formatting
   work, which artifacts need inspection? Leave bounded for INVESTIGATION.]
   LLM PROXY IMPACT GATE (causal/FIX only): [for separately authorized external API
   work — see below]
   OBSERVATION STATUS: [CONFIRMED or UNCONFIRMED; state the evidence limit]
   CODEX REVIEW: mandatory for causal/FIX claims; not an evidence gate for
   confirmed INVESTIGATION observations
   ```

   **LLM PROXY IMPACT GATE (MANDATORY only for separately authorized causal/FIX work):**
   For causal/FIX work that touches external API calls (Modal → Scribie/Gemini/WhisperX/LLM,
   or any new outbound HTTP from Modal), the agent MUST assess:
   (a) Does the proxy (`modal/shared/llm_proxy_server.py`) intercept this call?
   (b) Is the call sync request/response (replay-friendly) or async webhook (proxy can't replay)?
   (c) If async, what test-side mechanism replays the callback in E2E?

   Flag explicitly in the issue body's `## Affected Code` section or a dedicated
   `## LLM Proxy Impact` section. Async webhook flows MUST include a test-side callback
   synthesizer or Modal-side proxy-mode short-circuit (Lesson: #3953 #3945).

   **CODEX REVIEW GATE (MANDATORY for causal or fix claims):**
   The background agent MUST run Codex architecture review (Phase 2) before making a
   causal or implementation-ready claim. Codex review is not an evidence-acquisition
   gate for filing a confirmed observation as `INVESTIGATION`; PM cannot skip review
   when it is making a root-cause or fix claim. (Lesson: #2843.)

   **ARTIFACT GATE (MANDATORY only for separately authorized causal/FIX work):**
   Before launching causal/FIX work, identify which artifacts must be inspected (DOCX
   export, R2 content, pipeline input/output). The agent MUST inspect the actual output
   artifact's internal structure — not just read the generating code. A confirmed
   INVESTIGATION observation must preserve its evidence limit and must not download
   customer JSON/DOCX or capture new data merely to satisfy this gate. (Lesson: #2833.)

3. Create a tracking task: "Explore: [short description]"
4. Launch background Agent with `run_in_background: true` — **MANDATORY**
   Use the named agent type `explore-issue-agent`. Do not use `general-purpose`
   or the older `feature-dev:code-explorer` + issue-creator split for this workflow.
   The agent MUST receive exactly ONE Slack source (channel + root thread_ts).
5. PM is free — continue processing other events immediately
6. When agent completes (background notification), review the filed issue and notify Rajiv

**CRITICAL: Always use `run_in_background: true` when launching the Agent tool.**
The PM must remain event-driven. Never block on code exploration — the investigation
can take 5-15 minutes. Rajiv directive: "explore-issue skill should use a background
agent so that the pm remains event driven."

## Background Agent Prompt

Launch shape:

```
Task(
  description: "Explore issue: <short summary>",
  subagent_type: "explore-issue-agent",
  run_in_background: true,
  prompt: "Investigate and file via the explore-issue contract.

  ORIGINAL REQUEST:
  [Rajiv's exact words / ARGUMENTS]

  DECLARATION:
  [Paste the declaration gate fields from Quick Start step 2]

  OBLIGATION CONTEXT:
  [Paste STATE_FILE, CHANNEL, THREAD_TS, SOURCE_MESSAGE_TS, body_file, reason, original+current body SHA.]

  Requirements:
  - write IN_PROGRESS for this single source at launch
  - preserve source context from the obligation
  - inspect required artifacts only for separately authorized causal/FIX work
  - run Codex diagnosis review only for a causal/FIX claim
  - file a confirmed observation as `INVESTIGATION`, or a Codex-confirmed causal
    diagnosis as `FIX`/`CONFIRMED`; then produce READY_TO_FILE + permit and create
    the issue
  - resolve RESOLVED_FILED with the returned issue number/URL, or
    RESOLVED_NO_ISSUE on a typed no-issue disposition
  - return issue URL, labels, Codex output path, and resolution"
)
```

The detailed prompt below is retained as the agent's operating contract. The
agent definition at `~/.claude/agents/explore-issue-agent.md` is authoritative
for boundaries and completion output.

```
You are an explore-issue agent for the HeyDonna codebase.
Working directory: /Users/rajiv/Downloads/projects/heydonna-app

PROBLEM DESCRIPTION:
[Paste Rajiv's exact words — preserve his terminology, it often points to the exact code path]

OBLIGATION (single source only):
[channel, root thread_ts, state file, body file + SHA, reason, required_at]

STEP 0 — STATE MACHINE (at launch):
  Mark the single source IN_PROGRESS:
    source /Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh
    explore_issue_set_state "<CHANNEL>" "<THREAD_TS>" IN_PROGRESS
  If the state record does not exist yet, first upsert the obligation and write an
  OPEN record (the pre-issue-create-audit hook normally does this on denial), then
  set IN_PROGRESS:
    source /Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh
    explore_issue_upsert_obligation "<CHANNEL>" "<THREAD_TS>" \
      "<permalink>" "<body_file>" "<title>" "<reason>" "<required_at>" "<orig_sha>" "<cur_sha>"
    explore_issue_write_state "<CHANNEL>" "<THREAD_TS>" IN_PROGRESS \
      "<body_file>" "<title>" "<reason>" "<required_at>" "<orig_sha>" "<cur_sha>" "<obligation_id>"
  WAITING_EXTERNAL is allowed when investigation waits on an external answer:
    explore_issue_set_state "<CHANNEL>" "<THREAD_TS>" WAITING_EXTERNAL

PHASE 1 — Code Exploration:
  a. Search for relevant code using Grep, Glob, and Read tools
  b. Trace the execution path from the user-facing symptom to the root cause
  c. Identify the specific file(s) and line(s) where the bug/gap exists
  d. Check git log for recent changes that may have caused a regression
  e. Check if there are existing issues or PRs related to this problem:
     gh issue list --search "KEYWORDS" --state all --limit 5
  f. Document your findings: root cause, affected files, call chain

PHASE 2 — Codex Architecture Review (causal/FIX work only):
  Derive source-bound file paths FIRST (replace <CHANNEL> and <THREAD_TS> with the
  actual values from Phase 1; THREAD_SAFE replaces the dot with an underscore):
     _CG_SAFE="<CHANNEL>_$(printf '%s' '<THREAD_TS>' | tr '.' '_')"
     _PROMPT_PATH="/tmp/codex-explore-prompt-${_CG_SAFE}.txt"
     _OUTPUT_PATH="/tmp/codex-explore-output-${_CG_SAFE}.txt"
  These paths are unique to this source; concurrent investigations for different
  sources cannot overwrite them.

  a. Write the review prompt to the source-bound prompt path:
     cat > "$_PROMPT_PATH" << 'PROMPT_EOF'
     Review this root cause diagnosis for a bug in HeyDonna (court reporter transcript editor).
     Focus on production soundness: simplicity, integration fit, and hidden complexity.
     REPORTED SYMPTOM: [user's exact description from Phase 1]
     DIAGNOSED ROOT CAUSE: [your findings from Phase 1]
     AFFECTED CODE: [file:line references from Phase 1]
     Review for:
     1. ROOT CAUSE ACCURACY
     2. ALTERNATIVE PATHS
     3. OUT-OF-SCOPE TRAP
     4. FIX APPROACH
     5. APPROACH MATCH
     Verdict: CONFIRMED / NEEDS_DEEPER_INVESTIGATION / MISDIAGNOSED
     6. EFFORT ESTIMATE: classify S / M / L / XL. Output: `EFFORT:<S|M|L|XL>` on its own line.
     PROMPT_EOF
  b. Run Codex CLI with 15m timeout, writing to the source-bound output path:
     timeout 900 bash -c 'cat '"$_PROMPT_PATH"' | codex exec --skip-git-repo-check --sandbox read-only --full-auto 2>/dev/null' > "$_OUTPUT_PATH"
  c. Read the output: cat "$_OUTPUT_PATH"
  c2. Copy the actual Codex output to the per-source verdict file. Because both
      $_OUTPUT_PATH and the verdict path are source-bound, this cp is safe from
      concurrent-session contamination:
        source /Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh
        _VERDICT_PATH="$(explore_issue_verdict_path "<CHANNEL>" "<THREAD_TS>")"
        mkdir -p "$(dirname "$_VERDICT_PATH")"
        cp "$_OUTPUT_PATH" "$_VERDICT_PATH"
      (Replace <CHANNEL> and <THREAD_TS> with the actual values from Phase 1.)
      Do NOT use /tmp/codex-explore-output.txt or /tmp/codex-explore-prompt.txt;
      those are shared global paths from before this fix and must not be used here.
      The pre-issue-create gate reads from $_VERDICT_PATH; it is not the global path.
  d. Parse the verdict. [HIGH] ONE Codex pass only (Rajiv 2026-09-28 20:58 IST, thread `1790609092.596809`, verbatim: *"limit the codex arch review to 1 pass in explore issue agent"*); never re-run the reviewer on a revised body; residual concerns go under `## Open questions`:
     - If CONFIRMED: proceed to Phase 3 with the causal claim marked CONFIRMED.
     - If NEEDS_DEEPER_INVESTIGATION and the reported observation is CONFIRMED:
       proceed to Phase 3 as `INVESTIGATION`, with cause/control point
       `UNKNOWN` or `HYPOTHESIS` and explicit evidence limits. Do not launch an
       unapproved artifact/query/capture probe just to satisfy this branch.
     - If MISDIAGNOSED but the observation is CONFIRMED: discard the causal claim
       and proceed to Phase 3 as `INVESTIGATION`, with cause/control point
       `UNKNOWN` or `HYPOTHESIS` and explicit evidence limits; do not launch a new
       evidence probe.
     - If MISDIAGNOSED and the observation is UNCONFIRMED, or if the observation is
       otherwise UNCONFIRMED: do not file; go back to Phase 1 only when an authorized
       investigation can establish a reportable observation.

PHASE 3 — READY_TO_FILE + File GitHub Issue:
  After the observation is confirmed (for `INVESTIGATION`) or Codex CONFIRMED (for a
  causal/fix claim), mint the one-use permit bound to the EXACT raw digest of the FINAL
  body bytes and the title digest, then create the issue. The final body must carry the
  canonical source permalink. The permit is the only release for the pre-issue-create-audit
  gate. The body digest is of the file's exact bytes (trailing newlines and NUL bytes
  included): use explore_issue_file_sha256 on the file, never a command substitution
  such as `$(cat file)`, which strips them. A permit minted with the old stripped digest
  no longer matches and must be re-minted; consumed/UNKNOWN records are never touched:

  source /Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh
  FINAL_BODY_SHA=$(explore_issue_file_sha256 <final body file>)
  TITLE_DIGEST=$(explore_issue_sha256 "<final title>")
  explore_issue_mint_permit "<CHANNEL>" "<THREAD_TS>" "$FINAL_BODY_SHA" "$TITLE_DIGEST" "<obligation_id>"
  explore_issue_set_state "<CHANNEL>" "<THREAD_TS>" READY_TO_FILE

  Then create the issue with the exact final body/title the permit was minted against,
  through the single create owner. It is the ONLY command in the Bash call: a plain
  script path, no env prefix, no wrapper (bash -c, xargs, time, rtk), no `&&` `;` `|`,
  no redirection. Use `--body-file` with a literal path written in a previous step.
  The PreToolUse rewrite handlers registered in the settings in force (rtk, priority-label,
  agent-browser, convex, modal, send-to-slot, the rescue sandbox, and any other that can emit
  updatedInput) run in parallel with the guard, so the guard DISCOVERS them (not a fixed list)
  and dry-runs each on your exact command, letting it through only if none would rewrite, deny
  or block it: what you write is what runs. If one would, the guard blocks before anything
  runs or is written and names the handler; remove the text it keys on (put long or special
  body text in the `--body-file`, never inline) and run it again. Any compound or wrapped
  `gh issue create` is denied upfront, and a simple raw create is parsed byte-exactly (every
  -t/-b/-F spelling) and refused when anything about it is ambiguous or unreadable.
  The flags are the issue-create flags (--title/-t, --body/-b, --body-file/-F,
  --label/-l, --assignee/-a, --milestone/-m, --project/-p, --repo/-R); only the
  leading gh command words are replaced by the owner path:
    python3 /Users/rajiv/.claude/hooks/issue-create-owner.py create --title "<final title>" --body-file <final body file> --label "..."

  The owner holds the exact body bytes in one in-memory buffer, runs the audit
  (source/verdict/permit) and ledger/tracker gates on those bytes (the buffer reaches
  them in memory, never as a file; a rejected gate writes nothing: no obligation, no
  draft, no cache), then under the shared permit lock re-checks, binds source/invocation/digest
  and writes the durable in-flight record, runs gh with the identical bytes on stdin and
  consumes the permit only when gh printed exactly one issue URL AND a GitHub readback of
  that issue (repo, number, title, body) matches what was sent. Its stdout is that URL;
  its stderr ends with one status line:
    FILED <url>                 permit consumed; resolve the obligation with this URL.
    REFUSED NO_GH_START ...     a gate rejected the create before anything was written;
                                the permit is intact. Fix what the message names, then
                                run the owner again. A drift refusal means the
                                body/title changed after the mint: re-mint the permit
                                against the new final SHA/digest first.
    NO_GH_START                 gh could not be started; the in-flight record was
                                released and the permit is intact. Run it again.
    UNKNOWN / RECONCILIATION-ONLY / FILED_UNBOOKED
                                gh may have started, its URL could not be verified by
                                the GitHub readback (unavailable or mismatching), or
                                the permit bookkeeping failed. NEVER re-mint, restore or
                                create again for that source: an in-flight record is never
                                released by a claimed absence (explore_issue_restore_permit
                                refuses it); only the owner releases it, and only when gh
                                definitely never started. A CONSUMED record is never released
                                either, including a legacy one with no issue_url or
                                consumed_by (restore returns 6 for it and writes nothing):
                                a caller-supplied absence string is an assertion, not a
                                durable no-start proof. Use the existing reconcile-by-search
                                path to find the issue by canonical Source permalink
                                and resolve the same obligation. If search is
                                inconclusive, remain reconciliation-only; absence of a
                                response is not proof of no create.
  A raw create for this source is denied by the audit hook before any permit write;
  compound or wrapped forms of either command are denied too. A distinct reply source
  can still mint its own permit.

  Before reporting the issue as filed, register each confirmed OPEN prerequisite explicitly
  named in its body as a native GitHub blocked-by dependency. For each `<dep>`, resolve
  `ID=$(gh api repos/Scribie/heydonna-app/issues/<dep> -q .id)` and POST
  `gh api -X POST repos/Scribie/heydonna-app/issues/<new>/dependencies/blocked_by -F issue_id=$ID`.
  Read back `gh api repos/Scribie/heydonna-app/issues/<new>/dependencies/blocked_by` and
  verify every edge. Ignore incidental references, parent/umbrella issues and closed
  prerequisites. Report the verified edges, or `blocked-by: none`; failed or missing edges
  are not successful filing.

  **Classify the fix BEFORE writing the issue body** (HAPPY-PATH vs EDGE-CASE/ERROR-PATH)
  per the `## Test Coverage` rules below. Rajiv directive 2026-05-02 13:55 IST:
  *"E2E is happy-path only. Edge cases and error paths are covered by unit / integration / Python wire tests, NOT E2E."*

  Body format:
  ```
  Source: [verified source-message permalink with thread_ts=root]

  ## Source
  - Reporter: [name or Slack handle]
  - Slack: [same verified source-message permalink]
  - Channel: [channel ID]
  - Root thread_ts: [exact root timestamp]
  - Source message_ts: [exact reply timestamp, or root for a root report]

  ## Problem
  [User's exact reported symptom — quote their words]

  ## Fix Classification
  **[ ] INVESTIGATION** — observation confirmed; cause/control point UNKNOWN or HYPOTHESIS
  **[ ] HAPPY-PATH** — feature works under normal conditions
  **[ ] EDGE-CASE / ERROR-PATH** — guard / validator / error / retry / fallback branch

  ## Root Cause / Evidence Status
  [Diagnosed root cause with file:line references, or UNKNOWN/HYPOTHESIS for an
  INVESTIGATION disposition. State the evidence limit and do not imply proof.]
  [Codex confidence: CONFIRMED, or NOT_YET_PROVEN for INVESTIGATION]

  ## Affected Code
  - `file.ts:NN` — [what's wrong]

  ## LLM Proxy Impact
  [Required when the issue touches external API calls; omit if not applicable.]

  ## Suggested Fix
  [Approach recommended by Codex review]

  ## Test Coverage
  **Classification (REQUIRED — pick exactly one):**
  - [ ] **HAPPY-PATH** — E2E spec + unit/integration
  - [ ] **EDGE-CASE / ERROR-PATH** — unit + integration + Python wire, explicitly "NOT E2E"

  ## Schema PR Phase 8b (CONDITIONAL — only if issue touches convex schema/migrations)
  ## Subagent Recommendation
  [editor-specialist / ai-pipeline-specialist / fullstack-dev / pagination-template-specialist]
  ```

  For every Slack-origin issue, `## Source` is mandatory. Build the canonical permalink
  from the obligation's `channel`, root `thread_ts`, and `source_message_ts`
  (legacy records default the source message to the root):
  `https://scribiers.slack.com/archives/<CHANNEL>/p<SOURCE_MESSAGE_TS_WITHOUT_DOT>?thread_ts=<THREAD_TS>&cid=<CHANNEL>`.
  A bare timestamp, channel name, uncorroborated reply, or different-channel URL is
  insufficient. The pre-create hook rejects drift against the permit-bound body SHA,
  so the body must be byte-identical to the final body the permit was minted for.

  Labels:
  - Always include "status:todo" + "bug" or "enhancement".
  - Add effort label if Codex emits `EFFORT:S|M|L|XL` (check existing labels first).
  - Add domain labels when they exist: "editor", "ai", "pagination", etc.

PHASE 4 — Resolution (MANDATORY):
  After the issue is filed (or on a typed no-issue disposition), resolve the obligation
  and terminal state:

  ```bash
  source /Users/rajiv/.claude/hooks/explore-issue-latch-guard.sh
  # Issue filed:
  explore_issue_resolve_filed "<CHANNEL>" "<THREAD_TS>" "<issue_number>" "<issue_url>" "<title>" "<FINAL_BODY_SHA>" "<SOURCE_MESSAGE_TS>"
  # Typed no-issue disposition (Rajiv redirected to a verified direct-main fix):
  explore_issue_resolve_no_issue "<CHANNEL>" "<THREAD_TS>" "fixed_directly_on_main: <note>" "<title>" "<FINAL_BODY_SHA>" "<SOURCE_MESSAGE_TS>"
  ```

  The final optional source-message argument on both resolve helpers binds the
  resolution to the same reply as the state and permit; omitting it uses the
  exported selector. `explore_issue_resolve_obligation` also accepts it as its
  fifth argument. Terminal records are immutable and repeated resolution is a no-op.

  Resolution atomically resolves the pm-ops obligation (external_state RESOLVED_FILED
  with the issue number/URL, or RESOLVED_NO_ISSUE with the disposition) and writes the
  terminal state record. NEVER leave the source OPEN/IN_PROGRESS/READY_TO_FILE after
  filing; the durable obligation drives the intake, so the PM must not accumulate
  unresolved obligations.

Report back: "Filed #NNN: TITLE (INVESTIGATION: observation confirmed) — obligation
RESOLVED_FILED" or "Filed #NNN: TITLE (Codex: CONFIRMED) — obligation
RESOLVED_FILED" as applicable, or failure details with the exact state left behind.
```

## After Agent Completes

When the background agent finishes:
1. Read the result — verify the obligation resolved (RESOLVED_FILED + issue number, or
   RESOLVED_NO_ISSUE) and the issue was filed.
2. Notify Rajiv in the relevant Slack thread with the disposition: for `INVESTIGATION`,
   say "Filed #NNN — confirmed observation; cause not yet proven"; for `FIX`/`CONFIRMED`,
   say "Filed #NNN — [summary]. Codex validated the diagnosis."
3. If slots are available and Rajiv wants it assigned, use `/handoff`

## Anti-Patterns

- **Never batch unrelated Slack sources** — one obligation/source per agent. A batched
  agent blocks one source on another and corrupts the per-source state machine.
- **Never skip Codex review for a causal/FIX claim** — Phase 2 is mandatory for that
  claim. A confirmed `INVESTIGATION` observation may retain an UNKNOWN/HYPOTHESIS
  cause without an evidence-acquisition detour. The #2492 postmortem proved that
  unvalidated diagnoses waste entire implementation cycles.
- **Never substitute your own scenario for the reporter's** — use Rajiv's exact words in
  the issue.
- **Never file without checking existing issues** — Phase 1 step (e) prevents duplicates.
- **If Codex says MISDIAGNOSED, discard only the causal diagnosis.** If the observation
  remains CONFIRMED, continue as `INVESTIGATION` with UNKNOWN/HYPOTHESIS cause and file
  without delaying for another probe; truly UNCONFIRMED observations remain blocked.
- **Never resolve RESOLVED_NO_ISSUE without a typed disposition** — the no-issue path
  requires an explicit Rajiv redirect to a verified direct-main fix, never silence.

## Learned Lessons

- #2492: Slot diagnosed fullSection strategy but reporter said "manual formatting" (proceedings path).
- #2833: Code pattern analysis ≠ runtime behavior. Inspect the actual artifacts (R2
  content, exported DOCX) before diagnosing pipeline stages.
- #3633/#3634/#3636: exploration-skipping on customer bug reports creates duplicate,
  masking, SOP-skipping issues — the durable obligation gate exists to prevent this.
- Wedge (2026-08-05): the session latch created a circular create/completion gate and
  blocked ALL customer-origin intake. The durable source-keyed obligation + one-use
  permit replaced it; stop is non-blocking with a valid obligation.

### Mandatory only for separately authorized causal/FIX export/formatting/pipeline work:

These checks do not gate filing a confirmed observation as `INVESTIGATION`. Without
customer-data authority, preserve the evidence limit and do not download customer JSON
or DOCX, fetch R2 content, or require a real-data fixture merely to record the issue.

1. **R2 DATA INSPECTION** — Download the actual stored content and exported output
   from R2. If data survives a stage → that stage is NOT the root cause.
2. **RUNTIME VERIFICATION** — For third-party library assumptions, add empirical
   verification (e.g. `python3 -c "from docx import Document; ..."`).
3. **CONTRADICTION CHECK** — Before filing, check: does the evidence contradict the
   diagnosis?
4. **INTEGRATION TEST WITH REAL DATA** — For export bugs, the issue must require
   reproduction using the actual customer's editor JSON + exported DOCX as fixtures.
