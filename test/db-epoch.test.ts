import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import Database from "better-sqlite3";

import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function withDatabase(run: (db: MoPDatabase, path: string) => void): void {
  const directory = mkdtempSync(join(tmpdir(), "mop-epoch-test-"));
  const path = join(directory, "mop.db");
  try {
    run(new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: path }), path);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("same-slot replay is exact, canonical, and mutation-free", () => {
  withDatabase((db) => {
    assert.equal(db.getSlot(1)?.assignment_epoch, 0);
    const first = db.assignSlot(1, "issue", "github:repo-1", 10, "fix/10", null, null, null, 0);
    assert.deepEqual(first, { ok: true, conflict: false, assignment_epoch: 1, idempotent: false });
    assert.equal(db.getSlot(1)?.branch, "fix/10");
    assert.equal(db.getSlot(1)?.branch_ref, "refs/heads/fix/10");

    const beforeReplay = db.getSlot(1);
    const redelivery = db.assignSlot(
      1,
      "renamed task",
      "github:repo-1",
      10,
      "refs/heads/fix/10",
      "different-session",
      null,
      null,
      1,
    );
    assert.deepEqual(redelivery, { ok: true, conflict: false, assignment_epoch: 1, idempotent: true });
    assert.deepEqual(db.getSlot(1), beforeReplay);

    const next = db.assignSlot(1, "next", "github:repo-1", 11, "fix/11", null, null, null, 1);
    assert.deepEqual(next, {
      ok: false,
      conflict: true,
      assignment_epoch: 1,
      idempotent: false,
      reason: "slot_already_occupied",
      owner_slots: [1],
    });
    assert.deepEqual(db.getSlot(1), beforeReplay);

    assert.equal(db.releaseSlot(1, 1).ok, true);
    const successor = db.assignSlot(
      1,
      "next",
      "github:repo-1",
      11,
      "fix/11",
      null,
      null,
      null,
      1,
    );
    assert.deepEqual(successor, {
      ok: true,
      conflict: false,
      assignment_epoch: 2,
      idempotent: false,
    });
  });
});

test("assignment metadata is stored and cleared with the occupied tuple", () => {
  withDatabase((db) => {
    const assigned = db.assignSlot(
      1,
      "metadata assignment",
      "github:repo-1",
      10,
      "fix/10",
      null,
      20,
      "a".repeat(40),
      0,
      false,
      null,
      "implementation",
      "handoff-epoch-1",
    );
    assert.deepEqual(assigned, {
      ok: true,
      conflict: false,
      assignment_epoch: 1,
      idempotent: false,
    });
    const occupied = db.getSlot(1);
    assert.equal(occupied?.work_kind, "implementation");
    assert.equal(occupied?.handoff_id, "handoff-epoch-1");
    assert.equal(typeof occupied?.claimed_at, "string");
    assert.equal(occupied?.claimed_at, occupied?.assigned_at);

    assert.deepEqual(db.releaseSlot(1, 1), {
      ok: true,
      conflict: false,
      assignment_epoch: 1,
      idempotent: false,
    });
    const free = db.getSlot(1);
    assert.equal(free?.work_kind, null);
    assert.equal(free?.handoff_id, null);
    assert.equal(free?.claimed_at, null);
  });
});

test("checkout synchronization updates head without changing ownership epoch or turn", () => {
  withDatabase((db) => {
    db.assignSlot(1, "issue", "github:repo-1", 10, "fix/10-exact", null, null, null, 0);
    db.startAgentTurn(1, "turn-a");

    const first = db.syncSlotCheckout(1, "fix/10-exact", "a".repeat(40), 1);
    assert.deepEqual(first, {
      ok: true,
      conflict: false,
      assignment_epoch: 1,
      idempotent: false,
    });
    assert.equal(db.getSlot(1)?.head_sha, "a".repeat(40));
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
    assert.equal(db.getSlot(1)?.active_turn_id, "turn-a");
    assert.equal(db.getSlot(1)?.active_turn_state, "active");

    const repeated = db.syncSlotCheckout(1, "fix/10-exact", "a".repeat(40), 1);
    assert.equal(repeated.idempotent, true);
    assert.equal(repeated.assignment_epoch, 1);
  });
});

