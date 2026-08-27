import {
  assignmentTupleMatches,
  normalizeAssignmentTuple,
  normalizedFamily2ReleaseBody,
  computeFamily2ReleaseDigest,
  type AssignmentTupleInput,
} from "./db.js";
import {
  PM_TRANSITION_ASSIGNMENT_AUTHORITY,
  PM_TRANSITION_ASSIGNMENT_HEADER,
} from "./assignmentAuthority.js";

export interface Family2ReleaseResponse {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}
export type Family2ReleaseFetch = (input: string, init?: RequestInit) => Promise<Family2ReleaseResponse>;

export interface Family2ReleaseEffectRequest {
  base_url: string;
  slot: number;
  effect_id: string;
  expected_epoch: number;
  expected_session_id: string;
  expected_tuple: AssignmentTupleInput;
  intended_main_head: string;
}

export type Family2ReleaseEffectCode =
  | "released" | "invalid_request" | "slot_read_failed" | "slot_snapshot_invalid"
  | "slot_not_releasable" | "release_failed" | "release_response_invalid"
  | "free_readback_failed" | "effect_receipt_conflict" | "effect_receipt_invalid";

export interface Family2ReleaseEffectReceipt {
  success: boolean;
  code: Family2ReleaseEffectCode;
  message: string;
  remediation: string;
  release_id: string | null;
  request_digest: string | null;
  before: { slot: number; assignment_epoch: number; session_id: string; tuple: AssignmentTupleInput } | null;
  after: { slot: number; assignment_epoch: number; occupied: boolean; session_id: string | null } | null;
  idempotent?: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function responseReceipt(code: Family2ReleaseEffectCode, message: string, remediation: string, extra: Partial<Family2ReleaseEffectReceipt> = {}): Family2ReleaseEffectReceipt {
  return { success: code === "released", code, message, remediation, release_id: null, request_digest: null, before: null, after: null, ...extra };
}
function slotSnapshot(value: unknown): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  return isRecord(value.slot) ? value.slot : value;
}
function tupleFromSlot(slot: Record<string, unknown>): AssignmentTupleInput {
  return { repository_id: (slot.repository_id ?? null) as string | number | null, issue: (slot.issue ?? null) as number | null, pr: (slot.pr ?? null) as number | null, branch: (slot.branch ?? null) as string | null, head_sha: (slot.head_sha ?? null) as string | null, work_kind: (slot.work_kind ?? null) as string | null, handoff_id: (slot.handoff_id ?? null) as string | null, claimed_at: (slot.claimed_at ?? null) as string | null };
}
function validTuple(value: AssignmentTupleInput): boolean { try { return !!value && normalizeAssignmentTuple(value) !== null; } catch { return false; } }
function sameBinding(a: Family2ReleaseEffectRequest, b: Family2ReleaseEffectRequest): boolean {
  const at = normalizeAssignmentTuple(a.expected_tuple); const bt = normalizeAssignmentTuple(b.expected_tuple);
  return !!at && !!bt && assignmentTupleMatches(at, bt) && a.slot === b.slot && a.expected_epoch === b.expected_epoch && a.expected_session_id === b.expected_session_id && a.intended_main_head.toLowerCase() === b.intended_main_head.toLowerCase();
}

/** Stateless adapter for one already-committed Family-2 release effect. */
export class Family2ReleaseEffectAdapter {
  constructor(private readonly fetch: Family2ReleaseFetch = globalThis.fetch as Family2ReleaseFetch) {}

