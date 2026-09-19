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
 * Sanctioned recovery when the escape is unavailable or refused: kill only
 * the wedged Claude child on a shell-backed pane, then relaunch through
 * the canonical respawn route once the slot reads idle. Never reassign,
 * re-epoch, or touch another slot.
 */
export const WEDGED_BUSY_REMEDIATION =
  "Sanctioned wedge recovery: confirm the pane is shell-backed, interrupt/terminate only the wedged " +
  "Claude child (Ctrl-C, then /exit on the idle prompt), then POST /slots/:n/respawn once the slot reads idle. " +
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
