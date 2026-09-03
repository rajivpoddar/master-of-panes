---
name: heydonna-slack-postback
description: Read full HeyDonna Slack messages from Block Kit `blocks`, or post complete plain-text replies and PM follow-ups as Abhijit CTO. Use for Slack thread URLs, requests to read/review/check a Slack message or thread, and triggers like "post it back", "postback", "reply in the thread", "tell Dhruv", "ask Dhruva", "message PM", or references to #heydonna-dev. Uses Slack REST with local SLACK_CTO_BOT_TOKEN and fail-closed Abhijit identity verification; never uses Slack MCP or Rajiv's user token for CTO writes.
---

# HeyDonna Slack Postback

## Purpose

Convert explicit HeyDonna Slack follow-up requests into an actual Slack write as Abhijit CTO. When this skill triggers, the Slack postback is part of the task boundary unless the user asks for a draft or review-first flow.

The managed install root is
`/Users/rajiv/.codex/skills/heydonna-slack-postback`. Load credentials only
from `/Users/rajiv/Downloads/projects/heydonna-app/.env.local`; never depend on
the invoking task's checkout containing an ignored `.agents/` directory.

## Default Destination

- Prefer the active or referenced Slack thread in `#heydonna-dev`.
- Saved Slack IDs:
  - `#heydonna-dev`: `C0ALZJHGE49`
  - Dhurva PM / Dhruv mention: `<@U0ALEAYCAUT>` (`dhurva_pm`)
  - CTO/Abhijit: `<@U0BNFGX2UAX>` (`abhijit_cto`)
  - Rajiv: `<@UEQTTB97A>` (`rajiv`)
  - Rajiv DM channel: `D0BPG55FG72`
- Use the saved IDs directly for routine HeyDonna postbacks. Re-verify only if a Slack write/read fails, the destination appears wrong, or the user asks to route somewhere else.
- Slack mentions must use the user ID (`<@U0ALEAYCAUT>`, `<@U0BNFGX2UAX>`,
  `<@UEQTTB97A>`), never the user name (`@dhurva_pm`, `@abhijit_cto`,
  `@rajiv`). Display names are not reliable Slack routing targets.
- CTO product and process decisions and blockers escalate to Rajiv in DM channel
  `D0BPG55FG72` using `SLACK_CTO_BOT_TOKEN`, with `<@UEQTTB97A>` and the source
  `thread_ts` when replying to an existing message.
- A process decision is any change, waiver, override, exception, or
  discretionary choice affecting Ready Pool order/admission, slot
  assignment/reassignment/release, PR-conveyor states or sequencing, review
  ownership, CI/capture/rerun, merge/rollout, monitors, or automation. PM sends
  the recommendation to CTO in `#heydonna-dev`; CTO alone sends Rajiv the DM.
- The decision DM contains the source, evidence, one recommendation, exact
  question, and immediate effect. Do not perform the proposed mutation before
  explicit Rajiv approval. Material drift requires a new approval.
- If the user says "post this back", "same thread", "the thread", "ask Dhruv there", or similar, reply in the existing thread with `thread_ts`; do not create a new channel post.
- If no active thread can be resolved from the current task context, read/search recent `#heydonna-dev` Slack context. If the intended thread is still ambiguous, ask a concise clarification before writing.

## Slack Read Path

- Use the Abhijit CTO bot-token curl path for HeyDonna Slack reads. The token
  is stored in repo `.env.local` as `SLACK_CTO_BOT_TOKEN`.
- Rajiv's `SLACK_USER_TOKEN` is READ-ONLY. Use it only to read channels the
  CTO bot cannot access (for example customer/evidence channels). Never use
  it to post or update messages.
- Never use Slack MCP/connector send, edit, delete, draft, or schedule methods
  for HeyDonna CTO writes. They can inherit Rajiv's user identity. Do not use
  Slack bridge writes, `slack-send.sh`, Dhurva's `SLACK_BOT_TOKEN`, or Rajiv's
  `SLACK_USER_TOKEN` for CTO writes. This is an identity boundary, not a
  transport preference, so do not create an exception in the current turn.
