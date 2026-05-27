/**
 * MoP Ops Audit Scheduler — Hourly PM-pane ops review
 *
 * Owns: cadence (1h interval), no-overlap lock, PM busy queueing (via TmuxRelay),
 * trace logging, payload sanitization before PM injection (R4). Does NOT own:
 * business logic of the audit (delegated to ~/.claude/scripts/hourly-ops-review-bg.sh
 * — bounded Codex + local-signal call).
 *
 * Flow per tick:
 *   1. Acquire in-process lock (skip if previous tick still running).
 *   2. Check db.getConfig("ops_audit_paused") — skip if "true".
 *   3. Spawn hourly-ops-review-bg.sh with --reason scheduled. Capture stdout.
 *   4. Parse first line: INJECT_DECISION:<inject|skip> REASON:<short>.
 *      - skip → log skip event, write last-run timestamp, exit.
 *      - inject → sanitizePMPayload(raw) strips internal scheduler envelope
 *        (INJECT_DECISION line, `---` separators, `-- META:` trailer, legacy
 *        OWNER:dhruv/PM/Dhruv prefixes) and emits the clean R4 body shape:
 *          MoP: hourly ops audit                       (stable title — no reason)
 *          (blank line)
 *          REASON: <parsed reason>
 *          (blank line)
 *          ACTION_ITEMS:
 *          <ownerless-by-default items>
 *          (blank line)
 *          NEXT: invoke Skill(hourly-ops-audit) to process these action items.
 *        Then relay.injectToPM(clean) is called. INJECT_DECISION remains the
 *        internal bg-script → scheduler control channel — PM never sees it.
 *   5. Write last-run/status to DB config keys.
 *   6. Bound total wall-clock at BG_SCRIPT_TIMEOUT_MS (default 180s, allowing
 *      Codex 120s + overhead).
 *
 * Failure modes (all logged to mop-server log + DB events):
 *   - bg_script_not_found     — missing or non-executable bg-script
 *   - bg_script_timeout       — child process exceeded BG_SCRIPT_TIMEOUT_MS
 *   - bg_script_error         — non-zero exit
 *   - mop_db_locked           — config write/read failed (returned by sqlite)
 *   - malformed_first_line    — payload missing INJECT_DECISION prefix
 *   - payload_too_large       — > PAYLOAD_HARD_CAP bytes (still emitted truncated)
 *
 * Created: 2026-05-26 per Rajiv CTO directive thread C0ALZJHGE49/1779790681.847219.
 * Companion bg-script: ~/.claude/scripts/hourly-ops-review-bg.sh
 * Companion skill: ~/.claude/skills/hourly-ops-audit/SKILL.md
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import os from "node:os";
import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";

/**
 * bg-script path. Honors OPS_AUDIT_BG_SCRIPT_OVERRIDE env for deterministic tests
 * (R1 fix 3 — Rajiv directive 2026-05-26 16:29 IST thread `1779790681.847219`:
 * "Real Codex invocation + PM injection during `npm test` is unacceptable.").
 * Resolved at module load — process.env is stable for the scheduler lifetime.
 */
const BG_SCRIPT_PATH = process.env.OPS_AUDIT_BG_SCRIPT_OVERRIDE
  ?? path.join(os.homedir(), ".claude", "scripts", "hourly-ops-review-bg.sh");

// Config keys persisted in MoP DB (config table is KV).
const CFG_PAUSED = "ops_audit_paused";
const CFG_LAST_RUN_TS = "ops_audit_last_run_ts";
const CFG_LAST_RUN_STATUS = "ops_audit_last_run_status";
const CFG_LAST_RUN_REASON = "ops_audit_last_run_reason";
const CFG_LAST_RUN_DECISION = "ops_audit_last_run_decision";
const CFG_LAST_RUN_ELAPSED_MS = "ops_audit_last_run_elapsed_ms";
const CFG_LAST_RUN_PAYLOAD_BYTES = "ops_audit_last_run_payload_bytes";

export type OpsAuditTickReason = "scheduled" | "manual" | "boot";

export interface OpsAuditTickResult {
  decision: "inject" | "skip" | "error";
  reason: string;
  elapsedMs: number;
  payloadBytes: number;
  injected: boolean;
  truncated: boolean;
}

