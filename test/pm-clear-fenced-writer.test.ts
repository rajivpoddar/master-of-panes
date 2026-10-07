// CTO REVISE a6ec3b2 (C0ALZJHGE49 thread 1791379403.195509, ts 1791380350.083999):
// P1-1 the PM self-clear writer must carry the original request/session/pane/
//      turn pin and recheck it at paste and at the single Enter.
// P1-2 one durable consumed/possible-effect claim shared by Stop/drain that
//      survives failure and restart; only a proven zero-effect busy refusal defers.
// Offline only: compiled consumers (dist/) with a fake tmux; no live clear.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../dist/db.js";
import {
  makePmClearWriteFence,
  requestPmClearOnce,
  retryDeferredPmClearOnStop,
  waitForPmIdleDrain,
} from "../dist/pmClearLatch.js";
import { PM_CLEAR_COMMAND, TmuxRelay } from "../dist/relay.js";
import { DEFAULT_CONFIG } from "../dist/types.js";

const RULE = "─".repeat(40);
const REQUESTED_AT = "2026-10-07T12:38:06.342Z";

function freshDb(prefix: string): { db: MoPDatabase; directory: string; dbPath: string } {
  const directory = mkdtempSync(join(tmpdir(), prefix));
  const dbPath = join(directory, "mop.db");
  return { db: new MoPDatabase({ ...DEFAULT_CONFIG, dbPath }), directory, dbPath };
}

type FakePane = {
  paneId: string;
  addressPaneId: string;
  command: string;
  composer: string | null;
  buffer: string;
  commands: string[];
  onPaste?: () => void;
  onFirstProbe?: () => void;
};

function fakePane(overrides: Partial<FakePane> = {}): FakePane {
  return { paneId: "%1", addressPaneId: "%1", command: "claude", composer: "", buffer: "", commands: [], ...overrides };
}

function relayFor(pane: FakePane, db: MoPDatabase): TmuxRelay {
  const relay = new TmuxRelay(DEFAULT_CONFIG, {
    pmRuntime: "claude",
    runShell: async (command: string) => {
      pane.commands.push(command);
      if (command.includes("#{pane_id}|#{pane_current_command}")) {
        const first = pane.onFirstProbe;
        pane.onFirstProbe = undefined;
        first?.();
        return { stdout: `${pane.addressPaneId}|${pane.command}\n`, stderr: "" };
      }
      if (command.startsWith("tmux capture-pane")) {
        const body = pane.composer === null ? "loading..." : `${RULE}\n❯ ${pane.composer}\n${RULE}`;
        return { stdout: `output\n${body}\n`, stderr: "" };
      }
      const load = /^tmux load-buffer -b \S+ '([^']+)'$/.exec(command);
      if (load) {
        pane.buffer = readFileSync(load[1]!, "utf8");
        return { stdout: "", stderr: "" };
      }
      if (command.startsWith("tmux paste-buffer")) {
        pane.composer = `${pane.composer ?? ""}${pane.buffer}`;
        pane.onPaste?.();
        return { stdout: "", stderr: "" };
      }
      if (command.endsWith(" C-u")) pane.composer = "";
      if (command.endsWith(" Enter")) pane.composer = "";
      return { stdout: "", stderr: "" };
    },
  });
  relay.setDatabase(db);
  (relay as unknown as { pmBusy: boolean | null }).pmBusy = false;
  return relay;
}

function effects(pane: FakePane) {
  return {
    pastes: pane.commands.filter((c) => c.startsWith("tmux paste-buffer")),
    enters: pane.commands.filter((c) => c.endsWith(" Enter")),
  };
}

/** A live PM: generation and session the fence pins, mutated by the scenario. */
function pmState() {
  return { generation: 7, session: "41" };
}

function latch(db: MoPDatabase, state = "sending", requestedAt = REQUESTED_AT): void {
  db.setPendingClear(0);
  db.setConfig("pm_clear_requested_at", requestedAt);
  db.setConfig("pm_clear_delivery_state", state);
}

