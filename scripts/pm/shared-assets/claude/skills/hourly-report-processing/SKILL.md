---
name: hourly-report-processing
description: PM processing of the posted "Hourly — HH:MM IST" heartbeat report in #heydonna-dev. Investigates every error class in it, drives every open PR to its next transition, and fills every idle slot, all in the same PM turn. Use whenever a HeyDonna Alerts hourly post (or Rajiv's question on it) arrives.
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

## Trigger
- A HeyDonna Alerts message starting `*Hourly — HH:MM IST*` in C0ALZJHGE49 (the thread ts is the report's ts).
- Rajiv asking about that report ("are PRs moving?", "any limbo?", "why is S2 idle?").

## Inputs (live, never the report's prose alone)
- Report text: the error classes, open PRs and Slots block.
- `gh pr list --state open --json number,headRefOid,isDraft,labels,updatedAt`
- `bash ~/.claude/scripts/heartbeat-slots-block.sh` (live MoP slot state)
- Open pm-ops commitments: `pm-ops.db` `obligations where status='open'`. Preserve intentional holds/asks regardless of their kind; routine PR actions come from the live PR report/conveyor.

## Visual waiver sign-off (S109)
Source: Rajiv DM D0AMF0XE6TS thread 1793721920.626689, ts 1793723647.696169, "go". Every hourly, read all open `visual_waiver_signoff` obligations in the canonical pm-ops ledger, including merged PRs. Check the PR transition thread, merge-alert thread and PR comments for the named verifier's explicit production sign-off; record its permalink and resolve the exact obligation with `pm-ops.py obligation-resolve --kind visual_waiver_signoff --id <id> --reason '<sign-off permalink>'`. Otherwise, once `next_review_at` (deploy + 24h) is due, send ONE reminder total: use the resolved PR transition thread, otherwise the verified PR merge-alert thread in `C0ALZJHGE49`, mentioning the verifier; if neither exists, post a PR comment with no @-mentions and log the fallback. Never invent a thread timestamp. [HIGH] DM the verifier, don't wait (Rajiv C0ALZJHGE49 thread 1793872014.789359, ts 1793888537.826699: "note that i was never dm'ed about these. why?"): the moment a `visual_waiver_signoff` row's served revision is proven (the squash is an ancestor of the served release) and the verifier is Rajiv, DM him in D0AMF0XE6TS the SAME turn with the exact prod check steps (where to click, what to expect) and ask for "ok <PR>" (S9). The 24h thread reminder is a fallback, never the first ask. Check both available threads, PR comments and ledger for `visual_waiver_signoff:<id>` before sending; include that marker and record the successful Slack or comment permalink as evidence via the same upsert. Never resend an existing or uncertain send; reconcile first. If verifier identity, ledger, required reads or send result is unavailable or uncertain, report an owned retryable blocker; never assume sign-off.

## Daily feedback and P0 age (Rajiv D0AMF0XE6TS thread 1793729847.114659, ts 1793730455.699259)
On the first hourly after 00:00 IST each calendar day, run `python3 ~/.claude/scripts/regret-log.py review` and `python3 ~/.claude/scripts/outcome-vs-spend.py --date <previous-IST-date>`; include both outputs in that hourly thread. Check the prior hourly thread for that IST date before posting to avoid duplicates; if the check or command fails, report UNAVAILABLE with an owner and retry next hourly, not zero. `STOP_AND_REPLAN` means pause routine spawning and replan. The outcome metric is a proxy, not verified deployment evidence.

Every hourly list all open issues and PRs, including drafts, via paginated GitHub queries (for example `gh issue list -R heydonna-app/heydonna-app --state open --limit 1000 --json number,createdAt,assignees,labels` and `gh pr list -R heydonna-app/heydonna-app --state open --limit 1000 --json number,createdAt,assignees,labels`), then filter P0 labels locally. Do not use `--label`: GitHub search returned zero despite live matching records on 2026-11-04. Check label variants in the repository too. For each older than 4h, print `P0_AGED <n> <age> <owner>` in the hourly thread and assign exactly one accountable owner in the same turn, using the existing MoP or pm-ops obligation path; preserve any live owner and record the next action. Verify open state and linked PR merge status live. Failed or truncated reads are UNAVAILABLE, never an empty result.

Routine CI-red classification and thread replies are inline or ONE batched agent per hour, not one per alert; at ~12 concurrent PM background agents queue additional launches, while PM continues handling urgent input and same-turn ownership inline (S110).

## Step 1: Errors (Axiom section)
For each error class listed:
1. **Mapped to an open issue/PR AND count at or below the 7-day same-slot median:** no action. Record "tracked by #N".
2. **Unmapped, OR above median, OR a new class:** launch ONE `customer-artifact-investigator` (read-only, max 1 per report, Claude usage is high). Give it the class, users, the 1h window and the report thread. It posts its verdict (root cause, evidence line, next action, owner) in the report thread.
3. **P0 signals (save lockout, data loss, export failure for a customer):** fast lane per SOP Priority §2. Send the holding reply first, then the investigator.
Never diagnose from the count alone.

## Step 2: Open PRs
**[HIGH] The "*Hourly per-PR status*" alert is processed with `Skill(hourly-per-pr-processing)` the same turn it arrives** (Rajiv 2026-11-08, C0ALZJHGE49 thread 1794120614.633259, ts 1794121118.991139). That skill owns the per-PR dispositions below, using proxy agents for reworks; this section stays as the reference table.
**Trend line (mandatory, Rajiv 2026-11-03 C0ALZJHGE49 ts 1793692399.637039: "trace the open pr count each in each hourly report"):** run `python3 ~/.claude/scripts/pm/open-pr-trend.py` and put its single output line FIRST in the hourly reply. It shows open non-draft vs the last hourly, opened/merged in the last 60 min and today, and the last-6 trend vs the 25 cap. If the count rose, name in one line which PRs were opened and why (P0 only while the freeze holds).
**Then:** run `python3 ~/.claude/scripts/pm/dual-green-sweep.py` and list its new asks, follow-ups, and any errors in the hourly reply. The sweep sends exact-head asks and follows up in the original ask thread after 30 minutes; skipped E2E is manual only. Do not duplicate its asks or follow-ups. Rajiv, C0ALZJHGE49 thread 1793681148.228999, ts 1793683532.171189 and ts 1793683640.762909; no launchd schedule.

Each open PR gets exactly one disposition:

| State | Action (same turn) |
|---|---|
| Not draft, no `ci-head:<live head>` | `pm-admission-reviewer`. On PASS, run `pm-admit-ci.sh <PR>` yourself; reviewers cannot change PR state. |
| CI/E2E red, unowned | CI red → `ci-repair-agent`. E2E red → `e2e-failure-investigator`. Retain the accepted owner in the existing incident/PR path; claim an existing explicit obligation without creating a routine status mirror. |
| Dual green at the live head | Exact-head merge ask to CTO in the PR's CI-success alert thread, with the 12b.1 screenshot line. |
| Green, held for screenshots/proof | Confirm a slot or agent owns the proof. If none, queue it first for the next free slot. |
| Queued run older than 30 min | Check job state before calling it a stall (`gh run view --json jobs`). Follow-on jobs waiting on the single ops runner are a backlog, not a stall. A real stall goes to `ci-repair-agent` (SOP §15). |
| Draft or rework with no owner and no update for 3h or more | Limbo. Rework with no Convex/Modal need → PM bg agent in a disposable worktree. Otherwise queue it for the next free slot. |
| `pm-blocked:*` | Confirm that the wait and its wake condition are recorded. |

Read the live head first; the report's head may be stale.

**Pending merges get a CTO follow-up** (Rajiv 2026-10-15, C0ALZJHGE49 thread 1792025128.705119, ts 1792026682.252209: "test skip pr's don't need my approval. follow up with cto regarding pending merges."). Every hourly pass lists each merge ask (and each admin-merge ask) that is still unmerged with age, exact head, and the original ask ts, and sends CTO ONE batched @-mention follow-up in the report thread (one line per PR). Main-red test-skip PRs never wait on Rajiv: send them straight to CTO as admin-merge asks (SOP 23 "Skip PRs merge via admin bypass").
CTO refinement (C0ALZJHGE49 ts 1792026838.395109): the follow-up reconciles the EXISTING PR Merges assignment (status/blocker), never a new merge attempt, duplicate ask or paid CI. The standing admin waiver covers proven main-red test-only skips only, not repairs, un-skips or product changes.

### Step 2b: PR flow health (WIP cap)
Source: Rajiv C0ALZJHGE49 thread `1791722798.068729`, ts `1791723384.313629`: *"update the hourly report processing skill with this. do not let the pr count climb so high. we need to keep a healthy rate between merges and new pr creation."* Trigger: 2026-10-11, 22 open non-draft PRs. Main CI had had no green run since ~09:30Z: each run was cancelled by the next merge, or failed on the inherited sync-7808 test. So nothing reached dual-green, while 8 slots kept opening PRs.

Every hourly report computes and posts these three numbers:
- `open` = open non-draft PRs;
- `merged_6h` = merges in the last 6h;
- `opened_6h` = new PRs in the last 6h.

It also records the main CI state: the latest COMPLETED `ci.yml` run on main (skip cancelled runs).

- **Main not green (latest completed main run is red, or no green run in 2h):** P0. Fix main before anything else. Use a `ci-repair-agent`. If the cause is a test or product fault, run it as a `pm-hotfix` (local RED→GREEN, then an admin-merge ask to CTO without waiting on CI). Once it lands, rerun the failed jobs on the PRs that inherited the red. Every red PR that shares main's failing test is "inherited", not PR-owned.
- **Queue-spike drain: `open` > 25 (Rajiv 2026-10-15 19:45 IST, C0ALZJHGE49 thread 1792072892.236499, ts 1792073702.350709: "update the hourly report processing skill to handle the open pr queue spikes. whenever it crosses 25, we have to stop new issue assigments and let the queue drain."):** stop ALL `new_issue` assignments the same turn. Freed slots get only rework, proof or red-fix work on existing open PRs, oldest admitted first. While draining:
  - Do NOT re-admit PRs onto an inherited main red; wait until main is clean.
  - Chase CTO merge asks for dual-green PRs.
  - Post `open`/`merged_6h`/`opened_6h` plus a drain ETA in the hourly reply.
  Resume `new_issue` assignments only when `open` is 20 or fewer (hysteresis), or when Rajiv lifts the stop explicitly. This supersedes the 10-15 "PR cap should not block issue filing" lift for assignments. Issues may still be FILED, but stay unassigned.
- **WIP cap (alarm only): `open` > 20 (raised from 10, Rajiv 2026-10-12 ts 1791814045.091859):** no `new_issue` assignments. A freed slot goes only to rework, proof or a red fix on an existing open PR, oldest admitted first. The cap lifts once `open` is 20 or fewer.
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
- `<owner>` is a slot, a named PM bg agent, CTO, or Rajiv. A PR with no live owner is STUCK.
- STUCK = no owner, or the same gate for more than 4h (Step 2c). Each STUCK line gets an action in the same turn (assign, launch an agent, send the merge ask, re-admit, close if superseded), and the line names that action.
- PARKED = `pm-blocked:*` with a recorded wake condition and owner.
Never summarize the PR list as "+N more" in the reply. If the list is long, post it as a second reply in the same thread instead of truncating it. Rajiv checks this thread to see whether PRs are moving.

## Step 3: Slots
- **Idle free slot:** `mop-assign-slot` the next work in the same turn, chosen by `python3 ~/.claude/scripts/ready_pool_select.py` (add `--rework-only` while the Step 2b WIP cap is hit; no `new_issue` then). The order is priority label first (P0 > P1 > P2 > P3), then rework before repro before new_issue, then oldest. Run `--check <issue>` before assigning; exit 3 means PRIORITY_INVERSION, so don't assign. A P2-or-lower `new_issue` is allowed only when no ready P0/P1 exists, and its `priority_reason` goes into the assignment receipt (Rajiv C0ALZJHGE49 thread 1793195119.674919, ts 1793202340.562609).
- **PRIORITY_INVERSION** (the heartbeat Slots block and ACTIONS line print `PRIORITY_INVERSION S<n> on #X (P2) while P0 #Y is ready and unassigned`): in the same turn, assign the P0 to the next free slot. If none is free, preempt the P2 lane with Skill(preempt-slot) (push WIP, then reassign the P0) unless that lane is at dual green or admitted. Name the action in the hourly reply.
- **IDLE-WITH-LANE (idle more than 10 min, lane still owned):** re-send its last packet via `message-slot`, or reassign.
- **Slot waiting on PM** (review, CI, admission or a decision): answer it or release it. A PR wait never holds a slot.
- **No eligible work:** post Rajiv an explicit hold with the reason and an end time.
- **Slot checkout review, ALL slots** (Rajiv 2026-10-06 12:44 IST, DM thread `1791270714.623429`: "update the hourly report processing skill to review the slot checkout and course correct if required."; extended C0ALZJHGE49 thread `1791294312.230849`, ts `1791295060.867539`: "update the hourly report processing skill to run it for all slots. do a quick review of the slot checkout and intervene if required. do this in a bg agent"). On every hourly report, launch ONE background `general-purpose` agent for all eight slots (S1-S8, `heydonna-app-3001`..`3008`, tmux panes `0:0.1`..`0:0.8`; skip a slot whose checkout does not exist yet, it is "not provisioned"), whatever a cron or lane says, with no time gate. Prompt it with the hourly `ts`.
  Per slot it reads: branch, `git log -1 --format='%h %cr'`, `git status --short` count, `git ls-remote origin <branch>` vs local HEAD (unpushed?), the MoP assignment (`rtk proxy curl -s localhost:3100/slots`) and `tmux capture-pane -p -t 0:0.<N> -S -300 | tail -60`. It judges convergence on the assigned issue's ACs.
  **Time on lane** (Rajiv C0ALZJHGE49 thread `1791337506.322739`, ts `1791339078.814959`: "can we also track how long has the slot been on the issue/pr in the report? MoP keep this data, right?"): now minus the slot's MoP `assigned_at` (reset when the issue/PR changes, kept on a same-lane re-pin). If `assigned_at` is null, use the earliest timestamp of the newest contiguous run of `GET /events?slot=<N>&type=assignment_effect_projection_completed&limit=50` whose `issue_projection.issue` equals the current issue. The hourly Slots block shows the same value as `on #<issue>[/PR#<pr>] for <Xh Ym>`.
  It intervenes with `~/.claude/scripts/message-slot.sh <N> --force --file <file>` when any trigger fires: wrong work vs the MoP lane or branch differing from it, risky/out-of-scope diff, no push in 60 min, same command re-run with no code change, a single turn of 2h+, a blocked hook/permission wait, or time on lane over 2× the slice estimate or over 4h. That last trigger adds a split check: SOP 17, "A slot past 2× its slice estimate gets the hourly course correction and a split check". Name the remaining ACs, and return NEEDS_PM with a proposed split. Message = 3 parts: (1) commit and push WIP, reply `WIP pushed <branch>@<sha>`; (2) one concrete next step; (3) 30-min hard stop (pushed head or typed blocker). Never relax a test threshold as the fix.
  **Close-and-reimplement at 3h on a PR** (Rajiv C0ALZJHGE49 thread `1791348298.888079`, ts `1791349855.166769`: "add the close and reimplement rule when slots cross 3h on a pr"; trigger #9005/#8970: 16h, 36 commits, 3k lines for 3 ACs). When a slot has been on the same PR for more than 3h (time on lane), the verdict is `NEEDS_PM: CLOSE_REIMPLEMENT`. The agent asks the slot to push its WIP and returns the PR, branch@sha and remaining ACs. PM then, in that same turn: (1) closes the PR with a comment naming the branch@sha it keeps for reference; (2) gives the issue to a slot as a fresh `new_issue` from origin/main, with a minimal plan scoped to the issue's ACs, an explicit "do not cherry-pick the old branch wholesale" line and a 3h time box. If the issue has more than 3 ACs, split it first (SOP 17). Exceptions, which PM records in the hourly reply: a PR already at dual green or admitted to CI, or one whose only remaining step is a PM/CTO-owned gate.
  **Repro lanes count too: 3h on the ISSUE, PR or not** (Rajiv C0ALZJHGE49 thread `1791423886.620339`, ts `1791425729.455819`: "was it doing a repro or an implementation? if it's repro, it can be closed an reimplemented."; ts `1791425780.521879`: "update the hourly report processing skill"; trigger S2 on #9062, ~14h, ~10h of it repro/characterization with nothing pushed and no PR). The 3h clock is time on lane (the issue), not time since a PR was opened. For every slot past 3h on lane, the agent classifies the work as **repro/characterization** (investigating, tracing, profiling, building harnesses; no product-code diff that fixes an AC) or **implementation** (a product-code diff with AC tests). Repro past 3h is `NEEDS_PM: CLOSE_REIMPLEMENT` even with no PR. The slot pushes its WIP plus findings to the issue, PM parks or closes the lane, splits the remaining repro into its own thin issue if needed, and gives the implementation to a fresh `new_issue` from origin/main with a 3h box. An implementation lane past 3h gets the split check and a 30-min hard stop (pushed head plus ready PR, or a typed blocker); if it misses that, close and reimplement. PM decides in the same turn, and never exempts a lane only because a split already happened.
  It never assigns or releases slots, never touches another slot's work, never runs builds/tests. It posts a table (slot | lane | time on lane | last push | turn | verdict ON_TRACK/CORRECTED/NEEDS_PM | action) via `~/.claude/scripts/slack-send.sh -c C0ALZJHGE49 -t <hourly_ts> -f < file` and returns it. PM handles NEEDS_PM items (reassign/release/decision) the same turn.

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
Reply line: `boxes: test <free>/<target> (<queued> queued, <broken> broken, <expired> expired) · browser <free>/<target> · action: <what was done/owner>`.

## Step 3b: Training progress (Rajiv 2026-10-21, C0ALZJHGE49 thread 1792550718.626739, ts 1792551127.300669)
*"update the processing skill to check verify the sindhu run steps as well. if step count remains the same then check the status with superproofer agent an dm me."*

Each hourly, read the report's `*Training:*` line (e.g. `sindhu-r7-n5625 step 1150/5625 ... (as of HH:MM IST)`) and compare its run id and step against the PREVIOUS hourly report's training line in C0ALZJHGE49.
- Step advanced → note `training: <run> step A→B ON_TRACK` in the Step 4 reply.
- Step UNCHANGED for the same run (or the `as of` time is stale by >1h) → launch the `superproofer` agent (MODE: TRAIN status check, read-only: process alive, last log step/loss, CUDA/MemAvailable, errors; no relaunch, no fix). In the SAME turn, DM Rajiv in D0AMF0XE6TS: run id, the stuck step, last-advanced time, the superproofer finding and the PM default (e.g. BUILD fix → TRAIN per S29). Also include it in the Step 4 reply.
- No training line, or "no run active", → CHECK, don't skip. Read the last known run, in this order: the previous hourly training line, pm-ops training obligations, then the superproofer STATE/run parent. If that run never reached its final step (e.g. last seen 2754/5625), treat it as CRASHED. Do these in the SAME turn:
  1. Launch Skill(spark-resume-run): ONE resume attempt from the latest complete checkpoint, under the S29 rule that PM owns Spark failures. Local Spark needs no spend GO.
  2. DM Rajiv: run, last step, crash time, and that the resume was launched.
  3. Note it in the Step 4 reply.
  If the run did finish, record it as `training: <run> COMPLETE` and skip.
  Source: Rajiv, C0ALZJHGE49 thread 1793583921.659259, ts 1793585706.576799: *"why wasn't it auto restarted? does the hourly report processing have that?"* Trigger: the box reboot on 11-01 at 14:27 IST killed r7 at step 2754. The hourly read "no run active" and skipped it for about 17h.

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
for s in 0 proxies; do for w in $(tmux list-windows -t $s -F '#{window_id}:#{window_name}:#{pane_dead}' 2>/dev/null | awk -F: '($2 ~ /^px-|^proxy-/) && $3==1 {print $1}'); do tmux kill-window -t "$w"; done; done
```
Never close a window whose pane is alive (`pane_dead=0`), even when the heartbeat marks it STALE. A stale live proxy is triaged instead: read its feed, then steer it with SendMessage, or kill it and relaunch, recording the owner. The Step 4 reply includes `Proxy windows: closed N dead`.

## Step 3d: Social digest cycle receipts (CTO 2026-11-06, C0ALZJHGE49 thread 1793951140.445569, ts 1793954568.068649)
Every scheduled `donna-daily-social-digest` cycle (IST 00:30, 03:30 … 21:30) posts a receipt in C0C7U35LBKR showing attempts / confirmed / UNKNOWN / reason, even when it sends 0. Each hourly, check the cycles due in the last 3h. If a cycle has no receipt 45 min after its slot, flag it `MISSING` in the Step 4 reply (or `UNKNOWN` if a partial receipt exists) and report it in the CP Repairs thread. Missing proof is not proof that the slot never ran, and it is never a replay trigger. Add no new timer or registry.

## Step 4: Close the loop
Post ONE reply in the report thread with this content:
- TL;DR line.
- Errors: each class → tracked by #N / investigator launched.
- PRs: the full per-PR list from Step 2d (every open PR, one line each, MOVING/STUCK/PARKED), followed by the actions taken. Flow line: `open N (cap 20) · merged_6h M · opened_6h O · main <green|RED since HH:MM>`.
- Slots: assignments made, and course corrections sent (slot, issue, time on issue, reason).
- Agents: running count and inactive agents stopped (Step 3c).

No CTO @-mention unless the reply contains an exact-head admission or merge ask. Do not poll or watch workflows. Stop when every line has a disposition.

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
| 1 | Blocker merged/closed, contract still valid | Promote to the Ready Pool via pm-triage-agent (metadata READY, status:todo), then assign on the next free slot. | PM + pm-triage-agent |
| 2 | Blocker merged but design changed underneath (#11041/#11042 after #11038) | Contract rewrite first via issue-contract-auditor (blockers/ACs/next_action against what landed), THEN promote. Never promote a stale contract. | PM + issue-contract-auditor |
| 3 | Blocker PR closed UNMERGED, wake condition can never fire (#10294/#10323 waiting on #10322) | Re-slice: file a new slice OR rewrite the wake condition the same hour. | PM |
| 4 | Gate needs a plan-only step first (#11244, CTO AC-0 plan) | Assign as planning-only (plan-agent, no implementation) within caps (cap25 / slot idle rules). | PM -> free slot |
| 5 | Unowned upstream that is itself claimable (#10537 unblocks #10538-10540) | Promote the upstream FIRST as the highest-leverage item; children follow when it lands. | PM + pm-triage-agent |
| 6 | Waiting on a Rajiv or product decision (#10248 Clio approval, #11009 product call on #9334) | Same-turn DM to Rajiv (D0AMF0XE6TS) with the exact question and a PM default (S9). Product/pricing calls go to the CTO first and the DM cites the CTO ts (S84). Record the decision ts on the issue/obligation. | PM (CTO first for product) |
| 7 | Waiting on a bench/baseline refresh (#10016) | Launch a PM bg bench agent (S61), never a slot. Representative bench, 3 reps (rule 44). | PM bg bench agent |
| 8 | Waiting on a real recurrence/telemetry, instrumentation already merged (#10703, #9852, #8153) | Park with an explicit wake query: name the Axiom/event query that would fire. No wake condition (#6904) -> write one now. | PM |
| 9 | Delivered (a merged PR closes it) | Propose closure with evidence (merged PR, ACs mapped). Closures go to the CTO FIRST (Rajiv ts 1792908718.617279); close only after CTO review. | PM -> CTO |

### No repeat-without-action rule
An Unblockable item may NOT appear in two consecutive hourlies without an action taken on it. Before posting the thread reply, compare against the previous hourly's `*Unblockable now:*`; any carry-over must show the case number and the action taken (or the typed blocker + owner + wake) in the reply. A carry-over with no action is a miss, handled the same turn.

## [HIGH] Step 2e: Ready Pool refill, P0/P1 first, every hourly (Rajiv 2026-10-31)
Rajiv, DM D0AMF0XE6TS thread 1793324569.896659, ts 1793417547.753829: *"Why am I having to nudge for ready pool promotion and draft pr's? The hourly report processing skill should be doing all this. Update it."* Trigger: on 2026-10-31 the pool sat at 0–5 for 3h+. 26 P0s were open, 7 of them backlog leaves nobody promoted or split. Rajiv had to ask 5 times.

Every hourly, IN THE SAME TURN and without waiting to be asked:
1. **Measure the real pool.** Run `python3 ~/.claude/scripts/ready_pool_select.py`. The claimable total is the JSON field `eligible_count`, counted before `--limit` truncation. `ranked` is only the displayed shortlist (default `--limit` 15) and must never be reported as the count (CTO ts 1793963776.093039). `now_unblocked` lists issues whose dependency checks currently pass; it is not a count of changes caused by a cleanup. Count only picks that are actually dispatchable: not already on a slot or PR, not a tracker, no open blocker. Post `ready_pool: eligible <eligible_count> (snapshot HH:MM IST; displayed <len(ranked)>) (P0 a · P1 b · P2 c)` in the reply.
2. **If the real pool is under 10, OR any open P0/P1 is in backlog with no `pm-blocked:*` label:** launch ONE `pm-arch-reviewer` MODE: PROMOTE batch over every open P0, then every open P1, then backlog leaves. Act on its verdicts in the same turn:
   - READY: promote (status:todo).
   - NEEDS_SPLIT or TRACKER: launch ONE agent that files a bounded ≤3-AC child from the parent's own AC text (ICL ledger, internal-followup marker, validator) and promotes it.
   - NOT_READY with a stale contract: rewrite the contract via issue-contract-auditor.
   - Waiting on a decision: case 6 below. Product and policy calls the PM can answer from standing rules (S94, rule 34/48, prior Rajiv/CTO ts found by recall) are answered by PM, never escalated.
3. **P0/P1 preemption:** once a P0/P1 is promoted, it takes the next free slot. If none is free, preempt the lowest-priority P2/P3 lane (Skill(preempt-slot)) in the same turn.
4. **Plan-only gated P0/P1** (e.g. a CTO "plan completion only" ruling): assign as plan work (pm-hotfix MODE: PLAN when no slot is free), then send ONE head-pinned CTO sign-off ask.
5. Name every action in the hourly reply. A P0/P1 sitting in backlog with no blocker label and no action this hour is a MISS.
6. **Pool size never gates promotion (gap fix 2026-10-31, Rajiv D0AMF0XE6TS thread 1793425202.193379: "Why don't these issues get promoted correctly? Where is the gap?").** With the pool at 17-21 (above 10), RL customer issues sat in backlog: no READY_POOL_LOW fired, and nothing else looked at them. Every hourly, regardless of pool size, run `python3 ~/.claude/scripts/backlog-triage.py promote --dry-run` and act on its output in the same turn:
   - SELECTED → `promote --apply --issues <N...>`.
   - NEEDS_READY_POOL_METADATA (stub `ready-pool: priority: Px` only; RL call issues #11782/#11786 were filed this way) → fill the full block from the issue's own ACs (validator-checked body file), then promote.
   - `not_runnable:latest_pm_comment_parked` → read the park comment. If its named decision or dependency has resolved, post a `PM UNPARKED` comment and promote. Otherwise leave it parked and name it with its wake condition.
   - `blocked_label` → check that the `pm-blocked:*` label is still true for the WHOLE issue. Remove it when only a sub-case is blocked and the ACs already exclude that sub-case (#11782).
7. **No-status P0/P1/P2 sweep.** Open issues with a P-label but no `status:*` label are invisible to both the promoter (backlog-only) and the selector (todo-only). On 2026-10-31 there were P0 1, P1 25 and P2 46. Each hourly, give each P0/P1 one status (`status:backlog` + contract, or close it), at most 10 per hour, oldest first. Report the remaining count.

## [HIGH] Step 2f: Draft PR disposition, every hourly (Rajiv 2026-10-31, same ts)
Every hourly, IN THE SAME TURN, each open draft PR with no live owner (no slot lane, no PM bg agent, no live obligation) gets exactly ONE verdict and its action:
- **CLOSE:** the linked issue is closed, the work is superseded by a merged PR, or it's a parked draft older than 7d with no live blocker. Close it, keep the branch, and comment the reason plus the superseding PR.
- **REWORK-SLOT:** it needs Convex dev, Modal runtime or a browser (S14). Queue it as a rework lane, priority-ordered. Rework goes before new_issue, so it takes the next free slot via mop-assign-slot `--class rework`.
- **REWORK-PMBG:** no Convex/Modal needed (main-merge conflicts, inventory re-keys, test/doc fixes). Launch a PM background agent now (S13/S14). Use pm-hotfix MODE: REWORK only for PRs no slot owns.
- **PARK:** blocked on a named open dependency or decision. Label it `pm-blocked:<kind>`, and record the wake condition and owner in a PR comment.
- **READY-FOR-12B:** complete. Merge main, run 12b, admit.
Run the classification in ONE background agent when there are more than 5 drafts. Then PM executes its verdicts in the same turn. The reply lists every draft with its verdict (Step 2d format). An unowned draft carried over two hourlies with no verdict action is a MISS.
- **Move drafts with proxies (Rajiv 2026-11-08, C0ALZJHGE49 thread 1794141994.059929, ts 1794142651.070669: "why are there so many draft pr's? can we move them with proxy agents?").** A draft counts as "owned" only if its owner made progress in the last 60 min: a new commit, a live px-* window whose output changed, or a slot actively working it. Any draft with no progress for over 60 min gets a `pm-hotfix MODE: REWORK` proxy in the SAME turn. Model by priority: sol for P0/P1/sync, terra for P1 otherwise, luna for P2/P3. Give each proxy its own box owner name (`pm-agent-<issue>`). The brief must carry the exact next step: the open Codex P1s, the failing test, or the conflict. The goal of each draft proxy is "ready + head sent to PM". Only explicit holds (merge_hold, atomic release, CTO containment, privacy SAFETY_BLOCK awaiting a named decision) stay parked, and each needs its wake condition recorded.

## [HIGH] Step 2g: Broken remote boxes are repaired the same turn; stuck sessions are redone (Rajiv 2026-11-08)
Rajiv, C0ALZJHGE49 thread 1794141994.059929: ts 1794142432.901929 "just reset them and put them back into service asap. we're paying for these dammit. the stuck sessions have to be redone."; ts 1794142651.070669 "update the hourly report processing skill. broken boxes have to repaired asap. the stuck sessions have to be redone."
Every hourly (and on any heartbeat line showing `broken`/`expired` boxes), IN THE SAME TURN:
1. List them: `~/.claude/scripts/remote-test-box.sh list | grep -E 'broken|expired'`.
2. Reset each through the admin path, from a clean cwd: `cd $HOME && env -i HOME=$HOME PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin USER=$USER ~/.claude/scripts/remote-test-box.sh pool-reset <box>`. If a lease isn't quarantined, use `down <lease-alias>` (never the bare server name). This standing Rajiv authorization covers resetting stuck/fenced boxes. Do not wait for proof of idleness beyond what the script itself checks.
3. If pool-reset quarantines on unfenced/unaccounted processes, try `pool-reset <box> --rebuild`. If that still refuses, launch ONE `pm-ops-tooling` proxy (sol) to list the items, preserve any QA artifacts to ~/.heydonna/box-preserve/<box>/, terminate only that stuck lease's leftovers through a sanctioned path, and rebuild. Report the box free.
4. Redo the stuck session: identify the lane whose lease owner it was (slot N, pm-agent-<issue>, qa-capture/qa-spec run) and re-dispatch that work (rerun the proof/QA for that PR/issue) unless the lane has since moved on with a fresh box; record which.
5. The hourly reply lists each box: reset/rebuilt/free, plus the redone session or "lane moved on".
Never use raw hcloud deletes; never touch a live lease (the script's own checks decide).

## [HIGH] P0/P1 never wait for a slot (S103, Rajiv 2026-10-29)
Rajiv, D0AMF0XE6TS thread 1793254511.567489, ts 1793256031.251919: "P0 and p1's should be done with pm hotfix agent when slots are not available."
Each hourly pass lists every ready P0/P1 issue that has neither a slot nor a running pm-hotfix agent. If all slots are busy on equal or higher priority work, PM starts a pm-hotfix agent (MODE: PLAN, then IMPLEMENT) for each one in the SAME turn. Exception: work that needs a slot's Convex dev, Modal runtime or settings screenshots (S14) goes to the next free slot, or PM preempts a P2+ lane. An idle ready P0/P1 with no owner is reported as UNOWNED_P0P1.
