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
