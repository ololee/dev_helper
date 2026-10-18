"""Local, read-only game asset index. No project code or remote model is executed.

The catalog contains derived metadata; original files remain the source of truth.
Visual dHash/color features are deliberately distinct from imported semantic vectors.
"""
from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import heapq
import json
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave

VERSION = 1
DEFAULT_SKIP_DIRS = frozenset({'.git', '.svn', 'library', 'temp', 'obj', 'bin', 'node_modules',
    'build', 'dist', '__pycache__', '.venv', 'venv', 'logs', 'packagescache'})
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tga', '.tif', '.tiff', '.psd', '.dds', '.ktx', '.ktx2', '.exr', '.hdr', '.astc', '.pvr'}
KINDS = {'image', 'model', 'shader', 'font', 'material', 'animation', 'audio', 'spine',
    'code', 'scene', 'prefab', 'metadata', 'document', 'archive', 'video', 'file'}
EXT_KINDS = {
    'model': {'.fbx', '.obj', '.gltf', '.glb', '.blend', '.dae', '.3ds', '.ply', '.stl'},
    'shader': {'.shader', '.cginc', '.hlsl', '.glsl', '.vert', '.frag', '.compute', '.shadergraph', '.shadersubgraph'},
    'font': {'.ttf', '.otf', '.woff', '.woff2', '.fnt'},
    'material': {'.mat', '.mtl', '.material'},
    'animation': {'.anim', '.controller', '.overridecontroller', '.animation', '.clip', '.motion'},
    'audio': {'.wav', '.mp3', '.ogg', '.flac', '.aac', '.m4a', '.aiff', '.aif', '.opus', '.bank'},
    'spine': {'.skel', '.atlas'}, 'scene': {'.unity', '.scene', '.fire', '.tscn', '.scn'},
    'prefab': {'.prefab'}, 'code': {'.cs', '.py', '.js', '.ts', '.lua', '.cpp', '.c', '.h', '.hpp', '.java', '.kt', '.gd', '.swift', '.mm', '.m', '.go', '.rs', '.sh', '.bash', '.zsh', '.ps1', '.bat', '.cmd', '.sql'},
    'metadata': {'.meta'}, 'document': {'.md', '.txt', '.json', '.xml', '.yaml', '.yml', '.csv', '.css', '.uxml', '.uss', '.dcei'},
    'archive': {'.zip', '.tar', '.gz', '.7z', '.rar', '.bundle', '.assetbundle', '.wxapkg', '.ttpkg'},
    'video': {'.mp4', '.mov', '.webm', '.mkv', '.avi'},
}
TEXT_EXT = set().union(EXT_KINDS['code'], EXT_KINDS['shader'], EXT_KINDS['document'],
    EXT_KINDS['metadata'], EXT_KINDS['material'], EXT_KINDS['animation'],
    {'.asset', '.prefab', '.unity', '.scene', '.fire', '.atlas', '.obj', '.gltf', '.fnt'})
GUID_RE = re.compile(r'\bguid\s*:\s*([0-9a-fA-F]{32})\b')
C_UUID_RE = re.compile(r'["\']__uuid__["\']\s*:\s*["\']([^"\'\s]{1,128})["\']')
PATH_ATTR_RE = re.compile(r'\b(frameImage|image|texture|font|spine|skeleton|src|style)\s*=\s*(["\'])([^"\'\r\n]{1,500})\2', re.I)


class AssetLibraryError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise AssetLibraryError('Invalid project, asset or job ID') from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _signature(info):
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)


def _relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in ('..', '') or ':' in p or '\\' in p for p in path.parts):
        raise AssetLibraryError('Invalid relative asset path')
    return path


@contextmanager
def _read_source(root, relative):
    """Directory-relative O_NOFOLLOW traversal closes both leaf and parent symlink races."""
    parts = _relative(relative).parts
    if os.name != 'nt' and hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd:
        absolute = Path(root)
        fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in absolute.parts[1:] + parts[:-1]:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=fd)
            try:
                if not stat.S_ISREG(os.fstat(leaf).st_mode):
                    raise AssetLibraryError('Only regular files are indexed')
                with os.fdopen(leaf, 'rb', closefd=False) as handle:
                    yield handle
            finally:
                os.close(leaf)
        finally:
            os.close(fd)
    else:
        path = Path(root)
        for name in parts:
            path = path / name
            if path.is_symlink():
                raise AssetLibraryError('Symbolic links are not read')
        if not path.resolve().is_relative_to(Path(root)) or not path.is_file():
            raise AssetLibraryError('Asset escaped its project root')
        with path.open('rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise AssetLibraryError('Only regular files are indexed')
            yield handle


def _kind(path, header, text):
    suffix, name = path.suffix.lower(), path.name.lower()
    if header.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a')) or header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'image', {'confidence': 1, 'evidence': ['file signature']}
    if name.endswith(('.atlas.txt', '.skel.bytes')):
        return 'spine', {'confidence': 1, 'evidence': ['Spine compound extension']}
    if suffix == '.json' and text:
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get('skeleton'), dict) and isinstance(data.get('bones'), list):
                return 'spine', {'confidence': .98, 'evidence': ['Spine skeleton/bones JSON structure']}
        except (ValueError, RecursionError):
            pass
    if suffix == '.asset' and text:
        for keyword, value in [('SkeletonDataAsset', 'spine'), ('atlasFile:', 'spine'), ('m_Shader:', 'material'), ('m_AnimationClips:', 'animation')]:
            if keyword in text:
                return value, {'confidence': .85, 'evidence': ['Unity serialized field: ' + keyword]}
    if suffix in IMAGE_EXT:
        return 'image', {'confidence': .8, 'evidence': ['file extension; decoding checked separately']}
    for kind, extensions in EXT_KINDS.items():
        if suffix in extensions:
            return kind, {'confidence': .8, 'evidence': ['file extension: ' + suffix]}
    return 'file', {'confidence': .3, 'evidence': ['unrecognized or binary asset']}


def _usage(path, text, kind):
    """Role estimates remain labelled hypotheses, never a semantic model result."""
    lowered = path.as_posix().lower()
    guesses = []
    for category, regex in [('gui', r'(^|[/_. -])(ui|gui|icon|button|hud|sprite)([/_. -]|$)'),
            ('scene', r'(^|[/_. -])(scene|environment|terrain|skybox|background)([/_. -]|$)'),
            ('noise', r'(^|[/_. -])(noise|perlin|worley)([/_. -]|$)'),
            ('vfx', r'(^|[/_. -])(vfx|fx|particle|effect|effects)([/_. -]|$)'),
            ('model-texture', r'(^|[/_. -])(albedo|normal|roughness|metallic|diffuse|occlusion|pbr)([/_. -]|$)')]:
        if re.search(regex, lowered):
            guesses.append({'label': category, 'confidence': .55, 'evidence': 'path-name heuristic'})
    if text and 'ParticleSystem:' in text:
        guesses.append({'label': 'vfx', 'confidence': .95, 'evidence': 'Unity ParticleSystem component'})
    if text and 'CanvasRenderer:' in text:
        guesses.append({'label': 'gui', 'confidence': .9, 'evidence': 'Unity CanvasRenderer component'})
    return guesses


