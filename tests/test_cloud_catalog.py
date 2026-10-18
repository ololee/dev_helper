"""Portable catalog cloud tests use generated files and an in-memory provider."""
import hashlib
import io
import json
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from baidu_drive import BaiduDriveError
from server import Desktop
from starlette.testclient import TestClient


class MemoryCloud:
    def __init__(self):
        self.files={};self.paths={};self.dirs={'/apps/Fixture'};self._jobs={};self.upload_count=0;self.downloads=[]
        self.config={};self.lock=threading.RLock()

    def _token(self):return 'generated-offline-token'
    def close(self):pass
    def remote_path(self,path='',*,file=False):
        if not isinstance(path,str) or '..' in path.split('/') or '\\' in path:raise BaiduDriveError('Invalid path')
        value=path.rstrip('/') if path.startswith('/') else '/apps/Fixture'+('/'+path.rstrip('/') if path else '')
        if value!='/apps/Fixture' and not value.startswith('/apps/Fixture/'):raise BaiduDriveError('Outside scope')
        return value
    def mkdir(self,path):
        path=self.remote_path(path)
        if path in self.dirs:raise BaiduDriveError('Existing directory',409,provider_code='-8')
        self.dirs.add(path);return {'path':path,'isdir':1}
    def list_files(self,path='',start=0,limit=100):
        path=self.remote_path(path)
        if path not in self.dirs:raise BaiduDriveError('Missing directory',404,provider_code='-9')
        items=[{'path':d,'isdir':1,'server_filename':d.rsplit('/',1)[-1]} for d in sorted(self.dirs) if d!=path and d.rsplit('/',1)[0]==path]
        items.extend(self.file_info(fs) for fs,value in self.files.items() if value['path'].rsplit('/',1)[0]==path)
        return {'path':path,'items':items[start:start+limit],'hasMore':len(items)>start+limit,'directoryExists':True}
    def file_info(self,fs):
        value=self.files[str(fs)];return {k:value[k] for k in ('fs_id','path','server_filename','isdir','size','md5')}
    def job(self,identifier):return dict(self._jobs[identifier])
    def jobs(self):return {'items':list(self._jobs.values())}
    def cancel(self,identifier):self._jobs[identifier]['state']='cancelled';return self.job(identifier)
    def start_upload(self,local,remote_path,*,name=None):
        content=Path(local).read_bytes();remote_path=self.remote_path(remote_path);self.upload_count+=1
        fs=str(len(self.files)+100);digest=hashlib.sha256(content).hexdigest();md5=hashlib.md5(content).hexdigest()
        self.files[fs]={'fs_id':int(fs),'path':remote_path,'server_filename':remote_path.rsplit('/',1)[-1],'isdir':0,'size':len(content),'md5':md5,'content':content}
        self.paths[remote_path]=fs;identifier=str(uuid.uuid4())
        self._jobs[identifier]={'jobId':identifier,'state':'completed','operation':'upload','result':{'fsId':fs,'remotePath':remote_path,'pathConfirmed':True,'bytes':len(content),'sha256':digest,'md5':md5}}
        return self.job(identifier)
    def start_download(self,fs,local,*,expected_sha256=None,expected_md5=None):
        value=self.files[str(fs)];data=value['content'];sha=hashlib.sha256(data).hexdigest();md5=hashlib.md5(data).hexdigest();identifier=str(uuid.uuid4())
        self.downloads.append(str(fs))
        if expected_sha256 and expected_sha256!=sha or expected_md5 and expected_md5!=md5:
            self._jobs[identifier]={'jobId':identifier,'state':'failed','operation':'download','error':'Digest mismatch'}
        else:
            with Path(local).open('xb') as f:f.write(data)
            self._jobs[identifier]={'jobId':identifier,'state':'completed','operation':'download','result':{'fsId':str(fs),'remotePath':value['path'],'bytes':len(data),'sha256':sha,'md5':md5}}
        return self.job(identifier)


class CloudCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve();self.cloud=MemoryCloud();self.apps=[]
        self.desktop,self.client=self.app('source')
        self.source=self.root/'generated-project';self.source.mkdir()
        picture=io.BytesIO();Image.new('RGB',(20,12),(50,120,210)).save(picture,'PNG');self.picture=picture.getvalue()
        (self.source/'button.png').write_bytes(self.picture)
        (self.source/'button.png.meta').write_text('fileFormatVersion: 2\nguid: '+'a'*32+'\n')
        (self.source/'unit.prefab').write_text('%YAML 1.1\nMonoBehaviour:\n  texture: {fileID: 1, guid: '+'a'*32+', type: 3}\n')
        library=self.desktop.resources.library;self.project=library.register_project(self.source,'Generated portable fixture')
        scan=library.start_scan(self.project['id'])
        while scan['state'] in ('queued','running'):time.sleep(.01);scan=library.job(scan['jobId'])
        self.assertEqual(scan['state'],'completed',scan)
        self.assets={x['name']:x for x in library.list_assets(project_id=self.project['id'])['items']}
        self.image=self.assets['button.png'];library.save_annotation(self.image['id'],{'source':'manual','categories':['gui'],'purpose':'Generated button'})
        library.import_embeddings(self.project['id'],'generated-test-vector-v1',[{'assetId':self.image['id'],'vector':[1,0,0]},{'assetId':self.assets['unit.prefab']['id'],'vector':[.9,.1,0]}])

    def app(self,name):
        desktop=Desktop(8876,self.root/name/'data',self.root/name/'shared')
        with patch.object(desktop.discovery,'start'),patch.object(desktop.discovery,'stop'):
            client=TestClient(desktop.app(),base_url='http://127.0.0.1:8876');client.__enter__()
        desktop.resources.drive.close();desktop.resources.drive=self.cloud;self.apps.append((desktop,client))
        return desktop,client

    def tearDown(self):
        for desktop,client in reversed(self.apps):
            with patch.object(desktop.discovery,'stop'):client.__exit__(None,None,None)
        self.temp.cleanup()

    def wait(self,client,job,base='/api/resource-library/catalog/jobs'):
        deadline=time.monotonic()+15
        while job['state'] in ('queued','running'):
            self.assertLess(time.monotonic(),deadline,job);time.sleep(.01)
            response=client.get(base+'/'+job['jobId']);self.assertEqual(response.status_code,200,response.text);job=response.json()
        return job

    def publish(self):
        response=self.client.post('/api/resource-library/catalog/publish',json={'projectIds':[self.project['id']]})
        self.assertEqual(response.status_code,202,response.text)
        job=self.wait(self.client,response.json());self.assertEqual(job['state'],'completed',job);return job['result']

    def upload_image(self):
        response=self.client.post('/api/resource-library/backup',json={'assetIds':[self.image['id']],'remoteDirectory':'originals'})
        self.assertEqual(response.status_code,200,response.text)
        job=self.wait(self.client,response.json(),'/api/resource-library/backups');self.assertEqual(job['state'],'completed',job)
        return job

    def test_clean_computer_restores_search_vectors_dependencies_without_sources_then_fetches_one(self):
        self.upload_image();manifest=self.publish()
        target,client=self.app('target');self.source.rename(self.root/'old-computer-unavailable')
        with patch.object(target.resources.library,'start_scan',side_effect=AssertionError('Restore rescanned sources')),patch.object(target.resources.ai.client,'request',side_effect=AssertionError('Restore called a model')):
            response=client.post('/api/resource-library/catalog/restore',json={'manifestFsId':manifest['manifestFsId']})
            self.assertEqual(response.status_code,202,response.text);job=self.wait(client,response.json())
        self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['result']['originalFilesDownloaded'],0)
        searched=client.post('/api/resource-library/search',json={'query':'Generated button'}).json()
        self.assertEqual(searched['total'],1)
        asset=searched['items'][0];self.assertEqual(asset['id'],self.image['id']);self.assertEqual(asset['storage']['state'],'remote-only');self.assertTrue(asset['storage']['canFetch'])
        self.assertNotIn('thumbnailUrl',asset)
        with target.resources.library._db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0],2)
        similar=client.post('/api/resource-library/similar',json={'assetId':asset['id'],'mode':'semantic','model':'generated-test-vector-v1'})
        self.assertEqual(similar.status_code,200,similar.text);self.assertEqual(similar.json()['items'][0]['id'],self.assets['unit.prefab']['id'])
        deps=client.post('/api/resource-library/dependencies',json={'assetId':self.assets['unit.prefab']['id']}).json();self.assertTrue(deps['edges'])
        self.assertEqual(client.get(asset['contentUrl']).status_code,409)
        before=len(self.cloud.downloads)
        fetched=client.post('/api/resource-library/catalog/fetch',json={'assetId':asset['id']});self.assertEqual(fetched.status_code,202,fetched.text)
        job=self.wait(client,fetched.json());self.assertEqual(job['state'],'completed',job)
        self.assertEqual(len(self.cloud.downloads),before+1)
        self.assertEqual(client.get(asset['contentUrl']).content,self.picture)
        refreshed=client.post('/api/resource-library/get',json={'assetId':asset['id']}).json();self.assertEqual(refreshed['storage']['state'],'cached');self.assertIn('thumbnailUrl',refreshed)

    def test_catalog_listing_downloads_only_manifests_and_preserves_missing_original_coverage(self):
        manifest=self.publish();self.assertEqual(manifest['uploadedAssetCount'],0)
        self.cloud.downloads.clear();response=self.client.get('/api/resource-library/catalog/snapshots')
        self.assertEqual(response.status_code,200,response.text);items=response.json()['items'];self.assertEqual(len(items),1)
        self.assertEqual(items[0]['snapshotId'],manifest['snapshotId'])
        self.assertEqual(self.cloud.downloads,[manifest['manifestFsId']])
        self.assertNotIn(manifest['catalogFsId'],self.cloud.downloads)
        self.assertEqual(self.client.post('/api/resource-library/catalog/fetch',json={'assetId':self.assets['unit.prefab']['id']}).status_code,202)  # source is still local

    def test_tampered_catalog_fails_without_overwriting_existing_index(self):
        manifest=self.publish();target,client=self.app('target')
        fs=manifest['catalogFsId'];self.cloud.files[fs]['content']=b'x'*self.cloud.files[fs]['size']
        response=client.post('/api/resource-library/catalog/restore',json={'manifestFsId':manifest['manifestFsId']})
        job=self.wait(client,response.json());self.assertEqual(job['state'],'failed',job)
        self.assertEqual(target.resources.library.list_assets()['total'],0)
        self.assertEqual(list(target.resources.catalog.transient.iterdir()),[])

    def test_invalid_manifest_timestamps_and_boolean_versions_are_rejected(self):
        manifest=self.publish();fs=manifest['manifestFsId'];original=self.cloud.files[fs]['content']
        for fields in ({'createdAt':'bad'},{'createdAt':True},{'createdAt':float('inf')},{'schemaVersion':True}):
            value=json.loads(original);value.update(fields);raw=json.dumps(value).encode();self.cloud.files[fs].update(content=raw,size=len(raw))
            response=self.client.get('/api/resource-library/catalog/snapshots');self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['items'],[]);self.assertEqual(len(response.json()['errors']),1)

    def test_foreign_origin_and_unknown_fields_cannot_start_cloud_index_jobs(self):
        response=self.client.post('/api/resource-library/catalog/publish',json={},headers={'Origin':'https://foreign.invalid'})
        self.assertEqual(response.status_code,403);self.assertEqual(self.cloud.upload_count,0)
        response=self.client.post('/api/resource-library/catalog/restore',json={'manifestFsId':'123','apiKey':'untrusted'})
        self.assertEqual(response.status_code,400);self.assertEqual(self.cloud.downloads,[])

    def test_republish_after_restore_keeps_cloud_locations_and_original_vectors(self):
        self.upload_image();manifest=self.publish();target,client=self.app('target')
        response=client.post('/api/resource-library/catalog/restore',json={'manifestFsId':manifest['manifestFsId']});job=self.wait(client,response.json());self.assertEqual(job['state'],'completed',job)
        response=client.post('/api/resource-library/catalog/publish',json={});job=self.wait(client,response.json());self.assertEqual(job['state'],'completed',job)
        self.assertEqual(job['result']['uploadedAssetCount'],1);self.assertEqual(job['result']['embeddingCount'],2)

    def test_old_receipts_require_provider_confirmation_and_missing_path_flags_are_not_success(self):
        job=self.upload_image();workspace=self.desktop.resources
        with workspace.lock:
            workspace.backups[job['jobId']]['items'][0]['result'].pop('pathConfirmed')
        self.assertEqual(workspace.catalog.locations(),[])
        confirmed=workspace.catalog.locations(confirm_legacy=True)
        self.assertEqual(len(confirmed),1)
        self.assertTrue(workspace.backup(job['jobId'])['items'][0]['result']['pathConfirmed'])
        with self.assertRaises(BaiduDriveError):
            workspace.catalog._confirmed({'remotePath':'/apps/Fixture/catalogs/test','sha256':'a'*64,'bytes':12},'/apps/Fixture/catalogs/test','a'*64,12)

    def test_classified_upload_can_queue_index_and_vectors_as_a_separate_real_job(self):
        data={'assetIds':[self.image['id']],'remoteDirectory':'classified','layout':'classified'}
        plan=self.client.post('/api/resource-library/backup-plan',json=data).json()
        response=self.client.post('/api/resource-library/backup',json={**data,'planDigest':plan['planDigest'],'syncCatalog':True})
        job=self.wait(self.client,response.json(),'/api/resource-library/backups')
        deadline=time.monotonic()+3
        while not job.get('catalogSyncJobId') and time.monotonic()<deadline:
            time.sleep(.01);job=self.client.get('/api/resource-library/backups/'+job['jobId']).json()
        self.assertIn('catalogSyncJobId',job)
        sync=self.wait(self.client,self.client.get('/api/resource-library/catalog/jobs/'+job['catalogSyncJobId']).json())
        self.assertEqual(sync['state'],'completed',sync);self.assertEqual(sync['result']['embeddingCount'],2)


if __name__=='__main__':unittest.main()
