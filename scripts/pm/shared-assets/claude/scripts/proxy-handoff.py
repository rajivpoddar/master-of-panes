#!/usr/bin/env python3
"""Per-issue HANDOFF files for proxy lanes.

Rajiv approved ("yes, do it", D0AMF0XE6TS thread 1795441948.588769, ts 1795443398.969229):
  - ~/.claude/state/handoffs/issue-<N>.md holds DONE, OPEN, NEXT, DECIDED + WHY, DON'T for one issue.
  - A lane rewrites it at every push and before it stops; the launcher injects the current file into
    any successor's brief (same issue number).
  - DON'T entries (what was tried and failed, and why) are never dropped without PM approval.
  - A successor's first output is a two-line restate (goal + next step); the restate is checked against
    the handoff's first NEXT item, and a mismatch stops the lane before it spends.

Commands (the issue comes from --issue N, or from --brief FILE: PROXY_AGENT_ISSUE, then its ISSUE: line,
then its PR: line mapped through a pr-<M>.issue link, then the PR number itself):
  key       --brief F                      print the resolved key (rc 1 when none)
  path      (--issue N | --brief F)        print the handoff file path
  read      (--issue N | --brief F)        print the file (rc 1 when missing)
  validate  (--issue N | --file F)         rc 0 valid, rc 2 with reasons
  write     --issue N [--from F] [--by NAME] [--approve-drop SLACK_TS --drop TEXT ...]
                                            validate, refuse to lose a DON'T, replace atomically (stdin when no --from)
  add-dont  --issue N TEXT                 append one DON'T bullet (creates a skeleton when no file exists)
  inject    --brief F [--type T] [--name N] [--dry-run]
                                            print the HANDOFF block + contract for the brief (empty when not eligible)
  restate-check (--issue N | --brief F) (--next TEXT | --restate-file F | --jsonl F)
                                            RESTATE_OK / NO_HANDOFF rc 0, RESTATE_MISMATCH rc 4
  link      --pr M --issue N               remember that PR M belongs to issue N
  list                                      one line per handoff file

FAIL-OPEN: inject always exits 0 (a problem is logged to stderr and nothing is injected), so it can never block a launch.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

HOME = Path.home()
SELF = Path(__file__).resolve()
SECTIONS = ("DONE", "OPEN", "NEXT", "DECIDED + WHY", "DON'T")
_CANON = {"DONE": "DONE", "OPEN": "OPEN", "NEXT": "NEXT", "DECIDED+WHY": "DECIDED + WHY", "DONT": "DON'T"}
DEFAULT_TYPES = ("hd-planner", "hd-builder", "hd-proof-qa", "hd-investigator", "pm-hotfix", "pm-proof-qa")
PLACEHOLDERS = {"none", "n/a", "na", "tbd", "-", "nothing", "none yet"}
MAX_WRITE_BYTES = 40_000
INJECT_FULL_CHARS = 16_000
SLACK_TS = re.compile(r"^\d{9,11}\.\d{6}$")
SECRET_LIKE = re.compile(r"(?i)(bearer\s+[a-z0-9._~+/=-]{20,}|\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{10,})")
HEAD_RE = re.compile(r"^##\s+(.+?)\s*$")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
STOP = frozenset("""a an and are as at be but by for from has have in is it its not of on or so that the their then this to was were will with you your
into than also only when which who how all any can may must should would about after before over per via no yes do does did""".split())


def log(message: str) -> None:
    print(f"PROXY_HANDOFF {message}", file=sys.stderr)


def handoff_dir() -> Path:
    return Path(os.environ.get("PROXY_HANDOFF_DIR") or (HOME / ".claude/state/handoffs"))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def locked(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- keys -----------------------------------------------------------------------------------------------------------------------------
def _ref(text: str, key: str) -> str | None:
    m = re.search(r"^\s*" + key + r"\s*:\s*#?(\d{3,6})\b", text, re.I | re.M)
    return m.group(1) if m else None


def link_path(pr: str) -> Path:
    return handoff_dir() / f"pr-{pr}.issue"


def read_link(pr: str) -> str | None:
    try:
        value = link_path(pr).read_text().strip()
    except OSError:
        return None
    return value if re.fullmatch(r"\d{3,7}", value) else None


def write_link(pr: str, issue: str) -> None:
    with locked(handoff_dir()):
        atomic_write(link_path(pr), issue + "\n")


def resolve_key(brief_text: str, environ=os.environ) -> tuple[str | None, str | None, str | None]:
    """(key, issue, pr). Order: PROXY_AGENT_ISSUE, ISSUE: line, PR: line via link, PR number."""
    issue, pr = _ref(brief_text, "ISSUE"), _ref(brief_text, "PR")
    forced = (environ.get("PROXY_AGENT_ISSUE") or "").strip().lstrip("#")
    if re.fullmatch(r"\d{3,7}", forced):
        return forced, issue or forced, pr
    if issue:
        return issue, issue, pr
    if pr:
        return (read_link(pr) or pr), None, pr
    return None, None, None


def handoff_path(key: str) -> Path:
    return handoff_dir() / f"issue-{key}.md"


def eligible(role: str, environ=os.environ) -> bool:
    if (environ.get("PROXY_HANDOFF") or "1") == "0":
        return False
    allowed = (environ.get("PROXY_HANDOFF_TYPES") or "").split() or list(DEFAULT_TYPES)
    return role in allowed


# ---- parsing --------------------------------------------------------------------------------------------------------------------------
def canon_head(title: str) -> str | None:
    title = re.sub(r"\(.*?\)", "", title)
    return _CANON.get(re.sub(r"[^A-Z+]", "", title.upper()))


def parse(text: str) -> tuple[list[str], dict[str, list[str]], list[str]]:
    pre: list[str] = []
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    cur: str | None = None
    for line in text.splitlines():
        m = HEAD_RE.match(line)
        canon = canon_head(m.group(1)) if m else None
        if canon:
            cur = canon
            order.append(canon)
            sections.setdefault(canon, [])
            continue
        (pre if cur is None else sections[cur]).append(line)
    return pre, sections, order


def items(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        m = BULLET.match(line)
        if m:
            out.append(m.group(1).strip())
        elif out and line[:1] in (" ", "\t"):
            out[-1] += " " + line.strip()
        else:
            out.append(line.strip())
    return out


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x60*_]", "", text)).strip().lower().rstrip(".;")


def validate_text(text: str) -> list[str]:
    errors: list[str] = []
    if len(text.encode("utf-8")) > MAX_WRITE_BYTES:
        errors.append("too_large: trim DONE and DECIDED + WHY (never DON'T)")
    if SECRET_LIKE.search(text):
        errors.append("secret_like_token: handoff files are injected into briefs; remove it")
    _pre, sections, order = parse(text)
    for name in SECTIONS:
        if order.count(name) == 0:
            errors.append(f"missing_section:{name}")
        elif order.count(name) > 1:
            errors.append(f"duplicate_section:{name}")
        elif not items(sections.get(name, [])):
            errors.append(f"empty_section:{name} (write 'none yet' when there is nothing)")
    present = [s for s in order if s in SECTIONS]
    if not any(e.startswith(("missing_section", "duplicate_section")) for e in errors) and present != list(SECTIONS):
        errors.append("section_order: expected " + " > ".join(SECTIONS))
    nxt = items(sections.get("NEXT", []))
    if nxt and (len(nxt[0]) < 8 or norm(nxt[0]) in PLACEHOLDERS or nxt[0].upper().startswith("UNSET")):
        errors.append("next_unset: the first NEXT item must be one concrete step (ids, paths, shas)")
    return errors


def first_next(text: str) -> str | None:
    nxt = items(parse(text)[1].get("NEXT", []))
    if not nxt or nxt[0].upper().startswith("UNSET") or norm(nxt[0]) in PLACEHOLDERS:
        return None
    return nxt[0]


def dont_items(text: str) -> list[str]:
    return [i for i in items(parse(text)[1].get("DON'T", [])) if norm(i) not in PLACEHOLDERS]


def updated_meta(text: str) -> str:
    m = re.search(r"^updated:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else "unknown"


def stamp(text: str, key: str, by: str) -> str:
    """Normalise the preamble (title, fresh updated: line, the lane's other key: value lines) and trim section padding."""
    pre, sections, _order = parse(text)
    extra = [l for l in pre if re.match(r"^[a-z_]+:\s", l) and not l.startswith(("updated:", "by:"))]
    out = [f"# HANDOFF issue #{key}", f"updated: {now_iso()} by {by}"] + extra
    for name in SECTIONS:
        body = [l.rstrip() for l in sections.get(name, [])]
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        out += ["", f"## {name}"] + body
    return "\n".join(out).rstrip() + "\n"


# ---- commands -------------------------------------------------------------------------------------------------------------------------
def _key_from(args) -> str | None:
    if getattr(args, "issue", None):
        return str(args.issue).lstrip("#")
    if getattr(args, "brief", None):
        try:
            return resolve_key(Path(args.brief).read_text(errors="replace"))[0]
        except OSError:
            return None
    return None


def cmd_key(args) -> int:
    key = _key_from(args)
    if key:
        print(key)
        return 0
    return 1


def cmd_path(args) -> int:
    key = _key_from(args)
    if not key:
        return 1
    print(handoff_path(key))
    return 0


def cmd_read(args) -> int:
    key = _key_from(args)
    if not key:
        return 1
    try:
        sys.stdout.write(handoff_path(key).read_text())
    except OSError:
        return 1
    return 0


def cmd_validate(args) -> int:
    try:
        text = Path(args.file).read_text() if args.file else handoff_path(str(args.issue)).read_text()
    except OSError as exc:
        print(f"HANDOFF_INVALID unreadable:{type(exc).__name__}")
        return 2
    errors = validate_text(text)
    if errors:
        print("HANDOFF_INVALID " + "; ".join(errors))
        return 2
    print("HANDOFF_OK")
    return 0


def _by(args) -> str:
    return getattr(args, "by", None) or os.environ.get("PROXY_AGENT_NAME") or "unknown"


def cmd_write(args) -> int:
    key = str(args.issue).lstrip("#")
    text = Path(args.source).read_text() if args.source else sys.stdin.read()
    errors = validate_text(text)
    if errors:
        print("HANDOFF_REFUSED " + "; ".join(errors))
        return 2
    target = handoff_path(key)
    with locked(handoff_dir()):
        old = target.read_text() if target.exists() else ""
        new_norm = [norm(i) for i in dont_items(text)]
        lost = [d for d in dont_items(old) if not any(n.startswith(norm(d)) for n in new_norm)]
        dropped_ok: list[str] = []
        if lost:
            drops = [norm(d) for d in (args.drop or []) if norm(d)]
            if SLACK_TS.match(args.approve_drop or ""):
                dropped_ok = [d for d in lost if any(norm(d).startswith(x) for x in drops)]
            lost = [d for d in lost if d not in dropped_ok]
        if lost:
            print("HANDOFF_REFUSED dont_dropped (a DON'T is never removed or reworded without PM approval; keep it, extend it, or "
                  "pass --approve-drop <PM slack ts> --drop <its text>): " + " | ".join(l[:120] for l in lost))
            return 3
        if dropped_ok:
            with open(handoff_dir() / f"issue-{key}.drops.log", "a") as fh:
                for d in dropped_ok:
                    fh.write(f"{now_iso()} by={_by(args)} approved_ts={args.approve_drop} dropped={d!r}\n")
        if old:
            try:
                shutil.copy2(target, str(target) + ".prev")
            except OSError:
                pass
        atomic_write(target, stamp(text, key, _by(args)))
    print(f"HANDOFF_WRITTEN {target} dont_items={len(new_norm)} dropped_with_approval={len(dropped_ok)}")
    return 0


SKELETON = """# HANDOFF issue #{key}
updated: {ts} by {by}

## DONE
- none yet

## OPEN
- none yet

## NEXT
- UNSET: the first lane replaces this with its first concrete step

## DECIDED + WHY
- none yet

## DON'T
"""


def cmd_add_dont(args) -> int:
    key = str(args.issue).lstrip("#")
    target = handoff_path(key)
    text_item = " ".join(args.text).strip()
    if not text_item or SECRET_LIKE.search(text_item):
        print("HANDOFF_REFUSED empty_or_secret_like")
        return 2
    with locked(handoff_dir()):
        body = target.read_text() if target.exists() else SKELETON.format(key=key, ts=now_iso(), by=_by(args))
        if norm(text_item) in [norm(i) for i in dont_items(body)]:
            print("HANDOFF_UNCHANGED already present")
            return 0
        lines = body.splitlines()
        start = next((i for i, l in enumerate(lines) if (m := HEAD_RE.match(l)) and canon_head(m.group(1)) == "DON'T"), None)
        if start is None:
            lines += ["", "## DON'T"]
            start = len(lines) - 1
        end = next((i for i in range(start + 1, len(lines)) if HEAD_RE.match(lines[i])), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, f"- {text_item}")
        atomic_write(target, "\n".join(lines).rstrip() + "\n")
    print(f"HANDOFF_DONT_ADDED {target}")
    return 0


CONTRACT = """HANDOFF CONTRACT (Rajiv, D0AMF0XE6TS thread 1795441948.588769, ts 1795443398.969229). The brief above is your GOAL (its ACs and Rajiv's quoted directive); it is unchanged and wins over anything in a handoff.
1. BOOT RESTATE, before ANY tool call or spend: your first output is exactly two lines:
   RESTATE GOAL: <the brief's goal in one sentence, your words>
   RESTATE NEXT: <the first NEXT item of the handoff above, keeping its issue/PR numbers, file paths and shas; with no handoff, your first planned step>
   Then run: python3 -I {self} restate-check --issue {key} --next "<your RESTATE NEXT text>"
   RESTATE_OK or NO_HANDOFF: proceed. RESTATE_MISMATCH: STOP before spending anything more: no edits, no box, no model calls beyond reading. Print the RESULT block (verdict BLOCKED, notes "handoff_restate_mismatch") and end with BLOCKED: handoff_restate_mismatch <one line>. PM reconciles from primary sources.
2. KEEP THE HANDOFF CURRENT: at EVERY push and before you stop, write a draft with the five sections in this order (## DONE, ## OPEN, ## NEXT, ## DECIDED + WHY, ## DON'T) and install it with: python3 -I {self} write --issue {key} --from <draft>. The first NEXT item is ONE concrete step a successor can restate (ids, paths, shas). Keep it short: facts, not a diary; ids and links, not pasted logs.
3. DON'T = what was tried and failed, and why. A DON'T is never deleted or reworded: the write command refuses it. Add one with: python3 -I {self} add-dont --issue {key} "<what not to do and why>". Dropping one needs PM approval, never yours.
4. Your RESULT block (above) gets ONE more key: "handoff": "{path}". A missing handoff is recorded as handoff_status=missing."""


def render_for_injection(text: str) -> str:
    if len(text) <= INJECT_FULL_CHARS:
        return text.rstrip() + "\n"
    pre, sections, _order = parse(text)
    out = list(pre)
    for name in SECTIONS:
        body = "\n".join(sections.get(name, []))
        if name in ("DONE", "DECIDED + WHY") and len(body) > 2500:
            body = "[older lines cut for length; the full file is on disk]\n" + body[-2500:]
        out += ["", f"## {name}", body]
    return "\n".join(out).rstrip() + "\n"


def build_injection(brief: str, role: str, name: str) -> tuple[str, str]:
    """(block, status). status is a short machine line for the dry run."""
    if not eligible(role):
        return "", f"skip reason=type_not_eligible type={role}"
    try:
        text = Path(brief).read_text(errors="replace")
    except OSError:
        return "", "skip reason=brief_unreadable"
    key, issue, pr = resolve_key(text)
    if not key:
        return "", "skip reason=no_issue_ref"
    if issue and pr:
        try:
            write_link(pr, issue)
        except OSError:
            pass
    path = handoff_path(key)
    try:
        body = path.read_text()
    except OSError:
        body = ""
    if body:
        header = (f"=== HANDOFF FOR ISSUE #{key} (file {path}; updated {updated_meta(body)}) ===\n"
                  "A previous lane worked on this issue. Read this before anything else; it is the shared state for every lane on the issue.\n"
                  "--- file ---\n" + render_for_injection(body) + f"=== END HANDOFF #{key} ===\n")
        status = f"present issue={key} file={path} next={'set' if first_next(body) else 'UNSET'}"
    else:
        header = (f"=== HANDOFF FOR ISSUE #{key}: none yet (you are the first lane on this key) ===\n"
                  f"Create {path} with the contract below at your first push.\n=== END HANDOFF #{key} ===\n")
        status = f"absent issue={key} file={path}"
    return "\n\n" + header + "\n" + CONTRACT.format(self=SELF, key=key, path=path) + "\n", status


def cmd_inject(args) -> int:
    try:
        block, status = build_injection(args.brief, args.type, args.name or "")
        if args.dry_run:
            print(f"PROXY_AGENT_HANDOFF {status}")
        if block:
            sys.stdout.write(block)
    except Exception as exc:  # fail open: never block a launch
        log(f"inject_error={type(exc).__name__}")
    return 0


# ---- restate check --------------------------------------------------------------------------------------------------------------------
def tokens(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"[a-z0-9][a-z0-9_./:#@-]*", text.lower()):
        tok = tok.rstrip(".:,;-")
        tok = re.sub(r":\d+(?:-\d+)?$", "", tok)
        if tok:
            out.append(tok)
    return out


def is_ident(tok: str) -> bool:
    return any(c.isdigit() for c in tok) or "/" in tok


def ident_match(want: str, have: set[str]) -> bool:
    base = want.rsplit("/", 1)[-1]
    for t in have:
        if t == want or (len(t) >= 6 and (want.startswith(t) or t.startswith(want))):
            return True
        if "/" in want and t.rsplit("/", 1)[-1] == base:
            return True
    return False


def similarity(expected: str, restated: str) -> tuple[float, list[str], int, int]:
    exp, got = tokens(expected), tokens(restated)
    have = set(got)
    idents = sorted({t for t in exp if is_ident(t)})
    missing = [i for i in idents if not ident_match(i, have)]

    def stem(t: str) -> str:
        return t[:-1] if len(t) > 4 and t.endswith("s") else t

    content = {stem(t) for t in exp if not is_ident(t) and len(t) >= 3 and t not in STOP}
    gcontent = {stem(t) for t in got if not is_ident(t) and len(t) >= 3 and t not in STOP}
    coverage = (len(content & gcontent) / len(content)) if content else 1.0
    return coverage, missing, len(idents) - len(missing), len(idents)


def restate_from_jsonl(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if "RESTATE" not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "assistant":
                    continue
                blocks = (event.get("message") or {}).get("content") or []
                text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
                m = re.search(r"RESTATE NEXT:\s*(.+)", text)
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return ""


def cmd_restate_check(args) -> int:
    key = _key_from(args)
    if not key:
        print("NO_HANDOFF reason=no_issue_ref")
        return 0
    try:
        body = handoff_path(key).read_text()
    except OSError:
        print(f"NO_HANDOFF issue={key}")
        return 0
    expected = first_next(body)
    if not expected:
        print(f"NO_HANDOFF issue={key} reason=next_unset")
        return 0
    if args.jsonl:
        restated = restate_from_jsonl(args.jsonl)
    elif args.restate_file:
        restated = Path(args.restate_file).read_text(errors="replace")
    else:
        restated = args.next or ""
    m = re.search(r"RESTATE NEXT:\s*(.+)", restated)
    restated = m.group(1) if m else restated
    if not restated.strip():
        print(f"RESTATE_MISMATCH issue={key} reason=no_restate expected={expected[:160]!r}")
        return 4
    coverage, missing, hit, total = similarity(expected, restated)
    threshold = float(os.environ.get("PROXY_HANDOFF_RESTATE_MIN", "0.5"))
    id_ok = total == 0 or (hit / total) >= 0.75
    if coverage >= threshold and id_ok:
        print(f"RESTATE_OK issue={key} coverage={coverage:.2f} identifiers={hit}/{total}")
        return 0
    print(f"RESTATE_MISMATCH issue={key} coverage={coverage:.2f} identifiers={hit}/{total} "
          f"missing={','.join(missing) or '-'} expected={expected[:160]!r}")
    return 4


def cmd_link(args) -> int:
    write_link(str(args.pr).lstrip("#"), str(args.issue).lstrip("#"))
    print(f"HANDOFF_LINKED pr={args.pr} issue={args.issue}")
    return 0


def cmd_list(_args) -> int:
    for path in sorted(handoff_dir().glob("issue-*.md")):
        text = path.read_text(errors="replace")
        nxt = first_next(text) or "UNSET"
        print(f"{path.stem.removeprefix('issue-')}\t{updated_meta(text)}\tdont={len(dont_items(text))}\t{nxt[:100]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="proxy-handoff.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        return sp

    for name, fn in (("key", cmd_key), ("path", cmd_path), ("read", cmd_read)):
        sp = add(name, fn)
        sp.add_argument("--issue")
        sp.add_argument("--brief")
    sp = add("validate", cmd_validate)
    sp.add_argument("--issue")
    sp.add_argument("--file")
    sp = add("write", cmd_write)
    sp.add_argument("--issue", required=True)
    sp.add_argument("--from", dest="source")
    sp.add_argument("--by")
    sp.add_argument("--approve-drop", dest="approve_drop")
    sp.add_argument("--drop", action="append")
    sp = add("add-dont", cmd_add_dont)
    sp.add_argument("--issue", required=True)
    sp.add_argument("--by")
    sp.add_argument("text", nargs="+")
    sp = add("inject", cmd_inject)
    sp.add_argument("--brief", required=True)
    sp.add_argument("--type", default="")
    sp.add_argument("--name", default="")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("restate-check", cmd_restate_check)
    sp.add_argument("--issue")
    sp.add_argument("--brief")
    sp.add_argument("--next")
    sp.add_argument("--restate-file", dest="restate_file")
    sp.add_argument("--jsonl")
    sp = add("link", cmd_link)
    sp.add_argument("--pr", required=True)
    sp.add_argument("--issue", required=True)
    add("list", cmd_list)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
