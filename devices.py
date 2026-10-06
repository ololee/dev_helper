"""Persistent preferences and mDNS discovery; network addresses are resolved at runtime."""
from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path.rstrip("/") or parsed.query or parsed.fragment:
        raise ValueError("请填写设备 HTTP 根地址，不要附带路径、账号或参数。")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("端口无效。")
    return f"{parsed.scheme}://{parsed.netloc}"


def local_ips() -> list[str]:
    result: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            result.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        output = subprocess.check_output(["/sbin/ifconfig"], text=True, timeout=2)
        for line in output.splitlines():
            words = line.strip().split()
            if len(words) >= 2 and words[0] == "inet":
                address = ipaddress.ip_address(words[1])
                if address.is_private and not address.is_loopback and not address.is_link_local:
                    result.append(str(address))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return list(dict.fromkeys(result))


class Preferences:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.value = dict(androidUrl="", androidDeviceId="", autoClipboardSync=False,
                          autoKnowledgeSync=False, installPrivateSkills=False,
                          ttsUrl="http://127.0.0.1:8793", deviceId=str(uuid.uuid4()))
        if path.is_file():
            self.value.update(json.loads(path.read_text(encoding="utf-8")))
        else:
            self.save()

    def get(self) -> dict:
        with self.lock:
            return dict(self.value)

    def update(self, **changes) -> dict:
        with self.lock:
            self.value.update(changes)
            self.save()
            return dict(self.value)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class DeviceDiscovery:
    service_type = "_devhelper._tcp.local."

    def __init__(self, preferences: Preferences, port: int):
        self.preferences, self.port = preferences, port
        self.records: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.zeroconf = None
        self.browser = None
        self.advertisement = None
        self.error = None

    def start(self):
        try:
            self.zeroconf = Zeroconf()
            self.browser = ServiceBrowser(self.zeroconf, self.service_type, self)
            ips = local_ips()
            if ips:
                identity = self.preferences.get()["deviceId"]
                self.advertisement = ServiceInfo(
                    self.service_type, f"DevHelper-Mac-{identity[:8]}.{self.service_type}",
                    addresses=[socket.inet_aton(x) for x in ips], port=self.port,
                    properties={"platform": "mac", "deviceId": identity, "name": "这台 Mac"},
                    server=f"devhelper-{identity[:8]}.local.")
                self.zeroconf.register_service(self.advertisement, allow_name_change=True)
        except (OSError, RuntimeError) as exc:
            self.error = str(exc)

    def add_service(self, zeroconf, service_type, name):
        self.update_service(zeroconf, service_type, name)

    def update_service(self, zeroconf, service_type, name):
        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        if info is None:
            return
        properties = {k.decode(): v.decode(errors="replace") if isinstance(v, bytes) else str(v) for k, v in info.properties.items()}
        if properties.get("platform") != "android":
            return
        addresses = [f"http://{x}:{info.port}" for x in info.parsed_addresses() if ":" not in x]
        if not addresses:
            return
        with self.lock:
            self.records[name] = dict(id=properties.get("deviceId", name), name=properties.get("name", "Android 手机"), addresses=addresses)

    def remove_service(self, zeroconf, service_type, name):
        with self.lock:
            self.records.pop(name, None)

    def candidates(self) -> list[str]:
        config = self.preferences.get()
        with self.lock:
            records = list(self.records.values())
        chosen = [x for x in records if x["id"] == config["androidDeviceId"]] if config["androidDeviceId"] else records
        values = [config["androidUrl"]] if config["androidUrl"] else []
        for record in chosen:
            values.extend(record["addresses"])
        return list(dict.fromkeys(values))

    def stop(self):
        if self.browser:
            self.browser.cancel()
        if self.zeroconf:
            if self.advertisement:
                self.zeroconf.unregister_service(self.advertisement)
            self.zeroconf.close()