function fenceFor(db: MoPDatabase, pm: ReturnType<typeof pmState>, idle: () => boolean = () => true) {
  return makePmClearWriteFence({
    db,
    requestedAt: REQUESTED_AT,
    generation: 7,
    sessionToken: "41",
    currentGeneration: () => pm.generation,
    currentSessionToken: () => pm.session,
    idleProven: idle,
  });
}

test("legitimate current-generation delivery: one paste and one Enter, pinned to the pane id", async () => {
  const { db, directory } = freshDb("mop-pmclear-legit-");
  try {
    latch(db);
    const pane = fakePane();
    const pm = pmState();
    const result = await relayFor(pane, db).writePMClearFenced({ paneId: "%1", check: fenceFor(db, pm) });
    assert.equal(result.ok, true);
    const { pastes, enters } = effects(pane);
    assert.equal(pastes.length, 1);
    assert.equal(enters.length, 1);
    assert.match(pastes[0]!, / -t %1 /);
    assert.equal(enters[0], "tmux send-keys -t %1 Enter");
    assert.equal(pane.buffer, PM_CLEAR_COMMAND);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a PM turn that starts while the clear is queued behind another PM write gets zero effects", async () => {
  const { db, directory } = freshDb("mop-pmclear-queued-");
  try {
    latch(db);
    const pane = fakePane();
    const pm = pmState();
    const relay = relayFor(pane, db);
    // Occupy the serialized PM writer; a new PM turn starts before it drains.
    let release!: () => void;
    (relay as unknown as { directInjectChain: Promise<unknown> }).directInjectChain = new Promise<void>((r) => {
      release = r;
    });
    const pending = relay.writePMClearFenced({ paneId: "%1", check: fenceFor(db, pm) });
    pm.generation += 1; // UserPromptSubmit -> /pm-status start
    release();
    const result = await pending;
    assert.equal(result.ok, false);
    assert.equal(result.zeroEffect, true);
    assert.equal(result.busy, true);
    assert.deepEqual(effects(pane), { pastes: [], enters: [] });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a PM turn that starts after the paste never receives the Enter and is never labelled zero-effect", async () => {
  const { db, directory } = freshDb("mop-pmclear-afterpaste-");
  try {
    latch(db);
    const pm = pmState();
    const pane = fakePane({ onPaste: () => { pm.generation += 1; } });
    const result = await relayFor(pane, db).writePMClearFenced({ paneId: "%1", check: fenceFor(db, pm) });
    assert.equal(result.ok, false);
    assert.equal(effects(pane).enters.length, 0);
    assert.equal(pane.composer, "", "only our exact paste was cleared");
    assert.equal(result.zeroEffect, false, "post-paste outcome is a possible effect");
    assert.equal(result.busy, false);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("unknown, drifted or rebound identity refuses before any paste", async () => {
  const cases: Array<[string, (pane: FakePane, pm: ReturnType<typeof pmState>, db: MoPDatabase) => void]> = [
    ["pane rebound to another id", (pane) => { pane.addressPaneId = "%9"; }],
    ["pane not running claude", (pane) => { pane.command = "zsh"; }],
    ["unreadable composer", (pane) => { pane.composer = null; }],
    ["non-empty composer", (pane) => { pane.composer = "half typed"; }],
    ["session rebound", (_pane, pm) => { pm.session = "42"; }],
    ["request replaced by operator", (_pane, _pm, db) => { db.setConfig("pm_clear_requested_at", "2026-10-07T13:00:00.000Z"); }],
    ["latch already acknowledged", (_pane, _pm, db) => { db.clearPendingClear(0); }],
  ];
  for (const [name, mutate] of cases) {
    const { db, directory } = freshDb("mop-pmclear-drift-");
    try {
      latch(db);
      const pm = pmState();
      const pane = fakePane();
      // Drift lands between the caller's decision and the writer's turn.
      pane.onFirstProbe = () => mutate(pane, pm, db);
      if (name === "session rebound" || name.startsWith("request") || name.startsWith("latch")) mutate(pane, pm, db);
      const result = await relayFor(pane, db).writePMClearFenced({ paneId: "%1", check: fenceFor(db, pm) });
      assert.equal(result.ok, false, name);
      assert.equal(result.zeroEffect, true, name);
      assert.equal(result.busy, false, name);
      assert.deepEqual(effects(pane), { pastes: [], enters: [] }, name);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  }
});

function latchOptions(db: MoPDatabase, nowMs: number, sends: { n: number }, send = async () => {
  sends.n += 1;
  return { success: true };
}) {
  return {
    db,
    source: "test",
    nowMs,
    staleAfterMs: 10 * 60_000,
    recentSuppressMs: 10 * 60_000,
    hasRecentClearEvent: false,
    laterLifecycleEvent: null as { id: number; event_type: string; timestamp: string } | null,
    send,
  };
}

function laterStop(db: MoPDatabase) {
  db.logEvent(0, "pm_status_idle_drained", null, null, { event: "stop" });
  return db.getEvents(0, 1, "pm_status_idle_drained")[0]!;
}

test("Stop submits a deferred clear once; a later stale drain never resubmits it", async () => {
  const { db, directory } = freshDb("mop-pmclear-stop-drain-");
  try {
    latch(db, "deferred_busy");
    const sends = { n: 0 };
    const start = Date.parse(REQUESTED_AT) + 11 * 60_000;
    const stop = await retryDeferredPmClearOnStop({
      ...latchOptions(db, start, sends),
      backoffMs: 60_000,
      settleMs: 0,
      wait: async () => {},
      isCurrent: () => true,
      isIdleProven: () => true,
    });
    assert.equal(stop.kind, "sent");
    assert.equal(db.getConfig("pm_clear_delivery_state"), "awaiting_ack");

    // Drain 30s later, no SessionStart:clear ACK, crossed by a later lifecycle event.
    await new Promise((r) => setTimeout(r, 5));
    const evidence = laterStop(db);
    const drained = await waitForPmIdleDrain({
      afterEventId: 0,
      getDrainEvent: () => ({ id: evidence.id + 1 }),
      isPMBusy: () => false,
      isCurrent: () => true,
      wait: async () => {},
      onDrain: async () => {
        const again = await requestPmClearOnce({
          ...latchOptions(db, Date.now() + 30 * 60_000, sends),
          laterLifecycleEvent: evidence,
          isPMBusy: () => false,
        });
        assert.equal(again.kind, "pending");
      },
    });
    assert.equal(drained, true);
    assert.equal(sends.n, 1);
    assert.equal(db.hasPendingClear(0), true);
    assert.equal(db.getEvents(0, 10, "clear_pending_stale_repaired").length, 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a lost Enter ACK keeps the possible-effect claim; Stop, drain, backoff and restart never resend", async () => {
  const { db, directory, dbPath } = freshDb("mop-pmclear-lost-ack-");
  try {
    latch(db, "deferred_busy");
    const sends = { n: 0 };
    const now = Date.parse(REQUESTED_AT) + 60_000;
    const ambiguous = async () => {
      sends.n += 1;
      return { success: false, error: "mop_send_path_error: socket hang up" };
    };
    const first = await retryDeferredPmClearOnStop({
      ...latchOptions(db, now, sends, ambiguous),
      backoffMs: 60_000,
      settleMs: 0,
      wait: async () => {},
      isCurrent: () => true,
      isIdleProven: () => true,
    });
    assert.equal(first.kind, "uncertain");
    assert.equal(db.hasPendingClear(0), true, "ambiguous ACK must not erase the claim");
    assert.equal(db.getConfig("pm_clear_delivery_state"), "uncertain");

    const reopened = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
    for (const handle of [db, reopened]) {
      for (const offset of [2 * 60_000, 15 * 60_000, 60 * 60_000]) {
        const evidence = laterStop(handle);
        const stopRetry = await retryDeferredPmClearOnStop({
          ...latchOptions(handle, now + offset, sends),
          laterLifecycleEvent: evidence,
          backoffMs: 60_000,
          settleMs: 0,
          wait: async () => {},
          isCurrent: () => true,
          isIdleProven: () => true,
        });
        assert.equal(stopRetry.kind, "skipped");
        const drain = await requestPmClearOnce({
          ...latchOptions(handle, now + offset, sends),
          laterLifecycleEvent: evidence,
          isPMBusy: () => false,
        });
        assert.equal(drain.kind, "pending");
      }
    }
    assert.equal(sends.n, 1, "at most one possible clear");
    assert.equal(reopened.getEvents(0, 10, "pm_clear_ack_missing").length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("a restart mid-send leaves a durable 'sending' claim that is never resent", async () => {
  const { db, directory, dbPath } = freshDb("mop-pmclear-restart-");
  try {
    latch(db, "sending");
    const sends = { n: 0 };
    const restarted = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath });
    const evidence = laterStop(restarted);
    const result = await requestPmClearOnce({
      ...latchOptions(restarted, Date.parse(REQUESTED_AT) + 20 * 60_000, sends),
      laterLifecycleEvent: evidence,
      isPMBusy: () => false,
    });
    assert.equal(result.kind, "pending");
    assert.equal(sends.n, 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("only a proven zero-effect busy refusal defers; an unproven busy is uncertain", async () => {
  const { db, directory } = freshDb("mop-pmclear-busy-proof-");
  try {
    latch(db, "deferred_busy");
    const sends = { n: 0 };
    const unproven = await requestPmClearOnce({
      ...latchOptions(db, Date.now(), sends, async () => {
        sends.n += 1;
        return { success: false, busy: true };
      }),
      isPMBusy: () => false,
    });
    assert.equal(unproven.kind, "uncertain");
    assert.equal(db.getConfig("pm_clear_delivery_state"), "uncertain");

    latch(db, "deferred_busy");
    const proven = await requestPmClearOnce({
      ...latchOptions(db, Date.now(), sends, async () => {
        sends.n += 1;
        return { success: false, busy: true, zeroEffect: true };
      }),
      isPMBusy: () => false,
    });
    assert.equal(proven.kind, "deferred_busy");
    assert.equal(db.getConfig("pm_clear_delivery_state"), "deferred_busy");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("Stop settle never adopts a replacement request, and settle never touches a replaced latch", async () => {
  const { db, directory } = freshDb("mop-pmclear-replaced-");
  try {
    latch(db, "deferred_busy");
    const sends = { n: 0 };
    const replacedAt = "2026-10-07T12:45:00.000Z";
    const result = await retryDeferredPmClearOnStop({
      ...latchOptions(db, Date.parse(REQUESTED_AT) + 60_000, sends),
      backoffMs: 60_000,
      settleMs: 2000,
      wait: async () => latch(db, "deferred_busy", replacedAt),
      isCurrent: () => true,
      isIdleProven: () => true,
    });
    assert.equal(result.kind, "skipped");
    assert.equal(sends.n, 0);

    // A write in flight for request A must not settle a replacement B.
    latch(db, "deferred_busy");
    const inflight = await requestPmClearOnce({
      ...latchOptions(db, Date.now(), sends, async () => {
        sends.n += 1;
        latch(db, "deferred_busy", replacedAt);
        return { success: true };
      }),
      isPMBusy: () => false,
    });
    assert.equal(inflight.kind, "sent");
    assert.equal(db.getConfig("pm_clear_requested_at"), replacedAt);
    assert.equal(db.getConfig("pm_clear_delivery_state"), "deferred_busy");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("compiled /send route routes every PM self-clear through the fenced writer before any pane key", () => {
  const source = readFileSync(new URL("../dist/server.js", import.meta.url), "utf8");
  const route = source.slice(source.indexOf('app.post("/slots/:slotNum/send"'));
  const fenced = route.indexOf("relay.writePMClearFenced(");
  const firstGate = route.indexOf("paneExists(paneTarget)");
  const normalKey = route.indexOf("send-keys -t ${paneTarget} i");
  assert.ok(fenced > 0, "fenced writer is used");
  assert.ok(fenced < firstGate && fenced < normalKey, "PM clear returns before any other gate or key");
  assert.match(route, /pm_clear_authority_missing/);
  assert.doesNotMatch(route, /allowPmClear && relay\.isPMBusy\(\)/);
  for (const via of ["pm_status_stop_retry", "pm_status_stop", "options.source"]) {
    assert.ok(source.includes(`sendClearViaMopSendPath(0, ${via.includes(".") ? via : `"${via}"`}, {`), via);
  }
});

test("fix 1: authority is revalidated after the buffer load, immediately before the paste", async () => {
  const { db, directory } = freshDb("mop-pmclear-atpaste-");
  try {
    latch(db);
    const pm = pmState();
    const pane = fakePane();
    const relay = relayFor(pane, db);
    const origLoad = pane;
    // A new PM turn starts while the file/buffer awaits are in flight.
    const wrapped = (relay as unknown as { runShell: (c: string, o?: unknown) => Promise<{ stdout: string; stderr: string }> });
    const inner = wrapped.runShell;
    wrapped.runShell = async (c: string, o?: unknown) => {
      const r = await inner(c, o);
      if (c.startsWith("tmux load-buffer")) pm.generation += 1;
      return r;
    };
    const result = await relay.writePMClearFenced({ paneId: "%1", check: fenceFor(db, pm) });
    assert.equal(result.ok, false);
    assert.equal(result.zeroEffect, true);
    assert.deepEqual(effects(origLoad), { pastes: [], enters: [] });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("fix 2: a same-generation Stop without proven idle never writes", async () => {
  const { db, directory } = freshDb("mop-pmclear-stopidle-");
  try {
    latch(db);
    const pm = pmState();
    const pane = fakePane();
    const result = await relayFor(pane, db).writePMClearFenced({ paneId: "%1", check: fenceFor(db, pm, () => false) });
    assert.equal(result.ok, false);
    assert.equal(result.busy, true);
    assert.deepEqual(effects(pane), { pastes: [], enters: [] });

    latch(db, "deferred_busy");
    const sends = { n: 0 };
    const retry = await retryDeferredPmClearOnStop({
      ...latchOptions(db, Date.parse(REQUESTED_AT) + 60_000, sends),
      backoffMs: 60_000,
      settleMs: 0,
      wait: async () => {},
      isCurrent: () => true,
      isIdleProven: () => false,
    });
    assert.equal(retry.kind, "deferred_busy");
    assert.equal(sends.n, 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("fix 3: a post-paste refusal settles the latch as uncertain and is never resent", async () => {
  const { db, directory } = freshDb("mop-pmclear-postpaste-latch-");
  try {
    latch(db, "deferred_busy");
    const pm = pmState();
    const pane = fakePane({ onPaste: () => { pm.generation += 1; } });
    const relay = relayFor(pane, db);
    const sends = { n: 0 };
    const send = async () => {
      sends.n += 1;
      const w = await relay.writePMClearFenced({ paneId: "%1", check: fenceFor(db, pm) });
      return { success: w.ok, busy: w.busy, zeroEffect: w.zeroEffect, error: w.reason };
    };
    const first = await requestPmClearOnce({ ...latchOptions(db, Date.now(), sends, send), isPMBusy: () => false });
    assert.equal(first.kind, "uncertain");
    assert.equal(db.getConfig("pm_clear_delivery_state"), "uncertain");
    const again = await retryDeferredPmClearOnStop({
      ...latchOptions(db, Date.now() + 120_000, sends, send),
      backoffMs: 0,
      settleMs: 0,
      wait: async () => {},
      isCurrent: () => true,
      isIdleProven: () => true,
    });
    assert.equal(again.kind, "skipped");
    assert.equal(sends.n, 1);
    assert.equal(effects(pane).enters.length, 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
