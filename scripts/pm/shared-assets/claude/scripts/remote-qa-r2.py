#!/usr/bin/env python3
"""QA storage selection: base env (CI parity) by default; QA_R2_SCOPED=1 opts into scoped.

Values never appear in diagnostics or command arguments.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shlex
import stat

PRIVATE = Path.home() / '.heydonna/secrets/qa-r2.env'
# Production identifiers only, not signing secrets. Compare fingerprints, never log values.
PROD_PAIR = hashlib.sha256(b'1b792fbd07071476fe8ef472c8def16d/heydonna-uploads').hexdigest()
KEYS = ('R2_ACCOUNT_ID', 'R2_BUCKET_NAME', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_ENDPOINT_URL')
STORAGE = re.compile(r'(?:^|_)(?:R2|AWS|S3)_')


def guard_environment(values, source=None):
    """Scan ALL values, not just the canonical signing identity; never log values."""
    from urllib.parse import unquote, urlsplit
    forbidden = {'heydonna-uploads'}
    for name, value in (source or {}).items():
        if STORAGE.search(name) and any(tag in name for tag in ('PUBLIC', 'DOMAIN', 'BASE_URL')):
            host = urlsplit(value if '://' in value else 'https://' + value).hostname
            # R2's account endpoint is shared by buckets; account+QA bucket is
            # valid. Public/custom hosts, unlike account endpoints, bypass signing.
            if host and not host.endswith('.r2.cloudflarestorage.com'):
                forbidden.add(host.lower())
    for value in values.values():
        decoded = value.lower()
        for _ in range(3):
            decoded = unquote(decoded)
        if any(token in decoded for token in forbidden) or re.search(r'pub-[a-z0-9-]+\.r2\.dev', decoded):
            raise ValueError('QA_R2_PRODUCTION_REFERENCE_REFUSED')
    return values
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


def guard(values, production_key_hashes=()):
    if any(not values.get(k) for k in KEYS):
        raise ValueError('QA_R2_MISSING_IDENTITY')
    pair = values['R2_ACCOUNT_ID'] + '/' + values['R2_BUCKET_NAME']
    if (hashlib.sha256(pair.encode()).hexdigest() == PROD_PAIR
            or values['R2_BUCKET_NAME'] != 'heydonna-qa'
            or hashlib.sha256(values['R2_ACCESS_KEY_ID'].encode()).hexdigest() in production_key_hashes):
        raise ValueError('QA_R2_PRODUCTION_IDENTITY_REFUSED')
    if values['R2_ENDPOINT_URL'] != 'https://' + values['R2_ACCOUNT_ID'] + '.r2.cloudflarestorage.com':
        raise ValueError('QA_R2_ENDPOINT_REFUSED')
    if values.get('R2_PUBLIC_URL'):
        raise ValueError('QA_R2_PUBLIC_URL_REFUSED')
    return values


def qa_values():
    s = PRIVATE.lstat()
    if stat.S_ISLNK(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise ValueError('QA_R2_PRIVATE_FILE_PERMISSIONS')
    data = parse(PRIVATE.read_text())
    values = {k: data.get('QA_' + k, '') for k in KEYS}
    metadata = PRIVATE.with_name('qa-r2-production-fingerprints.json')
    import json
    hashes = json.loads(metadata.read_text())['access_key_sha256']
    if not isinstance(hashes, list) or not hashes or not all(re.fullmatch(r'[0-9a-f]{64}', h) for h in hashes):
        raise ValueError('QA_R2_PRODUCTION_METADATA_INVALID')
    return guard(values, hashes)


def isolated(original, values=None):
    values = qa_values() if values is None else guard(values)
    # Parse before rewrite: ambiguous/multiline shell entries cannot bypass removal.
    parsed = parse(original)
    rewritten = dict(values, R2_PUBLIC_URL='')
    for name in parsed:
        if not STORAGE.search(name) or name in rewritten:
            continue
        # QA_R2_SCOPED is a controller-only switch (read by selected()), not an app variable. Rendering it as an empty value
        # failed Next's env validation (scripts/env.mjs) and stopped M15 QA runs 3-6 before Playwright (run 6 root cause, 22 Nov).
        if name == 'QA_R2_SCOPED':
            continue
        # Preserve aliases explicitly, rather than letting dotenv/Convex defaults
        # silently restore them. Never expose signing secrets via NEXT_PUBLIC_.
        suffix = name.split('R2_', 1)[-1]
        canonical = 'R2_' + suffix
        if canonical in values and not (name.startswith('NEXT_PUBLIC_') and 'KEY' in suffix):
            rewritten[name] = values[canonical]
        elif suffix in ('BUCKET',):
            rewritten[name] = values['R2_BUCKET_NAME']
        elif suffix in ('ENDPOINT', 'ACCOUNT_ENDPOINT'):
            rewritten[name] = values['R2_ENDPOINT_URL']
        else:
            rewritten[name] = ''
    lines = [k + '=' + shlex.quote(v) for k, v in parsed.items() if not STORAGE.search(k)]
    lines.extend(k + '=' + shlex.quote(v) for k, v in rewritten.items())
    rendered = '\n'.join(lines) + '\n'
    guard(parse(rendered))
    guard_environment(parse(rendered), parsed)
    return rendered


def selected(original):
    """Base (CI-parity) storage by default; QA_R2_SCOPED=1 opts into scoped QA storage.

    Rajiv C0ALZJHGE49 thread 1794295319.738879, ts 1794372014.378099:
    "Qa also has to use heydonna uploads r2 bucket like ci does."
    """
    scoped = os.environ.get('QA_R2_SCOPED', parse(original).get('QA_R2_SCOPED', '0')) == '1'
    rendered = isolated(original) if scoped else original
    if not scoped:
        # Uploads CORS admits CI's exact localhost:3099 origin, not proxy port 3009.
        # Remote boxes have isolated networking; the checkout's host port is irrelevant.
        parsed = parse(rendered)
        parsed.update(PORT='3099', BASE_URL='http://localhost:3099',
                      NEXT_PUBLIC_BASE_URL='http://localhost:3099')
        rendered = ''.join(k + '=' + shlex.quote(v) + '\n' for k, v in parsed.items())
    values = {k: v for k, v in parse(rendered).items()
              if (STORAGE.search(k) if scoped else k.startswith('R2_'))}
    return rendered, values, scoped


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    rendered, _, scoped = selected(Path(a.input).read_text())
    fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(rendered)
    print('QA_R2_GUARD=PASS mode=scoped' if scoped else
          'QA_R2_GUARD=PASS mode=base-env (CI parity, Rajiv ts 1794372014.378099)')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('QA_R2_REFUSED: private identity or environment invalid (details withheld)')
