/**
 * Regression: assign over a live turn must interrupt BEFORE clear, as one
 * pinned operation.
 * Rajiv 2026-10-07 (C0ALZJHGE49 thread 1791348298.888079): "fix MoP to trigger
 * interrupt automatically before clear."
 * CTO REVISE on d85ede4 (ts 1791351726.554489): stale operation authority,
 * unknown-is-not-idle, no internal retry of an uncertain raw interrupt.
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
  pinDrift,
  pinFrom,
  type InterruptBeforeClearDeps,
  type OperationPin,
  type PaneActivity,
  type PaneObservation,
  type SlotRowLike,
} from "../src/interruptBeforeClear.js";

const REPO = "github:heydonna-app/heydonna-app";
const HEAD = "b".repeat(40);
const PANE_A: PaneObservation = { paneId: "%50", checkout: "/slots/5" };
const PANE_B: PaneObservation = { paneId: "%51", checkout: "/slots/5" };

// ─── Helper-level fake boundary ──────────────────────────────────────

function row(over: Partial<SlotRowLike> = {}): SlotRowLike {
  return {
    occupied: true, repository_id: REPO, issue: 8900, pr: null, handoff_id: "h-8900", branch: "fix/8900",
    assignment_epoch: 1, session_id: "s1", active_turn_id: "turnA", active_turn_state: "active",
    ...over,
  };
}

interface World {
  pane: PaneObservation | null;
  row: SlotRowLike | null;
  activity: PaneActivity[];
  keys: string[];
  possible: string[];
  cas: number;
  events: string[];
  /** Hooks run after the Nth paneActivity / sendKeyOnce call (1-based). */
  onActivity?: (n: number, w: World) => void;
  onSend?: (key: string, w: World) => "sent" | "uncertain";
  onCleanup?: (w: World) => void;
}

function world(over: Partial<World> = {}): World {
  return { pane: PANE_A, row: row(), activity: ["active"], keys: [], possible: [], cas: 0, events: [], ...over };
}

function deps(w: World): InterruptBeforeClearDeps {
  let t = 0;
  let n = 0;
  return {
    observePane: async () => w.pane,
    readRow: () => (w.row ? { ...w.row } : null),
    paneActivity: async (paneId) => {
      n += 1;
      const a = paneId === w.pane?.paneId ? w.activity[Math.min(n - 1, w.activity.length - 1)] : "unknown";
      w.onActivity?.(n, w);
      return a;
    },
    recordPossibleEffect: (key) => { w.possible.push(key); },
    sendKeyOnce: async (_paneId, key) => {
      w.keys.push(key);
      return w.onSend ? w.onSend(key, w) : "sent";
    },
    casTerminalize: (pin) => {
      if (pinDrift(pin, w.row)) return false;
      w.cas += 1;
      w.row = { ...w.row!, active_turn_id: null, active_turn_state: "inactive" };
      return true;
    },
    afterInterrupt: async () => { w.onCleanup?.(w); return "empty"; },
    log: (e) => { w.events.push(e); },
    sleep: async (ms) => { t += ms; },
    now: () => t,
    boundMs: 2000,
    pollMs: 250,
  };
}

test("idle: readable idle pane AND present inactive row -> no_live_turn, zero keys", async () => {
  const w = world({ activity: ["idle"], row: row({ active_turn_id: null, active_turn_state: "inactive" }) });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.reason, "no_live_turn");
  assert.ok(r.pin);
  assert.deepEqual(w.keys, []);
});

test("unknown is not idle: unknown pane with inactive row refuses with no effects", async () => {
  const w = world({ activity: ["unknown"], row: row({ active_turn_id: null, active_turn_state: "inactive" }) });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.ok, false);
  assert.equal(r.reason, "evidence_unknown");
  assert.deepEqual(w.keys, []);
  assert.equal(r.pin, undefined);
});