test("checkout synchronization fails closed on stale epoch, wrong branch, or free slot", () => {
  withDatabase((db) => {
    db.assignSlot(1, "issue", "github:repo-1", 10, "fix/10-exact", null, null, null, 0);

    assert.equal(
      db.syncSlotCheckout(1, "fix/10-exact", "a".repeat(40), 0).reason,
      "epoch_mismatch",
    );
    assert.equal(
      db.syncSlotCheckout(1, "fix/10-wrong", "a".repeat(40), 1).reason,
      "branch_mismatch",
    );
    assert.equal(db.getSlot(1)?.head_sha, null);

    db.releaseSlot(1, 1);
    assert.equal(
      db.syncSlotCheckout(1, "fix/10-exact", "a".repeat(40), 1).reason,
      "slot_not_occupied",
    );
  });
});

test("missing and stale expected epochs fail without mutation", () => {
  withDatabase((db) => {
    const missing = db.assignSlot(1, "issue", "github:repo-1", 10, "fix/10", null);
    assert.equal(missing.reason, "expected_epoch_required");
    assert.equal(db.getSlot(1)?.occupied, false);

    db.assignSlot(1, "issue", "github:repo-1", 10, "fix/10", null, null, null, 0);
    const stale = db.releaseSlot(1, 0);
    assert.equal(stale.reason, "epoch_mismatch");
    assert.equal(db.getSlot(1)?.occupied, true);
  });
});

test("release preserves epoch and hook turn state fails closed on mismatch", () => {
  withDatabase((db) => {
    db.assignSlot(1, "issue", "github:repo-1", 10, "fix/10", null, null, null, 0);
    db.startAgentTurn(1, "turn-a");
    assert.equal(db.getSlot(1)?.active_turn_state, "active");
    db.finishAgentTurn(1, "turn-b");
    assert.equal(db.getSlot(1)?.active_turn_state, "indeterminate");
    assert.equal(db.getSlot(1)?.idle, false);
    db.finishAgentTurn(1, "turn-a");
    assert.equal(db.getSlot(1)?.active_turn_state, "inactive");

    const released = db.releaseSlot(1, 1);
    assert.equal(released.assignment_epoch, 1);
    assert.equal(db.getSlot(1)?.occupied, false);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
  });
});

test("issue claim adoption rebinds the occupied tuple atomically", () => {
  withDatabase((db) => {
    assert.equal(
      db.assignSlot(
        1,
        "placeholder",
        "github:repo-1",
        10,
        "fix/10-pending",
        "session-a",
        null,
        null,
        0,
      ).ok,
      true,
    );

    const adopted = db.adoptIssueClaimSlot(
      1,
      "PR #20",
      "github:repo-1",
      10,
      "fix/10-real",
      20,
      "a".repeat(40),
      null,
      "refs/heads/fix/10-pending",
      null,
      1,
    );
    assert.deepEqual(adopted, {
      ok: true,
      conflict: false,
      assignment_epoch: 1,
      idempotent: false,
    });
    assert.deepEqual(
      {
        occupied: db.getSlot(1)?.occupied,
        repository_id: db.getSlot(1)?.repository_id,
        issue: db.getSlot(1)?.issue,
        pr: db.getSlot(1)?.pr,
        branch: db.getSlot(1)?.branch,
        head_sha: db.getSlot(1)?.head_sha,
        session_id: db.getSlot(1)?.session_id,
        assignment_epoch: db.getSlot(1)?.assignment_epoch,
      },
      {
        occupied: true,
        repository_id: "github:repo-1",
        issue: 10,
        pr: 20,
        branch: "fix/10-real",
        head_sha: "a".repeat(40),
        session_id: "session-a",
        assignment_epoch: 1,
      },
    );
    assert.deepEqual(
      db.adoptIssueClaimSlot(
        1,
        "ignored replay",
        "github:repo-1",
        10,
        "refs/heads/fix/10-real",
        20,
        "a".repeat(40),
        20,
        "refs/heads/fix/10-real",
        "a".repeat(40),
        1,
      ),
      {
        ok: true,
        conflict: false,
        assignment_epoch: 1,
        idempotent: true,
      },
    );
  });
});

