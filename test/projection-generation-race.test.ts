import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { registerAssignmentEffectRoutes } from "../src/assignmentEffectRoutes.js";
import { MoPDatabase } from "../src/db.js";
import { pinFrom } from "../src/interruptBeforeClear.js";
import {
  SUPERSEDED_REASON,
  createGhIssueOwnershipProjection,
  type GhRunner,
} from "../src/issueProjection.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { applyLabelsApi } from "./gh-labels-api-stub.js";

const REPO = "github:heydonna-app/heydonna-app";
const HEAD = "a".repeat(40);

/** Fake gh with per-issue labels and a pausable seam on the Nth call of a kind. */
function pausableGh(initial: Record<number, string[]>) {
  const labels: Record<number, string[]> = Object.fromEntries(
    Object.entries(initial).map(([k, v]) => [Number(k), [...v]]),
  );
  const log: string[] = [];
  const holds: Array<{ kind: "view" | "post" | "delete"; issue: number; reached: () => void; release: Promise<void> }> = [];
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

function writes(log: string[]): string[] {
  return log.filter((entry) => !entry.startsWith("view:"));
}

test("projector paused at its read seam makes no write once ownership advances", async () => {
  const gh = pausableGh({ 50: ["status:todo"] });
  let current = true;
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner, repository: "Scribie/heydonna-app" });
  const pause = gh.pauseNext("view", 50);
  const old = projection.onAssigned(50, 1, REPO, () => current);
  await pause.reached;
  current = false; // owner/epoch advanced while the old plan waits on GitHub
  pause.resume();
  const out = await old;
  assert.equal(out.status, "skipped");
  assert.equal(out.reason, SUPERSEDED_REASON);
  assert.equal(out.verified, false);
  assert.deepEqual(writes(gh.log), []);
  assert.deepEqual(gh.labels[50], ["status:todo"]);
});

test("projector paused mid-write stops before the next write, reports no success, successor converges", async () => {
  const gh = pausableGh({ 51: ["status:todo", "P1"] });
  let current = true;
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner, repository: "Scribie/heydonna-app" });
  const pause = gh.pauseNext("post", 51);
  const old = projection.onAssigned(51, 1, REPO, () => current);
  await pause.reached; // POST (adds) in flight; the DELETE of status:todo is still to come
  current = false;
  // The issue moves to slot 2: its projection must queue behind the in-flight write.
  const successor = projection.onAssigned(51, 2, REPO);
  pause.resume();
  const oldOut = await old;
  assert.equal(oldOut.status, "skipped");
  assert.equal(oldOut.reason, SUPERSEDED_REASON);
  assert.equal(oldOut.verified, false);
  assert.ok(writes(gh.log).slice(0, 1)[0].startsWith("post:51"), "only the already-in-flight write happened");
  assert.deepEqual(oldOut.removed_labels, [], "the old plan issued no further write after being superseded");
  const succOut = await successor;
  assert.equal(succOut.status, "projected");
  assert.equal(succOut.verified, true);
  assert.deepEqual([...gh.labels[51]].sort(), ["P1", "slot:2", "status:in-progress"]);
});

test("projector paused at readback never certifies a superseded plan", async () => {
  const gh = pausableGh({ 52: ["status:todo"] });
  let current = true;
  const projection = createGhIssueOwnershipProjection({ runGh: gh.runner, repository: "Scribie/heydonna-app" });
  // First view is the plan read; pause the SECOND view (readback) by pausing after the writes.
  const first = gh.pauseNext("delete", 52);
  const old = projection.onAssigned(52, 1, REPO, () => current);
  await first.reached;
  const readback = gh.pauseNext("view", 52);
  first.resume();
  await readback.reached;
  current = false;
  readback.resume();
  const out = await old;
  assert.equal(out.status, "skipped");
  assert.equal(out.reason, SUPERSEDED_REASON);
  assert.equal(out.verified, false);
});

