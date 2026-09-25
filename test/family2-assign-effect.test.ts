import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import {
  computeAssignmentEffectDigest,
  registerAssignmentEffectRoutes,
  type AssignmentEffectClearResult,
  type AssignmentEffectDeliveryResult,
} from "../src/assignmentEffectRoutes.js";

const REPO = "github:heydonna-app/heydonna-app";
const HEAD = "a".repeat(40);
const TASK = "Implement issue 8110: harden the assignment boundary.\n";

interface Harness {
  db: MoPDatabase;
  app: Hono;
  clearCalls: number[];
  deliverCalls: string[];
  clearResult: AssignmentEffectClearResult;
  deliveryVerified: boolean;
  deliverError: string | null;
  corruptTaskAfterDelivery: boolean;
  interruptCalls: number[];
  interruptResult: { ok: boolean; reason: string };
  worktreeResult: { clean: boolean | null; detail: string };
  projectedAssigned: Array<{ issue: number; slot: number; repository: string | null }>;
  projectedReleased: Array<{ issue: number; slot: number; repository: string | null }>;
  projectionCalls: number;
  projectorThrowOn: "assigned" | "released" | null;
  orderLog: string[];
  close: () => void;
}

function harness(): Harness {
  const directory = mkdtempSync(join(tmpdir(), "mop-assign-effect-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const state: Harness = {
    db,
    app: new Hono(),
    clearCalls: [],
    deliverCalls: [],
    clearResult: { ok: true, reason: "cleared" },
    deliveryVerified: true,
    deliverError: null,
    corruptTaskAfterDelivery: false,
    interruptCalls: [],
    interruptResult: { ok: true, reason: "interrupt_sent" },
    worktreeResult: { clean: true, detail: "clean" },
    projectedAssigned: [],
    projectedReleased: [],
    projectionCalls: 0,
    projectorThrowOn: null,
    orderLog: [],
    close: () => {
      db.close();
      rmSync(directory, { recursive: true, force: true });
    },
  };
  registerAssignmentEffectRoutes(state.app, {
    db,
    clearSlot: async (slot) => {
      state.clearCalls.push(slot);
      return state.clearResult;
    },
    interruptTurn: async (slot) => {
      state.interruptCalls.push(slot);
      return state.interruptResult;
    },
    observeWorktree: async () => state.worktreeResult,
    issueProjection: {
      onAssigned: async (issue, slot, repository) => {
        state.projectionCalls += 1;
        if (state.projectorThrowOn === "assigned") throw new Error("gh labels down");
        state.orderLog.push("project:assigned");
        state.projectedAssigned.push({ issue, slot, repository: repository ?? null });
        return {
          status: "projected", reason: null, repository: repository ?? null,
          issue, slot, added_labels: ["status:in-progress"], removed_labels: ["status:todo"], verified: true,
        };
      },
      onReleased: async (issue, slot, repository) => {
        state.projectionCalls += 1;
        if (state.projectorThrowOn === "released") throw new Error("gh labels down");
        state.orderLog.push("project:released");
        state.projectedReleased.push({ issue, slot, repository: repository ?? null });
        return {
          status: "projected", reason: null, repository: repository ?? null,
          issue, slot, added_labels: ["status:todo"], removed_labels: ["status:in-progress"], verified: true,
        };
      },
    },
    deliverTaskFile: async (slot, filePath) => {
      state.deliverCalls.push(`${slot}:${filePath}`);
      state.orderLog.push("deliver");
      if (state.corruptTaskAfterDelivery) {
        // Simulate a durable row that does not corroborate the delivery: the
        // ownership tuple survives but the recorded task text is gone.
        state.db.updateSlot(slot, { task: "" });
      }
      if (state.deliverError) {
        return {
          verified: false,
          reason: state.deliverError,
          receipt: { slot, verified: false },
        } satisfies AssignmentEffectDeliveryResult;
      }
      return {
        verified: state.deliveryVerified,
        reason: state.deliveryVerified ? undefined : "session_delivery_unverified",
        receipt: { slot, pane: "%1", mode: "file", verified: state.deliveryVerified },
      } satisfies AssignmentEffectDeliveryResult;
    },
  });
  return state;
}

function effectBody(
  slot: number,
  expectedEpoch: number,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    effect_id: `assign-${slot}-8110`,
    selection_class: "new_issue",
    expected_epoch: expectedEpoch,
    repository_id: REPO,
    issue: 8110,
    pr: null,
    branch: "fix/8110-assignment-boundary",
    head_sha: HEAD,
    work_kind: "implementation",
    handoff_id: "handoff-8110",
    claimed_at: "2026-09-21T15:00:00.000Z",
    task: TASK,
    task_file: "/tmp/8110-task.md",
    ...overrides,
  };
}

async function post(
  app: Hono,
  slot: number,
  body: Record<string, unknown>,
): Promise<{ status: number; json: any }> {
  const response = await app.request(`http://mop/slots/${slot}/assign-effect`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: response.status, json: await response.json() };
}

