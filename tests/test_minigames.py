"""Untrusted index boundaries plus an encrypted Cocos -> export -> Spine round trip."""
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import zipfile

from knowledge import KnowledgeService
from minigame_formats import derive_key, rc4, unpack_package, unpack_zip, inspect_tree, FormatError
from minigames import MiniGameService


def tpkg(entries):
    seed='fixture-seed'; key=derive_key(seed.encode())
    text=json.dumps({'__ttks':seed}).encode(); block=struct.pack('<ii',len(text)+4,0)+text
    header=b'TPKG'+struct.pack('<ii',2,len(block))+block+struct.pack('<i',len(entries))
    offset=len(header)+sum(4+len(name.encode())+8 for name,_ in entries); table=bytearray(); payload=bytearray()
    for name,raw in entries:
        encoded=name.encode(); table.extend(struct.pack('<i',len(encoded))+rc4(encoded+struct.pack('<ii',offset,len(raw)),key))
        out=bytearray(raw)
        if name.endswith(('.js','.json')):
            for pos in range(0,len(out),8192): out[pos:pos+1024]=rc4(bytes(out[pos:pos+1024]),key)
        else: out[:64]=rc4(bytes(out[:64]),key)
        payload.extend(out); offset+=len(raw)
    return header+table+payload


def wxapkg(entries):
    """Synthetic standard WXAPKG with Android's slash-prefixed directory entries."""
    index_size=4+sum(4+len(name.encode('utf-8'))+8 for name,_ in entries)
    offset=14+index_size; table=bytearray(struct.pack('>I',len(entries))); payload=bytearray()
    for name,raw in entries:
        encoded=name.encode('utf-8')
        table.extend(struct.pack('>I',len(encoded))+encoded+struct.pack('>II',offset,len(raw)))
        payload.extend(raw); offset+=len(raw)
    return b'\xbe'+struct.pack('>III',0,index_size,len(payload))+b'\xed'+table+payload


def wait(service,job):
    end=time.monotonic()+8
    while job['state'] in ('queued','running'):
        if time.monotonic()>end: raise AssertionError('job timed out')
        time.sleep(.02); job=service.job(job['id'])
    return job


