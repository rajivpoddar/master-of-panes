#!/bin/bash
# ~/.claude/skills/slack-dm/scripts/slack-send.sh
#
# Send a Slack message via curl + SLACK_BOT_TOKEN.
# Supports: text, blocks (table blocks), thread replies.
#
# Usage:
#   slack-send.sh [OPTIONS] MESSAGE
#
# Options:
#   -c CHANNEL_ID   Channel or DM ID (default: D0AMF0XE6TS = Rajiv DM)
#   -t THREAD_TS    Reply in thread (timestamp of parent message)
#   -b BLOCKS_JSON  Send Block Kit blocks (JSON string or @file path)
#   -f              Read message from stdin instead of argument
#
# Examples:
#   # Simple text DM to Rajiv
#   slack-send.sh "PR #2303 merged. CI green."
#
#   # Thread reply
#   slack-send.sh -t "1773036028.558669" "Updated — CI is green now."
#
#   # Block Kit table from JSON string
#   slack-send.sh -b '[{"type":"section","text":{"type":"mrkdwn","text":"*Status*"}}]'
#
#   # Block Kit from file
#   slack-send.sh -b @/tmp/blocks.json
#
#   # Read message from stdin (for multiline / pipe)
#   echo "Long message here" | slack-send.sh -f
#
#   # Different channel
#   slack-send.sh -c C0AEY9CEC4D "Message to #channel"

set -euo pipefail

# Self-test mode (no Slack call). Run with: SLACK_SEND_SELFTEST=1 bash slack-send.sh </dev/null
if [ "${SLACK_SEND_SELFTEST:-}" = "1" ]; then
  python3 - <<'PYTEST'
import re, sys

def has_markdown_table(t):
    lines = t.strip().split('\n')
    table_lines = [l for l in lines if re.match(r'^\s*\|.*\|.*\|', l)]
    return len(table_lines) >= 2

def _split_row(line):
    cells = line.split('|')
    if cells and cells[0].strip() == '':
        cells = cells[1:]
    if cells and cells[-1].strip() == '':
        cells = cells[:-1]
    return [c.strip() for c in cells]

def convert_table_to_mrkdwn_rows(t):
    lines = t.strip().split('\n')
    result = []
    in_table = False
    table_lines = []
    headers = []

    def _flush(table_lines, headers, out):
        for row in table_lines:
            parts = []
            for i, cell in enumerate(row):
                if i < len(headers):
                    parts.append(f'*{headers[i]}:* {cell}')
            if parts:
                out.append('• ' + ' | '.join(parts))

    for line in lines:
        is_table_line = bool(re.match(r'^\s*\|[^\n]*\|[^\n]*\|', line))
        is_separator = bool(re.match(r'^\s*\|[-:\s|]+\|', line))
        if is_table_line and not is_separator:
            if not in_table:
                in_table = True
                table_lines = []
                headers = []
            cells = _split_row(line)
            if not headers:
                headers = cells
            else:
                table_lines.append(cells)
        elif is_separator:
            continue
        else:
            if in_table and table_lines and headers:
                _flush(table_lines, headers, result)
                table_lines = []
                headers = []
                in_table = False
            result.append(line)
    if in_table and table_lines and headers:
        _flush(table_lines, headers, result)
    return '\n'.join(result)

def convert_table_to_codeblock(t):
    """Render markdown tables as monospaced ASCII grids inside a Slack code
    block. Slack mrkdwn renders triple-backtick blocks in a fixed-width font;
    box-drawing chars + per-column padding produce a visible table grid.
    Prose around tables is preserved as-is. Cells with markdown formatting
    are rendered as raw chars (Slack does not render mrkdwn inside code blocks).
    """
    lines = t.strip().split('\n')
    result = []
    in_table = False
    headers = []
    rows = []

    def _flush(headers, rows, out):
        if not headers:
            return
        cols = max(len(headers), max((len(r) for r in rows), default=0))
        # Pad headers and rows to cols columns
        headers_padded = headers + [''] * (cols - len(headers))
        rows_padded = [r + [''] * (cols - len(r)) for r in rows]
        widths = [len(headers_padded[i]) for i in range(cols)]
        for r in rows_padded:
            for i in range(cols):
                if len(r[i]) > widths[i]:
                    widths[i] = len(r[i])
        def _border(left, mid, right):
            return left + mid.join('─' * (w + 2) for w in widths) + right
        def _row(cells):
            return '│ ' + ' │ '.join(cells[i].ljust(widths[i]) for i in range(cols)) + ' │'
        out.append('```')
        out.append(_border('┌', '┬', '┐'))
        out.append(_row(headers_padded))
        out.append(_border('├', '┼', '┤'))
        for r in rows_padded:
            out.append(_row(r))
        out.append(_border('└', '┴', '┘'))
        out.append('```')

    for line in lines:
        is_table_line = bool(re.match(r'^\s*\|[^\n]*\|[^\n]*\|', line))
        is_separator = bool(re.match(r'^\s*\|[-:\s|]+\|', line))
        if is_table_line and not is_separator:
            if not in_table:
                in_table = True
                headers = []
                rows = []
            cells = _split_row(line)
            if not headers:
                headers = cells
            else:
                rows.append(cells)
        elif is_separator:
            continue
        else:
            if in_table and headers:
                _flush(headers, rows, result)
                headers = []
                rows = []
                in_table = False
            result.append(line)
    if in_table and headers:
        _flush(headers, rows, result)
    return '\n'.join(result)