test("issue claim adoption refuses a different-PR rewrite on an already PR-bound tuple", () => {
  withDatabase((db) => {
    db.assignSlot(
      1,
      "placeholder",
      "github:repo-1",
      10,
      "fix/10-pending",
      "session-a",
      null,
      null,
      0,
    );
    // Legitimate issue-only -> PR identity bind first (pr=20, epoch preserved).
    const bound = db.adoptIssueClaimSlot(
      1,
      "PR #20",
      "github:repo-1",
      10,
      "fix/10-real",
      20,
      "a".repeat(40),
      null,
      "refs/heads/fix/10-pending",
      null,
      1,
    );
    assert.deepEqual(bound, {
      ok: true,
      conflict: false,
      assignment_epoch: 1,
      idempotent: false,
    });

    const before = db.getSlot(1);
    // Reviewer scenario (ordinal-2 block): the observed tuple matches
    // (expected_current_pr=P1=20, same branch/head/epoch) but the caller
    // supplies a NEW pr=P2=21 — the row must fail closed, never be rewritten.
    const rewrite = db.adoptIssueClaimSlot(
      1,
      "rewrite",
      "github:repo-1",
      10,
      "fix/10-real",
      21,
      "a".repeat(40),
      20,
      "refs/heads/fix/10-real",
      "a".repeat(40),
      1,
    );
    assert.deepEqual(rewrite, {
      ok: false,
      conflict: true,
      assignment_epoch: 1,
      idempotent: false,
      reason: "slot_already_occupied",
      owner_slots: [1],
    });
    assert.deepEqual(db.getSlot(1), before, "PR-bound tuple must not be rewritten");
    assert.equal(db.getSlot(1)?.pr, 20);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
  });
});

test("issue claim adoption is idempotent on an already PR-bound tuple (same PR)", () => {
  withDatabase((db) => {
    db.assignSlot(
      1,
      "placeholder",
      "github:repo-1",
      10,
      "fix/10-pending",
      "session-a",
      null,
      null,
      0,
    );
    assert.equal(
      db.adoptIssueClaimSlot(
        1,
        "PR #20",
        "github:repo-1",
        10,
        "fix/10-real",
        20,
        "a".repeat(40),
        null,
        "refs/heads/fix/10-pending",
        null,
        1,
      ).ok,
      true,
    );
    const before = db.getSlot(1);
    // Re-issued adoption claiming the now PR-bound observed tuple with the
    // SAME pr is an idempotent no-op: epoch unchanged, no mutation.
    const replay = db.adoptIssueClaimSlot(
      1,
      "ignored replay",
      "github:repo-1",
      10,
      "refs/heads/fix/10-real",
      20,
      "a".repeat(40),
      20,
      "refs/heads/fix/10-real",
      "a".repeat(40),
      1,
    );
    assert.deepEqual(replay, {
      ok: true,
      conflict: false,
      assignment_epoch: 1,
      idempotent: true,
    });
    assert.deepEqual(db.getSlot(1), before, "idempotent re-adoption must not mutate the slot");
  });
});

