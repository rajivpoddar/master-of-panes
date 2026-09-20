import assert from "node:assert/strict";

/** Value the retired authority header used to carry; must never be required. */
const RETIRED_AUTHORITY_VALUE = "pm-transition-v1";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { Hono } from "hono";
import Database from "better-sqlite3";

import {
  authorizeWedgeInterrupt,
  registerWedgeInterruptRoute,
  snapshotShowsIdlePromptWithQueuedInput,
  WEDGE_INTERRUPT_KEY,
  WEDGE_INTERRUPT_QUEUE_DISPOSITION,
  WEDGE_DWELL_FLOOR_MS,
  WEDGE_DWELL_RULE,
  WEDGE_WATCHDOG_DELIVERY,
  WEDGED_BUSY_REMEDIATION,
} from "../src/wedgeInterruptRoute.js";
import { MoPDatabase } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

const WEDGE_SNAPSHOT = [
  "  │ Hedradra · Opus 4.8  │ main  │",
  "  │ › Working…                                                    │",
  "  │ Press up to edit queued messages                              │",
].join("\n");

interface FakeState {
  sends: Array<{ slotNum: number; key: string }>;
  identityOk: boolean;
  snapshot: string | null;
  gateSend: (() => void) | null;
  sendStarted: boolean;
  checkout: { head: string | null; branch: string | null } | null;
}

const CHECKOUT_PATH = "/Users/rajiv/Downloads/projects/heydonna-app-3001";
const CHECKOUT_HEAD = "1".repeat(40);

function fixture() {
  const directory = mkdtempSync(join(tmpdir(), "mop-wedge-interrupt-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const state: FakeState = {
    sends: [],
    identityOk: true,
    snapshot: WEDGE_SNAPSHOT,
    gateSend: null,
    sendStarted: false,
    checkout: { head: CHECKOUT_HEAD, branch: "fix/7748-thing" },
  };
  const app = new Hono();
  const NOW = Date.parse("2026-09-19T10:00:00.000Z");
  registerWedgeInterruptRoute(app, {
    db,
    verifyPaneIdentity: async (slotNum: number) =>
      state.identityOk
        ? { ok: true as const, snapshot: { paneId: `%${slotNum}`, currentPath: CHECKOUT_PATH } }
        : { ok: false as const, detail: "pane gone" },
    captureSnapshot: async () => state.snapshot,
    observeCheckout: async () => state.checkout,
    sendInterruptKey: async (slotNum: number) => {
      state.sends.push({ slotNum, key: WEDGE_INTERRUPT_KEY });
      state.sendStarted = true;
      if (state.gateSend) await new Promise<void>((resolve) => { state.gateSend = resolve; });
      return true;
    },
    nowMs: () => NOW,
  });
  return { app, db, directory, state, NOW };
}

async function busySlot(db: MoPDatabase, quietMs: number, nowMs: number) {
  const assigned = db.assignIssueToSlot(1, 10, "wedge fixture", "heydonna-app/heydonna-app") as {
    ok: boolean; assignment_epoch: number;
  };
  assert.equal(assigned.ok, true);
  db.startAgentTurn(1, "turn-abc");
  const old = new Date(nowMs - quietMs).toISOString();
  db.updateSlot(1, { active_turn_started_at: old, last_meaningful_work_at: old });
  return { epoch: assigned.assignment_epoch, turn: "turn-abc" };
}

/**
 * MoP retired the x-heydonna-assignment-authority header (single-user local
 * tool). The first argument is kept so the existing call sites stay readable,
 * but no authority header is ever sent — every conjunct the route enforces is
 * server-verified evidence, not a caller attestation.
 */
function interruptRequest(_retiredAuthority: string | undefined, body: unknown) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  } as RequestInit;
}

const STALE_AUTHORITY_HEADER = { "x-heydonna-assignment-authority": "pm-transition-v1" };

function signalEvents(db: MoPDatabase) {
  return db.getEvents(1, 50, "interrupt_signal_sent");
}


