import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import { MoPDatabase } from "../src/db.js";
import {
  SUPERSEDED_REASON,
  createGhIssueOwnershipProjection,
  type GhRunner,
} from "../src/issueProjection.js";
import type { MoPConfig } from "../src/types.js";
import { applyLabelsApi } from "./gh-labels-api-stub.js";

const REPO = "github:heydonna-app/heydonna-app";
const HEAD = "b".repeat(40);

/** Fake gh with per-issue labels and a pausable seam on the next call of a kind. */
function pausableGh(initial: Record<number, string[]>) {
  const labels: Record<number, string[]> = Object.fromEntries(
    Object.entries(initial).map(([k, v]) => [Number(k), [...v]]),
  );
  const log: string[] = [];
  const holds: Array<{ kind: string; issue: number; reached: () => void; release: Promise<void> }> = [];
  function pauseNext(kind: "view" | "post" | "delete", issue: number) {
    let reached!: () => void;
    let open!: () => void;
    const reachedP = new Promise<void>((r) => (reached = r));
    const release = new Promise<void>((r) => (open = r));
    holds.push({ kind, issue, reached, release });
    return { reached: reachedP, resume: open };
  }
  const runner: GhRunner = async (args) => {
    const isView = args[0] === "issue" && args[1] === "view";
    const issue = isView
      ? Number(args[2])
      : Number(/issues\/(\d+)\/labels/.exec(args.find((a) => a.includes("/labels")) ?? "")?.[1]);
    const kind = isView ? "view" : args.includes("POST") ? "post" : "delete";
    log.push(`${kind}:${issue}:${args.filter((a) => a.startsWith("labels[]=") || a.includes("/labels/")).join(",")}`);
    const index = holds.findIndex((h) => h.kind === kind && h.issue === issue);
    if (index >= 0) {
      const [hold] = holds.splice(index, 1);
      hold.reached();
      await hold.release;
    }
    if (isView) return JSON.stringify({ state: "OPEN", labels: labels[issue].map((name) => ({ name })) });
    labels[issue] = applyLabelsApi(args, labels[issue]);
    return "";
  };
  return { runner, labels, log, pauseNext };
}

const writes = (log: string[]): string[] => log.filter((entry) => !entry.startsWith("view:"));

async function withRoute(
  gh: ReturnType<typeof pausableGh>,
  run: (app: Hono, db: MoPDatabase) => Promise<void>,
): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-native-guard-"));
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
  registerAssignmentRoute(
    app,
    db,
    createGhIssueOwnershipProjection({ runGh: gh.runner, repository: "Scribie/heydonna-app" }),
  );
  try {
    await run(app, db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

const NATIVE_ASSIGN = (slot: number) => `/slots/${slot}/assign`;
const NATIVE_ADOPT = (slot: number) => `/slots/${slot}/adopt-issue-claim`;

const send = (app: Hono, path: string, body: Record<string, unknown>) =>
  app.request(path, {
    method: "POST",
    headers: new Headers({ "content-type": "application/json" }),
    body: JSON.stringify(body),
  });

const claim = (issue: number) => ({ repository_id: REPO, issue, task: `claim ${issue}` });
const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 20));

type Slot = NonNullable<ReturnType<MoPDatabase["getSlot"]>>;
function adoptBody(current: Slot, issue: number): Record<string, unknown> {
  return {
    expected_epoch: current.assignment_epoch,
    expected_current_repository_id: current.repository_id,
    expected_current_issue: current.issue,
    expected_current_pr: current.pr,
    expected_current_branch: current.branch,
    expected_current_head_sha: current.head_sha,
    expected_current_work_kind: current.work_kind,
    expected_current_handoff_id: current.handoff_id,
    expected_current_claimed_at: current.claimed_at,
    repository_id: REPO,
    issue,
    pr: 77,
    branch: `fix/${issue}-adopt`,
    head_sha: HEAD,
    work_kind: "implementation",
    handoff_id: `handoff-${issue}`,
    claimed_at: current.claimed_at ?? null,
    task: "adopt",
  };
}

