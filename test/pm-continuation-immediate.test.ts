// Rajiv C0ALZJHGE49 thread 1794970059.617269, ts 1794971912.636829: "continuation delivery is supposed to be injected right after
// the process is back up. it does not have to wait for stop or any other gate."
// Episode 33fdff94 (2026-11-18): the replacement PM (pid 27802) was verified at 08:18:49 IST, but the continuation stayed
// "waiting" and pending=true. Two causes, both covered here:
//   1. the restart flow ran reconcile BEFORE the health observer marked the replacement verified (cycled[0]=false, silent
//      return); the next try came after the restart cooldown;
//   2. by then a real Slack inbound had reached the replacement boot, and the input guard withheld the continuation forever.
// Fixtures only: not executed (Rajiv DM D0AMF0XE6TS thread 1793979986.975849, ts 1793983042.752819: MoP changes need no
// local testing or proof).
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { ProcessHealthChecker } from "../src/health.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const cwd = "/Users/rajiv/Downloads/projects/heydonna-app";
const health = readFileSync(new URL("../src/health.ts", import.meta.url), "utf8");

test("the restart flow verifies the replacement through the observer before it reconciles the continuation", () => {
  const flow = health.slice(health.indexOf("await this.recordPMReplacement(episode"), health.indexOf("await this.reconcilePMRestartContinuation(boot.id)"));
  assert.ok(flow.includes('if (trigger === "exit") await this.relay.observeExitAttempts?.()'), "observer runs between the bound receipt and reconcile");
});

test("the health tick retries the PM continuation after the observer and outside the per-slot cooldown loop", () => {
  const tail = health.slice(health.indexOf("await this.checkExitPending();\n    // The PM continuation"));
  assert.ok(tail.includes("this.db.getExitPending() && !this.pmInitiatedRespawns.has(0)"));
  assert.ok(tail.includes("await this.reconcilePMRestartContinuation()"));
});

test("no input guard remains in front of the PM continuation", () => {
  assert.ok(!health.includes("continuationInputRefusal"));
  assert.ok(!health.includes("pm_restart_continuation_withheld"));
  assert.ok(!health.includes("hasRealPromptSince"));
});

// 33fdff94 shape: waiting, verified replacement bound to MoP's own launch, real input already accepted by the replacement.
// Both with and without the post-restart cooldown that makes the per-slot loop skip the PM.
for (const cooldown of [false, true]) {
  test(`waiting continuation with a verified replacement and accepted real input is completed on the first tick${cooldown ? " (restart cooldown active)" : ""}; never re-injected`, async () => {
    const bin = mkdtempSync(join(tmpdir(), "mop-continuation-immediate-"));
    const oldPath = process.env.PATH;
    writeFileSync(join(bin, "tmux"), `#!/bin/sh\ncase "$*" in *pane_pid*) echo '4000|/dev/ttys040';; *) printf '%s\\n' '%40|${cwd}';; esac\n`, { mode: 0o700 });
    // The pane's Claude child is owned only through a trusted executable path (a bare "claude" argv0 needs a libproc probe).
    writeFileSync(join(bin, "ps"), `#!/bin/sh\nprintf '4000 1 -zsh\\n4001 4000 ${join(homedir(), ".local/opt/claude-code/claude")} --model claude-opus-5-5\\n'\n`, { mode: 0o700 });
    writeFileSync(join(bin, "git"), `#!/bin/sh\nprintf '%s\\n' '${cwd}'\n`, { mode: 0o700 });
    process.env.PATH = `${bin}:${oldPath}`;
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
    const sent: string[] = [];
    try {
      db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "old", cwd });
      db.setExitPending(true, false, true, "saved continuation");
      const episode = db.getExitStatus().episode!;
      db.saveExitAttempt(0, {
        episode, paneId: "%40", originalClaudePid: 3999, originalStart: "Wed Nov 18 00:29:17 2026",
        originalProcess: '["%40",3999,"Wed Nov 18 00:29:17 2026"]', sessionId: "old", inputEventId: null, assignmentEpoch: 0,
        stopEventId: null, ownerPin: null, phase: "verified", attemptCount: 1, submittedAt: new Date().toISOString(),
        blockedReason: null, replacementProcess: '["%40",4001,"Wed Nov 18 08:18:49 2026"]',
      });
      db.markSlotExitCycled(0);
      const exitId = db.logEvent(0, "exit_pending_triggered", "Stop", null, {
        source: "pm_only_stop", delivery: "delivered", episode, session_id: "old",
      });
      const bootId = db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "replacement", cwd });
      db.setConfig("pm_restart_launch", JSON.stringify({
        episode, launch_event_id: exitId, pane_id: "%40", command: "launch-pm.sh --via direct --model claude-opus-5-5",
        status: "booted", pane_pid: "4000", boot_event_id: bootId, process: "4000:4001",
      }));
      // A real Slack inbound reached the replacement 6s after its boot.
      db.logEvent(0, "UserPromptSubmit", "UserPromptSubmit", null, { session_id: "replacement" });
      assert.equal(db.getExitStatus().continuation_delivery, "waiting");

      const h = new ProcessHealthChecker(db, {
        observeExitAttempts: async () => undefined,
        hasPendingExitConfirmation: () => false,
        deliverGuardedToSlot: async (_slot: number, text: string, guard: any) => {
          assert.equal(await guard.recheck(), null);
          assert.equal(guard.finalCheck(), null);
          assert.equal(guard.allowNativeQueuedInput, true);
          sent.push(text); return { outcome: "delivered" };
        },
      } as any) as any;
      h.startTime = 0;
      h.isSlotProvisioned = (slot: number) => slot === 0;
      h.getPaneCommand = async () => "2.1.312";
      h.checkClaudeResponsiveness = async () => {};
      h.restartSlot = async () => { throw Error("must not restart productive PM"); };
      if (cooldown) h.lastRestart.set(0, Date.now());
      await h.checkAll();
      await h.checkAll();
      assert.deepEqual(sent, ["saved continuation"], "one injection, no re-inject on the next tick");
      const status = db.getExitStatus();
      assert.equal(status.pending, false);
      assert.equal(status.continuation_delivery, "delivered");
      assert.equal(status.dev_only, false);
      assert.equal(db.getEvents(0, 5, "exit_pending_complete").length, 1);
      assert.equal(db.getEvents(0, 5, "pm_restart_continuation").length, 1);
      assert.equal(db.getEvents(0, 5, "pm_restart_continuation_withheld").length, 0);
    } finally {
      process.env.PATH = oldPath;
      db.close();
      rmSync(bin, { recursive: true, force: true }); // Exact generated fixture only.
    }
  });
}
