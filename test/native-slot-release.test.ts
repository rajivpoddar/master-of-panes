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
  type CheckoutResetObservation,
  type NativeSlotReleaseRequest,
} from "../src/slotRelease.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { computeFamily2ReleaseDigest } from "../src/db.js";

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
  instruction?: (value: string) => void;
}

function coordinator(value: Fixture, options: AdapterOptions = {}): NativeSlotReleaseCoordinator {
  return new NativeSlotReleaseCoordinator({
    db: value.db,
    resolveOwningCheckout: options.resolveCheckout
      ?? (async () => options.checkout === undefined ? CHECKOUT : options.checkout),
    deliverInstruction: async (_slot, instruction) => {
      options.instruction?.(instruction);
      return options.delivered !== false;
    },
    owningSlotIsIdle: async () => options.idle !== false,
    resetAndObserveCheckout: options.observe ?? (async () => exactObservation()),
  });
}

test("MoP-derived checkout reset acknowledgement clears once and replay is safe typed drift", async () => {
  const value = fixture();
  try {
    let instruction = "";
    const release = coordinator(value, { instruction: (text) => { instruction = text; } });
    const first = await release.release(value.request);
    assert.equal(first.code, "released");
    assert.equal(first.success, true);
    assert.equal(first.acknowledgement?.checkout_path, CHECKOUT);
    assert.deepEqual(first.acknowledgement?.expected_tuple, value.request.expected_tuple);
    assert.match(instruction, /Stop work on the current assignment now/);
    assert.match(instruction, /switching your owning checkout .* to branch main, pulling origin\/main/);
    const free = value.db.getSlot(1)!;
    assert.equal(free.occupied, false);
    assert.equal(free.assignment_epoch, value.request.expected_epoch + 1);
    assert.equal(slotAssignmentTuple(free), null);

    const replay = await release.release(value.request);
    assert.equal(replay.code, "slot_already_free_unverifiable");
    assert.equal(replay.success, false);
    assert.equal(value.db.getSlot(1)?.assignment_epoch, value.request.expected_epoch + 1);
  } finally {
    closeFixture(value);
  }
});

test("numbered slots expose no compatibility or epoch-only clear surface", () => {
  const value = fixture();
  try {
    const compatibilityClearName = ["release", "Slot"].join("");
    const exactClearName = ["release", "Slot", "Exact"].join("");
    assert.equal(compatibilityClearName in value.db, false);
    assert.equal(exactClearName in value.db, false);
    assert.equal(value.db.getSlot(1)?.occupied, true);
  } finally {
    closeFixture(value);
  }
});

test("identity, delivery, idle, and reset acknowledgement failures preserve occupied", async (t) => {
  const cases: Array<{ name: string; code: string; options: AdapterOptions }> = [
    { name: "pane checkout unavailable", code: "checkout_identity_unavailable", options: { checkout: null } },
    { name: "delivery failed", code: "delivery_failed", options: { delivered: false } },
    { name: "slot stayed active", code: "slot_not_idle", options: { idle: false } },
    { name: "reset failed", code: "checkout_reset_failed", options: { observe: async () => exactObservation({ reset_succeeded: false }) } },
    { name: "dirty checkout", code: "dirty_checkout", options: { observe: async () => exactObservation({ clean: false }) } },
    { name: "wrong branch", code: "wrong_branch", options: { observe: async () => exactObservation({ branch: "fix/8100" }) } },
    { name: "wrong main head", code: "wrong_head", options: { observe: async () => exactObservation({ head: "c".repeat(40) }) } },
    { name: "wrong checkout", code: "ack_checkout_mismatch", options: { observe: async () => exactObservation({ checkout_path: "/tmp/other" }) } },
    {
      name: "pane changed checkout during reset",
      code: "ack_checkout_mismatch",
      options: {
        resolveCheckout: (() => {
          let read = 0;
          return async () => ++read === 1 ? CHECKOUT : "/tmp/other";
        })(),
      },
    },
  ];

  for (const testCase of cases) {
    await t.test(testCase.name, async () => {
      const value = fixture();
      try {
        const before = value.db.getSlot(1)!;
        const releaseResult = await coordinator(value, testCase.options).release(value.request);
        assert.equal(releaseResult.code, testCase.code);
        assert.deepEqual(value.db.getSlot(1), before);
      } finally {
        closeFixture(value);
      }
    });
  }
});

test("final CAS catches complete tuple identity change without epoch advance", async () => {
  const value = fixture();
  try {
    const release = coordinator(value, {
      observe: async () => {
        const drift = value.db.syncSlotCheckout(1, "fix/8100", "d".repeat(40), value.request.expected_epoch);
        assert.equal(drift.ok, true);
        assert.equal(drift.assignment_epoch, value.request.expected_epoch);
        return exactObservation();
      },
    });
    assert.equal((await release.release(value.request)).code, "clear_conflict");
    const occupied = value.db.getSlot(1)!;
    assert.equal(occupied.occupied, true);
    assert.equal(occupied.assignment_epoch, value.request.expected_epoch);
    assert.equal(occupied.head_sha, "d".repeat(40));
  } finally {
    closeFixture(value);
  }
});

test("final CAS catches epoch drift and preserves the replacement owner", async () => {
  const value = fixture();
  try {
    const desired: AssignmentTupleInput = {
      ...value.request.expected_tuple,
      branch: "fix/8100-successor",
      head_sha: "e".repeat(40),
    };
    const release = coordinator(value, {
      observe: async () => {
        assert.equal(value.db.rebindSlot(
          1,
          value.request.expected_epoch,
          value.request.expected_tuple,
          desired,
          "replacement owner",
        ).ok, true);
        return exactObservation();
      },
    });
    assert.equal((await release.release(value.request)).code, "clear_conflict");
    const replacement = value.db.getSlot(1)!;
    assert.equal(replacement.occupied, true);
    assert.equal(replacement.assignment_epoch, value.request.expected_epoch + 1);
    assert.equal(replacement.branch, "fix/8100-successor");
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
    let deliveries = 0;
    const release = coordinator(value, { instruction: () => { deliveries += 1; } });
    const first = await release.release(request);
    assert.equal(first.code, "released");
    assert.equal(first.idempotent, false);
    const stored = value.db.getNativeReleaseEffectReceipt(request.effect_id);
    assert.equal(stored?.released_epoch, request.expected_epoch + 1);
    const replay = await release.release(request);
    assert.equal(replay.code, "released");
    assert.equal(replay.idempotent, true);
    assert.equal(deliveries, 1);
    assert.equal(value.db.getSlot(1)?.occupied, false);
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
        return { stdout: "%11\t/Users/rajiv/Downloads/projects/heydonna-app-3001\n", stderr: "" };
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