test("atomic assignment commits ownership before delivery and returns both receipts", async () => {
  const h = harness();
  try {
    const before = h.db.getSlot(3)!;
    const result = await post(h.app, 3, effectBody(3, before.assignment_epoch));
    assert.equal(result.status, 200);
    assert.equal(result.json.status, "assigned");
    assert.equal(result.json.ownership_receipt.issue, 8110);
    assert.equal(result.json.delivery_receipt.verified, true);
    assert.deepEqual(h.clearCalls, [3], "new_issue must clear the session exactly once");
    assert.deepEqual(h.deliverCalls, ["3:/tmp/8110-task.md"]);
    const after = h.db.getSlot(3)!;
    assert.equal(after.occupied, true);
    assert.equal(after.issue, 8110);
    assert.equal(after.task, TASK);
    assert.equal(after.assignment_epoch, before.assignment_epoch + 1);
    const intent = h.db.getAssignmentEffectIntent("assign-3-8110")!;
    assert.equal(intent.state, "delivered");
    assert.equal(intent.committed_epoch, after.assignment_epoch);
  } finally {
    h.close();
  }
});

test("replaying the same binding is idempotent: no second epoch bump and no second task", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(2)!.assignment_epoch;
    const first = await post(h.app, 2, effectBody(2, epoch0));
    assert.equal(first.json.status, "assigned");
    const committed = h.db.getSlot(2)!.assignment_epoch;
    const second = await post(h.app, 2, effectBody(2, epoch0));
    assert.equal(second.status, 200);
    assert.equal(second.json.status, "assigned");
    assert.equal(second.json.idempotent, true);
    assert.equal(h.db.getSlot(2)!.assignment_epoch, committed, "replay must not bump the epoch");
    assert.deepEqual(h.deliverCalls, ["2:/tmp/8110-task.md"], "replay must not deliver twice");
    assert.deepEqual(h.clearCalls, [2], "replay must not clear twice");
  } finally {
    h.close();
  }
});

test("a failed delivery leaves a NAMED recoverable state and never rolls ownership back", async () => {
  const h = harness();
  try {
    h.deliverError = "session_delivery_unverified";
    const epoch0 = h.db.getSlot(4)!.assignment_epoch;
    const result = await post(h.app, 4, effectBody(4, epoch0));
    assert.equal(result.json.status, "refused");
    assert.equal(result.json.step_failed, "delivery");
    assert.equal(result.json.reason, "session_delivery_unverified");
    assert.equal(result.json.sanctioned_path, "mop-assign-slot");
    const after = h.db.getSlot(4)!;
    assert.equal(after.occupied, true, "ownership must NOT be rolled back implicitly");
    assert.equal(after.issue, 8110);
    assert.equal(after.task, TASK);
    assert.equal(after.assignment_epoch, epoch0 + 1);
    const intent = h.db.getAssignmentEffectIntent("assign-4-8110")!;
    assert.equal(intent.state, "pending_delivery");
    assert.match(result.json.slot_state_after, /occupied=true/);
    assert.match(result.json.slot_state_after, /task_present=true/);
  } finally {
    h.close();
  }
});

test("retrying a pending delivery resumes delivery without a second ownership commit", async () => {
  const h = harness();
  try {
    h.deliverError = "session_delivery_unverified";
    const epoch0 = h.db.getSlot(5)!.assignment_epoch;
    const failed = await post(h.app, 5, effectBody(5, epoch0));
    assert.equal(failed.json.step_failed, "delivery");
    const committed = h.db.getSlot(5)!.assignment_epoch;
    assert.equal(committed, epoch0 + 1);

    h.deliverError = null;
    const retried = await post(h.app, 5, effectBody(5, epoch0));
    assert.equal(retried.json.status, "assigned");
    assert.equal(retried.json.idempotent, false);
    assert.equal(h.db.getSlot(5)!.assignment_epoch, committed, "retry must not bump the epoch again");
    assert.deepEqual(h.deliverCalls, ["5:/tmp/8110-task.md", "5:/tmp/8110-task.md"]);
    assert.equal(h.db.getAssignmentEffectIntent("assign-5-8110")!.state, "delivered");
  } finally {
    h.close();
  }
});

test("a different binding under the same effect id is refused with zero mutation", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(1)!.assignment_epoch;
    await post(h.app, 1, effectBody(1, epoch0));
    const committed = h.db.getSlot(1)!.assignment_epoch;
    const conflict = await post(h.app, 1, effectBody(1, committed, { issue: 9999 }));
    assert.equal(conflict.status, 409);
    assert.equal(conflict.json.status, "refused");
    assert.equal(conflict.json.reason, "effect_binding_conflict");
    assert.equal(h.db.getSlot(1)!.assignment_epoch, committed);
    assert.equal(h.db.getSlot(1)!.issue, 8110, "the committed binding must survive");
  } finally {
    h.close();
  }
});

test("new_issue clear failure is recorded and the assign still succeeds", async () => {
  const h = harness();
  try {
    h.clearResult = { ok: false, reason: "assignment_clear_not_applied", detail: "refused (occupied; native release required)" };
    const epoch0 = h.db.getSlot(6)!.assignment_epoch;
    const result = await post(h.app, 6, effectBody(6, epoch0));
    // The clear is never gated: a failed clear is recorded, not refused.
    assert.equal(result.json.status, "assigned");
    const after = h.db.getSlot(6)!;
    assert.equal(after.occupied, true);
    assert.equal(after.issue, 8110);
    assert.equal(after.assignment_epoch, epoch0 + 1);
    assert.equal(h.deliverCalls.length, 1, "delivery still happens after a failed clear");
  } finally {
    h.close();
  }
});

