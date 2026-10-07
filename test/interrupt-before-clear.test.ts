/**
 * Regression: assign over a live turn must interrupt BEFORE clear.
 * Rajiv 2026-10-07 (C0ALZJHGE49 thread 1791348298.888079): "the interrupt was
 * sent after the clear. it should be the other way round. fix MoP to trigger
 * interrupt automatically before clear."
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { registerAssignmentEffectRoutes } from "../src/assignmentEffectRoutes.js";
import {
  interruptLiveTurnBeforeClear,
  type PaneActivity,
  type TurnRow,
} from "../src/interruptBeforeClear.js";

const REPO = "github:heydonna-app/heydonna-app";
const HEAD = "b".repeat(40);

function routeHarness(interruptResult: { ok: boolean; reason: string }) {
  const directory = mkdtempSync(join(tmpdir(), "mop-ibc-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const order: string[] = [];
  const app = new Hono();
  registerAssignmentEffectRoutes(app, {
    db,
    interruptTurn: async (slot) => {
      // The live turn must still be live when the interrupt runs: nothing
      // (clear or packet) has been sent to the pane yet.
      order.push(`interrupt:${db.getSlot(slot)!.active_turn_state}`);
      return interruptResult;
    },
    clearSlot: async () => {
      order.push("clear");
      return { ok: true, reason: "cleared" };
    },
    deliverTaskFile: async (slot) => {
      order.push("deliver");
      return { verified: true, receipt: { slot, verified: true } };
    },
    observeWorktree: async () => ({ clean: true, detail: "clean" }),
  });
  return {
    db, app, order,
    close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); },
  };
}

function seedLiveTurn(db: MoPDatabase, slot: number): number {
  const epoch0 = db.getSlot(slot)!.assignment_epoch;
  const seeded = db.assignSlot(
    slot, "in-flight lane", REPO, 8900, "fix/8900", null, HEAD, epoch0,
    "implementation", "handoff-8900", true,
  );
  assert.equal(seeded.ok, true);
  db.updateSlot(slot, { active_turn_id: "turn-live", active_turn_state: "active" });
  return db.getSlot(slot)!.assignment_epoch;
}

async function assign(app: Hono, slot: number, epoch: number) {
  const res = await app.request(`http://mop/slots/${slot}/assign-effect`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      effect_id: `assign-${slot}-8970`, selection_class: "new_issue", expected_epoch: epoch,
      repository_id: REPO, issue: 8970, pr: null, branch: "fix/8970", head_sha: HEAD,
      work_kind: "implementation", handoff_id: "handoff-8970",
      claimed_at: "2026-10-07T05:05:00.000Z", task: "Implement 8970.\n", task_file: "/tmp/8970.md",
    }),
  });
  return { status: res.status, json: (await res.json()) as any };
}

test("new_issue assign over a live turn: interrupt precedes clear precedes delivery", async () => {
  const h = routeHarness({ ok: true, reason: "interrupted_verified" });
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, 5, epoch);
    assert.equal(r.json.status, "assigned");
    assert.deepEqual(h.order, ["interrupt:active", "clear", "deliver"]);
    assert.equal(h.db.getSlot(5)!.assignment_epoch, epoch + 1);
  } finally { h.close(); }
});

test("unverified interrupt fails closed: no clear, no packet queued behind the live turn, epoch unchanged", async () => {
  const h = routeHarness({ ok: false, reason: "interrupt_unverified;pane=active" });
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, 5, epoch);
    assert.equal(r.status, 409);
    assert.equal(r.json.reason, "interrupt_unverified");
    assert.deepEqual(h.order, ["interrupt:active"], "neither /clear nor the packet reached the pane");
    const after = h.db.getSlot(5)!;
    assert.equal(after.assignment_epoch, epoch);
    assert.equal(after.issue, 8900);
  } finally { h.close(); }
});

function fakeSlot(opts: { pane: PaneActivity[]; row: TurnRow }) {
  const keys: string[] = [];
  const events: string[] = [];
  let row: TurnRow = { ...opts.row };
  let t = 0;
  let paneIdx = 0;
  const paneSeq = opts.pane;
  return {
    keys, events, get row() { return row; },
    deps: {
      readTurn: () => row,
      paneActivity: async () => paneSeq[Math.min(paneIdx++, paneSeq.length - 1)],
      sendKey: async (k: "Escape" | "C-c") => { keys.push(k); return true; },
      terminalizeTurn: () => { row = { active_turn_id: null, active_turn_state: "inactive" }; },
      log: (e: string) => { events.push(e); },
      sleep: async (ms: number) => { t += ms; },
      now: () => t,
      boundMs: 2000,
      pollMs: 250,
    },
  };
}

test("helper: idle slot sends no keys", async () => {
  const f = fakeSlot({ pane: ["idle"], row: { active_turn_id: null, active_turn_state: "inactive" } });
  const r = await interruptLiveTurnBeforeClear(f.deps);
  assert.equal(r.reason, "no_live_turn");
  assert.deepEqual(f.keys, []);
});

test("helper: Escape stops the turn; turn row terminalized; verified", async () => {
  const f = fakeSlot({ pane: ["active", "active", "idle"], row: { active_turn_id: "t1", active_turn_state: "active" } });
  const r = await interruptLiveTurnBeforeClear(f.deps);
  assert.equal(r.ok, true);
  assert.equal(r.reason, "interrupted_verified");
  assert.deepEqual(f.keys, ["Escape"]);
  assert.equal(f.row.active_turn_state, "inactive");
});

test("helper: Escape ignored -> C-c escalation", async () => {
  const pane: PaneActivity[] = [...Array(8).fill("active"), "idle"];
  const f = fakeSlot({ pane, row: { active_turn_id: "t1", active_turn_state: "active" } });
  const r = await interruptLiveTurnBeforeClear(f.deps);
  assert.equal(r.ok, true);
  assert.deepEqual(f.keys, ["Escape", "C-c"]);
});

test("helper: never idles within bound -> interrupt_unverified, row untouched", async () => {
  const f = fakeSlot({ pane: ["active"], row: { active_turn_id: "t1", active_turn_state: "active" } });
  const r = await interruptLiveTurnBeforeClear(f.deps);
  assert.equal(r.ok, false);
  assert.equal(r.reason, "interrupt_unverified");
  assert.deepEqual(f.keys, ["Escape", "C-c"]);
  assert.equal(f.row.active_turn_state, "active");
  assert.ok(f.events.includes("interrupt_before_clear_unverified"));
});

test("helper: indeterminate row with idle pane is interrupted-and-terminalized, not treated as idle", async () => {
  const f = fakeSlot({ pane: ["idle"], row: { active_turn_id: null, active_turn_state: "indeterminate" } });
  const r = await interruptLiveTurnBeforeClear(f.deps);
  assert.equal(r.ok, true);
  assert.equal(r.reason, "interrupted_verified");
  assert.equal(f.row.active_turn_state, "inactive");
});
