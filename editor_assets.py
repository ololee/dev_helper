"""Serve only the pinned Monaco distribution recorded in its provenance file."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent / 'static' / 'vendor' / 'monaco'
MIMES = {'.js': 'text/javascript', '.css': 'text/css', '.ttf': 'font/ttf',
         '.woff': 'font/woff', '.woff2': 'font/woff2', '.json': 'application/json'}


def distribution(root=ROOT, *, verify=False):
    root = Path(root)
    if root.is_symlink():
        raise ValueError('The editor distribution cannot be a symbolic link')
    provenance = root / 'PROVENANCE.json'
    if provenance.is_symlink():
        raise ValueError('The editor provenance cannot be a symbolic link')
    data = json.loads(provenance.read_text())
    if data.get('name') != 'monaco-editor' or data.get('version') != '0.52.2':
        raise ValueError('The editor differs from its pinned distribution')
    records = data.get('files', {})
    if not isinstance(records, dict) or not 2 <= len(records) <= 1000:
        raise ValueError('Invalid editor distribution manifest')
    for name, record in records.items():
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
                or str(path) != name or not isinstance(record, dict)):
            raise ValueError('Unsafe editor distribution path')
        target = root.joinpath(*path.parts)
        if any(root.joinpath(*path.parts[:index]).is_symlink()
               for index in range(1, len(path.parts) + 1)) or not target.is_file():
            raise ValueError('Missing or symbolic editor distribution file')
        if verify:
            content = target.read_bytes()
            if len(content) != record.get('bytes') or hashlib.sha256(content).hexdigest() != record.get('sha256'):
                raise ValueError('Editor distribution checksum does not match')
    return records


@lru_cache(maxsize=1)
def _allowed():
    return distribution(verify=True)


def editor_asset(name):
    prefix = 'vendor/monaco/'
    if not isinstance(name, str) or not name.startswith(prefix):
        return None
    relative = name.removeprefix(prefix)
    if relative not in _allowed() and relative != 'PROVENANCE.json':
        return None
    path = ROOT / relative
    if (ROOT.is_symlink() or path.is_symlink() or not path.is_file()
            or not path.resolve().is_relative_to(ROOT.resolve())):
        return None
    return path, MIMES.get(path.suffix.lower(), 'text/plain')
