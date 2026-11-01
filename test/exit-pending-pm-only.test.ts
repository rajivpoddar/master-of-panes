import assert from "node:assert/strict";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { ProcessHealthChecker } from "../src/health.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const database = () => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });

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
  test(`boot continuation ${prompt ? "saved literal" : "fallback"} precedes dev reset and survives duplicate callback`, async () => {
    const db = database();
    const messages: string[] = [];
    try {
      db.markSlotExitCycled(2);
      db.setExitPending(true, false, true, prompt);
      db.markSlotExitCycled(0);
      const health = new ProcessHealthChecker(db, {
        deliverGuardedToSlot: async (slot: number, text: string, guard: any) => {
          assert.equal(slot, 0); assert.equal(guard.bracketedPaste, true);
          assert.equal(await guard.recheck(), null); assert.equal(guard.finalCheck(), null);
          assert.equal(db.getExitStatus().cycled[2], true, "dev flag still untouched during delivery");
          messages.push(text); return { outcome: "delivered" };
        },
      } as any) as any;
      await health.deliverPMRestartContinuation("%40");
      await health.deliverPMRestartContinuation("%40");
      assert.deepEqual(messages, [prompt ?? "continue"]);
      const status = db.getExitStatus();
      assert.equal(status.pm_only, false); assert.equal(status.dev_only, true);
      assert.equal(status.continuation_delivery, "delivered"); assert.equal(status.stop_only, true);
      assert.equal(status.cycled[0], true);
      for (let i = 1; i <= 8; i++) assert.equal(status.cycled[i], false);
      await health.checkExitPending(); // sequence devs do not use already-idle scan
    } finally { db.close(); }
  });
}

for (const outcome of ["refused_pre_effect", "uncertain"]) {
  test(`${outcome} continuation does not arm devs or repeat an uncertain prompt`, async () => {
    const db = database(); let sends = 0;
    try {
      db.setExitPending(true, false, true, "resume"); db.markSlotExitCycled(0); db.markSlotExitCycled(3);
      const health = new ProcessHealthChecker(db, {
        deliverGuardedToSlot: async () => { sends++; return { outcome }; },
      } as any) as any;
      await health.deliverPMRestartContinuation("%40");
      await health.deliverPMRestartContinuation("%40");
      assert.equal(sends, 1); assert.equal(db.getExitStatus().pm_only, true);
      assert.equal(db.getExitStatus().cycled[3], true);
    } finally { db.close(); }
  });
}

test("rearming the same prompt during continuation cannot reset successor dev flags", async () => {
  const db = database();
  try {
    db.setExitPending(true, false, true, "resume");
    db.markSlotExitCycled(0);
    db.markSlotExitCycled(3);
    const health = new ProcessHealthChecker(db, {
      deliverGuardedToSlot: async () => {
        db.setExitPending(true, false, true, "resume");
        return { outcome: "delivered" };
      },
    } as any) as any;
    await health.deliverPMRestartContinuation("%40");
    assert.equal(db.getExitStatus().pm_only, true);
    assert.equal(db.getExitStatus().cycled[3], true);
    assert.equal(db.getExitStatus().continuation_delivery, "waiting");
  } finally { db.close(); }
});

test("new work after Stop refuses the exit attempt without holding that input", async () => {
  const db = database();
  try {
    db.setExitPending(true, false, true);
    db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
    let delivered = false;
    const processor = new HookProcessor(db, {
      observeSlotPane: async () => {
        db.logEvent(0, "UserPromptSubmit", "UserPromptSubmit", null, {});
        return { paneId: "%40" };
      },
      deliverGuardedToSlot: async (_slot: number, _text: string, guard: any) => {
        assert.equal(await guard.recheck(), "pm_work_changed_after_stop");
        delivered = guard.finalCheck() === null;
        return { outcome: "refused_pre_effect" };
      },
    } as any);
    await processor.process(0, { type: "Stop", session_id: "pm" });
    assert.equal(delivered, false);
    assert.equal(db.getEvents(0, 1, "UserPromptSubmit").length, 1);
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
    db.markSlotExitCycled(0);
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
    assert.equal(db.getExitStatus().dev_only, true);
  } finally { db.close(); }
});
}
