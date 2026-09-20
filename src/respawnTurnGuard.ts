/**
 * Truthful respawn reporting for orphaned hook turns.
 *
 * Live incident (slot 6, 2026-09-20): a hook turn stayed `active` while
 * `idle=true`; the first respawn boot-timed-out at 60s (spark model), the
 * watchdog relaunched the session with `--continue`, and a second respawn
 * reported "✓ respawned" while MoP still held the identical turn id — because
 * the respawn path never inspects the turn record. The session that owned the
 * turn never emitted Stop/SessionEnd, so the record could only be cleared by
 * the canonical abandon-turn path.
 *
 * This guard makes the respawn result conditional on the pre-respawn turn
 * record actually settling: a persisted pre-respawn turn is reported as a
 * typed non-success naming the canonical remedy instead of a bare success.
 */

export interface RespawnTurnSnapshot {
  active_turn_id: string | null;
  active_turn_state: string;
}

export interface RespawnTurnPersistence {
  persisted: boolean;
  turn_id: string | null;
}

export function evaluateRespawnTurnPersistence(
  preRespawnTurnId: string | null | undefined,
  postRespawn: RespawnTurnSnapshot | null | undefined,
): RespawnTurnPersistence {
  const pre = typeof preRespawnTurnId === "string" ? preRespawnTurnId.trim() : "";
  if (!pre) {
    return { persisted: false, turn_id: null };
  }
  const post = postRespawn?.active_turn_id ?? null;
  if (post !== pre) {
    // The turn record cleared, or a replacement turn was started by the new
    // session. Either way the respawn settled the record it inherited.
    return { persisted: false, turn_id: null };
  }
  return { persisted: true, turn_id: pre };
}

export function respawnTurnPersistenceRemedy(slot: number, turnId: string): string {
  return (
    `The pre-respawn hook turn ${turnId} is still recorded active, so the relaunch did not settle it ` +
    `(a boot timeout means the owning session never emitted Stop/SessionEnd for that turn). ` +
    `Terminalize exactly that turn through the canonical path: ` +
    `POST http://127.0.0.1:3100/slots/${slot}/abandon-turn ` +
    `{"turn_id":"${turnId}","reason":"<why>","actor":"<who>"} — it requires >= 5 min quiescence, refuses a ` +
    `replacement/indeterminate turn, clears exactly the named turn, and is idempotent on repeat. ` +
    `Then re-read the slot: release stays refused (slot_not_idle) until the turn record reads inactive. ` +
    `Do not respawn again for this condition.`
  );
}
