import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { registerFamily2Routes } from "../src/family2Routes.js";
import { MoPDatabase, slotAssignmentTuple, type AssignmentTupleInput } from "../src/db.js";
import { NativeSlotReleaseCoordinator } from "../src/slotRelease.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const REPO = "github:heydonna-app/heydonna-app";
const MAIN_HEAD = "b".repeat(40);
const CHECKOUT = "/tmp/mop-release-contract-checkout";
const RETIRED_HEADER = "x-heydonna-assignment-authority";

interface Fixture {
  db: MoPDatabase;
  directory: string;
  coordinator: NativeSlotReleaseCoordinator;
  app: Hono;
  interrupts: number[];
  close: () => void;
}

/**
 * One occupied slot with a branch-bearing owner tuple, the S1/#7982 shape.
 * The default checkout observation is clean main at MAIN_HEAD.
 */
function fixture(options: { settled?: boolean } = {}): Fixture {
  const directory = mkdtempSync(join(tmpdir(), "mop-release-contract-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const interrupts: number[] = [];
  assert.equal(
    db.assignSlot(1, "issue 7982 rework", REPO, 7982, "fix-7792-hydration-base-install", 7982, "a".repeat(40), 0, "rework", "rebase-7982-chainflip-20260920").ok,
    true,
  );
  db.updateSlot(1, { idle: true, activity: "waiting_for_pm_direction" });
  const coordinator = new NativeSlotReleaseCoordinator({
    db,
    resolveOwningCheckout: async () => CHECKOUT,
    interruptTurn: async (slot) => {
      interrupts.push(slot);
      return { ok: true, reason: "interrupt_sent" };
    },
    observeCheckout: async () => (options.settled === false
      ? { checkout_path: CHECKOUT, clean: false, unpushed_commits: ["deadbeef"], branch: "fix/7982" }
      : { checkout_path: CHECKOUT, clean: true, unpushed_commits: [], branch: "main", head: MAIN_HEAD }),
  });
  const app = new Hono();
  registerFamily2Routes(app, {
    db,
    nativeSlotRelease: coordinator,
    family2ReleaseEffectAdapter: {} as any,
    clearPlanApprovalTimer: () => undefined,
  });
  return { db, directory, coordinator, app, interrupts, close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); } };
}

function tupleOf(db: MoPDatabase, slot = 1): AssignmentTupleInput {
  const tuple = slotAssignmentTuple(db.getSlot(slot)!)!;
  return {
    repository_id: tuple.repository_id, issue: tuple.issue, pr: tuple.pr, branch: tuple.branch,
    head_sha: tuple.head_sha, work_kind: tuple.work_kind, handoff_id: tuple.handoff_id, claimed_at: tuple.claimed_at,
  };
}

function releaseOf(db: MoPDatabase, slot = 1) {
  return {
    slot,
    expected_epoch: db.getSlot(slot)!.assignment_epoch,
    expected_tuple: tupleOf(db, slot),
    intended_main_head: MAIN_HEAD,
  };
}

test("the retired authority contract is gone from every operator route", () => {
  assert.equal(existsSync(new URL("../src/assignmentAuthority.ts", import.meta.url)), false);
  for (const file of ["family2Routes.ts", "assignmentRoute.ts", "wedgeInterruptRoute.ts", "server.ts", "mcp.ts"]) {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    assert.equal(source.includes("assignment_authority_required"), false, `${file} still refuses on authority`);
    assert.equal(source.includes(RETIRED_HEADER), false, `${file} still reads the retired header`);
  }
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.equal(server.includes("/release-intent"), false, "the read/void intent ceremony is deleted");
});

test("release over HTTP needs no header and succeeds on a settled slot", async () => {
  const f = fixture();
  try {
    const epochBefore = f.db.getSlot(1)!.assignment_epoch;
    const response = await f.app.request("http://mop/slots/1/release", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...releaseOf(f.db), expected_repository_id: REPO, expected_issue: 7982 }),
    });
    assert.equal(response.status, 200);
    const body = await response.json() as Record<string, unknown>;
    assert.equal(body.success, true);
    assert.equal(body.code, "released");
    assert.equal(f.db.getSlot(1)!.occupied, false);
    assert.equal(f.db.getSlot(1)!.assignment_epoch, epochBefore + 1);
    assert.equal(slotAssignmentTuple(f.db.getSlot(1)!), null);
  } finally {
    f.close();
  }
});

test("a stale live release intent no longer bricks the release", async () => {
  const f = fixture();
  try {
    // The incident: an earlier attempt left a LIVE lease behind, so every
    // later claim failed and the slot could not be released. The operator
    // release no longer goes through the intent claim at all.
    const plantedEpoch = f.db.getSlot(1)!.assignment_epoch;
    const staleIntentId = f.db.claimNativeReleaseIntentWithToken(1, plantedEpoch, tupleOf(f.db), 600_000);
    assert.ok(staleIntentId && staleIntentId.length > 0);
    const result = await f.coordinator.release(releaseOf(f.db));
    assert.equal(result.success, true, "a stale intent must never brick the release");
    assert.equal(result.code, "released");
    assert.equal(f.db.getSlot(1)!.occupied, false, "the release committed");
    assert.equal(f.db.getSlot(1)!.assignment_epoch, plantedEpoch + 1);
  } finally {
    f.close();
  }
});

