import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { MoPDatabase } from "../src/db.js";
import type { PasteGuard } from "../src/pasteGuard.js";
import { AcceptanceLedger, type AcceptanceDeps } from "../src/pmAcceptance.js";
import { SendCustodyJournal, sendRequestDigest, sha256Hex } from "../src/sendCustody.js";
import { currentSendGuard, registerSendCustody, type PmPin } from "../src/sendCustodyRoutes.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const T0 = Date.parse("2026-11-08T19:13:54.000Z");
const COMMAND = "# slack-channel C0A in thread 1.0 | Tester | 12:43 AM\n[slack-event C0A/1794078832.227049]\nhello";
const OTHER = "# slack-channel C0A in thread 1.0 | Tester | 12:44 AM\n[slack-event C0A/1794078849.171979]\nsecond";

type World = {
  app: Hono;
  db: MoPDatabase;
  pastes: Array<{ text: string; paneId: string | undefined }>;
  events: Array<Record<string, unknown>>;
  pin: PmPin;
  /** Transcript contents by path. */
  files: Map<string, string>;
  clock: { now: number };
  /** Does the CLI record a prompt for a paste into the pinned session's transcript? */
  cliRecords: { value: boolean };
  handler: { respond: () => { status: number; body: Record<string, unknown> }; beforeWrite?: () => void; afterPaste?: () => void };
  pinResolvable: { value: boolean };
};

function world(dbPath: string): World {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
  const pin: PmPin = { paneId: "%1", session: "10", transcriptPath: "/session-10.jsonl" };
  const files = new Map<string, string>([["/session-10.jsonl", ""], ["/session-11.jsonl", ""]]);
  const clock = { now: 0 };
  const pastes: World["pastes"] = [];
  const events: Array<Record<string, unknown>> = [];
  const cliRecords = { value: true };
  const pinResolvable = { value: true };
  const handler: World["handler"] = { respond: () => ({ status: 200, body: { success: true, mode: "command", verified: true, queued: false } }) };
  const deps: AcceptanceDeps = {
    now: () => clock.now,
    sleep: async (ms) => { clock.now += ms; },
    identity: async (path) => {
      if (!files.has(path)) throw new Error("ENOENT");
      return { dev: 1, ino: path === "/session-10.jsonl" ? 100 : 101, size: Buffer.byteLength(files.get(path)!, "utf8") };
    },
    read: async (path, from, to) => Buffer.from(files.get(path) ?? "", "utf8").subarray(from, to).toString("utf8"),
  };
  const app = new Hono();
  registerSendCustody(app, {
    journal: new SendCustodyJournal({
      begin: (r, n) => db.beginSendCustody(r, n),
      finish: (s, st, re, n) => db.finishSendCustody(s, st, re, n),
      get: (s) => db.getSendCustody(s),
    }),
    ledger: new AcceptanceLedger(() => T0 + clock.now),
    parseSlot: (raw) => (/^\d+$/.test(raw) ? Number(raw) : null),
    customSlot: 0,
    resolvePin: async () => (pinResolvable.value ? { ...pin } : null),
    acceptanceTimeoutMs: 2000,
    acceptanceDeps: deps,
    wallClock: () => T0 + clock.now,
    logEvent: (slot, type, payload) => events.push({ slot, type, ...payload }),
  });
  // Stand-in for the real handler + relay: one serialized writer that honours the PasteGuard exactly
  // as TmuxRelay.injectDirectNow does (guard after load-buffer, immediately before paste-buffer).
  let writer: Promise<unknown> = Promise.resolve();
  app.post("/slots/:slotNum/send", async (c) => {
    const body = await c.req.json();
    const guard: PasteGuard | undefined = currentSendGuard();
    const outcome = handler.respond();
    if (!(outcome.status >= 200 && outcome.status < 300 && outcome.body.success === true) || outcome.body.queued === true) {
      return c.json(outcome.body, outcome.status as 200);
    }
    const job = writer.then(async () => {
      handler.beforeWrite?.();
      if (guard) {
        const verdict = await guard.beforePaste(body.command);
        if (!verdict.ok) return { status: 500, body: { success: false, reason: `pm_paste_refused_${verdict.reason}`, zero_effect: true } };
      }
      pastes.push({ text: body.command, paneId: guard?.paneId });
      // The paste lands in whatever session is current NOW, which is the point of the pin.
      if (cliRecords.value) {
        const path = pin.transcriptPath ?? "/session-10.jsonl";
        files.set(path, (files.get(path) ?? "") + JSON.stringify({
          type: "queue-operation", operation: "enqueue", timestamp: new Date(T0 + clock.now).toISOString(),
          content: `<pasted_content id="x">\n${body.command}\n</pasted_content id="x">`,
        }) + "\n");
      }
      handler.afterPaste?.();
      return outcome;
    });
    writer = job.catch(() => undefined);
    const result = await job;
    return c.json(result.body, result.status as 200);
  });
  return { app, db, pastes, events, pin, files, clock, cliRecords, handler, pinResolvable };
}

