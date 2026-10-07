#!/bin/bash
# Record a tmux pane as an MP4 video using capture-pane → aha → Playwright → ffmpeg
# Usage: record-pane.sh <SLOT> [DURATION_S] [WIDTH] [HEIGHT] [SLACK_THREAD_TS]
#
# Pipeline: tmux capture-pane -e → aha (ANSI→HTML) → Playwright screenshot → ffmpeg MP4
# Captures at 0.5fps (1 frame per 2s). 30 frames = 60s.
#
# Rajiv directive 2026-03-19: "convert the pane video to a proper shell script and a skill"

set -euo pipefail

SLOT="${1:?Usage: record-pane.sh <SLOT> [DURATION_S] [WIDTH] [HEIGHT] [SLACK_THREAD_TS]}"
DURATION="${2:-60}"
WIDTH="${3:-1170}"
HEIGHT="${4:-940}"
THREAD_TS="${5:-}"
PANE=$("$HOME/.claude/skills/tmux-slot-command/scripts/slot-pane.sh" "$SLOT") || { echo "ERROR: slot $SLOT pane identity unknown" >&2; exit 2; }
FRAMES=$((DURATION / 2))
CHANNEL="C0ALZJHGE49"
OUTPUT="/tmp/pane-slot${SLOT}-recording.mp4"
WORKDIR="/tmp/pane-record-${SLOT}-$$"

echo "Recording slot ${SLOT} for ${DURATION}s (${FRAMES} frames at 2s intervals)..."
mkdir -p "$WORKDIR"

# Capture frames
for i in $(seq 1 "$FRAMES"); do
  IDX=$(printf '%03d' "$i")

  # Capture pane with ANSI escapes → convert to HTML
  tmux capture-pane -t "$PANE" -p -e | aha --no-header > "$WORKDIR/raw_${IDX}.html" 2>/dev/null

  # Wrap in styled HTML (dark terminal theme, bottom-anchored)
  cat > "$WORKDIR/styled_${IDX}.html" << HTMLEOF
<html><head><style>
body { background: #1e1e2e; padding: 0; margin: 0; overflow: hidden; }
pre { font-family: 'Menlo', 'Courier New', monospace; font-size: 13px; line-height: 1.4; color: #cdd6f4; white-space: pre-wrap; word-wrap: break-word; margin: 0; padding: 10px 10px 10px 25px; position: absolute; bottom: 10px; left: 0; right: 0; }
</style></head><body><pre>$(cat "$WORKDIR/raw_${IDX}.html")</pre></body></html>
HTMLEOF

  # Screenshot via Playwright headless
  node -e "
    const { chromium } = require('playwright');
    (async () => {
      const b = await chromium.launch({ headless: true });
      const p = await b.newPage();
      await p.setViewportSize({ width: ${WIDTH}, height: ${HEIGHT} });
      await p.goto('file://${WORKDIR}/styled_${IDX}.html');
      await p.screenshot({ path: '${WORKDIR}/frame_${IDX}.png' });
      await b.close();
    })();
  " 2>/dev/null

  sleep 2
done

echo "Captured ${FRAMES} frames. Encoding MP4..."

# Combine into MP4
ffmpeg -framerate 0.5 -i "$WORKDIR/frame_%03d.png" \
  -c:v libx264 -pix_fmt yuv420p \
  -vf "scale=${WIDTH}:${HEIGHT}" \
  "$OUTPUT" -y 2>/dev/null

# Clean up work dir
rm -rf "$WORKDIR"

if [ ! -f "$OUTPUT" ]; then
  echo "ERROR: Recording failed — no output file"
  exit 1
fi

SIZE=$(wc -c < "$OUTPUT" | tr -d ' ')
echo "Recording complete: ${OUTPUT} (${SIZE} bytes, ${DURATION}s)"

# Upload to Slack if thread_ts provided
if [ -n "$THREAD_TS" ]; then
  source /Users/rajiv/Downloads/projects/heydonna-app/.env.local 2>/dev/null
  FILENAME="slot-${SLOT}-recording-$(date +%H%M).mp4"
  FILESIZE=$(wc -c < "$OUTPUT" | tr -d ' ')

  RESPONSE=$(curl -s -X POST "https://slack.com/api/files.getUploadURLExternal" \
    -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "filename=${FILENAME}&length=${FILESIZE}")

  UPLOAD_URL=$(echo "$RESPONSE" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r.get('upload_url',''))")
  FILE_ID=$(echo "$RESPONSE" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r.get('file_id',''))")

  if [ -n "$UPLOAD_URL" ]; then
    curl -s -X POST "$UPLOAD_URL" -F "file=@${OUTPUT}" > /dev/null

    curl -s -X POST "https://slack.com/api/files.completeUploadExternal" \
      -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{\"files\":[{\"id\":\"${FILE_ID}\",\"title\":\"${FILENAME}\"}],\"channel_id\":\"${CHANNEL}\",\"thread_ts\":\"${THREAD_TS}\",\"initial_comment\":\"Slot ${SLOT} — ${DURATION}s recording\"}" > /dev/null

    echo "Uploaded to Slack thread ${THREAD_TS}"
  else
    echo "Slack upload failed: ${RESPONSE}"
  fi
fi
