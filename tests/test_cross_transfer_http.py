"""Cross-device HTTP/MCP control plane uses generated plans and no cloud writes."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from server import Desktop
from starlette.testclient import TestClient


class CrossTransferHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name).resolve()
        self.desktop=Desktop(8876,root/'data',root/'shared')
        self.cross=Mock()
        self.cross.plan.side_effect=lambda source,target,operation,items,destination:{
            'items':[{'name':'generated.bin','source':items[0],'destination':destination}],
            'fileCount':1,'bytes':128,'_private':'never-public'}
        self.cross.execute.return_value={'state':'completed','items':[{'state':'completed'}],
                                         'sourceCleanupState':'retained','moveCommitted':False}
        self.desktop.resources.file_ops._transfer=self.cross
        self.patches=[patch.object(self.desktop.discovery,'start'),patch.object(self.desktop.discovery,'stop')]
        for active in self.patches:active.start()
        self.client=TestClient(self.desktop.app(),base_url='http://127.0.0.1:8876')
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        for active in reversed(self.patches):active.stop()
        self.temp.cleanup()

    def plan(self,source='local',operation='copy'):
        return self.client.post('/api/resource-library/file-ops/plan',json={
            'scope':source,'destinationScope':'netdisk' if source=='local' else 'local','operation':operation,
            'items':[{'projectId':'generated-project','path':'generated.bin'}] if source=='local' else [{'path':'generated.bin','fsId':'123'}],
            'destination':{'path':'target'} if source=='local' else {'projectId':'generated-project','path':'target'}})

    def wait_job(self,identifier):
        for _ in range(400):
            response=self.client.get('/api/resource-library/file-ops/jobs/'+identifier)
            self.assertEqual(response.status_code,200,response.text)
            value=response.json()
            if value['state'] not in ('queued','running'):return value
            time.sleep(.005)
        self.fail('generated transfer did not settle')

    def test_cross_paste_accepts_both_destinations_and_retains_history(self):
        for source in ('local','netdisk'):
            response=self.plan(source)
            self.assertEqual(response.status_code,200,response.text)
            plan=response.json();self.assertEqual(plan['scope'],'transfer')
            self.assertEqual(plan['sourceScope'],source)
            self.assertNotIn('never-public',response.text)
            started=self.client.post('/api/resource-library/file-ops/execute',json={key:plan[key] for key in ('planId','planDigest')})
            self.assertEqual(started.status_code,202,started.text)
            final=self.wait_job(started.json()['jobId'])
            self.assertEqual(final['state'],'completed');self.assertEqual(final['sourceScope'],source)
        listing=self.client.get('/api/resource-library/file-ops/jobs').json()['items']
        self.assertEqual(len([item for item in listing if item['scope']=='transfer']),2)

    def test_target_scope_schema_origin_and_extra_paths_are_rejected(self):
        valid=self.plan().json()
        for fields in ({'scope':'local','destinationScope':'invalid','operation':'copy','items':[]},
                       {'scope':'local','destinationScope':'netdisk','operation':'trash','items':[{'projectId':'p','path':'a'}],'destination':{'path':'target'}},
                       {'scope':'netdisk','destinationScope':'local','operation':'copy','items':[{'path':'a'}],'destination':{'path':'target'}},
                       {'scope':'local','destinationScope':'netdisk','operation':'copy','items':[{'projectId':'p','path':'a','root':'/tmp'}],'destination':{'path':'target'}}):
            response=self.client.post('/api/resource-library/file-ops/plan',json=fields)
            self.assertEqual(response.status_code,400,response.text)
        response=self.client.post('/api/resource-library/file-ops/execute',headers={'Origin':'https://example.invalid'},
                                  json={key:valid[key] for key in ('planId','planDigest')})
        self.assertEqual(response.status_code,403)
        self.cross.execute.assert_not_called()

    def test_live_progress_cancellation_and_account_switch_guard(self):
        entered=threading.Event()
        def run(plan,progress,cancelled):
            progress({'phase':'downloading','bytesTransferred':64,'totalBytes':128,'currentFile':'generated.bin',
                      'completedFiles':0,'fileCount':1})
            entered.set()
            while not cancelled():time.sleep(.005)
            return {'state':'cancelled','items':[{'state':'cancelled','sourcePreserved':True}],
                    'sourceCleanupState':'retained','moveCommitted':False}
        self.cross.execute.side_effect=run
        plan=self.plan('netdisk','move').json()
        started=self.client.post('/api/resource-library/file-ops/execute',json={key:plan[key] for key in ('planId','planDigest')}).json()
        identifier=started['jobId']
        try:
            self.assertTrue(entered.wait(2))
            value=self.client.get('/api/resource-library/file-ops/jobs/'+identifier).json()
            self.assertEqual(value['progress']['bytesTransferred'],64)
            with patch.object(self.desktop.resources.drive,'configure') as configure:
                response=self.client.post('/api/netdisk/config',json={'appName':'changed'})
                self.assertEqual(response.status_code,409,response.text);configure.assert_not_called()
            response=self.client.post('/api/resource-library/file-ops/jobs/'+identifier+'/cancel',json={})
            self.assertEqual(response.status_code,200,response.text)
            final=self.wait_job(identifier)
            self.assertEqual(final['state'],'cancelled');self.assertEqual(final['result']['sourceCleanupState'],'retained')
        finally:
            self.desktop.resources.file_ops.cancel(identifier)

    def test_scoped_mcp_transfer_plan_execute_job_and_scope_isolation(self):
        def rpc(scope,name,arguments):
            response=self.client.post('/mcp/'+scope,headers={'Accept':'application/json, text/event-stream'},
                json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':arguments}})
            self.assertEqual(response.status_code,200,response.text)
            return response.json()['result']
        result=rpc('assets','asset_transfer_plan',{'operation':'copy','items':[{'projectId':'generated-project','path':'generated.bin'}],'destination':{'path':'target'}})
        self.assertFalse(result.get('isError'),result)
        plan=result['structuredContent']
        fields={key:plan[key] for key in ('planId','planDigest')}
        rejected=rpc('netdisk','netdisk_transfer_execute',fields)
        self.assertTrue(rejected.get('isError'));self.cross.execute.assert_not_called()
        result=rpc('assets','asset_transfer_execute',fields)
        self.assertFalse(result.get('isError'),result)
        identifier=result['structuredContent']['jobId'];self.wait_job(identifier)
        result=rpc('assets','asset_transfer_job',{'jobId':identifier})
        self.assertEqual(result['structuredContent']['state'],'completed')
        rejected=rpc('netdisk','netdisk_transfer_job',{'jobId':identifier})
        self.assertTrue(rejected.get('isError'))