async function post(w: World, key: string | null, command = COMMAND, extra: Record<string, unknown> = {}) {
  const response = await w.app.request("/slots/0/send", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) },
    body: JSON.stringify({ command, force: true, ...extra }),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

function withWorld(run: (w: World, reopen: () => World) => Promise<void>): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-custody-routes-"));
  const path = join(directory, "mop.db");
  const worlds: World[] = [];
  const make = () => { const w = world(path); worlds.push(w); return w; };
  return run(make(), make).finally(() => {
    for (const w of worlds) { try { w.db.close(); } catch { /* closed */ } }
    rmSync(directory, { recursive: true, force: true });
  });
}

test("an unkeyed send is untouched by custody", () => withWorld(async (w) => {
  const first = await post(w, null);
  assert.equal(first.status, 200);
  assert.equal(first.body.accepted, undefined);
  assert.equal(w.events.length, 0);
  assert.equal(w.pastes[0].paneId, undefined, "no pin is imposed on an unkeyed send");
}));

test("a keyed send pastes to the PINNED pane id and is accepted only because the CLI recorded the prompt", () => withWorld(async (w) => {
  const result = await post(w, "k-ok");
  assert.equal(result.status, 200);
  assert.equal(result.body.accepted, true);
  assert.deepEqual(w.pastes, [{ text: COMMAND, paneId: "%1" }]);
  assert.equal(w.db.getSendCustody("0:k-ok")?.state, "accepted");
  assert.equal(w.db.getSendCustody("0:k-ok")?.destination, "%1|session:10");
}));

test("LIVE CASE 1794078832: pasted, tmux says verified, the CLI never records it -> UNCONFIRMED, not delivered", () => withWorld(async (w) => {
  w.cliRecords.value = false;
  const result = await post(w, "k-lost");
  assert.equal(result.status, 502);
  assert.equal(result.body.reason, "pm_acceptance_unconfirmed");
  assert.equal(result.body.safe_to_retry, false);
  assert.equal(w.pastes.length, 1);
  assert.equal(w.db.getSendCustody("0:k-lost")?.state, "ambiguous");
  const retry = await post(w, "k-lost");
  assert.equal(retry.status, 409);
  assert.equal(retry.body.reason, "send_outcome_unknown");
  assert.equal(w.pastes.length, 1);
}));

test("a different prompt with the same first/last characters does not credit the send", () => withWorld(async (w) => {
  const head = "H".repeat(260);
  const tail = "T".repeat(140);
  const sent = `${head}\nmerge it\n${tail}`;
  w.cliRecords.value = false;
  w.files.set("/session-10.jsonl", "");
  w.handler.afterPaste = () => {
    w.files.set("/session-10.jsonl", JSON.stringify({
      type: "queue-operation", operation: "enqueue", timestamp: new Date(T0 + w.clock.now).toISOString(),
      content: `<pasted_content id="x">\n${head}\nDELETE it\n${tail}\n</pasted_content id="x">`,
    }) + "\n");
  };
  const result = await post(w, "k-long", sent);
  assert.equal(result.status, 502);
  assert.equal(w.db.getSendCustody("0:k-long")?.state, "ambiguous");
}));

test("TWO DIFFERENT-KEY IDENTICAL-TEXT sends: the second paste is lost and is NOT credited with the first's record", () => withWorld(async (w) => {
  // First paste is recorded by the CLI; the second is dropped.
  let pasteCount = 0;
  w.cliRecords.value = false;
  w.handler.afterPaste = () => {
    pasteCount += 1;
    if (pasteCount === 1) {
      w.files.set("/session-10.jsonl", (w.files.get("/session-10.jsonl") ?? "") + JSON.stringify({
        type: "queue-operation", operation: "enqueue", timestamp: new Date(T0 + w.clock.now).toISOString(),
        content: `<pasted_content id="x">\n${COMMAND}\n</pasted_content id="x">`,
      }) + "\n");
    }
  };
  const [first, second] = await Promise.all([post(w, "k-a"), post(w, "k-b")]);
  assert.equal(w.pastes.length, 2);
  assert.equal(first.body.accepted, true);
  assert.equal(second.status, 502, "the lost second paste must stay UNKNOWN");
  assert.equal(w.db.getSendCustody("0:k-b")?.state, "ambiguous");
}));

