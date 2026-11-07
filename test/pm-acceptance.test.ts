import assert from "node:assert/strict";
import { appendFileSync, mkdtempSync, renameSync, rmSync, statSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  AcceptanceLedger,
  confirmPmAcceptance,
  defaultAcceptanceDeps,
  findAcceptedPrompt,
  promptMatchesSent,
  type AcceptanceDeps,
  type PasteCursor,
} from "../src/pmAcceptance.js";

const SENT =
  "# slack-channel C0ALZJHGE49 in thread 1794062577.800369 | Abhijit CTO | 12:43 AM\n" +
  "[slack-event C0ALZJHGE49/1794078832.227049]\n> Abhijit CTO @ 08 Nov, 00:40 IST: TL;DR: Prepare the bounded forward commit F\nbody \"quoted\" text";

const T0 = Date.parse("2026-11-07T19:13:54.000Z");
const at = (ms: number) => new Date(T0 + ms).toISOString();
const line = (record: unknown) => JSON.stringify(record) + "\n";
const wrapped = (text: string) => `<pasted_content id="ba3f">\n${text}\n</pasted_content id="ba3f">`;
const enqueue = (text: string, ms = 200) => line({ type: "queue-operation", operation: "enqueue", timestamp: at(ms), content: wrapped(text) });
const userAtIdle = (text: string, ms = 200) => line({ type: "user", timestamp: at(ms), message: { role: "user", content: wrapped(text) } });
const queuedCommand = (text: string, ms = 200) => line({ type: "attachment", timestamp: at(ms), attachment: { type: "queued_command", prompt: wrapped(text) } });
const never = () => false;

test("an enqueue, user or queued_command record carrying exactly the sent text is a prompt record", () => {
  for (const record of [enqueue(SENT), userAtIdle(SENT), queuedCommand(SENT), line({ type: "user", timestamp: at(5), message: { content: [{ type: "text", text: SENT }] } })]) {
    assert.ok(findAcceptedPrompt(record, 0, SENT, T0, never));
  }
});

test("QUOTED text never counts: tool results, model text, request blobs, removals, last-prompt and bridge reads", () => {
  const quoted = [
    line({ type: "user", timestamp: at(1), message: { role: "user", content: [{ type: "tool_result", tool_use_id: "t", content: SENT }] } }),
    line({ type: "assistant", timestamp: at(1), message: { role: "assistant", content: [{ type: "text", text: SENT }] } }),
    line({ type: "api-request-blob", timestamp: at(1), message: { role: "user", content: [{ type: "text", text: `see ${SENT}` }] } }),
    line({ type: "queue-operation", operation: "remove", timestamp: at(1), content: wrapped(SENT) }),
    line({ type: "last-prompt", timestamp: at(1), lastPrompt: SENT }),
    userAtIdle("I read this in bridge.db:\n" + SENT),
    userAtIdle(SENT + "\nplus an unrelated tail"),
  ].join("");
  assert.equal(findAcceptedPrompt(quoted, 0, SENT, T0, never), null);
});

test("WRONG POSITIVE FIXED: a long prompt whose middle differs is NOT the sent prompt", () => {
  const head = "H".repeat(260);
  const tail = "T".repeat(140);
  const sent = `${head}\nPlease merge #13074 now\n${tail}`;
  const different = `${head}\nPlease CLOSE #13074 and delete the branch\n${tail}`;
  assert.equal(promptMatchesSent(different, sent), false, "same first 200 / last 100 chars is not the same text");
  assert.equal(findAcceptedPrompt(enqueue(different), 0, sent, T0, never), null);
  assert.equal(promptMatchesSent(sent, sent), true);
});

test("matching normalises only line endings, outer whitespace and the paste wrapper", () => {
  assert.equal(promptMatchesSent(wrapped(SENT.replace(/\n/g, "\r\n")), SENT), true);
  assert.equal(promptMatchesSent(SENT, wrapped(SENT)), true);
  assert.equal(promptMatchesSent(`  ${SENT}\n\n`, SENT), true);
  assert.equal(promptMatchesSent("", ""), false);
  assert.equal(promptMatchesSent(SENT.replace("TL;DR", "tl;dr"), SENT), false);
  assert.equal(promptMatchesSent(SENT.replace(/ +/g, " ").replace("| ", "|"), SENT), false);
});