test("repro and rework classes skip the session clear and still commit then deliver", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(2)!.assignment_epoch;
    const result = await post(h.app, 2, effectBody(2, epoch0, {
      effect_id: "assign-2-8110-rework",
      selection_class: "rework",
    }));
    assert.equal(result.json.status, "assigned");
    assert.deepEqual(h.clearCalls, [], "non-new_issue classes must not clear the session");
    assert.deepEqual(h.deliverCalls, ["2:/tmp/8110-task.md"]);
  } finally {
    h.close();
  }
});

test("an occupied slot is transferred through the CAS rebind with the complete tuple", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    const seeded = h.db.assignSlot(
      3,
      "previous task",
      REPO,
      7000,
      "fix/7000-previous",
      null,
      HEAD,
      epoch0,
      "implementation",
      "handoff-7000",
      true,
    );
    assert.equal(seeded.ok, true);
    const occupiedEpoch = h.db.getSlot(3)!.assignment_epoch;

    const result = await post(h.app, 3, effectBody(3, occupiedEpoch, {
      effect_id: "assign-3-8110-rebind",
      selection_class: "rework",
    }));
    assert.equal(result.json.status, "assigned");
    const after = h.db.getSlot(3)!;
    assert.equal(after.issue, 8110);
    assert.equal(after.assignment_epoch, occupiedEpoch + 1);
  } finally {
    h.close();
  }
});

test("invalid tuple and unknown receipt are refused without mutation", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(1)!.assignment_epoch;
    const bad = await post(h.app, 1, effectBody(1, epoch0, { head_sha: "not-a-sha" }));
    assert.equal(bad.status, 400);
    assert.equal(bad.json.status, "refused");
    assert.equal(bad.json.step_failed, "ownership");
    assert.equal(h.db.getSlot(1)!.occupied, false);

    const missing = await h.app.request("http://mop/slots/1/assignment-effect-receipt?effect_id=nope");
    assert.equal(missing.status, 404);

    const ok = await post(h.app, 1, effectBody(1, epoch0));
    assert.equal(ok.json.status, "assigned");
    const receipt = await h.app.request("http://mop/slots/1/assignment-effect-receipt?effect_id=assign-1-8110");
    assert.equal(receipt.status, 200);
    const payload = await receipt.json();
    assert.equal(payload.success, true);
    assert.equal(payload.state, "delivered");
  } finally {
    h.close();
  }
});

test("the durable digest is stable across identical bindings and changes with the task text", () => {
  const base = {
    effect_id: "assign-1-8110",
    selection_class: "new_issue" as const,
    expected_epoch: 4,
    desired_tuple: {
      repository_id: REPO,
      issue: 8110,
      pr: null,
      branch: "fix/8110-assignment-boundary",
      head_sha: HEAD,
      work_kind: "implementation",
      handoff_id: "handoff-8110",
      claimed_at: "2026-09-21T15:00:00.000Z",
    },
    task: TASK,
    task_file: "/tmp/8110-task.md",
  };
  const digest = computeAssignmentEffectDigest(base);
  assert.equal(digest, computeAssignmentEffectDigest({ ...base }));
  assert.notEqual(digest, computeAssignmentEffectDigest({ ...base, task: TASK + "extra" }));
});

test("a readback failure is never replayed as an assigned success", async () => {
  const h = harness();
  try {
    h.corruptTaskAfterDelivery = true;
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;

    const first = await post(h.app, 3, effectBody(3, epoch0));
    assert.equal(first.json.status, "refused");
    assert.equal(first.json.step_failed, "readback");
    assert.equal(first.json.reason, "assignment_readback_inconsistent");
    assert.equal(
      h.db.getAssignmentEffectIntent("assign-3-8110")!.state,
      "pending_delivery",
      "a failed readback must not finalize the intent",
    );

    const second = await post(h.app, 3, effectBody(3, epoch0));
    assert.equal(second.json.status, "refused");
    assert.equal(second.json.step_failed, "readback", "an identical retry must stay a readback refusal");
    assert.notEqual(second.json.idempotent, true);
    assert.equal(
      h.db.getAssignmentEffectIntent("assign-3-8110")!.state,
      "pending_delivery",
      "the intent must still be un-finalized after the replay",
    );
  } finally {
    h.close();
  }
});

