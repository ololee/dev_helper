"""Durable HTTP mailbox and streaming transfers; never execute device tools here."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
import uvicorn

MAX_JSON = 1024 * 1024
ONLINE_SECONDS = 75
BLOB_SECONDS = 86400
REQUEST_SECONDS = 86400
DELIVERY_SECONDS = 1800
PAIRING_SECONDS = 300
PAIRING_ATTEMPT_SECONDS = 900


class RelayError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def identifier(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        raise RelayError('A valid UUID is required') from None


def now():
    return int(time.time() * 1000)


def display_name(value, maximum=160):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise RelayError('Invalid display name')
    return value


def safe_path(value):
    from urllib.parse import urlsplit, unquote
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise RelayError('Invalid request path')
    parsed = urlsplit(value)
    path = unquote(parsed.path)
    if parsed.scheme or parsed.netloc or parsed.fragment or '\\' in path or '..' in path.split('/'):
        raise RelayError('Only relative device API paths are supported')
    if path != '/mcp' and not path.startswith(('/api/knowledge/', '/api/workflows/')):
        raise RelayError('This device path is unavailable through the relay')
    return value


def content_headers(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {'content-type', 'range'}:
        raise RelayError('Only content-type and range headers are forwarded')
    result = {}
    for key, item in value.items():
        if not isinstance(item, str) or len(item) > 1024 or '\r' in item or '\n' in item:
            raise RelayError('Invalid forwarded header')
        result[key] = item
    return result


class RelayStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs_dir = self.root / 'blobs'
        self.blobs_dir.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'relay.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,created INTEGER);
            CREATE TABLE IF NOT EXISTS devices(workspace TEXT,id TEXT,metadata TEXT,catalog TEXT,seen INTEGER,
                PRIMARY KEY(workspace,id));
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,workspace TEXT,source TEXT,target TEXT,
                state TEXT,created INTEGER,updated INTEGER,value TEXT,digest TEXT);
            CREATE TABLE IF NOT EXISTS blobs(id TEXT PRIMARY KEY,workspace TEXT,created INTEGER,metadata TEXT);
            CREATE TABLE IF NOT EXISTS pairing_sessions(id TEXT PRIMARY KEY,workspace TEXT,owner TEXT,
                code_hash TEXT,state TEXT,created INTEGER,expires INTEGER);
            CREATE TABLE IF NOT EXISTS pairing_requests(id TEXT PRIMARY KEY,session TEXT,device TEXT,
                platform TEXT,name TEXT,receipt_hash TEXT,digest TEXT,state TEXT,created INTEGER,expires INTEGER);
            CREATE TABLE IF NOT EXISTS pairing_attempts(ip TEXT,created INTEGER,failed INTEGER);
            CREATE INDEX IF NOT EXISTS pairing_code ON pairing_sessions(code_hash,state);
            CREATE INDEX IF NOT EXISTS pairing_ip ON pairing_attempts(ip,created);
        ''')
        with self.db:
            # A process restart cannot establish whether a claimed script or tool ran.
            for row in self.db.execute("SELECT id,value FROM requests WHERE state='delivered'").fetchall():
                value = json.loads(row['value'])
                value.update(state='failed', error='delivery_unknown: relay restarted after delivery; operation was not replayed', updatedAt=now())
                self.db.execute('UPDATE requests SET state=?,updated=?,value=? WHERE id=?', ('failed', now(), json.dumps(value), row['id']))
        for pending in self.blobs_dir.glob('*.pending'):
            pending.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root / 'relay.sqlite3', 0o600)

    def close(self):
        self.db.close()

    def workspace(self, request):
        raw = request.headers.get('x-devhelper-workspace', '')
        try:
            workspace = identifier(raw)
        except RelayError:
            raise RelayError('Enter a valid connection code on both devices', 401) from None
        with self.lock:
            if not self.db.execute('SELECT 1 FROM workspaces WHERE id=?', (workspace,)).fetchone():
                raise RelayError('Connection code is not available', 403)
        return workspace

    def create_workspace(self):
        value = str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute('INSERT INTO workspaces VALUES(?,?)', (value, now()))
        return {'workspaceId': value, 'protocolVersion': 1}

    @staticmethod
    def pairing_hash(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def expire_pairing(self):
        stamp = now()
        self.db.execute("UPDATE pairing_sessions SET state='expired' WHERE state='waiting' AND expires<=?", (stamp,))
        self.db.execute("UPDATE pairing_requests SET state='expired' WHERE state IN ('pendingApproval','approved') AND expires<=?", (stamp,))
        self.db.execute('DELETE FROM pairing_attempts WHERE created<?', (stamp - PAIRING_ATTEMPT_SECONDS * 1000,))

    def pairing_owner(self, workspace, session_id, device):
        device = self.require_device(workspace, device)
        row = self.db.execute('SELECT * FROM pairing_sessions WHERE id=? AND workspace=? AND owner=?',
                              (identifier(session_id), workspace, device)).fetchone()
        if row is None:
            raise RelayError('Pairing is unavailable', 404)
        return row

    def create_pairing(self, workspace, value):
        if set(value) != {'deviceId'}:
            raise RelayError('deviceId is required')
        with self.lock, self.db:
            device = self.require_device(workspace, value['deviceId'])
            metadata = json.loads(self.db.execute('SELECT metadata FROM devices WHERE workspace=? AND id=?', (workspace, device)).fetchone()[0])
            if metadata['platform'] != 'android':
                raise RelayError('Create the pairing code on the phone', 403)
            self.expire_pairing()
            self.db.execute("UPDATE pairing_requests SET state='cancelled' WHERE state='pendingApproval' AND session IN (SELECT id FROM pairing_sessions WHERE workspace=? AND owner=? AND state='waiting')", (workspace, device))
            self.db.execute("UPDATE pairing_sessions SET state='cancelled' WHERE workspace=? AND owner=? AND state='waiting'", (workspace, device))
            for _ in range(100):
                code = f'{secrets.randbelow(1000000):06d}'
                code_hash = self.pairing_hash(code)
                if not self.db.execute("SELECT 1 FROM pairing_sessions WHERE code_hash=? AND state='waiting'", (code_hash,)).fetchone():
                    break
            else:
                raise RelayError('Try generating the pairing code again', 503)
            session_id, stamp = str(uuid.uuid4()), now()
            expires = stamp + PAIRING_SECONDS * 1000
            self.db.execute('INSERT INTO pairing_sessions VALUES(?,?,?,?,?,?,?)', (session_id, workspace, device, code_hash, 'waiting', stamp, expires))
        return {'id': session_id, 'code': code, 'expiresAt': expires, 'serverTime': stamp, 'state': 'waiting'}

    @staticmethod
    def pairing_request_metadata(row):
        return {'id': row['id'], 'pairingId': row['session'], 'deviceId': row['device'],
                'platform': row['platform'], 'name': row['name'], 'state': row['state'],
                'createdAt': row['created'], 'expiresAt': row['expires'], 'serverTime': now()}

    def join_pairing(self, value, ip):
        if set(value) != {'id', 'receiptSecret', 'code', 'deviceId', 'platform', 'name'}:
            raise RelayError('Provide the pairing code and device details')
        join_id, device = identifier(value['id']), identifier(value['deviceId'])
        receipt, code = value['receiptSecret'], value['code']
        if not isinstance(receipt, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', receipt):
            raise RelayError('A private random pairing receipt is required')
        if value['platform'] != 'mac':
            raise RelayError('Pair a computer using the phone code')
        name = display_name(value['name'])
        if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
            raise RelayError('Enter the six digit code displayed on the phone')
        receipt_hash = self.pairing_hash(receipt)
        digest = self.pairing_hash(json.dumps({'device': device, 'name': name, 'receipt': receipt_hash, 'code': self.pairing_hash(code)}, sort_keys=True))
        error = None
        with self.lock, self.db:
            self.expire_pairing()
            old = self.db.execute('SELECT * FROM pairing_requests WHERE id=?', (join_id,)).fetchone()
            if old:
                if not secrets.compare_digest(old['digest'], digest):
                    raise RelayError('Pairing request already has different details', 409)
                return self.pairing_request_metadata(old)
            counts = self.db.execute('SELECT COUNT(*),COALESCE(SUM(failed),0) FROM pairing_attempts WHERE ip=?', (ip,)).fetchone()
            if counts[0] >= 30 or counts[1] >= 10:
                raise RelayError('Too many pairing attempts; try again later', 429)
            session = self.db.execute("SELECT * FROM pairing_sessions WHERE code_hash=? AND state='waiting'", (self.pairing_hash(code),)).fetchone()
            self.db.execute('INSERT INTO pairing_attempts VALUES(?,?,?)', (ip, now(), int(session is None)))
            if not session:
                error = RelayError('Pairing code is invalid or expired', 404)
            elif self.db.execute("SELECT COUNT(*) FROM pairing_requests WHERE session=? AND state='pendingApproval'", (session['id'],)).fetchone()[0] >= 5:
                error = RelayError('The phone has pending requests; ask it to create a new code', 429)
            else:
                self.db.execute('INSERT INTO pairing_requests VALUES(?,?,?,?,?,?,?,?,?,?)', (join_id, session['id'], device,
                    'mac', name, receipt_hash, digest, 'pendingApproval', now(), session['expires']))
                row = self.db.execute('SELECT * FROM pairing_requests WHERE id=?', (join_id,)).fetchone()
        # Raise after commit so incorrect guesses still count towards the limit.
        if error:
            raise error
        return self.pairing_request_metadata(row)

    def pairing_requests(self, workspace, session_id, device):
        with self.lock, self.db:
            self.expire_pairing()
            session = self.pairing_owner(workspace, session_id, device)
            rows = self.db.execute('SELECT * FROM pairing_requests WHERE session=? ORDER BY created LIMIT 30', (session['id'],)).fetchall()
            return {'id': session['id'], 'state': session['state'], 'expiresAt': session['expires'], 'serverTime': now(),
                    'requests': [self.pairing_request_metadata(row) for row in rows]}

    def decide_pairing(self, workspace, session_id, value):
        if set(value) != {'deviceId', 'requestId', 'approve'} or not isinstance(value['approve'], bool):
            raise RelayError('deviceId, requestId and a boolean approve are required')
        with self.lock, self.db:
            self.expire_pairing()
            session = self.pairing_owner(workspace, session_id, value['deviceId'])
            row = self.db.execute('SELECT * FROM pairing_requests WHERE id=? AND session=?', (identifier(value['requestId']), session['id'])).fetchone()
            if row is None:
                raise RelayError('Pairing request is unavailable', 404)
            state = 'approved' if value['approve'] else 'declined'
            if row['state'] == state:
                return self.pairing_request_metadata(row)
            if session['state'] != 'waiting' or row['state'] != 'pendingApproval':
                raise RelayError('Pairing is already decided or expired', 409)
            expires = now() + PAIRING_SECONDS * 1000 if value['approve'] else row['expires']
            self.db.execute('UPDATE pairing_requests SET state=?,expires=? WHERE id=?', (state, expires, row['id']))
            if value['approve']:
                self.db.execute("UPDATE pairing_sessions SET state='approved' WHERE id=?", (session['id'],))
                self.db.execute("UPDATE pairing_requests SET state='declined' WHERE session=? AND id<>? AND state='pendingApproval'", (session['id'], row['id']))
            return self.pairing_request_metadata(self.db.execute('SELECT * FROM pairing_requests WHERE id=?', (row['id'],)).fetchone())

    def pairing_status(self, request_id, receipt):
        if not isinstance(receipt, str) or len(receipt) > 128:
            raise RelayError('Pairing request is unavailable', 404)
        with self.lock, self.db:
            self.expire_pairing()
            row = self.db.execute('SELECT * FROM pairing_requests WHERE id=?', (identifier(request_id),)).fetchone()
            if row is None or not secrets.compare_digest(row['receipt_hash'], self.pairing_hash(receipt)):
                raise RelayError('Pairing request is unavailable', 404)
            result = self.pairing_request_metadata(row)
            if row['state'] == 'approved':
                session = self.db.execute('SELECT workspace,owner FROM pairing_sessions WHERE id=?', (row['session'],)).fetchone()
                result.update(workspaceId=session['workspace'], approvingDeviceId=session['owner'], protocolVersion=1)
            return result

    def cancel_pairing(self, workspace, session_id, device):
        with self.lock, self.db:
            self.expire_pairing()
            session = self.pairing_owner(workspace, session_id, device)
            if session['state'] == 'approved':
                raise RelayError('Pairing is complete; cancelling a code does not revoke a connected computer', 409)
            self.db.execute("UPDATE pairing_sessions SET state='cancelled' WHERE id=? AND state='waiting'", (session['id'],))
            self.db.execute("UPDATE pairing_requests SET state='cancelled' WHERE session=? AND state='pendingApproval'", (session['id'],))
            return {'id': session['id'], 'state': 'cancelled' if session['state'] == 'waiting' else session['state'], 'expiresAt': session['expires']}

    def require_device(self, workspace, device):
        device = identifier(device)
        if not self.db.execute('SELECT 1 FROM devices WHERE workspace=? AND id=?', (workspace, device)).fetchone():
            raise RelayError('Register the device in this workspace first', 404)
        return device

    def register(self, workspace, value):
        from urllib.parse import urlsplit
        if set(value) - {'deviceId', 'platform', 'name', 'lanAddresses', 'sameLan', 'catalog'}:
            raise RelayError('Unknown registration fields')
        device = identifier(value.get('deviceId'))
        if value.get('platform') not in ('android', 'mac'):
            raise RelayError('platform must be android or mac')
        addresses = value.get('lanAddresses', [])
        if not isinstance(addresses, list) or len(addresses) > 24:
            raise RelayError('Invalid LAN addresses')
        for address in addresses:
            if not isinstance(address, str) or len(address) > 512:
                raise RelayError('Invalid LAN address')
            parsed = urlsplit(address)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.path.rstrip('/') or parsed.query or parsed.fragment:
                raise RelayError('LAN addresses must be HTTP roots without credentials')
        same_lan = value.get('sameLan', False)
        if not isinstance(same_lan, bool):
            raise RelayError('sameLan must be boolean')
        metadata = {'deviceId': device, 'platform': value['platform'], 'name': display_name(value.get('name', device)),
                    'lanAddresses': addresses, 'sameLan': same_lan}
        stamp = now()
        with self.lock, self.db:
            old = self.db.execute('SELECT catalog FROM devices WHERE workspace=? AND id=?', (workspace, device)).fetchone()
            catalog = self.clean_catalog(value['catalog']) if 'catalog' in value else json.loads(old['catalog']) if old else {}
            self.db.execute('INSERT OR REPLACE INTO devices VALUES(?,?,?,?,?)', (workspace, device, json.dumps(metadata), json.dumps(catalog), stamp))
        return {'deviceId': device, 'serverTime': stamp}

    @staticmethod
    def clean_catalog(value):
        if not isinstance(value, dict) or set(value) - {'documents', 'attachments', 'tasks', 'schedules'}:
            raise RelayError('Catalog must contain metadata lists')
        allowed = {'id', 'kind', 'title', 'name', 'revision', 'hash', 'sha256', 'bytes', 'mimeType', 'createdAt', 'updatedAt',
                   'status', 'phase', 'enabled', 'autoLoad', 'tags', 'deleted', 'source', 'hasMore', 'total'}
        result = {}
        for key, rows in value.items():
            if not isinstance(rows, list) or len(rows) > 10000:
                raise RelayError('Invalid catalog rows')
            cleaned = []
            for row in rows:
                if not isinstance(row, dict):
                    raise RelayError('Invalid metadata item')
                # Only known summary fields can reach server lists. Secrets/content are omitted.
                clean = {}
                for k, v in row.items():
                    if k not in allowed:
                        continue
                    if k == 'tags':
                        if isinstance(v, list) and len(v) <= 100 and all(isinstance(tag, str) and len(tag) <= 160 for tag in v):
                            clean[k] = v
                    elif isinstance(v, (str, int, float, bool)) or v is None:
                        if not isinstance(v, str) or len(v) <= 2048:
                            clean[k] = v
                if len(json.dumps(clean, ensure_ascii=False).encode()) > 8192:
                    raise RelayError('A metadata item is too large')
                cleaned.append(clean)
            result[key] = cleaned
        return result

    def devices(self, workspace):
        with self.lock:
            rows = self.db.execute('SELECT metadata,seen FROM devices WHERE workspace=? ORDER BY seen DESC', (workspace,)).fetchall()
        return {'devices': [{**json.loads(r['metadata']), 'lastSeen': r['seen'], 'online': now() - r['seen'] < ONLINE_SECONDS * 1000} for r in rows]}

    def catalog(self, workspace, device):
        with self.lock:
            row = self.db.execute('SELECT catalog,seen FROM devices WHERE workspace=? AND id=?', (workspace, identifier(device))).fetchone()
        if not row:
            raise RelayError('Device not found', 404)
        return {'deviceId': device, 'catalog': json.loads(row['catalog']), 'updatedAt': row['seen'], 'online': now() - row['seen'] < ONLINE_SECONDS * 1000}

    def blob(self, workspace, blob_id):
        with self.lock:
            row = self.db.execute('SELECT metadata FROM blobs WHERE workspace=? AND id=?', (workspace, identifier(blob_id))).fetchone()
        if not row:
            raise RelayError('Transfer file not found or expired', 404)
        value = json.loads(row['metadata'])
        if not (self.blobs_dir / value['id']).is_file():
            raise RelayError('Transfer file not found', 404)
        return value

    def blob_details(self, workspace, value):
        if value.get('blobId'):
            value['blob'] = self.blob(workspace, value['blobId'])
        return value

    def submit(self, workspace, value):
        if set(value) - {'id', 'sourceId', 'targetId', 'method', 'path', 'headers', 'body', 'blobId'}:
            raise RelayError('Unknown request fields')
        device_request = {'id': identifier(value.get('id')), 'sourceId': identifier(value.get('sourceId')),
                          'targetId': identifier(value.get('targetId')), 'method': value.get('method'),
                          'path': safe_path(value.get('path')), 'headers': content_headers(value.get('headers'))}
        if device_request['method'] not in ('GET', 'HEAD', 'POST', 'DELETE'):
            raise RelayError('Unsupported HTTP method')
        if 'body' in value and value.get('blobId'):
            raise RelayError('A request cannot have both JSON and a transfer file')
        if 'body' in value:
            device_request['body'] = value['body']
        if value.get('blobId'):
            device_request['blobId'] = identifier(value['blobId'])
        digest = hashlib.sha256(json.dumps(device_request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.lock, self.db:
            self.require_device(workspace, device_request['sourceId'])
            self.require_device(workspace, device_request['targetId'])
            old = self.db.execute('SELECT workspace,digest,value FROM requests WHERE id=?', (device_request['id'],)).fetchone()
            if old:
                if old['workspace'] != workspace or old['digest'] != digest:
                    raise RelayError('Request ID already belongs to a different operation', 409)
                return json.loads(old['value'])
            self.blob_details(workspace, device_request)
            stamp = now()
            device_request.update(state='pending', createdAt=stamp, updatedAt=stamp)
            self.db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)', (device_request['id'], workspace, device_request['sourceId'],
                device_request['targetId'], 'pending', stamp, stamp, json.dumps(device_request), digest))
        return device_request

    def get_request(self, workspace, request_id, source=None):
        with self.lock:
            row = self.db.execute('SELECT source,value FROM requests WHERE workspace=? AND id=?', (workspace, identifier(request_id))).fetchone()
        if not row:
            raise RelayError('Request not found', 404)
        if source and row['source'] != identifier(source):
            raise RelayError('Only the source device can inspect this request', 403)
        return json.loads(row['value'])

    def inbox(self, workspace, device):
        with self.lock, self.db:
            self.require_device(workspace, device)
            rows = self.db.execute("SELECT id,value FROM requests WHERE workspace=? AND target=? AND state='pending' ORDER BY created LIMIT 1", (workspace, device)).fetchall()
            result = []
            for row in rows:
                value = json.loads(row['value'])
                stamp = now()
                value.update(state='delivered', updatedAt=stamp)
                self.db.execute('UPDATE requests SET state=?,updated=?,value=? WHERE id=? AND state=?', ('delivered', stamp, json.dumps(value), row['id'], 'pending'))
                result.append(value)
        return {'requests': result}

    def reply(self, workspace, request_id, response):
        if set(response) - {'deviceId', 'status', 'headers', 'body', 'blobId'}:
            raise RelayError('Unknown reply fields')
        status = response.get('status')
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise RelayError('Invalid HTTP status')
        reply = {'status': status, 'headers': content_headers(response.get('headers'))}
        if 'body' in response:
            reply['body'] = response['body']
        if response.get('blobId'):
            if 'body' in response:
                raise RelayError('A reply cannot contain both JSON and a transfer file')
            reply['blobId'] = identifier(response['blobId'])
            self.blob_details(workspace, reply)
        with self.lock, self.db:
            task = self.get_request(workspace, request_id)
            if task['targetId'] != identifier(response.get('deviceId')):
                raise RelayError('Only the target device can reply', 403)
            if task['state'] == 'completed':
                if task['response'] != reply:
                    raise RelayError('This request already has a different reply', 409)
                return task
            if task['state'] != 'delivered':
                raise RelayError('Request was not claimed, expired or cancelled', 409)
            task.update(state='completed', response=reply, updatedAt=now())
            self.db.execute('UPDATE requests SET state=?,updated=?,value=? WHERE id=?', ('completed', now(), json.dumps(task), task['id']))
        return task

    def cancel(self, workspace, request_id, source):
        with self.lock, self.db:
            task = self.get_request(workspace, request_id, source)
            if task['state'] in ('pending', 'delivered'):
                already_delivered = task['state'] == 'delivered'
                task.update(state='cancelled', updatedAt=now(), deliveryUncertain=already_delivered)
                self.db.execute('UPDATE requests SET state=?,updated=?,value=? WHERE id=?', ('cancelled', now(), json.dumps(task), task['id']))
            return task

    def prune(self):
        stamp = now()
        with self.lock, self.db:
            self.expire_pairing()
            self.db.execute('DELETE FROM pairing_requests WHERE created<? AND expires<?', (stamp - 86400000, stamp))
            self.db.execute('DELETE FROM pairing_sessions WHERE created<? AND id NOT IN (SELECT session FROM pairing_requests)', (stamp - 86400000,))
            stale = self.db.execute("SELECT id,value FROM requests WHERE (state='pending' AND created<?) OR (state='delivered' AND updated<?)",
                (stamp - REQUEST_SECONDS * 1000, stamp - DELIVERY_SECONDS * 1000)).fetchall()
            for row in stale:
                value = json.loads(row['value'])
                error = 'Device remained offline; request expired' if value['state'] == 'pending' else 'delivery_unknown: no reply; operation was not replayed'
                value.update(state='failed', error=error, updatedAt=stamp)
                self.db.execute('UPDATE requests SET state=?,updated=?,value=? WHERE id=?', ('failed', stamp, json.dumps(value), row['id']))
            blobs = self.db.execute('SELECT id FROM blobs WHERE created<?', (stamp - BLOB_SECONDS * 1000,)).fetchall()
            for row in blobs:
                (self.blobs_dir / row['id']).unlink(missing_ok=True)
                self.db.execute('DELETE FROM blobs WHERE id=?', (row['id'],))


class RelayServer:
    def __init__(self, root):
        self.store = RelayStore(root)
        self.events = {}

    def signal(self, key):
        self.events.setdefault(key, asyncio.Event()).set()

    async def wait(self, key, seconds):
        if seconds <= 0:
            return
        event = self.events.setdefault(key, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        event.clear()

    async def body(self, request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_JSON:
                raise RelayError('JSON parameters are too large', 413)
        try:
            value = json.loads(data or b'{}')
        except (ValueError, UnicodeError):
            raise RelayError('Invalid JSON request') from None
        if not isinstance(value, dict):
            raise RelayError('JSON parameters must be an object')
        return value

    @staticmethod
    def waiting(request):
        try:
            value = float(request.query_params.get('wait', '0'))
        except ValueError:
            raise RelayError('Invalid polling interval') from None
        if not 0 <= value <= 25:
            raise RelayError('Polling interval must be between 0 and 25 seconds')
        return value

    async def endpoint(self, request: Request):
        try:
            route = request.url.path
            if route == '/health':
                return JSONResponse({'appId': 'devhelper-relay', 'version': '2.1.0', 'protocolVersion': 1, 'status': 'ok', 'transport': 'http', 'serverTime': now()})
            if route == '/api/relay/workspaces' and request.method == 'POST':
                await self.body(request)
                return JSONResponse(self.store.create_workspace(), 201)
            if route == '/api/relay/pairing/join' and request.method == 'POST':
                result = self.store.join_pairing(await self.body(request), request.client.host if request.client else 'unknown')
                return JSONResponse(result, 202 if result['state'] == 'pendingApproval' else 200, headers={'Cache-Control': 'no-store'})
            if route.startswith('/api/relay/pairing/requests/') and request.method == 'GET':
                return JSONResponse(self.store.pairing_status(route.rsplit('/', 1)[1], request.headers.get('x-devhelper-pairing', '')), headers={'Cache-Control': 'no-store'})
            workspace = self.store.workspace(request)
            self.store.prune()
            if route == '/api/relay/pairing' and request.method == 'POST':
                return JSONResponse(self.store.create_pairing(workspace, await self.body(request)), 201, headers={'Cache-Control': 'no-store'})
            if route.startswith('/api/relay/pairing/'):
                pieces = route.split('/')
                session_id = pieces[4]
                if len(pieces) == 6 and pieces[5] == 'requests' and request.method == 'GET':
                    result = self.store.pairing_requests(workspace, session_id, request.query_params.get('deviceId'))
                elif len(pieces) == 6 and pieces[5] == 'decision' and request.method == 'POST':
                    result = self.store.decide_pairing(workspace, session_id, await self.body(request))
                elif len(pieces) == 5 and request.method == 'DELETE':
                    result = self.store.cancel_pairing(workspace, session_id, request.query_params.get('deviceId'))
                else:
                    raise RelayError('Endpoint not found', 404)
                return JSONResponse(result, headers={'Cache-Control': 'no-store'})
            if route.startswith('/devices/') and route.endswith('/mcp') and request.method == 'POST':
                return await self.mcp_gateway(request, workspace)
            if route in ('/api/relay/register', '/api/relay/heartbeat') and request.method == 'POST':
                return JSONResponse(self.store.register(workspace, await self.body(request)))
            if route == '/api/relay/devices' and request.method == 'GET':
                return JSONResponse(self.store.devices(workspace))
            if route.startswith('/api/relay/catalog/') and request.method == 'GET':
                return JSONResponse(self.store.catalog(workspace, route.rsplit('/', 1)[1]))
            if route == '/api/relay/requests' and request.method == 'POST':
                task = self.store.submit(workspace, await self.body(request))
                self.signal((workspace, task['targetId']))
                return JSONResponse(task, 202 if task['state'] in ('pending', 'delivered') else 200)
            if route == '/api/relay/inbox' and request.method == 'GET':
                device = identifier(request.query_params.get('deviceId'))
                result = self.store.inbox(workspace, device)
                if not result['requests']:
                    await self.wait((workspace, device), self.waiting(request))
                    result = self.store.inbox(workspace, device)
                return JSONResponse(result)
            if route.startswith('/api/relay/replies/') and request.method == 'POST':
                task = self.store.reply(workspace, route.rsplit('/', 1)[1], await self.body(request))
                self.signal((workspace, task['id']))
                return JSONResponse(task)
            if route.startswith('/api/relay/requests/'):
                task_id = identifier(route.rsplit('/', 1)[1])
                source = identifier(request.query_params.get('sourceId'))
                if request.method == 'DELETE':
                    task = self.store.cancel(workspace, task_id, source)
                    self.signal((workspace, task_id))
                    return JSONResponse(task)
                if request.method == 'GET':
                    task = self.store.get_request(workspace, task_id, source)
                    if task['state'] in ('pending', 'delivered'):
                        await self.wait((workspace, task_id), self.waiting(request))
                        task = self.store.get_request(workspace, task_id, source)
                    return JSONResponse(task)
            if route == '/api/relay/blobs' and request.method == 'POST':
                with self.store.lock:
                    self.store.require_device(workspace, request.query_params.get('deviceId'))
                return await self.upload(request, workspace)
            if route.startswith('/api/relay/blobs/'):
                value = self.store.blob(workspace, route.rsplit('/', 1)[1])
                file = self.store.blobs_dir / value['id']
                if request.method in ('GET', 'HEAD'):
                    return FileResponse(file, media_type=value['mimeType'], filename=value['name'], headers={'X-Content-SHA256': value['sha256']})
                if request.method == 'DELETE':
                    with self.store.lock, self.store.db:
                        file.unlink(missing_ok=True)
                        self.store.db.execute('DELETE FROM blobs WHERE id=?', (value['id'],))
                    return JSONResponse({'id': value['id'], 'deleted': True})
            return JSONResponse({'error': 'Endpoint not found'}, 404)
        except RelayError as error:
            return JSONResponse({'error': str(error)}, error.status,
                headers={'Cache-Control': 'no-store'} if request.url.path.startswith('/api/relay/pairing') else None)
        except (ValueError, TypeError) as error:
            return JSONResponse({'error': str(error)[:200]}, 400)

    async def mcp_gateway(self, request, workspace):
        source = identifier(request.headers.get('x-devhelper-source'))
        target = identifier(request.path_params['device'])
        message = await self.body(request)
        if message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            raise RelayError('A JSON-RPC 2.0 MCP request is required')
        task = self.store.submit(workspace, {'id': str(uuid.uuid4()), 'sourceId': source,
            'targetId': target, 'method': 'POST', 'path': '/mcp',
            'headers': {'content-type': 'application/json'}, 'body': message})
        self.signal((workspace, target))
        if 'id' not in message:
            return Response(status_code=202)
        # The durable request remains inspectable if a client disconnects. No retry
        # or cancellation is inferred from an HTTP timeout.
        deadline = time.monotonic() + 120
        while task['state'] in ('pending', 'delivered') and time.monotonic() < deadline:
            await self.wait((workspace, task['id']), min(25, max(0, deadline - time.monotonic())))
            task = self.store.get_request(workspace, task['id'], source)
        if task['state'] == 'completed':
            response = task['response']
            if response['status'] == 202 or response['status'] == 204:
                return Response(status_code=response['status'])
            if 'body' in response:
                return JSONResponse(response['body'], response['status'])
        return JSONResponse({'jsonrpc': '2.0', 'id': message['id'], 'error': {
            'code': -32000, 'message': task.get('error', 'Device request has no completed JSON response'),
            'data': {'requestId': task['id'], 'state': task['state'], 'sourceId': source}}})

    async def upload(self, request, workspace):
        blob_id = str(uuid.uuid4())
        pending = self.store.blobs_dir / (blob_id + '.pending')
        final = self.store.blobs_dir / blob_id
        name = display_name(request.query_params.get('name', 'transfer'))
        mime = request.headers.get('content-type', 'application/octet-stream')
        if '\n' in mime or '\r' in mime or len(mime) > 200:
            raise RelayError('Invalid transfer content type')
        digest = hashlib.sha256()
        length = 0
        try:
            with pending.open('xb') as out:
                async for chunk in request.stream():
                    await asyncio.to_thread(out.write, chunk)
                    digest.update(chunk)
                    length += len(chunk)
                await asyncio.to_thread(out.flush)
                await asyncio.to_thread(os.fsync, out.fileno())
            expected_length = request.headers.get('content-length')
            if expected_length is not None and int(expected_length) != length:
                raise RelayError('Incomplete transfer file', 400)
            pending.replace(final)
            value = {'id': blob_id, 'bytes': length, 'sha256': digest.hexdigest(), 'mimeType': mime, 'name': name}
            with self.store.lock, self.store.db:
                self.store.db.execute('INSERT INTO blobs VALUES(?,?,?,?)', (blob_id, workspace, now(), json.dumps(value)))
            return JSONResponse(value, 201)
        except BaseException:
            pending.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise

    @contextlib.asynccontextmanager
    async def lifespan(self, app):
        yield
        self.store.close()

    def app(self):
        return Starlette(routes=[Route('/health', self.endpoint), Route('/devices/{device}/mcp', self.endpoint, methods=['POST']), Route('/api/relay/{path:path}', self.endpoint,
            methods=['GET', 'HEAD', 'POST', 'DELETE'])], lifespan=self.lifespan)


def main():
    parser = argparse.ArgumentParser(description='Run DevHelper outgoing-device HTTP relay')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8890)
    parser.add_argument('--data-dir', type=Path, default=Path('data/relay'))
    parser.add_argument('--ssl-certfile', type=Path)
    parser.add_argument('--ssl-keyfile', type=Path)
    args = parser.parse_args()
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error('Provide both TLS certificate and private key')
    config = uvicorn.Config(RelayServer(args.data_dir).app(), host=args.host, port=args.port, access_log=False,
        ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
        ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None)
    if args.ssl_certfile and os.name == 'posix':
        import signal
        import logging
        def reload_tls(signum, frame):
            # Existing transfers and mailbox claims survive certificate renewal.
            try:
                if config.ssl:
                    config.ssl.load_cert_chain(str(args.ssl_certfile), str(args.ssl_keyfile))
            except (OSError, ValueError) as error:
                logging.getLogger('uvicorn.error').error('Certificate reload failed: %s', error)
        signal.signal(signal.SIGUSR1, reload_tls)
    uvicorn.Server(config).run()



if __name__ == '__main__':
    main()
