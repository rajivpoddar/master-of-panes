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

test("MCP release input carries the quiescent fields end to end", () => {
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
  const withoutMode = schema.safeParse(base);
  assert.equal(withoutMode.success, true);
  assert.equal((withoutMode as { data: Record<string, unknown> }).data["release_mode"], undefined);
  const withMode = schema.safeParse({ ...base, release_mode: "quiescent_legacy_issue_only" });
  assert.equal(withMode.success, true);
  // The validated payload keeps the mode so the HTTP body forwards it;
  // the parent shape stripped it, forcing quiescent_release_required.
  assert.equal((withMode as { data: Record<string, unknown> }).data["release_mode"], "quiescent_legacy_issue_only");
  assert.equal(schema.safeParse({ ...base, release_mode: "bogus" }).success, false);
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

test("quiescent legacy refusal names the exact flat fields and server-minted identity", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const refused = await coordinator.release(routeShapedQuiescentRequest(6, epoch));
    assert.equal(refused.success, false);
    assert.equal(refused.code, "invalid_request");
    const text = `${refused.message} ${refused.remediation ?? ""}`;
    for (const field of ["expected_repository_id", "expected_issue", "expected_pr", "expected_branch", "expected_head_sha", "expected_work_kind", "expected_handoff_id", "expected_claimed_at", "intended_main_head"]) {
      assert.match(text, new RegExp(field), `refusal must name ${field}`);
    }
    assert.match(text, /minted server-side/, "refusal must state the effect identity is server-minted");
    assert.equal(db.getSlot(6)!.occupied, true);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("quiescent legacy refusal persists when only claimed_at is omitted", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const request = routeShapedQuiescentRequest(6, epoch);
    request.expected_tuple = { ...request.expected_tuple, pr: null, branch: null, head_sha: null, work_kind: null, handoff_id: null };
    const refused = await coordinator.release(request);
    assert.equal(refused.success, false);
    assert.equal(refused.code, "invalid_request");
    assert.match(`${refused.message} ${refused.remediation ?? ""}`, /expected_claimed_at/);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("quiescent legacy release proceeds with explicit nulls and the stored claimed_at", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    // The working prescription: re-read the exact stored tuple. The five
    // nullable fields arrive as explicit nulls; claimed_at carries the
    // stored stamp (it participates in the later tuple-match check).
    const stored = db.getSlot(6)!;
    const request = routeShapedQuiescentRequest(6, epoch);
    assert.equal(stored.pr, null);
    assert.equal(stored.branch, null);
    assert.equal(stored.head_sha, null);
    assert.equal(stored.work_kind, null);
    assert.equal(stored.handoff_id, null);
    request.expected_tuple = { ...request.expected_tuple, pr: null, branch: null, head_sha: null, work_kind: null, handoff_id: null, claimed_at: stored.claimed_at };
    const released = await coordinator.release(request);
    assert.equal(released.success, true);
    assert.equal(released.code, "released");
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("claimed_at-only drift names expected_claimed_at instead of generic tuple change", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const request = routeShapedQuiescentRequest(6, epoch);
    request.expected_tuple = { ...request.expected_tuple, pr: null, branch: null, head_sha: null, work_kind: null, handoff_id: null, claimed_at: null };
    const refused = await coordinator.release(request);
    assert.equal(refused.success, false);
    assert.equal(refused.code, "observed_tuple_mismatch");
    assert.match(refused.message, /expected_claimed_at/);
    assert.match(refused.message, /live claimed_at value/);
    assert.equal(db.getSlot(6)!.occupied, true);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("genuine tuple drift keeps the generic tuple-change message", async () => {
  const { db, directory, coordinator } = setup();
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const stored = db.getSlot(6)!;
    const request = routeShapedQuiescentRequest(6, epoch);
    request.expected_tuple = { repository_id: REPO, issue: 9999, pr: null, branch: null, head_sha: null, work_kind: null, handoff_id: null, claimed_at: stored.claimed_at };
    const refused = await coordinator.release(request);
    assert.equal(refused.success, false);
    assert.equal(refused.code, "observed_tuple_mismatch");
    assert.match(refused.message, /Complete owner tuple changed/);
    assert.doesNotMatch(refused.message, /expected_claimed_at/);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("attestation failure states the main-clean precondition and slot instruction ordering", async () => {
  const { db, directory, coordinator } = setup(async () => ({ ...attested(), clean: false, unpushed_commits: ["deadbeef"] }));
  try {
    assert.equal(db.assignSlot(6, "issue-only task", REPO, 7907, null, null, null, 0).ok, true);
    db.updateSlot(6, { idle: true, activity: "waiting_for_pm_direction" });
    const epoch = db.getSlot(6)!.assignment_epoch;
    const stored = db.getSlot(6)!;
    const request = routeShapedQuiescentRequest(6, epoch);
    request.expected_tuple = { ...request.expected_tuple, pr: null, branch: null, head_sha: null, work_kind: null, handoff_id: null, claimed_at: stored.claimed_at };
    const refused = await coordinator.release(request);
    assert.equal(refused.success, false);
    assert.equal(refused.code, "quiescent_attestation_failed");
    const text = `${refused.message} ${refused.remediation ?? ""}`;
    assert.match(text, /branch main at intended_main_head/);
    assert.match(text, /switch-to-main-and-pull/);
    assert.match(text, /stays occupied/);
    assert.equal(db.getSlot(6)!.occupied, true);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
