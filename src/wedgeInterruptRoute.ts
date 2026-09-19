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
 * Sanctioned wedge recovery. Interrupt-turn is primary; ordinary respawn only
 * after the slot genuinely reads idle. Preconditions below are
 * server-verified — no caller-supplied body alone satisfies the escape.
 */
export const WEDGED_BUSY_REMEDIATION =
  "Sanctioned wedge recovery: primary escape is POST /slots/:n/interrupt-turn (operator authority required; " +
  "server-verified evidence; sends Ctrl-C only; Enter and C-m are submits, not aborts). Interrupt DISCARDS " +
  "queued input: interrupt FIRST, then deliver the continuation — never deliver-then-interrupt. A wedge needs " +
  ">= 5 min with no meaningful progress plus a server-observed idle prompt with queued input; queued input " +
  "alone is normal working state, not a wedge. If interrupt is refused/unavailable: confirm the pane is " +
  "shell-backed, terminate only the wedged Claude child, then POST /slots/:n/respawn once the slot genuinely " +
  "reads idle. Do not reassign, reset ownership/epoch, or touch another slot.";

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
  | "wedge_quiet_insufficient"
  | "interrupt_in_progress";

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
  } | null | undefined,
  pins: WedgeInterruptPins | null | undefined,
  snapshot: string | null,
  nowMs: number,
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
  const s = slot as { last_meaningful_work_at?: unknown; active_turn_started_at?: unknown };
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
  verifyPaneIdentity: (slotNum: number) => Promise<{ ok: boolean; detail?: string; snapshot?: { paneId?: string } }>;
  captureSnapshot: (paneId: string) => Promise<string | null>;
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
    if (!syncOnly.ok && syncOnly.reason !== "pane_evidence_unavailable") {
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
      // Full recheck on fresh server-owned state immediately before the effect:
      // resumed activity, epoch/turn drift, or lost pane evidence refuses here.
      const fresh = authorizeWedgeInterrupt(
        deps.db.getSlot(slotNum) as unknown as Parameters<typeof authorizeWedgeInterrupt>[0],
        body,
        snapshot,
        deps.nowMs(),
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
        via: "interrupt_turn_evidence_verified",
      });
      return c.json({
        success: true,
        slot: slotNum,
        signal_sent: true,
        interrupt_key: WEDGE_INTERRUPT_KEY,
        queue_disposition: WEDGE_INTERRUPT_QUEUE_DISPOSITION,
        followup: "Re-deliver the continuation AFTER this interrupt; queued input was discarded.",
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
        `Queued input alone is normal working state, not a wedge.`
      );
    case "wedge_quiet_insufficient":
      return (
        `Slot ${slotNum} wedge not proven: authoritative no-meaningful-progress duration is under 5 minutes.`
      );
    default:
      return `Slot ${slotNum} turn interrupt refused (${reason}).`;
  }
}
