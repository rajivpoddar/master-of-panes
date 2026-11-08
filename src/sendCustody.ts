/**
 * Durable custody for keyed `POST /slots/:n/send` requests.
 *
 * A caller that loses the response (timeout, reset, crash, restart) cannot know
 * whether text reached the pane. A keyed send lets it ask MoP instead of guessing,
 * and lets MoP refuse to paste twice. The rules, in order of importance:
 *
 *  1. CUSTODY BEFORE EFFECT. The attempt is written durably (`started`) BEFORE any
 *     pane I/O. A crash after the paste therefore leaves `started`, which is read as
 *     UNKNOWN, never as "nothing happened". If the write itself fails nothing is sent.
 *  2. ONLY AFFIRMATIVE ZERO-EFFECT MAY BE RETRIED. `refused_zero_effect` is recorded
 *     only when the handler proved no pane write happened. Every other outcome that is
 *     not an acceptance (a timeout, a 5xx, a paste whose acceptance could not be
 *     confirmed, a lost response) is `ambiguous` and is never pasted again.
 *  3. THE KEY BINDS THE REQUEST. Reuse of a key with a different payload is refused.
 *     A recorded send is never re-executed for a different destination (a PM session
 *     replaced by /clear or respawn is a different recipient).
 *  4. ACCEPTED means the PM CLI recorded the prompt (see pmAcceptance.ts), not that
 *     tmux returned success. QUEUED means MoP holds it durably and has not pasted it.
 */

import { createHash } from "node:crypto";

export type SendOutcome = { status: number; body: Record<string, unknown> };

export type CustodyState = "started" | "refused_zero_effect" | "queued" | "ambiguous" | "accepted";

export type CustodyRow = {
  scope: string;
  slot: number;
  key: string;
  /** sha256 of the whole request (command/file, force, raw, allow_pm_clear). */
  digest: string;
  /** sha256 of the trimmed command text; null for file sends. Callers compare this. */
  command_digest: string | null;
  /** Pane id + session token of the recipient the attempt was bound to. */
  destination: string;
  state: CustodyState;
  reason: string | null;
  /** Abort pane tail, or `enter=<n>` after an accepted notice submit. */
  detail?: string | null;
  attempts: number;
  created_at: number;
  updated_at: number;
};

export type BeginDecision =
  | { kind: "execute"; row: CustodyRow }
  | { kind: "conflict"; row: CustodyRow }
  | { kind: "duplicate"; row: CustodyRow }
  | { kind: "in_progress"; row: CustodyRow }
  | { kind: "unknown"; row: CustodyRow };

export type BeginRequest = {
  scope: string;
  slot: number;
  key: string;
  digest: string;
  commandDigest: string | null;
  destination: string;
};

/** Persistence for custody rows. `begin` and `finish` must be atomic and durable. */
export interface CustodyStore {
  begin(request: BeginRequest, nowMs: number): BeginDecision;
  finish(scope: string, state: CustodyState, reason: string | null, nowMs: number): void;
  get(scope: string): CustodyRow | null;
}

