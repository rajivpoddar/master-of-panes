import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  MoPDatabase,
  computeNoPaneReleaseDigest,
  slotAssignmentTuple,
  type AssignmentTupleInput,
} from "../src/db.js";
import {
  NativeSlotReleaseCoordinator,
  type CheckoutReadOnlyObservation,
  type CheckoutResetObservation,
  type NativeSlotNoPaneReleaseRequest,
  type NativeSlotReleaseRequest,
} from "../src/slotRelease.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const MAIN_HEAD = "b".repeat(40);
const CHECKOUT = "/tmp/mop-release-selfturn-checkout";
const SESSION_ID = "f5fcb838-63f7-4db8-8ca5-5cba558aa1db";

interface Harness {
  db: MoPDatabase;
  directory: string;
  resets: number;
  close: () => void;
  coordinator: NativeSlotReleaseCoordinator;
}

/**
 * Slot 1 owns a branch-bearing tuple whose checkout is NOT settled (on a feature
 * branch), so `release()` takes the checkout-reset branch. As of the 2026-09-21
 * directive that branch delivers NO pane prose: the release effect is the settle
 * guard plus the checkout switch to main at the exact intended head.
 */
function harness(options: {
  task?: string;
  onSleep?: (db: MoPDatabase, tick: number) => void;
  onIdleProbe?: (db: MoPDatabase) => void;
  selfTurnSettleTimeoutMs?: number;
  selfTurnSettlePollMs?: number;
} = {}) {
  const directory = mkdtempSync(join(tmpdir(), "mop-release-selfturn-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const task = options.task ?? "issue 8018 bounded rework";
  assert.equal(
    db.assignSlot(1, task, "github:heydonna-app/heydonna-app", 8018, "fix-8018-self-turn", 8018, "a".repeat(40), 0, "rework", "handoff-8018").ok,
    true,
  );
  db.updateSlot(1, { idle: true, activity: "waiting_for_pm_direction", last_meaningful_work_at: null });
  const state = { resets: 0, now: Date.now(), tick: 0 };
  const coordinator = new NativeSlotReleaseCoordinator({
    db,
    resolveOwningCheckout: async () => CHECKOUT,
    owningSlotIsIdle: async () => {
      options.onIdleProbe?.(db);
      return true;
    },
    resetAndObserveCheckout: async (): Promise<CheckoutResetObservation> => {
      state.resets += 1;
      return { checkout_path: CHECKOUT, branch: "main", head: MAIN_HEAD, clean: true, reset_succeeded: true, error: null };
    },
    // NOT settled: on a feature branch -> the checkout-reset branch runs.
    observeCheckout: async (): Promise<CheckoutReadOnlyObservation> => ({
      checkout_path: CHECKOUT, clean: true, unpushed_commits: [], branch: "fix-8018-self-turn", head: "a".repeat(40),
    }),
    selfTurnSettleTimeoutMs: options.selfTurnSettleTimeoutMs,
    selfTurnSettlePollMs: options.selfTurnSettlePollMs ?? 1_000,
    nowMs: () => state.now,
    sleep: async (ms: number) => {
      state.now += ms;
      state.tick += 1;
      options.onSleep?.(db, state.tick);
    },
  });
  return {
    db,
    directory,
    coordinator,
    get resets() { return state.resets; },
    close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); },
  } satisfies Harness;
}

function releaseRequest(db: MoPDatabase): NativeSlotReleaseRequest {
  const current = db.getSlot(1)!;
  const tuple = slotAssignmentTuple(current)!;
  return {
    slot: 1,
    expected_epoch: current.assignment_epoch,
    expected_tuple: {
      repository_id: tuple.repository_id, issue: tuple.issue, pr: tuple.pr, branch: tuple.branch,
      head_sha: tuple.head_sha, work_kind: tuple.work_kind, handoff_id: tuple.handoff_id, claimed_at: tuple.claimed_at,
    },
    intended_main_head: MAIN_HEAD,
  };
}

function effectAudits(db: MoPDatabase) {
  return db.getEvents(1, 50, "release_effect_delivered");
}

function quiet(db: MoPDatabase): void {
  db.updateSlot(1, { last_meaningful_work_at: new Date(Date.now() - 10 * 60 * 1000).toISOString() });
}

