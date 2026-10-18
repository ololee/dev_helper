"""Generated binary assets and an offline provider; never contacts cloud APIs."""
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from desktop.asset_library import AssetLibrary, AssetLibraryError
    from desktop.baidu_drive import BaiduDriveError
    from desktop.cross_file_transfer import CrossFileTransfer
    from desktop.local_file_ops import LocalFileOperator
    from desktop.netdisk_file_ops import NetdiskFileOperator
    from desktop.tests.test_netdisk_file_ops import OfflineCloud
except ImportError:
    from asset_library import AssetLibrary, AssetLibraryError
    from baidu_drive import BaiduDriveError
    from cross_file_transfer import CrossFileTransfer
    from local_file_ops import LocalFileOperator
    from netdisk_file_ops import NetdiskFileOperator
    from tests.test_netdisk_file_ops import OfflineCloud


class BinaryCloud(OfflineCloud):
    """The existing paginated metadata fake, with deterministic binary jobs."""
    def __init__(self):
        self.contents = {}
        self.transfer_calls = []
        self.transfer_jobs = {}
        self.progress_reads = 0
        self.cancel_calls = []
        self.fail_upload_number = None
        self.fail_download_number = None
        self.rename_receipt = False
        self.corrupt_download = False
        self.block_transfers = False
        self.after_download = None
        self.upload_count = self.download_count = 0
        super().__init__()

    def add(self, path, content=b'', directory=False):
        identifier = super().add(path, content=content, directory=directory)
        if not directory:
            self.contents[identifier] = bytes(content)
        return identifier

    def mkdir(self, path):
        path = self.remote_path(path, file=True)
        if any(item['path'] == path for item in self.files.values()):
            raise BaiduDriveError('Generated name conflict', 409)
        return self.file_info(self.add(path, directory=True))

    def _transfer(self, operation, size, complete, failed=False):
        identifier = 'generated-transfer-' + str(len(self.transfer_jobs) + 1)
        self.transfer_jobs[identifier] = {'jobId': identifier, 'state': 'running',
            'bytesTransferred': 0, '_size': size, '_complete': complete,
            '_reads': 0, '_failed': failed, 'operation': operation}
        return self._public_job(identifier)

    def _public_job(self, identifier):
        return {k: copy.deepcopy(v) for k, v in self.transfer_jobs[identifier].items()
                if not k.startswith('_')}

    def start_upload(self, local, remote_path, *, name=None, conflict_policy='rename'):
        self.upload_count += 1
        data, target = Path(local).read_bytes(), self.remote_path(remote_path, file=True)
        self.transfer_calls.append({'operation': 'upload', 'path': target,
            'conflict_policy': conflict_policy, 'bytes': len(data)})
        def complete():
            if conflict_policy != 'fail':
                raise AssertionError('Cross uploads must fail on conflict')
            if any(item['path'] == target for item in self.files.values()):
                raise BaiduDriveError('Generated late name conflict', 409)
            identifier = self.add(target, data)
            return {'remotePath': target + '.renamed' if self.rename_receipt else target,
                'pathConfirmed': True, 'fsId': identifier, 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest()}
        return self._transfer('upload', len(data), complete,
                              failed=self.upload_count == self.fail_upload_number)

    def start_download(self, fs_id, local, *, expected_sha256=None, expected_md5=None):
        self.download_count += 1
        identifier = str(fs_id)
        self.transfer_calls.append({'operation': 'download', 'fsId': identifier,
            'expected_sha256': expected_sha256, 'expected_md5': expected_md5})
        data = self.contents[identifier]
        def complete():
            payload = (bytes([data[0] ^ 255]) + data[1:]) if self.corrupt_download and data else data
            # Deliberately do not trust a fake receipt: cross transfer must also
            # hash actual downloaded bytes, including providers reporting success.
            Path(local).write_bytes(payload)
            if self.after_download:
                self.after_download()
            return {'fsId': identifier, 'bytes': len(payload),
                'sha256': hashlib.sha256(payload).hexdigest()}
        return self._transfer('download', len(data), complete,
                              failed=self.download_count == self.fail_download_number)

    def job(self, identifier):
        item = self.transfer_jobs[identifier]
        if item['state'] != 'running':
            return self._public_job(identifier)
        item['_reads'] += 1
        self.progress_reads += 1
        if self.block_transfers or item['_reads'] == 1:
            item['bytesTransferred'] = item['_size'] // 2
        elif item['_failed']:
            item.update(state='failed', error='Generated transfer failure')
        else:
            try:
                item.update(result=item['_complete'](), state='completed',
                            bytesTransferred=item['_size'])
            except BaiduDriveError:
                item.update(state='failed', error='Generated transfer failure')
        return self._public_job(identifier)

    def cancel(self, identifier):
        self.cancel_calls.append(identifier)
        self.transfer_jobs[identifier]['state'] = 'cancelled'
        return self._public_job(identifier)


class CrossFileTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / 'game'
        self.destination = self.base / 'restored'
        self.source.mkdir(); self.destination.mkdir()
        (self.source / 'one.txt').write_bytes(b'original generated asset')
        (self.source / 'two.txt').write_bytes(b'second generated asset')
        (self.source / 'hero.bin').write_bytes(bytes(range(256)) * 3)
        (self.source / 'hero.bin.meta').write_text('fileFormatVersion: 2\nguid: ' + 'a' * 32 + '\n')
        (self.source / 'pack' / 'empty').mkdir(parents=True)
        (self.source / 'pack' / 'binary.dat').write_bytes(b'\x00\xffgenerated-directory-content')
        self.library = AssetLibrary(self.base / 'index')
        self.project = self.library.register_project(self.source, 'Generated binary source')
        self.target = self.library.register_project(self.destination, 'Generated binary destination')
        self.scan(self.project)
        self.assets = {item['path']: item for item in self.library.list_assets(limit=100)['items']}
        self.drive = BinaryCloud()
        self.workspace = SimpleNamespace(library=self.library, drive=self.drive,
            lock=threading.RLock(), backups={}, _save_backups=lambda: None,
            public_error=lambda error: str(error))
        self.local = LocalFileOperator(self.library, self.base / 'local-operations')
        self.netdisk = NetdiskFileOperator(self.workspace)
        self.cross = CrossFileTransfer(self.workspace, self.local, self.netdisk,
                                       self.base / 'cross-staging')
        self.sleep_patch = patch('cross_file_transfer.time.sleep', return_value=None)
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        self.library.close()
        self.temp.cleanup()

    def scan(self, project):
        job = self.library.start_scan(project['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = self.library.job(job['jobId'])
            if current['state'] not in ('queued', 'running'):
                self.assertEqual(current['state'], 'completed', current)
                return
            time.sleep(.005)
        self.fail('Generated index scan did not finish')

    def remote(self, path):
        target = self.drive.remote_path(path, file=True)
        value = next(item for item in self.drive.files.values() if item['path'] == target)
        return {'path': target, 'fsId': str(value['fs_id'])}

    def upload_plan(self, operation='copy', paths=('one.txt',)):
        return self.cross.plan('local', 'netdisk', operation,
            [{'projectId': self.project['id'], 'path': path} for path in paths],
            {'path': '/apps/Fixture/target'})

    def download_plan(self, operation='copy', paths=('source/one.txt',), target_path=''):
        return self.cross.plan('netdisk', 'local', operation,
            [self.remote(path) for path in paths],
            {'projectId': self.target['id'], 'path': target_path})

    def known(self, path='one.txt', cloud_path='source/one.txt', *, vector=True):
        asset = self.assets[path]
        remote = self.remote(cloud_path)
        self.library.save_annotation(asset['id'], {'source': 'manual', 'label': 'Generated GUI icon',
            'categories': ['gui'], 'purpose': 'Generated fixture classification'})
        if vector:
            self.library.import_embeddings(self.project['id'], 'generated-vector-space',
                [{'assetId': asset['id'], 'vector': [1.0, 0.0, 0.0]}])
        with self.library._db() as db:
            db.execute('INSERT INTO remote_locations VALUES(?,?,?,?,?,?)',
                (asset['id'], 'baidu-netdisk', remote['fsId'], remote['path'], asset['sha256'], asset['bytes']))
        return asset

    def assert_sources_preserved(self, result):
        self.assertFalse(result['moveCommitted'], result)
        self.assertTrue(result['sourcePreserved'], result)
        self.assertTrue(all(item['sourcePreserved'] for item in result['items']), result)

    def remote_only_assets(self, paths=('one.txt',)):
        """Use the real upload CUT workflow to produce portable cloud records."""
        for path in paths:
            self.known(path, 'source/' + path)
        moved = self.cross.execute(self.upload_plan('move', paths))
        self.assertEqual(moved['state'], 'completed', moved)
        self.assertTrue(moved['moveCommitted'], moved)
        result = {}
        for outcome in moved['items']:
            identifier = outcome['targetAssetId']
            result[outcome['name']] = self.library.asset(identifier)
        return result

    def assert_classification_vector(self, asset_id):
        with self.library._db() as db:
            note = db.execute('SELECT data FROM annotations WHERE asset_id=?', (asset_id,)).fetchone()
            vector = db.execute('SELECT model,sha256 FROM embeddings WHERE asset_id=?', (asset_id,)).fetchone()
        self.assertIsNotNone(note)
        self.assertEqual(json.loads(note['data'])['categories'], ['gui'])
        self.assertIsNotNone(vector)
        self.assertEqual(vector['model'], 'generated-vector-space')
        self.assertEqual(vector['sha256'], self.library.asset(asset_id)['sha256'])

    def assert_catalog_absent(self, asset_id):
        with self.library._db() as db:
            self.assertIsNone(db.execute('SELECT id FROM assets WHERE id=?', (asset_id,)).fetchone())
            for table in ('annotations', 'embeddings', 'remote_locations', 'local_files', 'asset_storage'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE asset_id=?', (asset_id,)).fetchone()[0], 0)
            if self.library.fts:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM asset_search WHERE id=?', (asset_id,)).fetchone()[0], 0)
            for table in ('refs', 'identifiers'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE source_id=?', (asset_id,)).fetchone()[0], 0)

    def remote_prefab_assets(self):
        paths = ('references.prefab', 'references.prefab.meta')
        contents = (b'%YAML 1.1\n--- !u!1 &1\nGameObject:\n  m_Material: {fileID: 2100000, guid: ' + b'c' * 32 + b', type: 2}\n',
                    b'fileFormatVersion: 2\nguid: ' + b'b' * 32 + b'\n')
        for path, content in zip(paths, contents):
            (self.source / path).write_bytes(content)
            self.drive.add('source/' + path, content)
        self.scan(self.project)
        self.assets = {item['path']: item for item in self.library.list_assets(limit=100)['items']}
        return self.remote_only_assets(paths)

    def test_upload_copy_includes_meta_binary_and_empty_directory(self):
        plan = self.upload_plan(paths=('hero.bin', 'pack'))
        self.assertEqual(plan['fileCount'], 3)
        result = self.cross.execute(plan)
        self.assertEqual(result['state'], 'completed', result)
        self.assert_sources_preserved(result)
        for path in ('hero.bin', 'hero.bin.meta', 'pack/binary.dat'):
            remote = self.remote('target/' + path)
            self.assertEqual(self.drive.contents[remote['fsId']], (self.source / path).read_bytes())
        self.assertTrue(self.drive.file_info(self.remote('target/pack/empty')['fsId'])['isdir'])
        self.assertTrue(all(call['conflict_policy'] == 'fail' for call in self.drive.transfer_calls))
        self.assertEqual(result['bytesTransferred'], plan['bytes'])

    def test_upload_move_readback_verifies_all_files_before_trash(self):
        plan = self.upload_plan('move', ('hero.bin', 'pack'))
        result = self.cross.execute(plan)
        self.assertEqual(result['state'], 'completed', result)
        self.assertTrue(result['moveCommitted'], result)
        self.assertEqual(result['sourceCleanupState'], 'completed')
        self.assertFalse((self.source / 'hero.bin').exists())
        self.assertFalse((self.source / 'hero.bin.meta').exists())
        self.assertFalse((self.source / 'pack').exists())
        downloads = [call for call in self.drive.transfer_calls if call['operation'] == 'download']
        self.assertEqual(len(downloads), 3)
        self.assertTrue(all(call['expected_sha256'] and call['expected_md5'] is None for call in downloads))
        self.assertEqual(result['bytesTransferred'], plan['bytes'] * 2)

    def test_upload_move_retains_searchable_cloud_classification_vectors_and_restorable_source(self):
        original = self.known()
        result = self.cross.execute(self.upload_plan('move'))
        self.assertEqual(result['state'], 'completed', result)
        self.assertTrue(result['moveCommitted'], result)
        target_id = result['items'][0]['targetAssetId']
        self.assertNotEqual(target_id, original['id'])
        cloud_asset = self.library.asset(target_id)
        self.assertEqual(cloud_asset['availability'], 'remote')
        self.assertEqual(cloud_asset['sha256'], original['sha256'])
        cloud_project = self.library.project(cloud_asset['projectId'])
        self.assertTrue(cloud_project['options']['remoteBacked'])
        found = self.library.list_assets(query='Generated GUI icon', limit=100)['items']
        self.assertIn(target_id, {item['id'] for item in found})
        with self.library._db() as db:
            self.assertIsNone(db.execute('SELECT id FROM assets WHERE id=?', (original['id'],)).fetchone())
            annotation = db.execute('SELECT data FROM annotations WHERE asset_id=?', (target_id,)).fetchone()
            vector = db.execute('SELECT model,sha256 FROM embeddings WHERE asset_id=?', (target_id,)).fetchone()
        self.assertEqual(json.loads(annotation['data'])['categories'], ['gui'])
        self.assertEqual(vector['model'], 'generated-vector-space')
        self.assertEqual(vector['sha256'], original['sha256'])
        self.assertEqual(self.library.remote_locations(target_id)[0]['path'], '/apps/Fixture/target/one.txt')
        # Recover the cloud-only asset into a different local project before
        # restoring its original trash ID: all three identities must coexist.
        roundtrip = self.cross.execute(self.download_plan(paths=('target/one.txt',)))
        self.assertEqual(roundtrip['state'], 'completed', roundtrip)
        recovered_id = roundtrip['items'][0]['targetAssetId']
        self.assertNotIn(recovered_id, (target_id, original['id']))
        recovered = self.library.asset(recovered_id)
        self.assertEqual(recovered['projectId'], self.target['id'])
        self.assertEqual(recovered['sha256'], original['sha256'])
        self.assertEqual((self.destination / 'one.txt').read_bytes(), b'original generated asset')
        self.assertEqual(self.drive.transfer_calls[-1]['expected_sha256'], original['sha256'])
        self.assertIsNone(self.drive.transfer_calls[-1]['expected_md5'])
        with self.library._db() as db:
            recovered_note = db.execute('SELECT data FROM annotations WHERE asset_id=?', (recovered_id,)).fetchone()
            recovered_vector = db.execute('SELECT model,sha256 FROM embeddings WHERE asset_id=?', (recovered_id,)).fetchone()
        self.assertEqual(json.loads(recovered_note['data'])['categories'], ['gui'])
        self.assertEqual(recovered_vector['model'], 'generated-vector-space')
        self.assertEqual(recovered_vector['sha256'], original['sha256'])
        trash = next(item for item in self.local.list_trash()['items'] if item['source']['path'] == 'one.txt')
        restored = self.local.restore(trash['trashId'])
        self.assertEqual(restored['state'], 'completed', restored)
        self.assertIn(original['id'], restored['assetIds'])
        self.assertEqual((self.source / 'one.txt').read_bytes(), b'original generated asset')
        self.assertEqual(self.library.asset(original['id'])['sha256'], original['sha256'])
        with self.library._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM embeddings WHERE asset_id IN (?,?)',
                (target_id, original['id'])).fetchone()[0], 2)

    def test_cloud_copy_tree_restores_classification_vectors_and_empty_directory(self):
        asset = self.known()
        self.drive.add('source/empty', directory=True)
        result = self.cross.execute(self.download_plan(paths=('source',)))
        self.assertEqual(result['state'], 'completed', result)
        self.assert_sources_preserved(result)
        self.assertEqual((self.destination / 'source/one.txt').read_bytes(), (self.source / 'one.txt').read_bytes())
        self.assertTrue((self.destination / 'source/empty').is_dir())
        restored = self.library.list_assets(project_id=self.target['id'], limit=100)['items']
        clone = next(item for item in restored if item['path'] == 'source/one.txt')
        self.assertNotEqual(clone['id'], asset['id'])
        with self.library._db() as db:
            note = db.execute('SELECT data FROM annotations WHERE asset_id=?', (clone['id'],)).fetchone()
            vector = db.execute('SELECT model,dimensions,vector,sha256 FROM embeddings WHERE asset_id=?', (clone['id'],)).fetchone()
        self.assertEqual(json.loads(note['data'])['categories'], ['gui'])
        self.assertEqual(vector['model'], 'generated-vector-space')
        self.assertEqual(vector['dimensions'], 3)
        self.assertEqual(vector['sha256'], clone['sha256'])
        self.assertEqual(self.library.remote_locations(clone['id'])[0]['fsId'], self.remote('source/one.txt')['fsId'])

    def test_cloud_move_commits_local_tree_before_provider_cleanup(self):
        self.known()
        self.drive.add('source/empty', directory=True)
        plan = self.download_plan('move', ('source',))
        result = self.cross.execute(plan)
        self.assertEqual(result['state'], 'completed', result)
        self.assertTrue(result['moveCommitted'], result)
        self.assertEqual(result['sourceCleanupState'], 'completed')
        self.assertTrue((self.destination / 'source/one.txt').is_file())
        self.assertTrue((self.destination / 'source/empty').is_dir())
        self.assertFalse(any(item['path'].startswith('/apps/Fixture/source') for item in self.drive.files.values()))
        self.assertEqual(self.drive.calls[-1]['query']['opera'], 'delete')

    def test_second_upload_failure_preserves_every_move_source(self):
        self.drive.fail_upload_number = 2
        result = self.cross.execute(self.upload_plan('move', ('one.txt', 'two.txt')))
        self.assertNotEqual(result['state'], 'completed', result)
        self.assert_sources_preserved(result)
        self.assertEqual(result['sourceCleanupState'], 'retained')
        self.assertTrue((self.source / 'one.txt').is_file())
        self.assertTrue((self.source / 'two.txt').is_file())
        self.assertEqual(self.local.list_trash()['items'], [])

    def test_second_download_failure_commits_nothing_and_keeps_all_cloud_sources(self):
        self.drive.fail_download_number = 2
        result = self.cross.execute(self.download_plan('move', ('source/one.txt', 'source/two.txt')))
        self.assertNotEqual(result['state'], 'completed', result)
        self.assert_sources_preserved(result)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.remote('source/one.txt'); self.remote('source/two.txt')
        self.assertEqual(self.drive.calls, [])

    def test_cancel_inflight_transfer_stops_job_and_preserves_source(self):
        self.drive.block_transfers = True
        result = self.cross.execute(self.upload_plan('move'),
            cancelled=lambda: self.drive.progress_reads > 0)
        self.assertEqual(result['state'], 'cancelled', result)
        self.assert_sources_preserved(result)
        self.assertEqual(len(self.drive.cancel_calls), 1)
        self.assertTrue((self.source / 'one.txt').is_file())

    def test_cancel_before_transfer_makes_no_binary_mutation(self):
        result = self.cross.execute(self.upload_plan('move'), cancelled=lambda: True)
        self.assertEqual(result['state'], 'cancelled', result)
        self.assert_sources_preserved(result)
        self.assertEqual(self.drive.transfer_calls, [])

    def test_renamed_upload_receipt_never_trashes_original(self):
        self.drive.rename_receipt = True
        result = self.cross.execute(self.upload_plan('move'))
        self.assertNotEqual(result['state'], 'completed', result)
        self.assert_sources_preserved(result)
        self.assertEqual(self.local.list_trash()['items'], [])
        self.assertTrue((self.source / 'one.txt').is_file())

    def test_upload_move_rejects_corrupt_readback_even_with_completed_job(self):
        self.drive.corrupt_download = True
        result = self.cross.execute(self.upload_plan('move'))
        self.assertNotEqual(result['state'], 'completed', result)
        self.assert_sources_preserved(result)
        self.assertEqual(self.local.list_trash()['items'], [])
        self.assertTrue((self.source / 'one.txt').is_file())

    def test_provider_opaque_digest_is_not_forwarded_as_local_md5(self):
        item = self.remote('source/one.txt')
        self.drive.files[item['fsId']]['md5'] = 'Opaque-AZ09:provider-token'
        result = self.cross.execute(self.download_plan())
        self.assertEqual(result['state'], 'completed', result)
        call = self.drive.transfer_calls[0]
        self.assertIsNone(call['expected_md5'])
        self.assertIsNone(call['expected_sha256'])
        self.assertEqual((self.destination / 'one.txt').read_bytes(), (self.source / 'one.txt').read_bytes())
        self.assertFalse(result['items'][0]['files'][0]['verifiedAgainstManifest'])

    def test_existing_target_and_symbolic_link_are_rejected(self):
        (self.destination / 'one.txt').write_bytes(b'keep existing')
        with self.assertRaises((AssetLibraryError, BaiduDriveError)):
            self.download_plan()
        self.assertEqual((self.destination / 'one.txt').read_bytes(), b'keep existing')
        outside = self.base / 'outside'
        outside.mkdir()
        (self.destination / 'linked').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((AssetLibraryError, BaiduDriveError, OSError)):
            self.download_plan(target_path='linked')
        (self.source / 'unsafe.bin').symlink_to(self.source / 'one.txt')
        with self.assertRaises((AssetLibraryError, BaiduDriveError, OSError)):
            self.upload_plan(paths=('unsafe.bin',))
        self.assertEqual(self.drive.transfer_calls, [])

    def test_target_created_during_download_is_preserved_without_overwrite(self):
        plan = self.download_plan('move')
        self.drive.after_download = lambda: (self.destination / 'one.txt').write_bytes(b'concurrent user file')
        result = self.cross.execute(plan)
        self.assertNotEqual(result['state'], 'completed', result)
        self.assert_sources_preserved(result)
        self.assertEqual((self.destination / 'one.txt').read_bytes(), b'concurrent user file')
        self.remote('source/one.txt')

    def test_progress_reports_real_binary_bytes_and_verification_total(self):
        observed = []
        plan = self.upload_plan('move', ('one.txt', 'two.txt'))
        result = self.cross.execute(plan, progress=lambda value: observed.append(copy.deepcopy(value)))
        self.assertEqual(result['state'], 'completed', result)
        counts = [value['bytesTransferred'] for value in observed]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[-1], plan['bytes'] * 2)
        self.assertEqual(observed[-1]['completedFiles'], 2)
        self.assertTrue(all(value['totalBytes'] == plan['bytes'] * 2 for value in observed))
        self.assertIn('verifying-upload', {value['phase'] for value in observed})

    def test_cloud_source_changed_before_cleanup_is_not_deleted_by_fresh_plan(self):
        plan = self.download_plan('move')
        source = self.remote('source/one.txt')
        original = self.cross._verify_destinations
        def verify_then_external_edit(transfer_plan, receipts):
            original(transfer_plan, receipts)
            self.drive.contents[source['fsId']] = b'new generated cloud source'
            self.drive.files[source['fsId']].update(size=len(self.drive.contents[source['fsId']]),
                md5='Opaque-new-source-token', server_mtime=1002)
        with patch.object(self.cross, '_verify_destinations', side_effect=verify_then_external_edit):
            result = self.cross.execute(plan)
        self.assert_sources_preserved(result)
        self.assertNotEqual(result['state'], 'completed', result)
        self.assertEqual(self.drive.contents[self.remote('source/one.txt')['fsId']], b'new generated cloud source')
        self.assertEqual((self.destination / 'one.txt').read_bytes(), b'original generated asset')

    def test_local_source_changed_and_reindexed_before_cleanup_remains_intact(self):
        plan = self.upload_plan('move')
        original = self.cross._verify_destinations
        def verify_then_external_edit(transfer_plan, receipts):
            original(transfer_plan, receipts)
            (self.source / 'one.txt').write_bytes(b'new generated local source')
            analyzed = self.library._analyze(self.source, 'one.txt', self.project['options'], self.project['id'])
            with self.library.lock, self.library._db() as db:
                self.library._upsert(db, self.project['id'], analyzed)
        with patch.object(self.cross, '_verify_destinations', side_effect=verify_then_external_edit):
            result = self.cross.execute(plan)
        self.assert_sources_preserved(result)
        self.assertNotEqual(result['state'], 'completed', result)
        self.assertEqual((self.source / 'one.txt').read_bytes(), b'new generated local source')
        self.assertEqual(self.drive.contents[self.remote('target/one.txt')['fsId']], b'original generated asset')

    def test_continuous_progress_keeps_transfer_alive_beyond_original_timeout(self):
        clock = [0.0]
        reads = [0]
        def next_job(identifier):
            reads[0] += 1
            clock[0] += 1000.0
            return {'jobId': identifier, 'state': 'completed' if reads[0] == 5 else 'running',
                'phase': 'uploading', 'bytesTransferred': reads[0] * 2,
                'result': {'verified': True}}
        self.cross._total, self.cross._count = 10, 1
        with patch('cross_file_transfer.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.drive, 'job', side_effect=next_job), \
                patch.object(self.drive, 'cancel') as cancel:
            result = self.cross._wait({'jobId': 'generated-long-transfer', 'state': 'running',
                'phase': 'uploading', 'bytesTransferred': 0}, 'uploading', 10, 'generated.bin')
        self.assertEqual(result, {'verified': True})
        self.assertEqual(clock[0], 5000.0)
        self.assertEqual(self.cross._bytes, 10)
        cancel.assert_not_called()

    def test_unacknowledged_cancel_has_bounded_wait_and_retains_staging(self):
        clock = [0.0]
        def now():
            clock[0] += 1
            return clock[0]
        job = {'jobId': 'generated-pending-transfer', 'state': 'running', 'bytesTransferred': 0}
        self.cross._total, self.cross._count, self.cross._cancelled = 10, 1, lambda: True
        with patch('cross_file_transfer.time.monotonic', side_effect=now), \
                patch.object(self.drive, 'job', return_value=job), \
                patch.object(self.drive, 'cancel', return_value=job) as cancel:
            with self.assertRaises(AssetLibraryError) as caught:
                self.cross._wait(job, 'uploading', 10, 'generated.bin')
        self.assertTrue(getattr(caught.exception, 'unknown', False))
        self.assertTrue(self.cross._retain_staging)
        self.assertLess(clock[0], 30)
        cancel.assert_called_once_with('generated-pending-transfer')

    def test_partial_provider_cleanup_reports_each_source_without_false_move_success(self):
        self.drive.failures['/apps/Fixture/source/two.txt'] = -7
        result = self.cross.execute(self.download_plan('move', ('source/one.txt', 'source/two.txt')))
        self.assertEqual(result['state'], 'completed-with-errors', result)
        self.assertFalse(result['moveCommitted'])
        self.assertFalse(result['sourcePreserved'])
        self.assertEqual(result['sourceCleanupState'], 'partial')
        by_name = {item['name']: item for item in result['items']}
        self.assertFalse(by_name['one.txt']['sourcePreserved'])
        self.assertEqual(by_name['one.txt']['sourceCleanupState'], 'completed')
        self.assertTrue(by_name['two.txt']['sourcePreserved'])
        self.assertEqual(by_name['two.txt']['sourceCleanupState'], 'failed')
        self.assertFalse(any(item['path'] == '/apps/Fixture/source/one.txt' for item in self.drive.files.values()))
        self.remote('source/two.txt')
        self.assertEqual((self.destination / 'one.txt').read_bytes(), b'original generated asset')
        self.assertEqual((self.destination / 'two.txt').read_bytes(), b'second generated asset')

    def test_cloud_cut_prunes_only_confirmed_remote_only_source_after_complete_clone(self):
        source = self.remote_only_assets()['one.txt']
        result = self.cross.execute(self.download_plan('move', ('target/one.txt',)))
        self.assertEqual(result['state'], 'completed', result)
        self.assertTrue(result['moveCommitted'], result)
        self.assertEqual(result['items'][0]['sourceCleanupState'], 'completed')
        self.assert_catalog_absent(source['id'])
        target_id = result['items'][0]['targetAssetId']
        self.assertNotEqual(target_id, source['id'])
        self.assert_classification_vector(target_id)
        self.assertIn(target_id, {item['id'] for item in self.library.list_assets(query='Generated GUI icon')['items']})
        self.assertEqual((self.destination / 'one.txt').read_bytes(), b'original generated asset')

    def test_cloud_cut_preserves_source_with_another_remote_location(self):
        source = self.remote_only_assets()['one.txt']
        other = self.remote('source/one.txt')
        with self.library._db() as db:
            db.execute('INSERT INTO remote_locations VALUES(?,?,?,?,?,?)',
                (source['id'], 'baidu-netdisk', other['fsId'], other['path'], source['sha256'], source['bytes']))
        result = self.cross.execute(self.download_plan('move', ('target/one.txt',)))
        self.assertEqual(result['state'], 'completed', result)
        self.assert_classification_vector(source['id'])
        self.assertEqual(self.library.remote_locations(source['id'])[0]['fsId'], other['fsId'])

    def test_cloud_cut_preserves_valid_cached_source_catalog(self):
        source = self.remote_only_assets()['one.txt']
        cached = self.base / 'generated-cache-source.bin'
        cached.write_bytes(b'original generated asset')
        self.library.cache_file(source['id'], cached)
        result = self.cross.execute(self.download_plan('move', ('target/one.txt',)))
        self.assertEqual(result['state'], 'completed', result)
        self.assert_classification_vector(source['id'])
        with self.library.open_file(source['id']) as handle:
            self.assertEqual(handle.read(), b'original generated asset')

    def test_cloud_cut_keeps_non_remote_only_real_local_original_catalog(self):
        source = self.known()
        result = self.cross.execute(self.download_plan('move'))
        self.assertEqual(result['state'], 'completed', result)
        self.assert_classification_vector(source['id'])
        self.assertEqual((self.source / 'one.txt').read_bytes(), b'original generated asset')
        self.assertEqual(self.library.asset(source['id'])['availability'], 'local')

    def test_partial_cloud_cut_prunes_successful_source_and_keeps_failed_source(self):
        sources = self.remote_only_assets(('one.txt', 'two.txt'))
        self.drive.failures['/apps/Fixture/target/two.txt'] = -7
        result = self.cross.execute(self.download_plan('move', ('target/one.txt', 'target/two.txt')))
        self.assertEqual(result['state'], 'completed-with-errors', result)
        self.assertEqual(result['sourceCleanupState'], 'partial')
        self.assert_catalog_absent(sources['one.txt']['id'])
        self.assert_classification_vector(sources['two.txt']['id'])
        for outcome in result['items']:
            self.assert_classification_vector(outcome['targetAssetId'])

    def test_unknown_cloud_cleanup_never_prunes_source_catalog(self):
        source = self.remote_only_assets()['one.txt']
        self.drive.mode = 'async-pending'
        result = self.cross.execute(self.download_plan('move', ('target/one.txt',)))
        self.assertFalse(result['moveCommitted'], result)
        self.assertNotEqual(result['items'][0]['sourceCleanupState'], 'completed')
        self.assert_classification_vector(source['id'])

    def test_cloud_cut_keeps_source_when_target_classification_differs(self):
        source = self.remote_only_assets()['one.txt']
        original = self.cross._verify_destinations
        def mutate_target(plan, receipts):
            original(plan, receipts)
            target = self.library.list_assets(project_id=self.target['id'])['items'][0]
            self.library.save_annotation(target['id'], {'source': 'manual', 'label': 'Edited local note',
                'categories': ['scene']})
        with patch.object(self.cross, '_verify_destinations', side_effect=mutate_target):
            result = self.cross.execute(self.download_plan('move', ('target/one.txt',)))
        self.assertEqual(result['sourceCleanupState'], 'completed', result)
        self.assert_classification_vector(source['id'])

    def test_cloud_cut_keeps_source_when_target_vector_is_missing(self):
        source = self.remote_only_assets()['one.txt']
        original = self.cross._verify_destinations
        def remove_target_vector(plan, receipts):
            original(plan, receipts)
            target = self.library.list_assets(project_id=self.target['id'])['items'][0]
            with self.library.lock, self.library._db() as db:
                db.execute('DELETE FROM embeddings WHERE asset_id=?', (target['id'],))
        with patch.object(self.cross, '_verify_destinations', side_effect=remove_target_vector):
            result = self.cross.execute(self.download_plan('move', ('target/one.txt',)))
        self.assertEqual(result['sourceCleanupState'], 'completed', result)
        self.assert_classification_vector(source['id'])

    def test_cloud_cut_does_not_garbage_collect_unrelated_orphan_catalog(self):
        sources = self.remote_only_assets(('one.txt', 'two.txt'))
        orphan = sources['two.txt']
        with self.library._db() as db:
            db.execute('DELETE FROM remote_locations WHERE asset_id=?', (orphan['id'],))
        result = self.cross.execute(self.download_plan('move', ('target/one.txt',)))
        self.assertEqual(result['state'], 'completed', result)
        self.assert_catalog_absent(sources['one.txt']['id'])
        self.assert_classification_vector(orphan['id'])
        self.assertEqual(self.library.remote_locations(orphan['id']), [])

    def test_cloud_cut_preserves_prefab_references_and_meta_identifiers_before_pruning(self):
        sources = self.remote_prefab_assets()
        result = self.cross.execute(self.download_plan('move', ('target/references.prefab', 'target/references.prefab.meta')))
        self.assertEqual(result['state'], 'completed', result)
        targets = {item['name']: item['targetAssetId'] for item in result['items']}
        for name, source in sources.items():
            self.assert_catalog_absent(source['id'])
            self.assert_classification_vector(targets[name])
        with self.library._db() as db:
            refs = db.execute('SELECT namespace,value,evidence FROM refs WHERE source_id=?', (targets['references.prefab'],)).fetchall()
            identifiers = db.execute('SELECT namespace,value,target_path FROM identifiers WHERE source_id=?', (targets['references.prefab.meta'],)).fetchall()
        self.assertIn(('unity-guid', 'c' * 32, 'unity-yaml-guid'), [tuple(row) for row in refs])
        self.assertIn(('unity-guid', 'b' * 32, 'references.prefab'), [tuple(row) for row in identifiers])

    def test_cloud_cut_missing_target_references_keeps_source_dependency_metadata(self):
        sources = self.remote_prefab_assets()
        original = self.cross._verify_destinations
        def lose_references(plan, receipts):
            original(plan, receipts)
            target = next(item for item in self.library.list_assets(project_id=self.target['id'])['items']
                          if item['path'] == 'references.prefab')
            with self.library.lock, self.library._db() as db:
                db.execute('DELETE FROM refs WHERE source_id=?', (target['id'],))
        with patch.object(self.cross, '_verify_destinations', side_effect=lose_references):
            result = self.cross.execute(self.download_plan('move', ('target/references.prefab',)))
        self.assertEqual(result['sourceCleanupState'], 'completed', result)
        self.assert_classification_vector(sources['references.prefab']['id'])
        with self.library._db() as db:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM refs WHERE source_id=?', (sources['references.prefab']['id'],)).fetchone()[0], 0)

    def test_cloud_cut_missing_target_identifiers_keeps_source_identifier_metadata(self):
        sources = self.remote_prefab_assets()
        original = self.cross._verify_destinations
        def lose_identifiers(plan, receipts):
            original(plan, receipts)
            target = next(item for item in self.library.list_assets(project_id=self.target['id'])['items']
                          if item['path'] == 'references.prefab.meta')
            with self.library.lock, self.library._db() as db:
                db.execute('DELETE FROM identifiers WHERE source_id=?', (target['id'],))
        with patch.object(self.cross, '_verify_destinations', side_effect=lose_identifiers):
            result = self.cross.execute(self.download_plan('move', ('target/references.prefab.meta',)))
        self.assertEqual(result['sourceCleanupState'], 'completed', result)
        self.assert_classification_vector(sources['references.prefab.meta']['id'])
        with self.library._db() as db:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM identifiers WHERE source_id=?', (sources['references.prefab.meta']['id'],)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