test("pending_delivery resume refuses on ownership drift with zero delivery", async () => {
  const h = harness();
  try {
    h.deliverError = "session_delivery_unverified";
    const epoch0 = h.db.getSlot(5)!.assignment_epoch;
    const failed = await post(h.app, 5, effectBody(5, epoch0));
    assert.equal(failed.json.step_failed, "delivery");
    assert.equal(h.db.getAssignmentEffectIntent("assign-5-8110")!.state, "pending_delivery");
    const deliveriesBefore = h.deliverCalls.length;

    // External ownership change: another actor rebinds the slot away.
    const now = h.db.getSlot(5)!;
    const rebind = h.db.rebindSlot(
      5,
      now.assignment_epoch,
      {
        repository_id: now.repository_id,
        issue: now.issue,
        pr: now.pr,
        branch: now.branch,
        head_sha: now.head_sha,
        work_kind: now.work_kind,
        handoff_id: now.handoff_id,
        claimed_at: now.claimed_at,
      },
      {
        repository_id: REPO,
        issue: 9999,
        pr: null,
        branch: "fix/9999-someone-else",
        head_sha: HEAD,
        work_kind: "implementation",
        handoff_id: "handoff-9999",
        claimed_at: "2026-09-21T16:00:00.000Z",
      },
      "someone else's task",
    );
    assert.equal(rebind.ok, true);

    h.deliverError = null;
    const retried = await post(h.app, 5, effectBody(5, epoch0));
    assert.equal(retried.json.status, "refused");
    assert.equal(retried.json.step_failed, "ownership");
    assert.equal(retried.json.reason, "assignment_ownership_drift_on_resume");
    assert.equal(
      h.deliverCalls.length,
      deliveriesBefore,
      "a resumed intent must deliver nothing when ownership drifted",
    );
    assert.equal(h.db.getSlot(5)!.issue, 9999, "the external owner must survive untouched");
  } finally {
    h.close();
  }
});

test("invalid_assignment_tuple names the failing tuple predicate (additive diagnosability)", async () => {
  const h = harness();
  try {
    const epoch = h.db.getSlot(1)!.assignment_epoch;
    const cases: Array<[string, Record<string, unknown>]> = [
      ["head_sha", { head_sha: "abc1234" }],          // short SHA is the case that misled the caller
      ["head_sha", { head_sha: 12345 }],              // non-string
      ["work_kind", { work_kind: "" }],               // empty
      ["work_kind", { work_kind: "   " }],            // whitespace-only
      ["work_kind", { work_kind: "banana" }],           // NONEMPTY but not in ASSIGNMENT_WORK_KINDS
      ["work_kind", { work_kind: "Implementation" }],   // case variant is not a member
      ["work_kind", { handoff_id: "" }],              // empty (pair named by work_kind)
      ["work_kind", { handoff_id: "  " }],            // whitespace-only (pair named by work_kind)
      ["issue", { issue: 0 }],                        // not > 0
      ["issue", { issue: 1.5 }],                      // not an integer
      ["repository_id", { repository_id: "" }],       // normalizes empty
    ];
    for (const [field, overrides] of cases) {
      const r = await post(h.app, 1, effectBody(1, epoch, overrides));
      assert.equal(r.status, 400, `${field}: expected 400`);
      assert.equal(r.json.status, "refused");
      assert.equal(r.json.step_failed, "ownership", "refusal shape unchanged");
      assert.equal(r.json.reason, "invalid_assignment_tuple", "reason code unchanged");
      assert.equal(r.json.sanctioned_path, "mop-assign-slot", "sanctioned path unchanged");
      assert.equal(r.json.failed_field, field, `expected failed_field=${field}, got ${r.json.failed_field}`);
      assert.equal(h.db.getSlot(1)!.occupied, false, "no mutation on refusal");
    }
    // A valid tuple must NOT carry failed_field and must still work.
    const ok = await post(h.app, 1, effectBody(1, epoch, {}));
    assert.notEqual(ok.status, 400, "a valid tuple must not be refused");
    assert.equal(ok.json.failed_field, undefined, "failed_field must be absent on a non-refusal");
  } finally {
    h.close();
  }
});

function simpleBody(
  slot: number,
  expectedEpoch: number,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return effectBody(slot, expectedEpoch, {
    effect_id: `assign-${slot}-simple-8131`,
    issue: 8131,
    branch: "fix/8131-simple",
    handoff_id: "handoff-8131",
    ...overrides,
  });
}

function auditEvents(h: Harness, slot: number, type: string): Array<Record<string, unknown>> {
  return h.db.getEvents(slot)
    .filter((entry) => entry.event_type === type)
    .map((entry) => JSON.parse(entry.payload) as Record<string, unknown>);
}

test("assign onto an occupied slot with an active stale turn succeeds and audits the displacement", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(6)!.assignment_epoch;
    const seeded = h.db.assignSlot(
      6, "stale lane task", REPO, 8083, "fix/8083-lane", 8115, HEAD, epoch0,
      "implementation", "handoff-8083", true,
    );
    assert.equal(seeded.ok, true);
    h.db.updateSlot(6, {
      active_turn_id: "98e27143-stale",
      active_turn_state: "active",
      active_turn_started_at: "2026-09-23T08:00:00.000Z",
      last_meaningful_work_at: "2026-09-23T08:00:00.000Z",
    });
    const occupiedEpoch = h.db.getSlot(6)!.assignment_epoch;

    // One call, no release-first, no abandon-turn, no retry.
    const result = await post(h.app, 6, simpleBody(6, occupiedEpoch));
    assert.equal(result.json.status, "assigned");
    const after = h.db.getSlot(6)!;
    assert.equal(after.issue, 8131);
    assert.equal(after.assignment_epoch, occupiedEpoch + 1, "exactly one epoch bump");
    assert.equal(after.active_turn_id, null, "stale turn pointer cleared in the same write");
    assert.equal(after.active_turn_state, "inactive");
    assert.equal(h.interruptCalls.length, 1, "the live turn is interrupted once");
    assert.deepEqual(result.json.displacement.prior.issue, 8083);
    assert.deepEqual(result.json.displacement.prior.active_turn_id, "98e27143-stale");
    const displaced = auditEvents(h, 6, "assignment_displaced");
    assert.equal(displaced.length, 1, "exactly one displacement audit row");
    assert.equal((displaced[0].prior as Record<string, unknown>).issue, 8083);
    assert.equal((displaced[0].prior as Record<string, unknown>).active_turn_id, "98e27143-stale");
    assert.equal(displaced[0].worktree_reset, false, "the worktree is never reset");
  } finally {
    h.close();
  }
});

