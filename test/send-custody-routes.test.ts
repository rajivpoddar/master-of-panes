import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { MoPDatabase } from "../src/db.js";
import type { AcceptanceDeps } from "../src/pmAcceptance.js";
import { SendCustodyJournal, sendRequestDigest, sha256Hex } from "../src/sendCustody.js";
import { registerSendCustody } from "../src/sendCustodyRoutes.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const COMMAND = "# slack-channel C0A in thread 1.0 | Tester | 12:43 AM\n[slack-event C0A/1794078832.227049]\nhello";
const promptRecord = (text: string) =>
  JSON.stringify({ type: "queue-operation", operation: "enqueue", content: `<pasted_content id="x">\n${text}\n</pasted_content id="x">` }) + "\n";

type World = {
  app: Hono;
  db: MoPDatabase;
  pastes: string[];
  events: Array<Record<string, unknown>>;
  setDestination: (value: string) => void;
  /** What the stand-in pane does after a paste: the CLI records the prompt (true) or drops it (false). */
  cliRecordsPrompt: { value: boolean };
  handler: { respond: () => { status: number; body: Record<string, unknown> } };
};

function world(dbPath: string, existingDb?: MoPDatabase): World {
  const db = existingDb ?? new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
  const pastes: string[] = [];
  const events: Array<Record<string, unknown>> = [];
  let destination = "%1|session:10";
  const cliRecordsPrompt = { value: true };
  let transcript = "";
  let now = 0;
  const deps: AcceptanceDeps = {
    now: () => now,
    sleep: async (ms) => { now += ms; },
    size: async () => transcript.length,
    read: async (_p, from, to) => transcript.slice(from, to),
  };
  const handler = { respond: () => ({ status: 200, body: { success: true, mode: "command", verified: true, queued: false } as Record<string, unknown> }) };
  const app = new Hono();
  registerSendCustody(app, {
    journal: new SendCustodyJournal({
      begin: (r, n) => db.beginSendCustody(r, n),
      finish: (s, st, re, n) => db.finishSendCustody(s, st, re, n),
      get: (s) => db.getSendCustody(s),
    }),
    parseSlot: (raw) => (/^\d+$/.test(raw) ? Number(raw) : null),
    customSlot: 0,
    destination: async () => destination,
    transcriptPath: () => "/transcript.jsonl",
    acceptanceTimeoutMs: 2000,
    acceptanceDeps: deps,
    logEvent: (slot, type, payload) => events.push({ slot, type, ...payload }),
  });
  app.post("/slots/:slotNum/send", async (c) => {
    const body = await c.req.json();
    const outcome = handler.respond();
    if (outcome.status >= 200 && outcome.status < 300 && outcome.body.success === true && outcome.body.queued !== true) {
      pastes.push(body.command);
      if (cliRecordsPrompt.value) transcript += promptRecord(body.command);
    }
    return c.json(outcome.body, outcome.status as 200);
  });
  return { app, db, pastes, events, setDestination: (v) => { destination = v; }, cliRecordsPrompt, handler };
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
}));

test("a keyed send is accepted only because the CLI recorded the prompt", () => withWorld(async (w) => {
  const result = await post(w, "k-ok");
  assert.equal(result.status, 200);
  assert.equal(result.body.accepted, true);
  assert.equal(w.db.getSendCustody("0:k-ok")?.state, "accepted");
}));

