"""Read-only, deterministic plans for classified game-resource cloud archives.

SQLite remains the working catalog. These plans and their JSON metadata are a
portable archive contract; they never open a cloud connection or modify files.
Only controlled categories influence paths. In particular, path-name usage
hypotheses and arbitrary model labels are not asserted as confirmed categories.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
import unicodedata
import uuid

try:
    from .asset_library import AssetLibraryError
except ImportError:
    from asset_library import AssetLibraryError

FORMAT_VERSION = 1
MAX_ASSETS = 100
# This order is also the deterministic primary-category tie-breaker.
CATEGORY_LABELS = {
    'gui': '界面', 'scene': '场景', 'noise': '噪声', 'vfx': '特效',
    'model-texture': '模型贴图', 'model': '模型', 'shader': '着色器',
    'font': '字体', 'material': '材质', 'animation': '动画',
    'music': '音乐', 'sound-effect': '音效', 'spine': 'Spine动画',
    'code': '代码', 'prefab': '预制体', 'image': '图片', 'audio': '音频',
    'video': '视频', 'document': '文档', 'metadata': '元数据',
    'archive': '压缩包', 'other': '待复核',
}
CATEGORY_PRIORITY = {key: index for index, key in enumerate(CATEGORY_LABELS)}
ALIASES = {
    'ui': 'gui', 'interface': 'gui', '界面': 'gui', 'gui': 'gui',
    'scene': 'scene', 'environment': 'scene', '场景': 'scene',
    'noise': 'noise', '噪声': 'noise', 'vfx': 'vfx', 'effect': 'vfx',
    'particle': 'vfx', '特效': 'vfx', '粒子': 'vfx',
    'model-texture': 'model-texture', 'model texture': 'model-texture', '模型贴图': 'model-texture',
    'model': 'model', '模型': 'model', 'shader': 'shader', '着色器': 'shader',
    'font': 'font', '字体': 'font', 'material': 'material', '材质': 'material',
    'animation': 'animation', '动画': 'animation', 'music': 'music', '音乐': 'music',
    'sound-effect': 'sound-effect', 'sound effect': 'sound-effect', 'sfx': 'sound-effect', '音效': 'sound-effect',
    'spine': 'spine', 'spine动画': 'spine', 'code': 'code', '代码': 'code',
    'prefab': 'prefab', '预制体': 'prefab', 'image': 'image', '图片': 'image',
    'audio': 'audio', '音频': 'audio', 'video': 'video', '视频': 'video',
    'document': 'document', '文档': 'document', 'metadata': 'metadata', '元数据': 'metadata',
    'archive': 'archive', '压缩包': 'archive', 'other': 'other', 'file': 'other', '待复核': 'other',
}
FACT_FIELDS = ('kind', 'extension', 'mimeType', 'classification', 'structureAnalysis',
               'contentVersion', 'indexingStatus', 'analysisLimits', 'image', 'model',
               'shader', 'font', 'material', 'animation', 'audio', 'spine', 'ocr', 'usageHypotheses')
ANNOTATION_FIELDS = ('source', 'sha256', 'label', 'categories', 'purpose', 'ocrText', 'evidence', 'confidence')
_WINDOWS_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)
_HASH = re.compile(r'^[0-9a-f]{64}$')
_MD5 = re.compile(r'^[0-9a-f]{32}$')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (TypeError, ValueError, AttributeError):
        raise AssetLibraryError('Invalid archive asset or project ID') from None


def _trim_bytes(value, limit):
    return value.encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def _safe_segment(value, *, byte_limit=160, fallback='resource'):
    """Produce an inert NFC filename segment safe on Windows, macOS and Android."""
    if not isinstance(value, str):
        raise AssetLibraryError('Archive names must be strings')
    value = unicodedata.normalize('NFC', value)
    value = ''.join('_' if unicodedata.category(char).startswith('C') or char in '<>:"/\\|?*' else char for char in value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    value = _trim_bytes(value, byte_limit).rstrip(' .') or fallback
    if _WINDOWS_RESERVED.match(value):
        value = '_' + _trim_bytes(value, byte_limit - 1)
    return value


def _filename(name):
    # Keep the last extension visible when a very long filename must be shortened.
    name = _safe_segment(name, byte_limit=1200)
    extension = PurePosixPath(name).suffix
    if len(extension.encode('utf-8')) > 24:
        extension = ''
    stem = name[:-len(extension)] if extension else name
    return _safe_segment(stem, byte_limit=150 - len(extension.encode('utf-8'))) + extension


def _relative_source(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise AssetLibraryError('Invalid relative source path in archive')
    path = PurePosixPath(value)
    if path.is_absolute() or '\\' in value or ':' in value or any(part in ('', '.', '..') for part in value.split('/')) or any(ord(char) < 32 for char in value):
        raise AssetLibraryError('Archive metadata must use a safe relative source path')
    return value


def _remote_directory(value):
    if not isinstance(value, str) or not value.startswith('/') or len(value.encode('utf-8')) > 3000 or '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AssetLibraryError('Invalid remote archive directory')
    if any(part in ('.', '..') for part in value.split('/')):
        raise AssetLibraryError('Remote archive directory cannot contain . or ..')
    return re.sub('/+', '/', value).rstrip('/') or '/'


def _portable(value, depth=0):
    """Copy bounded JSON facts; reject malformed data instead of silent truncation."""
    if depth > 16:
        raise AssetLibraryError('Archive metadata is too deeply nested')
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 10 ** 20:
            raise AssetLibraryError('Archive metadata number is too large')
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AssetLibraryError('Archive metadata must contain finite numbers')
        return value
    if isinstance(value, str):
        if len(value) > 65536:
            raise AssetLibraryError('Archive metadata text is too long')
        return value
    if isinstance(value, list):
        if len(value) > 2000:
            raise AssetLibraryError('Archive metadata list is too large')
        return [_portable(part, depth + 1) for part in value]
    if isinstance(value, dict):
        if len(value) > 100 or any(not isinstance(key, str) or len(key) > 100 for key in value):
            raise AssetLibraryError('Archive metadata object is too large')
        return {key: _portable(value[key], depth + 1) for key in sorted(value)}
    raise AssetLibraryError('Archive metadata must be JSON-compatible')


def _valid_annotations(asset):
    valid = []
    for annotation in asset.get('annotations', []):
        if (not isinstance(annotation, dict) or annotation.get('source') not in ('manual', 'ai')
                or annotation.get('stale') or annotation.get('sha256') != asset['sha256']):
            continue
        clean = _portable({key: annotation[key] for key in ANNOTATION_FIELDS if key in annotation})
        # Empty category entries are inert; nonempty labels remain portable tags.
        categories = clean.get('categories', [])
        if not isinstance(categories, list) or len(categories) > 50 or any(not isinstance(value, str) or len(value) > 100 for value in categories):
            raise AssetLibraryError('Invalid archive annotation categories')
        clean['categories'] = sorted({value.strip() for value in categories if value.strip()}, key=lambda value: (value.casefold(), value))
        valid.append(clean)
    return sorted(valid, key=lambda value: (value['source'] != 'manual', _canonical(value)))


def _classify(asset, annotations):
    tags = sorted({value for annotation in annotations for value in annotation.get('categories', [])}, key=lambda value: (value.casefold(), value))
    uncertain_categories = False
    for source in ('manual', 'ai'):
        for annotation in annotations:
            if annotation['source'] != source:
                continue
            categories = {ALIASES.get(value.casefold()) for value in annotation.get('categories', [])}
            categories.discard(None)
            if not categories:
                if annotation.get('categories'):
                    uncertain_categories = True
                continue
            if source == 'ai':
                if categories == {'other'}:
                    uncertain_categories = True
                    continue
                categories.discard('other')
                confidence = annotation.get('confidence')
                if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                        or not math.isfinite(confidence) or confidence < .65 or confidence > 1):
                    uncertain_categories = True
                    continue
            category = min(categories, key=CATEGORY_PRIORITY.__getitem__)
            return category, source, tags, category == 'other'
    category = asset.get('kind') if asset.get('kind') in CATEGORY_LABELS else 'other'
    # Filename heuristics in usageHypotheses deliberately do not participate.
    return category, 'analysis', tags, uncertain_categories or category in ('other', 'image', 'audio')


def _dependencies(library, asset_id):
    result = library.dependencies(asset_id, direction='out', depth=1)
    nodes = []
    for node in result.get('nodes', []):
        if node['id'] == asset_id:
            continue
        nodes.append({'assetId': _uuid(node['id']), 'projectId': _uuid(node['projectId']),
                      'relativeSourcePath': _relative_source(node['path']), 'kind': node['kind'],
                      'sha256': node['sha256']})
    edges = result.get('edges', [])
    unresolved = result.get('unresolved', [])
    nodes.sort(key=lambda value: value['assetId'])
    edges.sort(key=_canonical)
    unresolved.sort(key=_canonical)
    truncated = bool(result.get('truncated')) or any(len(values) > 500 for values in (nodes, edges, unresolved))
    return {'direction': 'out', 'depth': 1, 'scope': 'direct',
            'nodes': _portable(nodes[:500]), 'edges': _portable(edges[:500]), 'unresolved': _portable(unresolved[:500]),
            'dependencyCount': len(nodes), 'referenceCount': len(edges) + len(unresolved),
            'exportedDependencyCount': min(len(nodes), 500), 'countsComplete': not result.get('truncated', False),
            'truncated': truncated}


def _plan_item(library, asset):
    identifier = _uuid(asset['id'])
    project_id = _uuid(asset['projectId'])
    if not isinstance(asset.get('sha256'), str) or not _HASH.fullmatch(asset['sha256']) or not isinstance(asset.get('md5'), str) or not _MD5.fullmatch(asset['md5']):
        raise AssetLibraryError('Archive requires current SHA256 and MD5 values; scan the project again', 409)
    if isinstance(asset.get('bytes'), bool) or not isinstance(asset.get('bytes'), int) or asset['bytes'] < 0:
        raise AssetLibraryError('Archive requires the current source size; scan the project again', 409)
    relative = _relative_source(asset['path'])
    project = library.project(project_id)
    # The library validates descriptor signatures and refuses missing/changed/symlink sources.
    # Do not rehash gigabytes or read source bytes just to show a preview.
    library.file_path(identifier)
    annotations = _valid_annotations(asset)
    category, source, categories, needs_review = _classify(asset, annotations)
    dependencies = _dependencies(library, identifier)
    metadata = {
        'assetId': identifier, 'projectId': project_id, 'projectName': project['name'],
        'relativeSourcePath': relative, 'name': asset['name'], 'bytes': asset['bytes'],
        'sha256': asset['sha256'], 'md5': asset['md5'], 'modifiedAt': asset.get('modifiedAt'),
        'facts': _portable({key: asset[key] for key in FACT_FIELDS if key in asset}),
        'annotations': annotations, 'dependencies': dependencies,
        'classification': {'primaryCategory': category, 'categoryLabel': CATEGORY_LABELS[category],
                           'source': source, 'categories': categories, 'needsReview': needs_review},
    }
    metadata = _portable(metadata)
    return {'assetId': identifier, 'projectId': project_id, 'projectName': project['name'],
            'relativeSourcePath': relative, 'sha256': asset['sha256'], 'md5': asset['md5'],
            'bytes': asset['bytes'], 'name': asset['name'], 'primaryCategory': category,
            'categoryLabel': CATEGORY_LABELS[category], 'categories': categories,
            'classificationSource': source, 'needsReview': needs_review,
            'dependencyCount': dependencies['dependencyCount'], 'metadata': metadata}


def archive_plan_item_fingerprint(item):
    """Hash the complete portable preview item, excluding only its own fingerprint."""
    if not isinstance(item, dict):
        raise AssetLibraryError('Invalid archive plan item')
    return hashlib.sha256(_canonical({key: value for key, value in item.items() if key != 'itemDigest'}).encode('utf-8')).hexdigest()


def archive_item_digest(item):
    """Public concise alias for execution-time comparison of rebuilt items."""
    return archive_plan_item_fingerprint(item)


def build_archive_plan(library, asset_ids, remote_directory, *, layout='classified'):
    """Preview selected assets without network traffic, directories, writes or models.

    Rebuild and compare planDigest before upload. Each item's itemDigest can also
    be compared with a rebuilt item to detect changed classifications or metadata.
    snapshot retains the previous flat naming scheme inside a future task folder.
    """
    if (not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= MAX_ASSETS
            or any(not isinstance(value, str) for value in asset_ids) or len(set(asset_ids)) != len(asset_ids)):
        raise AssetLibraryError('每批选择 1 到 100 个不同资源')
    if layout not in ('classified', 'snapshot'):
        raise AssetLibraryError('Archive layout must be classified or snapshot')
    directory = _remote_directory(remote_directory)
    with library.lock:
        assets = library.assets(asset_ids)
        items = []
        for asset in assets:
            item = _plan_item(library, asset)
            filename = item['assetId'] + '_' + _filename(item['name'])
            if layout == 'classified':
                # Full project ID avoids equal/case-folded/truncated project-name collisions.
                project_dir = _safe_segment(item['projectName'], byte_limit=72, fallback='project') + '_' + item['projectId']
                category_dir = item['primaryCategory'] + '-' + item['categoryLabel']
                path = '/'.join((project_dir, category_dir, filename))
            else:
                path = filename
            if len((directory.rstrip('/') + '/' + path).encode('utf-8')) > 4096:
                raise AssetLibraryError('Remote archive path is too long')
            item['archiveRelativePath'] = path
            item['itemDigest'] = archive_plan_item_fingerprint(item)
            items.append(item)
    category_counts = Counter(item['primaryCategory'] for item in items)
    total_bytes = sum(item['bytes'] for item in items)
    review_count = sum(item['needsReview'] for item in items)
    summary = {'fileCount': len(items), 'totalBytes': total_bytes, 'needsReviewCount': review_count,
               'assetCount': len(items), 'bytes': total_bytes, 'needsReview': review_count,
               'projectCount': len({item['projectId'] for item in items}),
               'categories': [{'id': category, 'label': CATEGORY_LABELS[category],
                               'fileCount': category_counts[category],
                               'bytes': sum(item['bytes'] for item in items if item['primaryCategory'] == category)}
                              for category in CATEGORY_LABELS if category in category_counts],
               'categoryCounts': dict(sorted(category_counts.items())),
               'sources': dict(sorted(Counter(item['classificationSource'] for item in items).items()))}
    result = {'formatVersion': FORMAT_VERSION, 'format': 'devhelper-cloud-archive-v1',
              'layout': layout, 'remoteDirectory': directory,
              'pathPrefix': '' if layout == 'classified' else '<任务ID>/',
              'items': items, 'summary': summary}
    digest_payload = {**result, 'items': sorted(items, key=lambda value: value['assetId'])}
    result['planDigest'] = hashlib.sha256(_canonical(digest_payload).encode('utf-8')).hexdigest()
    return result