def convert_text_to_blocks(t):
    """Convert text with markdown tables into BlockKit blocks list.
    Returns None when no table is present (caller falls back to text path).
    Tables become top-level 'table' blocks (Slack 2024+); surrounding prose
    becomes section blocks with mrkdwn text. Mirrors the runtime impl below
    — keep in sync.

    Cell shape (verified 2026-05-03 via chat.postMessage probe):
        {'type': 'rich_text',
         'elements': [{'type': 'rich_text_section',
                       'elements': [{'type': 'text', 'text': '...',
                                     'style': {'bold': True}}]}]}

    Block shape:
        {'type': 'table', 'rows': [[cell, cell, ...], [cell, ...]]}
    First row is header (bold)."""
    if not has_markdown_table(t):
        return None
    lines = t.split('\n')
    blocks = []
    prose_buf = []
    table_rows = []
    in_table = False

    def _flush_prose():
        if not prose_buf:
            return
        text = '\n'.join(prose_buf).strip('\n')
        if text.strip():
            blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}})
        prose_buf.clear()

    def _cell(s, bold=False):
        text_val = s if s else ' '
        el = {'type': 'text', 'text': text_val}
        if bold:
            el['style'] = {'bold': True}
        return {'type': 'rich_text',
                'elements': [{'type': 'rich_text_section', 'elements': [el]}]}

    def _flush_table():
        if not table_rows:
            return
        n_cols = max(len(r) for r in table_rows)
        padded = [r + [''] * (n_cols - len(r)) for r in table_rows]
        rows = []
        for ri, row in enumerate(padded):
            is_header = (ri == 0)
            rows.append([_cell(c, bold=is_header) for c in row])
        blocks.append({'type': 'table', 'rows': rows})
        table_rows.clear()

    for line in lines:
        is_table_line = bool(re.match(r'^\s*\|[^\n]*\|[^\n]*\|', line))
        is_separator = bool(re.match(r'^\s*\|[-:\s|]+\|', line))
        if is_separator and in_table:
            continue
        if is_table_line and not is_separator:
            if not in_table:
                _flush_prose()
                in_table = True
            table_rows.append(_split_row(line))
        else:
            if in_table:
                _flush_table()
                in_table = False
            prose_buf.append(line)
    if in_table:
        _flush_table()
    else:
        _flush_prose()
    return blocks

failed = 0
def check(label, cond):
    global failed
    if cond:
        print(f'  PASS {label}')
    else:
        print(f'  FAIL {label}')
        failed += 1

# 1. single-cell '| only |' must NOT be detected as a table row
PAT = r'^\s*\|[^\n]*\|[^\n]*\|'
check("BUG-1 single-cell rejected", not re.match(PAT, '| only |'))

# 2. prose with inline pipes "Use | or |" must NOT be detected
check("BUG-1 prose with inline pipes rejected", not re.match(PAT, 'Use | or |'))

# 3. two-cell row '| a | b |' detected
check("two-cell row detected", bool(re.match(PAT, '| a | b |')))

# 4. has_markdown_table with single-col block must NOT trigger conversion
single_col = "| header |\n|---|\n| only |"
check("has_markdown_table rejects single col", not has_markdown_table(single_col))

# 5. interior empty cell preserved
out = convert_table_to_mrkdwn_rows("| A | B | C |\n|---|---|---|\n| a |  | c |")
check("BUG-2 interior empty preserved", '*B:* ' in out and '*C:* c' in out)

