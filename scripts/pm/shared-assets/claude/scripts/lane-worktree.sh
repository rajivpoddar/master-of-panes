#!/usr/bin/env bash
# lane-worktree.sh - the ONLY git-worktree path for fenced proxy lanes (PROXY_AGENT_SANDBOX=1).
#
#   lane-worktree.sh add    <path> [<branch>|<sha>]   create a worktree of the current repo (detached unless <branch>)
#   lane-worktree.sh remove <path> [--force]          remove a worktree this lane may own
#
# Why: the proxy Bash fence (proxy-agent.sh) cannot write a repo's .git, and `git worktree add|remove` must. The fence
# used to exclude "git worktree *" wholesale, which let any lane add or remove ANY registered worktree (CTO REVISE on
# f203baec, C0ALZJHGE49 thread 1795015806.949919, ts 1795015902.846089). Only this helper is excluded now. It is a
# trusted, runs-unsandboxed script, so every check below is a security boundary:
#   (a) the lane name comes from PROXY_AGENT_NAME (same sanitising as proxy-agent.sh); the target must be a DIRECT child
#       of /private/tmp (/tmp resolves there) named "<lane>-<suffix>". Any other lane's prefix is REFUSED. When a worktree
#       carries a lane-owner marker (written by `add` inside its git admin dir, which a fenced lane cannot write), the
#       marker must also equal this lane, so lane "x" cannot touch lane "x-2"'s "x-2-*" worktrees.
#   (b) every git call runs with hooks suppressed (-c core.hooksPath=/dev/null), fsmonitor off, and GIT_* env removed.
#   (c) `remove` only removes a worktree that `git worktree list` registers for the current repo, never the main
#       worktree, never a symlink, and only passes --force for such a registered, lane-owned path.
#   (d) exactly one output line per action: "lane-worktree: OK|REFUSED|FAILED action=... lane=... path=... ...".
# Exit codes: 0 ok, 2 refused (or usage), 1 git failure. Run it as a standalone command, not inside a compound
# command, or the fence will not recognise it as the excluded helper.
set -euo pipefail

TMP_ROOT="/private/tmp"
ACTION="" RAW_PATH="" LANE="" TARGET=""

usage() {
  printf 'usage: lane-worktree.sh add <path> [<branch>|<sha>] | remove <path> [--force]\n' >&2
  exit 2
}
refuse() {
  printf 'lane-worktree: REFUSED action=%s lane=%s path=%s reason=%s\n' "${ACTION:-?}" "${LANE:-?}" "${RAW_PATH:-?}" "$1" >&2
  exit 2
}
fail() {
  local rc="$1" detail="$2"
  detail="$(printf '%s' "$detail" | head -n1 | tr -c '[:print:]' ' ')"
  printf 'lane-worktree: FAILED action=%s lane=%s path=%s rc=%s detail=%s\n' "$ACTION" "$LANE" "${TARGET:-$RAW_PATH}" "$rc" "$detail" >&2
  exit 1
}