test("tuple and epoch drift are superseded, audited, and the live row wins", async () => {
  const f = fixture();
  try {
    const presented = releaseOf(f.db);
    const result = await f.coordinator.release({
      ...presented,
      expected_epoch: presented.expected_epoch + 7,
      expected_tuple: { ...presented.expected_tuple, issue: 999999 },
    });
    assert.equal(result.code, "released");
    assert.equal(result.superseded?.epoch_drift, true);
    assert.equal(result.superseded?.tuple_drift, true);
    assert.equal(f.db.getSlot(1)!.occupied, false);
    assert.equal(f.db.getEvents(1, 20, "slot_released_simple").length, 1, "the release is audited");

    // The released owner is the LIVE one, not the presented one.
    const freed = f.db.getSlot(1)!;
    assert.equal(freed.assignment_epoch, presented.expected_epoch + 1);
    assert.equal(slotAssignmentTuple(freed), null);
  } finally {
    f.close();
  }
});

test("a retry after a completed release is an idempotent success with no second effect", async () => {
  const f = fixture();
  try {
    const request = releaseOf(f.db);
    const first = await f.coordinator.release(request);
    assert.equal(first.code, "released");
    assert.equal(first.idempotent, false);
    const epochAfterFirst = f.db.getSlot(1)!.assignment_epoch;

    const retry = await f.coordinator.release(request);
    assert.equal(retry.success, true);
    assert.equal(retry.code, "released");
    assert.equal(retry.idempotent, true);
    assert.equal(f.db.getSlot(1)!.assignment_epoch, epochAfterFirst, "no second epoch bump");
  } finally {
    f.close();
  }
});

test("the orphaned-turn shape releases immediately: interrupt, terminalize, audit", async () => {
  const f = fixture();
  try {
    const turnId = "b830d078-31e6-4549-a3d7-3581d767debe";
    f.db.updateSlot(1, {
      active_turn_id: turnId,
      active_turn_state: "active",
      active_turn_started_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
      // live slot-6 shape: idle=true while an old hook turn is still active
      idle: true,
      activity: "branching",
      last_meaningful_work_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
    });
    // No abandon-turn round-trip, no retry: one call frees the slot.
    const result = await f.coordinator.release({ slot: 1 });
    assert.equal(result.success, true);
    assert.equal(result.code, "released");
    assert.deepEqual(f.interrupts, [1], "the live turn is interrupted once");
    const freed = f.db.getSlot(1)!;
    assert.equal(freed.occupied, false);
    assert.equal(freed.active_turn_id, null, "the turn id is terminalized in the same write");
    assert.equal(freed.active_turn_state, "inactive");
    const events = f.db.getEvents(1, 20, "slot_released_simple");
    assert.equal(events.length, 1);
    const payload = JSON.parse(events[0].payload) as Record<string, unknown>;
    assert.equal((payload.prior as Record<string, unknown>).active_turn_id, turnId);
    assert.equal((payload.interrupt as Record<string, unknown>).reason, "interrupt_sent");
    assert.equal(payload.worktree_reset, false);
  } finally {
    f.close();
  }
});

test("a finished slot releases immediately inside the former settling window", async () => {
  const f = fixture();
  try {
    f.db.updateSlot(1, { last_meaningful_work_at: new Date().toISOString() });
    const released = await f.coordinator.release(releaseOf(f.db));
    assert.equal(released.code, "released");
    assert.equal(released.success, true);
    assert.equal(f.db.getSlot(1)!.occupied, false);
  } finally {
    f.close();
  }
});

test("DND and a busy row no longer block an explicit release", async () => {
  for (const [name, updates, cause] of [
    ["dnd", { dnd: true }, "dnd"],
    ["busy row", { idle: false }, "productive_work"],
  ] as const) {
    const f = fixture();
    try {
      f.db.updateSlot(1, updates as Record<string, unknown>);
      const epochBefore = f.db.getSlot(1)!.assignment_epoch;
      const result = await f.coordinator.release({ slot: 1 });
      assert.equal(result.code, "released", name);
      assert.equal(result.success, true, name);
      assert.equal(f.db.getSlot(1)!.occupied, false, name);
      assert.equal(f.db.getSlot(1)!.assignment_epoch, epochBefore + 1, name);
    } finally {
      f.close();
    }
  }
});

test("an unsettled checkout is observed for the audit, never reset or refused", async () => {
  const f = fixture({ settled: false });
  try {
    const result = await f.coordinator.release({ slot: 1 });
    assert.equal(result.code, "released");
    assert.equal(result.success, true);
    assert.equal(f.db.getSlot(1)!.occupied, false);
    const events = f.db.getEvents(1, 20, "slot_released_simple");
    assert.equal(events.length, 1);
    const payload = JSON.parse(events[0].payload) as Record<string, unknown>;
    assert.equal((payload.worktree as Record<string, unknown>).clean, false, "dirty state observed, not reset");
    assert.equal(payload.worktree_reset, false);
  } finally {
    f.close();
  }
});
