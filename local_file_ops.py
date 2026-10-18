"""Explicit, no-overwrite file operations inside registered local projects.

All source traversal uses held directory descriptors and O_NOFOLLOW. A plan is
not authorization by itself: its complete filesystem fingerprints are checked
again before any item is changed. No cloud transfer or model call is performed.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
import threading
import time
import uuid

try:
    from .asset_library import AssetLibraryError, _id, _json, _signature
    from .explorer_browse import _directory_path
except ImportError:
    from asset_library import AssetLibraryError, _id, _json, _signature
    from explorer_browse import _directory_path

_FLAGS = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
_TABLES = ('annotations', 'embeddings', 'identifiers', 'refs', 'remote_locations', 'local_files', 'asset_storage')


def _error(message, status=409):
    return AssetLibraryError(message, status)


def _supported():
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise _error('此平台暂不支持安全的本地文件修改', 501)
    if sys.platform not in ('darwin', 'linux'):
        raise _error('此平台缺少原子无覆盖重命名接口', 501)


@contextmanager
def _absolute_directory(path, *, create=False):
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, _FLAGS)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, _FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, _FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _directory(root, relative=''):
    with _absolute_directory(root) as root_fd:
        descriptor = os.dup(root_fd)
        try:
            for component in relative.split('/') if relative else []:
                child = os.open(component, _FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)


@contextmanager
def _parent(root, relative):
    path = PurePosixPath(relative)
    directory = '' if str(path.parent) == '.' else path.parent.as_posix()
    with _directory(root, directory) as descriptor:
        yield descriptor, path.name


def _fingerprint(info):
    return {'size': info.st_size, 'mtimeNs': info.st_mtime_ns, 'ctimeNs': info.st_ctime_ns,
            'device': info.st_dev, 'inode': info.st_ino, 'mode': stat.S_IFMT(info.st_mode)}


def _tree(parent, name):
    result = []

    def visit(directory, leaf, relative, depth):
        if depth > 128 or len(result) >= 100000:
            raise _error('目录超过安全处理范围，请分批选择较小的目录')
        info = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise _error('所选目录含符号链接或特殊文件，未执行操作')
        entry = {'relative': relative, 'isDir': stat.S_ISDIR(info.st_mode), **_fingerprint(info)}
        result.append(entry)
        if entry['isDir']:
            descriptor = os.open(leaf, _FLAGS, dir_fd=directory)
            try:
                if _fingerprint(os.fstat(descriptor)) != _fingerprint(info):
                    raise _error('目录在读取时发生变化，请重新预览')
                for child in sorted(os.listdir(descriptor)):
                    visit(descriptor, child, '/'.join(filter(None, (relative, child))), depth + 1)
                if _fingerprint(os.fstat(descriptor)) != _fingerprint(info):
                    raise _error('目录在读取时发生变化，请重新预览')
            finally:
                os.close(descriptor)

    visit(parent, name, '', 0)
    return result


def _exists(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _rename_exclusive(source_fd, source, destination_fd, destination):
    """No existence-check/rename gap; the kernel rejects every existing target."""
    _supported()
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        function = getattr(library, 'renameatx_np', None)
        flag = 0x00000004  # RENAME_EXCL
    else:
        function = getattr(library, 'renameat2', None)
        flag = 1  # RENAME_NOREPLACE
    if function is None:
        raise _error('系统未提供原子无覆盖重命名，未修改文件', 501)
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), flag):
        number = ctypes.get_errno()
        if number == errno.EXDEV:
            raise _error('暂不支持跨磁盘移动或回收，请使用复制后核对原件')
        if number in (errno.EEXIST, errno.ENOTEMPTY):
            raise _error('目标已存在，未覆盖任何文件')
        if number in (errno.ENOSYS, errno.ENOTSUP, errno.EINVAL):
            raise _error('此文件系统不支持原子无覆盖重命名，未修改文件', 501)
        raise OSError(number, os.strerror(number))


def _remove_owned(parent, name):
    """Only remove a newly created disposable copy, never a source tree."""
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        descriptor = os.open(name, _FLAGS, dir_fd=parent)
        try:
            # Copied source directories may be read-only. This descriptor is
            # exclusively an application-created rollback copy, never a source.
            os.fchmod(descriptor, (info.st_mode & 0o777) | 0o700)
            for child in os.listdir(descriptor):
                _remove_owned(descriptor, child)
        finally:
            os.close(descriptor)
        os.rmdir(name, dir_fd=parent)
    else:
        os.unlink(name, dir_fd=parent)


def _copy_node(source_fd, source, destination_fd, destination):
    info = os.stat(source, dir_fd=source_fd, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        os.mkdir(destination, 0o700, dir_fd=destination_fd)
        source_child = os.open(source, _FLAGS, dir_fd=source_fd)
        try:
            destination_child = os.open(destination, _FLAGS, dir_fd=destination_fd)
        except BaseException:
            os.close(source_child)
            raise
        try:
            before = _fingerprint(os.fstat(source_child))
            if before != _fingerprint(info):
                raise _error('复制来源已变化')
            for name in sorted(os.listdir(source_child)):
                _copy_node(source_child, name, destination_child, name)
            if _fingerprint(os.fstat(source_child)) != before:
                raise _error('复制期间来源目录已变化')
            os.utime(destination_child, ns=(info.st_atime_ns, info.st_mtime_ns))
            os.fchmod(destination_child, info.st_mode & 0o777)
            os.fsync(destination_child)
        finally:
            os.close(source_child)
            os.close(destination_child)
    elif stat.S_ISREG(info.st_mode):
        source_child = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
        try:
            destination_child = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                        0o600, dir_fd=destination_fd)
        except BaseException:
            os.close(source_child)
            raise
        try:
            if not stat.S_ISREG(os.fstat(source_child).st_mode) or _fingerprint(os.fstat(source_child)) != _fingerprint(info):
                raise _error('复制来源已变化')
            digest = hashlib.sha256()
            while block := os.read(source_child, 1024 * 1024):
                digest.update(block)
                view = memoryview(block)
                while view:
                    view = view[os.write(destination_child, view):]
            os.fsync(destination_child)
            if _fingerprint(os.fstat(source_child)) != _fingerprint(info):
                raise _error('复制期间来源文件已变化')
        finally:
            os.close(source_child)
            os.close(destination_child)
        check = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=destination_fd)
        try:
            actual = hashlib.sha256()
            while block := os.read(check, 1024 * 1024):
                actual.update(block)
            if actual.digest() != digest.digest():
                raise _error('复制结果校验失败，原文件保留')
            os.utime(check, ns=(info.st_atime_ns, info.st_mtime_ns))
            os.fchmod(check, info.st_mode & 0o777)
        finally:
            os.close(check)
    else:
        raise _error('复制来源包含符号链接或特殊文件')


def _moved_tree_matches(before, after):
    """rename changes the root inode's ctime, but must preserve every identity."""
    if len(before) != len(after):
        return False
    for expected, actual in zip(before, after):
        expected, actual = dict(expected), dict(actual)
        if not expected['relative']:
            expected.pop('ctimeNs', None)
            actual.pop('ctimeNs', None)
        if expected != actual:
            return False
    return True


