---
name: bash-single-quote-python-c-inner-quote-collision
description: |
  Fix for SyntaxError / NameError in `python3 -c '<block>'` where the block is wrapped in OUTER single quotes
  and contains INNER single quotes. Use when: (1) a python3 -c / python -c inline block fails with
  "SyntaxError: f-string: expecting a valid expression after '{'" or a NameError on a string-literal key,
  (2) the error reports a line number that doesn't match the file's apparent content, (3) the source file
  LOOKS syntactically correct but only fails when run via the shell. The shell silently strips inner single
  quotes inside a single-quoted -c argument, corrupting the Python source before python parses it.
  NOT for: (4) heredoc-based `python3 <<'EOF'` that conflicts with piped stdin — see
  bash-python3-pipe-stdin-heredoc-conflict; (5) Python raw-string / regex escape problems
  (python-raw-string-assertion-escape-mismatch); (6) ruff/flake8 lint escape warnings (ruff-w605-docstring-regex-escape).
author: Claude Code
version: 1.0.0
date: 2026-05-28
last-validated: 2026-05-28
supersedes: []
---

# Bash single-quote vs `python3 -c '...'` inner-quote collision

## When NOT to Use
- The Python is delivered via a heredoc and the conflict is stdin being consumed by the heredoc — see `bash-python3-pipe-stdin-heredoc-conflict`.
- The bug is a Python regex / raw-string escape (`\d` warnings, `re.escape`) — see `python-raw-string-assertion-escape-mismatch` / `ruff-w605-docstring-regex-escape`.
- You need literal backticks or `$(...)` preserved in a bash string — see `bash-literal-backticks-via-heredoc`.
- The error is a genuine Python bug visible when you run the SAME file directly with `python3 file.py` (no shell-quoting layer). This skill is only for the `-c '...'` shell-mangling class.

## Problem
A `python3 -c '<block>'` (or `python -c '...'`) invocation wrapped in OUTER single quotes cannot contain INNER single quotes. POSIX shells have no escape for `'` inside a `'...'` string — every inner `'` is interpreted as "close the single-quoted span", so the shell silently rewrites the Python source before `python3` ever sees it.

The source file/heredoc looks 100% valid Python in isolation, so reading the file teaches you nothing. The corruption is introduced at shell-expansion time.

## Context / Trigger Conditions
- `python3 -c '<...>'` inline block (often embedded in a `.sh` script, a CI step, or a `$(... | python3 -c '...')` command substitution).
- Runtime error such as:
  - `SyntaxError: f-string: expecting a valid expression after '{'` — from an f-string like `f"...{','.join(x)}..."` becoming `f"...{,.join(x)}..."`.
  - `NameError: name 'updatedAt' is not defined` — from `d.get('updatedAt')` becoming `d.get(updatedAt)`.
- The reported line number indexes the `-c` STRING (e.g., "line 63"), not the file line (e.g., 153). Grepping the file for the reported line misleads.
- Often lurks silently: if most of the block uses double quotes (safe under outer `'...'`) and only a few lines use inner single quotes, only those lines break — and Python stops at the FIRST SyntaxError, masking later ones.

## Solution
**Diagnose:** Compare the RUNTIME-executed source (visible in stderr / the error log — it shows the post-shell-mangling version, e.g. `{,.join(...)}`) against the file content (`{','.join(...)}`). A quote that is present in the file but absent at runtime = shell single-quote collision.

**Fix — pick one:**

(a) **Pre-compute into DOUBLE-quoted variables** (smallest diff; works even when the block reads piped stdin). Move every inner-single-quoted expression out of the f-string into a plain assignment using double quotes, then reference the variable quote-free:
```python
# BROKEN inside python3 -c '...'
print(f"#{n} reason=missing_{','.join(missing)} updatedAt={d.get('updatedAt')}")
# FIXED
joined = ",".join(missing)          # double quotes — safe under outer '...'
ua = d.get("updatedAt")             # double quotes — safe
print(f"#{n} reason=missing_{joined} updatedAt={ua}")   # no quotes inside {}
```
Why it works: double quotes do not collide with the outer shell single-quote, and the f-string `{}` placeholders now contain only bare identifiers.

(b) **Switch to a single-quoted heredoc** (cleanest when the block does NOT read piped stdin — a heredoc IS stdin, so this is unavailable for `printf ... | python3 -c '...'` patterns):
```bash
python3 - <<'PYEOF'
print(f"{','.join(x)}")   # any quotes safe — 'PYEOF' = zero shell interpolation
PYEOF
```
The single-quoted delimiter (`'PYEOF'`) disables ALL shell expansion/quote-processing inside the heredoc body.

## Verification
Re-run the command. The previously-failing path should execute. For a script, grep the run log for the prior `SyntaxError` / `NameError` and confirm the failing print/expression now produces real output.

## Example
a PM helper script’s `python3 -c '...'` backlog-audit block (piped stdin) had three f-string prints using `{','.join(missing)}` and `{issue.get('updatedAt')}`. At runtime the shell delivered `{,.join(missing)}` → `SyntaxError: f-string: expecting a valid expression after '{'` reported at "line 63" (the `-c` string), while file line 153 read correctly. Because the block read `json.load(sys.stdin)` from a pipe, heredoc (option b) was unavailable → applied option (a): added `joined_missing = ",".join(missing)` and `ua = issue.get("updatedAt")` before the prints, referenced `{joined_missing}`/`{ua}`. Rerun printed `BACKLOG_MISFILED` lines + `BACKLOG_AUDIT_SUMMARY` with zero SyntaxError.

## Notes
- Rule of thumb inside `python3 -c '...'`: use ONLY double quotes for every Python string literal and dict key. Reserve single quotes for places the shell can't see them (i.e., never).
- f-strings are doubly deceptive: `f"...{ '...' }..."` is valid Python (the inner `'` differs from the f-string's `"` delimiter), so static reading of the file passes — but the SHELL layer still strips the inner `'`.
- The same collision hits `awk '...'`, `perl -e '...'`, `jq '...'`, and any `-e`/`-c` style single-quoted program argument. The double-quote-the-inner-strings fix generalizes.
- If you must embed a literal single quote in a single-quoted shell string, the POSIX idiom is `'\''` (close, escaped-quote, reopen) — but for multi-line Python, option (a) or (b) is far more readable.