# 6. standard 3-col header+sep+row -> exactly 1 bullet
out2 = convert_table_to_mrkdwn_rows("| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |")
check("standard 3-col -> 1 bullet", out2.count('• ') == 1 and '*A:* 1' in out2)

# 7. two-cell row produces correct bullet
out3 = convert_table_to_mrkdwn_rows("| A | B |\n|---|---|\n| a | b |")
check("two-cell bullet", '*A:* a' in out3 and '*B:* b' in out3)

# 8. BlockKit: prose + table + prose -> 3 blocks (section, table, section)
bk1 = convert_text_to_blocks("prose\n\n| H1 | H2 |\n|---|---|\n| a | b |\n\nmore prose")
check(
    "BK prose+table+prose -> 3 blocks (section/table/section)",
    bk1 is not None
    and len(bk1) == 3
    and bk1[0]['type'] == 'section'
    and bk1[1]['type'] == 'table'
    and len(bk1[1]['rows']) == 2
    and bk1[2]['type'] == 'section'
)

# 9. BlockKit: empty interior cell preserved as ' ' placeholder (Slack rejects
#    truly empty rich_text_section; we substitute a single space).
bk2 = convert_text_to_blocks("| H1 | H2 | H3 |\n|---|---|---|\n| a |  | c |")
rows = bk2[0]['rows']
check(
    "BK empty interior cell -> space placeholder",
    bk2 is not None
    and len(rows) == 2
    and len(rows[1]) == 3
    and rows[1][1]['elements'][0]['elements'][0]['text'] == ' '
    and rows[1][2]['elements'][0]['elements'][0]['text'] == 'c'
)

# 10. BlockKit: no-table input returns None (caller falls back)
bk3 = convert_text_to_blocks("just prose\nno table here")
check("BK no-table returns None", bk3 is None)

# TBL-1: Default emits top-level 'type: table' block (NOT rich_text_table).
#   This is the rejection-shape vs working-shape discriminator. Verified working
#   via direct chat.postMessage probe 2026-05-03 ts=1777795366.758379.
tbl1 = convert_text_to_blocks("| Header A | Header B |\n|---|---|\n| r1c1 | r1c2 |")
check(
    "TBL-1 default emits top-level type=table",
    tbl1 is not None
    and len(tbl1) == 1
    and tbl1[0]['type'] == 'table'
    and 'rows' in tbl1[0]
)

# TBL-2: Header row cells carry style.bold=True; data row cells do not.
tbl2 = convert_text_to_blocks("| Header A | Header B |\n|---|---|\n| r1c1 | r1c2 |")
header_cell = tbl2[0]['rows'][0][0]
data_cell = tbl2[0]['rows'][1][0]
header_text_el = header_cell['elements'][0]['elements'][0]
data_text_el = data_cell['elements'][0]['elements'][0]
check(
    "TBL-2 header bold styling, data unstyled",
    header_text_el.get('text') == 'Header A'
    and header_text_el.get('style') == {'bold': True}
    and data_text_el.get('text') == 'r1c1'
    and 'style' not in data_text_el
)

# TBL-3: Cell shape is rich_text -> rich_text_section -> text (verified working
#   shape per chat.postMessage probe). NOT rich_text_table element.
tbl3 = convert_text_to_blocks("| A | B |\n|---|---|\n| 1 | 2 |")
cell = tbl3[0]['rows'][0][0]
check(
    "TBL-3 cell shape rich_text/rich_text_section/text",
    cell['type'] == 'rich_text'
    and cell['elements'][0]['type'] == 'rich_text_section'
    and cell['elements'][0]['elements'][0]['type'] == 'text'
)

# 11. Code-block: 2x2 table -> grid output with header + data + box-drawing borders
cb1 = convert_table_to_codeblock("| H1 | H2 |\n|---|---|\n| a  | b  |")
check(
    "CB 2x2 grid renders box-drawing + cells",
    '```' in cb1
    and '┌' in cb1 and '┬' in cb1 and '┐' in cb1
    and '├' in cb1 and '┼' in cb1 and '┤' in cb1
    and '└' in cb1 and '┴' in cb1 and '┘' in cb1
    and '│ H1 │ H2 │' in cb1
    and '│ a  │ b  │' in cb1
)

