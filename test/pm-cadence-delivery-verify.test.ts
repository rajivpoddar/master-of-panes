// Rajiv 2026-10-16, DM D0AMF0XE6TS thread 1792121896.574029: "why are these
// getting dropped?" The 08:13 IST hourly inject reported injected=true but never
// reached the PM transcript; the due_key was marked done and never re-sent.
// CTO REVISE (C0ALZJHGE49 thread 1792122734.658139, ts 1792123311.071369):
// only same-session user/queue acceptance verifies; incomplete or wrong-session
// evidence stays pending with no re-send; re-send is composer-fenced and never
// presses Enter unless PM idle is proven.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { PMCadenceScheduler, verifyPMCadenceDelivery } from "../src/pmCadence.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const SESSION = "4bf50d5a-45c9-4721-979d-99c2efb9f33d";
const OTHER = "0bd0fe5f-2212-4d77-9fbd-f1591f103469";
const HOURLY = "MoP: run Skill(hourly-heartbeat) now with a background agent.";
const RULE = "─".repeat(40);

class FakeMopDb {
  readonly config = new Map<string, string>();
  readonly events: string[] = [];
  getConfig(key: string): string | null {
    return this.config.get(key) || null;
  }
  setConfig(key: string, value: string): void {
    this.config.set(key, value);
  }
  logEvent(_slot: number, type: string): void {
    this.events.push(type);
  }
  hasPMQueueDelivery(): boolean {
    return false;
  }
}

type Pane = { composer: string; buffer: string; commands: string[] };

/** Real TmuxRelay fenced writer over a fake claude pane. */
function fencedRelay(pane: Pane, idle: boolean): TmuxRelay {
  const relay = new TmuxRelay(DEFAULT_CONFIG, {
    pmRuntime: "claude",
    runShell: async (command: string) => {
      pane.commands.push(command);
      if (command.includes("#{pane_id}|#{pane_current_command}")) return { stdout: "%1|claude\n", stderr: "" };
      if (command.includes("#{pane_id}")) return { stdout: "%1\n", stderr: "" };
      if (command.startsWith("tmux capture-pane")) return { stdout: `out\n${RULE}\n❯ ${pane.composer}\n${RULE}\n`, stderr: "" };
      const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(command);
      if (load) pane.buffer = readFileSync(load[1]!, "utf8");
      if (command.startsWith("tmux paste-buffer")) pane.composer += pane.buffer;
      if (command.endsWith(" C-u") || command.endsWith(" Enter")) pane.composer = "";
      return { stdout: "", stderr: "" };
    },
  });
  (relay as unknown as { pmBusy: boolean | null }).pmBusy = idle ? false : null;
  return relay;
}

class SubmitRelay {
  submissions: string[] = [];
  async submitToPM(message: string) {
    this.submissions.push(message);
    return { ok: true } as never;
  }
}

function rec(fields: Record<string, unknown>): string {
  return JSON.stringify({ sessionId: SESSION, ...fields }) + "\n";
}
const userRec = (ts: Date, content: unknown, sessionId = SESSION) =>
  rec({ type: "user", timestamp: ts.toISOString(), sessionId, message: { role: "user", content } });

async function armHourly(db: FakeMopDb): Promise<Date> {
  db.setConfig("pm_cadence_last_stop_session", SESSION);
  const cadence = new PMCadenceScheduler(db as never, new SubmitRelay() as never);
  const at = new Date();
  await cadence.runManual("hourly-heartbeat");
  return at;
}

function withDir(fn: (path: string) => Promise<void>): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), "mop-cadence-verify-"));
  return fn(join(dir, `${SESSION}.jsonl`)).finally(() => rmSync(dir, { recursive: true, force: true }));
}
const later = (ms: number) => new Date(Date.now() + ms);
const enterCount = (pane: Pane) => pane.commands.filter((c) => c.endsWith(" Enter")).length;

test("inject reported ok, missing from the same-session transcript: fenced re-send once (idle proven)", () =>
  withDir(async (path) => {
    const db = new FakeMopDb();
    const injectedAt = await armHourly(db);
    writeFileSync(path, userRec(new Date(injectedAt.getTime() - 60_000), "earlier prompt") + userRec(injectedAt, "unrelated"));
    const pane: Pane = { composer: "", buffer: "", commands: [] };
    const relay = fencedRelay(pane, true);

    await verifyPMCadenceDelivery(db as never, relay, { sessionId: SESSION, transcriptPath: path }, later(10_000), 0);
    assert.equal(enterCount(pane), 1);
    assert.ok(db.events.includes("pm_cadence_redelivered"));

    await verifyPMCadenceDelivery(db as never, relay, { sessionId: SESSION, transcriptPath: path }, later(60_000), 0);
    await verifyPMCadenceDelivery(db as never, relay, { sessionId: SESSION, transcriptPath: path }, later(90_000), 0);
    assert.equal(enterCount(pane), 1, "re-sent at most once per due_key");
    assert.ok(db.events.includes("pm_cadence_delivery_unverified"));
  }));

