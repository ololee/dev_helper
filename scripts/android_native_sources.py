"""Explicit, source-only release list for DevHelper's Android transfer component.

This is an integration source snapshot, not a complete Android distribution.
Never replace this list with a recursive copy of app/, a build directory, or SDKs.
"""
from __future__ import annotations

from pathlib import Path


ANDROID_FILES = (
    "app/src/main/java/cn/ololee/devhelper/netdisk/BaiduBinaryFileClient.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeTransferCloud.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeTransferEngine.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFileTransferRuntime.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFileTransferService.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFileTransferTools.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFileTransferHttpEndpoint.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFileTransferPresentation.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFileManagerActivity.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFileAtomicMove.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFileOperationEngine.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFilePreview.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFilePreviewPolicy.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFilePreviewRuntime.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/NativeFilePreviewTools.kt",
    "app/src/main/cpp/native_file_ops.cpp",
    "app/src/main/cpp/CMakeLists.txt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/BaiduNetdiskRuntime.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/BaiduNetdiskTools.kt",
    "app/src/main/java/cn/ololee/devhelper/netdisk/BaiduNetdiskActivity.kt",
    "app/src/main/java/cn/ololee/devhelper/assets/ResourceLibraryActivity.kt",
    "app/src/main/java/cn/ololee/devhelper/knowledge/NativeManagementActivity.kt",
    "app/src/main/java/cn/ololee/devhelper/knowledge/NativeManagementScreens.kt",
    "app/src/main/java/cn/ololee/devhelper/knowledge/KnowledgeHttpEndpoint.java",
    "app/src/test/java/cn/ololee/devhelper/netdisk/NativeTransferEngineTest.kt",
    "app/src/test/java/cn/ololee/devhelper/netdisk/BaiduBinaryFileClientTest.kt",
    "app/src/test/java/cn/ololee/devhelper/netdisk/NativeFileTransferToolsTest.kt",
    "app/src/test/java/cn/ololee/devhelper/netdisk/NativeFileTransferPresentationTest.kt",
    "app/src/test/java/cn/ololee/devhelper/netdisk/NativeFileOperationEngineTest.kt",
    "app/src/test/java/cn/ololee/devhelper/netdisk/NativeFilePreviewPolicyTest.kt",
    "app/src/test/java/cn/ololee/devhelper/netdisk/NativeCloudDirectoryPolicyTest.kt",
    "app/src/test/java/cn/ololee/devhelper/netdisk/NativeRenameCopyTest.kt",
    "app/src/androidTest/java/cn/ololee/devhelper/netdisk/NativeFileTransferDeviceTest.kt",
    "app/src/androidTest/java/cn/ololee/devhelper/netdisk/NativeFileOperationDeviceTest.kt",
    "app/src/androidTest/java/cn/ololee/devhelper/netdisk/NativeFilePreviewDeviceTest.kt",
    "app/build.gradle.kts",
    "gradle/libs.versions.toml",
    "app/src/main/AndroidManifest.xml",
)
ALLOWED_SUFFIXES = frozenset({".kt", ".java", ".kts", ".xml", ".toml", ".cpp"})


def release_files(source: Path) -> dict[str, Path]:
    """Return validated public paths without copying or modifying any file.

    In the development tree, source is the desktop/ directory alongside app/.
    In a standalone published desktop checkout, reuse its android-native/ snapshot.
    Both layouts use exactly the same immutable, source-only file list.
    """
    source = Path(source).expanduser().absolute()
    development = source.parent
    marker = development / "app/src/main/AndroidManifest.xml"
    root = development if marker.is_file() else source / "android-native"
    if root.is_symlink() or not root.is_dir():
        raise ValueError("The Android integration source root is missing or is a symlink")
    resolved_root = root.resolve(strict=True)
    files: dict[str, Path] = {}
    for relative in ANDROID_FILES:
        part = Path(relative)
        if part.is_absolute() or ".." in part.parts or (part.suffix not in ALLOWED_SUFFIXES and part.name != "CMakeLists.txt"):
            raise ValueError("The Android release list contains a non-source path: " + relative)
        path = root / part
        current = root
        for component in part.parts:
            current = current / component
            if current.is_symlink():
                raise ValueError("An Android integration source path is a symlink: " + relative)
        if not path.is_file():
            raise ValueError("A required Android integration source file is missing: " + relative)
        if not path.resolve(strict=True).is_relative_to(resolved_root):
            raise ValueError("An Android integration source escapes its source root: " + relative)
        public_path = "android-native/" + part.as_posix()
        if public_path in files:
            raise ValueError("An Android integration source path is duplicated: " + relative)
        files[public_path] = path
    return files
