import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import {
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "../src/assignmentAuthority.js";
import { MoPDatabase, computeFamily2ReleaseDigest, slotAssignmentTuple } from "../src/db.js";
import {
  createGhIssueOwnershipProjection,
  type GhRunner,
  type IssueOwnershipProjection,
  type IssueProjectionOutcome,
} from "../src/issueProjection.js";
import { NativeSlotReleaseCoordinator } from "../src/slotRelease.js";
import { DEFAULT_CONFIG, type MoPConfig } from "../src/types.js";

const MAIN_HEAD = "b".repeat(40);
const CHECKOUT = "/tmp/mop-issue-projection-checkout";
const REPO = "heydonna-app/heydonna-app";

/** Fake gh: serves `issue view` from a mutable label map and records edits. */
function fakeGh(initial: { state?: string; labels: string[] }) {
  const state = initial.state ?? "OPEN";
  let labels = [...initial.labels];
  const edits: string[][] = [];
  const failure = { failWrites: false };
  const runner: GhRunner = async (args) => {
    if (args[0] === "issue" && args[1] === "view") {
      return JSON.stringify({ state, labels: labels.map((name) => ({ name })) });
    }
    if (args[0] === "issue" && args[1] === "edit") {
      if (failure.failWrites) {
        throw new Error("gh: issue edit failed");
      }
      edits.push(args);
      for (let index = 0; index < args.length; index += 1) {
        if (args[index] === "--add-label") {
          labels = [...new Set([...labels, args[index + 1]])];
        }
        if (args[index] === "--remove-label") {
          labels = labels.filter((label) => label !== args[index + 1]);
        }
      }
      return "";
    }
    throw new Error(`unexpected gh invocation: ${args.join(" ")}`);
  };
  return { runner, edits, labels: () => labels, failure };
}

function recordingProjection(): IssueOwnershipProjection & { calls: string[] } {
  const calls: string[] = [];
  const make = (label: string) => async (
    issue: number,
    slot: number,
    repositoryId: string | null | undefined,
  ): Promise<IssueProjectionOutcome> => {
    calls.push(`${label}:${issue}:${slot}:${repositoryId ?? "null"}`);
    return {
      status: "projected",
      reason: null,
      repository: REPO,
      issue,
      slot,
      added_labels: [`status:in-progress`, `slot:${slot}`],
      removed_labels: ["status:todo"],
      verified: true,
    };
  };
  return {
    calls,
    onAssigned: make("assign"),
    onReleased: make("release"),
  };
}

/* ── projector contract ─────────────────────────────────────────────── */

test("assignment projects status:in-progress and the exact slot label, preserving unrelated labels", async () => {
  const gh = fakeGh({ labels: ["status:todo", "P1", "customer-feedback"] });
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner, repository: REPO });
  const outcome = await projection.onAssigned(7952, 3, "992731533");

  assert.equal(outcome.status, "projected");
  assert.equal(outcome.verified, true);
  assert.deepEqual(outcome.added_labels, ["status:in-progress", "slot:3"]);
  assert.deepEqual(outcome.removed_labels, ["status:todo"]);
  assert.deepEqual([...gh.labels()].sort(), ["P1", "customer-feedback", "slot:3", "status:in-progress"].sort());
  assert.equal(gh.edits.length, 1);
  assert.ok(gh.edits[0].includes("--repo") && gh.edits[0].includes(REPO));
});

test("assignment is idempotent for an already-projected claim and corrects a foreign slot label", async () => {
  const already = fakeGh({ labels: ["status:in-progress", "slot:3", "P1"] });
  const projection = createGhIssueOwnershipProjection({ runGh: already.runner, repository: REPO });
  const steady = await projection.onAssigned(7952, 3, "992731533");
  assert.equal(steady.status, "unchanged");
  assert.equal(steady.verified, true);
  assert.equal(already.edits.length, 0, "a replayed assignment must perform no label write");

  const drifted = fakeGh({ labels: ["status:in-progress", "slot:2", "P1"] });
  const driftedProjection = createGhIssueOwnershipProjection({ runGh: drifted.runner, repository: REPO });
  const corrected = await driftedProjection.onAssigned(7952, 3, "992731533");
  assert.equal(corrected.status, "projected");
  assert.deepEqual(corrected.added_labels, ["slot:3"]);
  assert.deepEqual(corrected.removed_labels, ["slot:2"]);
  assert.deepEqual([...drifted.labels()].sort(), ["P1", "slot:3", "status:in-progress"].sort());
});