test("missing evidence refuses with no effects: missing turn row, unobservable pane", async () => {
  const noRow = world({ row: null, activity: ["idle"] });
  const r1 = await interruptLiveTurnBeforeClear(deps(noRow));
  assert.equal(r1.reason, "evidence_missing");
  const noPane = world({ pane: null, activity: ["idle"] });
  const r2 = await interruptLiveTurnBeforeClear(deps(noPane));
  assert.equal(r2.reason, "evidence_missing");
  assert.deepEqual([...noRow.keys, ...noPane.keys], []);
});

test("Escape stops the bound turn; CAS terminalizes the ORIGINAL turn; verified", async () => {
  const w = world({ activity: ["active", "active", "idle"] });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.reason, "interrupted_verified");
  assert.deepEqual(w.keys, ["Escape"]);
  assert.deepEqual(w.possible, ["Escape"], "possible effect recorded before the send");
  assert.equal(w.cas, 1);
  assert.equal(r.pin!.turnId, null);
});

test("same-turn escalation: Escape ignored -> C-c against the unchanged bound turn", async () => {
  const w = world({ activity: [...Array(6).fill("active"), "idle"] });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.ok, true);
  assert.deepEqual(w.keys, ["Escape", "C-c"]);
});

test("post-effect ack loss: uncertain Escape is never replayed and nothing escalates", async () => {
  const w = world({ onSend: () => "uncertain" });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.reason, "interrupt_uncertain");
  assert.deepEqual(w.keys, ["Escape"], "exactly one attempt");
  assert.deepEqual(w.possible, ["Escape"]);
  assert.equal(w.cas, 0);
  assert.equal(w.row!.active_turn_id, "turnA", "row untouched");
});

test("turn drift during poll (new turn B): refuse, no C-c, no CAS, B untouched", async () => {
  const w = world({
    activity: ["active"],
    onActivity: (n, ww) => { if (n === 3) ww.row = { ...ww.row!, active_turn_id: "turnB" }; },
  });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.reason, "operation_drift");
  assert.match(r.detail!, /drift:turn@poll_Escape/);
  assert.deepEqual(w.keys, ["Escape"]);
  assert.equal(w.cas, 0);
  assert.equal(w.row!.active_turn_id, "turnB");
});

test("pane rebind %50 -> %51 before C-c: refuse, C-c never sent", async () => {
  const w = world({
    activity: Array(6).fill("active"),
    onActivity: (n, ww) => { if (n === 5) ww.pane = PANE_B; },
  });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.reason, "operation_drift");
  assert.deepEqual(w.keys, ["Escape"]);
  assert.equal(w.cas, 0);
});

test("epoch/owner drift during poll refuses without CAS", async () => {
  const w = world({
    activity: ["active"],
    onActivity: (n, ww) => { if (n === 2) ww.row = { ...ww.row!, assignment_epoch: 2, issue: 9999 }; },
  });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.reason, "operation_drift");
  assert.equal(w.cas, 0);
  assert.equal(w.row!.active_turn_id, "turnA");
});

test("drift at composer cleanup (rebind to turn B) refuses before CAS", async () => {
  const w = world({
    activity: ["active", "active", "idle"],
    onCleanup: (ww) => { ww.row = { ...ww.row!, active_turn_id: "turnB", assignment_epoch: 2 }; },
  });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.reason, "operation_drift");
  assert.match(r.detail!, /post_cleanup/);
  assert.equal(w.cas, 0);
  assert.equal(w.row!.active_turn_id, "turnB");
});

test("never idle within the bound -> interrupt_unverified, row untouched", async () => {
  const w = world({ activity: ["active"] });
  const r = await interruptLiveTurnBeforeClear(deps(w));
  assert.equal(r.reason, "interrupt_unverified");
  assert.deepEqual(w.keys, ["Escape", "C-c"]);
  assert.equal(w.cas, 0);
});

// ─── Route-level fake boundary ───────────────────────────────────────

