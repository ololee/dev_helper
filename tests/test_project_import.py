"""Project imports are isolated, idempotent and never run a device tool."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/import_project.py'
spec = importlib.util.spec_from_file_location('devhelper_project_import', SCRIPT)
project_import = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project_import)


class ProjectImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.source = Path(self.tmp.name) / 'public-skill.md'
        self.source.write_text('---\nname: devhelper-connect\ndescription: Public connection guidance\n---\n# Connect\n', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def run_import(self, **kwargs):
        return project_import.import_project(self.root, skill_source=self.source, skip_check=True, **kwargs)

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def test_both_clients_merge_comments_other_servers_and_skill_without_global_changes(self):
        codex = self.root / '.codex/config.toml'
        codex.parent.mkdir()
        original = b'# Keep this comment\r\nmodel = "synthetic-model"\r\n[mcp_servers.other]\r\nurl = "http://localhost:1/mcp"\r\n'
        codex.write_bytes(original)
        claude = self.root / '.mcp.json'
        claude.write_text(json.dumps({'custom': {'keep': True}, 'mcpServers': {'other': {'command': 'synthetic-tool', 'args': ['--keep']}}}))
        result = self.run_import()
        self.assertTrue(codex.read_bytes().startswith(original))
        self.assertEqual(tomllib.loads(codex.read_text())['mcp_servers']['devhelper']['url'], project_import.DEFAULT_URL)
        parsed = json.loads(claude.read_text())
        self.assertEqual(parsed['custom'], {'keep': True})
        self.assertEqual(parsed['mcpServers']['other']['args'], ['--keep'])
        self.assertEqual(parsed['mcpServers']['devhelper'], {'type': 'http', 'url': project_import.DEFAULT_URL})
        for folder in ('.agents', '.claude'):
            self.assertEqual((self.root / folder / 'skills/devhelper-connect/SKILL.md').read_bytes(), self.source.read_bytes())
        self.assertEqual(result['verification']['status'], 'skipped')
        self.assertEqual(result['clientConnection'], 'not-tested')
        self.assertEqual(len(result['changes']), 6)
        self.assertEqual(set(path.name for path in Path(self.tmp.name).iterdir()), {'project', 'public-skill.md'})

    def test_repeat_is_byte_idempotent_and_preserves_extra_client_options(self):
        self.run_import()
        codex = self.root / '.codex/config.toml'
        codex.write_text(codex.read_text() + 'enabled = false\nstartup_timeout_sec = 30\n')
        claude = self.root / '.mcp.json'
        parsed = json.loads(claude.read_text())
        parsed['mcpServers']['devhelper']['headers'] = {'X-Synthetic': 'preserved'}
        claude.write_text(json.dumps(parsed, separators=(',', ':')))
        before = self.files()
        result = self.run_import()
        self.assertEqual(result['changes'], [])
        self.assertEqual(self.files(), before)
        self.assertFalse(result['clients'][0]['projectEntryEnabled'])

    def test_dry_run_checks_and_previews_without_writes(self):
        with patch.object(project_import, 'check_connection', return_value={'status': 'verified'}) as probe:
            result = project_import.import_project(self.root, skill_source=self.source, dry_run=True)
        probe.assert_called_once_with(project_import.DEFAULT_URL)
        self.assertFalse(result['applied'])
        self.assertEqual(len(result['changes']), 6)
        self.assertEqual(self.files(), {})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_network_failure_does_not_create_any_directories(self):
        with patch.object(project_import, 'check_connection', side_effect=project_import.ImportFailure('offline')):
            with self.assertRaises(project_import.ImportFailure):
                project_import.import_project(self.root, skill_source=self.source)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_conflicting_second_client_is_preflighted_before_any_writes_or_network(self):
        (self.root / '.mcp.json').write_text(json.dumps({'mcpServers': {'devhelper': {'type': 'http', 'url': 'http://localhost:1/mcp'}}}))
        before = self.files()
        with patch.object(project_import, 'check_connection') as probe:
            with self.assertRaisesRegex(project_import.ImportFailure, 'Claude'):
                project_import.import_project(self.root, skill_source=self.source)
        probe.assert_not_called()
        self.assertEqual(self.files(), before)
        self.assertFalse((self.root / '.codex').exists())

    def test_malformed_json_and_duplicate_entries_are_refused_without_writes(self):
        for value in ('not-json', '[]', '{"mcpServers":[]}', '{"mcpServers":{},"mcpServers":{}}', '{"mcpServers":{},"extra":NaN}', '{"mcpServers":{},"extra":Infinity}'):
            (self.root / '.mcp.json').write_text(value)
            before = self.files()
            with self.subTest(value=value), self.assertRaises(project_import.ImportFailure):
                self.run_import()
            self.assertEqual(self.files(), before)

    def test_malformed_and_inline_toml_are_preserved(self):
        codex = self.root / '.codex/config.toml'
        codex.parent.mkdir()
        for value in ('[broken', 'mcp_servers = {other = {url = "http://localhost:1/mcp"}}\n', 'mcp_servers = "invalid"'):
            codex.write_text(value)
            with self.subTest(value=value), self.assertRaises(project_import.ImportFailure):
                self.run_import()
            self.assertEqual(codex.read_text(), value)
            self.assertFalse((self.root / '.mcp.json').exists())

    def test_existing_stdio_or_different_url_are_not_overwritten(self):
        codex = self.root / '.codex/config.toml'
        codex.parent.mkdir()
        for entry in ('command = "synthetic"', 'url = "http://localhost:9/mcp"', 'url = "' + project_import.DEFAULT_URL + '"\ncommand = "synthetic"'):
            codex.write_text('[mcp_servers.devhelper]\n' + entry + '\n')
            before = codex.read_bytes()
            with self.subTest(entry=entry), self.assertRaises(project_import.ImportFailure):
                self.run_import()
            self.assertEqual(codex.read_bytes(), before)

    def test_symlink_parent_target_and_skill_folder_are_refused(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        target = outside / 'untouched'
        target.write_text('keep')
        for relative, link_target in (('.codex', outside), ('.mcp.json', target), ('.claude/skills/devhelper-connect', outside)):
            link = self.root / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(link_target)
            with self.subTest(relative=relative), self.assertRaises(project_import.ImportFailure):
                self.run_import()
            link.unlink()
            self.assertEqual(target.read_text(), 'keep')
            self.assertFalse((self.root / '.codex/config.toml').exists())

    def test_unknown_skill_and_user_edited_managed_skill_are_never_overwritten(self):
        directory = self.root / '.claude/skills/devhelper-connect'
        directory.mkdir(parents=True)
        skill = directory / 'SKILL.md'
        skill.write_text('user-owned Skill')
        with self.assertRaisesRegex(project_import.ImportFailure, '同名 Skill'):
            self.run_import()
        self.assertFalse((self.root / '.codex').exists())
        skill.unlink()
        self.run_import()
        skill.write_text(skill.read_text() + '\nUser edit\n')
        before = self.files()
        with self.assertRaisesRegex(project_import.ImportFailure, '本地编辑'):
            self.run_import()
        self.assertEqual(self.files(), before)

    def test_managed_skill_can_update_but_matching_unmanaged_skill_is_not_claimed(self):
        self.run_import()
        self.source.write_text(self.source.read_text() + '\nNew public guidance\n')
        result = self.run_import()
        self.assertEqual(len(result['changes']), 4)
        directory = self.root / '.agents/skills/devhelper-connect'
        self.assertEqual((directory / 'SKILL.md').read_bytes(), self.source.read_bytes())
        (directory / project_import.MARKER).unlink()
        result = self.run_import(client='codex')
        self.assertEqual(result['changes'], [])
        self.assertFalse((directory / project_import.MARKER).exists())

    def test_forged_marker_or_missing_managed_file_are_refused(self):
        self.run_import()
        marker = self.root / '.claude/skills/devhelper-connect' / project_import.MARKER
        for data in ({'managedBy': 'someone-else'}, {'formatVersion': 1, 'managedBy': project_import.MANAGER, 'files': {'../../private.md': 'x'}}):
            marker.write_text(json.dumps(data))
            before = self.files()
            with self.subTest(data=data), self.assertRaises(project_import.ImportFailure):
                self.run_import()
            self.assertEqual(self.files(), before)

    def test_write_failure_rolls_back_existing_content_and_new_files(self):
        (self.root / '.mcp.json').write_text('{"custom": true}\n')
        before = self.files()
        original_atomic = project_import._atomic
        calls = 0
        def fail_fourth(path, content, mode=None):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError('synthetic disk failure')
            return original_atomic(path, content, mode)
        with patch.object(project_import, '_atomic', side_effect=fail_fourth):
            with self.assertRaisesRegex(project_import.ImportFailure, '已回退'):
                self.run_import()
        self.assertEqual(self.files(), before)
        self.assertEqual(set(path.name for path in self.root.iterdir()), {'.mcp.json'})

    def test_concurrent_edit_after_probe_is_preserved(self):
        existing = self.root / '.mcp.json'
        existing.write_text('{}')
        def concurrent_edit(url):
            existing.write_text('{"userEdited": true}')
            return {'status': 'verified'}
        with patch.object(project_import, 'check_connection', side_effect=concurrent_edit):
            with self.assertRaisesRegex(project_import.ImportFailure, '发生变化'):
                project_import.import_project(self.root, skill_source=self.source)
        self.assertEqual(json.loads(existing.read_text()), {'userEdited': True})
        self.assertFalse((self.root / '.codex').exists())

    def test_single_client_without_skill_needs_no_source_file(self):
        self.source.unlink()
        result = self.run_import(client='claude', without_skill=True)
        self.assertEqual([entry['client'] for entry in result['clients']], ['claude'])
        self.assertIsNone(result['clients'][0]['skill'])
        self.assertEqual(set(self.files()), {'.mcp.json'})

    def test_existing_claude_streamable_http_alias_is_accepted_without_rewriting(self):
        config = self.root / '.mcp.json'
        original = json.dumps({'mcpServers': {'devhelper': {'type': 'streamable-http', 'url': project_import.DEFAULT_URL}}}, separators=(',', ':'))
        config.write_text(original)
        result = self.run_import(client='claude', without_skill=True)
        self.assertEqual(result['changes'], [])
        self.assertEqual(config.read_text(), original)

    def test_unsafe_urls_fail_even_when_check_is_skipped(self):
        for value in ('stdio://localhost', 'http://user:password@localhost/mcp', 'http://localhost/mcp?token=synthetic', 'http://localhost/mcp#fragment', 'http://localhost:0/mcp', 'http://localhost:99999/mcp', 'http://localhost/../mcp', 'http://localhost/%2e%2e/mcp', 'http://localhost/mcp\n', 'http://localhost\\other/mcp', 'http://localhost:/mcp', 'http://local%2fhost/mcp', 'http://localhost/中文/mcp'):
            with self.subTest(value=value), self.assertRaises(project_import.ImportFailure):
                self.run_import(url=value)
        self.assertEqual(self.files(), {})
        self.assertEqual(project_import.validate_url('http://127.0.0.1:8876/'), project_import.DEFAULT_URL)
        self.assertEqual(project_import.validate_url('http://[::1]:8876/mcp/'), 'http://[::1]:8876/mcp')


class ProjectImportProtocolTests(unittest.TestCase):
    def server(self, *, phone=False, sse=False, wrong_identity=False, redirect=False, oversized=False, rpc_error=False):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def reply(self, value, status=200, content_type='application/json'):
                raw = json.dumps(value).encode()
                if oversized:
                    raw = b' ' * (project_import.MAX_RESPONSE_BYTES + 1)
                if content_type == 'text/event-stream':
                    raw = b': keepalive\n\nevent: message\ndata: ' + raw + b'\n\n'
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    # Oversized-response checks intentionally close early.
                    pass
            def do_GET(self):
                requests.append(('GET', self.path, None))
                if redirect:
                    self.send_response(302)
                    self.send_header('Location', '/must-not-visit')
                    self.end_headers()
                    return
                value = {'status': 'ok', 'transport': 'streamable-http'}
                if not phone:
                    value['appId'] = 'devhelper-desktop'
                self.reply(value)
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(('POST', self.path, data))
                if data['method'] == 'notifications/initialized':
                    self.send_response(202)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if data['method'] == 'initialize':
                    result = {'protocolVersion': '2025-06-18', 'serverInfo': {'name': 'other-app' if wrong_identity else ('libsu-mcp' if phone else 'devhelper-desktop')}, 'capabilities': {'tools': {}}}
                elif data['method'] == 'tools/list':
                    result = {'tools': [{'name': 'knowledge_status' if phone else 'devhelper_list_devices', 'inputSchema': {'type': 'object'}}]}
                else:
                    raise AssertionError('Importer tried to execute an unexpected method')
                envelope = {'jsonrpc': '2.0', 'id': data['id'], 'error': {'code': -32000, 'message': 'synthetic failure'}} if rpc_error else {'jsonrpc': '2.0', 'id': data['id'], 'result': result}
                self.reply(envelope, content_type='text/event-stream' if sse else 'application/json')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:' + str(server.server_port) + '/mcp', requests

    def test_desktop_and_phone_probe_never_run_tools_or_read_private_resources(self):
        for phone, sse in ((False, False), (True, False), (False, True)):
            url, calls = self.server(phone=phone, sse=sse)
            with self.subTest(phone=phone, sse=sse):
                result = project_import.check_connection(url)
                self.assertEqual(result['status'], 'verified')
                self.assertEqual(result['toolCount'], 1)
                self.assertEqual([(method, path) for method, path, _ in calls], [('GET', '/health'), ('POST', '/mcp'), ('POST', '/mcp'), ('POST', '/mcp')])
                self.assertEqual([data['method'] for method, _, data in calls if method == 'POST'], ['initialize', 'notifications/initialized', 'tools/list'])

    def test_redirect_different_service_protocol_error_and_oversized_response_are_rejected(self):
        for option in ('redirect', 'wrong_identity', 'oversized', 'rpc_error'):
            url, calls = self.server(**{option: True})
            with self.subTest(option=option), self.assertRaises(project_import.ImportFailure):
                project_import.check_connection(url)
            self.assertNotIn('/must-not-visit', [path for _, path, _ in calls])


if __name__ == '__main__':
    unittest.main()
