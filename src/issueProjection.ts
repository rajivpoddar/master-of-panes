/**
 * Canonical issue-side ownership projection for the native MoP assignment and
 * release transitions.
 *
 * The durable MoP assignment row is the owner authority; the GitHub issue
 * labels are a derived projection. This module is the single writer for that
 * projection so the retired PM transition script is not resurrected and no
 * caller has to hand-edit labels.
 *
 * Contracts:
 * - only ever touches the exact `slot:<slot>` label and the issue lifecycle
 *   labels this projection owns (`status:in-progress` on claim, `status:todo`
 *   when unwinding a claim); priority, OPEN state and unrelated labels are
 *   preserved;
 * - label operations are computed from a live read and applied idempotently, so
 *   a retry on an already-projected or already-released issue performs no
 *   duplicate work;
 * - every failure is returned as a typed outcome and never thrown, so the
 *   caller always still reports the durable MoP mutation truthfully.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export type IssueProjectionStatus = "projected" | "unchanged" | "skipped" | "failed";

export interface IssueProjectionOutcome {
  status: IssueProjectionStatus;
  reason: string | null;
  repository: string | null;
  issue: number;
  slot: number;
  added_labels: string[];
  removed_labels: string[];
  verified: boolean;
}

export interface IssueOwnershipProjection {
  onAssigned(issue: number, slot: number, repositoryId: string | null | undefined): Promise<IssueProjectionOutcome>;
  onReleased(issue: number, slot: number, repositoryId: string | null | undefined): Promise<IssueProjectionOutcome>;
}

export const DEFAULT_PROJECTION_REPOSITORY = "heydonna-app/heydonna-app";
export const DEFAULT_LEGACY_REPOSITORY_ID = "992731533";

/** Outcome used when no projector is wired (tests, non-PM deployments). */
export const NO_ISSUE_PROJECTION: IssueOwnershipProjection = {
  async onAssigned(issue, slot) {
    return outcome("skipped", "projection_not_configured", null, issue, slot);
  },
  async onReleased(issue, slot) {
    return outcome("skipped", "projection_not_configured", null, issue, slot);
  },
};

function outcome(
  status: IssueProjectionStatus,
  reason: string | null,
  repository: string | null,
  issue: number,
  slot: number,
  added: string[] = [],
  removed: string[] = [],
  verified = false,
): IssueProjectionOutcome {
  return {
    status,
    reason,
    repository,
    issue,
    slot,
    added_labels: added,
    removed_labels: removed,
    verified,
  };
}

export interface GhRunner {
  (args: string[]): Promise<string>;
}

export interface IssueProjectionOptions {
  runGh?: GhRunner;
  repository?: string;
  legacyRepositoryIds?: string[];
  timeoutMs?: number;
}

interface IssueSnapshot {
  state: string;
  labels: string[];
}

const LIFECYCLE_LABELS = [
  "status:todo",
  "status:in-progress",
  "status:in-review",
  "status:blocked",
  "status:done",
  "status:backlog",
  "status:deferred",
];

function defaultRunGh(timeoutMs: number): GhRunner {
  return async (args: string[]): Promise<string> => {
    const { stdout } = await execFileAsync("gh", args, {
      timeout: timeoutMs,
      maxBuffer: 4 * 1024 * 1024,
      env: process.env,
    });
    return stdout;
  };
}

/**
 * Build the canonical projection writer. `runGh` is injectable so tests never
 * touch GitHub.
 */