test("a nearby delivered message does not stand in for the lost one (live case 1794078832)", () => {
  const neighbour =
    "# slack-channel C0ALZJHGE49 in thread 1794078849.171979 | HeyDonna Alerts | 12:44 AM\n[slack-event C0ALZJHGE49/1794078849.171979]\n:x: *CI failed*";
  assert.equal(findAcceptedPrompt(userAtIdle(neighbour), 0, SENT, T0, never), null);
  assert.ok(findAcceptedPrompt(userAtIdle(neighbour), 0, neighbour, T0, never));
});

test("a record older than the paste, or with no usable timestamp, is not this paste's prompt", () => {
  assert.equal(findAcceptedPrompt(enqueue(SENT, -60_000), 0, SENT, T0, never), null);
  assert.equal(findAcceptedPrompt(line({ type: "queue-operation", operation: "enqueue", content: wrapped(SENT) }), 0, SENT, T0, never), null);
  assert.equal(findAcceptedPrompt(line({ type: "user", timestamp: "not a date", message: { content: wrapped(SENT) } }), 0, SENT, T0, never), null);
});

test("absolute byte offsets are exact, including multi-byte text, and claimed records are skipped", () => {
  const first = enqueue("# héllo wörld 日本語\n[slack-event C0/1.1]\nfirst");
  const second = enqueue(SENT);
  const text = first + second;
  const hit = findAcceptedPrompt(text, 1000, SENT, T0, never);
  assert.equal(hit?.offset, 1000 + Buffer.byteLength(first, "utf8"));
  assert.equal(findAcceptedPrompt(text, 1000, SENT, T0, (offset) => offset === hit!.offset), null);
});

type Clock = { now: number };
function fakeFile(initial = ""): {
  deps: AcceptanceDeps;
  clock: Clock;
  append: (text: string) => void;
  at: (ms: number, action: () => void) => void;
  state: { dev: number; ino: number };
  cursor: (text?: string, startedAtMs?: number) => PasteCursor;
  size: () => number;
} {
  const clock: Clock = { now: 0 };
  let content = initial;
  const state = { dev: 1, ino: 100 };
  const scheduled: Array<{ at: number; action: () => void }> = [];
  const deps: AcceptanceDeps = {
    now: () => clock.now,
    sleep: async (ms) => {
      clock.now += ms;
      for (const item of scheduled.filter((s) => s.at <= clock.now)) {
        scheduled.splice(scheduled.indexOf(item), 1);
        item.action();
      }
    },
    identity: async () => ({ dev: state.dev, ino: state.ino, size: Buffer.byteLength(content, "utf8") }),
    read: async (_p, from, to) => Buffer.from(content, "utf8").subarray(from, to).toString("utf8"),
  };
  return {
    deps,
    clock,
    append: (text) => { content += text; },
    at: (ms, action) => { scheduled.push({ at: ms, action }); },
    state,
    cursor: (text = SENT, startedAtMs = T0) => ({ path: "/t.jsonl", dev: state.dev, ino: state.ino, size: Buffer.byteLength(content, "utf8"), startedAtMs, text }),
    size: () => Buffer.byteLength(content, "utf8"),
  };
}

const intact = async () => true;

async function confirm(file: ReturnType<typeof fakeFile>, ledger: AcceptanceLedger, cursor: PasteCursor, extra: Partial<Parameters<typeof confirmPmAcceptance>[0]> = {}) {
  const { window, overlapsIdentical } = ledger.open(cursor);
  return confirmPmAcceptance({ ledger, cursor, window, overlapsIdentical, recipientIntact: intact, timeoutMs: 3000, deps: file.deps, ...extra });
}

test("acceptance is found when the CLI records the prompt shortly after this paste", async () => {
  const file = fakeFile(line({ type: "assistant", timestamp: at(-5000), message: { content: [] } }));
  const cursor = file.cursor();
  file.at(400, () => file.append(enqueue(SENT, 350)));
  assert.deepEqual(await confirm(file, new AcceptanceLedger(), cursor), { accepted: true, reason: "transcript_prompt_record" });
});

test("PASTED BUT NEVER ACCEPTED (live case): other prompts appear, this one does not -> unconfirmed", async () => {
  const file = fakeFile();
  const cursor = file.cursor();
  file.at(600, () => file.append(
    line({ type: "assistant", timestamp: at(500), message: { content: [{ type: "text", text: "working" }] } }) +
    userAtIdle("# slack-channel C0ALZJHGE49 in thread 1\n[slack-event C0ALZJHGE49/1794078849.171979]\nnext message", 550),
  ));
  const result = await confirm(file, new AcceptanceLedger(), cursor);
  assert.deepEqual(result, { accepted: false, reason: "pm_acceptance_unconfirmed" });
  assert.ok(file.clock.now >= 3000, "waited the full window before giving up");
});

