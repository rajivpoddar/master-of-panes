import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { MoPDatabase } from "../src/db.js";
import { registerRetiredClearRefusals, registerSessionClearRoute } from "../src/sessionClearRoute.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const CAPABILITY = "a".repeat(64);
const PANE = {
  slot: 1,
  address: "0:0.1",
  paneId: "%101",
  currentPath: "/fixture/checkout",
  expectedPath: "/fixture/checkout",
};

function headers(auth = true): Headers {
  const result = new Headers({ "content-type": "application/json" });
  if (auth) {
    result.set("x-heydonna-direct-client", "mop-release-assign-v1");
    result.set("x-mop-capability", CAPABILITY);
  }
  return result;
}

async function setupRoute(
  relay: TmuxRelay,
  db: MoPDatabase,
): Promise<Hono> {
  const app = new Hono();
  registerSessionClearRoute(app, {
    db,
    relay,
    verifyPane: async () => ({ ok: true, snapshot: PANE }),
    observeCheckout: async () => ({
      checkout_path: PANE.currentPath,
      clean: true,
      unpushed_commits: [],
      branch: "main",
      head: "b".repeat(40),
    }),
  });
  return app;
}

function body(sessionStartedAt: string, requestToken: string): Record<string, unknown> {
  return {
    expected_epoch: 0,
    expected_session_id: "session-1",
    expected_session_started_at: sessionStartedAt,
    expected_age_seconds: 7 * 60 * 60,
    checkout_path: PANE.currentPath,
    checkout_branch: "main",
    checkout_head: "b".repeat(40),
    request_token: requestToken,
  };
}

function jsonRequest(requestBody: Record<string, unknown>, authenticated = true): RequestInit {
  return {
    method: "POST",
    headers: headers(authenticated),
    body: JSON.stringify(requestBody),
  };
}

test("session clear is authenticated, final-fenced, durable, and replay-safe", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-session-clear-route-"));
  const previousCapability = process.env.MOP_LOCAL_CAPABILITY;
  process.env.MOP_LOCAL_CAPABILITY = CAPABILITY;
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.startAgentTurn(1, "session-1");
    db.finishAgentTurn(1, "session-1");
    db.updateSlot(1, { session_started_at: new Date(Date.now() - 7 * 60 * 60 * 1000).toISOString() });
    const current = db.getSlot(1);
    assert.ok(current?.session_started_at);

    let paneEffects = 0;
    const relay = {
      sendToSlotAsync: async (
        _slot: number,
        _command: string,
        _force?: boolean,
        _raw?: boolean,
        options?: { beforeFirstEffect?: (pane: typeof PANE) => boolean },
      ) => {
        const allowed = options?.beforeFirstEffect?.(PANE) ?? false;
        if (allowed) paneEffects += 1;
        return allowed;
      },
    } as unknown as TmuxRelay;
    const app = await setupRoute(relay, db);

    const unauthenticated = await app.request(
      "/slots/1/session/clear",
      jsonRequest(body(current.session_started_at, "unauthenticated"), false),
    );
    assert.equal(unauthenticated.status, 403);
    assert.equal(db.getSessionClearEffect("unauthenticated"), null);
    assert.equal(paneEffects, 0);

    const first = await app.request(
      "/slots/1/session/clear",
      jsonRequest(body(current.session_started_at, "clear-1")),
    );
    assert.equal(first.status, 200);
    const firstResult = await first.json();
    assert.equal(firstResult.success, true);
    assert.equal(firstResult.effect, true);
    assert.equal(firstResult.idempotent, false);
    assert.equal(paneEffects, 1);
    assert.equal(db.getSessionClearEffect("clear-1")?.status, "completed");

    const replay = await app.request(
      "/slots/1/session/clear",
      jsonRequest(body(current.session_started_at, "clear-1")),
    );
    assert.equal(replay.status, 200);
    const replayResult = await replay.json();
    assert.equal(replayResult.idempotent, true);
    assert.equal(replayResult.effect, false);
    assert.equal(paneEffects, 1);

    db.updateSlot(1, { dnd: true });
    const drift = await app.request(
      "/slots/1/session/clear",
      jsonRequest(body(current.session_started_at, "clear-drift")),
    );
    assert.equal(drift.status, 409);
    assert.equal(db.getSessionClearEffect("clear-drift"), null);
    assert.equal(paneEffects, 1);
    db.close();
  } finally {
    if (previousCapability === undefined) delete process.env.MOP_LOCAL_CAPABILITY;
    else process.env.MOP_LOCAL_CAPABILITY = previousCapability;
    rmSync(directory, { recursive: true, force: true });
  }
});

test("broad clear aliases refuse before parsing and cannot reach pane delivery", async () => {
  const app = new Hono();
  registerRetiredClearRefusals(app);
  for (const path of ["/clear", "/slots/1/clear"]) {
    const response = await app.request(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "not-json",
    });
    assert.equal(response.status, 410);
    assert.deepEqual(await response.json(), {
      success: false,
      effect: false,
      code: "session_clear_exact_route_required",
    });
  }
});

test("an effect-start response loss becomes permanent ambiguity and cannot redeliver", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-session-clear-ambiguous-"));
  const previousCapability = process.env.MOP_LOCAL_CAPABILITY;
  process.env.MOP_LOCAL_CAPABILITY = CAPABILITY;
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.startAgentTurn(1, "session-1");
    db.finishAgentTurn(1, "session-1");
    db.updateSlot(1, { session_started_at: new Date(Date.now() - 7 * 60 * 60 * 1000).toISOString() });
    const current = db.getSlot(1);
    assert.ok(current?.session_started_at);
    let paneEffects = 0;
    const relay = {
      sendToSlotAsync: async (
        _slot: number,
        _command: string,
        _force?: boolean,
        _raw?: boolean,
        options?: { beforeFirstEffect?: (pane: typeof PANE) => boolean },
      ) => {
        const allowed = options?.beforeFirstEffect?.(PANE) ?? false;
        if (allowed) paneEffects += 1;
        return false;
      },
    } as unknown as TmuxRelay;
    const app = await setupRoute(relay, db);
    const request = jsonRequest(body(current.session_started_at, "clear-ambiguous"));

    const first = await app.request("/slots/1/session/clear", request);
    assert.equal(first.status, 503);
    assert.equal((await first.json()).code, "session_clear_effect_ambiguous");
    assert.equal(db.getSessionClearEffect("clear-ambiguous")?.status, "ambiguous");
    assert.equal(paneEffects, 1);

    const replay = await app.request("/slots/1/session/clear", jsonRequest(body(current.session_started_at, "clear-ambiguous")));
    assert.equal(replay.status, 503);
    assert.equal((await replay.json()).code, "session_clear_effect_ambiguous");
    assert.equal(paneEffects, 1);
    db.close();
  } finally {
    if (previousCapability === undefined) delete process.env.MOP_LOCAL_CAPABILITY;
    else process.env.MOP_LOCAL_CAPABILITY = previousCapability;
    rmSync(directory, { recursive: true, force: true });
  }
});
