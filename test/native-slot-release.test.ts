import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  MoPDatabase,
  slotAssignmentTuple,
  type AssignmentTupleInput,
} from "../src/db.js";
import {
  NativeSlotReleaseCoordinator,
  type NativeSlotNoPaneReleaseRequest,
  type CheckoutReadOnlyObservation,
  type CheckoutResetObservation,
  type NativeSlotReleaseRequest,
} from "../src/slotRelease.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { computeFamily2ReleaseDigest } from "../src/db.js";
import { computeNoPaneReleaseDigest } from "../src/db.js";

const ASSIGNMENT_HEAD = "a".repeat(40);
const MAIN_HEAD = "b".repeat(40);
const CHECKOUT = "/tmp/mop-native-release-checkout";

interface Fixture {
  db: MoPDatabase;
  directory: string;
  request: NativeSlotReleaseRequest;
}

function fixture(): Fixture {
  const directory = mkdtempSync(join(tmpdir(), "mop-native-release-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  assert.equal(db.assignSlot(1, "issue 8100", "github:heydonna-app/heydonna-app", 8100, "fix/8100", 8101, ASSIGNMENT_HEAD, 0, "implementation", "handoff-8100", ).ok, true);
  const current = db.getSlot(1)!;
  const expectedTuple = slotAssignmentTuple(current)!;
  return {
    db,
    directory,
    request: {
      slot: 1,
      expected_epoch: current.assignment_epoch,
      expected_tuple: {
        repository_id: expectedTuple.repository_id,
        issue: expectedTuple.issue,
        pr: expectedTuple.pr,
        branch: expectedTuple.branch,
        head_sha: expectedTuple.head_sha,
        work_kind: expectedTuple.work_kind,
        handoff_id: expectedTuple.handoff_id,
        claimed_at: expectedTuple.claimed_at,
      },
      intended_main_head: MAIN_HEAD,
    },
  };
}

function legacyIssueOnlyFixture(): Fixture {
  const directory = mkdtempSync(join(tmpdir(), "mop-native-release-legacy-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  assert.equal(db.assignSlot(4, "legacy issue-only task", "github:heydonna-app/heydonna-app", 7554, null, null, null, 0).ok, true);
  db.updateSlot(4, { idle: true, activity: "waiting_for_pm_direction" });
  const current = db.getSlot(4)!;
  const expectedTuple = slotAssignmentTuple(current)!;
  return {
    db,
    directory,
    request: {
      slot: 4,
      expected_epoch: current.assignment_epoch,
      expected_tuple: {
        repository_id: expectedTuple.repository_id,
        issue: expectedTuple.issue,
        pr: expectedTuple.pr,
        branch: expectedTuple.branch,
        head_sha: expectedTuple.head_sha,
        work_kind: expectedTuple.work_kind,
        handoff_id: expectedTuple.handoff_id,
        claimed_at: expectedTuple.claimed_at,
      },
      intended_main_head: MAIN_HEAD,
    },
  };
}

function closeFixture(value: Fixture): void {
  value.db.close();
  rmSync(value.directory, { recursive: true, force: true });
}

function exactObservation(
  overrides: Partial<CheckoutResetObservation> = {},
): CheckoutResetObservation {
  return {
    checkout_path: CHECKOUT,
    branch: "main",
    head: MAIN_HEAD,
    clean: true,
    reset_succeeded: true,
    error: null,
    ...overrides,
  };
}

interface AdapterOptions {
  checkout?: string | null;
  resolveCheckout?: () => Promise<string | null>;
  delivered?: boolean;
  idle?: boolean;
  observe?: () => Promise<CheckoutResetObservation>;
  observeReadOnly?: () => Promise<CheckoutReadOnlyObservation>;
  instruction?: (value: string) => void;
  interrupt?: () => void;
  interruptResult?: { ok: boolean; reason: string };
}

function coordinator(value: Fixture, options: AdapterOptions = {}): NativeSlotReleaseCoordinator {
  return new NativeSlotReleaseCoordinator({
    db: value.db,
    resolveOwningCheckout: options.resolveCheckout
      ?? (async () => options.checkout === undefined ? CHECKOUT : options.checkout),
    interruptTurn: async () => {
      options.interrupt?.();
      return options.interruptResult ?? { ok: true, reason: "interrupt_sent" };
    },
    observeCheckout: options.observeReadOnly ?? (async (): Promise<CheckoutReadOnlyObservation> => ({
      checkout_path: CHECKOUT,
      clean: true,
      unpushed_commits: [],
    })),
  });
}

function noPaneRequest(value: Fixture): NativeSlotNoPaneReleaseRequest {
  const expectedTuple = value.request.expected_tuple;
  const request = {
    slot: value.request.slot,
    expected_epoch: value.request.expected_epoch,
    expected_tuple: expectedTuple,
    expected_task: "issue 8100",
    checkout_path: CHECKOUT,
    effect_id: "no-pane-release-test-effect",
    request_digest: "",
  } satisfies NativeSlotNoPaneReleaseRequest;
  request.request_digest = computeNoPaneReleaseDigest({
    effect_id: request.effect_id,
    expected_epoch: request.expected_epoch,
    expected_tuple: request.expected_tuple,
    expected_task: request.expected_task,
    checkout_path: request.checkout_path,
  });
  return request;
}

function quiescentRequest(value: Fixture): NativeSlotReleaseRequest {
  const effect_id = "quiescent-legacy-release-7554";
  const request = {
    ...value.request,
    effect_id,
    request_digest: "",
  };
  request.request_digest = computeFamily2ReleaseDigest(request);
  return request;
}

test("release with only a slot number frees the row, bumps epoch once, clears the tuple", async () => {
  const value = fixture();
  try {
    const epochBefore = value.db.getSlot(1)!.assignment_epoch;
    const result = await coordinator(value).release({ slot: 1 });
    assert.equal(result.success, true);
    assert.equal(result.code, "released");
    assert.equal(result.idempotent, false);
    const freed = value.db.getSlot(1)!;
    assert.equal(freed.occupied, false);
    assert.equal(freed.assignment_epoch, epochBefore + 1);
    assert.equal(slotAssignmentTuple(freed), null);
    assert.equal(freed.task, null);
    assert.equal(freed.active_turn_id, null);
    assert.equal(freed.active_turn_state, "inactive");
  } finally {
    closeFixture(value);
  }
});

test("release with an active turn interrupts, terminalizes, and audits the prior", async () => {
  const value = fixture();
  try {
    const turnId = "98e27143-live-turn";
    value.db.updateSlot(1, {
      active_turn_id: turnId,
      active_turn_state: "active",
      active_turn_started_at: new Date(Date.now() - 60 * 1000).toISOString(),
      last_meaningful_work_at: new Date(Date.now() - 60 * 1000).toISOString(),
    });
    const epochBefore = value.db.getSlot(1)!.assignment_epoch;
    let interrupts = 0;
    const release = coordinator(value, {
      interrupt: () => { interrupts += 1; },
    });
    const result = await release.release({ slot: 1 });
    assert.equal(result.success, true);
    assert.equal(result.code, "released");
    assert.equal(interrupts, 1, "the live turn is interrupted exactly once");
    const freed = value.db.getSlot(1)!;
    assert.equal(freed.occupied, false);
    assert.equal(freed.assignment_epoch, epochBefore + 1);
    assert.equal(freed.active_turn_id, null, "the turn id is terminalized in the same write");
    assert.equal(freed.active_turn_state, "inactive");
    const events = value.db.getEvents(1, 10).filter((event) => event.event_type === "slot_released_simple");
    assert.equal(events.length, 1, "one audit row");
    const payload = JSON.parse(events[0].payload) as Record<string, unknown>;
    assert.equal((payload.prior as Record<string, unknown>).active_turn_id, turnId);
    assert.equal((payload.prior as Record<string, unknown>).issue, 8100);
    assert.equal(payload.worktree_reset, false, "the worktree is never touched");
  } finally {
    closeFixture(value);
  }
});

test("DND and busy rows do not block an explicit release", async () => {
  for (const name of ["dnd", "busy row"] as const) {
    const value = fixture();
    try {
      value.db.updateSlot(1, name === "dnd" ? { dnd: true } : { idle: false, activity: "working" });
      const epochBefore = value.db.getSlot(1)!.assignment_epoch;
      const result = await coordinator(value).release({ slot: 1 });
      assert.equal(result.code, "released", name);
      assert.equal(result.success, true, name);
      assert.equal(value.db.getSlot(1)!.occupied, false, name);
      assert.equal(value.db.getSlot(1)!.assignment_epoch, epochBefore + 1, name);
    } finally {
      closeFixture(value);
    }
  }
});

test("release of a free slot is an idempotent no-op with no epoch bump", async () => {
  const value = fixture();
  try {
    assert.equal((await coordinator(value).release({ slot: 1 })).code, "released");
    const epochAfterFirst = value.db.getSlot(1)!.assignment_epoch;
    const retry = await coordinator(value).release({ slot: 1 });
    assert.equal(retry.success, true);
    assert.equal(retry.code, "released");
    assert.equal(retry.idempotent, true);
    assert.equal(value.db.getSlot(1)!.assignment_epoch, epochAfterFirst, "no second epoch bump");
  } finally {
    closeFixture(value);
  }
});

test("a stale presented identity still releases on the live row", async () => {
  const value = fixture();
  try {
    const result = await coordinator(value).release({
      slot: 1,
      expected_epoch: value.request.expected_epoch + 7,
      expected_tuple: { ...value.request.expected_tuple, issue: 999999 },
      intended_main_head: "c".repeat(40),
    });
    assert.equal(result.code, "released");
    assert.equal(result.success, true);
    assert.equal(result.superseded?.epoch_drift, true);
    assert.equal(result.superseded?.tuple_drift, true);
    assert.equal(value.db.getSlot(1)!.occupied, false);
  } finally {
    closeFixture(value);
  }
});

test("a dirty checkout releases without reset, instruction, or refusal; the audit observes it", async () => {
  const value = fixture();
  try {
    const result = await coordinator(value, {
      observeReadOnly: async () => ({ checkout_path: CHECKOUT, clean: false, unpushed_commits: ["deadbeef"], branch: "fix/8100" }),
    }).release({ slot: 1 });
    assert.equal(result.code, "released");
    assert.equal(result.success, true);
    assert.equal(value.db.getSlot(1)!.occupied, false);
    const events = value.db.getEvents(1, 10).filter((event) => event.event_type === "slot_released_simple");
    assert.equal(events.length, 1);
    const payload = JSON.parse(events[0].payload) as Record<string, unknown>;
    assert.equal((payload.worktree as Record<string, unknown>).clean, false, "dirty state observed, not reset");
    assert.equal(payload.worktree_reset, false);
  } finally {
    closeFixture(value);
  }
});

test("an invalid slot number is invalid_request with no mutation", async () => {
  const value = fixture();
  try {
    const before = value.db.getSlot(1)!;
    const result = await coordinator(value).release({ slot: 99 } as unknown as NativeSlotReleaseRequest);
    assert.equal(result.code, "invalid_request");
    assert.equal(result.success, false);
    assert.deepEqual(value.db.getSlot(1), before);
  } finally {
    closeFixture(value);
  }
});

test("REVISE-2: slot-only release unwinds the live predecessor lane labels", async () => {
  const value = fixture();
  try {
    const released: Array<{ issue: number; slot: number; repository: string | null }> = [];
    const release = new NativeSlotReleaseCoordinator({
      db: value.db,
      resolveOwningCheckout: async () => CHECKOUT,
      interruptTurn: async () => ({ ok: true, reason: "no_live_turn" }),
      observeCheckout: async () => ({ checkout_path: CHECKOUT, clean: true, unpushed_commits: [] }),
      issueProjection: {
        onAssigned: async () => { throw new Error("assign must not project on release"); },
        onReleased: async (issue, slot, repository) => {
          released.push({ issue, slot, repository: repository ?? null });
          return {
            status: "projected", reason: null, repository: repository ?? null,
            issue, slot, added_labels: ["status:todo"], removed_labels: ["status:in-progress"],
            verified: true,
          };
        },
      },
    });
    // No presented tuple at all: the freed lane's labels still unwind from
    // the live predecessor row.
    const result = await release.release({ slot: 1 });
    assert.equal(result.code, "released");
    assert.equal(result.success, true);
    assert.deepEqual(released, [{ issue: 8100, slot: 1, repository: "github:heydonna-app/heydonna-app" }]);
    assert.equal(result.issue_projection?.status, "projected");
    assert.equal(result.issue_projection?.issue, 8100);
  } finally {
    closeFixture(value);
  }
});

test("releasing one slot does not mutate an unrelated occupied slot", async () => {
  const value = fixture();
  try {
    assert.equal(value.db.assignSlot(2, "other", "github:other/repo", 9000, "fix/9000", null, null, 0, ).ok, true);
    const slotTwo = value.db.getSlot(2)!;
    assert.equal((await coordinator(value).release(value.request)).code, "released");
    assert.deepEqual(value.db.getSlot(2), slotTwo);
  } finally {
    closeFixture(value);
  }
});

test("effect-bound release persists an atomic receipt and replays without a second clear", async () => {
  const value = fixture();
  try {
    const request = {
      ...value.request,
      effect_id: "family2-effect-8100",
      request_digest: computeFamily2ReleaseDigest({ effect_id: "family2-effect-8100", ...value.request }),
    };
    const release = coordinator(value);
    const first = await release.release(request);
    assert.equal(first.code, "released");
    assert.equal(first.idempotent, false);
    const stored = value.db.getNativeReleaseEffectReceipt(request.effect_id);
    assert.equal(stored?.released_epoch, request.expected_epoch + 1);
    const replay = await release.release(request);
    assert.equal(replay.code, "released");
    assert.equal(replay.idempotent, true);
    assert.equal(value.db.getSlot(1)?.occupied, false);
  } finally {
    closeFixture(value);
  }
});

test("no-pane release clears one exact stale lease, audits it, and replays from the receipt", async () => {
  const value = fixture();
  try {
    let resolveCalls = 0;
    let readOnlyCalls = 0;
    const request = noPaneRequest(value);
    const first = await coordinator(value, {
      resolveCheckout: async () => { resolveCalls += 1; return CHECKOUT; },
      observeReadOnly: async () => {
        readOnlyCalls += 1;
        return { checkout_path: CHECKOUT, clean: true, unpushed_commits: [] };
      },
    }).releaseWithoutPane(request);
    assert.equal(first.code, "released");
    assert.equal(first.success, true);
    assert.equal(first.idempotent, false);
    assert.equal(resolveCalls, 1);
    assert.equal(readOnlyCalls, 1);
    assert.equal(value.db.getSlot(1)?.occupied, false);
    assert.equal(value.db.getSlot(1)?.assignment_epoch, request.expected_epoch + 1);
    assert.equal(value.db.getEvents(1, 10).some((event) => event.event_type === "slot_released_no_pane"), true);

    const replay = await coordinator(value, {
      resolveCheckout: async () => { throw new Error("replay must not resolve checkout"); },
      observeReadOnly: async () => { throw new Error("replay must not inspect checkout"); },
    }).releaseWithoutPane(request);
    assert.equal(replay.code, "released");
    assert.equal(replay.success, true);
    assert.equal(replay.idempotent, true);
    assert.equal(value.db.getSlot(1)?.assignment_epoch, request.expected_epoch + 1);
  } finally {
    closeFixture(value);
  }
});

test("no-pane release refuses DND, active turns, dirty checkouts, and tuple/task drift before clear", async (t) => {
  const cases: Array<{ name: string; mutate: (db: MoPDatabase) => void; observe?: () => Promise<CheckoutReadOnlyObservation>; code: string }> = [
    { name: "DND", mutate: (db) => db.updateSlot(1, { dnd: true }), code: "dnd_active" },
    { name: "active hook turn", mutate: (db) => db.updateSlot(1, { active_turn_id: "turn", active_turn_state: "active" }), code: "active_turn" },
    { name: "productive activity", mutate: (db) => db.updateSlot(1, { activity: "working" }), code: "productive_work" },
    { name: "task drift", mutate: (db) => db.updateSlot(1, { task: "different task" }), code: "task_mismatch" },
    { name: "dirty checkout", mutate: () => {}, observe: async () => ({ checkout_path: CHECKOUT, clean: false, unpushed_commits: ["commit"] }), code: "checkout_not_clean" },
  ];
  for (const testCase of cases) {
    await t.test(testCase.name, async () => {
      const value = fixture();
      try {
        testCase.mutate(value.db);
        const before = value.db.getSlot(1)!;
        const result = await coordinator(value, { observeReadOnly: testCase.observe }).releaseWithoutPane(noPaneRequest(value));
        assert.equal(result.code, testCase.code);
        assert.deepEqual(value.db.getSlot(1), before);
        assert.equal(value.db.getNativeReleaseEffectReceipt("no-pane-release-test-effect"), null);
      } finally {
        closeFixture(value);
      }
    });
  }
});

test("no-pane release accepts only the explicit quiescent waiting activity", async () => {
  const value = fixture();
  try {
    value.db.updateSlot(1, { activity: "waiting_for_pm_direction" });
    const request = noPaneRequest(value);
    const result = await coordinator(value).releaseWithoutPane(request);
    assert.equal(result.code, "released");
    assert.equal(value.db.getSlot(1)?.occupied, false);
    assert.equal(value.db.getSlot(1)?.assignment_epoch, request.expected_epoch + 1);
  } finally {
    closeFixture(value);
  }
});

test("forged Family-2 digest refuses before delivery/reset/clear", async () => {
  const value = fixture();
  try {
    let deliveries = 0;
    const before = value.db.getSlot(1)!;
    const result = await coordinator(value, { instruction: () => { deliveries += 1; } }).release({
      ...value.request,
      effect_id: "family2-effect-forged",
      request_digest: "f".repeat(64),
    });
    assert.equal(result.code, "effect_digest_mismatch");
    assert.equal(deliveries, 0);
    assert.deepEqual(value.db.getSlot(1), before);
    assert.equal(value.db.getNativeReleaseEffectReceipt("family2-effect-forged"), null);
  } finally {
    closeFixture(value);
  }
});

test("native adapter derives the checkout from the numbered pane, not caller input", async () => {
  const commands: string[] = [];
  let gitCalls = 0;
  const relay = new TmuxRelay(DEFAULT_CONFIG, {
    runShell: async (command) => {
      commands.push(command);
      if (command.startsWith("tmux display-message")) {
        return { stdout: "%11|/Users/rajiv/Downloads/projects/heydonna-app-3001\n", stderr: "" };
      }
      if (command.startsWith("git -C")) {
        gitCalls += 1;
        return {
          stdout: `${gitCalls === 1 ? "/Users/rajiv/Downloads/projects/heydonna-app-3001" : CHECKOUT}\n`,
          stderr: "",
        };
      }
      throw new Error(`unexpected command: ${command}`);
    },
  });
  assert.equal(await relay.getSlotCheckoutPath(1), CHECKOUT);
  assert.match(commands[0], /tmux display-message -t 0:0\.1/);
  assert.match(commands[1], /git -C .* rev-parse --show-toplevel/);
  assert.equal(await relay.getSlotCheckoutPath(0), null);
});

test("local reset helper switches the derived checkout to exact clean main", () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-reset-helper-"));
  const origin = join(directory, "origin.git");
  const checkout = join(directory, "checkout");
  try {
    execFileSync("git", ["init", "--bare", "-q", origin]);
    execFileSync("git", ["clone", "-q", origin, checkout]);
    execFileSync("git", ["-C", checkout, "config", "user.email", "test@example.com"]);
    execFileSync("git", ["-C", checkout, "config", "user.name", "MoP Test"]);
    execFileSync("git", ["-C", checkout, "switch", "-q", "-c", "main"]);
    writeFileSync(join(checkout, "proof"), "main\n");
    execFileSync("git", ["-C", checkout, "add", "proof"]);
    execFileSync("git", ["-C", checkout, "commit", "-q", "-m", "main"]);
    execFileSync("git", ["-C", checkout, "push", "-q", "-u", "origin", "main"]);
    const mainHead = execFileSync("git", ["-C", checkout, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
    execFileSync("git", ["-C", checkout, "switch", "-q", "-c", "work"]);

    const output = execFileSync("python3", [
      join(process.cwd(), "scripts/release-slot-reset-and-ack.py"),
      "--checkout", checkout,
      "--intended-main-head", mainHead,
    ], { encoding: "utf8" });
    const observation = JSON.parse(output) as CheckoutResetObservation;
    assert.equal(observation.checkout_path, checkout);
    assert.equal(observation.branch, "main");
    assert.equal(observation.head, mainHead);
    assert.equal(observation.clean, true);
    assert.equal(observation.reset_succeeded, true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
