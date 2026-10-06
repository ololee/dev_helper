"""Phone task alerts routed through the same LAN/relay connection as device tools.

This client never queues or retries a notification: losing a response does not
prove that the phone did not already display it. The phone owns permissions,
sound/vibration preferences, system do-not-disturb checks and event deduplication.
"""
from __future__ import annotations

import httpx
import time


BASE = '/api/workflows/notifications/'
SETTINGS = ('enabled', 'sound', 'vibrate', 'requestNextStep')
LIMITS = dict(eventId=200, title=120, message=500, nextStep=300, taskId=200)
MAX_EXPIRATION = 253402300799999
DEFAULT_TITLE = '任务已完成'
DEFAULT_MESSAGE = '任务已经完成，请查看结果。'


class NotificationError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _object(value, keys):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise NotificationError('提醒参数包含未知字段。')
    return value


class PhoneNotifications:
    """The router signature is (device, HTTP method, path, JSON body or None)."""

    def __init__(self, router):
        self.router = router

    async def _request(self, method, path, body=None):
        event_id = body.get('eventId') if isinstance(body, dict) else None
        write_notice = path == 'notify'
        try:
            value = await self.router('android', method, BASE + path, body)
        except httpx.ConnectError:
            value = dict(status='unavailable', reason='phone_unavailable', delivered=False)
        except httpx.HTTPStatusError as failed:
            status = failed.response.status_code
            value = dict(status='unavailable' if status in (404, 501, 503) else 'failed',
                         reason='phone_unavailable' if status == 503 else 'phone_request_rejected',
                         delivered=False, httpStatus=status)
        except Exception:
            value = dict(status='unknown' if write_notice else 'unavailable',
                         reason='phone_delivery_unconfirmed' if write_notice else 'phone_unavailable', delivered=False)
        if not isinstance(value, dict):
            return dict(status='unknown' if write_notice else 'unavailable', reason='invalid_phone_response', delivered=False,
                        **({'eventId': event_id} if event_id else {}))
        if value.get('isError') or value.get('error'):
            return dict(status='failed', reason='phone_request_rejected', delivered=False,
                        **({'eventId': event_id} if event_id else {}))
        if isinstance(value.get('structuredContent'), dict):
            value = value['structuredContent']
        if write_notice:
            if not isinstance(value.get('status'), str):
                return dict(status='unknown', reason='invalid_phone_response', delivered=False, eventId=event_id)
            # Keep actual phone delivery facts. A transport HTTP 200 alone is not
            # evidence that an alert was posted or that it made a sound.
            value = dict(value, delivered=value.get('delivered') is True)
            value.setdefault('eventId', event_id)
        return value

    async def config(self, settings=None):
        if settings is None:
            return await self._request('GET', 'config')
        _object(settings, SETTINGS)
        if any(not isinstance(value, bool) for value in settings.values()):
            raise NotificationError('提醒设置必须为开启或关闭。')
        return await self._request('POST', 'config', dict(settings))

    async def status(self):
        return await self._request('GET', 'status')

    async def notify(self, fields):
        _object(fields, (*LIMITS, 'expiresAt'))
        payload = {'title': DEFAULT_TITLE, 'message': DEFAULT_MESSAGE, 'nextStep': '下一步需要我做什么？', 'taskId': '',
                   'expiresAt': int(time.time() * 1000) + 20000, **fields}
        if 'eventId' not in payload:
            raise NotificationError('需要 eventId，以避免同一任务重复提醒。')
        for name, limit in LIMITS.items():
            value = payload[name]
            if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
                raise NotificationError(name + ' 超出允许长度或不是有效文字。')
            if name == 'eventId' and not value.strip():
                raise NotificationError('eventId 不能为空。')
        expiry = payload['expiresAt']
        if isinstance(expiry, bool) or not isinstance(expiry, int) or not 1 <= expiry <= MAX_EXPIRATION:
            raise NotificationError('expiresAt 必须为有效的毫秒时间戳。')
        return await self._request('POST', 'notify', payload)

    async def call_tool(self, name, arguments=None):
        fields = {} if arguments is None else arguments
        if name == 'devhelper_notification_config':
            _object(fields, ('settings',))
            if 'settings' in fields:
                _object(fields['settings'], SETTINGS)
            return await self.config(fields.get('settings'))
        if name == 'devhelper_notification_status':
            _object(fields, ())
            return await self.status()
        if name == 'devhelper_notification_notify':
            return await self.notify(fields)
        raise NotificationError('未知的手机提醒工具。', 404)

    @staticmethod
    def tool_specs():
        def spec(name, description, properties=None, required=(), read_only=False):
            return dict(name=name, description=description,
                        inputSchema=dict(type='object', properties=properties or {}, required=list(required), additionalProperties=False),
                        annotations=dict(readOnlyHint=read_only, destructiveHint=False, idempotentHint=True, openWorldHint=True))
        settings = dict(type='object', properties={name: {'type': 'boolean'} for name in SETTINGS}, additionalProperties=False)
        strings = {name: {'type': 'string', 'maxLength': limit} for name, limit in LIMITS.items()}
        strings['eventId']['minLength'] = 1
        strings['expiresAt'] = {'type': 'integer', 'minimum': 1, 'maximum': MAX_EXPIRATION}
        return [spec('devhelper_notification_config',
                     'Read or update connected-phone task-alert preferences. Optional settings include enabled, sound, vibrate and requestNextStep. System do-not-disturb is always respected; this cannot grant OS notification permission.',
                     {'settings': settings}),
                spec('devhelper_notification_status',
                     'Read connected-phone alert availability, notification permission and do-not-disturb status. Does not display an alert.', read_only=True),
                spec('devhelper_notification_notify',
                     'After a task is verified complete, request one connected-phone alert. Reuse the same eventId for the same completion; never claim delivery when status is disabled, suppressed, unavailable or unknown. Title/message are optional; nextStep can ask the user what to do next. Phone preferences and system do-not-disturb are enforced. No retry or offline queue is automatic.',
                     strings, ('eventId',))]
