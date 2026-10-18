"""Scoped, reviewed Baidu file-manager plans with confirmed per-item outcomes.

Protocol references (checked 2026-10-18):
https://pan.baidu.com/union/doc/mksg0s9l4
https://github.com/baidu-netdisk/baidu-drive-sdk-go/blob/main/baidudriver/api/file_manager.go
The official SDK confirms POST filemanager, form async=0/filelist/ondup=fail,
copy/move objects {path,dest,newname}, rename {path,newname}, delete [path],
and individual info[].errno/path. No mutation request is automatically retried.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from pathlib import PurePosixPath
import re
import secrets
import threading
import time
import uuid

try:
    from .baidu_drive import BaiduDriveError, FILE_API
    from .file_name_patterns import renamed_name
except ImportError:
    from baidu_drive import BaiduDriveError, FILE_API
    from file_name_patterns import renamed_name

PROVIDER = 'baidu-netdisk'
PLAN_SECONDS = 20 * 60
PAGE_SIZE = 1000
MAX_DIRECTORY_ITEMS = 500000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _fsid(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r'[0-9]{1,24}', str(value)) or int(value) <= 0:
        raise BaiduDriveError('文件 ID 格式错误', 400)
    return str(int(value))


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BaiduDriveError('百度未返回有效的' + label + '，请刷新列表后重试', 502)
    return value


class NetdiskFileOperator:
    def __init__(self, workspace):
        self.workspace = workspace
        self.drive = workspace.drive
        self.library = workspace.library
        self.lock = threading.RLock()
        self._key = secrets.token_bytes(32)
        self._used = {}

    def _path(self, value, *, source=False):
        return self.drive.remote_path(value, file=source)

    def _identity(self):
        return {'userId': self.drive.authorized_user_id(), 'root': self.drive.remote_path(),
                'clientId': self.drive.status().get('clientId', '')}

    def _list(self, path, cache=None):
        if cache is not None and path in cache:
            return cache[path]
        path = self._path(path)
        entries, seen = [], set()
        start = 0
        while True:
            page = self.drive.list_files(path, start=start, limit=PAGE_SIZE)
            if page.get('directoryExists') is False:
                raise BaiduDriveError('目标目录不存在，请先创建目录', 409)
            values = page.get('items')
            if not isinstance(values, list):
                raise BaiduDriveError('网盘目录响应不完整，未执行文件操作', 502)
            for entry in values:
                if not isinstance(entry, dict):
                    raise BaiduDriveError('网盘目录响应格式错误', 502)
                actual = self._path(entry.get('path'), source=True)
                if str(PurePosixPath(actual).parent) != path or actual in seen:
                    raise BaiduDriveError('目录正在变化或返回重复项目，请刷新后重新计划', 409)
                seen.add(actual)
                entries.append(entry)
            if len(entries) > MAX_DIRECTORY_ITEMS:
                raise BaiduDriveError('目录过大，无法完成冲突检查；请改用较小的目标目录', 409)
            if not page.get('hasMore', False):
                break
            if not values:
                raise BaiduDriveError('网盘目录分页不完整，未执行文件操作', 502)
            start += len(values)
        if cache is not None:
            cache[path] = entries
        return entries

    def _fingerprint(self, info, expected_path=None):
        path = self._path(info.get('path'), source=True)
        if expected_path is not None and path != expected_path:
            raise BaiduDriveError('所选文件的 ID 与路径已变化，请刷新后重新计划', 409)
        isdir = info.get('isdir')
        if isdir not in (0, 1) or isinstance(isdir, bool):
            raise BaiduDriveError('百度未返回有效的文件类型', 502)
        md5 = info.get('md5')
        # Baidu's field can contain a provider-encoded/opaque digest rather than
        # a literal hexadecimal MD5. Preserve it exactly for provider-to-provider
        # equality; it is never proof of a local file's MD5 or SHA256.
        if md5 not in (None, '') and (not isinstance(md5, str) or not 1 <= len(md5) <= 256 or
                                      re.search(r'[\x00-\x1f\x7f]', md5)):
            raise BaiduDriveError('百度返回了无效的文件摘要', 502)
        result = {'fsId': _fsid(info.get('fs_id')), 'path': path, 'isDirectory': bool(isdir),
                  'bytes': _integer(info.get('size'), '文件大小'),
                  'serverMtime': _integer(info.get('server_mtime'), '修改时间'),
                  'md5': md5 if md5 else None}
        for source, target in (('local_mtime', 'localMtime'), ('server_ctime', 'serverCtime')):
            if source in info:
                result[target] = _integer(info[source], '文件时间')
        return result

    def _entry(self, path, fs_id=None, cache=None):
        path = self._path(path, source=True)
        if fs_id is None:
            matches = [x for x in self._list(str(PurePosixPath(path).parent), cache) if x.get('path') == path]
            if len(matches) != 1:
                raise BaiduDriveError('所选文件不存在或目录正在变化，请刷新后重试', 409)
            fs_id = matches[0].get('fs_id')
        return self._fingerprint(self.drive.file_info(_fsid(fs_id)), path)

    def _directory(self, path, cache=None):
        path = self._path(path)
        if path == self.drive.remote_path():
            result = {'path': path, 'applicationRoot': True}
        else:
            result = self._entry(path, cache=cache)
            if not result['isDirectory']:
                raise BaiduDriveError('粘贴目标必须是已存在的目录', 409)
        entries = self._list(path, cache)
        # The application root cannot be queried with the file-only filemetas
        # guard. Its complete listing binds the existing destination instead.
        facts = [{k: x.get(k) for k in ('fs_id', 'path', 'isdir', 'size', 'server_mtime')} for x in entries]
        result['listingDigest'] = hashlib.sha256(_json(sorted(facts, key=lambda x: x['path'])).encode()).hexdigest()
        return result, entries

    def _seal(self, plan):
        return hmac.new(self._key, _json({k: v for k, v in plan.items() if k != '_seal'}).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _name(name):
        if (not isinstance(name, str) or not 1 <= len(name) <= 240 or name.strip(' .') != name or
                name in ('.', '..') or re.search(r'[/\\\x00-\x1f\x7f:*?"<>|]', name)):
            raise BaiduDriveError('新名称不能包含路径、控制符或保留字符，也不能以点或空格开头结尾', 400)
        return name

    def plan(self, operation, items, destination=None, name=None):
        if operation not in ('copy', 'move', 'rename', 'trash'):
            raise BaiduDriveError('不支持的文件操作', 400)
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise BaiduDriveError('每批请选择 1–100 个文件或目录', 400)
        if operation == 'rename':
            if destination is not None:
                raise BaiduDriveError('重命名保留每项原目录，不接受目标目录', 400)
            name = self._name(name)
            if len(items) > 1 and '{index}' not in name and '{name}' not in name:
                raise BaiduDriveError('批量重命名模板必须包含 {index} 或 {name}', 400)
        elif name is not None:
            raise BaiduDriveError('此操作不接受新名称', 400)
        if operation in ('copy', 'move'):
            if not isinstance(destination, dict) or set(destination) != {'path'}:
                raise BaiduDriveError('请选择已存在的粘贴目录', 400)
        elif destination is not None:
            raise BaiduDriveError('此操作不接受目标目录', 400)
        # Read local/legacy location receipts before taking drive.lock. Existing
        # upload code takes workspace.lock before drive.lock, so the reverse
        # order here would deadlock concurrent uploads.
        location_rows = self._rows(None)
        with self.lock, self.drive.lock:
            identity = self._identity()
            cache, sources, paths, ids = {}, [], set(), set()
            for item in items:
                if not isinstance(item, dict) or set(item) - {'path', 'fsId'} or 'path' not in item:
                    raise BaiduDriveError('文件项目只接受 path 和可选 fsId', 400)
                source = self._entry(item['path'], item.get('fsId'), cache)
                if source['path'] in paths or source['fsId'] in ids:
                    raise BaiduDriveError('批量选择包含重复文件', 400)
                paths.add(source['path']); ids.add(source['fsId']); sources.append(source)
            for source in sources:
                if source['isDirectory'] and any(path.startswith(source['path'] + '/') for path in paths):
                    raise BaiduDriveError('不能同时选择目录及其内部文件，请只选择父目录', 400)
            directories = {}
            if operation in ('copy', 'move'):
                directory, listing = self._directory(destination['path'], cache)
                directories[directory['path']] = (directory, listing)
            elif operation == 'rename':
                for source in sources:
                    parent = str(PurePosixPath(source['path']).parent)
                    if parent not in directories:
                        directories[parent] = self._directory(parent, cache)
            occupied = {x['path'] for _, listing in directories.values() for x in listing}
            targets, public = set(), []
            for index, source in enumerate(sources, 1):
                target = None
                if directories:
                    parent = str(PurePosixPath(source['path']).parent) if operation == 'rename' else self._path(destination['path'])
                    directory = directories[parent][0]
                    if source['isDirectory'] and (directory['path'] == source['path'] or directory['path'].startswith(source['path'] + '/')):
                        raise BaiduDriveError('不能复制或移动目录到自身或其内部', 409)
                    if operation == 'rename':
                        try:
                            new_name = renamed_name(name, PurePosixPath(source['path']).name,
                                                    source['isDirectory'], index, len(sources))
                        except ValueError as error:
                            raise BaiduDriveError(str(error), 400) from None
                        new_name = self._name(new_name)
                    else:
                        new_name = PurePosixPath(source['path']).name
                    target = self._path(directory['path'] + '/' + new_name, source=True)
                    if target in occupied or target in targets or target == source['path']:
                        raise BaiduDriveError('目标已有同名项目；不会覆盖、合并或自动改名', 409)
                    targets.add(target)
                public.append({'source': source['path'], 'destination': target, 'name': PurePosixPath(source['path']).name,
                               'fsId': source['fsId'], 'isDirectory': source['isDirectory'], 'bytes': source['bytes']})
            now = time.time()
            location_fingerprints = {}
            for source in sources:
                for row in location_rows:
                    if row['path'] != source['path'] and not (source['isDirectory'] and row['path'].startswith(source['path'] + '/')):
                        continue
                    key = row['fs_id']
                    if key in location_fingerprints:
                        continue
                    try:
                        before = source if row['path'] == source['path'] and key == source['fsId'] else self._entry(row['path'], key)
                        location_fingerprints[key] = before if not before['isDirectory'] and before['bytes'] == row['bytes'] else None
                    except (BaiduDriveError, ValueError, KeyError):
                        location_fingerprints[key] = None
            result = {'planId': str(uuid.uuid4()), 'operation': operation, 'scope': 'baidu-netdisk',
                      'scopeRoot': identity['root'], 'items': public, 'fileCount': len(sources),
                      'bytes': sum(x['bytes'] for x in sources), 'overwrite': False,
                      'trashPolicy': '删除调用百度网盘接口，回收及保留期限遵守平台策略；本工具不清空回收站' if operation == 'trash' else None,
                      '_account': identity, '_sources': sources,
                      '_directories': [state for state, _ in directories.values()],
                      '_locationFingerprints': location_fingerprints,
                      '_expiresAt': now + PLAN_SECONDS}
            result['_seal'] = self._seal(result)
            return result

    @staticmethod
    def _missing(error):
        return isinstance(error, BaiduDriveError) and (error.status == 404 or str(error.provider_code) in ('-9', '31066'))

    def _probe(self, source, target, operation, ack, cache):
        """Only concrete provider metadata can confirm the requested end state."""
        if operation == 'trash':
            try:
                current = self.drive.file_info(source['fsId'])
            except BaiduDriveError as error:
                if self._missing(error):
                    return ('completed' if ack == 0 else 'unknown'), None
                return 'unknown', None
            if ack not in (None, 0) and current.get('path') == source['path']:
                return 'failed', None
            return 'unknown', None
        try:
            if operation in ('move', 'rename'):
                receipt = self._fingerprint(self.drive.file_info(source['fsId']))
                if receipt['path'] != target:
                    return ('failed' if ack not in (None, 0) else 'unknown'), None
            else:
                entries = self._list(str(PurePosixPath(target).parent), cache)
                matches = [x for x in entries if x.get('path') == target]
                if len(matches) != 1:
                    return ('failed' if ack not in (None, 0) else 'unknown'), None
                receipt = self._entry(target, matches[0].get('fs_id'))
                if ack not in (None, 0):
                    return 'failed', None
            if (receipt['isDirectory'] != source['isDirectory'] or
                    not source['isDirectory'] and (receipt['bytes'] != source['bytes'] or
                    source.get('md5') and receipt.get('md5') != source['md5'])):
                return 'unknown', None
            if operation == 'copy' and ack is None:
                return 'unknown', None
            return 'completed', receipt
        except (BaiduDriveError, ValueError, KeyError):
            return 'unknown', None

    def _rows(self, source):
        with self.library.lock, self.library._db() as db:
            rows = db.execute('SELECT r.*,a.md5 asset_md5 FROM remote_locations r JOIN assets a ON a.id=r.asset_id WHERE r.provider=?', (PROVIDER,)).fetchall()
            result = [dict(x) for x in rows if source is None or x['path'] == source['path'] or source['isDirectory'] and x['path'].startswith(source['path'] + '/')]
        # Legacy completed backup receipts are also live fallback pointers.
        with self.workspace.lock:
            backups = copy.deepcopy(self.workspace.backups)
        for backup in backups.values():
            for entry in backup.get('items', []):
                receipt = entry.get('result') or {}
                path = receipt.get('remotePath', '')
                if (entry.get('state') != 'completed' or receipt.get('pathConfirmed') is False or
                        not isinstance(path, str) or source is not None and not (path == source['path'] or source['isDirectory'] and path.startswith(source['path'] + '/'))):
                    continue
                try:
                    asset = self.library.asset(entry['assetId'])
                    if receipt.get('sha256') != asset['sha256'] or receipt.get('bytes') != asset['bytes']:
                        continue
                    row = {'asset_id': asset['id'], 'provider': PROVIDER, 'fs_id': _fsid(receipt['fsId']),
                           'path': path, 'sha256': asset['sha256'], 'bytes': asset['bytes'], 'asset_md5': asset['md5']}
                    if row not in result:
                        result.append(row)
                except (BaiduDriveError, ValueError, KeyError):
                    continue
        return result

    def _update_locations(self, source, target, operation, state, receipt, location_fingerprints=None, warnings=None):
        if operation == 'copy' and state != 'completed':
            return 0
        rows = self._rows(source)
        validated, changed = {}, 0
        for row in rows:
            key = (row['asset_id'], row['fs_id'], row['path'])
            expected = target + row['path'][len(source['path']):] if target else None
            value = None
            if state == 'completed' and operation in ('move', 'rename', 'copy'):
                try:
                    before = (location_fingerprints or {}).get(row['fs_id'])
                    if before is None and row['fs_id'] == source['fsId'] and row['path'] == source['path']:
                        before = source
                    if operation == 'copy':
                        value = receipt if row['path'] == source['path'] else self._entry(expected)
                    else:
                        value = self._fingerprint(self.drive.file_info(row['fs_id']), expected)
                    if (not before or before['path'] != row['path'] or before['bytes'] != row['bytes'] or
                            value['isDirectory'] or value['bytes'] != row['bytes'] or
                            not before.get('md5') or value.get('md5') != before['md5'] or
                            operation != 'copy' and value['fsId'] != row['fs_id']):
                        value = None
                except (BaiduDriveError, ValueError, KeyError):
                    value = None
                if value is None and warnings is not None:
                    warnings.append(row['path'])
            validated[key] = value
        with self.library.lock, self.library._db() as db:
            for row in rows:
                value = validated[(row['asset_id'], row['fs_id'], row['path'])]
                if operation != 'copy':
                    changed += db.execute('DELETE FROM remote_locations WHERE asset_id=? AND provider=? AND fs_id=? AND path=?',
                        (row['asset_id'], PROVIDER, row['fs_id'], row['path'])).rowcount
                if value:
                    db.execute('INSERT OR REPLACE INTO remote_locations VALUES(?,?,?,?,?,?)',
                        (row['asset_id'], PROVIDER, value['fsId'], value['path'], row['sha256'], row['bytes']))
                    changed += 1
        if operation == 'copy':
            return changed
        with self.workspace.lock:
            for backup in self.workspace.backups.values():
                for entry in backup.get('items', []):
                    old = entry.get('result') or {}
                    path = old.get('remotePath')
                    match = next((row for row in rows if row['asset_id'] == entry.get('assetId') and row['fs_id'] == str(old.get('fsId')) and row['path'] == path), None)
                    if match is None:
                        continue
                    value = validated[(match['asset_id'], match['fs_id'], match['path'])]
                    old.setdefault('originalRemotePath', path)
                    old.update(pathConfirmed=bool(value), lastFileOperation=operation)
                    if value:
                        old.update(remotePath=value['path'], fsId=value['fsId'])
                    else:
                        old['locationState'] = 'trashed' if operation == 'trash' and state == 'completed' else 'needs-verification'
                    entry['result'] = old
            self.workspace._save_backups()
        return changed

    def execute(self, plan):
        with self.lock:
            with self.drive.lock:
                if (not isinstance(plan, dict) or not isinstance(plan.get('_seal'), str) or
                        not hmac.compare_digest(plan['_seal'], self._seal(plan))):
                    raise BaiduDriveError('文件操作计划无效或被修改，请重新预览', 409)
                now = time.time()
                self._used = {k: expiry for k, expiry in self._used.items() if expiry > now}
                if plan['_expiresAt'] < now or plan['planId'] in self._used:
                    raise BaiduDriveError('文件操作计划已过期或已执行，请重新预览', 409)
                if self._identity() != plan['_account']:
                    raise BaiduDriveError('网盘账号或应用目录已变化，请重新预览', 409)
                operation, sources = plan['operation'], plan['_sources']
                cache = {}
                for source in sources:
                    if self._entry(source['path'], source['fsId'], cache) != source:
                        raise BaiduDriveError('源文件已变化，未执行；请重新预览', 409)
                for before in plan.get('_locationFingerprints', {}).values():
                    if before and self._entry(before['path'], before['fsId'], cache) != before:
                        raise BaiduDriveError('目录内已关联素材在预览后发生变化，未执行；请重新预览', 409)
                for planned_directory in plan['_directories']:
                    directory, listing = self._directory(planned_directory['path'], cache)
                    if directory != planned_directory:
                        raise BaiduDriveError('目标目录已变化，未执行；请重新预览', 409)
                    occupied = {x['path'] for x in listing}
                    if any(item['destination'] in occupied for item in plan['items']):
                        raise BaiduDriveError('目标出现同名项目，不会覆盖；请重新预览', 409)
                provider_items = []
                for item in plan['items']:
                    if operation == 'trash':
                        provider_items.append(item['source'])
                    elif operation == 'rename':
                        provider_items.append({'path': item['source'], 'newname': PurePosixPath(item['destination']).name})
                    else:
                        provider_items.append({'path': item['source'], 'dest': str(PurePosixPath(item['destination']).parent),
                                               'newname': PurePosixPath(item['destination']).name, 'ondup': 'fail'})
                self._used[plan['planId']] = plan['_expiresAt']
                response, request_error = {}, None
                try:
                    response = self.drive._json('POST', FILE_API,
                        query={'method': 'filemanager', 'opera': 'delete' if operation == 'trash' else operation},
                        form={'async': '0', 'ondup': 'fail', 'filelist': _json(provider_items)}, authenticated=True, retry=False)
                except Exception:
                    # A timeout or top-level provider error can follow a partial
                    # mutation. Read the actual state; never submit this plan again.
                    request_error = '百度未返回确定的逐项回执；已停止重放，请核对实际网盘状态'
                info = response.get('info')
                ack = {}
                if isinstance(info, list):
                    for value in info:
                        if not isinstance(value, dict) or value.get('path') not in {x['source'] for x in plan['items']}:
                            continue
                        path = value['path']
                        code = value.get('errno', value.get('error_code'))
                        if isinstance(code, str) and re.fullmatch(r'-?\d{1,12}', code):
                            code = int(code)
                        if isinstance(code, int) and not isinstance(code, bool) and path not in ack:
                            ack[path] = code
                        else:
                            ack[path] = None
                outcomes = []
                task = response.get('taskid')
                asynchronous = task not in (None, '', 0, '0')
                for source, item in zip(sources, plan['items']):
                    state, receipt = self._probe(source, item['destination'], operation, ack.get(item['source']), {})
                    if asynchronous and source['isDirectory'] and state == 'completed':
                        # A newly visible directory does not prove an asynchronous
                        # recursive copy/delete task has finished all descendants.
                        state = 'unknown'
                    outcomes.append({**item, 'state': state, '_receipt': receipt})
                # A provider may return an async task despite async=0. Only repeat
                # metadata reads, and do not call a queued provider task completed.
                if asynchronous:
                    for pause in (.1, .25, .5):
                        if not any(x['state'] == 'unknown' for x in outcomes):
                            break
                        time.sleep(pause)
                        for source, item in zip(sources, outcomes):
                            if item['state'] == 'unknown':
                                item['state'], item['_receipt'] = self._probe(source, item['destination'], operation, ack.get(item['source']), {})
                                if source['isDirectory'] and item['state'] == 'completed':
                                    item['state'] = 'unknown'
            updated = 0
            for source, item in zip(sources, outcomes):
                if item['state'] in ('completed', 'unknown'):
                    try:
                        location_warnings = []
                        updated += self._update_locations(source, item['destination'], operation, item['state'], item['_receipt'],
                            plan.get('_locationFingerprints', {}), location_warnings)
                        if location_warnings:
                            item['locationsNeedVerification'] = len(location_warnings)
                            item['warning'] = '部分云端资源位置缺少一致的提供方回执，已标记待核验；原 SHA256 下载校验仍保留'
                    except Exception:
                        item['state'] = 'unknown'
                        item['error'] = '云端可能已完成，但资源位置索引未能保存，请核对后更新云索引'
                if item['state'] != 'completed' and 'error' not in item:
                    code = ack.get(item['source'])
                    item['error'] = ('百度返回失败（' + str(code) + '），未自动重试' if code not in (None, 0)
                                     else request_error or '百度尚未确认文件操作完成；请核对网盘，未自动重试')
                item.pop('_receipt', None)
            count = sum(x['state'] == 'completed' for x in outcomes)
            result = {'operation': operation, 'items': outcomes, 'completedCount': count,
                      'failedCount': sum(x['state'] == 'failed' for x in outcomes),
                      'unknownCount': sum(x['state'] == 'unknown' for x in outcomes),
                      'locationsUpdated': updated, 'mutationRequests': 1, 'automaticRetry': False,
                      'state': 'completed' if count == len(outcomes) else 'completed-with-errors'}
            if asynchronous:
                result['providerTaskPending'] = any(x['state'] == 'unknown' for x in outcomes)
            return result
