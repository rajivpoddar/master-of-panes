---
name: cp-repair-request
description: |
  PM-owned escalation channel for control-plane repair/bypass requests.
  Rajiv directive 2026-08-15 01:45: every CP repair request MUST start a NEW
  top-level thread in #heydonna-dev with the CTO at-mentioned (<@U0BNFGX2UAX>)
  — never a DM, never a reply buried in an existing thread. Use whenever the
  canonical typed transition refuses for a control-plane-only reason and the
  standing CTO rule (attempt canonical once, then bypass the direct surfaces)
  requires the CTO to execute the repair or the direct bypass. NOT for:
  routine PM transitions, slot nudges, product decisions, or answers to
  Rajiv's questions (those follow their own routing).
---

# CP Repair Request (PM -> CTO, new thread)

## Trigger

1. The canonical typed transition was attempted exactly once and refused for a
   control-plane-only reason (state mismatch with no handler, installed-parity
   drift, persistence failure, admission-gate refusal, wedge), AND
2. the repair or direct bypass is CTO-executed per the standing rule
   (2026-08-15 01:09): attempt canonical once, then bypass the direct
   authoritative surfaces.

## Procedure

1. **Attempt the canonical transition exactly once.** Keep the refusal output
   (exact command, exit code, reason) as evidence.
2. **Verify the authoritative tuple** (live GitHub/MoP state, head, epoch,
   labels) — the request must name the exact stuck transition and the verified
   postcondition it should produce.
3. **Post a NEW top-level thread** in #heydonna-dev (channel `C0ALZJHGE49`)
   with the CTO at-mentioned. Do NOT reply inside an existing thread and never
   DM the CTO.

   Send with the standard PM slack path (no `thread-ts` argument = new
   top-level post):

   ```bash
   cat << 'EOF' | bash ~/.claude/skills/slack-message/scripts/slack-send.sh \
     -c C0ALZJHGE49 -f
   <@U0BNFGX2UAX> CP repair request — <decision class>

   Stuck action: <exact skill invocation + exit + reason>
   Evidence checked: <live tuple: PR/issue, head, labels, epochs, receipts>
   PM recommended default: <one recommendation>
   Exact question: <what the CTO must decide or execute>
   EOF
   ```

   Preserve the returned `ts` — it is the new thread id; all follow-ups on
   this repair stay in that thread.
4. **Record** the request (owner CTO, blocker = the refusal reason) so the
   repair stays tracked until the parity/bypass receipt lands.
5. On the CTO's receipt: execute only the canonical postcondition the receipt
   names (e.g. the receipt’s named read-back) — never a raw
   label edit or a second canonical retry of the same refused tuple.

## Guardrails

- One request per refusal class; don't spam a new thread per attempt.
- Never bypass the merge guard, never fire CI, never weaken capture/dependency
  gates through this channel.
- A refusal with a verified tuple and a clear CP-only cause is a valid
  request; a refusal caused by missing product proof is NOT (that goes through
  the ordinary proof chain, not the CP channel).
