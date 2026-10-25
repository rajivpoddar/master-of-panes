import assert from "node:assert/strict";
import test from "node:test";

import { applyLabelsApi } from "./gh-labels-api-stub.js";

import {
  createGhIssueOwnershipProjection,
  type GhRunner,
} from "../src/issueProjection.js";

for (const repositoryId of [
  "github:heydonna-app/heydonna-app",
  "heydonna-app/heydonna-app",
  "992731533",
  "github:Scribie/heydonna-app",
  "Scribie/heydonna-app",
  null,
]) {
  test(`live GitHub target preserves assignment identity ${repositoryId}`, async () => {
    let labels = ["status:todo", "P1"];
    const calls: string[][] = [];
    const runGh: GhRunner = async (args) => {
      calls.push(args);
      if (args[0] === "api") {
        assert.ok(args.some((arg) => arg.startsWith("repos/Scribie/heydonna-app/issues/")));
      } else {
        assert.equal(args[args.indexOf("--repo") + 1], "Scribie/heydonna-app");
      }
      if (args[1] === "view") {
        return JSON.stringify({ state: "OPEN", labels: labels.map((name) => ({ name })) });
      }
      assert.equal(args[0], "api");
      labels = applyLabelsApi(args, labels);
      return "";
    };
    const projection = createGhIssueOwnershipProjection({ runGh });
    const assigned = await projection.onAssigned(10586, 4, repositoryId);
    assert.equal(assigned.status, "projected");
    assert.equal(assigned.repository, "Scribie/heydonna-app");
    assert.equal(assigned.verified, true);
    assert.deepEqual([...labels].sort(), ["P1", "slot:4", "status:in-progress"]);

    const replay = await projection.onAssigned(10586, 4, repositoryId);
    assert.equal(replay.status, "unchanged");
    assert.equal(replay.verified, true);
    assert.equal(calls.filter((args) => args[0] === "api").length, 2);

    const released = await projection.onReleased(10586, 4, repositoryId);
    assert.equal(released.status, "projected");
    assert.equal(released.repository, "Scribie/heydonna-app");
    assert.equal(released.verified, true);
    assert.deepEqual([...labels].sort(), ["P1", "status:todo"]);
    assert.equal(calls.filter((args) => args[0] === "api").length, 5);
  });
}
