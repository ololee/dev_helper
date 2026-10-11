"""Persistent, bounded Spine bundles imported only from selected ZIP attachments.

The bundle manifest is the file-serving allowlist. Archives never become public
directories, and executable/HTML entries are never extracted or served.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import threading
import unicodedata
import uuid
import zipfile

if __package__:
    from .knowledge import KnowledgeError, Response, _identifier, _instant
else:
    from knowledge import KnowledgeError, Response, _identifier, _instant


MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ATLAS_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
SUPPORTED = frozenset(("3.8", "4.0", "4.1", "4.2", "4.3"))
VERSION = re.compile(r"^[0-9]+\.[0-9]+(?:\.[A-Za-z0-9_.-]+)?$")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def relative_path(value: str, directory: bool = False) -> str:
    """Validate an unescaped POSIX archive/HTTP path without normalizing it."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise KnowledgeError("ZIP 资源路径无效。")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value or ":" in value:
        raise KnowledgeError("ZIP 资源路径含不安全字符。")
    path = value[:-1] if directory and value.endswith("/") else value
    pieces = path.split("/")
    if len(pieces) > 64 or any(piece in ("", ".", "..") for piece in pieces):
        raise KnowledgeError("ZIP 资源路径必须位于资源包内。")
    return path


def _kind(name: str) -> str:
    name = name.lower()
    if name.endswith((".json", ".json.txt")):
        return "json"
    if name.endswith((".skel", ".skel.bytes")):
        return "skel"
    if name.endswith((".atlas", ".atlas.txt")):
        return "atlas"
    if PurePosixPath(name).suffix in IMAGE_TYPES:
        return "image"
    return ""


def _archive_limits(source: Path) -> None:
    """Bound central-directory allocation before ZipFile creates ZipInfo objects."""
    with source.open("rb") as stream:
        size = source.stat().st_size
        if size > MAX_ZIP_BYTES:
            raise KnowledgeError("ZIP 超过 256 MiB。", 413)
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise KnowledgeError("ZIP 缺少有效的目录记录。")
    disk, central_disk, on_disk, total, central_size, central_offset, comment = struct.unpack_from("<4H2IH", tail, offset + 4)
    if offset + 22 + comment != len(tail) or disk or central_disk or on_disk != total:
        raise KnowledgeError("ZIP 目录记录无效或包含分卷。")
    if total == 65535 or central_size == 0xffffffff or central_offset == 0xffffffff:
        raise KnowledgeError("ZIP64 资源包不在支持范围内。", 413)
    # One entry's fixed header is 46 bytes. UTF-8 paths/comments/extra fields
    # may vary; a 32 MiB central directory is already ample for 10000 resources.
    if total > MAX_ENTRIES or central_size > 32 * 1024 * 1024:
        raise KnowledgeError("ZIP 条目或目录记录超出限制。", 413)
    if central_size < total * 46 or central_offset + central_size > size - len(tail) + offset:
        raise KnowledgeError("ZIP 目录记录越界。")
    # EOCD counts are untrusted too. Walk the fixed-size headers before Python
    # allocates objects; a forged low count cannot hide thousands of entries.
    with source.open("rb") as stream:
        stream.seek(size - len(tail) + offset - central_size)
        remaining, counted = central_size, 0
        while remaining:
            header = stream.read(46)
            if remaining < 46 or len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise KnowledgeError("ZIP 目录条目无效。")
            name_length, extra_length, comment_length = struct.unpack_from("<3H", header, 28)
            variable = name_length + extra_length + comment_length
            if variable > remaining - 46:
                raise KnowledgeError("ZIP 目录条目越界。")
            stream.seek(variable, 1)
            remaining -= 46 + variable
            counted += 1
            if counted > MAX_ENTRIES:
                raise KnowledgeError("ZIP 条目超过 10000 个。", 413)
        if counted != total:
            raise KnowledgeError("ZIP 目录条目数量不匹配。")


def _stem(name: str) -> str:
    for suffix in (".skel.bytes", ".json.txt", ".atlas.txt", ".json", ".skel", ".atlas"):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return PurePosixPath(name).stem


