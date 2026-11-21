#!/usr/bin/env bash
# Link bulk-read and code-write into ~/.local/bin (on every proxy lane's PATH) from the active MoP release. Idempotent.
# The links point through the "current" release symlink, so activating a new release updates them with no re-run.
# Usage: install-bulk-read-links.sh [SCRIPTS_DIR] [BIN_DIR]
set -euo pipefail
scripts="${1:-$HOME/.local/share/master-of-panes/current/scripts}"
bin="${2:-$HOME/.local/bin}"
mkdir -p "$bin"
for name in bulk-read code-write; do
  [[ -x "$scripts/$name" ]] || { echo "missing $scripts/$name" >&2; exit 1; }
  if [[ -e "$bin/$name" && ! -L "$bin/$name" ]]; then echo "skipped $bin/$name: a regular file is already there" >&2; continue; fi
  ln -sfn "$scripts/$name" "$bin/$name"
  echo "linked $bin/$name -> $scripts/$name"
done