test("TWO identical-text sends where only the SECOND is recorded: the first is not credited with it", () => withWorld(async (w) => {
  let pasteCount = 0;
  w.cliRecords.value = false;
  w.handler.afterPaste = () => {
    pasteCount += 1;
    if (pasteCount === 2) {
      w.files.set("/session-10.jsonl", (w.files.get("/session-10.jsonl") ?? "") + JSON.stringify({
        type: "queue-operation", operation: "enqueue", timestamp: new Date(T0 + w.clock.now).toISOString(),
        content: `<pasted_content id="x">\n${COMMAND}\n</pasted_content id="x">`,
      }) + "\n");
    }
  };
  const [first, second] = await Promise.all([post(w, "k-a"), post(w, "k-b")]);
  assert.equal(first.status, 502, "the first paste's window ends where the second paste begins");
  assert.equal(second.body.accepted === true, false, "identical prompts in flight cannot be told apart, so neither is delivered");
}));

test("a queued receipt is custody, not delivery: duplicates are answered queued", () => withWorld(async (w) => {
  w.handler.respond = () => ({ status: 200, body: { success: true, queued: true, verified: false } });
  const first = await post(w, "k-q");
  assert.equal(first.body.queued, true);
  assert.equal(first.body.accepted, undefined);
  const again = await post(w, "k-q");
  assert.deepEqual([again.body.duplicate, again.body.queued, again.body.accepted, again.body.custody_state], [true, true, false, "queued"]);
  assert.equal(w.pastes.length, 0);
}));

test("CONFLICTING payload under the same key is refused and nothing is pasted", () => withWorld(async (w) => {
  assert.equal((await post(w, "k-c")).status, 200);
  const conflict = await post(w, "k-c", COMMAND + "\nedited");
  assert.equal(conflict.status, 409);
  assert.equal(conflict.body.reason, "idempotency_key_conflict");
  assert.equal(w.pastes.length, 1);
}));

test("CHANGED recipient: an accepted key is not pasted into the replacement PM session", () => withWorld(async (w) => {
  assert.equal((await post(w, "k-r")).status, 200);
  w.pin.paneId = "%9";
  w.pin.session = "99";
  const later = await post(w, "k-r");
  assert.equal(later.status, 200);
  assert.deepEqual([later.body.duplicate, later.body.destination_changed, later.body.accepted], [true, true, true]);
  assert.equal(w.pastes.length, 1);
}));

test("REPLACEMENT DURING THE FIRST SEND, before the paste: the writer refuses, nothing is written, the retry binds the new recipient", () => withWorld(async (w) => {
  // custody began against session 10; the PM is replaced while the send waits for the writer
  w.handler.beforeWrite = () => { w.pin.session = "11"; w.pin.transcriptPath = "/session-11.jsonl"; };
  const first = await post(w, "k-mid");
  assert.equal(first.status, 500);
  assert.equal(first.body.reason, "pm_paste_refused_recipient_changed");
  assert.equal(first.body.safe_to_retry, true);
  assert.equal(w.pastes.length, 0, "nothing reached any pane");
  assert.equal(w.db.getSendCustody("0:k-mid")?.state, "refused_zero_effect");

  w.handler.beforeWrite = undefined;
  const retry = await post(w, "k-mid");
  assert.equal(retry.body.accepted, true);
  assert.deepEqual(w.pastes, [{ text: COMMAND, paneId: "%1" }]);
  assert.equal(w.db.getSendCustody("0:k-mid")?.destination, "%1|session:11");
}));

test("REPLACEMENT DURING THE FIRST SEND, pane id changed: the writer refuses", () => withWorld(async (w) => {
  w.handler.beforeWrite = () => { w.pin.paneId = "%2"; };
  const result = await post(w, "k-pane");
  assert.equal(result.body.reason, "pm_paste_refused_recipient_changed");
  assert.equal(w.pastes.length, 0);
}));

test("REPLACEMENT DURING THE FIRST SEND, after the paste: the old custody stays UNKNOWN even though a record exists", () => withWorld(async (w) => {
  w.handler.afterPaste = () => { w.pin.session = "11"; w.pin.transcriptPath = "/session-11.jsonl"; };
  const result = await post(w, "k-after");
  assert.equal(result.status, 502);
  assert.equal(result.body.detail, "recipient_changed_during_send");
  assert.equal(w.pastes.length, 1);
  assert.equal(w.db.getSendCustody("0:k-after")?.state, "ambiguous");
  assert.equal((await post(w, "k-after")).status, 409);
  assert.equal(w.pastes.length, 1);
}));

test("the pasted prompt lands in a DIFFERENT session's transcript than the pinned one: UNKNOWN", () => withWorld(async (w) => {
  // The record is written to the replacement session's file; the pinned session's file never sees it.
  w.cliRecords.value = false;
  w.handler.afterPaste = () => {
    w.files.set("/session-11.jsonl", JSON.stringify({
      type: "queue-operation", operation: "enqueue", timestamp: new Date(T0 + w.clock.now).toISOString(),
      content: `<pasted_content id="x">\n${COMMAND}\n</pasted_content id="x">`,
    }) + "\n");
  };
  const result = await post(w, "k-other-file");
  assert.equal(result.status, 502);
  assert.equal(w.db.getSendCustody("0:k-other-file")?.state, "ambiguous");
}));

