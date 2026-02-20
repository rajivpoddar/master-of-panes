---
name: pane-handoff
description: Hand off a GitHub issue to a tmux dev pane — extract issue, git sync, assign state, send instructions, and optionally launch supervisor. Usage /master-of-panes:pane-handoff <pane> <issue>
arguments: "<pane> <issue>"
---

# /master-of-panes:pane-handoff

Full-workflow handoff of a GitHub issue to a tmux dev pane. Extracts the issue, syncs git, assigns state, sends instructions, and optionally launches a background supervisor.

## Arguments

`<pane> <issue>` — pane number (1-4) and GitHub issue number.

Example: `/master-of-panes:pane-handoff 4 1284`

## Instructions

Parse `$ARGUMENTS`: first token is the pane number, second is the GitHub issue number.

### Phase 1: Extract and Prepare

#### Step 1.1: Fetch issue details

```bash
ISSUE=<issue_number>
PANE=<pane_number>

# Fetch issue
TITLE=$(gh issue view $ISSUE --json title -q '.title')
LABELS=$(gh issue view $ISSUE --json labels -q '.labels[].name' | tr '\n' ',')
BODY=$(gh issue view $ISSUE --json body -q '.body')
```

#### Step 1.2: Detect recommended subagent

Based on issue labels and body text, detect the appropriate subagent:

| Pattern | Subagent |
|---------|----------|
| `integration.test`, `test.coverage`, `write.*test` | `integration-test-specialist` |
| `editor`, `track.change`, `prosemirror`, `tiptap`, `marks`, `extension` | `editor-specialist` |
| `pagination`, `page.break`, `template`, `layout.dsl`, `placeholder` | `pagination-template-specialist` |
| `ai`, `proofread`, `gemini`, `modal`, `transcription` | `ai-pipeline-specialist` |
| Everything else | `fullstack-dev` |

#### Step 1.3: Build handoff file

Extract the full issue (title, body, labels, comments) to `/tmp/handoff-<ISSUE>.md`.

The handoff file MUST use this exact workflow prefix template:

