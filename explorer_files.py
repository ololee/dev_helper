"""Explicit Explorer previews and macOS application hand-off for indexed assets.

Originals are never executed or edited. HTTP previews retain the descriptor
validated by AssetLibrary; native application hand-off uses a verified copy.
The HTTP caller must enforce a loopback-only, same-origin POST for native_action.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import threading
from urllib.parse import quote
import uuid

from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

try:
    from .asset_library import AssetLibraryError, TEXT_EXT
except ImportError:
    from asset_library import AssetLibraryError, TEXT_EXT

TEXT_LIMIT = 1024 * 1024
CHUNK_SIZE = 64 * 1024
_IMAGE_FORMATS = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'GIF': 'image/gif',
                  'WEBP': 'image/webp', 'BMP': 'image/bmp', 'ICO': 'image/x-icon'}
_AUDIO = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
          '.aac': 'audio/aac', '.ogg': 'audio/ogg', '.oga': 'audio/ogg',
          '.opus': 'audio/ogg', '.flac': 'audio/flac', '.aif': 'audio/aiff', '.aiff': 'audio/aiff'}
_VIDEO = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
          '.webm': 'video/webm', '.ogv': 'video/ogg'}
_TEXT = TEXT_EXT | {'.html', '.htm', '.xhtml', '.svg', '.mjs', '.cjs', '.log',
                    '.ini', '.toml', '.cfg', '.properties', '.command', '.url', '.webloc'}
_NATIVE = set(_AUDIO) | set(_VIDEO) | {
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.tif', '.tiff',
    '.tga', '.psd', '.exr', '.hdr', '.pdf', '.fbx', '.obj', '.glb', '.gltf',
    '.dae', '.3ds', '.ply', '.stl', '.ttf', '.otf', '.woff', '.woff2'}
_RASTER_NATIVE = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico'}


def _extension(asset):
    return Path(str(asset.get('name') or asset.get('path') or '')).suffix.lower()


def preview_info(asset):
    """Describe capabilities without opening a source or trusting its MIME string."""
    extension = _extension(asset)
    image = asset.get('image') or {}
    kind, mime = 'none', 'application/octet-stream'
    # Active web formats always become inert text, even if mislabeled as images.
    if extension in {'.html', '.htm', '.xhtml', '.svg', '.js', '.mjs', '.cjs'}:
        kind, mime = 'text', 'text/plain'
    elif image.get('status') == 'analyzed' and image.get('format') in _IMAGE_FORMATS:
        kind, mime = 'image', _IMAGE_FORMATS[image['format']]
    elif extension in _AUDIO:
        kind, mime = 'audio', _AUDIO[extension]
    elif extension in _VIDEO:
        kind, mime = 'video', _VIDEO[extension]
    elif extension == '.pdf':
        kind, mime = 'pdf', 'application/pdf'
    elif extension in _TEXT:
        kind, mime = 'text', 'text/plain'
    native = extension in _NATIVE
    if extension in _RASTER_NATIVE and kind != 'image':
        native = False
    return {'previewKind': kind, 'previewMime': mime,
            'canOpenNative': platform.system() == 'Darwin' and native}


def _range(value, size):
    if value is None:
        return 0, size, 200
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError('Invalid byte range')
    match = re.fullmatch(r'bytes=(\d{0,20})-(\d{0,20})', value.strip())
    if not match or not any(match.groups()) or size == 0:
        raise ValueError('Unsupported byte range')
    left, right = match.groups()
    if not left:
        count = int(right)
        if count <= 0:
            raise ValueError('Invalid suffix range')
        start, end = max(0, size - count), size - 1
    else:
        start = int(left)
        end = min(int(right), size - 1) if right else size - 1
        if start >= size or end < start:
            raise ValueError('Unsatisfiable byte range')
    return start, end - start + 1, 206


def _headers(asset):
    return {'Content-Disposition': "inline; filename*=UTF-8''" + quote(str(asset['name']), safe=''),
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store',
            'Content-Security-Policy': "sandbox; default-src 'none'", 'Accept-Ranges': 'bytes'}


def preview_response(library, asset_id, *, method='GET', range_header=None):
    """Build an inert or media preview, including bounded single-byte ranges."""
    asset = library.asset(asset_id)
    return _preview_response(asset, library.open_file(asset_id), method=method,
                             range_header=range_header)


def _download_asset(workspace, job_id):
    try:
        if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AssetLibraryError('无效的下载任务 ID', 400) from None
    # Read the provider ledger directly, never a decorated workspace.job method.
    job = workspace.drive.job(job_id)
    if job.get('operation') != 'download' or job.get('state') != 'completed':
        raise AssetLibraryError('文件尚未下载完成，暂不能预览', 409)
    return {'name': workspace.safe_name(job['name'], max_length=240)}


def _download_image_fact(asset, source):
    """Recognize a raster header within a fixed buffer; never decode its pixels."""
    if _extension(asset) in {'.html', '.htm', '.xhtml', '.svg', '.js', '.mjs', '.cjs'}:
        return asset
    try:
        from PIL import Image
    except ImportError:
        return asset
    try:
        raw = source.read(256 * 1024)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format in _IMAGE_FORMATS:
                return {**asset, 'image': {'status': 'analyzed', 'format': image.format}}
    except (OSError, ValueError, Image.DecompressionBombError):
        pass
    finally:
        source.seek(0)
    return asset


def download_preview_info(workspace, job_id):
    """Describe only a completed download's checked, application-managed file."""
    asset = _download_asset(workspace, job_id)
    try:
        with workspace.open_download_file(job_id) as source:
            asset = _download_image_fact(asset, source)
    except (OSError, ValueError):
        raise AssetLibraryError('下载文件不可用或已变化，请重新下载', 409) from None
    return {**preview_info(asset), 'canOpenNative': False,
            'previewUrl': '/api/netdisk/jobs/' + job_id + '/preview'}


