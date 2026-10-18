"""HTTP/MCP resource regressions with generated files and an offline cloud stub."""
import hashlib
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, AsyncMock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from baidu_drive import BaiduDriveError
from server import Desktop
from starlette.testclient import TestClient
import httpx


class OfflineDrive:
    """Records exact generated binary bytes; never makes a cloud connection."""
    def __init__(self):
        self.uploads = []
        self.directories = []
        self._jobs = {}
        self.gate = threading.Event()
        self.gate.set()
        self.fail_number = None
        self.raise_number = None
        self.fail_manifest = False
        self.rename = False
        self.unconfirmed_number = None

    def _token(self): return 'offline-fixture-token'
    def remote_path(self, path, *, file=False):
        if path.startswith('/apps/Fixture/') or path == '/apps/Fixture': return path.rstrip('/')
        if path.startswith('/') or '..' in path.split('/'): raise BaiduDriveError('Invalid fixture remote path')
        return '/apps/Fixture/' + path
    def mkdir(self, path):
        self.directories.append(path)
        return {'path': path, 'isdir': 1}
    def list_files(self, path='', start=0, limit=100):
        return {'path': path, 'items': [], 'directoryExists': True}
    def start_upload(self, path, remote_path, *, name=None):
        number = len(self.uploads) + 1
        if number == self.raise_number: raise BaiduDriveError('Offline fixture upload was rejected', 409)
        content = Path(path).read_bytes()
        self.uploads.append(dict(path=remote_path, content=content))
        identifier = str(uuid.uuid4())
        failed = number == self.fail_number or self.fail_manifest and remote_path.endswith('/manifest.json')
        self._jobs[identifier] = dict(jobId=identifier, operation='upload', state='queued', name=name or Path(path).name,
            _failed=failed, result={'remotePath': remote_path + ('.renamed' if self.rename else ''),
                'fsId': str(number), 'bytes': len(content), 'md5': hashlib.md5(content).hexdigest(),
                'sha256': hashlib.sha256(content).hexdigest()})
        if number == self.unconfirmed_number:
            self._jobs[identifier]['result']['pathConfirmed'] = False
        return self.job(identifier)
    def job(self, identifier):
        item = dict(self._jobs[identifier])
        if self.gate.is_set():
            item['state'] = 'failed' if item['_failed'] else 'completed'
            if item['_failed']: item['error'] = 'Offline fixture transfer failed'
        item.pop('_failed', None)
        return item
    def jobs(self): return {'items': [self.job(identifier) for identifier in self._jobs]}
    def cancel(self, identifier):
        self._jobs[identifier]['state'] = 'cancelled'
        self.gate.set()
        return self.job(identifier)
    def close(self): self.gate.set()


class ResourceWorkspaceHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'generated-game'
        self.source.mkdir()
        self.desktop = Desktop(8876, self.root / 'data', self.root / 'shared')
        self.discovery_start = patch.object(self.desktop.discovery, 'start')
        self.discovery_stop = patch.object(self.desktop.discovery, 'stop')
        self.discovery_start.start(); self.discovery_stop.start()
        self.client = TestClient(self.desktop.app(),base_url='http://127.0.0.1:8876')
        self.client.__enter__()
        self.drive_stub = None

    def tearDown(self):
        if self.drive_stub: self.drive_stub.gate.set()
        self.client.__exit__(None, None, None)
        self.discovery_start.stop(); self.discovery_stop.stop()
        self.temp.cleanup()

    def test_phone_credential_transfer_uses_lan_and_never_sends_client_secret(self):
        from httpx import Response
        config = {'clientId':'fixture-client','clientSecret':'fixture-secret','appName':'Fixture','accessToken':'fixture-access','refreshToken':'fixture-refresh','redirectUri':'oob'}
        workspace = self.desktop.resources
        with workspace.drive.lock:
            workspace.drive.config.update(config)
        async def request(method,path,**fields):
            self.assertEqual((method,path),('POST','/api/knowledge/netdisk/config'))
            self.assertEqual(fields['transport'],'lan')
            self.assertNotIn('clientSecret',fields['json'])
            self.assertEqual(fields['json']['accessToken'],'fixture-access')
            return Response(200,json={'configured':True,'hasAccessToken':True,'remoteRoot':'/apps/Fixture'})
        with patch.object(self.desktop,'device_request',side_effect=request):
            response=self.client.post('/api/netdisk/connect-android',json={'appId':'fixture-app','thirdId':'fixture-user','from':'fixture-source'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['configured'])
        for secret in ('fixture-secret','fixture-access','fixture-refresh'):
            self.assertNotIn(secret,response.text)

    def test_phone_credential_transfer_derives_identity_from_authorized_account(self):
        from httpx import Response
        workspace = self.desktop.resources
        with workspace.drive.lock:
            workspace.drive.config.update({'clientId':'fixture-client','clientSecret':'fixture-secret','appName':'Fixture','accessToken':'fixture-access'})
        async def request(method,path,**fields):
            self.assertEqual(fields['transport'],'lan')
            self.assertEqual(fields['json']['thirdId'],'1234567890123')
            self.assertEqual(fields['json']['from'],'devhelper')
            self.assertNotIn('clientSecret',fields['json'])
            return Response(200,json={'configured':True,'hasAccessToken':True,'remoteRoot':'/apps/Fixture'})
        with patch.object(workspace.drive,'authorized_user_id',return_value='1234567890123') as lookup,patch.object(self.desktop,'device_request',side_effect=request):
            response=self.client.post('/api/netdisk/connect-android',json={'appId':'fixture-app'})
        self.assertEqual(response.status_code,200,response.text)
        lookup.assert_called_once()
        for secret in ('fixture-secret','fixture-access','1234567890123'):
            self.assertNotIn(secret,response.text)

    def fixture_project(self):
        image = io.BytesIO()
        Image.new('RGBA', (24, 16), (230, 40, 70, 180)).save(image, 'PNG')
        self.image_bytes = image.getvalue()
        (self.source / 'texture.png').write_bytes(self.image_bytes)
        (self.source / 'texture-copy.png').write_bytes(self.image_bytes)
        (self.source / 'opaque.bin').write_bytes(bytes(range(256)) * 257)
        (self.source / 'texture.png.meta').write_text('fileFormatVersion: 2\nguid: ' + 'a' * 32 + '\n')
        (self.source / 'unit.prefab').write_text('%YAML 1.1\nMonoBehaviour:\n  texture: {fileID: 1, guid: ' + 'a' * 32 + ', type: 3}\n')
        response = self.client.post('/api/resource-library/projects', json={'root': str(self.source), 'name': 'Generated fixture project'})
        self.assertEqual(response.status_code, 200, response.text)
        self.project = response.json()
        response = self.client.post('/api/resource-library/scan', json={'projectId': self.project['id']})
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        self.assertIn(job['state'], ('queued', 'running', 'completed'))
        deadline = time.monotonic() + 10
        while job['state'] in ('queued', 'running'):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
            response = self.client.get('/api/resource-library/jobs/' + job['jobId'])
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual(job['progress']['indexed'], 5)
        response = self.client.get('/api/resource-library/assets', params={'projectId': self.project['id']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assets = {item['path']: item for item in response.json()['items']}
        return self.assets

    def rpc(self, scope, method, params=None, headers=None):
        response = self.client.post('/mcp/' + scope, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}},
            headers={'Accept': 'application/json, text/event-stream', **(headers or {})})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['result']

    def mock_drive(self):
        self.desktop.resources.drive.close()
        self.drive_stub = OfflineDrive()
        self.desktop.resources.drive = self.drive_stub
        return self.drive_stub

    def wait_backup(self, identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = self.client.get('/api/resource-library/backups/' + identifier)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            if result['state'] not in ('queued', 'running'): return result
            time.sleep(.02)
        self.fail('Offline fixture backup did not finish')

    def assert_staging_empty(self):
        deadline=time.monotonic()+2
        while list(self.desktop.resources.staging.iterdir()) and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(list(self.desktop.resources.staging.iterdir()),[])

    def backup_start(self, ids):
        response = self.client.post('/api/resource-library/backup', json={'assetIds': ids, 'remoteDirectory': 'backups'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_http_register_scan_search_get_dependencies_and_similarity(self):
        items = self.fixture_project()
        response = self.client.get('/api/resource-library/projects')
        self.assertEqual(response.json()['items'][0]['id'], self.project['id'])
        self.assertNotIn(str(self.source), response.text)
        response = self.client.get('/api/resource-library/assets', params={'projectId': self.project['id'], 'kind': 'image'})
        self.assertEqual(response.json()['total'], 2)
        image = items['texture.png']
        response = self.client.post('/api/resource-library/get', json={'assetId': image['id']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['image']['width'], 24)
        self.assertEqual(response.json()['sha256'], hashlib.sha256(self.image_bytes).hexdigest())
        self.assertIn('contentUrl', response.json())
        self.assertNotIn('base64', response.json())
        response = self.client.post('/api/resource-library/dependencies', json={'assetId': items['unit.prefab']['id']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['edges'][0]['to'], image['id'])
        response = self.client.post('/api/resource-library/similar', json={'assetId': image['id'], 'mode': 'exact'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['method'], 'sha256-exact')
        self.assertEqual(response.json()['items'][0]['id'], items['texture-copy.png']['id'])

    def test_http_binary_stream_is_exact_nosniff_and_head_is_empty(self):
        items = self.fixture_project()
        for relative in ('texture.png', 'opaque.bin'):
            asset = items[relative]
            metadata = self.client.post('/api/resource-library/get', json={'assetId': asset['id']}).json()
            response = self.client.get(metadata['contentUrl'])
            self.assertEqual(response.status_code, 200, response.text[:100])
            self.assertEqual(response.content, (self.source / relative).read_bytes())
            self.assertEqual(response.headers['content-type'], 'application/octet-stream')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            self.assertTrue(response.headers['content-disposition'].startswith('attachment;'))
            head = self.client.head(metadata['contentUrl'])
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b'')
            self.assertEqual(int(head.headers['content-length']),len(response.content))

    def test_scoped_mcp_catalogs_are_isolated_and_cannot_invoke_root_tools(self):
        for scope, prefix in (('assets', 'asset_'), ('netdisk', 'netdisk_')):
            init = self.rpc(scope, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'generated-fixture', 'version': '1'}})
            self.assertEqual(init['serverInfo']['name'], 'devhelper-' + scope)
            names = [tool['name'] for tool in self.rpc(scope, 'tools/list')['tools']]
            self.assertTrue(names)
            self.assertTrue(all(name.startswith(prefix) for name in names))
            with patch.object(self.desktop, 'dispatch', side_effect=AssertionError('Root dispatch must not run')) as root_dispatch:
                for name in ('knowledge_list_documents', 'devhelper_call_tool', 'netdisk_status' if scope == 'assets' else 'asset_projects'):
                    result = self.rpc(scope, 'tools/call', {'name': name, 'arguments': {}})
                    self.assertTrue(result['isError'], result)
                root_dispatch.assert_not_called()
        result = self.rpc('assets', 'tools/call', {'name': 'asset_projects', 'arguments': {}})
        self.assertEqual(result['structuredContent']['items'], [])
        result = self.rpc('netdisk', 'tools/call', {'name': 'netdisk_status', 'arguments': {}})
        self.assertFalse(result['structuredContent']['authorized'])

    def test_config_secret_is_write_only_and_unconfigured_stream_never_stages(self):
        before = list(self.desktop.resources.staging.iterdir())
        response = self.client.post('/api/netdisk/upload?name=fixture.bin&remotePath=fixture.bin', content=b'\x00\xffraw-data', headers={'content-type': 'application/octet-stream'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(list(self.desktop.resources.staging.iterdir()), before)
        config = dict(clientId='fixture-client', clientSecret='fixture-secret-must-not-return', appName='Fixture', redirectUri='oob')
        response = self.client.post('/api/netdisk/config', json=config)
        self.assertEqual(response.status_code, 200, response.text)
        for result in (response, self.client.get('/api/netdisk/config'), self.client.get('/api/netdisk/status')):
            self.assertNotIn(config['clientSecret'], result.text)
            self.assertTrue(result.json()['hasClientSecret'])
        result = self.rpc('netdisk', 'tools/call', {'name': 'netdisk_status', 'arguments': {}})
        self.assertNotIn(config['clientSecret'], json.dumps(result))

    def test_unconfigured_asset_upload_rejected_before_any_staging_copy(self):
        items = self.fixture_project()
        with patch.object(self.desktop.resources, 'stage_asset', wraps=self.desktop.resources.stage_asset) as stage:
            response = self.client.post('/api/netdisk/upload-asset', json={'assetId': items['texture.png']['id'], 'remotePath': 'texture.png'})
            self.assertEqual(response.status_code, 409, response.text)
            stage.assert_not_called()
        self.assert_staging_empty()

    def test_explicit_foreign_origins_block_mcp_configuration_and_binary_upload(self):
        for scope in ('assets', 'netdisk'):
            response = self.client.post('/mcp/' + scope, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                headers={'Accept': 'application/json, text/event-stream', 'Origin': 'https://foreign.invalid'})
            self.assertEqual(response.status_code, 403)
        with patch.object(self.desktop.resources.drive, '_token', side_effect=AssertionError('Foreign origin reached drive')) as token:
            response = self.client.post('/api/netdisk/upload?name=fixture.bin&remotePath=fixture.bin', content=b'raw', headers={'Origin': 'https://foreign.invalid'})
            self.assertEqual(response.status_code, 403)
            token.assert_not_called()
        response = self.client.post('/api/netdisk/config', json={'appName': 'Wrong'}, headers={'Origin': 'https://foreign.invalid'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.desktop.resources.drive.status()['appName'], '')

    def test_completed_cloud_download_has_real_binary_content_and_partials_are_unavailable(self):
        workspace = self.desktop.resources
        identifier = str(uuid.uuid4())
        name = 'generated-download.bin'
        payload = bytes(range(256)) * 33
        (workspace.downloads / name).write_bytes(payload)
        workspace.drive._jobs[identifier] = dict(jobId=identifier, operation='download', name=name, state='running')
        response = self.client.get('/api/netdisk/jobs/' + identifier + '/content')
        self.assertEqual(response.status_code, 409, response.text)
        workspace.drive._jobs[identifier]['state'] = 'completed'
        response = self.client.get('/api/netdisk/jobs/' + identifier)
        self.assertEqual(response.status_code, 200, response.text)
        content = self.client.get(response.json()['contentUrl'])
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.content, payload)
        self.assertEqual(content.headers['content-type'], 'application/octet-stream')
        (workspace.downloads / name).unlink()
        (workspace.downloads / name).symlink_to(self.source / 'unavailable')
        self.assertGreaterEqual(self.client.get(response.json()['contentUrl']).status_code, 400)

    def test_backup_never_claims_completion_while_transfer_is_queued(self):
        items = self.fixture_project(); drive = self.mock_drive(); drive.gate.clear()
        result = self.backup_start([items['texture.png']['id']])
        try:
            deadline = time.monotonic() + 3
            while not drive.uploads and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(drive.uploads)
            running = self.client.get('/api/resource-library/backups/' + result['jobId']).json()
            self.assertIn(running['state'], ('queued', 'running'))
            self.assertEqual(running['completedCount'], 0)
            self.assertNotIn('manifest', running)
        finally: drive.gate.set()
        result = self.wait_backup(result['jobId'])
        self.assertEqual(result['state'], 'completed', result)

    def classified_plan(self, ids):
        response=self.client.post('/api/resource-library/backup-plan',json={'assetIds':ids,'remoteDirectory':'organized-library','layout':'classified'})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def start_classified(self, ids, plan):
        return self.client.post('/api/resource-library/backup',json={'assetIds':ids,'remoteDirectory':'organized-library','layout':'classified','planDigest':plan['planDigest']})

    def test_archive_preview_is_local_read_only_and_mcp_returns_same_digest(self):
        items=self.fixture_project();drive=self.mock_drive();ids=[items['texture.png']['id'],items['opaque.bin']['id']]
        with patch.object(drive,'_token',side_effect=AssertionError('Preview accessed credentials')),patch.object(self.desktop.resources,'stage_asset',side_effect=AssertionError('Preview staged bytes')):
            plan=self.classified_plan(ids)
            rpc=self.rpc('assets','tools/call',{'name':'asset_backup_plan','arguments':{'assetIds':ids,'remoteDirectory':'organized-library'}})
        self.assertEqual(rpc['structuredContent']['planDigest'],plan['planDigest'])
        self.assertFalse(plan['cloudWrites']);self.assertFalse(plan['inferenceUsed'])
        self.assertEqual(plan['summary']['fileCount'],2)
        self.assertEqual(len(plan['planDigest']),64)
        self.assertEqual(drive.directories,[]);self.assertEqual(drive.uploads,[])
        self.assertNotIn(str(self.source),json.dumps(plan))

    def test_classified_upload_requires_preview_and_rejects_changed_classification_before_token(self):
        items=self.fixture_project();drive=self.mock_drive();ids=[items['texture.png']['id']]
        plan=self.classified_plan(ids)
        self.desktop.resources.library.save_annotation(ids[0],{'source':'manual','categories':['gui'],'label':'Generated interface'})
        with patch.object(drive,'_token',side_effect=AssertionError('Stale plan accessed credentials')):
            missing=self.client.post('/api/resource-library/backup',json={'assetIds':ids,'remoteDirectory':'organized-library','layout':'classified'})
            self.assertEqual(missing.status_code,409,missing.text)
            stale=self.start_classified(ids,plan)
            self.assertEqual(stale.status_code,409,stale.text)
        self.assertEqual(drive.uploads,[]);self.assertEqual(drive.directories,[])

    def test_classified_archive_preserves_binary_and_portable_classification_catalog(self):
        items=self.fixture_project();drive=self.mock_drive();ids=[items['texture.png']['id'],items['opaque.bin']['id']]
        self.desktop.resources.library.save_annotation(ids[0],{'source':'manual','categories':['gui','vfx'],'purpose':'Generated fixture for cloud archive tests','evidence':'Chosen in generated fixture'})
        plan=self.classified_plan(ids)
        response=self.start_classified(ids,plan);self.assertEqual(response.status_code,200,response.text)
        result=self.wait_backup(response.json()['jobId'])
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual(result['layout'],'classified');self.assertEqual(result['planDigest'],plan['planDigest'])
        for index,entry in enumerate(plan['items']):
            upload=drive.uploads[index]
            self.assertEqual(upload['path'],plan['remoteDirectory']+'/'+entry['archiveRelativePath'])
            self.assertEqual(hashlib.sha256(upload['content']).hexdigest(),entry['sha256'])
            self.assertNotIn(result['jobId'],upload['path'])
        self.assertEqual(plan['items'][0]['classificationSource'],'manual')
        self.assertEqual(plan['items'][0]['primaryCategory'],'gui')
        self.assertIn('/gui-',drive.uploads[0]['path'])
        catalog=drive.uploads[-1]
        self.assertEqual(catalog['path'],plan['remoteDirectory']+'/_catalog/'+result['jobId']+'/manifest.json')
        manifest=json.loads(catalog['content'])
        self.assertEqual(manifest['formatVersion'],2)
        self.assertEqual(manifest['planDigest'],plan['planDigest'])
        self.assertEqual(manifest['items'][0]['metadata']['annotations'][0]['categories'],['gui','vfx'])
        self.assertNotIn(str(self.source),catalog['content'].decode())
        self.assertTrue(all(i['remotePath']==i['result']['remotePath'] for i in manifest['items']))
        self.assert_staging_empty()

    def test_queued_classified_archive_stops_before_upload_if_annotation_changed(self):
        items=self.fixture_project();drive=self.mock_drive();ids=[items['texture.png']['id']]
        plan=self.classified_plan(ids);pending=[]
        with patch.object(self.desktop.resources.pool,'submit',side_effect=lambda fn,*args:pending.append((fn,args))):
            response=self.start_classified(ids,plan)
        self.assertEqual(response.status_code,200,response.text)
        self.desktop.resources.library.save_annotation(ids[0],{'source':'manual','categories':['gui']})
        fn,args=pending.pop();fn(*args)
        result=self.wait_backup(response.json()['jobId'])
        self.assertEqual(result['state'],'failed',result)
        self.assertEqual(result['completedCount'],0)
        self.assertEqual(drive.uploads,[]);self.assertEqual(drive.directories,[])
        self.assert_staging_empty()

    def test_existing_archive_directory_is_checked_and_never_renamed(self):
        self.mock_drive();workspace=self.desktop.resources;created=set();path='/apps/Fixture/organized-library'
        duplicate=BaiduDriveError('Existing name',409,provider_code='-8')
        with patch.object(self.drive_stub,'mkdir',side_effect=duplicate),patch.object(self.drive_stub,'list_files',return_value={'path':path,'directoryExists':True}) as listing:
            workspace._ensure_archive_directory(path,created)
        self.assertEqual(created,{path});listing.assert_called_once_with(path,start=0,limit=1)
        with patch.object(self.drive_stub,'mkdir',return_value={'path':path+'(1)','isdir':1}):
            with self.assertRaises(BaiduDriveError):workspace._ensure_archive_directory(path,set())
        with patch.object(self.drive_stub,'mkdir',side_effect=duplicate),patch.object(self.drive_stub,'list_files',side_effect=BaiduDriveError('Not a directory',409)):
            with self.assertRaises(BaiduDriveError):workspace._ensure_archive_directory(path,set())

    def test_classified_archive_does_not_complete_when_rapid_upload_path_is_unknown(self):
        items=self.fixture_project();drive=self.mock_drive();ids=[items['texture.png']['id']]
        plan=self.classified_plan(ids)
        for unconfirmed_number in (1,2):
            with self.subTest(unconfirmed=unconfirmed_number):
                drive.uploads.clear();drive.unconfirmed_number=unconfirmed_number
                response=self.start_classified(ids,plan);self.assertEqual(response.status_code,200,response.text)
                result=self.wait_backup(response.json()['jobId'])
                self.assertEqual(result['state'],'failed',result)
                self.assertNotIn('manifest',result)
                if unconfirmed_number==1:
                    self.assertEqual(result['completedCount'],0)
                    self.assertTrue(result['items'][0]['cloudUploadCompleted'])
                    self.assertFalse(result['items'][0]['remotePathConfirmed'])
                    self.assertEqual(len(drive.uploads),1)
                else:
                    self.assertEqual(result['completedCount'],1)
                    self.assertEqual(result['manifestState'],'failed')
                    self.assertFalse(result['unconfirmedManifestReceipt']['pathConfirmed'])
                self.assert_staging_empty()

    def test_backup_completion_ledger_and_manifest_match_exact_uploaded_bytes(self):
        items = self.fixture_project(); drive = self.mock_drive(); drive.rename = True
        selected = [items[name] for name in ('texture.png', 'opaque.bin')]
        result = self.wait_backup(self.backup_start([item['id'] for item in selected])['jobId'])
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(result['fileCount'], 2)
        self.assertEqual(result['completedCount'], 2)
        self.assertEqual(result['failedCount'],0)
        self.assertEqual(result['pendingCount'],0)
        self.assertEqual(result['incompleteCount'],0)
        self.assertEqual(result['manifestState'],'completed')
        self.assertEqual(len(result['items']), 2)
        self.assertTrue(all(entry['state'] == 'completed' for entry in result['items']))
        for item, entry, upload in zip(selected, result['items'], drive.uploads):
            self.assertEqual(hashlib.sha256(upload['content']).hexdigest(), item['sha256'])
            self.assertEqual(entry['sha256'], item['sha256'])
            self.assertEqual(entry['remotePath'], entry['result']['remotePath'])
        manifest = json.loads(drive.uploads[-1]['content'])
        self.assertEqual(len(manifest['items']), 2)
        self.assertEqual([entry['sha256'] for entry in manifest['items']], [item['sha256'] for item in selected])
        persisted = json.loads(self.desktop.resources.state_path.read_text())
        self.assertEqual(persisted[result['jobId']]['state'], 'completed')
        self.assert_staging_empty()

    def test_backup_upload_failure_keeps_full_per_asset_ledger_and_counts(self):
        items = self.fixture_project(); drive = self.mock_drive(); drive.raise_number = 2
        selected = [items[name] for name in ('texture.png', 'opaque.bin', 'texture-copy.png')]
        result = self.wait_backup(self.backup_start([item['id'] for item in selected])['jobId'])
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['fileCount'], 3)
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual(result['failedCount'],1)
        self.assertEqual(result['pendingCount'],0)
        self.assertEqual(result['skippedCount'],1)
        self.assertEqual(result['incompleteCount'],2)
        self.assertEqual(result['manifestState'],'skipped')
        self.assertEqual(len(result['items']), 3)
        self.assertEqual([entry['assetId'] for entry in result['items']], [item['id'] for item in selected])
        self.assertTrue(all(entry['state'] not in ('queued', 'running') for entry in result['items']))
        completed = sum(entry['state'] == 'completed' for entry in result['items'])
        self.assertEqual(completed, result['completedCount'])
        self.assert_staging_empty()
        self.assertFalse(any(upload['path'].endswith('/manifest.json') for upload in drive.uploads))

    def test_manifest_failure_does_not_mark_backup_complete(self):
        items = self.fixture_project(); drive = self.mock_drive(); drive.fail_manifest = True
        result = self.wait_backup(self.backup_start([items['texture.png']['id']])['jobId'])
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['completedCount'], 1)
        self.assertEqual(result['items'][0]['state'], 'completed')
        self.assertEqual(result['manifestState'],'failed')
        self.assertEqual(result['failedCount'],0)
        self.assertEqual(result['incompleteCount'],0)
        self.assertNotIn('manifest', result)
        self.assert_staging_empty()

    def test_dns_rebinding_and_wrong_ports_are_rejected_even_with_matching_origin(self):
        for authority in ('foreign.invalid:8876','127.0.0.1:8888','127.000.0.1:8876','127.0.0.1:08876'):
            headers={'Host':authority,'Origin':'http://'+authority}
            for path in ('/api/netdisk/status','/api/resource-library/projects'):
                self.assertEqual(self.client.get(path,headers=headers).status_code,403)
            response=self.client.post('/mcp/assets',json={'jsonrpc':'2.0','id':1,'method':'tools/list'},
                headers={**headers,'Accept':'application/json, text/event-stream'})
            self.assertEqual(response.status_code,403)
        self.assertEqual(self.client.get('/api/netdisk/status',headers={'Origin':'http://127.0.0.1:8876'}).status_code,200)
        self.assertEqual(self.client.get('/api/netdisk/status',headers={'Host':'localhost:8876','Origin':'http://localhost:8876'}).status_code,200)
        with patch('resource_workspace.local_ips',return_value=['192.168.50.10']):
            self.assertEqual(self.client.get('/api/netdisk/status',headers={'Host':'192.168.50.10:8876'}).status_code,200)
            self.assertEqual(self.client.get('/api/netdisk/status',headers={'Host':'192.168.50.11:8876'}).status_code,403)

    def test_source_swap_to_symlink_is_rejected_before_success_headers(self):
        items=self.fixture_project();item=items['texture.png']
        private=self.root/'outside.txt';private.write_bytes(b'outside-fixture-must-not-stream')
        actual=self.desktop.resources.library.open_file
        @contextlib.contextmanager
        def swapped(identifier):
            file=self.source/item['path'];file.unlink();file.symlink_to(private)
            with actual(identifier) as source:yield source
        with patch.object(self.desktop.resources.library,'open_file',swapped):
            response=self.client.get('/api/resource-library/assets/'+item['id']+'/content')
        self.assertEqual(response.status_code,409,response.text)
        self.assertNotIn(private.read_text(),response.text)

    def test_backup_does_not_upload_new_index_content_under_old_snapshot_hash(self):
        items=self.fixture_project();drive=self.mock_drive();item=items['texture.png']
        stage=self.desktop.resources.stage_asset
        def changed(identifier,expected_sha256=None):
            self.assertEqual(expected_sha256,item['sha256'])
            return stage(identifier,expected_sha256='0'*64)
        with patch.object(self.desktop.resources,'stage_asset',side_effect=changed):
            result=self.wait_backup(self.backup_start([item['id']])['jobId'])
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['completedCount'],0)
        self.assertEqual(result['failedCount'],1)
        self.assertFalse(drive.uploads)
        self.assert_staging_empty()

    def test_single_asset_and_raw_binary_upload_cleanup_after_completion(self):
        items=self.fixture_project();drive=self.mock_drive()
        response=self.client.post('/api/netdisk/upload-asset',json={'assetId':items['texture.png']['id'],'remotePath':'one.png'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(drive.uploads[0]['content'],self.image_bytes)
        raw=b'\x00\xffopaque-binary-no-json'*4096
        response=self.client.post('/api/netdisk/upload?name=raw.bin&remotePath=raw.bin',content=raw,headers={'Content-Type':'application/octet-stream'})
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(drive.uploads[1]['content'],raw)
        self.assert_staging_empty()

    def test_external_ai_config_and_models_keep_keys_private(self):
        ai=self.desktop.resources.ai
        ai.client.close()
        def provider(request):
            self.assertTrue(request.url.path.endswith('/models'))
            self.assertEqual(request.headers['Authorization'],'Bearer fixture-private-ai-key')
            return httpx.Response(200,json={'data':[{'id':'fixture-embedding','owned_by':'fixture','providerSecret':'never-return'}]})
        ai.client=httpx.Client(transport=httpx.MockTransport(provider))
        response=self.client.post('/api/resource-library/ai/config',json={'embedding':{'baseUrl':'https://fixture.invalid/v1','apiKey':'fixture-private-ai-key'}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn('fixture-private-ai-key',response.text)
        self.assertTrue(response.json()['embedding']['hasApiKey'])
        response=self.client.get('/api/resource-library/ai/models?kind=embedding')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['items'][0]['id'],'fixture-embedding')
        self.assertNotIn('providerSecret',response.text)
        response=self.client.post('/api/resource-library/ai/config',json={'embedding':{'apiKey':''}})
        self.assertTrue(response.json()['embedding']['hasApiKey'])
        response=self.client.post('/api/resource-library/ai/config',json={'embedding':{'clearKey':True}})
        self.assertFalse(response.json()['embedding']['hasApiKey'])

    def test_batch_embedding_job_and_semantic_search_are_accessible_through_http_and_mcp(self):
        assets=self.fixture_project();ai=self.desktop.resources.ai
        ai.client.close()
        def provider(request):
            self.assertTrue(request.url.path.endswith('/embeddings'))
            body=json.loads(request.read())
            return httpx.Response(200,json={'data':[{'index':i,'embedding':[1.,.1,.2]} for i,_ in enumerate(body['input'])]})
        ai.client=httpx.Client(transport=httpx.MockTransport(provider))
        response=self.client.post('/api/resource-library/ai/config',json={'embedding':{'baseUrl':'https://fixture.invalid/v1','model':'fixture-model','apiKey':'fixture-api-key'}})
        self.assertEqual(response.status_code,200,response.text)
        response=self.client.post('/api/resource-library/ai/jobs',json={'kind':'embed','assetIds':[assets['texture.png']['id']]})
        self.assertEqual(response.status_code,202,response.text)
        identifier=response.json()['jobId'];deadline=time.monotonic()+8
        while True:
            job=self.client.get('/api/resource-library/ai/jobs/'+identifier).json()
            if job['state'] not in ('queued','running'):break
            self.assertLess(time.monotonic(),deadline);time.sleep(.01)
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['progress']['succeeded'],1)
        response=self.client.post('/api/resource-library/search',json={'mode':'semantic','query':'a red interface icon','kind':'image'})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual(result['items'][0]['id'],assets['texture.png']['id'])
        self.assertIn('thumbnailUrl',result['items'][0])
        self.assertEqual(result['queryGeneratedBy'],'external-api')
        rpc=self.rpc('assets','tools/call',{'name':'asset_search','arguments':{'mode':'semantic','query':'a red interface icon','kind':'image'}})
        self.assertFalse(rpc.get('isError'),rpc)
        self.assertEqual(rpc['structuredContent']['items'][0]['id'],assets['texture.png']['id'])

    def test_thumbnail_is_binary_bounded_and_alias_cannot_reach_model_configuration(self):
        assets=self.fixture_project();item=assets['texture.png'];path='/api/resource-library/assets/'+item['id']+'/thumbnail'
        response=self.client.get(path)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.headers['content-type'],'image/png')
        self.assertEqual(hashlib.sha256(response.content).hexdigest(),response.headers['X-Content-SHA256'])
        with Image.open(io.BytesIO(response.content)) as image:self.assertLessEqual(max(image.size),512)
        head=self.client.head(path)
        self.assertEqual(head.content,b'')
        self.assertEqual(int(head.headers['content-length']),len(response.content))
        self.assertEqual(self.client.get(path,headers={'If-None-Match':response.headers['etag']}).status_code,304)
        alias='/api/knowledge/resource-library/assets/'+item['id']+'/thumbnail'
        self.assertEqual(self.client.get(alias).content,response.content)
        self.assertEqual(self.client.post('/api/knowledge/resource-library/ai/config',json={}).status_code,404)
        (self.source/'texture.png').write_bytes(b'source-changed')
        self.assertEqual(self.client.get(path).status_code,409)

    def test_paired_relay_resource_alias_streams_bytes_without_base64(self):
        item=self.fixture_project()['texture.png']
        saved=[]
        async def upload(content,**kwargs):
            if isinstance(content,bytes):value=content
            else:
                chunks=[]
                async for chunk in content:chunks.append(chunk)
                value=b''.join(chunks)
            saved.append(value)
            return {'id':'fixture-binary-blob','bytes':len(value),'sha256':hashlib.sha256(value).hexdigest()}
        path='/api/knowledge/resource-library/assets/'+item['id']+'/content'
        with patch.object(self.desktop.relay,'upload_blob',side_effect=upload):
            response=self.client.portal.call(self.desktop.relay_execute,{'method':'GET','path':path,'body':{},'headers':{}})
        self.assertEqual(response['status'],200)
        self.assertEqual(response['blobId'],'fixture-binary-blob')
        self.assertEqual(saved,[self.image_bytes])
        self.assertNotIn('body',response)

    def test_model_settings_reject_foreign_origins_and_search_validates_schema(self):
        response=self.client.post('/api/resource-library/ai/config',json={'embedding':{'apiKey':'never-save'}},headers={'Origin':'https://foreign.invalid'})
        self.assertEqual(response.status_code,403)
        self.assertFalse(self.desktop.resources.ai.status()['embedding']['hasApiKey'])
        response=self.client.post('/api/resource-library/search',json={'mode':'invented','query':'x'})
        self.assertEqual(response.status_code,400,response.text)
        response=self.client.get('/static/../../data/resource-workspace/asset-ai/config.json')
        self.assertEqual(response.status_code,404)

    def test_json_limit_is_explicit_and_foreign_origin_does_not_change_configuration(self):
        response=self.client.post('/api/netdisk/config',content=b' '*(4*1024*1024+1),headers={'Content-Type':'application/json'})
        self.assertEqual(response.status_code,413)
        self.assertFalse(self.desktop.resources.drive.status()['configured'])


if __name__ == '__main__': unittest.main()
