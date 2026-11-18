---
name: proxy-agent
description: Overflow launcher when PM_SUBAGENT_CAP is full (S148). Runs a file-based PM agent out of process via CLIProxyAPI while the PM stays on direct Claude. Do not use this as the default launch — Skill(launch-agent) comes first. 12b is never a proxy run (S147). Runs are named peer sessions steerable with native SendMessage; safety hooks stay on.
---

# proxy-agent

Source: Rajiv, DM D0AMF0XE6TS thread 1794087326.732679:
- ts 1794102850.404149: "can we use these instead of hotfixes now? the model has to chosen based on how urgent the work is."
- ts 1794103421.702049: "create a skill for the proxy subagents and wire it up in buddhi."

Buddhi rule: S120 for a proxy that is already the overflow. S148: new work starts at `Skill(launch-agent)`. This skill runs only when live in-process PM subagents are at `PM_SUBAGENT_CAP` (ceiling 6), or when launch-agent's overflow step calls it. 12b never comes here (S147).

## Why
The PM runs on direct Claude because PM quality degrades behind cliproxy. In-process subagents always share the PM's endpoint, so they bill Claude. A proxy agent is a separate `claude -p` process pointed at CLIProxyAPI (127.0.0.1:8317), which routes to Codex, Grok and Antigravity accounts. Prompt caching works there because session-affinity was turned on 2026-11-08.

## Launch (always Bash `run_in_background: true`)
1. Write the brief to a file with the **Write tool**, never a heredoc in the launch command. PM hooks scan Bash text: for example, a brief that mentions an issue-create command gets blocked. The brief must be complete and one-shot, and it keeps the agent's usual header contract, e.g. hd-planner / hd-builder (pm-hotfix is a compatibility shim) / `ISSUE:` / `PR:` / `PLAN:` / `SPECIALIST:`.
2. Launch it:
   ```bash
   PROXY_AGENT_NAME=proxy-<short-task> PROXY_AGENT_OUT_DIR=<scratchpad>/proxy \
     ~/.claude/scripts/proxy-agent.sh <agent-type> <brief-file> [--model auto|<alias>] [--max-turns N] [--out <file>]
   ```
   - `<agent-type>` must exist as `~/.claude/agents/<type>.md` or as `.claude/agents/<type>.md` in `--cwd`. Built-in agents such as claude-code-guide and Explore cannot run this way.
   - The PM is notified when the Bash job exits. The last line is `PROXY_AGENT_DONE rc=<n> ... out=<file> log=<file>`. Read the `--out` file for the report. Read the `.log` only for errors, and never print keys.
3. Record ownership with `pm-ops.py obligation-upsert ... --owner "proxy-agent <model> (<name>, <time>) ..."`.

## Checkout and tmux
- The single `PM_ROLE_TYPES` allowlist contains `growth`, `pm-ops-tooling`, `pm-triage-agent`, `customer-artifact-investigator`, `cleanup-pr-runner`, `pm-admission-reviewer`, `donna-influencer` and `explore-issue-agent`. These start in `/Users/rajiv/Downloads/projects/heydonna-app`, including when `--cwd` is supplied. Other types retain dev routing (explicit `--pm-role` remains available). Dev agents must create their own worktree; the launcher does not yet create one automatically.
- Before the tracked-changes guard, ordinary unstaged tracked-file modifications in the exact dedicated setup checkout are saved as a binary-capable `git diff` in `~/.heydonna/proxy-setup-dirt/<UTC>.patch`, then restored with `git checkout -- <files>`. Success logs `PROXY_SETUP_DIR_RESTORED patch=<path>`. HEAD is never moved by this restore; untracked/ignored files are untouched. Staged changes, deletions, renames, conflicts, submodule dirt and other checkouts retain the existing refusal. Archive/checkout errors refuse the launch. Offline proof: `python3 -I ~/.claude/scripts/tests/test_proxy_agent_routing.py`; normal `PROXY_AGENT_DRY_RUN=1` reports routing without touching git.
- Default cwd: `/Users/rajiv/Downloads/projects/heydonna-app-proxy` (PORT 3009); override with `PROXY_AGENT_CHECKOUT` (or `--cwd`). The untouched 3005 checkout remains an explicit fallback. Missing clones, live slots 3001–3004, PM cwd and tracked changes are refused; untracked files are allowed. Never edit/commit in this rule-source clone: create a separate worktree for edits.
- The dedicated checkout has its own Convex dev project `heydonna-proxy` / `small-setter-918`; do not reuse a slot deployment. Provider/Modal endpoints and seeded browser-QA readiness must be provisioned separately before local app use; app proof stays on remote boxes. No MoP slot registration or local MoP relay is installed.
- Each named peer runs in a detached `0:px-<name>` window of the attached session, without changing the selected window or PM pane layout. `PROXY_AGENT_TMUX_SESSION` is a test/alternate-session override. Clean exits close automatically; failed windows remain dead for review.
- A wrapper EXIT trap reports normal completion and early shell termination via `~/.claude/scripts/proxy-agent-stop.sh` → the existing `message-pm.sh` MoP relay. The message includes name, rc, output path and the final RESULT line (or UNAVAILABLE reason). This is verified PM pane delivery, NOT a claim of native SendMessage transport. Delivery is fail-open and recorded in `<out>.delivery`; Bash completion remains available. SIGKILL/machine failure cannot run an EXIT trap.
- The wrapper waits via `tmux wait-for`; output/log paths and native `--name` steering remain unchanged.

