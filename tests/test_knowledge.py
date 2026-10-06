import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave

try:
    from desktop.knowledge import KnowledgeService, KnowledgeError, API
except ImportError:
    from knowledge import KnowledgeService, KnowledgeError, API


PNG = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 8, 6) + bytes([8, 2, 0, 0, 0]) + b'test-data'


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.shared = self.root / 'shared-files'
        self.shared.mkdir()
        self.service = KnowledgeService(self.root / 'store', file_roots=[self.shared])

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def document(self, **fields):
        return self.service.save_document({'kind': 'memory', 'title': '测试记忆', 'content': '# 原始正文\n测试向量段落', **fields})

    def batch(self, doc, **fields):
        return {'space': 'test', 'dimension': 2, 'model': 'external-model', 'vectors': [
            {'documentId': doc['id'], 'revision': doc['revision'], 'chunkIndex': 0, 'content': '测试向量段落', 'vector': [3., 4.], **fields}]}

    def test_markdown_persistence_revision_and_context_flags(self):
        doc = self.document(tags=['Android', 'Android'])
        self.assertEqual(doc['tags'], ['Android'])
        raw = self.service.markdown_root / doc['id'] / 'revision-1.md'
        self.assertEqual(raw.read_text(), doc['content'])
        self.assertIn(doc['content'], self.service.bootstrap())
        saved = self.service.save_document({'id': doc['id'], 'title': '更新标题', 'expectedRevision': 1})
        self.assertEqual(saved['revision'], 2)
        self.assertEqual(saved['content'], doc['content'])
        self.assertFalse(raw.exists())
        with self.assertRaises(KnowledgeError) as ctx:
            self.service.save_document({'id': doc['id'], 'content': '覆盖', 'expectedRevision': 1})
        self.assertEqual(ctx.exception.status, 409)
        hidden = self.document(title='不自动加载', content='此段仅手动加载', autoLoad=False)
        self.assertNotIn(hidden['content'], self.service.bootstrap())
        self.assertEqual(self.service.search_documents({'query': '手动加载'})['sources'][0]['id'], hidden['id'])
        self.service.close()
        self.service = KnowledgeService(self.root / 'store', file_roots=[self.shared])
        self.assertEqual(self.service.read_document(doc['id'])['title'], '更新标题')
        self.assertTrue(self.service.delete_document(doc['id'])['deleted'])
        self.assertFalse((self.service.markdown_root / doc['id']).exists())

    def test_audio_notes_filtering_references_and_explicit_loading(self):
        source = io.BytesIO()
        with wave.open(source, 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 1600)
        recording = self.service.import_attachment(source.getvalue(), 'owned.wav', 'audio/wav')
        self.service.import_attachment(PNG, 'owned.png', 'image/png')
        page = self.service.handle('GET', API + '/attachments', {'mediaType': 'audio', 'limit': '1'})
        self.assertEqual(page.data['total'], 1)
        self.assertEqual(page.data['attachments'][0]['id'], recording['id'])
        note = self.document(kind='note', content='[录音](' + recording['contentPath'] + ')')
        self.assertFalse(note['autoLoad'])
        self.assertNotIn(note['content'], self.service.bootstrap())
        self.assertEqual(self.service.list_documents('note')['total'], 1)
        self.assertEqual(self.service.document_stats()['noteDocuments'], 1)
        with self.assertRaises(KnowledgeError):
            self.service.delete_attachment(recording['id'])
        self.assertEqual(self.service.list_resources({'kind': 'audio'})['items'][0]['id'], recording['id'])
        self.service.delete_document(note['id'])
        self.service.delete_attachment(recording['id'])

    def test_vector_batch_rolls_back_and_document_change_invalidates(self):
        doc = self.document()
        invalid = self.batch(doc)
        invalid['vectors'].append({**invalid['vectors'][0], 'chunkIndex': 1, 'content': '不属于正文'})
        with self.assertRaises(KnowledgeError):
            self.service.import_vectors(invalid)
        self.assertEqual(self.service.vector_status()['totalSpaces'], 0)
        self.assertEqual(self.service.import_vectors(self.batch(doc))['imported'], 1)
        sources = self.service.search_vectors({'space': 'test', 'vector': [3, 4]})['sources']
        self.assertAlmostEqual(sources[0]['score'], 1., places=6)
        self.assertEqual(sources[0]['documentId'], doc['id'])
        self.service.save_document({'id': doc['id'], 'content': '修改正文', 'expectedRevision': 1})
        self.assertEqual(self.service.vector_status()['totalVectors'], 0)
        self.assertEqual(self.service.search_vectors({'space': 'test', 'vector': [3, 4]})['sources'], [])
        with self.assertRaises(KnowledgeError):
            self.service.import_vectors(self.batch(doc))
        self.service.delete_vector_space('test')
        self.assertEqual(self.service.read_document(doc['id'])['content'], '修改正文')

    def test_disabled_documents_excluded_from_retrieval(self):
        doc = self.document(enabled=False)
        self.service.import_vectors(self.batch(doc))
        self.assertEqual(self.service.search_documents({'query': '测试'})['sources'], [])
        self.assertEqual(self.service.search_vectors({'space': 'test', 'vector': [3, 4]})['sources'], [])
        self.assertNotIn(doc['content'], self.service.bootstrap())
        self.assertEqual(self.service.list_documents()['total'], 1)

    def test_schedules_execute_once_repeat_skip_and_restart_does_not_replay(self):
        calls = []
        self.service.dispatch = lambda tool, args: calls.append((tool, args)) or {'ok': True}
        once = self.service.save_schedule({'title': '单次', 'toolName': 'test.echo', 'arguments': {'text': 'x'}, 'runAt': '2020-01-01T00:00:00Z'})
        self.assertTrue(self.service.run_due_once())
        self.assertFalse(self.service.run_due_once())
        self.assertEqual(len(calls), 1)
        finished = self.service.read_schedule(once['id'])
        self.assertEqual(finished['lastStatus'], 'completed')
        self.assertFalse(finished['enabled'])
        repeat = self.service.save_schedule({'title': '重复', 'toolName': 'test.echo', 'runAt': '2020-01-01T00:00:00Z', 'intervalMinutes': 10})
        claim = self.service.claim_due(now=repeat['runAtMillis'] + 25 * 60000)[0]
        self.assertEqual(claim['nextRunMillis'], repeat['runAtMillis'] + 30 * 60000)
        with self.assertRaises(KnowledgeError):
            self.service.delete_schedule(repeat['id'])
        self.service.close()
        self.service = KnowledgeService(self.root / 'store', file_roots=[self.shared])
        recovered = self.service.read_schedule(repeat['id'])
        self.assertEqual(recovered['lastStatus'], 'interrupted')
        self.assertFalse(recovered['running'])
        # A one-shot claimed before process termination must never run twice.
        pending = self.service.save_schedule({'title': '中断单次', 'toolName': 'test.echo', 'runAt': '2020-01-01T00:00:00Z'})
        self.service.claim_due()
        self.service.close()
        self.service = KnowledgeService(self.root / 'store', file_roots=[self.shared])
        recovered = self.service.read_schedule(pending['id'])
        self.assertFalse(recovered['enabled'])
        self.assertEqual(recovered['lastStatus'], 'interrupted')
        self.assertEqual(self.service.claim_due(), [])

    def test_attachment_stream_references_and_read_only_shared_directory(self):
        image = self.shared / 'test.png'
        image.write_bytes(PNG)
        attachment = self.service.import_file(str(image))
        self.assertEqual(attachment['bytes'], len(PNG))
        self.assertEqual(attachment['mimeType'], 'image/png')
        path = attachment['contentPath']
        self.assertEqual(path, API + '/attachments/' + attachment['id'] + '/content')
        doc = self.document(content='![测试](' + path + ')')
        with self.assertRaises(KnowledgeError) as ctx:
            self.service.delete_attachment(attachment['id'])
        self.assertEqual(ctx.exception.status, 409)
        resources = self.service.list_resources({})
        self.assertEqual(resources['items'][0]['referenceDocuments'][0]['id'], doc['id'])
        self.assertFalse(resources['items'][0]['canDelete'])
        renamed = self.service.rename_attachment(attachment['id'], '新名称.png')
        self.assertEqual(renamed['contentPath'], path)
        response = self.service.handle('GET', path)
        self.assertEqual(response.file.read_bytes(), PNG)
        self.assertTrue(self.service.list_attachments()['unlimited'])
        self.assertTrue(image.exists())
        self.service.delete_document(doc['id'])
        self.assertTrue(self.service.delete_attachment(attachment['id'])['deleted'])
        outside = self.root / 'private.png'
        outside.write_bytes(PNG)
        (self.shared / 'escape.png').symlink_to(outside)
        entries = self.service.browse_files({'path': str(self.shared)})['entries']
        self.assertEqual([entry['name'] for entry in entries], ['test.png'])
        for selected in (outside, self.shared / 'escape.png'):
            with self.assertRaises(KnowledgeError) as ctx:
                self.service.import_file(str(selected))
            self.assertEqual(ctx.exception.status, 403)

    def test_http_and_mcp_contract_and_upload_rollback(self):
        response = self.service.handle('POST', API + '/documents', data={'kind': 'skill', 'title': '调用工具', 'content': '仅作为参考数据'})
        self.assertEqual(response.status, 200)
        doc = response.data
        conflict = self.service.handle('POST', API + '/documents', data={'id': doc['id'], 'expectedRevision': 0})
        self.assertEqual(conflict.status, 409)
        exported = self.service.handle('GET', API + '/export/' + doc['id'])
        self.assertEqual(exported.body.decode(), doc['content'])
        self.assertEqual(exported.mime, 'text/markdown; charset=utf-8')
        context = self.service.read_resource('knowledge://bootstrap')
        self.assertIn(doc['content'], context['contents'][0]['text'])
        tool = self.service.call_tool('knowledge_read_document', {'id': doc['id']})
        self.assertFalse(tool['isError'])
        self.assertEqual(tool['structuredContent']['kind'], 'skill')
        self.assertTrue(self.service.call_tool('knowledge_read_document', {'id': doc['id'], 'unknown': True})['isError'])
        self.assertIn(doc['content'], self.service.get_prompt('research_context')['messages'][0]['content']['text'])
        bad = self.service.handle('POST', API + '/attachments/upload', body=io.BytesIO(PNG), headers={'Content-Length': str(len(PNG) + 1)})
        self.assertEqual(bad.status, 400)
        self.assertEqual(list(self.service.attachment_root.iterdir()), [])
        good = self.service.handle('POST', API + '/attachments/upload', {'name': 'test.png'}, body=io.BytesIO(PNG), headers={'Content-Length': str(len(PNG))})
        self.assertEqual(good.status, 200)
        self.assertEqual(good.data['bytes'], len(PNG))
        self.assertIsNone(self.service.handle('GET', API + '/status'))
        self.assertEqual(self.service.handle('POST', API + '/media/edit-image', data={}).status, 501)

    def test_short_stream_reads_and_invalid_vectors_leave_no_state(self):
        class ShortReads(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3) if size >= 0 else 3)
        attachment = self.service.import_attachment(ShortReads(PNG), '分块.png', length=len(PNG))
        response = self.service.handle('GET', attachment['contentPath'])
        self.assertEqual(response.file.read_bytes(), PNG)
        doc = self.document()
        for vector in ([0., 0.], [float('nan'), 1.], [1.], [True, 1.]):
            with self.assertRaises(KnowledgeError):
                self.service.import_vectors(self.batch(doc, vector=vector))
        self.assertEqual(self.service.vector_status()['totalVectors'], 0)
        self.assertEqual(self.service.vector_status()['totalSpaces'], 0)

    def test_failed_tool_result_finishes_claim_and_unaware_time_is_rejected(self):
        self.service.dispatch = lambda tool, args: {'isError': True, 'structuredContent': {'error': '设备离线'}}
        schedule = self.service.save_schedule({'title': '失败任务', 'toolName': 'test.echo', 'runAt': '2020-01-01T00:00:00Z'})
        self.assertTrue(self.service.run_due_once())
        failed = self.service.read_schedule(schedule['id'])
        self.assertEqual(failed['lastStatus'], 'failed')
        self.assertFalse(failed['running'])
        self.assertFalse(failed['enabled'])
        self.assertIn('设备离线', failed['lastError'])
        with self.assertRaises(KnowledgeError):
            self.service.save_schedule({'title': '无时区', 'toolName': 'test.echo', 'runAt': '2026-10-06T09:00:00'})
        self.assertEqual(self.service.list_schedules()['total'], 1)


if __name__ == '__main__':
    unittest.main()
