---
name: respawn
description: Restart the current HeyDonna dev-slot Claude Code session through MoP while preserving its session and canonical launcher environment.
---

# Respawn

Use only to reload the current numbered-slot Claude Code process or newly installed tools. For stale context, use the MoP clear operation instead.

Run exactly:

```bash
bash .claude/skills/respawn/scripts/respawn.sh
```

Arguments, model switches, and intermediate shell commands are intentionally unsupported. The script queues one duplicate-fenced request to MoP, waits for this turn to become idle, and then MoP owns `/exit`, the canonical `~/.claude/scripts/launch-slot-N.sh --continue` launch, boot verification, continuation, and restart fencing.

After running the script, stop immediately.