test("assign then immediately assign another lane: no wait, no retry", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(2)!.assignment_epoch;
    const first = await post(h.app, 2, simpleBody(2, epoch0, {
      effect_id: "assign-2-first-8131",
      selection_class: "rework",
    }));
    assert.equal(first.json.status, "assigned");
    const epoch1 = h.db.getSlot(2)!.assignment_epoch;
    assert.equal(epoch1, epoch0 + 1);
    // The second lane lands immediately on the now-occupied slot.
    const second = await post(h.app, 2, effectBody(2, epoch1, {
      effect_id: "assign-2-second-8132",
      selection_class: "rework",
      issue: 8132,
      branch: "fix/8132-second",
      handoff_id: "handoff-8132",
    }));
    assert.equal(second.json.status, "assigned");
    const after = h.db.getSlot(2)!;
    assert.equal(after.issue, 8132);
    assert.equal(after.assignment_epoch, epoch1 + 1, "one bump per assignment");
    assert.deepEqual((second.json.displacement.prior as Record<string, unknown>).issue, 8131);
  } finally {
    h.close();
  }
});

test("the same lane on two slots refuses duplicate_assignment naming the holder", async () => {
  const h = harness();
  try {
    const epochA = h.db.getSlot(1)!.assignment_epoch;
    const seeded = h.db.assignSlot(
      1, "owner task", REPO, 8131, "fix/8131-simple", null, HEAD, epochA,
      "implementation", "handoff-8131", true,
    );
    assert.equal(seeded.ok, true);
    const epochB = h.db.getSlot(2)!.assignment_epoch;
    const result = await post(h.app, 2, simpleBody(2, epochB, {
      effect_id: "assign-2-dupe-8131",
    }));
    assert.equal(result.status, 409);
    assert.equal(result.json.status, "refused");
    assert.equal(result.json.step_failed, "ownership");
    assert.equal(result.json.reason, "duplicate_assignment");
    assert.equal(result.json.sanctioned_path, "mop-assign-slot");
    assert.deepEqual(result.json.owner_slots, [1], "the holder slot is retained in the refusal");
    // The holder keeps the lane; the refused slot is untouched.
    assert.equal(h.db.getSlot(1)!.issue, 8131);
    assert.equal(h.db.getSlot(1)!.occupied, true);
    assert.equal(h.db.getSlot(2)!.occupied, false);
    assert.equal(h.db.getSlot(2)!.assignment_epoch, epochB, "zero mutation on refusal");
  } finally {
    h.close();
  }
});

test("same-lane re-assign to the same slot is idempotent", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(5)!.assignment_epoch;
    const first = await post(h.app, 5, simpleBody(5, epoch0, {
      effect_id: "assign-5-same-lane",
      selection_class: "rework",
    }));
    assert.equal(first.json.status, "assigned");
    const epoch1 = h.db.getSlot(5)!.assignment_epoch;
    assert.equal(epoch1, epoch0 + 1);
    const second = await post(h.app, 5, simpleBody(5, epoch0, {
      effect_id: "assign-5-same-lane-again",
      selection_class: "rework",
    }));
    assert.equal(second.json.status, "assigned");
    assert.equal(h.db.getSlot(5)!.assignment_epoch, epoch1, "no second ownership commit");
    assert.equal(h.db.getSlot(5)!.issue, 8131);
  } finally {
    h.close();
  }
});

test("a minimal --slot/--issue binding assigns with no tuple ceremony", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    // No branch, head, work-kind, handoff, or task text: the lane identity
    // alone commits. PM passes --slot 3 --issue 8131 and nothing else.
    const result = await post(h.app, 3, {
      effect_id: "assign-3-minimal",
      selection_class: "new_issue",
      repository_id: REPO,
      issue: 8131,
    });
    assert.equal(result.json.status, "assigned");
    const after = h.db.getSlot(3)!;
    assert.equal(after.issue, 8131);
    assert.equal(after.occupied, true);
    assert.equal(after.assignment_epoch, epoch0 + 1);
    assert.deepEqual(result.json.delivery_receipt, {
      slot: 3,
      skipped: "no_task_text",
      verified: true,
    });
  } finally {
    h.close();
  }
});