test("no verifiable recipient: nothing is written or recorded, and the caller is told it may retry", () => withWorld(async (w) => {
  w.pinResolvable.value = false;
  const result = await post(w, "k-nopin");
  assert.equal(result.status, 409);
  assert.deepEqual([result.body.zero_effect, result.body.safe_to_retry], [true, true]);
  assert.equal(w.pastes.length, 0);
  assert.equal(w.db.getSendCustody("0:k-nopin"), null);
}));

test("a transcript that cannot be read keeps the send UNKNOWN", () => withWorld(async (w) => {
  w.pin.transcriptPath = "/does-not-exist.jsonl";
  const result = await post(w, "k-nofile");
  assert.equal(result.status, 502);
  assert.equal(result.body.detail, "no_paste_cursor");
}));

test("CRASH between paste and journal: after a restart the key reads UNKNOWN and is not pasted again", () => withWorld(async (w, reopen) => {
  const { digest, commandDigest } = sendRequestDigest({ command: COMMAND, force: true });
  w.db.beginSendCustody({ scope: "0:k-crash", slot: 0, key: "k-crash", digest, commandDigest, destination: "%1|session:10" }, Date.now());
  w.db.close();
  const restarted = reopen();
  const status = (await (await restarted.app.request("/slots/0/send-status?key=k-crash")).json()) as Record<string, unknown>;
  assert.deepEqual([status.state, status.reason], ["ambiguous", "crash_before_outcome"]);
  const retry = await post(restarted, "k-crash");
  assert.equal(retry.status, 409);
  assert.equal(retry.body.reason, "send_outcome_unknown");
  assert.equal(restarted.pastes.length, 0);
}));

test("an exception mid-send is recorded ambiguous, and the retry is refused", () => withWorld(async (w) => {
  w.handler.respond = () => { throw new Error("tmux hung after paste"); };
  const first = await w.app.request("/slots/0/send", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": "k-die" },
    body: JSON.stringify({ command: COMMAND, force: true }),
  });
  assert.equal(first.status, 500);
  w.handler.respond = () => ({ status: 200, body: { success: true, verified: true } });
  const retry = await post(w, "k-die");
  assert.equal(retry.status, 409);
  assert.equal(w.pastes.length, 0);
}));

test("PARTIAL effect: the handler pasted but could not confirm; the retry is refused", () => withWorld(async (w) => {
  w.handler.respond = () => ({ status: 502, body: { success: false, reason: "pm_submit_outcome_ambiguous", ambiguous: true, zero_effect: false } });
  assert.equal((await post(w, "k-p")).status, 502);
  w.handler.respond = () => ({ status: 200, body: { success: true, verified: true } });
  assert.equal((await post(w, "k-p")).status, 409);
  assert.equal(w.pastes.length, 0);
}));

test("only an affirmative zero-effect refusal is marked safe to retry, and then exactly once", () => withWorld(async (w) => {
  w.handler.respond = () => ({ status: 409, body: { success: false, reason: "pane_identity_mismatch" } });
  const refused = await post(w, "k-z");
  assert.deepEqual([refused.status, refused.body.zero_effect, refused.body.safe_to_retry], [409, true, true]);
  assert.equal(w.db.getSendCustody("0:k-z")?.state, "refused_zero_effect");
  w.handler.respond = () => ({ status: 200, body: { success: true, verified: true } });
  assert.equal((await post(w, "k-z")).body.accepted, true);
  assert.equal((await post(w, "k-z")).body.duplicate, true);
  assert.equal(w.pastes.length, 1);
  w.handler.respond = () => ({ status: 500, body: { success: false, reason: "tmux_exec_error" } });
  const generic = await post(w, "k-g");
  assert.equal(generic.body.safe_to_retry, undefined, "a generic failure is never advertised as retryable");
}));

test("send-status reports custody state and binds the claimed command digest", () => withWorld(async (w) => {
  await post(w, "k-s");
  const digest = sha256Hex(COMMAND.trim());
  const ok = (await (await w.app.request(`/slots/0/send-status?key=k-s&command_digest=${digest}`)).json()) as Record<string, unknown>;
  assert.deepEqual([ok.state, ok.command_digest_match], ["accepted", true]);
  const collision = (await (await w.app.request(`/slots/0/send-status?key=k-s&command_digest=${sha256Hex(OTHER)}`)).json()) as Record<string, unknown>;
  assert.equal(collision.command_digest_match, false);
  const missing = await w.app.request("/slots/0/send-status?key=never-sent");
  assert.equal(missing.status, 404);
}));
