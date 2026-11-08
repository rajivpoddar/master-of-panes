import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import Database from "better-sqlite3";
import { Hono } from "hono";

import {
  advanceByWholeIntervals,
  AlertReminderScheduler,
  AlertReminderStore,
  ensureAlertReminderSchema,
  normalizeAddRequest,
  readAlertsToken,
  registerAlertReminderRoutes,
  retryBackoffMs,
  SlackAlertsPoster,
  type AlertPoster,
  type AlertPostResult,
  type ObligationState,
} from "../src/alertReminders.js";
import { MoPDatabase } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

const T0 = Date.parse("2026-11-08T10:00:00.000Z");
const MIN = 60_000;
const HOUR = 60 * MIN;
const THREAD = "1793953549.727149";

function memoryStore(): { store: AlertReminderStore; db: Database.Database } {
  const db = new Database(":memory:");
  ensureAlertReminderSchema(db);
  return { store: new AlertReminderStore(db), db };
}

function request(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    key: "k1",
    thread_ts: THREAD,
    text: "recurring check",
    interval_s: 1800,
    until_at: new Date(T0 + 6 * HOUR).toISOString(),
    ...overrides,
  };
}

function normalized(overrides: Record<string, unknown> = {}, nowMs = T0) {
  const result = normalizeAddRequest(request(overrides), nowMs);
  assert.ok(result.ok, result.ok ? "" : result.error);
  return result.value;
}

class FakePoster implements AlertPoster {
  readonly calls: Array<{ channel: string; threadTs: string; text: string }> = [];
  results: AlertPostResult[] = [];
  configured = true;
  /** Runs while the send is "in flight" (after the call is recorded, before it resolves): lets a test cancel/replace mid-batch. */
  whileInFlight: (callNumber: number) => void = () => undefined;
  tokenConfigured(): boolean {
    return this.configured;
  }
  async post(channel: string, threadTs: string, text: string): Promise<AlertPostResult> {
    this.calls.push({ channel, threadTs, text });
    await Promise.resolve();
    this.whileInFlight(this.calls.length);
    return this.results.shift() ?? { ok: true, ts: `1794000000.${String(this.calls.length).padStart(6, "0")}` };
  }
}

function schedulerFixture(clock: { now: number }, obligationState?: (id: number) => ObligationState) {
  const { store, db } = memoryStore();
  const poster = new FakePoster();
  const events: Array<{ type: string; payload: Record<string, unknown> }> = [];
  const scheduler = new AlertReminderScheduler({
    store,
    poster,
    obligationState,
    now: () => clock.now,
    logEvent: (type, payload) => events.push({ type, payload }),
  });
  return { store, db, poster, scheduler, events };
}

// ─── pure helpers ────────────────────────────────────────

test("advanceByWholeIntervals moves to the next grid point strictly after now", () => {
  const interval = 30 * MIN;
  assert.equal(advanceByWholeIntervals(T0 + 5 * MIN, interval, T0), T0 + 5 * MIN, "future stays");
  assert.equal(advanceByWholeIntervals(T0, interval, T0), T0 + interval, "due now moves one interval");
  assert.equal(advanceByWholeIntervals(T0, interval, T0 + 5 * HOUR + 3 * MIN), T0 + 5 * HOUR + 30 * MIN, "stays on the original grid after downtime");
});

test("retry backoff doubles from one minute and caps at ten minutes", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 9].map(retryBackoffMs), [60_000, 120_000, 240_000, 480_000, 600_000, 600_000]);
});

test("normalizeAddRequest enforces key, interval floor, mandatory until and the 7 day horizon", () => {
  const ok = normalized();
  assert.equal(ok.channel, "C0ALZJHGE49");
  assert.equal(ok.first_at, T0 + 30 * MIN, "first post defaults to one interval from now");

  const bad = (overrides: Record<string, unknown>) => {
    const result = normalizeAddRequest(request(overrides), T0);
    assert.equal(result.ok, false, JSON.stringify(overrides));
    return result.ok ? "" : result.error;
  };
  assert.match(bad({ interval_s: 599 }), /at least 600/);
  assert.match(bad({ until_at: undefined }), /until_at is required/);
  assert.match(bad({ until_at: new Date(T0 - MIN).toISOString() }), /future/);
  assert.match(bad({ until_at: new Date(T0 + 7 * 24 * HOUR + MIN).toISOString() }), /within 7 days/);
  assert.match(bad({ key: "has space" }), /key must match/);
  assert.match(bad({ thread_ts: "abc" }), /thread_ts/);
  assert.match(bad({ text: "   " }), /text is required/);
  assert.match(bad({ channel: "general" }), /channel/);
  assert.match(bad({ stop_obligation_id: 0 }), /positive integer/);
  assert.match(bad({ interval_s: 3600, until_at: new Date(T0 + 30 * MIN).toISOString() }), /after until_at/);
  assert.equal(normalizeAddRequest(request({ until_at: new Date(T0 + 7 * 24 * HOUR).toISOString() }), T0).ok, true, "exactly 7 days is allowed");
});

// ─── store: idempotent add ───────────────────────────────

test("add is idempotent on key: same args no-op, different args refused, --replace overwrites", () => {
  const { store } = memoryStore();
  const first = store.add(normalized(), T0);
  assert.ok(first.ok);
  assert.equal(first.outcome, "created");

  const again = store.add(normalized(), T0 + MIN);
  assert.ok(again.ok);
  assert.equal(again.outcome, "noop");
  assert.equal(again.row.next_at, first.row.next_at, "no-op leaves the schedule alone");

  const conflict = store.add(normalized({ text: "different", interval_s: 3600 }), T0 + MIN);
  assert.equal(conflict.ok, false);
  if (!conflict.ok) {
    assert.equal(conflict.status, 409);
    assert.deepEqual(conflict.differs?.sort(), ["interval_s", "text"]);
  }
  assert.equal(store.get("k1")?.text, "recurring check", "refused add changed nothing");

  const replaced = store.add(normalized({ text: "different", replace: true }), T0 + MIN);
  assert.ok(replaced.ok);
  assert.equal(replaced.outcome, "replaced");
  assert.equal(store.get("k1")?.text, "different");
});

