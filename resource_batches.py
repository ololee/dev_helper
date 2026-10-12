"""Temporary resource ZIP jobs; immutable attachments remain in their own store."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import re
import threading
import time
import uuid
import zipfile
from importlib import import_module

def _knowledge():
    return import_module((__package__ + '.' if __package__ else '') + 'knowledge')


def selected_items(fields):
    KnowledgeError, _identifier = _knowledge().KnowledgeError, _knowledge()._identifier
    if not isinstance(fields, dict) or set(fields) != {'items'} or not isinstance(fields['items'], list):
        raise KnowledgeError('items must be an array')
    if not 1 <= len(fields['items']) <= 100:
        raise KnowledgeError('一次选择 1–100 条资源')
    seen, result = set(), []
    for item in fields['items']:
        if not isinstance(item, dict) or set(item) != {'source', 'id'} or item['source'] not in ('attachment', 'artifact'):
            raise KnowledgeError('Invalid resource selection')
        identifier = _identifier(item['id'])
        key = (item['source'], identifier)
        if key in seen:
            raise KnowledgeError('Duplicate resource selection')
        seen.add(key)
        result.append(dict(item))
    return result


class ResourceBatchExports:
    def __init__(self, store):
        self.store = store
        self.root = store.root / 'resource-batches'
        self.root.mkdir(exist_ok=True)
        for file in self.root.iterdir():
            if re.fullmatch(r'[0-9a-f-]{36}\.(zip|pending)', file.name) and not file.is_symlink():
                file.unlink()
        self.jobs = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='resource-batch')

    def start(self, fields):
        KnowledgeError = _knowledge().KnowledgeError
        items = selected_items(fields)
        with self.lock:
            if sum(j['state'] in ('queued', 'running') for j in self.jobs.values()) >= 8:
                raise KnowledgeError('导出任务较多，请等待已有任务完成', 409)
            for identifier in list(self.jobs):
                if len(self.jobs) < 32:
                    break
                if self.jobs[identifier]['state'] in ('completed', 'failed'):
                    (self.root / (identifier + '.zip')).unlink(missing_ok=True)
                    del self.jobs[identifier]
            identifier = str(uuid.uuid4())
            self.jobs[identifier] = dict(jobId=identifier, state='queued', progress=0, fileCount=len(items))
            self.pool.submit(self._run, identifier, items)
            return dict(self.jobs[identifier])

    def status(self, identifier):
        KnowledgeError, _identifier = _knowledge().KnowledgeError, _knowledge()._identifier
        _identifier(identifier)
        with self.lock:
            if identifier not in self.jobs:
                raise KnowledgeError('Resource export task not found', 404)
            return dict(self.jobs[identifier])

    def file(self, identifier):
        KnowledgeError = _knowledge().KnowledgeError
        if self.status(identifier)['state'] != 'completed':
            raise KnowledgeError('资源导出尚未完成', 409)
        file = self.root / (identifier + '.zip')
        if not file.is_file():
            raise KnowledgeError('Resource export file not found', 404)
        return file

    def _update(self, identifier, **fields):
        with self.lock:
            self.jobs[identifier].update(fields, updatedAt=time.time())

    def _run(self, identifier, items):
        KnowledgeError = _knowledge().KnowledgeError
        pending = self.root / (identifier + '.pending')
        final = self.root / (identifier + '.zip')
        current, total = '', 0
        self._update(identifier, state='running')
        try:
            with pending.open('xb') as raw:
                with zipfile.ZipFile(raw, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                    for index, item in enumerate(items, 1):
                        current = item['id']
                        if item['source'] != 'attachment':
                            raise KnowledgeError('请选择这台电脑上的附件，手机捕获资源请在手机侧导出')
                        with self.store.lock:
                            metadata, file = self.store._attachment(item['id'])
                            self.store._media_pins[item['id']] = self.store._media_pins.get(item['id'], 0) + 1
                        try:
                            current = metadata['name'] + ' (' + item['id'] + ')'
                            expected = file.stat()
                            name = re.sub(r'[\x00-\x1f\x7f/\\\u202a-\u202e\u2066-\u2069]', '_', metadata['name'])
                            if not name or name in ('.', '..'):
                                name = 'file'
                            copied = 0
                            with archive.open(f'resources/{index:03d}_{name}', 'w', force_zip64=True) as output, file.open('rb') as source:
                                while chunk := source.read(65536):
                                    output.write(chunk)
                                    copied += len(chunk)
                            after = file.stat()
                            if copied != expected.st_size or after.st_size != expected.st_size or after.st_mtime_ns != expected.st_mtime_ns:
                                raise KnowledgeError('资源在导出期间发生变化')
                            total += copied
                        finally:
                            with self.store.lock:
                                count = self.store._media_pins[item['id']] - 1
                                if count:
                                    self.store._media_pins[item['id']] = count
                                else:
                                    del self.store._media_pins[item['id']]
                        self._update(identifier, progress=index * 99 // len(items))
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(pending, final)
            self._update(identifier, state='completed', progress=100, sourceBytes=total, bytes=final.stat().st_size,
                         downloadPath=f'/api/knowledge/resources/batch-export/{identifier}/content')
        except BaseException as error:
            cleanup_error = ''
            try:
                pending.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
            except OSError as cleanup:
                cleanup_error = '; 临时文件清理失败: ' + str(cleanup)
            self._update(identifier, state='failed', error=current + ': ' + str(error) + cleanup_error)
            if not isinstance(error, Exception):
                raise

    def close(self):
        self.pool.shutdown(wait=True)
