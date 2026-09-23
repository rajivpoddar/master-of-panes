import assert from "node:assert/strict";
import test from "node:test";

import {
  createGhIssueOwnershipProjection,
  type GhRunner,
} from "../src/issueProjection.js";

const CANONICAL_ID = "github:heydonna-app/heydonna-app";

/** Stub gh: serves `issue view` from a mutable label map and applies edits. */
function stubGh(initialLabels: string[]) {
  let labels = [...initialLabels];
  const calls: string[][] = [];
  const runner: GhRunner = async (args) => {
    calls.push(args);
    if (args[0] === "issue" && args[1] === "view") {
      return JSON.stringify({ state: "OPEN", labels: labels.map((name) => ({ name })) });
    }
    if (args[0] === "issue" && args[1] === "edit") {
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
  return { runner, calls, labels: () => labels };
}

test("assign with the canonical github:-prefixed id projects (not repository_unmapped)", async () => {
  const gh = stubGh(["status:todo"]);
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner });
  const result = await projection.onAssigned(8162, 2, CANONICAL_ID);
  assert.equal(result.status, "projected");
  assert.equal(result.reason, null);
  assert.deepEqual([...result.added_labels].sort(), ["slot:2", "status:in-progress"]);
  assert.deepEqual(result.removed_labels, ["status:todo"]);
  assert.ok(gh.labels().includes("slot:2"));
  assert.ok(gh.labels().includes("status:in-progress"));
  assert.ok(!gh.labels().includes("status:todo"));
});

test("slot-only release with the canonical github:-prefixed id unwinds the labels", async () => {
  const gh = stubGh(["status:in-progress", "slot:2"]);
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner });
  const result = await projection.onReleased(8162, 2, CANONICAL_ID);
  assert.equal(result.status, "projected");
  assert.equal(result.reason, null);
  assert.ok(!gh.labels().includes("slot:2"));
});

test("a genuinely unknown repository id still skips as repository_unmapped", async () => {
  const gh = stubGh(["status:todo"]);
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner });
  const result = await projection.onAssigned(8162, 2, "github:someone/else");
  assert.equal(result.status, "skipped");
  assert.equal(result.reason, "repository_unmapped");
  assert.equal(gh.calls.length, 0);
});