test("a relative --until is not part of the idempotency comparison, an absolute one is", () => {
  const { store } = memoryStore();
  assert.ok(store.add(normalized({ until_relative: true }), T0).ok);
  const later = normalized({ until_relative: true, until_at: new Date(T0 + 7 * HOUR).toISOString() }, T0 + HOUR);
  const rerun = store.add(later, T0 + HOUR);
  assert.ok(rerun.ok && rerun.outcome === "noop", "relative until re-run is a no-op");

  const absolute = store.add(normalized({ until_relative: false, until_at: new Date(T0 + 8 * HOUR).toISOString() }, T0 + HOUR), T0 + HOUR);
  assert.equal(absolute.ok, false, "a different absolute until is a different arg");
  if (!absolute.ok) assert.deepEqual(absolute.differs, ["until_at"]);
});

test("adding over a stopped or expired key revives it as a fresh schedule", () => {
  const { store } = memoryStore();
  store.add(normalized(), T0);
  const stopped = store.stop("k1", "cancelled", T0 + MIN);
  assert.equal(stopped?.row.status, "stopped");
  assert.equal(store.stop("k1", "cancelled", T0 + 2 * MIN)?.changed, false, "cancel is idempotent");
  const revived = store.add(normalized({ text: "again" }, T0 + 5 * MIN), T0 + 5 * MIN);
  assert.ok(revived.ok);
  assert.equal(revived.outcome, "revived");
  assert.equal(revived.row.status, "active");
  assert.equal(revived.row.post_count, 0);
  assert.equal(store.stop("missing", "cancelled", T0), null);
});

test("stop_obligation_id is verified only when a write happens", () => {
  const { store } = memoryStore();
  const closed = store.add(normalized({ stop_obligation_id: 77 }), T0, () => ({ error: "stop_obligation_not_open", status: 400 }));
  assert.equal(closed.ok, false);
  assert.equal(store.get("k1"), null, "nothing written when the obligation check fails");

  assert.ok(store.add(normalized({ stop_obligation_id: 77 }), T0, () => null).ok);
  let calls = 0;
  const noop = store.add(normalized({ stop_obligation_id: 77 }), T0 + MIN, () => {
    calls += 1;
    return { error: "stop_obligation_not_open", status: 400 };
  });
  assert.ok(noop.ok && noop.outcome === "noop");
  assert.equal(calls, 0, "a no-op retry does not re-verify the obligation");
});

// ─── scheduler ───────────────────────────────────────────

test("tick posts a due row once, records the Slack ts, and advances by one interval", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler, events } = schedulerFixture(clock);
  store.add(normalized(), T0);

  clock.now = T0 + 29 * MIN;
  assert.equal((await scheduler.tick()).posted.length, 0, "not due yet");

  clock.now = T0 + 30 * MIN + 5_000;
  const result = await scheduler.tick();
  assert.deepEqual(result.posted, ["k1"]);
  assert.deepEqual(poster.calls, [{ channel: "C0ALZJHGE49", threadTs: THREAD, text: "recurring check" }]);
  const row = store.get("k1")!;
  assert.equal(row.post_count, 1);
  assert.match(row.last_post_ts ?? "", /^1794000000\./);
  assert.equal(row.next_at, T0 + 60 * MIN);
  assert.equal(row.status, "active");
  assert.equal(events.filter((e) => e.type === "alert_reminder_posted").length, 1);

  clock.now = T0 + 30 * MIN + 35_000;
  assert.equal((await scheduler.tick()).posted.length, 0, "a second tick in the same window does not repost");
  assert.equal(poster.calls.length, 1);
});

test("downtime never causes a burst: one post, next_at lands back on the original grid", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler } = schedulerFixture(clock);
  store.add(normalized({ interval_s: 600, until_at: new Date(T0 + 12 * HOUR).toISOString() }), T0);

  clock.now = T0 + 5 * HOUR + 3 * MIN; // MoP was down for ~5 hours: 30 missed ticks
  const result = await scheduler.tick();
  assert.deepEqual(result.posted, ["k1"]);
  assert.equal(poster.calls.length, 1, "exactly one catch-up post");
  assert.equal(store.get("k1")!.next_at, T0 + 5 * HOUR + 10 * MIN, "next due is the first grid point after now");

  assert.equal((await scheduler.tick()).posted.length, 0);
  assert.equal(poster.calls.length, 1);
});

test("the last scheduled post expires the row, and an elapsed until never posts late", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler } = schedulerFixture(clock);
  store.add(normalized({ interval_s: 600, until_at: new Date(T0 + 11 * MIN).toISOString() }), T0);

  clock.now = T0 + 10 * MIN + 1_000;
  const result = await scheduler.tick();
  assert.deepEqual(result.posted, ["k1"]);
  const row = store.get("k1")!;
  assert.equal(row.status, "expired");
  assert.equal(row.stop_reason, "schedule_complete");

  // second row whose window closed while MoP was down: no late post
  store.add(normalized({ key: "late", interval_s: 600, until_at: new Date(T0 + 40 * MIN).toISOString() }, clock.now), clock.now);
  clock.now += 3 * HOUR;
  const callsBefore = poster.calls.length;
  const second = await scheduler.tick();
  assert.deepEqual(second.expired, ["late"]);
  assert.equal(poster.calls.length, callsBefore);
  assert.equal(store.get("late")!.stop_reason, "until_passed");
});