export function createGhIssueOwnershipProjection(
  options: IssueProjectionOptions = {},
): IssueOwnershipProjection {
  const runGh = options.runGh ?? defaultRunGh(options.timeoutMs ?? 20_000);
  const repository = options.repository
    ?? process.env.MOP_PROJECTION_REPOSITORY
    ?? DEFAULT_PROJECTION_REPOSITORY;
  const legacyIds = new Set([
    process.env.MOP_LEGACY_REPOSITORY_ID ?? DEFAULT_LEGACY_REPOSITORY_ID,
    ...(options.legacyRepositoryIds ?? []),
    repository,
  ]);

  function resolveRepository(repositoryId: string | null | undefined): string | null {
    if (repositoryId === null || repositoryId === undefined) {
      return repository;
    }
    const value = String(repositoryId).trim();
    if (!value) {
      return repository;
    }
    if (legacyIds.has(value)) {
      return repository;
    }
    // Canonical "github:<owner>/<repo>" form (the mop-assign-slot.py default).
    // Strip one leading "github:" and compare against the same identity set.
    const unprefixed = value.startsWith("github:") ? value.slice("github:".length) : value;
    return legacyIds.has(unprefixed) ? repository : null;
  }

  async function readIssue(issue: number): Promise<IssueSnapshot> {
    const stdout = await runGh([
      "issue", "view", String(issue),
      "--repo", repository,
      "--json", "state,labels",
    ]);
    const parsed = JSON.parse(stdout) as { state?: string; labels?: Array<{ name?: string }> };
    return {
      state: String(parsed.state ?? "").toUpperCase(),
      labels: (parsed.labels ?? [])
        .map((label) => String(label?.name ?? ""))
        .filter((name) => name.length > 0),
    };
  }

  async function applyLabels(issue: number, add: string[], remove: string[]): Promise<void> {
    const args = ["issue", "edit", String(issue), "--repo", repository];
    for (const label of add) {
      args.push("--add-label", label);
    }
    for (const label of remove) {
      args.push("--remove-label", label);
    }
    await runGh(args);
  }

  async function project(
    issue: number,
    slot: number,
    repositoryId: string | null | undefined,
    mode: "assign" | "release",
  ): Promise<IssueProjectionOutcome> {
    const target = resolveRepository(repositoryId);
    if (target === null) {
      return outcome("skipped", "repository_unmapped", null, issue, slot);
    }
    const slotLabel = `slot:${slot}`;
    let before: IssueSnapshot;
    try {
      before = await readIssue(issue);
    } catch (error) {
      return outcome("failed", reasonFor("issue_projection_read_failed", error), repository, issue, slot);
    }

    const add: string[] = [];
    const remove: string[] = [];
    if (mode === "assign") {
      if (!before.labels.includes("status:in-progress")) {
        add.push("status:in-progress");
      }
      if (!before.labels.includes(slotLabel)) {
        add.push(slotLabel);
      }
      if (before.labels.includes("status:todo")) {
        remove.push("status:todo");
      }
      // A foreign slot label is stale derived state, never a second owner
      // model, so it is corrected to the durable owner.
      for (const label of before.labels) {
        if (/^slot:\d+$/.test(label) && label !== slotLabel) {
          remove.push(label);
        }
      }
    } else {
      if (before.labels.includes(slotLabel)) {
        remove.push(slotLabel);
      }
      const otherLifecycle = before.labels.filter(
        (label) => LIFECYCLE_LABELS.includes(label) && label !== "status:in-progress",
      );
      if (before.labels.includes("status:in-progress") && otherLifecycle.length === 0) {
        if (before.state === "OPEN") {
          remove.push("status:in-progress");
          add.push("status:todo");
        } else {
          // Terminal issue: drop only the derived slot label and never invent
          // a lifecycle status on a closed issue.
          remove.push("status:in-progress");
        }
      }
    }

    if (add.length === 0 && remove.length === 0) {
      return outcome("unchanged", null, repository, issue, slot, [], [], true);
    }
    try {
      await applyLabels(issue, add, remove);
    } catch (error) {
      return outcome("failed", reasonFor("issue_projection_write_failed", error), repository, issue, slot);
    }

    let after: IssueSnapshot;
    try {
      after = await readIssue(issue);
    } catch (error) {
      return outcome(
        "failed",
        reasonFor("issue_projection_readback_failed", error),
        repository,
        issue,
        slot,
        add,
        remove,
      );
    }
    const verified = add.every((label) => after.labels.includes(label))
      && remove.every((label) => !after.labels.includes(label));
    if (!verified) {
      return outcome(
        "failed",
        "issue_projection_readback_mismatch",
        repository,
        issue,
        slot,
        add,
        remove,
      );
    }
    return outcome("projected", null, repository, issue, slot, add, remove, true);
  }

  return {
    onAssigned: (issue, slot, repositoryId) => project(issue, slot, repositoryId, "assign"),
    onReleased: (issue, slot, repositoryId) => project(issue, slot, repositoryId, "release"),
  };
}

function reasonFor(prefix: string, error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error);
  return `${prefix}:${detail.split("\n")[0].slice(0, 200)}`;
}
