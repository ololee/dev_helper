"""HTTP and scoped MCP tests mutate generated temporary projects only."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Desktop
from starlette.testclient import TestClient


class FileOperationsHttpTests(unittest.TestCase):
    base_api = '/api/resource-library/file-ops'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'generated-game'
        self.source.mkdir()
        (self.source / 'target').mkdir()
        (self.source / 'moved').mkdir()
        self.original = b'generated asset bytes; never a user project'
        (self.source / 'one.txt').write_bytes(self.original)
        self.desktop = Desktop(8876, self.root / 'data', self.root / 'shared')
        self.discovery_start = patch.object(self.desktop.discovery, 'start')
        self.discovery_stop = patch.object(self.desktop.discovery, 'stop')
        self.discovery_start.start(); self.discovery_stop.start()
        self.client = TestClient(self.desktop.app(), base_url='http://127.0.0.1:8876')
        self.client.__enter__()
        self.cloud = patch.object(self.desktop.resources.drive, '_json', side_effect=AssertionError('Cloud calls are forbidden in local HTTP tests'))
        self.cloud_mock = self.cloud.start()
        response = self.client.post('/api/resource-library/projects', json={'root': str(self.source), 'name': 'Generated HTTP file operations'})
        self.assertEqual(response.status_code, 200, response.text)
        self.project = response.json()
        scan = self.client.post('/api/resource-library/scan', json={'projectId': self.project['id']})
        self.assertEqual(scan.status_code, 200, scan.text)
        deadline = time.monotonic() + 10
        status = scan.json()
        while status['state'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(.01)
            status = self.client.get('/api/resource-library/jobs/' + status['jobId']).json()
        self.assertEqual(status['state'], 'completed', status)

    def tearDown(self):
        try:
            self.cloud_mock.assert_not_called()
            self.client.__exit__(None, None, None)
        finally:
            self.cloud.stop()
            self.discovery_start.stop(); self.discovery_stop.stop()
            self.temp.cleanup()

    def item(self, path):
        return {'projectId': self.project['id'], 'path': path}

    def plan(self, operation, path, destination=None, name=None):
        fields = {'scope': 'local', 'operation': operation, 'items': [self.item(path)]}
        if destination is not None:
            fields['destination'] = self.item(destination)
        if name is not None:
            fields['name'] = name
        response = self.client.post(self.base_api + '/plan', json=fields)
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()
        self.assertIn('planDigest', plan)
        self.assertEqual(plan['scope'], 'local')
        self.assertNotIn('_manifests', response.text)
        self.assertNotIn(str(self.source), response.text)
        return plan

    def wait_job(self, identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = self.client.get(self.base_api + '/jobs/' + identifier)
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            if job['state'] not in ('queued', 'running'):
                return job
            time.sleep(.01)
        self.fail('Generated HTTP operation did not finish')

    def execute(self, plan):
        response = self.client.post(self.base_api + '/execute', json={'planId': plan['planId'], 'planDigest': plan['planDigest']})
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait_job(response.json()['jobId'])

    def rpc(self, scope, method, params=None):
        response = self.client.post('/mcp/' + scope, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}},
                                    headers={'Accept': 'application/json, text/event-stream'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['result']

    def test_http_copy_rename_move_trash_restore_preserves_bytes_and_live_index(self):
        plan = self.plan('copy', 'one.txt', destination='target')
        self.assertFalse((self.source / 'target/one.txt').exists())
        job = self.execute(plan)
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual((self.source / 'target/one.txt').read_bytes(), self.original)
        self.assertEqual((self.source / 'one.txt').read_bytes(), self.original)
        job = self.execute(self.plan('rename', 'target/one.txt', name='renamed.txt'))
        self.assertEqual(job['state'], 'completed', job)
        self.assertFalse((self.source / 'target/one.txt').exists())
        self.assertEqual((self.source / 'target/renamed.txt').read_bytes(), self.original)
        job = self.execute(self.plan('move', 'target/renamed.txt', destination='moved'))
        self.assertEqual(job['state'], 'completed', job)
        self.assertFalse((self.source / 'target/renamed.txt').exists())
        self.assertEqual((self.source / 'moved/renamed.txt').read_bytes(), self.original)
        assets = self.client.get('/api/resource-library/assets', params={'projectId': self.project['id']}).json()['items']
        self.assertEqual({item['path'] for item in assets}, {'one.txt', 'moved/renamed.txt'})
        job = self.execute(self.plan('trash', 'moved/renamed.txt'))
        self.assertEqual(job['state'], 'completed', job)
        self.assertFalse((self.source / 'moved/renamed.txt').exists())
        trash = self.client.get(self.base_api + '/trash')
        self.assertEqual(trash.status_code, 200, trash.text)
        self.assertEqual(len(trash.json()['items']), 1)
        identifier = trash.json()['items'][0]['trashId']
        restore = self.client.post(self.base_api + '/restore', json={'trashId': identifier})
        self.assertEqual(restore.status_code, 202, restore.text)
        job = self.wait_job(restore.json()['jobId'])
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual((self.source / 'moved/renamed.txt').read_bytes(), self.original)
        self.assertEqual(self.client.get(self.base_api + '/trash').json()['items'], [])
        self.assertEqual(len(self.client.get(self.base_api + '/jobs').json()['items']), 5)

    def test_invalid_scope_extra_schema_fields_and_escape_paths_are_rejected(self):
        valid = {'scope': 'local', 'operation': 'trash', 'items': [self.item('one.txt')]}
        examples = [
            {**valid, 'scope': 'outside'},
            {**valid, 'unexpected': True},
            {**valid, 'items': [{**self.item('one.txt'), 'fsId': '123'}]},
            {**valid, 'items': [self.item('../escape')]},
            {**valid, 'operation': 'shell'},
            {**valid, 'items': []},
        ]
        for fields in examples:
            with self.subTest(fields=fields):
                response = self.client.post(self.base_api + '/plan', json=fields)
                self.assertGreaterEqual(response.status_code, 400, response.text)
        plan = self.plan('trash', 'one.txt')
        bad_execute = self.client.post(self.base_api + '/execute', json={'planId': plan['planId'], 'planDigest': plan['planDigest'], 'scope': 'local'})
        self.assertEqual(bad_execute.status_code, 400, bad_execute.text)
        bad_restore = self.client.post(self.base_api + '/restore', json={'trashId': 'generated', 'extra': True})
        self.assertEqual(bad_restore.status_code, 400, bad_restore.text)
        self.assertEqual((self.source / 'one.txt').read_bytes(), self.original)
        self.assertEqual(self.client.get(self.base_api + '/jobs').json()['items'], [])

    def test_foreign_origin_and_wrong_digest_do_not_mutate_originals(self):
        plan = self.plan('trash', 'one.txt')
        fields = {'planId': plan['planId'], 'planDigest': plan['planDigest']}
        response = self.client.post(self.base_api + '/execute', json=fields, headers={'Origin': 'https://foreign.invalid'})
        self.assertEqual(response.status_code, 403, response.text)
        response = self.client.post(self.base_api + '/execute', json={**fields, 'planDigest': '0' * 64})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual((self.source / 'one.txt').read_bytes(), self.original)
        self.assertEqual(self.client.get(self.base_api + '/jobs').json()['items'], [])

    def test_mcp_scopes_annotations_plan_execute_and_cross_scope_rejection(self):
        assets = {tool['name']: tool for tool in self.rpc('assets', 'tools/list')['tools']}
        netdisk = {tool['name']: tool for tool in self.rpc('netdisk', 'tools/list')['tools']}
        self.assertIn('asset_file_plan', assets)
        self.assertIn('asset_file_execute', assets)
        self.assertIn('asset_file_restore', assets)
        self.assertIn('netdisk_file_plan', netdisk)
        self.assertNotIn('netdisk_file_execute', assets)
        self.assertNotIn('asset_file_execute', netdisk)
        self.assertTrue(assets['asset_file_plan']['annotations']['readOnlyHint'])
        self.assertFalse(assets['asset_file_execute']['annotations']['readOnlyHint'])
        self.assertTrue(assets['asset_file_execute']['annotations']['destructiveHint'])
        planned = self.rpc('assets', 'tools/call', {'name': 'asset_file_plan', 'arguments': {
            'operation': 'copy', 'items': [self.item('one.txt')], 'destination': self.item('target')}})
        self.assertFalse(planned.get('isError', False), planned)
        plan = planned['structuredContent']
        arguments = {'planId': plan['planId'], 'planDigest': plan['planDigest']}
        wrong_scope = self.rpc('netdisk', 'tools/call', {'name': 'netdisk_file_execute', 'arguments': arguments})
        self.assertTrue(wrong_scope.get('isError'), wrong_scope)
        self.assertFalse((self.source / 'target/one.txt').exists())
        executed = self.rpc('assets', 'tools/call', {'name': 'asset_file_execute', 'arguments': arguments})
        self.assertFalse(executed.get('isError', False), executed)
        job = self.wait_job(executed['structuredContent']['jobId'])
        self.assertEqual(job['state'], 'completed', job)
        read = self.rpc('assets', 'tools/call', {'name': 'asset_file_job', 'arguments': {'jobId': job['jobId']}})
        self.assertEqual(read['structuredContent']['state'], 'completed')
        wrong_job = self.rpc('netdisk', 'tools/call', {'name': 'netdisk_file_job', 'arguments': {'jobId': job['jobId']}})
        self.assertTrue(wrong_job.get('isError'), wrong_job)
        self.assertEqual((self.source / 'target/one.txt').read_bytes(), self.original)

    def test_restore_conflict_is_failed_and_never_overwrites_new_original(self):
        job = self.execute(self.plan('trash', 'one.txt'))
        self.assertEqual(job['state'], 'completed', job)
        identifier = self.client.get(self.base_api + '/trash').json()['items'][0]['trashId']
        replacement = b'new generated file that must not be overwritten'
        (self.source / 'one.txt').write_bytes(replacement)
        response = self.client.post(self.base_api + '/restore', json={'trashId': identifier})
        self.assertEqual(response.status_code, 202, response.text)
        job = self.wait_job(response.json()['jobId'])
        self.assertEqual(job['state'], 'failed', job)
        self.assertEqual((self.source / 'one.txt').read_bytes(), replacement)
        self.assertEqual(len(self.client.get(self.base_api + '/trash').json()['items']), 1)


if __name__ == '__main__':
    unittest.main()
