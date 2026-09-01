import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const root = resolve(import.meta.dirname, "..");
const source = (file: string) => readFileSync(resolve(root, file), "utf8");

test("core keeps native assignment/release/message surfaces while orchestration is absent", () => {
  const server = source("src/server.ts");
  const mcp = source("src/mcp.ts");
  const manifest = JSON.parse(source("scripts/pm/shared-assets/manifest.json")) as {
    entries: Array<{ source_path: string; canonical_target: string }>;
  };

  for (const token of [
    "StuckDetector", "OpsAuditScheduler", "PMCadenceScheduler",
    "P0EscalationWatcher", "Family2ReleaseEffect", "/approve-plan",
    "/pm-status", "/ops-audit", "/pm-cadence", "/p0-escalation",
  ]) {
    assert.equal(server.includes(token) || mcp.includes(token), false, token);
  }
  for (const token of ["/slots/:slotNum/send", "/slots/:slotNum/assign", "/slots/:slotNum/release"]) {
    assert.equal(server.includes(token) || source("src/assignmentRoute.ts").includes(token) || source("src/family2Routes.ts").includes(token), true, token);
  }
  for (const tool of ["mop_slot_status", "mop_all_slots", "mop_send_to_slot", "mop_release_slot"]) {
    assert.equal(mcp.includes(`\"${tool}\"`), true, tool);
  }
  for (const entry of manifest.entries) {
    assert.equal(entry.source_path.includes("pm/"), false, entry.source_path);
    assert.equal(entry.canonical_target.includes("pm-operator"), false, entry.canonical_target);
    assert.equal(entry.canonical_target.includes("control-plane"), false, entry.canonical_target);
  }
  for (const retired of ["src/stuck.ts", "src/opsAudit.ts", "src/pmCadence.ts", "src/p0EscalationWatch.ts", "src/family2ReleaseEffect.ts"]) {
    assert.equal(existsSync(resolve(root, retired)), false, retired);
  }
});
