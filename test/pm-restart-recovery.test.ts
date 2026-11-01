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