class FormatsTests(unittest.TestCase):
    def test_wechat_main_game_entries_and_wrapper_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'main.wxapkg'; output=root/'unpacked/packages/cache/main'
            source.write_bytes(wxapkg([('/game.js',b'Laya.init(100,100);'),('/game.json',b'{"deviceOrientation":"landscape"}')]))
            original=source.read_bytes(); unpack_package(source,output)
            self.assertTrue((output/'game.js').is_file())
            analysis=inspect_tree(root/'unpacked')
            self.assertEqual(analysis['gameType'],'game')
            self.assertEqual(analysis['engine'],'laya')
            self.assertTrue(any(item['match']=='小游戏主包入口' for item in analysis['gameTypeEvidence']))
            self.assertEqual(analysis['gameTypeIssues'],[])
            self.assertEqual(source.read_bytes(),original)

    def test_wechat_subpackage_is_game_with_partial_issue(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for index,prefix in enumerate(('subpackages/chapter','wasmcode')):
                source=root/f'chapter-{index}.wxapkg'; output=root/f'out-{index}'
                source.write_bytes(wxapkg([('/'+prefix+'/game.js',b'cc.game.run();'),('/'+prefix+'/texture.bin',b'fixture')]))
                unpack_package(source,output); analysis=inspect_tree(output)
                self.assertEqual(analysis['gameType'],'game')
                self.assertEqual(analysis['engine'],'cocos')
                self.assertEqual(analysis['gameTypeIssues'][0]['code'],'game_package_partial')
                self.assertEqual(analysis['gameTypeEvidence'][0]['match'],'小游戏分包入口')
            resource=root/'resource.wxapkg'
            resource.write_bytes(wxapkg([('/subpackages/chapter/game.json',b'{}')]))
            unpack_package(resource,root/'resource')
            self.assertEqual(inspect_tree(root/'resource')['gameType'],'unconfirmed')

    def test_wechat_ordinary_miniprogram_requires_paired_entry_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for index,companion in enumerate(('app.json','page-frame.html','page-frame.js')):
                source=root/f'ordinary-{index}.wxapkg'; output=root/f'out-{index}'
                source.write_bytes(wxapkg([('/app-service.js',b'App({});'),('/'+companion,b'{}'),('/pages/game.js',b'Page({});')]))
                unpack_package(source,output)
                self.assertEqual(inspect_tree(output)['gameType'],'miniprogram')
            separated=root/'separated'; (separated/'a').mkdir(parents=True); (separated/'b').mkdir()
            (separated/'a/app-service.js').write_text('App({});')
            (separated/'b/app.json').write_text('{}')
            (separated/'b/page-frame.html').write_text('<html></html>')
            self.assertEqual(inspect_tree(separated)['gameType'],'unconfirmed')
            lone_page=root/'lone/pages'; lone_page.mkdir(parents=True)
            (lone_page/'game.js').write_text('Page({});')
            self.assertEqual(inspect_tree(root/'lone')['gameType'],'unconfirmed')

    def test_wechat_plugin_and_unrelated_manifest_cannot_claim_game_type(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'plugin.wxapkg'
            source.write_bytes(wxapkg([('/plugin.js',b'module.exports = {};'),('/plugin.json',b'{}'),
                ('/project.config.json',b'{"compileType":"game","projectname":"generic-project"}'),
                ('/unrelated/manifest.json',b'{"gameType":"game","type":"game"}')]))
            unpack_package(source,root/'out'); analysis=inspect_tree(root/'out')
            self.assertEqual(analysis['gameType'],'unconfirmed')
            self.assertEqual(analysis['gameTypeEvidence'],[])
            self.assertEqual(analysis['name'],'')

    def test_conflicting_metadata_remains_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'config.json').write_text(json.dumps({'appId':'wx11111111','gameName':'名称甲'}))
            (root/'metadata.json').write_text(json.dumps({'appId':'wx22222222','gameName':'名称乙'}))
            (root/'game.js').write_text('/* game entry */')
            analysis=inspect_tree(root)
            self.assertEqual(analysis['appId'],''); self.assertEqual(analysis['name'],'')
            self.assertEqual({issue['code'] for issue in analysis['metadataIssues']},{'appid_conflict','game_name_conflict'})
            self.assertEqual(analysis['gameType'],'game')

    def test_tpkg_long_text_and_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'test.pkg'
            entries=[('cocos-js/cc.js',b'cc.game.run();'+b'0123456789'*2300),('img.png',bytes(range(256))*43)]
            source.write_bytes(tpkg(entries)); before=source.read_bytes()
            result=unpack_package(source,root/'out')
            self.assertEqual(result['files'],2)
            for name,raw in entries: self.assertEqual((root/'out'/name).read_bytes(),raw)
            self.assertEqual(source.read_bytes(),before)

    def test_invalid_indices_cannot_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for n,entries in enumerate(([('../outside',b'x')],[('x',b'a'),('X',b'b')],[('file',b'a'),('file/nested',b'b')])):
                source=root/f'{n}.pkg'; source.write_bytes(tpkg(entries))
                with self.assertRaises(FormatError): unpack_package(source,root/f'out{n}')
                self.assertFalse((root/f'out{n}').exists())
            self.assertFalse((root.parent/'outside').exists())

    def test_unknown_version_and_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'bad.pkg'
            for raw in (b'TPKG'+struct.pack('<ii',3,0),tpkg([('x',b'x')])[:-1]):
                source.write_bytes(raw)
                with self.assertRaises(FormatError): unpack_package(source,root/'out')
                self.assertFalse((root/'out').exists())

    def test_wechat_plain_and_encrypted(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'test.wxapkg'; name=b'/game.js'; raw=b'Laya.init(100,100);'
            index=struct.pack('>II',1,len(name))+name+struct.pack('>II',14+4+4+len(name)+8,len(raw))
            source.write_bytes(b'\xbe'+struct.pack('>III',0,len(index),len(raw))+b'\xed'+index+raw)
            unpack_package(source,root/'out'); self.assertEqual((root/'out/game.js').read_bytes(),raw)
            source.write_bytes(b'V1MMWXencrypted')
            with self.assertRaises(FormatError): unpack_package(source,root/'other')

    def test_wasm_is_not_unity_and_tuanjie_is_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'game.wasm').write_bytes(b'\x00asm')
            self.assertEqual(inspect_tree(root)['engine'],'unknown')
            (root/'README.txt').write_text('Tuanjie 2022.3.61t9')
            self.assertEqual(inspect_tree(root)['engine'],'unknown')
            (root/'game.bundle').write_bytes(b'UnityFS\x006\x002022.3.61t9\x00')
            result=inspect_tree(root); self.assertEqual(result['engine'],'tuanjie'); self.assertEqual(result['engineVersion'],'2022.3.61t9')

    def test_zip_traversal_does_not_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'bad.zip'
            with zipfile.ZipFile(source,'w') as z:z.writestr('../escape.js','evil')
            with self.assertRaises(FormatError): unpack_zip(source,root/'out')
            self.assertFalse((root/'out').exists())


class LibraryTests(unittest.TestCase):
    def test_partial_unity_export_keeps_output_and_reports_failed_collections(self):
        class PartialAdapter:
            def export(self,input_paths,output_dir,variant,**options):
                (output_dir/'recovered.asset').write_bytes(b'actual recovered fixture')
                return {'status':'partial','failed_collections':2,'asset_count':3,'file_count':1,
                        'output_dir':str(output_dir),'manifest_path':str(output_dir/'export-manifest.json')}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); service=MiniGameService(root/'library',assetripper=PartialAdapter()); source=root/'source.zip'
            with zipfile.ZipFile(source,'w') as archive: archive.writestr('fixture.bundle',b'UnityFS\x006\x002022.3.61f1\x00')
            try:
                game=wait(service,service.import_archive(source))['result']['gameId']
                exported=wait(service,service.export({'gameId':game,'mode':'unity-project','notify':False}))
                self.assertEqual(exported['state'],'completed',exported)
                self.assertIn('2 个集合未能导出',exported['message'])
                manifest=exported['result']['export']
                self.assertEqual(manifest['detail']['assetripper']['status'],'partial')
                issue=next(item for item in manifest['detail']['issues'] if item.get('code')=='unity_partial_export')
                self.assertEqual(issue['failedCollections'],2)
                self.assertEqual(service.export_file(manifest['id'],'recovered.asset').read_bytes(),b'actual recovered fixture')
                with zipfile.ZipFile(service.export_file(manifest['id'])) as archive:
                    self.assertIsNone(archive.testzip())
                    archived=json.loads(archive.read('devhelper-export.json'))
                    self.assertEqual(archived['detail']['issues'][0]['failedCollections'],2)
            finally: service.close()

    def test_queued_import_cancel_removes_pending_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); service=MiniGameService(root/'library'); source=root/'source.zip'
            with zipfile.ZipFile(source,'w') as archive: archive.writestr('fixture.bin',b'original fixture')
            original=source.read_bytes(); release=threading.Event(); entered=[threading.Event(),threading.Event()]
            try:
                for signal in entered:
                    def hold(event,report,signal=signal):
                        signal.set()
                        if not release.wait(8): raise AssertionError('review fixture timed out')
                        return {}
                    service._submit('test-hold',hold)
                self.assertTrue(all(signal.wait(2) for signal in entered))
                queued=service.import_archive(source)
                self.assertEqual(service.job(queued['id'])['state'],'queued')
                self.assertEqual(len(list(service.game_root.glob('.pending-*'))),1)
                service.cancel(queued['id']); release.set()
                self.assertEqual(wait(service,queued)['state'],'cancelled')
                service.close()
                self.assertEqual(list(service.game_root.glob('.pending-*')),[])
                self.assertEqual(service.list_games()['games'],[])
                self.assertEqual(source.read_bytes(),original)
            finally:
                release.set()
                if not service.closed: service.close()

    def test_spine_pack_only_contains_ready_skeleton_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); knowledge=KnowledgeService(root/'knowledge'); service=MiniGameService(root/'library',knowledge)
            source=root/'source.zip'
            skeleton=json.dumps({'skeleton':{'spine':'4.2.72'},'bones':[{'name':'root'}],'animations':{}})
            texture=b'\x89PNG\r\n\x1a\n'+bytes(4)+b'IHDR'+bytes(16)
            with zipfile.ZipFile(source,'w',compression=zipfile.ZIP_STORED) as archive:
                for name in ('hero','enemy'):
                    archive.writestr(name+'.json',skeleton)
                    archive.writestr(name+'.atlas',name+'.png\nsize: 1,1\nfilter: Linear,Linear\nregion\n  bounds: 0,0,1,1\n')
                    archive.writestr(name+'.png',texture)
                archive.writestr('incomplete.json',skeleton)
                archive.writestr('unrelated.json','{"data":"'+'a'*(17*1024**2)+'"}')
                archive.writestr('unrelated.png',texture)
            try:
                game=wait(service,service.import_archive(source))['result']['gameId']
                exported=wait(service,service.export({'gameId':game,'mode':'assets','notify':False}))
                self.assertEqual(exported['state'],'completed',exported)
                export_id=exported['result']['exportId']
                opened=service.open_spine(export_id,{'skeleton':'cached/hero.json'})
                expected={'cached/'+name+extension for name in ('hero','enemy') for extension in ('.json','.atlas','.png')}
                with zipfile.ZipFile(service.export_root/export_id/'spine.zip') as archive:
                    self.assertEqual(set(archive.namelist()),expected)
                self.assertEqual({item['skeleton'] for item in opened['bundle']['skeletons']},{'cached/hero.json','cached/enemy.json'})
                again=service.open_spine(export_id,{'skeleton':'cached/enemy.json'})
                self.assertEqual(opened['bundle']['id'],again['bundle']['id'])
                self.assertIn('enemy.json',again['openUrl'])
                with self.assertRaisesRegex(ValueError,'缺少有效图集或纹理'):
                    service.open_spine(export_id,{'skeleton':'cached/incomplete.json'})
            finally: service.close(); knowledge.close()

    def test_phone_raw_cocos_export_and_existing_spine(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); knowledge=KnowledgeService(root/'knowledge'); service=MiniGameService(root/'minigames',knowledge)
            original=root/'phone.zip'
            skel=json.dumps({'skeleton':{'spine':'4.2.72'},'bones':[{'name':'root'}],'animations':{}}).encode()
            png=b'\x89PNG\r\n\x1a\n'+bytes(4)+b'IHDR'+bytes(16)
            package=tpkg([('game.js',b'cc.game.run(); // Cocos Creator 3.8.8'),('assets/hero.json',skel),('assets/hero.atlas',b'hero.png\nsize: 1,1\nfilter: Linear,Linear\nregion\n  bounds: 0,0,1,1\n'),('assets/hero.png',png)])
            with zipfile.ZipFile(original,'w') as archive:
                archive.writestr('manifest.json',json.dumps({'game':{'appId':'tt1234567890abcd','name':'测试游戏','platform':'douyin'}}))
                archive.writestr('payload/cache/game.pkg',package)
            try:
                imported=wait(service,service.import_archive(original)); self.assertEqual(imported['state'],'completed',imported)
                game=imported['result']['game']; self.assertEqual(game['appId'],'tt1234567890abcd')
                self.assertEqual(game['cacheLabel'],'game.pkg')
                self.assertEqual(game['gameType'],'unconfirmed')
                exported=wait(service,service.export({'gameId':game['id'],'mode':'assets','notify':False})); self.assertEqual(exported['state'],'completed',exported)
                self.assertEqual(service.get_game(game['id'])['game']['engine'],'cocos')
                self.assertEqual(service.get_game(game['id'])['game']['gameType'],'game')
                self.assertTrue(exported['result']['export']['spine'])
                with zipfile.ZipFile(service.export_file(exported['result']['exportId'])) as z:
                    self.assertTrue(any(n.endswith('hero.json') for n in z.namelist()))
                    self.assertFalse(any(n.endswith('game.js') for n in z.namelist()))
                opened=service.open_spine(exported['result']['exportId'])
                self.assertEqual(opened['bundle']['skeletons'][0]['version'],'4.2.72')
                self.assertIn('entry=',opened['openUrl'])
                self.assertTrue(original.exists())
            finally: service.close(); knowledge.close()

    def test_name_edit_preserves_appid(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); service=MiniGameService(root/'library'); source=root/'data.zip'
            with zipfile.ZipFile(source,'w') as z:z.writestr('data.bin',b'plain')
            try:
                job=wait(service,service.import_archive(source)); id=job['result']['gameId']
                self.assertEqual(service.metadata(id,{'name':'自己的名称'})['appId'],'')
            finally:service.close()

    def test_cache_list_sandbox_uri_restores_cdn_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); service=MiniGameService(root/'library'); source=root/'data.zip'
            mapping={'files':{'https://cdn.example/assets/main/native/hero.png':{'url':'ttfile://user/gamecaches/main/hashed.png'}}}
            with zipfile.ZipFile(source,'w') as z:
                z.writestr('payload/gamecaches/cacheList.json',json.dumps(mapping))
                z.writestr('payload/gamecaches/main/hashed.png',b'image bytes')
                z.writestr('payload/game.js',b'cc.game.run();')
            try:
                job=wait(service,service.import_archive(source)); id=job['result']['gameId']
                exported=wait(service,service.export({'gameId':id,'mode':'assets','notify':False}))
                self.assertEqual(exported['state'],'completed',exported)
                self.assertEqual(service.export_file(exported['result']['exportId'],'cdn-assets/assets/main/native/hero.png').read_bytes(),b'image bytes')
            finally:service.close()


if __name__=='__main__':unittest.main()