## Model routing: Claude haiku/sonnet/opus/fable (CURRENT, Rajiv 2026-11-13)

Source: Rajiv, DM D0AMF0XE6TS thread 1794573312.657559, ts 1794577100.350209: *"enable only dc53c2ad-dev@heydonna.law and dc53c2ad-qa@heydonna.law. disable the rest of the accounts. configure proxy agents to use haiku/sonnet/opus/fable. fable should be used only for planning and P0's which get stuck. update the skill."*

- S138's two-account pin is dropped (S142). Claude proxy accounts follow the burn dial (four team accounts). An operator ceiling file, if present, still bounds CLAUDE_PROXY_CAP.
- Aliases: `haiku`=`claude-haiku-5-5`, `sonnet`=`claude-sonnet-5-5`, `opus`=`claude-opus-5-5`, `fable`=`claude-fable-5-2`. All 200k window / 64k out. Nested subagents always map haiku/sonnet/opus to the same Claude ids.
- **Ladders (CURRENT = buddhi S142/S143; S138/S139/S140 are dropped).** No Gemini, no Antigravity, no Sol (Rajiv ts 1794720589.429929: "apply 4. cut down sol usage on proxies.").
  - LIGHT: claude-haiku-5-5 → gpt-6-luna. MID: claude-sonnet-5-5 → gpt-6.1-terra. TOP: claude-opus-5-5 → grok-4.7. REVIEW: claude-fable-5-2 → TOP. First model not in 429/402 cooldown wins.
  - hd-builder rung 1 haiku→luna, rung 2 sonnet→terra, rung 3 opus. hd-proof-qa rung 1 sonnet→terra, rung 2 opus. hd-planner is TOP. Reviewer proxies use REVIEW. Do not pass `--model sol`, `hydrogen`, `carbon`, or `ag-*`.
  - 12b is not a proxy run. S147: every due 12b is an in-process sonnet PM subagent, cap-exempt.
  - OpenCode Go pool (Rajiv D0AMF0XE6TS thread 1794926930.078169, ts 1794930258.014469): LIGHT is haiku → hy4-flash-free → grok → luna, MID is sonnet → deepseek-v4.1-flash → grok → terra. hy4-flash-free, deepseek-v4.1-flash and muse-spark-1.3-contributor share ONE cap, `OPENCODE_PROXY_CAP` (default 2, read from `~/.claude/state/proxy-caps.env`). `muse-spark-1.3-contributor` (trains on prompts) is the final entry of EVERY tier but is emergency-only across ALL routes (CTO REVISE 2026-11-17, C0ALZJHGE49 thread 1794931098.013929, ts 1794932145.795929): it is used, as a tier pick or as an explicit `--model`, only when EVERY eligible non-emergency route in every tier (Claude, Grok, Codex, hy4, deepseek) is known capped, burn-refused or in a 402/429 cooldown. A healthy route outside the current rung, a probe failure or any unknown keeps it gated; a cap exemption is not emergency permission. The pool cap also guards an explicit `--model hy4-flash-free|deepseek-v4.1-flash|muse-spark-1.3-contributor` (cap 0 = off); a refused primary exits 75 with `REFUSED primary <id> (opencode pool_cap|emergency_gate blocker=<route>)`. Nested haiku/sonnet/opus aliases never use the OpenCode pool (the live count sees only the primary `--model`), so they stay on the Claude/Grok/Codex routes. Each launch appends to `~/.claude/state/proxy-emergency-muse.log`. Models route through CLIProxyAPI. `proxy-burn-dial.py` (PM-owned, `~/.claude/scripts/cost`, not a MoP asset) preserves an existing `OPENCODE_PROXY_CAP` line on apply.
  - Free-model scope gate (Rajiv DM D0AMF0XE6TS ts 1794984605.382609 "yes"; CTO REVISE C0ALZJHGE49 thread 1794985651.254889, ts 1794986198.378749). `hy4-flash-free` and `muse-spark-1.3-contributor` (only these two) are DEFAULT-DENY: a launch may use them only when its scope is verified safe. Declare it per launch with `--data-scope no-customer-content,no-prod-write` (a flag, not env). A missing or any other value, a sensitive role (customer-artifact-investigator, hd-investigator, sync-manager-specialist, lockout, pm-lock-release, prod-admin*, prod-modal-deploy*), an unreadable brief, or brief text naming beloved-cardinal-941, prod/production or customer denies; brief text can only veto, never grant. `remote-qa-runner` also needs `--qa-mode replay-only`: the launcher then pins `PROXY_AGENT_QA_REPLAY_ONLY=1` (the spec lane refuses `--on-miss` with `QA_REPLAY_ONLY_REFUSED`) and denies the capture lane in the generated settings; `--strict-replay` in the brief is not evidence. A denied launch skips both ids (tier picks fall to the next model; a raw `--model` exits 75 `REFUSED free-model guard`). The verdict is computed once, logged as `free-model scope=<verified|denied> why=<reason>`, recorded beside the run state and restored by `--resume` (a missing, mismatched or denied record stays denied; `--data-scope` is ignored on resume). Auto-continuation reuses the launch's model and does not continue a free-id run whose verdict is not `verified`. Caps, the emergency gate, deepseek, glm and every paid route are unchanged.
  - Caps are four independent numbers (Claude, Codex, Grok, total) plus the OpenCode pool cap. A Claude-pool deny never lowers Codex, Grok or total (ts 1794920638.829349). Codex under its Friday horizon takes proxy work. Grok 402 means the balance is exhausted.
  - Rung bounds (PM ops call 2026-11-13, within S131 "3h or earlier accepted checkpoint"): P0/P1 = 1h per rung; P2/P3 = 3h per rung. Worst-case stuck time is about 6h (P0/P1) or 18h (P2/P3) before the CTO decision. A run waiting on an external event (CI, CTO decision, box lease) pauses its clock.
  - Explicit `luna`/`terra` map to LIGHT/MID. `sol` does not. Nested haiku/sonnet/opus stay Claude ids. `fable` is the reviewer tier, not a 12b route.
