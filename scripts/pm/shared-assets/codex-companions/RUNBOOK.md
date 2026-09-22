# RUNBOOK — an auth-store migration MUST recycle the app-server brokers

Recorded 2026-09-19 (CTO instruction), next to the auth migration that exposed it.

## The rule

**Whenever the companion `auth.json` is migrated, replaced, or re-logged-in,
recycle the `app-server-broker` processes for EVERY companion home. Otherwise
each affected lane fails its next Codex review with an auth error that
`codex login status` cannot see.**

## Why

`app-server-broker.mjs` processes are long-lived — the ones observed on
2026-09-19 had been running **9 days, 7 days, and 2 days**. A broker loads
credentials **at startup** and keeps presenting them for its whole life.

On 2026-09-19 the home `~/.codex-companions` had its `auth.json` replaced with a
symlink to `~/.codex/auth.json` (the previous file was kept as
`auth.json.migrated-away-20260919T135209Z`; the login flow is recorded in
`log/codex-login.log`). **Every broker that predated that swap kept presenting
the pre-migration refresh token.**

## How it presents — and why it is misleading

The review fails with:

    Your access token could not be refreshed because you have since logged out
    or signed in to another account. Please sign in again.

That text points at the **credential**, and the credential is fine:

- `codex login status` returns rc 0, "Logged in using ChatGPT" — because it
  reads **cached state**, not the live refresh path.
- A **fresh** process in the companion's exact configuration
  (`CODEX_HOME=/Users/rajiv/.codex-companions`, the app binary, its
  `config.toml` model and effort) returns a clean reply.

So two surfaces answer confidently about the wrong thing. **The only reliable
discriminator is process age against the migration timestamp:**

    ps -eo pid,lstart,command | grep app-server-broker

A broker whose start time predates the migration is presenting a stale
credential, regardless of what `login status` says.

## Fix

Recycle the broker(s) for the affected checkout — **at a turn boundary, and only
where no review is in flight** — killing the broker parent **and** its
`codex app-server` children. A fresh broker spawns on the next invocation and
picks up the current credential. Confirm by start time: a broker that started
*after* the migration is carrying the live credential.

Do not recycle a checkout with an active review; you will kill that review.

## Routine token refresh recycles too (2026-09-22)

The 2026-09-19 migration is **not the only trigger.** A routine token refresh
does the same thing: on 2026-09-22T06:35:17Z the home credential was refreshed,
and 6 of the 7 live brokers — started 2026-09-11, 09-17, 09-19, 09-20, 09-21 —
then presented the pre-refresh snapshot. Same failure text, same misleading
`login status` rc 0.

**The rule is therefore about freshness, not about migrations:** a broker whose
start time predates the current `last_refresh` must not be reused.

### Owned pre-acquisition check

Run the managed gate before acquiring or entering a companion broker:

    python3 /Users/rajiv/.claude/scripts/codex-companion-broker-freshness.py \
        --cwd <checkout> --busy auto

It reads ONLY the companion `auth.json` top-level `last_refresh` (never token
material) and compares it with the selected broker parent's start epoch:

| state | action |
| --- | --- |
| broker start >= refresh | no-op, reuse the broker |
| broker start < refresh, verified idle | recycle that broker parent + its `app-server` children |
| broker start < refresh, review in flight | **defer** — never interrupt; retry after the review's terminal |
| refresh or start epoch missing/unparseable | **fail safe**, typed diagnostic; never guess |

Exit codes: `0` fresh/no-op, `3` deferred, `4` fail-safe. The check is dry-run by
default; `--recycle` performs the recycle, and only ever for the selected broker
parent and its own `app-server` children — never a bulk kill.

`--busy auto` treats an absent broker socket as idle (the broker is not
serving) and reports an existing socket as unknown, which defers. Never pass
`--busy idle` unless you have actually verified no review is in flight.

### Removal condition

Delete this helper — or bypass it — once the upstream plugin natively reloads
credentials, or enforces equivalent per-request/TTL freshness, and that
behaviour is verified. Until then the check is the only owned control.

## Scope

The companion brokers themselves remain **upstream plugin code** under
`~/.claude/plugins/cache/openai-codex/codex/*/scripts/app-server-broker.mjs`,
not a HeyDonna repo surface, and are **not ours to patch**. This runbook and the
pre-acquisition gate are ours; the vendor behaviour is a separate upstream
follow-up.
