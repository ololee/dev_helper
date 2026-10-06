#!/usr/bin/env python3
"""Publish only the explicit public-source manifest to a configured GitHub repo.

Default invocation is read-only. --configure stores a local target, --commit
creates a local reviewed source commit, and --push explicitly requests upload.
No runtime data, background pushes, force pushes or GitHub login are performed.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'publish-files.json'
CONFIG = ROOT / '.publish-target.json'


def run(argv, capture=False):
    process = subprocess.run(argv, cwd=ROOT, check=True, stdout=subprocess.PIPE if capture else None,
                             stderr=subprocess.PIPE if capture else None)
    return process.stdout if capture else b''


def github_identity(url: str):
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?', url)
    if not match or match[1] in ('.', '..') or match[2] in ('.', '..'):
        raise ValueError('Provide an explicit GitHub HTTPS or SSH repository URL without credentials')
    return match[1].lower(), match[2].lower()


def valid_path(value):
    if not isinstance(value, str) or not value or '\\' in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or '..' in path.parts or str(path) != value:
        return False
    blocked = {'.git', '.venv', 'data', 'shared', '__pycache__', 'build', 'dist', '.env'}
    if any(part in blocked or part.startswith('.env.') or part.endswith('.egg-info') for part in path.parts):
        return False
    if any(part.startswith('verification') or part.endswith(('.log', '.pid', '.sqlite3', '.db', '.pyc')) for part in path.parts):
        return False
    if value in ('control-state.json', 'control.lock', '.publish-target.json', '.publish-target.tmp'):
        return False
    return True


def manifest_paths(value):
    files = value['files']
    if not isinstance(files, list) or not all(valid_path(v) for v in files) or len(files) != len(set(files)):
        raise ValueError('The publish manifest has unsafe or duplicate paths')
    if 'publish-files.json' not in files:
        raise ValueError('The manifest must include itself')
    return set(files)


def check_portable_source(relative, content):
    target = PurePosixPath(relative)
    if target.suffix in ('.py', '.md', '.json', '.toml', '.html', '.yaml', '.command') or target.name in ('.gitignore', 'LICENSE', 'NOTICE'):
        private_component = rb'[^/\s"\']+/'
        if re.search(b'/' + b'Users' + b'/' + private_component, content) or re.search(b'/' + b'home' + b'/' + private_component, content):
            raise ValueError('A public source file contains a user-specific absolute path: ' + relative)


def read_manifest():
    files = manifest_paths(json.loads(MANIFEST.read_text(encoding='utf-8')))
    for relative in files:
        target = ROOT / relative
        if target.is_symlink() or not target.is_file() or ROOT.resolve() not in target.resolve().parents:
            raise ValueError('A manifest source file is missing or a symlink: ' + relative)
        # Public code must be portable and must not disclose private paths.
        check_portable_source(relative, target.read_bytes())
    return files


def check_publish_history():
    """A clean latest tree must not conceal private files in earlier commits."""
    checked_blobs = set()
    commits = run(['git', 'rev-list', 'HEAD'], True).splitlines()
    for raw_commit in commits:
        commit = raw_commit.decode('ascii')
        try:
            allowed = manifest_paths(json.loads(run(['git', 'show', commit + ':publish-files.json'], True)))
        except (subprocess.CalledProcessError, ValueError, KeyError, TypeError) as failed:
            raise ValueError('A reachable commit has no valid public manifest: ' + commit[:12]) from failed
        entries = run(['git', 'ls-tree', '-r', '-z', commit], True).split(b'\0')
        for entry in entries:
            if not entry:
                continue
            metadata, encoded_path = entry.split(b'\t', 1)
            mode, kind, object_id = metadata.split(b' ')
            relative = encoded_path.decode('utf-8')
            if relative not in allowed or mode not in (b'100644', b'100755') or kind != b'blob':
                raise ValueError('A reachable commit includes a non-public source file: ' + commit[:12] + ': ' + relative)
            key = (relative, object_id)
            if key not in checked_blobs:
                check_portable_source(relative, run(['git', 'cat-file', 'blob', object_id.decode('ascii')], True))
                checked_blobs.add(key)
    return len(commits)


def previous_manifest():
    try:
        value = json.loads(run(['git', 'show', 'HEAD:publish-files.json'], True))
        files = value.get('files', [])
        return {v for v in files if valid_path(v)}
    except (subprocess.CalledProcessError, ValueError, KeyError):
        return set()


def changed_paths():
    items = run(['git', 'status', '--porcelain', '-z', '--untracked-files=all'], True).split(b'\0')
    result = set()
    position = 0
    while position < len(items):
        record = items[position]
        position += 1
        if not record:
            continue
        if len(record) < 4:
            raise ValueError('Unexpected Git status output')
        status = record[:2].decode('ascii')
        result.add(record[3:].decode('utf-8'))
        if 'R' in status or 'C' in status:
            if position >= len(items):
                raise ValueError('Unexpected Git rename output')
            result.add(items[position].decode('utf-8'))
            position += 1
    return result


def configured_target():
    if not CONFIG.is_file():
        raise ValueError('No publishing target is configured; confirm a repository and use --configure first')
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    github_identity(config['repositoryUrl'])
    branch = config['branch']
    run(['git', 'check-ref-format', '--branch', branch], True)
    actual = run(['git', 'branch', '--show-current'], True).decode().strip()
    if actual != branch:
        raise ValueError('The current branch does not match the configured publish branch')
    origin = run(['git', 'remote', 'get-url', 'origin'], True).decode().strip()
    if github_identity(origin) != github_identity(config['repositoryUrl']):
        raise ValueError('origin does not match the configured repository; no upload was performed')
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--configure', metavar='GITHUB_URL')
    parser.add_argument('--branch', default='main')
    parser.add_argument('--commit', metavar='MESSAGE')
    parser.add_argument('--push', action='store_true')
    args = parser.parse_args()
    if args.configure:
        github_identity(args.configure)
        run(['git', 'check-ref-format', '--branch', args.branch], True)
        config = {'repositoryUrl': args.configure, 'branch': args.branch}
        temporary = CONFIG.with_suffix('.tmp')
        temporary.write_text(json.dumps(config, indent=2) + '\n')
        temporary.replace(CONFIG)
        print(json.dumps({'configured': True, 'repository': args.configure, 'branch': args.branch}))
        if not args.commit and not args.push:
            return
    current = read_manifest()
    allowed = current | previous_manifest()
    tracked = {p.decode() for p in run(['git', 'ls-files', '-z'], True).split(b'\0') if p}
    forbidden = (tracked | changed_paths()) - allowed
    if forbidden:
        raise ValueError('Files outside the public manifest must be preserved outside this publication: ' + ', '.join(sorted(forbidden)))
    if args.commit:
        configured_target()
        run(['git', 'add', '--', *sorted(allowed)])
        staged = {p.decode() for p in run(['git', 'diff', '--cached', '--name-only', '-z'], True).split(b'\0') if p}
        if staged - allowed:
            raise ValueError('The staged changes include files outside the public manifest')
        if staged:
            # This preview is source-only: manifest validation already excluded
            # runtime files and user-specific paths before anything was staged.
            run(['git', 'diff', '--cached', '--stat'])
            run(['git', 'commit', '-m', args.commit])
    if args.push:
        config = configured_target()
        if changed_paths():
            raise ValueError('Commit reviewed source changes before uploading; the worktree must be clean')
        check_publish_history()
        run(['git', 'push', 'origin', config['branch']])
        print(json.dumps({'pushed': True, 'repository': config['repositoryUrl'], 'branch': config['branch']}))
    else:
        print(json.dumps({'checked': True, 'manifestFiles': len(current), 'changedSourceFiles': sorted(changed_paths()), 'pushed': False}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as failed:
        print(str(failed), file=sys.stderr)
        raise SystemExit(1)
