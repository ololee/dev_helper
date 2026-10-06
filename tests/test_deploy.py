import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/devhelper-deploy/scripts/deploy.py'
if not SCRIPT.is_file():
    SCRIPT = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'skills/devhelper-deploy/scripts/deploy.py'
deploy = None
if SCRIPT.is_file():
    spec = importlib.util.spec_from_file_location('devhelper_deploy', SCRIPT)
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)


@unittest.skipIf(deploy is None, 'Deployment Skill is not bundled in this source checkout')
class DeploymentTests(unittest.TestCase):
    def test_offline_phone_does_not_sync_or_materialize(self):
        replies = [{'appId': 'devhelper-desktop'}, {'devices': [{'id': 'android', 'online': False}]}]
        with patch.object(deploy, 'api', side_effect=replies) as api:
            result = deploy.restore('http://127.0.0.1:8876', install_skills=True)
        self.assertFalse(result['restored'])
        self.assertFalse(result['autoSyncEnabled'])
        self.assertEqual([call.args[2] for call in api.call_args_list], ['/health', '/api/devices'])

    def test_restore_downloads_before_enabling_and_reports_skill_errors_privately(self):
        private_body = 'synthetic private document body'
        replies = [
            {'appId': 'devhelper-desktop'},
            {'devices': [{'id': 'android', 'online': True}]},
            {'initialized': True, 'summary': {'downloaded': 2}, 'conflicts': [{'contentPreview': private_body}]},
            {'autoSync': True},
            {'installedSkills': 1, 'removedSkills': 0, 'errors': [{'title': private_body, 'error': private_body}]},
        ]
        with patch.object(deploy, 'api', side_effect=replies) as api:
            result = deploy.restore('http://127.0.0.1:8876', install_skills=True)
        self.assertEqual([call.args[2] for call in api.call_args_list], ['/health', '/api/devices', '/api/sync/run', '/api/sync/config', '/api/sync/materialize'])
        self.assertEqual(api.call_args_list[2].args[3], {'direction': 'download'})
        self.assertEqual(result['sync']['downloaded'], 2)
        self.assertEqual(result['sync']['conflicts'], 1)
        self.assertEqual(result['privateSkills']['errorCount'], 1)
        self.assertNotIn(private_body, json.dumps(result))

    def test_local_source_never_copies_unlisted_runtime_data(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.mkdir()
            (source / 'server.py').write_text('print("synthetic public source")\n')
            (source / 'data').mkdir()
            (source / 'data/private.md').write_text('synthetic private document')
            (source / 'publish-files.json').write_text(json.dumps({'files': ['server.py', 'publish-files.json']}))
            deploy.copy_public_source(source, target)
            self.assertTrue((target / 'server.py').is_file())
            self.assertFalse((target / 'data').exists())
            self.assertFalse((target / '.git').exists())

    def test_local_source_rejects_runtime_and_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'target'
            source.mkdir()
            (source / 'publish-files.json').write_text(json.dumps({'files': ['data/private.md']}))
            with self.assertRaises(ValueError):
                deploy.copy_public_source(source, target)
            self.assertFalse(target.exists())
            target.mkdir()
            sentinel = target / 'keep.txt'
            sentinel.write_text('existing local file')
            with self.assertRaises(ValueError):
                deploy.copy_public_source(source, target)
            self.assertEqual(sentinel.read_text(), 'existing local file')


if __name__ == '__main__':
    unittest.main()
