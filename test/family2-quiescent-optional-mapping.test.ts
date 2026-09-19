import assert from "node:assert/strict";
import test from "node:test";
import { Hono } from "hono";

import { PM_TRANSITION_ASSIGNMENT_AUTHORITY, PM_TRANSITION_ASSIGNMENT_HEADER } from "../src/assignmentAuthority.js";
import { normalizedFamily2ReleaseBody } from "../src/db.js";
import { registerFamily2Routes } from "../src/family2Routes.js";
import { Family2ReleaseEffectAdapter } from "../src/family2ReleaseEffect.js";

const HEAD = "a".repeat(40);
const EPOCH = 866;
const NULLABLE = ["pr", "branch", "head_sha", "work_kind", "handoff_id", "claimed_at"] as const;

async function releaseWith(flatBody: Record<string, unknown>) {
  const captured: any[] = [];
  const app = new Hono();
  registerFamily2Routes(app, {
    db: { getNativeReleaseEffectReceipt: () => null, logEvent: () => undefined } as any,
    nativeSlotRelease: { release: async (req: any) => { captured.push(req); return { success: true, idempotent: false, assignment_epoch: EPOCH + 1 }; } } as any,
    family2ReleaseEffectAdapter: new Family2ReleaseEffectAdapter(async () => ({ ok: false, status: 404, json: async () => ({}) }) as any),
    clearPlanApprovalTimer: () => undefined,
  });
  const response = await app.request("http://mop/slots/1/release", {
    method: "POST",
    headers: { [PM_TRANSITION_ASSIGNMENT_HEADER]: PM_TRANSITION_ASSIGNMENT_AUTHORITY },
    body: JSON.stringify(flatBody),
  });
  return { response, request: captured[0] };
}

test("quiescent flat body canonicalizes absent nullable optionals to explicit null", async () => {
  const { response, request } = await releaseWith({
    expected_epoch: EPOCH,
    expected_repository_id: "heydonna-app/heydonna-app",
    expected_issue: 7945,
    intended_main_head: HEAD,
    release_mode: "quiescent_legacy_issue_only",
  });
  assert.equal(response.status, 200);
  for (const key of NULLABLE) {
    assert.equal(request.expected_tuple[key], null, `${key} must be explicit null, not undefined`);
  }
  const normalized = normalizedFamily2ReleaseBody({
    effect_id: `quiescent-legacy-issue-only:slot-1:epoch-${EPOCH}`,
    expected_epoch: EPOCH,
    expected_tuple: request.expected_tuple,
    intended_main_head: HEAD,
  } as any);
  assert.equal(Object.keys(normalized).length, 11);
});

test("required members are never defaulted to null", async () => {
  const { request } = await releaseWith({
    expected_epoch: EPOCH,
    expected_repository_id: "heydonna-app/heydonna-app",
    intended_main_head: HEAD,
    release_mode: "quiescent_legacy_issue_only",
  });
  assert.equal(request.expected_tuple.issue, undefined, "absent issue must stay absent");
  assert.throws(
    () => normalizedFamily2ReleaseBody({
      effect_id: `quiescent-legacy-issue-only:slot-1:epoch-${EPOCH}`,
      expected_epoch: EPOCH,
      expected_tuple: request.expected_tuple,
      intended_main_head: HEAD,
    } as any),
    /Family-2 release tuple is invalid/,
  );
});
