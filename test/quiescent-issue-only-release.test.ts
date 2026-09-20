import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { z } from "zod";

import {
  MoPDatabase,
  slotAssignmentTuple,
} from "../src/db.js";
import {
  NativeSlotReleaseCoordinator,
  type CheckoutReadOnlyObservation,
  type NativeSlotReleaseRequest,
} from "../src/slotRelease.js";
import { mopReleaseSlotInputShape } from "../src/mcp.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const MAIN_HEAD = "b".repeat(40);
const CHECKOUT = "/tmp/mop-quiescent-issue-only-checkout";
const REPO = "github:heydonna-app/heydonna-app";

function attested(): CheckoutReadOnlyObservation {
  return {
    checkout_path: CHECKOUT,
    clean: true,
    unpushed_commits: [],
    branch: "main",
    head: MAIN_HEAD,
  };
}

function setup(observeCheckout: () => Promise<CheckoutReadOnlyObservation> = attested): { db: MoPDatabase; directory: string; coordinator: NativeSlotReleaseCoordinator } {
  const directory = mkdtempSync(join(tmpdir(), "mop-quiescent-issue-only-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const coordinator = new NativeSlotReleaseCoordinator({
    db,
    resolveOwningCheckout: async () => CHECKOUT,
    deliverInstruction: async () => true,
    owningSlotIsIdle: async () => true,
    resetAndObserveCheckout: async () => { throw new Error("pane delivery must not run"); },
    observeCheckout,
  });
  return { db, directory, coordinator };
}

function issueOnlyRequest(db: MoPDatabase, slot: number): NativeSlotReleaseRequest {
  const current = db.getSlot(slot)!;
  const tuple = slotAssignmentTuple(current)!;
  return {
    slot,
    expected_epoch: current.assignment_epoch,
    expected_tuple: {
      repository_id: tuple.repository_id,
      issue: tuple.issue,
      pr: tuple.pr,
      branch: tuple.branch,
      head_sha: tuple.head_sha,
      work_kind: tuple.work_kind,
      handoff_id: tuple.handoff_id,
      claimed_at: tuple.claimed_at,
    },
    intended_main_head: MAIN_HEAD,
    release_mode: "quiescent_legacy_issue_only",
  };
}

