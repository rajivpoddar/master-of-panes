import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  publishReviewHistory,
  releasePlanReviewReservation,
  reviewBudgetPreflight,
  runReviewBudget,
} from "../shared-assets/codex/skills/codex-review-companion/codex-review-companion.mjs";

const runner = (_command, args) => {
  if (args[0] === "rev-parse" && args[1] === "--show-toplevel") {
    return { code: 0, stdout: "/fixture/repo\n", stderr: "" };
  }
  if (args[0] === "rev-parse" && args[1] === "HEAD") {
    return { code: 0, stdout: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n", stderr: "" };
  }
  if (args[0] === "config") {
    return { code: 0, stdout: "https://github.com/example/repo.git\n", stderr: "" };
  }
  return { code: 1, stdout: "", stderr: "unsupported" };
};

function marker(issue, head, verdict = "REVISE", blockerClass = "same-blocker", pr = null) {
  return [
    "MARKER_PROVENANCE: codex-review-companion",
    "TYPE: plan-review",
    "TIMESTAMP: 1788593707",
    `ISSUE: #${issue}`,
    pr ? `PR: #${pr}` : "PR: -",
    `HEAD_SHA: ${head}`,
    `VERDICT: ${verdict}`,
    "--- Blockers (1) ---",
    `BLOCKER_CLASS: ${blockerClass}`,
    "BLOCKER_STATUS: OPEN",
    "--- Review Output ---",
    "bounded finding",
    "",
  ].join("\n");
}

function freshFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "plan-cap-"));
  const history = path.join(root, "durable");
  const legacy = path.join(root, "legacy");
  fs.mkdirSync(legacy);
  process.env.CODEX_REVIEW_HISTORY_DIR = history;
  process.env.CODEX_REVIEW_LEGACY_DIR = legacy;
  return { root, history, legacy };
}

function args(issue, head) {
  return {
    reviewType: "plan",
    issue: String(issue),
    pr: null,
    repoRoot: "/fixture/repo",
    _currentHead: head,
  };
}

function completeOne(fixture, issue, head, blockerClass = "same-blocker", pr = null) {
  const admission = args(issue, head);
  const checked = runReviewBudget(admission, runner);
  assert.equal(checked.ok, true);
  assert.equal(checked.budget.review_type_caps.length, 0);
  const canonical = path.join(fixture.root, `marker-${head}.md`);
  fs.writeFileSync(canonical, marker(issue, head, "REVISE", blockerClass, pr));
  admission._canonicalMarkerPath = canonical;
  const published = publishReviewHistory(admission, runner);
  assert.equal(published.ok, true);
}

{
  const fixture = freshFixture();
  completeOne(fixture, 442, "1111111111111111111111111111111111111111", "PROOF");
  completeOne(fixture, 442, "2222222222222222222222222222222222222222", "IMPLEMENTATION");
  completeOne(fixture, 442, "3333333333333333333333333333333333333333", "SCOPE");
  const fourth = runReviewBudget(
    args(442, "4444444444444444444444444444444444444444"),
    runner,
  );
  assert.equal(fourth.ok, true);
  assert.equal(fourth.budget.decision, "rescue_required");
  assert.deepEqual(fourth.budget.review_type_caps, ["plan"]);
}

