import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import type { MoPDatabase } from "../src/db.js";
import type { LogManager } from "../src/logs.js";
import { TmuxRelay } from "../src/relay.js";
import { StuckDetector } from "../src/stuck.js";
import { DEFAULT_CONFIG, type EventLogEntry, type SlotState } from "../src/types.js";
delete process.env.MOP_PM_WAIT_NUDGES_DISABLED;
process.env.MOP_STALE_ACTIVE_NUDGE_ENABLED = "1";
process.env.MOP_PM_WAIT_NUDGE_INTERVAL_MS = "300000";
const CHECKOUT = "/Users/rajiv/Downloads/projects/heydonna-app-3001";
const RULE = "─".repeat(40);
const ANCHOR = new Date(Date.now() - 25 * 60_000);
function harness() {
 const s = { slot: 1, address: "0:0.1", occupied: true, dnd: false, session_id: "s1",
 repository_id: "github:heydonna-app/heydonna-app", issue: 8796, branch: "fix/8796",
 branch_ref: "refs/heads/fix/8796", assignment_epoch: 826,
 assigned_at: "2026-10-04T07:00:00.000Z", active_turn_state: "active", active_turn_id: "t1" } as SlotState;
 const p = { composer: "", buffer: "", pastes: 0, enters: 0, identities: 0, activity: "idle",
 ackLoss: false, subagent: false, nextPaneId: "%11", mtime: ANCHOR.getTime(), guardedCalls: 0,
 onIdentity: (_n: number) => {}, onPaste: () => {} };
 const render = () => `⏺ Done. Owner / next action: PM\n${RULE}\n❯ ${p.composer}\n${RULE}\n ? for shortcuts\n`;
 const events: EventLogEntry[] = [];
 const db = {
 getExitPending: () => false, hasPendingClear: () => false, hasActiveNativeReleaseIntent: () => false,
 claimNativeReleaseIntent: () => true, clearNativeReleaseIntent: () => undefined,
 hasRecentSubagentDispatch: () => p.subagent ? { taskTs: "recent" } : null,
 getSlot: () => s, getAllSlots: () => [s],
 getEvents: (_s: number, limit: number, type?: string) => events.filter(e => !type || e.event_type === type).slice(-limit).reverse(),
 logEvent: (slot: number, event_type: string, hook_type: string | null, tool_name: string | null, payload: unknown) => {
 events.push({ id: events.length + 1, timestamp: new Date().toISOString(), slot, event_type,
 hook_type, tool_name, payload: JSON.stringify(payload), processed: false }); return events.length; }
 } as unknown as MoPDatabase;
 const runShell = async (cmd: string) => {
 if (cmd.startsWith("tmux display-message")) { p.onIdentity(++p.identities); return { stdout: `${p.nextPaneId}|${CHECKOUT}\n`, stderr: "" }; }
 if (cmd.startsWith("git -C")) return { stdout: CHECKOUT, stderr: "" };
 if (cmd.startsWith("tmux capture-pane")) {
 if (p.ackLoss && p.enters > 0) throw new Error("capture timeout after Enter");
 if (cmd.includes(" -e ")) {
 if (p.activity === "unknown") throw new Error("capture unavailable");
 if (p.activity === "missing") return { stdout: "", stderr: "" };
 if (p.activity === "inherited-active") return { stdout: "\x1b[38;2;153;153;153m" + render(), stderr: "" };
 return { stdout: render().replace("❯", p.activity === "active" ? "\x1b[38;2;153;153;153m❯\x1b[0m" : "❯"), stderr: "" }; }
 return { stdout: render(), stderr: "" }; }
 if (cmd.startsWith("tmux load-buffer")) { p.buffer = await readFile(cmd.match(/'([^']+)'\s*$/)![1], "utf8"); return { stdout: "", stderr: "" }; }
 if (cmd.startsWith("tmux paste-buffer")) { p.pastes++; p.composer = p.buffer; p.onPaste(); return { stdout: "", stderr: "" }; }
 if (/^tmux send-keys .* Enter$/.test(cmd)) { p.enters++; p.composer = ""; return { stdout: "", stderr: "" }; }
 throw new Error(`Unexpected shell: ${cmd}`);
 };
 const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: runShell as never });
 // Simulate the reviewed legacy observer's unknown->idle collapse for RED;
 // neither the legacy nor corrected test may read a real slot pane.
 relay.captureOutput = async () => ({ output: render(), activity: p.activity === "active" ? "busy" : "idle" });
 // Isolate the legacy pre-lock numeric probe. The actual pinned probe and
 // guarded delivery remain real; no test reads or sends to a live pane.
 const probe = relay.getSlotActivityState.bind(relay) as (slot: number, paneId?: string) => Promise<"idle" | "active" | "unknown">;
 relay.getSlotActivityState = async (slot, paneId) => paneId ? probe(slot, paneId) : "idle";
 // REAL guarded delivery and composer submit: only shorten the simulated clock.
 const deliver = relay.deliverGuardedToSlot.bind(relay);
 relay.deliverGuardedToSlot = (slot, command, opts) => {
 p.guardedCalls++;
 return deliver(slot, command, {
 ...opts, timing: { dwellMs: 1, pollMs: 1, payloadGraceMs: 10, payloadStableMs: 1, clearGraceMs: 5 } });
 };
 const det = new StuckDetector(db, { getLogMtime: async () => new Date(p.mtime) } as unknown as LogManager, relay);
 return { p, s, events, relay, check: () => det.checkStaleActiveTurn(s) };
}

test("control: verified idle inside send lock gets one paste and one Enter", async () => {
 const h = harness(); await h.check(); assert.equal(h.p.pastes, 1); assert.equal(h.p.enters, 1);
});
for (const state of ["unknown", "active", "missing", "inherited-active"]) test(state + " at in-lock pre-paste boundary refuses all effects", async () => {
 const h = harness(); h.p.onIdentity = n => { if (n === 2) h.p.activity = state; };
 await h.check(); assert.equal(h.p.guardedCalls, 1);
 assert.equal(h.p.pastes, 0); assert.equal(h.p.enters, 0);
});
for (const state of ["unknown", "active", "inherited-active"]) test(state + " after paste prevents Enter and is not retried", async () => {
 const h = harness(); h.p.onPaste = () => { h.p.activity = state; };
 await h.check(); assert.equal(h.p.pastes, 1); assert.equal(h.p.enters, 0);
 assert.ok(h.events.some(e => e.event_type === "stale_active_stall_nudge_uncertain"));
 h.p.activity = "idle"; h.p.composer = ""; h.p.onPaste = () => {}; await h.check();
 assert.equal(h.p.pastes, 1); assert.equal(h.p.enters, 0);
});
