import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { evaluateLaneEvents, readLaneEvents } from "../src/laneEvents.js";
import type { MoPDatabase } from "../src/db.js";

function fixture() {
  let raw: string | null = null;
  let writes = 0;
  const slot = { slot: 1, occupied: true, issue: 12, pr: null, branch: "feature" as string | null, assigned_at: "2026-01-01T00:00:00.000Z", assignment_epoch: 1 };
  const db = { getConfig: () => raw, setConfig: (_key: string, value: string) => { raw = value; writes++; }, getAllSlots: () => [{ ...slot }], getSlot: () => ({ ...slot }) } as unknown as MoPDatabase;
  return { db, slot, writes: () => writes };
}

describe("lane events", () => {
  it("read-only access does not initialize and activation baselines passed thresholds", async () => {
    const f = fixture();
    assert.deepEqual(readLaneEvents(f.db), []);
    assert.equal(f.writes(), 0);
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T03:45:00Z"));
    assert.deepEqual(readLaneEvents(f.db), []);
  });
  it("emits once across assignment epoch repins and makes an overrun obligation", async () => {
    const f = fixture();
    const calls: string[][] = [];
    const runner = (async (_file: string, args: string[]) => {
      calls.push(args);
      return { stdout: args[0] === "pr" ? "[]" : "3", stderr: "" };
    }) as never;
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T00:00:00Z"), runner);
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T03:30:00Z"), runner);
    assert.deepEqual(readLaneEvents(f.db).map(e => [e.threshold, e.severity]), [["lane_no_pr", undefined], ["lane_overrun", "high"], ["lane_close_reimplement_due", undefined]]);
    assert.equal(calls.filter(c => c.includes("obligation-upsert")).length, 1);
    f.slot.assignment_epoch++;
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T03:31:00Z"), runner);
    assert.equal(readLaneEvents(f.db).length, 3);
  });
  it("matches issue fallback against branch names, not PR content", async () => {
    const f = fixture();
    const runner = (async (_file: string, args: string[]) => ({ stdout: args.includes("--head") ? "[]" : args[0] === "pr" ? '[{"number":8,"isDraft":true,"headRefName":"fix/issue-12"},{"number":9,"isDraft":false,"headRefName":"fix/issue-120"}]' : "4", stderr: "" })) as never;
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T00:00:00Z"), runner);
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T03:00:00Z"), runner);
    assert.deepEqual(readLaneEvents(f.db).map(e => [e.threshold, e.pr]), [["lane_overrun", 8]]);
  });
  it("falls back by issue when the current branch is missing", async () => {
    const f = fixture();
    f.slot.branch = null;
    const calls: string[][] = [];
    const runner = (async (_file: string, args: string[]) => {
      calls.push(args);
      return { stdout: args[0] === "pr" ? '[{"number":8,"isDraft":true,"headRefName":"fix/issue-12"}]' : "", stderr: "" };
    }) as never;
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T00:00:00Z"), runner);
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T03:00:00Z"), runner);
    assert.deepEqual(readLaneEvents(f.db).map(e => [e.threshold, e.pr, e.commit_count]), [["lane_overrun", 8, null]]);
    assert.equal(calls.some(args => args.includes("--head") || args[0] === "api"), false);
  });
  it("drops stale work when assignment changes during GitHub lookup", async () => {
    const f = fixture();
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T00:00:00Z"));
    const calls: string[][] = [];
    const runner = (async (_file: string, args: string[]) => {
      calls.push(args);
      if (args[0] === "pr") f.slot.issue = 99;
      return { stdout: "[]", stderr: "" };
    }) as never;
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T03:30:00Z"), runner);
    assert.deepEqual(readLaneEvents(f.db), []);
    assert.equal(calls.some(args => args.includes("obligation-upsert")), false);
  });
  it("does not persist an event if the lane changes during the writer", async () => {
    const f = fixture();
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T00:00:00Z"));
    const runner = (async (_file: string, args: string[]) => {
      if (args.includes("obligation-upsert")) f.slot.issue = 99;
      return { stdout: args[0] === "pr" ? "[]" : "1", stderr: "" };
    }) as never;
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T03:00:00Z"), runner);
    assert.deepEqual(readLaneEvents(f.db), []);
  });
  it("consumes the durable claim before an accepted or unknown writer across restart", async () => {
    for (const outcome of ["accepted-response-lost", "timeout"]) {
      const f = fixture();
      await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T00:00:00Z"));
      let writes = 0;
      const runner = (async (_file: string, args: string[]) => {
        if (args.includes("obligation-upsert")) {
          writes++;
          const state = JSON.parse(f.db.getConfig("lane_events_v1")!);
          assert.ok(state.seen.some((entry: string) => entry.includes("lane_overrun")));
          throw new Error(outcome);
        }
        return { stdout: args[0] === "pr" ? "[]" : "1", stderr: "" };
      }) as never;
      await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T03:00:00Z"), runner);
      await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T03:01:00Z"), runner);
      assert.equal(writes, 1);
      assert.equal(readLaneEvents(f.db).some(e => e.threshold === "lane_overrun"), false);
    }
  });
  it("suppresses a ready PR", async () => {
    const f = fixture();
    const runner = (async (_file: string, args: string[]) => ({ stdout: args[0] === "pr" ? '[{"number":44,"isDraft":false}]' : "1", stderr: "" })) as never;
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T00:00:00Z"), runner);
    await evaluateLaneEvents(f.db, "owner/repo", Date.parse("2026-01-01T04:00:00Z"), runner);
    assert.deepEqual(readLaneEvents(f.db), []);
  });
});