test("a zero-effect Slack failure is recorded, the due time is kept, and the retry backs off then recovers", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler, events } = schedulerFixture(clock);
  store.add(normalized({ interval_s: 600, until_at: new Date(T0 + 6 * HOUR).toISOString() }), T0);

  clock.now = T0 + 10 * MIN;
  poster.results = [{ ok: false, error: "slack_not_in_channel", effect: "none" }];
  const failed = await scheduler.tick();
  assert.deepEqual(failed.failed, ["k1"]);
  let row = store.get("k1")!;
  assert.equal(row.last_error, "slack_not_in_channel");
  assert.equal(row.last_outcome, "failed");
  assert.equal(row.unknown_count, 0, "a zero-effect failure is not an unknown delivery");
  assert.equal(row.fail_count, 1);
  assert.equal(row.next_at, T0 + 10 * MIN, "still due: failure restores the claimed time");
  assert.equal(row.retry_at, clock.now + 60_000);
  assert.equal(row.post_count, 0);
  assert.equal(row.status, "active");
  assert.ok(events.some((e) => e.type === "alert_reminder_post_failed"));

  clock.now += 30_000;
  assert.equal((await scheduler.tick()).claimed, 0, "inside the backoff window nothing is retried");

  clock.now += 31_000;
  const recovered = await scheduler.tick();
  assert.deepEqual(recovered.posted, ["k1"]);
  row = store.get("k1")!;
  assert.equal(row.last_error, null);
  assert.equal(row.last_outcome, "posted");
  assert.equal(row.fail_count, 0);
  assert.equal(row.retry_at, null);
  assert.equal(row.post_count, 1);
  assert.ok(row.next_at > clock.now);
});

test("a resolved stop obligation stops the reminder before it posts", async () => {
  const clock = { now: T0 };
  let state: ObligationState = "open";
  const { store, poster, scheduler } = schedulerFixture(clock, () => state);
  store.add(normalized({ stop_obligation_id: 55 }), T0, () => null);

  clock.now = T0 + 30 * MIN + 1_000;
  assert.deepEqual((await scheduler.tick()).posted, ["k1"], "open obligation keeps posting");

  state = "closed";
  clock.now = T0 + 60 * MIN + 1_000;
  const result = await scheduler.tick();
  assert.deepEqual(result.stopped, ["k1"]);
  assert.equal(result.posted.length, 0);
  assert.equal(poster.calls.length, 1);
  assert.equal(store.get("k1")!.status, "stopped");
  assert.equal(store.get("k1")!.stop_reason, "obligation_resolved");
});

test("an unreadable or missing obligation never stops a reminder", async () => {
  const clock = { now: T0 };
  let state: ObligationState = "unknown";
  const { store, scheduler } = schedulerFixture(clock, () => state);
  store.add(normalized({ stop_obligation_id: 55 }), T0, () => null);
  clock.now = T0 + 30 * MIN + 1_000;
  assert.equal((await scheduler.tick()).posted.length, 1);
  state = "missing";
  clock.now = T0 + 60 * MIN + 1_000;
  assert.equal((await scheduler.tick()).posted.length, 1);
  assert.equal(store.get("k1")!.status, "active");
});

test("cancelled rows never post", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler } = schedulerFixture(clock);
  store.add(normalized(), T0);
  store.stop("k1", "cancelled", T0 + MIN);
  clock.now = T0 + 2 * HOUR;
  assert.equal((await scheduler.tick()).claimed, 0);
  assert.equal(poster.calls.length, 0);
});

test("a throwing poster is an UNKNOWN delivery, cannot break the tick loop, and does not strand the rest of the batch", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler, events } = schedulerFixture(clock);
  store.add(normalized({ key: "a" }), T0);
  store.add(normalized({ key: "b" }), T0);
  let calls = 0;
  poster.post = async () => {
    calls += 1;
    if (calls === 1) throw new Error("boom");
    return { ok: true, ts: "1794000000.000777" };
  };
  clock.now = T0 + 31 * MIN;
  const result = await scheduler.tick();
  assert.deepEqual(result.unknown, ["a"]);
  assert.deepEqual(result.posted, ["b"], "the next claim in the batch still goes out");
  const a = store.get("a")!;
  assert.equal(a.last_outcome, "unknown");
  assert.equal(a.last_unknown_reason, "poster_threw");
  assert.equal(a.retry_at, null);
  assert.ok(events.some((e) => e.type === "alert_reminder_post_unknown"));
  assert.equal(scheduler.getStatus().ticking, false, "tick guard released");
});

// ─── Slack transport ─────────────────────────────────────

