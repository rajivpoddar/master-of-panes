---
name: pm-wait-nudge
description: Classify an injected slot wait notice and report it to PM.
---

Read the already-injected numbered-slot notice. If it is `LOCAL_CONTINUE`, continue local work and do not notify PM. If it is `PM_WAIT`, send PM one concise status/blocker notice through `message-pm` exactly once. If it is an idle/free assignment notice, send PM one concise free-slot and ready-work notice through `message-pm` exactly once. Do not assign, release, clear, reserve, or message a numbered slot from this skill.
