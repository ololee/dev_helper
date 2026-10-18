"""Pinned editor assets are verified locally and served through generated test apps.

No browser, model provider, device discovery, user files, or real bundle mutations
are involved. Malformed distributions are created only in temporary directories.
"""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import editor_assets
from server import Desktop
from starlette.testclient import TestClient


class EditorDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'fixture-editor'
        self.root.mkdir()
        self.files = {
            'vs/loader.js': b'/* generated editor loader fixture */',
            'vs/base/worker/workerMain.js': b'/* generated worker fixture */',
            'vs/editor/editor.main.css': b'.generated-editor { color: #123456 }',
            'vs/base/browser/ui/codicons/codicon/codicon.ttf': b'\x00\x01\x00\x00generated-font',
            'LICENSE': b'Generated fixture license; no production bundle modifications.',
        }
        for name, content in self.files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.manifest = {
            'name': 'monaco-editor', 'version': '0.52.2',
            'files': {name: {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                      for name, content in self.files.items()},
        }
        self.write_manifest()
        editor_assets._allowed.cache_clear()

    def tearDown(self):
        editor_assets._allowed.cache_clear()
        self.temp.cleanup()

    def write_manifest(self):
        (self.root / 'PROVENANCE.json').write_text(json.dumps(self.manifest))

    def fixture_distribution(self):
        """Patch the cache loader's bound default without touching real assets."""
        stack = ExitStack()
        original = editor_assets.distribution
        stack.enter_context(patch.object(editor_assets, 'ROOT', self.root))
        stack.enter_context(patch.object(editor_assets, 'distribution',
                                        side_effect=lambda **kw: original(self.root, **kw)))
        self.addCleanup(stack.close)
        editor_assets._allowed.cache_clear()
        return stack

    def test_distribution_verifies_the_complete_pinned_real_manifest(self):
        records = editor_assets.distribution(editor_assets.ROOT, verify=True)
        self.assertGreater(len(records), 100)
        self.assertIn('vs/loader.js', records)
        self.assertIn('vs/base/worker/workerMain.js', records)
        self.assertIn('vs/editor/editor.main.js', records)
        self.assertIn('LICENSE', records)
        self.assertIn('ThirdPartyNotices.txt', records)
        for name, record in records.items():
            with self.subTest(name=name):
                raw = (editor_assets.ROOT / name).read_bytes()
                self.assertEqual(len(raw), record['bytes'])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), record['sha256'])

    def test_generated_distribution_verifies_hashes_and_exact_allowlist(self):
        records = editor_assets.distribution(self.root, verify=True)
        self.assertEqual(set(records), set(self.files))
        self.fixture_distribution()
        for name in records:
            with self.subTest(name=name):
                target, mime = editor_assets.editor_asset('vendor/monaco/' + name)
                self.assertEqual(target, self.root / name)
                self.assertEqual(mime, editor_assets.MIMES.get(target.suffix, 'text/plain'))
        manifest, mime = editor_assets.editor_asset('vendor/monaco/PROVENANCE.json')
        self.assertEqual(manifest, self.root / 'PROVENANCE.json')
        self.assertEqual(mime, 'application/json')
        for name in ('vendor/monaco/package.json', 'vendor/monaco/vs/unknown.js',
                     'vendor/monaco/../resource-library.js', 'vendor/monaco/vs/../vs/loader.js',
                     'vendor/monaco/vs\\loader.js', 'vendor/monaco-evil/vs/loader.js',
                     '/vendor/monaco/vs/loader.js', None):
            with self.subTest(name=name):
                self.assertIsNone(editor_assets.editor_asset(name))

    def test_hash_and_length_mismatches_are_rejected_in_generated_distributions(self):
        target = self.root / 'vs/loader.js'
        original = target.read_bytes()
        target.write_bytes(b'!' * len(original))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            editor_assets.distribution(self.root, verify=True)
        target.write_bytes(original + b' length changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            editor_assets.distribution(self.root, verify=True)
        self.fixture_distribution()
        with self.assertRaisesRegex(ValueError, 'checksum'):
            editor_assets.editor_asset('vendor/monaco/vs/loader.js')

    def test_missing_file_root_and_provenance_symlinks_are_rejected(self):
        target = self.root / 'vs/loader.js'
        target.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing'):
            editor_assets.distribution(self.root, verify=True)
        target.write_bytes(self.files['vs/loader.js'])
        alias = self.root.parent / 'linked-editor'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            editor_assets.distribution(alias, verify=True)
        manifest = self.root / 'PROVENANCE.json'
        elsewhere = self.root.parent / 'generated-manifest.json'
        manifest.replace(elsewhere)
        manifest.symlink_to(elsewhere)
        with self.assertRaisesRegex(ValueError, 'provenance'):
            editor_assets.distribution(self.root, verify=True)

    def test_asset_and_parent_directory_symlinks_are_rejected(self):
        target = self.root / 'vs/loader.js'
        other = self.root.parent / 'generated-script.js'
        other.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            editor_assets.distribution(self.root, verify=True)
        target.unlink()
        target.write_bytes(self.files['vs/loader.js'])
        directory = self.root / 'vs/base'
        moved = self.root.parent / 'generated-base'
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            editor_assets.distribution(self.root, verify=True)

    def test_unsafe_manifest_paths_wrong_pin_and_non_mapping_records_are_rejected(self):
        for name in ('../outside.js', '/absolute.js', 'vs\\loader.js', 'vs//bad.js', './bad.js'):
            with self.subTest(path=name):
                self.manifest['files'][name] = {'bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    editor_assets.distribution(self.root, verify=True)
                del self.manifest['files'][name]
        for field, value in (('name', 'other-editor'), ('version', '0.0.0')):
            with self.subTest(field=field):
                original = self.manifest[field]
                self.manifest[field] = value
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, 'pinned'):
                    editor_assets.distribution(self.root, verify=True)
                self.manifest[field] = original
        for records in ([], {}, {'one.js': {}}):
            with self.subTest(records=records):
                self.manifest['files'] = records
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, 'manifest'):
                    editor_assets.distribution(self.root, verify=True)


class EditorHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.desktop = Desktop(8876, self.root / 'generated-data', self.root / 'generated-shared')
        self.discovery_start = patch.object(self.desktop.discovery, 'start')
        self.discovery_stop = patch.object(self.desktop.discovery, 'stop')
        self.discovery_start.start()
        self.discovery_stop.start()
        self.client = TestClient(self.desktop.app(), base_url='http://127.0.0.1:8876')
        self.client.__enter__()
        editor_assets._allowed.cache_clear()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.discovery_start.stop()
        self.discovery_stop.stop()
        editor_assets._allowed.cache_clear()
        self.temp.cleanup()

    def test_every_recorded_real_asset_is_served_with_exact_bytes_and_expected_mime(self):
        records = editor_assets.distribution(editor_assets.ROOT, verify=True)
        for name, record in records.items():
            with self.subTest(name=name):
                response = self.client.get('/static/vendor/monaco/' + name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.content), record['bytes'])
                self.assertEqual(hashlib.sha256(response.content).hexdigest(), record['sha256'])
                mime = editor_assets.MIMES.get(Path(name).suffix, 'text/plain')
                self.assertEqual(response.headers['content-type'].split(';')[0], mime)
                self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
                self.assertEqual(response.headers['cache-control'], 'no-cache')

    def test_worker_loader_editor_styles_font_and_provenance_have_correct_head_responses(self):
        expected = {
            'vs/loader.js': 'text/javascript',
            'vs/base/worker/workerMain.js': 'text/javascript',
            'vs/editor/editor.main.js': 'text/javascript',
            'vs/editor/editor.main.css': 'text/css',
            'vs/base/browser/ui/codicons/codicon/codicon.ttf': 'font/ttf',
            'PROVENANCE.json': 'application/json',
        }
        for name, mime in expected.items():
            with self.subTest(name=name):
                response = self.client.head('/static/vendor/monaco/' + name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b'')
                self.assertEqual(response.headers['content-type'].split(';')[0], mime)
                self.assertEqual(int(response.headers['content-length']), (editor_assets.ROOT / name).stat().st_size)
                self.assertEqual(response.headers['x-content-type-options'], 'nosniff')

    def test_unknown_and_encoded_traversal_paths_are_not_exposed_over_http(self):
        paths = (
            '/static/vendor/monaco/package.json',
            '/static/vendor/monaco/unknown.js',
            '/static/vendor/monaco/vs/loader.js.map',
            '/static/vendor/monaco-evil/vs/loader.js',
            '/static/vendor/monaco/%2e%2e/resource-library.js',
            '/static/vendor/monaco/vs/%2e%2e/vs/loader.js',
            '/static/vendor/monaco/vs%5cloader.js',
            '/static/vendor/monaco/%252e%252e/private.txt',
        )
        for path in paths:
            with self.subTest(path=path):
                for method in ('get', 'head'):
                    response = getattr(self.client, method)(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn(b'generated-data', response.content)

    def test_code_language_mappings_keep_contents_in_readonly_models_and_use_local_workers(self):
        response = self.client.get('/static/resource-library.js')
        self.assertEqual(response.status_code, 200)
        source = response.text
        for fragment in ("md:'markdown'", "lua:'lua'", "shader:'cpp'", "svg:'xml'",
                         'readOnly:true', 'domReadOnly:true', 'monaco.editor.createModel(text,registered',
                         "script.src='/static/vendor/monaco/vs/loader.js'",
                         "'/static/vendor/monaco/vs/base/worker/workerMain.js'",
                         'disposeTextViewer()', 'textViewer.model?.dispose()'):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, source)
        self.assertNotRegex(source, r'\b(?:eval|Function)\s*\(\s*(?:text|raw)\b')
        self.assertNotIn('cdn.jsdelivr.net', source)
        self.assertNotIn('unpkg.com', source)
        html = self.client.get('/resource-library')
        self.assertEqual(html.status_code, 200)
        self.assertIn("worker-src 'self'", html.text)
        self.assertNotIn("'unsafe-eval'", html.text)
        self.assertIn('id="preview-find"', html.text)
        self.assertIn('id="preview-wrap"', html.text)
        self.assertIn('id="preview-editor-status"', html.text)


if __name__ == '__main__':
    unittest.main()