test("SlackAlertsPoster reads the token at send time, posts as chat.postMessage, and never leaks it", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-alerts-env-"));
  try {
    const envFile = join(directory, ".env.local");
    writeFileSync(envFile, "SLACK_BOT_TOKEN=pm-token\nSLACK_ALERTS_BOT_TOKEN=xoxb-alerts-secret\n");
    assert.equal(readAlertsToken(envFile), "xoxb-alerts-secret");

    const seen: Array<{ url: string; auth: string | undefined; body: Record<string, unknown> }> = [];
    let reply: { status: number; body: unknown } = { status: 200, body: { ok: true, ts: "1794000000.000100" } };
    const fetchImpl = (async (url: string, init: RequestInit) => {
      seen.push({
        url,
        auth: (init.headers as Record<string, string>).Authorization,
        body: JSON.parse(String(init.body)),
      });
      return { status: reply.status, json: async () => reply.body } as Response;
    }) as unknown as typeof fetch;
    const poster = new SlackAlertsPoster(envFile, fetchImpl);

    assert.deepEqual(await poster.post("C0ALZJHGE49", THREAD, "hello"), { ok: true, ts: "1794000000.000100" });
    assert.equal(seen[0].url, "https://slack.com/api/chat.postMessage");
    assert.equal(seen[0].auth, "Bearer xoxb-alerts-secret", "uses the Alerts token, not the PM token");
    assert.equal(seen[0].body.thread_ts, THREAD);
    assert.equal(seen[0].body.channel, "C0ALZJHGE49");

    reply = { status: 200, body: { ok: false, error: "thread_not_found" } };
    const refused = await poster.post("C0ALZJHGE49", THREAD, "hello");
    assert.deepEqual(refused, { ok: false, error: "slack_thread_not_found", effect: "unknown" }, "an unlisted Slack error may have posted");

    reply = { status: 429, body: {} };
    assert.deepEqual(await poster.post("C0ALZJHGE49", THREAD, "hello"), { ok: false, error: "ratelimited", effect: "none" });

    const throwing = new SlackAlertsPoster(envFile, (async () => {
      throw new Error("connect failed with Bearer xoxb-alerts-secret");
    }) as unknown as typeof fetch);
    const transport = await throwing.post("C0ALZJHGE49", THREAD, "hello");
    assert.equal(transport.ok, false);
    assert.equal(transport.ok === false && transport.effect, "unknown");
    assert.doesNotMatch(JSON.stringify(transport), /xoxb|secret/, "error text carries no token");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("SlackAlertsPoster without a token reports alerts_token_missing and does not call Slack", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-alerts-env-"));
  try {
    const envFile = join(directory, ".env.local");
    writeFileSync(envFile, "SLACK_BOT_TOKEN=pm-token\n");
    let called = false;
    const poster = new SlackAlertsPoster(envFile, (async () => {
      called = true;
      return {} as Response;
    }) as unknown as typeof fetch);
    assert.equal(poster.tokenConfigured(), false);
    assert.deepEqual(await poster.post("C0ALZJHGE49", THREAD, "hello"), { ok: false, error: "alerts_token_missing", effect: "none" });
    assert.equal(called, false, "never falls back to the PM token");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

// ─── REST routes ─────────────────────────────────────────

test("REST routes: add, idempotent re-add, conflict, list, cancel", async () => {
  const clock = { now: T0 };
  const { store, scheduler } = schedulerFixture(clock);
  const app = new Hono();
  registerAlertReminderRoutes(app, { store, scheduler, now: () => clock.now, obligationState: () => "open" });
  const post = (path: string, body: unknown) =>
    app.request(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });

  const created = await post("/alert-reminders", request({ until_relative: true }));
  assert.equal(created.status, 200);
  const createdBody = (await created.json()) as { outcome: string; reminder: { key: string; next_at: string; status: string; last_outcome: string | null; unknown_count: number } };
  assert.equal(createdBody.outcome, "created");
  assert.equal(createdBody.reminder.last_outcome, null);
  assert.equal(createdBody.reminder.unknown_count, 0);
  assert.equal(createdBody.reminder.next_at, new Date(T0 + 30 * MIN).toISOString());

  const repeat = await post("/alert-reminders", request({ until_relative: true }));
  assert.equal(((await repeat.json()) as { outcome: string }).outcome, "noop");

  const conflict = await post("/alert-reminders", request({ text: "other" }));
  assert.equal(conflict.status, 409);
  assert.deepEqual(((await conflict.json()) as { differs: string[] }).differs, ["text"]);

  const invalid = await post("/alert-reminders", request({ interval_s: 60 }));
  assert.equal(invalid.status, 400);

  const withObligation = await post("/alert-reminders", request({ key: "k2", stop_obligation_id: 9 }));
  assert.equal(withObligation.status, 200);

  const list = (await (await app.request("/alert-reminders")).json()) as { reminders: Array<{ key: string }>; scheduler: { token_configured: boolean } };
  assert.deepEqual(list.reminders.map((r) => r.key).sort(), ["k1", "k2"]);
  assert.equal(typeof list.scheduler.token_configured, "boolean");
  assert.equal((await app.request("/alert-reminders?status=bogus")).status, 400);

  const cancelled = (await (await post("/alert-reminders/k1/cancel", {})).json()) as { outcome: string; reminder: { status: string } };
  assert.equal(cancelled.outcome, "cancelled");
  assert.equal(cancelled.reminder.status, "stopped");
  const again = (await (await post("/alert-reminders/k1/cancel", {})).json()) as { outcome: string };
  assert.equal(again.outcome, "already_stopped");
  assert.equal((await post("/alert-reminders/nope/cancel", {})).status, 404);
  const activeAfter = (await (await app.request("/alert-reminders")).json()) as { reminders: Array<{ key: string }> };
  assert.deepEqual(activeAfter.reminders.map((r) => r.key), ["k2"]);
});

test("REST add refuses a stop obligation that is closed, missing, or unreadable", async () => {
  const clock = { now: T0 };
  const { store, scheduler } = schedulerFixture(clock);
  let state: ObligationState = "closed";
  const app = new Hono();
  registerAlertReminderRoutes(app, { store, scheduler, now: () => clock.now, obligationState: () => state });
  const add = () =>
    app.request("/alert-reminders", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request({ stop_obligation_id: 9 })),
    });
  assert.equal((await add()).status, 400);
  state = "missing";
  assert.equal((await add()).status, 404);
  state = "unknown";
  assert.equal((await add()).status, 503);
  assert.equal(store.get("k1"), null);
});

// ─── durability ──────────────────────────────────────────

test("reminders live in MoP's own database and survive a restart", () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-alert-reminders-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  try {
    const first = new MoPDatabase(config);
    assert.ok(first.alertReminderStore().add(normalized({ stop_obligation_id: 12 }), T0, () => null).ok);
    first.close();

    const second = new MoPDatabase(config);
    const row = second.alertReminderStore().get("k1");
    assert.equal(row?.status, "active");
    assert.equal(row?.stop_obligation_id, 12);
    assert.equal(row?.interval_s, 1800);
    second.close();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("the table rejects an interval under ten minutes at the storage layer too", () => {
  const { db } = memoryStore();
  assert.throws(() =>
    db
      .prepare(
        "INSERT INTO alert_reminders (key, thread_ts, text, interval_s, next_at, until_at, status, created_at, updated_at) VALUES ('x', '1.2', 't', 599, 1, 2, 'active', 1, 1)",
      )
      .run(),
  );
});

// ─── delivery classification (ambiguous delivery is never retried) ───

type FetchBehaviour =
  | { kind: "reply"; status: number; body: unknown }
  | { kind: "unparseable"; status: number }
  | { kind: "throw"; error: unknown };

function connectError(code: string): Error {
  const cause = Object.assign(new Error(`connect ${code}`), { code });
  return Object.assign(new TypeError("fetch failed"), { cause });
}

/** A real SlackAlertsPoster over a scripted fetch, so classification is exercised end to end. */
function slackFixture(behaviours: FetchBehaviour[]) {
  const directory = mkdtempSync(join(tmpdir(), "mop-alerts-env-"));
  const envFile = join(directory, ".env.local");
  writeFileSync(envFile, "SLACK_ALERTS_BOT_TOKEN=xoxb-alerts-secret\n");
  const queue = [...behaviours];
  let fetchCalls = 0;
  const fetchImpl = (async () => {
    fetchCalls += 1;
    const next = queue.shift() ?? { kind: "reply", status: 200, body: { ok: true, ts: "1794000000.000999" } };
    if (next.kind === "throw") throw next.error;
    if (next.kind === "unparseable") {
      return { status: next.status, json: async () => { throw new SyntaxError("Unexpected end of JSON input"); } } as unknown as Response;
    }
    return { status: next.status, json: async () => next.body } as Response;
  }) as unknown as typeof fetch;
  return {
    poster: new SlackAlertsPoster(envFile, fetchImpl),
    fetchCalls: () => fetchCalls,
    cleanup: () => rmSync(directory, { recursive: true, force: true }),
  };
}

test("SlackAlertsPoster marks only affirmative zero-effect outcomes as retryable", async () => {
  const cases: Array<{ name: string; behaviour: FetchBehaviour; effect: "none" | "unknown"; error: RegExp }> = [
    { name: "not_in_channel", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "not_in_channel" } }, effect: "none", error: /slack_not_in_channel/ },
    { name: "channel_not_found", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "channel_not_found" } }, effect: "none", error: /slack_channel_not_found/ },
    { name: "invalid_auth", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "invalid_auth" } }, effect: "none", error: /slack_invalid_auth/ },
    { name: "ratelimited error body", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "ratelimited" } }, effect: "none", error: /slack_ratelimited/ },
    { name: "HTTP 429", behaviour: { kind: "reply", status: 429, body: {} }, effect: "none", error: /^ratelimited$/ },
    { name: "connection refused", behaviour: { kind: "throw", error: connectError("ECONNREFUSED") }, effect: "none", error: /slack_connect_ECONNREFUSED/ },
    {
      name: "connection refused on every address",
      behaviour: { kind: "throw", error: Object.assign(new TypeError("fetch failed"), { cause: Object.assign(new AggregateError([connectError("ECONNREFUSED"), connectError("ENETUNREACH")]), { code: "ECONNREFUSED" }) }) },
      effect: "none",
      error: /slack_connect_/,
    },
    { name: "timeout", behaviour: { kind: "throw", error: new DOMException("The operation was aborted due to timeout", "TimeoutError") }, effect: "unknown", error: /slack_transport_TimeoutError/ },
    { name: "connection reset after send", behaviour: { kind: "throw", error: connectError("ECONNRESET") }, effect: "unknown", error: /slack_transport_TypeError/ },
    { name: "mixed connect codes", behaviour: { kind: "throw", error: Object.assign(new TypeError("fetch failed"), { cause: Object.assign(new AggregateError([connectError("ECONNREFUSED"), connectError("ETIMEDOUT")]), { code: "ECONNREFUSED" }) }) }, effect: "unknown", error: /slack_transport_/ },
    { name: "unparseable reply", behaviour: { kind: "unparseable", status: 200 }, effect: "unknown", error: /slack_response_unparseable/ },
    { name: "HTTP 500", behaviour: { kind: "reply", status: 500, body: { ok: false, error: "ratelimited" } }, effect: "unknown", error: /slack_http_500/ },
    { name: "HTTP 503", behaviour: { kind: "reply", status: 503, body: {} }, effect: "unknown", error: /slack_http_503/ },
    { name: "fatal_error", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "fatal_error" } }, effect: "unknown", error: /slack_fatal_error/ },
    { name: "internal_error", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "internal_error" } }, effect: "unknown", error: /slack_internal_error/ },
    { name: "unlisted error", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "thread_not_found" } }, effect: "unknown", error: /slack_thread_not_found/ },
    { name: "ok without ts", behaviour: { kind: "reply", status: 200, body: { ok: true } }, effect: "unknown", error: /slack_ok_without_ts/ },
    { name: "null body", behaviour: { kind: "reply", status: 200, body: null }, effect: "unknown", error: /slack_http_200/ },
  ];
  for (const { name, behaviour, effect, error } of cases) {
    const { poster, cleanup } = slackFixture([behaviour]);
    try {
      const outcome = await poster.post("C0ALZJHGE49", THREAD, "hello");
      assert.equal(outcome.ok, false, name);
      if (outcome.ok) continue;
      assert.equal(outcome.effect, effect, `${name}: effect`);
      assert.match(outcome.error, error, `${name}: error`);
      assert.doesNotMatch(outcome.error, /xoxb|secret/, `${name}: no token in error text`);
    } finally {
      cleanup();
    }
  }
});

