import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const relay = readFileSync(new URL("../src/relay.ts", import.meta.url), "utf8");
const db = readFileSync(new URL("../src/db.ts", import.meta.url), "utf8");
test("UNKNOWN plus badge plus fresh Stop stays consumed without positive dialog evidence", () => {
  const observer = relay.slice(relay.indexOf("async observeExitAttempts"), relay.indexOf("private scheduleExitConfirmation"));
  assert.ok(!observer.includes('agents ? "blocked"'));
  assert.ok(observer.includes('attempt.phase = "unknown"'));
  assert.ok(db.includes('attempt.phase === "blocked" && attempt.blockedReason === "exit_dialog_observed"'));
  assert.ok(relay.includes('previous.inputEventId !=='));
});
test("owner-changed or unrelated replacement is retained, not cycled", () => {
  const observer = relay.slice(relay.indexOf("async observeExitAttempts"), relay.indexOf("private scheduleExitConfirmation"));
  assert.ok(observer.indexOf('if (!ownerMatches)') < observer.indexOf('db.markSlotExitCycled'));
  assert.ok(observer.indexOf('if (!bound)') < observer.indexOf('db.markSlotExitCycled'));
  assert.ok(observer.includes('replacement_not_owned_by_restart'));
});
test("failed cleanup preserves waiting inbound instead of joining its paste", () => {
  assert.ok(relay.includes('if (draft === command && await owns())'));
  assert.ok(relay.includes('removed = await owns() && composerText(await capture()) === ""'));
  const inject = relay.slice(relay.indexOf('private async injectDirectNow'), relay.indexOf('private async injectDirectNow') + 6000);
  assert.ok(inject.indexOf('pm_exit_cleanup_pending') < inject.indexOf('tmux paste-buffer'));
  assert.ok(inject.includes('return { ok: false, ambiguous: false, notReady: true }'));
  assert.ok(relay.indexOf('enterSent++;') < relay.indexOf('await this.runShell(`tmux send-keys -t ${paneId} Enter`', relay.indexOf('enterSent++;')));
});