  private async receipt(request: Family2ReleaseEffectRequest, base: string): Promise<Family2ReleaseEffectReceipt | null> {
    try {
      const response = await this.fetch(
        `${base}/slots/${request.slot}/release-receipt?effect_id=${encodeURIComponent(request.effect_id)}`,
        { headers: { [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY } },
      );
      if (response.status === 404) return null;
      const payload = await response.json();
      if (!response.ok || !isRecord(payload) || payload.success !== true || typeof payload.request_digest !== "string" || typeof payload.released_epoch !== "number") return responseReceipt("effect_receipt_invalid", "Durable release receipt is malformed.", "Stop and reconcile the committed outbox effect before retrying.", { release_id: request.effect_id });
      const receiptTuple = payload.expected_tuple as AssignmentTupleInput;
      const expectedDigest = computeFamily2ReleaseDigest(request);
      if (payload.effect_id !== request.effect_id || payload.request_digest.toLowerCase() !== expectedDigest || payload.expected_epoch !== request.expected_epoch || payload.slot !== request.slot || payload.expected_session_id !== request.expected_session_id || typeof payload.intended_main_head !== "string" || payload.intended_main_head.toLowerCase() !== request.intended_main_head.toLowerCase() || !assignmentTupleMatches(normalizeAssignmentTuple(receiptTuple), normalizeAssignmentTuple(request.expected_tuple))) return responseReceipt("effect_receipt_conflict", "Durable release receipt conflicts with the immutable effect binding.", "Keep the outbox row and obtain an exact transition reconciliation.", { release_id: request.effect_id, request_digest: expectedDigest });
      return responseReceipt("released", "The exact release effect was already committed; receipt consumed idempotently.", "Continue with remaining committed effects only.", { release_id: request.effect_id, request_digest: expectedDigest, idempotent: true, after: { slot: request.slot, assignment_epoch: payload.released_epoch, occupied: false, session_id: null } });
    } catch (error) {
      return responseReceipt("effect_receipt_invalid", `Release receipt read failed: ${error instanceof Error ? error.message : String(error)}`, "Keep the committed outbox effect retryable and reconcile the exact receipt.", { release_id: request.effect_id });
    }
  }

  private body(request: Family2ReleaseEffectRequest): Record<string, unknown> {
    return normalizedFamily2ReleaseBody(request);
  }

  async release(request: Family2ReleaseEffectRequest): Promise<Family2ReleaseEffectReceipt> {
    if (typeof request.base_url !== "string" || !request.base_url.trim() || !Number.isInteger(request.slot) || request.slot < 1 || request.slot > 4 || typeof request.effect_id !== "string" || !request.effect_id.trim() || !Number.isInteger(request.expected_epoch) || typeof request.expected_session_id !== "string" || !request.expected_session_id.trim() || !/^[0-9a-f]{40}$/i.test(request.intended_main_head) || !validTuple(request.expected_tuple)) return responseReceipt("invalid_request", "Immutable release effect identity and complete tuple are required.", "Retry with the committed Family-2 outbox binding.");
    const base = request.base_url.replace(/\/$/, "");
    const prior = await this.receipt(request, base); if (prior) return prior;
    let beforeResponse: Family2ReleaseResponse; let beforePayload: unknown;
    try { beforeResponse = await this.fetch(`${base}/slots/${request.slot}`); beforePayload = await beforeResponse.json(); } catch (error) { return responseReceipt("slot_read_failed", `Authoritative slot read failed: ${error instanceof Error ? error.message : String(error)}`, "Keep the committed effect durable and retry after MoP is healthy."); }
    const before = slotSnapshot(beforePayload); const currentTuple = before ? tupleFromSlot(before) : null; const normalized = currentTuple && normalizeAssignmentTuple(currentTuple);
    if (!beforeResponse.ok || !before || before.slot !== request.slot || before.occupied !== true || before.active_turn_state !== "inactive" || before.idle !== true || before.assignment_epoch !== request.expected_epoch || before.session_id !== request.expected_session_id || !normalized || !assignmentTupleMatches(normalized, normalizeAssignmentTuple(request.expected_tuple))) return responseReceipt("slot_not_releasable", "Current slot does not match the immutable committed release tuple.", "Leave the slot untouched and reconcile the committed effect before retrying.");
    const beforeReceipt = { slot: request.slot, assignment_epoch: request.expected_epoch, session_id: request.expected_session_id, tuple: request.expected_tuple };
    const body = this.body(request); const requestDigest = computeFamily2ReleaseDigest(request);
    let releaseResponse: Family2ReleaseResponse; let releasePayload: unknown;
    try { releaseResponse = await this.fetch(`${base}/slots/${request.slot}/release`, { method: "POST", headers: { "content-type": "application/json", [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY }, body: JSON.stringify({ ...body, request_digest: requestDigest }) }); releasePayload = await releaseResponse.json(); } catch (error) { return responseReceipt("release_failed", `Native release request failed: ${error instanceof Error ? error.message : String(error)}`, "Keep the effect uncertain; reconcile its durable receipt before retry.", { release_id: request.effect_id, request_digest: requestDigest, before: beforeReceipt }); }
    if (!releaseResponse.ok) return responseReceipt("release_failed", `Native release returned HTTP ${releaseResponse.status}`, "Keep the effect retryable and inspect the typed native refusal.", { release_id: request.effect_id, request_digest: requestDigest, before: beforeReceipt });
    if (!isRecord(releasePayload) || releasePayload.success !== true) return responseReceipt("release_response_invalid", "Native release response was malformed or unsuccessful.", "Keep the effect retryable and reconcile the durable receipt.", { release_id: request.effect_id, request_digest: requestDigest, before: beforeReceipt });
    let afterResponse: Family2ReleaseResponse; let afterPayload: unknown;
    try { afterResponse = await this.fetch(`${base}/slots/${request.slot}`); afterPayload = await afterResponse.json(); } catch (error) { return responseReceipt("free_readback_failed", `FREE readback failed: ${error instanceof Error ? error.message : String(error)}`, "Retry only through durable receipt reconciliation; do not reissue release.", { release_id: request.effect_id, request_digest: requestDigest, before: beforeReceipt }); }
    const after = slotSnapshot(afterPayload);
    if (!afterResponse.ok || !after || after.occupied !== false || after.assignment_epoch !== request.expected_epoch + 1 || after.session_id !== null) return responseReceipt("free_readback_failed", "Native release did not prove FREE epoch+1.", "Reconcile the exact durable receipt before retrying.", { release_id: request.effect_id, request_digest: requestDigest, before: beforeReceipt });
    return responseReceipt("released", "Native release committed and FREE epoch+1 readback verified.", "Consume this effect once and continue with remaining effects.", { release_id: request.effect_id, request_digest: requestDigest, before: beforeReceipt, after: { slot: request.slot, assignment_epoch: after.assignment_epoch as number, occupied: false, session_id: null }, idempotent: false });
  }
}

/** Consumer boundary for an immutable committed outbox payload. */
export async function consumeFamily2ReleaseEffect(payload: unknown, adapter: Family2ReleaseEffectAdapter): Promise<Family2ReleaseEffectReceipt> {
  if (!isRecord(payload) || typeof payload.base_url !== "string" || typeof payload.effect_id !== "string") return responseReceipt("invalid_request", "Committed Family-2 effect payload is missing immutable release bindings.", "Retain the outbox row and refuse delivery until its tuple is complete.");
  return adapter.release(payload as unknown as Family2ReleaseEffectRequest);
}