test("issue claim adoption fails closed on stale, active, or different claims", () => {
  withDatabase((db) => {
    assert.equal(
      db.adoptIssueClaimSlot(
        2,
        "free",
        "github:repo-1",
        10,
        "fix/10-real",
        20,
        "a".repeat(40),
        null,
        "refs/heads/fix/10-real",
        null,
        0,
      ).reason,
      "slot_not_occupied",
    );
    db.assignSlot(
      1,
      "placeholder",
      "github:repo-1",
      10,
      "fix/10-pending",
      null,
      null,
      null,
      0,
    );
    assert.equal(
      db.adoptIssueClaimSlot(
        1,
        "stale",
        "github:repo-1",
        10,
        "fix/10-real",
        20,
        "a".repeat(40),
        null,
        "refs/heads/fix/10-pending",
        null,
        0,
      ).reason,
      "epoch_mismatch",
    );

    // A wrong issue on a matching observed tuple is not an adoption: the
    // claim identity (issue) must agree, so the occupied slot stays put.
    assert.equal(
      db.adoptIssueClaimSlot(
        1,
        "wrong issue",
        "github:repo-1",
        11,
        "fix/11-real",
        21,
        "b".repeat(40),
        null,
        "refs/heads/fix/10-pending",
        null,
        1,
      ).reason,
      "slot_already_occupied",
    );
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
    assert.equal(db.getSlot(1)?.branch, "fix/10-pending");

    // An ACTIVE turn with an exactly matching observed tuple is the wedge
    // case: the identity bind proceeds while preserving the epoch and turn.
    db.startAgentTurn(1, "turn-a");
    assert.deepEqual(
      db.adoptIssueClaimSlot(
        1,
        "active bind",
        "github:repo-1",
        10,
        "fix/10-real",
        20,
        "a".repeat(40),
        null,
        "refs/heads/fix/10-pending",
        null,
        1,
      ),
      {
        ok: true,
        conflict: false,
        assignment_epoch: 1,
        idempotent: false,
      },
    );
    assert.equal(db.getSlot(1)?.pr, 20);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
    assert.equal(db.getSlot(1)?.active_turn_state, "active");
    assert.equal(db.getSlot(1)?.active_turn_id, "turn-a");

    // After the bind the observed tuple is pr-bound; an attempt that still
    // claims the old issue-only tuple is drift and fails closed.
    assert.equal(
      db.adoptIssueClaimSlot(
        1,
        "stale observation",
        "github:repo-1",
        10,
        "fix/10-real",
        20,
        "a".repeat(40),
        null,
        "refs/heads/fix/10-pending",
        null,
        1,
      ).reason,
      "observed_tuple_mismatch",
    );
    assert.equal(db.getSlot(1)?.pr, 20);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
  });
});

test("issue claim adoption rejects same-epoch checkout identity drift", () => {
  withDatabase((db) => {
    assert.equal(
      db.assignSlot(
        1,
        "placeholder",
        "github:repo-1",
        10,
        "fix/10-real",
        null,
        null,
        null,
        0,
      ).ok,
      true,
    );
    assert.equal(
      db.syncSlotCheckout(1, "fix/10-real", "b".repeat(40), 1).ok,
      true,
    );

    const result = db.adoptIssueClaimSlot(
      1,
      "stale observation",
      "github:repo-1",
      10,
      "fix/10-real",
      20,
      "a".repeat(40),
      null,
      "refs/heads/fix/10-real",
      null,
      1,
    );
    assert.equal(result.reason, "observed_tuple_mismatch");
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
    assert.equal(db.getSlot(1)?.pr, null);
    assert.equal(db.getSlot(1)?.head_sha, "b".repeat(40));
  });
});

