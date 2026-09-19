import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Hono } from "hono";
import { MoPDatabase } from "../src/db.js";
import { registerAssignmentRoute } from "../src/assignmentRoute.js";
import {
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "../src/assignmentAuthority.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import {
  setPaneCheckExecutor,
  type PaneCheckResult,
} from "../src/sessionDelivery.js";

function withApp(run: (app: Hono, db: MoPDatabase) => Promise<void>): Promise<void> {
  const directory = mkdtempSync(join(tmpdir(), "mop-assign-delivery-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const app = new Hono();
  registerAssignmentRoute(app, db);
  return run(app, db).finally(() => {
    setPaneCheckExecutor(null);
    db.close();
    rmSync(directory, { recursive: true, force: true });
  });
}

const headers = {
  "content-type": "application/json",
  [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY,
};

function issueBody(extra: Record<string, unknown> = {}) {
  return JSON.stringify({
    task: "Assign wedge fixture",
    repository_id: "github:heydonna-app/heydonna-app",
    issue: 7916,
    ...extra,
  });
}

test("blocked session delivery fails typed with no epoch advance", async () => {
  const dead: PaneCheckResult = { state: "dead" };
  await withApp(async (app, db) => {
    setPaneCheckExecutor(async () => dead);
    const before = db.getSlot(2)?.assignment_epoch ?? 0;
    const res = await app.request("/slots/2/assign", {
      method: "POST",
      headers,
      body: issueBody({ delivery: { pane_id: "0:0.2", transport: "tmux-send-keys" } }),
    });
    assert.equal(res.status, 409);
    const payload = (await res.json()) as Record<string, unknown>;
    assert.equal(payload["success"], false);
    assert.equal(payload["reason"], "session_delivery_unverified");
    assert.equal(db.getSlot(2)?.assignment_epoch ?? 0, before);
    assert.equal(db.getSlot(2)?.occupied ?? false, false);
  });
});

test("live session delivery succeeds and names the verified pane", async () => {
  await withApp(async (app, db) => {
    setPaneCheckExecutor(async (target) => ({ state: "live", paneId: target }));
    const before = db.getSlot(1)?.assignment_epoch ?? 0;
    const res = await app.request("/slots/1/assign", {
      method: "POST",
      headers,
      body: issueBody({
        delivery: { pane_id: "0:0.1", transport: "tmux-send-keys", packet_sha256: "a".repeat(64) },
      }),
    });
    assert.equal(res.status, 200);
    const payload = (await res.json()) as Record<string, unknown>;
    assert.equal(payload["occupied"], true);
    const delivery = payload["session_delivery"] as Record<string, unknown>;
    assert.equal(delivery["verified"], true);
    assert.equal(delivery["attested"], true);
    assert.equal(delivery["pane"], "0:0.1");
    assert.equal(db.getSlot(1)?.assignment_epoch ?? 0, before + 1);
  });
});

test("unattested assign to a live pane preserves success behavior", async () => {
  await withApp(async (app, db) => {
    setPaneCheckExecutor(async (target) => ({ state: "live", paneId: target }));
    const res = await app.request("/slots/3/assign", { method: "POST", headers, body: issueBody() });
    assert.equal(res.status, 200);
    const payload = (await res.json()) as Record<string, unknown>;
    assert.equal(payload["occupied"], true);
    const delivery = payload["session_delivery"] as Record<string, unknown>;
    assert.equal(delivery["verified"], true);
    assert.equal(delivery["attested"], false);
  });
});

test("invalid pane target fails typed with no mutation", async () => {
  await withApp(async (app, db) => {
    setPaneCheckExecutor(async (target) => ({ state: "live", paneId: target }));
    const before = db.getSlot(4)?.assignment_epoch ?? 0;
    const res = await app.request("/slots/4/assign", {
      method: "POST",
      headers,
      body: issueBody({ delivery: { pane_id: "0:0.4; rm -rf /" } }),
    });
    assert.equal(res.status, 409);
    const payload = (await res.json()) as Record<string, unknown>;
    assert.equal(payload["reason"], "session_delivery_unverified");
    assert.equal(db.getSlot(4)?.assignment_epoch ?? 0, before);
  });
});

test("unavailable pane checker degrades with an explicit marker, not silence", async () => {
  await withApp(async (app, db) => {
    setPaneCheckExecutor(async () => ({ state: "unknown", reason: "tmux_unavailable" }));
    const res = await app.request("/slots/5/assign", { method: "POST", headers, body: issueBody() });
    assert.equal(res.status, 200);
    const payload = (await res.json()) as Record<string, unknown>;
    assert.equal(payload["occupied"], true);
    const delivery = payload["session_delivery"] as Record<string, unknown>;
    assert.equal(delivery["verified"], false);
    assert.equal(delivery["reason"], "pane_check_unavailable");
  });
});
