"""Private LAN knowledge replication with semantic CAS and durable three-way bases."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from knowledge import KnowledgeError, document_sync_hash

FIELDS = ("kind", "title", "content", "tags", "enabled", "autoLoad")
ATTACHMENT = re.compile(r"/api/knowledge/attachments/([a-fA-F0-9-]{36})/(?:content|preview)")


class SyncConflict(RuntimeError):
    pass


class KnowledgeSync:
    def __init__(self, desktop, state_path: Path):
        self.desktop = desktop
        self.store = desktop.knowledge
        self.path = state_path
        self.lock = asyncio.Lock()
        self.task = None
        self.state = {"peers": {}, "initialized": False, "lastSyncAt": None, "conflicts": [], "summary": {}, "error": None}
        if state_path.is_file():
            self.state.update(json.loads(state_path.read_text(encoding="utf-8")))
        self.running = False
        self.phone_online = False

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".tmp")
        with pending.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(self.path)

    def view(self):
        return {key: value for key, value in self.state.items() if key != "peers"} | dict(
            autoSync=self.desktop.preferences.get().get("autoKnowledgeSync", False), running=self.running,
            phoneOnline=self.phone_online, phoneAddress=self.desktop.android_url)

    async def remote(self, method, path, data=None):
        origin = await self.desktop.find_android()
        self.phone_online = bool(origin)
        if not origin:
            raise RuntimeError("手机未连接，私有资料保留在当前设备；请开启手机 DevHelper。")
        response = await self.desktop.http.request(method, origin + "/api/knowledge/" + path, json=data, timeout=30)
        if response.status_code == 404 and path == "sync/manifest":
            raise RuntimeError("手机需要更新到支持资料同步的 DevHelper 1.8.1。")
        if response.status_code == 409:
            raise SyncConflict("资料在同步期间发生变化，两端内容均已保留。")
        value = response.json()
        if response.is_error:
            raise RuntimeError(str(value.get("error", "手机同步请求失败"))[:300])
        return value

    async def manifest(self, side):
        value = await asyncio.to_thread(self.store.sync_manifest) if side == "mac" else await self.remote("GET", "sync/manifest")
        if value.get("protocolVersion") != 1 or not isinstance(value.get("records"), list):
            raise RuntimeError("设备同步协议不兼容，未写入任何资料。")
        uuid.UUID(value["storeId"])
        return value

    async def record(self, side, identifier):
        return await asyncio.to_thread(self.store.sync_record, identifier) if side == "mac" else await self.remote("GET", "sync/records/" + identifier)

    async def apply(self, side, identifier, expected, record):
        body = dict(id=identifier, expectedHash=expected, record=record)
        if side == "android":
            return await self.remote("POST", "sync/apply", body)
        try:
            return await asyncio.to_thread(self.store.sync_apply, body)
        except KnowledgeError as error:
            if error.status == 409:
                raise SyncConflict(str(error)) from error
            raise

    async def attachment_metadata(self, side, identifier):
        if side == "mac":
            return await asyncio.to_thread(self.store.read_attachment, identifier)
        return await self.remote("GET", "attachments/" + identifier)

    async def transfer_attachment(self, source, destination, identifier):
        metadata = await self.attachment_metadata(source, identifier)
        try:
            existing = await self.attachment_metadata(destination, identifier)
        except KnowledgeError as error:
            if error.status != 404:
                raise
            existing = None
        except RuntimeError:
            # A missing Android attachment is its documented 404 JSON response.
            origin = await self.desktop.find_android()
            check = await self.desktop.http.get(origin + "/api/knowledge/attachments/" + identifier, timeout=15)
            if check.status_code != 404:
                raise
            existing = None
        if existing:
            if existing["sha256"] != metadata["sha256"]:
                raise SyncConflict("附件 ID 相同但文件不同，未覆盖文件。")
            return 0
        with tempfile.TemporaryFile() as stream:
            if source == "mac":
                _, path = await asyncio.to_thread(self.store._attachment, identifier)
                with path.open("rb") as original:
                    while chunk := await asyncio.to_thread(original.read, 65536):
                        await asyncio.to_thread(stream.write, chunk)
            else:
                origin = await self.desktop.find_android()
                async with self.desktop.http.stream("GET", origin + "/api/knowledge/attachments/" + identifier + "/content", timeout=120) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(65536):
                        await asyncio.to_thread(stream.write, chunk)
            stream.seek(0)
            if destination == "mac":
                await asyncio.to_thread(self.store.sync_import_attachment, identifier, metadata["sha256"], stream,
                                        metadata["name"], metadata["mimeType"], metadata["bytes"])
            else:
                origin = await self.desktop.find_android()
                async def chunks():
                    while chunk := await asyncio.to_thread(stream.read, 65536):
                        yield chunk
                response = await self.desktop.http.post(origin + "/api/knowledge/sync/attachments/" + identifier,
                    params=dict(sha256=metadata["sha256"], name=metadata["name"]), content=chunks(),
                    headers={"content-type": metadata["mimeType"], "content-length": str(metadata["bytes"])}, timeout=300)
                if response.status_code == 409:
                    raise SyncConflict("附件校验或版本冲突，未提交文档。")
                if response.is_error:
                    raise RuntimeError("附件传输失败，未提交引用该附件的文档。")
        return 1

    async def transfer(self, source, destination, identifier, source_hash, destination_hash):
        record = await self.record(source, identifier)
        if record["hash"] != source_hash:
            raise SyncConflict("源资料在同步期间已修改，请重新比较。")
        attachments = 0
        if not record["deleted"]:
            document = {field: record["document"][field] for field in FIELDS}
            if document_sync_hash(document) != source_hash:
                raise RuntimeError("同步内容校验失败，未覆盖任何资料。")
            if re.search(r"/(?:artifacts/|api/knowledge/resources/artifact/)", document["content"]):
                raise RuntimeError("这条资料引用了原始采集文件，请先在手机上将素材插入为文档附件后同步。")
            identifiers = sorted({str(uuid.UUID(value)) for value in ATTACHMENT.findall(document["content"])})
            for attachment in identifiers:
                attachments += await self.transfer_attachment(source, destination, attachment)
            portable = dict(deleted=False, document=document)
        else:
            portable = dict(deleted=True)
        result = await self.apply(destination, identifier, destination_hash, portable)
        if result["hash"] != source_hash:
            raise RuntimeError("同步未能收敛，已保留两端资料。")
        return attachments

    async def conflict(self, identifier, left, right, reason):
        async def detail(side, metadata):
            if metadata is None:
                return dict(deleted=False, missing=True, hash=None, title="当前设备没有此条资料")
            result = {key: metadata[key] for key in ("hash", "deleted", "title", "updatedAt") if key in metadata}
            if not metadata["deleted"]:
                current = await self.record(side, identifier)
                result["contentPreview"] = current["document"]["content"][:1600]
                result["title"] = current["document"]["title"]
            return result
        return dict(id=identifier, title=(left or right or {}).get("title", "删除的资料"), kind=(left or right or {}).get("kind", "memory"), reason=reason,
                    mac=await detail("mac", left), android=await detail("android", right))

    async def run(self, direction="bidirectional"):
        if direction not in ("bidirectional", "download"):
            raise ValueError("请选择双向同步或仅从手机下载。")
        if self.lock.locked():
            raise RuntimeError("资料同步正在进行，请稍候。")
        async with self.lock:
            self.running = True
            summary = dict(uploaded=0, downloaded=0, deleted=0, unchanged=0, conflicts=0, attachments=0)
            conflicts = []
            try:
                local, remote = await self.manifest("mac"), await self.manifest("android")
                peer_id = remote["storeId"]
                peer = self.state["peers"].setdefault(peer_id, dict(base={}, localStoreId=local["storeId"]))
                if peer.get("localStoreId") != local["storeId"]:
                    peer.update(base={}, localStoreId=local["storeId"])
                base = peer["base"]
                left, right = ({row["id"]: row for row in manifest["records"]} for manifest in (local, remote))
                for identifier in sorted(set(left) | set(right)):
                    l, r = left.get(identifier), right.get(identifier)
                    lh, rh = l["hash"] if l else None, r["hash"] if r else None
                    if lh == rh:
                        base[identifier] = lh
                        summary["unchanged"] += 1
                        continue
                    known = identifier in base
                    old = base.get(identifier)
                    source = destination = None
                    if l is None:
                        source, destination = "android", "mac"
                    elif r is None and direction == "bidirectional":
                        source, destination = "mac", "android"
                    elif known and lh == old:
                        source, destination = "android", "mac"
                    elif known and rh == old and direction == "bidirectional":
                        source, destination = "mac", "android"
                    elif direction == "download" and (r is None or (known and rh == old)):
                        # Download-only deployment never publishes local changes or infers deletion.
                        summary["unchanged"] += 1
                        continue
                    if source is None:
                        conflicts.append(await self.conflict(identifier, l, r, "两端存在不同版本，需要选择保留哪一端。"))
                        continue
                    source_hash, destination_hash = (lh, rh) if source == "mac" else (rh, lh)
                    try:
                        summary["attachments"] += await self.transfer(source, destination, identifier, source_hash, destination_hash)
                        base[identifier] = source_hash
                        summary["uploaded" if source == "mac" else "downloaded"] += 1
                        if source_hash == "deleted":
                            summary["deleted"] += 1
                        self.persist()
                    except SyncConflict as error:
                        # Fresh details avoid presenting the stale versions as choices.
                        fresh_local, fresh_remote = await self.manifest("mac"), await self.manifest("android")
                        a = next((x for x in fresh_local["records"] if x["id"] == identifier), None)
                        b = next((x for x in fresh_remote["records"] if x["id"] == identifier), None)
                        conflicts.append(await self.conflict(identifier, a, b, str(error)))
                summary["conflicts"] = len(conflicts)
                self.state.update(initialized=True, lastSyncAt=int(time.time() * 1000), summary=summary, conflicts=conflicts, error=None)
                if self.desktop.preferences.get().get("installPrivateSkills", False):
                    from private_skills import materialize
                    self.state["privateSkills"] = await asyncio.to_thread(materialize, self.store)
                self.persist()
            except Exception as error:
                summary["conflicts"] = len(conflicts)
                self.state.update(summary=summary, conflicts=conflicts, error=str(error)[:500])
                self.persist()
                raise
            finally:
                self.running = False
            return self.view()

    async def resolve(self, identifier, keep):
        identifier = str(uuid.UUID(identifier))
        if keep not in ("mac", "android"):
            raise ValueError("请选择电脑或手机版本。")
        async with self.lock:
            conflict = next((row for row in self.state["conflicts"] if row["id"] == identifier), None)
            if not conflict:
                raise ValueError("该冲突已处理，请刷新同步状态。")
            other = "android" if keep == "mac" else "mac"
            source_hash, destination_hash = conflict[keep].get("hash"), conflict[other].get("hash")
            if source_hash is None:
                raise ValueError("没有该条资料的一端不能作为保留版本。")
            # No unconditional force write: the versions displayed must still exist.
            await self.transfer(keep, other, identifier, source_hash, destination_hash)
            remote = await self.manifest("android")
            self.state["peers"][remote["storeId"]]["base"][identifier] = source_hash
            self.state["conflicts"] = [row for row in self.state["conflicts"] if row["id"] != identifier]
            self.state["summary"]["conflicts"] = len(self.state["conflicts"])
            self.state["error"] = None
            if self.desktop.preferences.get().get("installPrivateSkills", False):
                from private_skills import materialize
                self.state["privateSkills"] = await asyncio.to_thread(materialize, self.store)
            self.persist()
            return self.view()

    async def watch(self):
        while True:
            try:
                config = self.desktop.preferences.get()
                # New computer recovery copies from the phone and cannot publish emptiness.
                fresh = not self.state["initialized"] and self.store.document_stats()["totalDocuments"] == 0
                if (fresh or config.get("autoKnowledgeSync", False)) and not self.lock.locked():
                    await self.run("download" if fresh else "bidirectional")
            except Exception as error:
                self.state["error"] = str(error)[:500]
            await asyncio.sleep(10)

    def start(self):
        self.task = asyncio.create_task(self.watch())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
