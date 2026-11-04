import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { MoPDatabase } from "../src/db.js";
import { ProcessHealthChecker } from "../src/health.js";
import { HookProcessor } from "../src/hooks.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const cwd = "/Users/rajiv/Downloads/projects/heydonna-app";
const database = () => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });

for (const command of ["claude", "omp", "2.1.312"]) {
  test(`real boot gate accepts ${command} and health scan recognizes recovered PM`, async () => {
    const db = database();
    try {
      const health = new ProcessHealthChecker(db, {} as any) as any;
      let reads = 0;
      health.AGENT_BOOT_POLL_MS = 0;
      health.getPaneCommand = async () => ++reads < 2 ? "zsh" : command;
      assert.equal(await health.waitForAgentBoot(0, 2_000), command);
      assert.equal(db.getEvents(0, 1, "agent_booted").length, 1);
      let scanned = 0;
      health.startTime = 0;
      health.isSlotProvisioned = (slot: number) => slot === 0;
      health.getPaneCommand = async () => command;
      health.checkClaudeResponsiveness = async () => { scanned++; };
      await health.checkAll();
      assert.equal(scanned, 1);
      health.getPaneCommand = async () => "2.1.312-extra";
      assert.equal(await health.waitForAgentBoot(0, 0), null);
      health.getPaneCommand = async () => "2.1.312";
      assert.equal(await health.waitForAgentBoot(1, 0), null, "version-only recognition is PM bounded");
    } finally { db.close(); }
  });
}

for (const session of [undefined, "", "old"]) {
  test(`stale or missing PM Stop identity ${String(session)} cannot consume successor request`, async () => {
    const db = database(); let sends = 0;
    try {
      db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "successor" });
      db.setExitPending(true, false, true, "saved");
      const processor = new HookProcessor(db, {
        observeSlotPane: async () => { sends++; return { paneId: "%40" }; },
        deliverGuardedToSlot: async () => { sends++; return { outcome: "delivered" }; },
      } as any);
      await processor.process(0, { type: "Stop", session_id: session });
      assert.equal(sends, 0);
      assert.equal(db.getExitStatus().cycled[0], false);
    } finally { db.close(); }
  });
}

test("startup health scan recovers only waiting delivery bound to the episode's successor boot", async () => {
  const bin = mkdtempSync(join(tmpdir(), "mop-recovery-test-"));
  const oldPath = process.env.PATH;
  writeFileSync(join(bin, "tmux"), `#!/bin/sh\nprintf '%s\\n' '%40|${cwd}'\n`, { mode: 0o700 });
  writeFileSync(join(bin, "git"), `#!/bin/sh\nprintf '%s\\n' '${cwd}'\n`, { mode: 0o700 });
  process.env.PATH = `${bin}:${oldPath}`;
  try {
    for (const delivery of ["waiting", "attempted", "uncertain"]) {
      for (const recovered of [false, true]) {
        const db = database(); const sent: string[] = [];
        try {
          db.setExitPending(true, false, true, "saved continuation");
          db.markSlotExitCycled(0); db.markSlotExitCycled(3);
          db.setConfig("exit_pending_continuation_delivery", delivery);
          const boot = () => db.logEvent(0, "SessionStart", "SessionStart", null, {
            source: "resume", session_id: "pm", cwd,
          });
          if (!recovered) boot();
          db.logEvent(0, "exit_pending_triggered", "Stop", null, {
            episode: db.getExitStatus().episode, delivery: "delivered",
          });
          if (recovered) boot();
          // New instance models loss of every in-memory post-restart timer.
          const health = new ProcessHealthChecker(db, {
            // Successor dev panes are working: the idle-prompt dev exit never fires.
            observeSlotPane: async (slot: number) => ({ paneId: `%${slot}`, text: "" }),
            getSlotActivityState: async () => "busy",
            deliverGuardedToSlot: async (_slot: number, text: string, guard: any) => {
              assert.equal(await guard.recheck(), null);
              assert.equal(guard.finalCheck(), null);
              assert.equal(db.getExitStatus().cycled[3], true);
              sent.push(text); return { outcome: "delivered" };
            },
          } as any) as any;
          health.startTime = 0;
          health.isSlotProvisioned = (slot: number) => slot === 0;
          health.getPaneCommand = async () => "2.1.312";
          health.checkClaudeResponsiveness = async () => {};
          health.restartSlot = async () => { throw Error("must not restart productive PM"); };
          await health.checkAll();
          await health.checkAll();
          assert.deepEqual(sent, delivery === "waiting" && recovered ? ["saved continuation"] : []);
        } finally { db.close(); }
      }
    }
  } finally {
    process.env.PATH = oldPath;
    rmSync(bin, { recursive: true, force: true }); // Exact generated fixture only.
  }
});

