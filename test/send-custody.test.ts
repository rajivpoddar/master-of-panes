import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import {
  SendCustodyJournal,
  classifySendOutcome,
  isAffirmativeZeroEffect,
  parseIdempotencyKey,
  sendRequestDigest,
  type BeginRequest,
  type CustodyStore,
  type SendOutcome,
} from "../src/sendCustody.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const accepted: SendOutcome = { status: 200, body: { success: true, verified: true, accepted: true } };
const queued: SendOutcome = { status: 200, body: { success: true, queued: true, verified: false } };
const pastedNotAccepted: SendOutcome = { status: 200, body: { success: true, verified: true } };
const paneMismatch: SendOutcome = { status: 409, body: { success: false, reason: "pane_identity_mismatch" } };
const generic500: SendOutcome = { status: 500, body: { success: false, reason: "tmux_exec_error" } };
const partialEffect: SendOutcome = {
  status: 502,
  body: { success: false, reason: "pm_submit_outcome_ambiguous", ambiguous: true, zero_effect: false },
};

function request(over: Partial<BeginRequest> = {}): BeginRequest {
  const { digest, commandDigest } = sendRequestDigest({ command: "message one", force: true });
  return { scope: "0:k1", slot: 0, key: "k1", digest, commandDigest, destination: "%1|session:10", ...over };
}

function withDb(run: (open: () => MoPDatabase, path: string) => Promise<void> | void): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-custody-test-"));
  const path = join(directory, "mop.db");
  const opened: MoPDatabase[] = [];
  const open = () => {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: path });
    opened.push(db);
    return db;
  };
  return Promise.resolve(run(open, path)).finally(() => {
    for (const db of opened) {
      try { db.close(); } catch { /* already closed */ }
    }
    rmSync(directory, { recursive: true, force: true });
  });
}

function store(db: MoPDatabase): CustodyStore {
  return {
    begin: (r, n) => db.beginSendCustody(r, n),
    finish: (s, st, re, n) => db.finishSendCustody(s, st, re, n),
    get: (s) => db.getSendCustody(s),
  };
}

test("custody is durable BEFORE any pane I/O", () => withDb(async (open) => {
  const db = open();
  const journal = new SendCustodyJournal(store(db));
  let stateDuringIo: string | undefined;
  await journal.run(request(), async () => {
    stateDuringIo = db.getSendCustody("0:k1")?.state;
    return accepted;
  });
  assert.equal(stateDuringIo, "started");
  assert.equal(db.getSendCustody("0:k1")?.state, "accepted");
}));

test("an accepted key is answered as a duplicate and never executes again", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  let executions = 0;
  await journal.run(request(), async () => { executions++; return accepted; });
  const again = await journal.run(request(), async () => { executions++; return accepted; });
  assert.equal(again.kind, "duplicate");
  assert.equal(executions, 1);
}));

test("CRASH between paste and journal: the restarted process reads UNKNOWN and never pastes again", () => withDb(async (open) => {
  // Process 1 wrote custody, pasted, and died before recording the outcome.
  const first = open();
  const decision = first.beginSendCustody(request(), Date.now());
  assert.equal(decision.kind, "execute");
  first.close();

  // Process 2 starts fresh: nothing is in flight in memory.
  const second = open();
  const journal = new SendCustodyJournal(store(second));
  let executions = 0;
  const retry = await journal.run(request(), async () => { executions++; return accepted; });
  assert.equal(retry.kind, "rejected");
  assert.equal(retry.kind === "rejected" && retry.body.reason, "send_outcome_unknown");
  assert.equal(retry.kind === "rejected" && retry.body.safe_to_retry, false);
  assert.equal(executions, 0);
  assert.equal(second.getSendCustody("0:k1")?.state, "ambiguous");
  assert.equal(second.getSendCustody("0:k1")?.reason, "crash_before_outcome");
  assert.equal(journal.lookup("0:k1")?.state, "ambiguous");
}));

test("a crashed attempt is reported ambiguous by lookup even before anyone retries", () => withDb(async (open) => {
  const first = open();
  first.beginSendCustody(request(), Date.now());
  first.close();
  const journal = new SendCustodyJournal(store(open()));
  assert.equal(journal.lookup("0:k1")?.state, "ambiguous");
}));

