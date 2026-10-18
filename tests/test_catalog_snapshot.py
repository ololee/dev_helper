"""Portable catalogs are tested only with generated fixtures and temporary indexes."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from desktop.asset_library import AssetLibrary, AssetLibraryError
    from desktop.catalog_snapshot import CatalogSnapshot, APPLICATION_ID, COLUMNS
except ImportError:
    from asset_library import AssetLibrary, AssetLibraryError
    from catalog_snapshot import CatalogSnapshot, APPLICATION_ID, COLUMNS


class CatalogSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.game = self.root / 'game'
        self.game.mkdir()
        (self.game / 'texture.png').write_bytes(b'not a decoded image')
        (self.game / 'texture.png.meta').write_text('guid: ' + 'a' * 32)
        (self.game / 'unit.prefab').write_text('texture: {fileID: 1, guid: ' + 'a' * 32 + ', type: 3}')
        self.source = AssetLibrary(self.root / 'source-index')
        self.dest = AssetLibrary(self.root / 'dest-index')
        self.project = self.source.register_project(self.game, 'Generated game')
        self.scan(self.source, self.project['id'])
        self.items = {x['path']: x for x in self.source.list_assets()['items']}
        self.asset = self.items['texture.png']
        self.model = 'asset-metadata-text-v1:test:0123456789'
        self.source.save_annotation(self.asset['id'], {'source': 'manual', 'label': '红色按钮', 'categories': ['gui'], 'purpose': '测试菜单'})
        self.source.import_embeddings(self.project['id'], self.model, [{'assetId': self.asset['id'], 'vector': [1, 0, 0]}])
        self.location = {'assetId': self.asset['id'], 'provider': 'baidu-netdisk', 'fsId': '1234567', 'path': '/apps/test/game/texture.png', 'sha256': self.asset['sha256'], 'bytes': self.asset['bytes'], 'pathConfirmed': True, 'state': 'completed'}
        self.profile = {'space': self.model, 'baseUrl': 'https://models.example/v1', 'model': 'test-embedding', 'dimensions': 3, 'inputVersion': 'asset-metadata-text-v1', 'mode': 'chat'}
        self.file = self.root / 'catalog.sqlite3'

    def tearDown(self):
        self.source.close()
        self.dest.close()
        self.temp.cleanup()

    def scan(self, library, project):
        job = library.start_scan(project)
        for _ in range(1000):
            result = library.job(job['jobId'])
            if result['state'] not in ('queued', 'running'):
                self.assertEqual(result['state'], 'completed', result)
                return
            time.sleep(.01)
        self.fail('fixture scan timeout')

    def export(self, **kwargs):
        return CatalogSnapshot(self.source).export(self.file, locations=[self.location], query_profiles=[self.profile], **kwargs)

    def restore(self, summary):
        return CatalogSnapshot(self.dest).restore(self.file, expected_sha256=summary['sha256'])

    def mutate(self, sql, values=()):
        with sqlite3.connect(self.file) as db:
            db.execute(sql, values)
        return hashlib.sha256(self.file.read_bytes()).hexdigest()

    def test_roundtrip_search_vectors_dependencies_no_source_or_old_root(self):
        summary = self.export()
        shutil.rmtree(self.game)
        with patch.object(self.dest, '_analyze', side_effect=AssertionError('must not analyze originals')):
            result = self.restore(summary)
        self.assertEqual(result['assetsImported'], 3)
        self.assertEqual(result['vectorsSkipped'], 0)
        self.assertEqual(summary['counts']['embeddings'], 1)
        self.assertEqual(summary['uploadedAssetCount'], 1)
        restored = self.dest.asset(self.asset['id'])
        self.assertEqual(restored['availability'], 'remote')
        self.assertEqual(restored['remoteLocations'][0]['fsId'], '1234567')
        self.assertEqual(self.dest.list_assets(query='红色按钮')['total'], 1)
        self.assertEqual(self.dest.query_embedding([1, 0, 0], self.model)['items'][0]['id'], self.asset['id'])
        dep = self.dest.dependencies(self.items['unit.prefab']['id'])
        self.assertEqual(dep['edges'][0]['to'], self.asset['id'])
        self.assertEqual(self.dest.query_profiles(), [self.profile])
        project = self.dest.project(self.project['id'], include_root=True)
        self.assertTrue(project['options']['remoteBacked'])
        self.assertNotEqual(project['root'], str(self.game))
        for operation in (lambda: self.dest.file_path(self.asset['id']), lambda: self.dest.start_scan(self.project['id'])):
            with self.assertRaises(AssetLibraryError) as failure:
                operation()
            self.assertEqual(failure.exception.status, 409)
        with self.assertRaises(AssetLibraryError) as failure:
            with self.dest.open_file(self.asset['id']):
                self.fail('remote only original was opened')
        self.assertEqual(failure.exception.status, 409)
        raw = self.file.read_bytes()
        self.assertNotIn(str(self.game).encode(), raw)
        with sqlite3.connect(self.file) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            self.assertEqual(tables, set(COLUMNS))
            self.assertEqual(db.execute('PRAGMA application_id').fetchone()[0], APPLICATION_ID)
            self.assertEqual(db.execute('SELECT vector FROM embeddings').fetchone()[0], struct.pack('<3f', 1, 0, 0))

    def test_on_demand_cache_verifies_hash_and_keeps_metadata_unchanged(self):
        summary = self.export()
        self.restore(summary)
        before = self.dest.asset(self.asset['id'])
        self.dest.cache_file(self.asset['id'], self.game / 'texture.png')
        cached = self.dest.asset(self.asset['id'])
        self.assertEqual(cached['availability'], 'cached')
        self.assertEqual(cached['sha256'], before['sha256'])
        with self.dest.open_file(self.asset['id']) as handle:
            self.assertEqual(handle.read(), b'not a decoded image')
        path = self.dest.file_path(self.asset['id'])
        self.assertTrue(path.is_relative_to(self.dest.root))
        path.write_bytes(b'tampered')
        with self.assertRaises(AssetLibraryError):
            self.dest.file_path(self.asset['id'])
        self.dest.cache_file(self.asset['id'], self.game / 'texture.png')
        self.assertEqual(self.dest.file_path(self.asset['id']).read_bytes(), b'not a decoded image')
        bad = self.root / 'bad.bin'
        bad.write_bytes(b'wrong')
        with self.assertRaises(AssetLibraryError):
            self.dest.cache_file(self.asset['id'], bad)
        self.assertFalse(list((self.dest.root / 'files').glob('.incoming-*')))

    def test_republish_inherits_remote_locations_and_profiles(self):
        summary = self.export()
        self.restore(summary)
        again = self.root / 'republished.sqlite3'
        result = CatalogSnapshot(self.dest).export(again)
        self.assertEqual(result['counts']['locations'], 1)
        self.assertEqual(result['counts']['embeddings'], 1)
        self.assertEqual(result['queryProfiles'], [self.profile])
        self.assertNotIn(b'remote-projects', again.read_bytes())

    def test_restore_idempotent_and_preserves_manual_local_fields(self):
        summary = self.export()
        self.restore(summary)
        self.dest.save_annotation(self.asset['id'], {'source': 'manual', 'purpose': '本机手工笔记'})
        result = self.restore(summary)
        self.assertEqual(result['assetsImported'], 0)
        self.assertEqual(result['assetsMerged'], 3)
        self.assertEqual(result['vectorsSkipped'], 1)
        manual = self.dest.asset(self.asset['id'])['annotations'][0]
        self.assertEqual(manual['purpose'], '本机手工笔记')
        self.assertEqual(manual['categories'], ['gui'])
        self.assertEqual(self.dest.list_assets(query='本机手工笔记')['total'], 1)

    def test_content_conflict_keeps_both_versions_and_cloud_location_correct(self):
        summary = self.export()
        self.restore(summary)
        old = self.dest.asset(self.asset['id'])
        (self.game / 'texture.png').write_bytes(b'new version')
        self.scan(self.source, self.project['id'])
        self.file.unlink()
        changed = self.source.asset(self.asset['id'])
        location = {**self.location, 'sha256': changed['sha256'], 'bytes': changed['bytes'], 'fsId': '8888'}
        summary = CatalogSnapshot(self.source).export(self.file, locations=[location])
        result = self.restore(summary)
        conflict = next(x for x in result['conflicts'] if x['assetId'] == self.asset['id'])
        self.assertNotEqual(conflict['restoredAssetId'], self.asset['id'])
        self.assertEqual(self.dest.asset(self.asset['id'])['sha256'], old['sha256'])
        new = self.dest.asset(conflict['restoredAssetId'])
        self.assertEqual(new['sha256'], changed['sha256'])
        self.assertEqual(new['remoteLocations'][0]['fsId'], '8888')
        self.assertEqual(new['availability'], 'remote')
        self.assertEqual(self.restore(summary)['assetsImported'], 0)

    def test_export_scope_does_not_leak_unselected_projects(self):
        another = self.root / 'another'
        another.mkdir()
        (another / 'private.txt').write_text('not selected')
        project = self.source.register_project(another)
        self.scan(self.source, project['id'])
        summary = self.export(project_ids=[self.project['id']])
        self.assertEqual(summary['counts']['projects'], 1)
        self.assertNotIn(b'not selected', self.file.read_bytes())

    def test_missing_or_wrong_hash_and_symbolic_input_rejected_before_restore(self):
        summary = self.export()
        with self.assertRaises(AssetLibraryError):
            CatalogSnapshot(self.dest).restore(self.file, expected_sha256='0' * 64)
        link = self.root / 'linked.sqlite3'
        link.symlink_to(self.file)
        with self.assertRaises(AssetLibraryError):
            CatalogSnapshot(self.dest).restore(link, expected_sha256=summary['sha256'])
        self.assertEqual(self.dest.projects(), [])

    def test_executable_and_extra_schema_rejected(self):
        summary = self.export()
        for sql in ["CREATE TRIGGER surprise AFTER INSERT ON assets BEGIN DELETE FROM assets; END", 'CREATE VIEW surprise AS SELECT * FROM assets', 'CREATE TABLE surprise(secret TEXT)', 'CREATE VIRTUAL TABLE surprise USING fts5(text)']:
            with self.subTest(sql=sql):
                original = self.file.read_bytes()
                digest = self.mutate(sql)
                with self.assertRaises(AssetLibraryError):
                    CatalogSnapshot(self.dest).restore(self.file, expected_sha256=digest)
                self.assertEqual(self.dest.projects(), [])
                self.file.write_bytes(original)

    def test_nan_wrong_dimensions_zero_and_stale_vectors_rejected(self):
        self.export()
        variants = [(3, struct.pack('<3f', math.nan, 0, 1), self.asset['sha256']), (3, struct.pack('<2f', 1, 0), self.asset['sha256']), (3, struct.pack('<3f', 0, 0, 0), self.asset['sha256']), (3, struct.pack('<3f', 1, 0, 0), 'f' * 64), (5000, b'\0' * 20000, self.asset['sha256'])]
        for dimensions, vector, sha in variants:
            with self.subTest(dimensions=dimensions, sha=sha):
                original = self.file.read_bytes()
                digest = self.mutate('UPDATE embeddings SET dimensions=?,vector=?,sha256=?', (dimensions, vector, sha))
                with self.assertRaises(AssetLibraryError):
                    CatalogSnapshot(self.dest).inspect(self.file, expected_sha256=digest)
                self.file.write_bytes(original)

    def test_traversal_locations_paths_credentials_and_unconfirmed_files_rejected(self):
        for fields in [{'path': '/apps/test/../escape'}, {'pathConfirmed': False}, {'state': 'running'}, {'sha256': 'f' * 64}, {'fsId': '-4'}]:
            with self.subTest(fields=fields):
                with self.assertRaises(AssetLibraryError):
                    CatalogSnapshot(self.source).export(self.file, locations=[{**self.location, **fields}])
                self.assertFalse(self.file.exists())
        with self.assertRaises(AssetLibraryError):
            CatalogSnapshot(self.source).export(self.file, query_profiles=[{**self.profile, 'apiKey': 'must-not-upload'}])
        summary = self.export()
        digest = self.mutate('UPDATE assets SET path=? WHERE id=?', ('../outside', self.asset['id']))
        with self.assertRaises(AssetLibraryError):
            CatalogSnapshot(self.dest).restore(self.file, expected_sha256=digest)

    def test_existing_local_project_new_cloud_asset_is_remote_only(self):
        summary = self.export()
        # Seed an existing project identity with a safe but different local root.
        local = self.root / 'local-game'
        local.mkdir()
        with self.dest._db() as db:
            p = self.source.project(self.project['id'], include_root=True)
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?)', (p['id'], str(local), p['name'], json.dumps(p['options']), p['createdAt'], p['updatedAt'], '{}'))
        self.restore(summary)
        self.assertEqual(self.dest.asset(self.asset['id'])['availability'], 'remote')
        with self.assertRaises(AssetLibraryError) as result:
            self.dest.file_path(self.asset['id'])
        self.assertIn('fetch', str(result.exception))
        self.scan(self.dest, self.project['id'])
        self.assertEqual(self.dest.list_assets()['total'], 3)
        self.assertEqual(self.dest.asset(self.asset['id'])['availability'], 'remote')

    def test_existing_embedding_dimension_conflict_skips_incompatible_space(self):
        summary = self.export()
        local = self.root / 'separate-game'
        local.mkdir()
        (local / 'local.txt').write_text('local resource')
        project = self.dest.register_project(local)
        self.scan(self.dest, project['id'])
        asset = self.dest.list_assets(project_id=project['id'])['items'][0]
        self.dest.import_embeddings(project['id'], self.model, [{'assetId': asset['id'], 'vector': [1, 0]}])
        result = self.restore(summary)
        self.assertEqual(result['vectorsSkipped'], 1)
        self.assertEqual(result['incompatibleVectorSpaces'], [self.model])
        self.assertEqual(self.dest.asset(self.asset['id'])['embeddings'], [])
        self.assertEqual(self.dest.asset(asset['id'])['embeddings'][0]['dimensions'], 2)

    def test_metadata_cannot_override_asset_identity_or_break_ocr_search(self):
        self.export()
        for metadata in [{'id': 'forged'}, {'remoteLocations': [{'fsId': 'unknown'}]}, {'ocr': 'not an object'}, {'ocr': {'status': 'analyzed', 'text': ['not text']}}]:
            with self.subTest(metadata=metadata):
                original = self.file.read_bytes()
                digest = self.mutate('UPDATE assets SET metadata=? WHERE id=?', (json.dumps(metadata), self.asset['id']))
                with self.assertRaises(AssetLibraryError):
                    CatalogSnapshot(self.dest).restore(self.file, expected_sha256=digest)
                self.assertEqual(self.dest.projects(), [])
                self.file.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
