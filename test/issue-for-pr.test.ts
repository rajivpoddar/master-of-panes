import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase, type AssignmentTupleInput } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";

// Rajiv 2026-10-03 08:25 IST (C0ALZJHGE49/1790995117.258909, "do it"):
// post-merge cleanup resolves the delivered issue from MoP's own tuples.

const REPO = "github:heydonna-app/heydonna-app";
let seq = 0;

function withDb(run: (db: MoPDatabase) => void): void {
  const directory = mkdtempSync(join(tmpdir(), "mop-issue-for-pr-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    run(db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

function mint(db: MoPDatabase, tuple: Partial<AssignmentTupleInput>): void {
  seq += 1;
  const desired: AssignmentTupleInput = {
    repository_id: REPO, issue: null, pr: null, branch: null, head_sha: null,
    work_kind: null, handoff_id: null, claimed_at: null, ...tuple,
  };
  const result = db.mintAssignmentEffectIntent({
    effect_id: `effect-${seq}`, request_digest: `digest-${seq}`, slot: 1,
    selection_class: "new_issue", before_epoch: seq, desired_tuple: desired, task_digest: "t",
  });
  assert.equal(result.ok, true);
}

test("pr match resolves the issue", () => withDb((db) => {
  mint(db, { issue: 8700, pr: 8701, branch: "fix/a" });
  assert.deepEqual(db.resolveIssueForPr(REPO, 8701, null), { issue: 8700, source: "pr", ambiguous: false });
}));

test("bare owner/name repository is accepted", () => withDb((db) => {
  mint(db, { issue: 8700, pr: 8701 });
  assert.equal(db.resolveIssueForPr("heydonna-app/heydonna-app", 8701, null).issue, 8700);
}));

test("new_issue branch fallback when the pr was never recorded", () => withDb((db) => {
  mint(db, { issue: 8710, branch: "feat/8710-thing" });
  assert.deepEqual(db.resolveIssueForPr(REPO, 8799, "feat/8710-thing"), { issue: 8710, source: "branch", ambiguous: false });
}));

test("pr match wins over branch", () => withDb((db) => {
  mint(db, { issue: 8720, pr: 8721 });
  mint(db, { issue: 8722, branch: "b" });
  assert.equal(db.resolveIssueForPr(REPO, 8721, "b").issue, 8720);
}));

test("ambiguous distinct issues return no issue", () => withDb((db) => {
  mint(db, { issue: 8730, pr: 8731 });
  mint(db, { issue: 8732, pr: 8731 });
  assert.deepEqual(db.resolveIssueForPr(REPO, 8731, null), { issue: null, source: "pr", ambiguous: true });
  mint(db, { issue: 8740, branch: "shared" });
  mint(db, { issue: 8741, branch: "shared" });
  assert.deepEqual(db.resolveIssueForPr(REPO, 1, "shared"), { issue: null, source: "branch", ambiguous: true });
}));

test("repeated assignments for the same issue are not ambiguous", () => withDb((db) => {
  mint(db, { issue: 8750, branch: "x" });
  mint(db, { issue: 8750, pr: 8751, branch: "x" });
  assert.equal(db.resolveIssueForPr(REPO, 8751, "x").issue, 8750);
}));

test("no match and other repository return no issue", () => withDb((db) => {
  mint(db, { repository_id: "github:other/repo", issue: 1, pr: 2 });
  assert.deepEqual(db.resolveIssueForPr(REPO, 2, null), { issue: null, source: null, ambiguous: false });
}));

test("a PR number recorded as the issue (repro shape) is ignored", () => withDb((db) => {
  mint(db, { issue: 8594, pr: 8619 });
  mint(db, { issue: 8619, pr: 8619 });
  assert.deepEqual(db.resolveIssueForPr(REPO, 8619, null), { issue: 8594, source: "pr", ambiguous: false });
}));
