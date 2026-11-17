/**
 * Conditional PM Claude RSS arm at the server boundary.
 *
 * Rajiv DM D0AMF0XE6TS thread 1794921293.133799, ts 1794921896.970909: "track the
 * claude process memory usage in hourlies and set exit pending automatically".
 * CTO race finding: the hourly client did GET /exit-status then POST /exit-pending,
 * so an owner that armed between the two calls (a clear handoff, a refused-exit
 * recovery, any other PM-only episode) was superseded by the RSS arm, and a
 * superseding PM-only arm also erases the refused terminal's retained continuation.
 *
 * The check now lives where the state lives. POST /exit-pending with
 * `condition: "pm_rss"` is evaluated synchronously, with no await between this
 * decision and `db.setExitPending`, so the check and the arm are one atomic step
 * in the single-threaded server. Any existing owner state means a 409 with zero
 * mutation. An unreadable owner lookup is not absence: it also preserves.
 *
 * Rule 50: this only declines to arm. It never holds, delays or gates PM input or
 * tools, and the UserPromptSubmit chain is untouched. The Stop hook still sends
 * /exit at the next PM Stop after the handoff is saved (selectPmStopMaintenance).
 */
import type { MoPDatabase } from "./db.js";
import { isPmHandoffPending } from "./pmHandoffClear.js";
import { readPmExitTerminal } from "./pmHandoffExit.js";

export const PM_RSS_ARM_CONDITION = "pm_rss";

type Db = Pick<MoPDatabase, "getConfig" | "getExitPending" | "getExitStatus" | "hasPendingClear">;

export type PmRssArmProbes = {
  /** Read-only: a pinned daily or clear maintenance request (pendingPMMaintenance). */
  pendingMaintenance: () => { kind: string } | null;
  /** Read-only: a PM maintenance request currently holds the soft boundary. */
  boundaryHeld: () => boolean;
};

export type PmRssArmOwner = { kind: string; [detail: string]: unknown };

export type PmRssArmDecision =
  | { ok: true; rss_bytes: number; threshold_bytes: number }
  | { ok: false; status: 400 | 409; reason: string; error: string; owner: PmRssArmOwner | null };

const positiveInteger = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value > 0;

/** The first existing owner of PM restart/clear state, or null when none is proven. */
export function pmRssArmOwner(db: Db, probes: PmRssArmProbes): PmRssArmOwner | null {
  try {
    if (db.getExitPending()) {
      const status = db.getExitStatus();
      return {
        kind: "exit_pending", episode: status.episode, pm_only: status.pm_only, with_devs: status.with_devs,
        dev_only: status.dev_only, continuation_delivery: status.continuation_delivery,
      };
    }
    const terminal = readPmExitTerminal(db);
    if (terminal) return { kind: "exit_refused_terminal", episode: terminal.episode };
    if (db.hasPendingClear(0)) return { kind: "pm_clear_pending" };
    if (isPmHandoffPending(db)) return { kind: "pm_clear_handoff_requested" };
    const pending = probes.pendingMaintenance();
    if (pending) return { kind: "pm_maintenance_pending", maintenance: pending.kind };
    if (probes.boundaryHeld()) return { kind: "pm_maintenance_held" };
    return null;
  } catch (error) {
    // An unknown lookup is not absence (same custody rule as the handoff obligations).
    return { kind: "owner_state_unknown", error: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * Validate and decide a conditional RSS arm. Pure and synchronous: the caller
 * must not await between a `{ ok: true }` result and `db.setExitPending`.
 */
export function evaluatePmRssArm(db: Db, body: Record<string, unknown>, probes: PmRssArmProbes): PmRssArmDecision {
  const bad = (reason: string, error: string): PmRssArmDecision => ({ ok: false, status: 400, reason, error, owner: null });
  if (body.condition !== PM_RSS_ARM_CONDITION) return bad("unsupported_condition", "condition must be 'pm_rss'");
  if (body.enabled !== true || body.pm_only !== true) return bad("rss_arm_requires_pm_only", "pm_rss arm requires enabled:true and pm_only:true");
  if (body.with_devs === true || body.dev_only === true || body.recover_episode !== undefined) {
    return bad("rss_arm_never_cycles_devs", "pm_rss arm is PM-only: no with_devs, dev_only or recover_episode");
  }
  if (!positiveInteger(body.rss_bytes) || !positiveInteger(body.threshold_bytes)) {
    return bad("invalid_rss_evidence", "pm_rss arm requires positive integer rss_bytes and threshold_bytes");
  }
  if (body.rss_bytes < body.threshold_bytes) {
    return { ok: false, status: 409, reason: "below_threshold", error: "PM RSS is below the arm threshold; nothing armed", owner: null };
  }
  const owner = pmRssArmOwner(db, probes);
  if (owner) {
    return {
      ok: false, status: 409, reason: "owner_state_preserved", owner,
      error: `PM RSS arm refused: existing owner state (${owner.kind}) preserved; nothing changed`,
    };
  }
  return { ok: true, rss_bytes: body.rss_bytes, threshold_bytes: body.threshold_bytes };
}
