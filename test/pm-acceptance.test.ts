import assert from "node:assert/strict";
import { appendFileSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  chunkContainsAcceptedPrompt,
  confirmPmAcceptance,
  defaultAcceptanceDeps,
  promptMatchesSent,
  type AcceptanceDeps,
} from "../src/pmAcceptance.js";

const SENT =
  "# slack-channel C0ALZJHGE49 in thread 1794062577.800369 | Abhijit CTO | 12:43 AM\n" +
  "[slack-event C0ALZJHGE49/1794078832.227049]\n> Abhijit CTO @ 08 Nov, 00:40 IST: TL;DR: Prepare the bounded forward commit F\nbody \"quoted\" text";

const line = (record: unknown) => JSON.stringify(record) + "\n";
const wrapped = (text: string) => `<pasted_content id="ba3f">\n${text}\n</pasted_content id="ba3f">`;

const enqueue = (text: string) => line({ type: "queue-operation", operation: "enqueue", timestamp: "2026-11-07T19:13:54.200Z", content: wrapped(text) });
const userAtIdle = (text: string) => line({ type: "user", message: { role: "user", content: wrapped(text) } });
const queuedCommand = (text: string) => line({ type: "attachment", attachment: { type: "queued_command", prompt: wrapped(text) } });

test("an enqueue, user or queued_command record carrying the sent text is an acceptance", () => {
  assert.equal(chunkContainsAcceptedPrompt(enqueue(SENT), SENT), true);
  assert.equal(chunkContainsAcceptedPrompt(userAtIdle(SENT), SENT), true);
  assert.equal(chunkContainsAcceptedPrompt(queuedCommand(SENT), SENT), true);
  assert.equal(chunkContainsAcceptedPrompt(line({ type: "user", message: { content: [{ type: "text", text: SENT }] } }), SENT), true);
});

test("QUOTED text never counts: tool results, model text, request blobs, removals and bridge reads", () => {
  const quoted = [
    line({ type: "user", message: { role: "user", content: [{ type: "tool_result", tool_use_id: "t", content: SENT }] } }),
    line({ type: "assistant", message: { role: "assistant", content: [{ type: "text", text: SENT }] } }),
    line({ type: "api-request-blob", message: { role: "user", content: [{ type: "text", text: `see ${SENT}` }] } }),
    line({ type: "queue-operation", operation: "remove", content: wrapped(SENT) }),
    line({ type: "last-prompt", lastPrompt: SENT }),
    // A different prompt that merely contains the marker line (a quoted event) is not our paste.
    userAtIdle("I read this in bridge.db:\n" + SENT),
    userAtIdle(SENT + "\nplus an unrelated tail"),
  ].join("");
  assert.equal(chunkContainsAcceptedPrompt(quoted, SENT), false);
});

test("a nearby delivered message does not stand in for the lost one (live case 1794078832)", () => {
  const neighbour =
    "# slack-channel C0ALZJHGE49 in thread 1794078849.171979 | HeyDonna Alerts | 12:44 AM\n[slack-event C0ALZJHGE49/1794078849.171979]\n:x: *CI failed*";
  assert.equal(chunkContainsAcceptedPrompt(userAtIdle(neighbour), SENT), false);
  assert.equal(chunkContainsAcceptedPrompt(userAtIdle(neighbour), neighbour), true);
});

test("matching normalises line endings and the paste wrapper only", () => {
  assert.equal(promptMatchesSent(wrapped(SENT.replace(/\n/g, "\r\n")), SENT), true);
  assert.equal(promptMatchesSent(SENT, wrapped(SENT)), true);
  assert.equal(promptMatchesSent("", ""), false);
  const long = "L".repeat(600);
  assert.equal(promptMatchesSent(`${long}\nmiddle changed\n${"T".repeat(120)}`, `${long}\nmiddle\n${"T".repeat(120)}`), true);
  assert.equal(promptMatchesSent(`${long}x`, `${long}y`), false);
});