// Episode 498d3cae (2026-11-04 02:34:58Z): the single /exit was recorded
// `uncertain`; the watchdog saw the PM die and restarted it with --continue
// (SAME session UUID). The continuation path only accepted `delivered`, so it
// never ran, dev_only never armed and exit custody blocked PM clears.
// CTO correction (C0ALZJHGE49 thread 1793754459.969419, ts 1793761597.648009):
// settle the continuation from an independent watchdog restart, never promote
// the exit receipt, and never treat a boot or session id alone as proof.
test("uncertain /exit continuation matrix: watchdog restart settles once; boot alone never; newer episode untouched; response loss once", async () => {
  const { pmExitInCustody, PM_EXIT_CONSUMED_EPISODE_KEY } = await import("../src/pmHandoffExit.js");
  const bin = mkdtempSync(join(tmpdir(), "mop-recovery-test-"));
  const oldPath = process.env.PATH;
  writeFileSync(join(bin, "tmux"), `#!/bin/sh\nprintf '%s\\n' '%40|${cwd}'\n`, { mode: 0o700 });
  writeFileSync(join(bin, "git"), `#!/bin/sh\nprintf '%s\\n' '${cwd}'\n`, { mode: 0o700 });
  process.env.PATH = `${bin}:${oldPath}`;
  const configRows = (db: any) => JSON.stringify(db.db.prepare("SELECT key, value FROM config WHERE key LIKE 'exit\\_%' ESCAPE '\\' OR key LIKE 'pm\\_exit\\_%' ESCAPE '\\' ORDER BY key").all());
  type Case = "watchdog_restart" | "new_uuid_boot_only" | "newer_episode" | "response_loss";
  try {
    for (const kind of ["watchdog_restart", "new_uuid_boot_only", "newer_episode", "response_loss"] as Case[]) {
      const db = database(); const sent: string[] = [];
      try {
        db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "pm", cwd });
        db.setExitPending(true, false, true, "saved continuation");
        const episode = db.getExitStatus().episode!;
        db.markSlotExitCycled(0); db.markSlotExitCycled(3);
        db.setConfig(PM_EXIT_CONSUMED_EPISODE_KEY, episode);
        db.logEvent(0, "exit_pending_triggered", "Stop", null, {
          source: "pm_only_stop", delivery: "uncertain", episode, session_id: "pm",
        });
        const exitReceipt = () => db.getEvents(0, 5, "exit_pending_triggered").map((e) => e.payload);
        const receiptBefore = JSON.stringify(exitReceipt());
        if (kind === "new_uuid_boot_only") {
          db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "pm-new", cwd });
        } else {
          db.logEvent(0, "process_dead", null, null, { pane_command: "zsh" });
          db.logEvent(0, "SessionStart", "SessionStart", null, { source: "resume", session_id: "pm", cwd });
          db.logEvent(0, "agent_booted", null, null, { command: "claude" });
          db.logEvent(0, "process_restarted", null, null, { command: "launch-pm --continue" });
        }
        if (kind === "newer_episode") db.setExitPending(true, true); // a newer dev-only episode owns the state
        const before = configRows(db);
        const relay = {
          observeSlotPane: async () => ({ paneId: "%40" }),
          getSlotActivityState: async () => "busy", // dev panes are working: never exited
          deliverGuardedToSlot: async (_slot: number, text: string, guard: any) => {
            if (guard.recheck) assert.equal(await guard.recheck(), null);
            if (guard.finalCheck) assert.equal(guard.finalCheck(), null);
            sent.push(text); return { outcome: kind === "response_loss" ? "uncertain" : "delivered" };
          },
          injectToPM() {}, sendToSlot() { throw Error("must not send to a dev"); },
        };
        const health = new ProcessHealthChecker(db, relay as any) as any;
        health.startTime = 0;
        health.isSlotProvisioned = (slot: number) => slot === 0;
        health.getPaneCommand = async () => "2.1.312";
        health.checkClaudeResponsiveness = async () => {};
        health.restartSlot = async () => { throw Error("must not restart productive PM"); };
        await health.checkAll();
        await health.checkAll();
        await new HookProcessor(db, relay as any).process(0, { type: "Stop", session_id: "pm" });
        const status = db.getExitStatus();
        assert.ok(!sent.includes("/exit"), `${kind}: never resend /exit`);
        assert.equal(JSON.stringify(exitReceipt()), receiptBefore, `${kind}: exit receipt stays uncertain`);
        const claims = db.getEvents(0, 5, "pm_restart_continuation_claim");
        if (kind === "watchdog_restart") {
          assert.deepEqual(sent, ["saved continuation"], "one guarded continuation");
          assert.equal(claims.length, 1);
          assert.equal(status.dev_only, true); assert.equal(status.pm_only, false);
          assert.equal(status.continuation_delivery, "delivered");
          assert.equal(pmExitInCustody(db), false); assert.equal(db.getConfig(PM_EXIT_CONSUMED_EPISODE_KEY), "");
        } else if (kind === "response_loss") {
          assert.deepEqual(sent, ["saved continuation"], "at most one attempt");
          assert.equal(claims.length, 1);
          assert.equal(status.continuation_delivery, "attempted");
          assert.equal(status.pm_only, true); assert.equal(status.dev_only, false);
          assert.equal(status.cycled[3], true, "dev flags untouched");
          assert.equal(pmExitInCustody(db), true, "custody kept without a delivered receipt");
        } else if (kind === "new_uuid_boot_only") {
          assert.deepEqual(sent, []); assert.equal(claims.length, 0);
          assert.equal(status.continuation_delivery, "waiting"); assert.equal(status.pm_only, true);
          assert.equal(pmExitInCustody(db), true, "uncertain custody kept");
          const typed = db.getEvents(0, 5, "pm_restart_continuation_uncertain");
          assert.equal(typed.length, 1, "typed uncertain, once");
          assert.equal(JSON.parse(typed[0].payload).reason, "no_watchdog_restart_after_attempt");
          assert.equal(configRows(db), before);
        } else {
          assert.deepEqual(sent, []); assert.equal(claims.length, 0);
          assert.equal(configRows(db), before, "newer episode byte-identical");
        }
      } finally { db.close(); }
    }
  } finally {
    process.env.PATH = oldPath;
    rmSync(bin, { recursive: true, force: true }); // Exact generated fixture only.
  }
});
