import assert from "node:assert/strict";
import test from "node:test";
import { ProcessHealthChecker } from "../src/health.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

// CTO P1 on 071144d (C0ALZJHGE49/1791372028.387449): unpinned activity
// consumers probed 0:0.$SLOT, so with S6 at index 7 and S7 at index 6 the
// exit_pending writer read S7's idle pane and pasted /exit into active S6.
// Every activity check must bind to the checkout-verified pane id.

const P = (n: number) => `/Users/rajiv/Downloads/projects/heydonna-app-300${n}`;
type Pane = { id: string; path: string };

function world(panes: Record<string, Pane>, activeIds: Set<string>) {
  const commands: string[] = [];
  const runShell = async (command: string) => {
    commands.push(command);
    const disp = command.match(/^tmux display-message -t (\S+) -p/);
    if (disp) {
      const pane = panes[disp[1]] ?? Object.values(panes).find((p) => p.id === disp[1]);
      if (!pane) throw Object.assign(new Error("can't find pane"), { code: 1 });
      return { stdout: `${pane.id}|${pane.path}\n`, stderr: "" };
    }
    if (command.startsWith("tmux list-panes")) {
      return { stdout: Object.values(panes).map((p) => `${p.id}|${p.path}`).join("\n") + "\n", stderr: "" };
    }
    const git = command.match(/^git -C '([^']+)' rev-parse --show-toplevel/);
    if (git) return { stdout: `${git[1]}\n`, stderr: "" };
    if (command.includes("is-active.sh")) {
      const pin = command.match(/--pane-id (%\d+)/);
      // An unpinned probe is the defect: model it as reading pane index == slot.
      const slot = Number(command.match(/is-active\.sh (\d+)/)?.[1]);
      const id = pin ? pin[1] : panes[`0:0.${slot}`]?.id;
      if (id && activeIds.has(id)) return { stdout: "", stderr: "" };
      throw Object.assign(new Error("idle"), { code: 1 });
    }
    return { stdout: "", stderr: "" };
  };
  return { runShell, commands };
}

// Live layout: S6 is %7 at index 7, S7 is the pane at index 6.
const LIVE: Record<string, Pane> = {
  "0:0.6": { id: "%155", path: P(7) },
  "0:0.7": { id: "%7", path: P(6) },
};

async function exitPendingWriter(relay: TmuxRelay) {
  const sent: Array<[number, string]> = [];
  const cycled: number[] = [];
  const db = {
    getExitPending: () => true,
    getExitStatus: () => ({ cycled: {} as Record<number, boolean> }),
    getSlot: () => ({ dnd: false }),
    markSlotExitCycled: (slot: number) => cycled.push(slot),
    logEvent: () => undefined,
    setExitPending: () => undefined,
    clearExitPending: () => undefined,
  };
  (relay as unknown as { sendToSlot: (s: number, c: string) => boolean }).sendToSlot = (s, c) => {
    sent.push([s, c]);
    return true;
  };
  (relay as unknown as { injectToPM: () => void }).injectToPM = () => undefined;
  const checker = new ProcessHealthChecker(db as never, relay);
  const internals = checker as unknown as { getPaneCommand: (s: number) => Promise<string>; checkExitPending: () => Promise<void> };
  internals.getPaneCommand = async (slot: number) => (slot === 6 || slot === 7 ? "claude" : "zsh");
  await internals.checkExitPending();
  return { sent, cycled };
}

test("S6 active / S7 idle: activity binds to S6's pane id and /exit is refused", async () => {
  const { runShell, commands } = world(LIVE, new Set(["%7"]));
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell });
  assert.equal(await relay.getSlotActivityState(6), "active");
  assert.equal(await relay.getSlotActivityState(7), "idle");
  assert.ok(commands.some((c) => /is-active\.sh 6 --pane-id %7$/.test(c)));
  assert.equal(commands.some((c) => /is-active\.sh \d+$/.test(c)), false, "no unpinned probe");
  const { sent, cycled } = await exitPendingWriter(relay);
  assert.deepEqual(sent, [[7, "/exit"]]);
  assert.deepEqual(cycled, [7]);
});

test("inverse: S7 active / S6 idle refuses S7 and cycles only S6", async () => {
  const { runShell } = world(LIVE, new Set(["%155"]));
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell });
  assert.equal(await relay.getSlotActivityState(6), "idle");
  assert.equal(await relay.getSlotActivityState(7), "active");
  const { sent, cycled } = await exitPendingWriter(relay);
  assert.deepEqual(sent, [[6, "/exit"]]);
  assert.deepEqual(cycled, [6]);
});

test("missing S6 pane fails closed: unknown, never idle, no /exit", async () => {
  const { runShell } = world({ "0:0.6": LIVE["0:0.6"] }, new Set());
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell });
  assert.equal(await relay.getSlotActivityState(6), "unknown");
  assert.equal(await relay.isSlotActive(6), true);
  const { sent } = await exitPendingWriter(relay);
  assert.equal(sent.some(([s]) => s === 6), false);
});

test("rebound S6 address (two panes on S6 checkout / pane moved to other cwd) fails closed", async () => {
  const dup = world({ ...LIVE, "0:0.8": { id: "%156", path: P(6) }, "0:0.7": { id: "%7", path: P(5) } }, new Set());
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: dup.runShell });
  // index 7 now shows S5's checkout; only %156 holds S6 -> exactly one match resolves to it
  assert.equal(await relay.getSlotActivityState(6), "idle");
  assert.ok(dup.commands.some((c) => /is-active\.sh 6 --pane-id %156$/.test(c)));
  const amb = world({ ...LIVE, "0:0.7": { id: "%7", path: P(5) }, "0:0.8": { id: "%156", path: P(6) }, "0:0.9": { id: "%157", path: P(6) } }, new Set());
  const relay2 = new TmuxRelay(DEFAULT_CONFIG, { runShell: amb.runShell });
  assert.equal(await relay2.getSlotActivityState(6), "unknown");
  assert.equal(amb.commands.some((c) => c.includes("is-active.sh 6")), false);
  const { sent } = await exitPendingWriter(relay2);
  assert.equal(sent.some(([s]) => s === 6), false);
});

test("boot syncs stale S6 address metadata to 0:0.7 without touching issue or epoch", async () => {
  const { mkdtempSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const Database = (await import("better-sqlite3")).default;
  const { MoPDatabase } = await import("../src/db.js");
  const dir = mkdtempSync(join(tmpdir(), "mop-addr-"));
  const path = join(dir, "mop.db");
  try {
    new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: path }).close();
    const raw = new Database(path);
    raw.prepare("UPDATE slots SET address='0:0.6', issue=9031, assignment_epoch=228 WHERE slot=6").run();
    raw.close();
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: path });
    const s6 = db.getSlot(6)!;
    assert.equal(s6.address, "0:0.7");
    assert.equal(s6.issue, 9031);
    assert.equal(s6.assignment_epoch, 228);
    assert.equal(db.getSlot(7)!.address, "0:0.6");
    db.close();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