test("escape procedure documents the watchdog relaunch as the delivery path", () => {
  // The live sequence that worked: park at shell -> watchdog relaunch with
  // --continue -> queued continuation delivered. That relaunch is sanctioned.
  assert.match(WEDGE_WATCHDOG_DELIVERY, /watchdog/i);
  assert.match(WEDGE_WATCHDOG_DELIVERY, /--continue/);
  assert.match(WEDGE_WATCHDOG_DELIVERY, /sanctioned/i);
  assert.match(WEDGE_WATCHDOG_DELIVERY, /at or before/i);
  // The same guidance must be reachable from the respawn busy refusal.
  assert.match(WEDGED_BUSY_REMEDIATION, /watchdog/i);
  assert.match(WEDGED_BUSY_REMEDIATION, /--continue/);
  assert.match(WEDGED_BUSY_REMEDIATION, /recovery delivery/i);
  // Queue survival is never claimed; the interrupt discards queued input.
  assert.match(WEDGED_BUSY_REMEDIATION, /DISCARDS/);
  assert.doesNotMatch(WEDGED_BUSY_REMEDIATION, /queued input (?:is|will be) (?:preserved|kept|survives)/i);
  const serverSrc = readFileSync(
    fileURLToPath(new URL("../src/server.ts", import.meta.url)),
    "utf8",
  );
  assert.match(serverSrc, /remediation: WEDGED_BUSY_REMEDIATION/);
});


test("movement leg: live HEAD/branch movement or an unreadable checkout refuses", () => {
  const now = Date.parse("2026-09-19T10:00:00.000Z");
  const quietSlot = {
    occupied: true,
    idle: false,
    dnd: false,
    assignment_epoch: 7,
    active_turn_id: "t1",
    head_sha: "a".repeat(40),
    branch: "fix/7748-thing",
    last_meaningful_work_at: new Date(now - 6 * 60 * 1000).toISOString(),
    active_turn_started_at: new Date(now - 10 * 60 * 1000).toISOString(),
  };
  const pins = { expected_assignment_epoch: 7, expected_active_turn_id: "t1" };
  const stationary = { head: "a".repeat(40), branch: "fix/7748-thing" };
  assert.equal(authorizeWedgeInterrupt(quietSlot, pins, WEDGE_SNAPSHOT, now, stationary).ok, true);
  assert.equal(
    authorizeWedgeInterrupt(quietSlot, pins, WEDGE_SNAPSHOT, now, { head: "b".repeat(40), branch: "fix/7748-thing" }).reason,
    "checkout_moved",
  );
  assert.equal(
    authorizeWedgeInterrupt(quietSlot, pins, WEDGE_SNAPSHOT, now, { head: "a".repeat(40), branch: "main" }).reason,
    "checkout_moved",
  );
  assert.equal(authorizeWedgeInterrupt(quietSlot, pins, WEDGE_SNAPSHOT, now, null).reason, "checkout_movement_unverifiable");
  assert.equal(
    authorizeWedgeInterrupt(quietSlot, pins, WEDGE_SNAPSHOT, now, { head: null, branch: "fix/7748-thing" }).reason,
    "checkout_movement_unverifiable",
  );
  // Queued input alone: a live turn still producing tool progress is refused
  // even with identical HEAD/branch.
  const working = { ...quietSlot, last_meaningful_work_at: new Date(now - 30 * 1000).toISOString() };
  assert.equal(authorizeWedgeInterrupt(working, pins, WEDGE_SNAPSHOT, now, stationary).reason, "wedge_quiet_insufficient");
});

