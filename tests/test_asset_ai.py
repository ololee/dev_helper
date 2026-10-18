"""Generated assets and protocol fixtures only; no user key or real cloud calls."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

try:
    from desktop.asset_ai import AssetAIService, AssetAIError, INPUT_VERSION, _space
    from desktop.asset_library import AssetLibrary
    from desktop.catalog_snapshot import CatalogSnapshot
except ImportError:
    from asset_ai import AssetAIService, AssetAIError, INPUT_VERSION, _space
    from asset_library import AssetLibrary
    from catalog_snapshot import CatalogSnapshot


class Protocol:
    def __init__(self):
        self.calls = []
        self.failures = []
        self.bad_index = False
        self.mismatch_sha = False
        self.missing = False
        self.inject_id = False
        self.bad_annotation = False
        self.mutate = None
        self.started = threading.Event()
        self.release = None
        self.uploaded = []
        self.deleted = []
        self.chat_inputs = []
        self.embedding_inputs = []
        self.vision_inputs = []
        self.echo_key = False

    def __call__(self, request):
        body = request.read()
        self.calls.append((request.method, str(request.url), dict(request.headers), body))
        self.started.set()
        if self.release:
            self.release.wait(5)
        if self.failures:
            status = self.failures.pop(0)
            return httpx.Response(status, headers={'Retry-After': '0'}, json={'error': 'echo private-fixture-key /private/user/project'})
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': 'fixture-real-model', 'owned_by': 'fixture-provider', 'extra_secret': 'never-return'}, {'id': 'fixture-real-model'}, {'id': ''}]})
        if request.url.path.endswith('/files') and request.method == 'POST':
            self.uploaded.append(body)
            return httpx.Response(200, json={'id': 'file-fixture-' + str(len(self.uploaded))})
        if '/files/file-fixture-' in request.url.path and request.method == 'DELETE':
            self.deleted.append(request.url.path.rsplit('/', 1)[-1])
            return httpx.Response(200, json={'deleted': True})
        data = json.loads(body)
        if request.url.path.endswith('/embeddings'):
            self.embedding_inputs.extend(data['input'])
            vectors = []
            for index, text in enumerate(data['input']):
                values = [1., 0., 0.] if 'gui' in text.lower() or 'icon' in text.lower() or 'button' in text.lower() else [0., 1., 0.]
                vectors.append({'index': 0 if self.bad_index else index, 'embedding': values})
            if self.mutate:
                mutation, self.mutate = self.mutate, None
                mutation()
            return httpx.Response(200, json={'data': list(reversed(vectors)), 'model': data['model']})
        if request.url.path.endswith('/responses'):
            self.vision_inputs.append(data)
            values = [json.loads(part['text']) for part in data['input'][0]['content'] if part['type'] == 'input_text']
        else:
            values = json.loads(data['messages'][1]['content'])['items']
            self.chat_inputs.extend(values)
        rows = []
        for value in values:
            rows.append({'assetId': value['assetId'], 'sha256': '0' * 64 if self.mismatch_sha else value['sha256'], 'label': '按钮图标', 'categories': ['gui'],
                         'purpose': '用于用户界面；基于已提供证据推断', 'evidence': ['private-classify-key' if self.echo_key else '文件在 ui 目录'], 'confidence': 2.0 if self.bad_annotation else 0.6,
                         '_imagePixelsProvided': True})
        if self.inject_id:
            rows[0]['assetId'] = 'not-a-requested-resource'
        if self.missing:
            rows = rows[:1]
        if self.mutate:
            mutation, self.mutate = self.mutate, None
            mutation()
        content = json.dumps({'items': list(reversed(rows))}, ensure_ascii=False)
        if request.url.path.endswith('/responses'):
            return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': content}]}]})
        return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})


class AssetAITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'source'; self.source.mkdir()
        (self.source / 'ui').mkdir()
        Image.new('RGBA', (40, 20), (200, 20, 0, 100)).save(self.source / 'ui' / 'icon.png')
        (self.source / 'scene.obj').write_text('v 0 0 0\nv 1 1 1\nf 1 2 3\n')
        self.library = AssetLibrary(self.base / 'catalog')
        self.project = self.library.register_project(self.source, 'Generated game')
        self.scan()
        self.protocol = Protocol()
        self.ai = AssetAIService(self.library, self.base / 'private', transport=httpx.MockTransport(self.protocol), retry_delay=0)
        self.configure()

    def tearDown(self):
        if self.protocol.release:
            self.protocol.release.set()
        self.ai.close(); self.library.close(); self.tmp.cleanup()

    def configure(self):
        return self.ai.configure({'embedding': {'baseUrl': 'https://embed.fixture.test/v1', 'model': 'fixture-embedding-v1', 'apiKey': 'private-fixture-key', 'batchSize': 16},
                                  'classification': {'baseUrl': 'https://chat.fixture.test/v1', 'model': 'fixture-chat-v1', 'apiKey': 'private-classify-key', 'batchSize': 8}})

    def scan(self):
        job = self.library.start_scan(self.project['id'])
        for _ in range(1000):
            if self.library.job(job['jobId'])['state'] in ('completed', 'failed'):
                self.assertEqual(self.library.job(job['jobId'])['state'], 'completed')
                return
            time.sleep(.005)
        self.fail('fixture scan did not complete')

    def assets(self):
        return self.library.list_assets(self.project['id'], limit=500)['items']

    def wait(self, job):
        for _ in range(1000):
            result = self.ai.job(job['jobId'])
            if result['state'] in ('completed', 'completed-with-errors', 'failed', 'interrupted', 'cancelled'):
                return result
            time.sleep(.005)
        self.fail('model task did not finish')

    def test_config_private_redacted_blank_preserved_and_explicit_clear(self):
        status = self.ai.status()
        self.assertTrue(status['embedding']['configured'])
        self.assertNotIn('private-fixture-key', json.dumps(status))
        self.assertNotIn('apiKey', status['embedding'])
        self.assertEqual(self.ai.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ai.root.stat().st_mode & 0o777, 0o700)
        self.ai.configure({'embedding': {'apiKey': '   '}})
        self.assertTrue(self.ai.status()['embedding']['hasApiKey'])
        self.ai.configure({'embedding': {'clearKey': True}})
        self.assertFalse(self.ai.status()['embedding']['hasApiKey'])
        with self.assertRaises(AssetAIError):
            self.ai.start_job('embed', project_id=self.project['id'])

    def test_bad_url_redirect_and_cross_origin_key_reuse_are_rejected(self):
        for value in ['http://external.test/v1', 'https://user:key@api.test/v1', 'https://api.test/v1?key=secret', 'https://api.test/v1/embeddings']:
            with self.subTest(value=value), self.assertRaises(AssetAIError):
                self.ai.configure({'embedding': {'baseUrl': value, 'apiKey': 'new-fixture-key'}})
        with self.assertRaises(AssetAIError):
            self.ai.configure({'embedding': {'baseUrl': 'https://other.fixture.test'}})
        self.protocol.failures = [302]
        with self.assertRaises(AssetAIError):
            self.ai.embed_query('button')
        self.assertEqual(len(self.protocol.calls), 1)

    def test_deepseek_embedding_is_not_advertised_as_working(self):
        with self.assertRaises(AssetAIError):
            self.ai.configure({'embedding': {'baseUrl': 'https://api.deepseek.com', 'apiKey': 'generated-fixture'}})
        self.assertNotIn('deepseek', [v['id'] for v in self.ai.status()['presets'] if v['kind'] == 'embedding'])

    def test_batch_embedding_correct_indexes_no_binaries_or_base64(self):
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(result['progress']['succeeded'], 2)
        space = self.ai.status()['embedding']['space']
        items = self.assets()
        self.assertTrue(all(any(value['model'] == space for value in item['embeddings']) for item in items))
        body = json.loads(self.protocol.calls[0][3])
        self.assertEqual(body['encoding_format'], 'float')
        self.assertEqual(len(body['input']), 2)
        self.assertNotIn('base64', self.protocol.calls[0][3].decode())
        self.assertNotIn(str(self.source), self.protocol.calls[0][3].decode())
        self.assertTrue(all(not json.loads(value)['imagePixelsProvided'] for value in body['input']))
        self.assertNotIn('private-fixture-key', json.dumps(self.ai.jobs()))

    def test_both_classifies_before_embedding_and_incremental_skips(self):
        result = self.wait(self.ai.start_job('both', project_id=self.project['id']))
        self.assertEqual(result['progress']['succeeded'], 4, result)
        self.assertTrue(self.protocol.calls[0][1].endswith('/chat/completions'))
        self.assertTrue(any('按钮图标' in value for value in self.protocol.embedding_inputs))
        self.assertTrue(all('image pixels were not provided' in item['annotations'][0]['evidence'] for item in self.assets()))
        calls = len(self.protocol.calls)
        result = self.wait(self.ai.start_job('both', project_id=self.project['id']))
        self.assertEqual(result['progress']['skipped'], 4, result)
        self.assertEqual(len(self.protocol.calls), calls)
        self.ai.configure({'embedding': {'model': 'different-vector-space'}})
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['progress']['succeeded'], 2)
        self.assertTrue(all(len(item['embeddings']) == 2 for item in self.assets()))

    def test_embedding_query_profiles_survive_model_changes_and_deleted_job_history(self):
        first = self.ai.status()['embedding']
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['state'], 'completed', result)
        self.ai.configure({'embedding': {'model': 'fixture-embedding-v2', 'dimensions': 3}})
        second = self.ai.status()['embedding']
        self.assertNotEqual(first['space'], second['space'])
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['state'], 'completed', result)
        # Profiles remain useful even after settings point at a third provider,
        # and without the historical task records used by the migration fallback.
        self.ai.configure({'embedding': {'baseUrl': 'https://another.fixture.test/v1', 'model': 'fixture-embedding-v3', 'apiKey': 'next-private-fixture-key'}})
        with self.ai._db() as db:
            db.execute('DELETE FROM items')
            db.execute('DELETE FROM jobs')
        self.assertEqual(self.ai.jobs(), [])
        profiles = self.library.query_profiles()
        self.assertEqual({p['space'] for p in profiles}, {first['space'], second['space']})
        by_space = {p['space']: p for p in profiles}
        self.assertIsNone(by_space[first['space']]['dimensions'])
        self.assertEqual(by_space[second['space']]['dimensions'], 3)
        for original in (first, second):
            restored_section = by_space[original['space']]
            self.assertEqual(_space(restored_section, 'embedding'), original['space'])
        target = self.base / 'portable.sqlite3'
        calls = len(self.protocol.calls)
        exported = CatalogSnapshot(self.library).export(target)
        self.assertEqual(exported['counts']['embeddings'], 4)
        self.assertEqual(exported['queryProfiles'], profiles)
        self.assertEqual(len(self.protocol.calls), calls)
        for secret in ('private-fixture-key', 'next-private-fixture-key', 'private-classify-key'):
            self.assertNotIn(secret.encode(), target.read_bytes())
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM query_profiles').fetchone()[0], 2)
        recovered = AssetLibrary(self.base / 'recovered-catalog')
        try:
            summary = CatalogSnapshot(recovered).restore(target, expected_sha256=exported['sha256'])
            self.assertEqual(summary['vectorsSkipped'], 0)
            self.assertEqual(recovered.query_profiles(), profiles)
            self.assertEqual(recovered.query_embedding([1, 0, 0], first['space'])['totalCandidates'], 2)
        finally:
            recovered.close()

    def test_incremental_embedding_hit_backfills_profile_without_api_request(self):
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['state'], 'completed', result)
        with self.library._db() as db:
            db.execute('DELETE FROM query_profiles')
        calls = len(self.protocol.calls)
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['progress']['skipped'], 2, result)
        self.assertEqual(len(self.protocol.calls), calls)
        self.assertEqual(self.library.query_profiles()[0]['space'], self.ai.status()['embedding']['space'])

    def test_metadata_change_reembeds_but_original_unchanged_skips(self):
        self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        target = self.assets()[0]
        self.library.save_annotation(target['id'], {'source': 'manual', 'label': 'New description', 'categories': ['music']})
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['progress']['succeeded'], 1, result)
        self.assertEqual(result['progress']['skipped'], 1, result)

    def test_invalid_embedding_indexes_fail_without_saving(self):
        self.protocol.bad_index = True
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['state'], 'completed-with-errors', result)
        self.assertEqual(result['progress']['failed'], 2)
        self.assertTrue(all(not item['embeddings'] for item in self.assets()))

    def test_classification_id_or_sha_mismatch_never_saves_annotations(self):
        for flag in ('mismatch_sha', 'inject_id'):
            with self.subTest(flag=flag):
                setattr(self.protocol, flag, True)
                result = self.wait(self.ai.start_job('classify', project_id=self.project['id']))
                self.assertEqual(result['progress']['failed'], 2, result)
                self.assertTrue(all(not item['annotations'] for item in self.assets()))
                setattr(self.protocol, flag, False)

    def test_partial_classification_saves_valid_rows_and_retries_failed_only(self):
        self.protocol.missing = True
        result = self.wait(self.ai.start_job('classify', project_id=self.project['id']))
        self.assertEqual(result['progress']['succeeded'], 1, result)
        self.assertEqual(result['progress']['failed'], 1, result)
        self.protocol.missing = False
        retry = self.wait(self.ai.retry_job(result['jobId']))
        self.assertEqual(retry['assetCount'], 1)
        self.assertEqual(retry['progress']['succeeded'], 1)

    def test_invalid_annotation_confidence_does_not_replace_user_data(self):
        target = self.assets()[0]
        self.library.save_annotation(target['id'], {'source': 'manual', 'label': 'Keep my note'})
        self.protocol.bad_annotation = True
        result = self.wait(self.ai.start_job('classify', project_id=self.project['id']))
        self.assertEqual(result['progress']['failed'], 2)
        self.assertEqual(self.library.asset(target['id'])['annotations'][0]['label'], 'Keep my note')
        self.assertTrue(all(value['source'] != 'ai' for item in self.assets() for value in item['annotations']))

    def test_source_change_during_api_is_not_saved_to_new_sha(self):
        target = next(item for item in self.assets() if item['kind'] == 'model')
        def mutate():
            (self.source / 'scene.obj').write_text('v 8 8 8\n')
            self.scan()
        self.protocol.mutate = mutate
        result = self.wait(self.ai.start_job('embed', asset_ids=[target['id']]))
        self.assertEqual(result['progress']['failed'], 1, result)
        self.assertFalse(self.library.asset(target['id'])['embeddings'])

    def test_annotation_change_during_embedding_request_cannot_republish_stale_vector(self):
        target = self.assets()[0]
        def mutate():
            self.library.save_annotation(target['id'], {'source': 'manual', 'purpose': 'Changed while the API request was in flight'})
        self.protocol.mutate = mutate
        result = self.wait(self.ai.start_job('embed', asset_ids=[target['id']]))
        self.assertEqual(result['progress']['failed'], 1, result)
        self.assertFalse(self.library.asset(target['id'])['embeddings'])
        again = self.wait(self.ai.retry_job(result['jobId']))
        self.assertEqual(again['progress']['succeeded'], 1)
        self.assertIn('Changed while the API request was in flight', self.protocol.embedding_inputs[-1])

    def test_manual_annotation_change_during_classification_does_not_save_stale_classification(self):
        target = self.assets()[0]
        def mutate():
            self.library.save_annotation(target['id'], {'source': 'manual', 'purpose': 'The user corrected the purpose during classification'})
        self.protocol.mutate = mutate
        result = self.wait(self.ai.start_job('classify', asset_ids=[target['id']]))
        self.assertEqual(result['progress']['failed'], 1, result)
        self.assertTrue(all(value['source'] != 'ai' for value in self.library.asset(target['id'])['annotations']))

    def test_api_transient_retries_and_errors_do_not_echo_secrets(self):
        self.protocol.failures = [429, 503]
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(len(self.protocol.calls), 3)
        self.protocol.failures = [401]
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id'], force=True))
        self.assertEqual(result['progress']['failed'], 2)
        self.assertNotIn('private-fixture-key', json.dumps(result))
        self.assertNotIn('/private/user', json.dumps(result))
        self.assertEqual(len(self.protocol.calls), 4)

    def test_active_task_uses_old_config_snapshot(self):
        self.protocol.release = threading.Event()
        first = self.ai.start_job('embed', project_id=self.project['id'])
        self.assertTrue(self.protocol.started.wait(2))
        self.ai.configure({'embedding': {'model': 'next-model', 'apiKey': 'next-private-key'}})
        self.protocol.release.set()
        result = self.wait(first)
        self.assertEqual(result['state'], 'completed', result)
        body = json.loads(self.protocol.calls[0][3])
        self.assertEqual(body['model'], 'fixture-embedding-v1')
        self.assertEqual(self.protocol.calls[0][2]['authorization'], 'Bearer private-fixture-key')
        self.assertTrue(all(item['embeddings'][0]['model'] != self.ai.status()['embedding']['space'] for item in self.assets()))

    def test_cancelled_job_never_saves_pending_api_result(self):
        self.protocol.release = threading.Event()
        job = self.ai.start_job('embed', project_id=self.project['id'])
        self.assertTrue(self.protocol.started.wait(2))
        self.ai.cancel(job['jobId'])
        self.protocol.release.set()
        result = self.wait(job)
        self.assertEqual(result['state'], 'cancelled', result)
        self.assertEqual(result['progress']['cancelled'], 2)
        self.assertTrue(all(not item['embeddings'] for item in self.assets()))

    def test_restart_marks_running_job_interrupted_preserving_items(self):
        self.ai.close()
        identifier = 'generated-job-fixture'
        data = {'jobId': identifier, 'kind': 'embed', 'state': 'running'}
        item = self.assets()[0]
        with sqlite3.connect(self.ai.database) as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?)', (identifier, 'running', json.dumps(data), time.time()))
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?)', (identifier, item['id'], 'embed', item['sha256'], 'running', '{}'))
        self.ai = AssetAIService(self.library, self.base / 'private', transport=httpx.MockTransport(self.protocol), retry_delay=0)
        result = self.ai.job(identifier)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['progress']['interrupted'], 1)

    def test_connection_test_and_query_cache_use_generated_text_only(self):
        for kind in ('embedding', 'classification'):
            result = self.ai.test_connection(kind)
            self.assertTrue(result['ok'])
            self.assertFalse(result['imagePixelsSent'])
        self.assertEqual(len(self.ai.jobs()), 0)
        first = self.ai.embed_query('  button  ')
        second = self.ai.embed_query('button')
        self.assertFalse(first['cached']); self.assertTrue(second['cached'])
        self.assertEqual(first['vector'], second['vector'])
        self.assertTrue(all(not item['embeddings'] for item in self.assets()))

    def test_vision_uses_multipart_jpeg_file_id_and_deletes_only_owned_uploads(self):
        self.ai.configure({'classification': {'mode': 'responses-vision'}})
        result = self.wait(self.ai.start_job('both', project_id=self.project['id']))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(len(self.protocol.uploaded), 1)
        self.assertIn(b'Content-Type: image/jpeg', self.protocol.uploaded[0])
        self.assertIn(b'\xff\xd8', self.protocol.uploaded[0])
        self.assertIn(b'vision', self.protocol.uploaded[0])
        self.assertEqual(self.protocol.deleted, ['file-fixture-1'])
        content = self.protocol.vision_inputs[0]['input'][0]['content']
        image_parts = [v for v in content if v['type'] == 'input_image']
        self.assertEqual(image_parts, [{'type': 'input_image', 'file_id': 'file-fixture-1', 'detail': 'auto'}])
        self.assertNotIn('base64', json.dumps(self.protocol.vision_inputs))
        for item in self.assets():
            evidence = item['annotations'][0]['evidence']
            self.assertEqual('compressed image preview' in evidence, item['kind'] == 'image')
            self.assertEqual('image pixels were not provided' in evidence, item['kind'] != 'image')

    def test_vision_cleans_owned_files_on_mismatched_response_and_never_claims_success(self):
        self.ai.configure({'classification': {'mode': 'responses-vision'}})
        self.protocol.inject_id = True
        result = self.wait(self.ai.start_job('classify', project_id=self.project['id']))
        self.assertEqual(result['progress']['failed'], 2)
        self.assertEqual(self.protocol.deleted, ['file-fixture-1'])
        self.assertTrue(all(not item['annotations'] for item in self.assets()))

    def test_vision_connection_test_uses_generated_image_and_does_not_read_assets(self):
        self.ai.configure({'classification': {'mode': 'responses-vision'}})
        with patch.object(self.library, 'open_file', side_effect=AssertionError('must not read user assets')):
            result = self.ai.test_connection('classification')
        self.assertTrue(result['ok']); self.assertTrue(result['imagePixelsSent'])
        self.assertEqual(self.protocol.deleted, ['file-fixture-1'])

    def test_semantic_search_generates_query_and_filters_kind_and_model_space(self):
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        self.assertEqual(result['state'], 'completed')
        found = self.ai.search('gui button', project_id=self.project['id'], limit=200, kind='image')
        self.assertEqual(len(found['items']), 1)
        self.assertEqual(found['items'][0]['kind'], 'image')
        self.assertEqual(found['items'][0]['score'], 1.)
        self.assertFalse(found['approximate'])
        self.assertFalse(found['imageEmbedding'])
        calls = len(self.protocol.calls)
        self.ai.search('gui button', project_id=self.project['id'], limit=100, kind='image')
        self.assertEqual(len(self.protocol.calls), calls)
        self.ai.configure({'embedding': {'model': 'new-not-yet-indexed-model'}})
        found = self.ai.search('gui button', project_id=self.project['id'])
        self.assertEqual(found['items'], [])

    def test_unscanned_source_change_never_sends_metadata_or_pixels(self):
        target = next(value for value in self.assets() if value['kind'] == 'image')
        (self.source / target['path']).write_bytes(b'changed after scanning')
        self.ai.configure({'classification': {'mode': 'responses-vision'}})
        result = self.wait(self.ai.start_job('both', asset_ids=[target['id']]))
        self.assertEqual(result['progress']['failed'], 2)
        self.assertEqual(self.protocol.calls, [])

    def test_uncertain_binary_upload_is_not_retried(self):
        self.ai.configure({'classification': {'mode': 'responses-vision'}})
        target = next(value for value in self.assets() if value['kind'] == 'image')
        self.protocol.failures = [503]
        result = self.wait(self.ai.start_job('classify', asset_ids=[target['id']]))
        self.assertEqual(result['progress']['failed'], 1)
        self.assertEqual(len(self.protocol.calls), 1)
        self.assertTrue(self.protocol.calls[0][1].endswith('/files'))

    def test_vision_preview_is_bounded_jpeg_without_exif_or_original_filename(self):
        target = next(value for value in self.assets() if value['kind'] == 'image')
        preview = self.ai._jpeg_preview(target)
        self.assertTrue(preview.startswith(b'\xff\xd8'))
        self.assertLessEqual(len(preview), 1024 * 1024)
        with Image.open(io.BytesIO(preview)) as image:
            self.assertLessEqual(max(image.size), 1024)
            self.assertEqual(image.mode, 'RGB')
            self.assertFalse(image.getexif())
        self.assertNotIn(target['name'].encode(), preview)

    def test_list_models_does_not_require_model_name_and_only_returns_allowlisted_fields(self):
        self.ai.configure({'embedding': {'model': ''}})
        self.assertFalse(self.ai.status()['embedding']['configured'])
        result = self.ai.list_models('embedding')
        self.assertEqual(result['items'], [{'id': 'fixture-real-model', 'owned_by': 'fixture-provider'}])
        self.assertFalse(result['capabilitiesVerified'])
        self.assertEqual(self.protocol.calls[0][0], 'GET')
        self.assertEqual(self.protocol.calls[0][3], b'')
        self.assertNotIn('private-fixture-key', json.dumps(result))
        self.assertNotIn('extra_secret', json.dumps(result))
        self.ai.configure({'embedding': {'clearKey': True}})
        with self.assertRaises(AssetAIError):
            self.ai.list_models('embedding')

    def test_classification_key_echo_is_redacted_before_annotation_storage(self):
        self.protocol.echo_key = True
        result = self.wait(self.ai.start_job('classify', project_id=self.project['id']))
        self.assertEqual(result['state'], 'completed')
        self.assertNotIn('private-classify-key', json.dumps(self.assets()))
        self.assertTrue(all('[redacted]' in item['annotations'][0]['evidence'] for item in self.assets()))
        again = self.wait(self.ai.start_job('classify', project_id=self.project['id']))
        self.assertEqual(again['progress']['skipped'], 2)

    def test_job_summary_derives_safe_bounded_errors_without_returning_all_items(self):
        self.protocol.failures = [401]
        result = self.wait(self.ai.start_job('embed', project_id=self.project['id']))
        summary = self.ai.jobs()[0]
        self.assertEqual(summary['state'], 'completed-with-errors')
        self.assertEqual(summary['error'], result['items'][0]['error'])
        self.assertIn('401', summary['error'])
        self.assertEqual(len(summary['sampleErrors']), 2)
        self.assertNotIn('items', summary)
        self.assertNotIn('private-fixture-key', json.dumps(summary))
        with sqlite3.connect(self.ai.database) as db:
            stored = json.loads(db.execute('SELECT data FROM jobs WHERE id=?', (result['jobId'],)).fetchone()[0])
        self.assertNotIn('error', stored)
        self.assertNotIn('sampleErrors', stored)

    def test_blank_base_url_cannot_smuggle_old_key_into_a_different_provider(self):
        with self.assertRaises(AssetAIError):
            self.ai.configure({'embedding': {'baseUrl': ''}})
        self.ai.configure({'embedding': {'baseUrl': '', 'clearKey': True}})
        self.assertFalse(self.ai.status()['embedding']['hasApiKey'])
        self.ai.configure({'embedding': {'apiKey': 'standalone-fixture-key'}})
        with self.assertRaises(AssetAIError):
            self.ai.configure({'embedding': {'baseUrl': 'https://another.fixture.test/v1'}})

    def test_job_item_pagination_is_bounded(self):
        result = self.wait(self.ai.start_job('both', project_id=self.project['id']))
        page = self.ai.job(result['jobId'], offset=1, limit=2)
        self.assertEqual(len(page['items']), 2)
        self.assertEqual(page['progress']['total'], 4)
        self.assertNotIn('items', self.ai.jobs()[0])

    def test_description_remains_valid_bounded_json_for_long_metadata(self):
        target = self.assets()[0]
        self.library.save_annotation(target['id'], {'source': 'manual', 'purpose': '长描述' * 1300, 'evidence': '证据' * 4000})
        text = self.ai._describe(self.library.asset(target['id']), 'embed')
        self.assertLessEqual(len(text), 6000)
        self.assertFalse(json.loads(text)['imagePixelsProvided'])


if __name__ == '__main__':
    unittest.main()
