import json
import sys
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Desktop, transform_paths
from starlette.testclient import TestClient


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.desktop = Desktop(8876, self.path / 'data', self.path / 'shared')
        self.discovery_start = patch.object(self.desktop.discovery, 'start')
        self.discovery_stop = patch.object(self.desktop.discovery, 'stop')
        self.discovery_start.start()
        self.discovery_stop.start()
        self.client = TestClient(self.desktop.app())
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.discovery_start.stop()
        self.discovery_stop.stop()
        self.temp.cleanup()

    def rpc(self, method, params=None):
        response = self.client.post('/mcp', json=dict(jsonrpc='2.0', id=1, method=method, params=params or {}),
                                    headers={'Accept': 'application/json, text/event-stream'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_http_mcp_is_stateless_and_inputs_are_validated(self):
        init = self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'owned-test', 'version': '1'}})
        self.assertEqual(init['result']['serverInfo']['name'], 'devhelper-desktop')
        tools = self.rpc('tools/list')['result']['tools']
        self.assertIn('devhelper_transfer_clipboard', [tool['name'] for tool in tools])
        bad = self.rpc('tools/call', {'name': 'devhelper_write_clipboard', 'arguments': {'target': 'unknown', 'text': 'owned-test'}})
        self.assertTrue(bad['result']['isError'])
        self.assertFalse(self.client.get('/api/config').json()['autoClipboardSync'])
        self.assertEqual(self.client.post('/api/config', json={'androidUrl': 'http://localhost:8765/secret'}).status_code, 400)
        self.assertEqual(self.client.post('/api/tools/call', json={'device': 'mac', 'name': 'devhelper_read_clipboard', 'arguments': {'device': 'unknown'}}).status_code, 400)

    def test_memory_proxy_paths_remain_portable_and_conflicts_survive_http(self):
        api = '/device-api/mac/api/knowledge'
        content = '# Owned test\n![image](/device-api/mac/api/knowledge/attachments/test/content)'
        response = self.client.post(api + '/documents', json={'kind': 'memory', 'title': 'Owned test', 'content': content})
        self.assertEqual(response.status_code, 200, response.text)
        doc = response.json()
        if 'document' in doc:
            doc = doc['document']
        saved = self.desktop.knowledge.read_document(doc['id'])
        self.assertNotIn('/device-api/', saved['content'])
        self.assertIn('/device-api/mac/', self.client.get(api + '/documents/' + doc['id']).json()['content'])
        conflict = self.client.post(api + '/documents', json={'id': doc['id'], 'content': 'old overwrite', 'expectedRevision': 0})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.client.post(api + '/search', content=b'invalid', headers={'content-type': 'application/json'}).status_code, 400)
        self.assertEqual(self.client.get(api + '/assets/markdown-it.min.js').status_code, 200)

    def test_streamed_upload_and_range_download(self):
        import base64
        raw = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
        api = '/device-api/mac/api/knowledge'
        upload = self.client.post(api + '/attachments/upload?name=owned.png', content=raw, headers={'content-type': 'image/png'})
        self.assertEqual(upload.status_code, 200, upload.text)
        value = upload.json()
        metadata = value.get('attachment', value)
        path = metadata['contentPath']
        self.assertEqual(self.client.get(path).content, raw)
        partial = self.client.get(path, headers={'range': 'bytes=0-7'})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, raw[:8])
        self.assertEqual(self.client.head(path).content, b'')

    def test_notes_workflow_http_mcp_and_scheduled_queue(self):
        self.assertEqual(self.client.get('/notes').status_code, 200)
        self.assertEqual(self.client.get('/device-ui/mac/notes').status_code, 200)
        self.assertEqual(self.client.get('/device-ui/android/notes').status_code, 200)
        config = self.client.post('/api/workflows/config', json={'deepseekApiKey': 'owned-synthetic-test-key'}).json()
        self.assertTrue(config['hasDeepseekApiKey'])
        self.assertNotIn('owned-synthetic-test-key', json.dumps(config))
        tools = self.rpc('tools/list')['result']['tools']
        self.assertIn('devhelper_workflow_submit', [item['name'] for item in tools])
        task = self.rpc('tools/call', {'name': 'devhelper_workflow_submit', 'arguments': {'type': 'tool', 'device': 'mac', 'toolName': 'knowledge_list_documents', 'arguments': {}}})['result']['structuredContent']
        for _ in range(50):
            result = self.client.get('/device-api/mac/api/workflows/tasks/' + task['id']).json()
            if result['status'] not in ('pending', 'running'):
                break
            time.sleep(.02)
        self.assertEqual(result['status'], 'succeeded', result)
        schedule = self.client.post('/device-api/mac/api/knowledge/schedules', json={
            'title': 'Owned scheduled workflow', 'toolName': 'devhelper_workflow_submit',
            'arguments': {'type': 'tool', 'device': 'mac', 'toolName': 'knowledge_list_documents', 'arguments': {}},
            'runAt': '2020-01-01T00:00:00Z', 'enabled': True}).json()
        for _ in range(70):
            saved = self.desktop.knowledge.read_schedule(schedule['id'])
            if saved['lastStatus'] == 'completed':
                break
            time.sleep(.02)
        self.assertEqual(saved['lastStatus'], 'completed', saved)
        self.assertEqual(self.client.get('/api/workflows/tasks').status_code, 200)


if __name__ == '__main__':
    unittest.main()
