#!/usr/bin/env python3
"""Deploy one explicitly chosen source, start its own controller and restore from phone."""
from __future__ import annotations
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit


def github_identity(value: str) -> tuple[str, str]:
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?', value)
    if not match or match[1] in ('.', '..') or match[2] in ('.', '..'):
        raise ValueError('Provide an explicit GitHub HTTPS or SSH repository URL without credentials or query parameters')
    return match[1].lower(), match[2].lower()


def run(argv, cwd=None, capture=False):
    result = subprocess.run([str(v) for v in argv], cwd=cwd, text=True, check=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def install_environment(checkout: Path):
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11+ is required')
    if not (checkout / 'pyproject.toml').is_file() or not (checkout / 'control.py').is_file():
        raise ValueError('The selected source is not a standalone DevHelper desktop checkout')
    python = checkout / '.venv/bin/python'
    if not python.is_file():
        run([sys.executable, '-m', 'venv', checkout / '.venv'])
    run([python, '-m', 'pip', 'install', '-e', '.'], checkout)


def config_path(checkout: Path) -> Path:
    return checkout / 'data/deployment.json'


def save_config(checkout: Path, repo: str):
    path = config_path(checkout)
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'repositoryUrl': repo}, indent=2) + '\n')
    temporary.replace(path)


def update(checkout: Path, requested_repo=None):
    if not (checkout / '.git').is_dir():
        raise ValueError('The selected directory is not a Git checkout')
    config = config_path(checkout)
    if requested_repo:
        configured = requested_repo
    elif config.is_file():
        configured = json.loads(config.read_text())['repositoryUrl']
    else:
        raise ValueError('No repository is configured; supply --repo with the user-confirmed URL')
    origin = run(['git', 'remote', 'get-url', 'origin'], checkout, True)
    if github_identity(configured) != github_identity(origin):
        raise ValueError('The configured repository and origin differ; no update was performed')
    if run(['git', 'status', '--porcelain'], checkout, True):
        raise ValueError('The checkout contains local changes; keep them and resolve before updating')
    run(['git', 'pull', '--ff-only'], checkout)
    install_environment(checkout)
    save_config(checkout, configured)


def copy_public_source(source: Path, checkout: Path):
    """Allowlist-only local installation for an isolated verification machine."""
    source = source.expanduser().resolve(strict=True)
    if checkout.exists():
        raise ValueError('Local-source installation requires a new directory; the existing destination was preserved')
    manifest = json.loads((source / 'publish-files.json').read_text(encoding='utf-8'))['files']
    validated = []
    for relative in manifest:
        if not isinstance(relative, str):
            raise ValueError('The source manifest includes a non-string path')
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or str(path) != relative or '\\' in relative:
            raise ValueError('The source manifest includes an unsafe path')
        blocked = {'.git', '.venv', '.asr-venv', 'data', 'shared', '__pycache__', 'build', 'dist', '.env'}
        if relative in ('control-state.json', 'control.lock', '.publish-target.json', '.publish-target.tmp') or any(p in blocked or p.startswith('.env.') or p.endswith('.egg-info') for p in path.parts) or any(p.startswith('verification') or p.endswith(('.log', '.pid', '.db', '.sqlite3', '.pyc')) for p in path.parts):
            raise ValueError('The source manifest includes runtime data')
        original = source / relative
        if original.is_symlink() or not original.is_file() or source not in original.resolve().parents:
            raise ValueError('A source file is missing or escapes the source directory')
        validated.append((original, checkout / relative))
    for original, target in validated:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)


def api(base, method, path, data=None):
    origin = urlsplit(base)
    if origin.scheme not in ('http', 'https') or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:
        raise ValueError('Provide a DevHelper HTTP root address without credentials or extra paths')
    encoded = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(base + path, data=encoded, method=method,
                                     headers={'Content-Type': 'application/json'} if encoded is not None else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=180) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError('The deployment API response exceeds 4 MiB')
        result = json.loads(body)
        if isinstance(result, dict) and result.get('error'):
            raise ValueError(str(result['error'])[:500])
        return result


