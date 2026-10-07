import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { touchPmClearRequestedMarker } from "../src/pmClearLatch.js";

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
