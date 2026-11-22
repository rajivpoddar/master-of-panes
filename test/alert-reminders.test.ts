import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import Database from "better-sqlite3";
import { Hono } from "hono";

import {
  advanceByWholeIntervals,
  ALERT_REMINDER_EXPIRY_WARN_LEAD_MS,
  AlertReminderScheduler,
  AlertReminderStore,
  ensureAlertReminderSchema,
  expiryEventType,
  expiryWarningMessage,
  normalizeAddRequest,
  parseExpiredSince,
  parseExpiryEventType,
  readAlertsToken,
  registerAlertReminderRoutes,
  reminderEndsAt,
  retryBackoffMs,
  revalidateExpiryWarning,
  SlackAlertsPoster,
  type AlertPoster,
  type AlertPostResult,
  type ObligationState,
} from "../src/alertReminders.js";
import { MoPDatabase } from "../src/db.js";
import { PMCadenceScheduler } from "../src/pmCadence.js";
import type { PMSubmitResult } from "../src/relay.js";
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

function schedulerFixture(
  clock: { now: number },
  obligationState?: (id: number) => ObligationState,
  submitToPM?: (message: string, eventType: string) => Promise<PMSubmitResult>,
) {
  const { store, db } = memoryStore();
  const poster = new FakePoster();
  const events: Array<{ type: string; payload: Record<string, unknown> }> = [];
  const scheduler = new AlertReminderScheduler({
    store,
    poster,
    obligationState,
    submitToPM,
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

test("extend moves only until_at on an active row and never revives, shortens, or rewrites a lapsed row", () => {
  const { store } = memoryStore();
  const created = store.add(normalized(), T0);
  assert.ok(created.ok);
  const before = store.get("k1");
  assert.ok(before);
  const later = T0 + 5 * 24 * HOUR;
  const extended = store.extendUntil("k1", later, T0 + HOUR);
  assert.ok(extended);
  assert.equal(extended.outcome, "extended");
  assert.equal(extended.row.until_at, later);
  assert.equal(extended.row.next_at, before.next_at);
  assert.equal(extended.row.claim_gen, before.claim_gen);
  assert.equal(extended.row.interval_s, before.interval_s);
  assert.equal(extended.row.post_count, before.post_count);
  assert.equal(extended.row.stop_obligation_id, before.stop_obligation_id);
  assert.equal(extended.row.text, before.text);
  assert.equal(store.extendUntil("k1", later, T0 + HOUR)?.outcome, "already_far_enough");
  assert.equal(store.extendUntil("k1", later - HOUR, T0 + HOUR)?.outcome, "already_far_enough");
  assert.equal(store.get("k1")?.until_at, later);
  const lapsedAt = later + MIN;
  const lapsed = store.extendUntil("k1", later + 48 * HOUR, lapsedAt);
  assert.equal(lapsed?.outcome, "lapsed");
  assert.equal(store.get("k1")?.status, "active");
  assert.equal(store.get("k1")?.until_at, later);
  assert.equal(store.stop("k1", "cancelled", lapsedAt)?.row.status, "stopped");
  assert.equal(store.extendUntil("k1", later + 3 * HOUR, lapsedAt)?.outcome, "not_active");
  assert.equal(store.get("k1")?.status, "stopped");
  assert.equal(store.get("k1")?.until_at, later);
  assert.equal(store.extendUntil("missing", later, T0), null);
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
  const fetchImpl = (async (_url: string | URL | Request, init?: RequestInit) => {
    assert.equal(init?.redirect, "manual");
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
    { name: "redirect with misleading error", behaviour: { kind: "reply", status: 302, body: { ok: false, error: "invalid_auth" } }, effect: "unknown", error: /slack_http_302/ },
    { name: "non-200 with misleading success", behaviour: { kind: "reply", status: 201, body: { ok: true, ts: "1.2" } }, effect: "unknown", error: /slack_http_201/ },
    { name: "connection refused", behaviour: { kind: "throw", error: connectError("ECONNREFUSED") }, effect: "unknown", error: /slack_transport_TypeError/ },
    {
      name: "connection refused on every address",
      behaviour: { kind: "throw", error: Object.assign(new TypeError("fetch failed"), { cause: Object.assign(new AggregateError([connectError("ECONNREFUSED"), connectError("ENETUNREACH")]), { code: "ECONNREFUSED" }) }) },
      effect: "unknown",
      error: /slack_transport_TypeError/,
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
    { name: "connection refused", behaviour: { kind: "throw", error: connectError("ECONNREFUSED") }, reason: /slack_transport_TypeError/ },
    { name: "redirect", behaviour: { kind: "reply", status: 302, body: { ok: false, error: "invalid_auth" } }, reason: /slack_http_302/ },
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

test("a zero-effect error (not_in_channel, 429) restores the due time and is retried after the backoff", async () => {
  const zeroEffect: Array<{ name: string; behaviour: FetchBehaviour; error: RegExp }> = [
    { name: "not_in_channel", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "not_in_channel" } }, error: /slack_not_in_channel/ },
    { name: "channel_not_found", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "channel_not_found" } }, error: /slack_channel_not_found/ },
    { name: "invalid_auth", behaviour: { kind: "reply", status: 200, body: { ok: false, error: "invalid_auth" } }, error: /slack_invalid_auth/ },
    { name: "429", behaviour: { kind: "reply", status: 429, body: {} }, error: /^ratelimited$/ },
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
  for (const name of ["claim_gen", "last_outcome", "unknown_count", "last_unknown_at", "last_unknown_reason", "expiry_warned_until_at"]) {
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
  assert.equal(kept.expiry_warned_until_at, null, "a migrated reminder has not been warned yet");
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

// ─── pre-expiry PM warning and the expired-without-obligation surface ──

const DAY = 24 * HOUR;
const delivered = async (): Promise<PMSubmitResult> => ({ ok: true, submitKey: "Enter", ambiguous: false });
const flush = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

/** A recorder for the injected PM submit; `impl` decides what each call returns or throws. */
function pmRecorder(impl: () => Promise<PMSubmitResult> = delivered) {
  const calls: Array<{ message: string; eventType: string }> = [];
  const submit = (message: string, eventType: string): Promise<PMSubmitResult> => {
    calls.push({ message, eventType });
    return impl();
  };
  return { calls, submit };
}

/** An hourly reminder that lives for 24h from T0, so its warning opens at T0+12h. */
function dailyReminder(overrides: Record<string, unknown> = {}) {
  return normalized({ interval_s: 3600, until_at: new Date(T0 + DAY).toISOString(), ...overrides });
}

test("pure helpers: the warning lead, the final post of a long-interval row, and the IST message", () => {
  assert.equal(ALERT_REMINDER_EXPIRY_WARN_LEAD_MS, 12 * HOUR);
  assert.equal(reminderEndsAt({ next_at: T0 + HOUR, until_at: T0 + DAY, interval_s: 3600 }), T0 + DAY, "on the grid: until itself");
  assert.equal(reminderEndsAt({ next_at: T0 + DAY, until_at: T0 + 3 * DAY + 2 * HOUR, interval_s: 86_400 }), T0 + 3 * DAY, "long interval: the last grid point, not until");
  assert.equal(reminderEndsAt({ next_at: T0 + 2 * DAY, until_at: T0 + DAY, interval_s: 3600 }), T0 + DAY, "next_at already past until: until");
  assert.equal(
    expiryWarningMessage({ key: "training-sindhu-r7-hourly", ends_at: Date.parse("2026-11-25T08:10:00Z") }),
    "reminder training-sindhu-r7-hourly expires 2026-11-25 13:40 IST; renew with mop reminder add --replace",
  );
});

test("parseExpiredSince accepts s/m/h/d and bare seconds, and nothing else", () => {
  assert.equal(parseExpiredSince("48h"), 48 * HOUR);
  assert.equal(parseExpiredSince(" 2d "), 2 * DAY);
  assert.equal(parseExpiredSince("90m"), 90 * MIN);
  assert.equal(parseExpiredSince("30s"), 30_000);
  assert.equal(parseExpiredSince("45"), 45_000);
  for (const bad of ["", "soon", "-1h", "0h", "1.5h", "48 h", "999d"]) {
    assert.equal(parseExpiredSince(bad), null, `${JSON.stringify(bad)} is refused`);
  }
});

test("the warning posts one line to PM once the final post is within 12h, and never again for that until_at", async () => {
  const clock = { now: T0 };
  const pm = pmRecorder();
  const { store, scheduler, events } = schedulerFixture(clock, undefined, pm.submit);
  store.add(dailyReminder(), T0);

  clock.now = T0 + 12 * HOUR - MIN;
  assert.deepEqual((await scheduler.tick()).expiry_warned, [], "13 minutes short of the lead: no warning yet");
  assert.equal(pm.calls.length, 0);

  clock.now = T0 + 12 * HOUR;
  const result = await scheduler.tick();
  assert.deepEqual(result.expiry_warned, ["k1"]);
  assert.equal(pm.calls.length, 1);
  assert.equal(pm.calls[0].message, "reminder k1 expires 2026-11-09 15:30 IST; renew with mop reminder add --replace");
  assert.equal(pm.calls[0].eventType, `cadence-reminder-expiry-k1-${T0 + DAY}`);
  assert.equal(store.get("k1")!.expiry_warned_until_at, T0 + DAY, "the claim records the until_at it warned for");
  await flush();
  assert.ok(events.some((e) => e.type === "alert_reminder_expiry_warned" && e.payload.key === "k1"));
  assert.equal(events.some((e) => e.type === "alert_reminder_expiry_warn_queued"), false, "a delivered receipt is not logged as queued");

  for (const offset of [30_000, 20 * MIN, 6 * HOUR, 11 * HOUR]) {
    clock.now = T0 + 12 * HOUR + offset;
    assert.deepEqual((await scheduler.tick()).expiry_warned, []);
  }
  assert.equal(pm.calls.length, 1, "exactly one warning per until_at");
});

test("no warning for a reminder with a stop obligation, one that ends later than 12h out, or a lifetime of 12h or less", async () => {
  const clock = { now: T0 };
  const pm = pmRecorder();
  const { store, scheduler } = schedulerFixture(clock, () => "open", pm.submit);
  store.add(dailyReminder({ key: "watched", stop_obligation_id: 5 }), T0, () => null);
  store.add(normalized({ key: "far", interval_s: 3600, until_at: new Date(T0 + 4 * DAY).toISOString() }), T0);
  store.add(normalized({ key: "short", interval_s: 3600, until_at: new Date(T0 + 12 * HOUR).toISOString() }), T0);
  store.add(normalized({ key: "shorter", interval_s: 3600, until_at: new Date(T0 + 6 * HOUR).toISOString() }), T0);

  for (const at of [T0 + HOUR, T0 + 5 * HOUR, T0 + 11 * HOUR, T0 + 12 * HOUR + 30 * MIN, T0 + 20 * HOUR]) {
    clock.now = at;
    assert.deepEqual((await scheduler.tick()).expiry_warned, [], `nothing to warn at +${(at - T0) / HOUR}h`);
  }
  assert.equal(pm.calls.length, 0);
  for (const key of ["watched", "far", "short", "shorter"]) assert.equal(store.get(key)!.expiry_warned_until_at, null, key);
});

test("a long-interval reminder is warned from its final post, not from until_at", async () => {
  const clock = { now: T0 };
  const pm = pmRecorder();
  const { store, scheduler } = schedulerFixture(clock, undefined, pm.submit);
  // Daily posts; until is 2h past the third grid point, so the last post (and the expiry) is at T0+3d.
  store.add(normalized({ key: "daily", interval_s: 86_400, until_at: new Date(T0 + 3 * DAY + 2 * HOUR).toISOString() }), T0);

  clock.now = T0 + 3 * DAY - 13 * HOUR;
  assert.deepEqual((await scheduler.tick()).expiry_warned, []);
  clock.now = T0 + 3 * DAY - 12 * HOUR;
  assert.deepEqual((await scheduler.tick()).expiry_warned, ["daily"]);
  assert.equal(pm.calls[0].message, "reminder daily expires 2026-11-11 15:30 IST; renew with mop reminder add --replace");
});

test("renewing with a new until_at re-arms the warning for the new bound", async () => {
  const clock = { now: T0 };
  const pm = pmRecorder();
  const { store, scheduler } = schedulerFixture(clock, undefined, pm.submit);
  store.add(dailyReminder(), T0);

  clock.now = T0 + 12 * HOUR;
  await scheduler.tick();
  assert.equal(pm.calls.length, 1);

  clock.now = T0 + 13 * HOUR;
  const renewed = store.add(
    normalized({ interval_s: 3600, until_at: new Date(T0 + 37 * HOUR).toISOString(), replace: true }, clock.now),
    clock.now,
  );
  assert.ok(renewed.ok);
  clock.now = T0 + 24 * HOUR;
  assert.deepEqual((await scheduler.tick()).expiry_warned, [], "the new final post is more than 12h away");
  clock.now = T0 + 26 * HOUR;
  assert.deepEqual((await scheduler.tick()).expiry_warned, ["k1"]);
  assert.equal(pm.calls.length, 2);
  assert.equal(pm.calls[1].eventType, `cadence-reminder-expiry-k1-${T0 + 37 * HOUR}`);
  assert.notEqual(pm.calls[0].eventType, pm.calls[1].eventType);
});

test("the warning's queue id carries the full key and cutoff losslessly, so distinct reminders never share a PM queue row", async () => {
  const until = T0 + DAY;
  const keys = ["a:b", "a-b", "a.b", "a~3Ab", "A:B", "k1", "k1-2", "training-sindhu-r7-hourly", "é", "a b", "日本", "~", "~~"];
  const ids = keys.map((key) => expiryEventType({ key, until_at: until }));
  assert.equal(new Set(ids).size, keys.length, "every key maps to its own id");
  for (const [i, key] of keys.entries()) {
    assert.ok(ids[i].startsWith("cadence-reminder-expiry-"), "stays a durable cadence event");
    assert.match(ids[i], /^[A-Za-z0-9._~-]+$/, "the id is plain ASCII");
    assert.deepEqual(parseExpiryEventType(ids[i]), { key, until_at: until }, `round-trips ${JSON.stringify(key)}`);
  }
  assert.equal(expiryEventType({ key: "a:b", until_at: until }), `cadence-reminder-expiry-a~3Ab-${until}`);
  assert.equal(expiryEventType({ key: "a-b", until_at: until }), `cadence-reminder-expiry-a-b-${until}`);
  assert.notEqual(expiryEventType({ key: "k1", until_at: until }), expiryEventType({ key: "k1", until_at: until + 1 }), "the cutoff is part of the id");

  for (const bad of [
    "cadence-hourly-heartbeat-2026-11-13:02",
    "cadence-reminder-expiry-",
    "cadence-reminder-expiry-k1",
    "cadence-reminder-expiry-k1-",
    "cadence-reminder-expiry--5",
    "cadence-reminder-expiry-k1-0",
    "cadence-reminder-expiry-k1-05",
    "cadence-reminder-expiry-k1-1e3",
    "cadence-reminder-expiry-k1-99999999999999999",
    "cadence-reminder-expiry-a:b-5",
    "cadence-reminder-expiry-a~3ab-5",
    "cadence-reminder-expiry-~ZZ-5",
    "cadence-reminder-expiry-~FF-5",
    "cadence-reminder-expiry-a~41-5",
  ]) {
    assert.equal(parseExpiryEventType(bad), null, `${bad} is not a canonical expiry id`);
  }

  // Two reminders with the same cutoff warned in one tick get two PM submits under two ids (the old
  // hyphen-mapped id was identical for these, and the queue's (slot, event_type) key replaced the first).
  const clock = { now: T0 };
  const pm = pmRecorder();
  const { store, scheduler } = schedulerFixture(clock, undefined, pm.submit);
  store.add(dailyReminder({ key: "dup:key" }), T0);
  store.add(dailyReminder({ key: "dup-key" }), T0);
  clock.now = T0 + 12 * HOUR;
  assert.deepEqual((await scheduler.tick()).expiry_warned.sort(), ["dup-key", "dup:key"]);
  assert.equal(pm.calls.length, 2);
  assert.notEqual(pm.calls[0].eventType, pm.calls[1].eventType);
  for (const call of pm.calls) {
    const parsed = parseExpiryEventType(call.eventType)!;
    assert.ok(call.message.includes(`reminder ${parsed.key} expires`), "each id names the reminder its payload is for");
  }
});

test("revalidateExpiryWarning delivers only for the exact key, active, unwatched, same cutoff, inside the window and before the final post", () => {
  const { store } = memoryStore();
  store.add(dailyReminder(), T0);
  store.add(dailyReminder({ key: "watched", stop_obligation_id: 5 }), T0, () => null);
  const id = expiryEventType({ key: "k1", until_at: T0 + DAY });
  const text = expiryWarningMessage({ key: "k1", ends_at: T0 + DAY });
  const verdict = (eventType: string, payload: string | null, at: number) => revalidateExpiryWarning(store, eventType, payload, at);

  assert.equal(verdict(id, text, T0 + 12 * HOUR), "deliver", "the window opens 12h before the final post");
  assert.equal(verdict(id, text, T0 + DAY - 1), "deliver", "still deliverable until the final post");
  assert.equal(verdict(id, `MoP: ${text}`, T0 + 13 * HOUR), "deliver", "a wrapped payload that still contains the exact line");
  assert.deepEqual(verdict(id, text, T0 + 12 * HOUR - 1), { drop: "reminder-outside-warning-window" });
  assert.deepEqual(verdict(id, text, T0 + DAY), { drop: "reminder-already-expired" }, "after expiry the expiry listing reports it");
  assert.deepEqual(verdict(id, text, T0 + DAY + 6 * HOUR), { drop: "reminder-already-expired" });
  assert.deepEqual(verdict(id, null, T0 + 13 * HOUR), { drop: "expiry-warning-stale-message" });
  assert.deepEqual(
    verdict(id, expiryWarningMessage({ key: "k1", ends_at: T0 + DAY - HOUR }), T0 + 13 * HOUR),
    { drop: "expiry-warning-stale-message" },
    "an old date is never presented as actionable",
  );
  assert.deepEqual(verdict("cadence-reminder-expiry-nope", text, T0 + 13 * HOUR), { drop: "expiry-warning-unrecognized-id" });
  assert.deepEqual(verdict(expiryEventType({ key: "missing", until_at: T0 + DAY }), text, T0 + 13 * HOUR), { drop: "reminder-missing" });
  assert.deepEqual(
    verdict(expiryEventType({ key: "watched", until_at: T0 + DAY }), expiryWarningMessage({ key: "watched", ends_at: T0 + DAY }), T0 + 13 * HOUR),
    { drop: "reminder-watched-by-obligation" },
  );
  assert.deepEqual(
    verdict(expiryEventType({ key: "k1", until_at: T0 + DAY + 1 }), text, T0 + 13 * HOUR),
    { drop: "reminder-cutoff-changed" },
    "the id names a different cutoff than the row's",
  );

  // Renewed after the claim: the older payload is dropped even though the new cutoff is inside its own window.
  const renewedAt = T0 + 13 * HOUR;
  const renewed = store.add(
    normalized({ interval_s: 3600, until_at: new Date(T0 + 37 * HOUR).toISOString(), replace: true }, renewedAt),
    renewedAt,
  );
  assert.ok(renewed.ok);
  assert.deepEqual(verdict(id, text, T0 + 26 * HOUR), { drop: "reminder-cutoff-changed" });
  assert.equal(
    verdict(expiryEventType({ key: "k1", until_at: T0 + 37 * HOUR }), expiryWarningMessage({ key: "k1", ends_at: T0 + 37 * HOUR }), T0 + 26 * HOUR),
    "deliver",
    "the renewed cutoff's own warning is still deliverable",
  );

  store.stop("k1", "cancelled", T0 + 27 * HOUR);
  assert.deepEqual(
    verdict(expiryEventType({ key: "k1", until_at: T0 + 37 * HOUR }), expiryWarningMessage({ key: "k1", ends_at: T0 + 37 * HOUR }), T0 + 28 * HOUR),
    { drop: "reminder-not-active" },
  );
});

test("revalidateExpiryWarning uses the final grid post, not until_at, for a long-interval reminder", () => {
  const { store } = memoryStore();
  const until = T0 + 3 * DAY + 2 * HOUR;
  store.add(normalized({ key: "daily", interval_s: 86_400, until_at: new Date(until).toISOString() }), T0);
  const id = expiryEventType({ key: "daily", until_at: until });
  const text = expiryWarningMessage({ key: "daily", ends_at: T0 + 3 * DAY });
  assert.equal(revalidateExpiryWarning(store, id, text, T0 + 3 * DAY - HOUR), "deliver");
  assert.deepEqual(
    revalidateExpiryWarning(store, id, text, T0 + 3 * DAY + HOUR),
    { drop: "reminder-already-expired" },
    "the last post has passed even though until_at has not",
  );
});

test("PMCadenceScheduler revalidates a queued expiry warning at the delivery boundary, retains on an unreadable store, and leaves other events alone", () => {
  const { store } = memoryStore();
  const now = Date.now();
  store.add(normalized({ key: "live", interval_s: 1800, until_at: new Date(now + 6 * HOUR).toISOString() }, now), now);
  const row = store.get("live")!;
  const ends = reminderEndsAt(row);
  const id = expiryEventType({ key: "live", until_at: row.until_at });
  const text = expiryWarningMessage({ key: "live", ends_at: ends });

  type QueuedRow = { slot: number; event_type: string; enqueued_at: string; payload?: string | null };
  const wired: { check?: (row: QueuedRow) => unknown } = {};
  const relay = {
    submitToPM: async () => ({ ok: true }),
    setPMQueuedEffectCheck: (fn: (row: QueuedRow) => unknown) => { wired.check = fn; },
  };
  let storeThrows = false;
  const config = new Map<string, string>();
  const db = {
    getConfig: (k: string) => config.get(k) ?? null,
    setConfig: (k: string, v: string) => config.set(k, v),
    logEvent: () => undefined,
    hasPMQueueDelivery: () => false,
    alertReminderStore: () => {
      if (storeThrows) throw new Error("database is locked");
      return store;
    },
  };
  const cadence = new PMCadenceScheduler(db as never, relay as never);
  const queuedCheck = wired.check;
  assert.ok(queuedCheck, "the cadence scheduler owns the queue's pre-paste check");

  // Through the wired check: the queued payload reaches revalidation.
  assert.equal(queuedCheck({ slot: 0, event_type: id, enqueued_at: "x", payload: text }), "deliver");
  assert.deepEqual(queuedCheck({ slot: 0, event_type: id, enqueued_at: "x", payload: "reminder live expires 2020-01-01 00:00 IST; renew with mop reminder add --replace" }), { drop: "expiry-warning-stale-message" });
  assert.deepEqual(queuedCheck({ slot: 0, event_type: id, enqueued_at: "x" }), { drop: "expiry-warning-stale-message" }, "no payload, no actionable warning");

  // The direct method, with an explicit clock.
  assert.equal(cadence.revalidateQueuedReminder(id, new Date(now), text), "deliver");
  assert.deepEqual(cadence.revalidateQueuedReminder(id, new Date(ends + 1), text), { drop: "reminder-already-expired" });
  store.stop("live", "cancelled", now + MIN);
  assert.deepEqual(cadence.revalidateQueuedReminder(id, new Date(now + 2 * MIN), text), { drop: "reminder-not-active" });

  // An unreadable reminder store never drops the row: it is retained for the next delivery attempt.
  storeThrows = true;
  assert.equal(cadence.revalidateQueuedReminder(id, new Date(now), text), "retain");
  assert.equal(queuedCheck({ slot: 0, event_type: id, enqueued_at: "x", payload: text }), "retain");

  // Non-expiry events never touch the reminder store.
  assert.equal(cadence.revalidateQueuedReminder("something-else-entirely", new Date(now)), "deliver");
});

test("every unconfirmed outcome stays consumed: a throw, a rejection, an ambiguous, withheld or refused receipt is never retried", async () => {
  const cases: Array<{ name: string; impl: () => Promise<PMSubmitResult>; type: string; outcome?: string }> = [
    { name: "sync throw", impl: () => { throw new Error("pane gone"); }, type: "alert_reminder_expiry_warn_unconfirmed", outcome: "threw" },
    { name: "rejection", impl: () => Promise.reject(new Error("socket reset")), type: "alert_reminder_expiry_warn_unconfirmed", outcome: "threw" },
    { name: "ambiguous", impl: async () => ({ ok: true, submitKey: "Enter", ambiguous: true }), type: "alert_reminder_expiry_warn_unconfirmed", outcome: "ambiguous" },
    { name: "withheld", impl: async () => ({ ok: true, submitKey: "Enter", ambiguous: false, withheld: "guard" }), type: "alert_reminder_expiry_warn_unconfirmed", outcome: "withheld" },
    { name: "refused", impl: async () => ({ ok: false, submitKey: "Enter", ambiguous: false, refused: "guard" }), type: "alert_reminder_expiry_warn_unconfirmed", outcome: "refused" },
    { name: "not delivered", impl: async () => ({ ok: false, submitKey: "Enter", ambiguous: false }), type: "alert_reminder_expiry_warn_unconfirmed", outcome: "not_delivered" },
    // Queued is not delivered: it is logged as queued, never as warned, and the claim stays consumed.
    { name: "queued", impl: async () => ({ ok: true, submitKey: "Enter", ambiguous: false, queued: true }), type: "alert_reminder_expiry_warn_queued" },
  ];
  for (const c of cases) {
    const clock = { now: T0 };
    const pm = pmRecorder(c.impl);
    const { store, scheduler, events } = schedulerFixture(clock, undefined, pm.submit);
    store.add(dailyReminder(), T0);

    clock.now = T0 + 12 * HOUR;
    const first = await scheduler.tick();
    assert.deepEqual(first.expiry_warned, ["k1"], c.name);
    await flush();
    const logged = events.find((e) => e.type.startsWith("alert_reminder_expiry_warn"));
    assert.equal(logged?.type, c.type, c.name);
    if (c.outcome) assert.equal(logged?.payload.outcome, c.outcome, c.name);

    for (const offset of [MIN, HOUR, 8 * HOUR]) {
      clock.now = T0 + 12 * HOUR + offset;
      assert.deepEqual((await scheduler.tick()).expiry_warned, [], `${c.name}: not retried`);
    }
    assert.equal(pm.calls.length, 1, `${c.name}: one submit in total`);
    assert.equal(events.some((e) => e.type === "alert_reminder_expiry_warn_error"), false, c.name);
  }
});

test("without a PM submit nothing is claimed, so the warning is still available once one is wired", async () => {
  const clock = { now: T0 };
  const { store, scheduler } = schedulerFixture(clock);
  store.add(dailyReminder(), T0);
  clock.now = T0 + 12 * HOUR;
  assert.deepEqual((await scheduler.tick()).expiry_warned, []);
  assert.equal(store.get("k1")!.expiry_warned_until_at, null);
});

test("a PM submit that never answers does not hold up the post tick, and the warning does not touch claim fencing", async () => {
  const clock = { now: T0 };
  const pm = pmRecorder(() => new Promise<PMSubmitResult>(() => undefined));
  const { store, poster, scheduler } = schedulerFixture(clock, undefined, pm.submit);
  store.add(dailyReminder(), T0);

  clock.now = T0 + 12 * HOUR;
  const before = store.get("k1")!;
  let timer: NodeJS.Timeout | undefined;
  const guard = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error("tick waited on the PM submit")), 2_000);
  });
  const result = await Promise.race([scheduler.tick(), guard]);
  clearTimeout(timer);
  assert.deepEqual(result.expiry_warned, ["k1"]);
  assert.deepEqual(result.posted, ["k1"], "the due post still goes out in the same tick");
  assert.equal(poster.calls.length, 1);
  const after = store.get("k1")!;
  assert.equal(after.claim_gen, before.claim_gen + 1, "only the post's own claim advanced the generation");
  assert.equal(after.expiry_warned_until_at, T0 + DAY);
});

test("expired_since lists reminders that expired with no stop obligation inside the window, newest first", async () => {
  const clock = { now: T0 };
  const { store, scheduler } = schedulerFixture(clock);
  const app = new Hono();
  registerAlertReminderRoutes(app, { store, scheduler, now: () => clock.now, obligationState: () => "open" });
  const lapsing = (key: string, nowMs: number, extra: Record<string, unknown> = {}) =>
    normalized({ key, interval_s: 600, until_at: new Date(nowMs + 11 * MIN).toISOString(), ...extra }, nowMs);

  store.add(lapsing("lapsed-old", T0), T0);
  store.expireStale(T0 + HOUR);

  const t1 = T0 + 60 * HOUR;
  store.add(lapsing("lapsed-recent", t1), t1);
  store.add(lapsing("lapsed-watched", t1, { stop_obligation_id: 7 }), t1, () => null);
  store.add(lapsing("cancelled", t1), t1);
  store.add(normalized({ key: "still-active", until_at: new Date(t1 + 6 * HOUR).toISOString() }, t1), t1);
  store.stop("cancelled", "cancelled", t1 + MIN);
  store.expireStale(t1 + HOUR);

  clock.now = t1 + 2 * HOUR;
  const recent = (await (await app.request("/alert-reminders?expired_since=48h")).json()) as {
    success: boolean;
    expired_since_ms: number;
    reminders: Array<{ key: string; status: string; stop_reason: string }>;
    scheduler: unknown;
  };
  assert.equal(recent.success, true);
  assert.equal(recent.expired_since_ms, 48 * HOUR);
  assert.deepEqual(recent.reminders.map((r) => r.key), ["lapsed-recent"], "not the watched, cancelled, active or long-expired rows");
  assert.equal(recent.reminders[0].status, "expired");
  assert.equal(recent.reminders[0].stop_reason, "until_passed");
  assert.ok(recent.scheduler);

  const wide = (await (await app.request("/alert-reminders?expired_since=100h")).json()) as { reminders: Array<{ key: string }> };
  assert.deepEqual(wide.reminders.map((r) => r.key), ["lapsed-recent", "lapsed-old"], "newest expiry first");

  for (const bad of ["soon", "0h", "999d", "-5h"]) {
    const refused = await app.request(`/alert-reminders?expired_since=${encodeURIComponent(bad)}`);
    assert.equal(refused.status, 400, bad);
    assert.equal(((await refused.json()) as { success: boolean }).success, false, bad);
  }
  // The existing status listing is unchanged.
  const active = (await (await app.request("/alert-reminders")).json()) as { reminders: Array<{ key: string }> };
  assert.deepEqual(active.reminders.map((r) => r.key), ["still-active"]);
});

test("a table from before the warning column gains it in place and the old reminder keeps working", () => {
  const db = new Database(":memory:");
  db.exec(V1_DDL);
  db.prepare(
    `INSERT INTO alert_reminders (key, thread_ts, text, interval_s, next_at, until_at, status, created_by, created_at, updated_at)
     VALUES ('old', ?, 'check', 3600, ?, ?, 'active', 'cli', ?, ?)`,
  ).run(THREAD, T0 + HOUR, T0 + DAY, T0, T0);
  ensureAlertReminderSchema(db);
  ensureAlertReminderSchema(db);
  const store = new AlertReminderStore(db);
  assert.equal(store.get("old")!.expiry_warned_until_at, null);
  assert.deepEqual(store.claimExpiryWarnings(T0 + 12 * HOUR).map((w) => w.key), ["old"]);
  assert.deepEqual(store.claimExpiryWarnings(T0 + 12 * HOUR), [], "claimed once, not again");
});

// Rajiv C0ALZJHGE49 ts 1795323488.236789: "change the alert to spark up or down. move it to top level thread in dev channel."
// An EMPTY thread_ts is the "channel root" sentinel: chat.postMessage must then carry NO thread_ts key at all, because sending
// an empty string is not the same request as omitting it.
test("an empty thread_ts posts to the channel root and omits thread_ts entirely", async () => {
  const envDir = mkdtempSync(join(tmpdir(), "alert-env-"));
  const envFile = join(envDir, "alerts.env");
  writeFileSync(envFile, "SLACK_ALERTS_BOT_TOKEN=xoxb-alerts-secret\n");
  const seen: Array<{ body: Record<string, unknown> }> = [];
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    seen.push({ body: JSON.parse(String(init.body)) });
    return { status: 200, json: async () => ({ ok: true, ts: "1794000000.000200" }) } as Response;
  }) as unknown as typeof fetch;
  const poster = new SlackAlertsPoster(envFile, fetchImpl);
  assert.deepEqual(await poster.post("C0ALZJHGE49", "", "hello"), { ok: true, ts: "1794000000.000200" });
  assert.equal(seen[0].body.channel, "C0ALZJHGE49");
  assert.equal("thread_ts" in seen[0].body, false, "a top-level post must omit thread_ts, not send an empty one");
});