test("GREEN: a release delivers NO pane prose, emits a machine-readable effect receipt, and releases exactly once", async () => {
  const value = harness();
  try {
    const request = releaseRequest(value.db);
    const result = await value.coordinator.release(request);

    assert.equal(result.success, true, `expected a clean release: ${result.code} / ${result.message}`);
    assert.equal(result.code, "released");
    assert.equal(value.db.getSlot(1)!.occupied, false);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, request.expected_epoch + 1);
    assert.equal(slotAssignmentTuple(value.db.getSlot(1)!), null);
    assert.equal(value.resets, 1, "the checkout switch still runs");

    // The stop-work message must not exist in ANY path: no instruction event at all.
    assert.equal(
      value.db.getEvents(1, 50, "release_instruction_delivered").length,
      0,
      "a release must never deliver a pane instruction",
    );
    const audits = effectAudits(value.db);
    assert.equal(audits.length, 1, "the release effect is audited exactly once");
    const payload = JSON.parse(audits[0].payload) as Record<string, unknown>;
    assert.equal(payload.delivery, "none");
    assert.equal(payload.instruction_bytes, 0, "no prose bytes are delivered");
    assert.equal(payload.effect, "checkout_reset_to_main");
    assert.equal(payload.epoch, request.expected_epoch);
    assert.equal(typeof payload.settle_ms, "number");
  } finally {
    value.close();
  }
});

test("GREEN: a turn that appears during the release settle and clears does not block the release", async () => {
  const value = harness({
    onSleep: (db, tick) => {
      if (tick === 1) { db.startAgentTurn(1, SESSION_ID); quiet(db); }
      if (tick === 2) db.finishAgentTurn(1, SESSION_ID);
    },
  });
  try {
    const result = await value.coordinator.release(releaseRequest(value.db));
    assert.equal(result.success, true, `expected release: ${result.code} / ${result.message}`);
    assert.equal(result.code, "released");
    const payload = JSON.parse(effectAudits(value.db)[0].payload) as Record<string, unknown>;
    assert.equal(payload.induced_turn_id, SESSION_ID, "the observed turn id is bound and audited");
    assert.equal(payload.settle_outcome, "settled");
    assert.equal(value.db.getEvents(1, 50, "release_instruction_delivered").length, 0);
  } finally {
    value.close();
  }
});

test("NEGATIVE: a turn that appears and never clears refuses bounded, with zero release mutation and no prose", async () => {
  const value = harness({
    selfTurnSettleTimeoutMs: 5_000,
    onSleep: (db, tick) => { if (tick === 1) { db.startAgentTurn(1, SESSION_ID); quiet(db); } },
  });
  try {
    const request = releaseRequest(value.db);
    const before = value.db.getSlot(1)!;
    const result = await value.coordinator.release(request);

    assert.equal(result.success, false);
    assert.equal(result.code, "slot_not_idle");
    assert.equal(value.resets, 0, "no checkout reset");
    assert.equal(value.db.getEvents(1, 50, "slot_released").length, 0, "zero release mutation");
    const after = value.db.getSlot(1)!;
    assert.equal(after.occupied, before.occupied);
    assert.equal(after.assignment_epoch, before.assignment_epoch);
    assert.equal(after.active_turn_id, SESSION_ID, "the turn is left exactly as it was");
    assert.equal(JSON.parse(effectAudits(value.db)[0].payload).settle_outcome, "timeout");
    assert.equal(value.db.getEvents(1, 50, "release_instruction_delivered").length, 0);
  } finally {
    value.close();
  }
});

test("NEGATIVE: an unrelated pre-existing active turn refuses before any release effect", async () => {
  const value = harness();
  try {
    value.db.startAgentTurn(1, "some-other-session");
    const result = await value.coordinator.release(releaseRequest(value.db));
    assert.equal(result.success, false);
    assert.equal(result.code, "slot_not_idle");
    assert.equal(result.cause, "active_turn");
    assert.equal(effectAudits(value.db).length, 0, "no effect, no audit");
    assert.equal(value.db.getSlot(1)!.occupied, true);
  } finally {
    value.close();
  }
});