def download_preview_response(workspace, job_id, *, method='GET', range_header=None):
    """Preview one completed download, never an arbitrary path or partial file."""
    asset = _download_asset(workspace, job_id)
    return _preview_response(asset, workspace.open_download_file(job_id), method=method,
                             range_header=range_header, probe_image=True)


def _preview_response(asset, context, *, method='GET', range_header=None, probe_image=False):
    if method not in ('GET', 'HEAD'):
        raise AssetLibraryError('预览只支持 GET 或 HEAD', 405)
    try:
        source = context.__enter__()
    except (OSError, ValueError):
        raise AssetLibraryError('源文件已变化或不可安全读取，请重新扫描或下载', 409) from None
    closed = threading.Event()
    close_lock = threading.Lock()

    def close():
        with close_lock:
            if not closed.is_set():
                closed.set()
                context.__exit__(None, None, None)

    try:
        if probe_image:
            asset = _download_image_fact(asset, source)
        info = preview_info(asset)
        if info['previewKind'] == 'none':
            raise AssetLibraryError('此格式暂不支持直接预览，请下载或在文件夹中查看', 409)
        file_info = os.fstat(source.fileno())
        if not stat.S_ISREG(file_info.st_mode):
            raise AssetLibraryError('只能预览普通文件', 409)
        headers = _headers(asset)
        if info['previewKind'] == 'text':
            raw = source.read(TEXT_LIMIT + 1)
            body = raw[:TEXT_LIMIT].decode('utf-8-sig', errors='replace').encode('utf-8')
            close()
            headers['X-Preview-Truncated'] = 'true' if len(raw) > TEXT_LIMIT else 'false'
            size = len(body)
        else:
            body, size = None, file_info.st_size
        try:
            start, count, status = _range(range_header, size)
        except ValueError:
            close()
            headers.update({'Content-Range': f'bytes */{size}', 'Content-Length': '0'})
            return Response(b'', status_code=416, headers=headers)
        headers['Content-Length'] = str(count)
        if status == 206:
            headers['Content-Range'] = f'bytes {start}-{start + count - 1}/{size}'
        if method == 'HEAD':
            close()
            return Response(b'', status_code=status, media_type=info['previewMime'], headers=headers)
        if body is not None:
            return Response(body[start:start + count], status_code=status,
                            media_type=info['previewMime'], headers=headers)
        source.seek(start)

        def read():
            try:
                remaining = count
                while remaining:
                    block = source.read(min(CHUNK_SIZE, remaining))
                    if not block:
                        raise OSError('File changed during preview')
                    remaining -= len(block)
                    yield block
            finally:
                close()

        return StreamingResponse(read(), status_code=status, media_type=info['previewMime'],
                                 headers=headers, background=BackgroundTask(close))
    except BaseException:
        close()
        raise


