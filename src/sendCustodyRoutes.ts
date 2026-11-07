/**
 * HTTP glue for keyed sends: wraps `POST /slots/0/send` in custody + acceptance proof and
 * serves `GET /slots/:n/send-status`. Kept separate from server.ts so it can be exercised
 * against a stand-in handler.
 */

import type { Hono } from "hono";
import { confirmPmAcceptance, defaultAcceptanceDeps, type AcceptanceDeps } from "./pmAcceptance.js";
import { SendCustodyJournal, parseIdempotencyKey, sendRequestDigest } from "./sendCustody.js";

export type SendCustodyRouteDeps = {
  journal: SendCustodyJournal;
  /** Parses the :slotNum param; null when invalid. */
  parseSlot: (raw: string) => number | null;
  /** Only this slot has acceptance evidence; other slots keep their existing behaviour. */
  customSlot: number;
  destination: (slot: number) => Promise<string>;
  transcriptPath: () => string | null;
  acceptanceTimeoutMs: number;
  acceptanceDeps?: AcceptanceDeps;
  pollMs?: number;
  logEvent: (slot: number, type: string, payload: Record<string, unknown>) => void;
};

const json = (body: unknown, status: number): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export function registerSendCustody(app: Hono, deps: SendCustodyRouteDeps): void {
  const acceptanceDeps = deps.acceptanceDeps ?? defaultAcceptanceDeps;

  app.use("/slots/:slotNum/send", async (c, next) => {
    const key = c.req.method === "POST" ? parseIdempotencyKey(c.req.header("idempotency-key")) : null;
    const slot = deps.parseSlot(c.req.param("slotNum"));
    if (!key || slot === null || slot !== deps.customSlot) return next();
    const body = (await c.req.json().catch(() => null)) as Record<string, unknown> | null;
    if (!body || typeof body !== "object") return next();
    const { digest, commandDigest } = sendRequestDigest(body);

    const run = await deps.journal.run(
      { scope: `${slot}:${key}`, slot, key, digest, commandDigest, destination: await deps.destination(slot) },
      async () => {
        const transcriptPath = deps.transcriptPath();
        const fromOffset = transcriptPath ? await acceptanceDeps.size(transcriptPath).catch(() => null) : null;
        await next();
        const status = c.res.status;
        const original = (await c.res.clone().json().catch(() => ({}))) as Record<string, unknown>;
        if (!(status >= 200 && status < 300 && original.success === true) || original.queued === true) {
          return { status, body: original };
        }
        // The paste returned success at tmux level. It only counts once the CLI recorded the prompt.
        const proof = await confirmPmAcceptance({
          transcriptPath,
          fromOffset,
          command: typeof body.command === "string" ? body.command : "",
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
