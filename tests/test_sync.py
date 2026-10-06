import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, FileResponse
from starlette.routing import Route
from knowledge import KnowledgeService, KnowledgeError, document_sync_hash
from sync import KnowledgeSync, SyncConflict
from private_skills import materialize

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


class Preferences:
    def get(self):
        return {'autoKnowledgeSync': False, 'installPrivateSkills': False}


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.local = KnowledgeService(self.root / 'mac')
        self.phone = KnowledgeService(self.root / 'phone')
        async def endpoint(request):
            path = request.url.path
            raw = await request.body()
            binary = '/sync/attachments/' in path
            data = json.loads(raw) if raw and not binary else None
            value = self.phone.handle(request.method, path, dict(request.query_params), data, io.BytesIO(raw) if binary else None, dict(request.headers))
            if value.file:
                return FileResponse(value.file, media_type=value.mime)
            return JSONResponse(value.data, value.status)
        app = Starlette(routes=[Route('/api/knowledge/{path:path}', endpoint, methods=['GET', 'POST', 'DELETE'])])
        class Desktop:
            android_url = 'http://phone.invalid'
            preferences = Preferences()
            async def find_android(self):
                return self.android_url
        self.desktop = Desktop()
        self.desktop.knowledge = self.local
        self.desktop.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False)
        self.sync = KnowledgeSync(self.desktop, self.root / 'sync-state.json')

    async def asyncTearDown(self):
        await self.desktop.http.aclose()
        self.local.close()
        self.phone.close()
        self.temp.cleanup()

    def doc(self, store, title='Owned private memory', **fields):
        return store.save_document({'kind': 'memory', 'title': title, 'content': '# 合成测试\n原始正文', **fields})

    async def test_first_computer_restore_preserves_ids_media_and_phone(self):
        identifier = str(uuid.uuid4())
        media = self.phone.sync_import_attachment(identifier, hashlib.sha256(PNG).hexdigest(), io.BytesIO(PNG), 'owned.png', 'image/png', len(PNG))
        content = '# 随身资料\n![插图](' + media['contentPath'] + ')'
        memory = self.doc(self.phone, content=content)
        skill = self.doc(self.phone, title='Private skill', kind='skill')
        before = self.phone.sync_manifest()
        result = await self.sync.run('download')
        self.assertEqual(result['summary']['downloaded'], 2)
        self.assertEqual(self.phone.sync_manifest(), before)
        self.assertEqual(self.local.read_document(memory['id'])['content'], content)
        self.assertEqual(self.local.read_document(skill['id'])['kind'], 'skill')
        self.assertEqual(self.local.read_attachment(identifier)['sha256'], media['sha256'])
        revision = self.local.read_document(memory['id'])['revision']
        self.assertEqual((await self.sync.run())['summary']['unchanged'], 2)
        self.assertEqual(self.local.read_document(memory['id'])['revision'], revision)

    async def test_bidirectional_edits_conflicts_and_cas_resolution(self):
        document = self.doc(self.phone)
        await self.sync.run()
        self.local.save_document({'id': document['id'], 'content': '电脑离线编辑'})
        self.phone.save_document({'id': document['id'], 'content': '手机在线编辑'})
        result = await self.sync.run()
        self.assertEqual(len(result['conflicts']), 1)
        self.assertEqual(self.local.read_document(document['id'])['content'], '电脑离线编辑')
        self.assertEqual(self.phone.read_document(document['id'])['content'], '手机在线编辑')
        self.phone.save_document({'id': document['id'], 'content': '冲突显示后再次编辑'})
        with self.assertRaises(SyncConflict):
            await self.sync.resolve(document['id'], 'mac')
        await self.sync.run()
        await self.sync.resolve(document['id'], 'android')
        self.assertEqual(self.local.read_document(document['id'])['content'], '冲突显示后再次编辑')
        self.assertEqual((await self.sync.run())['conflicts'], [])
        self.local.save_document({'id': document['id'], 'content': '电脑单端编辑'})
        self.assertEqual((await self.sync.run())['summary']['uploaded'], 1)
        self.assertEqual(self.phone.read_document(document['id'])['content'], '电脑单端编辑')

    async def test_deletion_is_explicit_persistent_and_does_not_resurrect(self):
        document = self.doc(self.phone)
        await self.sync.run()
        self.phone.delete_document(document['id'])
        result = await self.sync.run()
        self.assertEqual(result['summary']['deleted'], 1)
        self.assertEqual(self.local.sync_record(document['id'])['hash'], 'deleted')
        self.local.close()
        self.local = KnowledgeService(self.root / 'mac')
        self.desktop.knowledge = self.local
        self.sync = KnowledgeSync(self.desktop, self.root / 'sync-state.json')
        await self.sync.run()
        self.assertTrue(self.phone.sync_record(document['id'])['deleted'])
        self.assertEqual(self.local.document_stats()['totalDocuments'], 0)

    async def test_delete_vs_edit_conflict_keeps_both_until_choice(self):
        document = self.doc(self.phone)
        await self.sync.run()
        self.phone.delete_document(document['id'])
        self.local.save_document({'id': document['id'], 'content': '删除期间电脑编辑'})
        result = await self.sync.run()
        self.assertTrue(result['conflicts'][0]['android']['deleted'])
        self.assertEqual(self.local.read_document(document['id'])['content'], '删除期间电脑编辑')
        await self.sync.resolve(document['id'], 'mac')
        self.assertEqual(self.phone.read_document(document['id'])['content'], '删除期间电脑编辑')

    async def test_download_mode_never_publishes_local_changes(self):
        remote = self.doc(self.phone)
        local = self.doc(self.local, title='Only on Mac')
        await self.sync.run('download')
        self.assertEqual(self.phone.document_stats()['totalDocuments'], 1)
        self.local.save_document({'id': remote['id'], 'content': 'Mac pending offline edit'})
        await self.sync.run('download')
        self.assertEqual(self.phone.read_document(remote['id'])['content'], remote['content'])
        self.assertEqual(self.local.read_document(local['id'])['title'], 'Only on Mac')

    async def test_attachment_checksum_failure_leaves_document_unpublished(self):
        identifier = str(uuid.uuid4())
        self.phone.sync_import_attachment(identifier, hashlib.sha256(PNG).hexdigest(), io.BytesIO(PNG), 'owned.png', 'image/png', len(PNG))
        document = self.doc(self.phone, content='![](/api/knowledge/attachments/' + identifier + '/content)')
        original = self.phone.read_attachment
        def invalid(identifier):
            value = original(identifier)
            value['sha256'] = '0' * 64
            return value
        with patch.object(self.phone, 'read_attachment', invalid):
            with self.assertRaises(KnowledgeError):
                await self.sync.run()
        self.assertEqual(self.local.document_stats()['totalDocuments'], 0)
        self.assertEqual(self.local.list_attachments()['total'], 0)
        await self.sync.run()
        self.assertEqual(self.local.read_document(document['id'])['id'], document['id'])

    async def test_portable_hash_cas_and_stable_media_import(self):
        document = {'kind': 'skill', 'title': '私有技能😀', 'content': '# 合成记忆\n![图片](/api/knowledge/attachments/00000000-0000-0000-0000-000000000001/content)', 'enabled': True, 'autoLoad': False, 'tags': ['研究', 'personal']}
        self.assertEqual(document_sync_hash(document), '98c9f8bb58a8ffffd02139a1428b8e5d5c3d33b3fe39aec6474bafdbacf92c46')
        identifier = str(uuid.uuid4())
        created = self.local.sync_apply({'id': identifier, 'expectedHash': None, 'record': {'deleted': False, 'document': document}})
        with self.assertRaises(KnowledgeError):
            self.local.sync_apply({'id': identifier, 'expectedHash': None, 'record': {'deleted': True}})
        self.assertEqual(self.local.sync_record(identifier)['hash'], created['hash'])
        self.local.sync_apply({'id': identifier, 'expectedHash': created['hash'], 'record': {'deleted': True}})
        self.local.sync_apply({'id': identifier, 'expectedHash': 'deleted', 'record': {'deleted': False, 'document': document}})
        self.assertFalse(self.local.sync_record(identifier)['deleted'])

    async def test_private_skill_install_protects_manual_edits_and_unrelated_skills(self):
        root = self.root / 'codex-skills'
        root.mkdir()
        unrelated = root / 'public-skill'
        unrelated.mkdir()
        (unrelated / 'SKILL.md').write_text('Unrelated unchanged')
        document = self.doc(self.local, kind='skill', content='---\nname: original-private\ndescription: private helper\n---\n# Instructions\nRead local resources.')
        installed = materialize(self.local, root)
        self.assertEqual(installed['installedSkills'], 1)
        folder = root / ('devhelper-private-' + document['id'].replace('-', ''))
        (folder / 'SKILL.md').write_text('Manual local edit')
        self.local.save_document({'id': document['id'], 'content': 'Phone revision'})
        result = materialize(self.local, root)
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual((folder / 'SKILL.md').read_text(), 'Manual local edit')
        self.local.delete_document(document['id'])
        result = materialize(self.local, root)
        self.assertEqual(result['removedSkills'], 0)
        self.assertEqual((unrelated / 'SKILL.md').read_text(), 'Unrelated unchanged')


if __name__ == '__main__':
    unittest.main()
