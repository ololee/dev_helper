"""Explorer routes use generated fixtures and never launch an OS application."""
import asyncio
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from server import Desktop
from starlette.requests import Request
from starlette.testclient import TestClient


class ExplorerHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'generated-game'
        (self.source / 'ui').mkdir(parents=True)
        (self.source / 'docs').mkdir()
        image = io.BytesIO()
        Image.new('RGBA', (16, 12), (30, 120, 200, 180)).save(image, 'PNG')
        self.image_bytes = image.getvalue()
        (self.source / 'ui' / 'icon.png').write_bytes(self.image_bytes)
        self.html = b'<html><script>alert("untrusted fixture")</script><body>Test</body></html>'
        (self.source / 'unsafe.html').write_bytes(self.html)
        (self.source / 'root.txt').write_text('Fixture root text', encoding='utf-8')
        (self.source / 'docs' / 'readme.md').write_text('# Generated fixture', encoding='utf-8')
        self.desktop = Desktop(8876, self.root / 'data', self.root / 'shared')
        self.workspace = self.desktop.resources
        self.patches = [patch.object(self.desktop.discovery, 'start'),
                        patch.object(self.desktop.discovery, 'stop'),
                        patch('resource_workspace.sys.platform', 'darwin'),
                        patch('explorer_files.platform.system', return_value='Darwin'),
                        patch('resource_workspace.native_action', return_value={'opened': True, 'copy': True})]
        self.native = None
        for index, active in enumerate(self.patches):
            result = active.start()
            if index == len(self.patches) - 1:
                self.native = result
        self.client = TestClient(self.desktop.app(), base_url='http://127.0.0.1:8876',
                                 client=('127.0.0.1', 43000))
        self.client.__enter__()
        self.project = self.workspace.library.register_project(self.source, 'Generated Explorer fixture')
        job = self.workspace.library.start_scan(self.project['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.workspace.library.job(job['jobId'])
            if state['state'] not in ('queued', 'running'):
                self.assertEqual(state['state'], 'completed', state)
                break
            time.sleep(.01)
        else:
            self.fail('fixture scan did not finish')
        self.assets = {x['path']: x for x in self.workspace.library.list_assets()['items']}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for active in reversed(self.patches):
            active.stop()
        self.temp.cleanup()

    def browse(self, **fields):
        return self.client.post('/api/resource-library/browse', json={'projectId': self.project['id'], **fields})

    def route(self, relative, action):
        return '/api/resource-library/assets/' + self.assets[relative]['id'] + '/' + action

    def request(self, path, *, method='POST', data=None, peer='127.0.0.1', host='127.0.0.1:8876', headers=None):
        """Build real ASGI peer/Host contexts without a proxy or network server."""
        body = json.dumps({} if data is None else data).encode()
        raw_headers = [(b'host', host.encode()), (b'content-type', b'application/json')]
        raw_headers.extend((name.lower().encode(), value.encode()) for name, value in (headers or {}).items())
        scope = {'type': 'http', 'http_version': '1.1', 'scheme': 'http', 'method': method,
                 'path': path, 'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
                 'client': (peer, 43000), 'server': ('127.0.0.1', 8876), 'headers': raw_headers}
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {'type': 'http.disconnect'}
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}

        return Request(scope, receive)

    def test_browse_schema_pagination_folders_and_preview_links(self):
        response = self.client.post('/api/resource-library/browse', json={})
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()['items'][0]
        self.assertTrue(project['isDir'])
        self.assertEqual(project['id'], 'project:' + self.project['id'])
        self.assertNotIn('previewUrl', project)
        response = self.browse(limit=2, offset=1)
        self.assertEqual(response.status_code, 200, response.text)
        listing = response.json()
        self.assertEqual((listing['total'], listing['limit'], listing['offset']), (4, 2, 1))
        self.assertEqual([x['name'] for x in listing['items']], ['ui', 'root.txt'])
        folder, text = listing['items']
        for field in ('previewUrl', 'previewKind', 'contentUrl', 'thumbnailUrl', 'canOpenNative', 'canReveal'):
            self.assertNotIn(field, folder)
        self.assertEqual(text['previewKind'], 'text')
        self.assertEqual(text['previewUrl'], self.route('root.txt', 'preview'))
        self.assertEqual(listing['breadcrumbs'][0]['projectId'], self.project['id'])
        image = self.browse(path='ui').json()['items'][0]
        self.assertEqual(image['previewKind'], 'image')
        self.assertEqual(image['previewUrl'], self.route('ui/icon.png', 'preview'))
        self.assertEqual(image['thumbnailUrl'], self.route('ui/icon.png', 'thumbnail'))
        for invalid in ({'limit': 0}, {'limit': 501}, {'limit': True}, {'limit': 1.5}, {'offset': False},
                        {'offset': -1}, {'path': '../outside'},
                        {'sort': 'name; DROP TABLE assets'}, {'unknown': True}, {'path': []}):
            with self.subTest(invalid=invalid):
                failed = self.browse(**invalid)
                self.assertEqual(failed.status_code, 400, failed.text)
        self.assertEqual(self.client.post('/api/resource-library/browse', json=[]).status_code, 400)
        self.native.assert_not_called()

    def test_preview_get_head_and_range_are_exact_and_safe(self):
        url = self.route('ui/icon.png', 'preview')
        full = self.client.get(url)
        self.assertEqual(full.status_code, 200, full.text)
        self.assertEqual(full.content, self.image_bytes)
        self.assertEqual(full.headers['content-type'], 'image/png')
        self.assertEqual(full.headers['x-content-type-options'], 'nosniff')
        self.assertIn('sandbox', full.headers['content-security-policy'])
        head = self.client.head(url)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b'')
        self.assertEqual(int(head.headers['content-length']), len(self.image_bytes))
        for range_value, expected, expected_range in (
                ('bytes=2-5', self.image_bytes[2:6], f'bytes 2-5/{len(self.image_bytes)}'),
                ('bytes=-4', self.image_bytes[-4:], f'bytes {len(self.image_bytes)-4}-{len(self.image_bytes)-1}/{len(self.image_bytes)}')):
            with self.subTest(range_value=range_value):
                ranged = self.client.get(url, headers={'Range': range_value})
                self.assertEqual(ranged.status_code, 206)
                self.assertEqual(ranged.content, expected)
                self.assertEqual(ranged.headers['content-range'], expected_range)
                ranged_head = self.client.head(url, headers={'Range': range_value})
                self.assertEqual(ranged_head.status_code, 206)
                self.assertEqual(ranged_head.content, b'')
                self.assertEqual(int(ranged_head.headers['content-length']), len(expected))
        for invalid in ('bytes=999999-', 'bytes=0-2,4-6', 'bytes=-0'):
            response = self.client.get(url, headers={'Range': invalid})
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response.content, b'')
        self.assertEqual(self.client.post(url, json={}).status_code, 405)
        self.native.assert_not_called()

    def test_html_preview_is_inert_text_not_active_page(self):
        response = self.client.get(self.route('unsafe.html', 'preview'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, self.html)
        self.assertTrue(response.headers['content-type'].startswith('text/plain'))
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertEqual(response.headers['content-security-policy'], "sandbox; default-src 'none'")
        self.assertEqual(response.headers['x-preview-truncated'], 'false')
        self.native.assert_not_called()

    def test_open_and_reveal_require_indexed_asset_and_empty_post(self):
        for action in ('open', 'reveal'):
            url = self.route('ui/icon.png', action)
            with self.subTest(action=action):
                self.native.reset_mock()
                response = self.client.post(url, json={})
                self.assertEqual(response.status_code, 200, response.text)
                self.native.assert_called_once_with(self.workspace.library, self.assets['ui/icon.png']['id'],
                                                    action, self.workspace.root / 'preview-copies')
                self.native.reset_mock()
                for body in ({'path': '/tmp/unindexed'}, {'assetId': self.assets['root.txt']['id']},
                             {'command': 'do not execute'}, []):
                    failed = self.client.post(url, json=body)
                    self.assertEqual(failed.status_code, 400, failed.text)
                self.assertEqual(self.client.get(url).status_code, 405)
                self.assertEqual(self.client.head(url).status_code, 405)
                self.native.assert_not_called()

    def test_native_actions_do_not_trust_forwarding_lan_or_relay(self):
        async def exercise():
            for action in ('open', 'reveal'):
                path = self.route('ui/icon.png', action)
                denied = [
                    self.request(path, peer='192.168.1.77', headers={'X-Forwarded-For': '127.0.0.1'}),
                    self.request(path, peer='192.168.1.77', headers={'Forwarded': 'for=127.0.0.1;host=localhost:8876'}),
                    self.request(path, peer='127.0.0.1', host='192.168.1.22:8876'),
                    self.request(path, peer='0.0.0.0'),
                    self.request(path, peer='testclient'),
                    self.request(path, peer='127.0.0.1', headers={'Origin': 'http://evil.example'}),
                ]
                with patch('resource_workspace.local_ips', return_value=['192.168.1.22']):
                    for request in denied:
                        result = await self.workspace.api(request)
                        self.assertEqual(result.status_code, 403, result.body)
                trusted = await self.workspace.api(self.request(path), trusted_relay=True)
                self.assertEqual(trusted.status_code, 403, trusted.body)
        asyncio.run(exercise())
        self.native.assert_not_called()

    def test_real_loopback_with_localhost_name_or_ipv6_is_allowed(self):
        async def exercise():
            for peer, host in (('127.0.0.1', 'localhost:8876'), ('::1', '[::1]:8876')):
                for action in ('open', 'reveal'):
                    request = self.request(self.route('ui/icon.png', action), peer=peer, host=host,
                                           headers={'X-Forwarded-For': '192.168.1.77'})
                    result = await self.workspace.api(request)
                    self.assertEqual(result.status_code, 200, result.body)
        asyncio.run(exercise())
        self.assertEqual(self.native.call_count, 4)

    def test_local_capabilities_are_correct_and_remote_originals_cannot_open(self):
        image = self.browse(path='ui').json()['items'][0]
        self.assertTrue(image['canOpenNative'])
        self.assertTrue(image['canReveal'])
        html = next(item for item in self.browse().json()['items'] if item['name'] == 'unsafe.html')
        self.assertFalse(html['canOpenNative'])
        self.assertTrue(html['canReveal'])
        with self.workspace.library._db() as db:
            db.execute('INSERT INTO asset_storage(asset_id,remote_only) VALUES(?,1)', (image['id'],))
        remote = self.browse(path='ui').json()['items'][0]
        self.assertEqual(remote['availability'], 'remote')
        self.assertFalse(remote['canOpenNative'])
        self.assertFalse(remote['canReveal'])
        self.assertNotIn('thumbnailUrl', remote)
        self.native.assert_not_called()

    def test_native_context_is_isolated_for_concurrent_requests_and_reset(self):
        barrier = threading.Barrier(2)
        original = self.workspace.browse_assets

        def overlapping(args):
            barrier.wait(timeout=5)
            return original(args)

        async def exercise():
            data = {'projectId': self.project['id'], 'path': 'ui'}
            local = self.request('/api/resource-library/browse', data=data)
            lan = self.request('/api/resource-library/browse', data=data, peer='192.168.1.77',
                               headers={'X-Forwarded-For': '127.0.0.1'})
            with patch.object(self.workspace, 'browse_assets', side_effect=overlapping):
                results = await asyncio.gather(self.workspace.api(local), self.workspace.api(lan))
            self.assertFalse(self.workspace._native_context.get())
            return [json.loads(result.body)['items'][0] for result in results]

        local, lan = asyncio.run(exercise())
        self.assertTrue(local['canOpenNative'])
        self.assertTrue(local['canReveal'])
        self.assertFalse(lan['canOpenNative'])
        self.assertFalse(lan['canReveal'])
        self.assertFalse(self.workspace._native_context.get())
        self.assertFalse(self.workspace.call('asset_get', {'assetId': local['id']})['canOpenNative'])
        self.native.assert_not_called()

    def test_mcp_never_exposes_native_actions_or_context(self):
        headers = {'Accept': 'application/json, text/event-stream'}

        def rpc(method, params=None):
            response = self.client.post('/mcp/assets', json={'jsonrpc': '2.0', 'id': 1,
                'method': method, 'params': params or {}}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()['result']

        tools = rpc('tools/list')['tools']
        self.assertIn('asset_browse', {tool['name'] for tool in tools})
        self.assertFalse(any(tool['name'] in ('asset_open', 'asset_reveal', 'asset_open_native') for tool in tools))
        result = rpc('tools/call', {'name': 'asset_browse',
                                  'arguments': {'projectId': self.project['id'], 'path': 'ui'}})
        self.assertFalse(result.get('isError'), result)
        image = result['structuredContent']['items'][0]
        self.assertEqual(image['previewKind'], 'image')
        self.assertFalse(image['canOpenNative'])
        self.assertFalse(image['canReveal'])
        result = rpc('tools/call', {'name': 'asset_get', 'arguments': {'assetId': image['id']}})
        self.assertFalse(result['structuredContent']['canOpenNative'])
        self.assertFalse(result['structuredContent']['canReveal'])
        self.native.assert_not_called()

    def download_job(self, name, content, *, state='completed', operation='download'):
        """Prepare one generated app-owned file; no provider transfer is started."""
        identifier = str(uuid.uuid4())
        (self.workspace.downloads / name).write_bytes(content)
        return {'jobId': identifier, 'operation': operation, 'state': state, 'name': name,
                'result': {'bytes': len(content)}}

    def test_download_png_job_exposes_preview_and_exact_get_head_ranges(self):
        job = self.download_job('generated-download.png', self.image_bytes)
        prefix = '/api/netdisk/jobs/' + job['jobId']
        with (patch.object(self.workspace.drive, 'job', side_effect=lambda _identifier: json.loads(json.dumps(job))),
              patch.object(self.workspace.drive, 'start_download') as download):
            details = self.client.get(prefix)
            self.assertEqual(details.status_code, 200, details.text)
            preview = details.json()
            self.assertEqual(preview['previewKind'], 'image')
            self.assertEqual(preview['previewMime'], 'image/png')
            self.assertEqual(preview['previewUrl'], prefix + '/preview')
            self.assertFalse(preview['canOpenNative'])
            full = self.client.get(preview['previewUrl'])
            self.assertEqual(full.status_code, 200)
            self.assertEqual(full.content, self.image_bytes)
            self.assertEqual(full.headers['content-type'], 'image/png')
            ranged = self.client.get(preview['previewUrl'], headers={'Range': 'bytes=2-9'})
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.content, self.image_bytes[2:10])
            self.assertEqual(ranged.headers['content-range'], f'bytes 2-9/{len(self.image_bytes)}')
            head = self.client.head(preview['previewUrl'])
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b'')
            self.assertEqual(int(head.headers['content-length']), len(self.image_bytes))
            ranged_head = self.client.head(preview['previewUrl'], headers={'Range': 'bytes=2-9'})
            self.assertEqual(ranged_head.status_code, 206)
            self.assertEqual(ranged_head.content, b'')
            self.assertEqual(ranged_head.headers['content-length'], '8')
            download.assert_not_called()
        self.native.assert_not_called()

    def test_download_html_job_preview_remains_inert_text(self):
        job = self.download_job('generated-download.html', self.html)
        prefix = '/api/netdisk/jobs/' + job['jobId']
        with (patch.object(self.workspace.drive, 'job', side_effect=lambda _identifier: json.loads(json.dumps(job))),
              patch.object(self.workspace.drive, 'start_download') as download):
            details = self.client.get(prefix)
            self.assertEqual(details.status_code, 200, details.text)
            self.assertEqual(details.json()['previewKind'], 'text')
            self.assertEqual(details.json()['previewMime'], 'text/plain')
            self.assertEqual(details.json()['previewUrl'], prefix + '/preview')
            response = self.client.get(prefix + '/preview')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, self.html)
            self.assertTrue(response.headers['content-type'].startswith('text/plain'))
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            self.assertEqual(response.headers['content-security-policy'], "sandbox; default-src 'none'")
            download.assert_not_called()
        self.native.assert_not_called()

    def test_download_preview_rejects_pending_jobs_and_uploads_without_starting_transfer(self):
        with (patch.object(self.workspace.drive, 'start_download') as download,
              patch.object(self.workspace, 'open_download_file') as open_file):
            for state, operation in (('queued', 'download'), ('running', 'download'),
                                     ('failed', 'download'), ('completed', 'upload')):
                with self.subTest(state=state, operation=operation):
                    job = self.download_job('generated-not-previewable.png', self.image_bytes,
                                            state=state, operation=operation)
                    prefix = '/api/netdisk/jobs/' + job['jobId']
                    with patch.object(self.workspace.drive, 'job', return_value=job):
                        details = self.client.get(prefix)
                        self.assertEqual(details.status_code, 200, details.text)
                        self.assertNotIn('previewUrl', details.json())
                        for method in (self.client.get, self.client.head):
                            response = method(prefix + '/preview')
                            self.assertEqual(response.status_code, 409, response.text)
            download.assert_not_called()
            open_file.assert_not_called()
        self.native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
