---
name: qa-video-review
description: Review ProofShot QA recordings for evidence gaps and scenario faithfulness, then post the PM verdict and corrections to the same Slack thread.
---

# QA video review

Use after a browser QA video is posted. This is PM-owned analysis; the recording and frames are evidence, not a substitute for the brief or exact-head ACs.

## Required inputs
- PR number and exact 40-hex head.
- Complete acceptance-criteria list and QA brief, including ordered scenario steps for each AC.
- Local video path or Slack file ID, plus the channel and thread timestamp where QA was posted.
- Owning slot/agent for any correction and matching `qa_video_review` pm-ops obligation.

If the Slack file is not local, retrieve/download it using the established Slack file workflow, preserving the original privately. Do not put customer content in a repo or public artifact.

## Analyze the recording
Run the deployed helper (it extracts frames and returns JSON gap evidence):

```bash
python3 ~/.claude/scripts/qa-video-review.py /absolute/path/session.mp4 --out /absolute/private/review-frames
```

It records duration, source frame count, extracted frame count, fixed-cadence frame timestamps, scene-change timestamps, and `freezedetect` / `blackdetect` intervals. Read the JSON report and inspect the extracted frames. Frame analysis is evidence-led: use timestamps and scene-diff to identify time jumps, frozen or black stretches, cuts between steps, missing steps, recordings that begin after setup or end before the result, and blank/spinner-only stretches. Do not infer continuity from a few attractive screenshots.

## Faithfulness matrix
For every AC, quote the scenario steps from the QA brief and map each step to one or more frame timestamps. Mark each step `EXECUTED`, `MISSING`, `SUBSTITUTED`, or `UNCLEAR`. A different file, project, or path is `SUBSTITUTED`, even if visually similar. Check wrong-project identity, read-only state, error badges, stale head, and any setup artifacts. Note when the recording starts after setup or ends before the observable result.

## Verdict and corrections
Return `PASS`, `FAIL`, or `INCONCLUSIVE`:
- `PASS`: every required step is visibly evidenced on the correct project/path/head, and no material recording gap invalidates the evidence.
- `FAIL`: a required step is visibly missing/substituted, or the recording reveals a concrete failed state.
- `INCONCLUSIVE`: the video cannot establish whether a required step happened (including material gaps or unclear identity).

Cite frame timestamps for every AC and every gap. Send actionable corrections to the owning slot/agent via `SendMessage`; use `message-slot` only as fallback. Do not wait for a response before recording the review.

## Mandatory same-thread summary
Post as PM in the SAME Slack thread that contains the QA video, using plain `slack-send.sh` (never `SLACK_SEND_AS=alerts`):

```bash
~/.claude/scripts/slack-send.sh -c <channel> -t <thread-ts> -f < /absolute/path/review-summary.txt
```

The summary must contain PR/head, verdict, one per-AC line with quoted brief step(s) and frame timestamps/status, all detected gaps with timestamps, and corrections sent (recipient + concise correction). Include the JSON report path. Then resolve the matching obligation with the Slack post timestamp as evidence:

```bash
python3 ~/.claude/scripts/pm-ops.py obligation-resolve --kind qa_video_review --target-type github_pr --target-id <PR> --pr <PR> --reason "<summary post ts>"
```

Do not resolve before the summary is posted. If posting or obligation resolution fails, report the exact blocker and leave the obligation open.

## Mandatory per-video learning loop

For EVERY video (PASS, FAIL or INCONCLUSIVE), read the session summary as well as the extracted frames: `idle_share`, `gaps_gt30s`, `daemon_wedged`, `agent_think`, watchdog restart count, wall time and teardown receipts. Correlate each >30s gap with command/controller logs and frame timestamps; frozen frames alone do not prove agent think. Missing summary fields are UNKNOWN and a review evidence gap, never zero.

Append exactly one structured row for this review/run to `~/.claude/skills/qa-brief/LESSONS.md` through its MoP source. Include date, PR/run/head, scenario, wall/idle, EACH gap interval/cause, wrong waits, scenario drift vs brief, broken-box leftovers and applied diff. Count normalized causes against previous rows. If any cause repeats >=2 times OR idle_share >0.4, patch scenarios/run.py or qa-brief guidance in this SAME review, record the actual diff and deploy it via MoP. Do not merely recommend a future fix. If blocked, report the patch blocker explicitly and do not claim it applied.

Every verdict ends with `LESSON_APPLIED: <file:change>` or `LESSON_NONE: <reason>` (a first non-recurring low-idle run may legitimately need no patch, but still gets a row). Include wall_s, idle_s, idle_share, gap causes and the lesson diff in the review. Review learning must not alter product code or weaken an AC. No Slack posts when the requesting brief explicitly prohibits them; return the review to PM instead and leave posting-bound obligations open.