export class OpsAuditScheduler {
  private db: MoPDatabase;
  private relay: TmuxRelay;
  private timer: NodeJS.Timeout | null = null;
  private bootCatchupTimer: NodeJS.Timeout | null = null;
  private running: boolean = false; // in-process lock

  // 1-hour cadence. Configurable via env for tests.
  private readonly TICK_INTERVAL_MS: number = parseInt(
    process.env.MOP_OPS_AUDIT_INTERVAL_MS ?? `${60 * 60 * 1000}`,
    10
  );
  // bg-script + Codex (Codex bounded at 120s inside bg-script).
  private readonly BG_SCRIPT_TIMEOUT_MS: number = parseInt(
    process.env.MOP_OPS_AUDIT_BG_TIMEOUT_MS ?? `${180 * 1000}`,
    10
  );
  // Inject payload hard cap (truncate above this).
  private readonly PAYLOAD_HARD_CAP: number = parseInt(
    process.env.MOP_OPS_AUDIT_PAYLOAD_CAP ?? "32768",
    10
  );
  private readonly BOOT_CATCHUP_DELAY_MS: number = parseInt(
    process.env.MOP_OPS_AUDIT_BOOT_DELAY_MS ?? `${30 * 1000}`,
    10
  );

  constructor(db: MoPDatabase, relay: TmuxRelay) {
    this.db = db;
    this.relay = relay;
  }

