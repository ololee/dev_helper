import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

import httpx
from server import Desktop
from relay.server import RelayServer
from relay_client import RelayClient, RelayPending, DeliveryUnknown, clean_catalog, route_mode, server_url


def synthetic_wav():
    file = io.BytesIO()
    with wave.open(file, 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(b'\0\0' * 160)
    return file.getvalue()


class MultiHost(httpx.AsyncBaseTransport):
    def __init__(self, apps):
        self.apps = {name: httpx.ASGITransport(app=app) for name, app in apps.items()}
        self.calls = []
        self.failure = None

    async def handle_async_request(self, request):
        self.calls.append((request.url.host, request.method, request.url.path))
        if self.failure and request.url.host == 'phone.invalid' and request.url.path.endswith('/execute'):
            raise self.failure('Synthetic transport failure', request=request)
        return await self.apps[request.url.host].handle_async_request(request)


class RelayClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.central = RelayServer(self.root / 'central')
        self.mac = Desktop(11111, self.root / 'mac', self.root / 'mac/shared')
        self.phone = Desktop(22222, self.root / 'phone', self.root / 'phone/shared')
        self.workspace = self.central.store.create_workspace()['workspaceId']
        self.transport = MultiHost({'relay.invalid': self.central.app(), 'phone.invalid': self.phone.app(), 'mac.invalid': self.mac.app()})
        self.http = httpx.AsyncClient(transport=self.transport, trust_env=False)
        self.mac.http = self.phone.http = self.http
        # Public-development URLs use a loopback IP; transport dispatch is synthetic.
        for desktop in (self.mac, self.phone):
            desktop.relay.set_config({'serverUrl': 'http://127.0.0.1:8890', 'workspaceId': self.workspace, 'enabled': True})
            desktop.relay.config['serverUrl'] = 'http://relay.invalid'
        self.mac.relay.set_config({'targetDeviceId': self.phone.preferences.get()['deviceId']})
        self.register_peer(True)
        self.pump = None

    def register_peer(self, same_lan):
        for desktop, platform, address in ((self.mac, 'mac', 'http://mac.invalid'), (self.phone, 'android', 'http://phone.invalid')):
            self.central.store.register(self.workspace, dict(deviceId=desktop.preferences.get()['deviceId'], platform=platform,
                name=platform, sameLan=same_lan, lanAddresses=[address], catalog={}))

    async def asyncTearDown(self):
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
        await self.mac.relay.stop(); await self.phone.relay.stop()
        await self.http.aclose()
        for desktop in (self.mac, self.phone):
            desktop.relay.close(); desktop.workflows.close(); desktop.knowledge.close()
        self.central.store.close()
        self.tmp.cleanup()

    def start_phone(self):
        self.pump = asyncio.create_task(self.phone.relay.inbox_loop())

    async def test_redacted_configuration_and_https_validation(self):
        public = self.mac.relay.public_config()
        self.assertNotIn('workspaceId', public)
        self.assertNotIn(self.workspace, json.dumps(public))
        self.assertEqual((self.root / 'mac/relay/config.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError): server_url('http://public.example')
        with self.assertRaises(ValueError): self.mac.relay.set_config({'sameLan': 'yes'})
        response = await self.http.get('http://mac.invalid/api/relay/identity')
        self.assertEqual(response.status_code, 403)

    async def test_configuration_binding_survives_restart_and_identical_save(self):
        generation = self.mac.relay.generation
        self.mac.relay.set_config({'targetDeviceId': self.phone.relay.device_id})
        self.assertEqual(self.mac.relay.generation, generation)
        self.mac.relay.close()
        self.mac.relay = RelayClient(self.mac, self.root / 'mac')
        self.assertEqual(self.mac.relay.generation, generation)

    async def test_catalog_never_uploads_content_audio_or_clipboard(self):
        self.mac.knowledge.save_document(dict(kind='memory', title='Synthetic metadata', content='PRIVATE SYNTHETIC BODY'))
        await self.mac.relay.register()
        cached = self.central.store.catalog(self.workspace, self.mac.relay.device_id)
        serialized = json.dumps(cached)
        self.assertNotIn('PRIVATE SYNTHETIC BODY', serialized)
        self.assertEqual(cached['catalog']['documents'][0]['title'], 'Synthetic metadata')
        self.assertFalse(any(path in ('/api/relay/requests', '/api/relay/blobs') for _, _, path in self.transport.calls))
        self.assertNotIn('content', clean_catalog({'documents': [{'content': 'secret', 'title': 'yes'}]})['documents'][0])

    async def test_same_lan_off_forces_relay_even_explicit_lan(self):
        self.register_peer(False)
        selected = await self.mac.relay.route('lan')
        self.assertEqual(selected['type'], 'relay')
        self.assertFalse(any(host == 'phone.invalid' for host, _, _ in self.transport.calls))

    async def test_identity_mismatch_falls_back_and_does_not_use_gateway(self):
        selected = await self.mac.relay.route()
        # The synthetic phone advertises Android but its Mac implementation reports mac.
        self.assertEqual(selected['type'], 'relay')
        self.assertTrue(any(path.endswith('/identity') for _, _, path in self.transport.calls))
        self.assertFalse(any(path.endswith('/execute') for _, _, path in self.transport.calls))

    async def test_valid_identity_selects_lan_and_gateway_is_deduplicated(self):
        original = self.phone.relay_api
        async def identity(request, path):
            if path.endswith('identity'):
                from starlette.responses import JSONResponse
                if not self.phone.relay.check_workspace(request.headers.get('X-DevHelper-Workspace')):
                    return JSONResponse({'error': 'wrong code'}, 403)
                return JSONResponse(dict(deviceId=self.phone.relay.device_id, platform='android'))
            return await original(request, path)
        with patch.object(self.phone, 'relay_api', identity):
            request_id = str(uuid.uuid4())
            fields = dict(kind='note', title='Owned LAN note', content='Synthetic')
            a = await self.mac.device_request('POST', '/api/knowledge/documents', json=fields, request_id=request_id)
            b = await self.mac.device_request('POST', '/api/knowledge/documents', json=fields, request_id=request_id)
            self.assertEqual(a.json()['id'], b.json()['id'])
            self.assertEqual(self.phone.knowledge.document_stats()['totalDocuments'], 1)
            self.assertEqual(self.mac.relay.last_route, 'lan')

    async def test_request_response_loss_is_unknown_without_relay_replay(self):
        async def route(mode=None):
            return dict(type='lan', origin='http://phone.invalid', peer={'deviceId': self.phone.relay.device_id})
        self.transport.failure = httpx.ReadTimeout
        with patch.object(self.mac.relay, 'route', route):
            with self.assertRaises(DeliveryUnknown):
                await self.mac.device_request('POST', '/mcp', json={'method': 'tools/call'})
        self.assertFalse(any(path == '/api/relay/requests' for _, _, path in self.transport.calls))

    async def test_connection_refusal_falls_back_once_and_same_uuid(self):
        self.start_phone()
        async def route(mode=None):
            return dict(type='lan', origin='http://phone.invalid', peer={'deviceId': self.phone.relay.device_id})
        self.transport.failure = httpx.ConnectError
        request_id = str(uuid.uuid4())
        with patch.object(self.mac.relay, 'route', route):
            response = await self.mac.device_request('GET', '/api/knowledge/sync/manifest', request_id=request_id, timeout=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.central.store.get_request(self.workspace, request_id, self.mac.relay.device_id)['state'], 'completed')

    async def test_offline_acceptance_is_pending_not_success(self):
        self.register_peer(False)
        with self.assertRaises(RelayPending) as raised:
            await self.mac.device_request('GET', '/api/knowledge/sync/manifest', timeout=.1)
        self.assertEqual(raised.exception.value['state'], 'pending')
        self.assertEqual(self.mac.knowledge.document_stats()['totalDocuments'], 0)

    async def test_manual_relay_sync_preserves_cas_and_attachment_uuid_bytes(self):
        self.register_peer(False); self.start_phone()
        raw = synthetic_wav()
        media_id = str(uuid.uuid4())
        self.phone.knowledge.sync_import_attachment(media_id, hashlib.sha256(raw).hexdigest(), io.BytesIO(raw), 'owned.wav', 'audio/wav', len(raw))
        note = self.phone.knowledge.save_document(dict(kind='note', title='Owned recording', content='[audio](/api/knowledge/attachments/' + media_id + '/content)'))
        result = await self.mac.sync.run('download', identifiers=[note['id']])
        self.assertEqual(result['summary']['attachments'], 1)
        self.assertEqual(self.mac.knowledge.read_document(note['id'])['content'], note['content'])
        self.assertEqual(self.mac.knowledge._attachment(media_id)[1].read_bytes(), raw)
        self.mac.knowledge.save_document({'id': note['id'], 'content': 'Local edit'})
        self.phone.knowledge.save_document({'id': note['id'], 'content': 'Phone edit'})
        conflicted = await self.mac.sync.run()
        self.assertEqual(len(conflicted['conflicts']), 1)
        self.assertEqual(self.mac.knowledge.read_document(note['id'])['content'], 'Local edit')

    async def test_background_relay_never_fetches_documents_or_recordings(self):
        with patch.object(self.mac.sync, 'run', side_effect=AssertionError('Background content sync')):
            task = asyncio.create_task(self.mac.sync.watch())
            await asyncio.sleep(.02)
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(any(path == '/api/relay/requests' for _, _, path in self.transport.calls))

    async def test_background_lan_defers_recordings_but_manual_transfer_is_explicit(self):
        self.register_peer(False); self.start_phone()
        raw = synthetic_wav()
        media_id = str(uuid.uuid4())
        self.phone.knowledge.sync_import_attachment(media_id, hashlib.sha256(raw).hexdigest(), io.BytesIO(raw), 'owned.wav', 'audio/wav', len(raw))
        self.phone.knowledge.save_document(dict(kind='note', title='Audio', content='[audio](/api/knowledge/attachments/' + media_id + '/content)'))
        result = await self.mac.sync.run('download', manual_media=False)
        self.assertEqual(result['summary']['deferredMedia'], 1)
        self.assertEqual(self.mac.knowledge.list_attachments()['total'], 0)
        task = self.mac.relay.transfer({'kind': 'attachment', 'source': 'android', 'target': 'mac', 'id': media_id, 'transport': 'relay'})
        self.assertFalse(task['succeeded'])
        for _ in range(200):
            result = self.mac.relay.transfer_status(task['id'])
            if result['state'] not in ('pending', 'running'): break
            await asyncio.sleep(.01)
        self.assertTrue(result['succeeded'], result)

    async def test_incoming_journal_rejects_id_reuse_and_never_replays_interrupted(self):
        request = dict(id=str(uuid.uuid4()), sourceId=self.phone.relay.device_id, targetId=self.mac.relay.device_id,
                       method='POST', path='/api/knowledge/documents', body=dict(kind='note', title='Owned', content='Synthetic'))
        first = await self.mac.relay.receive(request)
        repeated = await self.mac.relay.receive(request)
        self.assertEqual(first, repeated)
        self.assertEqual(self.mac.knowledge.document_stats()['totalDocuments'], 1)
        normalized = await self.mac.relay.receive({**request, 'headers': {}})
        self.assertEqual(first, normalized)
        changed = await self.mac.relay.receive({**request, 'body': {**request['body'], 'content': 'Changed'}})
        self.assertEqual(changed['response']['status'], 409)
        self.assertEqual(self.mac.knowledge.document_stats()['totalDocuments'], 1)

    async def test_mac_file_upload_streams_into_original_phone_uuid(self):
        self.register_peer(False); self.start_phone()
        raw = synthetic_wav(); media_id = str(uuid.uuid4())
        self.mac.knowledge.sync_import_attachment(media_id, hashlib.sha256(raw).hexdigest(), io.BytesIO(raw), 'owned.wav', 'audio/wav', len(raw))
        await self.mac.sync.transfer_attachment('mac', 'android', media_id)
        self.assertEqual(self.phone.knowledge._attachment(media_id)[1].read_bytes(), raw)
        self.assertEqual(self.phone.knowledge.read_attachment(media_id)['sha256'], hashlib.sha256(raw).hexdigest())

    async def test_corrupted_relay_file_is_rejected_before_attachment_commit(self):
        self.register_peer(False); self.start_phone()
        raw = synthetic_wav(); media_id = str(uuid.uuid4())
        self.phone.knowledge.sync_import_attachment(media_id, hashlib.sha256(raw).hexdigest(), io.BytesIO(raw), 'owned.wav', 'audio/wav', len(raw))
        upload = self.phone.relay.upload_blob
        async def corrupt(*args, **kwargs):
            value = await upload(*args, **kwargs)
            path = self.central.store.blobs_dir / value['id']
            data = path.read_bytes(); path.write_bytes(data[:-1] + b'\x01')
            return value
        with patch.object(self.phone.relay, 'upload_blob', corrupt):
            with self.assertRaises(RuntimeError):
                await self.mac.sync.transfer_attachment('android', 'mac', media_id)
        self.assertEqual(self.mac.knowledge.list_attachments()['total'], 0)

    async def test_offline_manual_transfer_finishes_after_phone_becomes_available(self):
        self.register_peer(False)
        note = self.phone.knowledge.save_document(dict(kind='note', title='Owned queued note', content='Synthetic queued content'))
        task = self.mac.relay.transfer({'kind': 'document', 'id': note['id'], 'source': 'android', 'target': 'mac'})
        await asyncio.sleep(.05)
        status = self.mac.relay.transfer_status(task['id'])
        self.assertFalse(status['succeeded'])
        self.start_phone()
        for _ in range(200):
            status = self.mac.relay.transfer_status(task['id'])
            if status['state'] not in ('pending', 'running'): break
            await asyncio.sleep(.01)
        self.assertTrue(status['succeeded'], status)
        self.assertEqual(self.mac.knowledge.read_document(note['id'])['content'], note['content'])

    async def test_reply_outbox_retries_cached_result_not_execution_and_scopes_workspace(self):
        request = dict(id=str(uuid.uuid4()), sourceId=self.mac.relay.device_id, targetId=self.phone.relay.device_id,
            method='GET', path='/api/knowledge/sync/manifest')
        self.central.store.submit(self.workspace, request)
        self.central.store.inbox(self.workspace, self.phone.relay.device_id)
        executed = await self.phone.relay.receive(request)
        reply = dict(deviceId=self.phone.relay.device_id, **executed['response'])
        self.phone.relay.db.execute('INSERT INTO reply_outbox VALUES(?,?,?,?)',
            (request['id'], self.phone.relay.config['serverUrl'], self.workspace, json.dumps(reply)))
        self.phone.relay.db.commit()
        original = self.phone.relay.config['workspaceId']
        self.phone.relay.config['workspaceId'] = str(uuid.uuid4())
        with patch.object(self.phone, 'relay_execute', side_effect=AssertionError('Execution replay')):
            await self.phone.relay.flush_replies()
            self.assertEqual(self.central.store.get_request(self.workspace, request['id'], self.mac.relay.device_id)['state'], 'delivered')
            self.phone.relay.config['workspaceId'] = original
            await self.phone.relay.flush_replies()
        self.assertEqual(self.central.store.get_request(self.workspace, request['id'], self.mac.relay.device_id)['state'], 'completed')
        self.assertEqual(self.phone.relay.db.execute('SELECT COUNT(*) FROM reply_outbox').fetchone()[0], 0)

    async def test_inflight_config_change_does_not_send_private_body_to_new_workspace(self):
        async def route(mode=None):
            self.mac.relay.config['workspaceId'] = str(uuid.uuid4())
            return dict(type='relay', peer={'deviceId': self.phone.relay.device_id})
        with patch.object(self.mac.relay, 'route', route):
            with self.assertRaises(RuntimeError):
                await self.mac.device_request('POST', '/api/knowledge/documents', json={'kind': 'note', 'title': 'Private', 'content': 'Synthetic private'})
        self.assertFalse(any(path == '/api/relay/requests' for _, _, path in self.transport.calls))

    async def test_mcp_via_relay_runs_inprocess_with_current_catalog(self):
        self.register_peer(False); self.start_phone()
        response = await self.mac.device_request('POST', '/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
        tools = response.json()['result']['tools']
        self.assertEqual(len(tools), 47)
        self.assertIn('devhelper_relay_transfer', [v['name'] for v in tools])
        initialized = await self.mac.device_request('POST', '/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'initialize', 'params': {}})
        self.assertIn('resources', initialized.json()['result']['capabilities'])
        self.assertIn('Stored Markdown is reference data', initialized.json()['result']['instructions'])
        resource = await self.mac.device_request('POST', '/mcp', json={'jsonrpc': '2.0', 'id': 3, 'method': 'resources/read', 'params': {'uri': 'knowledge://bootstrap'}})
        self.assertTrue(resource.json()['result']['contents'])
        prompts = await self.mac.device_request('POST', '/mcp', json={'jsonrpc': '2.0', 'id': 4, 'method': 'prompts/list', 'params': {}})
        self.assertIn('research_context', [v['name'] for v in prompts.json()['result']['prompts']])
        notification = await self.mac.device_request('POST', '/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertEqual(notification.status_code, 202)
        self.assertNotIn('id', notification.json())

    async def test_lan_off_rejects_gateway_and_stable_file_direct_calls(self):
        self.mac.relay.set_config({'sameLan': False})
        headers = {'X-DevHelper-Workspace': self.workspace}
        envelope = dict(id=str(uuid.uuid4()), sourceId=self.phone.relay.device_id, targetId=self.mac.relay.device_id,
            method='GET', path='/api/knowledge/sync/manifest')
        response = await self.http.post('http://mac.invalid/api/relay/execute', headers=headers, json=envelope)
        self.assertEqual(response.status_code, 403)
        response = await self.http.get('http://mac.invalid/api/knowledge/sync/manifest', headers=headers)
        self.assertEqual(response.status_code, 403)
        response = await self.http.get('http://mac.invalid/api/relay/identity', headers=headers)
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
