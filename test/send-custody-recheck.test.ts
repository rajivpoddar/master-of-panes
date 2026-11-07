import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { MoPDatabase } from "../src/db.js";
import { AcceptanceLedger, type AcceptanceDeps } from "../src/pmAcceptance.js";
import { TmuxRelay } from "../src/relay.js";
import { SendCustodyJournal } from "../src/sendCustody.js";
import { currentSendGuard, registerSendCustody, type PmPin } from "../src/sendCustodyRoutes.js";
import { DEFAULT_CONFIG } from "../src/types.js";

/**
 * The recipient pin must hold over the WHOLE write sequence, not just at its start. These tests run
 * the real route middleware (src/sendCustodyRoutes.ts) against the real serialized writer
 * (TmuxRelay.injectDirectNow); only tmux and the PM transcript are stand-ins. The "successor" is a
 * new PM session (a SessionStart that changes the session token, or a different pane) appearing
 * in one of the two gaps the CTO found:
 *   1. beforePaste awaits the transcript identity, and a SessionStart can land during that await.
 *   2. paste-buffer is followed by an awaited dwell and then Enter.
 * In neither gap may a key reach the successor's pane.
 */

const T0 = Date.parse("2026-11-08T19:13:54.000Z");
const TEXT = "# slack-channel C0A\n[slack-event C0A/1.1]\nhello";

type Shell = ConstructorParameters<typeof TmuxRelay>[1] extends infer D ? D extends { runShell?: infer R } ? R : never : never;

type World = {
  app: Hono;
  db: MoPDatabase;
  /** Every tmux command, in order. */
  tmux: string[];
  pin: PmPin;
  pinResolvable: { value: boolean };
  /** Runs inside the transcript identity read (gap 1). One-shot. */
  duringIdentity: { run?: () => void };
  /** Runs when paste-buffer is invoked, so the change lands in the paste -> Enter dwell (gap 2). One-shot. */
  duringPaste: { run?: () => void };
};

const keys = (w: World): string[] => w.tmux.filter((c) => c.startsWith("tmux send-keys"));
const pastes = (w: World): string[] => w.tmux.filter((c) => c.startsWith("tmux paste-buffer"));

function world(dbPath: string): World {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
  const pin: PmPin = { paneId: "%1", session: "10", transcriptPath: "/session-10.jsonl" };
  const files = new Map<string, string>([["/session-10.jsonl", ""], ["/session-11.jsonl", ""]]);
  const clock = { now: 0 };
  const tmux: string[] = [];
  const pinResolvable = { value: true };
  const duringIdentity: World["duringIdentity"] = {};
  const duringPaste: World["duringPaste"] = {};

  const runShell = (async (command: string) => {
    tmux.push(command);
    if (command.startsWith("tmux paste-buffer")) {
      // The CLI records the prompt in whichever session's transcript is current when it lands.
      const path = pin.transcriptPath ?? "/session-10.jsonl";
      files.set(path, (files.get(path) ?? "") + JSON.stringify({
        type: "queue-operation", operation: "enqueue", timestamp: new Date(T0 + clock.now).toISOString(),
        content: `<pasted_content id="x">\n${TEXT}\n</pasted_content id="x">`,
      }) + "\n");
      const hook = duringPaste.run;
      duringPaste.run = undefined;
      hook?.();
    }
    return { stdout: "", stderr: "" };
  }) as unknown as Shell;
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell, pmRuntime: "claude", pmPaneReady: async () => true });
  relay.drainPMQueue = async () => 0;

  const deps: AcceptanceDeps = {
    now: () => clock.now,
    sleep: async (ms) => { clock.now += ms; },
    identity: async (path) => {
      const hook = duringIdentity.run;
      duringIdentity.run = undefined;
      hook?.();
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
    logEvent: () => undefined,
  });
  // Mirrors the PM branch of POST /slots/:n/send in src/server.ts: submit through the relay with the
  // request's PasteGuard and map the result to the same refusal shapes.
  app.post("/slots/:slotNum/send", async (c) => {
    const body = await c.req.json();
    const submitted = await relay.submitToPM(body.command, undefined, currentSendGuard());
    if (!submitted.ok) {
      return c.json({
        success: false,
        reason: submitted.refused
          ? `pm_paste_refused_${submitted.refused}`
          : submitted.withheld
            ? `pm_submit_withheld_${submitted.withheld}`
            : submitted.ambiguous ? "pm_submit_outcome_ambiguous" : "tmux_exec_error",
        ambiguous: submitted.ambiguous === true,
        zero_effect: submitted.ambiguous !== true,
      }, submitted.ambiguous ? 502 : 500);
    }
    return c.json({ success: true, mode: "command", verified: !submitted.queued, queued: submitted.queued === true });
  });
  return { app, db, tmux, pin, pinResolvable, duringIdentity, duringPaste };
}