# 12. Code-block: 3-col with interior empty cell preserves spaces (not collapsed)
cb2 = convert_table_to_codeblock("| A | B | C |\n|---|---|---|\n| a |  | c |")
# B header width is 1, so empty interior cell becomes "│   │" (1 space pad)
check("CB interior empty cell rendered as spaces",
      '│ a │   │ c │' in cb2)

# 13. Code-block: varying-width cells -> all rows aligned to per-column max
cb3 = convert_table_to_codeblock("| A | Header B | C |\n|---|---|---|\n| short | x | longvalue |")
# Each column padded to max(header, data) width: A->5 (short), Header B->8, C->9 (longvalue)
check("CB varying widths -> aligned columns",
      '│ A     │ Header B │ C         │' in cb3
      and '│ short │ x        │ longvalue │' in cb3)

# 14. Code-block: multiple tables in one message -> two code blocks emitted
cb4 = convert_table_to_codeblock(
    "Table one:\n| A | B |\n|---|---|\n| 1 | 2 |\n\nTable two:\n| X | Y |\n|---|---|\n| 9 | 8 |"
)
check("CB multiple tables -> two code blocks",
      cb4.count('```') == 4
      and '│ A │ B │' in cb4
      and '│ X │ Y │' in cb4)

if failed:
    print(f'SELFTEST: {failed} failure(s)')
    sys.exit(1)
print('SELFTEST: all assertions passed')
PYTEST
  exit $?
fi

# Source bot token — prefer slot-specific token for per-slot identity
# Check current project's .env.local first (slot's own token), then PM's
# Find .env.local — check CWD first, then try git root, then slot paths
SLOT_ENV="${PWD}/.env.local"
if [ ! -f "$SLOT_ENV" ]; then
  GIT_ROOT=$(git rev-parse --show-toplevel || echo "")
  [ -n "$GIT_ROOT" ] && SLOT_ENV="${GIT_ROOT}/.env.local"
fi
PM_ENV="/Users/rajiv/Downloads/projects/heydonna-app/.env.local"

SLACK_BOT_TOKEN=""
# Try slot-specific token first (posts as the slot's own bot identity)
if [ -f "$SLOT_ENV" ]; then
  SLACK_BOT_TOKEN=$(grep '^SLACK_SLOT_BOT_TOKEN=' "$SLOT_ENV" | cut -d= -f2 || true)
fi
# Fall back to general bot token from current project or PM
if [ -z "$SLACK_BOT_TOKEN" ] && [ -f "$SLOT_ENV" ]; then
  SLACK_BOT_TOKEN=$(grep '^SLACK_BOT_TOKEN=' "$SLOT_ENV" | cut -d= -f2 || true)
fi
if [ -z "$SLACK_BOT_TOKEN" ] && [ -f "$PM_ENV" ]; then
  SLACK_BOT_TOKEN=$(grep '^SLACK_BOT_TOKEN=' "$PM_ENV" | cut -d= -f2)
fi

if [ -z "${SLACK_BOT_TOKEN:-}" ]; then
  echo "ERROR: SLACK_BOT_TOKEN not set in $ENV_FILE" >&2
  exit 1
fi

# Defaults — context-aware:
#   PM clone (heydonna-app/) → Rajiv DM (D0AMF0XE6TS)
#   Dev slot (heydonna-app-300N/) → #heydonna-dev (C0ALZJHGE49)
# Rationale: dev-slot bot tokens don't have access to Rajiv's DM (channel_not_found).
# See 21-lessons.md: "Dev slots should post to the active thread in #heydonna-dev"
if [[ "${PWD}" =~ heydonna-app-300[1-6] ]] || [[ "${SLOT_ENV:-}" =~ heydonna-app-300[1-6] ]]; then
  CHANNEL="C0ALZJHGE49"  # #heydonna-dev
  CHANNEL_DEFAULT=true
else
  CHANNEL="D0AMF0XE6TS"  # Rajiv DM (PM only)
  CHANNEL_DEFAULT=true
fi
THREAD_TS=""
BLOCKS=""
FROM_STDIN=false
CHANNEL_EXPLICIT=false

# Parse options
while getopts "c:t:b:f" opt; do
  case $opt in
    c) CHANNEL="$OPTARG"; CHANNEL_EXPLICIT=true ;;
    t) THREAD_TS="$OPTARG" ;;
    b) BLOCKS="$OPTARG" ;;
    f) FROM_STDIN=true ;;
    *) echo "Usage: slack-send.sh [-c channel] [-t thread_ts] [-b blocks] [-f] [message]" >&2; exit 1 ;;
  esac