test("a lost ACK, timeout, unparseable reply or 5xx is never retried: the occurrence is consumed and UNKNOWN is recorded", async () => {
  const ambiguous: Array<{ name: string; behaviour: FetchBehaviour; reason: RegExp }> = [
    { name: "timeout", behaviour: { kind: "throw", error: new DOMException("timeout", "TimeoutError") }, reason: /slack_transport_TimeoutError/ },
    { name: "lost response", behaviour: { kind: "throw", error: connectError("ECONNRESET") }, reason: /slack_transport_TypeError/ },
    { name: "parse failure", behaviour: { kind: "unparseable", status: 200 }, reason: /slack_response_unparseable/ },
    { name: "5xx", behaviour: { kind: "reply", status: 502, body: {} }, reason: /slack_http_502/ },
    { name: "generic slack error", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "fatal_error" } }, reason: /slack_fatal_error/ },
  ];
  for (const { name, behaviour, reason } of ambiguous) {
    const slack = slackFixture([behaviour]);
    try {
      const clock = { now: T0 };
      const { store } = memoryStore();
      const events: Array<{ type: string; payload: Record<string, unknown> }> = [];
      const schedulerWithSlack = new AlertReminderScheduler({ store, poster: slack.poster, now: () => clock.now, logEvent: (type, payload) => events.push({ type, payload }) });
      store.add(normalized({ interval_s: 600, until_at: new Date(T0 + 6 * HOUR).toISOString() }), T0);

      clock.now = T0 + 10 * MIN + 2_000;
      const first = await schedulerWithSlack.tick();
      assert.deepEqual(first.unknown, ["k1"], name);
      assert.deepEqual(first.failed, [], `${name}: not a retryable failure`);
      assert.equal(slack.fetchCalls(), 1, name);

      const row = store.get("k1")!;
      assert.equal(row.last_outcome, "unknown", name);
      assert.match(row.last_unknown_reason ?? "", reason, name);
      assert.equal(row.last_error, row.last_unknown_reason, `${name}: reason is visible on the row`);
      assert.equal(row.unknown_count, 1, name);
      assert.equal(row.last_unknown_at, clock.now, name);
      assert.equal(row.next_at, T0 + 20 * MIN, `${name}: the occurrence stays consumed, next_at is the following grid point`);
      assert.equal(row.retry_at, null, `${name}: no retry scheduled`);
      assert.equal(row.fail_count, 0, name);
      assert.equal(row.post_count, 0, name);
      assert.equal(row.status, "active", name);
      assert.ok(events.some((e) => e.type === "alert_reminder_post_unknown" && e.payload.key === "k1"), name);
      assert.ok(!events.some((e) => e.type === "alert_reminder_post_failed"), name);

      // Well past any backoff window, but before the next grid point: nothing is re-sent.
      clock.now = T0 + 19 * MIN;
      assert.equal((await schedulerWithSlack.tick()).claimed, 0, `${name}: no retry`);
      assert.equal(slack.fetchCalls(), 1, `${name}: still one send`);

      // The next scheduled occurrence goes out normally; the earlier uncertainty stays on record.
      clock.now = T0 + 20 * MIN + 2_000;
      assert.deepEqual((await schedulerWithSlack.tick()).posted, ["k1"], name);
      const after = store.get("k1")!;
      assert.equal(after.last_outcome, "posted", name);
      assert.equal(after.post_count, 1, name);
      assert.equal(after.unknown_count, 1, `${name}: UNKNOWN history is not erased by a later success`);
      assert.match(after.last_unknown_reason ?? "", reason, name);
      assert.equal(slack.fetchCalls(), 2, name);
    } finally {
      slack.cleanup();
    }
  }
});