function fakeDeps(files: { size: number; chunks: Array<{ at: number; size: number; text: string }> }): { deps: AcceptanceDeps; clock: () => number } {
  let now = 0;
  let current = files.size;
  let pending = [...files.chunks];
  let content = "";
  const deps: AcceptanceDeps = {
    now: () => now,
    sleep: async (ms) => {
      now += ms;
      while (pending.length && pending[0].at <= now) {
        const next = pending.shift()!;
        content += next.text;
        current = next.size;
      }
    },
    size: async () => current,
    read: async (_path, from, to) => content.slice(from - files.size, to - files.size),
  };
  return { deps, clock: () => now };
}

test("acceptance is found when the CLI records the prompt shortly after the paste", async () => {
  const text = enqueue(SENT);
  const { deps } = fakeDeps({ size: 1000, chunks: [{ at: 400, size: 1000 + text.length, text }] });
  const result = await confirmPmAcceptance({ transcriptPath: "/t.jsonl", fromOffset: 1000, command: SENT, timeoutMs: 5000, deps });
  assert.deepEqual(result, { accepted: true, reason: "transcript_prompt_record" });
});

test("PASTED BUT NEVER ACCEPTED (live case): unrelated records appear, the prompt does not, so it is unconfirmed", async () => {
  const noise =
    line({ type: "assistant", message: { content: [{ type: "text", text: "working" }] } }) +
    userAtIdle("# slack-channel C0ALZJHGE49 in thread 1\n[slack-event C0ALZJHGE49/1794078849.171979]\nnext message");
  const { deps, clock } = fakeDeps({ size: 0, chunks: [{ at: 600, size: noise.length, text: noise }] });
  const result = await confirmPmAcceptance({ transcriptPath: "/t.jsonl", fromOffset: 0, command: SENT, timeoutMs: 3000, deps });
  assert.deepEqual(result, { accepted: false, reason: "pm_acceptance_unconfirmed" });
  assert.ok(clock() >= 3000, "waited the full window before giving up");
});

test("a record written before the paste is ignored", async () => {
  const older = enqueue(SENT);
  const { deps } = fakeDeps({ size: older.length, chunks: [] });
  const result = await confirmPmAcceptance({ transcriptPath: "/t.jsonl", fromOffset: older.length, command: SENT, timeoutMs: 1000, deps });
  assert.equal(result.accepted, false);
});

test("without a transcript the answer is unconfirmed, never accepted", async () => {
  assert.deepEqual(await confirmPmAcceptance({ transcriptPath: null, fromOffset: null, command: SENT, timeoutMs: 100 }),
    { accepted: false, reason: "transcript_unavailable" });
  assert.deepEqual(await confirmPmAcceptance({ transcriptPath: "/x", fromOffset: null, command: SENT, timeoutMs: 100 }),
    { accepted: false, reason: "transcript_unavailable" });
});

test("a record split across two reads is found once complete", async () => {
  const text = enqueue(SENT);
  const cut = Math.floor(text.length / 2);
  const { deps } = fakeDeps({
    size: 0,
    chunks: [
      { at: 200, size: cut, text: text.slice(0, cut) },
      { at: 400, size: text.length, text: text.slice(cut) },
    ],
  });
  const result = await confirmPmAcceptance({ transcriptPath: "/t.jsonl", fromOffset: 0, command: SENT, timeoutMs: 2000, deps });
  assert.equal(result.accepted, true);
});

test("against a real file: appended prompt is accepted, earlier identical text is not", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-acceptance-"));
  try {
    const path = join(directory, "session.jsonl");
    writeFileSync(path, enqueue(SENT));
    const from = statSync(path).size;
    const waiting = confirmPmAcceptance({ transcriptPath: path, fromOffset: from, command: SENT, timeoutMs: 600, pollMs: 20, deps: defaultAcceptanceDeps });
    assert.equal((await waiting).accepted, false);
    const accepted = confirmPmAcceptance({ transcriptPath: path, fromOffset: from, command: SENT, timeoutMs: 2000, pollMs: 20, deps: defaultAcceptanceDeps });
    setTimeout(() => appendFileSync(path, enqueue(SENT)), 60);
    assert.equal((await accepted).accepted, true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
