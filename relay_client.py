"""Outgoing HTTP relay transport; catalogs never contain private document bodies."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import ipaddress
import json
import json as jsonlib
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit
import uuid

import httpx

from devices import local_ips, validate_url

TRANSPORT = contextvars.ContextVar('devhelper_transport', default='auto')
TRANSFER_JOB = contextvars.ContextVar('devhelper_transfer_job', default=None)
EXCHANGE = contextvars.ContextVar('devhelper_relay_exchange', default=None)
BOUND_GENERATION = contextvars.ContextVar('devhelper_relay_generation', default=None)
HEADER = 'X-DevHelper-Workspace'
ALLOWED = ('/api/knowledge/', '/api/workflows/')


class RelayPending(RuntimeError):
    def __init__(self, value):
        self.value = value
        self.result = dict(request=value, succeeded=False)
        super().__init__('请求仍在排队或送达状态；尚未确认执行成功。')


class DeliveryUnknown(RuntimeError):
    def __init__(self, identifier):
        self.value = dict(id=identifier, state='delivery_unknown', succeeded=False)
        self.result = dict(request=self.value, succeeded=False)
        super().__init__('请求可能已经送达，但未收到确认；未自动重放，请先检查目标设备。')


def identifier(value):
    parsed = str(uuid.UUID(value))
    if parsed != value:
        raise ValueError('A canonical UUID is required')
    return value


def server_url(value):
    root = validate_url(value)
    parsed = urlsplit(root)
    if parsed.scheme == 'http' and parsed.hostname != 'localhost':
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            raise ValueError('公网中转服务器需要 HTTPS。') from None
        if not (address.is_private or address.is_loopback):
            raise ValueError('公网中转服务器需要 HTTPS。')
    return root


@contextlib.contextmanager
def route_mode(mode='auto'):
    if mode not in ('auto', 'lan', 'relay'):
        raise ValueError('transport must be auto, lan or relay')
    token = TRANSPORT.set(mode)
    try:
        yield
    finally:
        TRANSPORT.reset(token)


def clean_catalog(value):
    allowed = {'id', 'kind', 'title', 'revision', 'hash', 'sha256', 'bytes', 'name', 'mimeType',
               'status', 'phase', 'updatedAt', 'createdAt', 'deleted', 'enabled', 'autoLoad'}
    return {key: [{k: v for k, v in row.items() if k in allowed and isinstance(v, (str, int, bool, type(None)))}
                  for row in value.get(key, []) if isinstance(row, dict)]
            for key in ('documents', 'attachments', 'tasks', 'schedules')}


class RelayClient:
    def __init__(self, desktop, data_dir):
        self.desktop = desktop
        self.root = Path(data_dir) / 'relay'
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / 'config.json'
        self.config = dict(serverUrl='', workspaceId='', enabled=False, sameLan=True, name='这台 Mac', targetDeviceId='')
        if self.path.is_file():
            self.config.update(json.loads(self.path.read_text()))
        self.db = sqlite3.connect(self.root / 'client.sqlite3', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS incoming(id TEXT PRIMARY KEY,digest TEXT NOT NULL,state TEXT NOT NULL,response TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS outgoing(id TEXT PRIMARY KEY,value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS reply_outbox(id TEXT PRIMARY KEY,server TEXT,workspace TEXT,reply TEXT)')
        # Never replay an operation whose delivery/execution was interrupted.
        self.db.execute("UPDATE incoming SET state='failed',response=? WHERE state='running'",
                        (json.dumps(dict(status=503, body={'error': 'Interrupted execution; not replayed', 'state': 'delivery_unknown'})),))
        for row in self.db.execute('SELECT id,value FROM transfers').fetchall():
            value = json.loads(row[1])
            if value['state'] in ('pending', 'running'):
                value.update(state='failed', error='服务重启中断了此传输；未自动重放。', succeeded=False)
                self.db.execute('UPDATE transfers SET value=? WHERE id=?', (json.dumps(value), row[0]))
        self.db.commit()
        self.tasks = []
        self.jobs = {}
        self.error = None
        self.online = False
        self.peers = []
        self.last_catalog = None
        self.last_route = None
        self.stop_event = False
        self.generation = self.config_generation()
        self.changed = asyncio.Event()

    def config_generation(self):
        # Stable across restarts, so an unchanged pending workflow can resume.
        return hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

    @property
    def device_id(self):
        return self.desktop.preferences.get()['deviceId']

    def headers(self):
        generation = BOUND_GENERATION.get()
        if generation is not None and generation != self.generation:
            raise RuntimeError('设备或连接设置已变化；原任务不会发送到新设备。')
        scope = EXCHANGE.get()
        if scope and scope != (self.config['serverUrl'], self.config['workspaceId']):
            raise RuntimeError('连接设置已变化；旧内容没有发送到新的配对空间。')
        return {HEADER: self.config['workspaceId']}

    def public_config(self):
        return {k: v for k, v in self.config.items() if k != 'workspaceId'} | dict(
            workspaceConfigured=bool(self.config['workspaceId']),
            workspacePreview=self.config['workspaceId'][:8] + '…' if self.config['workspaceId'] else '')

    def set_config(self, fields):
        if not isinstance(fields, dict) or set(fields) - set(self.config):
            raise ValueError('Unknown relay setting')
        current = dict(self.config)
        for key, value in fields.items():
            if key in ('enabled', 'sameLan'):
                if not isinstance(value, bool):
                    raise ValueError(key + ' must be boolean')
            elif not isinstance(value, str):
                raise ValueError(key + ' must be text')
            if key == 'serverUrl' and value:
                value = server_url(value)
            if key in ('workspaceId', 'targetDeviceId') and value:
                identifier(value)
            if key == 'name' and (not value.strip() or len(value) > 100):
                raise ValueError('Device name must contain 1–100 characters')
            current[key] = value
        if current['enabled'] and not (current['serverUrl'] and current['workspaceId']):
            raise ValueError('请先填写中转地址和连接码。')
        if current == self.config:
            return self.public_config()
        self.config = current
        pending = self.path.with_suffix('.tmp')
        pending.write_text(json.dumps(current, ensure_ascii=False))
        pending.chmod(0o600)
        pending.replace(self.path)
        self.generation = self.config_generation()
        self.online = False
        self.peers = []
        self.error = None
        self.changed.set()
        self.desktop.android_checked = 0
        self.desktop.clipboard.reset_sync_baseline()
        return self.public_config()

    def enabled(self):
        return self.config['enabled']

    def check_workspace(self, supplied):
        return bool(self.enabled() and self.config['workspaceId'] and supplied
                    and secrets.compare_digest(supplied, self.config['workspaceId']))

    def status(self):
        rows = [json.loads(row[0]) for row in self.db.execute('SELECT value FROM transfers ORDER BY rowid DESC LIMIT 50')]
        return dict(config=self.public_config(), connected=self.online, error=self.error, deviceId=self.device_id,
                    transport=self.last_route, devices=self.peers, transfers=rows,
                    policy='metadata_auto_content_manual', catalogUpdatedAt=self.last_catalog)

    async def api(self, method, path, **kwargs):
        if not self.enabled():
            raise RuntimeError('中转连接尚未开启。')
        scope = EXCHANGE.get()
        if scope and scope != (self.config['serverUrl'], self.config['workspaceId']):
            raise RuntimeError('连接设置在执行期间已变化；未向新空间发送旧请求内容。')
        response = await self.desktop.http.request(method, self.config['serverUrl'] + path,
            headers={**self.headers(), **kwargs.pop('headers', {})}, **kwargs)
        if response.is_error:
            raise RuntimeError('中转请求未完成（HTTP ' + str(response.status_code) + '）。')
        return response

    async def create_workspace(self, root):
        root = server_url(root)
        response = await self.desktop.http.post(root + '/api/relay/workspaces', json={}, timeout=15)
        response.raise_for_status()
        value = response.json()
        identifier(value['workspaceId'])
        return value

    def local_catalog(self):
        store = self.desktop.knowledge
        with store.lock:
            attachments = [json.loads(row[0]) for row in store.db.execute('SELECT metadata FROM attachments')]
            documents = store.sync_manifest()['records']
        tasks = self.desktop.workflows.list_tasks(0, 100)['tasks']
        schedules = store.list_schedules()['schedules']
        return clean_catalog(dict(documents=documents, attachments=attachments, tasks=tasks, schedules=schedules))

    async def register(self):
        ips = await asyncio.to_thread(local_ips)
        metadata = dict(deviceId=self.device_id, platform='mac', name=self.config['name'], sameLan=self.config['sameLan'],
                        lanAddresses=[f'http://{ip}:{self.desktop.port}' for ip in ips],
                        catalog=await asyncio.to_thread(self.local_catalog))
        generation = self.generation
        await self.api('POST', '/api/relay/heartbeat', json=metadata, timeout=15)
        if generation == self.generation:
            self.online = True
            self.error = None
            self.last_catalog = int(time.time() * 1000)

    async def devices(self):
        result = (await self.api('GET', '/api/relay/devices', timeout=15)).json()
        self.peers = result.get('devices', [])
        return result

    async def catalog(self, peer_id):
        identifier(peer_id)
        result = (await self.api('GET', '/api/relay/catalog/' + peer_id, timeout=15)).json()
        result['catalog'] = clean_catalog(result.get('catalog', {}))
        return result

    async def peer(self):
        await self.devices()
        candidates = [v for v in self.peers if v.get('platform') == 'android' and v['deviceId'] != self.device_id]
        chosen = self.config['targetDeviceId'] or self.desktop.preferences.get().get('androidDeviceId', '')
        if chosen:
            candidates = [v for v in candidates if v['deviceId'] == chosen]
        if len(candidates) != 1:
            raise RuntimeError('请选择一个已连接到相同中转的 Android 设备。')
        return candidates[0]

    async def route(self, mode=None):
        mode = mode or TRANSPORT.get()
        if mode not in ('auto', 'lan', 'relay'):
            raise ValueError('Invalid transport')
        peer = await self.peer()
        # OFF is authoritative even when the caller asks for LAN explicitly.
        allow_lan = self.config['sameLan'] and peer.get('sameLan') is True and peer.get('online') is True
        if mode != 'relay' and allow_lan:
            for candidate in peer.get('lanAddresses', []):
                try:
                    origin = validate_url(candidate)
                    response = await self.desktop.http.get(origin + '/api/relay/identity', headers=self.headers(), timeout=2)
                    identity = response.json()
                    if response.status_code == 200 and identity.get('deviceId') == peer['deviceId'] and identity.get('platform') == 'android':
                        self.last_route = 'lan'
                        return dict(type='lan', origin=origin, peer=peer)
                except (httpx.HTTPError, ValueError):
                    continue
        if mode == 'lan' and allow_lan:
            raise RuntimeError('指定的局域网通道不可达或设备身份不匹配。')
        self.last_route = 'relay'
        return dict(type='relay', peer=peer)

    async def available(self):
        try:
            peer = await self.peer()
            return bool(peer.get('online'))
        except (RuntimeError, httpx.HTTPError, ValueError):
            return False

    async def upload_blob(self, content, headers=None, name='transfer'):
        response = await self.api('POST', '/api/relay/blobs', params={'deviceId': self.device_id, 'name': name},
                                  content=content, headers=headers or {}, timeout=300)
        value = response.json()
        identifier(value['id'])
        if not isinstance(value.get('bytes'), int) or len(value.get('sha256', '')) != 64:
            raise RuntimeError('Invalid relay blob metadata')
        return value

    async def blob_response(self, value, method='GET', headers=None):
        blob = value.get('blob') or {}
        blob_id = value.get('blobId')
        identifier(blob_id)
        request = self.desktop.http.build_request(method, self.config['serverUrl'] + '/api/relay/blobs/' + blob_id,
            headers={**self.headers(), **(headers or {})}, timeout=300)
        response = await self.desktop.http.send(request, stream=True)
        expected = blob.get('sha256')
        if expected and response.headers.get('x-content-sha256') != expected:
            await response.aclose()
            raise RuntimeError('中转文件摘要不匹配，未导入。')
        if expected and method == 'GET' and response.status_code == 200:
            original = response
            class CheckedStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    digest = hashlib.sha256()
                    async for chunk in original.aiter_bytes(65536):
                        digest.update(chunk)
                        yield chunk
                    if digest.hexdigest() != expected:
                        raise RuntimeError('中转文件字节校验失败，未提交附件。')
                async def aclose(self):
                    await original.aclose()
            response = httpx.Response(original.status_code, headers={k: v for k, v in original.headers.items() if k.lower() != 'content-encoding'},
                stream=CheckedStream(), request=original.request)
        return response

    async def wait_request(self, request_id, wait_seconds=120):
        job = TRANSFER_JOB.get()
        if job:
            wait_seconds = 86400
        deadline = time.monotonic() + wait_seconds
        value = None
        while time.monotonic() < deadline:
            response = await self.api('GET', '/api/relay/requests/' + request_id,
                params={'sourceId': self.device_id, 'wait': min(25, max(0, int(deadline - time.monotonic())))}, timeout=30)
            value = response.json()
            if job:
                job.update(state='pending' if value['state'] == 'pending' else 'running',
                    request={k: value[k] for k in ('id', 'state', 'createdAt', 'updatedAt') if k in value})
                self.save_transfer(job)
            if value['state'] == 'completed':
                return value
            if value['state'] in ('failed', 'cancelled'):
                raise RuntimeError('中转请求已' + value['state'] + '；没有自动重放。')
            await asyncio.sleep(.05)
        raise RelayPending(value or dict(id=request_id, state='pending', succeeded=False))

    async def request(self, method, path, *, json=None, content=None, headers=None, params=None, stream=False, timeout=120, transport=None, request_id=None):
        scope = EXCHANGE.get()
        token = EXCHANGE.set(scope or (self.config['serverUrl'], self.config['workspaceId']))
        try:
            return await self._request(method, path, json=json, content=content, headers=headers, params=params,
                stream=stream, timeout=timeout, transport=transport, request_id=request_id)
        finally:
            EXCHANGE.reset(token)

    async def _request(self, method, path, *, json=None, content=None, headers=None, params=None, stream=False, timeout=120, transport=None, request_id=None):
        if not (path == '/mcp' or path.startswith(ALLOWED)) or method not in ('GET', 'HEAD', 'POST', 'DELETE'):
            raise ValueError('Relay path or method is not supported')
        if params:
            path += ('&' if '?' in path else '?') + urlencode(params)
        chosen = await self.route(transport)
        request_id = identifier(request_id) if request_id else str(uuid.uuid4())
        self.db.execute('INSERT OR REPLACE INTO outgoing VALUES(?,?)', (request_id, jsonlib.dumps(
            dict(id=request_id, targetId=chosen['peer']['deviceId'], transport=chosen['type'], origin=chosen.get('origin'), createdAt=int(time.time() * 1000)))))
        self.db.commit()
        envelope = dict(id=request_id, sourceId=self.device_id, targetId=chosen['peer']['deviceId'], method=method, path=path)
        filtered = {k.lower(): v for k, v in (headers or {}).items() if k.lower() in ('content-type', 'range')}
        if filtered:
            envelope['headers'] = filtered
        if json is not None:
            envelope['body'] = json
        if chosen['type'] == 'lan':
            try:
                if content is not None or (stream and json is None):
                    request = self.desktop.http.build_request(method, chosen['origin'] + path,
                        headers={**self.headers(), **(headers or {})}, content=content, timeout=timeout)
                    return await self.desktop.http.send(request, stream=stream)
                response = await self.desktop.http.post(chosen['origin'] + '/api/relay/execute',
                    json=envelope, headers=self.headers(), timeout=timeout)
                response.raise_for_status()
                value = response.json()
                if value.get('state') != 'completed':
                    raise RelayPending(value)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if transport == 'lan' or TRANSPORT.get() == 'lan':
                    raise
                # No bytes reached the peer. Reuse the same operation identifier.
                self.last_route = 'relay'
                chosen['type'] = 'relay'
            except (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout) as error:
                raise DeliveryUnknown(request_id) from error
        if chosen['type'] == 'relay':
            if content is not None:
                blob = await self.upload_blob(content, headers=filtered)
                envelope['blobId'] = blob['id']
            accepted = (await self.api('POST', '/api/relay/requests', json=envelope, timeout=30)).json()
            value = accepted if accepted.get('state') == 'completed' else await self.wait_request(request_id, timeout)
        result = value.get('response')
        if not isinstance(result, dict):
            raise RuntimeError('目标设备没有返回有效执行结果。')
        if result.get('blobId'):
            response = await self.blob_response(result, method, headers=filtered)
            if not stream:
                await response.aread()
                await response.aclose()
            return response
        response = httpx.Response(result['status'], json=result.get('body', {}), headers=result.get('headers', {}),
                                  request=httpx.Request(method, 'http://relay.invalid' + path))
        return response

    def incoming_status(self, request_id):
        row = self.db.execute('SELECT state,response FROM incoming WHERE id=?', (identifier(request_id),)).fetchone()
        if not row:
            return None
        return dict(id=request_id, state='completed' if row[1] else 'delivered', response=json.loads(row[1]) if row[1] else None)

    async def request_status(self, request_id):
        row = self.db.execute('SELECT value FROM outgoing WHERE id=?', (identifier(request_id),)).fetchone()
        if not row:
            raise ValueError('Outgoing request not found')
        saved = json.loads(row[0])
        if saved['transport'] == 'lan' and saved.get('origin'):
            try:
                response = await self.desktop.http.get(saved['origin'] + '/api/relay/identity', headers=self.headers(), timeout=2)
                if response.status_code == 200 and response.json().get('deviceId') == saved['targetId']:
                    response = await self.desktop.http.get(saved['origin'] + '/api/relay/execute/' + request_id, headers=self.headers(), timeout=10)
                    if response.status_code == 200:
                        return response.json()
            except (httpx.HTTPError, ValueError):
                pass
        try:
            return (await self.api('GET', '/api/relay/requests/' + request_id, params={'sourceId': self.device_id, 'wait': 0}, timeout=10)).json()
        except (RuntimeError, httpx.HTTPError):
            return dict(id=request_id, state='delivery_unknown', succeeded=False)

    async def receive(self, envelope):
        identifier(envelope['id']); identifier(envelope['sourceId'])
        if envelope.get('targetId') != self.device_id:
            raise ValueError('Wrong target device')
        if not (envelope.get('path') == '/mcp' or envelope.get('path', '').startswith(ALLOWED)):
            raise ValueError('Unsupported relay endpoint')
        if envelope.get('method') not in ('GET', 'HEAD', 'POST', 'DELETE'):
            raise ValueError('Unsupported relay method')
        canonical = {k: envelope[k] for k in ('id', 'sourceId', 'targetId', 'method', 'path', 'body', 'blobId') if k in envelope}
        canonical['headers'] = envelope.get('headers') or {}
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        row = self.db.execute('SELECT digest,state,response FROM incoming WHERE id=?', (envelope['id'],)).fetchone()
        if row:
            if row[0] != digest:
                return dict(id=envelope['id'], state='failed', response=dict(status=409, body={'error': 'Request ID conflict'}))
            return dict(id=envelope['id'], state='completed' if row[2] else 'delivered', response=json.loads(row[2]) if row[2] else None)
        self.db.execute('INSERT INTO incoming VALUES(?,?,?,NULL)', (envelope['id'], digest, 'running'))
        self.db.commit()
        exchange = EXCHANGE.set((self.config['serverUrl'], self.config['workspaceId']))
        try:
            response = await self.desktop.relay_execute(envelope)
        except asyncio.CancelledError:
            response = dict(status=503, body={'error': 'Interrupted; execution not replayed', 'state': 'delivery_unknown'})
            self.db.execute('UPDATE incoming SET state=?,response=? WHERE id=?', ('failed', json.dumps(response), envelope['id']))
            self.db.commit()
            raise
        except Exception:
            response = dict(status=503, body={'error': '目标设备执行失败，未自动重放。'})
        finally:
            EXCHANGE.reset(exchange)
        self.db.execute('UPDATE incoming SET state=?,response=? WHERE id=?', ('completed', json.dumps(response, ensure_ascii=False), envelope['id']))
        self.db.commit()
        return dict(id=envelope['id'], state='completed', response=response)

    async def heartbeat_loop(self):
        while not self.stop_event:
            self.changed.clear()
            if self.enabled():
                try:
                    await self.register()
                except (httpx.HTTPError, RuntimeError, ValueError):
                    self.online = False
                    self.error = '中转服务器未连接，请检查地址和连接码。'
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    async def inbox_loop(self):
        while not self.stop_event:
            if not self.enabled():
                await asyncio.sleep(1)
                continue
            try:
                generation = self.generation
                server, workspace = self.config['serverUrl'], self.config['workspaceId']
                value = (await self.api('GET', '/api/relay/inbox', params={'deviceId': self.device_id, 'wait': 25}, timeout=30)).json()
                if generation != self.generation:
                    continue
                for request in value.get('requests', []):
                    result = await self.receive(request)
                    response = result.get('response') or dict(status=503, body={'error': 'Already delivered, result unavailable'})
                    reply = dict(deviceId=self.device_id, **{k: v for k, v in response.items() if k in ('status', 'headers', 'body', 'blobId')})
                    self.db.execute('INSERT OR REPLACE INTO reply_outbox VALUES(?,?,?,?)',
                        (request['id'], server, workspace, json.dumps(reply, ensure_ascii=False)))
                    self.db.commit()
                    await self.flush_replies()
            except (httpx.HTTPError, RuntimeError, ValueError):
                await asyncio.sleep(2)

    async def flush_replies(self):
        if not self.enabled():
            return
        rows = self.db.execute('SELECT id,reply FROM reply_outbox WHERE server=? AND workspace=?',
            (self.config['serverUrl'], self.config['workspaceId'])).fetchall()
        generation = self.generation
        for request_id, encoded in rows:
            if generation != self.generation:
                return
            response = await self.desktop.http.post(self.config['serverUrl'] + '/api/relay/replies/' + request_id,
                headers=self.headers(), json=json.loads(encoded), timeout=15)
            if response.status_code in (200, 201, 404, 409):
                # Acknowledged, expired, or irreversibly failed delivery. Never execute again.
                self.db.execute('DELETE FROM reply_outbox WHERE id=?', (request_id,))
                self.db.commit()
            elif response.is_error:
                raise RuntimeError('执行结果尚未回传。')

    async def reply_loop(self):
        while not self.stop_event:
            try:
                await self.flush_replies()
            except (httpx.HTTPError, RuntimeError, ValueError):
                pass
            await asyncio.sleep(3)

    def save_transfer(self, value):
        value['updatedAt'] = int(time.time() * 1000)
        self.db.execute('INSERT OR REPLACE INTO transfers VALUES(?,?)', (value['id'], json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def transfer_status(self, task_id):
        row = self.db.execute('SELECT value FROM transfers WHERE id=?', (identifier(task_id),)).fetchone()
        if not row:
            raise ValueError('Transfer not found')
        return json.loads(row[0])

    def transfer(self, fields):
        allowed = {'kind', 'source', 'target', 'id', 'transport', 'direction', 'text'}
        if not isinstance(fields, dict) or set(fields) - allowed:
            raise ValueError('Invalid transfer fields')
        if fields.get('kind') not in ('document', 'attachment', 'sync', 'clipboard'):
            raise ValueError('请选择资料、文件或剪贴板传输。')
        if fields.get('source', 'android') not in ('mac', 'android') or fields.get('target', 'mac') not in ('mac', 'android'):
            raise ValueError('请选择有效设备。')
        if fields['kind'] in ('document', 'attachment'):
            identifier(fields.get('id'))
        with route_mode(fields.get('transport', 'auto')):
            pass
        task = dict(id=str(uuid.uuid4()), kind=fields['kind'], state='pending', succeeded=False, createdAt=int(time.time() * 1000), updatedAt=int(time.time() * 1000))
        self.save_transfer(task)
        self.jobs[task['id']] = asyncio.create_task(self.transfer_run(task, dict(fields)), name='devhelper-manual-transfer')
        return task

    async def transfer_run(self, task, fields):
        job_token = TRANSFER_JOB.set(task)
        generation_token = BOUND_GENERATION.set(self.generation)
        try:
            task['state'] = 'running'; self.save_transfer(task)
            source, target = fields.get('source', 'android'), fields.get('target', 'mac')
            with route_mode(fields.get('transport', 'auto')):
                if fields['kind'] == 'attachment':
                    if source == target:
                        raise ValueError('请选择不同的源与目标。')
                    await self.desktop.sync.transfer_attachment(source, target, fields['id'])
                    result = dict(id=fields['id'], source=source, target=target)
                elif fields['kind'] in ('document', 'sync'):
                    result = await self.desktop.sync.run(fields.get('direction', 'download' if source == 'android' else 'bidirectional'),
                        identifiers=[fields['id']] if fields['kind'] == 'document' else None)
                    if result.get('error') or result.get('conflicts'):
                        raise RuntimeError('同步存在错误或版本冲突，请在资料同步页处理。')
                    result = {k: result[k] for k in ('summary', 'lastSyncAt') if k in result}
                elif 'text' in fields:
                    result = await self.desktop.clipboard.publish(fields['text'], target)
                else:
                    result = await self.desktop.clipboard.pull(source, target)
                if isinstance(result, dict) and result.get('error'):
                    if result.get('request'):
                        if result['request'].get('state') == 'delivery_unknown':
                            raise DeliveryUnknown(result['request']['id'])
                        raise RelayPending(result['request'])
                    raise RuntimeError('传输没有完成，请在剪贴板页查看结果。')
                # Transfer journals contain metadata only, never clipboard bodies.
                task.update(state='completed', succeeded=True, result={k: v for k, v in result.items() if k not in ('text', 'clipboard')})
        except RelayPending as error:
            task.update(state='pending', succeeded=False, request=error.value, error=str(error))
        except DeliveryUnknown as error:
            task.update(state='delivery_unknown', succeeded=False, request=error.value, error=str(error))
        except asyncio.CancelledError:
            task.update(state='failed', succeeded=False, error='服务停止中断了此传输，未自动重放。')
            raise
        except Exception as error:
            task.update(state='failed', succeeded=False, error=str(error)[:300])
        finally:
            BOUND_GENERATION.reset(generation_token)
            TRANSFER_JOB.reset(job_token)
            self.save_transfer(task)
            self.jobs.pop(task['id'], None)

    def start(self):
        self.stop_event = False
        self.tasks = [asyncio.create_task(self.heartbeat_loop()), asyncio.create_task(self.inbox_loop()), asyncio.create_task(self.reply_loop())]

    async def stop(self):
        self.stop_event = True
        running = self.tasks + list(self.jobs.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        self.tasks = []

    def close(self):
        self.db.close()
