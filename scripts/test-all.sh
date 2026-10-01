#!/usr/bin/env bash
# Canonical MoP full suite (run before every commit to main): `npm run test:all`.
# 1. build dist  2. node suite (test/*.test.ts)
# 3. every pytest-discoverable python test under scripts/ (test_*.py)
# 4. the unittest-style test/*.test.py files (dotted names; pytest cannot import them)
# Script-style tests must be import-safe (runner behind __main__) so step 3 can
# collect them. scripts/test-*.mjs are legacy July probes and are not part of it.
set -euo pipefail
cd "$(dirname "$0")/.."
NODE="${MOP_NODE:-/Users/rajiv/.nvm/versions/node/v22.13.1/bin/node}"
npx tsc
"$NODE" --import tsx --test test/*.test.ts
python3 -m pytest -q -p no:cacheprovider scripts
for f in test/*.test.py; do
  echo "== $f"
  python3 "$f"
done
echo "MOP FULL SUITE: GREEN"
