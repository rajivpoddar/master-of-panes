/**
 * Idempotency for `POST /slots/:n/send`.
 *
 * A caller that loses the response (client timeout, connection reset, MoP restart)
 * cannot know whether the text was pasted into the pane. Without a key it must
 * either drop the message or risk pasting it twice. With an `Idempotency-Key`,
 * MoP answers a repeat of an already CONFIRMED send with the earlier outcome
 * (`duplicate: true`) and never pastes again, so the caller may repeat freely.
 *
 * Only confirmed successes are remembered: a failed attempt leaves no proof of
 * an effect, so the next attempt with the same key runs normally. A repeat that
 * arrives while the first attempt is still running waits for it instead of
 * racing it (a late-arriving request that the caller already gave up on is
 * exactly this case).
 */

export type SendOutcome = { status: number; body: Record<string, unknown> };

export function isConfirmedSend(outcome: SendOutcome): boolean {
  return outcome.status >= 200 && outcome.status < 300 && outcome.body.success === true;
}

type Entry = { at: number; outcome?: SendOutcome; pending?: Promise<SendOutcome> };

export class SendIdempotencyJournal {
  private readonly entries = new Map<string, Entry>();

  constructor(
    private readonly ttlMs: number,
    private readonly now: () => number = Date.now,
  ) {}

  /** Restore confirmed keys recorded before a restart. */
  hydrate(confirmed: Array<{ scope: string; at: number }>): void {
    for (const { scope, at } of confirmed) {
      if (this.now() - at > this.ttlMs) continue;
      this.entries.set(scope, {
        at,
        outcome: { status: 200, body: { success: true, recovered_from_journal: true } },
      });
    }
  }

  private prune(): void {
    for (const [scope, entry] of this.entries) {
      if (entry.outcome && this.now() - entry.at > this.ttlMs) this.entries.delete(scope);
    }
  }

  /**
   * Run `execute` unless an earlier attempt with the same scope already confirmed.
   * `onConfirmed` runs once, synchronously, when `execute` confirms (use it to
   * journal the key durably).
   */
  async run(
    scope: string,
    execute: () => Promise<SendOutcome>,
    onConfirmed: (outcome: SendOutcome) => void = () => undefined,
  ): Promise<{ outcome: SendOutcome; duplicate: boolean }> {
    this.prune();
    for (;;) {
      const prior = this.entries.get(scope);
      if (prior?.outcome) return { outcome: prior.outcome, duplicate: true };
      if (!prior?.pending) break;
      const settled = await prior.pending;
      if (isConfirmedSend(settled)) return { outcome: settled, duplicate: true };
      // The earlier attempt failed and left no provable effect: run as a fresh attempt.
    }

    let settle!: (outcome: SendOutcome) => void;
    const entry: Entry = {
      at: this.now(),
      pending: new Promise<SendOutcome>((resolve) => { settle = resolve; }),
    };
    this.entries.set(scope, entry);
    let outcome: SendOutcome = { status: 500, body: { success: false, reason: "send_idempotency_aborted" } };
    try {
      outcome = await execute();
      return { outcome, duplicate: false };
    } finally {
      if (isConfirmedSend(outcome)) {
        this.entries.set(scope, { at: this.now(), outcome });
        try {
          onConfirmed(outcome);
        } catch (error) {
          console.error("[send-idempotency] failed to journal confirmed key:", error);
        }
      } else if (this.entries.get(scope) === entry) {
        this.entries.delete(scope);
      }
      settle(outcome);
    }
  }
}

/** Header values are untrusted: printable ASCII without spaces, bounded length. */
export function parseIdempotencyKey(raw: string | undefined | null): string | null {
  const key = raw?.trim();
  if (!key || key.length > 256 || !/^[\x21-\x7e]+$/.test(key)) return null;
  return key;
}