test("an identical record written BEFORE this paste's cursor is not retrospective proof", async () => {
  const file = fakeFile(enqueue(SENT, -2000));
  const cursor = file.cursor();
  assert.equal((await confirm(file, new AcceptanceLedger(), cursor, { timeoutMs: 1000 })).accepted, false);
});

test("a LATER MANUAL COPY of the text is not delivery proof for a send that already gave up", async () => {
  const file = fakeFile();
  const ledger = new AcceptanceLedger();
  const cursor = file.cursor();
  const first = await confirm(file, ledger, cursor, { timeoutMs: 1000 });
  assert.equal(first.accepted, false);
  file.append(userAtIdle(SENT, 60_000)); // someone pastes it by hand a minute later
  // The send's window is closed; nothing re-scans on its behalf.
  const again = await confirm(file, ledger, file.cursor(SENT, T0 + 61_000), { timeoutMs: 500 });
  assert.equal(again.accepted, false, "a new paste's cursor starts after the manual copy too");
});

test("TWO DIFFERENT-KEY IDENTICAL-TEXT PASTES: one prompt record credits at most one, and only the paste it belongs to", async () => {
  const file = fakeFile();
  const ledger = new AcceptanceLedger();
  // Paste A, its own record, then paste B whose paste is lost (no second record ever appears).
  const a = file.cursor(SENT, T0);
  file.append(enqueue(SENT, 100));
  const resultA = await confirm(file, ledger, a, { timeoutMs: 1000 });
  assert.equal(resultA.accepted, true);
  const b = file.cursor(SENT, T0 + 2000); // taken inside the serialized writer AFTER A's record
  const resultB = await confirm(file, ledger, b, { timeoutMs: 1000 });
  assert.equal(resultB.accepted, false);
  assert.equal(resultB.reason, "pm_acceptance_unconfirmed");
});

test("identical text pasted while the earlier identical paste still awaits its record stays UNKNOWN (cannot be told apart)", async () => {
  const file = fakeFile();
  const ledger = new AcceptanceLedger();
  const a = file.cursor(SENT, T0);
  const openedA = ledger.open(a);
  const b = file.cursor(SENT, T0 + 500);
  const openedB = ledger.open(b); // B starts while A is unsettled
  assert.equal(openedB.overlapsIdentical, true);
  assert.equal(openedA.window.upper, b.size, "A's records end where B's paste begins");
  file.append(enqueue(SENT, 700)); // one record exists: whose is it?
  const resultB = await confirmPmAcceptance({ ledger, cursor: b, window: openedB.window, overlapsIdentical: true, recipientIntact: intact, timeoutMs: 1000, deps: file.deps });
  assert.deepEqual(resultB, { accepted: false, reason: "identical_prompt_in_flight" });
});

test("a record can be credited only once, even to a paste whose window contains it", async () => {
  const file = fakeFile();
  const ledger = new AcceptanceLedger();
  const a = file.cursor(SENT, T0);
  file.append(enqueue(SENT, 100));
  assert.equal((await confirm(file, ledger, a, { timeoutMs: 500 })).accepted, true);
  // A buggy second request reusing A's cursor still cannot claim A's record again.
  const replay = ledger.open({ ...a, size: a.size });
  const result = await confirmPmAcceptance({ ledger, cursor: a, window: replay.window, recipientIntact: intact, timeoutMs: 500, deps: file.deps });
  assert.equal(result.accepted, false);
  assert.equal(result.reason, "pm_acceptance_unconfirmed", "the claimed record is skipped, so there is nothing left to credit");
});

test("REPLACEMENT, ROTATION and TRUNCATION of the transcript leave the send UNKNOWN", async () => {
  const rotated = fakeFile();
  const rotatedCursor = rotated.cursor();
  rotated.at(200, () => { rotated.state.ino = 999; rotated.append(enqueue(SENT, 150)); });
  assert.deepEqual(await confirm(rotated, new AcceptanceLedger(), rotatedCursor), { accepted: false, reason: "transcript_replaced" });

  const swapped = fakeFile();
  const swappedCursor = swapped.cursor();
  swapped.at(200, () => { swapped.state.dev = 2; swapped.append(enqueue(SENT, 150)); });
  assert.equal((await confirm(swapped, new AcceptanceLedger(), swappedCursor)).reason, "transcript_replaced");

  const truncated = fakeFile(line({ type: "assistant", timestamp: at(-9000), message: { content: [] } }).repeat(20));
  const truncatedCursor = truncated.cursor();
  const originalIdentity = truncated.deps.identity;
  truncated.deps.identity = async (p) => ({ ...(await originalIdentity(p)), size: 10 });
  assert.deepEqual(await confirm(truncated, new AcceptanceLedger(), truncatedCursor), { accepted: false, reason: "transcript_truncated" });
});