test("an assistant or tool-result quotation of the prompt cannot verify", () =>
  withDir(async (path) => {
    const db = new FakeMopDb();
    const injectedAt = await armHourly(db);
    writeFileSync(
      path,
      userRec(new Date(injectedAt.getTime() - 60_000), "earlier") +
        rec({ type: "assistant", timestamp: injectedAt.toISOString(), message: { role: "assistant", content: [{ type: "text", text: HOURLY }] } }) +
        userRec(injectedAt, [{ type: "tool_result", content: HOURLY }]),
    );
    const pane: Pane = { composer: "", buffer: "", commands: [] };
    await verifyPMCadenceDelivery(db as never, fencedRelay(pane, true), { sessionId: SESSION, transcriptPath: path }, later(10_000), 0);
    assert.equal(db.events.includes("pm_cadence_delivery_verified"), false);
    assert.equal(enterCount(pane), 1, "quotations are not acceptance; prompt counts as absent");
  }));

test("a same-session user record verifies with no re-send", () =>
  withDir(async (path) => {
    const db = new FakeMopDb();
    const injectedAt = await armHourly(db);
    writeFileSync(path, userRec(new Date(injectedAt.getTime() - 60_000), "earlier") + userRec(injectedAt, HOURLY));
    const pane: Pane = { composer: "", buffer: "", commands: [] };
    await verifyPMCadenceDelivery(db as never, fencedRelay(pane, true), { sessionId: SESSION, transcriptPath: path }, later(10_000), 0);
    assert.ok(db.events.includes("pm_cadence_delivery_verified"));
    assert.equal(pane.commands.length, 0);
  }));

test("accepted record outside the read tail stays pending with no re-send", () =>
  withDir(async (path) => {
    const db = new FakeMopDb();
    const injectedAt = await armHourly(db);
    // The accepted record sits before >8MB of later records, so the tail misses it.
    const filler = userRec(new Date(injectedAt.getTime() + 1_000), "x".repeat(1024 * 1024));
    writeFileSync(path, userRec(injectedAt, HOURLY) + filler.repeat(9));
    const pane: Pane = { composer: "", buffer: "", commands: [] };
    await verifyPMCadenceDelivery(db as never, fencedRelay(pane, true), { sessionId: SESSION, transcriptPath: path }, later(10_000), 0);
    assert.equal(pane.commands.length, 0);
    assert.ok(db.getConfig("pm_cadence_hourly_heartbeat_pending_verify"), "still pending");
  }));

test("a Stop from the wrong session stays pending with no re-send", () =>
  withDir(async (path) => {
    const db = new FakeMopDb();
    const injectedAt = await armHourly(db);
    const otherPath = path.replace(SESSION, OTHER);
    writeFileSync(otherPath, userRec(injectedAt, "new session prompt", OTHER));
    const pane: Pane = { composer: "", buffer: "", commands: [] };
    await verifyPMCadenceDelivery(db as never, fencedRelay(pane, true), { sessionId: OTHER, transcriptPath: otherPath }, later(10_000), 0);
    assert.equal(pane.commands.length, 0);
    assert.ok(db.getConfig("pm_cadence_hourly_heartbeat_pending_verify"), "still pending");
  }));

test("a non-idle claude pane gets no Enter; the re-send stays available", () =>
  withDir(async (path) => {
    const db = new FakeMopDb();
    const injectedAt = await armHourly(db);
    writeFileSync(path, userRec(new Date(injectedAt.getTime() - 60_000), "earlier") + userRec(injectedAt, "unrelated"));
    const pane: Pane = { composer: "", buffer: "", commands: [] };
    await verifyPMCadenceDelivery(db as never, fencedRelay(pane, false), { sessionId: SESSION, transcriptPath: path }, later(10_000), 0);
    assert.equal(enterCount(pane), 0);
    assert.equal(pane.commands.some((c) => c.startsWith("tmux paste-buffer")), false, "no paste either");
    assert.ok(db.events.includes("pm_cadence_redeliver_refused"));
    const pending = JSON.parse(db.getConfig("pm_cadence_hourly_heartbeat_pending_verify")!);
    assert.equal(pending.retried, false);
  }));