test("a zero-effect error (not_in_channel, 429, connection refused) restores the due time and is retried after the backoff", async () => {
  const zeroEffect: Array<{ name: string; behaviour: FetchBehaviour; error: RegExp }> = [
    { name: "not_in_channel", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "not_in_channel" } }, error: /slack_not_in_channel/ },
    { name: "channel_not_found", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "channel_not_found" } }, error: /slack_channel_not_found/ },
    { name: "invalid_auth", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "invalid_auth" } }, error: /slack_invalid_auth/ },
    { name: "429", behaviour: { kind: "reply", status: 429, body: {} }, error: /^ratelimited$/ },
    { name: "connection refused", behaviour: { kind: "throw", error: connectError("ECONNREFUSED") }, error: /slack_connect_ECONNREFUSED/ },
  ];
  for (const { name, behaviour, error } of zeroEffect) {
    const slack = slackFixture([behaviour]);
    try {
      const clock = { now: T0 };
      const { store } = memoryStore();
      const scheduler = new AlertReminderScheduler({ store, poster: slack.poster, now: () => clock.now });
      store.add(normalized({ interval_s: 600, until_at: new Date(T0 + 6 * HOUR).toISOString() }), T0);

      clock.now = T0 + 10 * MIN + 2_000;
      const failed = await scheduler.tick();
      assert.deepEqual(failed.failed, ["k1"], name);
      assert.deepEqual(failed.unknown, [], name);
      let row = store.get("k1")!;
      assert.equal(row.last_outcome, "failed", name);
      assert.match(row.last_error ?? "", error, name);
      assert.equal(row.unknown_count, 0, `${name}: nothing unknown`);
      assert.equal(row.next_at, T0 + 10 * MIN, `${name}: the due time is restored`);
      assert.equal(row.retry_at, clock.now + 60_000, name);
      assert.equal(row.fail_count, 1, name);

      clock.now += 30_000;
      assert.equal((await scheduler.tick()).claimed, 0, `${name}: inside the backoff window`);

      clock.now += 31_000;
      assert.deepEqual((await scheduler.tick()).posted, ["k1"], `${name}: retried and delivered`);
      assert.equal(slack.fetchCalls(), 2, name);
      row = store.get("k1")!;
      assert.equal(row.post_count, 1, name);
      assert.equal(row.last_outcome, "posted", name);
      assert.equal(row.last_error, null, name);
      assert.equal(row.fail_count, 0, name);
    } finally {
      slack.cleanup();
    }
  }
});

// ─── claim generation fence (cancel / replace / expire during a batch) ───

test("a row cancelled during the batch is not sent", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler, events } = schedulerFixture(clock);
  store.add(normalized({ key: "a" }), T0);
  store.add(normalized({ key: "b" }), T0);
  poster.whileInFlight = (call) => {
    if (call === 1) store.stop("b", "cancelled", clock.now);
  };

  clock.now = T0 + 31 * MIN;
  const result = await scheduler.tick();
  assert.equal(result.claimed, 2);
  assert.deepEqual(poster.calls.map((c) => c.text), ["recurring check"], "only a was sent");
  assert.deepEqual(result.posted, ["a"]);
  assert.deepEqual(result.skipped, ["b"]);
  const b = store.get("b")!;
  assert.equal(b.status, "stopped");
  assert.equal(b.stop_reason, "cancelled");
  assert.equal(b.post_count, 0);
  assert.equal(b.last_post_ts, null);
  assert.equal(b.last_outcome, null);
  assert.ok(events.some((e) => e.type === "alert_reminder_send_skipped" && e.payload.key === "b" && e.payload.reason === "not_active"));
});

test("a row cancelled while its own send is in flight keeps the delivered receipt and stays stopped", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler } = schedulerFixture(clock);
  store.add(normalized(), T0);
  poster.whileInFlight = () => {
    store.stop("k1", "cancelled", clock.now);
  };
  clock.now = T0 + 31 * MIN;
  const result = await scheduler.tick();
  assert.deepEqual(result.posted, ["k1"], "the post already left; its receipt is true for this claim");
  const row = store.get("k1")!;
  assert.equal(row.status, "stopped");
  assert.equal(row.post_count, 1);
  assert.match(row.last_post_ts ?? "", /^1794000000\./);
  clock.now += 3 * HOUR;
  assert.equal((await scheduler.tick()).claimed, 0, "a cancelled row never posts again");
});

