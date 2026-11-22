#!/usr/bin/env python3
"""QA storage selection: the base env (CI parity), i.e. the ONE shared HeyDonna R2 key and the heydonna-uploads app-data bucket.

Rule 65 (Rajiv D0AMF0XE6TS thread 1795294139.951329, ts 1795316377.951089 and ts 1795316993.447349): every environment uses the same R2
account, bucket and key; there is no QA-only bucket, key, per-session credential or scoped mode. Values never appear in diagnostics
or command arguments.
"""
import argparse
import os
from pathlib import Path
import re
import shlex

LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')


def parse(text):
    result = {}
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            if line.strip() and not line.lstrip().startswith('#'):
                raise ValueError('QA_R2_AMBIGUOUS_ENV')
            continue
        try:
            parts = shlex.split(m[2], comments=True)
        except ValueError:
            raise ValueError('QA_R2_AMBIGUOUS_ENV') from None
        if len(parts) > 1:
            raise ValueError('QA_R2_AMBIGUOUS_ENV')
        value = parts[0] if parts else ''
        if m[1] in result and result[m[1]] != value:
            raise ValueError('QA_R2_DUPLICATE_ENV')
        result[m[1]] = value
    return result


def selected(original):
    """Base (CI-parity) storage: the env as given, with CI's browser origin.

    Rajiv C0ALZJHGE49 thread 1794295319.738879, ts 1794372014.378099:
    "Qa also has to use heydonna uploads r2 bucket like ci does."
    Returns (rendered env text, the R2_* values the disposable deployment must carry).
    """
    # Uploads CORS admits CI's exact localhost:3099 origin, not proxy port 3009.
    # Remote boxes have isolated networking; the checkout's host port is irrelevant.
    parsed = parse(original)
    parsed.update(PORT='3099', BASE_URL='http://localhost:3099',
                  NEXT_PUBLIC_BASE_URL='http://localhost:3099')
    rendered = ''.join(k + '=' + shlex.quote(v) + '\n' for k, v in parsed.items())
    values = {k: v for k, v in parse(rendered).items() if k.startswith('R2_')}
    return rendered, values


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    rendered, _ = selected(Path(a.input).read_text())
    fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(rendered)
    print('QA_R2_GUARD=PASS mode=base-env (CI parity, the one shared R2 key, rule 65)')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('QA_R2_REFUSED: environment invalid (details withheld)')
