/**
 * Typed wedge escape: POST /slots/:slotNum/interrupt-turn (Ctrl-C only).
 *
 * Live evidence (slot 5, 2026-09-19): one raw Ctrl-C aborted the wedged
 * turn, the queued continuation submitted, and the session stayed live on
 * the same pane — no /exit, kill, or relaunch. Interrupt is the primary
 * wedge escape; respawn stays fail-closed for busy slots.
 *
 * Trust model (fail-closed, server-owned evidence only):
 * - The request body carries ONLY identity pins (expected assignment epoch
 *   + expected active turn id). Caller-supplied quiet durations, prompt
 *   claims, or progress claims are never trusted — a fabricated body alone
 *   can never satisfy the escape.
 * - Eligibility is derived at request time AND rechecked immediately before
 *   the effect from server-owned state: current slot row (busy, DND-clear,
 *   exact pins), pinned pane identity, a live pane snapshot showing the idle
 *   prompt with queued input, and authoritative quiet >= 5 min from stored
 *   last_meaningful_work_at/active_turn_started_at (same 5-minute standard
 *   as the canonical abandon-turn quiescence).
 * - Queue disposition is DISCARD (verified live): interrupt FIRST, then
 *   re-deliver the continuation — never deliver-then-interrupt.
 * - Recovery delivery vs the watchdog: a parked shell is NOT terminal. The MoP
 *   health watchdog relaunches the session with `--continue` (the same path
 *   `exit_pending` uses), and that relaunch is what finally delivered the
 *   queued continuation live on slot 5 after three direct submissions failed.
 *   Deliver at or before that relaunch, or hold/pause the watchdog for the park
 *   window; never rely on queue survival, which the interrupt discards.
 * - Audit/response claim only what was verified: interrupt_signal_sent
 *   means the Ctrl-C keystroke landed, never that the turn ended.
 */

import { Hono } from "hono";

import { STALE_TURN_QUIESCENCE_MS } from "./db.js";
import type { MoPDatabase } from "./db.js";

/** The ONLY key this action may send. Enter/C-m are submits, never aborts. */
export const WEDGE_INTERRUPT_KEY = "C-c";

/** Abort clears the pane queue; the continuation must be re-delivered after. */
export const WEDGE_INTERRUPT_QUEUE_DISPOSITION = "discard-queued-input";

/**
 * Addendum-4 dwell floor. Queued input is the NORMAL state while an agent
 * works, so queuing alone never sanctions an interrupt: the escape needs
 * continuous queuing with NO HEAD/branch movement AND NO tool-name progress
 * for the full dwell. This floor is 3 minutes; the route enforces the
 * canonical 5-minute quiescence on top of it.
 */
export const WEDGE_DWELL_FLOOR_MS = 3 * 60 * 1000;

export const WEDGE_DWELL_RULE =
  "Interrupt is sanctioned only once movement has stopped: continuous queued input with NO HEAD/branch " +
  "movement AND NO tool-name progress for the full dwell (>= 3 min floor; this route requires the canonical " +
  "5-minute quiescence). Queued input alone is the NORMAL state while an agent works and never satisfies the " +
  "escape. Order: interrupt FIRST (it discards the queue), then deliver the continuation - and only after the " +
  "dwell is met.";

/**
 * Sanctioned wedge recovery. Interrupt-turn is primary; ordinary respawn only
 * after the slot genuinely reads idle. Preconditions below are
 * server-verified — no caller-supplied body alone satisfies the escape.
 */
/**
 * Sanctioned recovery delivery path, verified live on slot 5: after the park at
 * a clean shell, the MoP health watchdog restarted the session with
 * `--continue` and that relaunch finally delivered the queued combined message
 * where three direct submissions had failed. A parked shell is therefore not a
 * terminal state; the relaunch is the delivery opportunity.
 */
export const WEDGE_WATCHDOG_DELIVERY =
  "Watchdog relaunch with --continue is a sanctioned delivery path (verified live: it delivered the queued " +
  "continuation where repeated submissions did not). A parked shell is not terminal — the MoP health watchdog " +
  "restarts the session with --continue, the same path exit_pending uses. Deliver the continuation at or before " +
  "that relaunch, or hold/pause the watchdog for the park window. Do not rely on queue survival.";

