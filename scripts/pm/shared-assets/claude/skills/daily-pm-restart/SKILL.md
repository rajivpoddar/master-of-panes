---
name: daily-pm-restart
description: Daily 04:00 IST PM continuation snapshot, stale-process cleanup and safe-point dev exits.
---

# Daily PM restart

Source: Rajiv, 2026-10-27: "restart pm at 4 am in the morning. add it to the MoP, like the other notification. it should trigger a skill that does all this, preferably in a script. the dev slots can be restarted by just setting exit pending on it."

1. Run `~/.claude/scripts/pm/daily-pm-restart.sh`. Use `--dry-run` for inspection only. The script snapshots open canonical pm-ops obligations, MoP slots and open PRs into the dated 0400 resume file, updates the MEMORY.md resume pointer, runs the existing guarded stale-process reaper, arms MoP exit-pending for all dev slots, then replaces only PM pane `0:0.0` with the existing cliproxy launcher and a continuation prompt. Arming dev exits precedes replacing PM so the invoking process cannot die before requesting safe-point exits. Never interrupt or respawn a dev pane.
2. The new PM reads the resume file and current ledger, reconciles each listed background-agent owner with live work, and relaunches only stopped agents that still have open work. Preserve task evidence, branches and ownership; a person or slot owner is not an agent type. Do not duplicate a running or completed agent. If the original agent packet is unavailable, record that precise blocker rather than inventing its task.
3. After recovery, post ONE line to Rajiv's DM `D0AMF0XE6TS` with the resume path and any blocked relaunches using the existing slack-send.sh and a unique scratch input file. An idempotent `ALREADY_REQUESTED` result means finish recovery from the saved file, not force another restart.

The script is locked and checkpointed per IST date. Logs are `/tmp/daily-pm-restart-<date>.log`; state is `~/.claude/state/daily-pm-restart-<date>.json`. A failed snapshot, pointer validation, cleanup or safe-point request refuses the PM replacement. Never bypass those errors, clear slots, edit MoP DB rows, or mutate agent permissions/configuration.