test("a row replaced during the batch: the old completion does not touch the new row", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler, events } = schedulerFixture(clock);
  store.add(normalized(), T0);
  clock.now = T0 + 31 * MIN;
  const replacementFirstAt = clock.now + 30 * MIN;
  poster.whileInFlight = () => {
    const replaced = store.add(normalized({ text: "replacement", replace: true }, clock.now), clock.now);
    assert.ok(replaced.ok && replaced.outcome === "replaced");
  };

  const result = await scheduler.tick();
  assert.deepEqual(poster.calls.map((c) => c.text), ["recurring check"], "the old binding was sent once");
  assert.deepEqual(result.posted, [], "the stale completion is not recorded as a post");
  assert.deepEqual(result.stale, ["k1"]);
  const row = store.get("k1")!;
  assert.equal(row.text, "replacement");
  assert.equal(row.post_count, 0, "no receipt attached to the replacement");
  assert.equal(row.last_post_ts, null);
  assert.equal(row.last_posted_at, null);
  assert.equal(row.last_outcome, null);
  assert.equal(row.next_at, replacementFirstAt, "the replacement keeps its own schedule");
  assert.ok(events.some((e) => e.type === "alert_reminder_stale_completion" && e.payload.key === "k1" && e.payload.outcome === "posted" && typeof e.payload.slack_ts === "string"), "the orphaned receipt is still audited");

  // The replacement posts on its own schedule, not as a replay of the old occurrence.
  poster.whileInFlight = () => undefined;
  clock.now = replacementFirstAt + 1_000;
  assert.deepEqual((await scheduler.tick()).posted, ["k1"]);
  assert.deepEqual(poster.calls.map((c) => c.text), ["recurring check", "replacement"]);
});

test("a stale zero-effect failure or UNKNOWN completion cannot restore the due time or mark the replacement", async () => {
  for (const outcome of [
    { ok: false, error: "slack_not_in_channel", effect: "none" },
    { ok: false, error: "slack_transport_TimeoutError", effect: "unknown" },
  ] as const) {
    const clock = { now: T0 };
    const { store, poster, scheduler } = schedulerFixture(clock);
    store.add(normalized(), T0);
    clock.now = T0 + 31 * MIN;
    const replacementFirstAt = clock.now + 30 * MIN;
    poster.results = [outcome];
    poster.whileInFlight = () => {
      store.add(normalized({ text: "replacement", replace: true }, clock.now), clock.now);
    };
    const result = await scheduler.tick();
    assert.deepEqual(result.stale, ["k1"], outcome.effect);
    assert.deepEqual(result.failed, [], outcome.effect);
    assert.deepEqual(result.unknown, [], outcome.effect);
    const row = store.get("k1")!;
    assert.equal(row.next_at, replacementFirstAt, `${outcome.effect}: next_at not rewound`);
    assert.equal(row.last_error, null, outcome.effect);
    assert.equal(row.last_outcome, null, outcome.effect);
    assert.equal(row.fail_count, 0, outcome.effect);
    assert.equal(row.retry_at, null, outcome.effect);
    assert.equal(row.unknown_count, 0, outcome.effect);
    clock.now += MIN;
    assert.equal((await scheduler.tick()).claimed, 0, `${outcome.effect}: the replacement is not re-claimed early`);
    assert.equal(poster.calls.length, 1, outcome.effect);
  }
});

test("a stale completion does not clear an error the replacement already recorded", async () => {
  const clock = { now: T0 };
  const { store, db, poster, scheduler } = schedulerFixture(clock);
  store.add(normalized(), T0);
  clock.now = T0 + 31 * MIN;
  poster.whileInFlight = () => {
    store.add(normalized({ text: "replacement", replace: true }, clock.now), clock.now);
    // The replacement's own first occurrence has already failed while the old send was in flight.
    db.prepare("UPDATE alert_reminders SET last_error = 'slack_not_in_channel', last_outcome = 'failed', fail_count = 1 WHERE key = 'k1'").run();
  };
  await scheduler.tick();
  const row = store.get("k1")!;
  assert.equal(row.last_error, "slack_not_in_channel", "the stale success did not clear the replacement's error");
  assert.equal(row.last_outcome, "failed");
  assert.equal(row.fail_count, 1);
  assert.equal(row.post_count, 0);
});

test("a row replaced before its turn in the batch is not sent as the old binding", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler, events } = schedulerFixture(clock);
  store.add(normalized({ key: "a" }), T0);
  store.add(normalized({ key: "b" }), T0);
  clock.now = T0 + 31 * MIN;
  const replacementFirstAt = clock.now + 30 * MIN;
  poster.whileInFlight = (call) => {
    if (call === 1) store.add(normalized({ key: "b", text: "replacement", replace: true }, clock.now), clock.now);
  };
  const result = await scheduler.tick();
  assert.deepEqual(poster.calls.map((c) => c.text), ["recurring check"], "b's old text was never sent");
  assert.deepEqual(result.skipped, ["b"]);
  assert.ok(events.some((e) => e.type === "alert_reminder_send_skipped" && e.payload.key === "b" && e.payload.reason === "claim_superseded"));
  const b = store.get("b")!;
  assert.equal(b.text, "replacement");
  assert.equal(b.next_at, replacementFirstAt, "the replacement's schedule is untouched");
  assert.equal(b.post_count, 0);
});

test("a row that passes its cutoff before its turn in the batch is not sent", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler, events } = schedulerFixture(clock);
  store.add(normalized({ key: "a", interval_s: 600 }), T0);
  store.add(normalized({ key: "b", interval_s: 600, until_at: new Date(T0 + 11 * MIN).toISOString() }), T0);
  clock.now = T0 + 10 * MIN + 1_000;
  poster.whileInFlight = (call) => {
    if (call === 1) clock.now = T0 + 12 * MIN; // a slow first send runs b past its until_at
  };
  const result = await scheduler.tick();
  assert.deepEqual(result.posted, ["a"]);
  assert.deepEqual(result.skipped, ["b"]);
  assert.equal(poster.calls.length, 1);
  assert.ok(events.some((e) => e.type === "alert_reminder_send_skipped" && e.payload.key === "b" && e.payload.reason === "past_cutoff"));
  assert.equal(store.get("b")!.post_count, 0);
});