- For a Slack thread URL, parse:
  - channel from `archives/<CHANNEL_ID>/`
  - message/thread timestamp from `/p1782407589967399` as `1782407589.967399`
  - prefer the `thread_ts=` query parameter when present
- Read a thread with:
  ```bash
  set -a
  source /Users/rajiv/Downloads/projects/heydonna-app/.env.local
  set +a
  curl -sS -X POST https://slack.com/api/conversations.replies \
    -H "Authorization: Bearer $SLACK_CTO_BOT_TOKEN" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode channel=C0ALZJHGE49 \
    --data-urlencode ts=THREAD_TS \
    --data-urlencode limit=20 |
    python3 /Users/rajiv/.codex/skills/heydonna-slack-postback/scripts/render_slack_blocks.py
  ```
- Read recent `#heydonna-dev` context with:
  ```bash
  set -a
  source /Users/rajiv/Downloads/projects/heydonna-app/.env.local
  set +a
  curl -sS -X POST https://slack.com/api/conversations.history \
    -H "Authorization: Bearer $SLACK_CTO_BOT_TOKEN" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode channel=C0ALZJHGE49 \
    --data-urlencode limit=20 |
    python3 /Users/rajiv/.codex/skills/heydonna-slack-postback/scripts/render_slack_blocks.py
  ```
- Treat each message's `blocks` as the canonical visible content. Slack's
  top-level `text` is commonly a shortened notification fallback and can omit
  sections, lists, quotes, code, links, fields, or context.
- Always pass the complete Slack API response through
  `/Users/rajiv/.codex/skills/heydonna-slack-postback/scripts/render_slack_blocks.py`.
  It recursively renders Block Kit
  `rich_text`, section fields, context, lists, quotes, preformatted text,
  mentions, links, actions, images, and attachment blocks. Use top-level `text`
  only when no renderable blocks exist.
- Do not summarize or act on a message until its rendered block body has been
  inspected. Preserve message `ts`, user/bot identity, and thread ordering.
- If the renderer emits `[unsupported:TYPE]`, inspect that block or element in
  the raw API JSON before concluding that the message was read completely.
- If a read fails with `missing_scope`, report the exact Slack method and scope
  gap locally. Do not fall back to a bot identity because that can hide which
  messages Rajiv can actually see.
- Keep `SLACK_CTO_BOT_TOKEN` out of chat, logs, copied post bodies, and Slack
  messages.

## Slack Write Format

- Send every HeyDonna CTO write through
  `/Users/rajiv/.codex/skills/heydonna-slack-postback/scripts/cto_slack_rest.py`.
  The guard calls Slack REST directly, runs
  `auth.test` before the write, requires `user_id=U0BNFGX2UAX`, posts with
  `chat.postMessage`, reads the exact message back through
  `conversations.replies`, and requires the stored message author to be
  `U0BNFGX2UAX`. Any mismatch is a terminal transport failure.
- For replies to PM or operational threads, send the complete message in the
  top-level `text` field and omit the `blocks` field entirely.
- Preserve real line breaks as newline characters in the JSON `text` value.
  The bundled guard normalizes single-escaped `\n`, `\r\n`, `\r`, and `\t`
  sequences into real whitespace before constructing the payload exactly once
  with Python `json.dumps`. A doubled backslash remains visible. Use
  `--preserve-literal-escapes` only when the requested Slack prose intentionally
  displays escape syntax as code. Do not use `printf %b`, `echo -e`, or another
  decoder/serialization layer.
- Replies must always be threaded. For a channel mention, reply in the exact
  source thread: use the mention's `thread_ts` when present, otherwise use
  `thread_ts=<mention ts>`. For a DM, reply in the same DM thread: use the
  source `thread_ts` when present, otherwise use `thread_ts=<source ts>`.
  Do not create a new top-level post unless the user explicitly asks for one.
