#!/usr/bin/env python3
"""Assemble a reviewable standalone source release using an explicit file list."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parents[1]
CORE = ('server.py', 'control.py', 'clipboard.py', 'devices.py', 'knowledge.py', 'sync.py',
        'private_skills.py', 'web_assets.py', 'workflows.py', 'ai_support.py', 'notifications.py', 'relay_client.py', 'relay/__init__.py', 'relay/server.py', 'relay/PROTOCOL.md', 'relay/requirements.txt', 'scripts/deploy_relay.py', 'pyproject.toml', 'README.md', 'LICENSE', 'NOTICE',
        '.gitignore', '启动助手.command', '停止助手.command', 'scripts/publish.py', 'scripts/package_release.py', 'scripts/setup_asr.py', 'scripts/import_project.py', 'skills/devhelper-connect/SKILL.md')
ASSETS = ('knowledge.html', 'notes.html', 'vendor/markdown-it.min.js', 'vendor/markdown-it.LICENSE',
          'vendor/markdown-it.provenance.json', 'vendor/README.md')
SKILL = ('SKILL.md', 'agents/openai.yaml', 'scripts/deploy.py')


def prepare(output: Path, skill_source: Path | None = None):
    output = output.expanduser().resolve()
    if output == SOURCE or SOURCE in output.parents:
        raise ValueError('Choose an independent output directory outside the development source')
    files = {relative: SOURCE / relative for relative in CORE}
    files['static/index.html'] = SOURCE / 'static/index.html'
    files.update({str(path.relative_to(SOURCE)): path for path in (SOURCE / 'tests').glob('test_*.py')})
    sys.path.insert(0, str(SOURCE))
    from web_assets import asset_path
    files.update({'static/' + name: asset_path(name) for name in ASSETS})
    skills = skill_source or SOURCE / 'skills/devhelper-deploy'
    files.update({'skills/devhelper-deploy/' + relative: skills / relative for relative in SKILL})
    for relative, path in files.items():
        if not path.is_file() or path.is_symlink():
            raise ValueError('A required public source file is missing or a symlink: ' + relative)
    # Validate the complete source list before writing any output. This cannot
    # descend into runtime data, virtualenvs, proof files or private documents.
    for relative, path in files.items():
        target = output / relative
        if not target.parent.resolve().is_relative_to(output):
            raise ValueError('A release destination directory escapes the output: ' + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError('A release destination is a symlink: ' + relative)
        shutil.copy2(path, target)
    manifest = {'formatVersion': 1, 'files': sorted([*files, 'publish-files.json'])}
    (output / 'publish-files.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    provenance = json.loads((output / 'static/vendor/markdown-it.provenance.json').read_text())
    bundle = output / 'static/vendor/markdown-it.min.js'
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != provenance['bundleSha256']:
        raise ValueError('The vendored Markdown bundle hash differs from its published provenance')
    print(json.dumps({'prepared': True, 'output': str(output), 'publicFiles': len(manifest['files'])}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--skill-source', type=Path)
    args = parser.parse_args()
    prepare(args.output, args.skill_source)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as failed:
        print(str(failed), file=sys.stderr)
        raise SystemExit(1)