test("native assign route: claim reading from GitHub when ownership advances is skipped, writes nothing", async () => {
  const gh = pausableGh({ 8200: ["status:todo"], 9200: ["status:todo"] });
  await withRoute(gh, async (app, db) => {
    const pause = gh.pauseNext("view", 8200);
    const pending = send(app, NATIVE_ASSIGN(2), claim(8200));
    await pause.reached;
    const released = db.releaseSlotSimple(2);
    assert.equal(db.assignSlot(2, "next", REPO, 9200, "fix/9200", null, HEAD, released.assignment_epoch,
      "implementation", "handoff-9200", true).ok, true);
    pause.resume();
    const response = await pending;
    assert.equal(response.status, 200, "durable outcome is unchanged");
    const body = await response.json() as any;
    assert.equal(body.issue_projection.status, "skipped");
    assert.equal(body.issue_projection.reason, SUPERSEDED_REASON);
    assert.equal(body.issue_projection.verified, false);
    assert.deepEqual(writes(gh.log), []);
    assert.deepEqual(gh.labels[8200], ["status:todo"]);
  });
});

test("native assign route: paused mid-write stops, no stale later write, successor claim converges", async () => {
  const gh = pausableGh({ 8210: ["status:todo", "P1"] });
  await withRoute(gh, async (app, db) => {
    const pause = gh.pauseNext("post", 8210);
    const old = send(app, NATIVE_ASSIGN(2), claim(8210));
    await pause.reached;
    assert.equal(db.releaseSlotSimple(2).ok, true);
    const successor = send(app, NATIVE_ASSIGN(3), claim(8210)); // queues behind the in-flight write
    await tick();
    pause.resume();
    const oldBody = await (await old).json() as any;
    assert.equal(oldBody.issue_projection.status, "skipped");
    assert.equal(oldBody.issue_projection.reason, SUPERSEDED_REASON);
    assert.equal(oldBody.issue_projection.verified, false);
    assert.deepEqual(oldBody.issue_projection.removed_labels, []);
    const succBody = await (await successor).json() as any;
    assert.equal(succBody.issue_projection.status, "projected");
    assert.deepEqual([...gh.labels[8210]].sort(), ["P1", "slot:3", "status:in-progress"]);
  });
});

test("native adopt route: rebind reading from GitHub when ownership advances is skipped", async () => {
  const gh = pausableGh({ 8220: ["status:todo"], 9220: ["status:todo"] });
  await withRoute(gh, async (app, db) => {
    assert.equal((await send(app, NATIVE_ASSIGN(2), claim(8220))).status, 200);
    const baseline = writes(gh.log).length;
    const pause = gh.pauseNext("view", 8220);
    const pending = send(app, NATIVE_ADOPT(2), adoptBody(db.getSlot(2)!, 8220));
    await pause.reached;
    const released = db.releaseSlotSimple(2);
    assert.equal(db.assignSlot(2, "next", REPO, 9220, "fix/9220", null, HEAD, released.assignment_epoch,
      "implementation", "handoff-9220", true).ok, true);
    pause.resume();
    const response = await pending;
    assert.equal(response.status, 200);
    const body = await response.json() as any;
    assert.equal(body.issue_projection.status, "skipped");
    assert.equal(body.issue_projection.reason, SUPERSEDED_REASON);
    assert.equal(body.issue_projection.verified, false);
    assert.equal(writes(gh.log).length, baseline, "no write after the adoption was superseded");
  });
});

test("native adopt route: epoch advanced on the same slot while queued writes nothing further", async () => {
  const gh = pausableGh({ 8230: ["status:todo"] });
  await withRoute(gh, async (app, db) => {
    assert.equal((await send(app, NATIVE_ASSIGN(2), claim(8230))).status, 200);
    const baseline = writes(gh.log).length;
    const pause = gh.pauseNext("view", 8230);
    const pending = send(app, NATIVE_ADOPT(2), adoptBody(db.getSlot(2)!, 8230));
    await pause.reached;
    const epochBefore = db.getSlot(2)!.assignment_epoch;
    const released = db.releaseSlotSimple(2);
    db.assignSlot(2, "same", REPO, 8230, "fix/8230-other", null, HEAD, released.assignment_epoch,
      "implementation", "handoff-other", true);
    assert.ok(db.getSlot(2)!.assignment_epoch > epochBefore, "epoch advanced under the queued adoption");
    pause.resume();
    const body = await (await pending).json() as any;
    assert.equal(body.issue_projection.status, "skipped");
    assert.equal(body.issue_projection.verified, false);
    assert.equal(writes(gh.log).length, baseline);
  });
});
