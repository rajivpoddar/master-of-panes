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
  tokenConfigured(): boolean {
    return this.configured;
  }
  async post(channel: string, threadTs: string, text: string): Promise<AlertPostResult> {
    this.calls.push({ channel, threadTs, text });
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

test("a Slack failure is recorded, the due time is kept, and the retry backs off then recovers", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler, events } = schedulerFixture(clock);
  store.add(normalized({ interval_s: 600, until_at: new Date(T0 + 6 * HOUR).toISOString() }), T0);

  clock.now = T0 + 10 * MIN;
  poster.results = [{ ok: false, error: "slack_thread_not_found" }];
  const failed = await scheduler.tick();
  assert.deepEqual(failed.failed, ["k1"]);
  let row = store.get("k1")!;
  assert.equal(row.last_error, "slack_thread_not_found");
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

test("a throwing poster cannot break the tick loop", async () => {
  const clock = { now: T0 };
  const { store, poster, scheduler, events } = schedulerFixture(clock);
  store.add(normalized(), T0);
  poster.post = async () => {
    throw new Error("boom");
  };
  clock.now = T0 + 31 * MIN;
  await scheduler.tick();
  assert.ok(events.some((e) => e.type === "alert_reminder_tick_error"));
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
    assert.deepEqual(refused, { ok: false, error: "slack_thread_not_found" });

    reply = { status: 429, body: {} };
    assert.deepEqual(await poster.post("C0ALZJHGE49", THREAD, "hello"), { ok: false, error: "ratelimited" });

    const throwing = new SlackAlertsPoster(envFile, (async () => {
      throw new Error("connect failed with Bearer xoxb-alerts-secret");
    }) as unknown as typeof fetch);
    const transport = await throwing.post("C0ALZJHGE49", THREAD, "hello");
    assert.equal(transport.ok, false);
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
    assert.deepEqual(await poster.post("C0ALZJHGE49", THREAD, "hello"), { ok: false, error: "alerts_token_missing" });
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
  const createdBody = (await created.json()) as { outcome: string; reminder: { key: string; next_at: string; status: string } };
  assert.equal(createdBody.outcome, "created");
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
