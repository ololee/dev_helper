"""Text clipboard sharing. Clipboard content is kept in RAM, never in logs or settings."""
from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from typing import Awaitable, Callable


class MacClipboard:
    def _pasteboard(self):
        if sys.platform != "darwin":
            raise RuntimeError("当前版本的电脑剪贴板支持 macOS。")
        from AppKit import NSPasteboard
        return NSPasteboard.generalPasteboard()

    def read(self, max_chars=8192) -> dict:
        from AppKit import NSPasteboardTypeString
        pasteboard = self._pasteboard()
        value = pasteboard.stringForType_(NSPasteboardTypeString)
        text = str(value) if value is not None else ""
        return dict(status="ok", text=text[:max_chars], hasText=value is not None,
                    empty=value is None and not bool(pasteboard.pasteboardItems()),
                    truncated=len(text) > max_chars, length=len(text),
                    changeCount=int(pasteboard.changeCount()), method="macOSPasteboard")

    def write(self, text: str) -> dict:
        from AppKit import NSPasteboardTypeString
        pasteboard = self._pasteboard()
        pasteboard.clearContents()
        written = bool(pasteboard.setString_forType_(text, NSPasteboardTypeString))
        if not written:
            raise RuntimeError("无法写入 Mac 系统剪贴板。")
        result = self.read()
        result.update(written=True, verified=result["text"] == text)
        return result

    def snapshot(self):
        """Keep all pasteboard representations in memory during an owned verification."""
        pasteboard = self._pasteboard()
        return [[(str(kind), bytes(item.dataForType_(kind))) for kind in item.types() if item.dataForType_(kind) is not None]
                for item in (pasteboard.pasteboardItems() or [])]

    def restore(self, snapshot):
        from AppKit import NSPasteboardItem
        from Foundation import NSData
        pasteboard = self._pasteboard()
        items = []
        for record in snapshot:
            item = NSPasteboardItem.alloc().init()
            for kind, raw in record:
                item.setData_forType_(NSData.dataWithBytes_length_(raw, len(raw)), kind)
            items.append(item)
        pasteboard.clearContents()
        if items:
            pasteboard.writeObjects_(items)


