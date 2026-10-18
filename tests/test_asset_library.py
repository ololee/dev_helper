"""Asset index regressions use generated fixtures, never a private game project."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from desktop.asset_library import AssetLibrary, AssetLibraryError
except ImportError:
    from asset_library import AssetLibrary, AssetLibraryError
from PIL import Image


class AssetLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'game'
        self.source.mkdir()
        self.library = AssetLibrary(self.base / 'index')
        self.project = self.library.register_project(self.source, 'Fixture game')

    def tearDown(self):
        self.library.close()
        self.tmp.cleanup()

    def write(self, name, value):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            path.write_text(value, encoding='utf-8')
        else:
            path.write_bytes(value)
        return path

    def image(self, name='ui/icon.png', color=(240, 20, 20, 255), size=(30, 20)):
        result = io.BytesIO()
        Image.new('RGBA', size, color).save(result, 'PNG')
        return self.write(name, result.getvalue())

    def scan(self, force=False):
        job = self.library.start_scan(self.project['id'], force)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.library.job(job['jobId'])
            if result['state'] not in ('queued', 'running'):
                self.assertEqual(result['state'], 'completed', result)
                return result
            time.sleep(.01)
        self.fail('scan did not finish')

    def items(self):
        return {item['path']: item for item in self.library.list_assets(self.project['id'], limit=500)['items']}

    def test_image_hash_palette_transparency_binary_and_read_only(self):
        image = self.image()
        self.write('opaque.dat', b'\0\1\xff')
        self.write('empty', b'')
        original = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.source.rglob('*') if p.is_file()}
        result = self.scan()
        self.assertEqual(result['progress']['indexed'], 3)
        item = self.items()['ui/icon.png']
        self.assertEqual(item['kind'], 'image')
        self.assertEqual(item['md5'], hashlib.md5(image.read_bytes()).hexdigest())
        self.assertEqual(item['sha256'], hashlib.sha256(image.read_bytes()).hexdigest())
        self.assertEqual((item['image']['width'], item['image']['height']), (30, 20))
        self.assertTrue(item['image']['hasAlpha'])
        self.assertFalse(item['image']['hasTransparency'])
        self.assertFalse(item['image']['semantic'])
        self.assertEqual(item['usageHypotheses'][0]['label'], 'gui')
        self.assertEqual(item['ocr']['status'], 'not-analyzed')
        with self.library.open_file(item['id']) as source:
            self.assertEqual(source.read(), image.read_bytes())
        self.assertEqual(original, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in original})

    def test_incremental_keeps_id_skips_hash_and_prunes_removed(self):
        self.image()
        self.write('to-remove.txt', 'old')
        self.scan()
        first = self.items()
        with patch.object(self.library, '_analyze', side_effect=AssertionError('unchanged source re-read')):
            result = self.scan()
        self.assertEqual(result['progress']['unchanged'], 2)
        self.assertEqual(result['progress']['hashBytes'], 0)
        (self.source / 'to-remove.txt').unlink()
        result = self.scan()
        self.assertEqual(result['progress']['pruned'], 1)
        self.assertEqual(self.items()['ui/icon.png']['id'], first['ui/icon.png']['id'])

    def test_unity_cocos_spine_and_reverse_dependencies(self):
        texture = self.image('texture.png')
        guid = 'a' * 32
        self.write('texture.png.meta', 'fileFormatVersion: 2\nguid: ' + guid + '\n')
        self.write('unit.prefab', '%YAML 1.1\nMonoBehaviour:\n  texture: {fileID: 1, guid: ' + guid + ', type: 3}\nParticleSystem:\n')
        self.write('cocos.png', texture.read_bytes())
        compressed = 'AbCdEfGhIjKlMnOpQrStUv@f9941'
        self.write('cocos.png.meta', json.dumps({'uuid': compressed}))
        self.write('node.prefab', json.dumps([{'__uuid__': compressed}]))
        self.write('spine/skeleton.atlas', 'page.png\nsize: 30, 20\nformat: RGBA8888\nfilter: Linear,Linear\nrepeat: none\nregion\n  rotate: false\n  xy: 0,0\n')
        self.image('spine/page.png')
        self.write('spine/skeleton.json', '{"skeleton":{"spine":"4.1"},"bones":[]}')
        self.scan()
        items = self.items()
        dependencies = self.library.dependencies(items['unit.prefab']['id'])
        self.assertEqual(dependencies['edges'][0]['to'], items['texture.png']['id'])
        self.assertEqual(dependencies['unresolved'], [])
        inverse = self.library.dependencies(items['texture.png']['id'], 'in')
        self.assertEqual(inverse['edges'][0]['from'], items['unit.prefab']['id'])
        dependencies = self.library.dependencies(items['node.prefab']['id'])
        self.assertEqual(dependencies['edges'][0]['to'], items['cocos.png']['id'])
        dependencies = self.library.dependencies(items['spine/skeleton.atlas']['id'])
        self.assertEqual(dependencies['edges'][0]['to'], items['spine/page.png']['id'])
        self.assertEqual(items['spine/skeleton.json']['kind'], 'spine')
        self.assertEqual(items['unit.prefab']['usageHypotheses'][0]['label'], 'vfx')

    def test_dcei_shader_and_gltf_references_do_not_guess_unknown(self):
        self.image('Assets/AssetRaw/UI/Images/Shop/Button.png')
        self.write('Assets/AssetRaw/UI/Xmls/Part.xml', '<Frame/>')
        self.write('Assets/AssetRaw/UI/Xmls/Main.xml', "<DCEI><Frame frameImage='Shop/Button'/><include src='Part'/><Frame frameImage='Missing'/></DCEI>")
        self.write('shaders/a.shader', '#include "common.hlsl"\n#include "vendor.hlsl"')
        self.write('shaders/common.hlsl', 'float4 color;')
        self.write('model/model.gltf', '{"images":[{"uri":"texture.png"},{"uri":"data:application/octet-stream;base64,AA"}]}')
        self.image('model/texture.png')
        self.scan()
        items = self.items()
        deps = self.library.dependencies(items['Assets/AssetRaw/UI/Xmls/Main.xml']['id'])
        self.assertEqual(len(deps['edges']), 2)
        self.assertEqual(deps['unresolved'][0]['value'], 'Missing')
        deps = self.library.dependencies(items['shaders/a.shader']['id'])
        self.assertEqual(len(deps['edges']), 1)
        self.assertEqual(deps['unresolved'][0]['value'], 'shaders/vendor.hlsl')
        self.assertEqual(len(self.library.dependencies(items['model/model.gltf']['id'])['edges']), 1)

    def test_duplicate_unity_guid_is_ambiguous_not_false_dependency(self):
        for name in ('a', 'b'):
            self.write(name + '.mat', '%YAML 1.1\nMaterial:')
            self.write(name + '.mat.meta', 'guid: ' + 'f' * 32)
        self.write('a.prefab', 'm: {guid: ' + 'f' * 32 + '}')
        self.scan()
        deps = self.library.dependencies(self.items()['a.prefab']['id'])
        self.assertEqual(deps['edges'], [])
        self.assertEqual(deps['unresolved'][0]['reason'], 'ambiguous')
        inverse = self.library.dependencies(self.items()['a.mat']['id'], 'in')
        self.assertEqual(inverse['edges'], [])
        self.assertEqual(inverse['unresolved'][0]['reason'], 'ambiguous')

    def test_analysis_limits_do_not_reject_originals(self):
        self.library.register_project(self.source, max_text_bytes=10, max_image_pixels=100)
        image = self.image(size=(20, 20))
        self.write('large.json', '{"data":"123456789123456789"}')
        self.scan()
        items = self.items()
        self.assertEqual(items['ui/icon.png']['image']['reason'], 'pixel-limit')
        self.assertEqual(items['large.json']['structureAnalysis'], 'not-analyzed:text-byte-limit')
        self.assertEqual(self.library.file_path(items['ui/icon.png']['id']), image)

    def test_default_skips_symlink_escape_and_changed_file_download(self):
        self.write('Library/ignored.txt', 'generated')
        image = self.image()
        outside = self.base / 'private'
        outside.mkdir()
        (outside / 'secret').write_text('private')
        (self.source / 'escape').symlink_to(outside, target_is_directory=True)
        (self.source / 'linked.txt').symlink_to(outside / 'secret')
        result = self.scan()
        self.assertEqual(result['progress']['skippedSymlinks'], 2)
        self.assertEqual(result['progress']['skippedDirs'], 1)
        self.assertEqual(len(self.items()), 1)
        item = self.items()['ui/icon.png']
        image.write_bytes(b'changed')
        with self.assertRaises(AssetLibraryError) as ctx:
            self.library.file_path(item['id'])
        self.assertEqual(ctx.exception.status, 409)
        image.unlink()
        image.symlink_to(outside / 'secret')
        with self.assertRaises((AssetLibraryError, OSError)):
            with self.library.open_file(item['id']):
                self.fail('escaped symlink')

    def test_replacing_parent_directory_with_symlink_is_not_read(self):
        self.image()
        self.scan()
        item = self.items()['ui/icon.png']
        (self.source / 'ui').rename(self.base / 'moved-ui')
        (self.source / 'ui').symlink_to(self.base / 'moved-ui', target_is_directory=True)
        with self.assertRaises((AssetLibraryError, OSError)):
            with self.library.open_file(item['id']):
                self.fail('parent symlink read')

    def test_visual_similarity_and_exact_duplicate_across_projects(self):
        original = self.image('red.png')
        self.image('blue.png', color=(0, 0, 255, 255))
        self.write('same.png', original.read_bytes())
        self.scan()
        items = self.items()
        visual = self.library.similar(items['red.png']['id'])
        self.assertEqual(visual['items'][0]['id'], items['same.png']['id'])
        self.assertFalse(visual['approximate'])
        self.assertFalse(visual['semantic'])
        limited = self.library.similar(items['red.png']['id'], candidate_limit=1)
        self.assertTrue(limited['approximate'])
        exact = self.library.similar(items['red.png']['id'], mode='exact')
        self.assertEqual([x['id'] for x in exact['items']], [items['same.png']['id']])

    def test_embedding_dimension_model_finite_validation_and_content_change(self):
        self.image('red.png')
        self.image('blue.png', color=(0, 0, 255, 255))
        self.scan()
        items = self.items()
        red, blue = items['red.png']['id'], items['blue.png']['id']
        result = self.library.import_embeddings(self.project['id'], 'fixture-semantic-v1', [{'assetId': red, 'vector': [1, 0]}, {'assetId': blue, 'vector': [.9, .1]}])
        self.assertEqual(result['dimensions'], 2)
        search = self.library.similar(red, mode='semantic', model='fixture-semantic-v1')
        self.assertEqual(search['items'][0]['id'], blue)
        self.assertEqual(search['model'], 'fixture-semantic-v1')
        for vector in ([1, 2, 3], [float('nan'), 1], [0, 0], [True, 1], [10 ** 1000, 1]):
            with self.assertRaises(AssetLibraryError):
                self.library.import_embeddings(self.project['id'], 'fixture-semantic-v1', [{'assetId': red, 'vector': vector}])
        with self.assertRaises(AssetLibraryError):
            self.library.similar(red, mode='semantic', model='another-model')
        self.image('red.png', color=(0, 255, 0, 255))
        self.scan()
        self.assertEqual(self.library.asset(red)['embeddings'], [])
        self.assertEqual(len(self.library.query_embedding([1, 0], 'fixture-semantic-v1')['items']), 1)

    def test_invalid_embedding_batch_is_atomic(self):
        self.write('a.txt', 'a'); self.write('b.txt', 'b')
        self.scan()
        items = self.items()
        with self.assertRaises(AssetLibraryError):
            self.library.import_embeddings(self.project['id'], 'fixture', [{'assetId': items['a.txt']['id'], 'vector': [1]}, {'assetId': items['b.txt']['id'], 'vector': [1, 0]}])
        self.assertEqual(self.library.asset(items['a.txt']['id'])['embeddings'], [])
        with self.assertRaises(AssetLibraryError):
            self.library.import_embeddings(self.project['id'], 'fixture', [{'assetId': items['a.txt']['id'], 'vector': [1]}] * 1001)

    def test_annotations_preserve_raw_detection_and_mark_changed_content_stale(self):
        self.image()
        self.scan()
        item = self.items()['ui/icon.png']
        self.library.save_annotation(item['id'], {'source': 'ai', 'categories': ['vfx'], 'purpose': 'Predicted effect sprite', 'confidence': .7, 'evidence': 'visual review'})
        result = self.library.save_annotation(item['id'], {'source': 'manual', 'label': 'UI icon'})
        self.assertEqual(result['classification'], item['classification'])
        self.assertEqual(len(result['annotations']), 2)
        self.assertFalse(any(x['stale'] for x in result['annotations']))
        self.image(color=(10, 220, 90, 255))
        self.scan()
        self.assertTrue(all(x['stale'] for x in self.library.asset(item['id'])['annotations']))

    def test_local_apple_ocr_is_selected_without_remote_requests(self):
        self.image('ocr-route.png', (100, 100, 100))
        self.scan()
        item = next(v for v in self.library.list_assets()['items'] if v['name'] == 'ocr-route.png')
        result = {'status': 'analyzed', 'provider': 'apple-vision', 'text': 'Generated fixture', 'localOnly': True}
        with patch('asset_library.sys.platform', 'darwin'), patch('asset_library.shutil.which', return_value='/usr/bin/swiftc'), patch.object(self.library, '_analyze_apple_ocr', return_value=result) as recognize:
            self.assertEqual(self.library.analyze_ocr(item['id']), result)
            recognize.assert_called_once()

    def test_ocr_unavailable_is_not_reported_as_no_text(self):
        self.image()
        self.scan()
        item = self.items()['ui/icon.png']
        with patch('shutil.which', return_value=None):
            result = self.library.analyze_ocr(item['id'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['text'])
        self.assertNotIn('hasText', result)
        self.assertEqual(self.library.asset(item['id'])['ocr']['status'], 'not-analyzed')

    def test_manifest_restart_fts_and_private_paths(self):
        self.write('scripts/character.cs', 'class RareShieldController {}')
        self.scan()
        found = self.library.list_assets(query='RareShieldController')
        self.assertEqual(found['total'], 1)
        self.assertEqual(self.library.list_assets(query='" OR *').get('total'), 0)
        manifest = self.library.export_manifest(self.project['id'])
        self.assertNotIn(str(self.source), json.dumps(manifest))
        self.assertNotIn(str(self.source), json.dumps(self.library.projects()))
        self.assertEqual(self.library.project(self.project['id'], include_root=True)['root'], str(self.source))
        self.library.close()
        self.library = AssetLibrary(self.base / 'index')
        self.assertEqual(self.library.list_assets()['total'], 1)
        self.assertEqual(len(self.library.jobs()), 1)
        self.assertFalse(self.library.capabilities()['modelInference'])


    def test_small_structure_readers_and_updated_analysis_limits(self):
        self.write('mesh.obj', 'mtllib mesh.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        self.write('mesh.mtl', 'newmtl fixture')
        self.write('mesh.gltf', '{"meshes":[{}],"materials":[{}],"animations":[],"nodes":[{},{}]}')
        self.write('sprite.json', '{"skeleton":{"spine":"4.1.2"},"bones":[{"name":"root"}],"animations":{"idle":{},"walk":{}}}')
        self.write('glow.shader', 'Shader "Fixture" {\nProperties {\n_MainTex ("Texture", 2D) = "white" {}\n_Glow ("Glow", Float) = 1\n}\n}')
        import wave
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as writer:
            writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(8000)
            writer.writeframes(b'\0' * 1600)
        self.write('tone.wav', audio.getvalue())
        self.image()
        self.scan()
        items = self.items()
        self.assertEqual(items['mesh.obj']['model']['vertices'], 3)
        self.assertEqual(items['mesh.obj']['model']['faces'], 1)
        self.assertEqual(self.library.dependencies(items['mesh.obj']['id'])['edges'][0]['to'], items['mesh.mtl']['id'])
        self.assertEqual(items['mesh.gltf']['model']['meshes'], 1)
        self.assertEqual(items['mesh.gltf']['model']['nodes'], 2)
        self.assertEqual(items['sprite.json']['spine']['animations'], ['idle', 'walk'])
        self.assertEqual(items['glow.shader']['shader']['properties'], ['_MainTex', '_Glow'])
        self.assertAlmostEqual(items['tone.wav']['audio']['durationSeconds'], .1)
        self.library.register_project(self.source, max_image_pixels=100)
        result = self.scan()
        self.assertGreater(result['progress']['indexed'], 0)
        self.assertEqual(self.items()['ui/icon.png']['image']['reason'], 'pixel-limit')

    def test_file_types_are_categories_not_fake_parsed_models(self):
        for filename in ('unit.fbx', 'mesh.blend', 'light.shader', 'font.ttf', 'fire.mat', 'run.anim', 'music.mp3', 'scene.unity', 'unit.prefab', 'code.lua', 'skeletal.skel.bytes'):
            self.write(filename, b'fixture opaque data')
        self.scan()
        items = self.items()
        self.assertEqual({item['kind'] for item in items.values()}, {'model', 'shader', 'font', 'material', 'animation', 'audio', 'scene', 'prefab', 'code', 'spine'})
        self.assertEqual(items['unit.fbx']['structureAnalysis'], 'not-applicable')
        self.assertEqual(self.library.list_assets(kind='model')['total'], 2)


if __name__ == '__main__':
    unittest.main()
