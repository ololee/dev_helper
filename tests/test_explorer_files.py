"""Explorer file hand-off tests use generated assets and mock every OS launch."""
import hashlib
import io
from contextlib import contextmanager
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from PIL import Image
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

try:
    from desktop.asset_library import AssetLibrary, AssetLibraryError, _read_source
    import desktop.explorer_files as explorer
except ImportError:
    from asset_library import AssetLibrary, AssetLibraryError, _read_source
    import explorer_files as explorer


class ExplorerFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'game'
        self.source.mkdir()
        self.library = AssetLibrary(self.base / 'index')
        self.project = self.library.register_project(self.source, 'Generated Explorer fixtures')
        image = io.BytesIO()
        Image.new('RGB', (5, 3), 'red').save(image, 'PNG')
        self.payload = image.getvalue()
        (self.source / 'button.png').write_bytes(self.payload)
        (self.source / 'sound.wav').write_bytes(b'RIFF' + bytes(range(40)))
        (self.source / 'movie.mp4').write_bytes(b'\x00\x00\x00\x18ftypisom' + bytes(range(60)))
        (self.source / 'mesh.obj').write_text('v 0 0 0\n')
        (self.source / 'page.html').write_text('<script>window.owned=true</script>')
        (self.source / 'vector.svg').write_text('<svg onload="alert(1)"></svg>')
        (self.source / 'script.js').write_text('throw Error("never execute")')
        (self.source / 'danger.command').write_text('#!/bin/sh\necho never-execute\n')
        (self.source / 'file.webloc').write_text('https://example.invalid')
        (self.source / 'scene.blend').write_bytes(b'BLENDER' + bytes(range(20)))
        (self.source / 'opaque.bin').write_bytes(bytes(range(256)))
        (self.source / 'large.txt').write_bytes(b'x' * (explorer.TEXT_LIMIT + 70))
        job = self.library.start_scan(self.project['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = self.library.job(job['jobId'])
            if status['state'] not in ('queued', 'running'):
                self.assertEqual(status['state'], 'completed', status)
                break
            time.sleep(.01)
        else:
            self.fail('Generated scan did not complete')
        self.assets = {a['name']: a for a in self.library.list_assets(self.project['id'], limit=100)['items']}

        async def preview(request):
            try:
                return explorer.preview_response(self.library, request.path_params['id'],
                    method=request.method, range_header=request.headers.get('range'))
            except AssetLibraryError as error:
                return JSONResponse({'error': str(error)}, error.status)

        self.client = TestClient(Starlette(routes=[Route('/{id}', preview, methods=['GET', 'HEAD'])]))
        self.launch = patch.object(explorer.subprocess, 'run').start()
        self.darwin = patch.object(explorer.platform, 'system', return_value='Darwin').start()

    def tearDown(self):
        patch.stopall()
        self.client.close()
        self.library.close()
        self.tmp.cleanup()

    def url(self, name='button.png'):
        return '/' + self.assets[name]['id']

    def download_fixture(self, name='button.png', state='completed', operation='download'):
        identifier = str(uuid.uuid4())
        ledger = {identifier: {'jobId': identifier, 'name': name, 'state': state,
                              'operation': operation, 'result': {'bytes': (self.source / name).stat().st_size}}}
        @contextmanager
        def checked(job_id):
            job = ledger[job_id]
            if job['operation'] != 'download' or job['state'] != 'completed':
                raise ValueError('not completed')
            with _read_source(self.source, job['name']) as source:
                if source.seek(0, 2) != job['result']['bytes']:
                    raise ValueError('changed')
                source.seek(0)
                yield source
        workspace = SimpleNamespace(drive=SimpleNamespace(job=lambda value: ledger[value]),
                                    safe_name=lambda name, **_: name, open_download_file=checked)
        return workspace, identifier, ledger

    def test_raster_preview_uses_indexed_image_format_and_exact_bytes(self):
        result = explorer.preview_info(self.assets['button.png'])
        self.assertEqual(result, {'previewKind': 'image', 'previewMime': 'image/png', 'canOpenNative': True})
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.payload)
        self.assertEqual(response.headers['content-type'], 'image/png')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertEqual(response.headers['accept-ranges'], 'bytes')
        self.assertTrue(response.headers['content-disposition'].startswith('inline;'))
        self.assertIn('private', response.headers['cache-control'])
        self.assertEqual(int(response.headers['content-length']), len(self.payload))
        self.launch.assert_not_called()

    def test_range_partial_open_end_and_suffix(self):
        for header, expected, start, end in [
            ('bytes=0-7', self.payload[:8], 0, 7),
            ('bytes=8-', self.payload[8:], 8, len(self.payload) - 1),
            ('bytes=-6', self.payload[-6:], len(self.payload) - 6, len(self.payload) - 1),
            ('bytes=0-99999', self.payload, 0, len(self.payload) - 1),
            ('bytes=-99999', self.payload, 0, len(self.payload) - 1),
        ]:
            with self.subTest(header=header):
                response = self.client.get(self.url(), headers={'Range': header})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, expected)
                self.assertEqual(response.headers['content-range'], f'bytes {start}-{end}/{len(self.payload)}')
                self.assertEqual(int(response.headers['content-length']), len(expected))

    def test_unsatisfiable_malformed_and_multi_range(self):
        for header in ('bytes=999999-', 'bytes=9-2', 'bytes=-0', 'bytes=-', 'bytes=0-2,5-8',
                       'bytes=' + '9' * 500 + '-', 'items=0-2'):
            with self.subTest(header=header[:30]):
                response = self.client.get(self.url(), headers={'Range': header})
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response.content, b'')
                self.assertEqual(response.headers['content-range'], f'bytes */{len(self.payload)}')

    def test_head_is_empty_and_preserves_range_representation_length(self):
        head = self.client.head(self.url())
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b'')
        self.assertEqual(int(head.headers['content-length']), len(self.payload))
        partial = self.client.head(self.url(), headers={'Range': 'bytes=3-8'})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b'')
        self.assertEqual(partial.headers['content-length'], '6')

    def test_checked_descriptor_closes_for_get_head_and_rejected_ranges(self):
        opened = []
        original = self.library.open_file
        @contextmanager
        def tracked(identifier):
            with original(identifier) as handle:
                opened.append(handle)
                yield handle
        with patch.object(self.library, 'open_file', side_effect=tracked):
            for method, headers in [('GET', {}), ('HEAD', {}), ('GET', {'Range': 'bytes=999999-'})]:
                self.client.request(method, self.url(), headers=headers)
                self.assertTrue(opened[-1].closed)

    def test_web_formats_are_inert_text_and_never_native_opened(self):
        for name in ('page.html', 'vector.svg', 'script.js', 'danger.command', 'file.webloc'):
            with self.subTest(name=name):
                result = explorer.preview_info(self.assets[name])
                self.assertEqual(result['previewKind'], 'text')
                self.assertFalse(result['canOpenNative'])
                response = self.client.get(self.url(name))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['content-type'], 'text/plain; charset=utf-8')
                self.assertEqual(response.headers['x-preview-truncated'], 'false')
                self.assertIn("sandbox", response.headers['content-security-policy'])
                with self.assertRaises(AssetLibraryError):
                    explorer.native_action(self.library, self.assets[name]['id'], 'open', self.base / 'preview')
        self.launch.assert_not_called()

    def test_text_is_bounded_with_truncation_and_byte_ranges(self):
        response = self.client.get(self.url('large.txt'))
        self.assertEqual(len(response.content), explorer.TEXT_LIMIT)
        self.assertEqual(response.headers['x-preview-truncated'], 'true')
        partial = self.client.get(self.url('large.txt'), headers={'Range': 'bytes=-3'})
        self.assertEqual(partial.content, b'xxx')
        self.assertEqual(partial.headers['content-range'], f'bytes {explorer.TEXT_LIMIT - 3}-{explorer.TEXT_LIMIT - 1}/{explorer.TEXT_LIMIT}')

    def test_fixed_audio_and_video_mime_ignores_asset_supplied_mime(self):
        for name, mime in [('sound.wav', 'audio/wav'), ('movie.mp4', 'video/mp4')]:
            asset = {**self.assets[name], 'mimeType': 'text/html'}
            self.assertEqual(explorer.preview_info(asset)['previewMime'], mime)
            response = self.client.get(self.url(name), headers={'Range': 'bytes=0-7'})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers['content-type'], mime)

    def test_unknown_binary_and_false_image_facts_do_not_get_active_preview(self):
        response = self.client.get(self.url('opaque.bin'))
        self.assertEqual(response.status_code, 409)
        fake = {**self.assets['button.png'], 'image': {'status': 'not-analyzed'}, 'mimeType': 'image/png'}
        self.assertEqual(explorer.preview_info(fake)['previewKind'], 'none')
        self.assertFalse(explorer.preview_info(fake)['canOpenNative'])

    def test_source_change_and_source_symlink_are_rejected_before_headers(self):
        (self.source / 'button.png').write_bytes(b'changed')
        self.assertEqual(self.client.get(self.url()).status_code, 409)
        original = self.source / 'sound.wav'
        original.unlink()
        original.symlink_to(self.source / 'movie.mp4')
        self.assertEqual(self.client.get(self.url('sound.wav')).status_code, 409)
        self.launch.assert_not_called()

    def test_native_copy_is_hash_checked_private_and_keeps_safe_extension(self):
        cache = self.base / 'new-parent' / 'preview'
        result = explorer.native_action(self.library, self.assets['button.png']['id'], 'open', cache)
        self.assertTrue(result['opened'])
        self.assertTrue(result['copy'])
        files = list(cache.iterdir())
        self.assertEqual(len(files), 1)
        copy = files[0]
        self.assertRegex(copy.name, r'^[0-9a-f]{32}_button\.png$')
        self.assertEqual(hashlib.sha256(copy.read_bytes()).hexdigest(), self.assets['button.png']['sha256'])
        self.assertEqual(copy.stat().st_mode & 0o777, 0o400)
        self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.launch.call_args.args[0], ['/usr/bin/open', str(copy)])
        self.assertFalse(self.launch.call_args.kwargs['shell'])
        self.assertEqual((self.source / 'button.png').read_bytes(), self.payload)

    def test_native_copy_does_not_allow_scripts_blend_or_arbitrary_actions(self):
        for name in ('scene.blend', 'script.js', 'danger.command', 'page.html', 'file.webloc', 'opaque.bin'):
            with self.assertRaises(AssetLibraryError):
                explorer.native_action(self.library, self.assets[name]['id'], 'open', self.base / 'preview')
        with self.assertRaises(AssetLibraryError):
            explorer.native_action(self.library, self.assets['button.png']['id'], 'execute', self.base / 'preview')
        self.launch.assert_not_called()

    def test_native_mesh_is_supported_and_reveal_does_not_create_copy(self):
        self.assertTrue(explorer.preview_info(self.assets['mesh.obj'])['canOpenNative'])
        result = explorer.native_action(self.library, self.assets['mesh.obj']['id'], 'reveal', self.base / 'unused')
        self.assertTrue(result['revealed'])
        self.assertFalse((self.base / 'unused').exists())
        self.assertEqual(self.launch.call_args.args[0], ['/usr/bin/open', '-R', str(self.source / 'mesh.obj')])
        self.assertFalse(self.launch.call_args.kwargs['shell'])

    def test_native_rejects_symlink_cache_directory_or_ancestor(self):
        actual = self.base / 'actual'
        actual.mkdir()
        linked = self.base / 'linked'
        linked.symlink_to(actual, target_is_directory=True)
        for cache in (linked, linked / 'child'):
            with self.assertRaises(AssetLibraryError):
                explorer.native_action(self.library, self.assets['button.png']['id'], 'open', cache)
        self.assertEqual(list(actual.iterdir()), [])
        self.launch.assert_not_called()

    def test_native_checks_full_sha_before_os_handoff_and_cleans_failure(self):
        asset_id = self.assets['button.png']['id']
        old_asset = self.library.asset
        def wrong_sha(identifier):
            return {**old_asset(identifier), 'sha256': '0' * 64}
        with patch.object(self.library, 'asset', side_effect=wrong_sha):
            with self.assertRaises(AssetLibraryError):
                explorer.native_action(self.library, asset_id, 'open', self.base / 'preview')
        self.assertEqual(list((self.base / 'preview').iterdir()), [])
        self.launch.assert_not_called()

    def test_non_macos_native_action_is_unavailable(self):
        self.darwin.return_value = 'Linux'
        self.assertFalse(explorer.preview_info(self.assets['button.png'])['canOpenNative'])
        with self.assertRaises(AssetLibraryError) as caught:
            explorer.native_action(self.library, self.assets['button.png']['id'], 'reveal', self.base / 'preview')
        self.assertEqual(caught.exception.status, 501)
        self.launch.assert_not_called()

    def test_completed_download_image_info_and_same_checked_range_preview(self):
        workspace, identifier, _ = self.download_fixture()
        info = explorer.download_preview_info(workspace, identifier)
        self.assertEqual(info, {'previewKind': 'image', 'previewMime': 'image/png',
                               'canOpenNative': False, 'previewUrl': f'/api/netdisk/jobs/{identifier}/preview'})
        async def preview(request):
            return explorer.download_preview_response(workspace, identifier, method=request.method,
                                                       range_header=request.headers.get('range'))
        with TestClient(Starlette(routes=[Route('/preview', preview, methods=['GET', 'HEAD'])])) as client:
            response = client.get('/preview', headers={'Range': 'bytes=0-7'})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, self.payload[:8])
            self.assertEqual(response.headers['content-type'], 'image/png')
            self.assertEqual(response.headers['content-range'], f'bytes 0-7/{len(self.payload)}')
            head = client.head('/preview')
            self.assertEqual(head.content, b'')
            self.assertEqual(int(head.headers['content-length']), len(self.payload))
        self.launch.assert_not_called()

    def test_download_html_is_inert_plain_text(self):
        workspace, identifier, _ = self.download_fixture('page.html')
        info = explorer.download_preview_info(workspace, identifier)
        self.assertEqual(info['previewKind'], 'text')
        self.assertFalse(info['canOpenNative'])
        response = explorer.download_preview_response(workspace, identifier)
        self.assertEqual(response.media_type, 'text/plain')
        self.assertEqual(response.body.decode(), '<script>window.owned=true</script>')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertIn('sandbox', response.headers['content-security-policy'])

    def test_incomplete_or_upload_jobs_cannot_describe_or_preview_downloads(self):
        for state, operation in [('queued', 'download'), ('running', 'download'),
                                 ('failed', 'download'), ('completed', 'upload')]:
            workspace, identifier, _ = self.download_fixture(state=state, operation=operation)
            for call in (explorer.download_preview_info, explorer.download_preview_response):
                with self.assertRaises(AssetLibraryError) as caught:
                    call(workspace, identifier)
                self.assertEqual(caught.exception.status, 409)
        self.launch.assert_not_called()

    def test_download_preview_rejects_arbitrary_ids_and_changed_managed_files(self):
        workspace, identifier, _ = self.download_fixture()
        with self.assertRaises(AssetLibraryError):
            explorer.download_preview_info(workspace, '../../not-a-job')
        (self.source / 'button.png').write_bytes(b'changed download')
        for call in (explorer.download_preview_info, explorer.download_preview_response):
            with self.assertRaises(AssetLibraryError) as caught:
                call(workspace, identifier)
            self.assertEqual(caught.exception.status, 409)


if __name__ == '__main__':
    unittest.main()
