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
 * - reconciles derived slot, status and incompatible sub-status labels while
 *   preserving priority, OPEN state and unrelated labels;
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
export function configuredProjectionRepository(): string {
  return process.env.MOP_PROJECTION_REPOSITORY ?? DEFAULT_PROJECTION_REPOSITORY;
}
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

/**
 * Durable record, per issue, of the `status:` label the issue carried before the
 * assignment stripped it. Written at assign, consumed at release, so a released
 * issue returns to its real lifecycle instead of being reset to `status:todo`.
 * `null` clears the record (unknown or consumed).
 */
export interface IssuePriorStatusStore {
  get(issue: number): string | null;
  set(issue: number, status: string | null): void;
}

function memoryPriorStatusStore(): IssuePriorStatusStore {
  const map = new Map<number, string>();
  return {
    get: (issue) => map.get(issue) ?? null,
    set: (issue, status) => {
      if (status === null) map.delete(issue);
      else map.set(issue, status);
    },
  };
}

export interface IssueProjectionOptions {
  runGh?: GhRunner;
  repository?: string;
  legacyRepositoryIds?: string[];
  timeoutMs?: number;
  /** Durable pre-assignment status store; defaults to an in-process map (tests). */
  priorStatus?: IssuePriorStatusStore;
}

interface IssueSnapshot {
  state: string;
  labels: string[];
  body: string;
  /** Count of PRs that carry a closing reference to this issue (see readIssue). */
  linkedPullRequests: number;
}

/** Statuses a release may restore from the prior-status record. `in-review` needs a live PR and is never restored. */
const RESTORABLE_PRIOR_STATUSES = new Set(["status:todo", "status:blocked", "status:backlog", "status:deferred"]);

/**
 * Rule 54 status implied by the live ready-pool header: a hold other than
 * `dependency` means `blocked` (dependency-only waits may stay `todo`). Returns
 * null when the header implies nothing, leaving the caller's default in place.
 */
export function statusFromReadyPoolHeader(body: string): string | null {
  const header = /<!--\s*ready-pool:([\s\S]*?)-->/.exec(body);
  const holds = header ? /^\s*holds:\s*(.*)$/m.exec(header[1]) : null;
  if (!holds) return null;
  const nonDependency = holds[1]
    .split(",")
    .map((hold) => hold.trim())
    .filter((hold) => hold.length > 0 && hold !== "none" && hold !== "dependency");
  return nonDependency.length > 0 ? "status:blocked" : null;
}

/**
 * Status a released issue returns to. An open PR that closes the issue wins
 * (`in-review`); otherwise an independently projected lifecycle is kept, then
 * the recorded pre-assignment status, then the live header, then `todo`.
 */
export function releaseTargetStatus(
  snapshot: Pick<IssueSnapshot, "state" | "body" | "linkedPullRequests">,
  otherStatus: string | null,
  recorded: string | null,
): string {
  if (snapshot.state !== "OPEN") return otherStatus ?? "status:done";
  if (snapshot.linkedPullRequests > 0) return "status:in-review";
  if (otherStatus) return otherStatus;
  if (recorded && RESTORABLE_PRIOR_STATUSES.has(recorded)) return recorded;
  return statusFromReadyPoolHeader(snapshot.body) ?? "status:todo";
}

