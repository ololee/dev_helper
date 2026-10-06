import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import jsonschema

from notifications import BASE, NotificationError, PhoneNotifications
from relay_client import TRANSPORT
from workflows import WorkflowError, WorkflowService
from test_workflows import FakeDesktop


class PhoneNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router = AsyncMock(return_value=dict(status='posted', delivered=True))
        self.phone = PhoneNotifications(self.router)

    async def test_routes_config_status_and_explicit_notification_to_phone(self):
        self.router.return_value = dict(enabled=False, respectDnd=True)
        await self.phone.call_tool('devhelper_notification_config')
        self.router.assert_awaited_with('android', 'GET', BASE + 'config', None)
        await self.phone.call_tool('devhelper_notification_config', dict(settings=dict(enabled=True, sound=False)))
        self.router.assert_awaited_with('android', 'POST', BASE + 'config', dict(enabled=True, sound=False))
        await self.phone.call_tool('devhelper_notification_status')
        self.router.assert_awaited_with('android', 'GET', BASE + 'status', None)
        self.router.return_value = dict(status='posted', delivered=True)
        result = await self.phone.notify(dict(eventId='completed-1', title='合成标题', message='合成结果', nextStep='继续？', taskId='synthetic', expiresAt=123456))
        self.assertTrue(result['delivered'])
        self.assertEqual(result['eventId'], 'completed-1')
        self.router.assert_awaited_with('android', 'POST', BASE + 'notify',
                                      dict(eventId='completed-1', title='合成标题', message='合成结果', nextStep='继续？', taskId='synthetic', expiresAt=123456))

    async def test_notify_defaults_and_schema_match_runtime_validation(self):
        await self.phone.notify(dict(eventId='completed-2'))
        fields = self.router.await_args.args[3]
        self.assertEqual(fields['title'], '任务已完成')
        self.assertEqual(fields['nextStep'], '下一步需要我做什么？')
        self.assertGreater(fields['expiresAt'], 0)
        schemas = {item['name']: item for item in self.phone.tool_specs()}
        jsonschema.validate(dict(eventId='completed-2'), schemas['devhelper_notification_notify']['inputSchema'])
        jsonschema.validate(dict(settings=dict(enabled=True, vibrate=False)), schemas['devhelper_notification_config']['inputSchema'])
        self.assertTrue(schemas['devhelper_notification_notify']['annotations']['idempotentHint'])
        self.assertTrue(schemas['devhelper_notification_status']['annotations']['readOnlyHint'])

    async def test_invalid_settings_and_payloads_do_not_send_any_request(self):
        for fields in ({}, {'eventId': ''}, {'eventId': 'x\n'}, {'eventId': 'x', 'title': 'x' * 121},
                       {'eventId': 'x', 'message': 1}, {'eventId': 'x', 'unknown': True}, {'eventId': 'x\x85'}):
            with self.subTest(fields=fields), self.assertRaises(NotificationError):
                await self.phone.notify(fields)
        for expiry in (False, 0, 253402300800000, 123.4, '123'):
            with self.subTest(expiry=expiry), self.assertRaises(NotificationError):
                await self.phone.notify(dict(eventId='x', expiresAt=expiry))
        for fields in ({'settings': None}, {'settings': {'enabled': 'true'}}, {'settings': {'respectDnd': False}}, {'enabled': True}):
            with self.subTest(fields=fields), self.assertRaises(NotificationError):
                await self.phone.call_tool('devhelper_notification_config', fields)
        self.router.assert_not_awaited()

    async def test_suppressed_duplicate_and_offline_never_become_delivery_success(self):
        for status, reason in (('suppressed', 'dnd'), ('suppressed', 'disabled'), ('duplicate', 'already_seen')):
            self.router.return_value = dict(status=status, reason=reason, delivered=False)
            result = await self.phone.notify(dict(eventId='completed-3'))
            self.assertEqual(result['status'], status)
            self.assertEqual(result['reason'], reason)
            self.assertFalse(result['delivered'])
        request = httpx.Request('POST', 'http://phone.test' + BASE + 'notify')
        response = httpx.Response(503, request=request)
        self.router.side_effect = httpx.HTTPStatusError('offline', request=request, response=response)
        result = await self.phone.notify(dict(eventId='completed-3'))
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['delivered'])

    async def test_lost_response_is_unknown_and_not_retried(self):
        self.router.side_effect = httpx.ReadTimeout('synthetic response lost')
        result = await self.phone.notify(dict(eventId='completed-4'))
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['delivered'])
        self.router.assert_awaited_once()

    async def test_default_expiration_prevents_late_relay_delivery(self):
        with patch('notifications.time.time', return_value=1000):
            await self.phone.notify(dict(eventId='expiry-1'))
        self.assertEqual(self.router.await_args.args[3]['expiresAt'], 1020000)

    async def test_invalid_or_error_response_is_never_delivered(self):
        for raw in (None, {'ok': True}, {'isError': True}, {'error': 'synthetic'}):
            self.router.return_value = raw
            result = await self.phone.notify(dict(eventId='completed-5'))
            self.assertFalse(result['delivered'])
            self.assertIn(result['status'], ('unknown', 'failed'))


class WorkflowCompletionAlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.desktop = FakeDesktop(self.root, lambda _: (_ for _ in ()).throw(AssertionError('No cloud request expected')))
        self.service = WorkflowService(self.desktop, self.root / 'data')
        self.notifier = AsyncMock(return_value=dict(status='posted', delivered=True))
        self.service.bind_phone_notifier(self.notifier)

    async def asyncTearDown(self):
        await self.service.stop()
        self.service.close()
        await self.desktop.http.aclose()
        self.desktop.knowledge.close()
        self.desktop.phone.close()
        self.tmp.cleanup()

    def tool_task(self, **fields):
        return self.service.submit(dict(type='tool', toolName='synthetic_echo', arguments={'value': 'private-result'}, **fields))

    async def finished(self, task_id, notification=False):
        await self.service.start()
        for _ in range(200):
            value = self.service.get_task(task_id)
            if value['status'] not in ('pending', 'running') and (not notification or value.get('notification', {}).get('status') not in (None, 'sending')):
                return value
            await asyncio.sleep(.005)
        self.fail('Synthetic workflow did not finish')

    async def test_completion_is_off_by_default_and_setting_requires_a_boolean(self):
        self.assertFalse(self.service.public_config()['notifyPhoneOnCompletion'])
        for invalid in ('true', 1, None):
            with self.assertRaises(WorkflowError):
                self.service.set_config(dict(notifyPhoneOnCompletion=invalid))
        task = await self.finished(self.tool_task()['id'], notification=True)
        self.assertEqual(task['status'], 'succeeded')
        self.assertEqual(task['notification']['reason'], 'completion_alerts_disabled')
        self.notifier.assert_not_awaited()

    async def test_completed_task_notifies_once_without_shipping_private_task_content(self):
        self.service.set_config(dict(notifyPhoneOnCompletion=True, deepseekApiKey='synthetic-secret'))
        modes = []
        async def posted(payload):
            modes.append(TRANSPORT.get())
            return dict(status='posted', delivered=True, incidentalSecret='not stored', content='not stored')
        self.notifier.side_effect = posted
        submitted = self.tool_task(title='合成任务 synthetic-secret', transport='lan')
        task = await self.finished(submitted['id'], notification=True)
        await self.service._notify_completion(task)
        self.notifier.assert_awaited_once()
        payload = self.notifier.await_args.args[0]
        self.assertEqual(payload['eventId'], 'workflow:mac:' + task['id'] + ':completed')
        self.assertEqual(set(payload), {'eventId', 'title', 'message', 'nextStep', 'taskId'})
        self.assertEqual(modes, ['lan'])
        self.assertNotIn('private-result', json.dumps(payload))
        self.assertNotIn('synthetic-secret', json.dumps(payload))
        self.assertNotIn('incidentalSecret', task['notification'])
        self.assertEqual(task['notification']['status'], 'posted')
        self.assertTrue(task['notification']['delivered'])
        await self.service.stop()
        self.service.close()
        self.service = WorkflowService(self.desktop, self.root / 'data')
        self.service.bind_phone_notifier(self.notifier)
        await self.service._notify_completion(self.service.get_task(task['id']))
        self.notifier.assert_awaited_once()
        self.assertTrue(self.service.public_config()['notifyPhoneOnCompletion'])

    async def test_failed_and_cancelled_tasks_do_not_emit_completion_alerts(self):
        self.service.set_config(dict(notifyPhoneOnCompletion=True))
        failed = self.service.submit(dict(type='tool', toolName='not_available'))
        failed = await self.finished(failed['id'])
        self.assertEqual(failed['status'], 'failed')
        cancelled = self.tool_task()
        self.service.cancel(cancelled['id'])
        await self.service._notify_completion(self.service.get_task(cancelled['id']))
        self.desktop.wait_tool = asyncio.Event()
        running = self.tool_task()
        for _ in range(100):
            if self.desktop.calls:
                break
            await asyncio.sleep(.005)
        self.assertTrue(self.desktop.calls)
        self.service.cancel(running['id'])
        await asyncio.sleep(.01)
        self.assertEqual(self.service.get_task(running['id'])['status'], 'cancelled')
        await self.service._notify_completion(self.service.get_task(running['id']))
        self.notifier.assert_not_awaited()

    async def test_offline_suppression_and_unknown_outcomes_preserve_task_success(self):
        self.service.set_config(dict(notifyPhoneOnCompletion=True))
        for response in (dict(status='unavailable', reason='phone_unavailable', delivered=False),
                         dict(status='suppressed', reason='dnd', delivered=False),
                         dict(ok=True)):
            self.notifier.return_value = response
            task = await self.finished(self.tool_task()['id'], notification=True)
            self.assertEqual(task['status'], 'succeeded')
            self.assertFalse(task['notification']['delivered'])
            self.assertEqual(task['notification']['status'], response.get('status', 'unknown'))
        self.notifier.side_effect = httpx.ReadTimeout('synthetic unknown response')
        task = await self.finished(self.tool_task()['id'], notification=True)
        self.assertEqual(task['status'], 'succeeded')
        self.assertEqual(task['notification']['status'], 'unknown')
        calls = self.notifier.await_count
        await self.service._notify_completion(task)
        self.assertEqual(self.notifier.await_count, calls)

    async def test_direct_chat_completion_and_failure_follow_same_alert_policy(self):
        self.service.set_config(dict(notifyPhoneOnCompletion=True))
        with patch.object(self.service, '_chat_run', AsyncMock(return_value={'content': 'private-answer'})):
            result = await self.service.chat(dict(message='private-message'))
        task = self.service.get_task(result['taskId'])
        self.assertEqual(task['status'], 'succeeded')
        self.assertEqual(task['notification']['status'], 'posted')
        self.assertNotIn('private-message', json.dumps(self.notifier.await_args.args[0]))
        self.assertNotIn('private-answer', json.dumps(self.notifier.await_args.args[0]))
        with patch.object(self.service, '_chat_run', AsyncMock(side_effect=WorkflowError('synthetic model failure'))):
            with self.assertRaises(WorkflowError):
                await self.service.chat(dict(message='second-message'))
        self.notifier.assert_awaited_once()

    async def test_cancelled_notification_does_not_change_completed_chat_to_failed(self):
        self.service.set_config(dict(notifyPhoneOnCompletion=True))
        started = asyncio.Event()
        async def delayed(payload):
            started.set()
            await asyncio.Future()
        self.notifier.side_effect = delayed
        with patch.object(self.service, '_chat_run', AsyncMock(return_value={'content': 'answer'})):
            execution = asyncio.create_task(self.service.chat(dict(message='synthetic')))
            await asyncio.wait_for(started.wait(), timeout=1)
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await execution
        task = self.service.list_tasks()['tasks'][0]
        self.assertEqual(task['status'], 'succeeded')
        self.assertEqual(task['notification']['status'], 'unknown')
        self.assertEqual(task['notification']['reason'], 'delivery_interrupted')

    async def test_restart_marks_unconfirmed_notification_unknown_without_replaying(self):
        task = self.tool_task()
        stored = self.service._get(task['id'])
        stored.update(status='succeeded', notification={'status': 'sending', 'delivered': False, 'eventId': 'synthetic-interrupted'})
        self.service._save(stored)
        self.service.close()
        self.service = WorkflowService(self.desktop, self.root / 'data')
        self.service.bind_phone_notifier(self.notifier)
        restarted = self.service.get_task(task['id'])
        self.assertEqual(restarted['status'], 'succeeded')
        self.assertEqual(restarted['notification']['status'], 'unknown')
        await self.service._notify_completion(restarted)
        self.notifier.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