  /** Start the hourly scheduler. Safe to call multiple times (idempotent). */
  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => {
      this.tick("scheduled").catch((err) => {
        console.error("[ops-audit] tick threw:", err);
      });
    }, this.TICK_INTERVAL_MS);
    console.log(
      `[ops-audit] Scheduler started — interval ${this.TICK_INTERVAL_MS}ms, ` +
        `bg-script ${BG_SCRIPT_PATH}, timeout ${this.BG_SCRIPT_TIMEOUT_MS}ms`
    );
    this.bootCatchupTimer = setTimeout(() => {
      this.tickBootCatchup().catch((err) => {
        console.error("[ops-audit] boot catch-up threw:", err);
      });
    }, this.BOOT_CATCHUP_DELAY_MS);
  }

  /** Stop the scheduler (for clean shutdown). */
  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.bootCatchupTimer) {
      clearTimeout(this.bootCatchupTimer);
      this.bootCatchupTimer = null;
    }
  }

  private async tickBootCatchup(): Promise<void> {
    this.bootCatchupTimer = null;
    if (this.db.getConfig(CFG_PAUSED) === "true") {
      console.log("[ops-audit] boot catch-up skipped — paused");
      return;
    }

    const lastRunRaw = this.db.getConfig(CFG_LAST_RUN_TS);
    const lastRunMs = lastRunRaw ? Date.parse(lastRunRaw) : NaN;
    const ageMs = Number.isFinite(lastRunMs) ? Date.now() - lastRunMs : Infinity;
    if (ageMs < this.TICK_INTERVAL_MS) {
      console.log(`[ops-audit] boot catch-up skipped — last run age_ms=${ageMs}`);
      return;
    }

    console.log(`[ops-audit] boot catch-up running — last_run=${lastRunRaw ?? "none"} age_ms=${ageMs}`);
    await this.tick("boot");
  }

  /** Status snapshot for MCP/HTTP reporting. */
  getStatus(): {
    paused: boolean;
    running: boolean;
    interval_ms: number;
    bg_script_path: string;
    bg_script_present: boolean;
    last_run_ts: string | null;
    last_run_status: string | null;
    last_run_reason: string | null;
    last_run_decision: string | null;
    last_run_elapsed_ms: number | null;
    last_run_payload_bytes: number | null;
  } {
    const last_elapsed = this.db.getConfig(CFG_LAST_RUN_ELAPSED_MS);
    const last_bytes = this.db.getConfig(CFG_LAST_RUN_PAYLOAD_BYTES);
    return {
      paused: this.db.getConfig(CFG_PAUSED) === "true",
      running: this.running,
      interval_ms: this.TICK_INTERVAL_MS,
      bg_script_path: BG_SCRIPT_PATH,
      bg_script_present: existsSync(BG_SCRIPT_PATH),
      last_run_ts: this.db.getConfig(CFG_LAST_RUN_TS),
      last_run_status: this.db.getConfig(CFG_LAST_RUN_STATUS),
      last_run_reason: this.db.getConfig(CFG_LAST_RUN_REASON),
      last_run_decision: this.db.getConfig(CFG_LAST_RUN_DECISION),
      last_run_elapsed_ms: last_elapsed ? parseInt(last_elapsed, 10) : null,
      last_run_payload_bytes: last_bytes ? parseInt(last_bytes, 10) : null,
    };
  }

  /** Toggle pause state. Persisted in DB. */
  setPaused(paused: boolean): void {
    this.db.setConfig(CFG_PAUSED, paused ? "true" : "false");
    console.log(`[ops-audit] pause state → ${paused}`);
  }

  /**
   * Run one audit tick. Honors pause + in-process lock.
   * Public so the MCP tool can force a manual run via HTTP.
   */
  async tick(reason: OpsAuditTickReason): Promise<OpsAuditTickResult> {
    const startMs = Date.now();
    // In-process lock — prevent overlap.
    if (this.running) {
      const result: OpsAuditTickResult = {
        decision: "skip",
        reason: "lock_held_prior_tick_running",
        elapsedMs: 0,
        payloadBytes: 0,
        injected: false,
        truncated: false,
      };
      this.logTick(reason, result);
      return result;
    }
    // Pause check (skip for manual — operator override).
    if (reason !== "manual" && this.db.getConfig(CFG_PAUSED) === "true") {
      const result: OpsAuditTickResult = {
        decision: "skip",
        reason: "paused",
        elapsedMs: 0,
        payloadBytes: 0,
        injected: false,
        truncated: false,
      };
      this.logTick(reason, result);
      return result;
    }
    if (!existsSync(BG_SCRIPT_PATH)) {
      const result: OpsAuditTickResult = {
        decision: "error",
        reason: "bg_script_not_found",
        elapsedMs: 0,
        payloadBytes: 0,
        injected: false,
        truncated: false,
      };
      this.logTick(reason, result);
      return result;
    }

    this.running = true;
    try {
      const { stdout, exitCode, timedOut } = await this.runBgScript(reason);
      const elapsedMs = Date.now() - startMs;

      if (timedOut) {
        const result: OpsAuditTickResult = {
          decision: "error",
          reason: "bg_script_timeout",
          elapsedMs,
          payloadBytes: stdout.length,
          injected: false,
          truncated: false,
        };
        this.logTick(reason, result);
        return result;
      }
      if (exitCode !== 0) {
        const result: OpsAuditTickResult = {
          decision: "error",
          reason: `bg_script_exit_${exitCode}`,
          elapsedMs,
          payloadBytes: stdout.length,
          injected: false,
          truncated: false,
        };
        this.logTick(reason, result);
        return result;
      }

      const parsed = parseFirstLine(stdout);
      if (!parsed) {
        const result: OpsAuditTickResult = {
          decision: "error",
          reason: "malformed_first_line",
          elapsedMs,
          payloadBytes: stdout.length,
          injected: false,
          truncated: false,
        };
        this.logTick(reason, result);
        return result;
      }

      if (parsed.decision === "skip") {
        const result: OpsAuditTickResult = {
          decision: "skip",
          reason: parsed.reason,
          elapsedMs,
          payloadBytes: stdout.length,
          injected: false,
          truncated: false,
        };
        this.logTick(reason, result);
        return result;
      }

      // inject — R4: sanitize bg-script envelope before PM sees the payload.
      // PM-facing title is stable (no reason); reason moves into body REASON: field.
      // Per Rajiv CTO directive 2026-05-26 18:47 IST thread `1779790681.847219`:
      // strip INJECT_DECISION / `---` / `-- META:` / legacy OWNER:dhruv|PM|Dhruv prefixes.
      let cleanBody = sanitizePMPayload(stdout, parsed.reason);
      let truncated = false;
      if (cleanBody.length > this.PAYLOAD_HARD_CAP) {
        cleanBody = cleanBody.slice(0, this.PAYLOAD_HARD_CAP) + "\n-- TRUNCATED (mop-side hard cap)";
        truncated = true;
      }
      const message = cleanBody;
      const injected = this.relay.injectToPM(message);
      // payloadBytes accounting reflects the PM-facing message size, not raw stdout.
      const payloadBytes = message.length;
      const result: OpsAuditTickResult = {
        decision: "inject",
        reason: parsed.reason,
        elapsedMs,
        payloadBytes,
        injected,
        truncated,
      };
      this.logTick(reason, result);
      return result;
    } finally {
      this.running = false;
    }
  }

  /** Persist run summary in DB config + emit event row for history. */
  private logTick(triggerReason: OpsAuditTickReason, r: OpsAuditTickResult): void {
    const ts = new Date().toISOString();
    try {
      this.db.setConfig(CFG_LAST_RUN_TS, ts);
      this.db.setConfig(CFG_LAST_RUN_STATUS, r.decision === "error" ? "error" : "ok");
      this.db.setConfig(CFG_LAST_RUN_REASON, r.reason);
      this.db.setConfig(CFG_LAST_RUN_DECISION, r.decision);
      this.db.setConfig(CFG_LAST_RUN_ELAPSED_MS, String(r.elapsedMs));
      this.db.setConfig(CFG_LAST_RUN_PAYLOAD_BYTES, String(r.payloadBytes));
    } catch (err) {
      console.error("[ops-audit] config write failed:", err);
    }
    try {
      this.db.logEvent(0, "ops_audit_tick", null, null, {
        trigger: triggerReason,
        decision: r.decision,
        reason: r.reason,
        elapsed_ms: r.elapsedMs,
        payload_bytes: r.payloadBytes,
        injected: r.injected,
        truncated: r.truncated,
      });
    } catch (err) {
      console.error("[ops-audit] event log failed:", err);
    }
    console.log(
      `[ops-audit] tick trigger=${triggerReason} decision=${r.decision} ` +
        `reason=${r.reason} elapsed_ms=${r.elapsedMs} bytes=${r.payloadBytes} ` +
        `injected=${r.injected} truncated=${r.truncated}`
    );
  }

  /**
   * Spawn the bg-script with bounded stdout + wall-clock kill.
   * Returns { stdout, exitCode, timedOut }.
   */
  private runBgScript(reason: OpsAuditTickReason): Promise<{
    stdout: string;
    exitCode: number;
    timedOut: boolean;
  }> {
    return new Promise((resolve) => {
      const child = spawn(
        "/bin/bash",
        [BG_SCRIPT_PATH, "--reason", reason],
        { stdio: ["ignore", "pipe", "pipe"] }
      );
      let stdoutBuf = "";
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        try {
          child.kill("SIGKILL");
        } catch {
          /* ignore */
        }
      }, this.BG_SCRIPT_TIMEOUT_MS);

      child.stdout.on("data", (chunk: Buffer) => {
        stdoutBuf += chunk.toString("utf8");
        // Hard cap streaming buffer at 2x cap to prevent OOM.
        if (stdoutBuf.length > this.PAYLOAD_HARD_CAP * 4) {
          stdoutBuf = stdoutBuf.slice(0, this.PAYLOAD_HARD_CAP * 4);
        }
      });
      // Drain stderr to /dev/null (bg-script writes its trace to /tmp/mop-ops-audit.log).
      child.stderr.on("data", () => {
        /* ignore */
      });
      child.on("exit", (code) => {
        clearTimeout(timer);
        resolve({
          stdout: stdoutBuf,
          exitCode: code ?? -1,
          timedOut,
        });
      });
      child.on("error", (err) => {
        clearTimeout(timer);
        console.error("[ops-audit] spawn error:", err);
        resolve({ stdout: stdoutBuf, exitCode: -1, timedOut });
      });
    });
  }
}