test("active-turn issue claim adoption binds preserving epoch, turn, session, and dnd (#7208 wedge)", () => {
  withDatabase((db, path) => {
    // Issue-only claim already mid-work: pr=null but branch and recorded
    // checkout head are present, exactly the #7208 incident shape.
    assert.equal(
      db.assignSlot(
        4,
        "claim",
        "heydonna-app",
        7135,
        "fix/7135-proper-noun-letter-drop-corruption",
        "session-s4",
        null,
        "be79bc4817cda506798ec87f81543d1a808b8bd6",
        0,
      ).ok,
      true,
    );
    // Pin the live claim epoch (430 in the incident) and open an active turn.
    const raw = new Database(path);
    try {
      raw.prepare("UPDATE slots SET assignment_epoch = 430 WHERE slot = 4").run();
    } finally {
      raw.close();
    }
    db.startAgentTurn(4, "turn-4-active");
    assert.equal(db.getSlot(4)?.assignment_epoch, 430);
    assert.equal(db.getSlot(4)?.active_turn_state, "active");

    const adopted = db.adoptIssueClaimSlot(
      4,
      "rework PR #7208",
      "heydonna-app",
      7135,
      "refs/heads/fix/7135-proper-noun-letter-drop-corruption",
      7208,
      "be79bc4817cda506798ec87f81543d1a808b8bd6",
      null,
      "refs/heads/fix/7135-proper-noun-letter-drop-corruption",
      "be79bc4817cda506798ec87f81543d1a808b8bd6",
      430,
    );
    assert.deepEqual(adopted, {
      ok: true,
      conflict: false,
      assignment_epoch: 430,
      idempotent: false,
    });
    assert.deepEqual(
      {
        occupied: db.getSlot(4)?.occupied,
        repository_id: db.getSlot(4)?.repository_id,
        issue: db.getSlot(4)?.issue,
        pr: db.getSlot(4)?.pr,
        branch_ref: db.getSlot(4)?.branch_ref,
        head_sha: db.getSlot(4)?.head_sha,
        assignment_epoch: db.getSlot(4)?.assignment_epoch,
        active_turn_state: db.getSlot(4)?.active_turn_state,
        active_turn_id: db.getSlot(4)?.active_turn_id,
        session_id: db.getSlot(4)?.session_id,
        dnd: db.getSlot(4)?.dnd,
      },
      {
        occupied: true,
        repository_id: "heydonna-app",
        issue: 7135,
        pr: 7208,
        branch_ref: "refs/heads/fix/7135-proper-noun-letter-drop-corruption",
        head_sha: "be79bc4817cda506798ec87f81543d1a808b8bd6",
        assignment_epoch: 430,
        active_turn_state: "active",
        active_turn_id: "turn-4-active",
        session_id: "session-s4",
        dnd: false,
      },
    );
  });
});

test("active-turn adoption fails closed on every observed-tuple drift", () => {
  withDatabase((db) => {
    db.assignSlot(
      1,
      "placeholder",
      "github:repo-1",
      10,
      "fix/10-pending",
      "session-a",
      null,
      null,
      0,
    );
    db.startAgentTurn(1, "turn-a");

    const drifts: Array<{
      name: string;
      args: Parameters<MoPDatabase["adoptIssueClaimSlot"]>;
      reason: string;
    }> = [
      {
        name: "branch_ref drift",
        args: [1, "bind", "github:repo-1", 10, "fix/10-real", 20, "a".repeat(40), null, "refs/heads/fix/10-wrong", null, 1],
        reason: "observed_tuple_mismatch",
      },
      {
        name: "head_sha drift",
        args: [1, "bind", "github:repo-1", 10, "fix/10-real", 20, "a".repeat(40), null, "refs/heads/fix/10-pending", "b".repeat(40), 1],
        reason: "observed_tuple_mismatch",
      },
      {
        name: "expected_current_pr drift",
        args: [1, "bind", "github:repo-1", 10, "fix/10-real", 20, "a".repeat(40), 20, "refs/heads/fix/10-pending", null, 1],
        reason: "observed_tuple_mismatch",
      },
      {
        name: "issue drift",
        args: [1, "bind", "github:repo-1", 11, "fix/11-real", 21, "b".repeat(40), null, "refs/heads/fix/10-pending", null, 1],
        reason: "slot_already_occupied",
      },
      {
        name: "repository drift",
        args: [1, "bind", "github:repo-2", 10, "fix/10-real", 20, "a".repeat(40), null, "refs/heads/fix/10-pending", null, 1],
        reason: "slot_already_occupied",
      },
      {
        name: "epoch drift",
        args: [1, "bind", "github:repo-1", 10, "fix/10-real", 20, "a".repeat(40), null, "refs/heads/fix/10-pending", null, 0],
        reason: "epoch_mismatch",
      },
    ];

    for (const drift of drifts) {
      const before = db.getSlot(1);
      const result = db.adoptIssueClaimSlot(...drift.args);
      assert.equal(
        result.reason,
        drift.reason,
        `${drift.name} must fail closed with ${drift.reason}`,
      );
      assert.equal(result.ok, false);
      assert.deepEqual(db.getSlot(1), before, `${drift.name} must not mutate the slot`);
    }
  });
});