done
shift $((OPTIND - 1))

# Pre-flight gate: REJECT thread replies that omit -c <CHAN>.
# Rationale (Rajiv directives 2026-05-16 20:21 IST + 2026-05-18 10:43 IST):
#   - Channel-thread reply without -c silently defaults to Rajiv DM → misroute.
#   - DM-thread reply without -c silently defaults to DM top-level (NOT the thread).
# In BOTH cases the caller MUST extract <CHAN> from the slack-bridge header
# (`# slack-channel <CHAN_ID> in thread <THREAD_TS>`) preceding the trigger
# message and pass it explicitly.
# Memos: feedback_slack_send_channel_routing_when_slack_bridge_header_present
#        feedback_slack_send_dm_thread_routing_when_slack_bridge_header_present
if [ -n "$THREAD_TS" ] && [ "$CHANNEL_EXPLICIT" = false ]; then
  echo "ERROR: slack-send.sh -t <TS> requires explicit -c <CHAN> when threading." >&2
  echo "       Default-channel fallback for thread replies is forbidden (channel + DM thread misroute on omit)." >&2
  echo "       Extract <CHAN> from the slack-bridge header line preceding the trigger message:" >&2
  echo "         # slack-channel <CHAN_ID> in thread <THREAD_TS>" >&2
  echo "       Then invoke: slack-send.sh -c <CHAN_ID> -t <THREAD_TS> -f" >&2
  exit 2
fi

# Get message text
if [ "$FROM_STDIN" = true ]; then
  MESSAGE=$(cat)