export const WEDGED_BUSY_REMEDIATION =
  "Sanctioned wedge recovery: primary escape is POST /slots/:n/interrupt-turn (operator authority required; " +
  "server-verified evidence; sends Ctrl-C only; Enter and C-m are submits, not aborts). Interrupt DISCARDS " +
  "queued input: interrupt FIRST, then deliver the continuation — never deliver-then-interrupt. A wedge needs " +
  ">= 5 min with no meaningful progress plus a server-observed idle prompt with queued input; queued input " +
  "alone is normal working state, not a wedge. Recovery delivery: " +
  WEDGE_WATCHDOG_DELIVERY +
  " If interrupt is refused/unavailable: confirm the pane is shell-backed, terminate only the wedged Claude " +
  "child, then POST /slots/:n/respawn once the slot genuinely reads idle. Do not reassign, reset " +
  "ownership/epoch, or touch another slot.";

/**
 * Server-side pane observation showing the idle prompt with queued input,
 * in the exact shape observed live ("Press up to edit queued messages").
 * Anything else — including an unreadable snapshot — is not wedge evidence.
 */
export function snapshotShowsIdlePromptWithQueuedInput(snapshot: string | null): boolean {
  if (typeof snapshot !== "string" || snapshot.length === 0) return false;
  return /press\s+up\s+to\s+edit\s+queued\s+messages/i.test(snapshot);
}

export interface WedgeInterruptPins {
  expected_assignment_epoch?: unknown;
  expected_active_turn_id?: unknown;
}

export type WedgeInterruptRefusal =
  | "slot_not_busy_nothing_to_interrupt"
  | "slot_dnd_interrupt_refused"
  | "stale_identity"
  | "pane_identity_mismatch"
  | "pane_evidence_unavailable"
  | "checkout_movement_unverifiable"
  | "checkout_moved"
  | "wedge_quiet_insufficient"
  | "interrupt_in_progress";

/**
 * Live checkout observation for the movement leg: the slot checkout HEAD and
 * branch as read at request time, or null when the checkout could not be
 * inspected. A null observation is indeterminate, never "no movement".
 */
export type WedgeCheckoutObservation = { head?: string | null; branch?: string | null } | null;

export interface WedgeInterruptAuthorization {
  ok: boolean;
  reason: string;
  quiet_ms?: number;
}

/**
 * Pure authorization over server-owned state. `slot` is a fresh row read,
 * `snapshot` a live pane capture, `nowMs` the server clock. Returns ok only
 * when every conjunct holds.
 */
export function authorizeWedgeInterrupt(
  slot: {
    occupied?: unknown;
    idle?: unknown;
    dnd?: unknown;
    assignment_epoch?: unknown;
    active_turn_id?: unknown;
    last_meaningful_work_at?: unknown;
    active_turn_started_at?: unknown;
    head_sha?: unknown;
    branch?: unknown;
  } | null | undefined,
  pins: WedgeInterruptPins | null | undefined,
  snapshot: string | null,
  nowMs: number,
  checkout: WedgeCheckoutObservation = null,
): WedgeInterruptAuthorization {
  const busy = Boolean(slot && slot.occupied) && !Boolean(slot && slot.idle);
  if (!busy) return { ok: false, reason: "slot_not_busy_nothing_to_interrupt" };
  if (slot && slot.dnd) return { ok: false, reason: "slot_dnd_interrupt_refused" };
  const epoch = (slot as { assignment_epoch?: unknown } | null)?.assignment_epoch;
  const turnId = (slot as { active_turn_id?: unknown } | null)?.active_turn_id;
  if (
    !pins ||
    !Number.isInteger(pins.expected_assignment_epoch) ||
    typeof pins.expected_active_turn_id !== "string" ||
    pins.expected_active_turn_id.length === 0 ||
    typeof turnId !== "string" ||
    turnId.length === 0 ||
    epoch !== pins.expected_assignment_epoch ||
    turnId !== pins.expected_active_turn_id
  ) {
    return { ok: false, reason: "stale_identity" };
  }
  if (!snapshotShowsIdlePromptWithQueuedInput(snapshot)) {
    return { ok: false, reason: "pane_evidence_unavailable" };
  }
  const s = slot as {
    last_meaningful_work_at?: unknown;
    active_turn_started_at?: unknown;
    head_sha?: unknown;
    branch?: unknown;
  };
  // Movement leg: the checkpoint must not have moved while the turn queued.
  const recordedHead = typeof s.head_sha === "string" && s.head_sha.length > 0 ? s.head_sha : null;
  const recordedBranch = typeof s.branch === "string" && s.branch.length > 0 ? s.branch : null;
  const observedHead = checkout?.head ?? null;
  const observedBranch = checkout?.branch ?? null;
  if (checkout === null || (recordedHead && !observedHead) || (recordedBranch && !observedBranch)) {
    return { ok: false, reason: "checkout_movement_unverifiable" };
  }
  if ((recordedHead && observedHead !== recordedHead) || (recordedBranch && observedBranch !== recordedBranch)) {
    return { ok: false, reason: "checkout_moved" };
  }
  const lastRaw =
    typeof s.last_meaningful_work_at === "string" && s.last_meaningful_work_at.length > 0
      ? s.last_meaningful_work_at
      : typeof s.active_turn_started_at === "string"
        ? s.active_turn_started_at
        : "";
  const last = Date.parse(lastRaw);
  if (Number.isNaN(last)) return { ok: false, reason: "wedge_quiet_insufficient" };
  const quietMs = nowMs - last;
  if (quietMs < STALE_TURN_QUIESCENCE_MS) {
    return { ok: false, reason: "wedge_quiet_insufficient", quiet_ms: quietMs };
  }
  return { ok: true, reason: "wedge_interrupt_evidence_verified", quiet_ms: quietMs };
}