- Do not launch `gemini-*`, `hydrogen`, `carbon`, or `ag-*`.
- Pace from the hourly account line, not a heartbeat usage section.

The sections below are LEGACY and must not be followed. S142 dropped S138/S139/S140. S143 removed Gemini/Antigravity. Sol is not routed. 12b is in-process sonnet (S147), not `fable → sol`.

## LEGACY: Model by urgency (S120 / S129, superseded 2026-11-13)

Hydrogen (`gemini-4-hydrogen-xhigh`) is preferred when available, per Rajiv's 13 Nov directive (ts 1794573397.310359); `luna`/`terra` auto-remap; `sol` never. Per Rajiv's approved 13 Nov per-agent default (ts 1794573982.972709), dev proxies default to Luna and PM/admin proxies to Terra when Hydrogen is unavailable; an explicit `--model` always wins except for the approved Luna/Terra-to-Hydrogen remap.
| alias | use for | window / max out |
|---|---|---|
| `auto` (default) | Omit `--model`. Order, before the main model and the `ANTHROPIC_DEFAULT_*_MODEL` exports: `ag-opus` when Antigravity Claude Opus is not exhausted, else `gemini-pro` (`gemini-4-argon-high`, separate Antigravity quota from `gemini-3.8-flash`), else `gemini-flash` (`gemini-3.8-flash-high`), else `grok` (`grok-4.7`), else `luna` (`gpt-6-luna`). No round-robin. Gemini Flash is usable when `quotaInfo.remainingFraction` >= 0.10, or when listed with no `quotaInfo`; a `quotaInfo` containing only `resetTime` is treated as `0.00` (exhausted). Antigravity Claude and Gemini Pro `none` (missing `quotaInfo`) are UNKNOWN, not available: auto probes `claude-opus-5-5-high`, `claude-sonnet-5-5-high` and `gemini-4-argon-high` with a 1-token `/v1/messages` call, and only HTTP 200 (with no future `exhausted_until`) selects them. Never treats a 1-token probe 200 as available while a recorded `exhausted_until` is in the future. A 429 marks that exact model id exhausted for the `Resets in …` window parsed from the body, else 1 hour (`exhausted_until[<model_id>]` plus `ag_opus_exhausted_until` / `ag_sonnet_exhausted_until` / `gemini_pro_exhausted_until` / `gemini_flash_exhausted_until` / `grok_exhausted_until`, kept past the 5-minute cache). When a launched run exits `rc≠0` with a 429 `Individual quota reached … Resets in …` in the run's output file (`/tmp/proxy-agent/<run>.md`) or log, the parsed window is recorded for the exact model id (for every model, including `gemini-pro`, `gemini-flash` and `grok`) and the wrapper prints one clear `PROXY_AGENT_MODEL_EXHAUSTED model=<id> resets_in=<..> seconds=<..> until=<..>` line so PM can relaunch it. Each launch prints `proxy-agent: model=<alias> id=<id> window=<n> max_out=<n> haiku=<..> sonnet=<..> opus=<..> reason=<ag_opus=0.00 ag_sonnet=0.00 gemini_pro=1.00 gemini_rf=0.00 grok=200 rr=->`. Explicit `--model` skips this. | selected model's window |
| `ag-opus` / `ag-sonnet` | Explicit Antigravity Claude. `ag-opus` is `claude-opus-5-5-high`; `ag-sonnet` is `claude-sonnet-5-5-high` (`owned_by=antigravity`, the `-high` effort variants of Opus 5.5 / Sonnet 5.5). Skips the quota selector. Nested models stay the explicit fallback: haiku→`gemini-3.8-flash-high`, sonnet/opus→`grok-4.7`. | 1M / 128k |
| `sol` | **ONLY P0s and other genuinely urgent work** (live lockout/data-loss, P0 security- or sync-sensitive implementation). Never for investigations, analyses, reports, specs, ops tooling or non-P0 reworks; those use `luna` or `grok`/`gemini-flash` (Rajiv DM D0AMF0XE6TS thread 1794361418.525529, 07:14/07:16 IST 2026-11-11: "sol should only be used for P0s and other urgent work. do this with luna"; "update the proxy agent skill and add this rule"). Honored unchanged; not rewritten by `auto`. | 1.05M / 128k |
| `gemini-pro` (`gemini-4-argon-high`) / `gemini-flash` (`gemini-3.8-flash-high`) / `grok` (`grok-4.7`) | Subscription models `auto` spends after Antigravity Claude (`gemini-pro` → `gemini-flash` → `grok`): P1 rework, P2/P3, docs/body rework, CI classification/repair, investigations, triage, cleanup, heartbeat, issue filing. Passing an alias explicitly skips the quota selector. Nested subagents inside an explicit launch stay haiku→gemini-3.8-flash-high, sonnet/opus→grok-4.7 (Rajiv ts 1794144618.272919). | pro/flash 1.05M / 64k; grok 500k / 32k |
| `terra` / `luna` | Explicit GPT aliases, and `auto`'s fallback when the Antigravity, Gemini and Grok subscriptions are unusable. `luna` is gpt-6-luna; nested sonnet/opus falls through to `gpt-6.1-terra`. | 1.05M / 128k |

