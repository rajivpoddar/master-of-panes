import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import type { SlotState } from "./types.js";
import type { MoPDatabase } from "./db.js";
import { classifyWriterError } from "./hourlyReportObligation.js";

const exec = promisify(execFile);
const STATE = "lane_events_v1";
const thresholds = [[90, "lane_no_pr"], [180, "lane_overrun"], [210, "lane_close_reimplement_due"]] as const;
type LaneEvent = { slot: number; issue: number; pr: number | null; assigned_at: string; threshold: string; age_minutes: number; commit_count: number | null; severity?: "high" };
type State = { activated: boolean; seen: string[]; events: LaneEvent[] };
const empty = (): State => ({ activated: false, seen: [], events: [] });
const key = (slot: SlotState, pr: number | null, threshold: string) => JSON.stringify([slot.slot, slot.issue, pr, slot.assigned_at, threshold]);

/** Read-only: never initializes state or evaluates thresholds. */
export function readLaneEvents(db: MoPDatabase): LaneEvent[] {
  try { return (JSON.parse(db.getConfig(STATE) || "null") as State | null)?.events ?? []; } catch { return []; }
}

export async function evaluateLaneEvents(db: MoPDatabase, repo: string, now = Date.now(), execute: typeof exec = exec): Promise<void> {
  let state: State;
  try { state = JSON.parse(db.getConfig(STATE) || "null") ?? empty(); } catch { return; }
  const initial = !state.activated;
  const slots = db.getAllSlots();
  state.events = state.events.filter(e => slots.some(s => s.occupied && s.slot === e.slot && s.issue === e.issue && s.assigned_at === e.assigned_at));
  for (const slot of slots) {
    if (!slot.slot || !slot.occupied || !slot.issue || !slot.assigned_at) continue;
    const age = (now - Date.parse(slot.assigned_at)) / 60000;
    if (!Number.isFinite(age) || age < 90) continue;
    if (initial) {
      for (const [minutes, threshold] of thresholds) {
        if (age >= minutes) state.seen.push(key(slot, null, `baseline:${threshold}`));
      }
      continue;
    }
    const branch = slot.branch || slot.branch_ref?.replace(/^refs\/heads\//, "");
    try {
      let prs: Array<{number: number; isDraft: boolean}> = [];
      if (branch) {
        const result = await execute("gh", ["pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,isDraft"], { timeout: 10000 });
        prs = JSON.parse(result.stdout) as Array<{number: number; isDraft: boolean}>;
      }
      if (!prs.length) {
        const fallback = await execute("gh", ["pr", "list", "--repo", repo, "--state", "open", "--limit", "1000", "--json", "number,isDraft,headRefName"], { timeout: 10000 });
        const candidates = JSON.parse(fallback.stdout) as Array<{number: number; isDraft: boolean; headRefName: string}>;
        const issueToken = new RegExp(`(?:^|[^0-9])${slot.issue}(?:[^0-9]|$)`);
        prs = candidates.filter(p => typeof p.headRefName === "string" && issueToken.test(p.headRefName));
        if (prs.length > 1) continue;
      }
      const ready = prs.some(p => !p.isDraft);
      const pr = prs[0]?.number ?? null;
      state.events = state.events.filter(e => e.slot !== slot.slot || (!ready && e.pr === pr && !(e.threshold === "lane_no_pr" && pr !== null)));
      let count: number | null = null;
      if (branch) {
        try {
          const comparison = await execute("gh", ["api", `repos/${repo}/compare/main...${encodeURIComponent(branch)}`, "--jq", ".ahead_by"], { timeout: 10000 });
          const n = Number(comparison.stdout.trim());
          if (Number.isFinite(n)) count = n;
        } catch { /* unknown count is explicitly represented */ }
      }
      for (const [minutes, threshold] of thresholds) {
        if (age < minutes) continue;
        const id = key(slot, pr, threshold);
        if (state.seen.includes(id) || state.seen.includes(key(slot, null, `baseline:${threshold}`))) continue;
        if (ready || (threshold === "lane_no_pr" && pr !== null)) continue;
        const event: LaneEvent = { slot: slot.slot, issue: slot.issue, pr, assigned_at: slot.assigned_at, threshold, age_minutes: Math.floor(age), commit_count: count };
        if (threshold === "lane_overrun") {
          event.severity = "high";
          const target = Buffer.from(id).toString("base64url");
          try {
            const args = [process.env.MOP_PM_OPS_SCRIPT ?? join(homedir(), ".claude/scripts/pm-ops.py"), "obligation-upsert", "--kind", "lane_overrun", "--target-type", "lane", "--target-id", target, "--slot", String(slot.slot), "--issue", String(slot.issue), "--owner", "pm", "--severity", "high", "--horizon", "hourly", "--title", `Lane overrun: slot ${slot.slot} issue #${slot.issue}`, "--action", "PM review lane; user/PM owns close and reimplementation", "--evidence-json", JSON.stringify(event)];
            if (pr !== null) args.push("--pr", String(pr));
            await execute("python3", args, { timeout: 10000 });
          } catch (error) {
            // An ambiguous writer result may already have committed; never replay it.
            if (classifyWriterError(error) === "failed") continue;
          }
        }
        state.seen.push(id);
        state.events.push(event);
      }
    } catch { /* Unknown PR state must not create a false event. */ }
  }
  state.activated = true;
  db.setConfig(STATE, JSON.stringify(state));
}
