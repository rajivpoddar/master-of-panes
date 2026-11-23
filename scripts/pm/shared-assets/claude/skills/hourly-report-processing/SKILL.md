---
name: hourly-report-processing
description: PM processing of the posted "Hourly — HH:MM IST" heartbeat report in #heydonna-dev. Investigates every error class in it, drives every open PR to its next transition, and starts work for idle capacity (hd lanes via launch-agent), all in the same PM turn. Use whenever a HeyDonna Alerts hourly post (or Rajiv's question on it) arrives.
---

# Hourly Report Processing

Source: Rajiv, DM D0AMF0XE6TS thread 1791206320.393239, 2026-10-05 18:48 IST:
*"create skill for hourly report processing that investigates any errors, checks the open pr's and handles idle slot. add it to the SOP/buddhi"*

`hourly-heartbeat` PRODUCES the report. This skill CONSUMES it. The report is a work queue, not an FYI. Every line ends in an action, an owner, or an explicit "no action: <reason>". Every Pending PM actions line gets an owner or is done in the same PM turn (Rajiv C0ALZJHGE49 thread 1793769935.961309, ts 1793770291.164719).

## What belongs in obligations (Rajiv 2026-11-05)

Source: C0ALZJHGE49/thread 1793872014.789359/event 1793882883.242249:
"can it be skipped for routine stuff like issues and ready pools. we need it for things we want to track."

Use obligations for an explicit "track/follow up" request, an actual agent
commitment, customer recovery, financial uncertainty, safety/UNKNOWN custody,
and promised operational or served acceptance. Keep the source, owner, action
and actual completion/unblock evidence. An issue or PR reference is allowed;
it neither creates a commitment nor makes an existing one disposable.

Routine issue lifecycle, dependency/Ready Pool/retained-triage membership and
PR status stay in GitHub, the existing selector, reports and conveyor. Do not
create `ready_pool`, `triage_retained_scope`, `orphaned_issue`,
`hourly_pr_owner` or admission/status mirrors merely because a row appears.
A future feature/backlog item is not automatically a promised follow-up.
Act on the direct report without creating a second queue. Keep actual CI
failures, live ownership, merge holds and pending handoff receipts visible.
Use the existing `pm-ops.py obligation-upsert` for a deliberate commitment;
do not ban a kind or silently drop an intentional issue-linked upsert.

Retire a redundant record only by exact-ID guarded resolution after checking
its current claim, fingerprint and full remaining scope. Preserve any unique
commitment first. Record "routine tracking retained in GitHub/PM", never
"feature completed". Do not force claims, erase UNKNOWN or rewrite history.

## [HIGH] Hourly processing checklist (Rajiv 2026-11-14)
Source: Rajiv, C0ALZJHGE49 thread 1794671177.730179, ts 1794673308.379679: *"add a checklist to the hourly report processing skill"*. Evidence contract: CTO ts 1794673225.865779 (`hourly_report_due` resolves on verified delivery of the hourly parent; the PM thread reply is the processing evidence).

The PM thread reply does NOT @-mention the CTO by default (Rajiv D0AMF0XE6TS thread 1794878559.300109: "why is the cto being at mentioned for almost every reply now? how do we reduce the chatter? cto need not be pulled in for routine stuff."; supersedes the 2026-11-14 every-reply mention rule, ts 1794675774.355359, and the retired +5m CTO checklist audit). Mention `<@U0BNFGX2UAX>` ONLY for: a head-pinned merge ask; a genuine decision ask that passed Skill(pm-escalation-ladder) (ladder row cited); a CTO-owned review/approval request (S109, closure review, release exception); or a safety/data-loss/security escalation. Status, owner lines, cadence replies, ACK corrections and FYIs carry no CTO mention. It carries this checklist, one line per item, each marked DONE / NONE / PENDING (blocker, owner, next checkpoint) / UNAVAILABLE (reason). Separate *instruction delivered* from *picked up* from *completed*. Amend the same thread as receipts arrive; the audit may stay open after the reporting debt resolves.

- [ ] 0. **Account pace line FIRST** (Rajiv D0AMF0XE6TS thread 1794876281.724039, ts 1794877877.202879: "every hourly report first state if we are above or below this rate and ETA for these two accounts"). Line 1 of the reply (and of the composer post once MoP ships it): the ACTIVE PM keychain account (dev@scribie Ultra through Sat 2026-11-21 21:30 IST for this window) vs its dated ceiling; and Codex: the active spend-down account (rajiv@scribie first at xhigh, then rpoddar at high; spend by the Nov 24 term end) vs its current target, never the former Fri-noon reset horizon. Compute the rate as a ROLLING average since a same-account baseline sample (never a cross-account baseline): "% used" is whole-percent, so 1h deltas read 0 or 1 and flip the verdict (snapshot rate field is also unreliable). ceiling = (100 − used%) / hours-to-horizon; state ABOVE/BELOW on the rolling rate, the ETA to 100% at that rate, and the sample coverage (hours of data). No fixed %/h and no guessed account switch.
- [ ] 0a. **Rajiv outcome threads restate his original ask** (Rajiv C0ALZJHGE49 thread 1794296562.075779 ts 1795144656.382849: "fuck you for wasting 7d on this"; regret ledger 2026-11-20 ci-split-drift). Each status on a Rajiv-outcome thread quotes his original ask (ts) and says whether the work in flight IS that ask. A design that diverges from his ask (e.g. reviewer-driven gates turning a simple split into a different mechanism) gets his explicit OK before more cycles; otherwise go back to his design.
- [ ] 0b. **Rajiv outcome threads list every blocker** (Rajiv shame, C0ALZJHGE49 thread 1794296562.075779, ts 1794878439.941969; regret ledger 2026-11-17). Every cadence reply on a Rajiv outcome thread (e.g. CI cost) enumerates EVERY open blocker issue for the outcome (actual savings, not the PR in motion) with its owner. An unowned blocker gets an accountable owner the same turn regardless of P-label or S143 pacing. Owning is not automatic launch or budget authority: existing holds, caps and burn modes still govern any launch (CTO ts 1794878694.187869). Any feature/flag status claim quotes the live control read in that turn (`gh variable get`, workflow YAML, deployed config), never a PR title or memory.
- [ ] 1. Hourly parent delivered (ts) and per-PR status posted (ts cited).
- [ ] 2. Step 1 errors: every Axiom error class has a cause, an owner or "no action: <reason>".
- [ ] 3. Step 2 / 2d: every open PR has its next transition (merge ask, admit, 12b, rework owner, hold with row id).
- [ ] 3a. **Review-cap sweep, EVERY hourly** (Rajiv C0ALZJHGE49 thread 1795398411.851649, ts 1795399779.512919: "does the hourly processing skill check for the pr's stuck on cap? why did it require my merge to get these moving?"). For every open PR with a live head, run the launcher's cap check for each stage (plan/code/qa/12b) without launching (`python3 ~/.claude/scripts/proxy-review-cap.py check <stage> <PR>` or a dry launch), and list every PR at REVIEW_CAP_2. Same hour, per capped PR: if every open finding is "other" (proof supplied, no data-loss/security/behavior class), record the S153 disposition and move it to the next stage. Otherwise send the S159 ladder → CTO packet. Don't wait for a lane to hit the cap at launch. The reply lists each capped PR, its stage and the disposition sent.
- [ ] 3b. **Tooling-blocked dual greens** (same source): any PR that is green on its substantive runs but refused only by merge tooling (helper refusal, publisher/verifier shape) gets, the same hour, (a) a PM proxy repair lane for the tool (PM owns tooling repairs, not the CTO) and (b) the S157 admin ask when it's out-of-diff/tooling-only and not save-path. Don't wait for Rajiv to ask.
- [ ] 4. Dual greens: head-pinned CTO merge ask sent, with both run ids (Step 2; hourly-per-pr-processing).
- [ ] 4a. **Admin-merge asks for flake/infra CI reds** (Rajiv C0ALZJHGE49 thread 1795239875.958269, ts 1795243262.398409: "update the hourly alert processing and one item in the checklist about admin merge asks for ci's failing on flakes, infra, etc."). For every open PR whose exact-head E2E is green (or classifier-exempt) and whose CI is red only on infra (runner/network/OOM/cancel or watchdog collateral) or on flaky/intermittent tests, inside OR outside the PR diff (Rajiv ts 1795246513.230679: "4a in diff too"): send the head-pinned CTO admin-merge ask THIS hour (S157; S167 enforce_admins off → merge pinned head → on → readback), citing run ids and each failing test with why it is flake/infra/out-of-diff. No rerun loop, wait for main or skip PR first. For an in-diff flaky test, the ask cites one open follow-up issue for that test, so it stays tracked (this replaces the S155 in-PR skip for this case). A PR-caused or deterministic in-diff red, or any E2E red, goes to repair instead. The reply lists each ask sent, or "none eligible".
- [ ] 5. Step 2b-2c / 2f: WIP cap, stale/stuck PRs and drafts dispositioned.
- [ ] 6. Step 2e: Ready Pool refilled (P0/P1 first) or the reason it was not.
- [ ] 7. Step 3 / 3b / 3c-3: for EVERY live proxy running > 60 min, PM reads its own log/out file (not the badge), classifies it progressing / waiting / stalled / over bound, and sends each one a tailored SendMessage steer naming its next deliverable and checkpoint. The reply lists name, issue/PR, model, total/stage elapsed, last substantive progress with its time, blocker, the steer sent, and the next checkpoint. Every S131 bound is checked. A run that is *stalled* or *looping* (no new substantive evidence for more than 60 min and no named external wait, or repeating the same failing action) is promoted at the FIRST hourly check that finds it, never after waiting for the 3h bound (S144, Rajiv, C0ALZJHGE49 thread 1794671177.730179, ts 1794673980.861719: "proxies can get stuck in a loops sometimes. we shouldn't be waiting for 3h before promoting. it should happen after > 1h"). Runs *over bound* (S131 bound is 1h for every run, progressing or not; Rajiv ts 1795278253.434699) are promoted too. Promotion happens the same turn: push or preserve its WIP, stop and fence the old run, then relaunch ONE successor one tier up at the pushed head (next eligible configured rung via launch-agent; at the top rung, one CTO decision packet). The successor gets the original total and stage history, its proof and the next deliverable. No age-only kills, no relaunch loops, no clock reset. **Objective stall test (Rajiv C0ALZJHGE49 thread 1795430704.724349, ts 1795431490.454569; 23 Nov: three Ornith lanes sat 4–6h "alive" while streaming thinking tokens, one claimed a build it never wrote, one never pushed its finished commits):** log freshness, thinking/heartbeat lines and a lane's own progress claims are NOT progress. For each lane >60 min, check the remote branch/PR for a new pushed commit, PR comment or posted receipt in the last 60 min (git ls-remote / gh). None → it is stalled: promote it this hour (preserve WIP: push its clean worktree commits if unpushed, then stop and hand off one tier up), don't send another steer.
- [ ] 8. Every ACTIONS / Pending PM actions item has an owner or is done this turn.
- [ ] 9. Step 3a / 2g: remote boxes healthy, or broken ones deleted and recreated (S141).
- [ ] 10. Step 3x / 3c-2: dead-window cleanup result (DEAD_WINDOWS_CLOSED=N) plus per-window custody for any non-proxy scratch window closed.
- [ ] 11. Step 3d: **re-pace and apply the caps EVERY processing cycle** (Rajiv C0ALZJHGE49 thread 1794933979.934539, ts 1794934326.354799: "the pm subagent cap, proxy claude cap, proxy codex cap, proxy opencode cap, proxy grok cap has to be updated at each processing cycle and reported back on this thread"). Run `python3 ~/.claude/scripts/cost/proxy-burn-dial.py --apply` (S143 CTO pace formula: (100 − used%) / hours-to-horizon; caps are independent numbers), then post in the hourly thread one line: PM_SUBAGENT_CAP, CLAUDE_PROXY_CAP, CODEX_PROXY_CAP, OPENCODE_PROXY_CAP (value + source), GROK_PROXY_CAP, total, each with its change vs the previous cycle, plus live/total and any overrides used. A failed dial run is reported as such (caps unchanged), never silently skipped. **OpenCode is NOT dialed** (Rajiv C0ALZJHGE49 thread 1795416243.378619, ts 1795431121.581209: "remove opencode cap dial up/down from hourly report processing"): OPENCODE_PROXY_CAP stays fixed at 2 (floor = ceiling = 2 in proxy-burn-dial.py; Rajiv ts 1795431240.784339 "cap 2"); report it as "OPENCODE 2 (fixed)" and never raise or lower it in processing.
- [ ] 11a. Step 3d-bis: **Claude team account auto-switch EVERY cycle, before the dial** (Rajiv D0AMF0XE6TS ts 1795191736.936309). Run `python3 ~/.claude/scripts/cliproxy-claude-switch.py --auto` (`Skill(cliproxy-claude-switch)`). It switches only when the current pin has an unexpired exhaustion mark or measured weekly/session use is ≥99.5%. The `--on-429` path marks the pinned account through its usage reset. Both modes pick the highest measured headroom, excluding reserves (including `donna@heydonna.law`), active exhaustion marks and fully used accounts; there is no 95% cutoff. If another switch is in progress or the pin changed in the last five minutes, leave it alone. Report `KEEP`/`SWITCHED`/`NO_ACCOUNT_WITH_HEADROOM`/`UNAVAILABLE` with its reason in the hourly reply.
- [ ] 12. Step 3b training progress and Step 3d social digest receipts, when present.
- [ ] 13. Issue lifecycle dispositions and unblockables (Step 3d), with each issue's next action.
- [ ] 14. Obligations: actionable rows classified (no bulk re-dating); new commitments recorded.
- [ ] 15. RETIRED: no +5m CTO checklist-review reminder and no CTO checklist audit (Rajiv C0ALZJHGE49 thread 1794836804.990559, ts 1794837337.978459: "stop the cto review of checklist items"; disabled in pm-hourly-post.sh). Ordinary operational alerts are unchanged.