elif [ $# -gt 0 ]; then
  MESSAGE="$*"
else
  MESSAGE=""
fi

# Some PM-generated thread replies accidentally pipe a standalone `reply`
# marker before the real body. Drop only that complete first line; do not
# rewrite legitimate prose such as "Reply requested" or "reply with details".
if [[ "$MESSAGE" == *$'\n'* ]]; then
  FIRST_LINE="${MESSAGE%%$'\n'*}"
  FIRST_LINE="${FIRST_LINE%$'\r'}"
  if [[ "$FIRST_LINE" =~ ^[[:space:]]*[Rr][Ee][Pp][Ll][Yy][[:space:]]*$ ]]; then
    MESSAGE="${MESSAGE#*$'\n'}"
  fi
fi

# Product-safety pre-send hook. Keep the validator in tracked control-plane
# source; this live sender is symlinked from ~/.claude.
SPLIT_POLICY_GUARD="/Users/rajiv/.claude/skills/slack-message/scripts/block-unsupported-transcript-split.py"
if [ ! -f "$SPLIT_POLICY_GUARD" ]; then
  echo "ERROR: required Slack product-safety hook missing: $SPLIT_POLICY_GUARD" >&2
  exit 24
fi
python3 "$SPLIT_POLICY_GUARD" --message "$MESSAGE" --blocks "$BLOCKS"

# Build JSON payload
if [ -n "$BLOCKS" ]; then
  # Block Kit mode
  # If blocks starts with @, read from file
  if [[ "$BLOCKS" == @* ]]; then
    BLOCKS_FILE="${BLOCKS#@}"
    BLOCKS=$(cat "$BLOCKS_FILE")
  fi

  PAYLOAD=$(python3 -c "
import json, sys

channel = sys.argv[1]
thread_ts = sys.argv[2]
blocks_str = sys.argv[3]
text = sys.argv[4] if len(sys.argv) > 4 else ''

blocks = json.loads(blocks_str)
payload = {
    'channel': channel,
    'blocks': blocks
}

# Always set text field — Slack uses it for notifications AND the bridge
# reads msg.text for @mention/@channel routing.
# If no explicit text given, extract from blocks as fallback.
if not text:
    parts = []
    for b in blocks:
        if b.get('type') == 'section' and 'text' in b:
            parts.append(b['text'].get('text', ''))
        elif b.get('type') == 'header' and 'text' in b:
            parts.append(b['text'].get('text', ''))
    text = ' '.join(parts)
if text:
    payload['text'] = text
if thread_ts:
    payload['thread_ts'] = thread_ts

print(json.dumps(payload))
" "$CHANNEL" "$THREAD_TS" "$BLOCKS" "$MESSAGE")
else
  # Auto-wrap plain text in Block Kit (per buddhi: ALL messages must use Block Kit)
  if [ -z "$MESSAGE" ]; then
    echo "ERROR: No message provided. Use -f for stdin or pass as argument." >&2
    exit 1
  fi

  PAYLOAD=$(python3 -c "
import json, sys, re

channel = sys.argv[1]
thread_ts = sys.argv[2]
text = sys.argv[3]
text = text.encode('raw_unicode_escape').decode('unicode_escape')  # Decode \\n → newline
# Strip escaped quotes that leak from shell quoting
text = text.replace(chr(92) + chr(34), chr(34))

def has_markdown_table(t):
    \"\"\"Detect markdown pipe tables (| col | col | or |---|---|)\"\"\"
    lines = t.strip().split('\n')
    table_lines = [l for l in lines if re.match(r'^\s*\|.*\|.*\|', l)]
    return len(table_lines) >= 2  # At least header + separator or 2 data rows

def _split_row_inline(line):
    cells = line.split('|')
    if cells and cells[0].strip() == '':
        cells = cells[1:]
    if cells and cells[-1].strip() == '':
        cells = cells[:-1]
    return [c.strip() for c in cells]

def convert_text_to_blocks(t):
    \"\"\"Convert text with markdown tables into BlockKit blocks.
    Returns None when no table is present.
    Tables -> native top-level 'table' block (Slack 2024+ block type).
    Prose around tables -> section blocks with mrkdwn text.

    Cell shape (verified via api.slack.com chat.postMessage 2026-05-03):
        {'type': 'rich_text',
         'elements': [{'type': 'rich_text_section',
                       'elements': [{'type': 'text', 'text': '...',
                                     'style': {'bold': True}}]}]}

    Top-level block shape:
        {'type': 'table', 'rows': [[cell, cell, ...], [cell, ...]]}
    First row is treated as header by convention (we mark it bold).\"\"\"
    if not has_markdown_table(t):
        return None
    lines = t.split('\n')
    blocks = []
    prose_buf = []
    table_rows = []
    in_table = False

    def _flush_prose():
        if not prose_buf:
            return
        text_chunk = '\n'.join(prose_buf).strip('\n')
        if text_chunk.strip():
            blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': text_chunk}})
        prose_buf.clear()

    def _cell(s, bold=False):
        # Slack rejects empty rich_text_section elements — use a non-breaking
        # space as the placeholder so the cell still renders.
        text_val = s if s else ' '
        el = {'type': 'text', 'text': text_val}
        if bold:
            el['style'] = {'bold': True}
        return {'type': 'rich_text',
                'elements': [{'type': 'rich_text_section', 'elements': [el]}]}

    def _flush_table():
        if not table_rows:
            return
        n_cols = max(len(r) for r in table_rows)
        # Pad rows so every row has the same column count (table block requires
        # rectangular grid).
        padded = [r + [''] * (n_cols - len(r)) for r in table_rows]
        rows = []
        for ri, row in enumerate(padded):
            is_header = (ri == 0)
            rows.append([_cell(c, bold=is_header) for c in row])
        blocks.append({'type': 'table', 'rows': rows})
        table_rows.clear()

    for line in lines:
        is_table_line = bool(re.match(r'^\s*\|[^\n]*\|[^\n]*\|', line))
        is_separator = bool(re.match(r'^\s*\|[-:\s|]+\|', line))
        if is_separator and in_table:
            continue
        if is_table_line and not is_separator:
            if not in_table:
                _flush_prose()
                in_table = True
            table_rows.append(_split_row_inline(line))
        else:
            if in_table:
                _flush_table()
                in_table = False
            prose_buf.append(line)
    if in_table:
        _flush_table()
    else:
        _flush_prose()
    return blocks

def convert_table_to_mrkdwn_rows(t):
    \"\"\"Convert markdown tables to bold-header + bullet-row format for Slack.
    Slack mrkdwn does NOT support pipe tables. This renders each row as a
    bullet line with bold column headers inline — much more readable than
    code blocks on mobile.\"\"\"
    lines = t.strip().split('\n')
    result = []
    in_table = False
    table_lines = []
    headers = []

    def _split_row(line):
        # Split on '|', then strip ONLY the boundary artifacts (leading/trailing
        # empty cells produced by the framing pipes in '| a | b |').
        # Interior empty cells (e.g. '| a |  | c |') are preserved as ''.
        cells = line.split('|')
        if cells and cells[0].strip() == '':
            cells = cells[1:]
        if cells and cells[-1].strip() == '':
            cells = cells[:-1]
        return [c.strip() for c in cells]

    def _flush(table_lines, headers, out):
        for row in table_lines:
            parts = []
            for i, cell in enumerate(row):
                if i < len(headers):
                    # Preserve interior empty cells — render empty string,
                    # not skip — so column alignment with headers stays correct.
                    parts.append(f'*{headers[i]}:* {cell}')
            if parts:
                out.append('• ' + ' | '.join(parts))

    for line in lines:
        # Row detection: require AT LEAST 2 pipe-separated cells (≥3 total '|'
        # chars on the line). This rejects single-cell lines like '| only |'
        # which the previous '^\s*\|.*\|' pattern incorrectly matched.
        is_table_line = bool(re.match(r'^\s*\|[^\n]*\|[^\n]*\|', line))
        is_separator = bool(re.match(r'^\s*\|[-:\s|]+\|', line))

        if is_table_line and not is_separator:
            if not in_table:
                in_table = True
                table_lines = []
                headers = []
            cells = _split_row(line)
            if not headers:
                headers = cells
            else:
                table_lines.append(cells)
        elif is_separator:
            continue
        else:
            if in_table and table_lines and headers:
                _flush(table_lines, headers, result)
                table_lines = []
                headers = []
                in_table = False
            result.append(line)

    # Flush any remaining table
    if in_table and table_lines and headers:
        _flush(table_lines, headers, result)

    return '\n'.join(result)

def convert_table_to_codeblock(t):
    \"\"\"Render markdown tables as monospaced ASCII grids inside a Slack code
    block. Slack mrkdwn renders triple-backtick blocks in a fixed-width font;
    box-drawing chars + per-column padding produce a visible table grid.\"\"\"
    lines = t.strip().split('\n')
    result = []
    in_table = False
    headers = []
    rows = []

    def _split_row_cb(line):
        cells = line.split('|')
        if cells and cells[0].strip() == '':
            cells = cells[1:]
        if cells and cells[-1].strip() == '':
            cells = cells[:-1]
        return [c.strip() for c in cells]

    def _flush(headers, rows, out):
        if not headers:
            return
        cols = max(len(headers), max((len(r) for r in rows), default=0))
        headers_padded = headers + [''] * (cols - len(headers))
        rows_padded = [r + [''] * (cols - len(r)) for r in rows]
        widths = [len(headers_padded[i]) for i in range(cols)]
        for r in rows_padded:
            for i in range(cols):
                if len(r[i]) > widths[i]:
                    widths[i] = len(r[i])
        def _border(left, mid, right):
            return left + mid.join('─' * (w + 2) for w in widths) + right
        def _row(cells):
            return '│ ' + ' │ '.join(cells[i].ljust(widths[i]) for i in range(cols)) + ' │'
        fence = chr(96) * 3
        out.append(fence)
        out.append(_border('┌', '┬', '┐'))
        out.append(_row(headers_padded))
        out.append(_border('├', '┼', '┤'))
        for r in rows_padded:
            out.append(_row(r))
        out.append(_border('└', '┴', '┘'))
        out.append(fence)

    for line in lines:
        is_table_line = bool(re.match(r'^\s*\|[^\n]*\|[^\n]*\|', line))
        is_separator = bool(re.match(r'^\s*\|[-:\s|]+\|', line))
        if is_table_line and not is_separator:
            if not in_table:
                in_table = True
                headers = []
                rows = []
            cells = _split_row_cb(line)
            if not headers:
                headers = cells
            else:
                rows.append(cells)
        elif is_separator:
            continue
        else:
            if in_table and headers:
                _flush(headers, rows, result)
                headers = []
                rows = []
                in_table = False
            result.append(line)
    if in_table and headers:
        _flush(headers, rows, result)
    return '\n'.join(result)

# Auto-convert standard markdown to Slack mrkdwn (applies to BOTH table-cell
# prose-block sections AND the no-table fallback path). Run BEFORE block split
# so prose surrounding tables also gets converted.
import re
text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)  # **bold** → *bold*
text = re.sub(r'__(.+?)__', r'_\1_', text)        # __italic__ → _italic_
text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)  # ### header → *header*

# Strip shell-escape artifacts: heredocs with defensive backslash-before-backtick
# escaping leak literal sequences that render as backslash-backtick in Slack.
text = text.replace(chr(92) + chr(96), chr(96))

# Drop language tags on fenced code blocks (Slack mrkdwn ignores them).
text = re.sub(r'^' + chr(96)*3 + r'[a-zA-Z0-9_+-]+\s*$', chr(96)*3, text, flags=re.MULTILINE)

# Table rendering paths (priority order — Rajiv directive 2026-05-03:
# 'i want the tables as slack blockkit tables, not ascii tables'):
#   default -> Slack BlockKit native top-level 'table' block. Verified working
#       via direct chat.postMessage probe 2026-05-03 ts=1777795366.758379.
#       NOT to be confused with 'rich_text_table' (a different element that
#       returns 'unsupported type' from this workspace's bot token).
#   SLACK_TABLE_BULLETS=1 -> bold-header bullet rows (legacy mrkdwn fallback,
#       used for clients that don't render the table block).
#   SLACK_TABLE_CODEBLOCK=1 -> monospaced ASCII grid inside a code block
#       (legacy fallback for screenshot/log scraping).
import os as _os
blocks = None
if has_markdown_table(text):
    if _os.environ.get('SLACK_TABLE_BULLETS') == '1':
        text = convert_table_to_mrkdwn_rows(text)
    elif _os.environ.get('SLACK_TABLE_CODEBLOCK') == '1':
        text = convert_table_to_codeblock(text)
    else:
        # DEFAULT: real BlockKit table block. convert_text_to_blocks splits
        # the input into prose-section + table + prose-section blocks.
        blocks = convert_text_to_blocks(text)

if blocks is None:
    # No table — single mrkdwn section path (split if >3000 chars)
    if len(text) <= 3000:
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]
    else:
        chunks = []
        current = ''
        for line in text.split('\n'):
            if len(current) + len(line) + 1 > 2900:
                chunks.append(current)
                current = line
            else:
                current = current + '\n' + line if current else line
        if current:
            chunks.append(current)
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': chunk}} for chunk in chunks]

