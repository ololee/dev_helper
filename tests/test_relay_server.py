import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from relay.server import RelayServer, RelayStore


def uid():
    return str(uuid.uuid4())


class RelayServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = RelayServer(self.temp.name)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app()), base_url='http://relay.test')
        result = await self.client.post('/api/relay/workspaces', json={})
        self.workspace = result.json()['workspaceId']
        self.headers = {'X-DevHelper-Workspace': self.workspace}
        self.source, self.target = uid(), uid()
        for device, platform in ((self.source, 'mac'), (self.target, 'android')):
            result = await self.client.post('/api/relay/register', headers=self.headers,
                json={'deviceId': device, 'platform': platform, 'name': 'Synthetic test device'})
            self.assertEqual(result.status_code, 200)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.store.close()
        self.temp.cleanup()

    def envelope(self, **fields):
        return {'id': uid(), 'sourceId': self.source, 'targetId': self.target,
                'method': 'GET', 'path': '/api/knowledge/sync/manifest', **fields}

    async def submit(self, value):
        return await self.client.post('/api/relay/requests', headers=self.headers, json=value)

    async def claim(self):
        return await self.client.get('/api/relay/inbox', params={'deviceId': self.target}, headers=self.headers)

    async def test_catalog_is_metadata_only_and_cached_offline(self):
        result = await self.client.post('/api/relay/heartbeat', headers=self.headers, json={
            'deviceId': self.target, 'platform': 'android', 'sameLan': True,
            'lanAddresses': ['http://192.168.1.5:8765'], 'catalog': {'documents': [{
                'id': uid(), 'title': 'Synthetic title', 'content': 'PRIVATE BODY',
                'transcript': 'PRIVATE TRANSCRIPT', 'tags': [{'password': 'secret'}],
                'source': {'apiKey': 'secret'}, 'hash': 'a' * 64}]}})
        self.assertEqual(result.status_code, 200)
        self.server.store.db.execute('UPDATE devices SET seen=0 WHERE id=?', (self.target,))
        result = await self.client.get('/api/relay/catalog/' + self.target, headers=self.headers)
        self.assertFalse(result.json()['online'])
        row = result.json()['catalog']['documents'][0]
        self.assertEqual(set(row), {'id', 'title', 'hash'})
        self.assertNotIn('PRIVATE', result.text)
        self.assertNotIn('secret', result.text)

    async def test_workspace_isolation(self):
        other = (await self.client.post('/api/relay/workspaces', json={})).json()['workspaceId']
        headers = {'X-DevHelper-Workspace': other}
        self.assertEqual((await self.client.get('/api/relay/devices', headers=headers)).json()['devices'], [])
        self.assertEqual((await self.client.get('/api/relay/catalog/' + self.target, headers=headers)).status_code, 404)
        self.assertEqual((await self.client.get('/api/relay/devices')).status_code, 401)

    async def test_idempotent_submission_and_changed_payload_conflict(self):
        value = self.envelope()
        first = await self.submit(value)
        self.assertEqual(first.status_code, 202)
        self.assertEqual((await self.submit(value)).json(), first.json())
        self.assertEqual((await self.submit({**value, 'method': 'DELETE'})).status_code, 409)
        self.assertEqual(len((await self.claim()).json()['requests']), 1)
        self.assertEqual((await self.claim()).json()['requests'], [])

    async def test_only_target_reply_and_source_status(self):
        value = self.envelope()
        await self.submit(value)
        await self.claim()
        path = '/api/relay/replies/' + value['id']
        body = {'deviceId': self.source, 'status': 200, 'body': {'ok': True}}
        self.assertEqual((await self.client.post(path, headers=self.headers, json=body)).status_code, 403)
        body['deviceId'] = self.target
        result = await self.client.post(path, headers=self.headers, json=body)
        self.assertEqual(result.json()['state'], 'completed')
        self.assertEqual((await self.client.post(path, headers=self.headers, json=body)).json(), result.json())
        self.assertEqual((await self.client.post(path, headers=self.headers, json={**body, 'status': 500})).status_code, 409)
        path = '/api/relay/requests/' + value['id']
        self.assertEqual((await self.client.get(path, headers=self.headers)).status_code, 400)
        self.assertEqual((await self.client.get(path, headers=self.headers, params={'sourceId': self.target})).status_code, 403)
        self.assertEqual((await self.client.get(path, headers=self.headers, params={'sourceId': self.source})).json()['response']['body'], {'ok': True})

    async def test_pending_survives_restart_claimed_never_replays(self):
        pending, claimed = self.envelope(), self.envelope()
        await self.submit(claimed)
        await self.claim()
        await self.submit(pending)
        self.server.store.close()
        self.server.store = RelayStore(self.temp.name)
        claimed_result = self.server.store.get_request(self.workspace, claimed['id'], self.source)
        self.assertEqual(claimed_result['state'], 'failed')
        self.assertIn('delivery_unknown', claimed_result['error'])
        inbox = (await self.claim()).json()['requests']
        self.assertEqual([row['id'] for row in inbox], [pending['id']])

    async def test_cancellation_does_not_pretend_to_undo_execution(self):
        value = self.envelope()
        await self.submit(value)
        await self.claim()
        result = await self.client.delete('/api/relay/requests/' + value['id'], headers=self.headers, params={'sourceId': self.source})
        self.assertTrue(result.json()['deliveryUncertain'])
        self.assertEqual(result.json()['state'], 'cancelled')
        self.assertEqual((await self.claim()).json()['requests'], [])

    async def test_streaming_blob_hash_range_and_workspace_isolation(self):
        payload = b'Synthetic transfer data\x00' * 100000
        async def chunks():
            for offset in range(0, len(payload), 32768):
                yield payload[offset:offset + 32768]
        result = await self.client.post('/api/relay/blobs', params={'deviceId': self.source, 'name': 'test.bin'},
            headers={**self.headers, 'Content-Type': 'application/octet-stream'}, content=chunks())
        self.assertEqual(result.status_code, 201)
        blob = result.json()
        self.assertEqual(blob['bytes'], len(payload))
        self.assertEqual(blob['sha256'], hashlib.sha256(payload).hexdigest())
        path = '/api/relay/blobs/' + blob['id']
        head = await self.client.head(path, headers=self.headers)
        self.assertEqual(head.headers['x-content-sha256'], blob['sha256'])
        self.assertEqual(int(head.headers['content-length']), len(payload))
        part = await self.client.get(path, headers={**self.headers, 'Range': 'bytes=123-321'})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.content, payload[123:322])
        other = (await self.client.post('/api/relay/workspaces', json={})).json()['workspaceId']
        self.assertEqual((await self.client.get(path, headers={'X-DevHelper-Workspace': other})).status_code, 404)
        request = self.envelope(blobId=blob['id'])
        self.assertEqual((await self.submit(request)).json()['blob'], blob)
        self.assertEqual((await self.client.delete(path, headers=self.headers)).status_code, 200)
        self.assertEqual((await self.client.get(path, headers=self.headers)).status_code, 404)

    async def test_incomplete_blob_never_exposed(self):
        result = await self.client.post('/api/relay/blobs', params={'deviceId': self.source},
            headers={**self.headers, 'Content-Length': '100'}, content=b'partial')
        self.assertEqual(result.status_code, 400)
        self.assertEqual(list(self.server.store.blobs_dir.iterdir()), [])
        self.assertEqual(self.server.store.db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0], 0)

    async def test_rejects_arbitrary_hosts_paths_and_oversized_json(self):
        for path in ('https://external.example/api/knowledge/documents', '/api/knowledge/../secrets', '/api/knowledge/%2e%2e/secrets', '/api/admin', '/mcp#fragment'):
            self.assertEqual((await self.submit(self.envelope(path=path))).status_code, 400)
        result = await self.submit(self.envelope(body={'content': 'x' * (1024 * 1024)}))
        self.assertEqual(result.status_code, 413)

    async def test_longpoll_wakes_on_request_and_reply(self):
        waiting = asyncio.create_task(self.client.get('/api/relay/inbox', headers=self.headers,
            params={'deviceId': self.target, 'wait': 1}))
        await asyncio.sleep(0.01)
        value = self.envelope()
        await self.submit(value)
        self.assertEqual((await waiting).json()['requests'][0]['id'], value['id'])
        waiting = asyncio.create_task(self.client.get('/api/relay/requests/' + value['id'], headers=self.headers,
            params={'sourceId': self.source, 'wait': 1}))
        await asyncio.sleep(0.01)
        await self.client.post('/api/relay/replies/' + value['id'], headers=self.headers,
            json={'deviceId': self.target, 'status': 200, 'body': {'ok': True}})
        self.assertEqual((await waiting).json()['state'], 'completed')

    async def test_mcp_gateway_forwards_json_without_running_tools_on_linux(self):
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}
        gateway = asyncio.create_task(self.client.post('/devices/' + self.target + '/mcp',
            headers={**self.headers, 'X-DevHelper-Source': self.source}, json=message))
        inbox = await self.client.get('/api/relay/inbox', headers=self.headers, params={'deviceId': self.target, 'wait': 1})
        request = inbox.json()['requests'][0]
        self.assertEqual(request['body'], message)
        self.assertEqual(request['path'], '/mcp')
        expected = {'jsonrpc': '2.0', 'id': 1, 'result': {'tools': []}}
        await self.client.post('/api/relay/replies/' + request['id'], headers=self.headers,
            json={'deviceId': self.target, 'status': 200, 'body': expected})
        self.assertEqual((await gateway).json(), expected)
        result = await self.client.post('/devices/' + self.target + '/mcp',
            headers={**self.headers, 'X-DevHelper-Source': self.source},
            json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual(result.status_code, 202)