A reply missing any line is an incomplete processing record: add the missing lines in the same thread the same turn.

## Trigger
- A HeyDonna Alerts message starting `*Hourly — HH:MM IST*` in C0ALZJHGE49 (the thread ts is the report's ts).
- Rajiv asking about that report ("are PRs moving?", "any limbo?", "why is S2 idle?").
- The receipt-bound MoP handoff `cadence-hourly-processing-<parent-ts>` invokes this same skill after the confirmed parent post. It is a processing request, not a completion receipt. Reconcile the thread and existing Step 3 inspection owner first: reuse in-flight/completed inspection and already-issued directions for this parent. A duplicate Alerts message or heartbeat return never authorizes a second helper or a repeated slot send. A missing/UNKNOWN handoff is visible reconciliation work, not permission to force delivery.

## Inputs (live, never the report's prose alone)
- Report text: the error classes, open PRs and Slots block.
- `gh pr list --state open --json number,headRefOid,isDraft,labels,updatedAt`
- `bash ~/.claude/scripts/heartbeat-slots-block.sh` (live MoP slot state)
- Open pm-ops commitments: `pm-ops.db` `obligations where status='open'`. Preserve intentional holds/asks regardless of their kind; routine PR actions come from the live PR report/conveyor.

## Visual waiver sign-off (S109)
Source: Rajiv DM D0AMF0XE6TS thread 1793721920.626689, ts 1793723647.696169, "go". Every hourly, read all open `visual_waiver_signoff` obligations in the canonical pm-ops ledger, including merged PRs. Check the PR transition thread, merge-alert thread and PR comments for the named verifier's explicit production sign-off; record its permalink and resolve the exact obligation with `pm-ops.py obligation-resolve --kind visual_waiver_signoff --id <id> --reason '<sign-off permalink>'`. Otherwise, once `next_review_at` (deploy + 24h) is due, send ONE reminder total: use the resolved PR transition thread, otherwise the verified PR merge-alert thread in `C0ALZJHGE49`, mentioning the verifier; if neither exists, post a PR comment with no @-mentions and log the fallback. Never invent a thread timestamp. [HIGH] DM the verifier, don't wait (Rajiv C0ALZJHGE49 thread 1793872014.789359, ts 1793888537.826699: "note that i was never dm'ed about these. why?"): the moment a `visual_waiver_signoff` row's served revision is proven (the squash is an ancestor of the served release) and the verifier is Rajiv, DM him in D0AMF0XE6TS the SAME turn with the exact prod check steps (where to click, what to expect) and ask for "ok <PR>" (S9). The 24h thread reminder is a fallback, never the first ask. Check both available threads, PR comments and ledger for `visual_waiver_signoff:<id>` before sending; include that marker and record the successful Slack or comment permalink as evidence via the same upsert. Never resend an existing or uncertain send; reconcile first. If verifier identity, ledger, required reads or send result is unavailable or uncertain, report an owned retryable blocker; never assume sign-off.

## Daily feedback and P0 age (Rajiv D0AMF0XE6TS thread 1793729847.114659, ts 1793730455.699259)
On the first hourly after 00:00 IST each calendar day, read `docs/pm/regret-ledger.md` for the regret review (do not run regret-log.py or recreate its JSONL ledger) and run `python3 ~/.claude/scripts/outcome-vs-spend.py --date <previous-IST-date>`; include both outputs in that hourly thread. Check the prior hourly thread for that IST date before posting to avoid duplicates; if the check or command fails, report UNAVAILABLE with an owner and retry next hourly, not zero. `STOP_AND_REPLAN` means pause routine spawning and replan. The outcome metric is a proxy, not verified deployment evidence.

Every hourly list all open issues and PRs, including drafts, via paginated GitHub queries (for example `gh issue list -R heydonna-app/heydonna-app --state open --limit 1000 --json number,createdAt,assignees,labels` and `gh pr list -R heydonna-app/heydonna-app --state open --limit 1000 --json number,createdAt,assignees,labels`), then filter P0 labels locally. Do not use `--label`: GitHub search returned zero despite live matching records on 2026-11-04. Check label variants in the repository too. For each older than 4h, print `P0_AGED <n> <age> <owner>` in the hourly thread and assign exactly one accountable owner in the same turn, using the existing MoP or pm-ops obligation path; preserve any live owner and record the next action. Verify open state and linked PR merge status live. Failed or truncated reads are UNAVAILABLE, never an empty result.

Routine CI-red classification and thread replies are inline or ONE batched agent per hour, not one per alert; at ~12 concurrent PM background agents queue additional launches, while PM continues handling urgent input and same-turn ownership inline (S110).

### Routine obligation batch (CTO REVISE #3, C0ALZJHGE49 thread 1795353473.878109, ts 1795355302.148809)
CTO: *"Feed the existing hourly lane the private routine-batch.json from the same bucket claim. The hourly report can show counts. Record the report post and the lane's acceptance as distinct outcomes, using the existing lane receipt, and keep one trigger."*
- When the `cadence-hourly-processing-<parent-ts>` handoff text carries a `ROUTINE BATCH` clause, it names a private `routine-batch.json` in that bucket's claim dir (`~/.claude/hourly-heartbeat-posts/<YYYY-MM-DD:HH>/`). The ONE batched agent above works exactly the rows in that file (rows, then ids, then deferred rows from `pm-ops.py obligation-list --actionable --format json`) and each row's outcome is recorded in pm-ops. It is the same handoff as this skill's trigger: no second trigger, and never hand the same bucket's batch off again.
- Never copy owners, titles or required actions into Slack: the hourly report shows counts only. FAST rows stay on the Stop drain; the Stop hook never lists routine rows and an hourly-only set never blocks Stop.
- Two separate outcomes: the REPORT POST (the parent receipt; `routine-batch.posted` in the claim) and the LANE ACCEPTANCE (MoP's receipt on the parent's `processing` record, which also names the batch it accepted). A posted report never proves the lane accepted the batch; if acceptance is missing or UNKNOWN, reconcile it as visible work and do not re-send.

## Step 1: Errors (Axiom section)
For each error class listed:
1. **Mapped to an open issue/PR AND count at or below the 7-day same-slot median:** no action. Record "tracked by #N".
2. **Unmapped, OR above median, OR a new class:** launch via Skill(launch-agent) (it picks transport from env PM_SUBAGENT_CAP; 12b and hd-proof-qa always launch via proxy-agent.sh; due 12b alone is burn-cap exempt (S147; CTO ts 1795074821.126009)): launch ONE `customer-artifact-investigator` (read-only, max 1 per report, Claude usage is high). Give it the class, users, the 1h window and the report thread. It posts its verdict (root cause, evidence line, next action, owner) in the report thread.
3. **P0 signals (save lockout, data loss, export failure for a customer):** fast lane per SOP Priority §2. Start the read-only investigator first. For a machine lockout alert keep any holding note internal, set one alert-thread recheck at +5 min and send no user/Abi message in that interval unless Rajiv or the user asks (lockout-processing); a direct customer request still gets an immediate safe reply.
Never diagnose from the count alone.

## Step 2: Open PRs
**[HIGH] The "*Hourly per-PR status*" alert is processed with `Skill(hourly-per-pr-processing)` the same turn it arrives** (Rajiv 2026-11-08, C0ALZJHGE49 thread 1794120614.633259, ts 1794121118.991139). That skill owns the per-PR dispositions below, using proxy agents for reworks; this section stays as the reference table.
**Trend line (mandatory, Rajiv 2026-11-03 C0ALZJHGE49 ts 1793692399.637039: "trace the open pr count each in each hourly report"):** run `python3 ~/.claude/scripts/pm/open-pr-trend.py` and put its single output line right after the account pace line (checklist item 0) in the hourly reply. It shows open non-draft vs the last hourly, opened/merged in the last 60 min and today, and the last-6 trend vs the 25 cap. If the count rose, name in one line which PRs were opened and why (P0 only while the freeze holds).
**Then:** run `python3 ~/.claude/scripts/pm/dual-green-sweep.py` and list its new asks, follow-ups, and any errors in the hourly reply. The sweep sends exact-head asks and follows up in the original ask thread after 30 minutes; skipped E2E is manual only. Do not duplicate its asks or follow-ups. Rajiv, C0ALZJHGE49 thread 1793681148.228999, ts 1793683532.171189 and ts 1793683640.762909; no launchd schedule.

Each open PR gets exactly one disposition:

| State | Action (same turn) |
|---|---|
| Not draft, no `ci-head:<live head>`, not already genuinely dual green | `pm-admission-reviewer` (12b) at the live product head via `proxy-agent.sh` (never in-process). On PASS, run `pm-admit-ci.sh <PR>` yourself; reviewers cannot change PR state. An already dual-green PR without a 12b gets the missing receipt reported in the CTO ask, not a retrospective 12b (S126). |
| CI/E2E red, unowned | CI red → `ci-repair-agent`. E2E red → `e2e-failure-investigator`. Retain the accepted owner in the existing incident/PR path; claim an existing explicit obligation without creating a routine status mirror. |
| Dual green at the live head | Exact-head merge ask to CTO in the PR's CI-success alert thread, with the 12b.1 screenshot line. |
| CI red only on infra or flaky tests (in- or out-of-diff), E2E green (or exempt) at the live head | Head-pinned CTO admin-merge ask the same hour (checklist 4a; S157/S167) with run ids and the per-test flake/infra/out-of-diff reason. PR-caused or deterministic in-diff reds go to `ci-repair-agent`. |
| Green, held for screenshots/proof | If not yet genuinely dual green: confirm a live lane owns the required proof, else launch it (`hd-proof-qa` via proxy-agent.sh). If genuinely dual green: do not restart proof or add a PM hold; list optional/late proof in the immediate CTO merge ask (S126). |
| Queued run older than 30 min | Check job state before calling it a stall (`gh run view --json jobs`). Follow-on jobs waiting on the single ops runner are a backlog, not a stall. A real stall goes to `ci-repair-agent` (SOP §15). |
| Draft or rework with no owner and no update for 3h or more | Limbo. Rework with no Convex/Modal need → PM bg agent in a disposable worktree. Otherwise launch an `hd-builder` rework via `Skill(launch-agent)` (proof on isolated remote boxes). |
| `pm-blocked:*` | Confirm that the wait and its wake condition are recorded. |

Read the live head first; the report's head may be stale.

**Pending merges get a CTO follow-up** (Rajiv 2026-10-15, C0ALZJHGE49 thread 1792025128.705119, ts 1792026682.252209: "test skip pr's don't need my approval. follow up with cto regarding pending merges."). Every hourly pass lists each merge ask (and each admin-merge ask) that is still unmerged with age, exact head, and the original ask ts, and sends CTO ONE batched @-mention follow-up in the report thread (one line per PR). Main-red test-skip PRs never wait on Rajiv: send them straight to CTO as admin-merge asks (SOP 23 "Skip PRs merge via admin bypass").
CTO refinement (C0ALZJHGE49 ts 1792026838.395109): the follow-up reconciles the EXISTING PR Merges assignment (status/blocker), never a new merge attempt, duplicate ask or paid CI. The standing admin waiver covers proven main-red test-only skips only, not repairs, un-skips or product changes; the exact-head E2E-green/CI-only-out-of-diff S157 route is separate. Every otherwise-authorized admin merge uses the S167 sequence (CTO turns enforce_admins off, merges only the pinned head, restores it even if the merge fails, reads back protection; no separate approval, no new merge eligibility). Lockouts stay on normal admission.

### Step 2b: PR flow health (WIP cap)
Source: Rajiv C0ALZJHGE49 thread `1791722798.068729`, ts `1791723384.313629`: *"update the hourly report processing skill with this. do not let the pr count climb so high. we need to keep a healthy rate between merges and new pr creation."* Trigger: 2026-10-11, 22 open non-draft PRs. Main CI had had no green run since ~09:30Z: each run was cancelled by the next merge, or failed on the inherited sync-7808 test. So nothing reached dual-green, while 8 slots kept opening PRs.

Every hourly report computes and posts these three numbers:
- `open` = open non-draft PRs;
- `merged_6h` = merges in the last 6h;
- `opened_6h` = new PRs in the last 6h.

It also records the main CI state: the latest COMPLETED `ci.yml` run on main (skip cancelled runs).

- **Main not green (latest completed main run is red, or no green run in 2h):** handle in parallel; a main red never halts unrelated admissions (S118). Verify causality from the exact logs; the same test name does not prove inheritance. A proven main flake: rerun immediately. A deterministic main test red: ONE untested skip PR plus follow-up issue (`main-red-skip`), merged immediately via the authorized CTO admin route. Actual corruption/data loss → launch via Skill(launch-agent) (it picks transport from env PM_SUBAGENT_CAP; 12b and hd-proof-qa always launch via proxy-agent.sh; due 12b alone is burn-cap exempt (S147; CTO ts 1795074821.126009)): an `hd-planner` plan when the fix is not obvious, then an `hd-builder` (`proxy-agent.sh hd-builder <brief>`; escalate with `--rung 2|3` on evidence; remote RED→GREEN, then an admin-merge ask to CTO). CI (non-E2E) reds use a `ci-repair-agent`. Apply S157/S166 to affected PRs; do not blanket-rerun every PR that shares the failing test.
- **Queue-spike drain: `open` > 25 (Rajiv 2026-10-15 19:45 IST, C0ALZJHGE49 thread 1792072892.236499, ts 1792073702.350709: "update the hourly report processing skill to handle the open pr queue spikes. whenever it crosses 25, we have to stop new issue assigments and let the queue drain."):** stop ALL `new_issue` assignments the same turn. Freed slots get only rework, proof or red-fix work on existing open PRs, oldest admitted first. While draining:
  - Re-admit only as S157/S166 require; a main red never blocks admission of other PRs (S118).
  - Chase CTO merge asks for dual-green PRs.
  - Post `open`/`merged_6h`/`opened_6h` plus a drain ETA in the hourly reply.
  Resume eligible new work automatically at 25 or fewer open (S76: no hysteresis, no Rajiv approval). Issues may still be FILED while over 25, but stay unassigned.
- **WIP cap:** the only PR-count cap is 25 open non-drafts (S76); the former 20 alarm/resume threshold is retired.
- **Rate check: `opened_6h` > `merged_6h` + 3:** treat it as an unhealthy pipeline even under the cap. Name the bottleneck stage (main red, CI queue, review, merge ask, rework) and act on it in the same turn: launch an agent, or send a merge ask or a re-admission.
- **Queue triage, each hour while over the cap:** close superseded PRs and park PRs waiting on a product decision (`pm-blocked:product`, with the decision ask sent). A draft older than 24h with no owner gets closed, keeping its branch.

### Step 2c: Stale and stuck PRs (Rajiv 2026-10-22, C0ALZJHGE49 thread 1792651516.322139, ts 1792652087.924089)
*"update the hourly report processing to check for stale/stuck pr's."* Trigger: Rajiv asked where #9936 (open 7d) and #10322 (open 4d) were stuck; the hourly reports had not flagged either.

Each hourly, list EVERY open PR (drafts included) and flag it when either is true:
- **stale:** no new commit, comment or label change in more than 24h; or
- **stuck:** non-draft and in the same gate for more than 4h (not admitted; admitted but CI not terminal; dual green but no merge ask; merge ask unanswered).

For each flagged PR, the Step 4 reply gives ONE line: PR, age, the exact gate or blocker (a decision with its owner and ts, a dependency, CI, review, merge ask), the owner and the next action. In the same turn, act on the ones PM can move:
- send the missing merge ask or re-admission;
- chase the person who owes a decision;
- close it, keeping the branch, when it's superseded or has been a parked draft for more than 7d with no live blocker.
A parked PR (`pm-blocked:*`) is listed with its wake condition and who owns it, never left silent.
Do not flag a PR whose owner claim is live (slot lane or an obligation row suppressing it).

### Step 2d: Every open PR in the thread reply (Rajiv 2026-10-26, C0ALZJHGE49 thread 1793018827.131949, ts 1793019217.961569)
*"update the hourly report processing skill to check all the open pr's and post a reply to this thread so that i check"*. Trigger: Rajiv had to ask "are the pr's moving? any stuck?" because the hourly reply only covered red and flagged PRs.

Each hourly, check EVERY open PR (drafts included, via `gh pr list --state open --limit 100`) against its live head. The Step 4 thread reply carries ONE line per PR, oldest update first, in this format:
`#PR draft?|ready · <gate> · <owner> · <next action / wake> · <MOVING|STUCK|PARKED>`
- `<gate>` is exactly one of: draft-rework · 12b · CI running · CI red (inherited|PR-owned) · E2E red · capture/replay · visual proof · dual-green/merge ask · pm-blocked:<kind>.
- `<owner>` is a live hd lane (in-process task or named proxy), a named PM bg agent, CTO, or Rajiv. A PR with no live owner is STUCK.
- STUCK = no owner, or the same gate for more than 4h (Step 2c). Each STUCK line gets an action in the same turn (assign, launch an agent, send the merge ask, re-admit, close if superseded), and the line names that action.
- PARKED = `pm-blocked:*` with a recorded wake condition and owner.
Never summarize the PR list as "+N more" in the reply. If the list is long, post it as a second reply in the same thread instead of truncating it. Rajiv checks this thread to see whether PRs are moving.

## Step 3: Slots
Rajiv C0ALZJHGE49/thread `1794375876.799129`, event `1794378820.437879`: "check the inprogress work and send course correction". Inspect and steer EVERY current in-progress lane, even when the report has no slot ACTIONS. The inspection/delivery rules below govern this step: cached age, an owner name or a previous escalation is not current steering evidence. No automatic age-based kill, reset, reassignment, scope reduction or waiver. Preserve customer recovery independently of this hourly work.
- **Proxy lanes: verify, don't trust self-reports** (Rajiv D0AMF0XE6TS thread 1794624936.567599, ts 1794641763.847759: "the hourly check should still happen though. weaker agents might need steering. we shouldn't rely only on the self reports."). Slots are retired; every live px- proxy is a lane. Auto rung escalation (proxy-agent-stop.sh) does NOT replace this check. Per lane, read the primary evidence yourself: the live PR head (`gh pr view`), the last commits/diff, the run's .md/.jsonl tail and the remote-box receipts. Compare against the brief and the plan's ACs, and steer with SendMessage (CONTINUE / CORRECT / escalate). A proxy's TO_PM claim ("tests green", "ready") is a lead, never the evidence. Weak-rung builders (Haiku/Luna) get a specific next step, not "continue".
- **Idle capacity (numbered slots are retired, S137):** run `python3 ~/.claude/scripts/ready_pool_select.py` (add `--rework-only` while over the 25 cap; no `new_issue` then). The order is priority label first (P0 > P1 > P2 > P3), then rework before repro before new_issue, then oldest. Run `--check <issue>` before assigning; exit 3 means PRIORITY_INVERSION, so don't assign. Then `Skill(launch-agent)` starts ONE hd-planner/hd-builder owner under current caps; proof runs on isolated remote environments. A P2-or-lower `new_issue` is allowed only when no ready P0/P1 exists, and its `priority_reason` goes into the assignment receipt (Rajiv C0ALZJHGE49 thread 1793195119.674919, ts 1793202340.562609).
- **PRIORITY_INVERSION** (a P2 owner running while P0 #Y is ready and unowned): in the same turn, start the P0 via `Skill(launch-agent)` (hd-planner/hd-builder); P0/P1 never wait for a retired slot. Never preempt or release a lane that is at dual green or admitted to free capacity. Name the action in the hourly reply.
- **IDLE lane (idle more than 10 min, still owned):** SendMessage the live proxy its last packet and next checkpoint; if it is stalled, follow Step 3's finite handoff.
- **Lane waiting on PM** (review, CI, admission or a decision): answer it. A PR wait never holds a lane.
- **No eligible work:** post Rajiv an explicit hold with the reason and an end time.
- **Lane checkout review, ALL live lanes** (Rajiv 2026-10-06 DM `1791270714.623429`, extended C0ALZJHGE49/`1791295060.867539`): use ONE read-only background `general-purpose` inspector for every live hd lane (in-process task or named proxy; numbered slots are retired, S137), keyed to this hourly parent. Reuse an existing inspector; normal agent limits apply, with no short-proxy cap exception. If capacity is unavailable, PM does the bounded reads itself or records the missing lanes with owner/wake; never silently skips them. The inspector returns findings to PM; it sends no slot messages and performs no lifecycle, build, test or Slack effects.
  Per lane, bound reads to its current assignment/issue/PR and full head, branch, last commit, dirty/unpushed state, and recent substantive pane/log/diff/test/review evidence. Record the observation UTC and explicit parent issue -> current child issue mapping; unavailable fields remain UNKNOWN. Use the existing read-only slot/pane/log surfaces; a static badge or quiet pane does not prove owned background work has finished. Judge convergence against the actual accepted AC and runtime/test boundary, not just elapsed time. Re-read the live assignment/epoch before a PM direction; a changed lane invalidates that proposal and receives no stale send.
  **Time on lane** (Rajiv C0ALZJHGE49 thread `1791337506.322739`, ts `1791339078.814959`: "can we also track how long has the slot been on the issue/pr in the report? MoP keep this data, right?"): use the lane's original start from its proxy-run/assignment custody record (Step 3b); a rename, re-pin or push never resets it. If unavailable, report UNKNOWN.
  PM chooses per eligible lane: **CONTINUE** with a concrete next proof/delivery checkpoint, **CORRECT** with an exact bounded instruction, or **NEEDS_PM** for a genuine preserved-WIP handoff through canonical lifecycle. Wrong branch, risky/out-of-scope diff, repeated unchanged failures, stalled push or an overrun triggers inspection and a split check, never an automatic destructive action. Distinguish repro from implementation and identify remaining ACs. The finite 3h handoff below supersedes repeated slot hard stops and close/reimplement guidance for this unresolved work; do not re-date the lane or renew a deadline instead of transferring it. Never waive an AC or relax a test threshold to satisfy an hourly.

### Step 3: finite stuck-work handoff
Rajiv C0ALZJHGE49/thread `1794390464.591959`, event `1794394074.267489` (superseded in part by S144 and S149): at the FIRST hourly check that finds >1h without meaningful progress and no named external wait, preserve/push WIP and transfer ONCE to the next eligible configured rung via launch-agent; the 3h bound applies only to progressing but unresolved work. PM alone performs the existing canonical lifecycle and `Skill(proxy-agent)` operations. The inspector proposes; it never launches, releases or messages a worker.

- At the first hourly check finding **>1h without meaningful progress and no named external wait** (or **1h on progressing but unresolved work**; S131 amended by Rajiv 2026-11-21 ts 1795278253.434699), checkpoint and transfer the remaining work ONCE to the next eligible configured rung (**ONE successor**) via launch-agent. Completed coding awaiting an external CI/review/customer event is **EXTERNAL_WAIT**, retained with the existing conveyor coordinator, not a reason to launch another coder to wait.
- A stalled successor moves up again only if a higher eligible configured rung exists; at the top rung it reaches **one evidence-backed CTO decision** packet at its stage bound or earlier accepted checkpoint. No repeated launch at the same rung, deadline renewal, extra agent or model/review carousel. Existing coding proxies move up when stalled; an already active owner keeps its real stage/history, never restarts at stage zero.
- **[HIGH] Stuck proxy → CTO handoff, same hourly** (Rajiv C0ALZJHGE49 thread 1794401530.641159, ts 1794401909.184269: "update processing skill to handoff stuck agents to cto."). Any proxy classified STALLED at the top configured rung (or past its stage bound without a pushed product commit, a passing proof or a closed review finding), and any proxy on its second stall at any model with no further stage, is handed to the CTO in the SAME hourly turn. Post ONE packet in the PR's/issue's existing thread, explicitly @-mentioning `<@U0BNFGX2UAX>` (run `Skill(pm-escalation-ladder)` first when the hook requires a ladder row). The packet contains: proxy name/model, PR/issue, full head, total and stage elapsed, the exact failing boundary (test/assertion/review finding/env blocker with file:line or run id), what was tried, retained WIP/worktree/leases, and the one decision needed. The proxy keeps custody (no kill and no further relaunch) until the CTO answers, and its row shows `HANDED_TO_CTO <ts>`. A rung carousel, re-dating or silent waiting is a miss.
- **Proxy status is part of every hourly reply** (same source): one row per live or uncollected proxy with model, PR/issue, run time, last-output age, classification and decision. The composer's Proxies section must carry the same fields; UNKNOWN fields in the composer are filled in by PM's reply from `ps` elapsed time + the proxy's output/jsonl mtime + its RESULT line, never left as UNKNOWN when the data exists.
- Retain original lane start/total elapsed, each stage's original owner/model/start/head, prior missed bounds and current checkpoint in the existing handoff brief and hourly thread. A rename, re-pin, new child issue or push does not reset elapsed work on the same remaining scope. If stage history is unavailable, report UNKNOWN; do not grant a fresh 3h allowance.
- Before transfer, PM verifies issue/epoch/PR/full head/worktree, DND, old writer and live child/remote jobs. Preserve and non-force push actual WIP, including safe tracked/untracked implementation assets; private artifacts/secrets stay private with authorized evidence references. A failed push or unaccounted old writer blocks transfer: retain owner and next eligible action without resetting the deadline.
- Use canonical ownership-safe checkpoint/retirement before a successor starts. Retain old result, lease/job IDs and collection custody; consume in-flight results without launching duplicate paid work. No signal-kill of unrelated processes, parallel old/new writers or abandoned running results. Slot release/reassignment follows canonical PM lifecycle only after custody is established; a proposed handoff is not a successful release.
- The brief carries exact PR/head, accepted scope/plan, valid proof/receipts, remaining ACs, old WIP/owner/results/leases, stage history, smallest first task at the real failing boundary and next bound. Every REWORK brief also carries a head-bound proof manifest (retro #13357/#13435; CTO C0ALZJHGE49 thread 1794234970.206389, ts 1794578332.315349): the changed behavior; its owning test/QA case; the PR head and merge-base the proof is bound to; and which receipts (CI, E2E, QA, Codex, 12b) the next push or main merge invalidates versus which stay applicable, with run IDs. The proxy reuses a still-applicable receipt instead of re-running it, runs the affected real-path case early, and returns the manifest updated for the new head. PM launches the successor via `Skill(launch-agent)` (`hd-builder`, MODE: REWORK when an accepted plan exists) at the next eligible rung; use exact model IDs only for an approved pin (gpt-6.2-sol replaces gpt-6.1-sol where Sol is used; gpt-6.1-terra stays in its tiers). Verify the native launch/recipient receipt and resolved model; names or requested aliases alone are not proof. Respect existing proxy capacity/cost limits. Cap, DND, refusal, model unavailability or UNKNOWN is an explicit blocker/owner/wake, never a bypass, rekey or restarted 3h clock. Issued/UNKNOWN effects remain consumed/reconciliation-only.

### Step 3b: hourly proxy work progress
Carry explicit original work/stage start and meaningful-progress UTC, boundary and checkpoint in the existing proxy assignment/handoff custody record (source `proxy-agent`, target type `proxy_assignment`, payload tied to exact `proxy` and `ref`). The report reader uses those retained fields only, never the event's notice/launch timestamp. Missing fields remain UNKNOWN; no new registry or invented history. Retain complete rows in the saved hourly inputs/PM reply when parent compaction abbreviates them.
Use the same hourly inspection/claims and enumerate every relevant proxy, including completed-but-uncollected work; never count only live windows. Per proxy record PR/issue/full head/worktree, resolved model/owner, original total elapsed and current stage elapsed, last meaningful commit/diff/test/review result with UTC, exact blocker, received instruction/receipt and next bound. Preserve explicit parent/child mapping. Do not infer convergence from a live process, changing output, silence or low CPU.

Classify **ACTIVE_PRODUCTIVE**, **EXTERNAL_WAIT**, **STALLED**, or **COMPLETED_UNCOLLECTED** from evidence. Productive work retains its checkpoint within the stage bound; external waits name their event/coordinator and consume the existing result. STALLED follows the finite rung -> CTO rule above. COMPLETED_UNCOLLECTED means collect the existing terminal/result under its custody, not rerun implementation. Missing `RESULT` or a queued remote job alone never authorizes a coder relaunch; use a genuinely read-only result collector only after reconciling the old owner and native receipts. Unknown evidence remains UNKNOWN, not a cleanup target. PM's hourly reply gives each disposition and actual direction/handoff receipt; dead-proxy cleanup remains separately ownership-safe.
  **PM alone communicates** through installed `Skill(message-slot)` (native SendMessage to the verified peer by default; supported background fallback only as that skill permits). No raw pane input, direct slot REST or inspector `--force` send. One actionable direction names the current tuple, observed blocker/progress, requested next result and the existing time bound (or bounded 30-min checkpoint when none exists). A healthy lane still receives a useful continue/checkpoint direction. Respect DND, unknown ownership and real background work; stopping/rescoping must preserve dirty/unpushed WIP and consume running results through the canonical PM path.
  Retain the exact outgoing message and native receipt. Distinguish NOT_ISSUED, ACCEPTED/QUEUED, ACKNOWLEDGED, REFUSED, UNKNOWN and completed result. Sender success alone is not pickup or completion. A prior matching direction for this parent may be reused with its receipt; issued/UNKNOWN is consumed/reconciliation-only, with no blind retry or forced fallback. DND/refusal/unavailable/UNKNOWN gets a visible blocker, next owner and wake, not a silent skip. PM's same-thread reply includes every provisioned lane (or explicitly not provisioned/free): tuple + evidence UTC/progress + decision + communication disposition/receipt + next checkpoint. This reply is the processing record (the +5m CTO checklist verifier is retired, Rajiv ts 1794837337.978459), not a new Stop, merge or customer-recovery gate.

## Step 3a: Remote test/browser boxes (Rajiv 2026-11-02, DM D0AMF0XE6TS thread 1793614728.759459, ts 1793615262.985719)
*"add the remote test/browser boxes status to the hourly report. add the recovery steps to the hourly report processing skill"*. Trigger: test pool showed 0 free / 8 with 7 queued, from 2 broken, 1 expired and 1 mid-preempt box. Slots and PM agents sat waiting on leases (#12040, #12498, #11813, #12542) while the browser pool had 9/12 free.

Each hourly, read the report's `*Remote boxes:*` line. If it is missing, run `~/.claude/scripts/remote-test-box.sh list` yourself. For EACH pool (test cx33, browser cx43), record free / leased / preempting / expired / broken / queued counts, plus the oldest queued request's age.

Recovery, in the same turn, in this order. Delegate the mutating steps to ONE PM bg agent; never run them in the foreground:
1. **Expired leases**: run `remote-test-box.sh reap`. It resets expired leases whose launcher is gone and rebuilds long-broken boxes.
2. **`ORPHAN_REFUSED` / custody-marked (`retry_unresolved`, `transport_unknown`) leases** that reap would not touch:
   - verify no live owner: no local `pgrep -f <lease-alias>`; box-side `ps` shows no test process; the owner agent or slot no longer holds that lease (check ListAgents and MoP `/slots`; a newer lease for the same PR is a different lease);
   - then `remote-test-box.sh custody-resolve <lease> --by pm --reason "<evidence>"`, followed by the fenced rebuild/reset path.
   - Never release a lease whose launcher pid is alive.
3. **Broken boxes**: after custody is resolved, use the fenced single-box rebuild, one box at a time, and verify each returns `free`.
4. **Queue ≥ 4, or the oldest queued request ≥ 30 min, with the other pool mostly free**: name the imbalance in the reply. Pool sizes are `RTB_TPOOL_SIZE` / `RTB_POOL_SIZE` in remote-test-box.sh, and Rajiv approves size changes.
5. **Stopped agents hold leases**: when Step 3c stops an agent, check `list` for leases it owned in the same turn, and release them through step 2.
6. **Waiting work**: tell each slot or agent whose queue attempt timed out (rc75) to re-queue ONE request with `--wait 3600` at its correct priority. Don't escalate a retry loop. p3 is the default for non-P0/P1 proof. A request only preempts strictly lower priority.
0. **[HIGH] S141: no quarantine or forensic holds for test/browser/Mac boxes** (Rajiv C0ALZJHGE49 thread 1794577570.649789, ts 1794579813.202609: "do not overcomplicate this. do not quarantine test/browser boxes or hold them for forensics. these are for dev testing. rebuild and put it back in service asap. update buddhi/skills/memory"). A broken/expired/stuck/refused box is REBUILT IN PLACE from its fixed image under the generation fence the same turn, after verifying no live owner or in-flight provider action: check (a) that no lease owner pid is alive (kill -0 + pstart for pid/reset_pid/rebuild_pid in the lease record) and (b) that no provider action is pending on that exact server (`hcloud server describe <id>` / `hcloud action list`); an in-flight action is reconciled first, never duplicated. Never delete a surviving VM to repair it (Rajiv 2026-11-20 ts 1795186504.333049 / 1795186640.923159); recreate only a provider-confirmed missing server within the existing pool bound, then verify it is healthy and grantable (`list` readback). A refusal (unaccounted units, custody hold, committed-rebuild/sealed state, CTO hold) is not authority to bypass custody: reconcile the exact refusal and any pending effect with the pool owner and use only the fenced in-place rebuild once custody is clear; do not use admin/test/raw hooks or remove provider protections to bypass it. No snapshots required.
7. **[HIGH] Broken boxes go back into service the SAME hourly; never carry a broken box to the next hour** (Rajiv C0ALZJHGE49 thread 1794498426.840189, ts 1794503678.266509: *"rebuild and put it back into service asap"*; ts 1794503873.020719: *"are the broken test/browser pools being checked in the hourllies? they have to be reparied and put back into service asap. we are already paying a lot for these. update your skill."*). Trigger: the browser pool sat at 0/5 for 2h+ on 12 Nov (3 broken, 1 expired, a dead P0 queue ticket at the head), blocking a partner-date QA run.
   - Every hourly, count `broken`/`expired` boxes in BOTH pools. Any count > 0 launch via Skill(launch-agent) (it picks transport from env PM_SUBAGENT_CAP; 12b and hd-proof-qa always launch via proxy-agent.sh; due 12b alone is burn-cap exempt (S147; CTO ts 1795074821.126009)): launches the repair agent (`autoscaler-main-repair` proxy) the same turn. A broken box is paid capacity doing nothing.
    - If the fenced rebuild refuses for any reason (`REBUILD_BUSY`, quarantine, unaccounted units, custody hold, committed/sealed rebuild state), follow item 0 (S141): a refusal is not authority to bypass custody. Reconcile the exact refusal and any pending effect with the pool owner, then use only the fenced in-place rebuild of that SAME registered slot once custody is clear (never admin/test/raw hooks, never remove provider protections, never delete a surviving server); recreate only a provider-confirmed missing server (same type, same count; not a capacity change). Read back healthy/grantable. No forensic snapshot, quarantine or hold is a prerequisite.   - A queue ticket whose waiter AND controller PIDs are both dead is a dead ticket, not demand. Clear it through the reconcile-only path; it must never sit ahead of live work.
   - The reply states per box: before state → action → after state, and the hours each box spent broken.
Reply line: `boxes: test <free>/<target> (<queued> queued, <broken> broken, <expired> expired) · browser <free>/<target> · action: <what was done/owner>`.

## Step 3b: Training progress (Rajiv 2026-10-21, C0ALZJHGE49 thread 1792550718.626739, ts 1792551127.300669)

**RETIRED for Sindhu (Rajiv 2026-11-22, D0AMF0XE6TS thread 1795294265.657749, ts 1795316277.054839: "sindhu run is abandoned. stop tracking it."; C0ALZJHGE49 ts 1795315975.411499: "drop sindhu training from the hourlies").** The hourly no longer checks, resumes or reports the Sindhu run (sindhu-r7-*). Do not launch spark-resume-run for it. This step applies only to a NEW training run Rajiv explicitly starts.
*"update the processing skill to check verify the sindhu run steps as well. if step count remains the same then check the status with superproofer agent an dm me."*

Each hourly, read the report's `*Training:*` line (e.g. `sindhu-r7-n5625 step 1150/5625 ... (as of HH:MM IST)`) and compare its run id and step against the PREVIOUS hourly report's training line in C0ALZJHGE49.
- Step advanced → note `training: <run> step A→B ON_TRACK` in the Step 4 reply.
- Step UNCHANGED for the same run (or the `as of` time is stale by >1h) → launch via Skill(launch-agent) (it picks transport from env PM_SUBAGENT_CAP; 12b and hd-proof-qa always launch via proxy-agent.sh; due 12b alone is burn-cap exempt (S147; CTO ts 1795074821.126009)): launch the `superproofer` agent (MODE: TRAIN status check, read-only: process alive, last log step/loss, CUDA/MemAvailable, errors; no relaunch, no fix). In the SAME turn, DM Rajiv in D0AMF0XE6TS: run id, the stuck step, last-advanced time, the superproofer finding and the PM default (e.g. BUILD fix → TRAIN per S29). Also include it in the Step 4 reply.
- No training line, or "no run active", → CHECK, don't skip. Read the last known run, in this order: the previous hourly training line, pm-ops training obligations, then the superproofer STATE/run parent. If that run never reached its final step (e.g. last seen 2754/5625), treat it as CRASHED. Do these in the SAME turn:
  1. Launch Skill(spark-resume-run): ONE resume attempt from the latest complete checkpoint, under the S29 rule that PM owns Spark failures. Local Spark needs no spend GO.
  2. DM Rajiv: run, last step, crash time, and that the resume was launched.
  3. Note it in the Step 4 reply.
  If the run did finish, record it as `training: <run> COMPLETE` and skip.
  Source: Rajiv, C0ALZJHGE49 thread 1793583921.659259, ts 1793585706.576799: *"why wasn't it auto restarted? does the hourly report processing have that?"* Trigger: the box reboot on 11-01 at 14:27 IST killed r7 at step 2754. The hourly read "no run active" and skipped it for about 17h.

## [HIGH] Dead tmux window cleanup (every hourly)
Source: Rajiv, C0ALZJHGE49 thread 1794653217.364359, ts 1794653496.600909: *"add a dead tmux window cleanup step to the hourly report processing as well. i just manually killed a few right now. it piles up each cycle."*
- Every hourly, PM runs `~/.claude/scripts/tmux-dead-window-cleanup.sh --apply` (dry-run without `--apply`). It kills only (1) windows whose pane tmux reports dead (`pane_dead=1`), in any session, and (2) `px-*` windows with no live proxy-agent/claude/codex/node/python descendant (the proxy exited and left an idle shell).
- It never touches `0:0` (the PM pane) or any window with a live agent process. A live proxy is stopped by killing its processes (see the stop-proxy memory), never by this step.
- PM's own helper windows (`run-*.sh` launched with `tmux new-window`) close on exit. If one is left dead, this step removes it.
- The hourly reply states `DEAD_WINDOWS=<n>` with the killed names.

## Step 3c: Inactive background agents (Rajiv 2026-10-22, DM D0AMF0XE6TS thread 1792637361.212159, ts 1792638231.158209)
Stale agents (2026-10-25, Rajiv C0ALZJHGE49 thread 1792914695.006599): Step 3c also stops every agent the composer lists as `stale` (quiet >3h or completed but uncollected, up to 24h), not only completed/inactive.
*"add a inactive bg agent check the hourly report. udpate hourly report processing to stop inactive agents."* Trigger: the donna-influencer and two blog-post-builder agents from the previous PM session sat idle for 12h+ after their PRs merged (#10727); Rajiv spotted them in the PM pane footer.

Each hourly, PM (not the bg agent: only PM's session can stop its tasks) checks every background agent visible to this session:
Rajiv 2026-10-23 (C0ALZJHGE49 thread 1792770386.754829, ts 1792770807.935489): *"the hourly report processing should stop these inactive/completed agents every hour."* Completed agents count too: a finished agent stays listed in the pane until TaskStopped.
1. List them with the `ListAgents` tool (status running/completed per subagent), or the PM pane's background-task footer. For each, read the transcript's last write time and last action (`~/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/<session>/subagents/agent-<id>.jsonl` + `.meta.json` for agentType/description).
2. **Stop every `completed` agent** (final report already returned) each hourly, no exceptions beyond rule 3's live-monitor check. **Inactive** = no transcript write for ≥60 min, OR its work is already terminal (its PR merged/closed, its issue closed, or it returned its final report and is waiting on nothing), OR it was launched by a previous PM session and has no open pm-ops owner row.
3. **Stop** every inactive agent with `TaskStop <id>` in the SAME turn. Never stop an agent that is mid-tool (a write within the last 60 min) or one that owns an open pm-ops row whose wake hasn't fired. Waiting on a remote box, CI run or Spark job counts as active if its monitor/box is live; check before stopping.
4. If a stopped agent owned unfinished work, record its last state on the owning pm-ops row (or create one) so the work is re-launched deliberately, never silently dropped.
5. The Step 4 reply includes the line `Agents: N running · stopped K inactive (<description, idle Xm>…)`, or `none inactive`.
The hourly heartbeat post carries the same count once the composer supports it (MoP change; until then the PM reply is the record).

## Step 3c-2: Dead proxy windows (Rajiv 2026-11-08, C0ALZJHGE49 thread 1794120614.633259, ts 1794120794.458589)
*"close the dead proxy windows. update hourly report processing skill to do it automatically?"*
Each hourly, PM closes every proxy tmux window whose pane process has exited (`pane_dead=1`): `px-*` windows in session `0` and `proxy-*` windows in session `proxies`. A dead pane has no live process, so closing it loses nothing; the run's out/log/jsonl files and its PROXY_STOP message are the record.
```bash
# Automated: pm-hourly-post.sh closes every dead window (pane_dead=1, PM scratch windows included, except 'claude') and prints DEAD_WINDOWS_CLOSED=N. PM re-runs this at processing time and reports the count:
for s in 0 proxies; do for w in $(tmux list-windows -t $s -F '#{window_id}|#{window_name}|#{pane_dead}' 2>/dev/null | awk -F'|' '$3==1 && $2!="claude" {print $1}'); do tmux kill-window -t "$w"; done; done
```
Never close a window whose pane is alive (`pane_dead=0`), even when the heartbeat marks it STALE. Classify its work through Step 3b; silence is not death and never authorizes kill/relaunch. Even for an exited pane, reconcile live children/remote jobs, retained results and UNKNOWN custody before ownership-safe cleanup. The Step 4 reply includes `Proxy windows: closed N dead` and Step 3b progress/handoff dispositions.

## Step 3c-3: Every proxy running for more than an hour (Rajiv 2026-11-13, C0ALZJHGE49 thread 1794548660.126669, ts 1794549737.295419)
*"Update the processing skill to check all proxies running for more than an hour."*
Each hourly, list EVERY live proxy (`ListAgents` plus live `px-*` tmux panes with `pane_dead=0`) whose current run has been going for more than 60 minutes. Use the run's own launch time (proxy-agent out/jsonl timestamp, or the pm-ops `proxy_run` row), never the report badge alone.
For each one, record in the Step 4 reply: name · issue/PR · model · elapsed · S131 stage start/bound · last substantive progress (head pushed, proof receipt, review marker), with its time · current blocker · next checkpoint. Classify each:
- *progressing:* new evidence within the last 60 min. Send one tailored checkpoint naming the next deliverable.
- *waiting:* a named external wait (CI run, CTO decision, merge, box lease). Record the wait and its wake; the S131 clock pauses for external waits.
- *stalled / looping:* no new substantive evidence for more than 60 min and no named wait, or the log shows the same action failing repeatedly. Promote it the same turn, without waiting for the 3h bound or a second check (S144, Rajiv ts 1794673980.861719): push or preserve WIP, stop and fence the run, and relaunch ONE successor one tier up at the pushed head.
- *over bound:* past its S131 1h bound (Rajiv 2026-11-21 ts 1795278253.434699). Push WIP, hand off and transfer the same turn (next eligible rung; at the top rung, one CTO decision packet). No age-only kill.
Report rows for exited or fenced runs as `ended`, with their PROXY_STOP result, not as live. Stale badges in the heartbeat report don't prove a run is alive or dead; check the pane and the socket.

## Step 3d: Social digest cycle receipts (CTO 2026-11-06, C0ALZJHGE49 thread 1793951140.445569, ts 1793954568.068649)
Every scheduled `donna-daily-social-digest` cycle (IST 00:30, 03:30 … 21:30) posts a receipt in C0C7U35LBKR showing attempts / confirmed / UNKNOWN / reason, even when it sends 0. Each hourly, check the cycles due in the last 3h. If a cycle has no receipt 45 min after its slot, flag it `MISSING` in the Step 4 reply (or `UNKNOWN` if a partial receipt exists) and report it in the CP Repairs thread. Missing proof is not proof that the slot never ran, and it is never a replay trigger. Add no new timer or registry.

## Step 3d: Proxy caps monitor (Rajiv D0AMF0XE6TS thread 1794659684.547559, ts 1794662285.037179: "we have to set the caps in the morning brief and monitor the caps in the hourlies")
Every hourly cycle, run `python3 -I ~/.claude/scripts/cost/proxy-burn-dial.py --apply` (checklist item 11): report each independent pool cap with its source/change and any overrides, plus the PM pane account's pace per item 0 (no fixed %/h target). Unmeasured accounts are named, never shown as 0; a refused or unavailable dial is reported, never invented. A DOWN decision pauses eligible new assignments; in-progress PRs continue.

## Step 4: Close the loop
Post ONE reply in the report thread with this content:
- Account pace line FIRST (checklist item 0), then the PR trend line (Step 2), then the TL;DR line.
- Errors: each class → tracked by #N / investigator launched.
- PRs: the full per-PR list from Step 2d (every open PR, one line each, MOVING/STUCK/PARKED), followed by the actions taken. Flow line: `open N (cap 25) · merged_6h M · opened_6h O · main <green|RED since HH:MM>`.
- Slots: Step 3's per-lane current tuple/evidence, concrete decision, actual PM communication receipt/disposition and next checkpoint. Missing inspection or communication stays explicit with blocker/owner/wake; "working", an owner name, a checklist or launching the inspector is not completed steering.
- Proxies: Step 3b's complete work-progress rows, total/stage elapsed, resolved model, status, real handoff/collection receipt or exact blocked disposition. A missed stage bound remains visible through rung -> CTO.
- Agents: running count and inactive agents stopped (Step 3c).

Hourly-thread PM replies carry NO CTO @-mention unless the reply contains a merge ask, a ladder-cleared decision ask, a CTO review/approval request or a safety escalation (Rajiv D0AMF0XE6TS thread 1794878559.300109). Do not poll or watch workflows. Stop when every line has a disposition.

## ONE PR cap = 25 (Rajiv 2026-10-16, C0ALZJHGE49 ts 1792128929.335409; supersedes Step 2b caps)
"just keep one cap at 25 open prs". If open non-draft PRs > 25, stop new_issue assignments (rework/repro only). At 25 or fewer, assign new_issue automatically; no Rajiv approval, no hysteresis. The 20 WIP alarm and the 20 resume threshold are retired.


## Thread replies are PM, never Alerts (Rajiv 2026-10-20)
Source: C0ALZJHGE49 thread 1792510362.837119, ts 1792510805.167479: "note that the replies has to be posted by pm, not alerts. alerts just post the top level thread which the pm processes."
- `SLACK_SEND_AS=alerts` is ONLY for the top-level report post.
- Every reply/disposition IN the thread is posted as PM: plain `bash /Users/rajiv/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -t <thread_ts> -f < file` (no SLACK_SEND_AS).
- A bg agent running this skill returns its disposition text to PM; PM (or the agent as PM identity) posts the thread reply the SAME turn.

## Step 3d: Unblockable now (Rajiv 2026-10-25)
Source: C0ALZJHGE49 thread 1792910728.369929, ts 1792910836.320109: "why are highlighting stale MoP labels in hourly review? we need to unblock the dependencies, not fix the labels."
The hourly report extends this SAME section as `*Unblockable now / issue lifecycle:*`, with counts and the top ten findings; never add a duplicate reconciler section. Act on it per the section below. Also act on open `parent_next_slice` obligations (cleanup-pr): confirm the parent's next child is READY, else split the parent.

### Issue lifecycle dispositions (every hourly)
Source: Rajiv, DM D0AMF0XE6TS thread 1793084931.341779, ts 1793086564.280889: "add the lifecycle reconciler to the hourly report and its corresponding processing skill. there's already a section there for it."

The composer runs `issue-lifecycle.py` without obligation writes. Read ALL
findings in its existing saved output, not only the report's top ten. Re-read
each issue's live body/ACs, MoP ownership, PR references and blocker states.
Preserve any separately tracked commitment. Choose ONE disposition per finding:

- **Re-queue:** quote the exact next unchecked AC and identifier from the issue
  body; use the existing `ready-pool-contract-repair` path to add/refresh the
  canonical Ready Pool block and `status:todo`, reconciling obsolete lifecycle
  labels only after checking live ownership. Never invent, rewrite, split or
  silently waive an AC. If the path is unavailable or the next AC is ambiguous,
  park with that precise reason instead of hand-rolling a contract.
- **Close with evidence:** map EVERY declared AC to merged/runtime evidence and
  follow the existing CTO-first closure rule below. A done label or merged
  "Closes" PR is not sufficient proof; unmet ACs must not be closed.
- **Unblock:** verify every named blocker is closed with shipped ACs or its PR
  merged (closed-unmerged is not enough), and that no approval/external/runtime
  wake remains. Use the existing unblock/promoter workflow below; do not treat
  label cleanup as the outcome or invent new ACs.
- **Park with a reason:** name the remaining AC/blocker, owner and wake condition
  or next-review time. Preserve live ownership/WIP; missing references and
  unavailable providers are unknown, never "resolved".

Record issue, disposition, exact AC/evidence, owner and real unblock event in
the existing issue/PR/source thread. A routine wait needs no obligation mirror.
Update or resolve an explicit commitment only when its own scope is satisfied.
Include per-issue dispositions in Step 4 as PM. An unavailable collector is
UNAVAILABLE, not zero;
never advance its merge cursor manually. The first run backfills 24h of partial
merges; historical merged closing references are checked for all open issues
regardless of that cursor.

## Unblock handling (Rajiv 2026-10-25 ts 1792910836.320109)
For each item under `*Unblockable now:*`, in the SAME hourly cycle:
1. Verify the blocker actually landed: a merged PR (not closed-unmerged), or a closed issue whose ACs shipped. Read the cited blocker, not the label.
2. If the issue's Ready Pool contract is complete (ac_summary, claimable_slot_type, blockers: none, next_action), promote it to the Ready Pool (status:todo via the normal promoter path).
3. If the contract is incomplete or stale, file a contract refresh (rewrite blockers/next_action/ACs against what landed, or split the parent if too broad).
4. Resolve the matching pm-ops obligations for that issue: `python3 ~/.claude/scripts/pm-ops.py obligation-resolve --kind <kind> --id <id> --reason "<reason + Rajiv ts>"` (list open rows first and match on issue number).

Rules:
- Never spend hourly effort on label drift; MoP projects labels. Do not edit labels to "fix" ownership or status.
- Close dispositions still require CTO review (Rajiv ts 1792908718.617279): do not close an issue on the PM's verification alone; send the evidence to the CTO and close only after that review. Do not close an issue with an unmet AC.
- Tooling: the PreToolUse hook allows ONE `gh issue comment|close|edit` per Bash call with a literal issue number (no shell variables, no `&&` chains); write comment text to a scratchpad file and use `--body-file`.
- Post the per-issue disposition in the hourly thread reply as PM (Step 4).


### Per-hour playbook: one case per dependency class (Rajiv ts 1792911663.785719: "update hourly processing skill to handle these dependencies")
Classify every `*Unblockable now:*` item (and every other stuck dependency seen in the hourly) into exactly one case, act in the SAME hour, and name the case + owner in the thread reply. Run steps 1-4 above for every case that ends in promotion.

| # | Class (example) | One-line action | Owner |
|---|---|---|---|
| 1 | Blocker merged/closed, contract still valid | Promote to the Ready Pool via pm-triage-agent (metadata READY, status:todo), then launch via launch-agent (hd-planner/hd-builder) under current caps. | PM + pm-triage-agent |
| 2 | Blocker merged but design changed underneath (#11041/#11042 after #11038) | Contract rewrite first via issue-contract-auditor (blockers/ACs/next_action against what landed), THEN promote. Never promote a stale contract. | PM + issue-contract-auditor |
| 3 | Blocker PR closed UNMERGED, wake condition can never fire (#10294/#10323 waiting on #10322) | Re-slice: file a new slice OR rewrite the wake condition the same hour. | PM |
| 4 | Gate needs a plan-only step first (#11244, CTO AC-0 plan) | Launch as planning-only (`hd-planner`, no implementation) within caps (cap 25). | PM -> hd-planner |
| 5 | Unowned upstream that is itself claimable (#10537 unblocks #10538-10540) | Promote the upstream FIRST as the highest-leverage item; children follow when it lands. | PM + pm-triage-agent |
| 6 | Waiting on a Rajiv or product decision (#10248 Clio approval, #11009 product call on #9334) | Same-turn DM to Rajiv (D0AMF0XE6TS) with the exact question and a PM default (S9). For a new user-facing feature/surface or credential flow, scope change or build-vs-workaround choice, DM Rajiv the same turn with the CTO recommendation, fastest-unblock alternative and cost; his explicit OK precedes execution and no separate CTO forward is needed. Pricing/billing/policy and technical calls stay CTO-final via the ladder. Record the decision ts on the issue/obligation. | PM (Rajiv for the named product exception; CTO otherwise) |
| 7 | Waiting on a bench/baseline refresh (#10016) | Launch a PM bg bench agent (S61), never a slot. Representative bench per rule 44: 1 proofread rep per arm (near-tie only expands), 3 format/SC reps per arm; Gemini on Vertex batch. | PM bg bench agent |
| 8 | Waiting on a real recurrence/telemetry, instrumentation already merged (#10703, #9852, #8153) | Park with an explicit wake query: name the Axiom/event query that would fire. No wake condition (#6904) -> write one now. | PM |
| 9 | Delivered (a merged PR closes it) | Propose closure with evidence (merged PR, ACs mapped). Closures go to the CTO FIRST (Rajiv ts 1792908718.617279); close only after CTO review. | PM -> CTO |

### No repeat-without-action rule
An Unblockable item may NOT appear in two consecutive hourlies without an action taken on it. Before posting the thread reply, compare against the previous hourly's `*Unblockable now:*`; any carry-over must show the case number and the action taken (or the typed blocker + owner + wake) in the reply. A carry-over with no action is a miss, handled the same turn.

## [HIGH] Step 2e: Ready Pool refill, P0/P1 first, every hourly (Rajiv 2026-10-31)
Rajiv, DM D0AMF0XE6TS thread 1793324569.896659, ts 1793417547.753829: *"Why am I having to nudge for ready pool promotion and draft pr's? The hourly report processing skill should be doing all this. Update it."* Trigger: on 2026-10-31 the pool sat at 0–5 for 3h+. 26 P0s were open, 7 of them backlog leaves nobody promoted or split. Rajiv had to ask 5 times.

Every hourly, IN THE SAME TURN and without waiting to be asked:
1. **Measure the real pool.** Run `python3 ~/.claude/scripts/ready_pool_select.py`. The claimable total is the JSON field `eligible_count`, counted before `--limit` truncation. `ranked` is only the displayed shortlist (default `--limit` 15) and must never be reported as the count (CTO ts 1793963776.093039). `now_unblocked` lists issues whose dependency checks currently pass; it is not a count of changes caused by a cleanup. The ≥10 pool minimum includes dependency-only blocked issues (S113): report them separately from the currently claimable count, using the selector's own fields and never a recreated eligibility filter. Before any launch, `ready_pool_select.py --check` must confirm all waits clear. Post `ready_pool: eligible <eligible_count> (snapshot HH:MM IST; displayed <len(ranked)>) (P0 a · P1 b · P2 c)` in the reply.
2. **If the real pool is under 10, OR any open P0/P1 is in backlog with no structured hold (Ready Pool header `holds:` / live GitHub blocked-by):** launch via Skill(launch-agent) (it picks transport from env PM_SUBAGENT_CAP; 12b and hd-proof-qa always launch via proxy-agent.sh; due 12b alone is burn-cap exempt (S147; CTO ts 1795074821.126009)): launch ONE `pm-arch-reviewer` MODE: PROMOTE batch over every open P0, then every open P1, then backlog leaves. Act on its verdicts in the same turn:
   - READY: promote (status:todo).
   - NEEDS_SPLIT or TRACKER: launch via Skill(launch-agent) (it picks transport from env PM_SUBAGENT_CAP; 12b and hd-proof-qa always launch via proxy-agent.sh; due 12b alone is burn-cap exempt (S147; CTO ts 1795074821.126009)): launch ONE agent that files a bounded ≤3-AC child from the parent's own AC text (ICL ledger, internal-followup marker, validator) and promotes it.
   - NOT_READY with a stale contract: rewrite the contract via issue-contract-auditor.
   - Waiting on a decision: case 6 below. Product and policy calls the PM can answer from standing rules (S94, rule 34/48, prior Rajiv/CTO ts found by recall) are answered by PM, never escalated.
3. **P0/P1 launch:** once a P0/P1 is promoted, launch hd-planner/hd-builder via `Skill(launch-agent)` the same turn; it never waits for, or preempts, another lane (S103/S137).
4. **Plan-only gated P0/P1** (e.g. a CTO "plan completion only" ruling): assign as plan work (`hd-planner`), then send ONE head-pinned CTO sign-off ask.
5. Name every action in the hourly reply. A P0/P1 sitting in backlog with no structured hold and no action this hour is a MISS.
6. **Pool size never gates promotion (gap fix 2026-10-31, Rajiv D0AMF0XE6TS thread 1793425202.193379: "Why don't these issues get promoted correctly? Where is the gap?").** With the pool at 17-21 (above 10), RL customer issues sat in backlog: no READY_POOL_LOW fired, and nothing else looked at them. Every hourly, regardless of pool size, run `python3 ~/.claude/scripts/backlog-triage.py promote --dry-run` and act on its output in the same turn:
   - SELECTED → `promote --apply --issues <N...>`.
   - NEEDS_READY_POOL_METADATA (stub `ready-pool: priority: Px` only; RL call issues #11782/#11786 were filed this way) → fill the full block from the issue's own ACs (validator-checked body file), then promote.
   - `not_runnable:latest_pm_comment_parked` → read the park comment. If its named decision or dependency has resolved, post a `PM UNPARKED` comment and promote. Otherwise leave it parked and name it with its wake condition.
   - `blocked_label` → check that the structured hold (header `holds:` / live blocked-by; legacy `pm-blocked:*` labels are migration input, not authority) is still true for the WHOLE issue. Narrow it via pm-triage-agent when only a sub-case is blocked and the ACs already exclude that sub-case (#11782).
7. **No-status P0/P1/P2 sweep.** Open issues with a P-label but no `status:*` label are invisible to both the promoter (backlog-only) and the selector (todo-only). On 2026-10-31 there were P0 1, P1 25 and P2 46. Each hourly, route each P0/P1 through pm-triage-agent/backlog-triage.py (the single writers of `status:`/`sub:`, rule 54) for one status (backlog + contract, or close it), at most 10 per hour, oldest first. Never hand-set status/sub or create a pm-blocked dependency label. Report the remaining count.

## [HIGH] Step 2f: Draft PR disposition, every hourly (Rajiv 2026-10-31, same ts)
Every hourly, IN THE SAME TURN, each open draft PR with no live owner (no slot lane, no PM bg agent, no live obligation) gets exactly ONE verdict and its action:
- **CLOSE:** the linked issue is closed, the work is superseded by a merged PR, or it's a parked draft older than 7d with no live blocker. Close it, keep the branch, and comment the reason plus the superseding PR.
- **REWORK-HD:** it needs Convex dev, Modal runtime or a browser (S14). Launch `hd-builder` `MODE: REWORK` via `Skill(launch-agent)` (proof on isolated remote boxes); rework goes before new_issue.
- **REWORK-PMBG:** no Convex/Modal needed (main-merge conflicts, inventory re-keys, test/doc fixes). launch via Skill(launch-agent) (it picks transport from env PM_SUBAGENT_CAP; 12b and hd-proof-qa always launch via proxy-agent.sh; due 12b alone is burn-cap exempt (S147; CTO ts 1795074821.126009)): launch a PM background agent now (S13/S14). Use `hd-builder` (rework) for PRs no live lane owns.
- **PARK:** blocked on a named open dependency or decision. Label it `pm-blocked:<kind>`, and record the wake condition and owner in a PR comment.
- **READY-FOR-12B:** complete. Run 12b at the live product head, then admit (merge main only on a GitHub conflict or changed-file overlap, S166).
launch via Skill(launch-agent) (it picks transport from env PM_SUBAGENT_CAP; 12b and hd-proof-qa always launch via proxy-agent.sh; due 12b alone is burn-cap exempt (S147; CTO ts 1795074821.126009)): run the classification in ONE background agent when there are more than 5 drafts. Then PM executes its verdicts in the same turn. The reply lists every draft with its verdict (Step 2d format). An unowned draft carried over two hourlies with no verdict action is a MISS.
- **Move drafts with proxies (Rajiv 2026-11-08, C0ALZJHGE49 thread 1794141994.059929, ts 1794142651.070669: "why are there so many draft pr's? can we move them with proxy agents?").** A draft counts as "owned" only if its owner made progress in the last 60 min: a new commit, a live px-* window whose output changed, or a slot actively working it. launch via Skill(launch-agent) (it picks transport from env PM_SUBAGENT_CAP; 12b and hd-proof-qa always launch via proxy-agent.sh; due 12b alone is burn-cap exempt (S147; CTO ts 1795074821.126009)): any draft with no progress for over 60 min gets an `hd-builder` rework proxy (`proxy-agent.sh hd-builder <brief>`) in the SAME turn. Use the default rung for new work except named pins; escalate (`--rung 2|3`) on evidence (stall, failed review), not just priority. No Gemini/Hydrogen/Carbon or Antigravity proxies; follow launch-agent's live role/rung selection and exact configured IDs. Give each proxy its own box owner name (`pm-agent-<issue>`). The brief must carry the exact next step: the open Codex P1s, the failing test, or the conflict, plus the head-bound proof manifest described in Step 3's handoff brief. The goal of each draft proxy is "ready + head sent to PM". Only explicit holds (merge_hold, atomic release, CTO containment, privacy SAFETY_BLOCK awaiting a named decision) stay parked, and each needs its wake condition recorded.

For an already-owned unresolved draft, Step 3b's finite handoff and EXTERNAL_WAIT rules take precedence over the 60-min draft heuristic above. Output/liveness alone is not progress; never launch a duplicate coder, bypass capacity or reset its stage based on this heuristic.

## [HIGH] Step 2g: Broken remote boxes are repaired the same turn; stuck sessions are redone (Rajiv 2026-11-08)
Rajiv, C0ALZJHGE49 thread 1794141994.059929: ts 1794142432.901929 "just reset them and put them back into service asap. we're paying for these dammit. the stuck sessions have to be redone."; ts 1794142651.070669 "update the hourly report processing skill. broken boxes have to repaired asap. the stuck sessions have to be redone."
Every hourly (and on any heartbeat line showing `broken`/`expired` boxes), IN THE SAME TURN:
1. List them: `~/.claude/scripts/remote-test-box.sh list | grep -E 'broken|expired'`.
2. Reset each through the admin path, from a clean cwd: `cd $HOME && env -i HOME=$HOME PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin USER=$USER ~/.claude/scripts/remote-test-box.sh pool-reset <box>`. If a lease isn't quarantined, use `down <lease-alias>` (never the bare server name). This standing Rajiv authorization covers resetting stuck/fenced boxes. Do not wait for proof of idleness beyond what the script itself checks.
3. If pool-reset refuses (quarantine, unaccounted processes/units, custody hold, sealed rebuild), apply Step 3a item 0 (S141) the same turn: reconcile the exact refusal and any pending effect with the pool owner, then rebuild the existing VM in place through the fenced path once custody is clear; recreate only if the provider confirms the server is missing. Never use admin/test/raw hooks or delete a surviving server. No artifact preservation or forensic step is required. Report verified state (healthy/grantable readback).
4. Redo the stuck session: identify the lane whose lease owner it was (slot N, pm-agent-<issue>, qa-capture/qa-spec run) and re-dispatch that work (rerun the proof/QA for that PR/issue) unless the lane has since moved on with a fresh box; record which.
5. The hourly reply lists each box: reset/rebuilt/free, plus the redone session or "lane moved on".
Use the single S141 procedure (Step 3a item 0): an exact-server `hcloud server delete <id>` is authorized there after the live-owner and pending-provider-action checks. Never delete any other server, and never touch a live lease (a live owner pid).

## [HIGH] P0/P1 never wait for a slot (S103, Rajiv 2026-10-29)
Rajiv, D0AMF0XE6TS thread 1793254511.567489, ts 1793256031.251919: "P0 and p1's should be done with pm hotfix agent when slots are not available."
Each hourly pass lists every ready P0/P1 issue that has neither a running `hd-planner`/`hd-builder` lane nor a live owner. PM starts an `hd-planner` (plan) and then an `hd-builder` (implement) for each one in the SAME turn via `Skill(launch-agent)`; Convex/Modal/browser proof runs on isolated remote boxes, not a slot (S14 retired with S137). An idle ready P0/P1 with no owner is reported as UNOWNED_P0P1.

## [HIGH] Disk ≥95% → empty Trash (Rajiv D0AMF0XE6TS thread 1795194166.074639, ts 1795194280.697719: "update the rules and empty trash whenever it reaches <95%")
Every hourly and on every worktree-lru disk alert: if `df -h /` shows ≥95% used, run `osascript -e 'with timeout of 3600 seconds' -e 'tell application "Finder" to empty trash' -e 'end timeout'` in the background from the PM session (no ask), then report before/after free GiB in the thread.

## [HIGH] Trash purge runs from the PM session (Rajiv C0ALZJHGE49 thread 1795055851.351669, 2026-11-19)
launchd's worktree-lru can't read ~/.Trash (macOS TCC; logs TRASH-SKIP-ROOT), so its purge_owned_trash never runs and cleanups free nothing. Do NOT grant Full Disk Access to a shared interpreter. Each hourly cycle PM runs from the PM session: `python3 -c "import importlib.util as u;s=u.spec_from_file_location('w','/Users/rajiv/.claude/scripts/worktree-lru-cleanup.py');m=u.module_from_spec(s);s.loader.exec_module(m);m.purge_owned_trash(True)"` (purges only entries the job trashed >24 h ago), then reports `df -h /` free GiB in the hourly reply.
