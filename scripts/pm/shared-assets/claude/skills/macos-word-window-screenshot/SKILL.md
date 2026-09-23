---
name: macos-word-window-screenshot
description: Capture a specific open Microsoft Word document window on macOS as a PNG, including Word windows on another display. Use for DOCX visual QA screenshots when whole-screen capture shows the wrong app or a terminal Screen Recording prompt needs diagnosis.
---

# Word Window Screenshot

Capture the named Word window, not the primary display. This is a read-only screenshot tool: do not edit, save, close, restore, or export the document. A successful screenshot is not approval of the document's layout.

## Capture

1. Open the intended DOCX in Microsoft Word. If Word requests access to a temporary folder, use an authorized copy in Downloads; do not grant broad filesystem access or alter the original.
2. Run this from the terminal process whose Screen Recording grant is being tested (normally Alacritty). Use a distinctive substring of the Word window title and a **new** output filename:

```bash
/usr/bin/swift /Users/rajiv/.claude/skills/macos-word-window-screenshot/scripts/capture-word-window.swift \
  'ny-ack-8127-export' /tmp/ny-ack-8127-word-check.png
```

The helper requires exactly one visible, titled Microsoft Word window matching the substring. It checks the terminal's Screen Recording grant, captures that window by CoreGraphics window ID, and verifies a nonempty decodable PNG. It refuses to overwrite an existing image. Word may be on a display with negative screen coordinates; no bounds calculation is needed.

3. Inspect the PNG visually. Confirm it shows the requested Word document and page, without a permission dialog or loading state. For line-number QA, inspect actual text-to-gutter alignment; file creation alone proves only capture access.

## Failure Boundaries

- No or multiple matching windows: bring the intended document forward or use a more specific title. Never silently choose a different document.
- Screen Recording denied: stop and report the requesting terminal app. Do not run `tccutil`, toggle privacy settings, or relaunch apps without user direction. An enabled toggle can belong to an older app code signature.
- Screenshot succeeds but shows the wrong page: navigate Word only as the user requested, then capture to a new path. Do not treat a screenshot of another display or an embedded image of an old permission prompt as a live permission failure.
- Never upload a legal-document screenshot to Slack or another service unless that delivery is requested and authorized.
