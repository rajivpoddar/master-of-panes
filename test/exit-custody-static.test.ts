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
test("continuation destination and draft ownership is the live replacement process in the launched pane, never a SessionStart row or the input fence (CTO REVISE ts 1795004139.496649, finding 1)", () => {
  const owner = health.slice(health.indexOf("let replaced = false;"), health.indexOf("const send = () => this.relay.deliverGuardedToSlot(0, prompt"));
  assert.ok(owner.includes("continuationDraft"));
  assert.ok(owner.includes("await this.pmAgentProcess(paneId)") && owner.includes("ProcessHealthChecker.ownedAgent(id)"));
  assert.ok(!owner.includes("latestSessionStartEventId"), "SessionStart stays evidence in the receipt only");
  assert.ok(!owner.includes("inputFence") && !owner.includes("continuationInputRefusal"), "inbound/real input cancels the Enter, not ownership");
  assert.ok(!health.includes("session_start_not_observed"), "no wait for a SessionStart row before the continuation");
});
test("PM restart effects are rechecked inside the send lock and recorded durably before they happen (CTO REVISE ts 1795004139.496649, finding 2)", () => {
  const menu = health.slice(health.indexOf("private async answerPMExitMenu"), health.indexOf("* Poll until the PM pane runs the agent"));
  assert.ok(menu.includes("withSlotSendLock(0") && menu.includes("this.visiblePane(paneId)") && menu.includes("this.sameEpisode(episode)"));
  assert.ok(menu.indexOf("this.markRestartStep(") < menu.indexOf("send-keys"), "the key is recorded before it is sent");
  assert.ok(!menu.includes("observeSlotPane"), "the in-lock menu read is the visible screen, not scrollback");
  const launch = health.slice(health.indexOf("private async sendPMLaunch"), health.indexOf("private isAgentCommand"));
  assert.ok(launch.includes("withSlotSendLock(0") && !launch.includes("isShellPromptVisible"), "the prompt-suffix heuristic never authorizes the send");
  // CTO REVISE C0ALZJHGE49 thread 1795715075.690009, ts 1795719337.686639: "replace the prompt-suffix heuristic at the
  // final send with evidence that the shell input is actually empty." Cursor + screen in one read, pinned across the delay.
  assert.ok(launch.includes("shellInputProvablyEmpty(evidence)") && launch.includes("samePromptCursor(anchor, evidence)"));
  // CTO REVISE ts 1795717248.453549: one owned launcher submission. The delay comes before the final guarded check, then
  // the exact --continue text and its Enter go in ONE tmux command list that the tmux server runs only if nothing moved.
  assert.ok(launch.includes("SHELL_COMMANDS.has(command)") && launch.includes("this.sameEpisode(episode)"), "the final check also proves the foreground is the shell");
  assert.ok(launch.indexOf("this.sleepMs(this.PM_LAUNCH_DELAY_MS)") < launch.indexOf("withSlotSendLock(0"), "the delay comes before the guarded check, never between text and Enter");
  const sends = launch.match(/tmux if-shell -F[^`]*/g) ?? [];
  assert.equal(sends.length, 1, "exactly one send in the launcher path");
  assert.equal((launch.match(/send-keys -t \$\{paneId\} -l/g) ?? []).length, 1, "the launcher text is typed in one place only");
  assert.ok(launch.includes("send-keys -t ${paneId} -l ${tmuxQuote(typed)} ; send-keys -t ${paneId} Enter ; display-message -p sent"),
    "the launcher text and its Enter travel together inside the atomic condition");
  assert.ok(launch.includes('stdout.trim() === "sent"'), "only the tmux server's own receipt counts as sent");
  assert.ok(launch.indexOf("this.markRestartStep(") < launch.indexOf("tmux if-shell"), "the launcher step is recorded before it is sent");
  assert.ok(!health.includes("submitPMLaunch") && !health.includes("PM_LAUNCH_ENTER_"), "no separate delayed Enter path remains");
  assert.ok(launch.includes("this.pmLaunchSubmitted(paneId, exactPM)"), "the process probe confirms only");
});
test("the next tick after a MoP restart settles a confirmed receipt and resumes only a positively identified unsent launch (CTO REVISE ts 1795004139.496649, finding 3)", () => {
  const resume = health.slice(health.indexOf("private async resumePMExitEpisode"), health.indexOf("// ─── Post-Restart Continue Injection"));
  assert.ok(resume.includes("this.continuationDeliveryRecorded(status.episode)") && resume.includes("this.finishPMContinuation(status)"));
  assert.ok(resume.includes('this.restartStepDone(status.episode, "launched")') && resume.includes("ProcessHealthChecker.ownedAgent(process)"));
  assert.ok(!resume.includes("setConfig") && !/getSlotActivityState|setSlotState|clearSlot|assignSlot/.test(resume), "no new gate or state on PM input, no dev slot");
});
