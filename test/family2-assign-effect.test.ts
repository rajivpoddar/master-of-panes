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
    deliverTaskFile: async (slot, filePath) => {
      state.deliverCalls.push(`${slot}:${filePath}`);
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
    const conflict = await post(h.app, 1, effectBody(1, epoch0, { issue: 9999 }));
    assert.equal(conflict.status, 409);
    assert.equal(conflict.json.status, "refused");
    assert.equal(conflict.json.reason, "effect_binding_conflict");
    assert.equal(h.db.getSlot(1)!.assignment_epoch, committed);
    assert.equal(h.db.getSlot(1)!.issue, 8110, "the committed binding must survive");
  } finally {
    h.close();
  }
});

test("new_issue clear failure refuses at the clean step before any ownership commit", async () => {
  const h = harness();
  try {
    h.clearResult = { ok: false, reason: "assignment_clear_not_applied", detail: "refused (occupied; native release required)" };
    const epoch0 = h.db.getSlot(6)!.assignment_epoch;
    const result = await post(h.app, 6, effectBody(6, epoch0));
    assert.equal(result.json.status, "refused");
    assert.equal(result.json.step_failed, "clean");
    assert.equal(result.json.reason, "assignment_clear_not_applied");
    const after = h.db.getSlot(6)!;
    assert.equal(after.occupied, false, "no ownership commit may happen after a failed clear");
    assert.equal(after.assignment_epoch, epoch0);
    assert.deepEqual(h.deliverCalls, [], "nothing may be delivered after a failed clear");
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