class ClipboardShare:
    def __init__(self, preferences, android_call: Callable[[str, dict], Awaitable[dict]], local=None):
        self.preferences = preferences
        self.android_call = android_call
        self.local = local or MacClipboard()
        self.lock = asyncio.Lock()
        self.current = dict(text="", version=0, source="manual", updatedAt=None, error=None)
        self.baseline: dict[str, str] = {}
        self._sync_initialized = False
        self.task = None

    def view(self) -> dict:
        return {**self.current, "autoSync": self.preferences.get()["autoClipboardSync"],
                "androidReadMode": "手机前台读取或 Root 后台读取"}

    def reset_sync_baseline(self):
        """Call when auto sharing or the selected Android endpoint changes."""
        self.baseline = {}
        self._sync_initialized = False

    async def read_device(self, device: str, max_chars=8192) -> dict:
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 1 <= max_chars <= 8192:
            raise ValueError("读取范围必须在 1–8192 字符之间。")
        if device == "mac":
            result = await asyncio.to_thread(self.local.read, max_chars)
        elif device == "android":
            result = await self.android_call("research_get_clipboard", {"maxChars": max_chars})
        else:
            raise ValueError("请选择电脑或手机。")
        if result.get("status", "ok") != "ok":
            reason = result.get("message") or result.get("error") or "手机后台无法读取剪贴板，请在 DevHelper 的剪贴板页面读取后重试。"
            raise RuntimeError(reason)
        result = dict(result)
        if result.get("hasText", "text" in result) is False:
            result["hasText"] = False
            # Android image/URI clips correctly omit text. They are never converted to text.
            result.setdefault("text", "")
        elif not isinstance(result.get("text"), str):
            raise RuntimeError("设备没有返回有效的剪贴板文本。")
        else:
            result["hasText"] = True
            if len(result["text"]) > max_chars:
                result["text"] = result["text"][:max_chars]
                result["truncated"] = True
        return result

    async def write_device(self, device: str, text: str) -> dict:
        if device not in ("mac", "android"):
            raise ValueError("请选择电脑或手机。")
        self.validate_text(text)
        if device == "mac":
            result = await asyncio.to_thread(self.local.write, text)
        else:
            result = await self.android_call("research_set_clipboard", {"text": text})
        if result.get("status", "ok") != "ok" or result.get("written") is not True:
            raise RuntimeError(result.get("message") or result.get("error") or "写入剪贴板未完成。")
        if result.get("verified") is False:
            raise RuntimeError("已提交写入，但无法确认目标剪贴板内容；请在目标设备检查后重试。")
        self.baseline[device] = self.fingerprint(text)
        return result

    @staticmethod
    def fingerprint(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def validate_text(text):
        if not isinstance(text, str) or "\0" in text:
            raise ValueError("共享剪贴板需要有效文本，不能包含 NUL 字符。")
        try:
            # Match Android's UTF-16 bound before starting a multi-device write.
            too_long = len(text.encode("utf-16-le")) // 2 > 8192
        except UnicodeEncodeError as exc:
            raise ValueError("剪贴板文本含有无效字符。") from exc
        if too_long:
            raise ValueError("共享剪贴板支持最多 8192 字符的文本。")

    @classmethod
    def sample_fingerprint(cls, sample):
        if sample["hasText"]:
            return cls.fingerprint(sample["text"])
        # A non-text state is distinct from an explicitly copied empty text string.
        # An available pasteboard counter also detects replacing an image with another image.
        marker = "nontext:empty" if sample.get("empty") else "nontext:other"
        counter = sample.get("changeCount", sample.get("changedAt"))
        return marker + (f":{counter}" if isinstance(counter, int) else "")

    def write_failure(self, written, destination, error):
        # A submitted write may have changed a pasteboard even when readback failed. Treat
        # the next observation as a new baseline, not as a copy to echo to the other device.
        self.reset_sync_baseline()
        self.current["error"] = str(error)
        return {**self.view(), "status": "partial" if written else "error", "partial": bool(written),
                "writtenDevices": written, "failedDevice": destination, "error": str(error)}

    def adopt(self, text: str, source: str):
        self.current.update(text=text, version=self.current["version"] + 1, source=source, updatedAt=int(time.time() * 1000), error=None)

    async def publish(self, text: str, target: str) -> dict:
        self.validate_text(text)
        if target not in ("mac", "android", "both"):
            raise ValueError("请选择电脑、手机或两端。")
        async with self.lock:
            destinations = ["android", "mac"] if target == "both" else [target]
            written = []
            for destination in destinations:
                try:
                    await self.write_device(destination, text)
                    written.append(destination)
                except (RuntimeError, OSError) as exc:
                    return self.write_failure(written, destination, exc)
            self.adopt(text, "manual")
            return {**self.view(), "status": "ok", "partial": False, "writtenDevices": written}

    async def pull(self, source: str, target: str | None = None) -> dict:
        if source not in ("mac", "android"):
            raise ValueError("请选择电脑或手机。")
        if target is not None and target not in ("mac", "android", "both"):
            raise ValueError("请选择电脑、手机或两端。")
        async with self.lock:
            result = await self.read_device(source)
            written = []
            if target is not None:
                if not result["hasText"]:
                    raise RuntimeError("当前剪贴板不是文本，未修改目标设备的剪贴板。")
                if result.get("truncated"):
                    raise RuntimeError("剪贴板文本超过共享上限，已显示前段，请先缩短内容再发送。")
                self.validate_text(result["text"])
                destinations = ["android", "mac"] if target == "both" else [target]
                for destination in destinations:
                    if destination != source:
                        try:
                            await self.write_device(destination, result["text"])
                            written.append(destination)
                        except (RuntimeError, OSError) as exc:
                            return self.write_failure(written, destination, exc)
            self.adopt(result["text"], source)
            if not result.get("truncated"):
                self.baseline[source] = self.sample_fingerprint(result)
            return {**self.view(), "status": "ok", "partial": False, "writtenDevices": written,
                    **{k: result[k] for k in ("hasText", "empty", "truncated", "length") if k in result}}

    async def sync_once(self):
        async with self.lock:
            samples = {device: await self.read_device(device) for device in ("mac", "android")}
            if not self.preferences.get()["autoClipboardSync"]:
                self.reset_sync_baseline()
                return
            if any(value.get("truncated") for value in samples.values()):
                raise RuntimeError("剪贴板文本超过共享上限，自动共享暂缓。")
            fingerprints = {key: self.sample_fingerprint(value) for key, value in samples.items()}
            if not self._sync_initialized or not all(key in self.baseline for key in samples):
                self.baseline = fingerprints
                self._sync_initialized = True
                self.current["error"] = None
                return
            changed = [key for key in samples if fingerprints[key] != self.baseline.get(key)]
            if len(changed) == 2 and fingerprints["mac"] != fingerprints["android"]:
                self.current["error"] = "两端剪贴板同时变化，已保留各自内容，请手动选择要共享的一端。"
                self.baseline = fingerprints
                return
            if not changed:
                return
            if len(changed) == 2:
                # Both ends already contain the same text; writing would only create an echo.
                if samples["mac"]["hasText"]:
                    self.adopt(samples["mac"]["text"], "mac")
                self.baseline = fingerprints
                return
            source = changed[0]
            if not samples[source]["hasText"]:
                # A new image/file is not a text source. It can still receive future text.
                self.baseline = fingerprints
                return
            target = "android" if source == "mac" else "mac"
            # Device reads span network round trips. Do not overwrite a new destination copy
            # that arrived after its first sample was read.
            latest_target = await self.read_device(target)
            if latest_target.get("truncated"):
                raise RuntimeError("目标剪贴板文本超过共享上限，自动共享暂缓。")
            latest_fingerprint = self.sample_fingerprint(latest_target)
            if latest_fingerprint != fingerprints[target]:
                if latest_fingerprint == fingerprints[source]:
                    self.adopt(samples[source]["text"], source)
                    self.baseline = {key: fingerprints[source] for key in samples}
                else:
                    self.current["error"] = "目标剪贴板在同步期间变化，已保留两端内容，请手动选择要共享的一端。"
                    self.baseline = {**fingerprints, target: latest_fingerprint}
                return
            await self.write_device(target, samples[source]["text"])
            self.adopt(samples[source]["text"], source)
            self.baseline = {key: fingerprints[source] for key in samples}

    async def watch(self):
        while True:
            if self.preferences.get()["autoClipboardSync"]:
                try:
                    await self.sync_once()
                except (RuntimeError, OSError, ValueError) as exc:
                    self.current["error"] = str(exc)
            else:
                self.reset_sync_baseline()
            await asyncio.sleep(2)

    def start(self):
        self.task = asyncio.create_task(self.watch())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