test("release removes the slot label and unwinds only the claim lifecycle it owns", async () => {
  const claimed = fakeGh({ labels: ["status:in-progress", "slot:3", "P1"] });
  const projection = createGhIssueOwnershipProjection({ runGh: claimed.runner, repository: REPO });
  const released = await projection.onReleased(7952, 3, "992731533");
  assert.equal(released.status, "projected");
  assert.deepEqual(released.removed_labels, ["slot:3", "status:in-progress"]);
  assert.deepEqual(released.added_labels, ["status:todo"]);
  assert.deepEqual([...claimed.labels()].sort(), ["P1", "status:todo"].sort());

  // A review-owned lifecycle is not MoP's to rewrite: only the slot label goes.
  const inReview = fakeGh({ labels: ["status:in-review", "slot:3", "P1"] });
  const reviewProjection = createGhIssueOwnershipProjection({ runGh: inReview.runner, repository: REPO });
  const reviewReleased = await reviewProjection.onReleased(7952, 3, "992731533");
  assert.deepEqual(reviewReleased.removed_labels, ["slot:3"]);
  assert.deepEqual(reviewReleased.added_labels, []);
  assert.deepEqual([...inReview.labels()].sort(), ["P1", "status:in-review"].sort());

  // A closed issue never gains an invented lifecycle status.
  const closed = fakeGh({ state: "CLOSED", labels: ["status:in-progress", "slot:3"] });
  const closedProjection = createGhIssueOwnershipProjection({ runGh: closed.runner, repository: REPO });
  const closedReleased = await closedProjection.onReleased(7952, 3, "992731533");
  assert.deepEqual(closedReleased.added_labels, [], "a closed issue must never gain a lifecycle status");
  assert.deepEqual(closedReleased.removed_labels, ["slot:3", "status:in-progress"]);
  assert.deepEqual([...closed.labels()].sort(), []);
});

test("projection failures and unmapped repositories are typed, never thrown", async () => {
  const failing = createGhIssueOwnershipProjection({
    runGh: async () => { throw new Error("gh: not authenticated"); },
    repository: REPO,
  });
  const failed = await failing.onAssigned(7952, 3, "992731533");
  assert.equal(failed.status, "failed");
  assert.match(failed.reason ?? "", /^issue_projection_read_failed:/);
  assert.equal(failed.verified, false);

  const unmapped = createGhIssueOwnershipProjection({
    runGh: async () => { throw new Error("must not be called"); },
    repository: REPO,
  });
  const skipped = await unmapped.onAssigned(7952, 3, "someone-elses-repo");
  assert.equal(skipped.status, "skipped");
  assert.equal(skipped.reason, "repository_unmapped");

  // A write that reports success but does not land is a typed readback failure.
  const silent: GhRunner = async (args) => {
    if (args[1] === "view") return JSON.stringify({ state: "OPEN", labels: [{ name: "status:todo" }] });
    return "";
  };
  const silentProjection = createGhIssueOwnershipProjection({ runGh: silent, repository: REPO });
  const mismatch = await silentProjection.onAssigned(7952, 3, "992731533");
  assert.equal(mismatch.status, "failed");
  assert.equal(mismatch.reason, "issue_projection_readback_mismatch");
});

/* ── native route integration ───────────────────────────────────────── */

