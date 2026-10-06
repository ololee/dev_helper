import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/publish.py'
spec = importlib.util.spec_from_file_location('devhelper_publish', SCRIPT)
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.multiple(publish, ROOT=self.root, MANIFEST=self.root / 'publish-files.json', CONFIG=self.root / '.publish-target.json')
        self.patch.start()
        (self.root / 'server.py').write_text('print("public source")\n')
        (self.root / 'publish-files.json').write_text(json.dumps({'files': ['server.py', 'publish-files.json']}))
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'DevHelper test'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'devhelper-test@invalid.local'], cwd=self.root, check=True)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_manifest_cannot_include_runtime_or_symlinks(self):
        for path in ('data/private.md', '.venv/bin/python', 'control-state.json', '../private.md', '/private.md', 'verification.json'):
            self.assertFalse(publish.valid_path(path))
        (self.root / 'server.py').unlink()
        (self.root / 'server.py').symlink_to(self.root / 'publish-files.json')
        with self.assertRaises(ValueError):
            publish.read_manifest()

    def test_user_specific_paths_block_publication(self):
        private_path = '/' + 'Users' + '/example-account/private.txt'
        (self.root / 'server.py').write_text('PATH=' + repr(private_path))
        with self.assertRaises(ValueError):
            publish.read_manifest()

    def test_unknown_untracked_or_staged_files_block_before_push(self):
        private = self.root / 'private-notes.md'
        private.write_text('synthetic private content')
        original_run = publish.run
        called_push = []
        def guarded_run(argv, capture=False):
            if argv[:2] == ['git', 'push']:
                called_push.append(argv)
                raise AssertionError('A blocked publication attempted network access')
            return original_run(argv, capture)
        for staged in (False, True):
            if staged:
                subprocess.run(['git', 'add', 'private-notes.md'], cwd=self.root, check=True)
            with patch.object(publish, 'run', guarded_run), patch('sys.argv', ['publish.py', '--push']):
                with self.assertRaises(ValueError):
                    publish.main()
        self.assertEqual(called_push, [])

    def test_read_only_check_leaves_index_and_history_unchanged(self):
        index_before = subprocess.check_output(['git', 'ls-files', '-z'], cwd=self.root)
        with patch('sys.argv', ['publish.py']):
            publish.main()
        index_after = subprocess.check_output(['git', 'ls-files', '-z'], cwd=self.root)
        self.assertEqual(index_before, index_after)
        self.assertFalse((self.root / '.publish-target.json').exists())

    def test_repo_target_rejects_credentials_and_unconfigured_publish(self):
        self.assertEqual(publish.github_identity('https://github.com/example/devhelper.git'), ('example', 'devhelper'))
        for url in ('https://token@github.com/example/devhelper', 'https://example.com/a/b', 'https://github.com/a/b?token=x'):
            with self.assertRaises(ValueError):
                publish.github_identity(url)
        with self.assertRaises(ValueError):
            publish.configured_target()


if __name__ == '__main__':
    unittest.main()