def _image_features(handle, pixel_limit):
    try:
        from PIL import Image, ImageOps
        handle.seek(0)
        with Image.open(handle) as original:
            result = {'status': 'analyzed', 'format': original.format, 'width': original.width,
                'height': original.height, 'frames': getattr(original, 'n_frames', 1),
                'hasAlpha': 'A' in original.getbands() or 'transparency' in original.info}
            if original.width * original.height > pixel_limit:
                return {**result, 'status': 'not-analyzed', 'reason': 'pixel-limit', 'pixelLimit': pixel_limit}, None, None
            original.draft('RGB', (256, 256))
            image = ImageOps.exif_transpose(original)
            if result['hasAlpha']:
                alpha = image.convert('RGBA').getchannel('A')
                minimum, maximum = alpha.getextrema()
                result.update(alphaMin=minimum, alphaMax=maximum, hasTransparency=minimum < 255)
            else:
                result['hasTransparency'] = False
            small = image.convert('RGBA')
            small.thumbnail((96, 96))
            pixels = [(r, g, b) for r, g, b, a in (small.get_flattened_data() if hasattr(small, 'get_flattened_data') else small.getdata()) if a >= 16]
            counts = Counter((r // 16, g // 16, b // 16) for r, g, b in pixels)
            result['palette'] = [{'hex': '#%02x%02x%02x' % tuple(v * 16 + 8 for v in key), 'fraction': round(count / max(1, len(pixels)), 4)} for key, count in counts.most_common(5)]
            histogram = [0.0] * 48
            for pixel in pixels:
                for channel, value in enumerate(pixel):
                    histogram[channel * 16 + value // 16] += 1
            denominator = math.sqrt(sum(v * v for v in histogram)) or 1
            histogram = [v / denominator for v in histogram]
            rgb = Image.new('RGB', small.size, (127, 127, 127))
            rgb.paste(small, mask=small.getchannel('A'))
            gray = rgb.convert('L').resize((9, 8), Image.Resampling.LANCZOS)
            values = list(gray.get_flattened_data() if hasattr(gray, 'get_flattened_data') else gray.getdata())
            bits = sum((values[y * 9 + x] > values[y * 9 + x + 1]) << (y * 8 + x) for y in range(8) for x in range(8))
            result['featureType'] = 'visual-dhash64-rgbhist48-v1'
            result['dHash'] = '%016x' % bits
            result['semantic'] = False
            result['analyzedFrames'] = [0]
            return result, '%016x' % bits, struct.pack('<48f', *histogram)
    except ImportError:
        return {'status': 'unavailable', 'reason': 'Pillow-not-installed'}, None, None
    except Exception as error:
        return {'status': 'not-analyzed', 'reason': 'decode-failed', 'errorType': type(error).__name__}, None, None


def _normal_uuid(value):
    value = value.strip()
    # Compressed Cocos UUIDs are case-sensitive; sub-asset suffixes are identities.
    return value.lower() if re.fullmatch(r'[0-9a-fA-F-]{32,36}', value) else value


def _resource_key(value):
    return value.strip().replace('\\', '/').removesuffix('.xml')


def _parse_references(path, text, kind):
    """Returns identifiers for this source and outgoing, explicitly sourced references."""
    identifiers, references = [], []
    relative = path.as_posix()
    identifiers.append(('path', relative, relative))
    for marker in ('/Resources/', '/AssetRaw/UI/Images/', '/AssetRaw/UI/Xmls/', '/AssetRaw/UI/Styles/'):
        probe = '/' + relative
        if marker in probe:
            address = probe.split(marker, 1)[1]
            address = str(PurePosixPath(address).with_suffix(''))
            identifiers.append(('resource', address, relative))
    if not text:
        return identifiers, references
    for guid in GUID_RE.findall(text):
        if path.suffix.lower() != '.meta':
            references.append(('unity-guid', guid.lower(), 'unity-yaml-guid'))
    for value in C_UUID_RE.findall(text):
        references.append(('cocos-uuid', _normal_uuid(value), 'cocos-serialized-uuid'))
    if path.suffix.lower() == '.meta':
        destination = relative[:-5]
        match = re.search(r'^guid:\s*([0-9a-fA-F]{32})\s*$', text, re.M)
        if match:
            identifiers.append(('unity-guid', match.group(1).lower(), destination))
        try:
            data = json.loads(text)
            def visit(value, depth=0):
                if depth > 32 or not isinstance(value, dict):
                    return
                candidate = value.get('uuid')
                if isinstance(candidate, str) and 1 <= len(candidate) <= 128:
                    identifiers.append(('cocos-uuid', _normal_uuid(candidate), destination))
                for child in value.get('subMetas', {}).values() if isinstance(value.get('subMetas'), dict) else ():
                    visit(child, depth + 1)
            visit(data)
        except (ValueError, RecursionError):
            pass
    if path.name.lower().endswith(('.atlas', '.atlas.txt')):
        # A page header occurs at start/after a blank line and is followed by page metadata.
        lines = text.splitlines()
        for index, line in enumerate(lines):
            value = line.strip()
            if not value or ':' in value or index and lines[index - 1].strip():
                continue
            if index + 1 < len(lines) and re.match(r'\s*(size|format|filter|repeat|pma|scale)\s*:', lines[index + 1]):
                candidate = path.parent / value
                if not candidate.is_absolute() and '..' not in candidate.parts and '\\' not in value:
                    references.append(('path', candidate.as_posix(), 'spine-atlas-page'))
    if path.suffix.lower() in {'.xml', '.dcei', '.uxml'}:
        for attribute, _, value in PATH_ATTR_RE.findall(text):
            if value.startswith(('#', '{', 'http:', 'https:', 'data:')):
                continue
            if attribute.lower() == 'src' and re.search(r'<include\b[^>]*\bsrc\s*=\s*(["\'])' + re.escape(value) + r'\1', text, re.I):
                references.append(('resource', _resource_key(value), 'dcei-include'))
            else:
                references.append(('resource', _resource_key(value), 'dcei-resource-attribute:' + attribute))
    if path.suffix.lower() in EXT_KINDS['shader']:
        for value in re.findall(r'^\s*#\s*include\s*["<]([^">\r\n]+)[">]', text, re.M):
            candidate = path.parent / value
            if not candidate.is_absolute() and '..' not in candidate.parts:
                references.append(('path', candidate.as_posix(), 'shader-include'))
    if path.suffix.lower() == '.obj':
        for value in re.findall(r'^\s*mtllib\s+([^\r\n]+)', text, re.M):
            candidate = path.parent / value.strip()
            if not candidate.is_absolute() and '..' not in candidate.parts:
                references.append(('path', candidate.as_posix(), 'obj-material-library'))
    if path.suffix.lower() == '.gltf':
        try:
            data = json.loads(text)
            for group in ('buffers', 'images'):
                for entry in data.get(group, []):
                    value = entry.get('uri') if isinstance(entry, dict) else None
                    if isinstance(value, str) and not value.startswith(('data:', 'http:', 'https:')):
                        candidate = path.parent / value
                        if not candidate.is_absolute() and '..' not in candidate.parts:
                            references.append(('path', candidate.as_posix(), 'gltf-' + group))
        except (ValueError, TypeError, AttributeError, RecursionError):
            pass
    return list(dict.fromkeys(identifiers)), list(dict.fromkeys(references))


def _structure_metadata(path, text, kind):
    if not text:
        return {}
    if kind == 'model' and path.suffix.lower() == '.obj':
        counts = Counter(line.split(maxsplit=1)[0] for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#'))
        return {'model': {'status': 'analyzed', 'format': 'OBJ', 'vertices': counts['v'], 'normals': counts['vn'], 'uvs': counts['vt'], 'faces': counts['f'], 'objects': counts['o'], 'groups': counts['g']}}
    if path.suffix.lower() == '.gltf':
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {'model': {'status': 'analyzed', 'format': 'glTF JSON', **{field: len(data.get(field, [])) for field in ('meshes', 'materials', 'animations', 'skins', 'textures', 'nodes') if isinstance(data.get(field, []), list)}}}
        except (ValueError, RecursionError):
            pass
    if kind == 'shader':
        return {'shader': {'status': 'analyzed', 'method': 'text-declarations-only', 'properties': list(dict.fromkeys(re.findall(r'^\s*(_[A-Za-z0-9_]+)\s*\(', text, re.M)))[:500], 'compiled': False}}
    if kind == 'spine' and path.suffix.lower() == '.json':
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get('skeleton'), dict):
                return {'spine': {'status': 'analyzed', 'format': 'Spine JSON', 'version': str(data['skeleton'].get('spine', ''))[:100], 'bones': len(data.get('bones', [])) if isinstance(data.get('bones'), list) else None, 'animations': list(data.get('animations', {}))[:1000] if isinstance(data.get('animations'), dict) else []}}
        except (ValueError, RecursionError):
            pass
    return {}


class AssetLibrary:
    def __init__(self, data_dir):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'assets.sqlite3'
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='asset-index')
        self.closed = False
        with self._db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,root TEXT UNIQUE NOT NULL,name TEXT NOT NULL,options TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,stats TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),path TEXT NOT NULL,size INTEGER,mtime_ns INTEGER,ctime_ns INTEGER,device INTEGER,inode INTEGER,sha256 TEXT,md5 TEXT,kind TEXT,extension TEXT,mime TEXT,metadata TEXT NOT NULL,search_text TEXT,parser_version INTEGER,dhash TEXT,color BLOB,UNIQUE(project_id,path));
                CREATE TABLE IF NOT EXISTS asset_directories(project_id TEXT NOT NULL REFERENCES projects(id),path TEXT NOT NULL,mtime_ns INTEGER,PRIMARY KEY(project_id,path));
                CREATE INDEX IF NOT EXISTS assets_project_kind ON assets(project_id,kind);
                CREATE INDEX IF NOT EXISTS assets_sha256 ON assets(sha256);
                CREATE TABLE IF NOT EXISTS identifiers(source_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,project_id TEXT NOT NULL,namespace TEXT NOT NULL,value TEXT NOT NULL,target_path TEXT NOT NULL,UNIQUE(source_id,namespace,value,target_path));
                CREATE INDEX IF NOT EXISTS identifier_lookup ON identifiers(project_id,namespace,value);
                CREATE TABLE IF NOT EXISTS refs(source_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,namespace TEXT NOT NULL,value TEXT NOT NULL,evidence TEXT NOT NULL,UNIQUE(source_id,namespace,value,evidence));
                CREATE INDEX IF NOT EXISTS refs_source ON refs(source_id);
                CREATE TABLE IF NOT EXISTS annotations(asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,source TEXT NOT NULL,data TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(asset_id,source));
                CREATE TABLE IF NOT EXISTS embeddings(asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,model TEXT NOT NULL,dimensions INTEGER NOT NULL,vector BLOB NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(asset_id,model));
                CREATE INDEX IF NOT EXISTS embeddings_model ON embeddings(model,dimensions);
                CREATE TABLE IF NOT EXISTS remote_locations(asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,provider TEXT NOT NULL,fs_id TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,PRIMARY KEY(asset_id,provider,fs_id));
                CREATE TABLE IF NOT EXISTS local_files(asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,sha256 TEXT NOT NULL,relative_path TEXT NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,ctime_ns INTEGER NOT NULL,device INTEGER NOT NULL,inode INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS asset_storage(asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,remote_only INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS query_profiles(space TEXT PRIMARY KEY,base_url TEXT NOT NULL,model TEXT NOT NULL,dimensions INTEGER,input_version TEXT NOT NULL,mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL);
            ''')
            try:
                db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS asset_search USING fts5(id UNINDEXED,project_id UNINDEXED,text)')
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            for row in db.execute("SELECT id,data FROM jobs WHERE state IN ('queued','running')").fetchall():
                data = json.loads(row['data'])
                data.update(state='interrupted', error='Server restarted; run the scan again')
                db.execute('UPDATE jobs SET state=?,data=? WHERE id=?', ('interrupted', _json(data), row['id']))
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        self.closed = True
        self.pool.shutdown(wait=True)

    def capabilities(self):
        return {'indexVersion': VERSION, 'modelInference': False, 'visualFeatures': 'dHash64 + RGB histogram48; not semantic embeddings',
            'semanticEmbeddings': 'imported normalized vectors; same exact model name and dimensions only',
            'ocr': {'provider': 'apple-vision' if sys.platform == 'darwin' else 'tesseract', 'available': bool(sys.platform == 'darwin' and shutil.which('swiftc') or shutil.which('tesseract')), 'automatic': False, 'localOnly': True},
            'defaultSkipDirs': sorted(DEFAULT_SKIP_DIRS), 'symlinks': 'skipped', 'maxFileBytes': None,
            'defaultMaxTextBytes': 8 * 1024 ** 2, 'defaultMaxImagePixels': 32_000_000,
            'dependencyFormats': ['Unity meta GUID/YAML', 'Cocos meta/serialized UUID', 'Spine atlas pages', 'DCEI XML/resource addresses', 'glTF external images/buffers', 'shader includes'],
            'limits': 'Analysis limits do not exclude originals from indexing or binary download; binary Unity/FBX references require an upstream exporter.'}

    def register_project(self, root, name='', skip_dirs=None, max_text_bytes=8 * 1024 ** 2, max_image_pixels=32_000_000):
        if not isinstance(root, (str, Path)):
            raise AssetLibraryError('Choose a project directory')
        supplied = Path(root).expanduser()
        if supplied.is_symlink():
            raise AssetLibraryError('Project root must not be a symbolic link')
        try:
            path = supplied.resolve(strict=True)
        except (OSError, RuntimeError):
            raise AssetLibraryError('Project directory is unavailable', 404) from None
        if not path.is_dir() or path == Path(path.anchor) or path == Path.home().resolve():
            raise AssetLibraryError('Choose a specific project directory, not the disk or home directory')
        if path == self.root or path.is_relative_to(self.root):
            raise AssetLibraryError('The index storage is not a source project')
        if not isinstance(name, str) or len(name) > 200:
            raise AssetLibraryError('Project name must be at most 200 characters')
        if skip_dirs is None:
            skip_dirs = sorted(DEFAULT_SKIP_DIRS)
        if not isinstance(skip_dirs, (list, tuple)) or len(skip_dirs) > 100 or any(not isinstance(v, str) or not v or len(v) > 100 or '/' in v or '\\' in v or v in ('.', '..') for v in skip_dirs):
            raise AssetLibraryError('skip_dirs must contain directory names only')
        for value, high in ((max_text_bytes, 64 * 1024 ** 2), (max_image_pixels, 250_000_000)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
                raise AssetLibraryError('Invalid analysis limit')
        options = dict(skipDirs=sorted(set(v.casefold() for v in skip_dirs)), maxTextBytes=max_text_bytes, maxImagePixels=max_image_pixels)
        with self.lock, self._db() as db:
            previous = db.execute('SELECT id FROM projects WHERE root=?', (str(path),)).fetchone()
            identifier = previous['id'] if previous else str(uuid.uuid4())
            stamp = time.time()
            db.execute('INSERT INTO projects(id,root,name,options,created,updated) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,options=excluded.options,updated=excluded.updated', (identifier, str(path), name.strip() or path.name, _json(options), stamp, stamp))
        return self.project(identifier)

    def project(self, project_id, include_root=False):
        with self._db() as db:
            row = db.execute('SELECT * FROM projects WHERE id=?', (_id(project_id),)).fetchone()
            if row is None:
                raise AssetLibraryError('Project not found', 404)
            result = {'id': row['id'], 'name': row['name'], 'options': json.loads(row['options']), 'createdAt': row['created'], 'updatedAt': row['updated'], 'stats': json.loads(row['stats'])}
            result['assetCount'] = db.execute('SELECT COUNT(*) FROM assets WHERE project_id=?', (row['id'],)).fetchone()[0]
            if include_root:
                result['root'] = row['root']
            return result

    def projects(self, include_root=False):
        with self._db() as db:
            identifiers = [r[0] for r in db.execute('SELECT id FROM projects ORDER BY updated DESC')]
        return [self.project(value, include_root) for value in identifiers]

    def start_scan(self, project_id, force=False):
        project = self.project(project_id)
        if project['options'].get('remoteBacked'):
            raise AssetLibraryError('Restored cloud catalogs do not need scanning; fetch originals as needed', 409)
        if not isinstance(force, bool) or self.closed:
            raise AssetLibraryError('Invalid scan request or library closed')
        with self.lock, self._db() as db:
            active = db.execute("SELECT data FROM jobs WHERE project_id=? AND state IN ('queued','running')", (project_id,)).fetchone()
            if active:
                return json.loads(active['data'])
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 8:
                raise AssetLibraryError('Wait for existing index jobs', 409)
            identifier = str(uuid.uuid4())
            data = {'jobId': identifier, 'projectId': project_id, 'state': 'queued', 'createdAt': time.time(), 'progress': {'discovered': 0, 'indexed': 0, 'unchanged': 0}}
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)', (identifier, project_id, 'queued', _json(data), data['createdAt']))
        self.pool.submit(self._run_scan, identifier, project_id, force)
        return data

    def job(self, job_id):
        with self._db() as db:
            row = db.execute('SELECT data FROM jobs WHERE id=?', (_id(job_id),)).fetchone()
        if row is None:
            raise AssetLibraryError('Index job not found', 404)
        return json.loads(row['data'])

    def jobs(self, limit=50):
        limit = _limit(limit, 100)
        with self._db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM jobs ORDER BY created DESC LIMIT ?', (limit,))]

    def _update_job(self, identifier, **fields):
        with self.lock, self._db() as db:
            data = json.loads(db.execute('SELECT data FROM jobs WHERE id=?', (identifier,)).fetchone()[0])
            data.update(fields, updatedAt=time.time())
            db.execute('UPDATE jobs SET state=?,data=? WHERE id=?', (data['state'], _json(data), identifier))

    def _walk(self, root, options, stats, directories=None):
        pending = [Path(root)]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            stats['skippedSymlinks'] += 1
                        elif entry.is_dir(follow_symlinks=False):
                            if entry.name.casefold() in options['skipDirs'] or Path(entry.path) == self.root:
                                stats['skippedDirs'] += 1
                            else:
                                if directories is not None:
                                    directories.append((Path(entry.path).relative_to(root).as_posix(), entry.stat(follow_symlinks=False).st_mtime_ns))
                                pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path).relative_to(root).as_posix()
            except OSError:
                stats['walkErrors'] += 1

    def _analyze(self, root, relative, options, project_id):
        with _read_source(root, relative) as handle:
            before = os.fstat(handle.fileno())
            sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
            header, collected = b'', bytearray()
            text_cap = options['maxTextBytes']
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                sha.update(block)
                md5.update(block)
                if not header:
                    header = block[:128]
                if len(collected) < text_cap + 1:
                    collected.extend(block[:text_cap + 1 - len(collected)])
            path = PurePosixPath(relative)
            text, text_status = '', 'not-applicable'
            if path.suffix.lower() in TEXT_EXT or path.name.lower().endswith('.atlas.txt'):
                if before.st_size > text_cap:
                    text_status = 'not-analyzed:text-byte-limit'
                else:
                    try:
                        if b'\0' not in collected:
                            text = collected.decode('utf-8-sig')
                            text_status = 'analyzed'
                        else:
                            text_status = 'not-analyzed:binary'
                    except UnicodeError:
                        text_status = 'not-analyzed:encoding'
            kind, classification = _kind(path, header, text)
            metadata = {'classification': classification, 'usageHypotheses': _usage(path, text, kind),
                'structureAnalysis': text_status, 'ocr': {'status': 'not-analyzed', 'text': None},
                'indexedAt': time.time(), 'contentVersion': VERSION, 'indexingStatus': 'ready',
                'analysisLimits': {key: options[key] for key in ('maxTextBytes', 'maxImagePixels')}}
            metadata.update(_structure_metadata(path, text, kind))
            if kind in ('model', 'material', 'animation', 'font', 'audio', 'spine') and kind not in metadata:
                metadata[kind] = {'status': 'not-analyzed', 'reason': 'format-requires-specialized-reader'}
            dhash, color = None, None
            if kind == 'image':
                metadata['image'], dhash, color = _image_features(handle, options['maxImagePixels'])
            if kind == 'font' and path.suffix.lower() in {'.ttf', '.otf'} and before.st_size <= text_cap:
                try:
                    from PIL import ImageFont
                    handle.seek(0)
                    font = ImageFont.truetype(handle, 12)
                    family, style = font.getname()
                    metadata['font'] = {'status': 'analyzed', 'family': family, 'style': style, 'method': 'FreeType font metadata'}
                except Exception as error:
                    metadata['font'] = {'status': 'not-analyzed', 'reason': 'font-metadata-unavailable', 'errorType': type(error).__name__}
            if kind == 'audio' and path.suffix.lower() == '.wav':
                try:
                    handle.seek(0)
                    with wave.open(handle, 'rb') as audio:
                        metadata['audio'] = {'status': 'analyzed', 'channels': audio.getnchannels(), 'sampleRate': audio.getframerate(), 'frames': audio.getnframes(), 'durationSeconds': audio.getnframes() / max(1, audio.getframerate())}
                except (ValueError, OSError, EOFError, wave.Error):
                    metadata['audio'] = {'status': 'not-analyzed', 'reason': 'unsupported-or-invalid-wave'}
            identifiers, refs = _parse_references(path, text, kind)
            after = os.fstat(handle.fileno())
            if _signature(before) != _signature(after):
                raise AssetLibraryError('Source changed while indexing; scan again', 409)
            # A moved/deleted path must not silently retain the metadata of its former inode.
            current = Path(root) / relative
            if current.is_symlink() or _signature(current.stat()) != _signature(after):
                raise AssetLibraryError('Source changed while indexing; scan again', 409)
            searchable = relative + ' ' + kind + ' ' + ' '.join(x['label'] for x in metadata['usageHypotheses'])
            if kind in ('document', 'code', 'shader'):
                searchable += ' ' + text[:65536]
            return {'id': str(uuid.uuid5(uuid.UUID(project_id), relative)), 'relative': relative, 'stat': _signature(after), 'sha256': sha.hexdigest(), 'md5': md5.hexdigest(), 'kind': kind,
                'extension': path.suffix.lower(), 'mime': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                'metadata': metadata, 'search': searchable, 'identifiers': identifiers, 'refs': refs, 'dhash': dhash, 'color': color}

    def _upsert(self, db, project_id, item):
        existing = db.execute('SELECT id FROM assets WHERE project_id=? AND path=?',
                              (project_id, item['relative'])).fetchone()
        if existing:
            item['id'] = existing['id']
        else:
            collision = db.execute('SELECT id FROM assets WHERE id=?', (item['id'],)).fetchone()
            if collision:
                item['id'] = str(uuid.uuid4())
        old = db.execute('SELECT sha256,metadata FROM assets WHERE id=?', (item['id'],)).fetchone()
        if old and old['sha256'] == item['sha256']:
            ocr = json.loads(old['metadata']).get('ocr', {})
            if ocr.get('status') == 'analyzed':
                item['metadata']['ocr'] = ocr
        if old and old['sha256'] != item['sha256']:
            db.execute('DELETE FROM embeddings WHERE asset_id=?', (item['id'],))
            # User annotations are retained with the content hash they described.
        db.execute('DELETE FROM asset_storage WHERE asset_id=?', (item['id'],))
        values = (item['id'], project_id, item['relative'], *item['stat'], item['sha256'], item['md5'], item['kind'], item['extension'], item['mime'], _json(item['metadata']), item['search'], VERSION, item['dhash'], item['color'])
        db.execute('INSERT INTO assets VALUES(' + ','.join('?' for _ in values) + ') ON CONFLICT(id) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns,device=excluded.device,inode=excluded.inode,sha256=excluded.sha256,md5=excluded.md5,kind=excluded.kind,extension=excluded.extension,mime=excluded.mime,metadata=excluded.metadata,search_text=excluded.search_text,parser_version=excluded.parser_version,dhash=excluded.dhash,color=excluded.color', values)
        db.execute('DELETE FROM refs WHERE source_id=?', (item['id'],))
        db.execute('DELETE FROM identifiers WHERE source_id=?', (item['id'],))
        db.executemany('INSERT OR IGNORE INTO identifiers VALUES(?,?,?,?,?)', [(item['id'], project_id, *entry) for entry in item['identifiers']])
        db.executemany('INSERT OR IGNORE INTO refs VALUES(?,?,?,?)', [(item['id'], *entry) for entry in item['refs']])
        self._refresh_search(db, item['id'])

    def _refresh_search(self, db, asset_id):
        if not self.fts:
            return
        row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
        pieces = [row['search_text']]
        for annotation in db.execute('SELECT data FROM annotations WHERE asset_id=?', (asset_id,)):
            data = json.loads(annotation['data'])
            if data.get('sha256') == row['sha256']:
                pieces.extend(data.get(key, '') for key in ('label', 'purpose', 'ocrText'))
                pieces.extend(data.get('categories', []))
        ocr = json.loads(row['metadata']).get('ocr', {})
        if ocr.get('status') == 'analyzed':
            pieces.append(ocr.get('text') or '')
        db.execute('DELETE FROM asset_search WHERE id=?', (asset_id,))
        db.execute('INSERT INTO asset_search VALUES(?,?,?)', (asset_id, row['project_id'], ' '.join(pieces)))

    def _run_scan(self, identifier, project_id, force):
        started = time.monotonic()
        stats = dict(discovered=0, indexed=0, unchanged=0, failed=0, skippedSymlinks=0, skippedDirs=0, walkErrors=0, hashBytes=0, pruned=0)
        errors, seen = [], set()
        self._update_job(identifier, state='running')
        try:
            project = self.project(project_id, include_root=True)
            root, options = Path(project['root']), project['options']
            if not root.is_dir() or root.is_symlink():
                raise AssetLibraryError('Source project is unavailable', 409)
            with self._db() as db:
                previous = {row['path']: row for row in db.execute('SELECT * FROM assets WHERE project_id=?', (project_id,))}
                remote_only = {row[0] for row in db.execute('SELECT s.asset_id FROM asset_storage s JOIN assets a ON a.id=s.asset_id WHERE a.project_id=? AND s.remote_only=1', (project_id,))}
            pending_items, pending_bytes, directories = [], 0, []
            def flush():
                nonlocal pending_bytes
                if pending_items:
                    with self.lock, self._db() as db:
                        for pending_item in pending_items:
                            self._upsert(db, project_id, pending_item)
                    pending_items.clear()
                    pending_bytes = 0
            for relative in self._walk(root, options, stats, directories):
                seen.add(relative)
                stats['discovered'] += 1
                try:
                    old = previous.get(relative)
                    info = (root / relative).lstat()
                    if old and not force and old['parser_version'] == VERSION and json.loads(old['metadata']).get('analysisLimits') == {key: options[key] for key in ('maxTextBytes', 'maxImagePixels')} and (old['size'], old['mtime_ns'], old['ctime_ns'], old['device'], old['inode']) == _signature(info):
                        stats['unchanged'] += 1
                    else:
                        item = self._analyze(root, relative, options, project_id)
                        if old:
                            item['id'] = old['id']
                        pending_items.append(item)
                        pending_bytes += len(item['search']) + sum(len(str(r)) for r in item['refs'])
                        if len(pending_items) >= 64 or pending_bytes >= 8 * 1024 ** 2:
                            flush()
                        stats['indexed'] += 1
                        stats['hashBytes'] += info.st_size
                except (OSError, ValueError, TypeError, RecursionError) as error:
                    stats['failed'] += 1
                    if len(errors) < 50:
                        errors.append({'path': relative, 'errorType': type(error).__name__})
                if stats['discovered'] % 64 == 0:
                    self._update_job(identifier, progress=dict(stats))
            flush()
            if not stats['walkErrors']:
                stale = [row['id'] for path, row in previous.items() if path not in seen and row['id'] not in remote_only]
                with self.lock, self._db() as db:
                    if self.fts:
                        db.executemany('DELETE FROM asset_search WHERE id=?', [(value,) for value in stale])
                    db.executemany('DELETE FROM assets WHERE id=?', [(value,) for value in stale])
                    db.execute('DELETE FROM asset_directories WHERE project_id=?', (project_id,))
                    db.executemany('INSERT INTO asset_directories VALUES(?,?,?)',
                                   [(project_id, path, mtime) for path, mtime in directories])
                    stats['pruned'] = len(stale)
            stats['elapsedSeconds'] = round(time.monotonic() - started, 3)
            with self.lock, self._db() as db:
                db.execute('UPDATE projects SET stats=?,updated=? WHERE id=?', (_json(stats), time.time(), project_id))
            self._update_job(identifier, state='completed' if not stats['failed'] and not stats['walkErrors'] else 'completed-with-errors', progress=stats, errors=errors, finishedAt=time.time())
        except Exception as error:
            self._update_job(identifier, state='failed', progress=stats, error=type(error).__name__ + ': ' + str(error), finishedAt=time.time())

    def list_assets(self, project_id=None, query='', kind=None, limit=100, offset=0, sort='name', order='asc'):
        limit, offset = _limit(limit, 500), _offset(offset)
        if project_id is not None:
            _id(project_id)
        if kind is not None and kind not in KINDS:
            raise AssetLibraryError('Unknown asset kind')
        if not isinstance(query, str) or len(query) > 500:
            raise AssetLibraryError('Query must be at most 500 characters')
        sorts = {'name': 'a.path COLLATE NOCASE', 'modified': 'a.mtime_ns', 'size': 'a.size', 'kind': 'a.kind COLLATE NOCASE'}
        if sort not in sorts or order not in ('asc', 'desc'):
            raise AssetLibraryError('Sort must be name/modified/size/kind with asc/desc order')
        clauses, args = [], []
        if project_id:
            clauses.append('a.project_id=?'); args.append(project_id)
        if kind:
            clauses.append('a.kind=?'); args.append(kind)
        tokens = re.findall(r'\w+', query, re.UNICODE)
        if query.strip():
            if self.fts and tokens:
                clauses.append('a.id IN (SELECT id FROM asset_search WHERE asset_search MATCH ?)')
                args.append(' AND '.join('"' + v.replace('"', '""') + '"*' for v in tokens[:32]))
            else:
                clauses.append('a.search_text LIKE ? ESCAPE \'\\\'')
                args.append('%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self._db() as db:
            total = db.execute('SELECT COUNT(*) FROM assets a' + where, args).fetchone()[0]
            rows = db.execute('SELECT a.* FROM assets a' + where + ' ORDER BY ' + sorts[sort] + ' ' + order.upper() + ',a.id LIMIT ? OFFSET ?', [*args, limit, offset]).fetchall()
            return {'items': [self._public_asset(db, row) for row in rows], 'total': total, 'limit': limit, 'offset': offset, 'sort': sort, 'order': order}

    def _public_asset(self, db, row):
        result = {'id': row['id'], 'projectId': row['project_id'], 'path': row['path'], 'name': PurePosixPath(row['path']).name,
            'bytes': row['size'], 'modifiedAt': row['mtime_ns'] / 1e9, 'sha256': row['sha256'], 'md5': row['md5'],
            'kind': row['kind'], 'extension': row['extension'], 'mimeType': row['mime'], **json.loads(row['metadata'])}
        result['annotations'] = []
        for item in db.execute('SELECT source,data,updated FROM annotations WHERE asset_id=? ORDER BY source', (row['id'],)):
            data = json.loads(item['data'])
            result['annotations'].append({**data, 'source': item['source'], 'updatedAt': item['updated'], 'stale': data.get('sha256') != row['sha256']})
        result['embeddings'] = [{'model': r['model'], 'dimensions': r['dimensions']} for r in db.execute('SELECT model,dimensions FROM embeddings WHERE asset_id=? AND sha256=?', (row['id'], row['sha256']))]
        result['remoteLocations'] = self._remote_locations(db, row['id'], row['sha256'])
        cached = db.execute('SELECT 1 FROM local_files WHERE asset_id=? AND sha256=?', (row['id'], row['sha256'])).fetchone()
        options = json.loads(db.execute('SELECT options FROM projects WHERE id=?', (row['project_id'],)).fetchone()[0])
        remote = db.execute('SELECT remote_only FROM asset_storage WHERE asset_id=?', (row['id'],)).fetchone()
        result['availability'] = 'cached' if cached else 'remote' if options.get('remoteBacked') or remote and remote[0] else 'local'
        return result

    def asset(self, asset_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (_id(asset_id),)).fetchone()
            if row is None:
                raise AssetLibraryError('Asset not found', 404)
            return self._public_asset(db, row)

    def assets(self, asset_ids):
        if not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 500 or any(not isinstance(v, str) for v in asset_ids) or len(set(asset_ids)) != len(asset_ids):
            raise AssetLibraryError('Select 1–500 unique asset IDs')
        return [self.asset(value) for value in asset_ids]

    def file_path(self, asset_id):
        with self._db() as db:
            row = db.execute('SELECT a.*,p.root,p.options FROM assets a JOIN projects p ON p.id=a.project_id WHERE a.id=?', (_id(asset_id),)).fetchone()
        if row is None:
            raise AssetLibraryError('Asset not found', 404)
        cached = self._cached_file(row)
        if cached:
            return cached
        if self.asset(asset_id)['availability'] == 'remote':
            raise AssetLibraryError('Original is stored in cloud; fetch this asset first', 409)
        try:
            with _read_source(row['root'], row['path']) as handle:
                if _signature(os.fstat(handle.fileno())) != (row['size'], row['mtime_ns'], row['ctime_ns'], row['device'], row['inode']):
                    raise AssetLibraryError('Source changed; scan again before downloading', 409)
            path = Path(row['root']) / row['path']
            if not path.resolve().is_relative_to(Path(row['root'])):
                raise AssetLibraryError('Source escaped project root', 409)
            return path
        except OSError:
            raise AssetLibraryError('Source removed or no longer safe; scan again', 409) from None

    @contextmanager
    def open_file(self, asset_id):
        """Use this descriptor for streamed HTTP content to avoid a later symlink race."""
        item = self.asset(asset_id)
        with self._db() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
        cached = self._cached_file(row)
        if cached:
            with _read_source(self.root, cached.relative_to(self.root).as_posix()) as handle:
                with self._db() as db:
                    saved = db.execute('SELECT * FROM local_files WHERE asset_id=? AND sha256=?', (asset_id, row['sha256'])).fetchone()
                if saved is None or _signature(os.fstat(handle.fileno())) != tuple(saved[key] for key in ('size', 'mtime_ns', 'ctime_ns', 'device', 'inode')):
                    raise AssetLibraryError('Cached original changed; fetch this asset again', 409)
                yield handle
            return
        project = self.project(item['projectId'], include_root=True)
        if item['availability'] == 'remote':
            raise AssetLibraryError('Original is stored in cloud; fetch this asset first', 409)
        with _read_source(project['root'], item['path']) as handle:
            info = os.fstat(handle.fileno())
            with self._db() as db:
                row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if _signature(info) != (row['size'], row['mtime_ns'], row['ctime_ns'], row['device'], row['inode']):
                raise AssetLibraryError('Source changed; scan again', 409)
            yield handle

    @staticmethod
    def _remote_locations(db, asset_id, sha256):
        return [{'assetId': row['asset_id'], 'provider': row['provider'], 'fsId': row['fs_id'], 'path': row['path'], 'sha256': row['sha256'], 'bytes': row['bytes']} for row in db.execute('SELECT * FROM remote_locations WHERE asset_id=? AND sha256=? ORDER BY provider,fs_id', (asset_id, sha256))]

    def remote_locations(self, asset_id):
        item = self.asset(asset_id)
        with self._db() as db:
            return self._remote_locations(db, item['id'], item['sha256'])

    def query_profiles(self):
        with self._db() as db:
            return [{'space': row['space'], 'baseUrl': row['base_url'], 'model': row['model'], 'dimensions': row['dimensions'], 'inputVersion': row['input_version'], 'mode': row['mode']} for row in db.execute('SELECT * FROM query_profiles ORDER BY space')]

    def _cached_file(self, asset_row):
        with self._db() as db:
            saved = db.execute('SELECT * FROM local_files WHERE asset_id=? AND sha256=?', (asset_row['id'], asset_row['sha256'])).fetchone()
        if saved is None:
            return None
        try:
            with _read_source(self.root, saved['relative_path']) as handle:
                if _signature(os.fstat(handle.fileno())) != tuple(saved[key] for key in ('size', 'mtime_ns', 'ctime_ns', 'device', 'inode')):
                    raise OSError('cache changed')
            return self.root / saved['relative_path']
        except (OSError, AssetLibraryError):
            raise AssetLibraryError('Cached original changed; fetch this asset again', 409) from None

    def cache_file(self, asset_id, source_path):
        """Install one downloaded original after validating its exact declared content."""
        item = self.asset(asset_id)
        source_path = Path(source_path).absolute()
        directory = self.root / 'files'
        if directory.is_symlink():
            raise AssetLibraryError('Unsafe original cache directory', 409)
        directory.mkdir(exist_ok=True, mode=0o700)
        destination = directory / item['sha256']
        temporary = directory / ('.incoming-' + str(uuid.uuid4()))
        try:
            sha, count = hashlib.sha256(), 0
            with _read_source(source_path.parent, source_path.name) as source, temporary.open('xb') as target:
                os.chmod(temporary, 0o600)
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    count += len(block)
                    if count > item['bytes']:
                        raise AssetLibraryError('Downloaded original size does not match catalog', 409)
                    sha.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            if count != item['bytes'] or sha.hexdigest() != item['sha256']:
                raise AssetLibraryError('Downloaded original hash does not match catalog', 409)
            with self.lock, self._db() as db:
                current = db.execute('SELECT sha256 FROM assets WHERE id=?', (asset_id,)).fetchone()
                if current is None or current[0] != item['sha256']:
                    raise AssetLibraryError('Asset changed during download', 409)
                os.replace(temporary, destination)
                info = destination.stat()
                # Shared same-SHA cache paths receive the replacement file signature together.
                db.execute('UPDATE local_files SET size=?,mtime_ns=?,ctime_ns=?,device=?,inode=? WHERE relative_path=?', (*_signature(info), destination.relative_to(self.root).as_posix()))
                db.execute('INSERT OR REPLACE INTO local_files VALUES(?,?,?,?,?,?,?,?)', (asset_id, item['sha256'], destination.relative_to(self.root).as_posix(), *_signature(info)))
        finally:
            temporary.unlink(missing_ok=True)
        return self.asset(asset_id)

    def dependencies(self, asset_id, direction='out', depth=1):
        item = self.asset(asset_id)
        if direction not in ('out', 'in') or isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 8:
            raise AssetLibraryError('Dependency direction must be out/in and depth 1–8')
        edges, unresolved, nodes, pending = [], [], {asset_id: item}, deque([(asset_id, 0)])
        visited, resolution_counts = set(), {}
        with self._db() as db:
            while pending:
                current, distance = pending.popleft()
                if current in visited or distance >= depth:
                    continue
                visited.add(current)
                if direction == 'out':
                    rows = db.execute('''SELECT r.*,a.id target_id,a.path target_path FROM refs r
                        LEFT JOIN identifiers i ON i.project_id=? AND i.namespace=r.namespace AND i.value=r.value
                        LEFT JOIN assets a ON a.project_id=i.project_id AND a.path=i.target_path WHERE r.source_id=?''', (item['projectId'], current)).fetchall()
                else:
                    rows = db.execute('''SELECT DISTINCT r.*,? target_id FROM refs r JOIN assets s ON s.id=r.source_id
                        JOIN identifiers i ON i.project_id=s.project_id AND i.namespace=r.namespace AND i.value=r.value
                        JOIN assets a ON a.project_id=i.project_id AND a.path=i.target_path
                        WHERE a.id=?''', (current, current)).fetchall()
                for row in rows:
                    key = (row['namespace'], row['value'])
                    if key not in resolution_counts:
                        resolution_counts[key] = db.execute('SELECT COUNT(DISTINCT a.id) FROM identifiers i JOIN assets a ON a.project_id=i.project_id AND a.path=i.target_path WHERE i.project_id=? AND i.namespace=? AND i.value=?', (item['projectId'], *key)).fetchone()[0]
                    if not row['target_id'] or resolution_counts[key] > 1:
                        unresolved.append({'from': row['source_id'], 'namespace': row['namespace'], 'value': row['value'], 'evidence': row['evidence'], 'reason': 'ambiguous' if resolution_counts[key] > 1 else 'not-indexed-or-external'})
                        continue
                    edge = {'from': row['source_id'], 'to': row['target_id'], 'namespace': row['namespace'], 'value': row['value'], 'evidence': row['evidence']}
                    if edge not in edges:
                        edges.append(edge)
                    neighbor = row['target_id'] if direction == 'out' else row['source_id']
                    if neighbor not in nodes:
                        if len(nodes) >= 1000:
                            return {'nodes': list(nodes.values()), 'edges': edges, 'unresolved': unresolved, 'truncated': True}
                        node_row = db.execute('SELECT * FROM assets WHERE id=?', (neighbor,)).fetchone()
                        nodes[neighbor] = self._public_asset(db, node_row)
                        pending.append((neighbor, distance + 1))
        # Atlas + .meta aliases can produce the same resolution; retain one edge per evidence.
        unique_unresolved = list({_json(value): value for value in unresolved}.values())
        return {'nodes': list(nodes.values()), 'edges': edges, 'unresolved': unique_unresolved, 'truncated': False}

    def save_annotation(self, asset_id, data):
        item = self.asset(asset_id)
        allowed = {'label', 'categories', 'purpose', 'ocrText', 'evidence', 'confidence', 'source'}
        if not isinstance(data, dict) or set(data) - allowed or data.get('source') not in ('manual', 'ai'):
            raise AssetLibraryError('Annotation source must be manual/ai with supported fields')
        clean = {}
        for field, limit in (('label', 300), ('purpose', 4000), ('ocrText', 65536), ('evidence', 12000)):
            if field in data:
                if not isinstance(data[field], str) or len(data[field]) > limit:
                    raise AssetLibraryError('Invalid annotation ' + field)
                clean[field] = data[field]
        if 'categories' in data:
            if not isinstance(data['categories'], list) or len(data['categories']) > 50 or any(not isinstance(v, str) or len(v) > 100 for v in data['categories']):
                raise AssetLibraryError('Invalid annotation categories')
            clean['categories'] = list(dict.fromkeys(data['categories']))
        if 'confidence' in data:
            value = data['confidence']
            if not _finite_number(value) or not 0 <= value <= 1:
                raise AssetLibraryError('Annotation confidence must be 0–1')
            clean['confidence'] = value
        clean['sha256'] = item['sha256']
        with self.lock, self._db() as db:
            previous = db.execute('SELECT data FROM annotations WHERE asset_id=? AND source=?', (asset_id, data['source'])).fetchone()
            if previous is None or json.loads(previous['data']) != clean:
                self._invalidate_description_vectors(db, asset_id)
            db.execute('INSERT INTO annotations VALUES(?,?,?,?) ON CONFLICT(asset_id,source) DO UPDATE SET data=excluded.data,updated=excluded.updated', (asset_id, data['source'], _json(clean), time.time()))
            self._refresh_search(db, asset_id)
        return self.asset(asset_id)

    @staticmethod
    def _invalidate_description_vectors(db, asset_id):
        # These spaces are reserved for generated metadata/annotation embeddings.
        # Imported image/audio vectors do not depend on the edited descriptions.
        db.execute("DELETE FROM embeddings WHERE asset_id=? AND model LIKE 'asset-metadata-text-%'", (asset_id,))

    def similar(self, asset_id, limit=20, mode='visual', model=None, candidate_limit=10000):
        item = self.asset(asset_id)
        limit = _limit(limit, 100)
        candidate_limit = _limit(candidate_limit, 100000)
        if mode == 'exact':
            with self._db() as db:
                rows = db.execute('SELECT * FROM assets WHERE sha256=? AND id!=? ORDER BY path LIMIT ?', (item['sha256'], asset_id, limit)).fetchall()
                return {'items': [{**self._public_asset(db, row), 'score': 1.0} for row in rows], 'method': 'sha256-exact', 'approximate': False}
        if mode == 'semantic':
            model = _model(model)
            with self._db() as db:
                row = db.execute('SELECT * FROM embeddings WHERE asset_id=? AND model=? AND sha256=?', (asset_id, model, item['sha256'])).fetchone()
            if not row:
                raise AssetLibraryError('No current embedding for this asset and model', 409)
            vector = struct.unpack('<' + str(row['dimensions']) + 'f', row['vector'])
            return self.query_embedding(vector, model, limit=limit, candidate_limit=candidate_limit, exclude_id=asset_id)
        if mode != 'visual':
            raise AssetLibraryError('Similarity mode must be exact, visual or semantic')
        with self._db() as db:
            row = db.execute('SELECT dhash,color FROM assets WHERE id=?', (asset_id,)).fetchone()
            if not row['dhash'] or not row['color']:
                raise AssetLibraryError('No decoded visual features for this asset', 409)
            total = db.execute('SELECT COUNT(*) FROM assets WHERE dhash IS NOT NULL AND id!=?', (asset_id,)).fetchone()[0]
            candidates = db.execute('SELECT id,dhash,color FROM assets WHERE dhash IS NOT NULL AND id!=? ORDER BY (project_id=?) DESC,id LIMIT ?', (asset_id, item['projectId'], candidate_limit)).fetchall()
            target_hash = int(row['dhash'], 16)
            target_color = struct.unpack('<48f', row['color'])
            scored = []
            for candidate in candidates:
                shape = 1 - (target_hash ^ int(candidate['dhash'], 16)).bit_count() / 64
                color = sum(a * b for a, b in zip(target_color, struct.unpack('<48f', candidate['color'])))
                score = .65 * shape + .35 * max(0, min(1, color))
                scored.append((score, candidate['id']))
            scored.sort(reverse=True)
            items = []
            for score, identifier in scored[:limit]:
                asset_row = db.execute('SELECT * FROM assets WHERE id=?', (identifier,)).fetchone()
                items.append({**self._public_asset(db, asset_row), 'score': round(score, 6)})
        return {'items': items, 'method': 'visual-dhash64-rgbhist48-v1', 'semantic': False, 'approximate': len(candidates) < total,
            'candidatesCompared': len(candidates), 'totalCandidates': total, 'candidateSelection': 'same project first; stable asset ID; exhaustive only when all candidates fit'}

    def import_embeddings(self, project_id, model, items):
        with self.lock:
            self.project(project_id)
            model = _model(model)
            if not isinstance(items, list) or not 1 <= len(items) <= 1000:
                raise AssetLibraryError('Import 1–1000 embeddings per request')
            validated, dimensions, seen = [], None, set()
            with self._db() as db:
                existing_dims = {r[0] for r in db.execute('SELECT DISTINCT dimensions FROM embeddings WHERE model=?', (model,))}
            for entry in items:
                if not isinstance(entry, dict) or set(entry) != {'assetId', 'vector'}:
                    raise AssetLibraryError('Each embedding needs assetId and vector')
                item = self.asset(entry['assetId'])
                if item['projectId'] != project_id or item['id'] in seen:
                    raise AssetLibraryError('Embedding assets must be unique and belong to this project')
                vector = _vector(entry['vector'])
                if dimensions is None:
                    dimensions = len(vector)
                if len(vector) != dimensions or existing_dims and existing_dims != {dimensions}:
                    raise AssetLibraryError('Same model requires identical vector dimensions')
                seen.add(item['id'])
                validated.append((item['id'], model, dimensions, struct.pack('<' + str(dimensions) + 'f', *vector), item['sha256']))
            with self.lock, self._db() as db:
                db.executemany('INSERT INTO embeddings VALUES(?,?,?,?,?) ON CONFLICT(asset_id,model) DO UPDATE SET dimensions=excluded.dimensions,vector=excluded.vector,sha256=excluded.sha256', validated)
            return {'count': len(validated), 'model': model, 'dimensions': dimensions, 'normalized': True, 'generatedLocally': False}

    def query_embedding(self, vector, model, project_id=None, limit=20, candidate_limit=10000, exclude_id=None, kind=None):
        vector, model = _vector(vector), _model(model)
        limit, candidate_limit = _limit(limit, 1000), _limit(candidate_limit, 100000)
        clauses, args = ['e.model=?', 'e.dimensions=?', 'e.sha256=a.sha256'], [model, len(vector)]
        if project_id is not None:
            clauses.append('a.project_id=?'); args.append(_id(project_id))
        if exclude_id:
            clauses.append('a.id!=?'); args.append(_id(exclude_id))
        if kind is not None:
            if kind not in KINDS:
                raise AssetLibraryError('Unknown asset kind')
            clauses.append('a.kind=?'); args.append(kind)
        where = ' AND '.join(clauses)
        with self._db() as db:
            total = db.execute('SELECT COUNT(*) FROM embeddings e JOIN assets a ON a.id=e.asset_id WHERE ' + where, args).fetchone()[0]
            rows = db.execute('SELECT e.asset_id,e.vector FROM embeddings e JOIN assets a ON a.id=e.asset_id WHERE ' + where + ' ORDER BY e.asset_id LIMIT ?', [*args, candidate_limit])
            try:
                import numpy as np
                target = np.asarray(vector, dtype='<f4')
            except ImportError:
                np, target = None, None
            scores, compared = [], 0
            for row in rows:
                compared += 1
                if np is not None:
                    score = float(np.dot(target, np.frombuffer(row['vector'], dtype='<f4')))
                else:
                    score = sum(a * b for a, b in zip(vector, struct.unpack('<' + str(len(vector)) + 'f', row['vector'])))
                candidate = (score, row['asset_id'])
                if len(scores) < limit:
                    heapq.heappush(scores, candidate)
                elif candidate > scores[0]:
                    heapq.heapreplace(scores, candidate)
            scores.sort(reverse=True)
            items = [{**self._public_asset(db, db.execute('SELECT * FROM assets WHERE id=?', (identifier,)).fetchone()), 'score': round(max(-1, min(1, score)), 6)} for score, identifier in scores]
        return {'items': items, 'method': 'cosine-imported-semantic', 'model': model, 'dimensions': len(vector), 'approximate': compared < total,
            'candidatesCompared': compared, 'totalCandidates': total, 'accelerator': 'numpy' if np is not None else 'python',
            'candidateSelection': 'stable asset ID; exhaustive only when all candidates fit; streamed top-K memory'}

    def analyze_ocr(self, asset_id, language='eng', timeout=30):
        item = self.asset(asset_id)
        if item['kind'] != 'image' or item.get('image', {}).get('status') != 'analyzed':
            raise AssetLibraryError('OCR requires a decoded image within the analysis pixel limit')
        if not isinstance(language, str) or not re.fullmatch(r'[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*', language) or len(language) > 100:
            raise AssetLibraryError('Invalid OCR language')
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
            raise AssetLibraryError('OCR timeout must be 1–120 seconds')
        if sys.platform == 'darwin' and shutil.which('swiftc'):
            return self._analyze_apple_ocr(item, timeout)
        engine = shutil.which('tesseract')
        if not engine:
            return {'status': 'unavailable', 'provider': 'tesseract', 'text': None, 'reason': 'not-installed'}
        with tempfile.TemporaryDirectory(prefix='ocr-', dir=self.root) as directory:
            input_path = Path(directory) / ('source' + item['extension'])
            with self.open_file(asset_id) as source, input_path.open('wb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            output = Path(directory) / 'result'
            try:
                process = subprocess.run([engine, str(input_path), str(output), '-l', language], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
            except subprocess.TimeoutExpired:
                return {'status': 'not-analyzed', 'provider': 'tesseract', 'text': None, 'reason': 'timeout'}
            text_file = output.with_suffix('.txt')
            if process.returncode or not text_file.is_file():
                return {'status': 'not-analyzed', 'provider': 'tesseract', 'text': None, 'reason': 'engine-or-language-unavailable'}
            if text_file.stat().st_size > 8 * 1024 ** 2:
                return {'status': 'not-analyzed', 'provider': 'tesseract', 'text': None, 'reason': 'output-byte-limit'}
            text = text_file.read_text('utf-8')
        result = {'status': 'analyzed', 'provider': 'tesseract', 'language': language, 'text': text, 'hasText': bool(text.strip()), 'sha256': item['sha256']}
        with self.lock, self._db() as db:
            row = db.execute('SELECT sha256,metadata FROM assets WHERE id=?', (asset_id,)).fetchone()
            if row['sha256'] != item['sha256']:
                raise AssetLibraryError('Source was reindexed during OCR; retry', 409)
            metadata = json.loads(row['metadata'])
            previous = metadata.get('ocr', {})
            if (previous.get('status'), previous.get('text')) != (result.get('status'), result.get('text')):
                self._invalidate_description_vectors(db, asset_id)
            metadata['ocr'] = result
            db.execute('UPDATE assets SET metadata=? WHERE id=?', (_json(metadata), asset_id))
            self._refresh_search(db, asset_id)
        return result

    def _analyze_apple_ocr(self, item, timeout):
        source = Path(__file__).with_name('apple_ocr.swift')
        if not source.is_file():
            return {'status': 'unavailable', 'provider': 'apple-vision', 'text': None, 'reason': 'helper-missing'}
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:20]
        binary = self.root / ('apple-ocr-' + digest)
        with self.lock:
            if not binary.is_file():
                temporary = self.root / ('ocr-build-' + uuid.uuid4().hex)
                try:
                    compiled = subprocess.run([shutil.which('swiftc'), str(source), '-O', '-o', str(temporary)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
                    if compiled.returncode:
                        return {'status': 'unavailable', 'provider': 'apple-vision', 'text': None, 'reason': 'compile-failed'}
                    os.chmod(temporary, 0o700)
                    os.replace(temporary, binary)
                except (OSError, subprocess.TimeoutExpired):
                    return {'status': 'unavailable', 'provider': 'apple-vision', 'text': None, 'reason': 'compiler-unavailable'}
                finally:
                    temporary.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix='vision-', dir=self.root) as directory:
            image = Path(directory) / ('source' + item['extension'])
            with self.open_file(item['id']) as input_file, image.open('wb') as output:
                shutil.copyfileobj(input_file, output, 1024 * 1024)
            try:
                process = subprocess.run([str(binary), str(image)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
                if process.returncode or len(process.stdout) > 2 * 1024 * 1024:
                    return {'status': 'not-analyzed', 'provider': 'apple-vision', 'text': None, 'reason': 'recognition-failed'}
                result = json.loads(process.stdout)
                if result.get('status') != 'analyzed' or not isinstance(result.get('text'), str) or not isinstance(result.get('lines'), list):
                    raise ValueError('Invalid local OCR result')
            except subprocess.TimeoutExpired:
                return {'status': 'not-analyzed', 'provider': 'apple-vision', 'text': None, 'reason': 'timeout'}
            except (OSError, ValueError):
                return {'status': 'not-analyzed', 'provider': 'apple-vision', 'text': None, 'reason': 'invalid-result'}
        result.update(hasText=bool(result['text'].strip()), sha256=item['sha256'])
        with self.lock, self._db() as db:
            row = db.execute('SELECT sha256, metadata FROM assets WHERE id=?', (item['id'],)).fetchone()
            if not row or row['sha256'] != item['sha256']:
                raise AssetLibraryError('Source was reindexed during OCR; retry', 409)
            metadata = json.loads(row['metadata'])
            previous = metadata.get('ocr', {})
            if (previous.get('status'), previous.get('text')) != (result.get('status'), result.get('text')):
                self._invalidate_description_vectors(db, item['id'])
            metadata['ocr'] = result
            db.execute('UPDATE assets SET metadata=? WHERE id=?', (_json(metadata), item['id']))
            self._refresh_search(db, item['id'])
        return result

    def export_manifest(self, project_id):
        project = self.project(project_id)
        with self._db() as db:
            rows = db.execute('SELECT * FROM assets WHERE project_id=? ORDER BY path', (project_id,)).fetchall()
            items = [self._public_asset(db, row) for row in rows]
            references = [dict(row) for row in db.execute('SELECT r.source_id,r.namespace,r.value,r.evidence FROM refs r JOIN assets a ON a.id=r.source_id WHERE a.project_id=? ORDER BY r.source_id,r.namespace,r.value', (project_id,))]
        return {'format': 'devhelper-asset-library-v1', 'exportedAt': time.time(), 'project': project,
            'assets': items, 'references': references, 'notes': ['Relative source paths only; no embedded file bytes or vectors.', 'Classification and usage hypotheses are distinct from manual/AI annotations.', 'Unresolved dependencies are not inferred from a similar filename.']}


def _limit(value, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise AssetLibraryError('Invalid result or candidate limit')
    return value


def _offset(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000_000:
        raise AssetLibraryError('Invalid offset')
    return value


def _model(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise AssetLibraryError('Use an explicit embedding model and version identifier')
    return value.strip()


def _finite_number(value):
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


def _vector(value):
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 4096 or any(not _finite_number(v) for v in value):
        raise AssetLibraryError('Vector needs 1–4096 finite numeric dimensions')
    # Rescale first: finite 1e308 inputs must not overflow the normalization sum.
    maximum = max(abs(v) for v in value)
    if maximum == 0:
        raise AssetLibraryError('Zero vectors cannot be normalized')
    scaled = [v / maximum for v in value]
    norm = math.sqrt(sum(v * v for v in scaled))
    return [v / norm for v in scaled]
