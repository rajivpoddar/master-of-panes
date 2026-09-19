export const PM_TRANSITION_ASSIGNMENT_HEADER =
  "x-heydonna-assignment-authority";

export const PM_TRANSITION_ASSIGNMENT_AUTHORITY = "pm-transition-v1";

export const ASSIGNMENT_IDENTITY_PATCH_FIELDS = new Set([
  "occupied",
  "status",
  "repository_id",
  "issue",
  "pr",
  "branch",
  "branch_ref",
  "head_sha",
  "assignment_epoch",
  "assigned_at",
  "work_kind",
  "handoff_id",
  "claimed_at",
]);

export const ASSIGNMENT_AUTHORITY_REQUIRED_MESSAGE =
  "Assignment authority is required. pm-transition.sh is retired; the canonical PM release/assign path is the direct REST route with header x-heydonna-assignment-authority: pm-transition-v1.";

export const ASSIGNMENT_AUTHORITY_REQUIRED_REMEDIATION =
  "POST /slots/:n/release with that header and JSON body {expected_epoch, expected_repository_id, expected_issue, " +
  "expected_pr:null, expected_branch:null, expected_head_sha:null, expected_work_kind:null, expected_handoff_id:null, " +
  "expected_claimed_at:<live value from a fresh slot read, equal to the stored claimed_at, not null>, " +
  "intended_main_head:<40-hex head>, release_mode:'quiescent_legacy_issue_only'}. " +
  "Precondition: the slot is idle/inactive with its checkout on branch main at intended_main_head, clean with no " +
  "unpushed commits (send switch-to-main-and-pull first). The effect identity is minted server-side; do not " +
  "supply effect_id. Stale MCP clients (frozen pre-contract bundle): call this REST route directly.";

// Assign-family remediation for POST /slots/:n/assign and
// POST /slots/:n/adopt-issue-claim authority refusals. This must never name
// the release route: an assignment caller following a release recipe would
// perform the wrong operation. Bodies below mirror the route validators.
export const ASSIGNMENT_ROUTE_REMEDIATION =
  "POST /slots/:n/assign with header x-heydonna-assignment-authority: pm-transition-v1 and JSON body " +
  '{issue:<positive integer>, task:"<non-empty task>"} (repository_id defaults to the legacy repository when omitted). ' +
  "For a complete claim, send all of {expected_epoch, repository_id, issue, pr (null for issue-only or a positive integer), " +
  'branch, head_sha (40-hex), work_kind, handoff_id, task} with valid values. ' +
  "Rebind callers use POST /slots/:n/adopt-issue-claim with the same header and the expected_current_*/desired tuple body. " +
  "Do not call the release route for assignment.";

export function isPmTransitionAssignmentRequest(
  authority: string | undefined
): boolean {
  return authority === PM_TRANSITION_ASSIGNMENT_AUTHORITY;
}

export function assignmentIdentityPatchFields(
  updates: Record<string, unknown>
): string[] {
  return Object.keys(updates)
    .filter((field) => ASSIGNMENT_IDENTITY_PATCH_FIELDS.has(field))
    .sort();
}
