import assert from "node:assert/strict";
import test from "node:test";

import {
  SendIdempotencyJournal,
  isConfirmedSend,
  parseIdempotencyKey,
  type SendOutcome,
} from "../src/sendIdempotency.js";

const ok: SendOutcome = { status: 200, body: { success: true, verified: true } };
const refused: SendOutcome = { status: 409, body: { success: false, reason: "pane_identity_mismatch" } };
const TTL = 6 * 60 * 60 * 1000;

test("a repeat of a confirmed key is answered with the earlier outcome and never executes again", async () => {
  const journal = new SendIdempotencyJournal(TTL);
  let executions = 0;
  const confirmed: SendOutcome[] = [];
  const first = await journal.run("0:k", async () => { executions++; return ok; }, (o) => confirmed.push(o));
  assert.deepEqual(first, { outcome: ok, duplicate: false });
  const second = await journal.run("0:k", async () => { executions++; return ok; }, (o) => confirmed.push(o));
  assert.deepEqual(second, { outcome: ok, duplicate: true });
  assert.equal(executions, 1);
  assert.equal(confirmed.length, 1);
});

test("a failed attempt is not remembered, so the same key can still be delivered", async () => {
  const journal = new SendIdempotencyJournal(TTL);
  const failed = await journal.run("0:k", async () => refused);
  assert.equal(failed.duplicate, false);
  let executions = 0;
  const retry = await journal.run("0:k", async () => { executions++; return ok; });
  assert.deepEqual(retry, { outcome: ok, duplicate: false });
  assert.equal(executions, 1);
});

test("an exception releases the key instead of wedging it", async () => {
  const journal = new SendIdempotencyJournal(TTL);
  await assert.rejects(journal.run("0:k", async () => { throw new Error("tmux down"); }), /tmux down/);
  const retry = await journal.run("0:k", async () => ok);
  assert.equal(retry.duplicate, false);
});

test("a late-arriving request waits for the running attempt and is a duplicate of it", async () => {
  const journal = new SendIdempotencyJournal(TTL);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let executions = 0;
  const original = journal.run("0:k", async () => { executions++; await gate; return ok; });
  const late = journal.run("0:k", async () => { executions++; return ok; });
  release();
  assert.deepEqual(await original, { outcome: ok, duplicate: false });
  assert.deepEqual(await late, { outcome: ok, duplicate: true });
  assert.equal(executions, 1);
});

test("a waiter takes over, once, when the running attempt fails", async () => {
  const journal = new SendIdempotencyJournal(TTL);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let executions = 0;
  const original = journal.run("0:k", async () => { executions++; await gate; return refused; });
  const waiterA = journal.run("0:k", async () => { executions++; return ok; });
  const waiterB = journal.run("0:k", async () => { executions++; return ok; });
  release();
  assert.equal((await original).outcome.status, 409);
  const results = await Promise.all([waiterA, waiterB]);
  assert.equal(executions, 2, "one failed attempt plus exactly one takeover");
  assert.deepEqual(results.map((r) => r.duplicate).sort(), [false, true]);
});

test("scopes are independent per slot and key", async () => {
  const journal = new SendIdempotencyJournal(TTL);
  await journal.run("0:a", async () => ok);
  assert.equal((await journal.run("0:b", async () => ok)).duplicate, false);
  assert.equal((await journal.run("1:a", async () => ok)).duplicate, false);
});

test("keys confirmed before a restart are recognised after hydration, and expire", async () => {
  let now = 1_000_000;
  const journal = new SendIdempotencyJournal(TTL, () => now);
  journal.hydrate([
    { scope: "0:recent", at: now - 60_000 },
    { scope: "0:ancient", at: now - TTL - 1 },
  ]);
  let executions = 0;
  const recent = await journal.run("0:recent", async () => { executions++; return ok; });
  assert.equal(recent.duplicate, true);
  assert.equal(recent.outcome.body.recovered_from_journal, true);
  assert.equal((await journal.run("0:ancient", async () => { executions++; return ok; })).duplicate, false);
  assert.equal(executions, 1);
  now += TTL + 1;
  assert.equal((await journal.run("0:recent", async () => { executions++; return ok; })).duplicate, false);
});

test("a journaling failure never turns a delivered message into an error", async () => {
  const journal = new SendIdempotencyJournal(TTL);
  const result = await journal.run("0:k", async () => ok, () => { throw new Error("db locked"); });
  assert.deepEqual(result, { outcome: ok, duplicate: false });
  assert.equal((await journal.run("0:k", async () => ok)).duplicate, true);
});

test("only a 2xx with success:true counts as confirmed", () => {
  assert.equal(isConfirmedSend(ok), true);
  assert.equal(isConfirmedSend({ status: 200, body: { success: false } }), false);
  assert.equal(isConfirmedSend({ status: 502, body: { success: true } }), false);
  assert.equal(isConfirmedSend(refused), false);
});

test("header values are untrusted", () => {
  assert.equal(parseIdempotencyKey("slack-event:C0ALZJHGE49/1794076454.036239"), "slack-event:C0ALZJHGE49/1794076454.036239");
  assert.equal(parseIdempotencyKey("  padded  "), "padded");
  assert.equal(parseIdempotencyKey(undefined), null);
  assert.equal(parseIdempotencyKey(""), null);
  assert.equal(parseIdempotencyKey("has space"), null);
  assert.equal(parseIdempotencyKey("new\nline"), null);
  assert.equal(parseIdempotencyKey("x".repeat(257)), null);
});
