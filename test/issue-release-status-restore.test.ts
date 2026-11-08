import assert from "node:assert/strict";
import test from "node:test";

import { applyLabelsApi } from "./gh-labels-api-stub.js";

import {
  createGhIssueOwnershipProjection,
  statusFromReadyPoolHeader,
  type GhRunner,
  type IssuePriorStatusStore,
} from "../src/issueProjection.js";

// Release must return an issue to its real lifecycle, never reset it to status:todo
// (MoP control-plane defect 2026-11-08: released/displaced issues re-entered the Ready Pool).

const REPO = "Scribie/heydonna-app";
const HOLD_BODY = "<!-- ready-pool:\npriority: P1\nholds: pm-gate\n-->";
const DEPENDENCY_BODY = "<!-- ready-pool:\nblockers: #999999\nholds: dependency\n-->";

function ghIssue(initial: { labels: string[]; body?: string; linkedPrs?: number[]; state?: string }) {
  let labels = [...initial.labels];
  const runner: GhRunner = async (args) => {
    if (args[0] === "issue" && args[1] === "view") {
      return JSON.stringify({
        state: initial.state ?? "OPEN",
        labels: labels.map((name) => ({ name })),
        body: initial.body ?? "",
        closedByPullRequestsReferences: (initial.linkedPrs ?? []).map((number) => ({ number })),
      });
    }
    if (args[0] === "api") {
      labels = applyLabelsApi(args, labels);
      return "";
    }
    throw new Error(`unexpected gh invocation: ${args.join(" ")}`);
  };
  return { runner, labels: () => [...labels].sort() };
}

function memoryStore(): IssuePriorStatusStore & { entries: Map<number, string> } {
  const entries = new Map<number, string>();
  return {
    entries,
    get: (issue) => entries.get(issue) ?? null,
    set: (issue, status) => {
      if (status === null) {
        entries.delete(issue);
      } else {
        entries.set(issue, status);
      }
    },
  };
}

test("assign then release restores the recorded blocked status instead of todo", async () => {
  const gh = ghIssue({ labels: ["status:blocked", "P1", "sub:pm-gate"], body: HOLD_BODY });
  const priorStatus = memoryStore();
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner, repository: REPO, priorStatus });

  const assigned = await projection.onAssigned(7105, 3, null);
  assert.equal(assigned.status, "projected");
  assert.deepEqual(gh.labels(), ["P1", "slot:3", "status:in-progress"]);
  assert.equal(priorStatus.entries.get(7105), "status:blocked");

  const released = await projection.onReleased(7105, 3, null);
  assert.equal(released.status, "projected");
  assert.deepEqual(released.added_labels, ["status:blocked"]);
  assert.deepEqual(gh.labels(), ["P1", "status:blocked"]);
  assert.equal(priorStatus.entries.has(7105), false, "the record is consumed by a successful release");
});

test("release with an open closing PR sets status:in-review", async () => {
  const gh = ghIssue({ labels: ["status:in-progress", "slot:3", "P1"], linkedPrs: [12993] });
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner, repository: REPO, priorStatus: memoryStore() });

  const released = await projection.onReleased(12553, 3, null);
  assert.equal(released.status, "projected");
  assert.deepEqual(gh.labels(), ["P1", "status:in-review"]);
});

test("an open closing PR replaces a different independent status with in-review", async () => {
  const gh = ghIssue({ labels: ["status:todo", "status:in-progress", "slot:3", "P1"], linkedPrs: [12994] });
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner, repository: REPO, priorStatus: memoryStore() });

  const released = await projection.onReleased(12554, 3, null);
  assert.equal(released.status, "projected");
  assert.deepEqual(gh.labels(), ["P1", "status:in-review"]);
});

test("unknown prior status reads the live header: dependency-only stays todo, other holds become blocked", async () => {
  const dependency = ghIssue({ labels: ["status:in-progress", "slot:3"], body: DEPENDENCY_BODY });
  await createGhIssueOwnershipProjection({ runGh: dependency.runner, repository: REPO, priorStatus: memoryStore() })
    .onReleased(8001, 3, null);
  assert.deepEqual(dependency.labels(), ["status:todo"]);

  const held = ghIssue({ labels: ["status:in-progress", "slot:3"], body: HOLD_BODY });
  await createGhIssueOwnershipProjection({ runGh: held.runner, repository: REPO, priorStatus: memoryStore() })
    .onReleased(8002, 3, null);
  assert.deepEqual(held.labels(), ["status:blocked"]);
});

test("a recorded in-review is never restored without a live PR, and no record defaults to todo", async () => {
  const gh = ghIssue({ labels: ["status:in-progress", "slot:3"] });
  const priorStatus = memoryStore();
  priorStatus.set(8003, "status:in-review");
  await createGhIssueOwnershipProjection({ runGh: gh.runner, repository: REPO, priorStatus }).onReleased(8003, 3, null);
  assert.deepEqual(gh.labels(), ["status:todo"]);
});

test("a re-assign of a claimed issue keeps the record from the first claim", async () => {
  const gh = ghIssue({ labels: ["status:blocked", "P1"], body: HOLD_BODY });
  const priorStatus = memoryStore();
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner, repository: REPO, priorStatus });

  await projection.onAssigned(8004, 3, null);
  await projection.onAssigned(8004, 4, null);
  assert.equal(priorStatus.entries.get(8004), "status:blocked");

  await projection.onReleased(8004, 4, null);
  assert.deepEqual(gh.labels(), ["P1", "status:blocked"]);
});

test("the header maps only non-dependency holds to blocked", () => {
  assert.equal(statusFromReadyPoolHeader(DEPENDENCY_BODY), null);
  assert.equal(statusFromReadyPoolHeader(HOLD_BODY), "status:blocked");
  assert.equal(statusFromReadyPoolHeader("<!-- ready-pool:\nholds: none\n-->"), null);
  assert.equal(statusFromReadyPoolHeader("no header at all"), null);
});