function routeHarness(gh: ReturnType<typeof pausableGh>) {
  const directory = mkdtempSync(join(tmpdir(), "mop-gen-race-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const app = new Hono();
  registerAssignmentEffectRoutes(app, {
    db,
    clearSlot: async () => ({ ok: true, reason: "cleared" }),
    interruptTurn: async (slot) => ({
      ok: true,
      reason: "interrupt_sent",
      pin: pinFrom({ paneId: "%1", checkout: "/fake" }, { ...db.getSlot(slot)!, session_id: null }),
    }),
    observeWorktree: async () => ({ clean: true, detail: "clean" }),
    issueProjection: createGhIssueOwnershipProjection({ runGh: gh.runner, repository: "Scribie/heydonna-app" }),
    deliverTaskFile: async (slot) => ({ verified: true, receipt: { slot, pane: "%1", mode: "file", verified: true } }),
  });
  return { db, app, close: () => { db.close(); rmSync(directory, { recursive: true, force: true }); } };
}

function body(slot: number, epoch: number, issue: number): Record<string, unknown> {
  return {
    effect_id: `assign-${slot}-${issue}`,
    selection_class: "new_issue",
    expected_epoch: epoch,
    repository_id: REPO,
    issue,
    pr: null,
    branch: `fix/${issue}-race`,
    head_sha: HEAD,
    work_kind: "implementation",
    handoff_id: `handoff-${issue}`,
    claimed_at: "2026-09-21T15:00:00.000Z",
    task: "task\n",
    task_file: "/tmp/task.md",
  };
}

async function post(app: Hono, slot: number, payload: Record<string, unknown>) {
  const response = await app.request(`http://mop/slots/${slot}/assign-effect`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return { status: response.status, json: (await response.json()) as any };
}

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 20));

test("assign-effect detached projection paused at gh read, issue moved to another slot: old work writes nothing", async () => {
  const gh = pausableGh({ 8110: ["status:todo", "P2"] });
  const h = routeHarness(gh);
  try {
    const pause = gh.pauseNext("view", 8110);
    const first = await post(h.app, 3, body(3, h.db.getSlot(3)!.assignment_epoch, 8110));
    assert.equal(first.status, 200);
    assert.equal(first.json.issue_projection.status, "pending", "response stays nonblocking");
    await pause.reached;

    // Owner advances: slot 3 is released and the same issue is claimed by slot 4.
    assert.equal(h.db.releaseSlotSimple(3).ok, true);
    const second = await post(h.app, 4, body(4, h.db.getSlot(4)!.assignment_epoch, 8110));
    assert.equal(second.status, 200);
    await tick();
    assert.deepEqual(writes(gh.log), [], "successor queues behind the paused old work; nothing written yet");

    pause.resume();
    for (let i = 0; i < 50 && !gh.labels[8110].includes("slot:4"); i += 1) await tick();

    assert.ok(!writes(gh.log).some((e) => e.includes("slot:3")), "no write for the superseded slot");
    assert.deepEqual([...gh.labels[8110]].sort(), ["P2", "slot:4", "status:in-progress"]);
    const oldReceipt = JSON.parse(h.db.getAssignmentEffectIntent("assign-3-8110")!.delivery_receipt as string);
    assert.equal(oldReceipt.issue_projection.status, "skipped");
    assert.equal(oldReceipt.issue_projection.reason, SUPERSEDED_REASON);
    assert.notEqual(oldReceipt.issue_projection.status, "projected", "no stale success");
  } finally {
    h.close();
  }
});

test("assign-effect detached projection paused at gh read, epoch advanced on the same slot: old work writes nothing", async () => {
  const gh = pausableGh({ 8111: ["status:todo"], 9001: ["status:todo"] });
  const h = routeHarness(gh);
  try {
    const pause = gh.pauseNext("view", 8111);
    const first = await post(h.app, 3, body(3, h.db.getSlot(3)!.assignment_epoch, 8111));
    assert.equal(first.status, 200);
    await pause.reached;
    const released = h.db.releaseSlotSimple(3);
    const re = h.db.assignSlot(3, "replacement", REPO, 9001, "fix/9001-r", null, HEAD, released.assignment_epoch,
      "implementation", "handoff-9001", true);
    assert.equal(re.ok, true);
    pause.resume();
    await tick();
    await tick();
    assert.deepEqual(writes(gh.log), [], "stale plan for the old epoch writes nothing");
    assert.deepEqual(gh.labels[8111], ["status:todo"]);
    const receipt = JSON.parse(h.db.getAssignmentEffectIntent("assign-3-8111")!.delivery_receipt as string);
    assert.equal(receipt.issue_projection.status, "skipped");
  } finally {
    h.close();
  }
});
