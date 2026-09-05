import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  publishReviewHistory,
  releasePlanReviewReservation,
  runReviewBudget,
} from "../shared-assets/codex/skills/codex-review-companion/codex-review-companion.mjs";

const runner = (_command, args) => {
  if (args[0] === "rev-parse" && args[1] === "--show-toplevel") {
    return { code: 0, stdout: "/fixture/repo\n", stderr: "" };
  }
  if (args[0] === "config") {
    return { code: 0, stdout: "https://github.com/example/repo.git\n", stderr: "" };
  }
  return { code: 1, stdout: "", stderr: "unsupported" };
};

function marker(issue, head, verdict = "REVISE") {
  return [
    "MARKER_PROVENANCE: codex-review-companion",
    "TYPE: plan-review",
    `ISSUE: #${issue}`,
    `HEAD_SHA: ${head}`,
    `VERDICT: ${verdict}`,
    "--- Blockers (1) ---",
    "BLOCKER_CLASS: same-blocker",
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

function completeOne(fixture, issue, head) {
  const admission = args(issue, head);
  const checked = runReviewBudget(admission, runner);
  assert.equal(checked.ok, true);
  assert.equal(checked.budget.review_type_caps.length, 0);
  const canonical = path.join(fixture.root, `marker-${head}.md`);
  fs.writeFileSync(canonical, marker(issue, head));
  admission._canonicalMarkerPath = canonical;
  const published = publishReviewHistory(admission, runner);
  assert.equal(published.ok, true);
}

{
  const fixture = freshFixture();
  completeOne(fixture, 442, "1111111111111111111111111111111111111111");
  completeOne(fixture, 442, "2222222222222222222222222222222222222222");
  completeOne(fixture, 442, "3333333333333333333333333333333333333333");
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
  const retained = marker(442, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
  fs.writeFileSync(path.join(fixture.legacy, "plan-issue-442-a.md"), retained);
  fs.writeFileSync(path.join(fixture.legacy, "plan-issue-442-b.md"), retained);
  const imported = runReviewBudget(args(442, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"), runner);
  assert.equal(imported.ok, true);
  assert.equal(imported.budget.blocking_round_counts_48h.plan, 1);
  releasePlanReviewReservation(args(442, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"));
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
  completeOne(fixture, 442, "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee");
  completeOne(fixture, 442, "ffffffffffffffffffffffffffffffffffffffff");
  const first = runReviewBudget(args(442, "9999999999999999999999999999999999999999"), runner);
  assert.equal(first.ok, true);
  const second = runReviewBudget(args(442, "8888888888888888888888888888888888888888"), runner);
  assert.equal(second.ok, true);
  assert.equal(second.budget.decision, "rescue_required");
  releasePlanReviewReservation(first);
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