test("an S6-shaped wedge (occupied, stale turn, no Stop hook) clears in one assign call", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(6)!.assignment_epoch;
    const seeded = h.db.assignSlot(
      6, "terminal lane task", REPO, 8099, "fix/8099-lane", 8115, HEAD, epoch0,
      "implementation", "handoff-8099", true,
    );
    assert.equal(seeded.ok, true);
    // Stale turn with no Stop hook coming: terminalize-by-assign is the path.
    h.db.updateSlot(6, {
      active_turn_id: "stale-no-stop-hook",
      active_turn_state: "active",
      active_turn_started_at: "2026-09-23T08:00:00.000Z",
      last_meaningful_work_at: "2026-09-23T08:00:00.000Z",
    });
    const occupiedEpoch = h.db.getSlot(6)!.assignment_epoch;
    const result = await post(h.app, 6, simpleBody(6, occupiedEpoch, {
      effect_id: "assign-6-s6-shape",
      issue: 8131,
    }));
    assert.equal(result.json.status, "assigned");
    const after = h.db.getSlot(6)!;
    assert.equal(after.issue, 8131);
    assert.equal(after.active_turn_id, null);
    assert.equal(after.active_turn_state, "inactive");
    assert.equal(h.interruptCalls.length, 1);
  } finally {
    h.close();
  }
});

test("new_issue projects the lane labels exactly once and returns the outcome", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    const result = await post(h.app, 3, effectBody(3, epoch0));
    assert.equal(result.json.status, "assigned");
    assert.deepEqual(h.projectedAssigned, [{ issue: 8110, slot: 3, repository: REPO }]);
    assert.deepEqual(h.projectedReleased, [], "a free-slot assign releases nothing");
    assert.equal(result.json.issue_projection.status, "projected");
    assert.equal(result.json.issue_projection.issue, 8110);
    assert.equal(result.json.issue_projection.slot, 3);
    // Labels run only after delivery succeeds: never before the slot has
    // both the ownership record and the task.
    assert.deepEqual(h.orderLog, ["deliver", "project:assigned"]);
    // The delivered receipt durably carries the projection outcome.
    const stored = h.db.getAssignmentEffectIntent("assign-3-8110")!;
    const receipt = JSON.parse(stored.delivery_receipt as string) as Record<string, unknown>;
    assert.equal((receipt.issue_projection as Record<string, unknown>).status, "projected");
  } finally {
    h.close();
  }
});

test("idempotent replay projects zero times and clobbers no labels", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    const first = await post(h.app, 3, effectBody(3, epoch0, { effect_id: "assign-3-proj-replay" }));
    assert.equal(first.json.status, "assigned");
    assert.equal(h.projectedAssigned.length, 1);
    assert.equal(first.json.issue_projection.status, "projected");
    const clearCount = h.clearCalls.length;
    const deliveryCount = h.deliverCalls.length;
    const committedEpoch = h.db.getSlot(3)!.assignment_epoch;
    const second = await post(h.app, 3, effectBody(3, committedEpoch, { effect_id: "assign-3-proj-replay" }));
    assert.equal(second.json.status, "assigned");
    assert.equal(second.json.idempotent, true);
    assert.equal(h.db.getSlot(3)!.assignment_epoch, committedEpoch);
    assert.equal(h.clearCalls.length, clearCount);
    assert.equal(h.deliverCalls.length, deliveryCount);
    assert.equal(h.projectedAssigned.length, 1, "the replay must not re-project");
    assert.equal(h.projectedReleased.length, 0);
    // The replay returns the STORED outcome without a new GitHub call.
    assert.equal(second.json.issue_projection.status, "projected");
    assert.equal(second.json.issue_projection.issue, 8110);
  } finally {
    h.close();
  }
});

test("a projector throw still returns assigned with a typed failed projection", async () => {
  const h = harness();
  try {
    h.projectorThrowOn = "assigned";
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    const result = await post(h.app, 3, effectBody(3, epoch0, { effect_id: "assign-3-proj-throw" }));
    assert.equal(result.json.status, "assigned", "labels never refuse the durable assignment");
    assert.equal(result.json.issue_projection.status, "failed");
    assert.match(String(result.json.issue_projection.reason), /issue_projection_unexpected/);
    assert.equal(h.db.getSlot(3)!.issue, 8110, "ownership committed despite the label failure");
    assert.equal(h.db.getSlot(3)!.occupied, true);
    // Same-binding resume finishes the labels WITHOUT repeating clear,
    // ownership commit, or pane delivery.
    h.projectorThrowOn = null;
    const clearsBefore = h.clearCalls.length;
    const deliveriesBefore = h.deliverCalls.length;
    const epochAfterFirst = h.db.getSlot(3)!.assignment_epoch;
    const resumed = await post(h.app, 3, effectBody(3, epochAfterFirst, { effect_id: "assign-3-proj-throw" }));
    assert.equal(resumed.json.status, "assigned");
    assert.equal(resumed.json.issue_projection.status, "projected", "the resume finishes the labels");
    assert.equal(h.projectedAssigned.length, 1, "exactly one successful projection total");
    assert.equal(h.clearCalls.length, clearsBefore, "no repeated clear");
    assert.equal(h.deliverCalls.length, deliveriesBefore, "no repeated delivery");
    assert.equal(h.db.getSlot(3)!.assignment_epoch, epochAfterFirst, "no repeated commit");
  } finally {
    h.close();
  }
});

