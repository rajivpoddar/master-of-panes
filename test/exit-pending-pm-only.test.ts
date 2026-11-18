import assert from "node:assert/strict";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { PM_RESTART_CONTINUATION_PROMPT, ProcessHealthChecker } from "../src/health.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const database = () => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });

// A flag alone is never proof of replacement: cycled[0] needs a VERIFIED exit attempt of the CURRENT episode. Call it after
// setExitPending(true, ...) (the episode id is generated there).
function markPmCycled(db: MoPDatabase) {
  db.saveExitAttempt(0, {
    episode: db.getExitStatus().episode!, paneId: "%40", originalClaudePid: 4000, originalStart: "Wed Nov 18 00:29:17 2026",
    originalProcess: '["%40",4000,"Wed Nov 18 00:29:17 2026"]', sessionId: "old", inputEventId: null, assignmentEpoch: 0,
    stopEventId: null, ownerPin: null, phase: "verified", attemptCount: 1, submittedAt: new Date().toISOString(),
    blockedReason: null, replacementProcess: '["%40",4001,"Wed Nov 18 08:18:49 2026"]',
  });
  db.markSlotExitCycled(0);
}

test("PM-only arming persists the prompt without resetting any dev flag; health scan sends nothing", async () => {
  const db = database();
  try {
    db.markSlotExitCycled(1);
    db.markSlotExitCycled(4);
    const before = db.getExitStatus().cycled;
    db.setExitPending(true, false, true, "Read the canonical ledger's continuation.\nResume PM.");
    assert.equal(db.getExitStatus().pm_only, true);
    assert.match(db.getExitStatus().continuation_prompt!, /ledger's/);
    for (let i = 1; i <= 8; i++) assert.equal(db.getExitStatus().cycled[i], before[i]);
    const health = new ProcessHealthChecker(db, { observeSlotPane() { throw Error("must not scan"); } } as any) as any;
    await health.checkExitPending();
  } finally { db.close(); }
});

test("real PM Stop without slot row consumes /exit once, while dev Stops and input remain untouched", async () => {
  const db = database();
  const sends: Array<[number, string]> = [];
  try {
    db.setExitPending(true, false, true, "resume");
    const relay = {
      observeSlotPane: async () => ({ paneId: "%40" }),
      deliverGuardedToSlot: async (slot: number, text: string, guard: any) => {
        assert.equal(await guard.recheck(), null);
        assert.equal(guard.finalCheck(), null);
        sends.push([slot, text]); return { outcome: "delivered" };
      },
      injectToPM() {}, sendToSlot() { throw Error("must not send to a dev"); },
    };
    const processor = new HookProcessor(db, relay as any) as any;
    assert.equal(db.getSlot(0), undefined);
    db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
    // An input before this Stop is just work; no input gate or tool counter.
    db.logEvent(0, "UserPromptSubmit", "UserPromptSubmit", null, {});
    await processor.process(0, { type: "Stop", session_id: "pm" });
    await processor.process(0, { type: "Stop", session_id: "pm" });
    assert.deepEqual(sends, [[0, "/exit"]]);
    assert.equal(db.getExitStatus().cycled[0], true);
    assert.equal(db.getExitStatus().cycled[1], false);
    assert.equal(db.getExitStatus().pm_only, true, "wait for respawn + continuation before arming devs");
  } finally { db.close(); }
});

for (const prompt of [undefined, "Read /some/path. It's PM's saved prompt.\nResume."]) {
  test(`boot continuation ${prompt ? "saved literal" : "fallback"} ends the PM-only episode without arming devs and survives duplicate callback`, async () => {
    const db = database();
    const messages: string[] = [];
    try {
      db.setExitPending(true, false, true, prompt);
      markPmCycled(db);
      const health = new ProcessHealthChecker(db, {
        deliverGuardedToSlot: async (slot: number, text: string, guard: any) => {
          assert.equal(slot, 0); assert.equal(guard.bracketedPaste, true);
          assert.equal(guard.allowNativeQueuedInput, true, "a busy PM queues the continuation instead of refusing it");
          assert.equal(await guard.recheck(), null); assert.equal(guard.finalCheck(), null);
          assert.equal(db.getExitStatus().dev_only, false, "no dev phase is armed during delivery");
          messages.push(text); return { outcome: "delivered" };
        },
      } as any) as any;
      await health.deliverPMRestartContinuation("%40");
      await health.deliverPMRestartContinuation("%40");
      // Fresh PM launch: the fallback is the handoff-pointing continuation, never bare "continue".
      assert.deepEqual(messages, [prompt ?? PM_RESTART_CONTINUATION_PROMPT]);
      // PM-only means PM only (Rajiv C0ALZJHGE49 thread 1794970059.617269, ts 1794971912.636829): the episode ends with its
      // continuation (delivered, pending cleared, exit_pending_complete logged) and no dev slot is ever armed.
      const status = db.getExitStatus();
      assert.equal(status.pending, false); assert.equal(status.pm_only, false); assert.equal(status.dev_only, false);
      assert.equal(status.continuation_delivery, "delivered"); assert.equal(status.stop_only, false);
      assert.equal(db.getEvents(0, 5, "exit_pending_complete").length, 1);
      assert.equal(JSON.parse(db.getEvents(0, 1, "exit_pending_complete")[0]!.payload).source, "pm_restart_continuation");
      const scanned: number[] = [];
      health.getPaneCommand = async () => "claude";
      health.relay.observeSlotPane = async (slot: number) => { scanned.push(slot); return { paneId: `%${slot}`, text: "" }; };
      health.relay.getSlotActivityState = async () => "busy";
      await health.checkExitPending();
      assert.deepEqual(messages, [prompt ?? PM_RESTART_CONTINUATION_PROMPT], "no /exit to any dev");
      assert.deepEqual(scanned, [], "no slot is scanned once the PM-only episode has ended");
    } finally { db.close(); }
  });
}

for (const outcome of ["refused_pre_effect", "uncertain"]) {
  test(`${outcome} continuation never arms devs; only a proven zero-effect refusal is retried`, async () => {
    const db = database(); let sends = 0;
    try {
      db.setExitPending(true, false, true, "resume"); markPmCycled(db);
      const health = new ProcessHealthChecker(db, {
        deliverGuardedToSlot: async () => { sends++; return { outcome }; },
      } as any) as any;
      await health.deliverPMRestartContinuation("%40");
      await health.deliverPMRestartContinuation("%40");
      assert.equal(sends, outcome === "refused_pre_effect" ? 2 : 1, "zero effect is retried; an uncertain prompt is never repeated");
      assert.equal(db.getExitStatus().pm_only, true); assert.equal(db.getExitStatus().dev_only, false);
      assert.equal(db.getExitStatus().continuation_delivery, outcome === "refused_pre_effect" ? "waiting" : "attempted");
    } finally { db.close(); }
  });
}

test("rearming the same prompt during continuation cannot reset successor dev flags", async () => {
  const db = database();
  try {
    db.setExitPending(true, false, true, "resume");
    markPmCycled(db);
    const health = new ProcessHealthChecker(db, {
      deliverGuardedToSlot: async () => {
        db.setExitPending(true, false, true, "resume");
        return { outcome: "delivered" };
      },
    } as any) as any;
    await health.deliverPMRestartContinuation("%40");
    assert.equal(db.getExitStatus().pm_only, true);
    assert.equal(db.getExitStatus().pending, true, "the newer episode is untouched");
    assert.equal(db.getExitStatus().continuation_delivery, "waiting");
  } finally { db.close(); }
});

test("new work after Stop does not refuse the exit attempt (no work fence; Rajiv ts 1793758450.451269)", async () => {
  const db = database();
  try {
    db.setExitPending(true, false, true);
    db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
    const sent: string[] = [];
    const processor = new HookProcessor(db, {
      observeSlotPane: async () => {
        db.logEvent(0, "UserPromptSubmit", "UserPromptSubmit", null, {});
        return { paneId: "%40" };
      },
      deliverGuardedToSlot: async (_slot: number, text: string, guard: any) => {
        assert.equal(await guard.recheck(), null);
        assert.equal(guard.finalCheck(), null);
        assert.equal(guard.skipSlashClear, true, "no Escape/C-u into a busy PM turn");
        sent.push(text);
        return { outcome: "delivered" };
      },
    } as any);
    await processor.process(0, { type: "Stop", session_id: "pm" });
    assert.deepEqual(sent, ["/exit"]);
    assert.equal(db.getEvents(0, 1, "UserPromptSubmit").length, 1, "input never held");
  } finally { db.close(); }
});

for (const command of ["claude", "2.1.312"]) {
test(`existing post-respawn timer dispatches PM continuation for ${command} rather than generic continue`, async () => {
  const { readFileSync } = await import("node:fs");
  const { default: ts } = await import("typescript");
  const source = readFileSync(new URL("../src/health.ts", import.meta.url), "utf8");
  const tree = ts.createSourceFile("health.ts", source, ts.ScriptTarget.Latest, true);
  let method: any;
  const visit = (node: any) => {
    if (ts.isMethodDeclaration(node) && node.name.getText(tree) === "scheduleContinueInjection") method = node;
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.ok(method);
  const js = ts.transpileModule(`class Probe { ${method.getText(tree)} }`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const db = database();
  let callback: (() => Promise<void>) | undefined;
  const generic: string[] = [];
  try {
    db.setExitPending(true, false, true, "saved continuation");
    markPmCycled(db);
    db.logEvent(0, "exit_pending_triggered", "Stop", null, {
      episode: db.getExitStatus().episode, delivery: "delivered",
    });
    db.logEvent(0, "SessionStart", "SessionStart", null, {
      session_id: "recovered", source: "resume", cwd: "/fixture",
    });
    const sent: string[] = [];
    const health = new ProcessHealthChecker(db, {
      deliverGuardedToSlot: async (_slot: number, text: string) => {
        sent.push(text); return { outcome: "delivered" };
      },
    } as any) as any;
    health.getPaneCommand = async () => command;
    health.reconcilePMRestartContinuation = async () => health.deliverPMRestartContinuation("%40");
    const schedule = new Function("verifyPaneIdentity", "execShell", "setTimeout",
      `${js}; return Probe.prototype.scheduleContinueInjection;`)(
      async () => ({ ok: true, snapshot: { paneId: "%40" } }),
      async (text: string) => { generic.push(text); },
      (cb: () => Promise<void>) => { callback = cb; return { unref() {} }; },
    );
    schedule.call(health, 0);
    await callback!();
    assert.deepEqual(sent, ["saved continuation"]);
    assert.deepEqual(generic, []);
    assert.equal(db.getExitStatus().pending, false, "the PM-only episode ends with its continuation");
    assert.equal(db.getExitStatus().dev_only, false);
  } finally { db.close(); }
});
}

// Rajiv C0ALZJHGE49 thread 1794970059.617269, ts 1794971912.636829: "continuation delivery is supposed to be injected right after
// the process is back up. it does not have to wait for stop or any other gate." No input guard: real input the replacement
// boot already accepted, or PM inbound pending, never withholds the continuation (supersedes the withhold of CTO REVISE
// ts 1794949061.287419, which starved episode 33fdff94 for 30+ minutes).
function continuationFixture(relayExtra: Record<string, unknown> = {}) {
  const db = database();
  const messages: string[] = [];
  db.setExitPending(true, false, true, "resume");
  markPmCycled(db);
  db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "old", source: "startup" });
  db.logEvent(0, "UserPromptSubmit", "UserPromptSubmit", null, { session_id: "old" });
  const bootId = db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "replacement", source: "startup" });
  const relay = {
    deliverGuardedToSlot: async (_slot: number, text: string, guard: any) => {
      assert.equal(await guard.recheck(), null);
      assert.equal(guard.finalCheck(), null);
      messages.push(text);
      return { outcome: "delivered" };
    },
    ...relayExtra,
  };
  const health = new ProcessHealthChecker(db, relay as any) as any;
  return { db, messages, bootId, health };
}