test("NEGATIVE: a replacement turn during the release settle refuses and names the different turn", async () => {
  const value = harness({
    selfTurnSettleTimeoutMs: 20_000,
    onSleep: (db, tick) => {
      if (tick === 1) { db.startAgentTurn(1, SESSION_ID); quiet(db); }
      if (tick === 2) { db.finishAgentTurn(1, SESSION_ID); db.startAgentTurn(1, "replacement-session"); quiet(db); }
    },
  });
  try {
    const request = releaseRequest(value.db);
    const result = await value.coordinator.release(request);
    assert.equal(result.success, false);
    assert.equal(result.code, "slot_not_idle");
    assert.match(result.message, /different turn/);
    assert.match(result.message, /replacement-session/);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, request.expected_epoch, "no release mutation");
    assert.equal(JSON.parse(effectAudits(value.db)[0].payload).settle_outcome, "replacement_turn");
  } finally {
    value.close();
  }
});

test("NEGATIVE: epoch drift during the release settle refuses without release mutation", async () => {
  const value = harness({
    selfTurnSettleTimeoutMs: 20_000,
    onSleep: (db, tick) => {
      if (tick !== 1) return;
      const current = db.getSlot(1)!;
      const tuple = slotAssignmentTuple(current)!;
      assert.equal(db.rebindSlot(1, current.assignment_epoch, tuple, {
        repository_id: tuple.repository_id, issue: tuple.issue, pr: tuple.pr,
        branch: "fix-8018-replacement", head_sha: "c".repeat(40),
        work_kind: tuple.work_kind, handoff_id: "handoff-8018-replacement",
        claimed_at: tuple.claimed_at,
      }, "replacement owner during release settle").ok, true);
    },
  });
  try {
    const result = await value.coordinator.release(releaseRequest(value.db));
    assert.equal(result.success, false);
    assert.equal(result.code, "epoch_mismatch");
    assert.equal(value.db.getSlot(1)!.occupied, true, "the replacement owner is untouched");
    assert.equal(value.db.getSlot(1)!.assignment_epoch, 2, "the replacement epoch stands");
    assert.equal(value.db.getEvents(1, 50, "slot_released").length, 0);
  } finally {
    value.close();
  }
});

test("no-pane release accepts a stored task that differs from the caller only by surrounding whitespace", async () => {
  const value = harness({ task: "issue 8018 bounded rework\n" });
  try {
    const current = value.db.getSlot(1)!;
    const tuple = slotAssignmentTuple(current)!;
    const expectedTuple: AssignmentTupleInput = {
      repository_id: tuple.repository_id, issue: tuple.issue, pr: tuple.pr, branch: tuple.branch,
      head_sha: tuple.head_sha, work_kind: tuple.work_kind, handoff_id: tuple.handoff_id, claimed_at: tuple.claimed_at,
    };
    const request = {
      slot: 1,
      expected_epoch: current.assignment_epoch,
      expected_tuple: expectedTuple,
      expected_task: current.task!,
      checkout_path: CHECKOUT,
      effect_id: "no-pane-release:slot-1:epoch-0",
      request_digest: "",
    } satisfies NativeSlotNoPaneReleaseRequest;
    request.request_digest = computeNoPaneReleaseDigest({
      effect_id: request.effect_id,
      expected_epoch: request.expected_epoch,
      expected_tuple: request.expected_tuple,
      expected_task: request.expected_task,
      checkout_path: request.checkout_path,
    });

    const result = await value.coordinator.releaseWithoutPane(request);
    assert.equal(result.success, true, "a byte-identical task must satisfy the fence");
    assert.equal(result.code, "released");
    assert.equal(value.db.getSlot(1)!.occupied, false);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, request.expected_epoch + 1);
    assert.equal(value.db.getEvents(1, 50, "slot_released_no_pane").length, 1);
  } finally {
    value.close();
  }
});

test("SOURCE FENCE: the stop-work prose exists in no release path", () => {
  const root = new URL("../", import.meta.url).pathname;
  const slotRelease = readFileSync(join(root, "src", "slotRelease.ts"), "utf8");
  const server = readFileSync(join(root, "src", "server.ts"), "utf8");
  assert.equal(
    /Stop work on the current assignment/.test(slotRelease + server),
    false,
    "no source path may carry the stop-work prose",
  );
  assert.equal(/buildLiteralResetInstruction/.test(slotRelease), false, "the prose builder is gone");
  assert.equal(/deliverInstruction/.test(slotRelease + server), false, "the release has no pane delivery seam");
});