12b does not run as a proxy. S147 (ts 1794919509.795299) restored the S112 in-process sonnet pin for every due review. The `fable → sol` line is dead.

## Steering
Each run is a named peer session (`--name`). Find it with `ListAgents` under "Peer sessions" and send it messages with native `SendMessage` (`to: "<PROXY_AGENT_NAME>"`). The message is delivered at the agent's next tool round and treated as authoritative within the task's scope. Use this for CTO rulings or corrections that arrive mid-run. Launch a follow-up run only after the original has exited. Verified 2026-11-08: a probe was received verbatim.

## Safety (do not weaken)
- `--setting-sources user,project` keeps every user-level guard: block-all-pr-merge, block-secret-dump, block-convex-prod-destructive, the no-host-tsc/vitest rule, the issue-contract audits and the CI-rerun gate. It also keeps the deny rules for `git reset --hard`, `git restore`, `gh pr merge` and `git push --force`.
- A generated settings file re-adds the project safety hooks: the CTO-ladder gate, the issue validators, modal-secret, raw-tmux and block-force-push-rebase.
- Project settings/rules load from the proxy checkout; local settings (including MoP relays) are excluded. `PROXY_AGENT_RUN=1` suppresses reporting/state-writing hooks, not safety guards. UserPromptSubmit scripts are unchanged.
- Never go back to `disableAllHooks`. Verified 2026-11-08: a merge was blocked and a force-push was denied.

