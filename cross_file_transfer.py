"""Reviewed binary transfers between registered local projects and scoped cloud.

Copies become usable before a move cleans up any source. Remote digest fields
are opaque provider tokens; actual downloaded bytes are checked with SHA256.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import threading
import time
import uuid

try:
    from .asset_library import AssetLibraryError, _read_source
    from .baidu_drive import BaiduDriveError
    from .explorer_browse import _directory_path
    from .local_file_ops import (_absolute_directory, _directory, _parent, _tree,
        _copy_node, _exists, _fingerprint, _rename_exclusive, _remove_owned)
except ImportError:
    from asset_library import AssetLibraryError, _read_source
    from baidu_drive import BaiduDriveError
    from explorer_browse import _directory_path
    from local_file_ops import (_absolute_directory, _directory, _parent, _tree,
        _copy_node, _exists, _fingerprint, _rename_exclusive, _remove_owned)


class _Stopped(AssetLibraryError):
    def __init__(self, message='传输已取消，尚未清理的源文件保留', *, unknown=False):
        super().__init__(message, 409)
        self.unknown = unknown


class CrossFileTransfer:
    def __init__(self, workspace, local_operator, netdisk_operator, private_root):
        self.workspace, self.library, self.drive = workspace, workspace.library, workspace.drive
        self.local, self.netdisk = local_operator, netdisk_operator
        self.root = Path(private_root).absolute()
        with _absolute_directory(self.root, create=True) as descriptor:
            os.fchmod(descriptor, 0o700)
        self.lock = threading.RLock()
        self._callback = None
        self._cancelled = None
        self._last_progress = 0.
        self._bytes = 0
        self._completed_files = 0
        self._retain_staging = False

    @staticmethod
    def _check_items(items):
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise AssetLibraryError('跨设备传输每批请选择 1–100 个文件或目录', 400)

    def _local_sources(self, items):
        self._check_items(items)
        expanded, explicit = list(copy.deepcopy(items)), set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {'projectId', 'path'}:
                raise AssetLibraryError('本地来源需要 projectId 和相对 path', 400)
            explicit.add((item['projectId'], item['path']))
        for item in items:
            project, relative, _ = self.local._source(item)
            if relative.lower().endswith('.meta') or (project['id'], relative + '.meta') in explicit:
                continue
            with _parent(project['root'], relative + '.meta') as (parent, name):
                if _exists(parent, name):
                    expanded.append({'projectId': project['id'], 'path': relative + '.meta'})
        sources, paths = [], []
        with self.local.lock, self.library.lock, self.library._db() as db:
            self.local._idle(db)
            for item in expanded:
                project, relative, manifest = self.local._source(item)
                absolute = Path(project['root']) / relative
                if any(absolute == old or absolute.is_relative_to(old) or old.is_relative_to(absolute) for old in paths):
                    raise AssetLibraryError('不能重复选择项目或同时选择父目录与内部文件', 400)
                paths.append(absolute)
                records = self.local._snapshot(db, project['id'], relative)
                measured = {relative + ('/' + x['relative'] if x['relative'] else ''): x for x in manifest if not x['isDir']}
                for table, row in records:
                    if table == 'assets':
                        entry = measured.get(row['path'])
                        if not entry or tuple(entry[k] for k in ('size', 'mtimeNs', 'ctimeNs', 'device', 'inode')) != tuple(row[k] for k in ('size', 'mtime_ns', 'ctime_ns', 'device', 'inode')):
                            raise AssetLibraryError('来源与索引不同，请先重新扫描该工程', 409)
                sources.append({'source': item, 'project': project, 'manifest': manifest,
                                'records': records, 'name': PurePosixPath(relative).name})
        return sources

    def _remote_tree(self, source):
        entries = []
        def visit(fingerprint, relative, depth):
            if depth > 128 or len(entries) >= 100000:
                raise AssetLibraryError('目录过大，请分批选择较小的目录', 409)
            entries.append({'relative': relative, 'fingerprint': fingerprint})
            if fingerprint['isDirectory']:
                for entry in self.netdisk._list(fingerprint['path']):
                    actual = self.netdisk._entry(entry['path'], entry.get('fs_id'))
                    child = '/'.join(filter(None, (relative, PurePosixPath(actual['path']).name)))
                    if _directory_path(child) != child:
                        raise AssetLibraryError('云端名称不能安全映射到本地目录', 409)
                    visit(actual, child, depth + 1)
                if self.netdisk._entry(fingerprint['path'], fingerprint['fsId']) != fingerprint:
                    raise AssetLibraryError('云端目录在读取时发生变化，请重新预览', 409)
        visit(source, '', 0)
        return entries

    def _known(self, source):
        result = {}
        for row in self.netdisk._rows(source):
            try:
                asset = self.library.asset(row['asset_id'])
                if asset['sha256'] != row['sha256'] or asset['bytes'] != row['bytes']:
                    continue
                key = str(row['fs_id'])
                value = {'assetId': asset['id'], 'sha256': asset['sha256'], 'bytes': asset['bytes'],
                         'projectId': asset['projectId'], 'path': asset['path']}
                if key in result and result[key] and result[key]['sha256'] != value['sha256']:
                    result[key] = None
                elif key not in result:
                    result[key] = value
            except (ValueError, KeyError):
                continue
        return result

    def plan(self, source_scope, destination_scope, operation, items, destination):
        if {source_scope, destination_scope} != {'local', 'netdisk'} or source_scope == destination_scope:
            raise AssetLibraryError('跨设备传输仅支持本地与百度网盘互传', 400)
        if operation not in ('copy', 'move'):
            raise AssetLibraryError('跨设备传输仅支持复制或移动', 400)
        self._check_items(items)
        with self.lock:
            account = self.netdisk._identity()
            if source_scope == 'local':
                if not isinstance(destination, dict) or set(destination) != {'path'}:
                    raise AssetLibraryError('请选择已存在的应用网盘目标目录', 400)
                sources = self._local_sources(items)
                directory, listing = self.netdisk._directory(destination['path'])
                existing = {x['path'] for x in listing}
                destination_state = directory
                for source in sources:
                    source['target'] = self.drive.remote_path(directory['path'] + '/' + source['name'], file=True)
                    if source['target'] in existing:
                        raise AssetLibraryError('目标已有同名项目，不会覆盖或自动改名', 409)
            else:
                if not isinstance(destination, dict) or set(destination) != {'projectId', 'path'}:
                    raise AssetLibraryError('请选择已注册本地工程中的目标目录', 400)
                checked = self.netdisk.plan('trash', items)
                sources = []
                for fingerprint in checked['_sources']:
                    sources.append({'source': {'path': fingerprint['path'], 'fsId': fingerprint['fsId']},
                        'name': PurePosixPath(fingerprint['path']).name, 'fingerprint': fingerprint,
                        'tree': self._remote_tree(fingerprint), 'known': self._known(fingerprint)})
                project = self.local._project(destination['projectId'])
                relative = _directory_path(destination['path'])
                with _directory(project['root'], relative) as parent:
                    destination_state = {'project': project, 'path': relative, 'fingerprint': _fingerprint(os.fstat(parent))}
                    for source in sources:
                        source['target'] = '/'.join(filter(None, (relative, source['name'])))
                        if _exists(parent, source['name']):
                            raise AssetLibraryError('本地目标已有同名项目，不会覆盖', 409)
                        with self.library._db() as db:
                            if self.local._indexed(db, project['id'], source['target']):
                                raise AssetLibraryError('目标路径已有资源索引，请核对后重试', 409)
            names = [source['name'].casefold() for source in sources]
            if len(set(names)) != len(names):
                raise AssetLibraryError('所选来源生成同名目标，请分批传输', 409)
            public, total, count = [], 0, 0
            for source in sources:
                if source_scope == 'local':
                    files = [x for x in source['manifest'] if not x['isDir']]
                    size = sum(x['size'] for x in files)
                    is_dir = source['manifest'][0]['isDir']
                    target = {'path': source['target']}
                else:
                    files = [x for x in source['tree'] if not x['fingerprint']['isDirectory']]
                    size = sum(x['fingerprint']['bytes'] for x in files)
                    is_dir = source['fingerprint']['isDirectory']
                    target = {'projectId': destination['projectId'], 'path': source['target']}
                total += size; count += len(files)
                public.append({'name': source['name'], 'source': source['source'], 'destination': target,
                               'isDirectory': is_dir, 'fileCount': len(files), 'bytes': size})
            return {'scope': 'transfer', 'sourceScope': source_scope, 'destinationScope': destination_scope,
                'operation': operation, 'items': public, 'fileCount': count, 'bytes': total,
                'totalBytes': total * (2 if source_scope == 'local' and operation == 'move' else 1),
                'overwrite': False, 'cleanupAfterVerification': operation == 'move',
                '_version': 1, '_account': account, '_sources': sources, '_destination': destination_state,
                '_arguments': copy.deepcopy({'source_scope': source_scope, 'destination_scope': destination_scope,
                    'operation': operation, 'items': items, 'destination': destination})}

    def _check(self):
        if self._cancelled and self._cancelled():
            raise _Stopped()

    def _emit(self, phase, *, force=False, **fields):
        now = time.monotonic()
        if not force and now - self._last_progress < .2:
            return
        self._last_progress = now
        if self._callback:
            try:
                self._callback({'phase': phase, 'bytesTransferred': self._bytes,
                    'totalBytes': self._total, 'fileCount': self._count,
                    'completedFiles': self._completed_files, **fields})
            except Exception:
                # A failed UI observer must never replay or interrupt an
                # uncertain provider mutation behind the caller's back.
                pass

    def _wait(self, job, phase, size, current_file):
        self._active_transfer_job = job['jobId']
        base, previous = self._bytes, 0
        deadline = time.monotonic() + 1800
        activity = (job.get('bytesTransferred'), job.get('phase'), job.get('state'))
        cancel_deadline, cancel_requested = None, False
        while True:
            amount = min(size, max(previous, int(job.get('bytesTransferred', 0) or 0)))
            previous = amount; self._bytes = base + amount
            self._emit(phase, currentFile=current_file, transferJobId=job['jobId'])
            if job.get('state') not in ('queued', 'running'):
                break
            stopping = bool(self._cancelled and self._cancelled()) or time.monotonic() >= deadline
            if stopping and not cancel_requested:
                cancel_requested = True; cancel_deadline = time.monotonic() + 3
                try:
                    self.drive.cancel(job['jobId'])
                except Exception:
                    pass
            if cancel_deadline and time.monotonic() >= cancel_deadline:
                self._retain_staging = True
                raise _Stopped('传输任务尚未确认停止，源文件保留；请核对后台传输任务', unknown=True)
            time.sleep(.05 if cancel_requested else .1)
            job = self.drive.job(job['jobId'])
            current_activity = (job.get('bytesTransferred'), job.get('phase'), job.get('state'))
            if current_activity != activity:
                activity = current_activity
                deadline = time.monotonic() + 1800
        if cancel_requested or job.get('state') == 'cancelled':
            raise _Stopped()
        if job.get('state') != 'completed':
            raise AssetLibraryError('文件传输未完成，源文件全部保留；请查看传输任务', 409)
        self._bytes = base + size
        self._emit(phase, force=True, currentFile=current_file, transferJobId=job['jobId'])
        self._active_transfer_job = None
        return job.get('result') or {}

    @staticmethod
    def _digest(path):
        digest, count = hashlib.sha256(), 0
        with _read_source(path.parent, path.name) as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block); count += len(block)
        return digest.hexdigest(), count

    def _validate_sources(self, plan):
        if self.netdisk._identity() != plan['_account']:
            raise AssetLibraryError('网盘账号或应用目录已变化，源文件保留', 409)
        for source in plan['_sources']:
            self._check()
            if plan['sourceScope'] == 'local':
                project, _, manifest = self.local._source(source['source'])
                if project != source['project'] or manifest != source['manifest']:
                    raise AssetLibraryError('本地来源在预览后发生变化，源文件保留', 409)
            else:
                current = self.netdisk._entry(source['source']['path'], source['source']['fsId'])
                if current != source['fingerprint'] or self._remote_tree(current) != source['tree']:
                    raise AssetLibraryError('云端来源在传输期间发生变化，源文件保留', 409)

    def _stage_local(self, source, stage, index):
        name = 'node-' + str(index)
        with _parent(source['project']['root'], source['source']['path']) as (parent, leaf), _absolute_directory(stage) as target:
            if _tree(parent, leaf) != source['manifest']:
                raise AssetLibraryError('来源在复制前发生变化', 409)
            _copy_node(parent, leaf, target, name)
            if _tree(parent, leaf) != source['manifest']:
                raise AssetLibraryError('来源在复制期间发生变化', 409)
        return stage / name

    def _upload(self, plan, source, staged, outcome):
        receipts = []
        for entry in source['manifest']:
            self._check()
            relative = entry['relative']
            target = source['target'] + ('/' + relative if relative else '')
            local_path = staged / relative if relative else staged
            if entry['isDir']:
                created = self.drive.mkdir(target)
                actual = self.netdisk._entry(target, created.get('fs_id') or created.get('fsId'))
                if not actual['isDirectory']:
                    raise AssetLibraryError('云端未确认所建目录', 409)
                outcome.setdefault('createdDirectories', []).append({'path': target, 'fsId': actual['fsId']})
                outcome.update(targetState='partial', targetVerified=False)
                if not relative:
                    outcome['targetFsId'] = outcome['destinationFsId'] = actual['fsId']
                continue
            digest, size = self._digest(local_path)
            result = self._wait(self.drive.start_upload(local_path, target, name=PurePosixPath(target).name,
                conflict_policy='fail'), 'uploading', size, target)
            if (result.get('remotePath') != target or result.get('pathConfirmed') is not True or
                    result.get('bytes') != size or result.get('sha256') != digest or not result.get('fsId')):
                raise AssetLibraryError('上传回执未确认精确目标、摘要、大小与 ID；源文件保留', 409)
            actual = self.netdisk._entry(target, result['fsId'])
            if actual['isDirectory'] or actual['bytes'] != size:
                raise AssetLibraryError('云端副本与上传计划不一致', 409)
            if plan['operation'] == 'move':
                verify = staged.parent / (uuid.uuid4().hex + '.verify')
                try:
                    self._wait(self.drive.start_download(actual['fsId'], verify, expected_sha256=digest),
                               'verifying-upload', size, target)
                    if self._digest(verify) != (digest, size):
                        raise AssetLibraryError('云端回读 SHA256 校验失败，源文件保留', 409)
                finally:
                    if not self._retain_staging:
                        verify.unlink(missing_ok=True)
                if self.netdisk._entry(target, actual['fsId']) != actual:
                    raise AssetLibraryError('云端副本在校验期间发生变化，源文件保留', 409)
            relative_source = source['source']['path'] + ('/' + relative if relative else '')
            receipts.append({'relative': relative, 'path': target, 'fsId': actual['fsId'], 'bytes': size,
                             'sha256': digest, 'sourcePath': relative_source, '_fingerprint': actual,
                             'verification': 'sha256-readback' if plan['operation'] == 'move' else 'upload-receipt'})
            if not relative:
                outcome['targetFsId'] = outcome['destinationFsId'] = actual['fsId']
            outcome['files'] = [{k: v for k, v in item.items() if not k.startswith('_') and k != 'sourcePath'} for item in receipts]
            outcome.update(targetState='partial' if outcome['isDirectory'] else 'available',
                           targetVerified=not outcome['isDirectory'])
            self._completed_files += 1
            self._emit('uploading', force=True, currentFile=target)
        outcome['files'] = [{k: v for k, v in item.items() if not k.startswith('_') and k != 'sourcePath'} for item in receipts]
        return receipts

    def _download(self, source, stage, index, outcome):
        root = stage / ('node-' + str(index))
        receipts = []
        for node in source['tree']:
            self._check()
            before, relative = node['fingerprint'], node['relative']
            path = root / relative if relative else root
            if before['isDirectory']:
                path.mkdir(mode=0o700)
                continue
            if self.netdisk._entry(before['path'], before['fsId']) != before:
                raise AssetLibraryError('下载前云端文件已变化，源文件保留', 409)
            known = source['known'].get(before['fsId'])
            expected = known['sha256'] if known and known['bytes'] == before['bytes'] else None
            fields = {'expected_sha256': expected} if expected else {}
            self._wait(self.drive.start_download(before['fsId'], path, **fields), 'downloading', before['bytes'], before['path'])
            digest, size = self._digest(path)
            if size != before['bytes'] or expected and digest != expected:
                raise AssetLibraryError('下载副本大小或 SHA256 与已知清单不一致，源文件保留', 409)
            if self.netdisk._entry(before['path'], before['fsId']) != before:
                raise AssetLibraryError('下载期间云端文件已变化，源文件保留', 409)
            receipts.append({'relative': relative, 'bytes': size, 'sha256': digest,
                'fsId': before['fsId'], 'path': before['path'], 'verifiedAgainstManifest': bool(expected), '_known': known,
                'verification': 'manifest-sha256' if expected else 'download-sha256-stable-provider'})
            outcome['files'] = [{k: v for k, v in item.items() if not k.startswith('_')} for item in receipts]
            outcome.update(targetState='staged', targetVerified=False)
            self._completed_files += 1
            self._emit('downloading', force=True, currentFile=before['path'])
        outcome['files'] = [{k: v for k, v in item.items() if not k.startswith('_')} for item in receipts]
        return root, receipts

    def _index_upload(self, source, receipts):
        with self.local.lock, self.library.lock, self.library._db() as db:
            self.local._idle(db)
            for receipt in receipts:
                analyzed = self.library._analyze(source['project']['root'], receipt['sourcePath'],
                    source['project']['options'], source['project']['id'])
                if analyzed['sha256'] != receipt['sha256']:
                    raise AssetLibraryError('来源已变化，未把云端旧副本关联到新原件', 409)
                self.library._upsert(db, source['project']['id'], analyzed)
                db.execute('INSERT OR REPLACE INTO remote_locations VALUES(?,?,?,?,?,?)',
                    (analyzed['id'], 'baidu-netdisk', receipt['fsId'], receipt['path'], receipt['sha256'], receipt['bytes']))
                receipt['assetId'] = analyzed['id']

    def _cloud_catalog(self, plan, all_receipts, outcomes):
        """Keep classifications/vectors searchable when local originals move away.

        Cloud records get independent identities so local trash can still restore
        the original IDs. They contain expected SHA256, not a fake local copy.
        """
        with self.local.lock, self.library.lock, self.library._db() as db:
            self.local._idle(db)
            mappings = {}
            for source, receipts in zip(plan['_sources'], all_receipts):
                project_id = str(uuid.uuid5(uuid.UUID(source['project']['id']),
                    'cloud:' + plan['_account']['userId'] + ':' + plan['_account']['root']))
                placeholder = self.root / ('cloud-project-' + project_id)
                with _absolute_directory(placeholder, create=True):
                    pass
                options = {**source['project']['options'], 'remoteBacked': True, 'crossTransferCloud': True}
                db.execute('INSERT OR IGNORE INTO projects(id,root,name,options,created,updated,stats) VALUES(?,?,?,?,?,?,?)',
                    (project_id, str(placeholder), source['project']['name'] + ' · 百度网盘',
                     json.dumps(options, ensure_ascii=False), time.time(), time.time(), '{}'))
                for receipt in receipts:
                    relative = receipt['path'][len(plan['_account']['root']) + 1:]
                    asset_id = str(uuid.uuid5(uuid.UUID(project_id), relative))
                    mappings[(source['project']['id'], receipt['sourcePath'])] = (project_id, relative, asset_id)
            for source, receipts, outcome in zip(plan['_sources'], all_receipts, outcomes):
                ids = []
                for receipt in receipts:
                    project_id, relative, target_id = mappings[(source['project']['id'], receipt['sourcePath'])]
                    records = self.local._snapshot(db, source['project']['id'], receipt['sourcePath'])
                    originals = [row for table, row in records if table == 'assets' and row['path'] == receipt['sourcePath']]
                    if len(originals) != 1 or originals[0]['sha256'] != receipt['sha256']:
                        raise AssetLibraryError('本地分类索引已变化，未清理原件', 409)
                    old = originals[0]
                    existing = db.execute('SELECT sha256 FROM assets WHERE project_id=? AND path=?', (project_id, relative)).fetchone()
                    if existing and existing['sha256'] != receipt['sha256']:
                        raise AssetLibraryError('云端目录已有不同版本的分类索引，未清理原件', 409)
                    copied = {**old, 'id': target_id, 'project_id': project_id, 'path': relative,
                              'ctime_ns': 0, 'device': 0, 'inode': 0}
                    columns = list(copied)
                    db.execute('INSERT OR IGNORE INTO assets(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', list(copied.values()))
                    db.execute('INSERT OR REPLACE INTO asset_storage VALUES(?,1)', (target_id,))
                    for table, row in records:
                        if table not in ('annotations', 'embeddings', 'refs', 'identifiers'):
                            continue
                        value = {**row}
                        if table in ('annotations', 'embeddings'):
                            value['asset_id'] = target_id
                        else:
                            value['source_id'] = target_id
                        if table == 'identifiers':
                            value['project_id'] = project_id
                            mapped = mappings.get((source['project']['id'], value['target_path']))
                            if mapped:
                                value['target_path'] = mapped[1]
                            elif value['target_path'] == receipt['sourcePath']:
                                value['target_path'] = relative
                            else:
                                continue
                        fields = list(value)
                        db.execute('INSERT OR IGNORE INTO ' + table + '(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')', list(value.values()))
                    db.execute('INSERT OR REPLACE INTO remote_locations VALUES(?,?,?,?,?,?)',
                        (target_id, 'baidu-netdisk', receipt['fsId'], receipt['path'], receipt['sha256'], receipt['bytes']))
                    self.library._refresh_search(db, target_id)
                    receipt['assetId'] = target_id
                    ids.append(target_id)
                outcome['targetAssetIds'] = ids
                if len(ids) == 1 and not outcome['isDirectory']:
                    outcome['targetAssetId'] = ids[0]

    def _commit_downloads(self, plan, downloads, outcomes):
        destination = plan['_destination']
        project = self.local._project(destination['project']['id'])
        if project != destination['project']:
            raise AssetLibraryError('本地目标工程已变化，未提交下载副本', 409)
        with _directory(project['root'], destination['path']) as parent:
            if _fingerprint(os.fstat(parent)) != destination['fingerprint']:
                raise AssetLibraryError('本地目标目录在传输期间发生变化，未覆盖任何文件', 409)
            for source, (staged, receipts), outcome in zip(plan['_sources'], downloads, outcomes):
                self._check()
                outcome['state'] = 'running'
                temporary = '.devhelper-incoming-' + uuid.uuid4().hex
                owned = False
                try:
                    owned = True
                    with _absolute_directory(staged.parent) as incoming:
                        _copy_node(incoming, staged.name, parent, temporary)
                    with self.local.lock, self.library.lock, self.library._db() as db:
                        self.local._idle(db)
                        current = self.local._project(project['id'])
                        if current['_rootFingerprint'] != project['_rootFingerprint']:
                            raise AssetLibraryError('目标工程根目录已变化', 409)
                        with _directory(project['root'], destination['path']) as current_parent:
                            actual_parent = os.fstat(current_parent)
                            held_parent = os.fstat(parent)
                            if (actual_parent.st_dev, actual_parent.st_ino) != (held_parent.st_dev, held_parent.st_ino):
                                raise AssetLibraryError('目标父目录已变化，源文件保留', 409)
                        if self.local._indexed(db, project['id'], source['target']):
                            raise AssetLibraryError('目标索引已出现同名资源，不会覆盖', 409)
                        _rename_exclusive(parent, temporary, parent, source['name'])
                        owned = False
                        outcome['destinationCommitted'] = True
                        outcome.update(targetState='needs-verification', targetVerified=False)
                        manifest = _tree(parent, source['name'])
                        ids = []
                        for receipt in receipts:
                            relative = source['target'] + ('/' + receipt['relative'] if receipt['relative'] else '')
                            if self._digest(Path(project['root']) / relative) != (receipt['sha256'], receipt['bytes']):
                                raise AssetLibraryError('本地提交副本与下载摘要不一致，源文件保留', 409)
                            with _parent(project['root'], relative) as (file_parent, file_name):
                                file_manifest = _tree(file_parent, file_name)
                            known = receipt.get('_known')
                            records = self.local._snapshot(db, known['projectId'], known['path']) if known else []
                            source_ref = {'projectId': known['projectId'], 'path': known['path']} if known else {'projectId': project['id'], 'path': '__unmapped_cloud_source__'}
                            item = {'source': source_ref, 'destination': {'projectId': project['id'], 'path': relative}}
                            created = self.local._reindex(db, item, records, 'copy', file_manifest)
                            receipt['_targetAssetIds'] = list(created)
                            ids.extend(created)
                            for asset_id in created:
                                db.execute('INSERT OR REPLACE INTO remote_locations VALUES(?,?,?,?,?,?)',
                                    (asset_id, 'baidu-netdisk', receipt['fsId'], receipt['path'], receipt['sha256'], receipt['bytes']))
                        self.local._directory_rows(db, project['id'], source['target'], manifest)
                        outcome['assetIds'] = ids
                        outcome['targetAssetIds'] = ids
                        if len(ids) == 1 and not outcome['isDirectory']:
                            outcome['targetAssetId'] = ids[0]
                    outcome['transferState'] = 'completed'
                    outcome['state'] = 'completed'
                    outcome.update(targetState='available', targetVerified=True)
                    self._emit('committing', force=True, currentFile=source['target'])
                finally:
                    if owned and _exists(parent, temporary):
                        _remove_owned(parent, temporary)

    def _verify_destinations(self, plan, receipts):
        for source, files in zip(plan['_sources'], receipts):
            for item in files:
                self._check()
                if plan['destinationScope'] == 'netdisk':
                    if self.netdisk._entry(item['path'], item['fsId']) != item['_fingerprint']:
                        raise AssetLibraryError('云端目标副本已变化，源文件保留', 409)
                else:
                    relative = source['target'] + ('/' + item['relative'] if item['relative'] else '')
                    target = Path(plan['_destination']['project']['root']) / relative
                    if self._digest(target) != (item['sha256'], item['bytes']):
                        raise AssetLibraryError('本地目标副本已变化，云端源文件保留', 409)

    def _cleanup(self, plan, outcomes):
        self._check()
        args = plan['_arguments']
        if plan['sourceScope'] == 'local':
            with self.local.lock, self.library.lock:
                cleanup_plan = self.local.plan('trash', args['items'])
                expected_sources = {(source['source']['projectId'], source['source']['path']): source['manifest']
                                    for source in plan['_sources']}
                cleanup_sources = {(item['source']['projectId'], item['source']['path']): manifest
                                   for item, manifest in zip(cleanup_plan['items'], cleanup_plan['_manifests'])}
                if cleanup_sources != expected_sources:
                    raise AssetLibraryError('来源在开始清理前已变化，未删除更新后的原件', 409)
                self._check()
                result = self.local.execute(cleanup_plan)
        else:
            with self.netdisk.lock:
                cleanup_plan = self.netdisk.plan('trash', args['items'])
                if cleanup_plan['_sources'] != [source['fingerprint'] for source in plan['_sources']]:
                    raise AssetLibraryError('云端来源在开始清理前已变化，未删除更新后的原件', 409)
                for source in plan['_sources']:
                    if self._remote_tree(source['fingerprint']) != source['tree']:
                        raise AssetLibraryError('云端目录内容在清理前已变化，源文件保留', 409)
                    # The provider operator rechecks all these nodes inside its
                    # own network lock immediately before the mutation. Include
                    # unindexed descendants, not only known library locations.
                    for node in source['tree']:
                        value = node['fingerprint']
                        cleanup_plan['_locationFingerprints'][value['fsId']] = value
                cleanup_plan['_seal'] = self.netdisk._seal(cleanup_plan)
                self._check()
                result = self.netdisk.execute(cleanup_plan)
        cleanup_items = result.get('items', [])
        for source, outcome in zip(plan['_sources'], outcomes):
            expected = source['source']
            matches = [item for item in cleanup_items if item.get('source') == expected or
                       isinstance(item.get('source'), str) and item['source'] == expected.get('path')]
            done = bool(matches) and all(item.get('state') == 'completed' for item in matches)
            preserved = False if done else None
            if not done:
                try:
                    if plan['sourceScope'] == 'local':
                        project, _, _ = self.local._source(expected)
                        preserved = True
                        for file in outcome.get('files', []):
                            relative = expected['path'] + ('/' + file['relative'] if file['relative'] else '')
                            if self._digest(Path(project['root']) / relative) != (file['sha256'], file['bytes']):
                                preserved = None
                                break
                    else:
                        preserved = self.netdisk._entry(expected['path'], expected['fsId']) == source['fingerprint']
                except (OSError, ValueError, BaiduDriveError):
                    preserved = None
            outcome.update(state='completed' if done else 'failed', sourcePreserved=preserved,
                           sourceCleanupState='completed' if done else 'failed')
            if not done:
                outcome['error'] = '副本已完成，但源清理尚未完整确认；请核对回收站及源目录'
        return result

    def _retire_cloud_sources(self, plan, all_receipts, outcomes):
        """Retire only this move's fully cloned, confirmed cloud-only sources.

        An ordinary catalog may intentionally contain metadata without originals.
        This is not garbage collection: unrelated entries and uncertain cleanup
        outcomes are never candidates.
        """
        legacy_locations = self.netdisk._rows(None)
        candidates = {}
        for source, receipts, outcome in zip(plan['_sources'], all_receipts, outcomes):
            if outcome.get('sourceCleanupState') != 'completed' or outcome.get('state') != 'completed':
                continue
            for receipt in receipts:
                known = receipt.get('_known')
                targets = receipt.get('_targetAssetIds') or []
                if not known or not targets:
                    continue
                value = candidates.setdefault(known['assetId'], {'known': known, 'receipts': [], 'outcomes': []})
                value['receipts'].append((receipt, targets))
                if outcome not in value['outcomes']:
                    value['outcomes'].append(outcome)
        with self.local.lock, self.library.lock, self.library._db() as db:
            self.local._idle(db)
            for source_id, candidate in candidates.items():
                known = candidate['known']
                source = db.execute('SELECT * FROM assets WHERE id=?', (source_id,)).fetchone()
                if source is None:
                    continue
                reason = None
                if source['sha256'] != known['sha256'] or source['size'] != known['bytes']:
                    reason = 'source-index-changed'
                project = db.execute('SELECT root,options FROM projects WHERE id=?', (source['project_id'],)).fetchone()
                storage = db.execute('SELECT remote_only FROM asset_storage WHERE asset_id=?', (source_id,)).fetchone()
                if not reason and not (project and (json.loads(project['options']).get('remoteBacked') or storage and storage[0])):
                    reason = 'source-is-not-cloud-only'
                if not reason and (db.execute('SELECT 1 FROM remote_locations WHERE asset_id=? LIMIT 1', (source_id,)).fetchone() or
                                   any(row['asset_id'] == source_id for row in legacy_locations)):
                    reason = 'another-cloud-location'
                if not reason and db.execute('SELECT 1 FROM local_files WHERE asset_id=? LIMIT 1', (source_id,)).fetchone():
                    # Even a cache needing revalidation is evidence we must not
                    # discard automatically during unrelated source cleanup.
                    reason = 'local-cache-or-cache-needs-verification'
                if not reason:
                    try:
                        with _read_source(project['root'], source['path']):
                            reason = 'local-original-exists'
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError):
                        reason = 'local-location-needs-verification'
                source_annotations = {row['source']: json.loads(row['data']) for row in db.execute(
                    'SELECT source,data FROM annotations WHERE asset_id=?', (source_id,))}
                source_vectors = {row['model']: (row['dimensions'], bytes(row['vector']), row['sha256']) for row in db.execute(
                    'SELECT model,dimensions,vector,sha256 FROM embeddings WHERE asset_id=?', (source_id,))}
                source_refs = {(row['namespace'], row['value'], row['evidence']) for row in db.execute(
                    'SELECT namespace,value,evidence FROM refs WHERE source_id=?', (source_id,))}
                source_identifiers = {(row['namespace'], row['value']) for row in db.execute(
                    "SELECT namespace,value FROM identifiers WHERE source_id=? AND namespace!='path'", (source_id,))}
                source_ocr = json.loads(source['metadata']).get('ocr', {})
                for receipt, targets in candidate['receipts']:
                    if reason:
                        break
                    if receipt['sha256'] != source['sha256'] or receipt['bytes'] != source['size']:
                        reason = 'download-does-not-match-source-index'
                        break
                    for target_id in targets:
                        target = db.execute('SELECT * FROM assets WHERE id=?', (target_id,)).fetchone()
                        if (target_id == source_id or target is None or target['sha256'] != source['sha256'] or
                                target['size'] != source['size']):
                            reason = 'target-index-needs-verification'
                            break
                        target_annotations = {row['source']: json.loads(row['data']) for row in db.execute(
                            'SELECT source,data FROM annotations WHERE asset_id=?', (target_id,))}
                        if any(target_annotations.get(key) != value for key, value in source_annotations.items()):
                            reason = 'classification-copy-needs-verification'
                            break
                        target_vectors = {row['model']: (row['dimensions'], bytes(row['vector']), row['sha256']) for row in db.execute(
                            'SELECT model,dimensions,vector,sha256 FROM embeddings WHERE asset_id=?', (target_id,))}
                        if any(target_vectors.get(key) != value for key, value in source_vectors.items()):
                            reason = 'vector-copy-needs-verification'
                            break
                        target_refs = {(row['namespace'], row['value'], row['evidence']) for row in db.execute(
                            'SELECT namespace,value,evidence FROM refs WHERE source_id=?', (target_id,))}
                        target_identifiers = {(row['namespace'], row['value']) for row in db.execute(
                            "SELECT namespace,value FROM identifiers WHERE source_id=? AND namespace!='path'", (target_id,))}
                        if not source_refs.issubset(target_refs) or not source_identifiers.issubset(target_identifiers):
                            reason = 'dependency-copy-needs-verification'
                            break
                        if source_ocr.get('status') == 'analyzed' and json.loads(target['metadata']).get('ocr') != source_ocr:
                            reason = 'ocr-copy-needs-verification'
                            break
                        # Check the actual delivered descriptor once more before
                        # removing its last independent metadata record.
                        try:
                            target_project = db.execute('SELECT root FROM projects WHERE id=?', (target['project_id'],)).fetchone()
                            if self._digest(Path(target_project['root']) / target['path']) != (source['sha256'], source['size']):
                                reason = 'target-original-needs-verification'
                                break
                        except (OSError, ValueError):
                            reason = 'target-original-needs-verification'
                            break
                if not reason:
                    if self.library.fts:
                        db.execute('DELETE FROM asset_search WHERE id=?', (source_id,))
                    db.execute('DELETE FROM assets WHERE id=? AND sha256=?', (source_id, source['sha256']))
                for outcome in candidate['outcomes']:
                    state = outcome.setdefault('sourceIndexCleanup', {'removedAssetIds': [], 'retained': []})
                    if reason:
                        state['retained'].append({'assetId': source_id, 'reason': reason})
                    else:
                        state['removedAssetIds'].append(source_id)

    def execute(self, plan, progress=None, cancelled=None):
        if not isinstance(plan, dict) or plan.get('_version') != 1:
            raise AssetLibraryError('跨设备传输计划无效，请重新预览', 400)
        with self.lock:
            self._callback, self._cancelled = progress, cancelled
            self._bytes, self._completed_files, self._last_progress = 0, 0, 0.
            self._retain_staging = False
            self._active_transfer_job = None
            self._total, self._count = plan['totalBytes'], plan['fileCount']
            outcomes = [{**copy.deepcopy(item), 'state': 'skipped', 'transferState': 'pending',
                'sourcePreserved': True, 'sourceCleanupState': 'retained',
                'targetState': 'pending', 'targetVerified': False} for item in plan['items']]
            stage = self.root / uuid.uuid4().hex
            stage.mkdir(mode=0o700)
            cleanup = None
            cancelled_state = False
            try:
                self._validate_sources(plan)
                self._check()
                if plan['destinationScope'] == 'netdisk':
                    directory, _ = self.netdisk._directory(plan['_destination']['path'])
                    if directory != plan['_destination']:
                        raise AssetLibraryError('网盘目标目录在预览后变化，请重新预览', 409)
                    all_receipts = []
                    for index, (source, outcome) in enumerate(zip(plan['_sources'], outcomes)):
                        outcome['state'], outcome['transferState'] = 'running', 'running'
                        staged = self._stage_local(source, stage, index)
                        receipts = self._upload(plan, source, staged, outcome)
                        self._index_upload(source, receipts)
                        all_receipts.append(receipts)
                        outcome['state'], outcome['transferState'] = 'completed', 'completed'
                        outcome.update(targetState='available', targetVerified=True,
                            targetAssetIds=[receipt['assetId'] for receipt in receipts])
                        if len(receipts) == 1 and not outcome['isDirectory']:
                            outcome['targetAssetId'] = receipts[0]['assetId']
                else:
                    downloads = []
                    for index, (source, outcome) in enumerate(zip(plan['_sources'], outcomes)):
                        outcome['state'], outcome['transferState'] = 'running', 'running'
                        downloads.append(self._download(source, stage, index, outcome))
                        outcome['transferState'] = 'downloaded'
                        outcome['state'] = 'skipped'
                    self._validate_sources(plan)
                    self._commit_downloads(plan, downloads, outcomes)
                    all_receipts = [receipts for _, receipts in downloads]
                    for outcome in outcomes:
                        outcome['state'] = 'completed'
                if plan['operation'] == 'move':
                    self._validate_sources(plan)
                    self._verify_destinations(plan, all_receipts)
                    if plan['sourceScope'] == 'local':
                        self._cloud_catalog(plan, all_receipts, outcomes)
                    self._emit('cleaning-sources', force=True)
                    cleanup = self._cleanup(plan, outcomes)
                    if plan['sourceScope'] == 'netdisk':
                        try:
                            self._retire_cloud_sources(plan, all_receipts, outcomes)
                        except Exception:
                            # Source deletion is already a confirmed external
                            # action. A bookkeeping failure must not fabricate
                            # a rollback or claim the cloud original was kept.
                            for item in outcomes:
                                if item['sourceCleanupState'] == 'completed':
                                    item['sourceIndexWarning'] = '源文件已完成移动，旧分类索引保留待核验'
            except Exception as error:
                cancelled_state = isinstance(error, _Stopped)
                state = 'unknown' if isinstance(error, _Stopped) and error.unknown else 'cancelled' if cancelled_state else 'failed'
                active = next((item for item in outcomes if item['state'] == 'running'), None)
                if active is None:
                    active = next((item for item in outcomes if item['transferState'] != 'completed'), outcomes[0] if outcomes else None)
                if active is not None:
                    active.update(state=state, error=self.workspace.public_error(error))
                    if self._active_transfer_job:
                        active['pendingTransferJobId' if self._retain_staging else 'transferJobId'] = self._active_transfer_job
                if plan['operation'] == 'move' and cleanup is None:
                    for item in outcomes:
                        item['sourceCleanupState'] = 'cancelled' if cancelled_state else 'retained'
                        if item['state'] == 'completed':
                            item.update(state=state, error='副本已传输；整批移动未完成，源文件保留')
            finally:
                if not self._retain_staging:
                    shutil.rmtree(stage, ignore_errors=True)
            done = sum(item['state'] == 'completed' for item in outcomes)
            cleaned = sum(item['sourceCleanupState'] == 'completed' for item in outcomes)
            cleanup_state = ('completed' if cleaned == len(outcomes) else 'partial' if cleaned else
                'cancelled' if cancelled_state else 'failed' if cleanup else 'retained') if plan['operation'] == 'move' else 'retained'
            result = {'scope': 'transfer', 'sourceScope': plan['sourceScope'], 'destinationScope': plan['destinationScope'],
                'operation': plan['operation'], 'items': outcomes,
                'state': 'completed' if done == len(outcomes) else 'cancelled' if cancelled_state else 'completed-with-errors' if done else 'failed',
                'completedCount': done, 'failedCount': sum(item['state'] in ('failed', 'unknown') for item in outcomes),
                'sourcePreserved': all(item['sourcePreserved'] is True for item in outcomes),
                'sourceCleanupState': cleanup_state, 'moveCommitted': plan['operation'] == 'move' and cleaned == len(outcomes),
                'bytesTransferred': self._bytes, 'totalBytes': self._total, 'completedFiles': self._completed_files}
            if cleanup is not None:
                result['sourceCleanup'] = cleanup
            if self._retain_staging:
                result['pendingTransferRetained'] = True
            self._emit(result['state'], force=True, items=outcomes, sourceCleanupState=cleanup_state)
            self._callback = self._cancelled = None
            return result
