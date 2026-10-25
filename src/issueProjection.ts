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

export type IssueProjectionStatus = "projected" | "unchanged" | "skipped" | "failed" | "pending";

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

/**
 * Synchronous durable-generation check supplied by the caller (a DB read of the
 * committed owner/epoch). The projector re-evaluates it after every await and
 * immediately before every network write; false means this plan was superseded
 * and the projector stops without writing or certifying anything.
 */
export type ProjectionGuard = () => boolean;

export const SUPERSEDED_REASON = "superseded_by_newer_ownership";

export interface IssueOwnershipProjection {
  onAssigned(
    issue: number,
    slot: number,
    repositoryId: string | null | undefined,
    guard?: ProjectionGuard,
  ): Promise<IssueProjectionOutcome>;
  onReleased(
    issue: number,
    slot: number,
    repositoryId: string | null | undefined,
    guard?: ProjectionGuard,
  ): Promise<IssueProjectionOutcome>;
}

// Per-issue write exclusion: an older plan's in-flight network write finishes
// before a successor reads, so the successor always converges on live labels.
const issueChains = new Map<number, Promise<unknown>>();
async function withIssueLock<T>(issue: number, work: () => Promise<T>): Promise<T> {
  const previous = issueChains.get(issue) ?? Promise.resolve();
  const run = previous.then(work, work);
  const tail = run.catch(() => undefined);
  issueChains.set(issue, tail);
  try {
    return await run;
  } finally {
    if (issueChains.get(issue) === tail) issueChains.delete(issue);
  }
}

export const DEFAULT_PROJECTION_REPOSITORY = "Scribie/heydonna-app";
// Stored assignment identities predate the repository transfer; do not migrate them.
const LEGACY_PROJECTION_REPOSITORY = "heydonna-app/heydonna-app";
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
    LEGACY_PROJECTION_REPOSITORY,
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

  // REST, not `gh issue edit`: edit takes ~40s wall against this repo (GraphQL
  // metadata round trips), which exceeded the 20s runGh timeout so every label
  // write was killed and no assignment ever projected (2026-10-25 incident).
  // The REST label endpoints answer in about a second. Each write is preceded
  // by a fresh generation check; returns false when superseded.
  async function applyLabels(
    issue: number,
    add: string[],
    remove: string[],
    live: () => boolean,
    applied: { added: string[]; removed: string[] },
  ): Promise<boolean> {
    const base = `repos/${repository}/issues/${issue}/labels`;
    if (add.length > 0) {
      if (!live()) return false;
      await runGh(["api", "-X", "POST", base, ...add.flatMap((label) => ["-f", `labels[]=${label}`])]);
      applied.added.push(...add);
    }
    for (const label of remove) {
      if (!live()) return false;
      await runGh(["api", "-X", "DELETE", `${base}/${encodeURIComponent(label)}`]);
      applied.removed.push(label);
    }
    return true;
  }

  function project(
    issue: number,
    slot: number,
    repositoryId: string | null | undefined,
    mode: "assign" | "release",
    guard?: ProjectionGuard,
  ): Promise<IssueProjectionOutcome> {
    return withIssueLock(issue, () => projectLocked(issue, slot, repositoryId, mode, guard));
  }

  async function projectLocked(
    issue: number,
    slot: number,
    repositoryId: string | null | undefined,
    mode: "assign" | "release",
    guard?: ProjectionGuard,
  ): Promise<IssueProjectionOutcome> {
    const live = (): boolean => {
      try {
        return guard ? guard() : true;
      } catch {
        return false;
      }
    };
    const applied = { added: [] as string[], removed: [] as string[] };
    const superseded = (): IssueProjectionOutcome =>
      outcome("skipped", SUPERSEDED_REASON, repository, issue, slot, applied.added, applied.removed);
    const target = resolveRepository(repositoryId);
    if (target === null) {
      return outcome("skipped", "repository_unmapped", null, issue, slot);
    }
    if (!live()) return superseded();
    const slotLabel = `slot:${slot}`;
    let before: IssueSnapshot;
    try {
      before = await readIssue(issue);
    } catch (error) {
      return outcome("failed", reasonFor("issue_projection_read_failed", error), repository, issue, slot);
    }
    if (!live()) return superseded();

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
      if (!(await applyLabels(issue, add, remove, live, applied))) return superseded();
    } catch (error) {
      return outcome("failed", reasonFor("issue_projection_write_failed", error), repository, issue, slot);
    }
    if (!live()) return superseded();

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
    if (!live()) return superseded();
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
    onAssigned: (issue, slot, repositoryId, guard) => project(issue, slot, repositoryId, "assign", guard),
    onReleased: (issue, slot, repositoryId, guard) => project(issue, slot, repositoryId, "release", guard),
  };
}

function reasonFor(prefix: string, error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error);
  // execFile errors put the useful stderr (or "killed" on timeout) after the first line.
  const stderr = String((error as { stderr?: unknown } | null)?.stderr ?? "").trim().split("\n")[0];
  const killed = (error as { killed?: unknown } | null)?.killed ? " [killed:timeout]" : "";
  return `${prefix}:${detail.split("\n")[0].slice(0, 120)}${killed}${stderr ? ` | ${stderr.slice(0, 120)}` : ""}`;
}