interface RouteWorld {
  pane: PaneObservation | null;
  session: string;
  order: string[];
  interrupt: (db: MoPDatabase) => { ok: boolean; reason: string; pin?: OperationPin };
  onClear?: (db: MoPDatabase, w: RouteWorld) => void;
  onObserveWorktree?: (db: MoPDatabase, w: RouteWorld) => void;
  /** Mutate on the Nth observePane call (1-based). */
  onObserve?: (n: number, w: RouteWorld) => void;
}

function routeHarness(over: Partial<RouteWorld> = {}) {
  const directory = mkdtempSync(join(tmpdir(), "mop-ibc-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const w: RouteWorld = {
    pane: PANE_A,
    session: "s1",
    order: [],
    interrupt: (d) => {
      const live = d.getSlot(5)!;
      d.updateSlot(5, { active_turn_id: null, active_turn_state: "inactive", idle: true });
      const after = d.getSlot(5)!;
      void live;
      return { ok: true, reason: "interrupted_verified", pin: pinFrom(PANE_A, { ...after, session_id: w.session }) };
    },
    ...over,
  };
  let observed = 0;
  const app = new Hono();
  registerAssignmentEffectRoutes(app, {
    db,
    interruptTurn: async (slot) => {
      w.order.push(`interrupt:${db.getSlot(slot)!.active_turn_state}`);
      return w.interrupt(db);
    },
    observePane: async () => {
      observed += 1;
      w.onObserve?.(observed, w);
      return w.pane;
    },
    readSession: () => w.session,
    clearSlot: async () => {
      w.order.push("clear");
      w.session = "s2"; // a real clear starts a new hook session
      w.onClear?.(db, w);
      return { ok: true, reason: "cleared" };
    },
    deliverTaskFile: async (slot) => {
      w.order.push("deliver");
      return { verified: true, receipt: { slot, verified: true } };
    },
    observeWorktree: async () => {
      w.onObserveWorktree?.(db, w);
      return { clean: true, detail: "clean" };
    },
  });
  return { db, app, w, close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); } };
}

function seedLiveTurn(db: MoPDatabase, slot: number): number {
  const epoch0 = db.getSlot(slot)!.assignment_epoch;
  const seeded = db.assignSlot(
    slot, "in-flight lane", REPO, 8900, "fix/8900", null, HEAD, epoch0,
    "implementation", "handoff-8900", true,
  );
  assert.equal(seeded.ok, true);
  db.updateSlot(slot, { active_turn_id: "turnA", active_turn_state: "active" });
  return db.getSlot(slot)!.assignment_epoch;
}

/** Another actor rebinds slot 5 to a new lane with a live turn B. */
function rebindToTurnB(db: MoPDatabase): void {
  const r = db.assignSlotSimple(5, {
    repository_id: REPO, issue: 7777, pr: null, branch: "fix/7777", head_sha: HEAD,
    work_kind: "implementation", handoff_id: "handoff-7777", claimed_at: "2026-10-07T05:06:00.000Z",
  }, "other lane");
  assert.equal(r.ok, true);
  db.updateSlot(5, { active_turn_id: "turnB", active_turn_state: "active" });
}

async function assign(app: Hono, epoch: number) {
  const res = await app.request("http://mop/slots/5/assign-effect", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      effect_id: "assign-5-8970", selection_class: "new_issue", expected_epoch: epoch,
      repository_id: REPO, issue: 8970, pr: null, branch: "fix/8970", head_sha: HEAD,
      work_kind: "implementation", handoff_id: "handoff-8970",
      claimed_at: "2026-10-07T05:05:00.000Z", task: "Implement 8970.\n", task_file: "/tmp/8970.md",
    }),
  });
  return { status: res.status, json: (await res.json()) as any };
}

test("route: assign over a live turn orders interrupt < clear < deliver (session change across clear allowed)", async () => {
  const h = routeHarness();
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, epoch);
    assert.equal(r.json.status, "assigned");
    assert.deepEqual(h.w.order, ["interrupt:active", "clear", "deliver"]);
    assert.equal(h.db.getSlot(5)!.assignment_epoch, epoch + 1);
  } finally { h.close(); }
});