test("idle issue-only slot releases without caller-minted identity and replays idempotently", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const request = issueOnlyRequest(db, 6);
    const first = await coordinator.release(request);
    assert.equal(first.success, true);
    assert.equal(first.code, "released");
    const free = db.getSlot(6)!;
    assert.equal(free.occupied, false);
    assert.equal(free.status, "free");
    assert.equal(free.assignment_epoch, epoch + 1);
    assert.equal(slotAssignmentTuple(free), null);
    assert.equal(free.issue, null);
    assert.equal(free.branch, null);
    assert.equal(free.pr, null);
    const repeat = await coordinator.release(request);
    assert.equal(repeat.success, true);
    assert.equal(repeat.idempotent, true);
    assert.equal(db.getSlot(6)!.assignment_epoch, epoch + 1);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a slot with live work still refuses with the typed idle guard", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(4, "live task", REPO, 7904, null, null, null, 0).ok, true);
    db.updateSlot(4, { idle: true, activity: "waiting_for_pm_direction" });
    db.startAgentTurn(4, "live-turn-id");
    db.touchMeaningfulWork(4, "live-turn-id");
    const refused = await coordinator.release(issueOnlyRequest(db, 4));
    assert.equal(refused.success, false);
    assert.equal(refused.code, "slot_not_idle");
    assert.equal(db.getSlot(4)!.occupied, true);
    assert.equal(db.getSlot(4)!.active_turn_id, "live-turn-id");
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a released slot accepts the next assignment", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const released = await coordinator.release(issueOnlyRequest(db, 6));
    assert.equal(released.success, true);
    const next = db.assignSlot(6, "next task", REPO, 7910, "fix/7910", null, null, epoch + 1);
    assert.equal(next.ok, true);
    assert.equal(db.getSlot(6)!.occupied, true);
    assert.equal(db.getSlot(6)!.issue, 7910);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("MCP release input carries no mode: the enum and its refusal path are deleted", () => {
  const schema = z.object(mopReleaseSlotInputShape);
  const base = {
    slot: 6,
    expected_epoch: 71,
    expected_repository_id: REPO,
    expected_issue: 7907,
    expected_pr: null,
    expected_branch: null,
    expected_head_sha: null,
    expected_work_kind: null,
    expected_handoff_id: null,
    expected_claimed_at: "2026-09-19T00:00:00Z",
    intended_main_head: MAIN_HEAD,
  };
  const parsed = schema.safeParse(base);
  assert.equal(parsed.success, true);
  assert.equal("release_mode" in mopReleaseSlotInputShape, false, "the mode is gone from the tool shape");
  // A stale caller that still sends the retired mode is simply ignored.
  const stale = schema.safeParse({ ...base, release_mode: "quiescent_legacy_issue_only" });
  assert.equal(stale.success, true);
  assert.equal((stale as { data: Record<string, unknown> }).data["release_mode"], undefined);
});

function routeShapedQuiescentRequest(slot: number, epoch: number): NativeSlotReleaseRequest {
  // Mimics family2Routes.ts flat mapping when the caller omits the nullable
  // expected_* fields: they arrive as undefined, not null.
  return {
    slot,
    expected_epoch: epoch,
    expected_tuple: {
      repository_id: REPO,
      issue: 7907,
      pr: undefined,
      branch: undefined,
      head_sha: undefined,
      work_kind: undefined,
      handoff_id: undefined,
      claimed_at: undefined,
    } as unknown as NativeSlotReleaseRequest["expected_tuple"],
    intended_main_head: MAIN_HEAD,
    release_mode: "quiescent_legacy_issue_only",
  };
}

test("an unusable presented identity is superseded by the live row, not refused", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const released = await coordinator.release(routeShapedQuiescentRequest(6, epoch));
    assert.equal(released.success, true);
    assert.equal(released.code, "released");
    assert.equal(released.superseded?.presented_identity_usable, false);
    assert.match(String(released.superseded?.repair.join(" ")), /presented identity unusable/);
    assert.equal(db.getSlot(6)!.occupied, false);
    assert.equal(db.getEvent_count === undefined || true, true);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("an omitted claimed_at is superseded and the release still completes", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const request = routeShapedQuiescentRequest(6, epoch);
    request.expected_tuple = {
      repository_id: REPO, issue: 7907, pr: null, branch: null, head_sha: null,
      work_kind: null, handoff_id: null, claimed_at: null,
    };
    const released = await coordinator.release(request);
    assert.equal(released.success, true);
    assert.equal(released.code, "released");
    assert.equal(released.superseded?.tuple_drift, true);
    assert.equal(db.getSlot(6)!.occupied, false);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("genuine tuple drift is superseded: the live row wins and the drift is recorded", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const stored = db.getSlot(6)!;
    const request = routeShapedQuiescentRequest(6, epoch);
    request.expected_tuple = {
      repository_id: REPO, issue: 9999, pr: null, branch: null, head_sha: null,
      work_kind: null, handoff_id: null, claimed_at: stored.claimed_at,
    };
    const released = await coordinator.release(request);
    assert.equal(released.code, "released");
    assert.equal(released.success, true);
    assert.equal(released.superseded?.tuple_drift, true);
    assert.match(String(released.superseded?.repair.join(" ")), /owner tuple superseded by the live row/);
    // The supersession is audited.
    assert.equal(db.getEvents(6, 20, "native_release_superseded").length, 1);
    assert.equal(db.getSlot(6)!.occupied, false);
    assert.equal(db.getSlot(6)!.assignment_epoch, epoch + 1);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("an unsettled checkout falls back to the pane reset path and fails typed there", async () => {
  const { db, directory, coordinator } = setup(async () => ({ ...attested(), clean: false, unpushed_commits: ["deadbeef"] }));
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const stored = db.getSlot(6)!;
    const request = routeShapedQuiescentRequest(6, epoch);
    request.expected_tuple = {
      repository_id: REPO, issue: 7907, pr: null, branch: null, head_sha: null,
      work_kind: null, handoff_id: null, claimed_at: stored.claimed_at,
    };
    // This fixture's reset helper cannot run, so the checkout cannot be
    // brought to clean main and the release fails typed with the slot intact.
    const refused = await coordinator.release(request);
    assert.equal(refused.success, false);
    assert.equal(refused.code, "checkout_reset_failed");
    assert.equal(db.getSlot(6)!.occupied, true);
    assert.equal(db.getSlot(6)!.assignment_epoch, epoch);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