def restore(base, phone=None, install_skills=False):
    if api(base, 'GET', '/health').get('appId') != 'devhelper-desktop':
        raise ValueError('The address is not a DevHelper desktop server')
    if phone:
        api(base, 'POST', '/api/config', {'androidUrl': phone})
    devices = api(base, 'GET', '/api/devices').get('devices', [])
    if not any(v.get('id') == 'android' and v.get('online') for v in devices):
        return {'restored': False, 'reason': 'phone_unavailable', 'autoSyncEnabled': False}
    result = api(base, 'POST', '/api/sync/run', {'direction': 'download'})
    api(base, 'POST', '/api/sync/config', {'autoSync': True})
    materialized = api(base, 'POST', '/api/sync/materialize', {'installSkills': True}) if install_skills else None
    # Report metadata only, never print downloaded memories or Skill bodies.
    sync_summary = {k: result[k] for k in ('initialized', 'state', 'uploaded', 'downloaded', 'error') if k in result and not isinstance(result[k], (dict, list))}
    sync_summary.update({k: v for k, v in result.get('summary', {}).items() if isinstance(v, (int, bool))})
    if 'conflicts' in result:
        sync_summary['conflicts'] = len(result['conflicts']) if isinstance(result['conflicts'], (dict, list)) else result['conflicts']
    skills_summary = {k: v for k, v in (materialized or {}).items() if isinstance(v, (int, bool))}
    if materialized is not None:
        skills_summary['errorCount'] = len(materialized.get('errors', []))
    return {'restored': True, 'autoSyncEnabled': True, 'sync': sync_summary,
            'privateSkills': skills_summary if install_skills else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'update', 'start', 'status', 'stop', 'restore'))
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument('--repo')
    sources.add_argument('--source', type=Path)
    parser.add_argument('--checkout', type=Path, default=Path.home() / 'DevHelper')
    parser.add_argument('--port', type=int, default=8876)
    parser.add_argument('--url')
    parser.add_argument('--phone', help='User-provided current Android HTTP root address; otherwise use discovery')
    parser.add_argument('--install-skills', action='store_true', help='Explicitly install restored enabled/autoLoad private Skills into managed Codex folders')
    parser.add_argument('--no-start', action='store_true', help='Prepare source and environment only')
    args = parser.parse_args()
    checkout = args.checkout.expanduser().resolve()
    if not 1 <= args.port <= 65535:
        raise ValueError('port must be between 1 and 65535')
    base = (args.url or f'http://127.0.0.1:{args.port}').rstrip('/')
    if args.action == 'install':
        if args.source:
            copy_public_source(args.source, checkout)
            install_environment(checkout)
        elif args.repo:
            github_identity(args.repo)
            if checkout.exists():
                if not (checkout / '.git').is_dir():
                    raise ValueError('The destination already exists and is not a Git checkout; it was preserved')
                update(checkout, args.repo)
            else:
                checkout.parent.mkdir(parents=True, exist_ok=True)
                run(['git', 'clone', '--', args.repo, checkout])
                install_environment(checkout)
                save_config(checkout, args.repo)
        else:
            raise ValueError('A new installation requires --repo or --source with the user-confirmed source')
        result = {'installed': True, 'checkout': str(checkout)}
        if not args.no_start:
            control(checkout, 'start', args.port)
            result['restore'] = restore(base, args.phone, args.install_skills)
        print(json.dumps(result, ensure_ascii=False))
    elif args.action == 'update':
        if args.source:
            raise ValueError('Use a configured Git checkout for updates; local source copying is only for a new isolated installation')
        update(checkout, args.repo)
        print(json.dumps({'updated': True, 'checkout': str(checkout)}))
    elif args.action == 'restore':
        print(json.dumps(restore(base, args.phone, args.install_skills), ensure_ascii=False))
    else:
        control(checkout, args.action, args.port)


def control(checkout: Path, action: str, port: int):
    python, script = checkout / '.venv/bin/python', checkout / 'control.py'
    if not python.is_file() or not script.is_file():
        raise ValueError('Install DevHelper in this directory before using its controller')
    run([python, script, action, '--port', port], checkout)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as failed:
        print(str(failed), file=sys.stderr)
        raise SystemExit(1)
