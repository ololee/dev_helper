"""Versioned, credential-free SQLite catalogs for cloud backup and portable restore.

Only ordinary whitelisted data tables are read from an incoming database. Its SQL
is never copied into the live catalog, nor are old machine source paths restored.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import struct
import tempfile
import time
from urllib.parse import urlsplit
import uuid

try:
    from .asset_library import AssetLibraryError, KINDS, _id, _json, _relative, _read_source, _signature
except ImportError:
    from asset_library import AssetLibraryError, KINDS, _id, _json, _relative, _read_source, _signature

SCHEMA_VERSION = 1
APPLICATION_ID = 0x44484331
MAX_BYTES = 2 * 1024 ** 3
MAX_ASSETS = 1_000_000
MAX_ROWS = 2_000_000
MAX_JSON_BYTES = 2 * 1024 ** 2
MAX_DIMENSIONS = 4096
TABLES = {
    'meta': 'key TEXT PRIMARY KEY,value TEXT NOT NULL',
    'projects': 'id TEXT PRIMARY KEY,name TEXT NOT NULL,options TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,stats TEXT NOT NULL',
    'assets': 'id TEXT PRIMARY KEY,project_id TEXT NOT NULL,path TEXT NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,sha256 TEXT NOT NULL,md5 TEXT,kind TEXT NOT NULL,extension TEXT NOT NULL,mime TEXT NOT NULL,metadata TEXT NOT NULL,search_text TEXT NOT NULL,parser_version INTEGER NOT NULL,dhash TEXT,color BLOB,UNIQUE(project_id,path)',
    'identifiers': 'source_id TEXT NOT NULL,project_id TEXT NOT NULL,namespace TEXT NOT NULL,value TEXT NOT NULL,target_path TEXT NOT NULL,UNIQUE(source_id,namespace,value,target_path)',
    'refs': 'source_id TEXT NOT NULL,namespace TEXT NOT NULL,value TEXT NOT NULL,evidence TEXT NOT NULL,UNIQUE(source_id,namespace,value,evidence)',
    'annotations': 'asset_id TEXT NOT NULL,source TEXT NOT NULL,data TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(asset_id,source)',
    'embeddings': 'asset_id TEXT NOT NULL,model TEXT NOT NULL,dimensions INTEGER NOT NULL,vector BLOB NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(asset_id,model)',
    'locations': 'asset_id TEXT NOT NULL,provider TEXT NOT NULL,fs_id TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,PRIMARY KEY(asset_id,provider,fs_id)',
    'query_profiles': 'space TEXT PRIMARY KEY,base_url TEXT NOT NULL,model TEXT NOT NULL,dimensions INTEGER,input_version TEXT NOT NULL,mode TEXT NOT NULL',
}
COLUMNS = {
    'meta': ('key', 'value'),
    'projects': ('id', 'name', 'options', 'created', 'updated', 'stats'),
    'assets': ('id', 'project_id', 'path', 'size', 'mtime_ns', 'sha256', 'md5', 'kind', 'extension', 'mime', 'metadata', 'search_text', 'parser_version', 'dhash', 'color'),
    'identifiers': ('source_id', 'project_id', 'namespace', 'value', 'target_path'),
    'refs': ('source_id', 'namespace', 'value', 'evidence'),
    'annotations': ('asset_id', 'source', 'data', 'updated'),
    'embeddings': ('asset_id', 'model', 'dimensions', 'vector', 'sha256'),
    'locations': ('asset_id', 'provider', 'fs_id', 'path', 'sha256', 'bytes'),
    'query_profiles': ('space', 'base_url', 'model', 'dimensions', 'input_version', 'mode'),
}
_HASH = re.compile(r'^[0-9a-f]{64}$')
_OPTIONS = {'skipDirs', 'maxTextBytes', 'maxImagePixels'}


def _bad(message='Invalid portable catalog'):
    raise AssetLibraryError(message)


def _text(value, maximum=4096, *, empty=True):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value or not empty and not value:
        _bad('Invalid catalog text field')
    return value


def _number(value, maximum=10 ** 20):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
        _bad('Invalid catalog number')
    return value


def _integer(value, maximum=2 ** 63 - 1, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _bad('Invalid catalog integer')
    return value


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _bad('Invalid catalog content hash')
    return value


def _json_object(value):
    _text(value, MAX_JSON_BYTES)
    if len(value.encode('utf-8')) > MAX_JSON_BYTES:
        _bad('Catalog JSON exceeds size limit')
    def reject_constant(_):
        _bad('Non-finite catalog JSON')
    try:
        result = json.loads(value, parse_constant=reject_constant)
        if not isinstance(result, dict):
            _bad('Catalog JSON field must contain an object')
        pending = [(result, 0)]
        while pending:
            child, depth = pending.pop()
            if depth > 48:
                _bad('Catalog JSON nesting exceeds limit')
            if isinstance(child, dict):
                for key, nested in child.items():
                    _text(key, 500)
                    pending.append((nested, depth + 1))
            elif isinstance(child, list):
                pending.extend((nested, depth + 1) for nested in child)
            elif isinstance(child, float) and not math.isfinite(child):
                _bad('Non-finite catalog JSON')
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _bad('Invalid catalog JSON')
    return result


def _options(value):
    data = _json_object(value)
    if set(data) - _OPTIONS:
        _bad('Portable project options must not contain machine-specific settings')
    skip = data.get('skipDirs', [])
    if not isinstance(skip, list) or len(skip) > 100:
        _bad('Invalid catalog skipped directories')
    for entry in skip:
        _text(entry, 100, empty=False)
        if entry in ('.', '..') or '/' in entry or '\\' in entry:
            _bad('Invalid catalog skipped directory')
    for key, high in [('maxTextBytes', 64 * 1024 ** 2), ('maxImagePixels', 250_000_000)]:
        if key in data:
            _integer(data[key], high, 1)
    return data


def _profile(value):
    if not isinstance(value, dict):
        _bad('Invalid query profile')
    allowed = {'space', 'baseUrl', 'base_url', 'model', 'dimensions', 'inputVersion', 'input_version', 'mode'}
    if set(value) - allowed:
        _bad('Query profiles cannot contain credentials or unknown fields')
    result = {'space': value.get('space'), 'base_url': value.get('baseUrl', value.get('base_url')),
              'model': value.get('model'), 'dimensions': value.get('dimensions'),
              'input_version': value.get('inputVersion', value.get('input_version')), 'mode': value.get('mode', 'chat')}
    _text(result['space'], 300, empty=False)
    _text(result['model'], 200, empty=False)
    _text(result['input_version'], 100, empty=False)
    _text(result['mode'], 50, empty=False)
    address = _text(result['base_url'], 1000, empty=False)
    try:
        parsed = urlsplit(address)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or any(ord(c) < 33 for c in address):
            _bad('Query profile API address cannot contain credentials or query parameters')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            _bad('Query profile requires HTTPS')
        parsed.port
    except ValueError:
        _bad('Invalid query profile API address')
    if result['dimensions'] is not None:
        _integer(result['dimensions'], MAX_DIMENSIONS, 1)
    return result


def _location(value):
    if not isinstance(value, dict):
        _bad('Invalid remote location')
    result = {'asset_id': value.get('assetId', value.get('asset_id')),
              'provider': value.get('provider', 'baidu-netdisk'), 'fs_id': str(value.get('fsId', value.get('fs_id', ''))),
              'path': value.get('path'), 'sha256': value.get('sha256'), 'bytes': value.get('bytes')}
    _id(result['asset_id'])
    if result['provider'] != 'baidu-netdisk' or not re.fullmatch(r'[0-9]{1,30}', result['fs_id']) or int(result['fs_id']) <= 0:
        _bad('Invalid Baidu file identity')
    path = _text(result['path'], 4096, empty=False)
    if not path.startswith('/apps/') or '\\' in path or any(part in ('.', '..', '') for part in path.split('/')[1:]):
        _bad('Remote asset path must be inside an application directory')
    _hash(result['sha256'])
    _integer(result['bytes'])
    # Explicit export callers must pass only completed, confirmed locations.
    if 'pathConfirmed' in value and value['pathConfirmed'] is not True:
        _bad('Remote location path has not been confirmed')
    if 'state' in value and value['state'] != 'completed':
        _bad('Remote upload has not completed')
    return result


def _insert(db, table, rows):
    columns = COLUMNS[table]
    db.executemany('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', rows)


def _summary(db):
    meta = {row['key']: json.loads(row['value']) for row in db.execute('SELECT key,value FROM meta')}
    profiles = [{'space': r['space'], 'baseUrl': r['base_url'], 'model': r['model'], 'dimensions': r['dimensions'], 'inputVersion': r['input_version'], 'mode': r['mode']} for r in db.execute('SELECT * FROM query_profiles ORDER BY space')]
    uploaded = db.execute('SELECT COUNT(DISTINCT asset_id) FROM locations').fetchone()[0]
    return {'snapshotId': meta['snapshotId'], 'schemaVersion': SCHEMA_VERSION, 'createdAt': meta['createdAt'], 'counts': meta['counts'], 'uploadedAssetCount': uploaded, 'queryProfiles': profiles}


class CatalogSnapshot:
    def __init__(self, library):
        self.library = library

    def export(self, path, *, project_ids=None, locations=None, query_profiles=None):
        path = Path(path).absolute()
        if path.exists() or path.is_symlink():
            raise AssetLibraryError('Catalog destination already exists', 409)
        if project_ids is not None and (not isinstance(project_ids, list) or not 1 <= len(project_ids) <= 10000 or len(set(project_ids)) != len(project_ids)):
            _bad('Choose unique project IDs for catalog export')
        if project_ids:
            for value in project_ids:
                _id(value)
        if locations is not None and not isinstance(locations, list) or query_profiles is not None and not isinstance(query_profiles, list):
            _bad('Locations and query profiles must be lists')
        identifier, created = str(uuid.uuid4()), time.time()
        fd, name = tempfile.mkstemp(prefix='.catalog-', suffix='.sqlite3', dir=path.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            with self.library.lock, self.library._db() as source:
                # Start a consistent read transaction; never copy the live WAL file itself.
                source.execute('BEGIN')
                selected = project_ids if project_ids is not None else [r[0] for r in source.execute('SELECT id FROM projects ORDER BY id')]
                if len(selected) > 10000:
                    _bad('Catalog project limit exceeded')
                with sqlite3.connect(temporary) as target:
                    target.row_factory = sqlite3.Row
                    target.execute('PRAGMA application_id=' + str(APPLICATION_ID))
                    target.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
                    for table, definition in TABLES.items():
                        target.execute('CREATE TABLE ' + table + '(' + definition + ')')
                    for project_id in selected:
                        row = source.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
                        if row is None:
                            raise AssetLibraryError('Project not found', 404)
                        options = {key: value for key, value in json.loads(row['options']).items() if key in _OPTIONS}
                        _insert(target, 'projects', [(row['id'], row['name'], _json(options), row['created'], row['updated'], row['stats'])])
                        _insert(target, 'assets', (tuple(row[key] for key in COLUMNS['assets']) for row in source.execute('SELECT * FROM assets WHERE project_id=? ORDER BY id', (project_id,))))
                        for table, key in [('identifiers', 'source_id'), ('refs', 'source_id'), ('annotations', 'asset_id'), ('embeddings', 'asset_id')]:
                            columns = ','.join('t.' + c for c in COLUMNS[table])
                            query = 'SELECT ' + columns + ' FROM ' + table + ' t JOIN assets a ON a.id=t.' + key + ' WHERE a.project_id=?'
                            if table == 'embeddings':
                                query += ' AND t.sha256=a.sha256'
                            _insert(target, table, (tuple(row) for row in source.execute(query, (project_id,))))
                    known = {r['id']: (r['sha256'], r['size']) for r in target.execute('SELECT id,sha256,size FROM assets')}
                    retained = {}
                    for row in source.execute('SELECT * FROM remote_locations'):
                        if row['asset_id'] in known:
                            location = _location(dict(row))
                            if (location['sha256'], location['bytes']) == known[row['asset_id']]:
                                retained[(location['asset_id'], location['provider'], location['fs_id'])] = location
                    for value in locations or []:
                        location = _location(value)
                        if location['asset_id'] not in known:
                            continue
                        if (location['sha256'], location['bytes']) != known[location['asset_id']]:
                            _bad('Remote file hash or size does not match catalog asset')
                        retained[(location['asset_id'], location['provider'], location['fs_id'])] = location
                    _insert(target, 'locations', [tuple(value[k] for k in COLUMNS['locations']) for value in retained.values()])
                    profiles = {r['space']: _profile(dict(r)) for r in source.execute('SELECT * FROM query_profiles')}
                    for value in query_profiles or []:
                        value = _profile(value)
                        profiles[value['space']] = value
                    spaces = {r[0] for r in target.execute('SELECT DISTINCT model FROM embeddings')}
                    _insert(target, 'query_profiles', [tuple(value[k] for k in COLUMNS['query_profiles']) for value in profiles.values() if value['space'] in spaces])
                    counts = {table: target.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in TABLES if table != 'meta'}
                    metadata = {'format': 'devhelper-catalog', 'schemaVersion': SCHEMA_VERSION, 'snapshotId': identifier, 'createdAt': created, 'counts': counts}
                    _insert(target, 'meta', [(key, _json(value)) for key, value in metadata.items()])
                    target.commit()
                    self._validate(target)
                if temporary.stat().st_size > MAX_BYTES:
                    _bad('Portable catalog exceeds 2 GiB')
                result = self.inspect(temporary, expected_sha256=self._file_hash(temporary))
                # O_EXCL above plus replace prevents partially written catalog exposure.
                os.link(temporary, path)
                os.chmod(path, 0o600)
                return result
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _file_hash(path):
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()

    @contextmanager
    def _incoming(self, path, expected_sha256):
        _hash(expected_sha256)
        supplied = Path(path).absolute()
        with tempfile.TemporaryDirectory(prefix='catalog-read-', dir=self.library.root) as directory:
            private = Path(directory) / 'catalog.sqlite3'
            digest, count = hashlib.sha256(), 0
            try:
                with _read_source(supplied.parent, supplied.name) as source, private.open('xb') as target:
                    before = _signature(os.fstat(source.fileno()))
                    if before[0] > MAX_BYTES:
                        _bad('Portable catalog exceeds 2 GiB')
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        count += len(block)
                        if count > MAX_BYTES:
                            _bad('Portable catalog exceeds 2 GiB')
                        target.write(block)
                        digest.update(block)
                    if _signature(os.fstat(source.fileno())) != before:
                        _bad('Catalog changed during validation')
                if count < 100 or digest.hexdigest() != expected_sha256:
                    _bad('Portable catalog SHA256 verification failed')
                db = sqlite3.connect(private.as_uri() + '?mode=ro&immutable=1', uri=True)
                try:
                    db.row_factory = sqlite3.Row
                    db.execute('PRAGMA trusted_schema=OFF')
                    db.execute('PRAGMA query_only=ON')
                    if hasattr(db, 'setlimit'):
                        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_JSON_BYTES + 65536)
                        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65536)
                        db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
                    self._validate(db)
                    yield db, digest.hexdigest(), count
                finally:
                    db.close()
            except (sqlite3.DatabaseError, OSError, UnicodeError) as error:
                raise AssetLibraryError('Portable catalog could not be read safely') from error

    def inspect(self, path, *, expected_sha256):
        with self._incoming(path, expected_sha256) as (db, digest, count):
            return {**_summary(db), 'sha256': digest, 'bytes': count}

    @staticmethod
    def _validate(db):
        if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
            _bad('Unsupported portable catalog format or schema version')
        tables = set()
        for row in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema'):
            if row['type'] == 'table' and row['name'] in TABLES and isinstance(row['sql'], str) and row['sql'].lstrip().upper().startswith('CREATE TABLE'):
                tables.add(row['name'])
            elif not (row['type'] == 'index' and row['name'].startswith('sqlite_autoindex_') and row['tbl_name'] in TABLES and row['sql'] is None):
                _bad('Portable catalogs cannot contain extra tables, views, triggers or executable schema')
        if tables != set(TABLES):
            _bad('Portable catalog data tables are incomplete')
        counts = {}
        for table, columns in COLUMNS.items():
            info = db.execute('PRAGMA table_xinfo(' + table + ')').fetchall()
            if tuple(r['name'] for r in info) != columns or any(r['hidden'] for r in info):
                _bad('Portable catalog table schema does not match version 1')
            count = db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            if count > (MAX_ASSETS if table == 'assets' else 10000 if table in ('projects', 'query_profiles') else 16 if table == 'meta' else MAX_ROWS):
                _bad('Portable catalog row limit exceeded')
            counts[table] = count
        metadata = {}
        for row in db.execute('SELECT * FROM meta'):
            _text(row['key'], 100)
            try:
                metadata[row['key']] = json.loads(row['value'])
            except (ValueError, TypeError, RecursionError):
                _bad('Invalid catalog metadata')
        if set(metadata) != {'format', 'schemaVersion', 'snapshotId', 'createdAt', 'counts'} or metadata['format'] != 'devhelper-catalog' or type(metadata['schemaVersion']) is not int or metadata['schemaVersion'] != SCHEMA_VERSION:
            _bad('Invalid portable catalog metadata')
        _id(metadata['snapshotId'])
        _number(metadata['createdAt'])
        if metadata['counts'] != {k: v for k, v in counts.items() if k != 'meta'}:
            _bad('Portable catalog row counts do not match manifest')
        if not isinstance(metadata['counts'], dict) or any(type(value) is not int for value in metadata['counts'].values()):
            _bad('Portable catalog counts must be integers')
        projects, assets, paths = set(), {}, set()
        for row in db.execute('SELECT * FROM projects'):
            projects.add(_id(row['id']))
            _text(row['name'], 200, empty=False)
            _options(row['options'])
            _json_object(row['stats'])
            _number(row['created']); _number(row['updated'])
        for row in db.execute('SELECT * FROM assets'):
            _id(row['id']); _id(row['project_id'])
            if row['project_id'] not in projects:
                _bad('Catalog asset references an unknown project')
            _text(row['path'], 4096, empty=False); _relative(row['path'])
            identity = (row['project_id'], row['path'])
            if row['id'] in assets or identity in paths:
                _bad('Duplicate catalog asset identity')
            paths.add(identity)
            _integer(row['size']); _integer(row['mtime_ns']); _hash(row['sha256'])
            if row['md5'] is not None and (not isinstance(row['md5'], str) or not re.fullmatch(r'[0-9a-f]{32}', row['md5'])):
                _bad('Invalid catalog MD5')
            if row['kind'] not in KINDS:
                _bad('Unknown catalog asset type')
            _text(row['extension'], 100); _text(row['mime'], 300)
            facts = _json_object(row['metadata'])
            if set(facts) & {'id', 'projectId', 'path', 'name', 'bytes', 'modifiedAt', 'sha256', 'md5', 'kind', 'extension', 'mimeType', 'annotations', 'embeddings', 'availability', 'remoteLocations'}:
                _bad('Catalog metadata cannot override resource identities or storage state')
            if 'ocr' in facts:
                ocr = facts['ocr']
                if not isinstance(ocr, dict) or not isinstance(ocr.get('status', ''), str) or ocr.get('text') is not None and not isinstance(ocr['text'], str):
                    _bad('Invalid catalog OCR metadata')
            _text(row['search_text'], MAX_JSON_BYTES)
            _integer(row['parser_version'], 100000)
            if row['dhash'] is not None and (not isinstance(row['dhash'], str) or not re.fullmatch(r'[0-9a-f]{16}', row['dhash'])):
                _bad('Invalid visual hash')
            if row['color'] is not None and (not isinstance(row['color'], bytes) or len(row['color']) != 192 or any(not math.isfinite(x) for x in struct.unpack('<48f', row['color']))):
                _bad('Invalid visual histogram')
            assets[row['id']] = (row['project_id'], row['sha256'], row['size'])
        for row in db.execute('SELECT * FROM annotations'):
            if row['asset_id'] not in assets or row['source'] not in ('manual', 'ai'):
                _bad('Invalid catalog annotation identity')
            data = _json_object(row['data'])
            if data.get('sha256') is not None:
                _hash(data['sha256'])
            for key in ('label', 'purpose', 'ocrText'):
                if key in data:
                    _text(data[key], 65536)
            if 'categories' in data and (not isinstance(data['categories'], list) or len(data['categories']) > 50 or any(not isinstance(x, str) or len(x) > 100 for x in data['categories'])):
                _bad('Invalid catalog categories')
            _number(row['updated'])
        models = {}
        for row in db.execute('SELECT * FROM embeddings'):
            _text(row['model'], 300, empty=False)
            dimensions = _integer(row['dimensions'], MAX_DIMENSIONS, 1)
            if row['asset_id'] not in assets or row['sha256'] != assets[row['asset_id']][1] or not isinstance(row['vector'], bytes) or len(row['vector']) != 4 * dimensions:
                _bad('Invalid portable embedding identity, shape or content hash')
            vector = struct.unpack('<' + str(dimensions) + 'f', row['vector'])
            if any(not math.isfinite(x) for x in vector) or not .999 <= math.sqrt(sum(x * x for x in vector)) <= 1.001:
                _bad('Portable embeddings must be finite normalized float32 vectors')
            if row['model'] in models and models[row['model']] != dimensions:
                _bad('Embedding space has inconsistent dimensions')
            models[row['model']] = dimensions
        for row in db.execute('SELECT * FROM identifiers'):
            if row['source_id'] not in assets or row['project_id'] != assets[row['source_id']][0]:
                _bad('Invalid catalog identifier source')
            _text(row['namespace'], 100, empty=False); _text(row['value'], 4096, empty=False)
            _text(row['target_path'], 4096, empty=False); _relative(row['target_path'])
        for row in db.execute('SELECT * FROM refs'):
            if row['source_id'] not in assets:
                _bad('Invalid catalog dependency source')
            _text(row['namespace'], 100, empty=False); _text(row['value'], 4096, empty=False); _text(row['evidence'], 12000)
        for row in db.execute('SELECT * FROM locations'):
            location = _location(dict(row))
            if location['asset_id'] not in assets or (location['sha256'], location['bytes']) != assets[location['asset_id']][1:]:
                _bad('Remote original does not match catalog asset')
        for row in db.execute('SELECT * FROM query_profiles'):
            value = _profile(dict(row))
            if value['space'] not in models or value['dimensions'] is not None and value['dimensions'] != models[value['space']]:
                _bad('Query profile must match a restored embedding space')

    def restore(self, path, *, expected_sha256):
        with self._incoming(path, expected_sha256) as (incoming, digest, count):
            summary = _summary(incoming)
            conflicts, imported, merged, new_projects, skipped_vectors = [], 0, 0, 0, 0
            with self.library.lock, self.library._db() as target:
                if target.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
                    raise AssetLibraryError('Wait for index scans before restoring a catalog', 409)
                for row in incoming.execute('SELECT * FROM projects'):
                    if target.execute('SELECT 1 FROM projects WHERE id=?', (row['id'],)).fetchone():
                        continue
                    placeholder = self.library.root / 'remote-projects' / row['id']
                    if placeholder.is_symlink() or placeholder.parent.is_symlink():
                        _bad('Unsafe cloud project placeholder')
                    placeholder.mkdir(parents=True, exist_ok=True, mode=0o700)
                    options = _options(row['options']) | {'remoteBacked': True, 'restoredSnapshotId': summary['snapshotId']}
                    target.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?)', (row['id'], str(placeholder), row['name'], _json(options), row['created'], row['updated'], row['stats']))
                    new_projects += 1
                identities, path_map = {}, {}
                for row in incoming.execute('SELECT * FROM assets ORDER BY id'):
                    existing = target.execute('SELECT id,sha256,path,project_id FROM assets WHERE id=?', (row['id'],)).fetchone()
                    collision = target.execute('SELECT id,sha256,path,project_id FROM assets WHERE project_id=? AND path=?', (row['project_id'], row['path'])).fetchone()
                    same = existing if existing and existing['sha256'] == row['sha256'] and existing['project_id'] == row['project_id'] else collision if collision and collision['sha256'] == row['sha256'] else None
                    if same:
                        identities[row['id']] = same['id']
                        path_map[(row['project_id'], row['path'])] = same['path']
                        merged += 1
                        continue
                    asset_id, relative = row['id'], row['path']
                    if existing or collision:
                        asset_id = str(uuid.uuid5(uuid.UUID(row['id']), row['sha256']))
                        original = PurePosixPath(relative)
                        relative = '__cloud_versions__/' + row['sha256'][:16] + '/' + row['id'] + '/' + original.name[:200]
                        variant = target.execute('SELECT id,sha256 FROM assets WHERE id=? OR (project_id=? AND path=?)', (asset_id, row['project_id'], relative)).fetchone()
                        if variant:
                            if variant['sha256'] != row['sha256']:
                                _bad('Catalog version identity collision')
                            asset_id = variant['id']; merged += 1
                            identities[row['id']] = asset_id
                            path_map[(row['project_id'], row['path'])] = relative
                            conflicts.append({'assetId': row['id'], 'restoredAssetId': asset_id, 'reason': 'different-content-preserved', 'path': row['path'], 'restoredPath': relative})
                            continue
                        conflicts.append({'assetId': row['id'], 'restoredAssetId': asset_id, 'reason': 'different-content-preserved', 'path': row['path'], 'restoredPath': relative})
                    identities[row['id']] = asset_id
                    path_map[(row['project_id'], row['path'])] = relative
                    values = (asset_id, row['project_id'], relative, row['size'], row['mtime_ns'], 0, 0, 0, row['sha256'], row['md5'], row['kind'], row['extension'], row['mime'], row['metadata'], row['search_text'], row['parser_version'], row['dhash'], row['color'])
                    target.execute('INSERT INTO assets VALUES(' + ','.join('?' for _ in values) + ')', values)
                    target.execute('INSERT INTO asset_storage VALUES(?,1)', (asset_id,))
                    imported += 1
                changed_descriptions = set()
                for row in incoming.execute('SELECT * FROM annotations'):
                    asset_id = identities[row['asset_id']]
                    previous = target.execute('SELECT data FROM annotations WHERE asset_id=? AND source=?', (asset_id, row['source'])).fetchone()
                    if previous:
                        # Preserve local values, but let cloud fill fields absent from a local note.
                        local, cloud = json.loads(previous['data']), json.loads(row['data'])
                        if local.get('sha256') == cloud.get('sha256'):
                            combined = {**cloud, **{k: v for k, v in local.items() if v not in ('', None, [])}}
                            if combined != cloud:
                                changed_descriptions.add(asset_id)
                            if combined != local:
                                self.library._invalidate_description_vectors(target, asset_id)
                            target.execute('UPDATE annotations SET data=? WHERE asset_id=? AND source=?', (_json(combined), asset_id, row['source']))
                        else:
                            changed_descriptions.add(asset_id)
                        continue
                    target.execute('INSERT INTO annotations VALUES(?,?,?,?)', (asset_id, row['source'], row['data'], row['updated']))
                incompatible_spaces = set()
                for row in incoming.execute('SELECT model,dimensions FROM embeddings GROUP BY model,dimensions'):
                    dimensions = {r[0] for r in target.execute('SELECT DISTINCT dimensions FROM embeddings WHERE model=?', (row['model'],))}
                    if dimensions and dimensions != {row['dimensions']}:
                        incompatible_spaces.add(row['model'])
                for row in incoming.execute('SELECT * FROM embeddings'):
                    if row['model'] in incompatible_spaces:
                        skipped_vectors += 1
                        continue
                    if identities[row['asset_id']] in changed_descriptions and row['model'].startswith('asset-metadata-text-'):
                        skipped_vectors += 1
                        continue
                    target.execute('INSERT OR IGNORE INTO embeddings VALUES(?,?,?,?,?)', (identities[row['asset_id']], row['model'], row['dimensions'], row['vector'], row['sha256']))
                for row in incoming.execute('SELECT * FROM identifiers'):
                    relative = path_map.get((row['project_id'], row['target_path']), row['target_path'])
                    value = path_map.get((row['project_id'], row['value']), row['value']) if row['namespace'] == 'path' else row['value']
                    target.execute('INSERT OR IGNORE INTO identifiers VALUES(?,?,?,?,?)', (identities[row['source_id']], row['project_id'], row['namespace'], value, relative))
                source_projects = {r['id']: r['project_id'] for r in incoming.execute('SELECT id,project_id FROM assets')}
                for row in incoming.execute('SELECT * FROM refs'):
                    value = path_map.get((source_projects[row['source_id']], row['value']), row['value']) if row['namespace'] == 'path' else row['value']
                    target.execute('INSERT OR IGNORE INTO refs VALUES(?,?,?,?)', (identities[row['source_id']], row['namespace'], value, row['evidence']))
                for row in incoming.execute('SELECT * FROM locations'):
                    target.execute('INSERT OR REPLACE INTO remote_locations VALUES(?,?,?,?,?,?)', (identities[row['asset_id']], row['provider'], row['fs_id'], row['path'], row['sha256'], row['bytes']))
                for row in incoming.execute('SELECT * FROM query_profiles'):
                    if row['space'] in incompatible_spaces:
                        continue
                    target.execute('INSERT OR IGNORE INTO query_profiles VALUES(?,?,?,?,?,?)', tuple(row))
                for asset_id in set(identities.values()):
                    self.library._refresh_search(target, asset_id)
            return {**summary, 'sha256': digest, 'bytes': count, 'projectsImported': new_projects, 'assetsImported': imported, 'assetsMerged': merged, 'vectorsSkipped': skipped_vectors, 'incompatibleVectorSpaces': sorted(incompatible_spaces), 'conflicts': conflicts}
