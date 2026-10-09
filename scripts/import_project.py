#!/usr/bin/env python3
"""Attach DevHelper HTTP MCP and its public Skill to an existing project.

Python 3.11+ and the standard library are sufficient. This writes project
configuration only; it never grants trust, changes client approval settings,
copies private memories, or runs tools during the connection check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:
    print("请使用 Python 3.11 或更新版本运行导入助手。", file=sys.stderr)
    raise SystemExit(1)

DEFAULT_URL = "http://127.0.0.1:8876/mcp"
SOURCE_SKILL = Path(__file__).resolve().parents[1] / "skills/devhelper-connect/SKILL.md"
MARKER = ".devhelper-managed.json"
MANAGER = "devhelper-project-import"
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PROTOCOL_VERSION = "2025-06-18"


class ImportFailure(ValueError):
    """An actionable import error whose text is safe to display."""


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportFailure("JSON 中存在重复设置名称，无法安全合并。")
        result[key] = value
    return result


def _json(data: bytes, label: str):
    try:
        def invalid_constant(_):
            raise ImportFailure(label + " 包含 JSON 不支持的数值。")
        return json.loads(data.decode("utf-8"), object_pairs_hook=_json_pairs, parse_constant=invalid_constant)
    except (UnicodeError, json.JSONDecodeError) as failed:
        raise ImportFailure(label + " 不是有效的 UTF-8 JSON。") from failed


def _encoded_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def validate_url(value: str):
    """Accept a direct HTTP endpoint; never silently drop credentials/query data."""
    if not isinstance(value, str) or not value or any(ord(char) < 33 or ord(char) > 126 for char in value) or "\\" in value:
        raise ImportFailure("请提供有效的 HTTP MCP 地址。")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError as failed:
        raise ImportFailure("HTTP MCP 地址的主机或端口无效。") from failed
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ImportFailure("MCP 地址必须使用 HTTP/HTTPS，且不能包含账户或密码。")
    if "%" in parts.netloc or parts.netloc.endswith(":"):
        raise ImportFailure("HTTP MCP 地址的主机或端口无效。")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise ImportFailure("MCP 地址不能包含查询参数或片段。")
    if port is not None and not 1 <= port <= 65535:
        raise ImportFailure("HTTP MCP 地址的端口无效。")
    path = parts.path.rstrip("/") or "/mcp"
    if not path.endswith("/mcp") or any(piece in (".", "..", "") for piece in path.split("/")[1:]) or "%" in path:
        raise ImportFailure("请使用以 /mcp 结尾的 HTTP MCP 地址。")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ImportFailure("服务返回了重定向；请使用服务的直接 HTTP MCP 地址。")


def _http(opener, url, method="GET", body=None, headers=None, limit=MAX_RESPONSE_BYTES):
    request_headers = {"Accept": "application/json, text/event-stream"}
    request_headers.update(headers or {})
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=raw, headers=request_headers, method=method)
    try:
        with opener.open(request, timeout=5) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ImportFailure("MCP 检查响应过大，已停止导入。")
            return response.status, response.headers, data
    except urllib.error.HTTPError as failed:
        raise ImportFailure("无法检查 MCP 服务：HTTP " + str(failed.code) + "。请确认服务已启动。") from failed
    except (urllib.error.URLError, TimeoutError, OSError) as failed:
        raise ImportFailure("无法连接 MCP 服务。请先启动服务，或使用 --skip-check 明确离线导入。") from failed


def _rpc_result(data, headers, request_id):
    content_type = headers.get("Content-Type", "").split(";", 1)[0].lower()
    if content_type == "text/event-stream":
        try:
            source = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeError as failed:
            raise ImportFailure("MCP 服务返回的事件内容无法读取。") from failed
        candidates = []
        for event in source.split("\n\n"):
            lines = [line[5:].lstrip(" ") for line in event.split("\n") if line.startswith("data:")]
            if lines:
                candidates.append(_json("\n".join(lines).encode("utf-8"), "MCP 事件"))
        payload = next((item for item in candidates if isinstance(item, dict) and item.get("id") == request_id), None)
    else:
        payload = _json(data, "MCP 响应")
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
        raise ImportFailure("服务没有返回有效的 MCP 响应。")
    if "error" in payload or not isinstance(payload.get("result"), dict):
        raise ImportFailure("MCP 检查失败；服务返回了协议错误。")
    return payload["result"]


def check_connection(url: str):
    """Only initialize and list schemas; no bootstrap, content reads or tool calls."""
    url = validate_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    health_url = url.removesuffix("/mcp") + "/health"
    status, _, data = _http(opener, health_url, limit=65536)
    health = _json(data, "服务状态")
    if status != 200 or not isinstance(health, dict) or health.get("transport") != "streamable-http" or health.get("status") != "ok":
        raise ImportFailure("这个地址不是可用的 DevHelper HTTP MCP 服务。")
    if "appId" in health and health["appId"] != "devhelper-desktop":
        raise ImportFailure("这个地址指向其他应用，已停止导入。")
    session_id = None
    session_headers = {}
    try:
        _, headers, data = _http(opener, url, "POST", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "devhelper-project-import", "version": "1.0.0"}},
        })
        result = _rpc_result(data, headers, 1)
        protocol = result.get("protocolVersion")
        if protocol not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            raise ImportFailure("服务返回了不支持的 MCP 协议版本。")
        server = result.get("serverInfo")
        if not isinstance(server, dict) or server.get("name") not in ("devhelper-desktop", "libsu-mcp"):
            raise ImportFailure("这个 MCP 服务不是 DevHelper。")
        if (health.get("appId") == "devhelper-desktop") != (server["name"] == "devhelper-desktop"):
            raise ImportFailure("服务状态与 MCP 身份不一致，已停止导入。")
        session_headers["MCP-Protocol-Version"] = protocol
        session_id = headers.get("Mcp-Session-Id")
        if session_id:
            if len(session_id) > 256 or any(ord(char) < 33 or ord(char) > 126 for char in session_id):
                session_id = None
                raise ImportFailure("服务返回了无效的 MCP 会话编号。")
            session_headers["Mcp-Session-Id"] = session_id
        _http(opener, url, "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_headers)
        _, headers, data = _http(opener, url, "POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session_headers)
        result = _rpc_result(data, headers, 2)
        catalog = result.get("tools")
        if not isinstance(catalog, list) or not catalog or not all(isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("inputSchema"), dict) for item in catalog):
            raise ImportFailure("服务没有返回有效的工具清单。")
        names = {item["name"] for item in catalog}
        required = "devhelper_list_devices" if server["name"] == "devhelper-desktop" else "knowledge_status"
        if required not in names:
            raise ImportFailure("工具清单不包含 DevHelper 的设备或知识管理入口。")
        return {"status": "verified", "server": server["name"], "toolCount": len(catalog), "protocolVersion": protocol,
                "operations": ["health", "initialize", "notifications/initialized", "tools/list"]}
    finally:
        if session_id:
            # Closing only the check's own MCP session does not execute a tool.
            try:
                _http(opener, url, "DELETE", headers=session_headers, limit=65536)
            except ImportFailure:
                pass


def _guard(root: Path, target: Path, directory=False):
    """Never traverse symlinks in the selected project or a destination path."""
    try:
        relative = target.relative_to(root)
    except ValueError as failed:
        raise ImportFailure("导入目标超出了指定工程。") from failed
    current = root
    for position, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise ImportFailure("导入路径包含符号链接：" + str(relative))
        if current.exists():
            is_directory = directory or position < len(relative.parts) - 1
            if is_directory and not current.is_dir():
                raise ImportFailure("导入目录被普通文件占用：" + str(relative))
            if not is_directory and not current.is_file():
                raise ImportFailure("导入目标不是普通文件：" + str(relative))
    if root.is_symlink() or not root.is_dir():
        raise ImportFailure("工程必须是已存在的普通目录，不能是符号链接。")


def _read(root, target):
    _guard(root, target)
    if not target.exists():
        return None
    if target.stat().st_size > MAX_CONFIG_BYTES:
        raise ImportFailure("已有配置过大，无法安全合并：" + str(target.relative_to(root)))
    return target.read_bytes()


def _codex_config(original: bytes | None, url: str):
    data = original or b""
    try:
        text = data.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError) as failed:
        raise ImportFailure(".codex/config.toml 不是有效 TOML；已保留原文件。") from failed
    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ImportFailure("Codex 的 mcp_servers 设置格式不正确。")
    if "devhelper" in servers:
        entry = servers["devhelper"]
        if isinstance(entry, dict) and entry.get("url") == url and "command" not in entry:
            return original
        raise ImportFailure("Codex 已有不同的 devhelper 配置；请先确认现有地址，未覆盖。")
    newline = "\r\n" if "\r\n" in text else "\n"
    suffix = ("" if not text or text.endswith(("\n", "\r")) else newline)
    suffix += newline + "# DevHelper HTTP MCP (project only)" + newline
    suffix += "[mcp_servers.devhelper]" + newline + "url = " + json.dumps(url, ensure_ascii=False) + newline
    combined = data + suffix.encode("utf-8")
    try:
        verified = tomllib.loads(combined.decode("utf-8"))
    except tomllib.TOMLDecodeError as failed:
        raise ImportFailure("现有 Codex 配置无法安全追加 devhelper 表；已保留原文件，请手动合并。") from failed
    if verified.get("mcp_servers", {}).get("devhelper", {}).get("url") != url:
        raise ImportFailure("Codex 配置校验失败，未写入。")
    return combined


def _claude_config(original: bytes | None, url: str):
    parsed = {} if original is None else _json(original, ".mcp.json")
    if not isinstance(parsed, dict):
        raise ImportFailure(".mcp.json 必须是 JSON 对象；已保留原文件。")
    servers = parsed.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ImportFailure("Claude 的 mcpServers 设置格式不正确。")
    if "devhelper" in servers:
        entry = servers["devhelper"]
        if isinstance(entry, dict) and entry.get("type") in ("http", "streamable-http") and entry.get("url") == url and "command" not in entry:
            return original
        raise ImportFailure("Claude 已有不同的 devhelper 配置；请先确认现有地址，未覆盖。")
    servers["devhelper"] = {"type": "http", "url": url}
    parsed["mcpServers"] = servers
    return _encoded_json(parsed)


def _skill_plan(root, directory, source):
    target, marker_path = directory / "SKILL.md", directory / MARKER
    original, old_marker = _read(root, target), _read(root, marker_path)
    digest = hashlib.sha256(source).hexdigest()
    marker = _encoded_json({"formatVersion": 1, "managedBy": MANAGER, "files": {"SKILL.md": digest}})
    if old_marker is not None:
        parsed = _json(old_marker, "DevHelper Skill 管理记录")
        if not isinstance(parsed, dict) or parsed.get("formatVersion") != 1 or parsed.get("managedBy") != MANAGER or not isinstance(parsed.get("files"), dict) or set(parsed["files"]) != {"SKILL.md"}:
            raise ImportFailure("已有 Skill 管理记录无法验证，未覆盖。")
        if original is None or hashlib.sha256(original).hexdigest() != parsed["files"]["SKILL.md"]:
            raise ImportFailure("DevHelper Skill 已被本地编辑，未覆盖：" + str(directory.relative_to(root)))
        return [(target, original, source), (marker_path, old_marker, marker)]
    if original is not None:
        if original != source:
            raise ImportFailure("工程中已有同名 Skill，内容不同，未覆盖：" + str(directory.relative_to(root)))
        # An identical user-owned Skill is usable without claiming ownership.
        return [(target, original, original)]
    if directory.exists() and any(directory.iterdir()):
        raise ImportFailure("工程中已有同名 Skill 目录，未覆盖：" + str(directory.relative_to(root)))
    return [(target, None, source), (marker_path, None, marker)]


def _atomic(path, data, mode=None):
    descriptor, temporary = tempfile.mkstemp(prefix=".devhelper-import-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode if mode is not None else 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _apply(root, plans):
    changes = [(path, old, new) for path, old, new in plans if old != new]
    made_directories, applied = [], []
    try:
        # Recheck the entire snapshot after the network probe and before writing.
        for path, old, _ in plans:
            if _read(root, path) != old:
                raise ImportFailure("导入期间工程配置发生变化，已停止；请重新导入。")
        for path, old, new in changes:
            parent = path.parent
            missing = []
            while parent != root and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                _guard(root, directory, directory=True)
                directory.mkdir()
                made_directories.append(directory)
            _guard(root, path)
            if _read(root, path) != old:
                raise ImportFailure("导入期间工程配置发生变化，已停止；请重新导入。")
            mode = stat.S_IMODE(path.stat().st_mode) if old is not None else None
            _atomic(path, new, mode)
            applied.append((path, old, new, mode))
    except Exception as failed:
        rollback_errors = []
        for path, old, new, mode in reversed(applied):
            try:
                _guard(root, path)
                if _read(root, path) != new:
                    raise ImportFailure("文件在导入期间被其他程序修改。")
                if old is None:
                    path.unlink()
                else:
                    _atomic(path, old, mode)
            except (ImportFailure, OSError):
                rollback_errors.append(str(path.relative_to(root)))
        for directory in reversed(made_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_errors:
            raise ImportFailure("导入失败；部分文件无法回退，请检查：" + ", ".join(rollback_errors)) from failed
        if isinstance(failed, ImportFailure):
            raise
        raise ImportFailure("写入失败，已回退本次导入的文件。请检查工程目录权限。") from failed


def import_project(project=None, client="both", url=DEFAULT_URL, *, dry_run=False,
                   skip_check=False, without_skill=False, skill_source=None):
    if client not in ("both", "codex", "claude"):
        raise ImportFailure("客户端必须是 both、codex 或 claude。")
    root = Path(os.path.abspath(Path(project if project is not None else Path.cwd()).expanduser()))
    if root.is_symlink() or not root.is_dir():
        raise ImportFailure("工程必须是已存在的普通目录，不能是符号链接。")
    root = root.resolve()
    url = validate_url(url)
    source = None
    if not without_skill:
        source_path = Path(skill_source) if skill_source is not None else SOURCE_SKILL
        if source_path.is_symlink() or not source_path.is_file() or source_path.stat().st_size > MAX_CONFIG_BYTES:
            raise ImportFailure("公开的 devhelper-connect Skill 缺失或无效；请使用完整发布包。")
        source = source_path.read_bytes()
        try:
            source.decode("utf-8")
        except UnicodeError as failed:
            raise ImportFailure("公开 Skill 不是有效的 UTF-8 文档。") from failed
    clients = ("codex", "claude") if client == "both" else (client,)
    plans, configured = [], []
    for selected in clients:
        relative = ".codex/config.toml" if selected == "codex" else ".mcp.json"
        target = root / relative
        original = _read(root, target)
        builder = _codex_config if selected == "codex" else _claude_config
        rendered = builder(original, url)
        plans.append((target, original, rendered))
        skill_relative = (".agents" if selected == "codex" else ".claude") + "/skills/devhelper-connect"
        if source is not None:
            plans.extend(_skill_plan(root, root / skill_relative, source))
        configured.append({"client": selected, "mcpConfig": relative,
                           "skill": None if source is None else skill_relative + "/SKILL.md"})
        if selected == "codex":
            parsed = tomllib.loads((rendered or b"").decode("utf-8"))
            configured[-1]["projectEntryEnabled"] = parsed["mcp_servers"]["devhelper"].get("enabled", True)
    # Validate ALL configurations and Skill paths before probing or mutating.
    for path, _, _ in plans:
        _guard(root, path)
    verification = {"status": "skipped", "reason": "explicit --skip-check", "operations": []} if skip_check else check_connection(url)
    changes = [{"path": str(path.relative_to(root)), "action": "create" if old is None else "update"}
               for path, old, new in plans if old != new]
    if not dry_run:
        _apply(root, plans)
    return {"project": str(root), "mcpUrl": url, "dryRun": dry_run, "applied": not dry_run,
            "verification": verification, "clients": configured, "changes": changes,
            "clientConnection": "not-tested",
            "notes": ["重新打开工程或重启客户端后加载配置；服务检查成功不代表客户端已连接。",
                      "Codex 仅在受信任工程中读取项目配置；导入不会修改信任或工具审批设置。",
                      "Claude Code 可能要求批准项目 MCP；已有同名本地配置优先于项目配置，请在 /mcp 确认实际连接。",
                      "127.0.0.1 指客户端运行的电脑；远程客户端请使用它能够访问的地址。"]}


def main():
    parser = argparse.ArgumentParser(description="将 DevHelper HTTP MCP 和公开 Skill 导入当前或指定工程；不会修改全局配置。")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="已有工程目录，默认当前目录")
    parser.add_argument("--client", choices=("both", "codex", "claude"), default="both")
    parser.add_argument("--url", default=DEFAULT_URL, help="可访问的 HTTP MCP 地址")
    parser.add_argument("--dry-run", action="store_true", help="检查配置和服务，仅预览，不写入")
    parser.add_argument("--skip-check", action="store_true", help="明确跳过服务检查，离线写入配置")
    parser.add_argument("--without-skill", action="store_true", help="只导入 MCP 配置")
    args = parser.parse_args()
    try:
        result = import_project(args.project, args.client, args.url, dry_run=args.dry_run,
                                skip_check=args.skip_check, without_skill=args.without_skill)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ImportFailure, OSError) as failed:
        print("导入失败：" + str(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
