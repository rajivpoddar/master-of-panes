# MoP Changelog

## 2026-05-01 — Codex activity-review gate on /check-slot timer

**Rajiv directive 2026-05-01 13:35** (Slack thread `1777622260.949769`): *"implement v2. check every 5m."*

### Added
- `HookProcessor.runActivityReview(slotNum)` — spawns `~/.claude/scripts/check-slot-bg.sh` (130s timeout) per tick to get the structured codex verdict (STATUS/HEALTH/BLOCKERS/RETRY_LOOP/CI_DEPENDENT). Reuses the same script PM's `/check-slot` skill body invokes today. Auth via `~/.codex/auth.json`.
- `HookProcessor.shouldInject(summary, suppressedConsecutive)` — pure gate function. Inject on STATUS:ERROR (failure preserves today's behavior), HEALTH:INTERVENE/WATCH, BLOCKERS:[1-9], RETRY_LOOP, CI_DEPENDENT. Liveness pings: every 3rd UNCHANGED (~15min), every 6th HEALTH:OK (~30min).
- `suppressedConsecutive: Map<slot, number>` — per-slot counter, reset on inject + on `stopCheckSlotTimer`.
- New event type: `check_slot_suppressed` (db row tracks verdict + counter for visibility).

### Changed
- 5-min `/check-slot` timer body now runs codex review BEFORE deciding to inject. Suppresses no-op `/check-slot N` injections to PM pane when the slot is healthy. PM's Anthropic context tokens drop by the suppressed-ratio (expected 60-80% of ticks).
- `/tmp/slot-N-check.txt` now contains: codex summary + `--- tmux capture ---` + raw tmux. PM's existing skill body keeps reading this file unchanged. When suppressed, the file still updates so PM can read it manually.
- Interval cadence unchanged (5 min). Failure mode unchanged (STATUS:ERROR → inject anyway).

### Risks
- Codex auth missing in MoP child env → STATUS:ERROR fallback injects unconditionally (today's behavior).
- Suppressed counter resets on MoP restart → worst case one extra inject after restart.
- Concurrent ticks across 4 slots can run up to 130s codex calls in parallel — independent timers, no serialization risk.

## 2026-04-30 — Add answer-prompt block detector

**Rajiv directive 2026-04-30:** *"we also need to detect this in MoP and trigger a slot-blocked command on pm pane, same way we do for compact"*

### Added
- `StuckDetector.detectAnswerPromptBlock(slot)` — fires when a slot is parked at a numbered-options menu (`❯ N. ...` cursor + `Enter to select · ↑/↓ to navigate · Esc to cancel` footer) waiting for user input.
- New event types: `answer_prompt_block_detected`, `block_dispatched`.
- Per-slot dedup via `lastBlockMatchLine` + 10-min `BLOCK_DISPATCH_DEDUP_MS`.
- `StuckDetector.resetBlockTracking(slot)` — called from `HookProcessor.handlePostToolUse` on idle→active transition so multi-block sessions get clean detection.
- PM-side handler: `.claude/commands/slot-blocked.md` (heydonna-app) — mirrors `slot-context-overflow` flow.

### Detection signature
- Reliable trigger requires BOTH: (a) navigation hint footer, AND (b) `❯ N.` cursor on a numbered option line within 15 lines above.
- Hint string alone (e.g., quoted in plan text) is suppressed — eliminates the mid-stream false positive.
- Idle-prompt gate via `relay.isSlotActive` defers detection while slot is busy.

### Wiring
- `HookProcessor.setStuckDetector` typedef extended to include `resetBlockTracking`.
- `checkAll` adds Phase 1b loop running the new detector against all non-DND, non-PM slots.
- Capture written to `/tmp/slot-N-blocked-capture.txt` before injection so the PM skill can read full context.

### Files
- `src/stuck.ts` — new method, regex constants, dedup map, reset hook, wiring.
- `src/hooks.ts` — extended detector type, idle→active reset call.
- (heydonna-app) `.claude/commands/slot-blocked.md` — PM-side handler skill.

### Verification
- Build: `tsc` clean.
- Positive test: synthetic menu → matches, would fire `/slot-blocked`.
- Negative test: live slot 2 with codex-companion `ctrl+b ctrl+b` prompt → no fire (signature requires numbered cursor).
- False-positive test: hint string in mid-stream output without numbered cursor → suppressed.