test("route refuses when the checkout observation is indeterminate", async () => {
  const { app, db, directory, state, NOW } = fixture();
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const body = { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn };
    state.checkout = null;
    const res = await app.request("/slots/1/interrupt-turn", interruptRequest(RETIRED_AUTHORITY_VALUE, body));
    assert.equal(res.status, 409);
    const payload = (await res.json()) as Record<string, unknown>;
    assert.equal(payload.reason, "checkout_movement_unverifiable");
    assert.match(String(payload.error), /queued input/i);

    // Restore the observation; a live turn with fresh tool progress still
    // refuses and the refusal text carries the dwell rule.
    state.checkout = { head: CHECKOUT_HEAD, branch: "fix/7748-thing" };
    db.updateSlot(1, { last_meaningful_work_at: new Date(NOW - 30 * 1000).toISOString() });
    const busy = await app.request("/slots/1/interrupt-turn", interruptRequest(RETIRED_AUTHORITY_VALUE, body));
    assert.equal(busy.status, 409);
    const busyBody = (await busy.json()) as Record<string, unknown>;
    assert.equal(busyBody.reason, "wedge_quiet_insufficient");
    assert.match(String(busyBody.error), /Queuing alone is never the trigger/);
    assert.equal(state.sends.length, 0);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("dwell rule: queuing alone never triggers the interrupt", () => {
  assert.equal(WEDGE_DWELL_FLOOR_MS, 3 * 60 * 1000);
  assert.match(WEDGE_DWELL_RULE, /Queued input alone/i);
  assert.match(WEDGE_DWELL_RULE, /HEAD\/branch/);
  assert.match(WEDGE_DWELL_RULE, /tool-name progress/i);
  assert.match(WEDGE_DWELL_RULE, /interrupt FIRST/i);
  assert.match(WEDGE_DWELL_RULE, /only after the dwell/i);
});

test("no force-respawn escape exists; busy respawn stays fail-closed", () => {
  const serverSrc = readFileSync(
    fileURLToPath(new URL("../src/server.ts", import.meta.url)),
    "utf8",
  );
  assert.doesNotMatch(serverSrc, /force_wedged_escape/);
  assert.doesNotMatch(serverSrc, /slot_respawn_wedged_escape/);
  assert.match(serverSrc, /registerWedgeInterruptRoute/);
  assert.match(serverSrc, /reason: "slot_busy_respawn_refused"/);
});

test("pane matcher accepts only the observed wedge shape", () => {
  assert.equal(snapshotShowsIdlePromptWithQueuedInput(WEDGE_SNAPSHOT), true);
  assert.equal(snapshotShowsIdlePromptWithQueuedInput("› Working…\n$ "), false);
  assert.equal(snapshotShowsIdlePromptWithQueuedInput("some queued text without the prompt"), false);
  assert.equal(snapshotShowsIdlePromptWithQueuedInput(null), false);
  assert.equal(snapshotShowsIdlePromptWithQueuedInput(""), false);
});

test("interrupt key is Ctrl-C only", () => {
  assert.equal(WEDGE_INTERRUPT_KEY, "C-c");
  assert.equal(WEDGE_INTERRUPT_QUEUE_DISPOSITION, "discard-queued-input");
});

test("identity, DND, quiet, evidence refusals are typed with zero effects", async () => {
  const { app, db, directory, state, NOW } = fixture();
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const goodBody = { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn };
    const cases: Array<[string, RequestInit, number, string]> = [
      ["no pins", interruptRequest(RETIRED_AUTHORITY_VALUE, {}), 409, "stale_identity"],
      ["wrong epoch", interruptRequest(RETIRED_AUTHORITY_VALUE, { ...goodBody, expected_assignment_epoch: 999 }), 409, "stale_identity"],
      ["wrong turn", interruptRequest(RETIRED_AUTHORITY_VALUE, { ...goodBody, expected_active_turn_id: "turn-other" }), 409, "stale_identity"],
    ];
    for (const [name, init, status, reason] of cases) {
      const res = await app.request("/slots/1/interrupt-turn", init);
      assert.equal(res.status, status, name);
      assert.equal(((await res.json()) as Record<string, unknown>).reason, reason, name);
    }
    // Fabricated caller attestations alone never satisfy: snapshot is the evidence.
    state.snapshot = "› Working…\n$ ";
    const noEvidence = await app.request("/slots/1/interrupt-turn", interruptRequest(RETIRED_AUTHORITY_VALUE, goodBody));
    assert.equal(noEvidence.status, 409);
    assert.equal(((await noEvidence.json()) as Record<string, unknown>).reason, "pane_evidence_unavailable");
    state.snapshot = WEDGE_SNAPSHOT;

    // DND refuses even with perfect pins + evidence.
    db.updateSlot(1, { dnd: true });
    const dnd = await app.request("/slots/1/interrupt-turn", interruptRequest(RETIRED_AUTHORITY_VALUE, goodBody));
    assert.equal(dnd.status, 409);
    assert.equal(((await dnd.json()) as Record<string, unknown>).reason, "slot_dnd_interrupt_refused");
    db.updateSlot(1, { dnd: false });

    // Fresh activity (recent meaningful work) refuses: quiet insufficient.
    db.updateSlot(1, { last_meaningful_work_at: new Date(NOW - 60 * 1000).toISOString() });
    const fresh = await app.request("/slots/1/interrupt-turn", interruptRequest(RETIRED_AUTHORITY_VALUE, goodBody));
    assert.equal(fresh.status, 409);
    assert.equal(((await fresh.json()) as Record<string, unknown>).reason, "wedge_quiet_insufficient");

    assert.equal(state.sends.length, 0);
    assert.equal(signalEvents(db).length, 0);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("identity failure and unsettled slot refuse with zero effects", async () => {
  const { app, db, directory, state, NOW } = fixture();
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const goodBody = { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn };
    state.identityOk = false;
    const res = await app.request("/slots/1/interrupt-turn", interruptRequest(RETIRED_AUTHORITY_VALUE, goodBody));
    assert.equal(res.status, 409);
    assert.equal(((await res.json()) as Record<string, unknown>).reason, "pane_identity_mismatch");
    state.identityOk = true;

    db.finishAgentTurn(1, pins.turn);
    const settled = await app.request("/slots/1/interrupt-turn", interruptRequest(RETIRED_AUTHORITY_VALUE, goodBody));
    assert.equal(settled.status, 409);
    assert.equal(((await settled.json()) as Record<string, unknown>).reason, "slot_not_busy_nothing_to_interrupt");

    assert.equal(state.sends.length, 0);
    assert.equal(signalEvents(db).length, 0);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("verified wedge sends one raw C-c with truthful signal audit", async () => {
  const { app, db, directory, state, NOW } = fixture();
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const res = await app.request(
      "/slots/1/interrupt-turn",
      interruptRequest(RETIRED_AUTHORITY_VALUE, { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn }),
    );
    assert.equal(res.status, 200);
    const body = (await res.json()) as Record<string, unknown>;
    assert.equal(body.success, true);
    assert.equal(body.signal_sent, true);
    assert.equal(body.interrupt_key, "C-c");
    assert.equal(body.queue_disposition, "discard-queued-input");
    assert.match(String(body.followup), /AFTER this interrupt/);
    assert.match(String(body.followup), /watchdog/i);
    assert.equal(body.watchdog_delivery, WEDGE_WATCHDOG_DELIVERY);
    assert.equal(body.movement_verified, true);
    assert.equal(body.dwell_rule, WEDGE_DWELL_RULE);
    assert.equal(body.interrupted, undefined);
    assert.equal(body.assignment_epoch, pins.epoch);

    assert.deepEqual(state.sends, [{ slotNum: 1, key: "C-c" }]);
    const events = signalEvents(db);
    assert.equal(events.length, 1);
    const payload = JSON.parse((events[0] as { payload: string }).payload) as Record<string, unknown>;
    assert.equal(payload.assignment_epoch, pins.epoch);
    assert.equal(payload.active_turn_id, pins.turn);
    assert.equal(payload.interrupt_key, "C-c");
    assert.equal(payload.queue_disposition, "discard-queued-input");
    assert.ok(typeof payload.quiet_ms === "number" && (payload.quiet_ms as number) >= 5 * 60 * 1000);

    // Ownership/epoch untouched by the signal.
    const row = db.getSlot(1);
    assert.equal(row?.occupied, true);
    assert.equal(row?.assignment_epoch, pins.epoch);
    assert.equal(row?.active_turn_id, pins.turn);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("relay failure returns typed 502 with no success audit", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-wedge-interrupt-"));
  const config: MoPConfig = {
    httpPort: 0, mcpTransport: "stdio", dbPath: join(directory, "mop.db"),
    slotCount: 4, pmPaneAddress: "0:0.0", legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const NOW = Date.parse("2026-09-19T10:00:00.000Z");
  let sends = 0;
  const app = new Hono();
  registerWedgeInterruptRoute(app, {
    db,
    verifyPaneIdentity: async (slotNum: number) => ({
      ok: true as const,
      snapshot: { paneId: `%${slotNum}`, currentPath: CHECKOUT_PATH },
    }),
    captureSnapshot: async () => WEDGE_SNAPSHOT,
    observeCheckout: async () => ({ head: CHECKOUT_HEAD, branch: "fix/7748-thing" }),
    sendInterruptKey: async () => { sends += 1; return false; },
    nowMs: () => NOW,
  });
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const res = await app.request(
      "/slots/1/interrupt-turn",
      interruptRequest(RETIRED_AUTHORITY_VALUE, { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn }),
    );
    assert.equal(res.status, 502);
    assert.equal(((await res.json()) as Record<string, unknown>).reason, "interrupt_delivery_failed");
    assert.equal(sends, 1);
    assert.equal(signalEvents(db).length, 0);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("concurrent force requests cannot double-send", async () => {
  const { app, db, directory, state, NOW } = fixture();
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const init = () => interruptRequest(RETIRED_AUTHORITY_VALUE, { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn });
    state.gateSend = (() => {}) as unknown as (() => void);
    const first = app.request("/slots/1/interrupt-turn", init());
    while (!state.sendStarted) await new Promise((r) => setTimeout(r, 5));
    const second = await app.request("/slots/1/interrupt-turn", init());
    assert.equal(second.status, 409);
    assert.equal(((await second.json()) as Record<string, unknown>).reason, "interrupt_in_progress");
    state.gateSend?.();
    state.gateSend = null;
    const firstRes = await first;
    assert.equal(firstRes.status, 200);
    assert.equal(state.sends.length, 1);
    assert.equal(signalEvents(db).length, 1);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("pure authorizer pins epoch+turn and 5-minute server quiet", () => {
  const slot = {
    occupied: true, idle: false, dnd: false, assignment_epoch: 7, active_turn_id: "t1",
    last_meaningful_work_at: new Date(Date.parse("2026-09-19T10:00:00.000Z") - 6 * 60 * 1000).toISOString(),
    active_turn_started_at: new Date(Date.parse("2026-09-19T10:00:00.000Z") - 10 * 60 * 1000).toISOString(),
  };
  const pins = { expected_assignment_epoch: 7, expected_active_turn_id: "t1" };
  const ok = authorizeWedgeInterrupt(
    slot,
    pins,
    WEDGE_SNAPSHOT,
    Date.parse("2026-09-19T10:00:00.000Z"),
    { head: null, branch: null },
  );
  assert.equal(ok.ok, true);
  assert.ok((ok.quiet_ms ?? 0) >= 5 * 60 * 1000);
  // Caller-supplied durations are not even read: extra fields change nothing.
  const withFabrication = authorizeWedgeInterrupt(
    slot,
    { ...pins, no_tool_progress_minutes: 999, idle_prompt_observed: true } as unknown as typeof pins,
    WEDGE_SNAPSHOT,
    Date.parse("2026-09-19T10:00:00.000Z"),
    { head: null, branch: null },
  );
  assert.equal(withFabrication.ok, true);
  assert.equal(withFabrication.quiet_ms, ok.quiet_ms);
  // Stale pins, recent work, and missing timestamps refuse.
  assert.equal(
    authorizeWedgeInterrupt(slot, { ...pins, expected_assignment_epoch: 8 }, WEDGE_SNAPSHOT, Date.parse("2026-09-19T10:00:00.000Z"), {
      head: null,
      branch: null,
    }).reason,
    "stale_identity",
  );
  const recent = { ...slot, last_meaningful_work_at: new Date(Date.parse("2026-09-19T10:00:00.000Z") - 60 * 1000).toISOString() };
  assert.equal(
    authorizeWedgeInterrupt(recent, pins, WEDGE_SNAPSHOT, Date.parse("2026-09-19T10:00:00.000Z"), { head: null, branch: null }).reason,
    "wedge_quiet_insufficient",
  );
});

void Database;

test("fabricated caller durations are not read; server clock governs", async () => {
  const { app, db, directory, NOW } = fixture();
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const res = await app.request(
      "/slots/1/interrupt-turn",
      interruptRequest(RETIRED_AUTHORITY_VALUE, {
        expected_assignment_epoch: pins.epoch,
        expected_active_turn_id: pins.turn,
        no_tool_progress_minutes: 999,
        idle_prompt_observed: true,
      }),
    );
    assert.equal(res.status, 200);
    const events = signalEvents(db);
    assert.equal(events.length, 1);
    const payload = JSON.parse((events[0] as { payload: string }).payload) as Record<string, unknown>;
    assert.ok(typeof payload.quiet_ms === "number" && (payload.quiet_ms as number) < 7 * 60 * 1000);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
