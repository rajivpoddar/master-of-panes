import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { registerFamily2Routes } from "../src/family2Routes.js";
import { MoPDatabase, slotAssignmentTuple, type AssignmentTupleInput } from "../src/db.js";
import { NativeSlotReleaseCoordinator, RELEASE_QUIESCENCE_MS } from "../src/slotRelease.js";
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
  close: () => void;
}

/**
 * One occupied slot with a branch-bearing owner tuple, the S1/#7982 shape.
 * The default checkout observation is clean main at MAIN_HEAD.
 */
function fixture(options: { settled?: boolean } = {}): Fixture {
  const directory = mkdtempSync(join(tmpdir(), "mop-release-contract-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  assert.equal(
    db.assignSlot(1, "issue 7982 rework", REPO, 7982, "fix-7792-hydration-base-install", 7982, "a".repeat(40), 0, "rework", "rebase-7982-chainflip-20260920").ok,
    true,
  );
  db.updateSlot(1, { idle: true, activity: "waiting_for_pm_direction" });
  const coordinator = new NativeSlotReleaseCoordinator({
    db,
    resolveOwningCheckout: async () => CHECKOUT,
    deliverInstruction: async () => true,
    owningSlotIsIdle: async () => true,
    resetAndObserveCheckout: async () => ({
      checkout_path: CHECKOUT, branch: "main", head: MAIN_HEAD, clean: true, reset_succeeded: true, error: null,
    }),
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
  return { db, directory, coordinator, app, close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); } };
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

test("a stale live release intent is superseded atomically and named in the response", async () => {
  const f = fixture();
  try {
    // The incident: an earlier attempt left a LIVE lease behind, so every
    // later claim failed and the slot could not be released.
    const plantedEpoch = f.db.getSlot(1)!.assignment_epoch;
    const staleIntentId = f.db.claimNativeReleaseIntentWithToken(1, plantedEpoch, tupleOf(f.db), 600_000);
    assert.ok(staleIntentId && staleIntentId.length > 0);
    const result = await f.coordinator.release(releaseOf(f.db));
    assert.equal(result.success, true, "a stale intent must never brick the release");
    assert.equal(result.code, "released");
    assert.equal(result.superseded?.superseded_intent_id, staleIntentId);
    assert.match(String(result.superseded?.repair.join(" ")), /release intent .* superseded/);
    const supersededEvents = f.db.getEvents(1, 20, "native_release_intent_superseded");
    assert.equal(supersededEvents.length, 1, "the supersession is audited");
    assert.match(supersededEvents[0].payload, new RegExp(String(staleIntentId)));
    assert.equal(f.db.getSlot(1)!.occupied, false, "the release committed");
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
    assert.equal(f.db.getEvents(1, 20, "native_release_superseded").length, 1);

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

test("the orphaned-turn shape refuses with the abandon-turn remedy, then releases after recovery", async () => {
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
    const blocked = await f.coordinator.release(releaseOf(f.db));
    assert.equal(blocked.success, false);
    assert.equal(blocked.code, "slot_not_idle", "the ONE surviving refusal");
    assert.equal(blocked.cause, "active_turn");
    assert.match(String(blocked.remediation), /abandon-turn/);
    assert.match(String(blocked.remediation), new RegExp(turnId), "the remedy names the exact blocked turn");

    assert.equal(f.db.abandonTurn(1, turnId, "owning session gone", "cto").ok, true);
    const after = await f.coordinator.release(releaseOf(f.db));
    assert.equal(after.code, "released", "the release succeeds once the stale turn is cleared");
    assert.equal(after.superseded?.ignored_activity, "branching", "stale activity on a closed turn is recorded, not refused");
  } finally {
    f.close();
  }
});

test("the quiescence window refuses briefly, then the same release succeeds", async () => {
  const f = fixture();
  try {
    f.db.updateSlot(1, { last_meaningful_work_at: new Date().toISOString() });
    const tooSoon = await f.coordinator.release(releaseOf(f.db));
    assert.equal(tooSoon.code, "slot_not_idle");
    assert.equal(tooSoon.cause, "quiescence");
    assert.match(String(tooSoon.remediation), /Retry this release in about \d+s/);
    assert.equal(f.db.getSlot(1)!.occupied, true, "a refusal mutates nothing");

    f.db.updateSlot(1, { last_meaningful_work_at: new Date(Date.now() - RELEASE_QUIESCENCE_MS - 1000).toISOString() });
    const released = await f.coordinator.release(releaseOf(f.db));
    assert.equal(released.code, "released");
  } finally {
    f.close();
  }
});

test("live work is still protected: DND and a busy row refuse", async () => {
  for (const [name, updates, cause] of [
    ["dnd", { dnd: true }, "dnd"],
    ["busy row", { idle: false }, "productive_work"],
  ] as const) {
    const f = fixture();
    try {
      f.db.updateSlot(1, updates as Record<string, unknown>);
      const before = f.db.getSlot(1)!;
      const result = await f.coordinator.release(releaseOf(f.db));
      assert.equal(result.code, "slot_not_idle", name);
      assert.equal(result.cause, cause, name);
      assert.deepEqual(f.db.getSlot(1), before, `${name} refusal mutates nothing`);
    } finally {
      f.close();
    }
  }
});

test("a checkout that is not settled is reset through the pane instead of refused", async () => {
  const f = fixture({ settled: false });
  try {
    let deliveries = 0;
    const coordinator = new NativeSlotReleaseCoordinator({
      db: f.db,
      resolveOwningCheckout: async () => CHECKOUT,
      deliverInstruction: async () => { deliveries += 1; return true; },
      owningSlotIsIdle: async () => true,
      resetAndObserveCheckout: async () => ({
        checkout_path: CHECKOUT, branch: "main", head: MAIN_HEAD, clean: true, reset_succeeded: true, error: null,
      }),
      observeCheckout: async () => ({ checkout_path: CHECKOUT, clean: false, unpushed_commits: ["deadbeef"], branch: "fix/7982" }),
    });
    const result = await coordinator.release(releaseOf(f.db));
    assert.equal(result.code, "released");
    assert.equal(deliveries, 1, "the pane is instructed before the reset");
  } finally {
    f.close();
  }
});