test("a recipient replaced while the send awaited its proof is UNKNOWN even if a record exists", async () => {
  const file = fakeFile();
  const cursor = file.cursor();
  file.append(enqueue(SENT, 100));
  const ledger = new AcceptanceLedger();
  const result = await confirm(file, ledger, cursor, { recipientIntact: async () => false });
  assert.deepEqual(result, { accepted: false, reason: "recipient_changed_during_send" });
  assert.equal(ledger.isClaimed(cursor.path, cursor.size), false, "a refused acceptance does not consume the record");
});

test("without a paste cursor the answer is unconfirmed, never accepted", async () => {
  const ledger = new AcceptanceLedger();
  assert.deepEqual(await confirmPmAcceptance({ ledger, cursor: null, window: null, recipientIntact: intact, timeoutMs: 100 }),
    { accepted: false, reason: "no_paste_cursor" });
});

test("a record split across two reads is found once complete", async () => {
  const file = fakeFile();
  const cursor = file.cursor();
  const record = enqueue(SENT, 150);
  const cut = Math.floor(record.length / 2);
  file.at(200, () => file.append(record.slice(0, cut)));
  file.at(600, () => file.append(record.slice(cut)));
  assert.equal((await confirm(file, new AcceptanceLedger(), cursor, { timeoutMs: 2000 })).accepted, true);
});

test("against a real file: appended prompt is accepted; rotation and truncation are UNKNOWN", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-acceptance-"));
  try {
    const path = join(directory, "session.jsonl");
    const open = async (ledger: AcceptanceLedger) => {
      const info = statSync(path);
      const cursor: PasteCursor = { path, dev: info.dev, ino: info.ino, size: info.size, startedAtMs: Date.now(), text: SENT };
      const opened = ledger.open(cursor);
      return { cursor, ...opened };
    };
    const nowRecord = (text: string) => line({ type: "queue-operation", operation: "enqueue", timestamp: new Date().toISOString(), content: wrapped(text) });

    writeFileSync(path, nowRecord(SENT)); // identical text written BEFORE the cursor
    const early = await open(new AcceptanceLedger());
    const none = await confirmPmAcceptance({ ledger: new AcceptanceLedger(), cursor: early.cursor, window: early.window, recipientIntact: intact, timeoutMs: 400, pollMs: 20, deps: defaultAcceptanceDeps });
    assert.equal(none.accepted, false);

    const ledger = new AcceptanceLedger();
    const accepted = await open(ledger);
    setTimeout(() => appendFileSync(path, nowRecord(SENT)), 60);
    assert.equal((await confirmPmAcceptance({ ledger, cursor: accepted.cursor, window: accepted.window, recipientIntact: intact, timeoutMs: 2000, pollMs: 20, deps: defaultAcceptanceDeps })).accepted, true);

    const rotation = await open(new AcceptanceLedger());
    setTimeout(() => { renameSync(path, join(directory, "old.jsonl")); writeFileSync(path, nowRecord(SENT)); }, 60);
    const rotated = await confirmPmAcceptance({ ledger: new AcceptanceLedger(), cursor: rotation.cursor, window: rotation.window, recipientIntact: intact, timeoutMs: 2000, pollMs: 20, deps: defaultAcceptanceDeps });
    assert.deepEqual(rotated, { accepted: false, reason: "transcript_replaced" });

    writeFileSync(path, nowRecord("padding ".repeat(50)));
    const trunc = await open(new AcceptanceLedger());
    setTimeout(() => truncateSync(path, 10), 60);
    const truncated = await confirmPmAcceptance({ ledger: new AcceptanceLedger(), cursor: trunc.cursor, window: trunc.window, recipientIntact: intact, timeoutMs: 2000, pollMs: 20, deps: defaultAcceptanceDeps });
    assert.deepEqual(truncated, { accepted: false, reason: "transcript_truncated" });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