test("issue claim adoption refuses a target already owned by another slot", () => {
  withDatabase((db) => {
    db.assignSlot(4, "original", "github:repo-1", 20, "fix/20", null, 20, null, 0);
    db.assignSlot(
      2,
      "claim",
      "github:repo-1",
      10,
      "fix/10-pending",
      "session-b",
      null,
      null,
      0,
    );

    const duplicate = db.adoptIssueClaimSlot(
      2,
      "bind conflict",
      "github:repo-1",
      10,
      "fix/10-real",
      20,
      "a".repeat(40),
      null,
      "refs/heads/fix/10-pending",
      null,
      1,
    );
    assert.deepEqual(duplicate, {
      ok: false,
      conflict: true,
      assignment_epoch: 1,
      idempotent: false,
      reason: "target_already_assigned",
      owner_slots: [4],
      owner_conflicts: [{
        slot: 4,
        matching_fields: ["pr"],
      }],
    });
    assert.equal(db.getSlot(2)?.pr, null);
    assert.equal(db.getSlot(2)?.assignment_epoch, 1);
  });
});

test("assignment rejects a target already owned by another occupied slot", () => {
  withDatabase((db) => {
    const first = db.assignSlot(4, "original", "github:repo-1", 6735, "fix/6735-pending", null, 6737, "old-head", 0);
    assert.equal(first.ok, true);

    const duplicate = db.assignSlot(2, "failover", "github:repo-1", 6735, "fix/6735-pending", null, 6737, "new-head", 0);
    assert.deepEqual(duplicate, {
      ok: false,
      conflict: true,
      assignment_epoch: 0,
      idempotent: false,
      reason: "target_already_assigned",
      owner_slots: [4],
      owner_conflicts: [{
        slot: 4,
        matching_fields: ["issue", "pr", "branch_ref"],
      }],
    });
    assert.equal(db.getSlot(2)?.occupied, false);
    assert.equal(db.getSlot(2)?.assignment_epoch, 0);
  });
});

test("repository scope permits the same target identities in another repository", () => {
  withDatabase((db) => {
    assert.equal(
      db.assignSlot(
        1,
        "repo one",
        "github:repo-1",
        10,
        "fix/shared",
        null,
        20,
        null,
        0,
      ).ok,
      true,
    );
    assert.equal(
      db.assignSlot(
        2,
        "repo two",
        "github:repo-2",
        10,
        "refs/heads/fix/shared",
        null,
        20,
        null,
        0,
      ).ok,
      true,
    );
  });
});

test("partial unique indexes enforce occupied repository identities", () => {
  withDatabase((db, path) => {
    db.assignSlot(
      1,
      "owner",
      "github:repo-1",
      10,
      "fix/10",
      null,
      20,
      null,
      0,
    );

    const raw = new Database(path);
    try {
      const indexes = raw.prepare(`
        SELECT name FROM sqlite_master
        WHERE type = 'index' AND name LIKE 'ux_slots_occupied_repository_%'
        ORDER BY name
      `).all() as Array<{ name: string }>;
      assert.deepEqual(indexes.map((row) => row.name), [
        "ux_slots_occupied_repository_branch_ref",
        "ux_slots_occupied_repository_issue",
        "ux_slots_occupied_repository_pr",
      ]);
      assert.throws(
        () => raw.prepare(`
          UPDATE slots
          SET occupied = 1,
              repository_id = 'github:repo-1',
              issue = 10,
              branch = 'different',
              branch_ref = 'refs/heads/different'
          WHERE slot = 2
        `).run(),
        /UNIQUE constraint failed/,
      );
    } finally {
      raw.close();
    }
    assert.equal(db.getSlot(2)?.occupied, false);
    assert.equal(db.getSlot(2)?.assignment_epoch, 0);
  });
});