## Moving open PRs (the conveyor)

S148: the Who column is the agent type. Launch it with `Skill(launch-agent)` (in-process until `PM_SUBAGENT_CAP`, this skill only when that cap is full). A "proxy agent" cell is the overflow owner, not a ban on an in-process run.
| PR state | Next action | Who |
|---|---|---|
| rework (12b / Codex / CTO block) | `hd-builder` / `PR:` / `PLAN:` | proxy agent; escalate with `--rung N` |
| CI red | `ci-repair-agent` (classify, then rerun a flake or fix test/config) | proxy agent, `auto` (haiku) |
| conflicts with main | `hd-builder`, conflict-only brief | proxy agent; escalate with `--rung N` |
| plan needed | `hd-planner` (plan-agent incl. `## Proof matrix` + QA spec design, Codex plan companion; STOP + HANDOFF) | proxy agent, planner TOP |
| plan reviewed | `hd-builder` (code + matrix tests + QA spec, Codex code companion, push, DRAFT PR; STOP + HANDOFF) | proxy agent, rung 1 by default |
| code-reviewed head | `hd-proof-qa` (matrix RED→GREEN, affected tests, typecheck, QA spec + video blank check, QA report, Codex QA, 12b self-check, PR body refresh) | proxy agent, proof-QA rung 1 |
| proof needs design/scope change | `hd-builder` (plan delta + plan review if scope changes, code review, push, STOP) then `hd-proof-qa` again | proxy agent, rung 1 by default |
| 12b | `pm-admission-reviewer` | in-process Agent, `sonnet` |
| admission | `~/.claude/scripts/ci/pm-admit-ci.sh <PR>` | PM, Bash background |
| dual green | head-pinned CTO merge ask | PM |
| merged | `cleanup-pr-runner` (served-revision readback, tracker) | proxy agent, `auto` (haiku) |

PM gates are unchanged:
- **Three-agent conveyor** (Rajiv D0AMF0XE6TS thread 1794624936.567599, ts 1794633707.502709; supersedes the straight-through dev chain): PM launches the planner (`hd-planner`, TOP: plan-agent → codex-plan-reviewer, which checks the Proof matrix; STOP) → on handback the implementer (`hd-builder`, rung 1: implementation → codex-code-reviewer → push; STOP, no self-QA) → on handback `hd-proof-qa` (rung 1: remote RED→GREEN per matrix row, affected tests, typecheck, QA spec, codex-qa-reviewer, 12b self-check) → PM 12b. hd-proof-qa owns test/spec rework and small named product fixes; design/scope changes return to PM for an `hd-builder` rework relaunch. Run companion scripts as subagents inside the proxy, or directly via `node ~/.claude/skills/codex-review-companion/codex-review-companion.mjs` when Claude subagents are 429-cooled; never via codex-server MCP. REWORK reuses an already-reviewed committed plan when scope is unchanged (no repeat plan review), otherwise reviews the plan delta. The brief's PLAN: path, or the PR body's linked committed plan, is authoritative even for parent/slice issue filenames.
- Follow the companion definitions' cap and exact-head marker handling; missing companions, timeouts, invalid markers and exhausted caps return `REVIEW_UNAVAILABLE stage=<stage> reason=<reason> head=<sha> marker=<path|missing>` with receipts and continue remaining work; unavailability alone is not a defect or a run-stop condition. Concrete unresolved defects still block readiness; never silently skip, exceed caps or claim PASS. PM inline review/12b decides unavailable-review disposition. This supersedes S102's no-Codex exception for proxies (Rajiv ts 1794115123.893259).
- plan before code (S102)
- PM inline plan and code review
- 12b before admission
- main-safety proof for hotfixes
- the CTO merges

In-flight in-process agents finish where they started; don't restart work to migrate it.

## S143: Proxy concurrency caps
- CI repairs and PM admin agents are exempt; total and Claude caps apply only to long-running hd-planner, hd-builder, hd-proof-qa, pm-hotfix, pm-proof-qa, hd-investigator and superproofer (Rajiv, DM D0AMF0XE6TS thread 1794732969.387329, ts 1794737134.234539).

## Limits
- There is no agent panel. The transcript is the `--out` and `.log` files plus the session JSONL.
- Claude Code still logs `[claude-code:unrecognized_model]` for gpt/grok/gemini ids. This is cosmetic, because context and output sizes are set via env.
- Each run starts with fresh context. Briefs must name files, heads, rulings and prior receipts.
