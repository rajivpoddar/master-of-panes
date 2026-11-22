#!/usr/bin/env python3
"""Emit the skill-listing settings fragment for proxy lanes launched by proxy-agent.sh.

Rajiv D0AMF0XE6TS thread 1795230917.966119, ts 1795310703.137599 ("Yes, do it"): proxy lanes list only the skills they
use, to cut the skill listing from their prompt. The PM pane keeps its full list.

Mechanism (Claude Code docs: https://code.claude.com/docs/en/skills#override-skill-visibility-from-settings and
https://code.claude.com/docs/en/settings-reference , keys skillOverrides and skillListingMaxDescChars, scope "Any file" so
a --settings file may carry them): skillOverrides maps a skill name to "on" | "name-only" | "user-invocable-only" | "off";
a skill absent from the map is "on" and "off" hides it from the listing the model receives. skillListingMaxDescChars cuts
each listed description. proxy-agent.sh merges this fragment into the lane's own --settings file only, so the PM pane and
every other session are untouched. Plugin skills (the PM skills plugin, Codex, Cloudflare) are not affected by
skillOverrides, and a hidden skill's SKILL.md can still be read from disk (the launcher's lane note says so).

Why both keys: the listing is capped at 1% of the context window (about 10k tokens on a 1M window). Hiding skills alone
does not shrink it, because the freed budget is refilled with longer descriptions of the skills that remain (measured with
/context: 10.3k tokens with all 500 skills, 10.5k with 69 allowed skills). Capping each description at 160 characters is
what lowers the cost (about 5.5k).

Allowlist per lane = BASE (the PM's list) + USED (skills lanes demonstrably invoke) + skills the lane's agent file and
brief invoke explicitly + skills those skills invoke explicitly (transitive) + PROXY_AGENT_SKILL_ALLOW_EXTRA (comma list).
Everything else discovered under the user and clone skill/command roots is set to "off". Over-inclusion is the safe
direction; a missed skill only costs one read of its SKILL.md.

Usage: proxy-skill-overrides.py [--agent-file F] [--brief F] [--cwd DIR] [--stats]
Prints the JSON settings fragment on stdout: {"skillOverrides": {...}, "skillListingMaxDescChars": N}.
PROXY_AGENT_SKILL_ALLOWLIST=0 prints {} so the lane keeps the default listing. PROXY_AGENT_SKILL_DESC_CHARS overrides
the description cap (0 leaves Claude Code's default).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

# Skills proxy lanes use (PM brief, Rajiv ts 1795310703.137599).
BASE = (
    "launch-agent", "proxy-agent", "self-qa", "qa-brief", "playwright-qa-spec", "qa-video-review",
    "codex-app-code-review", "codex-app-plan-review", "codex-app-qa-review", "codex-review-companion",
    "ci-failure-investigation", "lockout-processing", "remote-editor-screenshots", "review-and-pr",
    "address-pr-review", "git-sync-main", "archived-lessons", "recall", "test-value-audit",
    "behavioral-red-on-revert-test", "reproduce-bug-as-test",
)

# Evidence-based additions: every user-level skill that at least 3 distinct lanes invoked in the lane transcripts of
# 2026-11-18..22 (/tmp/proxy-agent/*.jsonl, 2105 lanes) and that BASE does not already cover. The two most-used skills of
# all (codex-review-cap-marker-handling 118 lanes, codex-companion-marker-verdict-protocol 117) were missing from BASE.
# Counts are lanes. Plugin skills (pm-skills:*, cloudflare:*, codex:*) are not affected by skillOverrides and are not listed.
USED = (
    "codex-review-cap-marker-handling", "codex-companion-marker-verdict-protocol",  # 118, 117
    "ci-health", "axiom-heydonna-logs", "safe-commit",  # 68, 60, 52
    "hetzner-ops-light-ssh-access", "message-pm", "codex-qa-review", "pm-hotfix",  # 42, 41, 27, 26
    "slack-bridge", "bench-vertex-batch-modal", "gh-pr-mark-ready-graphql", "modal-cli", "modal",  # 21, 20, 18, 16, 13
    "codex-code-review", "detached-proof-run-monitoring", "committed-capture-refresh", "modal-app-logs",  # 13, 12, 11, 11
    "qa-report-evidence-embedding", "langsmith-fetch", "slack-message",  # 11, 10, 8
    "merge-conflict-both-branches-added-features", "github-pr-comment-image-private-repo-workaround",  # 8, 8
    "donna-shorts", "convex-mcp-runoneoffquery-syntax", "validator-boundary-doctrine", "axiom-user-trace",  # 8, 7, 7, 7
    "download-convex-data", "cliproxy-claude-switch", "blog-post", "shell-cli-contract-testing",  # 6, 6, 6, 6
    "base-runner-offline-processing", "heydonna-review-cap-cto-approve-handoff", "r2-customer-artifact-extraction",  # 5, 5, 5
    "explore-issue", "convex-cli-run-invoke-syntax", "affected-test-vacuous-red-consistent-revert",  # 4, 4, 4
    "llm-fixture-miss-investigation", "convex-phase8b-full-seed-refresh",  # 4, 3
    "cache-key-hash-stability-across-runs-not-just-mechanics", "strict-replay-provenance-binding",  # 3, 3
    "heydonna-remotion-story-video", "git-diff-merge-base-vs-origin-main",  # 3, 3
    "qa-preexisting-failure-merge-base-verification",  # 3
)

DESC_CHARS = 160

HOME = Path(os.path.expanduser("~"))
_FM = re.compile(r"^---\s*\n([\s\S]*?)\n---", re.M)
_NAME = re.compile(r"^name:\s*[\"']?([^\"'\n]+?)[\"']?\s*$", re.M)
# Explicit invocation forms only: Skill(x), Skill({skill: "x"}), "skill": "x". Plain mentions are not enough (a transitive
# closure over plain mentions pulled in 60+ unrelated skills).
_EXPLICIT = re.compile(r"""(?:Skill\(\s*(?:\{\s*skill["']?\s*:\s*)?|["']?skill["']?\s*:\s*)["'`]?([A-Za-z0-9_][A-Za-z0-9_:-]*)""", re.I)


def read(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit) if limit else fh.read()
    except OSError:
        return ""


def discover(skill_roots, command_roots):
    """name -> path of its file, for every skill dir (by dir name and frontmatter name) and command file."""
    found = {}
    seen = set()
    for root in skill_roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in seen:
                dirnames[:] = []
                continue
            seen.add(real)
            if "SKILL.md" not in filenames:
                continue
            path = os.path.join(dirpath, "SKILL.md")
            found.setdefault(os.path.basename(dirpath).strip(), path)
            fm = _FM.search(read(path, 4000))
            nm = _NAME.search(fm.group(1)) if fm else None
            if nm:
                found.setdefault(nm.group(1).split(" #")[0].strip(), path)
    for root in command_roots:
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
            rel = os.path.relpath(dirpath, root)
            prefix = "" if rel == "." else rel.replace(os.sep, ":") + ":"
            for fn in filenames:
                if fn.endswith(".md"):
                    found.setdefault(prefix + fn[:-3], os.path.join(dirpath, fn))
    return found


def named_in(text, known):
    """Known skill names that text invokes explicitly."""
    return {m.group(1).rstrip(".:-") for m in _EXPLICIT.finditer(text)} & set(known)


def allowlist(known, agent_file, brief, extra):
    allowed = {name for name in BASE + USED if name in known} | {n for n in extra if n in known}
    for source in (agent_file, brief):
        if source:
            allowed |= named_in(read(source), known)
    queue = sorted(allowed)
    while queue:  # transitive: a skill that tells the lane to invoke another skill
        name = queue.pop()
        for ref in named_in(read(known[name]), known):
            if ref not in allowed:
                allowed.add(ref)
                queue.append(ref)
    # A skill is identified by its directory and by its frontmatter name: keep both when either is allowed.
    by_path = {}
    for name, path in known.items():
        by_path.setdefault(path, set()).add(name)
    for names in by_path.values():
        if names & allowed:
            allowed |= names
    return allowed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-file")
    ap.add_argument("--brief")
    ap.add_argument("--cwd")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)
    if os.environ.get("PROXY_AGENT_SKILL_ALLOWLIST", "1") == "0":
        print("{}")
        print("proxy-skill-overrides: disabled (PROXY_AGENT_SKILL_ALLOWLIST=0)", file=sys.stderr)
        return 0
    skill_roots = [HOME / ".claude" / "skills"]
    command_roots = [HOME / ".claude" / "commands"]
    if args.cwd:
        skill_roots.append(Path(args.cwd) / ".claude" / "skills")
        command_roots.append(Path(args.cwd) / ".claude" / "commands")
    known = discover([str(p) for p in skill_roots if p.is_dir()], [str(p) for p in command_roots if p.is_dir()])
    extra = {n.strip() for n in os.environ.get("PROXY_AGENT_SKILL_ALLOW_EXTRA", "").split(",") if n.strip()}
    allowed = allowlist(known, args.agent_file, args.brief, extra)
    overrides = {name: "off" for name in sorted(known) if name not in allowed}
    fragment = {"skillOverrides": overrides}
    try:
        chars = int(os.environ.get("PROXY_AGENT_SKILL_DESC_CHARS", DESC_CHARS))
    except ValueError:
        chars = DESC_CHARS
    if chars > 0:
        fragment["skillListingMaxDescChars"] = chars
    if args.stats:
        print(
            "proxy-skill-overrides: discovered=%d allowed=%d off=%d allowed_names=%s"
            % (len(known), len(allowed & set(known)), len(overrides), ",".join(sorted(allowed & set(known)))),
            file=sys.stderr,
        )
    json.dump(fragment, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
