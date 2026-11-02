import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync, statSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { recordPmClearFailureProof, requestPmClearOnce, touchPmClearRequestedMarker } from "../src/pmClearLatch.js";

test("PM clear request touches the self-clear Stop marker only while a clear is pending", () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-clear-marker-"));
  try {
    const marker = join(directory, "pm-self-clear-requested");
    assert.equal(touchPmClearRequestedMarker("failed", marker), false);
    assert.equal(touchPmClearRequestedMarker("recent", marker), false);
    assert.equal(existsSync(marker), false);
    for (const kind of ["sent", "deferred_busy", "pending"] as const) {
      assert.equal(touchPmClearRequestedMarker(kind, marker), true);
      assert.ok(Date.now() - statSync(marker).mtimeMs < 5_000);
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});


test("terminal failure emits a typed event and replaces deferred proof files", async () => {
  const { MoPDatabase } = await import("../src/db.js");
  const { DEFAULT_CONFIG } = await import("../src/types.js");
  const directory = mkdtempSync(join(tmpdir(), "mop-clear-failed-proof-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    const paths = [join(directory, "proof.json"), join(directory, "marker")];
    for (const path of paths) writeFileSync(path, JSON.stringify({ results: [{ slot: 0, status: "deferred" }] }));
    const result = await requestPmClearOnce({
      db, source: "test", nowMs: Date.now(), staleAfterMs: 0, recentSuppressMs: 0,
      hasRecentClearEvent: false, laterLifecycleEvent: null, isPMBusy: () => false,
      send: async () => ({ success: false, zeroEffect: true, error: "pm_pane_identity_drift" }),
      onTerminalFailure: (failure) => recordPmClearFailureProof(failure, paths),
    });
    assert.equal(result.kind, "failed");
    assert.equal(db.hasPendingClear(0), false);
    assert.equal(db.getConfig("pm_clear_delivery_state"), "failed");
    assert.equal(db.getEvents(0, 10, "pm_clear_delivery_failed").length, 1);
    for (const path of paths) {
      const text = readFileSync(path, "utf8");
      assert.equal(JSON.parse(text).status, "failed");
      assert.match(text, /MoP delivery failed, re-request/);
      assert.doesNotMatch(text, /deferred/);
    }
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
