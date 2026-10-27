import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { lifecycleEmailApproved, lifecycleEmailParts, isLifecycleEmailWindow, PMCadenceScheduler } from "../src/pmCadence.js";

test("lifecycle cadence observes weekday 10 ET including DST", () => {
  assert.equal(isLifecycleEmailWindow(new Date("2026-10-27T13:59:00Z")), false);
  assert.equal(isLifecycleEmailWindow(new Date("2026-10-27T14:00:00Z")), true);
  assert.equal(isLifecycleEmailWindow(new Date("2026-10-31T16:00:00Z")), false);
  assert.equal(isLifecycleEmailWindow(new Date("2026-11-02T14:59:00Z")), false);
  assert.equal(isLifecycleEmailWindow(new Date("2026-11-02T15:00:00Z")), true);
  assert.equal(lifecycleEmailParts(new Date("2026-10-28T01:00:00Z")).day, "2026-10-27-ET");
});

test("every template must be approved, missing and draft fail closed", () => {
  const root = mkdtempSync(join(tmpdir(), "mop-email-approval-"));
  const touches = ["D0", "D1", "D3", "D7", "FIRST_FILE_READY"];
  assert.equal(lifecycleEmailApproved(root), false);
  for (const touch of touches) writeFileSync(join(root, `${touch}.txt`), "STATUS: APPROVED (Rajiv cited)\n");
  assert.equal(lifecycleEmailApproved(root), true);
  for (const touch of touches) {
    writeFileSync(join(root, `${touch}.txt`), "STATUS: DRAFT_PENDING_RAJIV\n");
    assert.equal(lifecycleEmailApproved(root), false, touch);
    writeFileSync(join(root, `${touch}.txt`), "STATUS: APPROVED\n");
  }
});

test("lifecycle task is registered in the existing scheduler", () => {
  const db = { getConfig: () => null };
  const scheduler = new PMCadenceScheduler(db as never, {} as never);
  const task = scheduler.getStatus().tasks.find(t => t.task === "lifecycle-email");
  assert.ok(task);
  assert.match(task.command, /Skill\(lifecycle-email-processing\)/);
  assert.match(task.command, /send_allowed/);
});
