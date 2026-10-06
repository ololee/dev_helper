"""Materialize explicitly enabled private Skills into owned Codex skill folders."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import yaml

MEDIA = re.compile(r"/api/knowledge/attachments/([a-f0-9-]{36})/(?:content|preview)")
MARKER = ".devhelper-managed.json"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            result.update(chunk)
    return result.hexdigest()


def unchanged(directory, marker):
    if not isinstance(marker.get("files"), dict) or "SKILL.md" not in marker["files"]:
        return False
    for name, expected in marker.get("files", {}).items():
        if name != "SKILL.md" and not re.fullmatch(r"assets/[a-f0-9-]{36}\.[a-zA-Z0-9]+", name):
            return False
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            return False
    return True


def materialize(store, skills_root: Path | None = None):
    root = skills_root or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills"
    root.mkdir(parents=True, exist_ok=True)
    with store.lock:
        documents = [store.read_document(row["id"]) for row in store._document_rows()
                     if row["kind"] == "skill" and row["enabled"] and row["autoLoad"]]
    wanted = set()
    installed, removed, errors = 0, 0, []
    for document in documents:
        name = "devhelper-private-" + document["id"].replace("-", "")
        wanted.add(name)
        directory = root / name
        try:
            previous = None
            if directory.exists() or directory.is_symlink():
                if directory.is_symlink() or not (directory / MARKER).is_file() or (directory / MARKER).is_symlink():
                    raise RuntimeError("存在同名非托管目录，未覆盖")
                previous = json.loads((directory / MARKER).read_text())
                if previous.get("documentId") != document["id"] or not unchanged(directory, previous):
                    raise RuntimeError("本机 Skill 文件已手动修改，未覆盖；请在资料管理中合并修改")
                if (directory / "assets").is_symlink():
                    raise RuntimeError("附件目录是符号链接，未写入")
            body, metadata = document["content"], {}
            if body.startswith("---\n"):
                end = body.find("\n---", 4)
                if end >= 0:
                    parsed = yaml.safe_load(body[4:end])
                    if not isinstance(parsed, dict):
                        raise RuntimeError("Skill 元数据不是对象")
                    metadata = parsed
                    body = body[end + 4:].lstrip("\n")
            metadata["name"] = name
            if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
                metadata["description"] = "手机同步的私有 Skill：" + document["title"]
            with tempfile.TemporaryDirectory(prefix=".devhelper-skill-", dir=root) as temporary:
                staged = Path(temporary)
                for attachment in sorted(set(MEDIA.findall(body))):
                    _, original = store._attachment(attachment)
                    relative = "assets/" + attachment + original.suffix
                    (staged / "assets").mkdir(exist_ok=True)
                    shutil.copyfile(original, staged / relative)
                    body = re.sub(r"/api/knowledge/attachments/" + re.escape(attachment) + r"/(?:content|preview)", relative, body)
                text = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n\n" + body
                (staged / "SKILL.md").write_text(text, encoding="utf-8")
                files = {str(path.relative_to(staged)): digest(path) for path in staged.rglob("*") if path.is_file()}
                directory.mkdir(exist_ok=True)
                for relative in files:
                    destination = directory / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged / relative, destination)
                if previous:
                    for relative in set(previous.get("files", {})) - set(files):
                        (directory / relative).unlink(missing_ok=True)
                marker = {"documentId": document["id"], "files": files, "revision": document["revision"]}
                pending = directory / (MARKER + ".tmp")
                pending.write_text(json.dumps(marker), encoding="utf-8")
                pending.replace(directory / MARKER)
                installed += 1
        except Exception as error:
            errors.append(dict(id=document["id"], title=document["title"], error=str(error)[:250]))
    for directory in root.glob("devhelper-private-*"):
        if directory.name in wanted or directory.is_symlink() or not (directory / MARKER).is_file():
            continue
        try:
            marker = json.loads((directory / MARKER).read_text())
            # Only folders created here and containing no user additions are removable.
            actual = {str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file()}
            if unchanged(directory, marker) and actual == set(marker.get("files", {})) | {MARKER}:
                shutil.rmtree(directory)
                removed += 1
            else:
                errors.append(dict(id=marker.get("documentId"), error="已删除或禁用的 Skill 在本机有修改，已保留"))
        except (OSError, ValueError):
            continue
    return dict(installedSkills=installed, removedSkills=removed, skillsRoot=str(root), errors=errors)
