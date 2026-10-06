"""Render the existing management UI with independent per-device HTTP routes."""
from __future__ import annotations

import html
import json
from pathlib import Path
import re
from typing import Literal


Device = Literal["mac", "android"]
HERE = Path(__file__).resolve().parent


def asset_path(name: str) -> Path:
    """Prefer bundled release assets, with the Android checkout as dev fallback."""
    if name not in ("knowledge.html", "vendor/markdown-it.min.js", "vendor/markdown-it.LICENSE", "vendor/markdown-it.provenance.json", "vendor/README.md"):
        raise ValueError("Unknown DevHelper asset")
    packaged = HERE / "static" / name
    if packaged.is_file():
        return packaged
    source = HERE.parent / "app/src/main/assets" / name
    if source.is_file():
        return source
    raise FileNotFoundError(f"Required DevHelper asset is missing: {name}")


KNOWLEDGE_UI = asset_path("knowledge.html")


def _scope_paths(source: str, device: Device) -> str:
    """Preserve slash escaping in HTML, regex literals and RegExp JS strings."""
    for escape_count in (2, 1, 0):
        separator = "\\" * escape_count + "/"
        for prefix in ("/api/knowledge", "/artifacts"):
            original = prefix.replace("/", separator)
            scoped = (f"/device-api/{device}" + prefix).replace("/", separator)
            # Do not match a less escaped prefix inside a previously rewritten
            # form; /artifacts has only one slash and otherwise matches twice.
            expression = re.compile(r"(?<!\\)" + re.escape(original))
            source = expression.sub(lambda _match: scoped, source)
    return source


def _js_string(value: str) -> str:
    """JSON strings used inside an inline script must not close its HTML tag."""
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _replace_required(source: str, old: str, new: str) -> str:
    if old not in source:
        raise RuntimeError(f"Knowledge UI integration point changed: {old[:80]}")
    return source.replace(old, new)


def _mac_ui(source: str, shared_dir: Path) -> str:
    directory = str(shared_dir.expanduser().resolve())
    source = source.replace("手机", "电脑")
    source = source.replace(
        "电脑保存记忆与向量，电脑和 MCP 随时访问。",
        "记忆与向量保存在这台电脑，浏览器和 MCP 随时访问。",
    ).replace(
        "使用当前电脑地址，在同一网络的电脑上打开此页面。",
        "在同一网络的设备上打开当前地址即可管理。",
    )
    source = _replace_required(
        source,
        'value="/sdcard" placeholder="/sdcard/Download"',
        'value="' + html.escape(directory, quote=True) + '" placeholder="'
        + html.escape(directory + "/文件夹", quote=True) + '"',
    )
    source = source.replace("'/sdcard'", "DEFAULT_DIRECTORY")
    source = source.replace(
        "'请输入完整电脑目录路径，例如 /sdcard/Download。'",
        "'请输入完整电脑目录路径，例如 '+DEFAULT_DIRECTORY+'。'",
    )
    source = _replace_required(
        source,
        "const API=",
        "const MEDIA_EDITING_SUPPORTED=false;\nconst DEFAULT_DIRECTORY=" + _js_string(directory) + ";\nconst API=",
    )
    source = _replace_required(
        source,
        "function renderCaptureControls(){",
        "function renderCaptureControls(){\n"
        " if(!MEDIA_EDITING_SUPPORTED){['capture-screen-insert','capture-record-start','capture-record-stop','capture-record-refresh','capture-record-dismiss'].forEach(id=>$(id).disabled=true);return;}",
    )
    source = _replace_required(
        source,
        "async function requireCaptureService(){",
        "async function requireCaptureService(){\n"
        " if(!MEDIA_EDITING_SUPPORTED)throw new Error('屏幕捕获请切换到已连接的 Android 设备。');",
    )
    source = _replace_required(
        source,
        "function scheduleCapturePoll(){",
        "function scheduleCapturePoll(){\n if(!MEDIA_EDITING_SUPPORTED)return;",
    )
    source = _replace_required(
        source,
        "async function openMediaEditor(raw){",
        "async function openMediaEditor(raw){\n"
        " if(!MEDIA_EDITING_SUPPORTED){toast('当前电脑未启用媒体编辑，请下载后编辑，或切换到 Android 设备。',true);return;}",
    )
    source = _replace_required(
        source,
        "button.type='button';button.addEventListener('click',()=>busy(button,work));return button;",
        "button.type='button';if(!MEDIA_EDITING_SUPPORTED&&['编辑图片','剪辑视频'].includes(label)){button.hidden=true;button.disabled=true;}button.addEventListener('click',()=>busy(button,work));return button;",
    )
    source = _replace_required(
        source,
        "controls.appendChild(edit);info.appendChild(controls);",
        "if(MEDIA_EDITING_SUPPORTED)controls.appendChild(edit);info.appendChild(controls);",
    )
    # Retain every ID and control for the original event bindings. The CSS also
    # prevents later status updates from exposing unsupported capture/edit UI.
    source = _replace_required(
        source,
        "</head>",
        "<style id=\"desktop-capability-style\">"
        '.attachment-actions[aria-label="电脑屏幕捕获"],'
        "#capture-progress,#capture-progress+p,#capture-photo,#choose-desktop-files,"
        "#image-edit-modal,#video-edit-modal,#video-export-banner"
        "{display:none!important}</style>\n</head>",
    )
    return source


def render_knowledge_ui(device: Device, shared_dir: Path) -> str:
    """Read the Android editor and scope all API/media URLs to one device.

    mac keeps memory, vectors, schedules, attachments and shared-folder browsing;
    capture and media editing remain Android capabilities. Neither device gets
    access to the other iframe's API routes.
    """
    if device not in ("mac", "android"):
        raise ValueError("Unknown device; choose mac or android")
    source = KNOWLEDGE_UI.read_text(encoding="utf-8")
    if device == "mac":
        source = _mac_ui(source, Path(shared_dir))
    return _scope_paths(source, device)
