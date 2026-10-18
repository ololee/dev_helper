"""Explorer navigation uses generated source files and temporary SQLite catalogs."""
import io
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

try:
    from desktop.asset_library import AssetLibrary, AssetLibraryError
    from desktop.catalog_snapshot import CatalogSnapshot
    from desktop.explorer_browse import browse_assets
except ImportError:
    from asset_library import AssetLibrary, AssetLibraryError
    from catalog_snapshot import CatalogSnapshot
    from explorer_browse import browse_assets


class ExplorerBrowseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'game'
        self.source.mkdir()
        self.library = AssetLibrary(self.root / 'index')
        self.project = self.library.register_project(self.source, 'Generated game')

    def tearDown(self):
        self.library.close()
        self.temp.cleanup()

    def write(self, relative, contents='fixture'):
        target = self.source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents.encode() if isinstance(contents, str) else contents)
        return target

    def image(self, relative):
        contents = io.BytesIO()
        Image.new('RGB', (8, 6), '#ff8040').save(contents, 'PNG')
        return self.write(relative, contents.getvalue())

    def scan(self):
        job = self.library.start_scan(self.project['id'])
        for _ in range(1000):
            state = self.library.job(job['jobId'])
            if state['state'] not in ('queued', 'running'):
                self.assertEqual(state['state'], 'completed', state)
                return
            time.sleep(.01)
        self.fail('generated fixture scan timed out')

    def browse(self, **kwargs):
        return browse_assets(self.library, project_id=self.project['id'], **kwargs)

    def test_root_projects_and_project_metadata(self):
        self.write('a/b/file.txt')
        self.write('root.txt')
        self.scan()
        empty_source = self.root / 'empty'
        empty_source.mkdir()
        empty = self.library.register_project(empty_source, 'A empty project')
        result = browse_assets(self.library)
        self.assertEqual([x['name'] for x in result['items']], ['A empty project', 'Generated game'])
        self.assertEqual([x['childCount'] for x in result['items']], [0, 2])
        self.assertEqual(result['items'][1]['id'], 'project:' + self.project['id'])
        self.assertEqual(result['items'][0]['projectId'], empty['id'])
        self.assertEqual(result['breadcrumbs'], [])
        self.assertTrue(all(x['type'] == 'folder' and x['isDir'] for x in result['items']))
        self.assertTrue(all(x['path'] == '' and x['bytes'] is None for x in result['items']))
        self.assertNotIn(str(self.source), str(result))

    def test_immediate_children_breadcrumbs_and_recursive_counts(self):
        for path in ('root.txt', 'a/direct.txt', 'a/b/nested.txt', 'a/b/deeper/more.txt', 'ab/other.txt'):
            self.write(path)
        self.scan()
        root = self.browse()
        self.assertEqual([x['name'] for x in root['items']], ['a', 'ab', 'root.txt'])
        self.assertEqual(root['items'][0]['childCount'], 3)
        result = self.browse(path='a/')
        self.assertEqual(result['path'], 'a')
        self.assertEqual([x['name'] for x in result['items']], ['b', 'direct.txt'])
        self.assertEqual(result['items'][0]['path'], 'a/b')
        self.assertEqual(result['items'][0]['childCount'], 2)
        self.assertEqual(result['items'][0]['childCountMode'], 'recursive-files')
        self.assertEqual(result['items'][1]['path'], 'a/direct.txt')
        self.assertEqual(result['items'][1]['type'], 'file')
        self.assertFalse(result['items'][1]['isDir'])
        self.assertEqual(result['items'][1]['projectName'], 'Generated game')
        self.assertEqual([x['name'] for x in result['breadcrumbs']], ['Generated game', 'a'])
        nested = self.browse(path='a/b/deeper')
        self.assertEqual([x['path'] for x in nested['breadcrumbs']], ['', 'a', 'a/b', 'a/b/deeper'])
        self.assertEqual(root['items'][0]['id'], self.browse()['items'][0]['id'])

    def test_like_metacharacters_and_case_do_not_leak_neighbor_directories(self):
        for path in ('a_bc/own.txt', 'axbc/not-own.txt', 'percent%/own.txt', 'percent-other/not-own.txt',
                     'a/own.txt', 'ab/not-own.txt', 'A/not-own.txt', '中文/自己.txt'):
            self.write(path)
        self.scan()
        # macOS commonly uses a case-insensitive source volume; a cloud catalog
        # can still contain case-distinct paths from another machine.
        with self.library._db() as db:
            db.execute("UPDATE assets SET path='A/not-own.txt' WHERE path='a/not-own.txt'")
        for directory, expected in (('a_bc', 'own.txt'), ('percent%', 'own.txt'), ('a', 'own.txt'),
                                    ('A', 'not-own.txt'), ('中文', '自己.txt')):
            with self.subTest(directory=directory):
                result = self.browse(path=directory)
                self.assertEqual(result['total'], 1)
                self.assertEqual(result['items'][0]['path'], directory + '/' + expected)

    def test_missing_directories_empty_projects_and_invalid_project(self):
        self.write('one.txt')
        self.scan()
        result = self.browse(path='not-indexed/yet')
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['path'], 'not-indexed/yet')
        with self.assertRaises(AssetLibraryError) as missing:
            browse_assets(self.library, project_id='00000000-0000-4000-8000-000000000000')
        self.assertEqual(missing.exception.status, 404)

    def test_kind_filter_prunes_folders_and_counts_matching_descendants(self):
        self.image('mixed/red.png')
        self.image('mixed/deep/orange.png')
        self.write('mixed/readme.txt')
        self.write('text/readme.txt')
        self.image('direct.png')
        self.scan()
        root = self.browse(kind='image')
        self.assertEqual([x['name'] for x in root['items']], ['mixed', 'direct.png'])
        self.assertEqual(root['items'][0]['childCount'], 2)
        self.assertEqual([x['name'] for x in self.browse(path='mixed', kind='image')['items']], ['deep', 'red.png'])
        self.assertEqual(self.browse(path='text', kind='image')['total'], 0)
        self.assertEqual(browse_assets(self.library, kind='image')['items'][0]['childCount'], 3)
        self.assertEqual(browse_assets(self.library, kind='model')['items'], [])

    def test_global_sort_paging_and_folders_first_in_both_directions(self):
        for index in range(125):
            self.write('file-%03d.txt' % index, 'x' * (index + 1))
        self.write('z-folder/inside.txt')
        self.write('a-folder/inside.txt')
        self.scan()
        descending = self.browse(sort='name', order='desc', limit=3)
        self.assertEqual([x['name'] for x in descending['items']], ['z-folder', 'a-folder', 'file-124.txt'])
        self.assertEqual(descending['total'], 127)
        page = self.browse(sort='name', order='asc', offset=102, limit=5)
        self.assertEqual([x['name'] for x in page['items']], ['file-%03d.txt' % i for i in range(100, 105)])
        largest = self.browse(sort='size', order='desc', offset=2, limit=2)
        self.assertEqual([x['name'] for x in largest['items']], ['file-124.txt', 'file-123.txt'])
        self.assertEqual(self.browse(offset=10000)['items'], [])

    def test_modified_uses_max_descendant_time(self):
        self.write('older/one.txt')
        self.write('newer/one.txt')
        self.write('newer/deep/two.txt')
        self.scan()
        with self.library._db() as db:
            db.execute('UPDATE assets SET mtime_ns=1000000000')
            db.execute("UPDATE assets SET mtime_ns=9000000000 WHERE path='newer/deep/two.txt'")
        result = self.browse(sort='modified', order='desc')
        self.assertEqual([x['name'] for x in result['items']], ['newer', 'older'])
        self.assertEqual([x['modifiedAt'] for x in result['items']], [9.0, 1.0])

    def test_metadata_annotations_embeddings_and_availability_preserved(self):
        self.image('images/icon.png')
        self.scan()
        item = self.library.list_assets()['items'][0]
        self.library.save_annotation(item['id'], {'source': 'manual', 'purpose': 'test button'})
        self.library.import_embeddings(self.project['id'], 'test-v1', [{'assetId': item['id'], 'vector': [1., 0.]}])
        before = self.library.asset(item['id'])
        after = self.browse(path='images')['items'][0]
        for key, value in before.items():
            self.assertEqual(after[key], value, key)

    def test_cloud_restored_catalog_browses_without_source_disk_reads(self):
        self.image('assets/ui/button.png')
        self.write('assets/code/button.cs')
        self.scan()
        snapshot = self.root / 'catalog.sqlite3'
        exported = CatalogSnapshot(self.library).export(snapshot)
        shutil.rmtree(self.source)
        restored = AssetLibrary(self.root / 'restored')
        try:
            CatalogSnapshot(restored).restore(snapshot, expected_sha256=exported['sha256'])
            with (patch.object(restored, '_analyze', side_effect=AssertionError('source analysis')),
                  patch.object(restored, 'open_file', side_effect=AssertionError('source open')),
                  patch.object(restored, 'file_path', side_effect=AssertionError('source lookup')),
                  patch.object(Path, 'iterdir', side_effect=AssertionError('disk enumeration'))):
                root = browse_assets(restored)
                folder = browse_assets(restored, project_id=self.project['id'], path='assets')
                files = browse_assets(restored, project_id=self.project['id'], path='assets/ui')
            self.assertEqual(root['items'][0]['childCount'], 2)
            self.assertEqual([x['name'] for x in folder['items']], ['code', 'ui'])
            self.assertEqual(files['items'][0]['availability'], 'remote')
            self.assertEqual(files['items'][0]['name'], 'button.png')
        finally:
            restored.close()

    def test_rejects_path_escape_host_paths_controls_and_invalid_parameters(self):
        for value in ('../a', 'a/../b', './a', 'a/./b', '/tmp', '//server/share', 'C:/a', 'a\\b',
                      'a//b', 'a\x00b', 'a\nb', 'a\x7fb', 'a\x85b', [], None, 'a' * 4097):
            with self.subTest(path=repr(value)), self.assertRaises(AssetLibraryError):
                self.browse(path=value)
        with self.assertRaises(AssetLibraryError):
            browse_assets(self.library, path='a')
        for parameters in ({'project_id': 'bad'}, {'sort': 'name; DROP TABLE assets'}, {'sort': []},
                           {'order': 'sideways'}, {'kind': []}, {'kind': 'folder'}, {'limit': 0},
                           {'limit': 501}, {'limit': True}, {'offset': -1}, {'offset': True}):
            with self.subTest(parameters=parameters), self.assertRaises(AssetLibraryError):
                browse_assets(self.library, **parameters)


if __name__ == '__main__':
    unittest.main()