async function post(w: World, key: string) {
  const response = await w.app.request("/slots/0/send", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: JSON.stringify({ command: TEXT, force: true }),
  });
  return { status: response.status, body: (await response.json()) as Record<string, unknown> };
}

function withWorld(run: (w: World) => Promise<void>): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-custody-recheck-"));
  const w = world(join(directory, "mop.db"));
  return run(w).finally(() => {
    try { w.db.close(); } catch { /* closed */ }
    rmSync(directory, { recursive: true, force: true });
  });
}

/** The ways a successor can appear: a new session (SessionStart), a different pane, or an unverifiable pane. */
const SUCCESSORS: Array<[string, (w: World) => void]> = [
  ["a new SessionStart (session token changes)", (w) => { w.pin.session = "11"; w.pin.transcriptPath = "/session-11.jsonl"; }],
  ["a different pane id", (w) => { w.pin.paneId = "%2"; }],
  ["a recipient that can no longer be verified", (w) => { w.pinResolvable.value = false; }],
];

test("control: with a stable recipient the paste and then Enter go to the pinned pane and the send is accepted", () => withWorld(async (w) => {
  const result = await post(w, "k-ok");
  assert.equal(result.status, 200);
  assert.equal(result.body.accepted, true);
  assert.equal(pastes(w).length, 1);
  assert.deepEqual(keys(w).map((c) => c.replace(/\s+/g, " ")), ["tmux send-keys -t %1 Enter"]);
  assert.equal(w.db.getSendCustody("0:k-ok")?.state, "accepted");
}));

for (const [name, change] of SUCCESSORS) {
  test(`GAP 1 (beforePaste await): ${name} during the transcript identity read -> refused with zero effect, the successor gets NO paste and NO Enter`, () => withWorld(async (w) => {
    w.duringIdentity.run = () => change(w);
    const result = await post(w, "k-gap1");
    assert.equal(result.status, 500);
    assert.equal(result.body.reason, "pm_paste_refused_recipient_changed");
    assert.equal(result.body.safe_to_retry, true);
    assert.equal(pastes(w).length, 0, "nothing was pasted into the successor");
    assert.deepEqual(keys(w), [], "no Enter (or any key) was sent to the successor");
    assert.ok(w.tmux.some((c) => c.startsWith("tmux delete-buffer")), "the loaded buffer was discarded");
    assert.equal(w.db.getSendCustody("0:k-gap1")?.state, "refused_zero_effect");
  }));

  test(`GAP 2 (paste -> Enter dwell): ${name} after the paste -> NO Enter is sent, the send stays UNKNOWN and is never resent`, () => withWorld(async (w) => {
    w.duringPaste.run = () => change(w);
    const result = await post(w, "k-gap2");
    assert.equal(result.status, 502);
    assert.equal(result.body.reason, "pm_submit_withheld_recipient_changed");
    assert.deepEqual([result.body.ambiguous, result.body.zero_effect], [true, false]);
    assert.notEqual(result.body.safe_to_retry, true, "a paste may have landed, so it is never advertised as retryable");
    assert.equal(pastes(w).length, 1);
    assert.deepEqual(keys(w), [], "no Enter (or any key) was sent after the recipient changed");
    const row = w.db.getSendCustody("0:k-gap2");
    assert.deepEqual([row?.state, row?.reason], ["ambiguous", "pm_submit_withheld_recipient_changed"]);

    // Retrying the same key is refused (once the pane can be verified again): no second paste, no Enter, ever.
    w.pinResolvable.value = true;
    const before = w.tmux.length;
    const retry = await post(w, "k-gap2");
    assert.equal(retry.status, 409);
    assert.equal(retry.body.reason, "send_outcome_unknown");
    assert.equal(w.tmux.length, before, "the retry issued no tmux command at all");
  }));
}
