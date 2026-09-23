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
/** The live S3 shape: MoP records the Claude session id as the agent turn id. */
const SESSION_ID = "f5fcb838-63f7-4db8-8ca5-5cba558aa1db";

interface Harness {
  db: MoPDatabase;
  directory: string;
  deliveries: string[];
  resets: number;
  close: () => void;
}

/**
 * Slot 1 owns a branch-bearing tuple whose checkout is NOT settled (on a
 * feature branch), so `release()` takes the pane-mediated path and delivers the
 * reset instruction — exactly the production shape that self-blocked on S3.
 *
 * The delivered instruction is a user prompt, so the fake delivery mirrors the
 * real hook chain: it records an agent turn with the session id. The scripted
 * `sleep` then plays the Stop hook back.
 */
function harness(options: {
  task?: string;
  onDeliver?: (db: MoPDatabase) => void;
  onSleep?: (db: MoPDatabase, tick: number) => void;
  /** The post-delivery pane idle probe; used to model a prompt that registers late. */
  onIdleProbe?: (db: MoPDatabase) => void;
  /** Set false to model a prompt whose UserPromptSubmit has NOT registered yet. */
  autoRegisterOnDelivery?: boolean;
  selfTurnSettleTimeoutMs?: number;
  selfTurnSettlePollMs?: number;
  /** Read-only checkout observation; defaults to the unsettled feature-branch shape. */
  readOnly?: () => { checkout_path: string; clean: boolean; unpushed_commits: string[]; branch?: string; head?: string };
  /** Reset/attestation observation; defaults to a clean main acknowledgement. */
  reset?: () => CheckoutResetObservation;
} = {}) {
  const directory = mkdtempSync(join(tmpdir(), "mop-release-selfturn-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const task = options.task ?? "issue 8018 bounded rework";
  assert.equal(
    db.assignSlot(1, task, "github:heydonna-app/heydonna-app", 8018, "fix-8018-self-turn", 8018, "a".repeat(40), 0, "rework", "handoff-8018").ok,
    true,
  );
  db.updateSlot(1, { idle: true, activity: "waiting_for_pm_direction", last_meaningful_work_at: null });
  const state = { deliveries: [] as string[], resets: 0, now: Date.now(), tick: 0 };
  const coordinator = new NativeSlotReleaseCoordinator({
    db,
    resolveOwningCheckout: async () => CHECKOUT,
    deliverInstruction: async (_slot, instruction) => {
      state.deliveries.push(instruction);
      // UserPromptSubmit -> db.startAgentTurn(slot, session_id), exactly as the
      // live hook chain records the delivered instruction as an agent turn.
      // Suppressible: in the real race that hook can land AFTER this delivery,
      // which is exactly what the delivery-registration grace must absorb.
      if (options.autoRegisterOnDelivery !== false) db.startAgentTurn(1, SESSION_ID);
      options.onDeliver?.(db);
      return true;
    },
    owningSlotIsIdle: async () => {
      options.onIdleProbe?.(db);
      return true; // the pane probe that said "idle" while the row was busy
    },
    resetAndObserveCheckout: async (): Promise<CheckoutResetObservation> => {
      state.resets += 1;
      return options.reset?.() ?? { checkout_path: CHECKOUT, branch: "main", head: MAIN_HEAD, clean: true, reset_succeeded: true, error: null };
    },
    // NOT settled: on a feature branch -> forced pane-mediated path.
    observeCheckout: async (): Promise<CheckoutReadOnlyObservation> =>
      options.readOnly?.() ?? {
        checkout_path: CHECKOUT, clean: true, unpushed_commits: [], branch: "fix-8018-self-turn", head: "a".repeat(40),
      },
    selfTurnSettleTimeoutMs: options.selfTurnSettleTimeoutMs,
    selfTurnSettlePollMs: options.selfTurnSettlePollMs ?? 1_000,
    nowMs: () => state.now,
    sleep: async (ms: number) => {
      state.now += ms;
      state.tick += 1;
      options.onSleep?.(db, state.tick);
    },
  });
  const value: Harness & { coordinator: NativeSlotReleaseCoordinator } = {
    db,
    directory,
    coordinator,
    get deliveries() { return state.deliveries; },
    get resets() { return state.resets; },
    close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); },
  };
  return value;
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

function deliveryAudits(db: MoPDatabase) {
  return db.getEvents(1, 50, "release_instruction_delivered");
}

test("GREEN: the release waits out the turn its own reset instruction induces, then releases exactly once", async () => {
  const value = harness({
    // last_meaningful_work_at stays old so only the induced TURN is under test.
    onDeliver: (db) => db.updateSlot(1, { last_meaningful_work_at: new Date(Date.now() - 10 * 60 * 1000).toISOString() }),
    onSleep: (db, tick) => {
      if (tick === 1) {
        // Stop hook closes the induced turn.
        db.finishAgentTurn(1, SESSION_ID);
      }
    },
  });
  try {
    const request = releaseRequest(value.db);
    const result = await value.coordinator.release(request);

    assert.equal(result.success, true, "the release must not mistake its own instruction for a pre-existing turn");
    assert.equal(result.code, "released");
    assert.equal(result.idempotent, false);
    assert.equal(value.deliveries.length, 1, "exactly ONE reset instruction");
    assert.equal(value.resets, 1);
    assert.equal(value.db.getSlot(1)!.occupied, false);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, request.expected_epoch + 1);
    assert.equal(slotAssignmentTuple(value.db.getSlot(1)!), null);

    const audits = deliveryAudits(value.db);
    assert.equal(audits.length, 1, "the pane delivery is audited exactly once");
    const payload = JSON.parse(audits[0].payload) as Record<string, unknown>;
    assert.equal(payload.delivery, "pane");
    assert.equal(payload.induced_turn_id, SESSION_ID, "the audit names the self-induced turn");
    assert.equal(payload.settle_outcome, "settled");
    assert.equal(typeof payload.settle_ms, "number");
    assert.equal(payload.epoch, request.expected_epoch);
    // (`slot_released` is emitted by the HTTP route; the coordinator owns the
    // delivery audit above plus the FREE postcondition asserted below.)
  } finally {
    value.close();
  }
});