class LocalFileOperator:
    def __init__(self, library, private_root):
        self.library = library
        self.root = Path(os.path.abspath(private_root))
        self.trash = self.root / 'trash'
        self.database = self.root / 'trash.sqlite3'
        self.lock = threading.RLock()
        self.supported = (os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and
                          os.open in os.supports_dir_fd and sys.platform in ('darwin', 'linux'))
        if self.supported:
            with _absolute_directory(self.root, create=True) as descriptor:
                os.fchmod(descriptor, 0o700)
            with _absolute_directory(self.trash, create=True) as descriptor:
                os.fchmod(descriptor, 0o700)
        else:
            # Unsupported clients can still start the workbench and read their
            # ledger. They cannot plan an original-file mutation.
            if self.root.is_symlink() or self.trash.is_symlink():
                raise _error('回收目录不能是符号链接')
            self.root.mkdir(parents=True, exist_ok=True)
            self.trash.mkdir(exist_ok=True)
        if self.database.is_symlink():
            raise _error('回收记录不能是符号链接')
        with self._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS trash_entries(
                id TEXT PRIMARY KEY,source TEXT NOT NULL,name TEXT NOT NULL,deleted REAL NOT NULL,
                state TEXT NOT NULL,manifest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records(trash_id TEXT NOT NULL,seq INTEGER NOT NULL,
                    table_name TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(trash_id,seq));
                CREATE TABLE IF NOT EXISTS blobs(trash_id TEXT NOT NULL,seq INTEGER NOT NULL,
                    column_name TEXT NOT NULL,value BLOB NOT NULL,PRIMARY KEY(trash_id,seq,column_name));''')
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass

    @contextmanager
    def _db(self):
        if self.database.is_symlink():
            raise _error('回收记录不可用')
        if self.database.exists() and not stat.S_ISREG(self.database.lstat().st_mode):
            raise _error('回收记录必须是普通文件')
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _project(self, identifier):
        project = self.library.project(_id(identifier), include_root=True)
        if project['options'].get('remoteBacked'):
            raise _error('云资源库不能修改原件，请先选择本地工程')
        with _absolute_directory(project['root']) as descriptor:
            project['_rootFingerprint'] = {'device': os.fstat(descriptor).st_dev,
                                           'inode': os.fstat(descriptor).st_ino}
        return project

    def _idle(self, db):
        if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
            raise _error('请等待资源扫描结束后再修改文件')

    def _indexed(self, db, project_id, path):
        prefix = path + '/'
        return db.execute('SELECT * FROM assets WHERE project_id=? AND (path=? OR substr(path,1,?)=?)',
                          (project_id, path, len(prefix), prefix)).fetchall()

    def _source(self, value):
        if not isinstance(value, dict) or set(value) != {'projectId', 'path'}:
            raise _error('每项必须包含已注册的 projectId 和相对 path', 400)
        project = self._project(value['projectId'])
        path = _directory_path(value['path'])
        if not path:
            raise _error('不能复制、移动或删除整个工程根目录')
        absolute = Path(project['root']) / path
        if any(protected == absolute or protected.is_relative_to(absolute)
               for protected in (self.root, self.library.root)):
            raise _error('不能操作包含资源索引或回收站的目录')
        with _parent(project['root'], path) as (parent, name):
            manifest = _tree(parent, name)
        return project, path, manifest

    def plan(self, operation, items, destination=None, name=None):
        _supported()
        try:
            return self._plan(operation, items, destination, name)
        except OSError:
            raise _error('文件或目录不可用、包含符号链接或权限不足，请重新选择') from None

    def _plan(self, operation, items, destination=None, name=None):
        with self.lock, self.library.lock, self.library._db() as db:
            self._idle(db)
            if operation not in ('copy', 'move', 'rename', 'trash'):
                raise _error('不支持的文件操作', 400)
            if not isinstance(items, list) or not 1 <= len(items) <= 100:
                raise _error('每次选择 1 到 100 个文件或目录', 400)
            if operation == 'rename':
                if not isinstance(name, str) or not name or len(name) > 240:
                    raise _error('重命名需要有效的新名称或批量名称模板', 400)
            elif name is not None:
                raise _error('只有重命名可以指定新名称', 400)
            if operation in ('copy', 'move'):
                if not isinstance(destination, dict) or set(destination) != {'projectId', 'path'}:
                    raise _error('请选择已存在的目标目录', 400)
                target_project = self._project(destination['projectId'])
                target_directory = _directory_path(destination['path'])
                with _directory(target_project['root'], target_directory) as target_fd:
                    target_fingerprint = _fingerprint(os.fstat(target_fd))
            elif destination is not None:
                raise _error('此操作不接受目标目录', 400)
            original_items = json.loads(json.dumps(items))
            resolved, explicit = [], {}
            for value in items:
                project, relative, manifest = self._source(value)
                key = (project['id'], relative)
                if key in explicit:
                    raise _error('不能重复选择相同文件或目录')
                entry = ({'projectId': project['id'], 'path': relative}, project, relative, manifest)
                resolved.append(entry)
                explicit[key] = entry
            logical = [entry for entry in resolved if not (
                entry[2].endswith('.meta') and (entry[1]['id'], entry[2][:-5]) in explicit
                and not entry[2][:-5].lower().endswith('.meta'))]
            expanded, rename_targets = [], {}
            for index, (value, project, relative, manifest) in enumerate(logical, 1):
                key = (project['id'], relative)
                if operation == 'rename':
                    try:
                        try:
                            from .file_name_patterns import renamed_name
                        except ImportError:
                            from file_name_patterns import renamed_name
                        rename_targets[key] = renamed_name(name, PurePosixPath(relative).name,
                                                           manifest[0]['isDir'], index, len(logical))
                    except ValueError as error:
                        raise _error(str(error), 400) from None
                expanded.append((value, None))
                if relative.lower().endswith('.meta'):
                    continue
                companion = relative + '.meta'
                companion_value = {'projectId': project['id'], 'path': companion}
                if (project['id'], companion) in explicit:
                    if explicit[(project['id'], companion)][3][0]['isDir']:
                        raise _error('伴随 .meta 必须是普通文件')
                    expanded.append((companion_value, value))
                else:
                    with _parent(project['root'], companion) as (parent_fd, companion_name):
                        if _exists(parent_fd, companion_name):
                            info = os.stat(companion_name, dir_fd=parent_fd, follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode):
                                raise _error('伴随 .meta 不是安全的普通文件，未执行操作')
                            expanded.append((companion_value, value))
            planned, manifests, projects, absolutes, targets, identities = [], [], {}, [], set(), set()
            for value, companion_of in expanded:
                project, path, manifest = self._source(value)
                projects[project['id']] = project
                absolute = Path(project['root']) / path
                if any(absolute == other or absolute.is_relative_to(other) or other.is_relative_to(absolute) for other in absolutes):
                    raise _error('不能重复选择文件或同时选择父目录和子项')
                absolutes.append(absolute)
                identity = (manifest[0]['device'], manifest[0]['inode'])
                if identity in identities:
                    raise _error('不能重复选择指向相同原件的路径')
                identities.add(identity)
                indexed = self._indexed(db, project['id'], path)
                by_path = {path + ('/' + entry['relative'] if entry['relative'] else ''): entry for entry in manifest if not entry['isDir']}
                for row in indexed:
                    found = by_path.get(row['path'])
                    if not found or tuple(found[key] for key in ('size', 'mtimeNs', 'ctimeNs', 'device', 'inode')) != tuple(row[key] for key in ('size', 'mtime_ns', 'ctime_ns', 'device', 'inode')):
                        raise _error('所选文件与索引不同，请先重新扫描该工程')
                    storage = db.execute('SELECT remote_only FROM asset_storage WHERE asset_id=?', (row['id'],)).fetchone()
                    if storage and storage[0]:
                        raise _error('云端或缓存资源不能作为本地原件修改')
                destination_value = None
                if operation != 'trash':
                    if operation == 'rename':
                        target_project = project
                        target_directory = '' if str(PurePosixPath(path).parent) == '.' else str(PurePosixPath(path).parent)
                    if operation == 'rename':
                        source_key = (companion_of['projectId'], companion_of['path']) if companion_of else (project['id'], path)
                        target_name = rename_targets[source_key] + ('.meta' if companion_of else '')
                    else:
                        target_name = PurePosixPath(path).name
                    target_path = '/'.join(filter(None, (target_directory, target_name)))
                    target_absolute = Path(target_project['root']) / target_path
                    if manifest[0]['isDir'] and (target_absolute == absolute or target_absolute.is_relative_to(absolute)):
                        raise _error('不能将目录复制或移动到自身内部')
                    key = str(target_absolute).casefold()
                    if key in targets:
                        raise _error('批量操作的目标名称发生冲突，请分开处理')
                    targets.add(key)
                    with _parent(target_project['root'], target_path) as (target_fd, target_name):
                        if _exists(target_fd, target_name) or self._indexed(db, target_project['id'], target_path):
                            raise _error('目标已存在或已被索引，未覆盖任何文件')
                        if operation != 'copy' and manifest[0]['device'] != os.fstat(target_fd).st_dev:
                            raise _error('暂不支持跨磁盘移动，请使用复制')
                    destination_value = {'projectId': target_project['id'], 'path': target_path}
                    projects[target_project['id']] = target_project
                else:
                    with _absolute_directory(self.trash) as trash_fd:
                        if manifest[0]['device'] != os.fstat(trash_fd).st_dev:
                            raise _error('此目录与回收站不在同一磁盘，暂不能安全回收')
                files = [entry for entry in manifest if not entry['isDir']]
                planned.append({'name': PurePosixPath(path).name, 'source': {'projectId': project['id'], 'path': path},
                                'destination': destination_value, 'isDir': manifest[0]['isDir'],
                                'fileCount': len(files), 'bytes': sum(entry['size'] for entry in files),
                                **({'companionOf': companion_of} if companion_of else {})})
                manifests.append(manifest)
            arguments = {'operation': operation, 'items': original_items, 'destination': destination, 'name': name}
            return {'scope': 'local', 'operation': operation, 'items': planned,
                    'fileCount': sum(item['fileCount'] for item in planned), 'bytes': sum(item['bytes'] for item in planned),
                    '_arguments': json.loads(json.dumps(arguments)), '_manifests': manifests,
                    '_projects': projects, '_targetFingerprint': target_fingerprint if operation in ('copy', 'move') else None,
                    '_version': 1}

    def _snapshot(self, db, project_id, path):
        records = []
        for row in self._indexed(db, project_id, path):
            records.append(('assets', dict(row)))
            for table in _TABLES:
                column = 'source_id' if table in ('identifiers', 'refs') else 'asset_id'
                records.extend((table, dict(value)) for value in db.execute('SELECT * FROM ' + table + ' WHERE ' + column + '=?', (row['id'],)))
        return records

    def _save_trash(self, identifier, item, manifest, records):
        with self._db() as db:
            db.execute('INSERT INTO trash_entries VALUES(?,?,?,?,?,?)',
                       (identifier, _json(item['source']), item['name'], time.time(), 'prepared', _json(manifest)))
            for index, (table, row) in enumerate(records):
                clean = {key: value for key, value in row.items() if not isinstance(value, bytes)}
                db.execute('INSERT INTO records VALUES(?,?,?,?)', (identifier, index, table, _json(clean)))
                for key, value in row.items():
                    if isinstance(value, bytes):
                        db.execute('INSERT INTO blobs VALUES(?,?,?,?)', (identifier, index, key, value))

    def _load_records(self, identifier):
        with self._db() as db:
            records = []
            for record in db.execute('SELECT * FROM records WHERE trash_id=? ORDER BY seq', (identifier,)):
                if record['table_name'] not in ('assets', *_TABLES):
                    raise _error('回收元数据表无效')
                row = json.loads(record['data'])
                row.update({blob['column_name']: blob['value'] for blob in db.execute('SELECT column_name,value FROM blobs WHERE trash_id=? AND seq=?', (identifier, record['seq']))})
                records.append((record['table_name'], row))
            return records

    def _directory_rows(self, db, project_id, path, manifest=None):
        if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='asset_directories'").fetchone():
            return
        prefix = path + '/'
        db.execute('DELETE FROM asset_directories WHERE project_id=? AND (path=? OR substr(path,1,?)=?)',
                   (project_id, path, len(prefix), prefix))
        for entry in manifest or []:
            if entry['isDir']:
                relative = path + ('/' + entry['relative'] if entry['relative'] else '')
                db.execute('INSERT OR REPLACE INTO asset_directories(project_id,path,mtime_ns) VALUES(?,?,?)',
                           (project_id, relative, entry['mtimeNs']))

    def _reindex(self, db, item, records, operation, manifest):
        source, destination = item['source'], item['destination']
        project = self._project(destination['projectId'])
        old_assets = {row['path']: row for table, row in records if table == 'assets'}
        related = {}
        for table, row in records:
            if table != 'assets':
                identifier = row.get('asset_id', row.get('source_id'))
                related.setdefault(identifier, []).append((table, row))
        changed_ids = []
        for entry in manifest:
            if entry['isDir']:
                continue
            suffix = '/' + entry['relative'] if entry['relative'] else ''
            old = old_assets.get(source['path'] + suffix)
            target_path = destination['path'] + suffix
            analyzed = self.library._analyze(project['root'], target_path, project['options'], project['id'])
            if old and old['sha256'] != analyzed['sha256']:
                raise _error('操作期间原件内容已变化，未接受变更后的文件')
            if old and operation != 'copy':
                analyzed['id'] = old['id']
                db.execute('UPDATE assets SET project_id=?,path=? WHERE id=?', (project['id'], target_path, old['id']))
                db.execute('DELETE FROM local_files WHERE asset_id=?', (old['id'],))
            else:
                occupied = db.execute('SELECT project_id,path FROM assets WHERE id=?', (analyzed['id'],)).fetchone()
                if occupied and (occupied['project_id'] != project['id'] or occupied['path'] != target_path):
                    analyzed['id'] = str(uuid.uuid4())
            self.library._upsert(db, project['id'], analyzed)
            if old and old['sha256'] == analyzed['sha256']:
                old_metadata = json.loads(old['metadata'])
                if old_metadata.get('ocr', {}).get('status') == 'analyzed':
                    analyzed['metadata']['ocr'] = old_metadata['ocr']
                    db.execute('UPDATE assets SET metadata=? WHERE id=?', (_json(analyzed['metadata']), analyzed['id']))
                if operation == 'copy':
                    for table, original in related.get(old['id'], []):
                        if table not in ('annotations', 'embeddings', 'remote_locations'):
                            continue
                        copied = {**original, 'asset_id': analyzed['id']}
                        columns = list(copied)
                        db.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', list(copied.values()))
                self.library._refresh_search(db, analyzed['id'])
            changed_ids.append(analyzed['id'])
        if operation != 'copy':
            self._directory_rows(db, source['projectId'], source['path'])
        self._directory_rows(db, destination['projectId'], destination['path'], manifest)
        return changed_ids

    def _delete_index(self, db, records, item):
        for table, row in records:
            if table == 'assets':
                if self.library.fts:
                    db.execute('DELETE FROM asset_search WHERE id=?', (row['id'],))
                db.execute('DELETE FROM assets WHERE id=?', (row['id'],))
        self._directory_rows(db, item['source']['projectId'], item['source']['path'])

    def _refresh_rollback_signatures(self, project, records):
        # Moving a file out and back updates its ctime even when every byte was
        # preserved. Refresh only signatures, leaving notes/vectors untouched.
        with self.library._db() as db:
            for table, row in records:
                if table != 'assets':
                    continue
                analyzed = self.library._analyze(project['root'], row['path'], project['options'], project['id'])
                if analyzed['sha256'] != row['sha256']:
                    raise _error('回退位置的内容已变化，请重新扫描')
                db.execute('UPDATE assets SET size=?,mtime_ns=?,ctime_ns=?,device=?,inode=? WHERE id=?',
                           (*analyzed['stat'], row['id']))

    def _execute_item(self, item, manifest, operation):
        project = self._project(item['source']['projectId'])
        path = item['source']['path']
        trash_id = str(uuid.uuid4()) if operation == 'trash' else None
        with _parent(project['root'], path) as (source_fd, source_name):
            if _tree(source_fd, source_name) != manifest:
                raise _error('文件在操作前发生变化，请重新预览')
            target = item.get('destination')
            target_root = self.trash if operation == 'trash' else Path(self._project(target['projectId'])['root'])
            target_path = trash_id if operation == 'trash' else target['path']
            with _parent(target_root, target_path) as (target_fd, target_name):
                temporary = '.devhelper-copy-' + uuid.uuid4().hex if operation == 'copy' else None
                mutated, snapshot, copied_manifest, committed = False, None, None, False
                try:
                    with self.library._db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        snapshot = self._snapshot(db, project['id'], path)
                        if operation == 'trash':
                            self._save_trash(trash_id, item, manifest, snapshot)
                        if operation == 'copy':
                            try:
                                _copy_node(source_fd, source_name, target_fd, temporary)
                                if _tree(source_fd, source_name) != manifest:
                                    raise _error('复制期间原件已变化，未完成此项')
                                _rename_exclusive(target_fd, temporary, target_fd, target_name)
                            finally:
                                if _exists(target_fd, temporary):
                                    _remove_owned(target_fd, temporary)
                        else:
                            _rename_exclusive(source_fd, source_name, target_fd, target_name)
                        mutated = True
                        copied_manifest = _tree(target_fd, target_name)
                        if operation != 'copy' and not _moved_tree_matches(manifest, copied_manifest):
                            raise _error('原件在移动时发生变化，未更新资源索引')
                        if operation == 'trash':
                            self._delete_index(db, snapshot, item)
                            changed_ids = []
                        else:
                            changed_ids = self._reindex(db, item, snapshot, operation, copied_manifest)
                    committed = True
                    if operation == 'trash':
                        with self._db() as trash_db:
                            trash_db.execute("UPDATE trash_entries SET state='active' WHERE id=?", (trash_id,))
                    return {**item, 'state': 'completed', 'assetIds': changed_ids,
                            **({'trashId': trash_id} if trash_id else {})}
                except Exception as error:
                    if committed:
                        return {**item, 'state': 'completed', 'assetIds': changed_ids,
                                'warning': '文件及索引已完成，回收记录状态更新失败；请保留回收 ID',
                                **({'trashId': trash_id} if trash_id else {})}
                    recovery = None
                    if mutated:
                        try:
                            if operation == 'copy':
                                if copied_manifest is None or _tree(target_fd, target_name) != copied_manifest:
                                    raise _error('复制目标已被其他程序修改，保留副本')
                                _remove_owned(target_fd, target_name)
                            else:
                                _rename_exclusive(target_fd, target_name, source_fd, source_name)
                                self._refresh_rollback_signatures(project, snapshot)
                        except Exception:
                            recovery = {'location': {'trashId': trash_id} if trash_id else target,
                                        'message': '文件保留在此位置，自动回退未完成，请先核对，不要重复操作'}
                    if recovery:
                        return {**item, 'state': 'failed', 'error': str(error)[:300], 'filesystemChanged': True,
                                'recovery': recovery, **({'trashId': trash_id} if trash_id else {})}
                    if trash_id:
                        with self._db() as trash_db:
                            trash_db.execute("UPDATE trash_entries SET state='rolled-back' WHERE id=?", (trash_id,))
                    raise

    def execute(self, plan):
        with self.lock, self.library.lock:
            try:
                if not isinstance(plan, dict) or plan.get('_version') != 1:
                    raise _error('文件操作计划无效，请重新预览', 400)
                fresh = self.plan(**plan['_arguments'])
                if fresh != plan:
                    raise _error('文件或目录自预览后发生变化，请重新预览')
            except Exception as error:
                entries = plan.get('items', []) if isinstance(plan, dict) else []
                return {'state': 'failed', 'operation': plan.get('operation') if isinstance(plan, dict) else None,
                        'items': [{**item, 'state': 'failed' if index == 0 else 'skipped', 'error': str(error)[:300]}
                                  for index, item in enumerate(entries)], 'error': str(error)[:300]}
            results = []
            failed = False
            for item, manifest in zip(plan['items'], plan['_manifests']):
                if failed:
                    results.append({**item, 'state': 'skipped', 'error': '前一项未完成，未继续修改此项'})
                    continue
                try:
                    result = self._execute_item(item, manifest, plan['operation'])
                except Exception as error:
                    result = {**item, 'state': 'failed', 'error': str(error)[:300]}
                failed = result['state'] != 'completed'
                results.append(result)
            return {'operation': plan['operation'], 'state': 'partial' if failed and any(item['state'] == 'completed' for item in results) else 'failed' if failed else 'completed',
                    'items': results, 'completedCount': sum(item['state'] == 'completed' for item in results),
                    'failedCount': sum(item['state'] == 'failed' for item in results)}

    def list_trash(self):
        with self.lock, self._db() as db:
            return {'items': [{'trashId': row['id'], 'source': json.loads(row['source']), 'name': row['name'],
                               'deletedAt': row['deleted'], 'state': row['state']}
                              for row in db.execute("SELECT * FROM trash_entries WHERE state IN ('active','prepared') ORDER BY deleted DESC")]}

    def restore(self, trash_id):
        _supported()
        _id(trash_id)
        with self.lock, self.library.lock, self._db() as trash_db:
            record = trash_db.execute('SELECT * FROM trash_entries WHERE id=?', (trash_id,)).fetchone()
            if record is None or record['state'] not in ('active', 'prepared'):
                raise _error('回收项目不存在或已恢复', 404)
            source = json.loads(record['source'])
            if not isinstance(source, dict) or set(source) != {'projectId', 'path'} or not _directory_path(source.get('path')) or _directory_path(source['path']) != source['path']:
                raise _error('回收记录中的原位置无效')
            project = self._project(source['projectId'])
            records = self._load_records(trash_id)
            with self.library._db() as db:
                self._idle(db)
                for table, row in records:
                    if table == 'assets' and db.execute('SELECT 1 FROM assets WHERE id=? OR (project_id=? AND path=?)', (row['id'], row['project_id'], row['path'])).fetchone():
                        raise _error('原资源 ID 或路径已被占用，未覆盖任何文件')
                if self._indexed(db, project['id'], source['path']):
                    raise _error('原位置已有索引，未覆盖任何文件')
            with _absolute_directory(self.trash) as trash_fd, _parent(project['root'], source['path']) as (target_fd, target_name):
                if _exists(target_fd, target_name):
                    raise _error('原位置已有文件，未覆盖任何文件')
                # Validate regular files and directories before moving any bytes.
                manifest = _tree(trash_fd, trash_id)
                if not _moved_tree_matches(json.loads(record['manifest']), manifest):
                    raise _error('回收文件已发生变化，未恢复到原位置')
                _rename_exclusive(trash_fd, trash_id, target_fd, target_name)
                committed = False
                try:
                    with self.library._db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        for table, row in records:
                            columns = list(row)
                            db.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', list(row.values()))
                        item = {'source': source, 'destination': source}
                        ids = self._reindex(db, item, records, 'move', manifest)
                    committed = True
                    trash_db.execute("UPDATE trash_entries SET state='restored' WHERE id=?", (trash_id,))
                    return {'state': 'completed', 'trashId': trash_id, 'source': source,
                            'name': record['name'], 'assetIds': ids}
                except Exception:
                    if committed:
                        return {'state': 'completed', 'trashId': trash_id, 'source': source,
                                'name': record['name'], 'assetIds': ids,
                                'warning': '文件及索引已恢复，回收记录状态更新失败'}
                    _rename_exclusive(target_fd, target_name, trash_fd, trash_id)
                    raise