test("a stop obligation that closes during the batch stops the row before its send", async () => {
  const clock = { now: T0 };
  let state: ObligationState = "open";
  const { store, poster, scheduler } = schedulerFixture(clock, () => state);
  store.add(normalized({ key: "a" }), T0);
  store.add(normalized({ key: "b", stop_obligation_id: 55 }), T0, () => null);
  poster.whileInFlight = (call) => {
    if (call === 1) state = "closed";
  };
  clock.now = T0 + 31 * MIN;
  const result = await scheduler.tick();
  assert.deepEqual(poster.calls.length, 1, "b was not sent");
  assert.deepEqual(result.skipped, ["b"]);
  assert.deepEqual(result.stopped, ["b"]);
  const b = store.get("b")!;
  assert.equal(b.status, "stopped");
  assert.equal(b.stop_reason, "obligation_resolved");
  assert.equal(b.post_count, 0);
});

test("a skipped claim stays consumed: the row is not re-claimed in the same window", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler } = schedulerFixture(clock);
  store.add(normalized({ key: "a" }), T0);
  store.add(normalized({ key: "b" }), T0);
  poster.whileInFlight = (call) => {
    if (call === 1) {
      // Replace-then-the-same-args-again is idempotent; a cancel then revive makes a fresh schedule.
      store.stop("b", "cancelled", clock.now);
      store.add(normalized({ key: "b" }, clock.now), clock.now);
    }
  };
  clock.now = T0 + 31 * MIN;
  const result = await scheduler.tick();
  assert.deepEqual(result.skipped, ["b"], "a revive during the batch is a new binding too");
  const b = store.get("b")!;
  assert.equal(b.status, "active");
  assert.equal(b.next_at, clock.now + 30 * MIN);
  clock.now += MIN;
  assert.equal((await scheduler.tick()).claimed, 0);
});

// ─── migration of the live table ─────────────────────────

const V1_DDL = `
  CREATE TABLE alert_reminders (
    key TEXT PRIMARY KEY,
    channel TEXT NOT NULL DEFAULT 'C0ALZJHGE49',
    thread_ts TEXT NOT NULL,
    text TEXT NOT NULL,
    interval_s INTEGER NOT NULL CHECK (interval_s >= 600),
    next_at INTEGER NOT NULL,
    until_at INTEGER NOT NULL,
    stop_obligation_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('active', 'stopped', 'expired')),
    stop_reason TEXT,
    post_count INTEGER NOT NULL DEFAULT 0,
    last_post_ts TEXT,
    last_posted_at INTEGER,
    last_error TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER,
    created_by TEXT,
    source_ref TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
  );
  CREATE INDEX idx_alert_reminders_status_next ON alert_reminders(status, next_at);
`;

test("a table created by the first release is upgraded in place; existing reminders keep their data and keep working", async () => {
  const db = new Database(":memory:");
  db.exec(V1_DDL);
  const insert = db.prepare(
    `INSERT INTO alert_reminders (key, thread_ts, text, interval_s, next_at, until_at, stop_obligation_id, status, created_by, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 'cli', ?, ?)`,
  );
  insert.run("retries-13049", "1794016172.136909", "30-minute check", 1800, T0 + 30 * MIN, T0 + 24 * HOUR, 31154, T0, T0);
  insert.run("backlog-hygiene", THREAD, "Hourly check", 3600, T0 + HOUR, T0 + 12 * HOUR, null, T0, T0);

  ensureAlertReminderSchema(db);
  ensureAlertReminderSchema(db); // idempotent: a second boot is a no-op
  const columns = (db.prepare("PRAGMA table_info(alert_reminders)").all() as Array<{ name: string }>).map((c) => c.name);
  for (const name of ["claim_gen", "last_outcome", "unknown_count", "last_unknown_at", "last_unknown_reason"]) {
    assert.ok(columns.includes(name), `${name} added`);
  }

  const store = new AlertReminderStore(db);
  const kept = store.get("retries-13049")!;
  assert.equal(kept.status, "active");
  assert.equal(kept.text, "30-minute check");
  assert.equal(kept.interval_s, 1800);
  assert.equal(kept.next_at, T0 + 30 * MIN);
  assert.equal(kept.stop_obligation_id, 31154);
  assert.equal(kept.claim_gen, 0);
  assert.equal(kept.last_outcome, null);
  assert.equal(kept.unknown_count, 0);
  assert.deepEqual(store.list("active").map((r) => r.key).sort(), ["backlog-hygiene", "retries-13049"]);

  const clock = { now: T0 + 30 * MIN + 1_000 };
  const poster = new FakePoster();
  const scheduler = new AlertReminderScheduler({ store, poster, obligationState: () => "open", now: () => clock.now });
  const result = await scheduler.tick();
  assert.deepEqual(result.posted, ["retries-13049"], "the migrated live reminder posts on schedule");
  assert.equal(store.get("retries-13049")!.claim_gen, 1);
  assert.equal(store.get("retries-13049")!.post_count, 1);
  assert.equal(store.get("backlog-hygiene")!.post_count, 0, "the other reminder is not due and is untouched");
  assert.equal(store.get("backlog-hygiene")!.next_at, T0 + HOUR);
});

test("the upgraded schema stays readable by the previous release's statements (rollback safe)", () => {
  const { db } = memoryStore();
  // The previous release inserts with an explicit column list and reads with SELECT *: extra columns must not matter.
  db.prepare(
    "INSERT INTO alert_reminders (key, channel, thread_ts, text, interval_s, next_at, until_at, stop_obligation_id, status, stop_reason, post_count, last_post_ts, last_posted_at, last_error, fail_count, retry_at, created_by, source_ref, created_at, updated_at) VALUES ('old', 'C0ALZJHGE49', '1.2', 't', 600, 1, 2, NULL, 'active', NULL, 0, NULL, NULL, NULL, 0, NULL, NULL, NULL, 1, 1)",
  ).run();
  const row = db.prepare("SELECT * FROM alert_reminders WHERE key = 'old'").get() as { claim_gen: number; unknown_count: number };
  assert.equal(row.claim_gen, 0);
  assert.equal(row.unknown_count, 0);
});