# Fallback text for notifications: strip table pipes for readability.
fallback_text = text[:3000]

payload = {
    'channel': channel,
    'blocks': blocks,
    'text': fallback_text,
}
if thread_ts:
    payload['thread_ts'] = thread_ts

print(json.dumps(payload))
" "$CHANNEL" "$THREAD_TS" "$MESSAGE")
fi

# Send
# Pre-send mention-ID guard (2026-08-12, Rajiv: "wrong id for cto again"): the
# canonical Slack IDs are Rajiv <@UEQTTB97A> and CTO <@U0BNFGX2UAX>. A message
# that mentions one of the near-miss typo IDs (U0BNFGX2UAU, U0BNFGX2UAX vs UAU)
# would silently drop the 8e notification. Fail closed before the POST.
if printf '%s' "$PAYLOAD" | grep -qE 'U0BNFGX2UAU|U0BNFGX2UAT|U0BNFGX2UAY|UEQTTB97B|UEQTTB97C'; then
  echo "ERROR: payload contains a near-miss Slack mention ID (U0BNFGX2UAU-class typo). Canonical: CTO=<@U0BNFGX2UAX> Rajiv=<@UEQTTB97A>. Refusing to send." >&2
  exit 1
fi
RESULT=$(curl -s -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")


# If message contains @channel/@here or slot @mentions, also route via MoP
# (Slack Bolt silently drops self-messages, so the bridge never sees them)
python3 -c "
import json, sys, urllib.request

payload_str = sys.argv[1]
payload = json.loads(payload_str)
text = payload.get('text', '')

# Check for routable content
import re
has_routing = bool(re.search(r'<!channel>|<!here>|<@U0(AMETSAHC0|ALE8Z8X2P|AMEUQ8DR6|AMEUZPQ5N)>', text))
if has_routing:
    try:
        route_payload = json.dumps({'text': text, 'formatted': text}).encode()
        req = urllib.request.Request(
            'http://localhost:3100/api/slack-route',
            data=route_payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read())
        targets = result.get('targets', [])
        print(f'MoP-routed to {len(targets)} panes', file=sys.stderr)
    except Exception as e:
        print(f'MoP routing failed: {e}', file=sys.stderr)
" "$PAYLOAD" || true

# Parse result
python3 -c "
import json, sys
r = json.load(sys.stdin)
if r.get('ok'):
    ts = r.get('ts', '')
    channel = r.get('channel', '')
    print(f'OK ts={ts} channel={channel}')
else:
    print(f'ERROR: {r.get(\"error\", \"unknown\")}')
    sys.exit(1)
" <<< "$RESULT"