- Do not use Block Kit as the canonical write surface. PM runtimes may receive
  only top-level `text`; a brief fallback plus substantive blocks can therefore
  hide the actual instruction.
- Keep one plain-text message short enough for Slack to preserve it. If the
  required content cannot fit safely, send numbered consecutive plain-text
  replies in the same thread instead of moving any content into blocks.
- After an important PM instruction, re-read the thread and verify that the
  rendered top-level text contains the complete decision and next action. The
  guard's exact readback must match the normalized text. If escape syntax was
  intentionally requested as visible code, pass `--preserve-literal-escapes`.
  Fix an existing malformed message with `chat.update`; do not post a duplicate
  correction.
- Continue treating Block Kit as canonical for reads when incoming messages
  contain blocks. This write rule does not weaken the read/render requirement.

## Workflow

1. Use the Abhijit CTO bot-token REST guard for all Slack posts. The token is
   stored in the repo `.env.local` as `SLACK_CTO_BOT_TOKEN` and authenticates
   as `abhijit_cto` / `U0BNFGX2UAX`. Rajiv's `SLACK_USER_TOKEN` is read-only
   for channels the CTO bot cannot access and is never used for posts. Do not
   use `slack-send.sh`, or Dhurva's `SLACK_BOT_TOKEN` for HeyDonna CTO
   postbacks. Never use the Slack MCP connector for a HeyDonna CTO write.
2. Preserve postback content and arguments:
   - Default postback content is the full immediately preceding assistant answer or verdict, not a short recap, unless the user explicitly supplies replacement text to send.
   - If the user writes `postback, <instruction>` or `post this back, <instruction>`, treat the comma suffix as a postback parameter/routing instruction, not as replacement Slack body text.
   - Example: `postback, file with explore issue` means post the full prior answer to Slack and also file/route it through the explore-issue path.
   - Example: `post this back, ask Dhruv to file it` means post the full prior answer plus an explicit PM instruction to Dhruv; do not replace the prior answer with only "ask Dhruv to file it".
   - If trailing text clearly says `post this exact text:` or is quoted as the message body, use that text as the Slack body.
   - Do not answer or handle trailing postback parameters as separate local-only requests unless the user explicitly separates them from the postback.
3. Resolve destination:
   - Use the thread explicitly named by the user when provided.
   - Otherwise use the active HeyDonna Slack thread already inspected during the task.
   - Otherwise search/read recent `#heydonna-dev` context and choose only if unambiguous.
   - Read candidate thread messages through `render_slack_blocks.py`; never
     choose a destination from top-level fallback `text` alone when `blocks`
     are present.
4. Resolve Dhruv/Dhruva mentions:
   - Use the saved mention `<@U0ALEAYCAUT>` for Dhruv/Dhurva PM.
   - If the mention fails or Slack indicates the user is unavailable, fall back to plain text `Dhruv` or `Dhurva PM`; do not invent a replacement mention.
5. Compose the Slack message:
   - For plain `postback` / `post this back`, preserve the full substantive content being posted back. Do not silently compress it to a summary.
   - When a comma suffix contains routing parameters, append the resulting PM action or issue link after the full posted content.
   - Only shorten the message when Slack length limits require it; if shortening is necessary, preserve the decision, required changes, ACs, file paths, issue/PR IDs, and exact next action.
   - When posting into an existing thread, first read the thread and treat its latest reply as the immediate conversational context. Redraft the message as a direct reply to that latest message, preserving continuity and responding to its open question or decision; do not paste a standalone earlier answer unchanged.
   - Draft the post as a continuation of the Slack thread it will be posted to, not as a recap of the local Codex conversation.
   - Anchor the wording in the parent message and latest thread replies: answer the open decision, correct the latest claim, or give the next action in that thread's vocabulary.
   - Do not write meta-framing like "we discussed", "Rajiv asked", "my recommendation from our chat", or broad background unless that context already belongs in the Slack thread.
   - Lead with the thread-relevant status, decision, correction, or next action.
   - Include only the evidence needed to act: PR/issue/run IDs, failing command, root cause, and next action.
   - Preserve exact IDs, links, commands, filenames, and verdicts from the source context.
   - Keep internal routing metadata out of the visible message unless the user explicitly requests it.
   - Put the complete composed message in top-level plain text. Do not attach a
     `blocks` payload for PM or operational replies.
