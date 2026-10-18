"""Classified archive plans use generated fixtures, no cloud or credentials."""
import copy
import io
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from desktop.asset_library import AssetLibrary, AssetLibraryError
    from desktop.cloud_archive import build_archive_plan, archive_item_digest, CATEGORY_LABELS
except ImportError:
    from asset_library import AssetLibrary, AssetLibraryError
    from cloud_archive import build_archive_plan, archive_item_digest, CATEGORY_LABELS
from PIL import Image


class CloudArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'game'
        self.source.mkdir()
        self.library = AssetLibrary(self.base / 'index')
        self.project = self.library.register_project(self.source, '测试/游戏:CON')
        self.remote = '/apps/AI相册分类/游戏素材'

    def tearDown(self):
        self.library.close()
        self.temp.cleanup()

    def write(self, relative, data):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def image(self, relative='ui/button.png'):
        data = io.BytesIO()
        Image.new('RGBA', (24, 16), (20, 200, 40, 255)).save(data, 'PNG')
        return self.write(relative, data.getvalue())

    def scan(self, project_id=None):
        job = self.library.start_scan(project_id or self.project['id'], force=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.library.job(job['jobId'])
            if result['state'] not in ('queued', 'running'):
                self.assertEqual(result['state'], 'completed', result)
                return
            time.sleep(.01)
        self.fail('Generated-fixture scan timed out')

    def assets(self):
        return {item['path']: item for item in self.library.list_assets(self.project['id'], limit=500)['items']}

    def plan(self, ids=None, **kwargs):
        return build_archive_plan(self.library, ids or [item['id'] for item in self.assets().values()], self.remote, **kwargs)

    def test_read_only_portable_facts_are_not_usage_guesses(self):
        original = self.image()
        self.scan()
        before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in self.base.rglob('*') if path.is_file()}
        first = self.plan()
        second = self.plan()
        item = first['items'][0]
        self.assertEqual(first, second)
        self.assertEqual(item['primaryCategory'], 'image')
        self.assertEqual(item['classificationSource'], 'analysis')
        self.assertTrue(item['needsReview'])
        self.assertIn('/image-图片/', item['archiveRelativePath'])
        facts = item['metadata']['facts']
        self.assertEqual(facts['usageHypotheses'][0]['label'], 'gui')
        self.assertEqual((facts['image']['width'], facts['image']['height']), (24, 16))
        self.assertEqual(item['metadata']['relativeSourcePath'], 'ui/button.png')
        self.assertNotIn(str(self.base), json.dumps(first))
        self.assertNotIn('root', item['metadata'])
        self.assertNotIn('indexedAt', facts)
        self.assertEqual(before, {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in before})
        self.assertEqual(original.read_bytes(), before[original][1])

    def test_manual_categories_override_ai_without_duplicating_files(self):
        self.image()
        self.scan()
        asset = next(iter(self.assets().values()))
        self.library.save_annotation(asset['id'], {'source': 'ai', 'categories': ['model-texture'], 'confidence': .99})
        self.library.save_annotation(asset['id'], {'source': 'manual', 'categories': ['vfx', 'gui', 'vfx'], 'purpose': '图标'})
        item = self.plan()['items'][0]
        self.assertEqual(item['primaryCategory'], 'gui')
        self.assertEqual(item['classificationSource'], 'manual')
        self.assertEqual(item['categories'], ['gui', 'model-texture', 'vfx'])
        self.assertEqual(len(item['metadata']['annotations']), 2)
        self.assertIn('/gui-界面/', item['archiveRelativePath'])
        self.assertFalse(item['needsReview'])

    def test_empty_manual_categories_do_not_hide_valid_ai(self):
        self.image()
        self.scan()
        asset = next(iter(self.assets().values()))
        self.library.save_annotation(asset['id'], {'source': 'ai', 'categories': ['model-texture'], 'confidence': .9})
        self.library.save_annotation(asset['id'], {'source': 'manual', 'categories': ['', '  '], 'purpose': '用户备注'})
        item = self.plan()['items'][0]
        self.assertEqual((item['primaryCategory'], item['classificationSource']), ('model-texture', 'ai'))
        self.assertEqual(item['metadata']['annotations'][0]['purpose'], '用户备注')

    def test_stale_annotations_and_unknown_or_untrusted_categories_are_not_path_segments(self):
        self.image('opaque.dat')
        self.scan()
        asset = next(iter(self.assets().values()))
        self.library.save_annotation(asset['id'], {'source': 'manual', 'categories': ['gui']})
        self.write('opaque.dat', b'new unknown binary')
        self.scan()
        item = self.plan()['items'][0]
        self.assertEqual(item['primaryCategory'], 'other')
        self.assertEqual(item['metadata']['annotations'], [])
        for category in ('../../escape', 'CON', '$(whoami)', 'http://example.test', 'a\\b', 'unrecognized-ai-class'):
            self.library.save_annotation(asset['id'], {'source': 'ai', 'categories': [category], 'confidence': .99})
            item = self.plan()['items'][0]
            self.assertEqual(item['primaryCategory'], 'other')
            self.assertEqual(item['classificationSource'], 'analysis')
            self.assertTrue(item['needsReview'])
            self.assertEqual(item['categories'], [category])
            self.assertNotIn(category, item['archiveRelativePath'].split('/')[1])
            self.assertNotIn('..', PurePosixPath(item['archiveRelativePath']).parts)

    def test_low_confidence_ai_remains_annotation_not_directory_fact(self):
        self.image()
        self.scan()
        asset = next(iter(self.assets().values()))
        self.library.save_annotation(asset['id'], {'source': 'ai', 'categories': ['vfx'], 'confidence': .4})
        item = self.plan()['items'][0]
        self.assertEqual(item['primaryCategory'], 'image')
        self.assertEqual(item['classificationSource'], 'analysis')
        self.assertEqual(item['metadata']['annotations'][0]['confidence'], .4)
        self.assertTrue(item['needsReview'])

    def test_ai_only_other_falls_back_to_detected_kind_and_requires_review(self):
        self.write('mesh.obj', 'v 0 0 0\n')
        self.scan()
        asset = next(iter(self.assets().values()))
        self.library.save_annotation(asset['id'], {'source': 'ai', 'categories': ['other'], 'confidence': .99})
        item = self.plan()['items'][0]
        self.assertEqual((item['primaryCategory'], item['classificationSource']), ('model', 'analysis'))
        self.assertTrue(item['needsReview'])
        self.assertEqual(item['categories'], ['other'])
        self.library.save_annotation(asset['id'], {'source': 'manual', 'categories': ['other']})
        item = self.plan()['items'][0]
        self.assertEqual((item['primaryCategory'], item['classificationSource']), ('other', 'manual'))
        self.assertTrue(item['needsReview'])

    def test_known_bilingual_categories_and_explicit_other(self):
        self.image()
        self.scan()
        asset = next(iter(self.assets().values()))
        for category, expected in [('界面', 'gui'), ('SFX', 'sound-effect'), ('模型贴图', 'model-texture'), ('other', 'other')]:
            self.library.save_annotation(asset['id'], {'source': 'manual', 'categories': [category]})
            item = self.plan()['items'][0]
            self.assertEqual(item['primaryCategory'], expected)
            self.assertEqual(item['categoryLabel'], CATEGORY_LABELS[expected])
            self.assertEqual(item['needsReview'], expected == 'other')

    def test_duplicates_cross_projects_and_windows_names_are_safe(self):
        self.write('a/same.txt', 'one')
        self.write('b/same.txt', 'two')
        self.write('CON', 'three')
        self.write('a/非法?名*.png', b'unknown')
        self.write('a/' + '长' * 70 + '.txt', 'long')
        self.scan()
        other_root = self.base / 'other'
        other_root.mkdir()
        (other_root / 'same.txt').write_text('other')
        other = self.library.register_project(other_root, self.project['name'])
        self.scan(other['id'])
        ids = [item['id'] for item in self.assets().values()] + [self.library.list_assets(other['id'])['items'][0]['id']]
        items = self.plan(ids)['items']
        paths = [item['archiveRelativePath'] for item in items]
        self.assertEqual(len({path.casefold() for path in paths}), len(paths))
        self.assertEqual(len({path.split('/')[0] for path in paths}), 2)
        for path in paths:
            self.assertEqual(len(PurePosixPath(path).parts), 3)
            for segment in PurePosixPath(path).parts:
                self.assertLessEqual(len(segment.encode()), 240)
                self.assertFalse(re.search(r'[<>:"\\|?*\x00-\x1f]', segment))
                self.assertEqual(segment, segment.rstrip(' .'))
        long_name = next(item for item in items if item['name'].startswith('长'))
        self.assertTrue(long_name['archiveRelativePath'].endswith('.txt'))

    def test_selection_order_is_preserved_while_digest_is_order_independent(self):
        self.write('first.py', 'print(1)')
        self.write('second.py', 'print(2)')
        self.scan()
        ids = [item['id'] for item in self.assets().values()]
        first, second = self.plan(ids), self.plan(ids[::-1])
        self.assertEqual([item['assetId'] for item in first['items']], ids)
        self.assertEqual([item['assetId'] for item in second['items']], ids[::-1])
        self.assertEqual(first['planDigest'], second['planDigest'])
        self.assertEqual(first['summary']['bytes'], len(b'print(1)print(2)'))
        self.assertEqual(first['summary']['fileCount'], 2)
        self.assertEqual(first['summary']['totalBytes'], len(b'print(1)print(2)'))
        self.assertEqual(first['summary']['needsReviewCount'], 0)
        self.assertEqual(first['summary']['categories'], [{'id': 'code', 'label': '代码', 'fileCount': 2, 'bytes': len(b'print(1)print(2)')}])
        self.assertEqual(first['summary']['categoryCounts'], {'code': 2})

    def test_snapshot_has_no_fake_task_id_and_keeps_safe_flat_names(self):
        self.write('source.py', 'print(1)')
        self.scan()
        plan = self.plan(layout='snapshot')
        item = plan['items'][0]
        self.assertEqual(plan['pathPrefix'], '<任务ID>/')
        self.assertEqual(item['archiveRelativePath'], item['assetId'] + '_source.py')
        self.assertNotEqual(plan['planDigest'], self.plan()['planDigest'])

    def test_plan_digest_binds_actual_metadata_path_directory_and_content(self):
        source = self.image()
        self.scan()
        first = self.plan()
        asset_id = first['items'][0]['assetId']
        note = {'source': 'manual', 'categories': ['gui'], 'purpose': '按钮'}
        self.library.save_annotation(asset_id, note)
        classified = self.plan()
        self.assertNotEqual(first['planDigest'], classified['planDigest'])
        self.library.save_annotation(asset_id, note)
        self.assertEqual(classified['planDigest'], self.plan()['planDigest'])
        self.library.save_annotation(asset_id, {**note, 'purpose': '另一个按钮'})
        described = self.plan()
        self.assertNotEqual(classified['planDigest'], described['planDigest'])
        moved = build_archive_plan(self.library, [asset_id], self.remote + '/other')
        self.assertNotEqual(described['planDigest'], moved['planDigest'])
        source.write_bytes(b'changed')
        with self.assertRaises(AssetLibraryError) as caught:
            self.plan()
        self.assertEqual(caught.exception.status, 409)
        self.scan()
        self.assertNotEqual(described['planDigest'], self.plan()['planDigest'])

    def test_item_digest_includes_all_output_metadata_but_not_itself(self):
        self.image()
        self.scan()
        item = self.plan()['items'][0]
        self.assertEqual(item['itemDigest'], archive_item_digest(item))
        edited = copy.deepcopy(item)
        edited['metadata']['facts']['image']['width'] += 1
        self.assertNotEqual(item['itemDigest'], archive_item_digest(edited))
        edited = copy.deepcopy(item)
        edited['itemDigest'] = 'ignored'
        self.assertEqual(item['itemDigest'], archive_item_digest(edited))

    def test_dependencies_preserve_explicit_relations_and_digest_binds_changes(self):
        self.image('texture.png')
        self.write('texture.png.meta', 'guid: ' + 'a' * 32)
        self.write('unit.prefab', 'Texture: {guid: ' + 'a' * 32 + '}\nOther: {guid: ' + 'b' * 32 + '}')
        self.scan()
        assets = self.assets()
        plan = self.plan([assets['unit.prefab']['id']])
        item = plan['items'][0]
        deps = item['metadata']['dependencies']
        self.assertEqual(item['dependencyCount'], 1)
        self.assertEqual((deps['direction'], deps['depth'], deps['scope']), ('out', 1, 'direct'))
        self.assertEqual(deps['referenceCount'], 2)
        self.assertEqual(deps['nodes'][0]['assetId'], assets['texture.png']['id'])
        self.assertEqual(deps['unresolved'][0]['reason'], 'not-indexed-or-external')
        self.assertTrue(deps['countsComplete'])
        self.assertFalse(deps['truncated'])
        self.image('texture.png').write_bytes(b'changed-dependency')
        self.scan()
        self.assertNotEqual(plan['planDigest'], self.plan([assets['unit.prefab']['id']])['planDigest'])

    def test_large_dependency_exports_are_bounded_and_marked(self):
        self.write('unit.prefab', 'unit')
        self.scan()
        asset = next(iter(self.assets().values()))
        fake = {'nodes': [asset], 'edges': [], 'unresolved': [
            {'from': asset['id'], 'namespace': 'unity-guid', 'value': str(index), 'reason': 'not-indexed-or-external'} for index in range(2500)], 'truncated': False}
        with patch.object(self.library, 'dependencies', return_value=fake):
            deps = self.plan()['items'][0]['metadata']['dependencies']
        self.assertEqual(len(deps['unresolved']), 500)
        self.assertEqual(deps['referenceCount'], 2500)
        self.assertTrue(deps['truncated'])
        self.assertTrue(deps['countsComplete'])

    def test_deleted_symlink_and_unscanned_source_are_rejected(self):
        source = self.image()
        self.scan()
        source.unlink()
        with self.assertRaises(AssetLibraryError):
            self.plan()
        source.symlink_to(self.base / 'index' / 'catalog.sqlite3')
        with self.assertRaises(AssetLibraryError):
            self.plan()

    def test_invalid_selection_layout_remote_paths_and_metadata_fail_closed(self):
        self.image()
        self.scan()
        asset = next(iter(self.assets().values()))
        for ids in ([], [asset['id']] * 2, [str(index) for index in range(101)], 'wrong', [None]):
            with self.assertRaises(AssetLibraryError):
                build_archive_plan(self.library, ids, self.remote)
        for value in ('../escape', '/a/../b', '/a/./b', '/a\\b', '/a\x00b', '/' + '长' * 1100):
            with self.assertRaises(AssetLibraryError):
                build_archive_plan(self.library, [asset['id']], value)
        for layout in ('original', 'flat', None):
            with self.assertRaises(AssetLibraryError):
                self.plan(layout=layout)
        invalid = {**asset, 'image': {'width': float('nan')}}
        with patch.object(self.library, 'assets', return_value=[invalid]):
            with self.assertRaises(AssetLibraryError):
                self.plan()


if __name__ == '__main__':
    unittest.main()
