"""Reviewable file-operation plans and a serial, persisted execution queue.

Plans expire and are consumed at most once. Restart never replays a filesystem
or provider mutation; interrupted jobs remain visible for inspection.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from asset_library import AssetLibraryError
from local_file_ops import LocalFileOperator
from netdisk_file_ops import NetdiskFileOperator
from cross_file_transfer import CrossFileTransfer


def _public(value):
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()
                if not key.startswith('_')}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


class FileOperationsService:
    PLAN_TTL = 20 * 60

    def __init__(self, workspace):
        self.workspace = workspace
        self.root = workspace.root / 'file-operations'
        self.root.mkdir(exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError('文件操作私有目录不能是符号链接')
        # Resolve the application's configured storage alias (e.g. macOS /var)
        # once. Selected project traversal still uses held no-follow handles.
        self.root = self.root.resolve(strict=True)
        self.path = self.root / 'jobs.json'
        if self.path.is_symlink():
            raise ValueError('文件操作记录不能是符号链接')
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='file-operations')
        self.operators = {'local': LocalFileOperator(workspace.library, self.root),
                          'netdisk': NetdiskFileOperator(workspace)}
        self.plans = {}
        self._transfer = None
        self._cancellations = {}
        self._progress_writes = {}
        self.jobs = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.closed = False
        changed = False
        for job in self.jobs.values():
            if job['state'] in ('queued', 'running'):
                job.update(state='interrupted', updatedAt=time.time(),
                           error='服务已重启，请核对逐项结果和目录后再操作；未自动重试')
                changed = True
        if changed:
            self._save()

    def _save(self):
        temporary = self.root / (uuid.uuid4().hex + '.pending')
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.jobs, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _transfer_operator(self):
        with self.lock:
            if self._transfer is None:
                self._transfer = CrossFileTransfer(self.workspace, self.operators['local'],
                                                  self.operators['netdisk'], self.root / 'transfers')
            return self._transfer

    def plan(self, scope, operation, items, destination=None, name=None, destination_scope=None):
        if scope not in self.operators:
            raise AssetLibraryError('请选择本地资源或百度网盘')
        destination_scope = destination_scope or scope
        if destination_scope not in self.operators:
            raise AssetLibraryError('目标必须是本地工程或百度网盘')
        transfer = destination_scope != scope
        if transfer and (operation not in ('copy', 'move') or name is not None):
            raise AssetLibraryError('跨端操作支持复制或剪切后粘贴')
        with self.lock:
            if self.closed:
                raise AssetLibraryError('文件服务正在停止', 409)
        internal = (self._transfer_operator().plan(scope, destination_scope, operation, items, destination)
                    if transfer else self.operators[scope].plan(operation, items, destination=destination, name=name))
        public = _public(copy.deepcopy(internal))
        identifier = str(uuid.uuid4())
        job_scope = 'transfer' if transfer else scope
        public.update(planId=identifier, scope=job_scope, operation=operation,
                      expiresAt=time.time() + self.PLAN_TTL)
        if transfer:
            public.update(sourceScope=scope, destinationScope=destination_scope)
        canonical = json.dumps(public, sort_keys=True, ensure_ascii=False,
                               separators=(',', ':')).encode()
        digest = hashlib.sha256(secrets.token_bytes(32) + canonical).hexdigest()
        public['planDigest'] = digest
        with self.lock:
            if self.closed:
                raise AssetLibraryError('文件服务正在停止', 409)
            now = time.time()
            self.plans = {key: item for key, item in self.plans.items()
                          if item['public']['expiresAt'] > now or item.get('jobId')}
            if len(self.plans) >= 200:
                unused = next((key for key, item in self.plans.items() if item.get('jobId')), None)
                if unused:
                    del self.plans[unused]
                else:
                    raise AssetLibraryError('待确认操作较多，请完成现有操作后继续', 409)
            self.plans[identifier] = {'scope': job_scope, 'sourceScope': scope,
                                      'destinationScope': destination_scope, 'operation': operation,
                                      'internal': internal, 'public': public}
        return copy.deepcopy(public)

    def _queue(self, scope, operation, name, work, *, plan_id=None, items=None,
               source_scope=None, destination_scope=None):
        if self.closed:
            raise AssetLibraryError('文件服务正在停止', 409)
        if sum(job['state'] in ('queued', 'running') for job in self.jobs.values()) >= 16:
            raise AssetLibraryError('文件任务较多，请等待当前操作完成', 409)
        identifier = str(uuid.uuid4())
        job = {'jobId': identifier, 'scope': scope, 'operation': operation,
               'name': name, 'state': 'queued', 'createdAt': time.time(),
               'updatedAt': time.time(), 'items': items or []}
        if plan_id:
            job['planId'] = plan_id
        if scope == 'transfer':
            job.update(sourceScope=source_scope, destinationScope=destination_scope,
                       cancelSupported=True, cancelRequested=False,
                       progress={'phase':'queued', 'bytesTransferred':0, 'completedFiles':0})
            self._cancellations[identifier] = threading.Event()
        self.jobs[identifier] = job
        self._save()
        self.pool.submit(self._run, identifier, work)
        return copy.deepcopy(job)

    def execute(self, plan_id, plan_digest, expected_scope=None, expected_source_scope=None):
        with self.lock:
            value = self.plans.get(plan_id)
            if value is None:
                raise AssetLibraryError('操作预览已失效，请重新选择文件', 409)
            if not isinstance(plan_digest, str) or not hmac.compare_digest(
                    plan_digest, value['public']['planDigest']):
                raise AssetLibraryError('操作与预览不一致，请重新预览', 409)
            if expected_scope and value['scope'] != expected_scope:
                raise AssetLibraryError('此工具不能执行另一资源范围的操作', 403)
            if expected_source_scope and value['sourceScope'] != expected_source_scope:
                raise AssetLibraryError('此传输工具不能执行另一来源范围的操作', 403)
            if value.get('jobId'):
                return self.job(value['jobId'], expected_scope)
            if time.time() >= value['public']['expiresAt']:
                raise AssetLibraryError('操作预览已过期，请重新预览', 409)
            transfer = value['scope'] == 'transfer'
            operator = self._transfer_operator() if transfer else self.operators[value['scope']]
            work = ((lambda progress, cancelled: operator.execute(value['internal'], progress=progress, cancelled=cancelled))
                    if transfer else (lambda: operator.execute(value['internal'])))
            result = self._queue(value['scope'], value['operation'], '文件操作',
                                 work, plan_id=plan_id, items=value['public'].get('items', []),
                                 source_scope=value['sourceScope'], destination_scope=value['destinationScope'])
            value['jobId'] = result['jobId']
            return result

    def restore(self, trash_id):
        with self.lock:
            return self._queue('local', 'restore', '恢复已删除文件',
                               lambda: self.operators['local'].restore(trash_id))

    def trash(self):
        return self.operators['local'].list_trash()

    def _progress(self, identifier, fields):
        if not isinstance(fields, dict):return
        fields = _public(copy.deepcopy(fields))
        # Provider URLs, tokens and private staging paths are never progress
        # parameters. Operators report only user-visible names and counters.
        allowed = {'phase','bytesTransferred','totalBytes','fileCount','completedFiles',
                   'verifiedFiles','currentFile','items','verificationBytes','verificationTotalBytes'}
        fields = {key:value for key,value in fields.items() if key in allowed}
        json.dumps(fields, ensure_ascii=False, allow_nan=False)
        with self.lock:
            job = self.jobs[identifier]
            old_phase = job.get('progress', {}).get('phase')
            job.setdefault('progress', {}).update(fields)
            job['updatedAt'] = time.time()
            now = time.monotonic()
            if now-self._progress_writes.get(identifier,0) >= .5 or fields.get('phase',old_phase) != old_phase:
                self._progress_writes[identifier] = now
                self._save()

    def cancel(self, identifier, expected_source_scope=None):
        with self.lock:
            job = self.job(identifier, expected_scope='transfer', expected_source_scope=expected_source_scope)
            if job['state'] in ('queued','running'):
                self._cancellations[identifier].set()
                self.jobs[identifier].update(cancelRequested=True, updatedAt=time.time())
                self._save()
            return self.job(identifier, expected_scope='transfer', expected_source_scope=expected_source_scope)

    def _run(self, identifier, work):
        with self.lock:
            job = self.jobs[identifier]
            event = self._cancellations.get(identifier)
            if self.closed or event is not None and event.is_set():
                job.update(state='interrupted' if self.closed else 'cancelled',
                           error='服务正在停止，未开始执行' if self.closed else '已取消，源文件保留', updatedAt=time.time())
                if job['scope']=='transfer':
                    job.update(result={'items':[],'sourcePreserved':True,'sourceCleanupState':'retained','moveCommitted':False})
                self._save()
                return
            job.update(state='running', updatedAt=time.time())
            self._save()
        try:
            result = _public(work(lambda fields:self._progress(identifier,fields),
                                  lambda:event.is_set() or self.closed)
                             if job['scope']=='transfer' else work())
            items = result.get('items', [])
            states = [item.get('state') for item in items]
            completed = sum(state == 'completed' for state in states)
            if states:
                state = 'completed' if completed == len(states) else 'completed-with-errors' if completed else 'failed'
            else:
                state = result.get('state', 'completed')
                if state not in ('completed', 'completed-with-errors', 'failed'):
                    state = 'failed'
            if job['scope']=='transfer':
                if result.get('state')=='cancelled':state='cancelled'
                elif job['operation']=='move' and not (result.get('moveCommitted') is True and result.get('sourceCleanupState')=='completed'):
                    state='completed-with-errors' if completed else 'failed'
            with self.lock:
                job.update(state=state, result=result, completedCount=completed,
                           failedCount=sum(item in ('failed', 'unknown') for item in states),
                           skippedCount=states.count('skipped'), updatedAt=time.time())
                self._save()
        except Exception as error:
            with self.lock:
                job.update(state='cancelled' if event is not None and event.is_set() else 'failed',
                           error=self.workspace.public_error(error), updatedAt=time.time())
                self._save()

    def job(self, identifier, expected_scope=None, expected_source_scope=None):
        with self.lock:
            job = self.jobs.get(identifier)
            if job is None:
                raise AssetLibraryError('文件操作任务不存在', 404)
            if expected_scope and job['scope'] != expected_scope:
                raise AssetLibraryError('此工具不能读取另一资源范围的任务', 403)
            if expected_source_scope and job.get('sourceScope') != expected_source_scope:
                raise AssetLibraryError('此传输工具不能读取另一来源范围的任务', 403)
            return copy.deepcopy(job)

    def list_jobs(self, scope=None):
        with self.lock:
            items = [copy.deepcopy(job) for job in reversed(list(self.jobs.values()))
                     if scope is None or job['scope'] == scope]
        return {'items': items[:100]}

    def list_transfer_jobs(self, source_scope=None):
        return {'items':[job for job in self.list_jobs('transfer')['items']
                         if source_scope is None or job.get('sourceScope')==source_scope]}

    def netdisk_active(self):
        with self.lock:
            return any(job['state'] in ('queued','running') and job['scope'] in ('netdisk','transfer')
                       for job in self.jobs.values())

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for identifier,event in self._cancellations.items():
                if self.jobs[identifier]['state'] in ('queued','running'):event.set()
        self.pool.shutdown(wait=True)
