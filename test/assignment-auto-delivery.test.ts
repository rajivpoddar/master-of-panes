import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";

import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import { MoPDatabase } from "../src/db.js";
import { TmuxRelay } from "../src/relay.js";
import type { MoPConfig } from "../src/types.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function request(issue: number, task: string): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      issue,
      repository_id: "github:heydonna-app/heydonna-app",
      task,
    }),
  };
}

function completeRequest(overrides: Record<string, unknown> = {}): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      expected_epoch: 0,
      issue: 7616,
      repository_id: "github:heydonna-app/heydonna-app",
      pr: 7624,
      branch: "fix/7624-proof",
      head_sha: "a".repeat(40),
      work_kind: "repro",
      handoff_id: "handoff-7624",
      task: "REPRODUCTION\nexact full-head evidence",
      ...overrides,
    }),
  };
}

async function withRoute(
  deliver: (slot: number, task: string) => Promise<boolean>,
  run: (app: Hono, db: MoPDatabase) => Promise<void>,
): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-assignment-delivery-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 6,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  const app = new Hono();
  registerAssignmentRoute(app, db, deliver);
  try {
    await run(app, db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

test("one native assignment delivers the exact multiline task once", async () => {
  const deliveries: Array<[number, string]> = [];
  await withRoute(async (slot, task) => {
    deliveries.push([slot, task]);
    return true;
  }, async (app) => {
    const task = "NEW ISSUE HANDOFF\nissue contract\nworkflow chain";
    const response = await app.request("/slots/1/assign", request(7616, task));
    assert.equal(response.status, 200);
    const result = await response.json() as Record<string, unknown>;
    assert.equal(result.success, true);
    assert.equal(result.assigned, true);
    assert.equal(result.delivery_verified, true);
    assert.equal(result.assignment_epoch, 1);
    assert.deepEqual(deliveries, [[1, task]]);
  });
});

test("occupied and exact replay never deliver a second task", async () => {
  let deliveryCount = 0;
  await withRoute(async () => {
    deliveryCount += 1;
    return true;
  }, async (app) => {
    const first = await app.request("/slots/1/assign", request(7616, "first"));
    assert.equal(first.status, 200);
    const occupied = await app.request("/slots/1/assign", request(7617, "different"));
    assert.equal(occupied.status, 409);
    const replay = await app.request("/slots/1/assign", request(7616, "first"));
    assert.equal(replay.status, 409);
    assert.equal(deliveryCount, 1);
  });
});

test("delivery failure is explicit and never retried", async () => {
  let deliveryCount = 0;
  await withRoute(async () => {
    deliveryCount += 1;
    return false;
  }, async (app) => {
    const response = await app.request("/slots/1/assign", request(7616, "no false success"));
    assert.equal(response.status, 502);
    const result = await response.json() as Record<string, unknown>;
    assert.equal(result.reason, "assignment_delivery_failed");
    assert.equal((result.slot as Record<string, unknown>).occupied, true);
    assert.equal(deliveryCount, 1);
  });
});

test("PR-bound assignment writes the complete slot identity consumed by the heartbeat", async () => {
  const deliveries: string[] = [];
  await withRoute(async (_slot, task) => {
    deliveries.push(task);
    return true;
  }, async (app, db) => {
    const response = await app.request("/slots/1/assign", completeRequest());
    assert.equal(response.status, 200);
    const result = await response.json() as Record<string, unknown>;
    assert.equal(result.assignment_epoch, 1);
    assert.equal(result.pr, 7624);
    assert.equal(result.head_sha, "a".repeat(40));
    assert.equal(result.branch, "fix/7624-proof");
    assert.equal(result.branch_ref, "refs/heads/fix/7624-proof");
    assert.equal(result.work_kind, "repro");
    assert.equal(result.handoff_id, "handoff-7624");
    assert.deepEqual(deliveries, ["REPRODUCTION\nexact full-head evidence"]);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
    assert.equal(db.getSlot(1)?.pr, 7624);
    assert.equal(db.getSlot(1)?.head_sha, "a".repeat(40));
  });
});

test("a partial PR identity cannot fall back to a headless assignment", async () => {
  await withRoute(async () => true, async (app, db) => {
    const response = await app.request("/slots/1/assign", completeRequest({ head_sha: undefined }));
    assert.equal(response.status, 400);
    assert.equal((await response.json() as Record<string, unknown>).reason, "invalid_assignment_metadata");
    assert.equal(db.getSlot(1)?.occupied, false);
  });
});

test("assignment auto-delivery uses the real one-shot relay boundary", async () => {
  const serverSource = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.match(serverSource, /registerAssignmentRoute\(app, db, \(slot, task\) => relay\.sendToSlotOnceAsync\(slot, task\)\)/);
  assert.doesNotMatch(serverSource, /registerAssignmentRoute\(app, db, \(slot, task\) => relay\.sendToSlotAsync/);

  const failureModes = ["identity", "load", "paste", "submit", "none"] as const;
  for (const failure of failureModes) {
    const directory = mkdtempSync(join(tmpdir(), "mop-assignment-one-shot-"));
    const config: MoPConfig = {
      ...DEFAULT_CONFIG,
      httpPort: 0,
      dbPath: join(directory, "mop.db"),
    };
    const commands: string[] = [];
    const loadedBytes: Buffer[] = [];
    let expectedTask = "";
    const relay = new TmuxRelay(config, {
      runShell: async (command) => {
        commands.push(command);
        if (failure === "identity" && command.startsWith("tmux display-message")) {
          throw new Error("identity unavailable");
        }
        if (command.startsWith("tmux display-message")) {
          return { stdout: "%42|/Users/rajiv/Downloads/projects/heydonna-app-3001\n", stderr: "" };
        }
        if (command.startsWith("git -C")) {
          return { stdout: "/Users/rajiv/Downloads/projects/heydonna-app-3001\n", stderr: "" };
        }
        if (command.includes("tmux load-buffer")) {
          const filePath = /'([^']+)'$/.exec(command)?.[1];
          if (filePath) loadedBytes.push(readFileSync(filePath));
          if (failure === "load") throw new Error("load failed");
        }
        if (failure === "paste" && command.includes("tmux paste-buffer")) throw new Error("paste failed");
        if (failure === "submit" && command.includes("tmux send-keys") && command.endsWith(" Enter")) {
          throw new Error("submit failed");
        }
        return { stdout: "", stderr: "" };
      },
    });
    const db = new MoPDatabase(config);
    const app = new Hono();
    registerAssignmentRoute(app, db, (slot, task) => relay.sendToSlotOnceAsync(slot, task));
    try {
      const task = "ASSIGNMENT CANARY\nexact multiline bytes";
      expectedTask = task;
      const response = await app.request("/slots/1/assign", request(7616, task));
      const expectedStatus = failure === "none" ? 200 : 502;
      assert.equal(response.status, expectedStatus, failure);
      const loadCount = commands.filter((command) => command.includes("tmux load-buffer")).length;
      const pasteCount = commands.filter((command) => command.includes("tmux paste-buffer")).length;
      const submitCount = commands.filter((command) => command.includes("tmux send-keys") && command.endsWith(" Enter")).length;
      assert.ok(loadCount <= 1, `${failure}: load count ${loadCount}`);
      assert.ok(pasteCount <= 1, `${failure}: paste count ${pasteCount}`);
      assert.ok(submitCount <= 1, `${failure}: submit count ${submitCount}`);
      if (failure === "none") {
        assert.equal(loadCount, 1);
        assert.equal(pasteCount, 1);
        assert.equal(submitCount, 1);
        assert.deepEqual(loadedBytes, [Buffer.from(expectedTask)]);
        assert.ok(commands.some((command) => command.includes("tmux paste-buffer") && command.includes("-t %42")));
        assert.ok(commands.some((command) => command.includes("tmux send-keys -t %42 Enter")));
      }
    } finally {
      db.close();
      rmSync(directory, { recursive: true, force: true });
    }
  }
});