test("RED-BOUND NEGATIVE: an induced turn that never clears refuses bounded, with one audited delivery and no release", async () => {
  const value = harness({
    selfTurnSettleTimeoutMs: 5_000,
    onDeliver: (db) => db.updateSlot(1, { last_meaningful_work_at: new Date(Date.now() - 10 * 60 * 1000).toISOString() }),
    // No Stop hook: the row stays busy for the whole bounded wait.
  });
  try {
    const request = releaseRequest(value.db);
    const before = value.db.getSlot(1)!;
    const result = await value.coordinator.release(request);

    assert.equal(result.success, false);
    assert.equal(result.code, "slot_not_idle");
    assert.equal(result.cause, "active_turn");
    assert.match(result.message, /did not settle within/);
    assert.equal(value.deliveries.length, 1, "exactly one delivery, no retry loop");
    assert.equal(value.resets, 0, "no checkout reset");
    assert.equal(value.db.getEvents(1, 50, "slot_released").length, 0, "zero release mutation");
    const after = value.db.getSlot(1)!;
    assert.equal(after.occupied, before.occupied);
    assert.equal(after.assignment_epoch, before.assignment_epoch);
    assert.equal(after.active_turn_id, SESSION_ID, "the induced turn is left exactly as it was");
    const audits = deliveryAudits(value.db);
    assert.equal(audits.length, 1);
    assert.equal((JSON.parse(audits[0].payload) as Record<string, unknown>).settle_outcome, "timeout");
  } finally {
    value.close();
  }
});

