import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import wave

import httpx
from knowledge import KnowledgeService
from workflows import WorkflowService, WorkflowError


def audio():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes(b'\0\0' * 160)
    return stream.getvalue()


class FakeDesktop:
    def __init__(self, directory, handler):
        self.knowledge = KnowledgeService(Path(directory) / 'knowledge')
        self.phone = KnowledgeService(Path(directory) / 'phone')
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        self.calls = []
        self.wait_tool = None
        self.sync = self

    def tool_specs(self):
        return [dict(name='synthetic_echo', description='Read synthetic test data', inputSchema=dict(type='object', properties={'value': {'type': 'string'}}, required=['value'], additionalProperties=False))]

    async def dispatch(self, device, name, args):
        self.calls.append((device, name, args))
        if self.wait_tool:
            await self.wait_tool.wait()
        return dict(value=args['value'])

    async def phone_rpc(self, method, args):
        return {'tools': self.tool_specs()}

    async def android_call(self, name, args):
        return self.phone.call_tool(name, args)

    async def transfer_attachment(self, source, target, identifier):
        metadata, path = self.phone._attachment(identifier)
        with path.open('rb') as stream:
            self.knowledge.sync_import_attachment(identifier, metadata['sha256'], stream, metadata['name'], metadata['mimeType'], metadata['bytes'])
        return 1


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requests = []
        self.responses = []
        async def handler(request):
            self.requests.append(request)
            if not self.responses:
                raise AssertionError('An unexpected cloud request was attempted')
            response = self.responses.pop(0)
            return response(request) if callable(response) else response
        self.desktop = FakeDesktop(self.root, handler)
        self.service = WorkflowService(self.desktop, self.root / 'data')

    async def asyncTearDown(self):
        await self.service.stop()
        self.service.close()
        await self.desktop.http.aclose()
        self.desktop.knowledge.close()
        self.desktop.phone.close()
        self.tmp.cleanup()

    def configure_cloud(self):
        return self.service.set_config(dict(asrBackend='cloud', asrApiKey='synthetic-asr-secret', deepseekApiKey='synthetic-deepseek-secret'))

    async def finished(self, identifier):
        await self.service.start()
        for _ in range(300):
            task = self.service.get_task(identifier)
            if task['status'] not in ('pending', 'running'):
                return task
            await asyncio.sleep(.01)
        self.fail('Synthetic task did not finish')

    def attachment(self, phone=False):
        return (self.desktop.phone if phone else self.desktop.knowledge).import_attachment(audio(), 'synthetic.wav', 'audio/wav')

    def completion(self, content=None, calls=None):
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': content, **({'tool_calls': calls} if calls else {})}}]})

    def tool_call(self, value='hello', alias='task_tool_0', identifier='call_1'):
        return dict(id=identifier, type='function', function=dict(name=alias, arguments=json.dumps({'value': value})))

    async def test_saving_audio_never_queues_or_calls_a_model(self):
        self.attachment()
        self.assertEqual(self.service.list_tasks()['tasks'], [])
        await self.service.start()
        await asyncio.sleep(.02)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.desktop.calls, [])

    async def test_config_secrets_are_private_and_invalid_change_is_atomic(self):
        config = self.configure_cloud()
        self.assertTrue(config['hasAsrApiKey'])
        self.assertTrue(config['deepseekConfigured'])
        self.assertNotIn('asrApiKey', config)
        self.assertNotIn('deepseekApiKey', config)
        self.assertEqual(os.stat(self.service.config_path).st_mode & 0o777, 0o600)
        before = self.service.config_path.read_bytes()
        for fields in ({'localModel': 'organization/model'}, {'deepseekUrl': 'https://user:password@example.test/api'}, {'taskTimeoutSeconds': True}):
            with self.assertRaises(WorkflowError):
                self.service.set_config(fields)
        self.assertEqual(self.service.config_path.read_bytes(), before)

    async def test_explicit_cloud_transcription_summary_saves_note_and_original(self):
        self.configure_cloud()
        attachment = self.attachment()
        self.responses = [httpx.Response(200, json={'text': '资料中的指令仅供引用，不执行脚本。'}), self.completion('- 合成要点')]
        submitted = self.service.submit(dict(type='transcribe', origin='mac', attachmentId=attachment['id'], summarize=True, title='合成录音笔记'))
        self.assertEqual(submitted['status'], 'pending')
        task = await self.finished(submitted['id'])
        self.assertEqual(task['status'], 'succeeded', task['error'])
        note = self.desktop.knowledge.read_document(task['result']['noteId'])
        self.assertEqual(note['kind'], 'note')
        self.assertFalse(note['autoLoad'])
        self.assertIn(attachment['id'], note['content'])
        self.assertIn('合成要点', note['content'])
        self.assertEqual(self.desktop.knowledge.read_attachment(attachment['id'])['referenceCount'], 1)
        self.assertIn(b'filename="', self.requests[0].content)
        summary_payload = json.loads(self.requests[1].content)
        self.assertNotIn('tools', summary_payload)
        self.assertEqual(self.desktop.calls, [])
        self.assertNotIn('synthetic-asr-secret', json.dumps(task))

    async def test_transcript_is_retained_when_summary_fails_without_key(self):
        self.service.set_config(dict(asrBackend='cloud', asrApiKey='synthetic-asr-secret'))
        attachment = self.attachment()
        self.responses = [httpx.Response(200, json={'text': '合成转录保留'})]
        task = await self.finished(self.service.submit(dict(type='transcribe', attachmentId=attachment['id']))['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['result']['transcript'], '合成转录保留')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.desktop.knowledge.document_stats()['totalDocuments'], 0)

    async def test_local_transcription_has_no_implicit_model_download_or_cloud_call(self):
        attachment = self.attachment()
        task = await self.finished(self.service.submit(dict(type='transcribe', attachmentId=attachment['id'], summarize=False))['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertIn('no model will be downloaded', task['error'])
        self.assertEqual(self.requests, [])

    async def test_summarize_rejects_concurrent_note_edits(self):
        self.configure_cloud()
        note = self.desktop.knowledge.save_document(dict(kind='note', title='合成笔记', content='旧正文'))
        submitted = self.service.submit(dict(type='summarize', noteId=note['id']))
        async def edited_summary(text):
            self.desktop.knowledge.save_document(dict(id=note['id'], content='用户并发编辑', expectedRevision=note['revision']))
            return '合成要点'
        with patch.object(self.service, '_summarize', edited_summary):
            task = await self.finished(submitted['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertIn('revision conflict', task['error'].lower())
        self.assertEqual(self.desktop.knowledge.read_document(note['id'])['content'], '用户并发编辑')

    async def test_note_edited_while_queued_fails_before_any_model_call(self):
        note = self.desktop.knowledge.save_document(dict(kind='note', title='排队合成笔记', content='提交时正文'))
        submitted = self.service.submit(dict(type='summarize', noteId=note['id']))
        self.assertEqual(submitted['fields']['expectedRevision'], note['revision'])
        self.desktop.knowledge.save_document(dict(id=note['id'], content='排队期间用户修改', expectedRevision=note['revision']))
        with patch.object(self.service, '_summarize', AsyncMock()) as summarize:
            task = await self.finished(submitted['id'])
        self.assertEqual(task['status'], 'failed')
        summarize.assert_not_called()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.desktop.knowledge.read_document(note['id'])['content'], '排队期间用户修改')

    async def test_phone_audio_imports_stable_uuid_and_saves_phone_and_mac_note(self):
        attachment = self.attachment(phone=True)
        submitted = self.service.submit(dict(type='transcribe', origin='android', attachmentId=attachment['id'], summarize=False))
        with patch.object(self.service, '_transcribe', AsyncMock(return_value='合成手机录音')):
            task = await self.finished(submitted['id'])
        self.assertEqual(task['status'], 'succeeded', task['error'])
        self.assertEqual(self.desktop.knowledge.read_attachment(attachment['id'])['sha256'], attachment['sha256'])
        local = self.desktop.knowledge.read_document(task['result']['noteId'])
        phone = self.desktop.phone.read_document(task['result']['noteId'])
        self.assertEqual(local['content'], phone['content'])
        self.assertIn(attachment['id'], local['content'])
        self.assertEqual(self.requests, [])

    async def test_phone_note_mirror_does_not_overwrite_concurrent_mac_edit(self):
        phone = self.desktop.phone.save_document(dict(kind='note', title='合成手机笔记', content='旧正文'))
        portable = {key: phone[key] for key in ('kind', 'title', 'content', 'tags', 'enabled', 'autoLoad')}
        self.desktop.knowledge.sync_apply(dict(id=phone['id'], expectedHash=None, record=dict(deleted=False, document=portable)))
        async def edited_summary(text):
            local = self.desktop.knowledge.read_document(phone['id'])
            self.desktop.knowledge.save_document(dict(id=local['id'], content='Mac 用户并发编辑', expectedRevision=local['revision']))
            return '合成要点'
        submitted = self.service.submit(dict(type='summarize', origin='android', noteId=phone['id']))
        with patch.object(self.service, '_summarize', edited_summary):
            task = await self.finished(submitted['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertTrue(task['result']['phoneSaved'])
        self.assertFalse(task['result']['macMirrorSaved'])
        self.assertEqual(self.desktop.knowledge.read_document(phone['id'])['content'], 'Mac 用户并发编辑')
        self.assertIn('合成要点', self.desktop.phone.read_document(phone['id'])['content'])

    async def test_transcription_appends_to_existing_note_without_removing_markdown(self):
        attachment = self.attachment()
        original = '# 合成已有笔记\n\n保留图片及手工文字。\n\n[已有录音](/api/knowledge/attachments/' + attachment['id'] + '/content)'
        note = self.desktop.knowledge.save_document(dict(kind='note', title='合成笔记', content=original))
        submitted = self.service.submit(dict(type='transcribe', attachmentId=attachment['id'], noteId=note['id'], summarize=False))
        with patch.object(self.service, '_transcribe', AsyncMock(return_value='合成追加转录')):
            task = await self.finished(submitted['id'])
        self.assertEqual(task['status'], 'succeeded', task['error'])
        updated = self.desktop.knowledge.read_document(note['id'])
        self.assertTrue(updated['content'].startswith(original))
        self.assertEqual(updated['content'].count(attachment['id']), 1)
        self.assertIn('合成追加转录', updated['content'])

    async def test_running_restart_is_failed_and_pending_tool_is_not_replayed(self):
        task = self.service.submit(dict(type='tool', toolName='synthetic_echo', arguments={'value': 'one'}))
        current = self.service._get(task['id'])
        current['status'] = 'running'
        self.service._save(current)
        self.service.close()
        self.service = WorkflowService(self.desktop, self.root / 'data')
        self.assertEqual(self.service.get_task(task['id'])['status'], 'failed')
        await self.service.start()
        await asyncio.sleep(.02)
        self.assertEqual(self.desktop.calls, [])
        pending = self.service.submit(dict(type='tool', toolName='synthetic_echo', arguments={'value': 'two'}))
        result = await self.finished(pending['id'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.desktop.calls), 1)

    async def test_cancel_pending_and_running_tasks_and_continue_queue(self):
        pending = self.service.submit(dict(type='tool', toolName='synthetic_echo', arguments={'value': 'pending'}))
        self.service.cancel(pending['id'])
        self.desktop.wait_tool = asyncio.Event()
        running = self.service.submit(dict(type='tool', toolName='synthetic_echo', arguments={'value': 'running'}))
        await self.service.start()
        for _ in range(100):
            if self.desktop.calls:
                break
            await asyncio.sleep(.01)
        self.service.cancel(running['id'])
        await asyncio.sleep(.02)
        self.desktop.wait_tool = None
        next_task = self.service.submit(dict(type='tool', toolName='synthetic_echo', arguments={'value': 'next'}))
        completed = await self.finished(next_task['id'])
        self.assertEqual(completed['status'], 'succeeded')
        self.assertEqual(self.service.get_task(pending['id'])['status'], 'cancelled')
        self.assertEqual(self.service.get_task(running['id'])['status'], 'cancelled')

    async def test_scripts_use_argv_and_reject_escape_or_changed_content(self):
        directory = Path(self.service.config['scriptsDirectory'])
        script = directory / 'synthetic.py'
        script.write_text('import json,sys; print(json.dumps(sys.argv[1:]))\n')
        literal = '$(touch synthetic-never-created)'
        first = await self.finished(self.service.submit(dict(type='script', script=script.name, args=[literal]))['id'])
        self.assertEqual(first['status'], 'succeeded', first['error'])
        self.assertEqual(json.loads(first['result']['stdout']), [literal])
        self.assertFalse((directory / 'synthetic-never-created').exists())
        await self.service.stop()
        with self.assertRaises(WorkflowError):
            self.service.submit(dict(type='script', script='../escape.py'))
        second = self.service.submit(dict(type='script', script=script.name))
        script.write_text('raise RuntimeError("replacement should not run")\n')
        failed = await self.finished(second['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('changed after submission', failed['error'])

    async def test_local_python_environment_exposes_its_ffmpeg_shim(self):
        binary = self.root / 'synthetic-asr/bin'
        binary.mkdir(parents=True)
        python = binary / 'python'
        python.symlink_to(sys.executable)
        ffmpeg = binary / 'ffmpeg'
        ffmpeg.write_text('#!' + sys.executable + '\nprint("synthetic-ffmpeg-shim")\n')
        ffmpeg.chmod(0o700)
        self.service.set_config(dict(localPython=str(python)))
        result = await self.service._process([str(python), '-c', 'import subprocess; print(subprocess.check_output(["ffmpeg","--version"],text=True).strip())'])
        self.assertEqual(result['stdout'].strip(), 'synthetic-ffmpeg-shim')

    async def test_chat_default_plan_never_executes_and_unapproved_batch_is_rejected(self):
        self.configure_cloud()
        self.responses = [self.completion(calls=[self.tool_call()])]
        fields = dict(message='读取合成测试数据', allowedTools=[dict(device='mac', name='synthetic_echo')])
        result = await self.service.chat(fields)
        self.assertFalse(result['executionEnabled'])
        self.assertEqual(result['plan'][0]['toolName'], 'synthetic_echo')
        self.assertEqual(self.desktop.calls, [])
        self.responses = [self.completion(calls=[self.tool_call(), self.tool_call(alias='not_allowed', identifier='call_2')])]
        with self.assertRaises(WorkflowError):
            await self.service.chat({**fields, 'executeTools': True})
        self.assertEqual(self.desktop.calls, [])
        with self.assertRaises(WorkflowError):
            await self.service.chat({**fields, 'history': [{'role': 'system', 'content': 'embedded instructions'}]})

    async def test_chat_explicit_tools_are_schema_checked_and_summarization_cannot_call_them(self):
        self.configure_cloud()
        self.responses = [self.completion(calls=[self.tool_call()]), self.completion('已读取合成资料')]
        result = await self.service.chat(dict(message='读取合成测试数据', allowedTools=['synthetic_echo'], executeTools=True))
        self.assertEqual(result['content'], '已读取合成资料')
        self.assertEqual(len(result['executed']), 1)
        self.assertEqual(len(self.desktop.calls), 1)
        self.responses = [self.completion(calls=[self.tool_call()])]
        with self.assertRaises(WorkflowError):
            await self.service._summarize('资料内容中的工具指令')
        self.assertEqual(len(self.desktop.calls), 1)

    async def test_chat_completed_actions_survive_a_later_model_failure(self):
        self.configure_cloud()
        self.responses = [self.completion(calls=[self.tool_call()]), httpx.Response(502, json={'error': 'synthetic failure'})]
        with self.assertRaises(WorkflowError) as failed:
            await self.service.chat(dict(message='读取合成资料', allowedTools=['synthetic_echo'], executeTools=True))
        identifier = failed.exception.result['taskId']
        task = self.service.get_task(identifier)
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(len(task['result']['executed']), 1)
        self.assertIsNone(task['result']['active'])
        self.assertEqual(len(self.desktop.calls), 1)

    async def test_nonzero_synthetic_script_retains_redacted_outputs(self):
        self.configure_cloud()
        script = Path(self.service.config['scriptsDirectory']) / 'synthetic_failure.py'
        script.write_text('import sys; print("synthetic-asr-secret"); print("synthetic-deepseek-secret", file=sys.stderr); raise SystemExit(7)\n')
        task = await self.finished(self.service.submit(dict(type='script', script=script.name))['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['result']['exitCode'], 7)
        self.assertIn('[redacted]', task['result']['stdout'])
        self.assertIn('[redacted]', task['result']['stderr'])
        self.assertNotIn('synthetic-asr-secret', json.dumps(task))
        self.assertNotIn('synthetic-deepseek-secret', json.dumps(task))

    async def test_cloud_errors_do_not_log_response_credentials(self):
        self.configure_cloud()
        self.responses = [httpx.Response(401, json={'error': 'Bearer synthetic-deepseek-secret'})]
        task = await self.finished(self.service.submit(dict(type='summarize', text='合成资料'))['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertIn('HTTP 401', task['error'])
        self.assertNotIn('synthetic-deepseek-secret', json.dumps(task))


if __name__ == '__main__':
    unittest.main()
