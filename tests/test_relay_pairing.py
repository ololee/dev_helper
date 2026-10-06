import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay.server import RelayServer, RelayStore, now


def uid():
    return str(uuid.uuid4())


class RelayPairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = RelayServer(self.temp.name)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app()), base_url='http://relay.test')
        self.workspace = (await self.client.post('/api/relay/workspaces', json={})).json()['workspaceId']
        self.headers = {'X-DevHelper-Workspace': self.workspace}
        self.phone, self.other_phone = uid(), uid()
        for device in (self.phone, self.other_phone):
            await self.client.post('/api/relay/register', headers=self.headers,
                                   json={'deviceId': device, 'platform': 'android', 'name': 'Test phone'})

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.store.close()
        self.temp.cleanup()

    async def create(self):
        response = await self.client.post('/api/relay/pairing', headers=self.headers, json={'deviceId': self.phone})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertRegex(response.json()['code'], r'^[0-9]{6}$')
        return response.json()

    def join_body(self, pair):
        return {'id': uid(), 'receiptSecret': secrets.token_urlsafe(32), 'code': pair['code'],
                'deviceId': uid(), 'platform': 'mac', 'name': 'Test computer'}

    async def status(self, body, receipt=None):
        return await self.client.get('/api/relay/pairing/requests/' + body['id'],
            headers={'X-DevHelper-Pairing': receipt or body['receiptSecret']})

    async def decision(self, pair, body, approve=True, device=None):
        return await self.client.post('/api/relay/pairing/' + pair['id'] + '/decision', headers=self.headers,
            json={'deviceId': device or self.phone, 'requestId': body['id'], 'approve': approve})

    async def test_short_code_requires_phone_approval_before_secret_and_is_single_use(self):
        pair = await self.create()
        body = self.join_body(pair)
        joined = await self.client.post('/api/relay/pairing/join', json=body)
        self.assertEqual(joined.status_code, 202)
        self.assertNotIn('workspaceId', joined.json())
        self.assertNotIn('workspaceId', (await self.status(body)).json())
        pending = await self.client.get('/api/relay/pairing/' + pair['id'] + '/requests', headers=self.headers,
                                       params={'deviceId': self.phone})
        self.assertEqual(pending.json()['requests'][0]['name'], body['name'])
        self.assertNotIn('receiptSecret', pending.text)
        self.assertEqual((await self.decision(pair, body)).json()['state'], 'approved')
        self.assertEqual((await self.status(body)).json()['workspaceId'], self.workspace)
        self.assertEqual((await self.status(body, secrets.token_urlsafe(32))).status_code, 404)
        self.assertEqual((await self.client.post('/api/relay/pairing/join', json=self.join_body(pair))).status_code, 404)
        self.assertEqual((await self.decision(pair, body)).json()['state'], 'approved')
        self.assertEqual((await self.client.delete('/api/relay/pairing/' + pair['id'], headers=self.headers,
                        params={'deviceId': self.phone})).status_code, 409)

    async def test_join_retry_is_idempotent_and_changed_identity_conflicts(self):
        pair, body = await self.create(), None
        body = self.join_body(pair)
        first = await self.client.post('/api/relay/pairing/join', json=body)
        repeated = (await self.client.post('/api/relay/pairing/join', json=body)).json()
        self.assertEqual({k: v for k, v in repeated.items() if k != 'serverTime'},
                         {k: v for k, v in first.json().items() if k != 'serverTime'})
        self.assertGreaterEqual(repeated['serverTime'], first.json()['serverTime'])
        self.assertEqual((await self.client.post('/api/relay/pairing/join', json={**body, 'name': 'Changed'})).status_code, 409)
        await self.decision(pair, body)
        self.assertEqual((await self.client.post('/api/relay/pairing/join', json=body)).json()['state'], 'approved')

    async def test_only_owner_and_workspace_can_decide(self):
        pair = await self.create()
        body = self.join_body(pair)
        await self.client.post('/api/relay/pairing/join', json=body)
        self.assertEqual((await self.decision(pair, body, device=self.other_phone)).status_code, 404)
        self.assertEqual((await self.client.get('/api/relay/pairing/' + pair['id'] + '/requests')).status_code, 401)
        other = (await self.client.post('/api/relay/workspaces', json={})).json()['workspaceId']
        response = await self.client.post('/api/relay/pairing/' + pair['id'] + '/decision',
            headers={'X-DevHelper-Workspace': other}, json={'deviceId': self.phone, 'requestId': body['id'], 'approve': True})
        self.assertIn(response.status_code, (404, 403))
        self.assertEqual((await self.status(body)).json()['state'], 'pendingApproval')

    async def test_declining_and_approving_competing_requests_never_leaks_secret(self):
        pair = await self.create()
        bodies = [self.join_body(pair) for _ in range(3)]
        for body in bodies:
            await self.client.post('/api/relay/pairing/join', json=body)
        await self.decision(pair, bodies[0], False)
        self.assertEqual((await self.status(bodies[0])).json()['state'], 'declined')
        self.assertNotIn('workspaceId', (await self.status(bodies[0])).json())
        await self.decision(pair, bodies[1])
        self.assertEqual((await self.status(bodies[2])).json()['state'], 'declined')
        self.assertNotIn('workspaceId', (await self.status(bodies[2])).json())
        self.assertEqual((await self.decision(pair, bodies[2])).status_code, 409)

    async def test_expiry_and_approved_receipt_grace(self):
        pair = await self.create()
        body = self.join_body(pair)
        await self.client.post('/api/relay/pairing/join', json=body)
        with patch('relay.server.now', return_value=pair['expiresAt'] - 1000):
            approved = await self.decision(pair, body)
        self.assertGreater(approved.json()['expiresAt'], pair['expiresAt'])
        with patch('relay.server.now', return_value=pair['expiresAt'] + 1000):
            self.assertIn('workspaceId', (await self.status(body)).json())
        with patch('relay.server.now', return_value=approved.json()['expiresAt']):
            self.assertNotIn('workspaceId', (await self.status(body)).json())
            self.assertEqual((await self.status(body)).json()['state'], 'expired')
        pair = await self.create()
        with patch('relay.server.now', return_value=pair['expiresAt']):
            self.assertEqual((await self.client.post('/api/relay/pairing/join', json=self.join_body(pair))).status_code, 404)

    async def test_regeneration_cancellation_and_restart_preserve_decisions(self):
        pair = await self.create()
        body = self.join_body(pair)
        await self.client.post('/api/relay/pairing/join', json=body)
        await self.create()
        self.assertEqual((await self.status(body)).json()['state'], 'cancelled')
        pair = await self.create()
        body = self.join_body(pair)
        await self.client.post('/api/relay/pairing/join', json=body)
        self.server.store.close()
        self.server.store = RelayStore(self.temp.name)
        self.assertEqual((await self.status(body)).json()['state'], 'pendingApproval')
        await self.client.delete('/api/relay/pairing/' + pair['id'], headers=self.headers, params={'deviceId': self.phone})
        self.assertEqual((await self.status(body)).json()['state'], 'cancelled')

    async def test_failed_guess_rate_limit_is_durable_and_does_not_block_exact_retry(self):
        pair = await self.create()
        body = self.join_body(pair)
        await self.client.post('/api/relay/pairing/join', json=body)
        for _ in range(10):
            wrong = self.join_body(pair)
            wrong['code'] = '000000' if pair['code'] != '000000' else '111111'
            self.assertEqual((await self.client.post('/api/relay/pairing/join', json=wrong)).status_code, 404)
        self.server.store.close()
        self.server.store = RelayStore(self.temp.name)
        self.assertEqual((await self.client.post('/api/relay/pairing/join', json=self.join_body(pair))).status_code, 429)
        self.assertEqual((await self.client.post('/api/relay/pairing/join', json=body)).status_code, 202)
        with patch('relay.server.now', return_value=now() + 901000):
            fresh = await self.create()
            self.assertEqual((await self.client.post('/api/relay/pairing/join', json=self.join_body(fresh))).status_code, 202)

    async def test_database_and_unapproved_responses_do_not_store_plain_code_or_receipt(self):
        pair = await self.create()
        body = self.join_body(pair)
        await self.client.post('/api/relay/pairing/join', json=body)
        stored = json.dumps([dict(row) for row in self.server.store.db.execute('SELECT * FROM pairing_sessions')])
        stored += json.dumps([dict(row) for row in self.server.store.db.execute('SELECT * FROM pairing_requests')])
        self.assertNotIn(body['receiptSecret'], stored)
        self.assertNotIn('"code":', stored)
        self.assertNotIn(self.workspace, (await self.status(body)).text)