test("PARTIAL effect (pasted, outcome unproven) is ambiguous and is never retried", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  let executions = 0;
  const first = await journal.run(request(), async () => { executions++; return partialEffect; });
  assert.equal(first.kind, "executed");
  const retry = await journal.run(request(), async () => { executions++; return accepted; });
  assert.equal(retry.kind, "rejected");
  assert.equal(executions, 1);
  assert.equal(journal.lookup("0:k1")?.state, "ambiguous");
}));

test("a paste whose acceptance was not proven is ambiguous, not delivered", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  await journal.run(request(), async () => pastedNotAccepted);
  assert.equal(journal.lookup("0:k1")?.state, "ambiguous");
  assert.equal(journal.lookup("0:k1")?.reason, "success_without_acceptance");
}));

test("a generic failure or exception is ambiguous: only AFFIRMATIVE zero-effect may be retried", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  await journal.run(request(), async () => generic500);
  assert.equal(journal.lookup("0:k1")?.state, "ambiguous");
  const thrower = request({ scope: "0:k2", key: "k2" });
  await assert.rejects(journal.run(thrower, async () => { throw new Error("tmux hung"); }), /tmux hung/);
  assert.equal(journal.lookup("0:k2")?.state, "ambiguous");
  assert.equal((await journal.run(thrower, async () => accepted)).kind, "rejected");
}));

test("an affirmative zero-effect refusal may be retried, then is settled once", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  let executions = 0;
  await journal.run(request(), async () => { executions++; return paneMismatch; });
  assert.equal(journal.lookup("0:k1")?.state, "refused_zero_effect");
  const retry = await journal.run(request(), async () => { executions++; return accepted; });
  assert.equal(retry.kind, "executed");
  assert.equal(journal.lookup("0:k1")?.attempts, 2);
  const after = await journal.run(request(), async () => { executions++; return accepted; });
  assert.equal(after.kind, "duplicate");
  assert.equal(executions, 2);
}));

test("CONFLICTING payload under the same key is refused, whatever the prior state", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  await journal.run(request(), async () => accepted);
  const other = sendRequestDigest({ command: "a different message", force: true });
  let executions = 0;
  const conflict = await journal.run(
    request({ digest: other.digest, commandDigest: other.commandDigest }),
    async () => { executions++; return accepted; },
  );
  assert.equal(conflict.kind, "rejected");
  assert.equal(conflict.kind === "rejected" && conflict.status, 409);
  assert.equal(conflict.kind === "rejected" && conflict.body.reason, "idempotency_key_conflict");
  assert.equal(executions, 0);
  // Same text with a different force/raw flag is also a different request.
  const forced = sendRequestDigest({ command: "message one", force: false });
  assert.notEqual(forced.digest, request().digest);
  assert.equal(forced.commandDigest, request().commandDigest);
}));

test("CHANGED recipient: an accepted send is not repeated into the new PM session", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  await journal.run(request({ destination: "%1|session:10" }), async () => accepted);
  let executions = 0;
  const later = await journal.run(request({ destination: "%1|session:99" }), async () => { executions++; return accepted; });
  assert.equal(later.kind, "duplicate");
  assert.equal(later.kind === "duplicate" && later.destinationChanged, true);
  assert.equal(executions, 0);
}));

test("CHANGED recipient: an ambiguous send is not pasted into the replacement session", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  await journal.run(request({ destination: "%1|session:10" }), async () => partialEffect);
  let executions = 0;
  const later = await journal.run(request({ destination: "%2|session:11" }), async () => { executions++; return accepted; });
  assert.equal(later.kind, "rejected");
  assert.equal(executions, 0);
}));

test("CHANGED recipient: a zero-effect refusal retries against the CURRENT recipient", () => withDb(async (open) => {
  const db = open();
  const journal = new SendCustodyJournal(store(db));
  await journal.run(request({ destination: "%1|session:10" }), async () => paneMismatch);
  const retry = await journal.run(request({ destination: "%2|session:11" }), async () => accepted);
  assert.equal(retry.kind, "executed");
  assert.equal(db.getSendCustody("0:k1")?.destination, "%2|session:11");
}));

