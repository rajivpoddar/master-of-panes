---
name: lifecycle-email-processing
description: Daily HeyDonna lifecycle drip (D0/D1/D3/D7/FIRST_FILE_READY). Runs the deterministic selector, personalizes approved templates from ACTIVITY FACTS, sends from rajiv@heydonna.law, records state, posts ONE batch summary to #heydonna-growth. Triggered by the MoP weekday 10:00 ET cadence.
---

# Lifecycle email processing

Source: Rajiv, C0ALZJHGE49 thread 1793076476.707989; design ts 1793086314.202589; OK ts 1793086430.314129: *"ok. note that the subagent has to personalise the email based on the user activity"*.
Basis: `docs/investigations/2026-10-27-activation-aha-analysis.md`. The aha is the user's OWN file coming back formatted.

## How PM runs it
Launch `lifecycle-email-agent` in the background (`run_in_background=true`) with the prompt "Run Skill(lifecycle-email-processing)". Then return to normal event processing. Do not run it inline.

## Pieces
- Selector (deterministic, no LLM): `~/.claude/scripts/growth/lifecycle-drip.py`
  - `select [--dry-run]`: JSON `{meta, records, skipped}`. A non-dry run appends a `run` marker, which bounds the FIRST_FILE_READY window.
  - `record-sent --user-id U --email E --touch T --gmail-id G`: idempotent send-once record.
  - `opt-out --email E [--reason ...]`: adds the address to the opt-out list.
- State: `~/.claude/state/lifecycle-drip.jsonl` (send-once per (userId, touch)). Opt-outs: `~/.claude/state/lifecycle-drip-optout.jsonl`.
- Templates: `templates/{D0,D1,D3,D7,FIRST_FILE_READY}.txt`. The header holds `STATUS:`, `TOUCH:` and `SUBJECT:`, then `---`, then the body.

## Rules (selector)
- D0: signed up < 24h ago. D1: 1–2 days, touched the sample, no own file. D3: 3–4 days, no own upload. D7: 7–8 days, no activity in 5 days. FIRST_FILE_READY: first own-file transcript finished auto-process since the last real run. At most one touch per user per run (priority FIRST_FILE_READY > D0 > D1 > D3 > D7).
- Excluded: Scribie cohort (active member of an `externalOrgId=scribie` workspace, `cohort=scribie`, or a scribie.com/.ai source/referrer/landing), abuse-flagged (`adjustment` ledger row with an `abuse:` reason), @heydonna.law / @scribie.com, opted out, already sent, and anyone rajiv@ emailed manually in the last 3 days (so we don't double-email).

## Hard gates (agent refuses otherwise)
1. **Templates approved**: every template used must be `STATUS: APPROVED`. While any is `DRAFT_PENDING_RAJIV`, `meta.send_allowed=false`: run `select --dry-run` only, send nothing, record nothing, and return the would-send list to PM. Only Rajiv flips the status; PM edits the line after his written approval and cites the ts.
2. **Weekdays only** (America/New_York). `meta.weekday_et=false` means no send.
3. **Facts only**: personalization uses ONLY the record's `activity_facts`. No invented numbers, names, templates, states or outcomes. If a fact is absent, drop the sentence.
4. Plain text, Rajiv's founder voice, short. Keep the calendar link https://calendar.app.google/dDdvx2G3UzKrC7zY9 and the last line "Reply 'stop' and I won't email again." Never mention PR/issue numbers, internal terms (pipeline, BoN, ledger) or other users.

## Send loop (only when send_allowed)
For each record:
1. Fill the touch template: `{first_name}` (fall back to "Hi there,"), `{credits_left}` / `{audio_hours}` from facts, and replace each `{PERSONALIZE: ...}` with one or two sentences grounded in facts.
2. `gws gmail +send --to <email> --subject "<subject>" --body "<body>"` (rajiv@heydonna.law is the authenticated account). Take the `id` from the JSON output.
3. `python3 ~/.claude/scripts/growth/lifecycle-drip.py record-sent ...` immediately after each successful send. On a send failure, do not record; list it as skipped with the error.

## Slack (after the loop)
- ONE top-level post to #heydonna-growth `C0C7U35LBKR` (private, so emails are fine), as PM: `slack-send.sh -c C0C7U35LBKR -f < <unique scratch file>`. Content: date, count, then for each user the email, touch, subject and FULL body.
- ONE thread reply on that ts: Gmail message ids per user, plus the skipped users with reasons.
- Nothing is sent → no Slack post; return a one-line result to PM.

## Opt-out ("stop" replies)
Each run, before sending: `gws gmail users messages list --params '{"userId":"me","q":"in:inbox newer_than:14d stop"}'`. For each reply whose body is basically "stop" / "unsubscribe" / "no more emails", run `lifecycle-drip.py opt-out --email <sender> --reason "replied stop <gmail id>"`. List those in the thread reply. Manual opt-out works the same way: PM runs the `opt-out` command.