6. Send the message with the REST identity guard + `SLACK_CTO_BOT_TOKEN`:
   - Use direct send when the user says post/tell/ask/reply/share/send.
   - Use a draft only when the user explicitly asks for a draft or review-first workflow.
   - Include `thread_ts` for replies to an existing thread; do not create a new parent post when the request is a continuation of an active thread.
   - The bundled guard is mandatory. It constructs the JSON payload once,
     verifies the bot identity before the write, and verifies the stored author
     after the write. Do not replace it with Slack MCP or another wrapper.
   - Source the token and invoke the guard:
     ```bash
     set -a
     source /Users/rajiv/Downloads/projects/heydonna-app/.env.local
     set +a
     python3 /Users/rajiv/.codex/skills/heydonna-slack-postback/scripts/cto_slack_rest.py \
       --channel C0ALZJHGE49 \
       --thread-ts THREAD_TS \
       --text 'MESSAGE'
     ```
   - For complex multiline content, pass `--text-file PATH` or pipe the exact
     body on stdin instead of adding a second serialization layer. If upstream
     supplied visible single-escaped newlines, the guard converts them to real
     line breaks before posting and verifies that normalized text on readback.
   - Treat the guard's JSON receipt as the delivery receipt. It must report
     `auth_user_id=U0BNFGX2UAX`, `stored_user=U0BNFGX2UAX`, and the expected
     channel/thread. Independently render the thread only when Block Kit or
     broader conversational context must be inspected.
7. If the curl write fails, do not silently fall back to `slack-send.sh`,
   `SLACK_BOT_TOKEN`, Slack bridge, or Rajiv's user token. Report the Slack error
   locally and provide the exact post text so Rajiv can decide whether to retry,
   adjust permissions, or explicitly authorize a different transport.
8. Report back locally with the destination, Slack `ts`, and a one-line summary of what was posted.

## PM Routing Rules

- If the user asks to "ask/tell Dhruv to file", "have Dhruv refile", or similar in CTO/PM-routing context, post the instruction to Dhruv/PM rather than creating the GitHub issue yourself.
- All posts from this skill, including Dhruv/Dhurva PM handoffs, use
  `SLACK_CTO_BOT_TOKEN` and appear as Abhijit CTO (`U0BNFGX2UAX`). Rajiv's
  `SLACK_USER_TOKEN` is never used for posting; it is read-only for channels
  the CTO bot cannot access. A local bridge post or `slack-send.sh` bot-token
  post still breaks the PM handoff contract.
- If the message is a handoff, make the owner and next action explicit.
- If the message is a correction to an earlier thread, say what changed and what should be updated; avoid broad recap.
- If the user asks for "exceptions only" or PM-verbatim output, suppress healthy status and post only actionable exceptions.

## Safety Checks

- Treat `@here`, `@channel`, and customer-facing channels as high-impact. Do not add broad mentions unless explicitly requested.
- Do not claim a Slack write succeeded until Slack returns `ok=true` and a
  message `ts`, and the REST guard re-reads that exact message with
  `user=U0BNFGX2UAX`.
- Do not bypass an Abhijit CTO bot-token post failure by using `slack-send.sh`,
  the local Slack bridge, Slack MCP, Rajiv's `SLACK_USER_TOKEN`, or Dhurva's
  `SLACK_BOT_TOKEN`. Rajiv's `SLACK_USER_TOKEN` is read-only and may never be
  used to post or update messages.
- Keep `SLACK_CTO_BOT_TOKEN` out of chat, logs, and copied post bodies.
- Do not rely on stale local memory when current Slack/GitHub state is cheap to verify and affects the message.
- Do not post speculative blame. Use evidence-backed phrasing and identify uncertainty explicitly.
