"""Original-file mutations are exercised only inside generated temporary projects."""
import errno
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

try:
    from desktop.asset_library import AssetLibrary, AssetLibraryError
    from desktop.local_file_ops import LocalFileOperator
    import desktop.local_file_ops as fileops
except ImportError:
    from asset_library import AssetLibrary, AssetLibraryError
    from local_file_ops import LocalFileOperator
    import local_file_ops as fileops


class LocalFileOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.game = self.root / 'game'
        self.second = self.root / 'second'
        (self.game / 'Assets').mkdir(parents=True)
        (self.game / 'dest').mkdir()
        self.second.mkdir()
        encoded = io.BytesIO()
        Image.new('RGB', (4, 4), '#ff8000').save(encoded, 'PNG')
        self.image = encoded.getvalue()
        (self.game / 'Assets' / 'icon.png').write_bytes(self.image)
        (self.game / 'Assets' / 'readme.md').write_text('fixture notes')
        (self.game / 'root.txt').write_text('root fixture')
        self.library = AssetLibrary(self.root / 'index')
        self.first = self.library.register_project(self.game, 'First fixture')
        self.other = self.library.register_project(self.second, 'Second fixture')
        self.operator = LocalFileOperator(self.library, self.root / 'private')
        self.scan(self.first)
        self.items = {item['path']: item for item in self.library.list_assets()['items']}
        self.icon = self.items['Assets/icon.png']
        self.library.save_annotation(self.icon['id'], {'source': 'manual', 'purpose': 'orange test button', 'categories': ['gui']})
        self.library.import_embeddings(self.first['id'], 'test-image-v1', [{'assetId': self.icon['id'], 'vector': [1., 0., 0.]}])

    def tearDown(self):
        self.library.close()
        self.temp.cleanup()

    def scan(self, project, force=False):
        job = self.library.start_scan(project['id'], force=force)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.library.job(job['jobId'])
            if result['state'] not in ('queued', 'running'):
                self.assertEqual(result['state'], 'completed', result)
                return
            time.sleep(.01)
        self.fail('fixture scan timeout')

    def item(self, path, project=None):
        return {'projectId': (project or self.first)['id'], 'path': path}

    def plan(self, operation, paths, destination=None, name=None):
        return self.operator.plan(operation, [self.item(path) for path in paths], destination, name)

    def execute(self, plan):
        result = self.operator.execute(plan)
        self.assertEqual(result['state'], 'completed', result)
        return result

    def test_copy_file_clones_classification_and_binary_vectors_with_new_id(self):
        plan = self.plan('copy', ['Assets/icon.png'], self.item('dest'))
        self.assertEqual((plan['fileCount'], plan['bytes']), (1, len(self.image)))
        self.assertEqual(plan['items'][0]['destination']['path'], 'dest/icon.png')
        result = self.execute(plan)
        copied = self.library.asset(result['items'][0]['assetIds'][0])
        self.assertNotEqual(copied['id'], self.icon['id'])
        self.assertEqual(copied['path'], 'dest/icon.png')
        self.assertEqual(copied['sha256'], self.icon['sha256'])
        self.assertEqual(copied['annotations'][0]['purpose'], 'orange test button')
        self.assertEqual(copied['embeddings'], self.library.asset(self.icon['id'])['embeddings'])
        self.assertEqual((self.game / 'dest/icon.png').read_bytes(), self.image)
        self.assertEqual((self.game / 'Assets/icon.png').read_bytes(), self.image)

    def test_move_then_reuse_original_path_does_not_reuse_moved_asset_id(self):
        self.execute(self.plan('move', ['Assets/icon.png'], self.item('dest')))
        copied = self.execute(self.plan('copy', ['dest/icon.png'], self.item('Assets')))
        self.assertNotEqual(copied['items'][0]['assetIds'][0], self.icon['id'])
        self.assertEqual(self.library.asset(self.icon['id'])['path'], 'dest/icon.png')
        self.scan(self.first, force=True)
        self.assertEqual(self.library.asset(self.icon['id'])['path'], 'dest/icon.png')
        self.assertEqual(self.library.asset(copied['items'][0]['assetIds'][0])['path'], 'Assets/icon.png')

    def test_copy_preserves_source_modified_times_and_basic_permissions(self):
        source = self.game / 'Assets'
        file = source / 'icon.png'
        os.chmod(file, 0o640)
        os.chmod(source, 0o750)
        file_stamp = 1_600_000_001_123_456_789
        directory_stamp = 1_600_000_002_987_654_321
        os.utime(file, ns=(file_stamp, file_stamp))
        os.utime(source, ns=(directory_stamp, directory_stamp))
        self.scan(self.first)
        self.execute(self.plan('copy', ['Assets'], self.item('dest')))
        copied = self.game / 'dest/Assets'
        self.assertEqual(copied.stat().st_mtime_ns, directory_stamp)
        self.assertEqual((copied / 'icon.png').stat().st_mtime_ns, file_stamp)
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((copied / 'icon.png').stat().st_mode), 0o640)
        asset = next(item for item in self.library.list_assets()['items'] if item['path'] == 'dest/Assets/icon.png')
        with self.library.open_file(asset['id']) as content:
            self.assertEqual(content.read(), self.image)

    def test_metadata_companions_follow_copy_rename_and_trash(self):
        guid = 'a' * 32
        (self.game / 'Assets/icon.png.meta').write_text('fileFormatVersion: 2\nguid: ' + guid + '\n')
        self.scan(self.first)
        meta = next(item for item in self.library.list_assets()['items'] if item['path'] == 'Assets/icon.png.meta')
        copy_plan = self.plan('copy', ['Assets/icon.png'], self.item('dest'))
        self.assertEqual([item['name'] for item in copy_plan['items']], ['icon.png', 'icon.png.meta'])
        self.assertEqual(copy_plan['items'][1]['companionOf'], self.item('Assets/icon.png'))
        self.execute(copy_plan)
        self.assertTrue((self.game / 'dest/icon.png.meta').exists())
        rename = self.plan('rename', ['Assets/icon.png'], name='renamed.png')
        self.assertEqual([item['destination']['path'] for item in rename['items']], ['Assets/renamed.png', 'Assets/renamed.png.meta'])
        self.execute(rename)
        self.assertEqual(self.library.asset(meta['id'])['path'], 'Assets/renamed.png.meta')
        result = self.execute(self.plan('trash', ['Assets/renamed.png']))
        self.assertEqual(len(result['items']), 2)
        for item in result['items']:
            self.operator.restore(item['trashId'])
        self.assertTrue((self.game / 'Assets/renamed.png.meta').exists())
        # Explicit companions are not silently duplicated.
        plan = self.plan('copy', ['Assets/renamed.png', 'Assets/renamed.png.meta'], self.item('', self.other))
        self.assertEqual(len(plan['items']), 2)

    def test_batch_rename_uses_one_index_per_logical_source_and_keeps_ids(self):
        readme = self.items['Assets/readme.md']
        plan = self.plan('rename', ['Assets/icon.png', 'Assets/readme.md'], name='item_{index}_{name}{ext}')
        self.assertEqual([item['destination']['path'] for item in plan['items']],
                         ['Assets/item_1_icon.png', 'Assets/item_2_readme.md'])
        self.execute(plan)
        self.assertEqual(self.library.asset(self.icon['id'])['path'], 'Assets/item_1_icon.png')
        self.assertEqual(self.library.asset(readme['id'])['path'], 'Assets/item_2_readme.md')
        self.assertEqual(self.library.asset(self.icon['id'])['annotations'][0]['purpose'], 'orange test button')

    def test_batch_rename_explicit_companions_share_primary_index_even_if_selected_first(self):
        for path in ('Assets/icon.png.meta', 'Assets/readme.md.meta'):
            (self.game / path).write_text('fileFormatVersion: 2\nguid: ' + ('a' if 'icon' in path else 'b') * 32)
        self.scan(self.first)
        plan = self.plan('rename', ['Assets/readme.md.meta', 'Assets/icon.png',
                                   'Assets/readme.md', 'Assets/icon.png.meta'], name='resource_{index}{ext}')
        self.assertEqual([item['destination']['path'] for item in plan['items']],
                         ['Assets/resource_1.png', 'Assets/resource_1.png.meta',
                          'Assets/resource_2.md', 'Assets/resource_2.md.meta'])
        self.assertEqual(plan['items'][1]['companionOf'], self.item('Assets/icon.png'))
        self.assertEqual(plan['items'][3]['companionOf'], self.item('Assets/readme.md'))
        self.execute(plan)
        self.assertTrue((self.game / 'Assets/resource_1.png.meta').exists())
        self.assertTrue((self.game / 'Assets/resource_2.md.meta').exists())
        self.assertFalse((self.game / 'Assets/readme.md.meta').exists())

    def test_batch_rename_rejects_unsafe_templates_and_colliding_outputs_without_mutation(self):
        for template in ('same-name', '{ext}', '{name.__class__}', '../{index}'):
            with self.subTest(template=template), self.assertRaises(AssetLibraryError):
                self.plan('rename', ['Assets/icon.png', 'Assets/readme.md'], name=template)
        (self.game / 'Assets/same.png').write_bytes(self.image)
        (self.game / 'Assets/same.jpg').write_bytes(self.image)
        self.scan(self.first)
        with self.assertRaises(AssetLibraryError):
            self.plan('rename', ['Assets/same.png', 'Assets/same.jpg'], name='{name}')
        self.assertTrue((self.game / 'Assets/same.png').exists())
        self.assertTrue((self.game / 'Assets/same.jpg').exists())
        self.assertFalse((self.game / 'Assets/same').exists())

    def test_batch_rename_revalidates_new_target_collision_before_changing_any_source(self):
        plan = self.plan('rename', ['Assets/icon.png', 'Assets/readme.md'], name='new_{index}{ext}')
        (self.game / 'Assets/new_2.md').write_text('do not overwrite fixture')
        result = self.operator.execute(plan)
        self.assertEqual(result['state'], 'failed')
        self.assertTrue((self.game / 'Assets/icon.png').exists())
        self.assertTrue((self.game / 'Assets/readme.md').exists())
        self.assertFalse((self.game / 'Assets/new_1.png').exists())
        self.assertEqual((self.game / 'Assets/new_2.md').read_text(), 'do not overwrite fixture')

    def test_symlink_metadata_companion_is_rejected(self):
        outside = self.root / 'outside-meta'
        outside.write_text('private fixture')
        (self.game / 'Assets/icon.png.meta').symlink_to(outside)
        with self.assertRaises(AssetLibraryError):
            self.plan('trash', ['Assets/icon.png'])
        self.assertEqual((self.game / 'Assets/icon.png').read_bytes(), self.image)

    def test_move_and_rename_preserve_id_annotations_vectors_after_force_scan(self):
        result = self.execute(self.plan('move', ['Assets/icon.png'], self.item('dest')))
        self.assertEqual(result['items'][0]['assetIds'], [self.icon['id']])
        self.assertFalse((self.game / 'Assets/icon.png').exists())
        self.execute(self.plan('rename', ['dest/icon.png'], name='renamed.png'))
        moved = self.library.asset(self.icon['id'])
        self.assertEqual(moved['path'], 'dest/renamed.png')
        self.assertEqual(moved['annotations'][0]['purpose'], 'orange test button')
        self.assertEqual(moved['embeddings'][0]['model'], 'test-image-v1')
        self.scan(self.first, force=True)
        self.assertEqual(self.library.asset(self.icon['id'])['path'], 'dest/renamed.png')
        self.assertEqual(self.library.list_assets(query='orange test button')['items'][0]['id'], self.icon['id'])

    def test_cross_project_directory_move_reparses_relative_dependencies(self):
        (self.game / 'Assets' / 'skeleton.atlas').write_text('icon.png\nsize: 4,4\nformat: RGBA8888\n')
        self.scan(self.first)
        atlas = next(item for item in self.library.list_assets()['items'] if item['name'] == 'skeleton.atlas')
        self.execute(self.plan('move', ['Assets'], self.item('', self.other)))
        moved = self.library.asset(self.icon['id'])
        self.assertEqual(moved['projectId'], self.other['id'])
        self.assertEqual(moved['path'], 'Assets/icon.png')
        self.assertEqual(moved['embeddings'][0]['model'], 'test-image-v1')
        dependency = self.library.dependencies(atlas['id'])
        self.assertEqual(dependency['edges'][0]['to'], self.icon['id'])
        self.assertEqual(dependency['unresolved'], [])
        self.assertEqual(self.library.list_assets(self.first['id'])['total'], 1)
        self.scan(self.other, force=True)
        self.assertEqual(self.library.asset(self.icon['id'])['projectId'], self.other['id'])

    def test_trash_restore_preserves_complete_metadata_and_binary_vectors(self):
        before = self.library.asset(self.icon['id'])
        result = self.execute(self.plan('trash', ['Assets']))
        trash_id = result['items'][0]['trashId']
        self.assertFalse((self.game / 'Assets').exists())
        self.assertEqual(self.operator.list_trash()['items'][0]['trashId'], trash_id)
        self.assertEqual(self.library.list_assets()['total'], 1)
        with self.operator._db() as db:
            self.assertGreater(db.execute('SELECT COUNT(*) FROM blobs WHERE trash_id=?', (trash_id,)).fetchone()[0], 0)
        self.assertEqual(self.operator.restore(trash_id)['state'], 'completed')
        restored = self.library.asset(self.icon['id'])
        self.assertEqual(restored['annotations'], before['annotations'])
        self.assertEqual(restored['embeddings'], before['embeddings'])
        self.assertEqual(restored['sha256'], before['sha256'])
        self.assertEqual((self.game / 'Assets/icon.png').read_bytes(), self.image)
        self.assertEqual(self.operator.list_trash()['items'], [])
        with self.assertRaises(AssetLibraryError):
            self.operator.restore(trash_id)

    def test_empty_directory_copy_move_trash_restore(self):
        (self.game / 'empty').mkdir()
        self.execute(self.plan('copy', ['empty'], self.item('dest')))
        self.assertTrue((self.game / 'dest/empty').is_dir())
        self.execute(self.plan('rename', ['dest/empty'], name='renamed-empty'))
        result = self.execute(self.plan('trash', ['dest/renamed-empty']))
        self.operator.restore(result['items'][0]['trashId'])
        self.assertTrue((self.game / 'dest/renamed-empty').is_dir())
        with self.library._db() as db:
            if db.execute("SELECT 1 FROM sqlite_schema WHERE name='asset_directories'").fetchone():
                self.assertIsNotNone(db.execute('SELECT 1 FROM asset_directories WHERE project_id=? AND path=?',
                                              (self.first['id'], 'dest/renamed-empty')).fetchone())

    def test_stale_batch_revalidates_every_source_before_any_mutation(self):
        plan = self.plan('move', ['Assets/icon.png', 'root.txt'], self.item('dest'))
        (self.game / 'root.txt').write_text('changed after preview')
        result = self.operator.execute(plan)
        self.assertEqual(result['state'], 'failed')
        self.assertFalse((self.game / 'dest/icon.png').exists())
        self.assertTrue((self.game / 'Assets/icon.png').exists())
        self.assertEqual([item['state'] for item in result['items']], ['failed', 'skipped'])

    def test_rejects_root_overlaps_self_targets_collisions_and_invalid_names(self):
        for operation, paths, destination, name in (
                ('trash', [''], None, None), ('trash', ['Assets', 'Assets/icon.png'], None, None),
                ('trash', ['Assets/icon.png', 'Assets/icon.png'], None, None),
                ('copy', ['Assets'], self.item('Assets'), None),
                ('copy', ['root.txt'], self.item(''), None),
                ('rename', ['root.txt'], None, '../escape'),
                ('rename', ['root.txt'], None, 'bad\\name'),
                ('rename', ['root.txt'], None, '/outside')):
            with self.subTest(operation=operation, paths=paths), self.assertRaises(AssetLibraryError):
                self.plan(operation, paths, destination, name)

    def test_rejects_symlink_ancestor_descendant_and_fifo(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('private fixture')
        (self.game / 'link').symlink_to(outside, target_is_directory=True)
        (self.game / 'Assets/link').symlink_to(outside / 'secret')
        for path in ('link', 'link/secret', 'Assets'):
            with self.subTest(path=path), self.assertRaises(AssetLibraryError):
                self.plan('trash', [path])
        if hasattr(os, 'mkfifo'):
            os.mkfifo(self.game / 'fifo')
            with self.assertRaises(AssetLibraryError):
                self.plan('trash', ['fifo'])
        self.assertEqual((outside / 'secret').read_text(), 'private fixture')

    def test_kernel_no_replace_handles_destination_created_after_validation(self):
        plan = self.plan('copy', ['Assets/icon.png'], self.item('dest'))
        actual = fileops._rename_exclusive

        def race(source_fd, source, target_fd, target):
            if target == 'icon.png':
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=target_fd)
                os.write(fd, b'do not overwrite')
                os.close(fd)
            return actual(source_fd, source, target_fd, target)

        with patch.object(fileops, '_rename_exclusive', side_effect=race):
            result = self.operator.execute(plan)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual((self.game / 'dest/icon.png').read_bytes(), b'do not overwrite')
        self.assertEqual((self.game / 'Assets/icon.png').read_bytes(), self.image)
        self.assertFalse(list((self.game / 'dest').glob('.devhelper-copy-*')))

    def test_index_failure_rolls_back_move_and_keeps_original_metadata(self):
        plan = self.plan('move', ['Assets/icon.png'], self.item('dest'))
        with patch.object(self.library, '_upsert', side_effect=RuntimeError('generated indexing failure')):
            result = self.operator.execute(plan)
        self.assertEqual(result['state'], 'failed')
        self.assertTrue((self.game / 'Assets/icon.png').exists())
        self.assertFalse((self.game / 'dest/icon.png').exists())
        self.assertEqual(self.library.asset(self.icon['id'])['path'], 'Assets/icon.png')
        self.assertEqual(self.library.asset(self.icon['id'])['embeddings'][0]['model'], 'test-image-v1')
        with self.library.open_file(self.icon['id']) as original:
            self.assertEqual(original.read(), self.image)

    def test_partial_failure_reports_completed_then_failed_and_skipped(self):
        plan = self.plan('move', ['Assets/icon.png', 'Assets/readme.md', 'root.txt'], self.item('dest'))
        actual = fileops._rename_exclusive

        def fail_second(source_fd, source, target_fd, target):
            if source == 'readme.md':
                raise OSError(errno.EACCES, 'generated denied')
            return actual(source_fd, source, target_fd, target)

        with patch.object(fileops, '_rename_exclusive', side_effect=fail_second):
            result = self.operator.execute(plan)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual([item['state'] for item in result['items']], ['completed', 'failed', 'skipped'])
        self.assertTrue((self.game / 'dest/icon.png').exists())
        self.assertTrue((self.game / 'Assets/readme.md').exists())
        self.assertTrue((self.game / 'root.txt').exists())

    def test_restore_never_overwrites_new_original_location(self):
        result = self.execute(self.plan('trash', ['Assets/icon.png']))
        trash_id = result['items'][0]['trashId']
        (self.game / 'Assets/icon.png').write_bytes(b'new original')
        with self.assertRaises(AssetLibraryError):
            self.operator.restore(trash_id)
        self.assertEqual((self.game / 'Assets/icon.png').read_bytes(), b'new original')
        self.assertEqual((self.operator.trash / trash_id).read_bytes(), self.image)

    def test_modified_trash_payload_is_not_restored(self):
        result = self.execute(self.plan('trash', ['Assets/icon.png']))
        trash_id = result['items'][0]['trashId']
        (self.operator.trash / trash_id).write_bytes(b'modified trash fixture')
        with self.assertRaises(AssetLibraryError):
            self.operator.restore(trash_id)
        self.assertFalse((self.game / 'Assets/icon.png').exists())
        self.assertEqual(self.library.list_assets()['total'], 2)

    def test_cross_filesystem_move_is_rejected_without_touching_original(self):
        (self.game / 'empty').mkdir()
        real_tree = fileops._tree

        def other_device(parent, name):
            tree = real_tree(parent, name)
            tree[0]['device'] += 1
            return tree

        with patch.object(fileops, '_tree', side_effect=other_device):
            with self.assertRaises(AssetLibraryError) as failure:
                self.plan('move', ['empty'], self.item('dest'))
        self.assertIn('跨磁盘', str(failure.exception))
        self.assertTrue((self.game / 'empty').is_dir())
        self.assertFalse((self.game / 'dest/empty').exists())

    def test_cloud_projects_active_scans_and_changed_index_are_rejected(self):
        with self.library._db() as db:
            db.execute('UPDATE projects SET options=? WHERE id=?', (json.dumps({'remoteBacked': True}), self.other['id']))
        with self.assertRaises(AssetLibraryError):
            self.plan('copy', ['root.txt'], self.item('', self.other))
        with self.library._db() as db:
            db.execute("INSERT INTO jobs VALUES('fixture-active',?,'running','{}',0)", (self.first['id'],))
        with self.assertRaises(AssetLibraryError):
            self.plan('trash', ['root.txt'])
        with self.library._db() as db:
            db.execute("DELETE FROM jobs WHERE id='fixture-active'")
        (self.game / 'root.txt').write_text('changed before plan')
        with self.assertRaises(AssetLibraryError):
            self.plan('trash', ['root.txt'])

    def test_unsupported_platform_can_initialize_but_cannot_plan_mutations(self):
        with patch.object(fileops.sys, 'platform', 'win32'):
            operator = LocalFileOperator(self.library, self.root / 'windows-ledger')
            self.assertFalse(operator.supported)
            with self.assertRaises(AssetLibraryError) as failure:
                operator.plan('trash', [self.item('root.txt')])
            self.assertEqual(failure.exception.status, 501)
            self.assertEqual(operator.list_trash(), {'items': []})


if __name__ == '__main__':
    unittest.main()
