#!/usr/bin/env python3
"""Check Git's index for private runtime files and common credential patterns.

This is a publication guard, not a complete secret detector. Inspect the diff too.
Only paths and rule names are printed; matched values are never printed.
"""
import re
import subprocess
import sys
from pathlib import PurePosixPath

ROOT_FILES = {
    '.gitignore', '.gitattributes', 'README.md', 'README.en.md', 'LICENSE',
    'SECURITY.md', 'CONTRIBUTING.md', 'pyproject.toml', 'MANIFEST.in',
    'xdl.py', 'scrape.mjs', 'test_xdl.py', '启动 X Download.command',
}
SOURCE_DIRS = {'xdownload', 'tests', 'scripts', 'examples', '.github'}
PRIVATE_PARTS = {'data', 'downloads', '.venv', '__pycache__', 'build', 'dist', 'launchd', '.git'}
RULES = {
    'personal home path': re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'private network address': re.compile(r'\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
    'GitHub credential': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'),
    'API credential': re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'literal session cookie': re.compile(r'''(?:auth_token|ct0)\s*[=:]\s*["'][A-Za-z0-9%_-]{20,}["']'''),
}


def violations(name, content):
    path = PurePosixPath(name)
    if (not path.parts or any(part in PRIVATE_PARTS for part in path.parts)
            or (name not in ROOT_FILES and path.parts[0] not in SOURCE_DIRS)
            or path.name.startswith('.env') or path.suffix in {'.pem', '.key', '.pyc', '.log', '.part'}
            or path.name in {'settings.json', 'nas.json', 'bark.json', 'cookies.txt'}):
        return ['private or unreviewed path']
    try:
        content = content.decode('utf-8')
    except UnicodeDecodeError:
        return ['unexpected binary file']
    return [label for label, pattern in RULES.items() if pattern.search(content)]


def main():
    names = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
    failures = []
    for name in filter(None, names):
        content = subprocess.check_output(['git', 'show', ':' + name])
        failures.extend(f'{name}: {rule}' for rule in violations(name, content))
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return 1
    count = sum(bool(name) for name in names)
    if not count:
        print('No indexed files to review.', file=sys.stderr)
        return 1
    print(f'PASS: {count} indexed files passed the public-tree guard.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
