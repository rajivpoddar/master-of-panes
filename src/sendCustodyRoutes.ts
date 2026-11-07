/**
 * HTTP glue for keyed sends: wraps `POST /slots/0/send` in custody + acceptance proof and
 * serves `GET /slots/:n/send-status`. Kept separate from server.ts so it can be exercised
 * against a stand-in handler.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import type { Hono } from "hono";
import type { PasteGuard } from "./pasteGuard.js";
import {
  AcceptanceLedger,
  confirmPmAcceptance,
  defaultAcceptanceDeps,
  type AcceptanceDeps,
  type PasteCursor,
  type PasteWindow,
} from "./pmAcceptance.js";
import { SendCustodyJournal, isAffirmativeZeroEffect, parseIdempotencyKey, sendRequestDigest } from "./sendCustody.js";

/** The recipient a keyed send is pinned to, and the transcript of THAT session. */
export type PmPin = {
  /** Immutable tmux pane id. */
  paneId: string;
  /** Session token: the id of the newest SessionStart event. A /clear, compact or respawn changes it. */
  session: string;
  /** Transcript path from that SessionStart event itself, never from another hook type. */
  transcriptPath: string | null;
};

export type SendCustodyRouteDeps = {
  journal: SendCustodyJournal;
  ledger: AcceptanceLedger;
  /** Parses the :slotNum param; null when invalid. */
  parseSlot: (raw: string) => number | null;
  /** Only this slot has acceptance evidence; other slots keep their existing behaviour. */
  customSlot: number;
  /** Resolve the current recipient; null when it cannot be verified. */
  resolvePin: (slot: number) => Promise<PmPin | null>;
  acceptanceTimeoutMs: number;
  acceptanceDeps?: AcceptanceDeps;
  pollMs?: number;
  /** Wall clock used to stamp a paste; defaults to Date.now. */
  wallClock?: () => number;
  logEvent: (slot: number, type: string, payload: Record<string, unknown>) => void;
};

const json = (body: unknown, status: number): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const sendContext = new AsyncLocalStorage<{ guard: PasteGuard }>();

/** The pin enforced inside the serialized writer for the keyed request being handled, if any. */
export function currentSendGuard(): PasteGuard | undefined {
  return sendContext.getStore()?.guard;
}

const samePin = (a: PmPin, b: PmPin | null): boolean => b !== null && a.paneId === b.paneId && a.session === b.session;

export function registerSendCustody(app: Hono, deps: SendCustodyRouteDeps): void {
  const acceptanceDeps = deps.acceptanceDeps ?? defaultAcceptanceDeps;
  const wallClock = deps.wallClock ?? Date.now;

  app.use("/slots/:slotNum/send", async (c, next) => {
    const key = c.req.method === "POST" ? parseIdempotencyKey(c.req.header("idempotency-key")) : null;
    const slot = deps.parseSlot(c.req.param("slotNum"));
    if (!key || slot === null || slot !== deps.customSlot) return next();
    const body = (await c.req.json().catch(() => null)) as Record<string, unknown> | null;
    if (!body || typeof body !== "object") return next();
    const { digest, commandDigest } = sendRequestDigest(body);

    const pin = await deps.resolvePin(slot);
    if (!pin) {
      // No verifiable recipient: nothing is written and nothing is recorded.
      return json({ success: false, reason: "pane_identity_mismatch", zero_effect: true, safe_to_retry: true, idempotency_key: key }, 409);
    }

    const run = await deps.journal.run(
      { scope: `${slot}:${key}`, slot, key, digest, commandDigest, destination: `${pin.paneId}|session:${pin.session}` },
      async () => {
        let cursor: PasteCursor | null = null;
        let window: PasteWindow | null = null;
        let overlapsIdentical = false;
        const guard: PasteGuard = {
          paneId: pin.paneId,
          // Runs inside the relay's serialized writer, right before paste-buffer.
          async beforePaste(text) {
            if (!samePin(pin, await deps.resolvePin(slot))) return { ok: false, reason: "recipient_changed" };
            if (pin.transcriptPath) {
              const identity = await acceptanceDeps.identity(pin.transcriptPath).catch(() => null);
              if (identity) {
                cursor = { path: pin.transcriptPath, dev: identity.dev, ino: identity.ino, size: identity.size, startedAtMs: wallClock(), text };
                const opened = deps.ledger.open(cursor);
                window = opened.window;
                overlapsIdentical = opened.overlapsIdentical;
              }
            }
            return { ok: true };
          },
        };

        await sendContext.run({ guard }, () => next());
        const status = c.res.status;
        const original = (await c.res.clone().json().catch(() => ({}))) as Record<string, unknown>;
        if (!(status >= 200 && status < 300 && original.success === true)) {
          const outcome = { status, body: original };
          if (!isAffirmativeZeroEffect(outcome)) return outcome;
          // Tell the caller, in one place, that this refusal had no effect and may be retried.
          const refusal = { ...original, zero_effect: true, safe_to_retry: true };
          c.res = json(refusal, status);
          return { status, body: refusal };
        }
        if (original.queued === true) return { status, body: original };

        // The paste returned success at tmux level. It only counts once the CLI recorded THIS prompt.
        const proof = await confirmPmAcceptance({
          ledger: deps.ledger,
          cursor,
          window,
          overlapsIdentical,
          recipientIntact: async () => samePin(pin, await deps.resolvePin(slot)),
          timeoutMs: deps.acceptanceTimeoutMs,
          pollMs: deps.pollMs,
          deps: acceptanceDeps,
        });
        const outcome = proof.accepted
          ? { status, body: { ...original, accepted: true, acceptance: proof.reason } }
          : {
              status: 502,
              body: {
                success: false,
                reason: "pm_acceptance_unconfirmed",
                detail: proof.reason,
                pasted: true,
                ambiguous: true,
                zero_effect: false,
                safe_to_retry: false,
              },
            };
        c.res = json(outcome.body, outcome.status);
        return outcome;
      },
    );

    const recorded = deps.journal.lookup(`${slot}:${key}`);
    deps.logEvent(slot, "send_custody", {
      key,
      decision: run.kind,
      state: recorded?.state ?? null,
      reason: run.kind === "rejected" ? run.body.reason : recorded?.reason ?? null,
      destination_changed: run.kind === "duplicate" ? run.destinationChanged : undefined,
    });
    if (run.kind === "executed") return;
    if (run.kind === "rejected") return json({ ...run.body, idempotency_key: key }, run.status);
    return json(
      {
        success: true,
        duplicate: true,
        idempotency_key: key,
        custody_state: run.row.state,
        accepted: run.row.state === "accepted",
        queued: run.row.state === "queued",
        destination_changed: run.destinationChanged,
      },
      200,
    );
  });

  /** Read-only custody lookup for a caller that lost a response. Never sends anything. */
  app.get("/slots/:slotNum/send-status", (c) => {
    const slot = deps.parseSlot(c.req.param("slotNum"));
    const key = parseIdempotencyKey(c.req.query("key"));
    if (slot === null || !key) return c.json({ error: "slot and key are required" }, 400);
    const row = deps.journal.lookup(`${slot}:${key}`);
    if (!row) return c.json({ key, state: "unknown_key" }, 404);
    const claimed = c.req.query("command_digest");
    return c.json({
      key,
      state: row.state,
      reason: row.reason,
      attempts: row.attempts,
      command_digest: row.command_digest,
      command_digest_match: claimed ? claimed === row.command_digest : undefined,
      destination: row.destination,
      updated_at: row.updated_at,
    });
  });
}
