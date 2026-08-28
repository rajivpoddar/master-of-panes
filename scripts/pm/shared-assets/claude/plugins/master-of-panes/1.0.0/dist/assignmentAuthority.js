export const PM_TRANSITION_ASSIGNMENT_HEADER = "x-heydonna-assignment-authority";
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
export function isPmTransitionAssignmentRequest(authority) {
    return authority === PM_TRANSITION_ASSIGNMENT_AUTHORITY;
}
export function assignmentIdentityPatchFields(updates) {
    return Object.keys(updates)
        .filter((field) => ASSIGNMENT_IDENTITY_PATCH_FIELDS.has(field))
        .sort();
}
//# sourceMappingURL=assignmentAuthority.js.map