test("generic slot updates cannot mutate assignment identity", () => {
  withDatabase((db) => {
    db.updateSlot(2, {
      occupied: true,
      status: "active",
      repository_id: "github:repo-1",
      issue: 10,
      pr: 20,
      branch: "fix/10",
      branch_ref: "refs/heads/fix/10",
      head_sha: "a".repeat(40),
      assignment_epoch: 99,
    });
    const slot = db.getSlot(2);
    assert.equal(slot?.occupied, false);
    assert.equal(slot?.status, "free");
    assert.equal(slot?.repository_id, null);
    assert.equal(slot?.issue, null);
    assert.equal(slot?.pr, null);
    assert.equal(slot?.branch, null);
    assert.equal(slot?.branch_ref, null);
    assert.equal(slot?.head_sha, null);
    assert.equal(slot?.assignment_epoch, 0);
  });
});

test("legacy occupied rows require explicit immutable repository backfill", () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-i1-migration-test-"));
  const path = join(directory, "mop.db");
  try {
    const legacy = new Database(path);
    legacy.exec(`
      CREATE TABLE slots (
        slot INTEGER PRIMARY KEY,
        address TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'free',
        occupied INTEGER NOT NULL DEFAULT 0,
        session_id TEXT,
        task TEXT,
        issue INTEGER,
        branch TEXT,
        pr INTEGER,
        head_sha TEXT,
        assignment_epoch INTEGER NOT NULL DEFAULT 0,
        assigned_at TEXT,
        last_activity TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        dnd INTEGER NOT NULL DEFAULT 0,
        idle INTEGER NOT NULL DEFAULT 1
      );
      INSERT INTO slots (
        slot, address, status, occupied, task, issue, branch, pr,
        assignment_epoch
      ) VALUES (
        1, '0:0.1', 'active', 1, 'legacy', 10, 'fix/10', 20, 7
      );
    `);
    legacy.close();

    assert.throws(
      () => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: path }),
      /MOP_LEGACY_REPOSITORY_ID is required/,
    );

    const migrated = new MoPDatabase({
      ...DEFAULT_CONFIG,
      dbPath: path,
      legacyRepositoryId: "github:repo-legacy",
    });
    assert.equal(migrated.getSlot(1)?.repository_id, "github:repo-legacy");
    assert.equal(migrated.getSlot(1)?.branch, "fix/10");
    assert.equal(migrated.getSlot(1)?.branch_ref, "refs/heads/fix/10");
    migrated.close();
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("assignment rejects issue-only and branch-only duplicate ownership", () => {
  withDatabase((db) => {
    db.assignSlot(4, "original", "github:repo-1", 6735, "fix/6735-pending", null, null, null, 0);

    const sameIssue = db.assignSlot(2, "same issue", "github:repo-1", 6735, "different-branch", null, null, null, 0);
    assert.equal(sameIssue.reason, "target_already_assigned");
    assert.deepEqual(sameIssue.owner_slots, [4]);

    const sameBranch = db.assignSlot(3, "same branch", "github:repo-1", 9999, "fix/6735-pending", null, null, null, 0);
    assert.equal(sameBranch.reason, "target_already_assigned");
    assert.deepEqual(sameBranch.owner_slots, [4]);
  });
});

test("released ownership can be explicitly reassigned", () => {
  withDatabase((db) => {
    db.assignSlot(4, "original", "github:repo-1", 6735, "fix/6735-pending", null, 6737, "old-head", 0);
    const released = db.releaseSlot(4, 1);
    assert.equal(released.ok, true);

    const reassigned = db.assignSlot(2, "replacement", "github:repo-1", 6735, "fix/6735-pending", null, 6737, "new-head", 0);
    assert.deepEqual(reassigned, {
      ok: true,
      conflict: false,
      assignment_epoch: 1,
      idempotent: false,
    });
  });
});