test("route: unverified interrupt fails closed with no clear, no delivery, ownership unchanged", async () => {
  const h = routeHarness({ interrupt: () => ({ ok: false, reason: "evidence_unknown;pane_activity_unknown" }) });
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, epoch);
    assert.equal(r.status, 409);
    assert.equal(r.json.reason, "interrupt_unverified");
    assert.deepEqual(h.w.order, ["interrupt:active"]);
    assert.equal(h.db.getSlot(5)!.assignment_epoch, epoch);
    assert.equal(h.db.getSlot(5)!.issue, 8900);
  } finally { h.close(); }
});

test("route: ok interrupt without a pin is refused (no unpinned effects)", async () => {
  const h = routeHarness({ interrupt: () => ({ ok: true, reason: "no_live_turn" }) });
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, epoch);
    assert.equal(r.status, 409);
    assert.deepEqual(h.w.order, ["interrupt:active"]);
  } finally { h.close(); }
});

test("route: pane rebind %50 -> %51 before clear refuses with zero later effects", async () => {
  const h = routeHarness({ onObserve: (n, w) => { if (n === 1) w.pane = PANE_B; } });
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, epoch);
    assert.equal(r.status, 409);
    assert.equal(r.json.reason, "operation_drift");
    assert.match(r.json.drift, /drift:pane@pre_clear/);
    assert.deepEqual(h.w.order, ["interrupt:active"]);
    assert.equal(h.db.getSlot(5)!.assignment_epoch, epoch);
  } finally { h.close(); }
});

test("route: hook-session drift before clear refuses", async () => {
  const h = routeHarness({ onObserve: (n, w) => { if (n === 1) w.session = "s-other"; } });
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, epoch);
    assert.equal(r.status, 409);
    assert.match(r.json.drift, /drift:session@pre_clear/);
    assert.deepEqual(h.w.order, ["interrupt:active"]);
  } finally { h.close(); }
});

test("route (CTO repro): rebind to turn B during clear -> no terminalize/commit/delivery on B; epoch not 1->3", async () => {
  const h = routeHarness({ onClear: (db, w) => { rebindToTurnB(db); w.pane = PANE_B; } });
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, epoch);
    assert.equal(r.status, 409);
    assert.equal(r.json.reason, "operation_drift");
    assert.deepEqual(h.w.order, ["interrupt:active", "clear"], "no delivery");
    const after = h.db.getSlot(5)!;
    assert.equal(after.issue, 7777, "the other actor's ownership is unchanged");
    assert.equal(after.assignment_epoch, epoch + 1, "only the rebind's bump; ours never commits");
    assert.equal(after.active_turn_id, "turnB", "turn B is never terminalized");
  } finally { h.close(); }
});

test("route: drift at commit (turn B appears during worktree observe) refuses before the ownership write", async () => {
  const h = routeHarness({ onObserveWorktree: (db) => rebindToTurnB(db) });
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, epoch);
    assert.equal(r.status, 409);
    assert.match(r.json.drift, /@pre_commit/);
    assert.deepEqual(h.w.order, ["interrupt:active", "clear"]);
    assert.equal(h.db.getSlot(5)!.issue, 7777);
    assert.equal(h.db.getSlot(5)!.active_turn_id, "turnB");
  } finally { h.close(); }
});

test("route: pane drift before delivery refuses delivery to the new pane", async () => {
  // observePane calls: 1 pre_clear, 2 post_clear, 3 pre_commit, 4 pre_delivery.
  const h = routeHarness({ onObserve: (n, w) => { if (n === 4) w.pane = PANE_B; } });
  try {
    const epoch = seedLiveTurn(h.db, 5);
    const r = await assign(h.app, epoch);
    assert.equal(r.status, 409);
    assert.match(r.json.drift, /drift:pane@pre_delivery/);
    assert.deepEqual(h.w.order, ["interrupt:active", "clear"], "nothing delivered");
  } finally { h.close(); }
});
