"""File manager regressions use generated SQLite assets and an offline provider."""
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
    from desktop.asset_library import AssetLibrary
    from desktop.baidu_drive import BaiduDriveError, FILE_API
    from desktop.netdisk_file_ops import NetdiskFileOperator
except ImportError:
    from asset_library import AssetLibrary
    from baidu_drive import BaiduDriveError, FILE_API
    from netdisk_file_ops import NetdiskFileOperator


class OfflineCloud:
    root = '/apps/Fixture'

    def __init__(self):
        self.files = {}
        self.lock = threading.RLock()
        self.user = '1001'
        self.client = 'fixture-app'
        self.calls = []
        self.list_calls = []
        self.failures = {}
        self.mode = 'normal'
        self.add('source', directory=True)
        self.add('target', directory=True)
        self.add('source/one.txt', content=b'original generated asset')
        self.add('source/two.txt', content=b'second generated asset')

    def add(self, path, content=b'', directory=False):
        path = self.remote_path(path, file=True)
        fsid = str(max([int(x) for x in self.files] or [100]) + 1)
        self.files[fsid] = {'fs_id': int(fsid), 'path': path, 'server_filename': PurePosixPath(path).name,
            'isdir': int(directory), 'size': len(content), 'server_mtime': 1000, 'local_mtime': 1000,
            'server_ctime': 900, 'md5': None if directory else hashlib.md5(content).hexdigest()}
        return fsid

    def remote_path(self, path='', *, file=False):
        if not isinstance(path, str) or '..' in path.split('/') or '.' in path.split('/') or '\\' in path or '\0' in path:
            raise BaiduDriveError('Invalid scoped path')
        actual = path.rstrip('/') if path.startswith('/') else self.root + ('/' + path if path else '')
        if (actual != self.root and not actual.startswith(self.root + '/')) or file and actual == self.root:
            raise BaiduDriveError('Outside scoped files')
        return actual

    def authorized_user_id(self):
        return self.user

    def status(self):
        return {'clientId': self.client, 'scopeRoot': self.root}

    def file_info(self, fs_id):
        if str(fs_id) not in self.files:
            raise BaiduDriveError('Missing scoped file', 404, provider_code='-9')
        return copy.deepcopy(self.files[str(fs_id)])

    def list_files(self, path='', start=0, limit=1000):
        path = self.remote_path(path)
        self.list_calls.append((path, start))
        if path != self.root and not any(x['path'] == path and x['isdir'] for x in self.files.values()):
            raise BaiduDriveError('Missing directory', 404, provider_code='-9')
        values = sorted([copy.deepcopy(x) for x in self.files.values() if str(PurePosixPath(x['path']).parent) == path], key=lambda x: x['path'])
        # Force two-entry pages to prove callers actually walk the complete list.
        count = min(limit, 2)
        return {'items': values[start:start + count], 'hasMore': len(values) > start + count, 'directoryExists': True}

    def _json(self, method, url, *, query, form, authenticated, retry):
        self.calls.append({'method': method, 'url': url, 'query': query, 'form': form,
                           'authenticated': authenticated, 'retry': retry})
        values = json.loads(form['filelist'])
        if self.mode == 'async-pending':
            return {'errno': 0, 'taskid': 777, 'info': [{'path': value if isinstance(value, str) else value['path'], 'errno': 0} for value in values]}
        infos = []
        for value in values:
            path = value if isinstance(value, str) else value['path']
            code = self.failures.get(path, 0)
            infos.append({'path': path, 'errno': code})
            if code:
                continue
            selected = next(x for x in self.files.values() if x['path'] == path)
            descendants = [copy.deepcopy(x) for x in self.files.values() if x['path'] == path or selected['isdir'] and x['path'].startswith(path + '/')]
            operation = query['opera']
            if operation == 'delete':
                for item in descendants:
                    self.files.pop(str(item['fs_id']))
                continue
            target = str(PurePosixPath(path).parent) + '/' + value['newname'] if operation == 'rename' else value['dest'] + '/' + value['newname']
            for item in descendants:
                replacement = target + item['path'][len(path):]
                if operation == 'copy':
                    fsid = str(max(int(x) for x in self.files) + 1)
                    item['fs_id'] = int(fsid)
                    self.files[fsid] = item
                else:
                    fsid = str(item['fs_id'])
                self.files[fsid].update(path=replacement, server_filename=PurePosixPath(replacement).name, server_mtime=1001)
        if self.mode == 'timeout-after':
            raise BaiduDriveError('Generated network uncertainty', 502, retryable=True)
        if self.mode == 'missing-info':
            return {'errno': 0}
        if self.mode == 'async-applied':
            return {'errno': 0, 'taskid': 777, 'info': infos}
        return {'errno': 0, 'info': infos}


class NetdiskFileOperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'game'
        self.source.mkdir()
        (self.source / 'one.txt').write_bytes(b'original generated asset')
        self.library = AssetLibrary(self.base / 'index')
        self.project = self.library.register_project(self.source, 'Generated file operation fixtures')
        job = self.library.start_scan(self.project['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = self.library.job(job['jobId'])
            if status['state'] not in ('queued', 'running'):
                self.assertEqual(status['state'], 'completed', status)
                break
            time.sleep(.01)
        self.asset = self.library.list_assets(limit=10)['items'][0]
        self.drive = OfflineCloud()
        self.saves = 0
        self.workspace = SimpleNamespace(drive=self.drive, library=self.library, lock=threading.RLock(),
                                         backups={}, _save_backups=self.saved)
        self.operator = NetdiskFileOperator(self.workspace)
        self.one = next(x for x in self.drive.files.values() if x['path'].endswith('/one.txt'))
        self.two = next(x for x in self.drive.files.values() if x['path'].endswith('/two.txt'))
        self.sleep_patch = patch('time.sleep', return_value=None)
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        self.library.close()
        self.tmp.cleanup()

    def saved(self):
        self.saves += 1

    def item(self, info=None, *, with_id=True):
        info = info or self.one
        value = {'path': info['path']}
        if with_id:
            value['fsId'] = str(info['fs_id'])
        return value

    def plan(self, operation='move', **kwargs):
        return self.operator.plan(operation, [self.item()],
            destination={'path': '/apps/Fixture/target'} if operation in ('copy', 'move') else None, **kwargs)

    def record(self, cloud=None):
        cloud = cloud or self.one
        with self.library._db() as db:
            db.execute('INSERT INTO remote_locations VALUES(?,?,?,?,?,?)',
                (self.asset['id'], 'baidu-netdisk', str(cloud['fs_id']), cloud['path'], self.asset['sha256'], self.asset['bytes']))
        self.workspace.backups['generated-backup'] = {'items': [{'assetId': self.asset['id'], 'state': 'completed',
            'result': {'fsId': str(cloud['fs_id']), 'remotePath': cloud['path'], 'sha256': self.asset['sha256'],
                       'bytes': self.asset['bytes'], 'pathConfirmed': True}}]}

    def test_plan_is_scoped_and_read_only_with_confirmed_id_and_full_pagination(self):
        self.drive.add('target/a.txt')
        self.drive.add('target/b.txt')
        self.drive.add('target/c.txt')
        plan = self.operator.plan('copy', [self.item(with_id=False)], destination={'path': '/apps/Fixture/target'})
        self.assertEqual(plan['items'][0]['fsId'], str(self.one['fs_id']))
        self.assertEqual(plan['items'][0]['destination'], '/apps/Fixture/target/one.txt')
        self.assertEqual(plan['fileCount'], 1)
        self.assertFalse(plan['overwrite'])
        self.assertIn(('/apps/Fixture/target', 2), self.drive.list_calls)
        self.assertEqual(self.drive.calls, [])
        self.assertEqual(plan['_account']['userId'], '1001')

    def test_late_page_conflict_fails_without_mutation(self):
        for name in ('a.txt', 'b.txt', 'one.txt'):
            self.drive.add('target/' + name)
        with self.assertRaises(BaiduDriveError) as caught:
            self.plan('copy')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.drive.calls, [])

    def test_move_posts_official_synchronous_fail_contract_and_updates_both_receipts(self):
        self.record()
        result = self.operator.execute(self.plan())
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['items'][0]['state'], 'completed')
        self.assertEqual(len(self.drive.calls), 1)
        call = self.drive.calls[0]
        self.assertEqual(call['method'], 'POST')
        self.assertEqual(call['url'], FILE_API)
        self.assertEqual(call['query'], {'method': 'filemanager', 'opera': 'move'})
        self.assertEqual(call['form']['async'], '0')
        self.assertEqual(call['form']['ondup'], 'fail')
        self.assertFalse(call['retry'])
        self.assertEqual(json.loads(call['form']['filelist']), [{'path': '/apps/Fixture/source/one.txt', 'dest': '/apps/Fixture/target', 'newname': 'one.txt', 'ondup': 'fail'}])
        locations = self.library.remote_locations(self.asset['id'])
        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0]['path'], '/apps/Fixture/target/one.txt')
        backup = self.workspace.backups['generated-backup']['items'][0]['result']
        self.assertEqual(backup['remotePath'], locations[0]['path'])
        self.assertTrue(backup['pathConfirmed'])
        self.assertEqual(self.library.asset(self.asset['id'])['path'], 'one.txt')

    def test_rename_uses_single_object_and_confirms_same_fsid(self):
        self.record()
        result = self.operator.execute(self.plan('rename', name='renamed.txt'))
        self.assertEqual(result['completedCount'], 1)
        body = json.loads(self.drive.calls[0]['form']['filelist'])
        self.assertEqual(body, [{'path': '/apps/Fixture/source/one.txt', 'newname': 'renamed.txt'}])
        self.assertEqual(self.library.remote_locations(self.asset['id'])[0]['path'], '/apps/Fixture/source/renamed.txt')

    def test_trash_removes_database_location_and_disables_legacy_fallback(self):
        self.record()
        plan = self.plan('trash')
        self.assertIn('遵守平台策略', plan['trashPolicy'])
        result = self.operator.execute(plan)
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual(self.drive.calls[0]['query']['opera'], 'delete')
        self.assertEqual(json.loads(self.drive.calls[0]['form']['filelist']), ['/apps/Fixture/source/one.txt'])
        self.assertEqual(self.library.remote_locations(self.asset['id']), [])
        backup = self.workspace.backups['generated-backup']['items'][0]['result']
        self.assertFalse(backup['pathConfirmed'])
        self.assertEqual(backup['locationState'], 'trashed')

    def test_copy_preserves_source_and_adds_verified_new_fsid_location(self):
        self.record()
        result = self.operator.execute(self.plan('copy'))
        self.assertEqual(result['completedCount'], 1)
        locations = self.library.remote_locations(self.asset['id'])
        self.assertEqual(len(locations), 2)
        self.assertEqual({x['path'] for x in locations}, {'/apps/Fixture/source/one.txt', '/apps/Fixture/target/one.txt'})
        self.assertEqual(len({x['fsId'] for x in locations}), 2)
        self.assertTrue(self.workspace.backups['generated-backup']['items'][0]['result']['pathConfirmed'])

    def test_source_destination_and_account_changes_reject_stale_plan(self):
        for change in ('source', 'destination', 'account', 'application'):
            with self.subTest(change=change):
                plan = self.plan()
                if change == 'source':
                    self.one['server_mtime'] += 1
                elif change == 'destination':
                    self.drive.add('target/new.txt')
                elif change == 'account':
                    self.drive.user = '9999'
                else:
                    self.drive.client = 'different-app'
                with self.assertRaises(BaiduDriveError) as caught:
                    self.operator.execute(plan)
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(self.drive.calls, [])

    def test_tampered_expired_and_replayed_plans_never_repeat_mutation(self):
        plan = self.plan()
        damaged = copy.deepcopy(plan)
        damaged['items'][0]['destination'] = '/apps/Fixture/other.txt'
        with self.assertRaises(BaiduDriveError):
            self.operator.execute(damaged)
        with patch('time.time', return_value=plan['_expiresAt'] + 1):
            with self.assertRaises(BaiduDriveError):
                self.operator.execute(plan)
        self.operator.execute(plan)
        with self.assertRaises(BaiduDriveError):
            self.operator.execute(plan)
        self.assertEqual(len(self.drive.calls), 1)

    def test_root_sources_outside_paths_duplicate_targets_and_nested_selections_are_blocked(self):
        bad = [({'path': '/apps/Fixture'},), ({'path': '/other/one.txt'},)]
        for values in bad:
            with self.assertRaises(BaiduDriveError):
                self.operator.plan('trash', list(values))
        folder = next(x for x in self.drive.files.values() if x['path'] == '/apps/Fixture/source')
        with self.assertRaises(BaiduDriveError):
            self.operator.plan('trash', [self.item(folder), self.item()])
        with self.assertRaises(BaiduDriveError):
            self.operator.plan('copy', [self.item(folder)], destination={'path': '/apps/Fixture/source'})
        with self.assertRaises(BaiduDriveError):
            self.operator.plan('copy', [self.item()], destination={'path': '/apps/Fixture/source'})
        self.drive.add('elsewhere', directory=True)
        other = self.drive.file_info(self.drive.add('elsewhere/one.txt'))
        with self.assertRaises(BaiduDriveError):
            self.operator.plan('copy', [self.item(), self.item(other)], destination={'path': '/apps/Fixture/target'})
        self.assertEqual(self.drive.calls, [])

    def test_application_root_is_a_valid_existing_paste_target(self):
        plan = self.operator.plan('copy', [self.item()], destination={'path': '/apps/Fixture'})
        self.assertTrue(plan['_directories'][0]['applicationRoot'])
        result = self.operator.execute(plan)
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual(result['items'][0]['destination'], '/apps/Fixture/one.txt')

    def test_partial_provider_failure_preserves_success_and_per_item_error(self):
        self.drive.failures[self.two['path']] = -8
        plan = self.operator.plan('move', [self.item(), self.item(self.two)], destination={'path': '/apps/Fixture/target'})
        result = self.operator.execute(plan)
        self.assertEqual(result['state'], 'completed-with-errors')
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual(result['failedCount'], 1)
        self.assertEqual([x['state'] for x in result['items']], ['completed', 'failed'])
        self.assertIn('-8', result['items'][1]['error'])
        self.assertEqual(len(self.drive.calls), 1)

    def test_network_uncertainty_does_not_retry_and_stable_id_move_can_be_confirmed(self):
        self.record()
        self.drive.mode = 'timeout-after'
        result = self.operator.execute(self.plan())
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual(len(self.drive.calls), 1)
        self.assertEqual(self.library.remote_locations(self.asset['id'])[0]['path'], '/apps/Fixture/target/one.txt')

    def test_missing_copy_receipt_is_unknown_and_does_not_invent_new_pointer(self):
        self.record()
        self.drive.mode = 'missing-info'
        result = self.operator.execute(self.plan('copy'))
        self.assertEqual(result['unknownCount'], 1)
        self.assertEqual(result['completedCount'], 0)
        self.assertEqual(len(self.library.remote_locations(self.asset['id'])), 1)
        self.assertEqual(len(self.drive.calls), 1)

    def test_async_pending_is_not_completed_and_does_not_replay_mutation(self):
        self.drive.mode = 'async-pending'
        result = self.operator.execute(self.plan('copy'))
        self.assertEqual(result['unknownCount'], 1)
        self.assertTrue(result['providerTaskPending'])
        self.assertEqual(result['completedCount'], 0)
        self.assertEqual(len(self.drive.calls), 1)

    def test_folder_move_updates_descendant_paths_and_keeps_original_ids(self):
        self.record()
        folder = next(x for x in self.drive.files.values() if x['path'] == '/apps/Fixture/source')
        plan = self.operator.plan('move', [self.item(folder)], destination={'path': '/apps/Fixture/target'})
        result = self.operator.execute(plan)
        self.assertEqual(result['completedCount'], 1)
        location = self.library.remote_locations(self.asset['id'])[0]
        self.assertEqual(location['path'], '/apps/Fixture/target/source/one.txt')
        self.assertEqual(location['fsId'], str(self.one['fs_id']))
        self.assertEqual(self.library.asset(self.asset['id'])['path'], 'one.txt')

    def test_folder_copy_adds_only_verified_descendant_receipts(self):
        self.record()
        folder = next(x for x in self.drive.files.values() if x['path'] == '/apps/Fixture/source')
        plan = self.operator.plan('copy', [self.item(folder)], destination={'path': '/apps/Fixture/target'})
        result = self.operator.execute(plan)
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual({x['path'] for x in self.library.remote_locations(self.asset['id'])},
                         {'/apps/Fixture/source/one.txt', '/apps/Fixture/target/source/one.txt'})

    def test_folder_trash_removes_descendant_receipts(self):
        self.record()
        folder = next(x for x in self.drive.files.values() if x['path'] == '/apps/Fixture/source')
        result = self.operator.execute(self.operator.plan('trash', [self.item(folder)]))
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual(self.library.remote_locations(self.asset['id']), [])
        self.assertFalse(self.workspace.backups['generated-backup']['items'][0]['result']['pathConfirmed'])

    def test_async_directory_visibility_does_not_prove_recursive_completion(self):
        self.drive.mode = 'async-applied'
        folder = next(x for x in self.drive.files.values() if x['path'] == '/apps/Fixture/source')
        result = self.operator.execute(self.operator.plan('copy', [self.item(folder)], destination={'path': '/apps/Fixture/target'}))
        self.assertEqual(result['completedCount'], 0)
        self.assertEqual(result['unknownCount'], 1)
        self.assertEqual(len(self.drive.calls), 1)

    def test_incorrect_fsid_and_missing_mtime_cannot_create_plan(self):
        with self.assertRaises(BaiduDriveError):
            self.operator.plan('trash', [{'path': self.one['path'], 'fsId': self.two['fs_id']}])
        del self.one['server_mtime']
        with self.assertRaises(BaiduDriveError) as caught:
            self.plan('trash')
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(self.drive.calls, [])

    def test_rename_rejects_paths_and_multiple_items_without_mutating(self):
        for name in ('../escape', '', 'same/name', 'trailing.', ' leading', 'name\0bad'):
            with self.assertRaises(BaiduDriveError):
                self.plan('rename', name=name)
        with self.assertRaises(BaiduDriveError):
            self.operator.plan('rename', [self.item(), self.item(self.two)], name='new.txt')
        self.assertEqual(self.drive.calls, [])

    def test_batch_rename_previews_each_name_and_posts_once_with_verified_results(self):
        self.record()
        plan = self.operator.plan('rename', [self.item(), self.item(self.two)], name='asset_{index}_{name}{ext}')
        self.assertEqual([x['destination'] for x in plan['items']],
                         ['/apps/Fixture/source/asset_1_one.txt', '/apps/Fixture/source/asset_2_two.txt'])
        self.assertEqual(len(plan['_directories']), 1)
        self.assertEqual(self.drive.calls, [])
        result = self.operator.execute(plan)
        self.assertEqual(result['completedCount'], 2)
        self.assertEqual([x['state'] for x in result['items']], ['completed', 'completed'])
        self.assertEqual(len(self.drive.calls), 1)
        self.assertEqual(self.drive.calls[0]['query']['opera'], 'rename')
        self.assertEqual(json.loads(self.drive.calls[0]['form']['filelist']), [
            {'path': '/apps/Fixture/source/one.txt', 'newname': 'asset_1_one.txt'},
            {'path': '/apps/Fixture/source/two.txt', 'newname': 'asset_2_two.txt'}])
        self.assertEqual(self.library.remote_locations(self.asset['id'])[0]['path'], '/apps/Fixture/source/asset_1_one.txt')
        self.assertEqual(self.workspace.backups['generated-backup']['items'][0]['result']['remotePath'],
                         '/apps/Fixture/source/asset_1_one.txt')

    def test_batch_rename_supports_distinct_original_parents_and_file_extensions(self):
        other = self.drive.file_info(self.drive.add('target/third.JSON', content=b'generated third'))
        plan = self.operator.plan('rename', [self.item(), self.item(other)], name='{name}_v{index}{ext}')
        self.assertEqual([x['destination'] for x in plan['items']],
                         ['/apps/Fixture/source/one_v1.txt', '/apps/Fixture/target/third_v2.JSON'])
        self.assertEqual({x['path'] for x in plan['_directories']}, {'/apps/Fixture/source', '/apps/Fixture/target'})
        result = self.operator.execute(plan)
        self.assertEqual(result['completedCount'], 2)
        self.assertEqual(self.drive.file_info(self.one['fs_id'])['path'], '/apps/Fixture/source/one_v1.txt')
        self.assertEqual(self.drive.file_info(other['fs_id'])['path'], '/apps/Fixture/target/third_v2.JSON')

    def test_batch_rename_generated_collision_is_rejected_before_mutation(self):
        a = self.drive.file_info(self.drive.add('source/same.png', content=b'generated png-like bytes'))
        b = self.drive.file_info(self.drive.add('source/same.jpg', content=b'generated jpg-like bytes'))
        with self.assertRaises(BaiduDriveError) as caught:
            self.operator.plan('rename', [self.item(a), self.item(b)], name='renamed_{name}')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.drive.calls, [])

    def test_batch_rename_checks_existing_conflicts_in_each_parent(self):
        other = self.drive.file_info(self.drive.add('target/third.txt', content=b'generated third'))
        self.drive.add('target/other_a.txt')
        self.drive.add('target/other_b.txt')
        self.drive.add('target/renamed_2.txt')
        with self.assertRaises(BaiduDriveError) as caught:
            self.operator.plan('rename', [self.item(), self.item(other)], name='renamed_{index}{ext}')
        self.assertEqual(caught.exception.status, 409)
        self.assertIn(('/apps/Fixture/target', 2), self.drive.list_calls)
        self.assertEqual(self.drive.calls, [])

    def test_batch_rename_revalidates_all_parent_directories_before_execution(self):
        other = self.drive.file_info(self.drive.add('target/third.txt', content=b'generated third'))
        plan = self.operator.plan('rename', [self.item(), self.item(other)], name='renamed_{index}{ext}')
        self.drive.add('target/appeared-after-plan.txt')
        with self.assertRaises(BaiduDriveError) as caught:
            self.operator.execute(plan)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.drive.calls, [])

    def test_batch_rename_preserves_partial_per_item_provider_failure(self):
        self.drive.failures[self.two['path']] = -8
        plan = self.operator.plan('rename', [self.item(), self.item(self.two)], name='renamed_{index}{ext}')
        result = self.operator.execute(plan)
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual(result['failedCount'], 1)
        self.assertEqual([x['state'] for x in result['items']], ['completed', 'failed'])
        self.assertEqual(self.drive.file_info(self.one['fs_id'])['path'], '/apps/Fixture/source/renamed_1.txt')
        self.assertEqual(self.drive.file_info(self.two['fs_id'])['path'], '/apps/Fixture/source/two.txt')
        self.assertEqual(len(self.drive.calls), 1)

    def test_batch_rename_template_uses_empty_directory_extension_and_full_directory_name(self):
        folder = self.drive.file_info(self.drive.add('game.v1', directory=True))
        other = self.drive.file_info(self.drive.add('game.v2', directory=True))
        plan = self.operator.plan('rename', [self.item(folder), self.item(other)], name='{name}_copy{index}{ext}')
        self.assertEqual([x['destination'] for x in plan['items']],
                         ['/apps/Fixture/game.v1_copy1', '/apps/Fixture/game.v2_copy2'])
        self.assertEqual(self.operator.execute(plan)['completedCount'], 2)

    def test_provider_opaque_digest_is_preserved_verbatim_for_rename_receipts(self):
        self.one['md5'] = 'Gx' * 16
        self.assertNotEqual(self.one['md5'], self.asset['md5'])
        self.record()
        plan = self.plan('rename', name='opaque-renamed.txt')
        self.assertEqual(plan['_sources'][0]['md5'], 'Gx' * 16)
        result = self.operator.execute(plan)
        self.assertEqual(result['completedCount'], 1)
        location = self.library.remote_locations(self.asset['id'])[0]
        self.assertEqual(location['path'], '/apps/Fixture/source/opaque-renamed.txt')
        self.assertEqual(location['fsId'], str(self.one['fs_id']))
        self.assertEqual(location['sha256'], self.asset['sha256'])
        self.assertTrue(self.workspace.backups['generated-backup']['items'][0]['result']['pathConfirmed'])

    def test_opaque_digest_move_keeps_existing_sha_pointer_without_claiming_local_md5(self):
        self.one['md5'] = 'Gx' * 16
        self.record()
        result = self.operator.execute(self.plan('move'))
        self.assertEqual(result['completedCount'], 1)
        location = self.library.remote_locations(self.asset['id'])[0]
        self.assertEqual(location['path'], '/apps/Fixture/target/one.txt')
        self.assertEqual(location['sha256'], self.asset['sha256'])
        self.assertEqual(location['bytes'], self.asset['bytes'])
        self.assertEqual(self.library.asset(self.asset['id'])['md5'], self.asset['md5'])
        self.assertNotIn('md5Verified', json.dumps(result))
        self.assertNotIn('hashVerified', json.dumps(result))

    def test_opaque_digest_copy_adds_confirmed_new_id_with_expected_download_sha(self):
        self.one['md5'] = 'Gx' * 16
        self.record()
        result = self.operator.execute(self.plan('copy'))
        self.assertEqual(result['completedCount'], 1)
        locations = self.library.remote_locations(self.asset['id'])
        self.assertEqual(len(locations), 2)
        self.assertEqual({x['sha256'] for x in locations}, {self.asset['sha256']})
        self.assertEqual(len({x['fsId'] for x in locations}), 2)

    def test_opaque_descendant_digest_is_snapshotted_and_preserves_folder_move_receipt(self):
        self.one['md5'] = 'Gx' * 16
        self.record()
        folder = next(x for x in self.drive.files.values() if x['path'] == '/apps/Fixture/source')
        plan = self.operator.plan('move', [self.item(folder)], destination={'path': '/apps/Fixture/target'})
        self.assertEqual(plan['_locationFingerprints'][str(self.one['fs_id'])]['md5'], 'Gx' * 16)
        result = self.operator.execute(plan)
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual(self.library.remote_locations(self.asset['id'])[0]['path'], '/apps/Fixture/target/source/one.txt')
        self.assertNotIn('locationsNeedVerification', result['items'][0])

    def test_changed_opaque_descendant_digest_rejects_plan_before_folder_mutation(self):
        self.one['md5'] = 'Gx' * 16
        self.record()
        folder = next(x for x in self.drive.files.values() if x['path'] == '/apps/Fixture/source')
        plan = self.operator.plan('move', [self.item(folder)], destination={'path': '/apps/Fixture/target'})
        self.one['md5'] = 'gX' * 16
        with self.assertRaises(BaiduDriveError) as caught:
            self.operator.execute(plan)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.drive.calls, [])

    def test_opaque_digest_mismatch_after_copy_is_unknown_and_adds_no_receipt(self):
        self.one['md5'] = 'Gx' * 16
        self.record()
        original = self.drive._json
        def changed_receipt(*args, **kwargs):
            result = original(*args, **kwargs)
            for value in self.drive.files.values():
                if value['path'] == '/apps/Fixture/target/one.txt':
                    value['md5'] = 'DifferentOpaqueProviderDigest'
            return result
        with patch.object(self.drive, '_json', side_effect=changed_receipt):
            result = self.operator.execute(self.plan('copy'))
        self.assertEqual(result['unknownCount'], 1)
        self.assertEqual(result['completedCount'], 0)
        self.assertEqual(len(self.library.remote_locations(self.asset['id'])), 1)


if __name__ == '__main__':
    unittest.main()
