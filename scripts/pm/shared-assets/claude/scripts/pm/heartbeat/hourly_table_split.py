"""Decide which hourly pipe tables stay in the top-level Slack post.

Rajiv C0ALZJHGE49 thread 1795488293.729669, ts 1795488682.501089: the Burn table,
the OpenCode Go table and the proxy Lane | Model | Liveness table belong in the
main hourly report, not in thread replies. The first table (pace) already stayed
in the parent. Any other later table is still split out, because Slack allows
one native table block per message and slack-send.sh renders every later table
as plain pipe text.

Slack chat.postMessage rejects a text field over 40,000 characters
(https://api.slack.com/methods/chat.postMessage, msg_too_long). This module
never truncates: the caller reports the length and does not post.
"""
import re
import sys

# Slack chat.postMessage `text` maximum. Do not shorten the parent to fit.
SLACK_TEXT_LIMIT = 40000
MAX_BODY_ROWS = 99  # a Slack table block holds 100 rows, header included

ROW = re.compile(r"^\s*\|[^\n]*\|[^\n]*\|")
SEP = re.compile(r"^\s*\|[-:\s|]+\|")


def _cells(header_line):
    raw = (header_line or "").strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [cell.strip() for cell in raw.split("|")]


def keep_in_parent(intro_line, header_line):
    """True for the three tables Rajiv moved into the top-level hourly post."""
    intro = (intro_line or "").strip()
    if intro.startswith("*Burn:*") or intro.startswith("*OpenCode Go:*"):
        return True
    cells = _cells(header_line)
    return (
        cells[:3] == ["Pool / account", "State", "Weekly used"]
        or cells[:3] == ["Item", "Live now", "Registered last 1h"]
        or cells[:3] == ["Lane", "Model", "Liveness"]
    )


def split_hourly_tables(text):
    """Return parent text, reply bodies, and the parent length when it is over the Slack limit.

    parent is the untruncated text that would be posted (the whole report when nothing
    is split out). too_long is that length when it exceeds SLACK_TEXT_LIMIT, else None.
    """
    lines = text.split("\n")
    runs, i = [], 0
    while i < len(lines):
        if ROW.match(lines[i]):
            j = i
            while j < len(lines) and ROW.match(lines[j]):
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    drop, replies = set(), []
    for start, end in runs[1:]:  # the first table stays in the parent
        run = lines[start:end]
        body = [line for line in run[1:] if not SEP.match(line)]
        if not body:
            continue  # header only: nothing to render natively, leave it in the parent
        rule = [run[1]] if SEP.match(run[1]) else []
        first = start
        intro = []
        intro_line = ""
        if start >= 2 and lines[start - 1].strip() and not lines[start - 2].strip():
            intro, first = [lines[start - 1]], start - 1
            intro_line = lines[start - 1]
        if keep_in_parent(intro_line, run[0]):
            continue
        for page in range(0, len(body), MAX_BODY_ROWS):
            replies.append(
                "\n".join((intro if page == 0 else []) + [run[0]] + rule + body[page:page + MAX_BODY_ROWS]) + "\n"
            )
        drop.update(range(first, end))
    if replies:
        parent_lines, dropped = [], False
        for idx, line in enumerate(lines):
            if idx in drop:
                dropped = True
                continue
            if dropped and not line.strip() and parent_lines and not parent_lines[-1].strip():
                dropped = False
                continue
            dropped = False
            parent_lines.append(line)
        parent = "\n".join(parent_lines)
    else:
        parent = text
    too_long = len(parent) if len(parent) > SLACK_TEXT_LIMIT else None
    return {"parent": parent, "replies": replies, "too_long": too_long}


def main(argv):
    if len(argv) != 4:
        print("usage: hourly_table_split.py CLAIM SRC OUT", file=sys.stderr)
        return 1
    claim, src, out = argv[1:4]
    text = open(src, encoding="utf-8").read()
    result = split_hourly_tables(text)
    if result["too_long"] is not None:
        # Length only. No parent file and no reply files, so nothing can be posted shortened.
        print(result["too_long"])
        return 2
    if result["replies"]:
        for n, reply in enumerate(result["replies"], 1):
            open("%s/table-reply-%d.txt" % (claim, n), "w", encoding="utf-8").write(reply)
        open(out, "w", encoding="utf-8").write(result["parent"])
    print(len(result["replies"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
