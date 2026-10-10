"""Embedded TTS motion uses local assets and leaves media/API traffic unchanged."""
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from web_assets import decorate_tts_html
import test_server as server_fixture


class TtsMotionTests(unittest.TestCase):
    def test_preserves_provider_markup_and_inserts_once_before_head_close(self):
        source = '<!doctype html><html><HEAD><title>合成测试</title></HEAD><body><button>播放</button></body></html>'.encode()
        value = decorate_tts_html(source)
        self.assertIn(b'/api/knowledge/assets/motion.js', value)
        self.assertIn(b'/api/knowledge/assets/motion.css', value)
        self.assertEqual(value.split(b'id="devhelper-motion-assets"')[0], source.split(b'</HEAD>')[0] + b'<link ')
        self.assertTrue(value.endswith(source[source.index(b'</HEAD>'):]))
        self.assertEqual(decorate_tts_html(value), value)
        self.assertEqual(decorate_tts_html(b'<button>fragment</button>'), b'<button>fragment</button>')


class TtsMotionProxyTests(unittest.TestCase):
    def setUp(self):
        server_fixture.ServerTests.setUp(self)

    def tearDown(self):
        server_fixture.ServerTests.tearDown(self)

    def response(self, status=200, kind='text/html', raw=b'<head></head><script>fetch("/api/status");</script><audio src="/audio/owned.wav"></audio>'):
        return httpx.Response(status, headers={'content-type': kind, 'etag': 'owned-provider-etag', 'content-range': 'bytes 0-7/8'},
                              content=raw, request=httpx.Request('GET', 'http://owned.invalid/'))

    def test_proxy_adds_self_hosted_motion_after_api_rewrite_and_invalidates_provider_etag(self):
        with patch.object(self.desktop.http, 'send', AsyncMock(return_value=self.response())):
            response = self.client.get('/tts/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('/tts/api/status', response.text)
        self.assertIn('/tts/audio/owned.wav', response.text)
        self.assertIn('/api/knowledge/assets/motion.js', response.text)
        self.assertNotIn('/tts/api/knowledge/assets/motion.js', response.text)
        self.assertNotIn('etag', response.headers)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_error_html_does_not_gain_scripts(self):
        with patch.object(self.desktop.http, 'send', AsyncMock(return_value=self.response(status=503))):
            response = self.client.get('/tts/')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('devhelper-motion-assets', response.text)

    def test_audio_stream_bytes_and_range_status_stay_intact(self):
        raw = b'RIFFown!'
        with patch.object(self.desktop.http, 'send', AsyncMock(return_value=self.response(status=206, kind='audio/wav', raw=raw))):
            response = self.client.get('/tts/audio/owned.wav', headers={'Range': 'bytes=0-7'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, raw)
        self.assertEqual(response.headers['content-range'], 'bytes 0-7/8')
