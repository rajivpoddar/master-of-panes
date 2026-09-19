import assert from "node:assert/strict";
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
} from "../src/wedgeInterruptRoute.js";
import {
  isPmTransitionAssignmentRequest,
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "../src/assignmentAuthority.js";
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
}

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
  const state: FakeState = { sends: [], identityOk: true, snapshot: WEDGE_SNAPSHOT, gateSend: null, sendStarted: false };
  const app = new Hono();
  const NOW = Date.parse("2026-09-19T10:00:00.000Z");
  registerWedgeInterruptRoute(app, {
    db,
    isOperatorRequest: (h) => isPmTransitionAssignmentRequest(h),
    authorityHeader: (c) => c.req.header(PM_TRANSITION_ASSIGNMENT_HEADER),
    verifyPaneIdentity: async (slotNum: number) =>
      state.identityOk
        ? { ok: true as const, snapshot: { paneId: `%${slotNum}` } }
        : { ok: false as const, detail: "pane gone" },
    captureSnapshot: async () => state.snapshot,
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

function interruptRequest(authority: string | undefined, body: unknown) {
  return {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(authority === undefined ? {} : { [PM_TRANSITION_ASSIGNMENT_HEADER]: authority }),
    },
    body: JSON.stringify(body),
  } as RequestInit;
}

function signalEvents(db: MoPDatabase) {
  return db.getEvents(1, 50, "interrupt_signal_sent");
}

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

test("authority, identity, DND, quiet, evidence refusals are typed with zero effects", async () => {
  const { app, db, directory, state, NOW } = fixture();
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const goodBody = { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn };
    const cases: Array<[string, RequestInit, number, string]> = [
      ["no authority", interruptRequest(undefined, goodBody), 403, "assignment_authority_required"],
      ["wrong authority", interruptRequest("nope", goodBody), 403, "assignment_authority_required"],
      ["no pins", interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {}), 409, "stale_identity"],
      ["wrong epoch", interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, { ...goodBody, expected_assignment_epoch: 999 }), 409, "stale_identity"],
      ["wrong turn", interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, { ...goodBody, expected_active_turn_id: "turn-other" }), 409, "stale_identity"],
    ];
    for (const [name, init, status, reason] of cases) {
      const res = await app.request("/slots/1/interrupt-turn", init);
      assert.equal(res.status, status, name);
      assert.equal(((await res.json()) as Record<string, unknown>).reason, reason, name);
    }
    // Fabricated caller attestations alone never satisfy: snapshot is the evidence.
    state.snapshot = "› Working…\n$ ";
    const noEvidence = await app.request("/slots/1/interrupt-turn", interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, goodBody));
    assert.equal(noEvidence.status, 409);
    assert.equal(((await noEvidence.json()) as Record<string, unknown>).reason, "pane_evidence_unavailable");
    state.snapshot = WEDGE_SNAPSHOT;

    // DND refuses even with perfect pins + evidence.
    db.updateSlot(1, { dnd: true });
    const dnd = await app.request("/slots/1/interrupt-turn", interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, goodBody));
    assert.equal(dnd.status, 409);
    assert.equal(((await dnd.json()) as Record<string, unknown>).reason, "slot_dnd_interrupt_refused");
    db.updateSlot(1, { dnd: false });

    // Fresh activity (recent meaningful work) refuses: quiet insufficient.
    db.updateSlot(1, { last_meaningful_work_at: new Date(NOW - 60 * 1000).toISOString() });
    const fresh = await app.request("/slots/1/interrupt-turn", interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, goodBody));
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
    const res = await app.request("/slots/1/interrupt-turn", interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, goodBody));
    assert.equal(res.status, 409);
    assert.equal(((await res.json()) as Record<string, unknown>).reason, "pane_identity_mismatch");
    state.identityOk = true;

    db.finishAgentTurn(1, pins.turn);
    const settled = await app.request("/slots/1/interrupt-turn", interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, goodBody));
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
      interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn }),
    );
    assert.equal(res.status, 200);
    const body = (await res.json()) as Record<string, unknown>;
    assert.equal(body.success, true);
    assert.equal(body.signal_sent, true);
    assert.equal(body.interrupt_key, "C-c");
    assert.equal(body.queue_disposition, "discard-queued-input");
    assert.match(String(body.followup), /AFTER this interrupt/);
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
    isOperatorRequest: (h) => isPmTransitionAssignmentRequest(h),
    authorityHeader: (c) => c.req.header(PM_TRANSITION_ASSIGNMENT_HEADER),
    verifyPaneIdentity: async (slotNum: number) => ({ ok: true as const, snapshot: { paneId: `%${slotNum}` } }),
    captureSnapshot: async () => WEDGE_SNAPSHOT,
    sendInterruptKey: async () => { sends += 1; return false; },
    nowMs: () => NOW,
  });
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const res = await app.request(
      "/slots/1/interrupt-turn",
      interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn }),
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
    const init = () => interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, { expected_assignment_epoch: pins.epoch, expected_active_turn_id: pins.turn });
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
  const ok = authorizeWedgeInterrupt(slot, pins, WEDGE_SNAPSHOT, Date.parse("2026-09-19T10:00:00.000Z"));
  assert.equal(ok.ok, true);
  assert.ok((ok.quiet_ms ?? 0) >= 5 * 60 * 1000);
  // Caller-supplied durations are not even read: extra fields change nothing.
  const withFabrication = authorizeWedgeInterrupt(
    slot,
    { ...pins, no_tool_progress_minutes: 999, idle_prompt_observed: true } as unknown as typeof pins,
    WEDGE_SNAPSHOT,
    Date.parse("2026-09-19T10:00:00.000Z"),
  );
  assert.equal(withFabrication.ok, true);
  assert.equal(withFabrication.quiet_ms, ok.quiet_ms);
  // Stale pins, recent work, and missing timestamps refuse.
  assert.equal(authorizeWedgeInterrupt(slot, { ...pins, expected_assignment_epoch: 8 }, WEDGE_SNAPSHOT, Date.parse("2026-09-19T10:00:00.000Z")).reason, "stale_identity");
  const recent = { ...slot, last_meaningful_work_at: new Date(Date.parse("2026-09-19T10:00:00.000Z") - 60 * 1000).toISOString() };
  assert.equal(authorizeWedgeInterrupt(recent, pins, WEDGE_SNAPSHOT, Date.parse("2026-09-19T10:00:00.000Z")).reason, "wedge_quiet_insufficient");
});

void Database;

test("fabricated caller durations are not read; server clock governs", async () => {
  const { app, db, directory, NOW } = fixture();
  try {
    const pins = await busySlot(db, 6 * 60 * 1000, NOW);
    const res = await app.request(
      "/slots/1/interrupt-turn",
      interruptRequest(PM_TRANSITION_ASSIGNMENT_AUTHORITY, {
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
