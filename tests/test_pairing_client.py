"""Phone-approved short-code pairing uses only isolated synthetic workspaces."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import uuid

import httpx
from relay.server import RelayServer
from relay_client import RelayClient
from server import Desktop
from test_relay_client import MultiHost


class PairingClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.central = RelayServer(self.root / 'central')
        self.mac = Desktop(11111, self.root / 'mac', self.root / 'shared')
        self.phone_id = str(uuid.uuid4())
        self.old_workspace = self.central.store.create_workspace()['workspaceId']
        self.workspace = self.central.store.create_workspace()['workspaceId']
        self.central.store.register(self.workspace, dict(deviceId=self.phone_id, platform='android', name='Synthetic phone',
            sameLan=False, lanAddresses=[], catalog={}))
        self.pairing = self.central.store.create_pairing(self.workspace, {'deviceId': self.phone_id})
        self.transport = MultiHost({'pair.invalid': self.central.app(), 'mac.invalid': self.mac.app()})
        self.http = httpx.AsyncClient(transport=self.transport, trust_env=False)
        self.mac.http = self.http
        self.mac.relay.set_config(dict(serverUrl='https://pair.invalid', workspaceId=self.old_workspace, enabled=True))

    async def asyncTearDown(self):
        await self.mac.relay.stop()
        await self.http.aclose()
        self.mac.relay.close(); self.mac.workflows.close(); self.mac.knowledge.close(); self.central.store.close()
        self.tmp.cleanup()

    def fields(self):
        return dict(serverUrl='https://pair.invalid', code=self.pairing['code'], name='Synthetic Mac')

    def approve(self, request_id, approval=True):
        self.central.store.decide_pairing(self.workspace, self.pairing['id'],
            dict(deviceId=self.phone_id, requestId=request_id, approve=approval))

    async def test_pending_is_redacted_and_does_not_replace_legacy_config(self):
        response = await self.http.post('http://mac.invalid/api/relay/pair', json=self.fields())
        self.assertEqual(response.status_code, 202)
        public = response.json()
        self.assertEqual(public['state'], 'pendingApproval'); self.assertFalse(public['succeeded'])
        self.assertEqual(self.mac.relay.config['workspaceId'], self.old_workspace)
        secret = self.mac.relay.pair_value(public['id'])['receiptSecret']
        for path in ('/api/relay/config', '/api/relay/status', '/api/relay/pair/' + public['id']):
            value = (await self.http.get('http://mac.invalid' + path)).text
            for private in (secret, self.workspace, self.old_workspace, self.pairing['code']):
                self.assertNotIn(private, value)
        self.assertGreaterEqual(len(secret), 43)
        self.assertEqual((self.root / 'mac/relay/client.sqlite3').stat().st_mode & 0o777, 0o600)

    async def test_approval_auto_connects_and_selects_approving_phone(self):
        pending = await self.mac.relay.pair(self.fields()); self.approve(pending['id'])
        with patch('relay_client.local_ips', return_value=[]):
            result = await self.mac.relay.refresh_pair(pending['id'])
        self.assertEqual(result['state'], 'approved'); self.assertTrue(result['succeeded'])
        self.assertEqual(self.mac.relay.config['workspaceId'], self.workspace)
        self.assertEqual(self.mac.relay.config['targetDeviceId'], self.phone_id)
        self.assertTrue(self.mac.relay.online)
        peers = self.central.store.devices(self.workspace)['devices']
        self.assertTrue(any(row['deviceId'] == self.mac.relay.device_id for row in peers))
        self.assertNotIn('receiptSecret', self.mac.relay.pair_value(pending['id']))

    async def test_retry_and_restart_reuse_private_receipt_and_join_id(self):
        pending = await self.mac.relay.pair(self.fields())
        secret = self.mac.relay.pair_value(pending['id'])['receiptSecret']
        retried = await self.mac.relay.pair(self.fields())
        self.assertEqual(retried['id'], pending['id'])
        self.mac.relay.close(); self.mac.relay = RelayClient(self.mac, self.root / 'mac')
        self.assertEqual(self.mac.relay.pair_value(pending['id'])['receiptSecret'], secret)
        self.approve(pending['id'])
        with patch('relay_client.local_ips', return_value=[]):
            await self.mac.relay.refresh_pair(pending['id'])
        count = self.central.store.db.execute('SELECT COUNT(*) FROM pairing_requests').fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.mac.relay.pair_status(pending['id'])['state'], 'approved')

    async def test_decline_and_expiry_never_apply_workspace(self):
        pending = await self.mac.relay.pair(self.fields()); self.approve(pending['id'], False)
        result = await self.mac.relay.refresh_pair(pending['id'])
        self.assertEqual(result['state'], 'declined')
        self.assertEqual(self.mac.relay.config['workspaceId'], self.old_workspace)
        self.pairing = self.central.store.create_pairing(self.workspace, {'deviceId': self.phone_id})
        pending = await self.mac.relay.pair(self.fields())
        self.central.store.db.execute('UPDATE pairing_requests SET expires=0 WHERE id=?', (pending['id'],)); self.central.store.db.commit()
        result = await self.mac.relay.refresh_pair(pending['id'])
        self.assertEqual(result['state'], 'expired')
        self.assertEqual(self.mac.relay.config['workspaceId'], self.old_workspace)

    async def test_connection_changed_while_waiting_is_not_overwritten(self):
        pending = await self.mac.relay.pair(self.fields()); self.approve(pending['id'])
        self.mac.relay.set_config({'name': 'Changed setting'})
        result = await self.mac.relay.refresh_pair(pending['id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(self.mac.relay.config['workspaceId'], self.old_workspace)

    async def test_lost_join_response_retries_exact_same_request(self):
        original = self.transport.handle_async_request
        lost = True
        async def transport(request):
            nonlocal lost
            response = await original(request)
            if request.url.path.endswith('/pairing/join') and lost:
                lost = False
                raise httpx.ReadTimeout('Synthetic response lost', request=request)
            return response
        self.transport.handle_async_request = transport
        first = await self.mac.relay.pair(self.fields())
        self.assertEqual(first['state'], 'waitingNetwork')
        self.approve(first['id'])
        with patch('relay_client.local_ips', return_value=[]):
            second = await self.mac.relay.pair(self.fields())
        self.assertEqual(second['id'], first['id']); self.assertEqual(second['state'], 'approved')
        self.assertEqual(self.central.store.db.execute('SELECT COUNT(*) FROM pairing_requests').fetchone()[0], 1)

    async def test_approval_before_deadline_can_be_received_after_deadline(self):
        pending = await self.mac.relay.pair(self.fields()); self.approve(pending['id'])
        value = self.mac.relay.pair_value(pending['id']); value['expiresAt'] = 0; self.mac.relay.save_pair(value)
        with patch('relay_client.local_ips', return_value=[]):
            result = await self.mac.relay.refresh_pair(pending['id'])
        self.assertEqual(result['state'], 'approved')

    async def test_invalid_code_and_redirect_never_apply_credentials(self):
        with self.assertRaises(ValueError): await self.mac.relay.pair(dict(serverUrl='https://pair.invalid', code='abcdef'))
        with self.assertRaises(ValueError): await self.mac.relay.pair(dict(serverUrl='https://pair.invalid', code=123456))
        code = '000000' if self.pairing['code'] != '000000' else '000001'
        result = await self.mac.relay.pair(dict(serverUrl='https://pair.invalid', code=code))
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.mac.relay.config['workspaceId'], self.old_workspace)
        self.http._transport = httpx.MockTransport(lambda request: httpx.Response(307, headers={'Location':'https://other.invalid'}))
        result = await self.mac.relay.pair(self.fields())
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.mac.relay.config['workspaceId'], self.old_workspace)

    async def test_background_poll_connects_without_browser_status_poll(self):
        pending = await self.mac.relay.pair(self.fields()); self.approve(pending['id'])
        with patch('relay_client.local_ips', return_value=[]):
            worker = asyncio.create_task(self.mac.relay.pairing_loop())
            try:
                for _ in range(100):
                    if self.mac.relay.pair_status(pending['id'])['state'] == 'approved': break
                    await asyncio.sleep(.01)
                self.assertEqual(self.mac.relay.pair_status(pending['id'])['state'], 'approved')
            finally:
                worker.cancel(); await asyncio.gather(worker, return_exceptions=True)

    async def test_clock_skew_uses_server_remaining_time_and_reuses_pending_request(self):
        import time
        server_stamp = int(time.time() * 1000)
        for skew in (-600000, 600000):
            with self.subTest(skew=skew), patch('relay.server.now', return_value=server_stamp), patch('relay_client.time.time', return_value=(server_stamp + skew) / 1000):
                self.pairing = self.central.store.create_pairing(self.workspace, {'deviceId': self.phone_id})
                pending = await self.mac.relay.pair(self.fields())
                self.assertEqual(pending['state'], 'pendingApproval')
                self.assertGreater(pending['localExpiresAt'], server_stamp + skew)
                self.assertEqual(pending['localExpiresAt'] - (server_stamp + skew), pending['expiresAt'] - server_stamp)
                retried = await self.mac.relay.pair(self.fields())
                self.assertEqual(retried['id'], pending['id'])
                self.approve(pending['id'])
                with patch('relay_client.local_ips', return_value=[]):
                    approved = await self.mac.relay.refresh_pair(pending['id'])
                self.assertEqual(approved['state'], 'approved')

    async def test_legacy_response_without_server_clock_defers_expiry_to_server(self):
        original = self.transport.handle_async_request
        async def legacy(request):
            response = await original(request)
            if '/pairing/' in request.url.path and response.status_code < 400:
                await response.aread(); body = response.json(); body.pop('serverTime', None)
                return httpx.Response(response.status_code, json=body)
            return response
        self.transport.handle_async_request = legacy
        pending = await self.mac.relay.pair(self.fields())
        self.assertEqual(pending['state'], 'pendingApproval')
        self.assertNotIn('localExpiresAt', pending)
        value = self.mac.relay.pair_value(pending['id']); value['expiresAt'] = 0; self.mac.relay.save_pair(value)
        retried = await self.mac.relay.pair(self.fields())
        self.assertEqual(retried['id'], pending['id']); self.assertEqual(retried['state'], 'pendingApproval')


if __name__ == '__main__': unittest.main()
