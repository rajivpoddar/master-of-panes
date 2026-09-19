import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
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
  verifySessionPane,
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

/**
 * Runs `run` with a controlled `tmux` on PATH so the REAL checker path
 * (execFile argv + error mapping + output parsing) is exercised end to end.
 * Modes mirror observed tmux behaviour: "empty" is the exit-0 empty-expansion
 * shape seen for a syntactically valid but nonexistent target, "blank" is a
 * truly empty stdout, "dead" is pane_dead=1, "live" is a healthy pane, and
 * "absent" removes tmux entirely (execFile ENOENT).
 */
async function withFakeTmux<T>(
  mode: "empty" | "blank" | "dead" | "live" | "absent",
  run: () => Promise<T>,
): Promise<T> {
  const directory = mkdtempSync(join(tmpdir(), "mop-fake-tmux-"));
  const bin = join(directory, "bin");
  mkdirSync(bin);
  const originalPath = process.env["PATH"] ?? "";
  const originalMode = process.env["MOP_FAKE_TMUX_MODE"];
  try {
    if (mode !== "absent") {
      const script = join(bin, "tmux");
      writeFileSync(script, [
        "#!/bin/sh",
        'case "${MOP_FAKE_TMUX_MODE:-live}" in',
        '  empty) printf "%s" "|" ;;',
        '  blank) printf "%s" "" ;;',
        '  dead) printf "%s" "%5|1" ;;',
        '  live) printf "%s" "%2|0" ;;',
        "esac",
        "exit 0",
        "",
      ].join("\n"));
      chmodSync(script, 0o755);
      process.env["MOP_FAKE_TMUX_MODE"] = mode;
    }
    process.env["PATH"] = mode === "absent" ? bin : `${bin}:${originalPath}`;
    return await run();
  } finally {
    process.env["PATH"] = originalPath;
    if (originalMode === undefined) {
      delete process.env["MOP_FAKE_TMUX_MODE"];
    } else {
      process.env["MOP_FAKE_TMUX_MODE"] = originalMode;
    }
    rmSync(directory, { recursive: true, force: true });
  }
}

test("checker: exit-0 empty tmux output is positive target absence, not unknown", async () => {
  setPaneCheckExecutor(null);
  await withFakeTmux("empty", async () => {
    assert.deepEqual(await verifySessionPane("77:0.0"), { state: "dead" });
  });
  await withFakeTmux("blank", async () => {
    assert.deepEqual(await verifySessionPane("77:0.0"), { state: "dead" });
  });
});

test("checker: pane_dead=1 is dead and healthy output reports the pane identity", async () => {
  setPaneCheckExecutor(null);
  await withFakeTmux("dead", async () => {
    assert.deepEqual(await verifySessionPane("0:0.6"), { state: "dead" });
  });
  await withFakeTmux("live", async () => {
    assert.deepEqual(await verifySessionPane("0:0.6"), { state: "live", paneId: "%2" });
  });
});

test("checker: genuinely unavailable tmux stays indeterminate, never dead", async () => {
  setPaneCheckExecutor(null);
  await withFakeTmux("absent", async () => {
    assert.deepEqual(await verifySessionPane("0:0.6"), {
      state: "unknown",
      reason: "tmux_unavailable",
    });
  });
});

test("route: missing pane (exit-0 empty output) refuses with no mutation and no event", async () => {
  setPaneCheckExecutor(null);
  await withFakeTmux("empty", async () => {
    await withApp(async (app, db) => {
      const before = db.getSlot(4)?.assignment_epoch ?? 0;
      const res = await app.request("/slots/4/assign", {
        method: "POST",
        headers,
        body: issueBody({ delivery: { pane_id: "77:0.0", transport: "tmux-send-keys" } }),
      });
      assert.equal(res.status, 409);
      const payload = (await res.json()) as Record<string, unknown>;
      assert.equal(payload["success"], false);
      assert.equal(payload["reason"], "session_delivery_unverified");
      assert.equal(payload["pane"], "77:0.0");
      assert.equal(db.getSlot(4)?.assignment_epoch ?? 0, before);
      assert.equal(db.getSlot(4)?.occupied ?? false, false);
      assert.equal(db.getEvents(4, 50, "slot_assigned").length, 0);
    });
  });
});

test("route: real pane_dead=1 refuses with no mutation and no event", async () => {
  setPaneCheckExecutor(null);
  await withFakeTmux("dead", async () => {
    await withApp(async (app, db) => {
      const before = db.getSlot(6)?.assignment_epoch ?? 0;
      const res = await app.request("/slots/6/assign", {
        method: "POST",
        headers,
        body: issueBody({ delivery: { pane_id: "0:0.6", transport: "tmux-send-keys" } }),
      });
      assert.equal(res.status, 409);
      const payload = (await res.json()) as Record<string, unknown>;
      assert.equal(payload["reason"], "session_delivery_unverified");
      assert.equal(db.getSlot(6)?.assignment_epoch ?? 0, before);
      assert.equal(db.getEvents(6, 50, "slot_assigned").length, 0);
    });
  });
});

test("route: healthy tmux output records a verified delivery", async () => {
  setPaneCheckExecutor(null);
  await withFakeTmux("live", async () => {
    await withApp(async (app, db) => {
      const before = db.getSlot(1)?.assignment_epoch ?? 0;
      const res = await app.request("/slots/1/assign", {
        method: "POST",
        headers,
        body: issueBody({
          delivery: { pane_id: "0:0.1", transport: "tmux-send-keys", packet_sha256: "b".repeat(64) },
        }),
      });
      assert.equal(res.status, 200);
      const payload = (await res.json()) as Record<string, unknown>;
      const delivery = payload["session_delivery"] as Record<string, unknown>;
      assert.equal(delivery["verified"], true);
      assert.equal(delivery["pane"], "0:0.1");
      assert.equal(delivery["attested"], true);
      assert.equal(db.getSlot(1)?.assignment_epoch ?? 0, before + 1);
      assert.equal(db.getEvents(1, 50, "slot_assigned").length, 1);
    });
  });
});

test("route: genuinely unavailable checker keeps the explicit degraded marker", async () => {
  setPaneCheckExecutor(null);
  await withFakeTmux("absent", async () => {
    await withApp(async (app, db) => {
      const before = db.getSlot(5)?.assignment_epoch ?? 0;
      const res = await app.request("/slots/5/assign", {
        method: "POST",
        headers,
        body: issueBody({ delivery: { pane_id: "0:0.5" } }),
      });
      assert.equal(res.status, 200);
      const payload = (await res.json()) as Record<string, unknown>;
      const delivery = payload["session_delivery"] as Record<string, unknown>;
      assert.equal(delivery["verified"], false);
      assert.equal(delivery["reason"], "pane_check_unavailable");
      assert.equal(db.getSlot(5)?.assignment_epoch ?? 0, before + 1);
    });
  });
});