test("a failed projection retry refuses after the assigned slot is released", async () => {
  const h = harness();
  try {
    h.projectorThrowOn = "assigned";
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    const first = await post(h.app, 3, effectBody(3, epoch0, { effect_id: "assign-3-proj-released" }));
    assert.equal(first.json.issue_projection.status, "failed");
    const committedEpoch = h.db.getSlot(3)!.assignment_epoch;
    h.projectorThrowOn = null;
    const callsBeforeReleaseRetry = h.projectionCalls;

    const released = h.db.releaseSlotSimple(3);
    assert.equal(released.ok, true);
    const retry = await post(
      h.app,
      3,
      effectBody(3, committedEpoch, { effect_id: "assign-3-proj-released" }),
    );

    assert.equal(retry.status, 409);
    assert.equal(retry.json.reason, "assignment_superseded");
    assert.equal(h.projectionCalls, callsBeforeReleaseRetry, "superseded replay makes no projection calls");
    assert.equal(h.db.getSlot(3)!.assignment_epoch, committedEpoch + 1);
  } finally {
    h.close();
  }
});

test("a failed projection retry refuses after the slot is reassigned", async () => {
  const h = harness();
  try {
    h.projectorThrowOn = "assigned";
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    const first = await post(h.app, 3, effectBody(3, epoch0, { effect_id: "assign-3-proj-reassigned" }));
    assert.equal(first.json.issue_projection.status, "failed");
    const committedEpoch = h.db.getSlot(3)!.assignment_epoch;
    h.projectorThrowOn = null;

    const released = h.db.releaseSlotSimple(3);
    assert.equal(released.ok, true);
    const reassigned = h.db.assignSlot(
      3,
      "replacement task",
      REPO,
      9001,
      "fix/9001-replacement",
      null,
      HEAD,
      released.assignment_epoch,
      "implementation",
      "handoff-9001",
      true,
    );
    assert.equal(reassigned.ok, true);
    const replacementEpoch = h.db.getSlot(3)!.assignment_epoch;
    const callsBeforeRetry = h.projectionCalls;

    const retry = await post(
      h.app,
      3,
      effectBody(3, committedEpoch, { effect_id: "assign-3-proj-reassigned" }),
    );

    assert.equal(retry.status, 409);
    assert.equal(retry.json.reason, "assignment_superseded");
    assert.equal(h.projectionCalls, callsBeforeRetry, "superseded replay makes no projection calls");
    assert.equal(h.db.getSlot(3)!.issue, 9001, "replacement ownership remains intact");
    assert.equal(h.db.getSlot(3)!.assignment_epoch, replacementEpoch);
  } finally {
    h.close();
  }
});

test("occupied assign projects the displaced release plus the new claim, release-first", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    const seeded = h.db.assignSlot(
      3, "previous task", REPO, 7000, "fix/7000-previous", null, HEAD, epoch0,
      "implementation", "handoff-7000", true,
    );
    assert.equal(seeded.ok, true);
    const occupiedEpoch = h.db.getSlot(3)!.assignment_epoch;
    const result = await post(h.app, 3, effectBody(3, occupiedEpoch, {
      effect_id: "assign-3-proj-displace",
      selection_class: "rework",
    }));
    assert.equal(result.json.status, "assigned");
    assert.deepEqual(h.projectedReleased, [{ issue: 7000, slot: 3, repository: REPO }]);
    assert.deepEqual(h.projectedAssigned, [{ issue: 8110, slot: 3, repository: REPO }]);
    assert.equal(result.json.release_projection.status, "projected");
    assert.equal(result.json.release_projection.issue, 7000);
    assert.equal(result.json.issue_projection.status, "projected");
    assert.equal(result.json.issue_projection.issue, 8110);
    assert.deepEqual(h.orderLog, ["deliver", "project:released", "project:assigned"]);
  } finally {
    h.close();
  }
});

test("an already-projected lane reconciles without duplicate label work", async () => {
  const h = harness();
  try {
    // First bind projects the lane.
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    const first = await post(h.app, 3, effectBody(3, epoch0, { effect_id: "assign-3-proj-reconcile" }));
    assert.equal(first.json.status, "assigned");
    assert.equal(h.projectedAssigned.length, 1);
    // A stale duplicate bind of the same lane elsewhere refuses (no clobber),
    // and the holder keeps exactly one projection.
    const epoch2 = h.db.getSlot(2)!.assignment_epoch;
    const dupe = await post(h.app, 2, effectBody(2, epoch2, {
      effect_id: "assign-2-proj-dupe",
      issue: 8110,
      branch: "fix/8110-assignment-boundary",
      handoff_id: "handoff-8110",
    }));
    assert.equal(dupe.json.status, "refused");
    assert.equal(dupe.json.reason, "duplicate_assignment");
    assert.equal(h.projectedAssigned.length, 1, "a refused bind writes no labels");
    assert.equal(h.db.getSlot(3)!.issue, 8110, "the holder is untouched");
  } finally {
    h.close();
  }
});