function assertEpisodeComplete(db: MoPDatabase) {
  const status = db.getExitStatus();
  assert.equal(status.pending, false);
  assert.equal(status.continuation_delivery, "delivered");
  assert.equal(status.dev_only, false);
  assert.equal(db.getEvents(0, 5, "exit_pending_complete").length, 1);
}

test("old-process poll_event prompts after the replacement boot do not withhold the continuation", async () => {
  const f = continuationFixture();
  try {
    f.db.logEvent(0, "UserPromptSubmit", "UserPromptSubmit", null, { session_id: "old", source: "poll_event" });
    await f.health.deliverPMRestartContinuation("%40", f.bootId);
    assert.deepEqual(f.messages, ["resume"]);
    assertEpisodeComplete(f.db);
  } finally { f.db.close(); }
});

test("real input already accepted by the replacement boot does NOT withhold the continuation: delivered once, episode complete", async () => {
  const f = continuationFixture();
  try {
    f.db.logEvent(0, "UserPromptSubmit", "UserPromptSubmit", null, { session_id: "replacement" }); // a Slack inbound 6s after boot (33fdff94)
    await f.health.deliverPMRestartContinuation("%40", f.bootId);
    await f.health.deliverPMRestartContinuation("%40", f.bootId);
    assert.deepEqual(f.messages, ["resume"]);
    assertEpisodeComplete(f.db);
    assert.equal(f.db.getEvents(0, 5, "pm_restart_continuation_withheld").length, 0);
    assert.equal(f.db.getEvents(0, 5, "pm_restart_continuation").length, 1);
  } finally { f.db.close(); }
});