test("LIVE CASE 1794078832: pasted, tmux says verified, the CLI never records it -> UNCONFIRMED, not delivered", () => withWorld(async (w) => {
  w.cliRecordsPrompt.value = false;
  const result = await post(w, "k-lost");
  assert.equal(result.status, 502);
  assert.equal(result.body.reason, "pm_acceptance_unconfirmed");
  assert.equal(result.body.success, false);
  assert.equal(result.body.safe_to_retry, false);
  assert.equal(w.pastes.length, 1);
  assert.equal(w.db.getSendCustody("0:k-lost")?.state, "ambiguous");
  // The retry the old design would have made is refused: no second paste.
  const retry = await post(w, "k-lost");
  assert.equal(retry.status, 409);
  assert.equal(retry.body.reason, "send_outcome_unknown");
  assert.equal(w.pastes.length, 1);
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
  w.setDestination("%9|session:99");
  const later = await post(w, "k-r");
  assert.equal(later.status, 200);
  assert.deepEqual([later.body.duplicate, later.body.destination_changed, later.body.accepted], [true, true, true]);
  assert.equal(w.pastes.length, 1);
}));

test("CRASH between paste and journal: after a restart the key reads UNKNOWN and is not pasted again", () => withWorld(async (w, reopen) => {
  // Process 1 wrote custody, pasted, and died before it could record any outcome.
  const { digest, commandDigest } = sendRequestDigest({ command: COMMAND, force: true });
  w.db.beginSendCustody({ scope: "0:k-crash", slot: 0, key: "k-crash", digest, commandDigest, destination: "%1|session:10" }, Date.now());
  w.db.close();

  const restarted = reopen();
  const status = (await (await restarted.app.request("/slots/0/send-status?key=k-crash")).json()) as Record<string, unknown>;
  assert.deepEqual([status.state, status.reason], ["ambiguous", "crash_before_outcome"]);

  restarted.handler.respond = () => ({ status: 200, body: { success: true, verified: true } });
  const retry = await post(restarted, "k-crash");
  assert.equal(retry.status, 409);
  assert.equal(retry.body.reason, "send_outcome_unknown");
  assert.equal(retry.body.safe_to_retry, false);
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
  assert.equal(retry.body.reason, "send_outcome_unknown");
  assert.equal(w.pastes.length, 0);
}));

test("PARTIAL effect: the handler pasted but could not confirm; the retry is refused", () => withWorld(async (w) => {
  w.handler.respond = () => ({
    status: 502,
    body: { success: false, reason: "pm_submit_outcome_ambiguous", ambiguous: true, zero_effect: false },
  });
  assert.equal((await post(w, "k-p")).status, 502);
  w.handler.respond = () => ({ status: 200, body: { success: true, verified: true } });
  const retry = await post(w, "k-p");
  assert.equal(retry.status, 409);
  assert.equal(w.pastes.length, 0);
}));

test("only an affirmative zero-effect refusal may be retried, and then exactly once", () => withWorld(async (w) => {
  w.handler.respond = () => ({ status: 409, body: { success: false, reason: "pane_identity_mismatch" } });
  assert.equal((await post(w, "k-z")).status, 409);
  assert.equal(w.db.getSendCustody("0:k-z")?.state, "refused_zero_effect");
  w.handler.respond = () => ({ status: 200, body: { success: true, verified: true } });
  assert.equal((await post(w, "k-z")).body.accepted, true);
  assert.equal((await post(w, "k-z")).body.duplicate, true);
  assert.equal(w.pastes.length, 1);
}));

test("send-status reports custody state and binds the claimed command digest", () => withWorld(async (w) => {
  await post(w, "k-s");
  const digest = sha256Hex(COMMAND.trim());
  const ok = (await (await w.app.request(`/slots/0/send-status?key=k-s&command_digest=${digest}`)).json()) as Record<string, unknown>;
  assert.deepEqual([ok.state, ok.command_digest_match], ["accepted", true]);
  const collision = (await (await w.app.request(`/slots/0/send-status?key=k-s&command_digest=${sha256Hex("other text")}`)).json()) as Record<string, unknown>;
  assert.equal(collision.command_digest_match, false);
  const missing = await w.app.request("/slots/0/send-status?key=never-sent");
  assert.equal(missing.status, 404);
  assert.equal(((await missing.json()) as Record<string, unknown>).state, "unknown_key");
}));