```markdown
Analyze the following issue and follow the workflow.

## Workflow

1. Create branch: `git checkout -b fix/<ISSUE>-short-description`
2. Enter plan mode (EnterPlanMode tool)
3. Explore codebase and design solution
4. Exit plan mode with permissions (ExitPlanMode with allowedPrompts)
   — Plan is saved automatically to `docs/plans/` (configured in `.claude/settings.json`)
   — PM is notified automatically via tmux when the plan file is written
   — **Immediately after ExitPlanMode**, rename the plan file to the issue slug:
     ```bash
     # Find the file just written (newest in docs/plans/)
     PLAN=$(ls -t docs/plans/*.md | head -1)
     # Rename to issue-NNN-short-slug.md (3-5 words from issue title, lowercase, hyphenated)
     mv "$PLAN" docs/plans/issue-<ISSUE>-short-slug.md
     # Example: docs/plans/issue-1364-per-template-formatting-rules.md
     ```
5. Implement using the recommended subagent (see below)
6. Run automated tests: `npx tsc --noEmit && bunx vitest run && bun lint`
7. **Pre-QA setup — complete ALL that apply before delegating:**
   - **Dev server:** Check if running: `curl -s -o /dev/null -w "%{http_code}" http://localhost:$(grep PORT .env.local | cut -d= -f2)/`
     If not 200, run `/bun-dev-server` skill to start it on your slot's port
   - **Convex:** If ANY `convex/` files changed → `~/.claude/skills/convex-dev-deploy/scripts/deploy.sh`
   - **Modal:** If ANY `modal/` files changed → `cd modal/docx && modal deploy processor.py` (or `modal/audio`)
8. **Write a QA Brief BEFORE delegating to qa-tester.** Fill in all values by running the commands shown:
   ```
   ## QA Brief for Issue #NNN

   **Port:** $(grep PORT .env.local | cut -d= -f2)   ← run this, include actual number
   **Branch:** $(git branch --show-current)
   **Dev server running:** YES/NO
   **Convex deployed:** YES/NO
   **Modal deployed:** YES/NO

   **Test data** — choose based on what you're testing:
   - Performance/editor: transcript k171t7vczd62twehrtdvhz8mcx81aejg (long-test-file, 4853 paragraphs)
   - Proofreading/formatting/DOCX: transcript k17aq2hx1d0n2ms3stna2amb2d7z331b (TRZ Corp v. Max Mutual - Frazier Deposition, medium)
   - Simple create/upload flows: tests/e2e/fixtures/heydonna.mp3 (200KB, creates new project)
   - Fallback if IDs don't exist in dev deployment: use any transcript from dashboard projects list

   **Profile:** Use per-slot profiles — derive slot from port:
   `SLOT=$((PORT - 3000))`  → 3001→1, 3002→2, 3003→3, 3004→4
   Admin: `--profile ~/.agent-browser/profiles/admin-slot${SLOT}`
   Regular: `--profile ~/.agent-browser/profiles/regular-slot${SLOT}`
   ⚠️ NEVER use the shared `admin` or `regular` profiles — per-slot only.

   **Test plan** (5-10 steps, each with exact action and expected outcome):
   1. Navigate to http://localhost:PORT/...   ← always use absolute URL with actual PORT number
   2. [Exact action: click X / type Y / wait for Z] → [Expected outcome]
   3. [Continue...]

   **What cannot be tested automatically and why:**
   (List backend-only scenarios, network error conditions, Modal-dependent flows)

   ⛔ CROSS-PORT RULE: NEVER follow Clerk redirects to another port.
   If redirected to a different port, STOP — kill daemon, restart on YOUR port.
   All navigation must use absolute URLs: http://localhost:PORT/path
   Use per-slot profiles: `--profile ~/.agent-browser/profiles/admin-slot${SLOT}`
   See the `heydonna-agent-browser` skill for full patterns.
   ```
   Delegate to qa-tester — **run synchronously, NOT in background** (`run_in_background: false`).
   **The qa-tester must NOT send the report to Slack.** Generate `/tmp/qa-report-<ISSUE>.md` and
   screenshots to `/tmp/qa-<ISSUE>-*.png` only. PM reads and delivers the report.
9. STOP after QA report is generated — PM will review and trigger PR creation

## Dev Server Rules
- Use `/bun-dev-server` skill to start YOUR slot's server (detects port from .env.local automatically)
- **NEVER stop, kill, or restart dev servers on OTHER slots' ports**
- NEVER run kill/pkill/lsof commands targeting server processes on other ports
- Each slot has its own port (3001-3004) determined by the worktree's .env.local

## E2E Test Rules
- **DO NOT add new E2E smoke tests** unless the issue explicitly says to write one
- The smoke test suite covers critical paths only — new tests require PM approval
- You may add unit tests and integration tests freely
- If the `/review-and-pr` skill suggests adding an E2E test, skip it and proceed without
```

After the workflow prefix, append:
1. **Subagent recommendation** — primary subagent and QA delegation
2. **Merge policy** — NEVER merge, PM reviews all PRs
3. Full issue content from `gh issue view $ISSUE --json number,title,state,url,body,labels,comments` piped through jq

#### Step 1.4: Update GitHub labels

```bash
gh issue edit $ISSUE --remove-label status:todo --remove-label status:in-review \
  --add-label status:in-progress --add-label "slot:$PANE"
```

### Phase 2: Tmux Handoff

#### Step 2.1: Git sync

Send `/git-sync-main` to the pane and wait for idle:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/send-to-pane.sh $PANE '/git-sync-main' --wait
```

#### Step 2.2: Clear session

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/send-to-pane.sh $PANE '/clear' --wait
```

**NOTE:** Do NOT send `/rename` after `/clear`. The `/clear` command creates a new session,
so any `/rename` sent afterward either applies to the dead session or gets lost. Session
tracking is handled by the pane state file (`assign-pane.sh`) instead.

#### Step 2.3: Send handoff content

Load the handoff file into the pane via tmux buffer.
**IMPORTANT:** Use separate send-keys calls with a delay between text and Enter.

```bash
PANE_ADDR=$(source ${CLAUDE_PLUGIN_ROOT}/scripts/pane-lib.sh && load_config && pane_address $PANE)
tmux load-buffer "/tmp/handoff-${ISSUE}.md"
tmux paste-buffer -t "$PANE_ADDR"
sleep 1
tmux send-keys -t "$PANE_ADDR" Enter
```

#### Step 2.4: Assign state

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/assign-pane.sh $PANE "#$ISSUE: $TITLE"
```

#### Step 2.5: Capture session ID

After the handoff prompt is delivered, wait for the session to start, then capture the
Claude Code session UUID from the most recent `.jsonl` file in the checkout's project dir.

```bash
sleep 5

# Derive the project dir from the checkout path
# Claude Code stores sessions at ~/.claude/projects/<path-with-dashes>/
CHECKOUT_PATH=$(tmux display-message -t "$PANE_ADDR" -p '#{pane_current_path}')
PROJECT_DIR_NAME=$(echo "$CHECKOUT_PATH" | sed 's|^/||; s|/|-|g')
PROJECT_DIR="$HOME/.claude/projects/-${PROJECT_DIR_NAME}"

# Get the most recent session file
SESSION_ID=$(ls -t "$PROJECT_DIR"/*.jsonl 2>/dev/null | head -1 | xargs basename 2>/dev/null | sed 's/.jsonl$//')

if [ -n "$SESSION_ID" ]; then
  bash ${CLAUDE_PLUGIN_ROOT}/scripts/update-pane-state.sh $PANE --session "$SESSION_ID"
  echo "Session ID: $SESSION_ID"
fi
```

This allows later `/resume $SESSION_ID` if the session needs to be restored after a `/clear`.

#### Step 2.6: Verify delivery

Capture output to confirm the pane received the handoff:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/scripts/capture-output.sh $PANE 5
```

### Phase 3: Verify Hooks Are Active

No supervisor needed — hooks handle the full lifecycle automatically. Hooks are permanently
configured in each slot's `.claude/settings.json` (no per-handoff installation needed):

- **Plan written** → `notify-plan-ready.sh` PostToolUse hook fires → PM pane receives message
- **Slot goes idle** → `slot-idle-notify.sh` Stop hook fires → `tmux send-keys` injects idle notification into PM pane

After verifying delivery (Step 2.6), the handoff is complete.

### Report

Show a summary table:
- Issue: #<number> — <title>
- Pane: <number> (address: <tmux_addr>)
- Subagent: <detected_subagent>
- Labels: status:in-progress, slot:<N>
- Handoff file: /tmp/handoff-<ISSUE>.md
- State file: ~/.claude/tmux-panes/pane-<N>.json

## Error Handling

- If pane is in TESTING mode: Show testing info, suggest a different pane. NEVER overwrite a TESTING pane.
- If pane is already OCCUPIED: Show current task and suggest releasing first
- If issue not found: Show error and exit
- If git-sync fails: Retry once, then report failure
- If pane is ACTIVE when handoff starts: Wait for idle (up to 2 minutes)

## Slot Selection Guidelines

When the PM doesn't specify a slot number, prefer higher-numbered free slots (3, 2, 1) to avoid
conflicts with PM manual testing which typically uses lower-numbered slots. Always check pane
status first and skip any slots in TESTING or OCCUPIED state.
