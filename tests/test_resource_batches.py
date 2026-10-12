import hashlib
import io
from pathlib import Path
import tempfile
import time
import unittest
import uuid
import zipfile
try:
    from desktop.knowledge import KnowledgeService, API
except ImportError:
    from knowledge import KnowledgeService, API

class ResourceBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=KnowledgeService(self.root / 'store')
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
    def upload(self, name, data, mime='application/octet-stream'):
        response=self.store.handle('POST', API+'/attachments/upload', {'name':name}, headers={'content-type':mime,'content-length':str(len(data))},body=io.BytesIO(data))
        self.assertEqual(response.status,200)
        return response.data
    def await_job(self, identifier):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=self.store.resource_batches.status(identifier)
            if result['state'] in ('completed','failed'):
                return result
            time.sleep(.01)
        self.fail('ZIP did not complete')
    def test_arbitrary_files_empty_files_restart_and_sync_metadata(self):
        for name,data,mime in [('report.pdf',b'%PDF-1.7\nmock','application/pdf'),('run.sh',b'#!/bin/sh\nexit 0','text/plain'),('page.svg',b'<svg onload="alert(1)">','image/svg+xml'),('empty.bin',b'','application/octet-stream')]:
            item=self.upload(name,data,mime)
            self.assertEqual(item['kind'],'file')
            self.assertEqual(item['mimeType'],'application/octet-stream')
            response=self.store.handle('GET',item['contentPath'])
            self.assertEqual(response.file.read_bytes(),data)
            self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))
            self.assertEqual(item['sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(self.store.list_resources({'kind':'file'})['total'],4)
        self.store.close()
        self.store=KnowledgeService(self.root/'store')
        self.assertEqual(self.store.list_attachments(media_type='file')['total'],4)
    def test_zip_has_all_selected_bytes_and_does_not_change_sources(self):
        originals=[self.upload('../same.bin',b'abc'*90000),self.upload('same.bin',b'xyz'*50000)]
        fields={'items':[{'source':'attachment','id':item['id']} for item in originals]}
        response=self.store.handle('POST',API+'/resources/batch-export',data=fields)
        self.assertEqual(response.status,202)
        completed=self.await_job(response.data['jobId'])
        self.assertEqual(completed['state'],'completed')
        download=self.store.handle('GET',completed['downloadPath'])
        with zipfile.ZipFile(download.file) as archive:
            self.assertEqual(len(archive.namelist()),2)
            self.assertTrue(all(not name.startswith('/') and '../' not in name for name in archive.namelist()))
            self.assertEqual([hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()],[item['sha256'] for item in originals])
        self.assertFalse(self.store._media_pins)
        self.assertEqual(self.store.list_attachments()['total'],2)
    def test_zip_based_documents_accept_correct_browser_mime(self):
        blob=io.BytesIO()
        with zipfile.ZipFile(blob,'w') as archive:
            archive.writestr('[Content_Types].xml','<Types/>')
            archive.writestr('word/document.xml','<document/>')
        item=self.upload('document.docx',blob.getvalue(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        self.assertEqual(item['name'],'document.docx')
        self.assertEqual(item['kind'],'archive')
        self.assertEqual(self.store.handle('GET',item['contentPath']).file.read_bytes(),blob.getvalue())
    def test_missing_item_fails_whole_zip_without_download(self):
        item=self.upload('keep.bin',b'abc')
        job=self.store.resource_batches.start({'items':[{'source':'attachment','id':item['id']},{'source':'attachment','id':str(uuid.uuid4())}]})
        failed=self.await_job(job['jobId'])
        self.assertEqual(failed['state'],'failed')
        self.assertNotIn('downloadPath',failed)
        self.assertEqual(list(self.store.resource_batches.root.iterdir()),[])
        self.assertFalse(self.store._media_pins)
    def test_batch_delete_keeps_referenced_item_and_reports_partial_result(self):
        keep=self.upload('keep.txt',b'keep')
        remove=self.upload('remove.txt',b'remove')
        self.store.save_document({'kind':'note','title':'link','content':'[keep]('+keep['contentPath']+')'})
        fields={'items':[{'source':'attachment','id':item['id']} for item in (keep,remove)]}
        reply=self.store.call_tool('knowledge_delete_resource_batch',fields)
        self.assertFalse(reply['isError'])
        result=reply['structuredContent']
        self.assertEqual((result['deletedCount'],result['failedCount']),(1,1))
        self.assertEqual(self.store.list_attachments()['total'],1)
        self.assertEqual(self.store.read_attachment(keep['id'])['id'],keep['id'])
    def test_invalid_batch_has_no_side_effects(self):
        item=self.upload('keep.bin',b'keep')
        duplicate={'items':[{'source':'attachment','id':item['id']}]*2}
        for route in ('batch-delete','batch-export'):
            response=self.store.handle('POST',API+'/resources/'+route,data=duplicate)
            self.assertEqual(response.status,400)
        self.assertEqual(self.store.list_attachments()['total'],1)
