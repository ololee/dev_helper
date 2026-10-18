"""Protocol fixtures only: no user credentials and no requests to Baidu."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

try:
    from desktop.baidu_drive import BaiduDriveService, BaiduDriveError, BLOCK_SIZE, _trusted_url, _download_url, _Redirects
except ImportError:
    from baidu_drive import BaiduDriveService, BaiduDriveError, BLOCK_SIZE, _trusted_url, _download_url, _Redirects


class Transport:
    def __init__(self):
        self.calls = []
        self.parts = {}
        self.content = b'\x00\xffbinary-payload\x00'
        self.tokens = 0
        self.fail_part_once = False
        self.part_failed = False
        self.fail_download_once = False
        self.download_failed = False
        self.mismatch = False
        self.outside = False
        self.mutate = None
        self.error_body = None
        self.rapid_upload = False
        self.cached_first_block = False
        self.create_reply = None
        self.dlink = 'https://d.pcs.baidu.com/fixture?sign=fixture'

    def open(self, method, url, *, headers=None, body=None, timeout=90):
        query = parse_qs(urlsplit(url).query)
        action = query.get('method', [''])[0]
        payload = b''.join(body) if body is not None and not isinstance(body, bytes) else body or b''
        self.calls.append((method, url, dict(headers or {}), payload))
        if self.error_body is not None:
            return io.BytesIO(json.dumps(self.error_body).encode())
        if urlsplit(url).path.endswith('/token'):
            self.tokens += 1
            data = dict(access_token='private-fixture-token-' + str(self.tokens), refresh_token='private-fixture-refresh', expires_in=3600)
        elif action == 'list':
            data = dict(errno=0, list=[dict(fs_id=123, path='/apps/Fixture/image.png', server_filename='image.png', size=5, isdir=0, dlink='secret-must-not-return')])
        elif action == 'uinfo':
            data = dict(errno=0, uk=1234567890123, netdisk_name='private-fixture-name')
        elif action == 'precreate':
            form = parse_qs(payload.decode())
            blocks = json.loads(form['block_list'][0])
            missing = list(range(1 if self.cached_first_block else 0, len(blocks)))
            data = dict(errno=0, uploadid='fixture-upload-id', return_type=2 if self.rapid_upload else 1, block_list=missing)
        elif action == 'upload':
            if self.fail_part_once and not self.part_failed:
                self.part_failed = True
                raise URLError('access_token=private-fixture-token-1')
            boundary = headers['Content-Type'].split('boundary=')[1].encode()
            binary = payload.split(b'\r\n\r\n', 1)[1].removesuffix(b'\r\n--' + boundary + b'--\r\n')
            self.parts[int(query['partseq'][0])] = binary
            if self.mutate:
                self.mutate()
            data = dict(errno=0, md5='0'*32 if self.mismatch else hashlib.md5(binary).hexdigest())
        elif action == 'create':
            form = parse_qs(payload.decode())
            data = self.create_reply or dict(errno=0, fs_id=456, path=form['path'][0], size=int(form['size'][0]))
        elif action == 'filemetas':
            data = dict(errno=0, list=[dict(fs_id=123, path='/outside/private.bin' if self.outside else '/apps/Fixture/game.bin', size=len(self.content), isdir=0,
                md5=hashlib.md5(self.content).hexdigest(), dlink=self.dlink)])
        elif urlsplit(url).path == '/fixture':
            if self.fail_download_once and not self.download_failed:
                self.download_failed = True
                return io.BytesIO(self.content[:-1])
            return io.BytesIO(self.content)
        else:
            raise AssertionError('Unexpected fixture request')
        return io.BytesIO(json.dumps(data).encode())


class BaiduDriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.http = Transport()
        self.service = BaiduDriveService(self.root, self.http)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def configure(self):
        return self.service.configure(dict(clientId='fixture-app', clientSecret='fixture-secret', appName='Fixture', redirectUri='oob'))

    def authorize(self):
        self.configure()
        auth = self.service.authorization_url()
        self.service.exchange_code('fixture-code', auth['state'])

    def wait(self, job):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = self.service.job(job['jobId'])
            if result['state'] not in ('queued', 'running'):
                return result
            time.sleep(.01)
        self.fail('Fixture transfer timeout')

    def test_missing_config_makes_no_network_calls(self):
        self.assertEqual(self.service.status()['missingFields'], ['clientId', 'clientSecret', 'appName'])
        for call in (lambda: self.service.authorization_url(), lambda: self.service.list_files(), lambda: self.service.refresh()):
            with self.assertRaises(BaiduDriveError):
                call()
        self.assertEqual(self.http.calls, [])

    def test_config_redaction_private_permissions_and_reload(self):
        status = self.configure()
        self.assertTrue(status['configured'])
        self.assertNotIn('fixture-secret', json.dumps(status))
        if os.name != 'nt':
            self.assertEqual(self.service.config_path.stat().st_mode & 0o777, 0o600)
        self.service.configure({'clientSecret': ''})
        self.assertTrue(self.service.status()['hasClientSecret'])
        self.service.close()
        self.service = BaiduDriveService(self.root, self.http)
        self.assertTrue(self.service.status()['configured'])
        self.assertNotIn('clientSecret', self.service.status())

    def test_oauth_state_expires_and_is_single_use_tokens_redacted(self):
        self.configure()
        authorization = self.service.authorization_url()
        query = parse_qs(urlsplit(authorization['url']).query)
        self.assertEqual(query['state'], [authorization['state']])
        self.assertEqual(query['scope'], ['basic,netdisk'])
        for state in (None, 'wrong'):
            with self.assertRaises(BaiduDriveError):
                self.service.exchange_code('code', state)
        self.assertFalse(self.http.calls)
        self.service.config['oauthStateExpiresAt'] = time.time() - 1
        with self.assertRaises(BaiduDriveError):
            self.service.exchange_code('code', authorization['state'])
        authorization = self.service.authorization_url()
        state = authorization['state']
        result = self.service.exchange_code('code', state)
        self.assertTrue(result['authorized'])
        self.assertNotIn('private-fixture', json.dumps(result))
        self.assertNotIn('fixture-secret', self.http.calls[0][1])
        with self.assertRaises(BaiduDriveError):
            self.service.exchange_code('code', state)
        self.assertEqual(self.http.tokens, 1)

    def test_token_refresh_and_configuration_identity_invalidation(self):
        self.authorize()
        self.service.config['expiresAt'] = time.time() - 5
        self.service.list_files()
        self.assertEqual(self.http.tokens, 2)
        self.service.configure({'clientId': 'new-app'})
        self.assertFalse(self.service.status()['authorized'])
        self.assertFalse(self.service.status()['hasRefreshToken'])

    def test_parallel_calls_refresh_once(self):
        self.authorize()
        self.service.config['expiresAt'] = time.time() - 5
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(self.service.list_files) for _ in range(2)]
            self.assertTrue(all(result.result()['items'] for result in results))
        self.assertEqual(self.http.tokens, 2)

    def test_remote_scope_lists_and_creates_without_escaping(self):
        self.authorize()
        self.assertEqual(self.service.remote_path('images/a.png'), '/apps/Fixture/images/a.png')
        for path in ('../escape', '/apps/Other/a', '/apps/Fixture2/a', 'a/../b', 'a\\b', 'a\x00b'):
            with self.assertRaises(BaiduDriveError):
                self.service.remote_path(path)
        result = self.service.list_files()
        self.assertNotIn('dlink', result['items'][0])
        result = self.service.mkdir('new')
        self.assertEqual(result['path'], '/apps/Fixture/new')
        with self.assertRaises(BaiduDriveError):
            self.service.list_files(limit=1001)

    def test_new_application_root_is_empty_but_missing_subfolder_is_an_error(self):
        self.authorize()
        self.http.error_body = dict(errno=-9)
        result = self.service.list_files()
        self.assertEqual(result['items'], [])
        self.assertFalse(result['directoryExists'])
        with self.assertRaises(BaiduDriveError) as missing:
            self.service.list_files('missing-subfolder')
        self.assertEqual(missing.exception.provider_code, '-9')
        self.http.error_body = dict(errno=12)
        with self.assertRaises(BaiduDriveError):
            self.service.list_files()
        # Listing never creates a directory as a side effect.
        self.assertTrue(all(call[0] == 'GET' for call in self.http.calls[1:]))

    def test_authorized_user_id_comes_from_current_token_without_account_profile(self):
        self.authorize()
        self.assertEqual(self.service.authorized_user_id(), '1234567890123')
        call = self.http.calls[-1]
        query = parse_qs(urlsplit(call[1]).query)
        self.assertEqual(query['method'], ['uinfo'])
        self.assertEqual(query['access_token'], ['private-fixture-token-1'])
        self.http.error_body = dict(errno=0, uk=0)
        with self.assertRaises(BaiduDriveError):
            self.service.authorized_user_id()

    def test_streaming_binary_upload_blocks_retry_exact_bytes_and_digests(self):
        self.authorize()
        content = bytes(range(256)) * (BLOCK_SIZE // 256) + b'final\x00\xff'
        source = self.root / 'source.bin'
        source.write_bytes(content)
        self.http.fail_part_once = True
        result = self.wait(self.service.start_upload(source, 'backups/source.bin'))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(b''.join(self.http.parts[i] for i in sorted(self.http.parts)), content)
        self.assertEqual(result['result']['md5'], hashlib.md5(content).hexdigest())
        self.assertEqual(result['result']['sha256'], hashlib.sha256(content).hexdigest())
        for method, url, headers, body in self.http.calls:
            if parse_qs(urlsplit(url).query).get('method') in (['precreate'], ['create']):
                self.assertEqual(parse_qs(body.decode())['rtype'], ['1'])
            if parse_qs(urlsplit(url).query).get('method') == ['upload']:
                self.assertEqual(int(headers['Content-Length']), len(body))
        self.assertEqual(source.read_bytes(), content)
        self.assertNotIn(str(self.root), json.dumps(self.service.jobs()))
        self.assertNotIn('private-fixture', json.dumps(self.service.jobs()))

    def test_empty_file_upload_and_md5_mismatch_never_commits(self):
        self.authorize()
        source = self.root / 'empty.bin'
        source.touch()
        result = self.wait(self.service.start_upload(source, 'empty.bin'))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(self.http.parts[0], b'')
        self.http.calls.clear()
        self.http.mismatch = True
        source.write_bytes(b'changed')
        result = self.wait(self.service.start_upload(source, 'changed.bin'))
        self.assertEqual(result['state'], 'failed')
        self.assertIn('MD5', result['error'])
        self.assertFalse(any(parse_qs(urlsplit(call[1]).query).get('method') == ['create'] for call in self.http.calls))

    def test_rapid_upload_is_only_marked_complete_after_provider_success(self):
        self.authorize()
        self.http.rapid_upload = True
        source = self.root / 'rapid.bin'
        source.write_bytes(b'cached-data')
        result = self.wait(self.service.start_upload(source, 'rapid.bin'))
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['result']['rapidUpload'])
        self.assertFalse(result['result']['pathConfirmed'])
        self.assertFalse(self.http.parts)

    def test_normal_upload_receipt_distinguishes_requested_and_confirmed_path(self):
        self.authorize()
        source=self.root/'path-confirmation.bin';source.write_bytes(b'generated-path-fixture')
        result=self.wait(self.service.start_upload(source,'path-confirmation.bin'))
        self.assertEqual(result['state'],'completed',result)
        self.assertTrue(result['result']['pathConfirmed'])
        self.http.create_reply=dict(errno=0,fs_id=456,size=source.stat().st_size)
        result=self.wait(self.service.start_upload(source,'path-unconfirmed.bin'))
        self.assertEqual(result['state'],'completed',result)
        self.assertFalse(result['result']['pathConfirmed'])
        self.assertEqual(result['result']['remotePath'],'/apps/Fixture/path-unconfirmed.bin')

    def test_cached_blocks_are_retained_in_complete_file_block_list(self):
        self.authorize()
        self.http.cached_first_block = True
        source = self.root / 'cached.bin'
        source.write_bytes(b'x' * BLOCK_SIZE + b'last')
        result = self.wait(self.service.start_upload(source, 'cached.bin'))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(self.http.parts, {1: b'last'})
        commit = next(call for call in self.http.calls if parse_qs(urlsplit(call[1]).query).get('method') == ['create'])
        hashes = json.loads(parse_qs(commit[3].decode())['block_list'][0])
        self.assertEqual(hashes, [hashlib.md5(b'x' * BLOCK_SIZE).hexdigest(), hashlib.md5(b'last').hexdigest()])

    def test_create_missing_confirmation_is_not_fake_success(self):
        self.authorize()
        self.http.create_reply = {'errno': 0}
        source = self.root / 'missing.bin'
        source.write_bytes(b'content')
        result = self.wait(self.service.start_upload(source, 'missing.bin'))
        self.assertEqual(result['state'], 'failed')
        self.assertIn('检查云端列表', result['error'])

    def test_upload_source_mutation_stops_before_create(self):
        self.authorize()
        source = self.root / 'changing.bin'
        source.write_bytes(b'before')
        self.http.mutate = lambda: source.write_bytes(b'after-longer')
        result = self.wait(self.service.start_upload(source, 'changing.bin'))
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(any(parse_qs(urlsplit(call[1]).query).get('method') == ['create'] for call in self.http.calls))

    def test_download_streamed_and_incomplete_retry_preserves_destination(self):
        self.authorize()
        target = self.root / 'new.bin'
        self.http.fail_download_once = True
        result = self.wait(self.service.start_download(123, target))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(target.read_bytes(), self.http.content)
        self.assertTrue(result['result']['md5MatchesRemote'])
        self.assertEqual(result['result']['sha256'], hashlib.sha256(self.http.content).hexdigest())
        with self.assertRaises(BaiduDriveError):
            self.service.start_download(123, target)
        self.assertEqual(target.read_bytes(), self.http.content)
        self.assertFalse(list(self.root.glob('*.part')))

    def test_manifest_digest_verification_blocks_corrupt_download(self):
        self.authorize()
        target = self.root / 'verified.bin'
        result = self.wait(self.service.start_download(123, target, expected_sha256='0' * 64))
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('*.part')))
        result = self.wait(self.service.start_download(123, target,
            expected_sha256=hashlib.sha256(self.http.content).hexdigest()))
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['result']['verifiedAgainstManifest'])

    def test_download_outside_scope_and_unsafe_links_rejected(self):
        self.authorize()
        self.http.outside = True
        target = self.root / 'private.bin'
        result = self.wait(self.service.start_download(123, target))
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(target.exists())
        for url in ('http://d.pcs.baidu.com/file', 'https://baidu.com.evil.invalid/file', 'https://127.0.0.1/file', 'https://user:pw@baidu.com/file'):
            with self.assertRaises(BaiduDriveError):
                _trusted_url(url)
        for url in ('https://baidu.com/file', 'https://openapi.baidu.com/file', 'https://evilpcs.baidu.com/file', 'https://d.pcs.baidu.com:bad/file'):
            with self.assertRaises(BaiduDriveError):
                _download_url(url)

    def test_download_redirect_has_domain_boundaries_and_strips_bos_token(self):
        redirects = _Redirects()
        request = Request('https://d.pcs.baidu.com/file?access_token=fixture-token')
        result = redirects.redirect_request(request, None, 302, 'Found', {}, 'https://fixture.bcebos.com/file?sign=ok&access_token=fixture-token')
        self.assertNotIn('access_token', result.full_url)
        self.assertIn('sign=ok', result.full_url)
        for target in ('https://baidu.com.evil.invalid/file', 'https://openapi.baidu.com/file', 'http://d.pcs.baidu.com/file'):
            with self.assertRaises(BaiduDriveError):
                redirects.redirect_request(request, None, 302, 'Found', {}, target)
        with self.assertRaises(BaiduDriveError):
            redirects.redirect_request(Request('https://pan.baidu.com/rest/2.0/xpan/file?access_token=fixture-token'), None, 302, 'Found', {}, 'https://d.pcs.baidu.com/file')

    def test_provider_errors_do_not_echo_tokens_or_urls(self):
        self.authorize()
        self.http.error_body = dict(errno=20016, errmsg='access_token=private-fixture-token-1 https://example.invalid')
        with self.assertRaises(BaiduDriveError) as raised:
            self.service.list_files()
        self.assertNotIn('private-fixture', str(raised.exception))
        self.assertNotIn('https://', str(raised.exception))

    def test_inflight_jobs_become_interrupted_on_restart(self):
        self.configure()
        self.service.close()
        self.service.jobs_path.write_text(json.dumps({'fixture-job': dict(jobId='fixture-job', state='running', operation='upload')}))
        self.service = BaiduDriveService(self.root, self.http)
        self.assertEqual(self.service.job('fixture-job')['state'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
