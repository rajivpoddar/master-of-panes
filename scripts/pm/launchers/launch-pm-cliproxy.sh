#!/usr/bin/env bash
# Shim (Rajiv DM D0AMF0XE6TS ts 1794019433.856239): the single PM launcher is launch-pm.sh; old callers keep working.
exec bash "$(dirname "${BASH_SOURCE[0]}")/launch-pm.sh" --via cliproxy "$@"