def _cache_directory(root):
    """Create a private cache without following any existing ancestor symlink."""
    root = Path(root).absolute()
    if root == Path(root.anchor) or '..' in root.parts:
        raise AssetLibraryError('本机预览需要独立的私有缓存目录', 409)
    if os.name == 'nt' or not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise AssetLibraryError('此平台不支持安全的本机预览缓存', 501)
    descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in root.parts[1:]:
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(name, 0o700, dir_fd=descriptor)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        os.fchmod(descriptor, 0o700)
        return root, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def native_action(library, asset_id, action, cache_root):
    """Handle an explicit local user action. Never call for relay/LAN requests."""
    if action not in ('open', 'reveal'):
        raise AssetLibraryError('不支持的文件操作', 400)
    if platform.system() != 'Darwin':
        raise AssetLibraryError('当前平台暂不支持本机应用预览或 Finder 定位', 501)
    asset = library.asset(asset_id)
    if action == 'open' and not preview_info(asset)['canOpenNative']:
        raise AssetLibraryError('此格式请使用文本预览、下载或在 Finder 中查看', 409)
    cache_fd, cache_name = None, None
    try:
        if action == 'reveal':
            # file_path validates project/cached signatures; keep the checked
            # descriptor alive until the explicit, non-executing reveal returns.
            with library.open_file(asset_id):
                original = library.file_path(asset_id)
                subprocess.run(['/usr/bin/open', '-R', str(original)], shell=False,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True, timeout=10)
            return {'revealed': True, 'message': '已在 Finder 中显示该文件'}
        cache, cache_fd = _cache_directory(cache_root)
        suffix = _extension(asset)
        stem = re.sub(r'[^\w. -]', '_', Path(asset['name']).stem).strip(' .-')[:100] or 'asset'
        cache_name = uuid.uuid4().hex + '_' + stem + suffix
        descriptor = os.open(cache_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=cache_fd)
        with os.fdopen(descriptor, 'wb') as output, library.open_file(asset_id) as source:
            sha, count = hashlib.sha256(), 0
            for block in iter(lambda: source.read(CHUNK_SIZE), b''):
                count += len(block)
                if count > asset['bytes']:
                    raise AssetLibraryError('源文件已变化，请重新扫描或下载', 409)
                sha.update(block)
                output.write(block)
            if count != asset['bytes'] or sha.hexdigest() != asset['sha256']:
                raise AssetLibraryError('源文件校验失败，请重新扫描或下载', 409)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o400)
        subprocess.run(['/usr/bin/open', str(cache / cache_name)], shell=False,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=10)
        return {'opened': True, 'copy': True,
                'message': '已在本机应用中打开校验后的预览副本，原素材保持不变'}
    except AssetLibraryError:
        if cache_fd is not None and cache_name:
            try:
                os.unlink(cache_name, dir_fd=cache_fd)
            except OSError:
                pass
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        if cache_fd is not None and cache_name:
            try:
                os.unlink(cache_name, dir_fd=cache_fd)
            except OSError:
                pass
        raise AssetLibraryError('未能打开本机预览，请检查文件和关联应用', 409) from None
    finally:
        if cache_fd is not None:
            os.close(cache_fd)