/**
 * Parse the bg-script's first line.
 * Format: "INJECT_DECISION:<inject|skip> REASON:<short_label>"
 * Returns null on malformed input.
 */
export function parseFirstLine(stdout: string): { decision: "inject" | "skip"; reason: string } | null {
  if (!stdout) return null;
  const firstLine = stdout.split("\n", 1)[0]?.trim() ?? "";
  // Tolerate whitespace, missing space before REASON, missing REASON.
  const match = /^INJECT_DECISION:\s*(inject|skip)(?:\s+REASON:\s*(\S.*))?$/i.exec(firstLine);
  if (!match) return null;
  const decision = match[1].toLowerCase() as "inject" | "skip";
  const reason = (match[2] ?? "unspecified").trim();
  return { decision, reason };
}

/**
 * R4 — Sanitize bg-script payload before PM injection.
 *
 * The bg-script emits a scheduler-control envelope:
 *
 *   INJECT_DECISION:inject REASON:<short>
 *   ---
 *   ACTION_ITEMS:
 *     - ACTION: ...
 *       EVIDENCE: ...
 *       TRANSITION: ...
 *       TARGET: ...
 *   ---
 *   -- META: codex_status=ok elapsed_s=10 reason=ok
 *
 * INJECT_DECISION / `---` / `-- META:` are scheduler-internal — they let the
 * scheduler decide inject-vs-skip and log run telemetry. PM should never see
 * them. Per Rajiv CTO directive 2026-05-26 18:47 IST thread `1779790681.847219`,
 * the scheduler MUST strip those, drop any legacy `OWNER: dhruv|PM|Dhruv`
 * prefix lines (they encode the implicit PM-owned default — noise), and emit
 * the clean R4 shape to PM:
 *
 *   MoP: hourly ops audit              (stable title — no reason in title)
 *
 *   REASON: <parsed reason>
 *
 *   ACTION_ITEMS:
 *     - ACTION: <imperative>
 *       EVIDENCE: <one-line>
 *       TRANSITION: <verb>
 *       TARGET: <id>
 *     - ACTION: <imperative>
 *       OWNER: slot:N                  (OWNER kept ONLY for slot/rajiv exceptions)
 *       EVIDENCE: ...
 *       TRANSITION: ...
 *       TARGET: ...
 *
 *   NEXT: invoke Skill(hourly-ops-audit) to process these action items.
 *
 * The Stop hook + PreToolUse guard match on the title `MoP: hourly ops audit`
 * (Shape 3 regex in pm-context-injector.sh accepts both legacy `for <reason>`
 * and the new stable form during migration).
 *
 * Pure function — no side effects, no I/O. Exported for unit testing.
 */