test("REVISE-1: failed delivery then resume unwinds the displaced lane without repeating MoP steps", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    const seeded = h.db.assignSlot(
      3, "previous task", REPO, 7000, "fix/7000-previous", null, HEAD, epoch0,
      "implementation", "handoff-7000", true,
    );
    assert.equal(seeded.ok, true);
    const occupiedEpoch = h.db.getSlot(3)!.assignment_epoch;
    // Attempt 1: delivery fails AFTER the commit. No labels may run yet.
    h.deliverError = "session_delivery_unverified";
    const failed = await post(h.app, 3, effectBody(3, occupiedEpoch, {
      effect_id: "assign-3-resume-release",
      selection_class: "rework",
    }));
    assert.equal(failed.json.status, "refused");
    assert.equal(failed.json.step_failed, "delivery");
    assert.equal(h.db.getSlot(3)!.issue, 8110, "ownership committed before the failed delivery");
    assert.deepEqual(h.projectedAssigned, [], "no labels before delivery succeeds");
    assert.deepEqual(h.projectedReleased, [], "no labels before delivery succeeds");
    // The displaced prior is persisted on the committed intent.
    const recorded = h.db.getAssignmentEffectIntent("assign-3-resume-release")!;
    assert.deepEqual(JSON.parse(recorded.displaced_tuple as string), { issue: 7000, repository_id: REPO });
    // Attempt 2 (same binding): delivery completes; the resume unwinds the
    // prior lane first, then projects the new claim.
    h.deliverError = null;
    const clearsBefore = h.clearCalls.length;
    const resumed = await post(h.app, 3, effectBody(3, occupiedEpoch, {
      effect_id: "assign-3-resume-release",
      selection_class: "rework",
    }));
    assert.equal(resumed.json.status, "assigned");
    assert.deepEqual(h.projectedReleased, [{ issue: 7000, slot: 3, repository: REPO }]);
    assert.deepEqual(h.projectedAssigned, [{ issue: 8110, slot: 3, repository: REPO }]);
    assert.deepEqual(
      h.orderLog,
      ["deliver", "deliver", "project:released", "project:assigned"],
      "one failed delivery, then delivery, then release-first projection",
    );
    assert.equal(h.clearCalls.length, clearsBefore, "no repeated clear");
    assert.equal(h.db.getSlot(3)!.assignment_epoch, occupiedEpoch + 1, "no repeated commit");
    assert.equal(resumed.json.release_projection.issue, 7000);
    assert.equal(resumed.json.issue_projection.issue, 8110);
  } finally {
    h.close();
  }
});

test("REVISE-1-negative: resume with no recorded displacement projects only the new claim", async () => {
  const h = harness();
  try {
    const epoch0 = h.db.getSlot(3)!.assignment_epoch;
    h.deliverError = "session_delivery_unverified";
    const failed = await post(h.app, 3, effectBody(3, epoch0, { effect_id: "assign-3-resume-nodeplaced" }));
    assert.equal(failed.json.step_failed, "delivery");
    assert.equal(h.db.getAssignmentEffectIntent("assign-3-resume-nodeplaced")!.displaced_tuple, null);
    h.deliverError = null;
    const resumed = await post(h.app, 3, effectBody(3, epoch0, { effect_id: "assign-3-resume-nodeplaced" }));
    assert.equal(resumed.json.status, "assigned");
    assert.deepEqual(h.projectedReleased, [], "nothing to unwind for a free-slot assign");
    assert.deepEqual(h.projectedAssigned, [{ issue: 8110, slot: 3, repository: REPO }]);
  } finally {
    h.close();
  }
});

test("REVISE-3: refused duplicate new_issue is side-effect free and names the holder", async () => {
  const h = harness();
  try {
    const epochA = h.db.getSlot(1)!.assignment_epoch;
    assert.equal(h.db.assignSlot(
      1, "owner task", REPO, 8131, "fix/8131-simple", null, HEAD, epochA,
      "implementation", "handoff-8110", true,
    ).ok, true);
    const epochB = h.db.getSlot(2)!.assignment_epoch;
    const before = h.db.getSlot(2)!;
    const result = await post(h.app, 2, effectBody(2, epochB, {
      effect_id: "assign-2-dupe-sideeffect",
      issue: 8131,
      branch: "fix/8131-simple",
      handoff_id: "handoff-8110",
    }));
    assert.equal(result.status, 409);
    assert.equal(result.json.status, "refused");
    assert.equal(result.json.step_failed, "ownership");
    assert.equal(result.json.reason, "duplicate_assignment");
    assert.deepEqual(result.json.owner_slots, [1], "the holder slot is named");
    assert.deepEqual(h.clearCalls, [], "no session clear before the duplicate decision");
    assert.deepEqual(h.deliverCalls, [], "no delivery before the duplicate decision");
    assert.deepEqual(h.interruptCalls, [], "no interrupt before the duplicate decision");
    assert.deepEqual(h.orderLog, [], "no projector touched");
    assert.deepEqual(h.db.getSlot(2), before, "the refused slot row is byte-identical");
    assert.equal(h.db.getSlot(2)!.assignment_epoch, epochB);
    assert.equal(h.db.getSlot(1)!.issue, 8131, "the holder is untouched");
  } finally {
    h.close();
  }
});
