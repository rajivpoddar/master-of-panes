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
test("a saved PM handoff stays owned despite later UserPromptSubmit events (poll events during exit)", () => {
  const observer = relay.slice(relay.indexOf("async observeExitAttempts"), relay.indexOf("private scheduleExitConfirmation"));
  assert.ok(observer.includes("const savedPmHandoff = slot === 0 && pin?.savedPmHandoff === true;"));
  assert.ok(observer.includes("(savedPmHandoff || attempt.inputEventId ===") , "input equality applies only without a saved handoff");
  assert.ok(observer.indexOf("savedPmHandoff") < observer.indexOf("if (!ownerMatches)"));
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

test("an unsubmitted PM continuation draft is held and cleaned only on the exact owned draft, inside the send lock (CTO REVISE ts 1794950616.405579)", () => {
  const start = relay.indexOf("private async reconcileUnsubmittedContinuationDraft");
  const body = relay.slice(start, relay.indexOf("Check if a slot is currently active", start));
  // The hold is written FIRST and released only after the draft is proven gone.
  assert.ok(body.indexOf('setConfig("pm_exit_cleanup_pending", JSON.stringify') < body.indexOf("clearOwnedRefusedPasteComposer("));
  assert.ok(body.includes('if (removed) this.db?.setConfig("pm_exit_cleanup_pending", "")'));
  // Exact owned draft only, with awaited identity proof, a fresh read and a synchronous boot fence before each key.
  assert.ok(body.includes("composerOwnsOnlyPayload(first, payload"));
  assert.ok(body.indexOf("if (!(await owns())) return") < body.lastIndexOf("draft.sameBoot()"));
  assert.ok(body.lastIndexOf("draft.sameBoot()") < body.indexOf("C-u"), "the last fence is synchronous and right before the key");
  // EVERY key (CTO REVISE ts 1794951624.218829): the composer is re-read AFTER the ownership await and must equal the text
  // the helper last verified as owned, in the same callback that sends the key (no per-first-press special case).
  const keyCb = body.slice(body.indexOf("clearComposer: async (expectedComposer)"), body.indexOf("sleep,", body.indexOf("clearComposer: async (expectedComposer)")));
  assert.ok(keyCb.indexOf("await owns()") < keyCb.indexOf("composerText(await capture()) !== expectedComposer"));
  assert.ok(keyCb.indexOf("!== expectedComposer") < keyCb.indexOf("draft.sameBoot()"));
  assert.ok(keyCb.indexOf("draft.sameBoot()") < keyCb.indexOf("C-u"));
  assert.ok(!keyCb.includes("presses === 0"), "no first-press-only composer recheck");
  // Called from the guarded writer's finally (same lock), never after an Enter, never for /exit (its own block).
  const call = relay.indexOf("this.reconcileUnsubmittedContinuationDraft(paneId, command");
  assert.ok(relay.includes('slotNum === 0 && command !== "/exit" && opts.continuationDraft && effectAttempted'));
  assert.ok(relay.indexOf("if (enterSent === 0) {", relay.indexOf("opts.continuationDraft && effectAttempted")) < call);
  assert.ok(call > relay.indexOf('command === "/exit" && effectAttempted && enterSent === 0'));
  assert.ok(call < relay.indexOf("await fs.unlink(tmpFile)", call));
  // The consumed attempt is never reset here and the continuation is never resent.
  assert.ok(!body.includes("exit_pending_continuation_delivery"));
  assert.ok(!body.includes("Enter"));
});
test("an unresolved continuation Enter holds automated paste only: no clearing key, the consumed attempt stays consumed (CTO REVISE ts 1794951624.218829)", () => {
  const branch = relay.slice(relay.indexOf("} else if (!deliveryConfirmed) {"), relay.indexOf("await fs.unlink(tmpFile)", relay.indexOf("} else if (!deliveryConfirmed) {")));
  assert.ok(branch.includes("this.holdUnresolvedContinuationEnter(paneId)"));
  assert.ok(!branch.includes("C-u") && !branch.includes("reconcileUnsubmittedContinuationDraft") && !branch.includes("runShell"));
  const hold = relay.slice(relay.indexOf("private holdUnresolvedContinuationEnter"), relay.indexOf("private async reconcileUnsubmittedContinuationDraft"));
  assert.ok(hold.includes('setConfig("pm_exit_cleanup_pending", JSON.stringify'));
  assert.ok(!hold.includes("C-u") && !hold.includes("send-keys") && !hold.includes("exit_pending_continuation_delivery"));
  // Only an unconfirmed delivery holds: the flag is set exactly where the send is confirmed.
  assert.ok(relay.includes("deliveryConfirmed = true;"));
});
test("continuation draft ownership is physical (same boot + owned process), never the input fence", () => {
  const owner = health.slice(health.indexOf("const sameBoot ="), health.indexOf("const send = () => this.relay.deliverGuardedToSlot(0, prompt"));
  assert.ok(owner.includes("continuationDraft"));
  assert.ok(owner.includes("latestSessionStartEventId(this.db, 0) === bootId"));
  assert.ok(owner.includes("ProcessHealthChecker.ownedAgent(await this.pmAgentProcess(paneId))"));
  assert.ok(!owner.includes("inputFence") && !owner.includes("continuationInputRefusal"), "inbound/real input cancels the Enter, not ownership");
});