export function sanitizePMPayload(rawStdout: string, parsedReason: string): string {
  const lines = (rawStdout ?? "").split("\n");
  const bodyLines: string[] = [];
  let inMetaTrailer = false;

  for (const line of lines) {
    const trimmed = line.trim();

    // Strip the INJECT_DECISION scheduler-control line entirely.
    if (/^INJECT_DECISION:/i.test(trimmed)) continue;

    // Strip the internal `---` separators.
    if (trimmed === "---") continue;

    // Strip the `-- META:` trailer (everything from `-- META:` onward, including
    // any following lines like `-- ORIGINAL_CODEX_OUTPUT:` blocks).
    if (/^--\s*(META|ORIGINAL_CODEX_OUTPUT)\b/i.test(trimmed)) {
      inMetaTrailer = true;
      continue;
    }
    if (inMetaTrailer) continue;

    // Strip ownerless-default OWNER lines (legacy noise per R2/R4):
    //   `OWNER: dhruv`, `OWNER:dhruv`, `OWNER: PM`, `OWNER: Dhruv`, `OWNER: Dhruva`
    // (case-insensitive). Real exception OWNER lines (`OWNER: slot:N`,
    // `OWNER: rajiv`) are preserved.
    if (/^\s*OWNER:\s*(dhruv(a)?|PM)\s*$/i.test(line)) continue;

    // Also strip leading `Dhruv:` / `PM:` directive prefixes (rare legacy shape).
    if (/^\s*(Dhruv(a)?|PM):\s*$/i.test(line)) continue;

    bodyLines.push(line);
  }

  // Collapse leading + trailing blank lines, and collapse runs of 3+ blanks to 2.
  let body = bodyLines.join("\n").replace(/^\n+|\n+$/g, "").replace(/\n{3,}/g, "\n\n");

  // Compose the clean R4 PM-facing message.
  // Title is STABLE — no reason. Reason goes in body.
  // NEXT line tells PM to invoke the skill (informational; the hook + guard
  // enforce mechanically).
  const safeReason = (parsedReason ?? "").trim() || "unspecified";
  const out =
    `MoP: hourly ops audit\n` +
    `\n` +
    `REASON: ${safeReason}\n` +
    `\n` +
    `${body}\n` +
    `\n` +
    `NEXT: invoke Skill(hourly-ops-audit) to process these action items.\n`;

  return out;
}
