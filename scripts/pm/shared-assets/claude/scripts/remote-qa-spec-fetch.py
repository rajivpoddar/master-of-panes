#!/usr/bin/env python3
"""Split QA artifacts before the existing verified fetch, then publish verified media."""
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

MARKER = '.qa-media-index.json'
MEDIA = {'.zip', '.webm', '.mp4'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def regular_files(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('artifact root absent or symlink')
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            p = Path(directory) / name
            mode = p.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError('symlink or nonregular artifact refused')
            if stat.S_ISREG(mode):
                files.append(p)
    return sorted(files, key=lambda p: ('test-results' not in p.parts, str(p)))


def prepare(source, destination, mode):
    if mode not in ('small', 'media'):
        raise ValueError('invalid pass')
    files = regular_files(source)
    if destination.exists() or destination.is_symlink():
        raise ValueError('snapshot already exists')
    destination.mkdir(parents=True, mode=0o700)
    seen = {}
    duplicates = {}
    copied = 0
    for path in files:
        relative = path.relative_to(source)
        is_media = path.suffix.lower() in MEDIA
        if is_media != (mode == 'media'):
            continue
        if mode == 'media':
            key = (path.stat().st_size, digest(path))
            if key in seen:
                duplicates[str(relative)] = seen[key]
                continue
            seen[key] = str(relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target, follow_symlinks=False)
        copied += 1
    if mode == 'media':
        (destination / MARKER).write_text(json.dumps({'files': copied, 'duplicates': duplicates}, sort_keys=True))
    print(f'QA_FETCH_PREPARED pass={mode} files={copied} duplicates={len(duplicates)}')


def publish_media(source, target):
    files = regular_files(source)
    if target.is_symlink() or not target.is_dir():
        raise ValueError('verified small artifact directory missing')
    marker = source / MARKER
    info = json.loads(marker.read_text())
    media = [p for p in files if p != marker]
    if info.get('files') != len(media):
        raise ValueError('media index mismatch')
    duplicates = info.get('duplicates', {})
    if not isinstance(duplicates, dict):
        raise ValueError('invalid media duplicate index')
    def safe_relative(value):
        if not isinstance(value, str):
            raise ValueError('invalid media path')
        path = Path(value)
        if path.is_absolute() or not path.parts or any(p in ('.', '..') for p in path.parts):
            raise ValueError('unsafe media path')
        return path
    canonical = {p.relative_to(source) for p in media}
    links = {}
    for name, original in duplicates.items():
        dest, original = safe_relative(name), safe_relative(original)
        if original not in canonical or dest in canonical or dest in links:
            raise ValueError('invalid media duplicate mapping')
        links[dest] = original
    # Verify all destination paths before publishing any bytes; never replace small evidence.
    for relative in list(canonical) + list(links):
        cursor = target
        for part in relative.parts[:-1]:
            cursor = cursor / part
            if cursor.is_symlink() or (cursor.exists() and not cursor.is_dir()):
                raise ValueError('unsafe media destination')
        dest = target / relative
        if dest.exists() or dest.is_symlink():
            raise ValueError('media destination already exists')
    for path in media:
        dest = target / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as out, path.open('rb') as src:
            shutil.copyfileobj(src, out)
        if digest(dest) != digest(path):
            raise ValueError('published media hash mismatch')
    # Recreate duplicate paths as hard links, so existing traces.sha256 receipts remain usable
    # without downloading or storing another copy of the media bytes.
    for relative, original in links.items():
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.link(target / original, dest, follow_symlinks=False)
    has_trace = any(p.name.endswith('trace.zip') for p in media)
    has_video = any(p.suffix.lower() in ('.webm', '.mp4') for p in media)
    complete = has_trace and has_video
    print(f'QA_FETCH_MEDIA={"COMPLETE" if complete else "PARTIAL"} files={len(media)} duplicates={len(links)}' + ('' if complete else ' reason=missing-trace-or-video'))
    return 0 if complete else 3


if __name__ == '__main__':
    try:
        if len(sys.argv) == 5 and sys.argv[1] == 'prepare':
            prepare(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
        elif len(sys.argv) == 4 and sys.argv[1] == 'publish':
            sys.exit(publish_media(Path(sys.argv[2]), Path(sys.argv[3])))
        else:
            raise ValueError('invalid arguments')
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f'QA_FETCH_UNAVAILABLE: {exc}', file=sys.stderr)
        sys.exit(1)