test("NEGATIVE: a repeat request while the induced turn is still active delivers NOTHING", async () => {
  const value = harness({
    selfTurnSettleTimeoutMs: 3_000,
    onDeliver: (db) => db.updateSlot(1, { last_meaningful_work_at: new Date(Date.now() - 10 * 60 * 1000).toISOString() }),
  });
  try {
    const request = releaseRequest(value.db);
    const first = await value.coordinator.release(request);
    assert.equal(first.code, "slot_not_idle");
    assert.equal(value.deliveries.length, 1);

    const second = await value.coordinator.release(request);
    assert.equal(second.success, false);
    assert.equal(second.code, "slot_not_idle");
    assert.equal(value.deliveries.length, 1, "the retry must not inject a second reset instruction");
    assert.equal(deliveryAudits(value.db).length, 1);
  } finally {
    value.close();
  }
});

test("NEGATIVE: an unrelated pre-existing active turn refuses before any delivery", async () => {
  const value = harness();
  try {
    value.db.startAgentTurn(1, "some-other-session");
    const result = await value.coordinator.release(releaseRequest(value.db));
    assert.equal(result.success, false);
    assert.equal(result.code, "slot_not_idle");
    assert.equal(result.cause, "active_turn");
    assert.equal(value.deliveries.length, 0, "no instruction, no delivery, no audit");
    assert.equal(deliveryAudits(value.db).length, 0);
    assert.equal(value.db.getSlot(1)!.occupied, true);
  } finally {
    value.close();
  }
});

test("NEGATIVE: a replacement turn during the wait refuses and names the different turn", async () => {
  const value = harness({
    selfTurnSettleTimeoutMs: 20_000,
    onDeliver: (db) => db.updateSlot(1, { last_meaningful_work_at: new Date(Date.now() - 10 * 60 * 1000).toISOString() }),
    onSleep: (db, tick) => {
      if (tick === 1) {
        // The induced turn closes and a REPLACEMENT turn takes the slot in the
        // same settling tick, so the next poll must see the different turn id.
        db.finishAgentTurn(1, SESSION_ID);
        db.startAgentTurn(1, "replacement-session");
        db.updateSlot(1, { last_meaningful_work_at: new Date(Date.now() - 10 * 60 * 1000).toISOString() });
      }
    },
  });
  try {
    const request = releaseRequest(value.db);
    const result = await value.coordinator.release(request);
    assert.equal(result.success, false);
    assert.equal(result.code, "slot_not_idle");
    assert.equal(result.cause, "active_turn");
    assert.match(result.message, /different turn/);
    assert.match(result.message, /replacement-session/);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, request.expected_epoch, "no release mutation");
    assert.equal(value.deliveries.length, 1);
    const payload = JSON.parse(deliveryAudits(value.db)[0].payload) as Record<string, unknown>;
    assert.equal(payload.settle_outcome, "replacement_turn");
  } finally {
    value.close();
  }
});