export function sha256Hex(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

/** Request identity: any change to what would be pasted, or how, is a different request. */
export function sendRequestDigest(body: Record<string, unknown>): { digest: string; commandDigest: string | null } {
  const command = typeof body.command === "string" ? body.command.trim() : "";
  const file = typeof body.file === "string" ? body.file : "";
  const canonical = JSON.stringify({
    c: command,
    f: file,
    force: body.force === true,
    raw: body.raw === true,
    p: body.allow_pm_clear === true,
  });
  return { digest: sha256Hex(canonical), commandDigest: command && !file ? sha256Hex(command) : null };
}

/**
 * Refusals the handler makes BEFORE any pane write. Anything not listed here, and not
 * explicitly marked `zero_effect: true`, is treated as possibly having had an effect.
 */
const PRE_EFFECT_REFUSAL_REASONS = new Set([
  "pane_identity_mismatch",
  "pane_not_found",
  "dnd_no_force",
  "activity_identity_missing",
  "slot_active_force_required",
  "slot_active_review_blocked",
  "message_slot_wrapper_target_mismatch",
  "message_slot_wrapper_inline_message_blocked",
  "message_slot_wrapper_missing_file",
  "pm_control_command_blocked",
  "pm_raw_send_blocked",
  "pm_clear_authority_missing",
]);

export function isAffirmativeZeroEffect(outcome: SendOutcome): boolean {
  if (outcome.status >= 200 && outcome.status < 300) return false;
  if (outcome.body.zero_effect === true) return true;
  return typeof outcome.body.reason === "string" && PRE_EFFECT_REFUSAL_REASONS.has(outcome.body.reason);
}

export function classifySendOutcome(outcome: SendOutcome): { state: CustodyState; reason: string | null } {
  const reason = typeof outcome.body.reason === "string" ? outcome.body.reason : null;
  if (outcome.status >= 200 && outcome.status < 300 && outcome.body.success === true) {
    if (outcome.body.queued === true) return { state: "queued", reason };
    if (outcome.body.accepted === true) return { state: "accepted", reason };
    // A success without proof the PM CLI took it is not an acceptance.
    return { state: "ambiguous", reason: "success_without_acceptance" };
  }
  if (isAffirmativeZeroEffect(outcome)) return { state: "refused_zero_effect", reason };
  return { state: "ambiguous", reason: reason ?? `http_${outcome.status}` };
}

export type CustodyRun =
  | { kind: "executed"; outcome: SendOutcome; row: CustodyRow | null }
  | { kind: "duplicate"; row: CustodyRow; destinationChanged: boolean; outcome?: SendOutcome }
  | { kind: "rejected"; status: number; body: Record<string, unknown> };

export class SendCustodyJournal {
  /** Attempts executing in THIS process. A `started` row with no entry here is a crashed attempt. */
  private readonly inFlight = new Map<string, Promise<SendOutcome>>();

  constructor(
    private readonly store: CustodyStore,
    private readonly now: () => number = Date.now,
  ) {}

  async run(request: BeginRequest, execute: () => Promise<SendOutcome>): Promise<CustodyRun> {
    for (;;) {
      let decision: BeginDecision;
      try {
        decision = this.store.begin(request, this.now());
      } catch (error) {
        // Custody could not be made durable, so nothing may be sent. No effect has happened.
        console.error("[send-custody] cannot record attempt; refusing before any I/O:", error);
        return {
          kind: "rejected",
          status: 503,
          body: { success: false, reason: "send_custody_unavailable", zero_effect: true, safe_to_retry: true },
        };
      }
      const { row } = decision;
      switch (decision.kind) {
        case "conflict":
          return {
            kind: "rejected",
            status: 409,
            body: {
              success: false,
              reason: "idempotency_key_conflict",
              zero_effect: true,
              safe_to_retry: false,
              prior_state: row.state,
            },
          };
        case "duplicate":
          return { kind: "duplicate", row, destinationChanged: row.destination !== request.destination };
        case "unknown":
          return {
            kind: "rejected",
            status: 409,
            body: {
              success: false,
              reason: "send_outcome_unknown",
              zero_effect: true,
              safe_to_retry: false,
              prior_state: row.state,
              prior_reason: row.reason,
            },
          };
        case "in_progress": {
          const running = this.inFlight.get(request.scope);
          if (running) {
            // The earlier attempt is still executing here (for example a request the
            // caller already gave up on). Wait for it, then decide again from custody.
            await running.catch(() => undefined);
            continue;
          }
          // `started` with nothing executing: the process died between paste and journal.
          this.store.finish(request.scope, "ambiguous", "crash_before_outcome", this.now());
          continue;
        }
        case "execute":
          return this.execute(request, execute, row);
      }
    }
  }

  private async execute(request: BeginRequest, execute: () => Promise<SendOutcome>, row: CustodyRow): Promise<CustodyRun> {
    let settle!: (outcome: SendOutcome) => void;
    this.inFlight.set(request.scope, new Promise<SendOutcome>((resolve) => { settle = resolve; }));
    let outcome: SendOutcome = { status: 500, body: { success: false, reason: "send_execution_aborted" } };
    try {
      outcome = await execute();
      return { kind: "executed", outcome, row };
    } finally {
      const { state, reason } = classifySendOutcome(outcome);
      try {
        this.store.finish(request.scope, state, reason, this.now());
      } catch (error) {
        // The row stays `started`, which every later reader treats as UNKNOWN.
        console.error("[send-custody] failed to record outcome; custody stays started (unknown):", error);
      }
      this.inFlight.delete(request.scope);
      settle(outcome);
    }
  }

  lookup(scope: string): CustodyRow | null {
    const row = this.store.get(scope);
    if (row?.state === "started" && !this.inFlight.has(scope)) {
      // Report a crashed attempt as what it is.
      return { ...row, state: "ambiguous", reason: row.reason ?? "crash_before_outcome" };
    }
    return row;
  }
}

/** Header values are untrusted: printable ASCII without spaces, bounded length. */
export function parseIdempotencyKey(raw: string | undefined | null): string | null {
  const key = raw?.trim();
  if (!key || key.length > 256 || !/^[\x21-\x7e]+$/.test(key)) return null;
  return key;
}