test("pending PM inbound does NOT withhold the continuation", async () => {
  const f = continuationFixture({ hasPendingPMInbound: () => true });
  try {
    f.db.enqueuePendingPMEvent(1, "handoff", "inbound");
    await f.health.deliverPMRestartContinuation("%40", f.bootId);
    assert.deepEqual(f.messages, ["resume"]);
    assertEpisodeComplete(f.db);
  } finally { f.db.close(); }
});

test("a delivery already on record is never repeated: no second injection, the episode just completes", async () => {
  const f = continuationFixture();
  try {
    f.db.logEvent(0, "pm_restart_continuation", null, null, {
      episode: f.db.getExitStatus().episode, boot_event_id: f.bootId, delivery: "delivered",
    });
    await f.health.deliverPMRestartContinuation("%40", f.bootId);
    assert.deepEqual(f.messages, []);
    assertEpisodeComplete(f.db);
  } finally { f.db.close(); }
});

test("a zero-effect refusal hands the claim back and the next attempt delivers", async () => {
  let refuse = true;
  const f = continuationFixture({
    deliverGuardedToSlot: async (_slot: number, text: string) => {
      if (refuse) return { outcome: "refused_pre_effect", reason: "composer_not_empty" };
      f.messages.push(text); return { outcome: "delivered" };
    },
  });
  try {
    await f.health.deliverPMRestartContinuation("%40", f.bootId);
    assert.deepEqual(f.messages, []);
    assert.equal(f.db.getExitStatus().continuation_delivery, "waiting");
    assert.equal(f.db.getExitStatus().pending, true);
    refuse = false;
    await f.health.deliverPMRestartContinuation("%40", f.bootId);
    assert.deepEqual(f.messages, ["resume"]);
    assertEpisodeComplete(f.db);
  } finally { f.db.close(); }
});

test("a delivered PM-only episode still pending (dialog confirmation open at delivery) is cleared by the health observer", async () => {
  const f = continuationFixture({ hasPendingExitConfirmation: () => false, observeExitAttempts: async () => undefined });
  try {
    f.db.setConfig("exit_pending_continuation_delivery", "delivered");
    await f.health.checkExitPending();
    assertEpisodeComplete(f.db);
    assert.equal(JSON.parse(f.db.getEvents(0, 1, "exit_pending_complete")[0]!.payload).source, "health_observer");
  } finally { f.db.close(); }
});