def _binary_string(header: bytes, offset: int) -> tuple[str, int] | None:
    length = 0
    for index in range(5):
        if offset >= len(header):
            return None
        byte = header[offset]
        offset += 1
        length |= (byte & 0x7f) << (index * 7)
        if not byte & 0x80:
            if length <= 1:
                return "", offset
            length -= 1
            if length > 128 or offset + length > len(header):
                return None
            try:
                return header[offset:offset + length].decode("utf-8"), offset + length
            except UnicodeDecodeError:
                return None
    return None


def binary_version(header: bytes) -> str:
    """3.x has a string hash; 4.x+ has an eight-byte hash before version."""
    candidate = _binary_string(header, 8)
    if candidate and VERSION.fullmatch(candidate[0]) and int(candidate[0].split(".")[0]) >= 4:
        return candidate[0]
    hash_value = _binary_string(header, 0)
    candidate = _binary_string(header, hash_value[1]) if hash_value else None
    if candidate and VERSION.fullmatch(candidate[0]) and int(candidate[0].split(".")[0]) <= 3:
        return candidate[0]
    return ""


def _json_version(path: Path) -> str | None:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise KnowledgeError("JSON 骨架超过 16 MiB。", 413)
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            value = json.load(source)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("skeleton"), dict):
        return None
    bones = value.get("bones")
    if not isinstance(bones, list) or not bones or len(bones) > 100000:
        return None
    names = set()
    root = False
    for bone in bones:
        if not isinstance(bone, dict) or not isinstance(bone.get("name"), str) or not bone["name"] or bone["name"] in names:
            return None
        names.add(bone["name"])
        root |= not bone.get("parent")
    if not root or not isinstance(value["skeleton"].get("spine"), str):
        return None
    if "animations" in value and not isinstance(value["animations"], dict):
        return None
    if "slots" in value and not isinstance(value["slots"], list):
        return None
    if "skins" in value and not isinstance(value["skins"], (list, dict)):
        return None
    version = value["skeleton"]["spine"]
    return version if len(version) <= 32 and VERSION.fullmatch(version) else ""


