/**
 * Wedged-session respawn escape — typed, bounded operator attestation.
 *
 * Control point: POST /slots/:slotNum/respawn refuses a busy (occupied,
 * non-idle) slot to avoid killing in-flight work. A Claude session wedged
 * inside a turn that never ends (idle-looking prompt, queued input that
 * never submits, no tool-name progress) is unreachable by both delivery
 * and respawn. This module is the single decision function for that
 * escape: it either refuses with a typed reason or allows the existing
 * respawn flow to proceed unchanged (ownership and epoch preserved —
 * respawn never reassigns).
 */

/** Minimum quiet minutes before a busy slot may be treated as wedged. */
export const WEDGED_ESCAPE_MIN_QUIET_MINUTES = 5;

/**
 * Sanctioned recovery when the escape is unavailable or refused. The primary
 * escape is the interrupt-turn action (Ctrl-C only): it aborts the wedged
 * turn and the session stays live on the same pane — no /exit, no kill, no
 * relaunch. Enter and C-m are SUBMITS, not aborts, and are never an escape.
 * Fallback when interrupt is unavailable/refused: kill only the wedged
 * Claude child on a shell-backed pane, then relaunch through the canonical
 * respawn route once the slot reads idle. Never reassign, re-epoch, or
 * touch another slot.
 */
export const WEDGED_BUSY_REMEDIATION =
  "Sanctioned wedge recovery: primary escape is POST /slots/:n/interrupt-turn with attestation " +
  "(sends Ctrl-C only; Enter and C-m are submits, not aborts). Fallback: confirm the pane is shell-backed, " +
  "interrupt/terminate only the wedged Claude child (Ctrl-C, then /exit on the idle prompt), then " +
  "POST /slots/:n/respawn once the slot reads idle. " +
  "Do not reassign, reset ownership/epoch, or touch another slot.";

export interface WedgeAttestation {
  idle_prompt_observed?: unknown;
  no_tool_progress_minutes?: unknown;
}

export interface WedgedEscapeBody {
  force_wedged_escape?: unknown;
  wedge_attestation?: WedgeAttestation | null;
}

export type WedgedEscapeDecision =
  | "not_busy"
  | "refused_busy"
  | "attestation_insufficient"
  | "allowed";

export interface WedgedEscapeEvaluation {
  decision: WedgedEscapeDecision;
  reason: string;
  quiet_minutes?: number;
}

export function evaluateWedgedRespawnEscape(
  slot: { occupied?: unknown; idle?: unknown } | null | undefined,
  body: WedgedEscapeBody | null | undefined,
): WedgedEscapeEvaluation {
  const busy = Boolean(slot && slot.occupied) && !Boolean(slot && slot.idle);
  if (!busy) return { decision: "not_busy", reason: "slot_not_busy" };
  if (!body || body.force_wedged_escape !== true) {
    return { decision: "refused_busy", reason: "slot_busy_respawn_refused" };
  }
  const attestation = body.wedge_attestation;
  const quiet =
    attestation && typeof attestation.no_tool_progress_minutes === "number"
      ? attestation.no_tool_progress_minutes
      : Number.NaN;
  if (
    !attestation ||
    attestation.idle_prompt_observed !== true ||
    !Number.isFinite(quiet) ||
    quiet < WEDGED_ESCAPE_MIN_QUIET_MINUTES
  ) {
    return { decision: "attestation_insufficient", reason: "wedge_attestation_insufficient" };
  }
  return { decision: "allowed", reason: "wedged_escape_attested", quiet_minutes: quiet };
}

/** Minimum quiet minutes before a turn may be treated as wedged for interrupt. */
export const WEDGE_INTERRUPT_MIN_QUIET_MINUTES = 5;

/**
 * The ONLY key the interrupt-turn action may send. Ctrl-C aborts the live
 * turn; the session stays alive on the same pane so queued work can resume.
 * Enter and C-m are SUBMITS, not aborts — they must never be treated as an
 * escape, and this action takes no key parameter so no caller can select one.
 */
export const WEDGE_INTERRUPT_KEY = "C-c";

export interface WedgeInterruptAttestation {
  idle_prompt_with_queued_input_observed?: unknown;
  no_tool_progress_minutes?: unknown;
}

export interface WedgeInterruptBody {
  interrupt_attested?: unknown;
  wedge_attestation?: WedgeInterruptAttestation | null;
}

export type WedgeInterruptDecision =
  | "allowed"
  | "nothing_to_interrupt"
  | "attestation_insufficient"
  | "dnd_refused";

export interface WedgeInterruptEvaluation {
  decision: WedgeInterruptDecision;
  reason: string;
  quiet_minutes?: number;
}

export function evaluateWedgeInterrupt(
  slot: { occupied?: unknown; idle?: unknown; dnd?: unknown } | null | undefined,
  body: WedgeInterruptBody | null | undefined,
): WedgeInterruptEvaluation {
  const busy = Boolean(slot && slot.occupied) && !Boolean(slot && slot.idle);
  if (!busy) return { decision: "nothing_to_interrupt", reason: "slot_not_busy_nothing_to_interrupt" };
  if (slot && slot.dnd) return { decision: "dnd_refused", reason: "slot_dnd_interrupt_refused" };
  if (!body || body.interrupt_attested !== true) {
    return { decision: "attestation_insufficient", reason: "wedge_interrupt_attestation_insufficient" };
  }
  const attestation = body.wedge_attestation;
  const quiet =
    attestation && typeof attestation.no_tool_progress_minutes === "number"
      ? attestation.no_tool_progress_minutes
      : Number.NaN;
  if (
    !attestation ||
    attestation.idle_prompt_with_queued_input_observed !== true ||
    !Number.isFinite(quiet) ||
    quiet < WEDGE_INTERRUPT_MIN_QUIET_MINUTES
  ) {
    return { decision: "attestation_insufficient", reason: "wedge_interrupt_attestation_insufficient" };
  }
  return { decision: "allowed", reason: "wedge_interrupt_attested", quiet_minutes: quiet };
}
