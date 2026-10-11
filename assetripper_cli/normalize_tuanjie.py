"""Private worker: convert Tuanjie 2022.3 type-tree assets for AssetRipper.

Derived from the locally verified type-tree conversion approach, not a version
string substitution. UnityPy is MIT licensed; runtime installs retain notices.
Original inputs are opened read-only. This worker runs in a separate process so
normalization can be cancelled and its Python dependencies stay isolated.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sys


def is_unity_resource(header: bytes) -> bool:
    """Gate engine strings on binary structure; ordinary text is a sidecar."""
    if header.startswith((b"UnityFS\0", b"UnityRaw\0", b"UnityWeb\0", b"UnityArchive\0", b"UnityWebData")):
        return True
    # Serialized files store the format generation as a big-endian uint32 at
    # byte 8 and the endian flag at byte 16 (Unity 3.5+).
    return len(header) >= 20 and 9 <= int.from_bytes(header[8:12], "big") <= 35 and header[16] in (0, 1)


def _signature(node):
    return [(n.m_Level, n.m_Type, n.m_Name, n.m_ByteSize, n.m_MetaFlag)
            for n in node.traverse()]


def normalize(request):
    import UnityPy
    from UnityPy.files import SerializedFile
    from UnityPy.helpers.Tpk import get_typetree_node
    from UnityPy.helpers.UnityVersion import UnityVersion

    converted_files = changed_objects = 0
    versions = set()
    notes = set()
    prepared = []
    destination = Path(request["staging"])
    destination.mkdir(parents=True, exist_ok=True)

    def normalize_serialized(f):
        nonlocal changed_objects
        source_version = f.unity_version
        match = re.fullmatch(r"(2022\.3\.\d+)t\d+(?:\w*)", source_version)
        if not match:
            if re.search(r"\d+t\d+", source_version):
                raise ValueError("UNSUPPORTED_TUANJIE_VERSION")
            return False
        if not f._enable_type_tree or any(t.node is None for t in f.types):
            raise ValueError("TUANJIE_TYPE_TREE_REQUIRED")
        target_version = match[1] + "f1"
        target = UnityVersion.from_str(target_version)
        versions.add(target_version)
        ids = set(f.objects)
        nodes = {}
        for serialized_type in f.types:
            cid = serialized_type.class_id
            # MonoBehaviour contains script-specific fields. Preserve its own
            # embedded tree, bytes and identifiers instead of discarding data.
            if cid == 114 or cid < 0:
                continue
            node = get_typetree_node(cid, target)
            for entry in node.traverse():
                if entry.m_TypeFlags is None:
                    entry.m_TypeFlags = 0
                if entry.m_RefTypeHash is None:
                    entry.m_RefTypeHash = 0
            if _signature(serialized_type.node) != _signature(node):
                nodes[cid] = node
        for obj in f.objects.values():
            cid = obj.type.value
            if cid not in nodes:
                continue
            data = obj.read_typetree()
            if cid == 28:
                if data.get("m_WebStreaming") or data.get("m_DataStreamData", {}).get("size"):
                    raise ValueError("TUANJIE_STREAMED_TEXTURE_UNSUPPORTED")
                if data.get("m_OriginalAssetGuid"):
                    notes.add("Tuanjie original asset GUID metadata is omitted by the standard Unity layout.")
            if cid == 74:
                acl = data.get("m_MuscleClip", {}).get("m_Clip", {}).get("data", {}).get("m_ACLClip", {})
                if acl.get("m_Tracks"):
                    raise ValueError("TUANJIE_ACL_ANIMATION_UNSUPPORTED")
            if cid == 29:
                data.setdefault("m_StaticRenderers", [])
                data.setdefault("m_Portals", [])
            obj.save_typetree(data, nodes=nodes[cid])
            changed_objects += 1
        for serialized_type in f.types:
            if serialized_type.class_id in nodes:
                serialized_type.node = nodes[serialized_type.class_id]
        f.unity_version = target_version
        f.version = target
        # The verified source layout is WebGL. Keep the original platform rather
        # than force all inputs to the WebGL value used by the original sample.
        for external in f.externals:
            if hasattr(external, "path"):
                external.path = external.path.replace("tuanjie_builtin_extra", "unity_builtin_extra").replace(
                    "tuanjie default resources", "unity default resources").replace(
                    "tuanjie_default_resources", "unity default resources")
        if ids != set(f.objects):
            raise ValueError("TUANJIE_OBJECT_ID_CHANGED")
        return True

    def walk_file(f):
        if isinstance(f, SerializedFile):
            return normalize_serialized(f)
        changed = False
        for child in getattr(f, "files", {}).values():
            changed = walk_file(child) or changed
        for field in ("version_engine", "version_player"):
            value = getattr(f, field, "")
            if re.fullmatch(r"2022\.3\.\d+t\d+(?:\w*)", value):
                # A legacy conversion may have changed only serialized metadata
                # and left this bundle header behind. Only accept known standard
                # metadata with an embedded tree; an empty/stripped version or
                # missing tree must never be made to look compatible by a header
                # substitution alone.
                def validate_metadata(child):
                    if isinstance(child, SerializedFile):
                        if not child._enable_type_tree or any(t.node is None for t in child.types):
                            raise ValueError("TUANJIE_TYPE_TREE_REQUIRED")
                        if not re.fullmatch(r"2022\.3\.\d+f\d+(?:\w*)", child.unity_version):
                            raise ValueError("TUANJIE_METADATA_VERSION_REQUIRED")
                    else:
                        for descendant in getattr(child, "files", {}).values():
                            validate_metadata(descendant)
                validate_metadata(f)
                target = re.sub(r"t\d+(?:\w*)$", "f1", value)
                setattr(f, field, target)
                versions.add(target)
                changed = True
        return changed

    def identities(f):
        if isinstance(f, SerializedFile):
            return [sorted(f.objects)]
        return [item for child in getattr(f, "files", {}).values() for item in identities(child)]

    def copy_one(source, target):
        nonlocal converted_files
        target.parent.mkdir(parents=True, exist_ok=True)
        # UnityFS headers and uncompressed serialized metadata carry the engine
        # version. Plain Unity and ordinary sidecar files remain byte identical.
        with source.open("rb") as stream:
            header = stream.read(65536)
        if not is_unity_resource(header):
            shutil.copyfile(source, target)
            return
        if not re.search(rb"2022\.3\.\d+t\d+", header):
            if re.search(rb"\d{4}\.\d+\.\d+t\d+", header):
                raise ValueError("UNSUPPORTED_TUANJIE_VERSION")
            shutil.copyfile(source, target)
            return
        env = UnityPy.load(str(source))
        if len(env.files) != 1:
            raise ValueError("TUANJIE_CONTAINER_UNSUPPORTED")
        container = next(iter(env.files.values()))
        before = identities(container)
        if not walk_file(container):
            raise ValueError("TUANJIE_TYPE_TREE_REQUIRED")
        if isinstance(container, SerializedFile):
            data = container.save()
        else:
            data = container.save(packer="lz4")
        target.write_bytes(data)
        check = next(iter(UnityPy.load(data).files.values()))
        if before != identities(check):
            raise ValueError("TUANJIE_OBJECT_ID_CHANGED")
        # Re-read converted object trees: catches malformed output before export.
        def verify(f):
            if isinstance(f, SerializedFile):
                for obj in f.objects.values():
                    if obj.serialized_type.node:
                        obj.read_typetree()
            else:
                for child in getattr(f, "files", {}).values():
                    verify(child)
        verify(check)
        converted_files += 1
        print(json.dumps({"phase": "normalizing", "converted_files": converted_files}), flush=True)

    for index, value in enumerate(request["input_paths"]):
        source = Path(value)
        root = destination / str(index)
        target = root / source.name
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for child in source.rglob("*"):
                if child.is_symlink():
                    raise ValueError("INPUT_SYMLINK_UNSUPPORTED")
                if child.is_file():
                    copy_one(child, target / child.relative_to(source))
        else:
            copy_one(source, target)
        prepared.append(str(target))
    result = {"input_paths": prepared, "converted_files": converted_files,
              "changed_objects": changed_objects, "unity_versions": sorted(versions),
              "notes": sorted(notes)}
    Path(request["report"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    try:
        normalize(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    except Exception as exc:
        # No resource names, raw object data or local paths leave the worker.
        known = str(exc)
        code = known if re.fullmatch(r"[A-Z_]+", known) else "TUANJIE_NORMALIZATION_FAILED"
        print(json.dumps({"error": code}), file=sys.stderr, flush=True)
        raise SystemExit(2)