{
  const fixture = freshFixture();
  fs.writeFileSync(
    path.join(fixture.legacy, "plan-issue-999-unrelated.md"),
    "not a plan marker for another issue\n",
  );
  fs.writeFileSync(
    path.join(fixture.legacy, "plan-issue-442-wrong-type.md"),
    "TYPE: code-review\nISSUE: #442\nnot a plan marker\n",
  );
  const retained = marker(442, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "REVISE", "PROOF", 7585);
  fs.writeFileSync(path.join(fixture.legacy, "plan-pr-7585-issue-442-a.md"), retained);
  fs.writeFileSync(path.join(fixture.legacy, "plan-pr-7585-issue-442-b.md"), retained);
  const imported = runReviewBudget(args(442, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"), runner);
  assert.equal(imported.ok, true);
  assert.equal(imported.budget.blocking_round_counts_48h.plan, 1);
  releasePlanReviewReservation(args(442, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"));
}

{
  const fixture = freshFixture();
  fs.writeFileSync(
    path.join(fixture.legacy, "plan-pr-7585-issue-442-malformed.md"),
    "TYPE: plan-review\nISSUE: #442\nmissing provenance and completed identity\n",
  );
  const rejected = runReviewBudget(
    args(442, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    runner,
  );
  assert.equal(rejected.ok, false);
  assert.match(rejected.message, /PLAN_REVIEW_HISTORY_UNAVAILABLE/);
}

{
  const fixture = freshFixture();
  const duplicateClasses = marker(
    442,
    "abababababababababababababababababababab",
    "REVISE",
    "AUTH",
  ).replace(
    "BLOCKER_STATUS: OPEN\n--- Review Output ---",
    "BLOCKER_STATUS: OPEN\nBLOCKER_ID: AUTH-002\nBLOCKER_CLASS: AUTH\nBLOCKER_STATUS: OPEN\n--- Review Output ---",
  );
  const admission = runReviewBudget(
    args(442, "abababababababababababababababababababab"),
    runner,
  );
  assert.equal(admission.ok, true);
  const canonical = path.join(fixture.root, "duplicate-classes.md");
  fs.writeFileSync(canonical, duplicateClasses);
  admission._canonicalMarkerPath = canonical;
  assert.equal(publishReviewHistory(admission, runner).ok, true);
  const afterOneRound = runReviewBudget(
    args(442, "bcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbc"),
    runner,
  );
  assert.equal(afterOneRound.ok, true);
  assert.equal(afterOneRound.budget.decision, "allowed");
  releasePlanReviewReservation(afterOneRound);
}

{
  const fixture = freshFixture();
  const failedTransport = args(7449, "cccccccccccccccccccccccccccccccccccccccc");
  const admission = runReviewBudget(failedTransport, runner);
  assert.equal(admission.ok, true);
  releasePlanReviewReservation(failedTransport);
  const retry = runReviewBudget(args(7449, "dddddddddddddddddddddddddddddddddddddddd"), runner);
  assert.equal(retry.ok, true);
  assert.equal(retry.budget.decision, "allowed");
  releasePlanReviewReservation(args(7449, "dddddddddddddddddddddddddddddddddddddddd"));
}

{
  const fixture = freshFixture();
  completeOne(fixture, 442, "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "PROOF");
  completeOne(fixture, 442, "ffffffffffffffffffffffffffffffffffffffff", "IMPLEMENTATION");
  const first = runReviewBudget(args(442, "9999999999999999999999999999999999999999"), runner);
  assert.equal(first.ok, true);
  const second = runReviewBudget(args(442, "8888888888888888888888888888888888888888"), runner);
  assert.equal(second.ok, true);
  assert.equal(second.budget.decision, "rescue_required");
  releasePlanReviewReservation(first);
}

{
  const fixture = freshFixture();
  completeOne(fixture, 442, "abababababababababababababababababababab", "AUTH");
  completeOne(fixture, 442, "bcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbcbc", "AUTH");
  const capped = reviewBudgetPreflight(
    { ...args(442, "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"), pr: "7585", markerFile: path.join(fixture.root, "expected-marker") },
    runner,
  );
  assert.equal(capped.allowed, false);
  assert.match(capped.message, /same_blocker_class:AUTH/);
}

{
  const fixture = freshFixture();
  completeOne(fixture, 442, "edededededededededededededededededededed", "PROOF");
  completeOne(fixture, 442, "fefefefefefefefefefefefefefefefefefefefe", "IMPLEMENTATION");
  completeOne(fixture, 442, "1212121212121212121212121212121212121212", "SCOPE");
  const state = path.join(fixture.root, "state");
  fs.mkdirSync(state);
  const proof = path.join(fixture.root, "override-proof.md");
  fs.writeFileSync(proof, "authorized override proof\n");
  const expectedMarker = path.join(fixture.root, "override-marker.txt");
  fs.writeFileSync(path.join(state, "pm-rescope-pr-7585.json"), JSON.stringify({
    status: "resolved",
    terminal_decision: "override_with_evidence",
    pr: "7585",
    headRefOid: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    terminal_decision_proof: proof,
  }));
  fs.writeFileSync(path.join(state, "pm-review-pending-7585.json"), JSON.stringify({
    status: "pending",
    scope: "phase-a",
    pr: "7585",
    headRefOid: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    expected_marker: expectedMarker,
  }));
  process.env.HEYDONNA_PM_STATE_DIR = state;
  const overrideArgs = { ...args(442, "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"), pr: "7585", markerFile: expectedMarker };
  const admitted = reviewBudgetPreflight(overrideArgs, runner);
  assert.equal(admitted.allowed, true);
  assert.ok(overrideArgs._planReviewReservation);
  const canonical = path.join(fixture.root, "override-canonical.md");
  fs.writeFileSync(canonical, marker(442, "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "REVISE", "OVERRIDE", 7585));
  overrideArgs._canonicalMarkerPath = canonical;
  const finalized = publishReviewHistory(overrideArgs, runner);
  assert.equal(finalized.ok, true);
  assert.equal(fs.existsSync(overrideArgs._planReviewReservation || ""), false);
  delete process.env.HEYDONNA_PM_STATE_DIR;
}

{
  const fixture = freshFixture();
  const blockedRoot = path.join(fixture.root, "not-a-directory");
  fs.writeFileSync(blockedRoot, "occupied");
  process.env.CODEX_REVIEW_HISTORY_DIR = blockedRoot;
  const unavailable = runReviewBudget(args(442, "1212121212121212121212121212121212121212"), runner);
  assert.equal(unavailable.ok, false);
  assert.match(unavailable.message, /PLAN_REVIEW_HISTORY_UNAVAILABLE/);
}

console.log("plan review cap focused proof: PASS");
