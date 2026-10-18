import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { DEFAULT_OPS_AUDIT_BG_TIMEOUT_MS } from "../src/opsAudit.js";

// Obligation 23162: the hourly audit's pr-state-sweep preflight needs 60-95s and the rest of
// input gathering 40-80s. The old 180s bound killed loaded runs as bg_script_timeout.
test("ops-audit bg-script default bound covers the measured preflight and gather time", () => {
  assert.ok(
    DEFAULT_OPS_AUDIT_BG_TIMEOUT_MS >= 300_000,
    `default bound ${DEFAULT_OPS_AUDIT_BG_TIMEOUT_MS}ms is below the 300s measured-worst-case floor`,
  );
});

test("ops-audit scheduler uses the exported default and keeps the env override", () => {
  const source = readFileSync(new URL("../src/opsAudit.ts", import.meta.url), "utf8");
  assert.match(
    source,
    /process\.env\.MOP_OPS_AUDIT_BG_TIMEOUT_MS \?\? `\$\{DEFAULT_OPS_AUDIT_BG_TIMEOUT_MS\}`/,
  );
});
