"""Baidu Netdisk Open Platform adapter, with bounded binary transfers.

Implements the documented protocol instead of bundling the unlicensed 2022
generated SDK. Secrets and jobs live below the caller's private data directory.
No cloud operation runs until credentials are configured and the user authorizes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, HTTPRedirectHandler, build_opener
import hashlib
import http.client
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
import uuid

FILE_API = 'https://pan.baidu.com/rest/2.0/xpan/file'
META_API = 'https://pan.baidu.com/rest/2.0/xpan/multimedia'
NAS_API = 'https://pan.baidu.com/rest/2.0/xpan/nas'
UPLOAD_API = 'https://d.pcs.baidu.com/rest/2.0/pcs/superfile2'
TOKEN_API = 'https://openapi.baidu.com/oauth/2.0/token'
AUTHORIZE_API = 'https://openapi.baidu.com/oauth/2.0/authorize'
BLOCK_SIZE = 4 * 1024 * 1024
MAX_BLOCKS = 10000  # Open Platform's block_list limit, not a local storage quota.
IO_SIZE = 65536


class BaiduDriveError(Exception):
    def __init__(self, message, status=400, *, retryable=False, provider_code=None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.provider_code = provider_code


def _trusted_url(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise BaiduDriveError('百度网盘返回了无效的传输地址', 502) from None
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or parsed.username or parsed.password or
            port not in (None, 443) or not any(
                host == suffix or host.endswith('.' + suffix)
                for suffix in ('baidu.com', 'baidupcs.com', 'bcebos.com'))):
        raise BaiduDriveError('百度网盘返回了不受支持的传输地址', 502)
    return url


def _download_url(url):
    _trusted_url(url)
    host = (urlsplit(url).hostname or '').lower()
    if not any(host == suffix or host.endswith('.' + suffix)
               for suffix in ('pcs.baidu.com', 'baidupcs.com', 'bcebos.com')):
        raise BaiduDriveError('百度网盘返回了不受支持的下载域名', 502)
    return url


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Only binary CDN GET redirects are supported. Tokens are never forwarded
        # to a general Baidu service or a customer-owned BOS bucket.
        _download_url(request.full_url)
        _download_url(newurl)
        if request.get_method() != 'GET':
            raise BaiduDriveError('百度网盘请求被重定向，请重试', 502)
        parsed = urlsplit(newurl)
        if (parsed.hostname or '').endswith('bcebos.com'):
            query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != 'access_token']
            newurl = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class HTTPTransport:
    """Injectable transport. open() returns a context-managed binary response."""
    def __init__(self):
        self.opener = build_opener(_Redirects())

    def open(self, method, url, *, headers=None, body=None, timeout=90):
        _trusted_url(url)
        return self.opener.open(Request(url, data=body, headers=headers or {}, method=method), timeout=timeout)


def _atomic_private(path, value):
    pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
        if os.name != 'nt':
            path.chmod(0o600)
    finally:
        pending.unlink(missing_ok=True)


def _read_private(path, default):
    if not path.exists():
        return default
    if path.is_symlink():
        raise BaiduDriveError('私有网盘配置不能是符号链接')
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise BaiduDriveError('私有网盘配置损坏，原文件已保留') from None
    if not isinstance(result, type(default)):
        raise BaiduDriveError('私有网盘配置格式错误，原文件已保留')
    return result


class _MultipartSlice:
    """Reiterable multipart body: each retry seeks the same bounded file range."""
    def __init__(self, file, offset, size, check):
        self.file, self.offset, self.size, self.check = file, offset, size, check
        self.boundary = 'devhelper-' + secrets.token_hex(16)
        self.prefix = ('--' + self.boundary + '\r\nContent-Disposition: form-data; name="file"; filename="part.bin"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        self.suffix = ('\r\n--' + self.boundary + '--\r\n').encode()
        self.length = len(self.prefix) + size + len(self.suffix)

    def __iter__(self):
        self.file.seek(self.offset)
        yield self.prefix
        remaining = self.size
        while remaining:
            self.check()
            chunk = self.file.read(min(IO_SIZE, remaining))
            if not chunk:
                raise BaiduDriveError('上传源文件已改变，任务停止', 409)
            remaining -= len(chunk)
            yield chunk
        yield self.suffix


class BaiduDriveService:
    def __init__(self, data_root, transport=None):
        self.root = Path(data_root) / 'baidu-drive'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise BaiduDriveError('网盘私有目录不能是符号链接')
        if os.name != 'nt':
            self.root.chmod(0o700)
        self.config_path, self.jobs_path = self.root / 'config.json', self.root / 'jobs.json'
        self.lock = threading.RLock()
        self.config = _read_private(self.config_path, {})
        self._jobs = _read_private(self.jobs_path, {})
        self._cancellations = {}
        self.transport = transport or HTTPTransport()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='baidu-drive')
        self.closed = False
        for item in self._jobs.values():
            if item.get('state') in ('queued', 'running'):
                item.update(state='interrupted', error='服务重启，原文件保留；请重新发起传输', updatedAt=time.time())
        if self._jobs:
            _atomic_private(self.jobs_path, self._jobs)

    def status(self):
        with self.lock:
            c = self.config
            missing = [name for name in ('clientId', 'clientSecret', 'appName') if not c.get(name)]
            return dict(configured=not missing, authorized=bool(c.get('accessToken')),
                        missingFields=missing, clientId=c.get('clientId', ''),
                        hasClientSecret=bool(c.get('clientSecret')), hasRefreshToken=bool(c.get('refreshToken')),
                        appName=c.get('appName', ''), scopeRoot='/apps/' + c['appName'] if c.get('appName') else '',
                        redirectUri=c.get('redirectUri', 'oob'), expiresAt=c.get('expiresAt', 0),
                        binaryTransfers=True, blockSize=BLOCK_SIZE,
                        activeJobs=sum(j['state'] in ('queued', 'running') for j in self._jobs.values()))

    def configure(self, fields):
        allowed = {'clientId', 'clientSecret', 'appName', 'redirectUri', 'accessToken', 'refreshToken', 'clearSecret'}
        if not isinstance(fields, dict) or not set(fields).issubset(allowed):
            raise BaiduDriveError('网盘配置包含未知字段')
        with self.lock:
            if any(j['state'] in ('queued', 'running') for j in self._jobs.values()):
                raise BaiduDriveError('请等待传输结束后修改网盘配置', 409)
            updated = dict(self.config)
            identity_changed = False
            for key, value in fields.items():
                if key == 'clearSecret':
                    if not isinstance(value, bool):
                        raise BaiduDriveError('clearSecret 必须为布尔值')
                    continue
                if not isinstance(value, str) or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
                    raise BaiduDriveError('网盘配置字段格式错误: ' + key)
                value = value.strip()
                if key == 'clientSecret' and not value:
                    continue
                if key == 'appName' and (not value or value in ('.', '..') or re.search(r'[/\\\x7f]', value)):
                    raise BaiduDriveError('应用目录名称不能包含斜杠，且必须与百度开放平台一致')
                if key == 'redirectUri' and value != 'oob':
                    parsed = urlsplit(value)
                    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                        raise BaiduDriveError('回调地址需为 oob 或已登记的 HTTP(S) 地址')
                if key in ('clientId', 'clientSecret', 'redirectUri', 'appName') and updated.get(key) != value:
                    identity_changed = True
                updated[key] = value
            if fields.get('clearSecret'):
                updated.pop('clientSecret', None)
                identity_changed = True
            if identity_changed:
                for key in ('accessToken', 'refreshToken', 'expiresAt', 'oauthStateHash', 'oauthStateExpiresAt'):
                    updated.pop(key, None)
                for key in ('accessToken', 'refreshToken'):
                    if fields.get(key):
                        updated[key] = fields[key].strip()
            self.config = updated
            _atomic_private(self.config_path, self.config)
            return self.status()

    def authorization_url(self):
        with self.lock:
            self._require_configured(oauth_only=True)
            state = secrets.token_urlsafe(32)
            expires = time.time() + 600
            self.config.update(oauthStateHash=hashlib.sha256(state.encode()).hexdigest(), oauthStateExpiresAt=expires)
            _atomic_private(self.config_path, self.config)
            url = AUTHORIZE_API + '?' + urlencode(dict(response_type='code', client_id=self.config['clientId'],
                redirect_uri=self.config.get('redirectUri', 'oob'), scope='basic,netdisk', display='page', state=state))
            return dict(url=url, state=state, expiresAt=expires)

    def _require_configured(self, oauth_only=False):
        missing = self.status()['missingFields']
        if oauth_only:
            missing = [name for name in missing if name != 'appName']
        if missing:
            raise BaiduDriveError('请先填写百度网盘配置: ' + ', '.join(missing), 409)

    def exchange_code(self, code, state=None):
        if not isinstance(code, str) or not code.strip() or len(code) > 4096:
            raise BaiduDriveError('请输入本次百度授权码')
        with self.lock:
            self._require_configured(oauth_only=True)
            if any(j['state'] in ('queued', 'running') for j in self._jobs.values()):
                raise BaiduDriveError('请等待传输结束后切换网盘授权', 409)
            expected = self.config.get('oauthStateHash', '')
            if (not isinstance(state, str) or not expected or self.config.get('oauthStateExpiresAt', 0) < time.time() or
                    not hmac.compare_digest(hashlib.sha256(state.encode()).hexdigest(), expected)):
                raise BaiduDriveError('授权会话不匹配或已过期，请重新打开授权页面', 409)
            self.config.pop('oauthStateHash', None)
            self.config.pop('oauthStateExpiresAt', None)
            _atomic_private(self.config_path, self.config)
            response = self._json('POST', TOKEN_API, form=dict(grant_type='authorization_code', code=code.strip(),
                client_id=self.config['clientId'], client_secret=self.config['clientSecret'],
                redirect_uri=self.config.get('redirectUri', 'oob')))
            self._save_tokens(response)
            return self.status()

    def _save_tokens(self, response):
        if not isinstance(response.get('access_token'), str) or not response['access_token']:
            raise BaiduDriveError('百度未返回有效授权令牌，请重新授权', 502)
        self.config['accessToken'] = response['access_token']
        if response.get('refresh_token'):
            self.config['refreshToken'] = response['refresh_token']
        try:
            self.config['expiresAt'] = time.time() + max(0, int(response.get('expires_in', 0)))
        except (ValueError, TypeError):
            self.config['expiresAt'] = 0
        _atomic_private(self.config_path, self.config)

    def refresh(self):
        with self.lock:
            self._require_configured()
            if not self.config.get('refreshToken'):
                raise BaiduDriveError('没有刷新令牌，请重新授权百度网盘', 409)
            response = self._json('POST', TOKEN_API, form=dict(grant_type='refresh_token',
                refresh_token=self.config['refreshToken'], client_id=self.config['clientId'],
                client_secret=self.config['clientSecret']))
            self._save_tokens(response)
            return self.status()

    def _token(self):
        with self.lock:
            self._require_configured()
            if not self.config.get('accessToken'):
                raise BaiduDriveError('请先完成百度网盘授权', 409)
            expires = self.config.get('expiresAt', 0)
            if expires and expires <= time.time() + 60:
                self.refresh()
            return self.config['accessToken']

    def _json(self, method, endpoint, *, query=None, form=None, body=None, headers=None, authenticated=False, retry=False):
        values = dict(query or {})
        if authenticated:
            values['access_token'] = self._token()
        url = endpoint + ('?' + urlencode(values) if values else '')
        request_headers = {'User-Agent': 'pan.baidu.com', 'Accept': 'application/json'}
        request_headers.update(headers or {})
        if form is not None:
            body = urlencode(form).encode('utf-8')
            request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
        if isinstance(body, bytes):
            request_headers['Content-Length'] = str(len(body))
        for attempt in range(3 if retry else 1):
            try:
                with self.transport.open(method, url, headers=request_headers, body=body, timeout=90) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise BaiduDriveError('百度接口响应过大', 502)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise BaiduDriveError('百度接口响应格式错误', 502)
                code = result.get('errno', result.get('error_code', 0))
                if result.get('error') or code not in (None, 0, '0'):
                    safe_code = re.sub(r'[^A-Za-z0-9_-]', '', str(result.get('error') or code))[:48]
                    expired = str(code) in ('110', '111', '20016', '20017', '31045')
                    raise BaiduDriveError(('百度授权已过期，请刷新或重新授权' if expired else '百度网盘接口失败') + '（' + safe_code + '）',
                                          409 if expired else 502, retryable=str(code) in ('31034', '31326', '1'),
                                          provider_code=safe_code)
                return result
            except HTTPError as error:
                issue = BaiduDriveError('百度网盘 HTTP 请求失败（' + str(error.code) + '）', 502,
                                        retryable=error.code in (429, 500, 502, 503, 504))
                error.close()
            except (URLError, TimeoutError, OSError, http.client.HTTPException):
                issue = BaiduDriveError('百度网盘网络连接失败，请检查网络后重试', 502, retryable=True)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise BaiduDriveError('百度接口响应无法解析', 502) from None
            except BaiduDriveError as error:
                issue = error
            if not issue.retryable or attempt + 1 >= (3 if retry else 1):
                raise issue from None
            time.sleep(0.25 * 2 ** attempt)

    def remote_path(self, path='', *, file=False):
        if not isinstance(path, str) or len(path) > 4096 or re.search(r'[\\\x00-\x1f\x7f]', path):
            raise BaiduDriveError('网盘路径格式错误')
        with self.lock:
            self._require_configured()
            root = '/apps/' + self.config['appName']
        if '..' in path.split('/') or '.' in path.split('/'):
            raise BaiduDriveError('网盘路径不能包含 . 或 ..')
        target = root + ('/' + path if path and not path.startswith('/') else '') if not path.startswith('/') else path
        target = re.sub('/+', '/', target).rstrip('/')
        if target != root and not target.startswith(root + '/'):
            raise BaiduDriveError('仅允许访问本应用的网盘目录: ' + root)
        if file and target == root:
            raise BaiduDriveError('请选择应用目录内的文件路径')
        return target

    def _file(self, item):
        result = {}
        for name in ('fs_id', 'path', 'server_filename', 'size', 'isdir', 'md5', 'local_ctime', 'local_mtime', 'server_ctime', 'server_mtime', 'category'):
            if name in item:
                result[name] = item[name]
        return result

    def list_files(self, path='', start=0, limit=100):
        if isinstance(start, bool) or not isinstance(start, int) or start < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise BaiduDriveError('start 必须非负，limit 范围 1–1000')
        target = self.remote_path(path)
        try:
            result = self._json('GET', FILE_API, query=dict(method='list', dir=target, start=start, limit=limit, order='time', desc=1), authenticated=True, retry=True)
        except BaiduDriveError as error:
            # A newly authorized application has no folder until its first write.
            # Reading that one missing root is empty, without creating cloud data.
            if error.provider_code == '-9' and target == self.remote_path():
                return dict(path=target, items=[], start=start, limit=limit, hasMore=False, directoryExists=False)
            raise
        items = result.get('list', [])
        if not isinstance(items, list):
            raise BaiduDriveError('百度网盘列表响应格式错误', 502)
        return dict(path=target, items=[self._file(item) for item in items if isinstance(item, dict)], start=start, limit=limit, hasMore=bool(result.get('has_more', len(items) >= limit)), directoryExists=True)

    def authorized_user_id(self):
        """A stable account namespace for Android SDK tasks, from this token."""
        result = self._json('GET', NAS_API, query=dict(method='uinfo'), authenticated=True, retry=True)
        identifier = result.get('uk')
        if (isinstance(identifier, bool) or not isinstance(identifier, (str, int)) or
                not re.fullmatch(r'[0-9]{1,24}', str(identifier)) or int(identifier) <= 0):
            raise BaiduDriveError('百度未返回有效的账号用户 ID，请检查当前账号授权', 502)
        return str(identifier)

    def mkdir(self, path):
        target = self.remote_path(path, file=True)
        result = self._json('POST', FILE_API, query=dict(method='create'),
            form=dict(path=target, isdir=1, size=0, block_list='[]', rtype=0), authenticated=True)
        return self._file(result)

    def _start(self, operation, **fields):
        self._token()
        with self.lock:
            if self.closed:
                raise BaiduDriveError('网盘传输服务已关闭', 409)
            if sum(j['state'] in ('queued', 'running') for j in self._jobs.values()) >= 8:
                raise BaiduDriveError('网盘传输任务较多，请等待', 409)
            identifier = str(uuid.uuid4())
            self._jobs[identifier] = dict(jobId=identifier, operation=operation, state='queued', progress=0,
                bytesTransferred=0, createdAt=time.time(), updatedAt=time.time(), **fields)
            self._cancellations[identifier] = threading.Event()
            _atomic_private(self.jobs_path, self._jobs)
            return identifier

    def start_upload(self, local_path, remote_path, *, name=None):
        local = Path(local_path)
        if local.is_symlink() or not local.is_file():
            raise BaiduDriveError('请选择存在的普通文件')
        if local.stat().st_size > BLOCK_SIZE * MAX_BLOCKS:
            raise BaiduDriveError('该文件超过当前百度接口的 10000 个 4 MiB 分片上限，请先拆分文件')
        with self.lock:
            target = self.remote_path(remote_path, file=True)
            identifier = self._start('upload', name=name or local.name, remotePath=target, bytes=local.stat().st_size)
            self.pool.submit(self._run_upload, identifier, local, target)
            return self.job(identifier)

    def start_download(self, fs_id, local_path, *, expected_md5=None, expected_sha256=None):
        if isinstance(fs_id, bool) or not isinstance(fs_id, (int, str)) or not re.fullmatch(r'[0-9]{1,24}', str(fs_id)) or int(fs_id) <= 0:
            raise BaiduDriveError('fsId 必须是有效的百度文件 ID')
        local = Path(local_path)
        if local.exists() or local.is_symlink():
            raise BaiduDriveError('目标文件已存在，不会覆盖', 409)
        if not local.parent.is_dir():
            raise BaiduDriveError('请先选择存在的下载目录')
        for key, digest, length in (('expected_md5', expected_md5, 32), ('expected_sha256', expected_sha256, 64)):
            if digest is not None and (not isinstance(digest, str) or not re.fullmatch('[0-9a-fA-F]{' + str(length) + '}', digest)):
                raise BaiduDriveError(key + ' 格式错误')
        with self.lock:
            identifier = self._start('download', name=local.name, fsId=str(fs_id))
            self.pool.submit(self._run_download, identifier, str(fs_id), local, expected_md5, expected_sha256)
            return self.job(identifier)

    def job(self, identifier):
        with self.lock:
            if identifier not in self._jobs:
                raise BaiduDriveError('网盘任务不存在', 404)
            return json.loads(json.dumps(self._jobs[identifier]))

    def jobs(self):
        with self.lock:
            return dict(items=[self.job(i) for i in reversed(self._jobs)])

    def _update(self, identifier, *, persist=True, **fields):
        with self.lock:
            self._jobs[identifier].update(fields, updatedAt=time.time())
            if persist:
                _atomic_private(self.jobs_path, self._jobs)

    def _check(self, identifier):
        if self._cancellations[identifier].is_set():
            raise BaiduDriveError('传输已取消，原文件保留', 409)

    def cancel(self, identifier):
        with self.lock:
            job = self.job(identifier)
            if job['state'] in ('queued', 'running'):
                self._cancellations[identifier].set()
            return dict(job, cancelRequested=True)

    def _failed(self, identifier, error):
        # OSError paths and HTTP exception strings may contain private paths or tokens.
        message = str(error) if isinstance(error, BaiduDriveError) else '传输失败，原文件保留，请检查空间与网络后重试'
        state = 'cancelled' if self._cancellations[identifier].is_set() else 'failed'
        self._update(identifier, state=state, error=message)

    def _run_upload(self, identifier, local, target):
        try:
            self._update(identifier, state='running', phase='hashing')
            with local.open('rb') as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise BaiduDriveError('上传源文件不是普通文件')
                total, blocks, whole, sha = before.st_size, [], hashlib.md5(), hashlib.sha256()
                while True:
                    self._check(identifier)
                    chunk = source.read(BLOCK_SIZE)
                    if not chunk:
                        break
                    blocks.append(hashlib.md5(chunk).hexdigest())
                    whole.update(chunk)
                    sha.update(chunk)
                if not blocks:
                    blocks = [hashlib.md5(b'').hexdigest()]
                self._unchanged(source, before)
                form = dict(path=target, size=total, isdir=0, autoinit=1,
                    block_list=json.dumps(blocks, separators=(',', ':')), rtype=1,
                    local_mtime=int(before.st_mtime))
                # Precreate/create can rename a conflicting file; do not blindly retry
                # these mutations after an ambiguous network failure.
                pre = self._json('POST', FILE_API, query=dict(method='precreate'), form=form, authenticated=True)
                self._check(identifier)
                if str(pre.get('return_type')) == '2':
                    self._update(identifier, state='completed', phase='completed', progress=100, bytesTransferred=total,
                        result=dict(remotePath=pre.get('path', target), pathConfirmed=bool(pre.get('path')),
                                    bytes=total, md5=whole.hexdigest(), sha256=sha.hexdigest(), rapidUpload=True))
                    return
                upload_id = pre.get('uploadid')
                if not isinstance(upload_id, str) or not upload_id:
                    raise BaiduDriveError('百度预上传未返回 uploadid', 502)
                missing = pre.get('block_list', list(range(len(blocks))))
                if (not isinstance(missing, list) or any(
                        isinstance(n, bool) or not str(n).isdigit() or int(n) >= len(blocks) for n in missing)):
                    raise BaiduDriveError('百度预上传分片列表格式错误', 502)
                missing = [int(n) for n in missing]
                if len(set(missing)) != len(missing):
                    raise BaiduDriveError('百度预上传分片列表重复', 502)
                transferred = total - sum(max(0, min(BLOCK_SIZE, total - n * BLOCK_SIZE)) for n in missing)
                self._update(identifier, phase='uploading', progress=0, blockCount=len(blocks))
                for number in missing:
                    self._check(identifier)
                    self._unchanged(source, before)
                    part_size = max(0, min(BLOCK_SIZE, total - number * BLOCK_SIZE))
                    body = _MultipartSlice(source, number * BLOCK_SIZE, part_size, lambda: self._check(identifier))
                    reply = self._json('POST', UPLOAD_API, query=dict(method='upload', type='tmpfile', path=target,
                        uploadid=upload_id, partseq=number), authenticated=True, body=body, retry=True,
                        headers={'Content-Type': 'multipart/form-data; boundary=' + body.boundary, 'Content-Length': str(body.length)})
                    if str(reply.get('md5', '')).lower() != blocks[number]:
                        raise BaiduDriveError('上传分片 MD5 校验失败，任务停止', 502)
                    transferred += part_size
                    self._update(identifier, bytesTransferred=transferred, progress=min(99, transferred * 99 // max(1, total)))
                self._check(identifier)
                self._unchanged(source, before)
                self._update(identifier, phase='committing')
                form.pop('autoinit')
                form['uploadid'] = upload_id
                result = self._json('POST', FILE_API, query=dict(method='create'), form=form, authenticated=True)
                if int(result.get('size', -1)) != total or not result.get('fs_id'):
                    raise BaiduDriveError('云端创建结果未确认文件大小或 ID；请先检查云端列表，避免重复上传', 502)
                committed_path = self.remote_path(result.get('path', target), file=True)
                self._update(identifier, state='completed', phase='completed', progress=100, bytesTransferred=total,
                    result=dict(remotePath=committed_path, pathConfirmed=bool(result.get('path')), fsId=str(result.get('fs_id', '')), bytes=total,
                                md5=whole.hexdigest(), sha256=sha.hexdigest()))
        except BaseException as error:
            self._failed(identifier, error)
            if not isinstance(error, Exception):
                raise

    @staticmethod
    def _unchanged(source, before):
        after = os.fstat(source.fileno())
        if (after.st_size, after.st_mtime_ns, after.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
            raise BaiduDriveError('上传源文件在传输期间改变，任务停止', 409)

    def _run_download(self, identifier, fs_id, local, expected_md5=None, expected_sha256=None):
        pending = local.with_name('.' + local.name + '.' + identifier + '.part')
        try:
            self._update(identifier, state='running', phase='metadata')
            meta = self._json('GET', META_API, query=dict(method='filemetas', fsids=json.dumps([int(fs_id)]), dlink=1), authenticated=True, retry=True)
            items = meta.get('list', [])
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise BaiduDriveError('百度未返回可下载的文件', 404)
            item = items[0]
            if str(item.get('fs_id')) != fs_id:
                raise BaiduDriveError('百度返回的文件 ID 与请求不一致', 502)
            remote = self.remote_path(item.get('path', ''), file=True)
            if item.get('isdir') or not item.get('dlink'):
                raise BaiduDriveError('请选择可下载的普通文件')
            total = int(item.get('size', -1))
            if total < 0:
                raise BaiduDriveError('百度未返回有效文件大小', 502)
            link = _download_url(item['dlink'])
            parts = urlsplit(link)
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != 'access_token']
            if not (parts.hostname or '').endswith('bcebos.com'):
                query.append(('access_token', self._token()))
            link = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
            self._update(identifier, phase='downloading', bytes=total, remotePath=remote)
            for attempt in range(3):
                try:
                    self._check(identifier)
                    md5, sha, copied = hashlib.md5(), hashlib.sha256(), 0
                    # Exclusive temporary file protects previous downloads and symbolic links.
                    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as output, self.transport.open('GET', link,
                            headers={'User-Agent': 'pan.baidu.com', 'Accept-Encoding': 'identity'}, timeout=90) as response:
                        while chunk := response.read(IO_SIZE):
                            self._check(identifier)
                            output.write(chunk)
                            copied += len(chunk)
                            if copied > total:
                                raise BaiduDriveError('下载内容超过声明大小，任务停止', 502)
                            md5.update(chunk)
                            sha.update(chunk)
                            self._update(identifier, persist=False, bytesTransferred=copied, progress=min(99, copied * 99 // max(1, total)))
                        output.flush()
                        os.fsync(output.fileno())
                    if copied != total:
                        raise BaiduDriveError('下载未完成，文件大小校验失败', 502, retryable=True)
                    if ((expected_md5 and md5.hexdigest() != expected_md5.lower()) or
                            (expected_sha256 and sha.hexdigest() != expected_sha256.lower())):
                        raise BaiduDriveError('下载原始文件摘要与备份清单不一致，未保存最终文件', 502)
                    break
                except (HTTPError, URLError, OSError, TimeoutError, http.client.HTTPException, BaiduDriveError) as error:
                    pending.unlink(missing_ok=True)
                    self._check(identifier)
                    if isinstance(error, HTTPError):
                        retryable = error.code in (429, 500, 502, 503, 504)
                        error.close()
                    else:
                        retryable = error.retryable if isinstance(error, BaiduDriveError) else True
                    if not retryable or attempt == 2:
                        raise
                    time.sleep(0.25 * 2 ** attempt)
            self._check(identifier)
            # link() creates the destination atomically and refuses an existing file.
            # Both paths have the same parent/filesystem; source temp is then removed.
            os.link(pending, local)
            pending.unlink()
            self._update(identifier, state='completed', phase='completed', progress=100, bytesTransferred=copied,
                result=dict(name=local.name, remotePath=remote, fsId=fs_id, bytes=copied,
                            md5=md5.hexdigest(), sha256=sha.hexdigest(), remoteMd5=item.get('md5', ''),
                            md5MatchesRemote=md5.hexdigest() == str(item.get('md5', '')).lower(),
                            verifiedAgainstManifest=bool(expected_md5 or expected_sha256)))
        except BaseException as error:
            try:
                pending.unlink(missing_ok=True)
            finally:
                self._failed(identifier, error)
            if not isinstance(error, Exception):
                raise

    def close(self):
        with self.lock:
            self.closed = True
            for identifier, event in self._cancellations.items():
                if self._jobs[identifier]['state'] in ('queued', 'running'):
                    event.set()
        self.pool.shutdown(wait=True)