def _image_mime(path: Path) -> str | None:
    with path.open("rb") as source:
        header = source.read(32)
    extension = path.suffix.lower()
    valid = ((extension == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR")
             or (extension in (".jpg", ".jpeg") and header.startswith(b"\xff\xd8\xff"))
             or (extension == ".webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP")
             or (extension == ".gif" and header[:6] in (b"GIF87a", b"GIF89a"))
             or (extension == ".bmp" and header[:2] == b"BM"))
    return IMAGE_TYPES[extension] if valid else None


def _atlas_pages(path: Path, relative: str) -> list[str] | None:
    if path.stat().st_size > MAX_ATLAS_BYTES:
        raise KnowledgeError("Spine 图集超过 4 MiB。", 413)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        return None
    if "\x00" in text:
        return None
    pages = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        # Modern TextureAtlas readers allow global metadata before the first
        # texture page, whether or not the metadata has a following blank line.
        if not pages:
            metadata_end = 0
            while metadata_end < len(lines) and ":" in lines[metadata_end]:
                metadata_end += 1
            lines = lines[metadata_end:]
            if not lines:
                continue
        if len(lines) < 2 or ":" in lines[0] or not re.match(r"^(size|format|filter|repeat|pma|scale)\s*:", lines[1]):
            return None
        # Resolve atlas-relative images while allowing ordinary ../images paths
        # only when they remain within the imported bundle.
        page = lines[0]
        if "\\" in page or ":" in page or page.startswith("/") or any(ord(c) < 32 for c in page):
            return None
        pieces = list(PurePosixPath(relative).parent.parts)
        if pieces == ["."]:
            pieces = []
        for piece in page.split("/"):
            if piece == "..":
                if not pieces:
                    return None
                pieces.pop()
            elif piece not in ("", "."):
                pieces.append(piece)
        try:
            resolved = relative_path("/".join(pieces))
        except KnowledgeError:
            return None
        if _kind(resolved) != "image":
            return None
        pages.append(resolved)
    return pages or None


def _choose_atlas(skeleton: str, atlases: list[str]) -> str:
    parent = str(PurePosixPath(skeleton).parent)
    local = [atlas for atlas in atlases if str(PurePosixPath(atlas).parent) == parent]
    base = _stem(PurePosixPath(skeleton).name).casefold()
    stems = [base]
    shorter = base
    while True:
        match = re.search(r"-(pro|ess|pma|atlas)$", shorter)
        if not match:
            break
        shorter = shorter[:match.start()]
        stems.append(shorter)
    for scope in (local, atlases):
        for candidate in stems:
            for suffix in ("", "-pma", "-ess", "-atlas", "-pro"):
                matches = [path for path in scope if _stem(PurePosixPath(path).name).casefold() == candidate + suffix]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    return ""
        if len(scope) == 1:
            return scope[0]
    return ""


class SpineStore:
    def __init__(self, knowledge):
        self.knowledge = knowledge
        self.root = knowledge.root / "spine"
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()

    def _manifest(self, identifier: str) -> dict:
        directory = self.root / _identifier(identifier)
        manifest = directory / "manifest.json"
        if directory.is_symlink() or manifest.is_symlink() or not manifest.is_file():
            raise KnowledgeError("Spine 资源包不存在。", 404)
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise KnowledgeError("Spine 资源包清单已损坏。", 409)
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise KnowledgeError("Spine 资源包清单已损坏。", 409) from None
        if (not isinstance(value, dict) or value.get("id") != identifier or not isinstance(value.get("files"), dict)
                or not isinstance(value.get("skeletons"), list) or not isinstance(value.get("name"), str)
                or not isinstance(value.get("createdAt"), str)):
            raise KnowledgeError("Spine 资源包清单已损坏。", 409)
        return value

    @staticmethod
    def _public(manifest: dict) -> dict:
        return {key: manifest[key] for key in ("id", "name", "skeletons", "createdAt")}

    def bundles(self) -> dict:
        with self.lock:
            bundles = []
            for directory in self.root.iterdir():
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                try:
                    bundles.append(self._public(self._manifest(directory.name)))
                except KnowledgeError:
                    continue
            bundles.sort(key=lambda bundle: (bundle["createdAt"], bundle["id"]), reverse=True)
            return {"bundles": bundles}

    def bundle(self, identifier: str) -> dict:
        with self.lock:
            return self._public(self._manifest(identifier))

    def import_bundle(self, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) - {"attachmentId", "name"}:
            raise KnowledgeError("请选择一个已上传的 ZIP 附件。")
        identifier = _identifier(data.get("attachmentId"))
        name = data.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 160):
            raise KnowledgeError("资源包名称须为 1 至 160 个字符。")
        with self.knowledge.lock:
            attachment, source = self.knowledge._attachment(identifier)
            if attachment["mimeType"] != "application/zip" or attachment["bytes"] > MAX_ZIP_BYTES:
                raise KnowledgeError("请选择不超过 256 MiB 的 ZIP 附件。", 413 if attachment["bytes"] > MAX_ZIP_BYTES else 400)
            self.knowledge._media_pins[identifier] = self.knowledge._media_pins.get(identifier, 0) + 1
        try:
            with self.lock:
                return self._import_zip(source, name.strip() if name else attachment["name"])
        finally:
            with self.knowledge.lock:
                count = self.knowledge._media_pins.get(identifier, 1) - 1
                if count:
                    self.knowledge._media_pins[identifier] = count
                else:
                    self.knowledge._media_pins.pop(identifier, None)

    def _import_zip(self, source: Path, name: str) -> dict:
        identifier = str(uuid.uuid4())
        pending = self.root / (".pending-" + identifier)
        pending.mkdir()
        files_root = pending / "files"
        files_root.mkdir()
        published = False
        try:
            extracted = []
            _archive_limits(source)
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_ENTRIES:
                    raise KnowledgeError("ZIP 条目超过 10000 个。", 413)
                seen, file_keys, all_paths, aliases, total = set(), set(), [], {}, 0
                for entry in entries:
                    path = relative_path(entry.orig_filename, entry.is_dir())
                    key = unicodedata.normalize("NFC", path).casefold()
                    if key in seen:
                        raise KnowledgeError("ZIP 含重复或规范化后冲突的资源路径。")
                    seen.add(key)
                    raw_pieces, key_pieces = path.split("/"), key.split("/")
                    for depth in range(1, len(raw_pieces) + 1):
                        raw_prefix = "/".join(raw_pieces[:depth])
                        key_prefix = "/".join(key_pieces[:depth])
                        if key_prefix in aliases and aliases[key_prefix] != raw_prefix:
                            raise KnowledgeError("ZIP 含规范化后冲突的资源路径。")
                        aliases[key_prefix] = raw_prefix
                    all_paths.append((entry, path, key))
                    mode = entry.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1:
                        raise KnowledgeError("ZIP 不允许符号链接、特殊文件或加密条目。")
                    if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise KnowledgeError("ZIP 仅支持普通 STORE 或 DEFLATE 压缩。")
                    if entry.is_dir() != (file_type == stat.S_IFDIR) and file_type:
                        raise KnowledgeError("ZIP 条目类型与路径不一致。")
                    if not entry.is_dir():
                        file_keys.add(key)
                    if entry.file_size > MAX_MEMBER_BYTES:
                        raise KnowledgeError("ZIP 单个资源超过 128 MiB。", 413)
                    if entry.file_size > max(1024 * 1024, entry.compress_size * 1000):
                        raise KnowledgeError("ZIP 压缩比过高。", 413)
                    total += entry.file_size
                    if total > MAX_EXPANDED_BYTES:
                        raise KnowledgeError("ZIP 展开大小超过 512 MiB。", 413)
                for _, _, key in all_paths:
                    if any("/".join(key.split("/")[:i]) in file_keys for i in range(1, len(key.split("/")))):
                        raise KnowledgeError("ZIP 文件与目录路径冲突。")
                if total + 16 * 1024 * 1024 > shutil.disk_usage(self.root).free:
                    raise KnowledgeError("本机空间不足，无法导入资源包。", 507)
                actual = 0
                for entry, relative, _ in all_paths:
                    if entry.is_dir() or not _kind(relative):
                        continue
                    destination = files_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    size = 0
                    with archive.open(entry, "r") as resource, destination.open("xb") as output:
                        while chunk := resource.read(65536):
                            size += len(chunk)
                            actual += len(chunk)
                            if size > MAX_MEMBER_BYTES or actual > MAX_EXPANDED_BYTES or size > entry.file_size:
                                raise KnowledgeError("ZIP 实际展开大小超出限制。", 413)
                            output.write(chunk)
                    if size != entry.file_size:
                        raise KnowledgeError("ZIP 资源长度不匹配。")
                    extracted.append(relative)
            files, skeletons, atlases, pages = {}, [], [], {}
            for relative in sorted(extracted, key=str.casefold):
                path, kind = files_root / relative, _kind(relative)
                if kind == "image":
                    mime = _image_mime(path)
                    if mime:
                        files[relative] = mime
                elif kind == "atlas":
                    texture_pages = _atlas_pages(path, relative)
                    if texture_pages:
                        atlases.append(relative)
                        if len(atlases) > 256:
                            raise KnowledgeError("每个 ZIP 最多支持 256 个图集。", 413)
                        pages[relative] = texture_pages
                        files[relative] = "text/plain; charset=utf-8"
                else:
                    if kind == "json":
                        version = _json_version(path)
                    else:
                        with path.open("rb") as content:
                            version = binary_version(content.read(512)) or None
                    if version is None:
                        continue
                    runtime = ".".join(version.split(".")[:2]) if version else ""
                    supported = runtime in SUPPORTED
                    skeletons.append({"name": _stem(PurePosixPath(relative).name), "skeleton": relative,
                                      "atlas": "", "atlasName": "", "version": version,
                                      "runtime": runtime if supported else "", "format": kind,
                                      "supported": supported, "issue": ""})
                    if len(skeletons) > 512:
                        raise KnowledgeError("每个 ZIP 最多支持 512 个骨架。", 413)
                    files[relative] = "application/json; charset=utf-8" if kind == "json" else "application/octet-stream"
            if not skeletons:
                raise KnowledgeError("ZIP 中没有有效的 Spine JSON 或二进制骨架。")
            atlas_candidates = [{"atlas": candidate, "atlasName": PurePosixPath(candidate).name} for candidate in atlases]
            for skeleton in skeletons:
                atlas = _choose_atlas(skeleton["skeleton"], atlases)
                skeleton["atlas"] = atlas
                skeleton["atlasName"] = PurePosixPath(atlas).name if atlas else ""
                skeleton["atlasCandidates"] = atlas_candidates
                issues = []
                if not skeleton["supported"]:
                    issues.append("不支持 Spine 导出版本 " + skeleton["version"] if skeleton["version"] else "无法识别 Spine 导出版本")
                if not atlas:
                    issues.append("图集无法确定，请手动选择" if atlases else "未找到有效的 Spine 图集")
                elif missing := [page for page in pages[atlas] if page not in files]:
                    skeleton["missingTextures"] = missing
                    issues.append("图集缺少纹理：" + "、".join(missing[:4]))
                skeleton["issue"] = "；".join(issues)
            # Even valid filenames that are unrelated JSON or disguised textures
            # cannot be served; keep only files present in the validated manifest.
            for relative in extracted:
                if relative not in files:
                    (files_root / relative).unlink()
            manifest = {"id": identifier, "name": name, "createdAt": _instant(), "skeletons": skeletons, "files": files}
            with (pending / "manifest.json").open("xb") as output:
                manifest_bytes = 0
                for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(manifest):
                    encoded = chunk.encode("utf-8")
                    manifest_bytes += len(encoded)
                    if manifest_bytes > MAX_MANIFEST_BYTES:
                        raise KnowledgeError("资源包清单超过 8 MiB，请分包导入。", 413)
                    output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, self.root / identifier)
            published = True
            return self._public(manifest)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, UnicodeError, EOFError) as error:
            raise KnowledgeError("ZIP 无法解包：" + str(error)[:160]) from None
        finally:
            if not published:
                shutil.rmtree(pending, ignore_errors=True)

    def delete(self, identifier: str) -> dict:
        with self.lock:
            self._manifest(identifier)
            shutil.rmtree(self.root / identifier)
            return {"id": identifier, "deleted": True}

    def file(self, identifier: str, relative: str) -> Response:
        relative = relative_path(relative)
        with self.lock:
            manifest = self._manifest(identifier)
            mime = manifest["files"].get(relative)
            if not mime or not _kind(relative):
                raise KnowledgeError("Spine 资源不存在。", 404)
            base = self.root / identifier / "files"
            file = base / relative
            if base.is_symlink() or any(parent.is_symlink() for parent in (file, *file.parents) if parent == base or base in parent.parents):
                raise KnowledgeError("Spine 资源路径无效。", 403)
            try:
                resolved = file.resolve(strict=True)
            except (OSError, RuntimeError):
                raise KnowledgeError("Spine 资源不存在。", 404) from None
            if base.resolve() not in resolved.parents or not resolved.is_file():
                raise KnowledgeError("Spine 资源路径无效。", 403)
            return Response(file=resolved, mime=mime, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"})

    def handle(self, method: str, route: str, data: dict | None = None) -> Response:
        try:
            if route == "bundles" and method in ("GET", "HEAD"):
                return Response(data=self.bundles())
            if route == "import" and method == "POST":
                return Response(data=self.import_bundle(data))
            match = re.fullmatch(r"bundles/([^/]+)(?:/files/(.+))?", route)
            if match:
                identifier, relative = match.groups()
                if relative and method in ("GET", "HEAD"):
                    return self.file(identifier, relative)
                if not relative and method in ("GET", "HEAD"):
                    return Response(data=self.bundle(identifier))
                if not relative and method == "DELETE":
                    return Response(data=self.delete(identifier))
                return Response(status=405, data={"error": "Method not allowed"})
            return Response(status=404, data={"error": "Spine endpoint not found"})
        except KnowledgeError as error:
            return Response(status=error.status, data={"error": str(error)})
        except OSError:
            return Response(status=503, data={"error": "Spine 资源存储暂不可用。"})
