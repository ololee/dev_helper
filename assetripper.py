"""Computer-side AssetRipper business functions and CLI; no separate MCP server.

Install tools into a private runtime, then call the same export() from Python,
the existing HTTP service, or ``python -m assetripper``. Source revision and
archive digest are pinned. AssetRipper and AssetRipper-CLI are GPL-3.0; their
source and license notices stay alongside the locally built runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
from typing import Callable
import urllib.request
import uuid
import venv
import zipfile
from assetripper_cli.normalize_tuanjie import is_unity_resource

UPSTREAM_URL = "https://github.com/AssetRipper/AssetRipper"
CLI_URL = "https://github.com/MeikoMei16/AssetRipper-CLI"
CLI_REVISION = "e0dc88f6dbea9773d2a8f0916a9221e753897e56"
SOURCE_SHA256 = "f2e07f67b7b09a5e8383d90c50695b8d3fe03b74c048f3d215d6db03c9af283f"
SDK_VERSION = "10.0.401"
UNITYPY_VERSION = "1.25.3"
PROFILES = ("full-project", "full-raw", "audio", "cg", "characters", "backgrounds",
            "sprites", "ui", "player-art", "narrative")
VARIANTS = ("unity", "tuanjie", "auto")
Progress = Callable[[dict], None]


class AssetRipperError(RuntimeError):
    def __init__(self, message: str, code: str = "assetripper_error"):
        super().__init__(message)
        self.code = code


def _host_rid() -> str:
    system, machine = platform.system(), platform.machine().lower()
    architecture = {"aarch64": "arm64", "arm64": "arm64", "amd64": "x64", "x86_64": "x64"}.get(machine)
    prefix = {"Darwin": "osx", "Windows": "win", "Linux": "linux"}.get(system)
    if not architecture or not prefix:
        raise AssetRipperError("当前电脑架构不支持 AssetRipper 自动安装。", "unsupported_host")
    return f"{prefix}-{architecture}"


def default_private_root() -> Path:
    configured = os.environ.get("DEVHELPER_ASSETRIPPER_ROOT")
    if configured:
        return Path(configured).expanduser()
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif platform.system() == "Darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return base / "DevHelper/private-tools/assetripper"


def _emit(callback: Progress | None, **event) -> None:
    if callback:
        messages = {"normalizing": "正在转换团结资源", "exporting": "正在导出资源工程",
                    "building": "正在准备电脑导出工具", "downloading": "正在下载安装文件",
                    "installing_dependencies": "正在安装资源转换组件", "ready": "电脑导出工具已就绪",
                    "completed": "资源导出完成", "partial": "素材已导出，部分条目未成功，请查看结果"}
        if "message" not in event and event.get("phase") in messages:
            event["message"] = messages[event["phase"]]
        callback(event)


def _check_cancel(event) -> None:
    if event is not None and event.is_set():
        raise AssetRipperError("AssetRipper 操作已取消。", "cancelled")


def _json(path: Path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            # Publish can spawn compiler workers. Stop that process tree as
            # well as its launcher; all arguments are fixed except the PID of
            # the process we just created.
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, shell=False)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def _run(argv: list[str], *, cwd: Path, log: Path, cancel_event=None,
         progress_callback: Progress | None = None, phase: str = "exporting",
         timeout: float | None = None) -> tuple[int, list[str]]:
    """Drain both streams concurrently. Raw logs are private and never callbacks."""
    _check_cancel(cancel_event)
    log.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
                "ASSETRIPPER_JOB_WORKERS": "1"})
    try:
        process = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   bufsize=1, shell=False, start_new_session=os.name != "nt",
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    except OSError as exc:
        raise AssetRipperError("无法启动 AssetRipper，请检查电脑上的执行程序配置。", "launch_failed") from exc
    lines = queue.Queue()
    def read(stream, name):
        try:
            for line in stream:
                lines.put((name, line))
        finally:
            stream.close()
            lines.put((name, None))
    threads = [threading.Thread(target=read, args=(stream, name), daemon=True)
               for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]]
    for thread in threads:
        thread.start()
    tail, ended = [], 0
    start, last_update = time.monotonic(), 0.0
    try:
        with log.open("w", encoding="utf-8") as sink:
            while ended < 2 or process.poll() is None:
                _check_cancel(cancel_event)
                if timeout and time.monotonic() - start > timeout:
                    raise AssetRipperError("AssetRipper 操作超过等待时间。", "timeout")
                try:
                    stream, line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    ended += 1
                    continue
                sink.write(f"[{stream}] {line}")
                sink.flush()
                tail.append(line.rstrip())
                tail = tail[-30:]
                now = time.monotonic()
                if now - last_update > 0.5:
                    safe = {"phase": phase, "elapsed_seconds": int(now - start)}
                    # Worker only emits known fields, not paths or object names.
                    if phase == "normalizing":
                        try:
                            item = json.loads(line)
                            if isinstance(item.get("converted_files"), int):
                                safe["converted_files"] = item["converted_files"]
                        except (ValueError, TypeError, AttributeError):
                            pass
                    _emit(progress_callback, **safe)
                    last_update = now
            return process.wait(), tail
    finally:
        _stop(process)
        for thread in threads:
            thread.join(timeout=1)


def _download(url: str, path: Path, *, algorithm="sha256", digest=None,
              cancel_event=None, progress_callback=None, max_bytes=1024 * 1024 * 1024):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".download")
    hashed, count = hashlib.new(algorithm), 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "DevHelper-AssetRipper/1"})
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as sink:
            while chunk := response.read(1024 * 1024):
                _check_cancel(cancel_event)
                count += len(chunk)
                if count > max_bytes:
                    raise AssetRipperError("安装文件超过允许大小。", "download_too_large")
                hashed.update(chunk)
                sink.write(chunk)
                _emit(progress_callback, phase="downloading", downloaded_bytes=count)
        if digest and hashed.hexdigest().lower() != digest.lower():
            raise AssetRipperError("安装文件校验失败，未执行下载内容。", "checksum_failed")
        temporary.replace(path)
    except AssetRipperError:
        raise
    except (OSError, ValueError) as exc:
        raise AssetRipperError("下载 AssetRipper 依赖失败，请检查电脑网络后重试。", "download_failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _unpack(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    def checked(name):
        if "\\" in name or ":" in name:
            raise AssetRipperError("安装包包含不安全路径。", "unsafe_archive")
        path = (root / name).resolve()
        if path != root and root not in path.parents:
            raise AssetRipperError("安装包包含越界路径。", "unsafe_archive")
        return path
    total = 0
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zipped:
            for item in zipped.infolist():
                target = checked(item.filename)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise AssetRipperError("安装包包含符号链接。", "unsafe_archive")
                total += item.file_size
                if total > 3 * 1024**3:
                    raise AssetRipperError("安装包展开过大。", "unsafe_archive")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(item) as source, target.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
    else:
        with tarfile.open(archive) as tar:
            for item in tar:
                target = checked(item.name)
                if not (item.isfile() or item.isdir()):
                    raise AssetRipperError("安装包包含不安全条目。", "unsafe_archive")
                total += item.size
                if total > 3 * 1024**3:
                    raise AssetRipperError("安装包展开过大。", "unsafe_archive")
                if item.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(item) as source, target.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
                    target.chmod(item.mode & 0o777)


class AssetRipperAdapter:
    def __init__(self, private_root: Path | str):
        self.root = Path(private_root).expanduser().resolve()
        self.config_path = self.root / "config.json"
        self._setup_lock = threading.Lock()

    def _config(self):
        config = _json(self.config_path, {})
        return config if isinstance(config, dict) else {}

    def _command(self, config=None):
        config = config or self._config()
        value = config.get("executable")
        if not value or not Path(value).is_file():
            raise AssetRipperError("AssetRipper 尚未安装，请先运行电脑端 setup 或配置 CLI 执行程序。", "setup_required")
        if Path(value).name.lower().startswith("assetripper.gui"):
            raise AssetRipperError("这里需要 AssetRipper-CLI 执行程序，请运行 setup；GUI 不能作为导出命令使用。", "cli_required")
        if Path(value).suffix.lower() == ".dll":
            dotnet = config.get("dotnet") or self._find_dotnet()
            if not dotnet:
                raise AssetRipperError("此 CLI 需要 .NET 10，请配置 dotnet 或运行 setup。", "dotnet_required")
            return [str(dotnet), str(value)]
        if os.name != "nt" and not os.access(value, os.X_OK):
            raise AssetRipperError("AssetRipper 执行文件没有运行权限，请重新安装或配置。", "launch_failed")
        return [str(value)]

    def status(self) -> dict:
        config = self._config()
        try:
            self._command(config)
            available = True
        except AssetRipperError:
            available = False
        try:
            rid = _host_rid()
        except AssetRipperError:
            rid = "unsupported"
        python = config.get("python")
        return {"available": available, "configured": bool(config.get("executable")),
                "backend": "AssetRipper-CLI", "revision": config.get("revision", "external"),
                "host": rid, "setup_required": not available,
                "tuanjie_ready": bool(config.get("python_verified") and python and Path(python).is_file()),
                "supported_variants": list(VARIANTS), "supported_profiles": list(PROFILES)}

    def configure(self, config: dict) -> dict:
        if not isinstance(config, dict) or set(config) - {"executable", "dotnet", "python"}:
            raise AssetRipperError("AssetRipper 配置字段无效。", "invalid_configuration")
        current = self._config()
        for name, value in config.items():
            if value is None or value == "":
                current.pop(name, None)
                continue
            if not isinstance(value, (str, Path)) or not Path(value).expanduser().is_file():
                raise AssetRipperError("配置必须指向电脑上现有的执行文件。", "invalid_configuration")
            # Resolve resource paths, but retain a venv interpreter's launcher
            # path: resolving its symlink selects the base Python and silently
            # loses the venv's installed UnityPy dependency.
            selected = Path(value).expanduser()
            current[name] = str(selected.absolute() if name == "python" else selected.resolve())
        if "executable" in config:
            current["revision"] = "external"
        self.root.mkdir(parents=True, exist_ok=True)
        if "python" in config:
            current["python_verified"] = False
            if current.get("python"):
                code, _ = _run([current["python"], "-c", "import UnityPy; from UnityPy.helpers.Tpk import get_typetree_node"],
                               cwd=self.root, log=self.root / "setup-logs/python-check.log", timeout=30)
                if code:
                    raise AssetRipperError("配置的 Python 缺少 UnityPy，请安装依赖或运行 setup。", "python_dependency_failed")
                current["python_verified"] = True
        _write_json(self.config_path, current)
        return self.status()

    def _find_dotnet(self):
        candidates = [shutil.which("dotnet"), self.root / "dotnet" / ("dotnet.exe" if os.name == "nt" else "dotnet"),
                      Path.home() / ".dotnet" / ("dotnet.exe" if os.name == "nt" else "dotnet")]
        if os.name == "nt":
            candidates.append(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "dotnet/dotnet.exe")
        else:
            candidates.extend([Path("/usr/local/share/dotnet/dotnet"), Path("/opt/homebrew/share/dotnet/dotnet")])
        for path in candidates:
            if path and Path(path).is_file():
                return str(path)
        return None

    def _install_dotnet(self, cancel_event, progress_callback):
        metadata = self.root / "downloads/dotnet-releases.json"
        _download("https://builds.dotnet.microsoft.com/dotnet/release-metadata/10.0/releases.json", metadata,
                  cancel_event=cancel_event, progress_callback=progress_callback, max_bytes=10 * 1024**2)
        data = _json(metadata, {})
        rid = _host_rid()
        selected = None
        for release in data.get("releases", []):
            for sdk in release.get("sdks", [release.get("sdk", {})]):
                if sdk.get("version") == SDK_VERSION:
                    selected = next((f for f in sdk.get("files", []) if f.get("rid") == rid and
                                     f.get("name", "").endswith((".zip", ".tar.gz"))), None)
        if not selected or not selected.get("hash"):
            raise AssetRipperError("未找到匹配电脑的 .NET SDK，请安装 .NET 10 SDK 后重试。", "dotnet_required")
        url = selected["url"]
        if not url.startswith(("https://builds.dotnet.microsoft.com/", "https://download.visualstudio.microsoft.com/")):
            raise AssetRipperError(".NET 下载来源校验失败。", "untrusted_download")
        archive = self.root / "downloads" / ("dotnet.zip" if url.endswith(".zip") else "dotnet.tar.gz")
        _download(url, archive, algorithm="sha512", digest=selected["hash"], cancel_event=cancel_event,
                  progress_callback=progress_callback)
        _unpack(archive, self.root / "dotnet")
        return str(self.root / "dotnet" / ("dotnet.exe" if os.name == "nt" else "dotnet"))

    def setup(self, *, source_dir=None, dotnet=None, python=None, install_dotnet=False,
              install_tuanjie=True, cancel_event=None, progress_callback=None) -> dict:
        if not self._setup_lock.acquire(blocking=False):
            raise AssetRipperError("AssetRipper 正在安装，请稍后再试。", "setup_busy")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            log_root = self.root / "setup-logs"
            sdk = str(dotnet) if dotnet else self._find_dotnet()
            if not sdk and install_dotnet:
                sdk = self._install_dotnet(cancel_event, progress_callback)
            if not sdk:
                raise AssetRipperError("需要 .NET 10 SDK（至少 10.0.200）。请安装 SDK，或使用 setup --install-dotnet 自动安装到私有目录。", "dotnet_required")
            code, lines = _run([sdk, "--version"], cwd=self.root, log=log_root / "sdk.log", timeout=30,
                               cancel_event=cancel_event)
            versions = [tuple(map(int, match)) for line in lines for match in re.findall(r"^(\d+)\.(\d+)\.(\d+)", line)]
            if (code or not versions or max(versions) < (10, 0, 200)) and install_dotnet:
                sdk = self._install_dotnet(cancel_event, progress_callback)
                code, lines = _run([sdk, "--version"], cwd=self.root, log=log_root / "sdk.log", timeout=30,
                                   cancel_event=cancel_event)
                versions = [tuple(map(int, match)) for line in lines for match in re.findall(r"^(\d+)\.(\d+)\.(\d+)", line)]
            if code or not versions or max(versions) < (10, 0, 200):
                raise AssetRipperError("需要 .NET 10 SDK（至少 10.0.200），当前配置不满足编译要求。", "dotnet_required")
            if source_dir:
                source = Path(source_dir).expanduser().resolve()
                project = source / "src/AssetRipper.Tools.ExportRunner/AssetRipper.Tools.ExportRunner.csproj"
                if not project.is_file():
                    raise AssetRipperError("源码目录缺少 AssetRipper-CLI 导出工程。", "invalid_source")
                # Never patch or generate build files in the selected original.
                copied = self.root / "sources/local-cli"
                if copied.exists():
                    shutil.rmtree(copied)
                shutil.copytree(source, copied, ignore=shutil.ignore_patterns(".git", "artifacts", "bin", "obj"))
                source = copied
                revision = "local-source"
            else:
                source = self.root / "sources" / f"AssetRipper-CLI-{CLI_REVISION}"
                archive = self.root / "downloads/cli-source.zip"
                if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA256:
                    _download(f"https://codeload.github.com/MeikoMei16/AssetRipper-CLI/zip/{CLI_REVISION}", archive,
                              digest=SOURCE_SHA256, cancel_event=cancel_event,
                              progress_callback=progress_callback, max_bytes=20 * 1024**2)
                if not source.exists():
                    _unpack(archive, self.root / "sources")
                revision = CLI_REVISION
            # Patch the audited archive traversal dependency before building.
            # https://github.com/advisories/GHSA-6c8g-7p36-r338 (fixed in 0.48.0).
            dependency = source / "vendor/assetripper/Source/AssetRipper.IO.Files/AssetRipper.IO.Files.csproj"
            if dependency.is_file():
                text = dependency.read_text(encoding="utf-8-sig")
                text = text.replace('Include="SharpCompress" Version="0.47.3"', 'Include="SharpCompress" Version="0.48.0"')
                dependency.write_text(text, encoding="utf-8")
            runtime = self.root / "runtime/cli"
            _emit(progress_callback, phase="building", host=_host_rid())
            project = source / "src/AssetRipper.Tools.ExportRunner/AssetRipper.Tools.ExportRunner.csproj"
            code, _ = _run([sdk, "publish", str(project), "-c", "Release", "-r", _host_rid(),
                            "--self-contained", "true", "-o", str(runtime), "-p:PublishAot=false",
                            "--nologo", "-v", "quiet"], cwd=source, log=log_root / "build.log",
                           cancel_event=cancel_event, progress_callback=progress_callback, phase="building")
            executable = runtime / ("AssetRipper.Tools.ExportRunner.exe" if os.name == "nt" else "AssetRipper.Tools.ExportRunner")
            if code or not executable.is_file():
                raise AssetRipperError("AssetRipper 编译失败，详细日志保存在电脑私有安装目录。", "build_failed")
            notices = runtime / "licenses"
            notices.mkdir(exist_ok=True)
            for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
                if (source / name).is_file():
                    shutil.copyfile(source / name, notices / name)
            bundled = source / "vendor/assetripper/Source/Licenses"
            if bundled.is_dir():
                shutil.copytree(bundled, notices / "dependencies", dirs_exist_ok=True)
            _write_json(runtime / "provenance.json", {"upstream": UPSTREAM_URL, "cli": CLI_URL,
                        "revision": revision, "source_sha256": SOURCE_SHA256 if not source_dir else None,
                        "license": "GPL-3.0", "sharpcompress": "0.48.0",
                        "patch_reason": "GHSA-6c8g-7p36-r338", "host": _host_rid()})
            config = {"executable": str(executable), "revision": revision}
            if install_tuanjie:
                normalizer = self.root / "runtime/python"
                python_path = normalizer / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                if python:
                    python_path = Path(python).expanduser().absolute()
                else:
                    ready_marker = normalizer / ".devhelper-pip-ready"
                    if not python_path.is_file() or not ready_marker.is_file():
                        # uv-managed macOS Python links libpython relative to its
                        # executable. Symlink venv launchers retain that path;
                        # copying the launcher alone makes the venv unstartable.
                        try:
                            venv.EnvBuilder(with_pip=False, clear=True, symlinks=os.name != "nt").create(normalizer)
                        except (OSError, subprocess.SubprocessError) as exc:
                            raise AssetRipperError("无法创建团结转换环境，请检查电脑 Python 安装。", "python_environment_failed") from exc
                        code, _ = _run([str(python_path), "-m", "ensurepip", "--upgrade", "--default-pip"],
                                       cwd=self.root, log=log_root / "python-bootstrap.log", cancel_event=cancel_event,
                                       progress_callback=progress_callback, phase="installing_dependencies")
                        if code:
                            raise AssetRipperError("电脑 Python 缺少 pip 安装组件，请配置已有 UnityPy 的 Python。", "python_environment_failed")
                        ready_marker.write_text("ready\n", encoding="utf-8")
                    code, _ = _run([str(python_path), "-m", "pip", "install", "--disable-pip-version-check",
                                    f"UnityPy=={UNITYPY_VERSION}"], cwd=self.root, log=log_root / "python-install.log",
                                   cancel_event=cancel_event, progress_callback=progress_callback, phase="installing_dependencies")
                    if code:
                        raise AssetRipperError("团结转换依赖安装失败，请检查电脑网络；详细日志保存在私有安装目录。", "python_dependency_failed")
                code, _ = _run([str(python_path), "-c", "import UnityPy; from UnityPy.helpers.Tpk import get_typetree_node"],
                               cwd=self.root, log=log_root / "python-check.log", cancel_event=cancel_event, timeout=30)
                if code:
                    raise AssetRipperError("团结转换解释器缺少 UnityPy，请安装或重新运行 setup。", "python_dependency_failed")
                config["python"] = str(python_path)
                config["python_verified"] = True
            _write_json(self.config_path, config)
            _emit(progress_callback, phase="ready")
            return self.status()
        finally:
            self._setup_lock.release()

    def _detect_variant(self, paths, cancel_event):
        for selected in paths:
            candidates = selected.rglob("*") if selected.is_dir() else [selected]
            for path in candidates:
                _check_cancel(cancel_event)
                if path.is_symlink():
                    raise AssetRipperError("输入中有符号链接，请选择实际资源文件或目录。", "input_symlink")
                if path.is_file():
                    with path.open("rb") as stream:
                        head = stream.read(65536)
                    if is_unity_resource(head) and (re.search(rb"\d{4}\.\d+\.\d+t\d+", head) or b"tuanjie_builtin_extra" in head):
                        return "tuanjie"
        return "unity"

    def export(self, input_paths: list[Path], output_dir: Path, variant="auto", *, profile="full-project",
               cancel_event=None, progress_callback=None) -> dict:
        if variant not in VARIANTS or profile not in PROFILES:
            raise AssetRipperError("资源引擎或导出配置无效。", "invalid_options")
        if not isinstance(input_paths, (list, tuple)) or not input_paths:
            raise AssetRipperError("请选择至少一个电脑资源路径。", "missing_input")
        inputs = []
        for value in input_paths:
            path = Path(value).expanduser()
            if path.is_symlink() or not path.exists():
                raise AssetRipperError("电脑资源路径不存在或为符号链接。", "invalid_input")
            inputs.append(path.resolve())
        output = Path(output_dir).expanduser().resolve()
        if any(output == path or output in path.parents or (path.is_dir() and path in output.parents) for path in inputs):
            raise AssetRipperError("导出目录必须与原始资源分开。", "output_overlaps_input")
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise AssetRipperError("导出目录必须为空，以免覆盖现有文件。", "output_not_empty")
        command = self._command()
        detected = self._detect_variant(inputs, cancel_event)
        if variant == "unity" and detected == "tuanjie":
            raise AssetRipperError("检测到团结资源，请使用 tuanjie 或 auto 转换后导出。", "variant_mismatch")
        resolved_variant = detected if variant == "auto" else variant
        job = self.root / "jobs" / uuid.uuid4().hex
        job.mkdir(parents=True, exist_ok=False)
        prepared = inputs
        normalization = {"converted_files": 0, "changed_objects": 0, "unity_versions": [], "notes": []}
        if resolved_variant == "tuanjie":
            python = self._config().get("python")
            if not python or not Path(python).is_file():
                raise AssetRipperError("团结资源需要 UnityPy 转换依赖，请先运行 setup。", "tuanjie_setup_required")
            worker = Path(__file__).resolve().parent / "assetripper_cli/normalize_tuanjie.py"
            request = job / "normalization-request.json"
            report = job / "normalization-report.json"
            _write_json(request, {"input_paths": [str(p) for p in inputs], "staging": str(job / "input"), "report": str(report)})
            _emit(progress_callback, phase="normalizing")
            code, tail = _run([python, str(worker), str(request)], cwd=job, log=job / "normalization.log",
                             cancel_event=cancel_event, progress_callback=progress_callback, phase="normalizing")
            result = _json(report)
            if code or not result:
                reason = "tuanjie_normalization_failed"
                for line in tail:
                    try:
                        item = json.loads(line)
                        if re.fullmatch(r"[A-Z_]+", item.get("error", "")):
                            reason = item["error"].lower()
                    except (ValueError, AttributeError, TypeError):
                        pass
                raise AssetRipperError("团结转换失败：需要含类型树的 2022.3 资源；无类型树、ACL 动画或微信流式纹理暂不支持。", reason)
            prepared = [Path(p) for p in result["input_paths"]]
            normalization = {k: result[k] for k in normalization}
        staging = job / "output"
        _emit(progress_callback, phase="exporting")
        argv = [*command, "export", *map(str, prepared), "--output", str(staging),
                "--profile", profile, "--keep-output", "--recursive-unpack", "off", "--shard-strategy", "off"]
        code, _ = _run(argv, cwd=job, log=job / "export.log", cancel_event=cancel_event,
                       progress_callback=progress_callback)
        manifest = _json(staging / "export-manifest.json")
        if code or not isinstance(manifest, dict):
            raise AssetRipperError("AssetRipper 导出失败，详细日志已保留在电脑私有任务目录。", "export_failed")
        jobs = manifest.get("Jobs", [])
        asset_count = sum(int((row.get("Inventory") or {}).get("AssetCount", 0)) for row in jobs)
        failed = int(manifest.get("OutcomeSummary", {}).get("ExportFailedCollectionCount", 0))
        if asset_count <= 0:
            raise AssetRipperError("未读到可导出的 Unity 对象，请检查资源包、依赖与引擎版本。", "no_assets")
        _check_cancel(cancel_event)
        # Re-check immediately before publishing the finished result. shutil.move
        # supports another local disk; no existing result is ever deleted.
        if output.exists() and any(output.iterdir()):
            raise AssetRipperError("导出期间目标目录已被占用，请选择新的空目录。", "output_not_empty")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.rmdir()
        shutil.move(str(staging), str(output))
        files = [path for path in output.rglob("*") if path.is_file()]
        result = {"status": "partial" if failed else "completed", "backend": "AssetRipper-CLI",
                  "variant": resolved_variant, "profile": profile, "file_count": len(files),
                  "total_bytes": sum(path.stat().st_size for path in files), "asset_count": asset_count,
                  "failed_collections": failed, "output_dir": str(output),
                  "manifest_path": str(output / "export-manifest.json"), "normalization": normalization}
        _write_json(job / "result.json", result)
        _emit(progress_callback, phase=result["status"], file_count=len(files), asset_count=asset_count)
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DevHelper 电脑 AssetRipper 导出工具")
    parser.add_argument("--private-root", type=Path, default=default_private_root())
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    configure = subparsers.add_parser("configure")
    configure.add_argument("--executable", required=True)
    configure.add_argument("--dotnet")
    configure.add_argument("--python")
    setup = subparsers.add_parser("setup")
    setup.add_argument("--source-dir", type=Path)
    setup.add_argument("--dotnet")
    setup.add_argument("--python")
    setup.add_argument("--install-dotnet", action="store_true")
    setup.add_argument("--without-tuanjie", action="store_true")
    export = subparsers.add_parser("export")
    export.add_argument("--input", action="append", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--variant", choices=VARIANTS, default="auto")
    export.add_argument("--profile", choices=PROFILES, default="full-project")
    args = parser.parse_args(argv)
    adapter = AssetRipperAdapter(args.private_root)
    cancellation = threading.Event()
    def cancel(_signal, _frame):
        cancellation.set()
    previous = signal.signal(signal.SIGINT, cancel)
    def progress(event):
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
    try:
        if args.command == "status":
            result = adapter.status()
        elif args.command == "configure":
            result = adapter.configure({key: getattr(args, key) for key in ("executable", "dotnet", "python") if getattr(args, key)})
        elif args.command == "setup":
            result = adapter.setup(source_dir=args.source_dir, dotnet=args.dotnet, python=args.python,
                                   install_dotnet=args.install_dotnet, install_tuanjie=not args.without_tuanjie,
                                   cancel_event=cancellation, progress_callback=progress)
        else:
            result = adapter.export(args.input, args.output, args.variant, profile=args.profile,
                                    cancel_event=cancellation, progress_callback=progress)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except AssetRipperError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 130 if exc.code == "cancelled" else 1
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