async function withAssignmentRoute(
  projection: IssueOwnershipProjection,
  run: (app: Hono, db: MoPDatabase) => Promise<void>,
): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-issue-projection-route-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const app = new Hono();
  registerAssignmentRoute(app, db, projection);
  try {
    await run(app, db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

function assignRequest(body: Record<string, unknown>): RequestInit {
  const headers = new Headers({ "content-type": "application/json" });
  headers.set(PM_TRANSITION_ASSIGNMENT_HEADER, PM_TRANSITION_ASSIGNMENT_AUTHORITY);
  return { method: "POST", headers, body: JSON.stringify(body) };
}

test("native assignment projects the issue surface and reports it beside the durable tuple", async () => {
  const projection = recordingProjection();
  await withAssignmentRoute(projection, async (app, db) => {
    const response = await app.request("/slots/2/assign", assignRequest({
      repository_id: "992731533", issue: 7952, task: "issue-only native claim",
    }));
    assert.equal(response.status, 200);
    const body = await response.json() as Record<string, unknown>;
    assert.equal(body.occupied, true);
    assert.equal(body.issue, 7952);
    assert.equal((body.issue_projection as Record<string, unknown>).status, "projected");
    assert.deepEqual(projection.calls, ["assign:7952:2:992731533"]);
    assert.equal(db.getSlot(2)?.occupied, true);
  });
});

test("a projection failure never rewrites the durable assignment outcome or its status", async () => {
  const failing: IssueOwnershipProjection = {
    onAssigned: async (issue, slot) => ({
      status: "failed",
      reason: "issue_projection_write_failed:gh exited 1",
      repository: REPO,
      issue,
      slot,
      added_labels: [],
      removed_labels: [],
      verified: false,
    }),
    onReleased: async (issue, slot) => ({
      status: "failed",
      reason: "issue_projection_write_failed:gh exited 1",
      repository: REPO,
      issue,
      slot,
      added_labels: [],
      removed_labels: [],
      verified: false,
    }),
  };
  await withAssignmentRoute(failing, async (app, db) => {
    const response = await app.request("/slots/2/assign", assignRequest({
      repository_id: "992731533", issue: 7952, task: "issue-only native claim",
    }));
    assert.equal(response.status, 200, "a projection failure must not change the durable HTTP outcome");
    const body = await response.json() as Record<string, unknown>;
    assert.equal(body.occupied, true);
    assert.equal(body.issue, 7952);
    assert.equal((body.issue_projection as Record<string, unknown>).status, "failed");
    assert.match(String((body.issue_projection as Record<string, unknown>).reason), /issue_projection_write_failed/);
    assert.equal(db.getSlot(2)?.occupied, true);
    assert.equal(db.getSlot(2)?.issue, 7952);
  });
});

test("exact tuple/CAS mismatch refuses before any projection call", async () => {
  const projection = recordingProjection();
  await withAssignmentRoute(projection, async (app, db) => {
    const first = await app.request("/slots/2/assign", assignRequest({
      repository_id: "992731533", issue: 7952, task: "issue-only native claim",
    }));
    assert.equal(first.status, 200);
    const epoch = db.getSlot(2)!.assignment_epoch;

    const stale = await app.request("/slots/2/assign", assignRequest({
      repository_id: "992731533",
      issue: 7953,
      task: "stale retry",
      expected_epoch: epoch - 1,
      pr: 42,
      branch: "fix/7953",
      head_sha: "c".repeat(40),
      work_kind: "implementation",
      handoff_id: "handoff-7953",
    }));
    assert.equal(stale.status, 409);
    assert.equal(db.getSlot(2)?.issue, 7952);
    const projectedIssues = projection.calls.map((call) => call.split(":")[1]);
    assert.deepEqual(projectedIssues, ["7952"], "a refused transition must not project an issue surface");
  });
});

/* ── native release integration ─────────────────────────────────────── */

test("native release projects the released owner and keeps the durable release authoritative", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-issue-projection-release-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    assert.equal(
      db.assignSlot(4, "legacy issue-only task", "992731533", 7952, null, null, null, 0).ok,
      true,
    );
    db.updateSlot(4, { idle: true, activity: "waiting_for_pm_direction" });
    const current = db.getSlot(4)!;
    const tuple = slotAssignmentTuple(current)!;
    const request = {
      slot: 4,
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
      release_mode: "quiescent_legacy_issue_only" as const,
      effect_id: "issue-projection-release-7952",
      request_digest: "",
    };
    request.request_digest = computeFamily2ReleaseDigest(request);
    const projection = recordingProjection();
    const release = new NativeSlotReleaseCoordinator({
      db,
      issueProjection: projection,
      resolveOwningCheckout: async () => CHECKOUT,
      deliverInstruction: async () => true,
      owningSlotIsIdle: async () => true,
      resetAndObserveCheckout: async () => ({
        checkout_path: CHECKOUT,
        branch: "main",
        head: MAIN_HEAD,
        clean: true,
        reset_succeeded: true,
        error: null,
      }),
      observeCheckout: async () => ({
        checkout_path: CHECKOUT,
        clean: true,
        unpushed_commits: [],
        branch: "main",
        head: MAIN_HEAD,
        error: null,
      }),
    });
    const released = await release.release(request);
    assert.equal(released.success, true);
    assert.equal(released.code, "released");
    assert.equal(db.getSlot(4)?.occupied, false);
    assert.equal((released.issue_projection as Record<string, unknown> | null)?.status, "projected");
    assert.deepEqual(projection.calls, ["release:7952:4:992731533"]);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a durable release replay repairs a projection that failed on the first attempt", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-issue-projection-replay-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    assert.equal(
      db.assignSlot(4, "legacy issue-only task", "992731533", 7952, null, null, null, 0).ok,
      true,
    );
    db.updateSlot(4, { idle: true, activity: "waiting_for_pm_direction" });
    const current = db.getSlot(4)!;
    const tuple = slotAssignmentTuple(current)!;
    const request = {
      slot: 4,
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
      release_mode: "quiescent_legacy_issue_only" as const,
      effect_id: "issue-projection-replay-7952",
      request_digest: "",
    };
    request.request_digest = computeFamily2ReleaseDigest(request);

    // The issue surface still carries the live label while the write path is down.
    const gh = fakeGh({ labels: ["status:in-progress", "slot:4", "P1"] });
    gh.failure.failWrites = true;
    const projection = createGhIssueOwnershipProjection({ runGh: gh.runner, repository: REPO });
    const release = new NativeSlotReleaseCoordinator({
      db,
      issueProjection: projection,
      resolveOwningCheckout: async () => CHECKOUT,
      deliverInstruction: async () => true,
      owningSlotIsIdle: async () => true,
      resetAndObserveCheckout: async () => ({
        checkout_path: CHECKOUT,
        branch: "main",
        head: MAIN_HEAD,
        clean: true,
        reset_succeeded: true,
        error: null,
      }),
      observeCheckout: async () => ({
        checkout_path: CHECKOUT,
        clean: true,
        unpushed_commits: [],
        branch: "main",
        head: MAIN_HEAD,
        error: null,
      }),
    });

    const first = await release.release(request);
    assert.equal(first.success, true, "the durable FREE commit is authoritative");
    assert.equal(first.code, "released");
    assert.equal(db.getSlot(4)?.occupied, false, "the durable FREE postcondition still holds");
    assert.equal(first.issue_projection?.status, "failed");
    assert.match(first.issue_projection?.reason ?? "", /issue_projection_write_failed/);
    assert.deepEqual([...gh.labels()].sort(), ["P1", "slot:4", "status:in-progress"].sort());

    // The identical retry consumes the durable receipt and must still repair the surface.
    gh.failure.failWrites = false;
    const second = await release.release(request);
    assert.equal(second.success, true);
    assert.equal(second.code, "released");
    assert.equal(second.idempotent, true, "the retry is the durable effect replay");
    assert.equal(second.issue_projection?.status, "projected");
    assert.equal(second.issue_projection?.verified, true);
    assert.deepEqual(second.issue_projection?.removed_labels, ["slot:4", "status:in-progress"]);
    assert.deepEqual([...gh.labels()].sort(), ["P1", "status:todo"].sort(), "the replay healed the issue surface");

    // A third identical replay is idempotent and performs no label write at all.
    const editsBefore = gh.edits.length;
    const third = await release.release(request);
    assert.equal(third.success, true);
    assert.equal(third.issue_projection?.status, "unchanged");
    assert.equal(third.issue_projection?.verified, true);
    assert.equal(gh.edits.length, editsBefore, "an already-projected replay must perform zero edits");
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
