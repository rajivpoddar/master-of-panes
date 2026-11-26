#!/usr/bin/env python3
"""Render CLI JSONL privately; preserve the final result for the stop relay."""
import json
import os
import re
import sys
import time


def redact(text):
    # Redact before flattening/truncating, including credentials inherited by CLI.
    for key, value in os.environ.items():
        if len(value) >= 6 and re.search(r'token|secret|password|api.?key', key, re.I):
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'\b[A-Z][A-Z0-9_]*\s*=\s*(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s;]+)', '[ENV REDACTED]', text)
    text = re.sub(r'(?i)(bearer\s+)[\w.~/+\-=]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:[\w-]*(?:token|secret|password|api[_-]?key)[\w-]*)[\"\']?\s*[:=]\s*)[\"\']?[^\s,;\"\']+', r'\1[REDACTED]', text)
    text = re.sub(r'\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]+', '[REDACTED]', text)
    return ''.join(c if c == '\n' or c >= ' ' and c != '\x7f' else ' ' for c in text)


try:
    START = int(os.environ.get('PA_START') or 0) or time.time()
except ValueError:
    START = time.time()
COMBINED_PATH = os.environ.get('PROXY_AGENT_COMBINED_LOG', '/tmp/proxy-agent/all.log')
COMBINED_TAG = '%s %s' % (os.environ.get('PROXY_AGENT_NAME') or os.environ.get('PROXY_AGENT_RUN_NAME') or 'px-?',
                          os.environ.get('PROXY_AGENT_NOTICE_MODEL') or os.environ.get('PROXY_AGENT_STOP_MODEL') or '?')


def feed(label, text):
    # Local wall-clock timestamp per line (Rajiv 2026-11-08 ts 1794118703.418739).
    # The console-style renderer (11-14) was reverted: Rajiv D0AMF0XE6TS ts 1794636784.042129
    # "the one with the timestmaps is much more readable".
    stamp = time.strftime('%H:%M:%S')
    # Running time since launch (Rajiv D0AMF0XE6TS ts 1794636940.815249); PA_START is exported by
    # proxy-agent.sh run.sh and survives auto-continue; falls back to this renderer's start.
    secs = max(0, int(time.time() - START))
    run = f'+{secs // 3600}h{secs % 3600 // 60:02d}m' if secs >= 3600 else f'+{secs // 60}m{secs % 60:02d}s'
    # Proxy name + model on every line (Rajiv D0AMF0XE6TS ts 1794636833.872949).
    line = f'{stamp} {run} {COMBINED_TAG} [{label}] ' + ' '.join(redact(str(text)).split())[:400]
    print(line, flush=True)
    # Combined log for all proxies (Rajiv ts 1794635703.245649): the identical line.
    try:
        os.makedirs(os.path.dirname(COMBINED_PATH) or '.', exist_ok=True)
        fd = os.open(COMBINED_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, (line + '\n').encode('utf-8', 'replace'))
        os.close(fd)
    except OSError:
        pass


def main():
    out, raw = sys.argv[1:]
    seen_result = False
    # Raw events may contain sensitive tool data: owner-only, never echoed.
    with open(os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as events, open(out, 'w') as result:
        os.chmod(raw, 0o600)
        for line in sys.stdin:
            events.write(line)
            events.flush()
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                feed('error', 'UNAVAILABLE reason=invalid_json_event')
                continue
            kind = event.get('type')
            if kind == 'assistant':
                for block in event.get('message', {}).get('content', []):
                    if block.get('type') == 'text':
                        feed('assistant', block.get('text', ''))
                    elif block.get('type') == 'tool_use':
                        args = block.get('input') or {}
                        detail = args.get('description') or args.get('command') or args.get('file_path') or ''
                        feed('tool', str(block.get('name', 'unknown')) + ' ' + str(detail))
            elif kind == 'user':
                for block in event.get('message', {}).get('content', []):
                    if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('is_error'):
                        feed('tool error', block.get('content', 'tool failed'))
            elif kind == 'result':
                text = event.get('result', '')
                result.seek(0)
                result.truncate()
                result.write(text)
                if text and not text.endswith('\n'):
                    result.write('\n')
                result.flush()
                seen_result = True
                feed('error' if event.get('is_error') else 'result', text or event.get('errors', ''))
            elif kind == 'error':
                feed('error', event.get('error', event.get('message', 'CLI error')))
    if not seen_result:
        feed('error', 'UNAVAILABLE reason=missing_result_event')
        return 125
    return 0


if __name__ == '__main__':
    sys.exit(main())