test("a queued send is held by MoP: duplicates are answered queued, never pasted", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  await journal.run(request(), async () => queued);
  const again = await journal.run(request(), async () => accepted);
  assert.equal(again.kind, "duplicate");
  assert.equal(again.kind === "duplicate" && again.row.state, "queued");
}));

test("when custody cannot be written nothing is sent", async () => {
  const broken: CustodyStore = {
    begin: () => { throw new Error("database is locked"); },
    finish: () => undefined,
    get: () => null,
  };
  const journal = new SendCustodyJournal(broken);
  let executions = 0;
  const result = await journal.run(request(), async () => { executions++; return accepted; });
  assert.equal(result.kind, "rejected");
  assert.equal(result.kind === "rejected" && result.status, 503);
  assert.equal(result.kind === "rejected" && result.body.zero_effect, true);
  assert.equal(executions, 0);
});

test("when the outcome cannot be recorded the row stays started, which reads as UNKNOWN", () => withDb(async (open) => {
  const db = open();
  const flaky: CustodyStore = { ...store(db), finish: () => { throw new Error("disk full"); } };
  const journal = new SendCustodyJournal(flaky);
  const result = await journal.run(request(), async () => accepted);
  assert.equal(result.kind, "executed");
  assert.equal(db.getSendCustody("0:k1")?.state, "started");
  const next = new SendCustodyJournal(store(db));
  assert.equal((await next.run(request(), async () => accepted)).kind, "rejected");
}));

test("a request that arrives while the first is running waits, then follows custody", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let executions = 0;
  const original = journal.run(request(), async () => { executions++; await gate; return accepted; });
  const late = journal.run(request(), async () => { executions++; return accepted; });
  assert.equal(journal.lookup("0:k1")?.state, "started");
  release();
  assert.equal((await original).kind, "executed");
  assert.equal((await late).kind, "duplicate");
  assert.equal(executions, 1);
}));

test("a waiter behind an attempt that ends ambiguous is refused, not re-pasted", () => withDb(async (open) => {
  const journal = new SendCustodyJournal(store(open()));
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let executions = 0;
  const original = journal.run(request(), async () => { executions++; await gate; return partialEffect; });
  const late = journal.run(request(), async () => { executions++; return accepted; });
  release();
  await original;
  assert.equal((await late).kind, "rejected");
  assert.equal(executions, 1);
}));

test("outcome classification", () => {
  assert.equal(classifySendOutcome(accepted).state, "accepted");
  assert.equal(classifySendOutcome(queued).state, "queued");
  assert.equal(classifySendOutcome(pastedNotAccepted).state, "ambiguous");
  assert.equal(classifySendOutcome(paneMismatch).state, "refused_zero_effect");
  assert.equal(classifySendOutcome(generic500).state, "ambiguous");
  assert.equal(classifySendOutcome({ status: 500, body: { success: false, zero_effect: true } }).state, "refused_zero_effect");
  assert.equal(classifySendOutcome({ status: 200, body: { success: false, zero_effect: true } }).state, "ambiguous");
  assert.equal(isAffirmativeZeroEffect({ status: 502, body: { reason: "pm_acceptance_unconfirmed" } }), false);
});

test("keys are untrusted header values", () => {
  assert.equal(parseIdempotencyKey("slack-event:C0ALZJHGE49/1794076454.036239"), "slack-event:C0ALZJHGE49/1794076454.036239");
  assert.equal(parseIdempotencyKey("has space"), null);
  assert.equal(parseIdempotencyKey("x".repeat(257)), null);
  assert.equal(parseIdempotencyKey(undefined), null);
});

test("custody rows are pruned by age", () => withDb(async (open) => {
  const db = open();
  db.beginSendCustody(request({ scope: "0:old", key: "old" }), 1_000);
  db.beginSendCustody(request({ scope: "0:new", key: "new" }), 9_000);
  assert.equal(db.pruneSendCustody(5_000), 1);
  assert.equal(db.getSendCustody("0:old"), null);
  assert.notEqual(db.getSendCustody("0:new"), null);
}));
