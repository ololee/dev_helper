import asyncio
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from ai_support import ASSIST_ACTIONS, SCRIPT_TOOL, executable, image_block
from workflows import WorkflowError
import test_workflows as fixtures


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class AiWorkflowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.WorkflowTests.asyncSetUp
    asyncTearDown = fixtures.WorkflowTests.asyncTearDown
    configure_cloud = fixtures.WorkflowTests.configure_cloud
    finished = fixtures.WorkflowTests.finished
    completion = fixtures.WorkflowTests.completion
    tool_call = fixtures.WorkflowTests.tool_call

    async def test_capabilities_are_live_redacted_and_keep_useful_tools(self):
        self.configure_cloud()
        real = self.desktop.tool_specs()
        self.desktop.tool_specs = lambda: real + self.desktop.knowledge.tool_specs() + self.service.tool_specs() + [
            dict(name='devhelper_relay_config', inputSchema={'type': 'object'}),
            dict(name='devhelper_call_tool', inputSchema={'type': 'object'})]
        self.desktop.phone_rpc = AsyncMock(side_effect=RuntimeError('offline synthetic-secret'))
        capabilities = await self.service.ai_capabilities()
        names = {tool['name'] for tool in capabilities['tools']}
        self.assertTrue(capabilities['configured'])
        self.assertTrue(capabilities['features']['images'])
        self.assertIn('knowledge_save_document', names)
        self.assertIn('knowledge_save_schedule', names)
        self.assertIn(SCRIPT_TOOL, names)
        self.assertNotIn('devhelper_call_tool', names)
        self.assertNotIn('devhelper_workflow_submit', names)
        self.assertNotIn('devhelper_relay_config', names)
        self.assertFalse(capabilities['devices']['android']['available'])
        self.assertNotIn('synthetic-secret', json.dumps(capabilities))
        self.assertNotIn('synthetic-deepseek-secret', json.dumps(capabilities))
        self.assertEqual(self.requests, [])
        self.service.set_config({'deepseekModel': 'deepseek-v4-pro'})
        self.assertFalse((await self.service.ai_capabilities())['features']['images'])

    async def test_ai_limits_validate_atomically(self):
        self.configure_cloud()
        before = self.service.config_path.read_bytes()
        for fields in ({'deepseekThinking': 'auto'}, {'deepseekMaxTokens': 511}, {'deepseekMaxTokens': True},
                       {'deepseekMaxToolRounds': 13}, {'deepseekMaxToolCalls': 33}):
            with self.assertRaises(WorkflowError):
                self.service.set_config(fields)
        self.assertEqual(before, self.service.config_path.read_bytes())
        configured = self.service.set_config({'deepseekThinking': 'enabled', 'deepseekMaxTokens': 16384,
            'deepseekMaxToolRounds': 12, 'deepseekMaxToolCalls': 32})
        self.assertEqual(configured['deepseekMaxToolCalls'], 32)

    async def test_connection_is_explicit_small_and_reports_usage(self):
        with self.assertRaisesRegex(WorkflowError, 'API Key') as missing:
            await self.service.test_connection()
        self.assertEqual(missing.exception.status, 400)
        self.assertEqual(self.requests, [])
        self.configure_cloud()
        self.service.set_config({'deepseekThinking': 'enabled'})
        self.responses = [httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5}})]
        result = await self.service.test_connection()
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload['max_tokens'], 64)
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertNotIn('tools', payload)
        self.assertEqual(result['usage']['total_tokens'], 5)
        self.assertEqual(self.service.list_tasks()['tasks'], [])

    async def test_rag_uses_only_selected_device_and_enabled_sources(self):
        self.configure_cloud()
        self.desktop.knowledge.save_document(dict(kind='memory', title='Mac only', content='private Mac phrase', autoLoad=True))
        memory = self.desktop.phone.save_document(dict(kind='memory', title='Phone memory', content='phone golden context', autoLoad=True))
        note = self.desktop.phone.save_document(dict(kind='note', title='Golden', content='golden selected phone note'))
        self.desktop.phone.save_document(dict(kind='memory', title='Hidden', content='golden disabled phrase', enabled=False, autoLoad=True))
        self.responses = [self.completion('phone result')]
        result = await self.service.chat(dict(message='golden', contextDevice='android', documentIds=[note['id']]))
        payload = json.loads(self.requests[0].content)
        text = payload['messages'][-1]['content']
        self.assertIn('phone golden context', text)
        self.assertIn('golden selected phone note', text)
        self.assertNotIn('private Mac phrase', text)
        self.assertNotIn('golden disabled phrase', text)
        self.assertEqual({item['id'] for item in result['context']['sources']}, {memory['id'], note['id']})
        self.assertEqual(result['context']['device'], 'android')

    async def test_knowledge_toggle_still_includes_explicit_selection(self):
        self.configure_cloud()
        self.desktop.knowledge.save_document(dict(kind='memory', title='Automatic', content='automatic secret', autoLoad=True))
        note = self.desktop.knowledge.save_document(dict(kind='note', title='Selected', content='selected content'))
        self.responses = [self.completion('draft')]
        result = await self.service.chat(dict(message='explain', useKnowledge=False, documentIds=[note['id']]))
        text = json.loads(self.requests[0].content)['messages'][-1]['content']
        self.assertIn('selected content', text)
        self.assertNotIn('automatic secret', text)
        self.assertFalse(result['context']['knowledgeEnabled'])

    async def test_selected_phone_image_uses_verified_bytes_without_persisting_base64(self):
        self.configure_cloud()
        image = self.desktop.phone.import_attachment(PNG, 'selected.png', 'image/png')
        self.desktop.phone.import_attachment(PNG, 'unselected.png', 'image/png')
        self.responses = [self.completion('one pixel')]
        result = await self.service.chat(dict(message='describe', contextDevice='android', useKnowledge=False, imageIds=[image['id']]))
        blocks = json.loads(self.requests[0].content)['messages'][-1]['content']
        images = [item for item in blocks if item['type'] == 'image_url']
        self.assertEqual(len(images), 1)
        self.assertEqual(base64.b64decode(images[0]['image_url']['url'].split(',')[1]), PNG)
        self.assertEqual(len(self.desktop.knowledge.list_attachments()['attachments']), 1)
        persisted = self.service.get_task(result['taskId'])
        self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(persisted))
        self.assertEqual(persisted['result']['context']['media'][0]['id'], image['id'])

    async def test_gif_image_input_keeps_actual_supported_bytes(self):
        gif = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        block = image_block(gif)
        self.assertTrue(block['image_url']['url'].startswith('data:image/gif;base64,'))
        self.assertEqual(base64.b64decode(block['image_url']['url'].split(',')[1]), gif)

    async def test_wrong_media_or_changed_hash_never_reaches_cloud(self):
        self.configure_cloud()
        image = self.desktop.knowledge.import_attachment(PNG, 'selected.png', 'image/png')
        _, path = self.desktop.knowledge._attachment(image['id'])
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        with self.assertRaisesRegex(WorkflowError, 'changed'):
            await self.service.chat(dict(message='describe', useKnowledge=False, imageIds=[image['id']]))
        self.assertEqual(self.requests, [])
        self.service.set_config({'deepseekModel': 'deepseek-v4-pro'})
        with self.assertRaisesRegex(WorkflowError, 'deepseek-flash'):
            await self.service.chat(dict(message='describe', imageIds=[image['id']]))
        self.assertEqual(self.requests, [])

    async def test_assist_actions_return_drafts_without_saving(self):
        self.configure_cloud()
        note = self.desktop.knowledge.save_document(dict(kind='note', title='Original', content='original user text'))
        for action in ASSIST_ACTIONS:
            with self.subTest(action=action):
                self.responses = [self.completion('editable draft')]
                created = self.service.submit(dict(type='assist', action=action, noteId=note['id'], useKnowledge=False))
                finished = await self.finished(created['id'])
                self.assertEqual(finished['status'], 'succeeded', finished['error'])
                self.assertEqual(finished['type'], 'assist')
                self.assertTrue(finished['result']['draftOnly'])
                self.assertEqual(finished['result']['action'], action)
        self.assertEqual(self.desktop.knowledge.read_document(note['id'])['content'], 'original user text')
        self.assertEqual(self.desktop.knowledge.document_stats()['totalDocuments'], 1)
        self.assertEqual(self.desktop.calls, [])

    async def test_thinking_and_usage_survive_same_turn_tool_loop(self):
        self.configure_cloud()
        self.service.set_config({'deepseekThinking': 'enabled', 'deepseekMaxTokens': 800, 'deepseekMaxToolRounds': 1})
        self.responses = [httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': None,
            'reasoning_content': 'synthetic reasoning', 'tool_calls': [self.tool_call()]}}], 'usage': {'total_tokens': 7}}), self.completion('done')]
        result = await self.service.chat(dict(message='read', useKnowledge=False, allowedTools=['synthetic_echo'], executeTools=True))
        payload = json.loads(self.requests[1].content)
        self.assertEqual(payload['thinking'], {'type': 'enabled'})
        self.assertEqual(payload['max_tokens'], 800)
        assistant = next(item for item in payload['messages'] if item['role'] == 'assistant')
        self.assertEqual(assistant['reasoning_content'], 'synthetic reasoning')
        self.assertNotIn('_usage', assistant)
        self.assertEqual(result['usage'][0]['total_tokens'], 7)

    async def test_tool_failures_preserve_previous_results_and_error_detail(self):
        self.configure_cloud()
        calls = [self.tool_call(value='first', identifier='one'), self.tool_call(value='second', identifier='two')]
        self.responses = [self.completion(calls=calls)]
        self.desktop.dispatch = AsyncMock(side_effect=[{'value': 'completed'}, {'isError': True, 'content': [{'type': 'text', 'text': 'specific synthetic failure'}]}])
        with self.assertRaises(WorkflowError) as failure:
            await self.service.chat(dict(message='read', useKnowledge=False, allowedTools=['synthetic_echo'], executeTools=True))
        result = self.service.get_task(failure.exception.result['taskId'])['result']
        self.assertEqual([item['status'] for item in result['executed']], ['succeeded', 'failed'])
        self.assertIn('specific synthetic failure', json.dumps(result['executed'][1]['result']))
        self.assertIsNone(result['active'])
        self.assertEqual(len(self.requests), 1)

    async def test_tool_screenshot_is_real_user_image_and_not_in_task_data(self):
        self.configure_cloud()
        self.responses = [self.completion(calls=[self.tool_call()]), self.completion('screenshot observed')]
        self.desktop.dispatch = AsyncMock(return_value={'structuredContent': {'artifactId': 'synthetic'}, 'content': [
            {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode()}]})
        result = await self.service.chat(dict(message='inspect', useKnowledge=False, allowedTools=['synthetic_echo'], executeTools=True))
        messages = json.loads(self.requests[1].content)['messages']
        self.assertEqual(messages[-1]['role'], 'user')
        self.assertEqual(messages[-1]['content'][-1]['type'], 'image_url')
        self.assertEqual(messages[-2]['role'], 'tool')
        self.assertNotIn(base64.b64encode(PNG).decode(), messages[-2]['content'])
        self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(self.service.get_task(result['taskId'])))

    async def test_script_helper_prevalidates_entire_batch_and_uses_argv(self):
        self.configure_cloud()
        directory = Path(self.service.config['scriptsDirectory'])
        (directory / 'synthetic.py').write_text('import json,sys; print(json.dumps(sys.argv[1:]))\n')
        script_call = dict(id='script', type='function', function=dict(name='task_tool_1', arguments=json.dumps({'script': '../escape.py'})))
        self.responses = [self.completion(calls=[self.tool_call(), script_call])]
        fields = dict(message='run', useKnowledge=False, executeTools=True, allowedTools=['synthetic_echo', SCRIPT_TOOL])
        with self.assertRaises(WorkflowError):
            await self.service.chat(fields)
        self.assertEqual(self.desktop.calls, [])
        literal = '$(touch never-created)'
        script_call['function']['name'] = 'task_tool_0'
        script_call['function']['arguments'] = json.dumps({'script': 'synthetic.py', 'args': [literal]})
        self.responses = [self.completion(calls=[script_call]), self.completion('script done')]
        result = await self.service.chat({**fields, 'allowedTools': [SCRIPT_TOOL]})
        self.assertEqual(json.loads(result['executed'][0]['result']['stdout']), [literal])
        self.assertFalse((directory / 'never-created').exists())

    async def test_schedule_cannot_execute_an_unapproved_nested_tool(self):
        self.configure_cloud()
        original_catalog = self.desktop.tool_specs()
        schedule_spec = next(item for item in self.desktop.knowledge.tool_specs() if item['name'] == 'knowledge_save_schedule')
        self.desktop.tool_specs = lambda: original_catalog + [schedule_spec]
        schedule_call = dict(id='schedule', type='function', function=dict(name='task_tool_1', arguments=json.dumps({
            'title': 'synthetic', 'toolName': 'devhelper_workflow_submit', 'arguments': {}, 'runAt': '2099-01-01T00:00:00+00:00'})))
        self.responses = [self.completion(calls=[self.tool_call(), schedule_call])]
        with self.assertRaises(WorkflowError):
            await self.service.chat(dict(message='schedule', useKnowledge=False, executeTools=True,
                allowedTools=['synthetic_echo', 'knowledge_save_schedule']))
        self.assertEqual(self.desktop.calls, [])
        self.assertEqual(self.desktop.knowledge.list_schedules()['total'], 0)

    async def test_real_synthetic_video_samples_screenshots_and_removes_temporary_frames(self):
        ffmpeg = executable('ffmpeg', self.service.config)
        if not ffmpeg:
            self.skipTest('FFmpeg is not installed')
        video = self.root / 'synthetic.mp4'
        await self.service._process([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=32x32:d=1', '-pix_fmt', 'yuv420p', str(video)])
        frames, times, duration = await self.service._video_frames(video)
        self.assertEqual(len(frames), 6)
        self.assertEqual(len(times), 6)
        self.assertTrue(all(frame.startswith(b'\xff\xd8\xff') for frame in frames))
        self.assertGreater(duration, 0)
        self.assertEqual(list(self.service.artifacts.glob('ai-video-*')), [])

    async def test_selected_video_uploads_frames_with_honest_analysis_metadata(self):
        self.configure_cloud()
        # A synthetic MP4 container fixture: decoding is stubbed, cloud remains mocked.
        header = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom'
        video = self.desktop.knowledge.import_attachment(header, 'selected.mp4', 'video/mp4')
        self.responses = [self.completion('sampled blue scene')]
        with patch.object(self.service, '_video_frames', AsyncMock(return_value=([PNG] * 6, [0, 1, 2, 3, 4, 5], 6))):
            result = await self.service.chat(dict(message='describe', useKnowledge=False, videoIds=[video['id']]))
        blocks = json.loads(self.requests[0].content)['messages'][-1]['content']
        self.assertEqual(sum(item['type'] == 'image_url' for item in blocks), 6)
        media = result['context']['media'][0]
        self.assertEqual(media['analysis'], 'sampled_screenshots')
        self.assertFalse(media['audioIncluded'])
        self.assertEqual(media['timestamps'], [0, 1, 2, 3, 4, 5])
        self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(self.service.get_task(result['taskId'])))

    async def test_cancelled_direct_chat_retains_completed_result_and_stops_next_call(self):
        self.configure_cloud()
        self.responses = [self.completion(calls=[self.tool_call()])]
        async def cancelled_dispatch(device, name, args):
            running = next(item for item in self.service.list_tasks()['tasks'] if item['status'] == 'running')
            self.service.cancel(running['id'])
            return {'value': 'external action completed'}
        self.desktop.dispatch = cancelled_dispatch
        with self.assertRaises(asyncio.CancelledError):
            await self.service.chat(dict(message='read', useKnowledge=False, allowedTools=['synthetic_echo'], executeTools=True))
        task = self.service.list_tasks()['tasks'][0]
        self.assertEqual(task['status'], 'cancelled')
        self.assertEqual(task['result']['executed'][0]['status'], 'succeeded')
        self.assertEqual(len(self.requests), 1)


if __name__ == '__main__':
    unittest.main()