/** Synchronous per-slot reservation: check+acquire with no await between. */
const wedgeInterruptInFlight = new Set<number>();

export function acquireWedgeInterrupt(slotNum: number): boolean {
  if (wedgeInterruptInFlight.has(slotNum)) return false;
  wedgeInterruptInFlight.add(slotNum);
  return true;
}

export function releaseWedgeInterrupt(slotNum: number): void {
  wedgeInterruptInFlight.delete(slotNum);
}

export interface WedgeInterruptDeps {
  db: MoPDatabase;
  /** Existing operator authority boundary (static header check). */
  isOperatorRequest: (authorityHeader: string | undefined) => boolean;
  authorityHeader: (c: { req: { header: (name: string) => string | undefined } }) => string | undefined;
  verifyPaneIdentity: (slotNum: number) => Promise<{
    ok: boolean;
    detail?: string;
    /** paneId pins the pane; currentPath is the identity-verified checkout. */
    snapshot?: { paneId?: string; currentPath?: string };
  }>;
  captureSnapshot: (paneId: string) => Promise<string | null>;
  /** Live checkout HEAD/branch for the movement leg; null = indeterminate. */
  observeCheckout: (checkoutPath: string) => Promise<WedgeCheckoutObservation>;
  /** Sends exactly one raw C-c; resolves true only if the keystroke landed. */
  sendInterruptKey: (slotNum: number) => Promise<boolean>;
  nowMs: () => number;
}

/**
 * Register the real interrupt-turn route on `app`. In production this is
 * wired with live pane/relay boundaries; tests wire fakes + a temp DB.
 */