const SUB_STATUS_BY_STATUS: Record<string, string[]> = {
  "status:todo": ["sub:waiting-on-issue"],
  "status:blocked": ["sub:pm-gate", "sub:external", "sub:decision", "sub:infra"],
  "status:backlog": ["sub:needs-contract", "sub:pm-owned"],
};

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
  const repository = options.repository ?? configuredProjectionRepository();
  const priorStatus = options.priorStatus ?? memoryPriorStatusStore();
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
    // One read: `closedByPullRequestsReferences` lists PRs whose closing keyword
    // targets this issue. It carries no PR state, so an open-PR check is
    // approximated by any closing reference (a merged PR on an open issue is an
    // anomaly that resolves to `in-review`, which is never assignable).
    const stdout = await runGh([
      "issue", "view", String(issue),
      "--repo", repository,
      "--json", "state,labels,body,closedByPullRequestsReferences",
    ]);
    const parsed = JSON.parse(stdout) as {
      state?: string;
      labels?: Array<{ name?: string }>;
      body?: string;
      closedByPullRequestsReferences?: unknown[];
    };
    return {
      state: String(parsed.state ?? "").toUpperCase(),
      labels: (parsed.labels ?? [])
        .map((label) => String(label?.name ?? ""))
        .filter((name) => name.length > 0),
      body: String(parsed.body ?? ""),
      linkedPullRequests: (parsed.closedByPullRequestsReferences ?? []).length,
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

  // A stale record is harmless (the next assign overwrites it), so a store error
  // here must not turn a completed release into a thrown error.
  function clearPriorStatus(issue: number): void {
    try {
      priorStatus.set(issue, null);
    } catch {
      // intentionally ignored; see above
    }
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
      const priorStatuses = before.labels.filter(
        (label) => label.startsWith("status:") && label !== "status:in-progress",
      );
      // Record what the stripped status was. A re-assign of a claimed issue (already
      // in-progress, nothing to strip) keeps the record from the first claim.
      try {
        if (priorStatuses.length === 1) priorStatus.set(issue, priorStatuses[0]);
        else if (priorStatuses.length > 1 || !before.labels.includes("status:in-progress")) priorStatus.set(issue, null);
      } catch (error) {
        return outcome("failed", reasonFor("issue_projection_prior_status_write_failed", error), repository, issue, slot);
      }
      if (!before.labels.includes("status:in-progress")) {
        add.push("status:in-progress");
      }
      if (!before.labels.includes(slotLabel)) {
        add.push(slotLabel);
      }
      for (const label of before.labels) {
        if ((label.startsWith("status:") && label !== "status:in-progress") || label.startsWith("sub:")) {
          remove.push(label);
        }
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
        (label) => label.startsWith("status:") && label !== "status:in-progress",
      );
      // Multiple independent statuses have no safe precedence; refuse to guess.
      if (otherLifecycle.length > 1) {
        return outcome("failed", "issue_projection_ambiguous_status", repository, issue, slot);
      }
      let recorded: string | null;
      try {
        recorded = priorStatus.get(issue);
      } catch (error) {
        return outcome("failed", reasonFor("issue_projection_prior_status_read_failed", error), repository, issue, slot);
      }
      // Never reset a released issue to todo by default: it returns to the status it had
      // before the claim, or to in-review when a PR closes it (rule 54).
      const targetStatus = releaseTargetStatus(before, otherLifecycle[0] ?? null, recorded);
      const compatibleSubs = before.labels.filter(
        (label) => label.startsWith("sub:") && (SUB_STATUS_BY_STATUS[targetStatus] ?? []).includes(label),
      );
      if (compatibleSubs.length > 1) {
        return outcome("failed", "issue_projection_ambiguous_sub", repository, issue, slot);
      }
      if (before.labels.includes("status:in-progress")) remove.push("status:in-progress");
      // Exactly one status after release: an in-review PR link replaces any other lifecycle label.
      for (const label of otherLifecycle) {
        if (label !== targetStatus) remove.push(label);
      }
      if (!before.labels.includes(targetStatus)) add.push(targetStatus);
      for (const label of before.labels) {
        if (label.startsWith("sub:") && !(SUB_STATUS_BY_STATUS[targetStatus] ?? []).includes(label)) {
          remove.push(label);
        }
      }
    }

    if (add.length === 0 && remove.length === 0) {
      if (mode === "release") clearPriorStatus(issue);
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
    const statuses = after.labels.filter((label) => label.startsWith("status:"));
    const verified = add.every((label) => after.labels.includes(label))
      && remove.every((label) => !after.labels.includes(label))
      && (mode !== "assign" || (statuses.length === 1 && statuses[0] === "status:in-progress"))
      && (mode !== "release" || statuses.length === 1);
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
    if (mode === "release") clearPriorStatus(issue);
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
