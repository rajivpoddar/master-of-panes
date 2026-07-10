/**
 * MoP P0 Escalation Watcher
 *
 * Reads PM-owned pm-ops obligations and wakes PM with a normal prompt when a
 * p0_escalation obligation is due. It does not run slash commands and does not
 * clear obligations; PM owns the alert-processing skill and clears only after
 * Rajiv reply, terminal recovery proof, or Twilio call proof is recorded.
 */

import Database from "better-sqlite3";
import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";

type P0ObligationRow = {
  id: number;
  created_at: string;
  updated_at: string;
  target_type: string | null;
  target_id: string | null;
  title: string | null;
  required_action: string | null;
  evidence_json: string | null;
  next_review_at: string | null;
  suppress_until: string | null;
};

type P0WatchResult = {
  checked: number;
  due: number;
  injected: number;
  skipped: number;
  reason: "scheduled" | "manual" | "boot";
};

const DEFAULT_PM_OPS_DB =
  "/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app/state/pm-ops.db";

function parseMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

function safeEvidence(raw: string | null): Record<string, unknown> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function evidenceText(evidence: Record<string, unknown>, key: string): string | null {
  const value = evidence[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export class P0EscalationWatcher {
  private db: MoPDatabase;
  private relay: TmuxRelay;
  private timer: NodeJS.Timeout | null = null;
  private bootTimer: NodeJS.Timeout | null = null;
  private running = false;

  private readonly PM_OPS_DB = process.env.MOP_PM_OPS_DB ?? DEFAULT_PM_OPS_DB;
  private readonly CHECK_INTERVAL_MS = parseInt(
    process.env.MOP_P0_ESCALATION_CHECK_INTERVAL_MS ?? `${5 * 60 * 1000}`,
    10
  );
  private readonly BOOT_DELAY_MS = parseInt(
    process.env.MOP_P0_ESCALATION_BOOT_DELAY_MS ?? `${60 * 1000}`,
    10
  );
  private readonly DUE_AFTER_MS = parseInt(
    process.env.MOP_P0_ESCALATION_DUE_AFTER_MS ?? `${30 * 60 * 1000}`,
    10
  );
  private readonly RESURFACE_MS = parseInt(
    process.env.MOP_P0_ESCALATION_RESURFACE_MS ?? `${5 * 60 * 1000}`,
    10
  );

  constructor(db: MoPDatabase, relay: TmuxRelay) {
    this.db = db;
    this.relay = relay;
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => {
      void this.tick("scheduled");
    }, this.CHECK_INTERVAL_MS);
    this.bootTimer = setTimeout(() => {
      this.bootTimer = null;
      void this.tick("boot");
    }, this.BOOT_DELAY_MS);
    console.log(
      `[p0-escalation-watch] started — interval ${this.CHECK_INTERVAL_MS}ms, ` +
        `due_after ${this.DUE_AFTER_MS}ms, db=${this.PM_OPS_DB}`
    );
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.bootTimer) {
      clearTimeout(this.bootTimer);
      this.bootTimer = null;
    }
  }

  getStatus(): {
    running: boolean;
    ticking: boolean;
    boot_pending: boolean;
    db_path: string;
    check_interval_ms: number;
    due_after_ms: number;
    resurface_ms: number;
    last_run_ts: string | null;
    last_checked: string | null;
    last_due: string | null;
    last_injected: string | null;
  } {
    return {
      running: this.timer !== null,
      ticking: this.running,
      boot_pending: this.bootTimer !== null,
      db_path: this.PM_OPS_DB,
      check_interval_ms: this.CHECK_INTERVAL_MS,
      due_after_ms: this.DUE_AFTER_MS,
      resurface_ms: this.RESURFACE_MS,
      last_run_ts: this.db.getConfig("p0_escalation_watch_last_run_ts"),
      last_checked: this.db.getConfig("p0_escalation_watch_last_checked"),
      last_due: this.db.getConfig("p0_escalation_watch_last_due"),
      last_injected: this.db.getConfig("p0_escalation_watch_last_injected"),
    };
  }

  async tick(reason: "scheduled" | "manual" | "boot"): Promise<P0WatchResult> {
    if (this.running) {
      return { checked: 0, due: 0, injected: 0, skipped: 0, reason };
    }
    this.running = true;
    try {
      return this.run(reason);
    } finally {
      this.running = false;
    }
  }

  private run(reason: "scheduled" | "manual" | "boot"): P0WatchResult {
    const nowMs = Date.now();
    const nowIso = new Date(nowMs).toISOString();
    let rows: P0ObligationRow[] = [];
    try {
      const pmOpsDb = new Database(this.PM_OPS_DB, { readonly: true, fileMustExist: true });
      rows = pmOpsDb.prepare(`
        SELECT id, created_at, updated_at, target_type, target_id, title,
               required_action, evidence_json, next_review_at, suppress_until
        FROM obligations
        WHERE status='open' AND kind='p0_escalation'
        ORDER BY COALESCE(next_review_at, created_at) ASC, id ASC
        LIMIT 20
      `).all() as P0ObligationRow[];
      pmOpsDb.close();
    } catch (err) {
      this.db.logEvent(0, "p0_escalation_watch_error", null, null, {
        error: err instanceof Error ? err.message.slice(0, 300) : String(err),
        db: this.PM_OPS_DB,
      });
      return { checked: 0, due: 0, injected: 0, skipped: 0, reason };
    }

    let due = 0;
    let injected = 0;
    let skipped = 0;
    for (const row of rows) {
      const suppressMs = parseMs(row.suppress_until);
      if (suppressMs !== null && suppressMs > nowMs) {
        skipped++;
        continue;
      }
      const createdMs = parseMs(row.created_at);
      const reviewMs = parseMs(row.next_review_at);
      const dueMs = reviewMs ?? (createdMs !== null ? createdMs + this.DUE_AFTER_MS : nowMs);
      if (dueMs > nowMs) {
        skipped++;
        continue;
      }

      due++;
      const throttleKey = `p0_escalation_watch_last_surface_${row.id}`;
      const lastSurfaceMs = parseMs(this.db.getConfig(throttleKey));
      if (reason !== "manual" && lastSurfaceMs !== null && nowMs - lastSurfaceMs < this.RESURFACE_MS) {
        skipped++;
        continue;
      }

      const message = this.renderPrompt(row);
      const ok = this.relay.injectToPM(message);
      this.db.setConfig(throttleKey, nowIso);
      this.db.logEvent(0, "p0_escalation_watch_prompt", null, null, {
        obligation_id: row.id,
        target_type: row.target_type,
        target_id: row.target_id,
        injected: ok,
        reason,
        delivery_mode: "queued-normal-prompt",
      });
      if (ok) injected++;
    }

    this.db.setConfig("p0_escalation_watch_last_run_ts", nowIso);
    this.db.setConfig("p0_escalation_watch_last_checked", String(rows.length));
    this.db.setConfig("p0_escalation_watch_last_due", String(due));
    this.db.setConfig("p0_escalation_watch_last_injected", String(injected));
    return { checked: rows.length, due, injected, skipped, reason };
  }

  private renderPrompt(row: P0ObligationRow): string {
    const evidence = safeEvidence(row.evidence_json);
    const threadTs = evidenceText(evidence, "thread_ts") ?? row.target_id ?? "<thread_ts>";
    const channel = evidenceText(evidence, "channel") ?? "<channel>";
    const title = row.title ?? `P0 escalation obligation #${row.id}`;
    const action = row.required_action ?? "Invoke Skill(alert-processing); if no Rajiv reply or terminal recovery proof exists, run the escalation-call script.";
    return [
      "MoP: P0 escalation due",
      "",
      `Invoke Skill(alert-processing) now for p0_escalation obligation #${row.id}. Launch any investigation work in the skill as needed, then return to normal PM event processing. Do not run a slash command inline.`,
      "",
      `Alert thread: ${channel} ${threadTs}`,
      `Title: ${title}`,
      `Required action: ${action}`,
      "",
      `If the P0 is unresolved and Rajiv has not replied in the alert thread, run: cd /Users/rajiv/Downloads/projects/heydonna-app && bash scripts/escalation-call.sh "P0: production incident unresolved; alert thread ${threadTs}"`,
      "Afterward, reply in the originating alert thread with Twilio call SID/status, owner, and next action. Clear the pm-ops p0_escalation obligation only after Rajiv reply, terminal recovery proof, or call proof is recorded.",
    ].join("\n");
  }
}
