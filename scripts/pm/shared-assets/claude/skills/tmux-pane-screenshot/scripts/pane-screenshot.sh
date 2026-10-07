#!/bin/bash
# Take a visual screenshot of a tmux pane via ttyd + Playwright
# Usage: pane-screenshot.sh <SLOT> [SLACK_THREAD_TS]

set -euo pipefail

SLOT="${1:?Usage: pane-screenshot.sh <SLOT> [SLACK_THREAD_TS]}"
THREAD_TS="${2:-}"
PANE=$("$HOME/.claude/skills/tmux-slot-command/scripts/slot-pane.sh" "$SLOT") || { echo "ERROR: slot $SLOT pane identity unknown" >&2; exit 2; }
PORT=7682
OUTPUT="/tmp/pane-${SLOT}-screenshot.png"

# 1. Zoom the target pane
tmux resize-pane -t "$PANE" -Z 2>/dev/null || true
sleep 0.5

# 2. Start ttyd temporarily
ttyd -p "$PORT" -W tmux attach-session -t 0 &
TTYD_PID=$!
sleep 2

# 3. Screenshot via Playwright
node -e "
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  await page.goto('http://localhost:${PORT}', { waitUntil: 'networkidle' });
  await new Promise(r => setTimeout(r, 3000));
  await page.screenshot({ path: '${OUTPUT}' });
  await browser.close();
})();
" 2>/dev/null

# 4. Unzoom (restore layout)
tmux resize-pane -t "$PANE" -Z 2>/dev/null || true

# 5. Stop ttyd
kill "$TTYD_PID" 2>/dev/null || true
wait "$TTYD_PID" 2>/dev/null || true

echo "Screenshot saved: ${OUTPUT}"

# 6. Upload to Slack if thread_ts provided
if [ -n "$THREAD_TS" ]; then
  SLACK_TOKEN=$(grep '^SLACK_BOT_TOKEN=' /Users/rajiv/Downloads/projects/heydonna-app/.env.local | cut -d= -f2)
  FILENAME="slot-${SLOT}-screenshot.png"
  FILESIZE=$(wc -c < "$OUTPUT" | tr -d ' ')
  CHANNEL="C0ALZJHGE49"

  RESPONSE=$(curl -s -X POST "https://slack.com/api/files.getUploadURLExternal" \
    -H "Authorization: Bearer ${SLACK_TOKEN}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "filename=${FILENAME}&length=${FILESIZE}")

  UPLOAD_URL=$(echo "$RESPONSE" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r.get('upload_url',''))")
  FILE_ID=$(echo "$RESPONSE" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r.get('file_id',''))")

  curl -s -X POST "$UPLOAD_URL" -F "file=@${OUTPUT}" > /dev/null

  curl -s -X POST "https://slack.com/api/files.completeUploadExternal" \
    -H "Authorization: Bearer ${SLACK_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"files\":[{\"id\":\"${FILE_ID}\",\"title\":\"${FILENAME}\"}],\"channel_id\":\"${CHANNEL}\",\"thread_ts\":\"${THREAD_TS}\",\"initial_comment\":\"Slot ${SLOT} pane screenshot\"}" > /dev/null

  echo "Uploaded to Slack"
fi
