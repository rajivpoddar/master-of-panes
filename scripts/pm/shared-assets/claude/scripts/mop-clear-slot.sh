#!/bin/sh
set -eu

usage() {
  echo "usage: mop-clear-slot.sh SLOT EPOCH REPOSITORY ISSUE PR BRANCH HEAD WORK_KIND HANDOFF_ID CLAIMED_AT MAIN_HEAD EFFECT_ID REQUEST_DIGEST" >&2
  exit 2
}

[ "$#" -eq 13 ] || usage
slot=$1
epoch=$2
repository=$3
issue=$4
pr=$5
branch=$6
head=$7
work_kind=$8
handoff_id=$9
claimed_at=${10}
main_head=${11}
effect_id=${12}
request_digest=${13}

case "$slot" in 1|2|3|4|5|6) ;; *) usage ;; esac
case "$epoch" in ''|*[!0-9]*) usage ;; esac
case "$issue" in ''|*[!0-9]*) usage ;; esac
case "$pr" in null) ;; ''|*[!0-9]*) usage ;; esac
case "$head" in ???????*) [ "${#head}" -eq 40 ] || usage ;; *) usage ;; esac
case "$main_head" in ???????*) [ "${#main_head}" -eq 40 ] || usage ;; *) usage ;; esac
case "$effect_id" in *[!A-Za-z0-9._:/-]*|'') usage ;; esac
case "$request_digest" in *[!A-Fa-f0-9]*|'') usage ;; esac
case "$repository" in ''|*[!A-Za-z0-9._/-]*) usage ;; esac
case "$branch" in ''|*[!A-Za-z0-9._/-]*) usage ;; esac
case "$work_kind" in ''|*[!A-Za-z0-9._/-]*) usage ;; esac
case "$handoff_id" in ''|*[!A-Za-z0-9._:/-]*) usage ;; esac
case "$claimed_at" in ''|*[!A-Za-z0-9._:+-]*) usage ;; esac
case "${MOP_ASSIGNMENT_AUTHORITY:-pm-transition-v1}" in ''|*[!A-Za-z0-9._:-]*) usage ;; esac

base_url=${MOP_BASE_URL:-http://127.0.0.1:3100}
authority=${MOP_ASSIGNMENT_AUTHORITY:-pm-transition-v1}
if [ "$pr" = "null" ]; then pr_json=null; else pr_json=$pr; fi

curl --silent --show-error --fail-with-body \
  --request POST "$base_url/slots/$slot/release" \
  --header "Accept: application/json" \
  --header "Content-Type: application/json" \
  --header "x-heydonna-assignment-authority: $authority" \
  --data "{\"expected_epoch\":$epoch,\"expected_repository_id\":\"$repository\",\"expected_issue\":$issue,\"expected_pr\":$pr_json,\"expected_branch\":\"$branch\",\"expected_head_sha\":\"$head\",\"expected_work_kind\":\"$work_kind\",\"expected_handoff_id\":\"$handoff_id\",\"expected_claimed_at\":\"$claimed_at\",\"intended_main_head\":\"$main_head\",\"effect_id\":\"$effect_id\",\"request_digest\":\"$request_digest\"}"