[[ $# -ge 2 ]] || usage
ACTION="$1"; RAW_PATH="$2"; shift 2
REF="" FORCE=0
case "$ACTION" in
  add)    REF="${1:-}"; [[ $# -le 1 ]] || usage;;
  remove) if [[ "${1:-}" == "--force" ]]; then FORCE=1; shift; fi; [[ $# -eq 0 ]] || usage;;
  *)      usage;;
esac

# (a) lane identity and path prefix.
LANE="$(printf '%s' "${PROXY_AGENT_NAME:-}" | tr -c 'A-Za-z0-9._-' '-')"
while [[ "$LANE" == -* ]]; do LANE="${LANE#-}"; done
while [[ "$LANE" == *- ]]; do LANE="${LANE%-}"; done
LANE="${LANE:0:80}"
while [[ "$LANE" == *- ]]; do LANE="${LANE%-}"; done
[[ -n "$LANE" && "$LANE" != "." && "$LANE" != ".." ]] || refuse no_lane_identity

[[ "$RAW_PATH" == /* ]] || refuse path_not_absolute
while [[ "$RAW_PATH" == */ && "$RAW_PATH" != "/" ]]; do RAW_PATH="${RAW_PATH%/}"; done
case "$RAW_PATH/" in
  */../*|*/./*|*//*) refuse path_not_normalized;;
esac
BASE="${RAW_PATH##*/}"
PARENT_RAW="${RAW_PATH%/*}"
[[ -n "$PARENT_RAW" ]] || PARENT_RAW="/"
PARENT="$(cd "$PARENT_RAW" 2>/dev/null && pwd -P)" || refuse parent_missing
[[ "$PARENT" == "$TMP_ROOT" ]] || refuse outside_lane_prefix
[[ "$BASE" =~ ^[A-Za-z0-9._-]+$ ]] || refuse bad_basename
[[ "$BASE" == "$LANE-"?* ]] || refuse outside_lane_prefix
TARGET="$TMP_ROOT/$BASE"
[[ ! -L "$TARGET" ]] || refuse target_is_symlink

# (b) hooks-off, GIT_*-free git.
UNSET_ARGS=(-u GIT_DIR)
for v in $(compgen -e); do
  case "$v" in GIT_*) UNSET_ARGS+=(-u "$v");; esac
done
git_safe() { env "${UNSET_ARGS[@]}" git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }

COMMON_RAW="$(git_safe rev-parse --git-common-dir 2>/dev/null)" || refuse not_in_git_repo
COMMON="$(cd "$COMMON_RAW" 2>/dev/null && pwd -P)" || refuse not_in_git_repo

# Registered worktrees of this repo, to detect the target and the main worktree (always listed first).
registered=0 is_main=0 first=1
while IFS= read -r line; do
  case "$line" in
    "worktree "*)
      p="${line#worktree }"
      pc="$(cd "$p" 2>/dev/null && pwd -P)" || pc="$p"
      if [[ "$p" == "$TARGET" || "$pc" == "$TARGET" ]]; then
        registered=1
        [[ $first -eq 1 ]] && is_main=1
      fi
      first=0;;
  esac
done < <(git_safe worktree list --porcelain 2>/dev/null)

if [[ "$ACTION" == add ]]; then
  [[ ! -e "$TARGET" ]] || refuse target_exists
  [[ $registered -eq 0 ]] || refuse already_registered
  if [[ -z "$REF" ]]; then
    MODE=detach
    SHA="$(git_safe rev-parse --verify --quiet 'HEAD^{commit}')" || refuse ref_unresolved
  else
    [[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/@^~{}+-]*$ ]] || refuse ref_invalid
    if git_safe show-ref --verify --quiet "refs/heads/$REF"; then
      MODE=branch SHA=""
    else
      MODE=detach
      SHA="$(git_safe rev-parse --verify --quiet "$REF^{commit}")" || refuse ref_unresolved
    fi
  fi
  if [[ "$MODE" == branch ]]; then
    out="$(git_safe worktree add "$TARGET" "$REF" 2>&1)" || fail $? "$out"
  else
    out="$(git_safe worktree add --detach "$TARGET" "$SHA" 2>&1)" || fail $? "$out"
  fi
  # Ownership marker in the worktree's git admin dir (outside any fenced lane's write set).
  gd="$(git_safe -C "$TARGET" rev-parse --git-dir 2>/dev/null || true)"
  if [[ -n "$gd" ]]; then
    gd="$(cd "$TARGET" && cd "$gd" 2>/dev/null && pwd -P)" || gd=""
    [[ -z "$gd" ]] || printf '%s\n' "$LANE" > "$gd/lane-owner" 2>/dev/null || true
  fi
  head="$(git_safe -C "$TARGET" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
  printf 'lane-worktree: OK action=add lane=%s path=%s mode=%s head=%s\n' "$LANE" "$TARGET" "$MODE" "$head"
  exit 0
fi

# remove (c)
[[ -e "$TARGET" ]] || refuse not_found
[[ $registered -eq 1 ]] || refuse not_registered
[[ $is_main -eq 0 ]] || refuse main_worktree
gd="$(git_safe -C "$TARGET" rev-parse --git-dir 2>/dev/null)" || refuse not_registered
gd="$(cd "$TARGET" && cd "$gd" 2>/dev/null && pwd -P)" || refuse not_registered
case "$gd" in "$COMMON"/worktrees/*) ;; *) refuse not_registered;; esac
if [[ -f "$gd/lane-owner" ]]; then
  owner="$(head -n1 "$gd/lane-owner" 2>/dev/null || true)"
  [[ "$owner" == "$LANE" ]] || refuse owned_by_other_lane
fi
if [[ $FORCE -eq 1 ]]; then
  out="$(git_safe worktree remove --force "$TARGET" 2>&1)" || fail $? "$out"
else
  out="$(git_safe worktree remove "$TARGET" 2>&1)" || fail $? "$out"
fi
printf 'lane-worktree: OK action=remove lane=%s path=%s force=%s\n' "$LANE" "$TARGET" "$FORCE"
