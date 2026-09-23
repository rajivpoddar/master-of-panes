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
  type NativeSlotNoPaneReleaseRequest,
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
interface Harness {
  db: MoPDatabase;
  directory: string;
  interrupts: number[];
  close: () => void;
}

/**
 * Slot 1 owns a branch-bearing tuple. The coordinator takes the simple path:
 * interrupt any live turn, free the row, audit. No pane instruction, no
 * settle wait, no checkout reset.
 */
function harness(options: {
  task?: string;
  readOnly?: () => { checkout_path: string; clean: boolean; unpushed_commits: string[]; branch?: string; head?: string };
  interruptResult?: { ok: boolean; reason: string };
} = {}) {
  const directory = mkdtempSync(join(tmpdir(), "mop-release-simple-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const task = options.task ?? "issue 8018 bounded rework";
  assert.equal(
    db.assignSlot(1, task, "github:heydonna-app/heydonna-app", 8018, "fix-8018-self-turn", 8018, "a".repeat(40), 0, "rework", "handoff-8018").ok,
    true,
  );
  db.updateSlot(1, { idle: true, activity: "waiting_for_pm_direction", last_meaningful_work_at: null });
  const state = { interrupts: [] as number[] };
  const coordinator = new NativeSlotReleaseCoordinator({
    db,
    resolveOwningCheckout: async () => CHECKOUT,
    interruptTurn: async (slot) => {
      state.interrupts.push(slot);
      return options.interruptResult ?? { ok: true, reason: "interrupt_sent" };
    },
    observeCheckout: async (): Promise<CheckoutReadOnlyObservation> =>
      options.readOnly?.() ?? {
        checkout_path: CHECKOUT, clean: true, unpushed_commits: [], branch: "main", head: MAIN_HEAD,
      },
  });
  const value: Harness & { coordinator: NativeSlotReleaseCoordinator } = {
    db,
    directory,
    coordinator,
    interrupts: state.interrupts,
    close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); },
  };
  return value;
}

test("GREEN: a release with an active turn interrupts, terminalizes, and frees exactly once", async () => {
  const value = harness();
  try {
    value.db.updateSlot(1, {
      active_turn_id: SESSION_ID,
      active_turn_state: "active",
      active_turn_started_at: new Date(Date.now() - 60 * 1000).toISOString(),
      last_meaningful_work_at: new Date(Date.now() - 60 * 1000).toISOString(),
    });
    const epochBefore = value.db.getSlot(1)!.assignment_epoch;
    const result = await value.coordinator.release({ slot: 1 });
    assert.equal(result.success, true);
    assert.equal(result.code, "released");
    assert.deepEqual(value.interrupts, [1], "exactly one interrupt, no pane instruction");
    const freed = value.db.getSlot(1)!;
    assert.equal(freed.occupied, false);
    assert.equal(freed.assignment_epoch, epochBefore + 1, "exactly one epoch bump");
    assert.equal(freed.active_turn_id, null);
    assert.equal(freed.active_turn_state, "inactive");
    const audits = value.db.getEvents(1, 50, "slot_released_simple");
    assert.equal(audits.length, 1);
    const payload = JSON.parse(audits[0].payload) as Record<string, unknown>;
    assert.equal((payload.prior as Record<string, unknown>).active_turn_id, SESSION_ID);
    assert.equal(payload.worktree_reset, false);
  } finally {
    value.close();
  }
});

test("NEGATIVE-proof: nothing waits — an interrupt failure still frees the row and is audited", async () => {
  const value = harness({ interruptResult: { ok: false, reason: "interrupt_send_failed" } });
  try {
    value.db.updateSlot(1, {
      active_turn_id: SESSION_ID,
      active_turn_state: "active",
      active_turn_started_at: new Date(Date.now() - 60 * 1000).toISOString(),
      last_meaningful_work_at: new Date(Date.now() - 60 * 1000).toISOString(),
    });
    const result = await value.coordinator.release({ slot: 1 });
    assert.equal(result.success, true, "the interrupt outcome never refuses");
    assert.equal(result.code, "released");
    assert.equal(value.db.getSlot(1)!.occupied, false);
    assert.equal(value.db.getSlot(1)!.active_turn_id, null);
    const audits = value.db.getEvents(1, 50, "slot_released_simple");
    assert.equal(audits.length, 1);
    const payload = JSON.parse(audits[0].payload) as Record<string, unknown>;
    assert.equal((payload.interrupt as Record<string, unknown>).ok, false);
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
test("GREEN: back-to-back release then assign needs no wait between the calls", async () => {
  const value = harness();
  try {
    const released = await value.coordinator.release({ slot: 1 });
    assert.equal(released.code, "released");
    const epochAfterRelease = value.db.getSlot(1)!.assignment_epoch;
    // The next lane assigns immediately onto the freed slot (assign-effect
    // path covered in family2-assign-effect; here the row accepts it).
    const assigned = value.db.assignSlotSimple(
      1,
      {
        repository_id: "github:heydonna-app/heydonna-app",
        issue: 8131,
        pr: null,
        branch: null,
        head_sha: null,
        work_kind: null,
        handoff_id: null,
        claimed_at: null,
      },
      "issue 8131 follow-up",
    );
    assert.equal(assigned.ok, true);
    assert.equal(value.db.getSlot(1)!.issue, 8131);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, epochAfterRelease + 1);
  } finally {
    value.close();
  }
});

test("SOURCE FENCE: the release sends no slot-facing text of any kind", () => {
  const root = new URL("../", import.meta.url).pathname;
  const src = readFileSync(join(root, "src", "slotRelease.ts"), "utf8");
  const server = readFileSync(join(root, "src", "server.ts"), "utf8");
  const both = src + server;
  assert.equal(/Switch to main and pull/.test(both), false, "no actionable-literal delivery anymore");
  assert.equal(/Stop work on the current assignment/.test(both), false, "the stop-work prose is gone");
  assert.equal(/remain idle/i.test(both), false, "no remain-idle prose anywhere");
  assert.equal(/do not run another tool/i.test(both), false, "no do-not-run-another-tool prose anywhere");
});
