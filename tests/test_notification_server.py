import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_server as server_tests
from notifications import BASE
from relay_client import RelayPending


class NotificationServerTests(unittest.TestCase):
    setUp = server_tests.ServerTests.setUp
    tearDown = server_tests.ServerTests.tearDown
    rpc = server_tests.ServerTests.rpc

    def test_alert_api_and_mcp_preserve_dnd_suppression(self):
        self.desktop.notifications.router = AsyncMock(return_value={
            'status': 'suppressed', 'reason': 'do_not_disturb', 'delivered': False})
        api = self.client.post(BASE + 'notify', json={'eventId': 'synthetic-complete'})
        self.assertEqual(api.status_code, 200, api.text)
        self.assertEqual(api.json()['reason'], 'do_not_disturb')
        mcp = self.rpc('tools/call', {'name': 'devhelper_notification_notify', 'arguments': {'eventId': 'synthetic-complete'}})
        self.assertFalse(mcp['result']['isError'])
        self.assertFalse(mcp['result']['structuredContent']['delivered'])
        self.assertEqual(mcp['result']['structuredContent']['status'], 'suppressed')

    def test_notification_settings_and_invalid_fields(self):
        router = AsyncMock(return_value={'enabled': True, 'respectDnd': True})
        self.desktop.notifications.router = router
        self.assertEqual(self.client.post(BASE + 'config', json={'enabled': True, 'sound': False}).status_code, 200)
        router.assert_awaited_with('android', 'POST', BASE + 'config', {'enabled': True, 'sound': False})
        for path, body in [('config', {'respectDnd': False}), ('notify', {}), ('notify', {'eventId': 'one', 'deepseekApiKey': 'synthetic'})]:
            self.assertEqual(self.client.post(BASE + path, json=body).status_code, 400)
        self.assertEqual(router.await_count, 1)

    def test_disconnected_phone_is_not_queued(self):
        self.desktop.find_android = AsyncMock(return_value=None)
        self.desktop.device_request = AsyncMock()
        self.desktop.relay.request = AsyncMock()
        result = asyncio.run(self.desktop.phone_notification_request('android', 'POST', BASE + 'notify', {'eventId': 'one'}))
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['delivered'])
        self.desktop.device_request.assert_not_awaited()
        self.desktop.relay.request.assert_not_awaited()

    def test_unconfirmed_relay_alert_cancels_only_its_own_queue_entry(self):
        self.desktop.find_android = AsyncMock(return_value='relay')
        self.desktop.relay.enabled = lambda: True
        self.desktop.relay.request = AsyncMock(side_effect=RelayPending({'state': 'pending'}))
        self.desktop.relay.api = AsyncMock()
        result = asyncio.run(self.desktop.phone_notification_request('android', 'POST', BASE + 'notify', {'eventId': 'one'}))
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['delivered'])
        request_id = self.desktop.relay.request.await_args.kwargs['request_id']
        self.assertEqual(self.desktop.relay.api.await_args.args, ('DELETE', '/api/relay/requests/' + request_id))
        self.desktop.relay.request.assert_awaited_once()
