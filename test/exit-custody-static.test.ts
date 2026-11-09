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
  assert.ok(relay.includes('if (exactDraft === command && physicalRefusal() === null)'));
  assert.ok(relay.includes('removed = await owns() && composerText(await capture()) === ""'));
  const inject = relay.slice(relay.indexOf('private async injectDirectNow'), relay.indexOf('private async injectDirectNow') + 6000);
  assert.ok(inject.indexOf('pm_exit_cleanup_pending') < inject.indexOf('tmux paste-buffer'));
  assert.ok(inject.includes('return { ok: false, ambiguous: false, notReady: true }'));
  assert.ok(relay.indexOf('enterSent++;') < relay.indexOf('await this.runShell(`tmux send-keys -t ${paneId} Enter`', relay.indexOf('enterSent++;')));
});

const health = readFileSync(new URL("../src/health.ts", import.meta.url), "utf8");
test("replacement credit requires launch and owned boot provenance; delayed evidence is recoverable", () => {
  const observer = relay.slice(relay.indexOf("async observeExitAttempts"), relay.indexOf("private scheduleExitConfirmation"));
  assert.ok(observer.includes("receipt.bootEventId > receipt.launchEventId"));
  assert.ok(observer.includes("receipt.bootSession"));
  assert.ok(observer.includes("delayedReceiptRetention"));
  assert.ok(!observer.includes('if (!bound) { db.retainExitSlot'));
  const pm = health.slice(health.indexOf("// 3. Claude input prompt"), health.indexOf("// 4. ONE continuation"));
  assert.ok(pm.indexOf('launch_boot_process_unowned') < pm.indexOf('"exit_replacement_bound"'));
  assert.ok(health.includes('boots.length !== 1'));
  assert.ok(health.includes('ownedAgain !== ownedProcess'));
});
test("changed draft after awaited identity check is recaptured before synchronous cleanup fence", () => {
  const cleanup = relay.slice(relay.indexOf('const physicalRefusal ='), relay.indexOf('if (attempt && this.db && attempt.phase === "dispatching")'));
  assert.ok(cleanup.indexOf('if (await owns()) {', cleanup.indexOf('else if')) < cleanup.indexOf('const exactDraft'));
  const edge = cleanup.slice(cleanup.indexOf('const exactDraft'), cleanup.indexOf('C-u'));
  assert.ok(edge.includes('physicalRefusal() === null'));
  assert.ok(!edge.includes('await owns()'));
});
test("normal inbound cancels Enter but does not revoke physical paste cleanup ownership", () => {
  assert.ok(relay.includes('!physicalPaste && slotNum === 0 && pin.inboundGeneration !== this.pmInboundGeneration'));
  assert.ok(relay.includes('this.exitOwnerRefusal(slotNum, opts.exitOwnerPin, true)'));
  assert.ok(relay.includes('command === "/exit" && effectAttempted && enterSent === 0'));
  assert.ok(relay.includes('slotNum === 0 && removed'));
});