export function registerWedgeInterruptRoute(app: Hono, deps: WedgeInterruptDeps): void {
  app.post("/slots/:slotNum/interrupt-turn", async (c) => {
    const slotRaw = c.req.param("slotNum");
    const slotNum = Number(slotRaw);
    if (!Number.isInteger(slotNum) || slotNum < 1) {
      return c.json({ success: false, error: "Invalid slot number" }, 400);
    }
    if (!deps.isOperatorRequest(deps.authorityHeader(c))) {
      return c.json(
        {
          success: false,
          error: "assignment authority is required",
          reason: "assignment_authority_required",
        },
        403,
      );
    }
    const body = (await c.req.json().catch(() => ({}))) as WedgeInterruptPins;

    // Sync prechecks over a fresh server-owned row before reserving. The pane
    // snapshot is awaited evidence, so run the pure authorizer with a null
    // snapshot that can only fail the pane-evidence leg: any other refusal is
    // terminal here, and the full check re-runs after the awaited reads.
    const syncOnly = authorizeWedgeInterrupt(
      deps.db.getSlot(slotNum) as unknown as Parameters<typeof authorizeWedgeInterrupt>[0],
      body,
      null,
      deps.nowMs(),
    );
    if (
      !syncOnly.ok &&
      syncOnly.reason !== "pane_evidence_unavailable" &&
      syncOnly.reason !== "checkout_movement_unverifiable"
    ) {
      return c.json({ success: false, error: refusalText(slotNum, syncOnly.reason), reason: syncOnly.reason }, 409);
    }
    if (!acquireWedgeInterrupt(slotNum)) {
      return c.json(
        {
          success: false,
          error: `Slot ${slotNum} interrupt already in progress; concurrent request refused.`,
          reason: "interrupt_in_progress",
        },
        409,
      );
    }
    try {
      const identity = await deps.verifyPaneIdentity(slotNum);
      if (!identity.ok || !identity.snapshot?.paneId) {
        return c.json(
          {
            success: false,
            error: `Refused interrupt for slot ${slotNum}: ${identity.detail ?? "pane identity unverified"}`,
            reason: "pane_identity_mismatch",
          },
          409,
        );
      }
      const snapshot = await deps.captureSnapshot(identity.snapshot.paneId);
      // A verified identity without a checkout path cannot establish "no
      // movement": pass an indeterminate observation so the authorizer refuses.
      const checkoutPath = identity.snapshot.currentPath;
      const checkout: WedgeCheckoutObservation = checkoutPath
        ? await deps.observeCheckout(checkoutPath)
        : null;
      // Full recheck on fresh server-owned state immediately before the effect:
      // resumed activity, epoch/turn drift, or lost pane evidence refuses here.
      const fresh = authorizeWedgeInterrupt(
        deps.db.getSlot(slotNum) as unknown as Parameters<typeof authorizeWedgeInterrupt>[0],
        body,
        snapshot,
        deps.nowMs(),
        checkout,
      );
      if (!fresh.ok) {
        return c.json({ success: false, error: refusalText(slotNum, fresh.reason), reason: fresh.reason }, 409);
      }
      const delivered = await deps.sendInterruptKey(slotNum);
      if (!delivered) {
        return c.json(
          {
            success: false,
            error: `Interrupt keystroke did not land on slot ${slotNum}; turn untouched.`,
            reason: "interrupt_delivery_failed",
          },
          502,
        );
      }
      const row = deps.db.getSlot(slotNum) as unknown as {
        assignment_epoch?: unknown;
        active_turn_id?: unknown;
      };
      deps.db.logEvent(slotNum, "interrupt_signal_sent", null, null, {
        assignment_epoch: row?.assignment_epoch,
        active_turn_id: row?.active_turn_id,
        interrupt_key: WEDGE_INTERRUPT_KEY,
        quiet_ms: fresh.quiet_ms,
        queue_disposition: WEDGE_INTERRUPT_QUEUE_DISPOSITION,
        checkout_head: checkout?.head ?? null,
        checkout_branch: checkout?.branch ?? null,
        movement_verified: true,
        via: "interrupt_turn_evidence_verified",
      });
      return c.json({
        success: true,
        slot: slotNum,
        signal_sent: true,
        interrupt_key: WEDGE_INTERRUPT_KEY,
        queue_disposition: WEDGE_INTERRUPT_QUEUE_DISPOSITION,
        followup:
          "Re-deliver the continuation AFTER this interrupt; queued input was discarded. A parked shell is " +
          "not terminal: deliver at or before the watchdog relaunch with --continue, or hold the watchdog for " +
          "the park window.",
        watchdog_delivery: WEDGE_WATCHDOG_DELIVERY,
        dwell_rule: WEDGE_DWELL_RULE,
        movement_verified: true,
        checkout_branch: checkout?.branch ?? null,
        assignment_epoch: row?.assignment_epoch,
      });
    } finally {
      releaseWedgeInterrupt(slotNum);
    }
  });
}

function refusalText(slotNum: number, reason: string): string {
  switch (reason) {
    case "slot_not_busy_nothing_to_interrupt":
      return `Slot ${slotNum} is not busy; no turn to interrupt.`;
    case "slot_dnd_interrupt_refused":
      return `Slot ${slotNum} is DND; clear DND before interrupting its turn.`;
    case "stale_identity":
      return (
        `Slot ${slotNum} identity moved: provide the current expected_assignment_epoch and ` +
        `expected_active_turn_id from a fresh slot read.`
      );
    case "pane_evidence_unavailable":
      return (
        `Slot ${slotNum} wedge not proven: no live pane snapshot showing the idle prompt with queued input. ` +
        `Queued input alone is the normal state while an agent works and never triggers an interrupt.`
      );
    case "checkout_movement_unverifiable":
      return (
        `Slot ${slotNum} wedge not proven: the checkout HEAD/branch could not be observed, so "no movement" ` +
        `cannot be established. Interrupt requires stopped movement, not merely queued input.`
      );
    case "checkout_moved":
      return (
        `Slot ${slotNum} interrupt refused: the checkout HEAD/branch moved away from the recorded assignment, ` +
        `so work is still landing. Queued input with live movement is normal working state.`
      );
    case "wedge_quiet_insufficient":
      return (
        `Slot ${slotNum} wedge not proven: the dwell has not elapsed. An interrupt requires continuous queued ` +
        `input with no HEAD/branch movement and no tool-name progress for the full dwell (>= 3 min floor; ` +
        `this route requires the canonical 5 minutes). Queuing alone is never the trigger.`
      );
    default:
      return `Slot ${slotNum} turn interrupt refused (${reason}).`;
  }
}