test("NEGATIVE: epoch drift during the wait refuses without release mutation", async () => {
  const value = harness({
    selfTurnSettleTimeoutMs: 20_000,
    onDeliver: (db) => db.updateSlot(1, { last_meaningful_work_at: new Date(Date.now() - 10 * 60 * 1000).toISOString() }),
    onSleep: (db, tick) => {
      if (tick === 1) {
        // The induced turn closes and a replacement owner rebinds the slot in
        // the same settling tick (canonical rebind: bumps the epoch).
        db.finishAgentTurn(1, SESSION_ID);
        const current = db.getSlot(1)!;
        const tuple = slotAssignmentTuple(current)!;
        assert.equal(db.rebindSlot(1, current.assignment_epoch, tuple, {
          repository_id: tuple.repository_id, issue: tuple.issue, pr: tuple.pr,
          branch: "fix-8018-replacement", head_sha: "c".repeat(40),
          work_kind: tuple.work_kind, handoff_id: "handoff-8018-replacement",
          claimed_at: tuple.claimed_at,
        }, "replacement owner during release settle").ok, true);
      }
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
      expected_task: current.task!,       // byte-identical to the stored value (trailing newline)
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

test("no-pane release still refuses genuinely different task text", async () => {
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
      expected_task: "issue 8018 SOMETHING ELSE",
      checkout_path: CHECKOUT,
      effect_id: "no-pane-release:slot-1:epoch-0-mismatch",
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
    assert.equal(result.success, false);
    assert.equal(result.code, "task_mismatch");
    assert.equal(value.db.getSlot(1)!.occupied, true);
    assert.equal(value.db.getEvents(1, 50, "slot_released_no_pane").length, 0);
  } finally {
    value.close();
  }
});

/**
 * Keep turn lifecycle assertions focused on registration and settlement;
 * recent-work timestamps do not add a separate release wait.
 */
function markQuiet(db: MoPDatabase): void {
  db.updateSlot(1, { last_meaningful_work_at: new Date(Date.now() - 10 * 60 * 1000).toISOString() });
}

test("GREEN: the settle wait holds through delivery-registration, binds the induced turn that registers AFTER the first idle sample, then releases exactly once", async () => {
  // The live race: the pane's UserPromptSubmit lands a moment after delivery, so
  // the first post-delivery sample still reads an idle row with no induced turn.
  // The wait must not accept that sample as settlement.
  const value = harness({
    autoRegisterOnDelivery: false,
    onDeliver: markQuiet,
    onSleep: (db, tick) => {
      if (tick === 1) { db.startAgentTurn(1, SESSION_ID); markQuiet(db); } // the late UserPromptSubmit
      if (tick === 2) db.finishAgentTurn(1, SESSION_ID); // the Stop hook
    },
  });
  try {
    const request = releaseRequest(value.db);
    const result = await value.coordinator.release(request);

    assert.equal(result.success, true, `a late-registering induced turn must not become a refusal: ${result.code} / ${result.message}`);
    assert.equal(result.code, "released");
    assert.equal(value.deliveries.length, 1, "exactly ONE reset instruction");
    assert.equal(value.db.getSlot(1)!.occupied, false);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, request.expected_epoch + 1);

    const audits = deliveryAudits(value.db);
    assert.equal(audits.length, 1);
    const payload = JSON.parse(audits[0].payload) as Record<string, unknown>;
    assert.equal(payload.induced_turn_id, SESSION_ID, "the late-observed induced turn id is bound and audited");
    assert.equal(payload.settle_outcome, "settled");
    assert.ok(
      (payload.settle_ms as number) >= 2 * 1_000,
      `the wait must span the two-poll registration grace (settle_ms=${String(payload.settle_ms)})`,
    );
  } finally {
    value.close();
  }
});

test("RED/GREEN: registration landing just after the settle sample is still waited out, so the release never refuses on its own prompt", async () => {
  // On the shipped bytes the wait returns at settle_ms=0 / induced_turn_id=null;
  // the prompt then registers and the authoritative post-delivery re-check
  // refuses the release on a turn the release itself created.
  let registered = false;
  const value = harness({
    autoRegisterOnDelivery: false,
    onDeliver: markQuiet,
    onSleep: (db, tick) => {
      if (tick === 1 && !registered) { db.startAgentTurn(1, SESSION_ID); registered = true; markQuiet(db); }
      if (tick === 2) db.finishAgentTurn(1, SESSION_ID);
    },
    onIdleProbe: (db) => {
      if (!registered) { db.startAgentTurn(1, SESSION_ID); registered = true; }
    },
  });
  try {
    const request = releaseRequest(value.db);
    const result = await value.coordinator.release(request);

    assert.equal(result.success, true, `the release must wait out the registration it induced: ${result.code} / ${result.message}`);
    assert.equal(result.code, "released");
    assert.equal(value.deliveries.length, 1);
    const payload = JSON.parse(deliveryAudits(value.db)[0].payload) as Record<string, unknown>;
    assert.equal(payload.induced_turn_id, SESSION_ID);
    assert.ok((payload.settle_ms as number) >= 2 * 1_000);
  } finally {
    value.close();
  }
});

test("GREEN: when no induced turn ever registers, the wait exits on the bounded two-poll grace and releases once", async () => {
  let sleeps = 0;
  const value = harness({
    autoRegisterOnDelivery: false, // the delivered prompt never reaches the pane
    onDeliver: markQuiet,
    onSleep: () => { sleeps += 1; },
  });
  try {
    const request = releaseRequest(value.db);
    const result = await value.coordinator.release(request);

    assert.equal(result.success, true, `expected a bounded-grace release: ${result.code} / ${result.message}`);
    assert.equal(result.code, "released");
    assert.equal(sleeps, 2, "bounded: exactly the two-poll grace, never an indefinite wait");
    const payload = JSON.parse(deliveryAudits(value.db)[0].payload) as Record<string, unknown>;
    assert.equal(payload.induced_turn_id, null, "no induced turn was ever observed");
    assert.equal(payload.settle_ms, 2 * 1_000, "the grace is exactly two complete poll intervals");
    assert.equal(value.deliveries.length, 1);
  } finally {
    value.close();
  }
});

test("a finished idle slot with recent work is releasable immediately with no pane instruction", async () => {
  const value = harness({
    readOnly: () => ({ checkout_path: CHECKOUT, clean: true, unpushed_commits: [], branch: "main", head: MAIN_HEAD }),
  });
  try {
    value.db.updateSlot(1, {
      idle: true,
      activity: "waiting_for_pm_direction",
      last_meaningful_work_at: new Date().toISOString(),
    });
    const before = value.db.getSlot(1)!;
    const result = await value.coordinator.release(releaseRequest(value.db));
    assert.equal(result.code, "released");
    assert.equal(result.success, true);
    assert.deepEqual(value.deliveries, [], "a settled checkout needs no pane instruction");
    assert.equal(value.resets, 0, "a settled checkout needs no server-side reset either");
    const after = value.db.getSlot(1)!;
    assert.equal(after.occupied, false);
    assert.equal(after.assignment_epoch, before.assignment_epoch + 1);
  } finally {
    value.close();
  }
});

test("an on-main checkout with residue fails attestation without pane delivery", async () => {
  const value = harness({
    // Live S6 shape: branch main at the intended head, one untracked artifact,
    // nothing for the actionable literal to do.
    readOnly: () => ({
      checkout_path: CHECKOUT, clean: false, unpushed_commits: [], branch: "main", head: MAIN_HEAD,
    }),
    reset: () => ({
      checkout_path: CHECKOUT, branch: "main", head: MAIN_HEAD, clean: false, reset_succeeded: true, error: null,
    }),
    onDeliver: () => {
      throw new Error("an on-main checkout must not take a pane instruction");
    },
  });
  try {
    value.db.updateSlot(1, { idle: true, activity: "waiting_for_pm_direction", last_meaningful_work_at: null });
    const before = value.db.getSlot(1)!;
    const result = await value.coordinator.release(releaseRequest(value.db));
    assert.equal(result.code, "dirty_checkout");
    assert.equal(result.success, false);
    assert.deepEqual(value.deliveries, []);
    assert.equal(value.resets, 1, "the reset/attestation path still runs and still refuses");
    const after = value.db.getSlot(1)!;
    assert.equal(after.occupied, true, "a typed refusal leaves the slot occupied");
    assert.equal(after.assignment_epoch, before.assignment_epoch, "a refusal never advances the epoch");
  } finally {
    value.close();
  }
});

test("SOURCE FENCE: the only slot-facing release text is the actionable literal", () => {
  const root = new URL("../", import.meta.url).pathname;
  const src = readFileSync(join(root, "src", "slotRelease.ts"), "utf8");
  const server = readFileSync(join(root, "src", "server.ts"), "utf8");
  const both = src + server;
  assert.match(src, /RELEASE_ACTIONABLE_INSTRUCTION = "Switch to main and pull the latest origin\/main\."/);
  assert.equal(/Stop work on the current assignment/.test(both), false, "the stop-work prose is gone");
  assert.equal(/remain idle/i.test(both), false, "no remain-idle prose anywhere");
  assert.equal(/do not run another tool/i.test(both), false, "no do-not-run-another-tool prose anywhere");
});
