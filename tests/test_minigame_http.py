import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from server import Desktop
from starlette.testclient import TestClient


class MiniGameMetadataHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.desktop = Desktop(8876, self.root / 'data', self.root / 'shared')
        self.start = patch.object(self.desktop.discovery, 'start')
        self.stop = patch.object(self.desktop.discovery, 'stop')
        self.start.start()
        self.stop.start()
        self.client = TestClient(self.desktop.app())
        self.client.__enter__()
        self.api = '/device-api/mac/api/knowledge/minigames'

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.start.stop()
        self.stop.stop()
        self.temp.cleanup()

    @staticmethod
    def image(format='PNG'):
        out = io.BytesIO()
        Image.new('RGB', (32, 32), '#15a36d').save(out, format=format)
        return out.getvalue()

    def imported(self, label, stamp=None, icon=False, legacy=False):
        source = self.root / (label + '.zip')
        metadata = {'platform': 'wechat', 'appId': 'wx0123456789abcdef', 'name': label, 'nameSource': 'host-metadata'}
        if stamp is not None:
            if legacy:
                metadata['source'] = [{'modifiedSeconds': stamp // 1000}]
            else:
                metadata['cacheModifiedAt'] = stamp
        if icon:
            metadata.update(iconAvailable=True, iconArchivePath='devhelper-icon.png')
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('manifest.json', json.dumps({'game': metadata}))
            archive.writestr('game.js', 'GameGlobal; wx.createCanvas();')
            if icon:
                archive.writestr('devhelper-icon.png', self.image())
        job = self.desktop.minigames.import_archive(source, 'wechat')
        until = time.monotonic() + 10
        while job['state'] not in ('completed', 'failed', 'cancelled'):
            self.assertLess(time.monotonic(), until, 'Import did not complete')
            time.sleep(.02)
            job = self.desktop.minigames.job(job['id'])
        self.assertEqual(job['state'], 'completed', job)
        return job['result']['game']['id']

    def test_phone_metadata_icon_and_cache_time_survive_import_and_scoped_http(self):
        id = self.imported('Owned metadata fixture', 1700000000000, icon=True)
        response = self.client.get(self.api + '/games/' + id)
        self.assertEqual(response.status_code, 200, response.text)
        game = response.json()['game']
        self.assertEqual(game['name'], 'Owned metadata fixture')
        self.assertEqual(game['cacheModifiedAt'], 1700000000000)
        self.assertTrue(game['iconAvailable'])
        self.assertEqual(game['iconUrl'], self.api + '/games/' + id + '/icon')
        picture = self.client.get(game['iconUrl'])
        self.assertEqual(picture.status_code, 200)
        self.assertEqual(picture.headers['content-type'], 'image/png')
        self.assertEqual(Image.open(io.BytesIO(picture.content)).size, (32, 32))
        head = self.client.head(game['iconUrl'])
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b'')
        self.assertEqual(int(head.headers['content-length']), len(picture.content))

    def test_sort_applies_before_pagination_and_unknown_time_stays_last(self):
        unknown = self.imported('Unknown cache time')
        old = self.imported('Older fixture', 1600000000000, legacy=True)
        new = self.imported('Newer fixture', 1700000000000)
        for sort, expected in [('latest', [new, old, unknown]), ('earliest', [old, new, unknown])]:
            rows = []
            for offset in range(3):
                response = self.client.get(self.api + '/games', params={'sort': sort, 'offset': offset, 'limit': 1})
                self.assertEqual(response.status_code, 200, response.text)
                rows.extend(game['id'] for game in response.json()['games'])
            self.assertEqual(rows, expected)
        self.assertEqual(self.client.get(self.api + '/games?sort=bogus').status_code, 400)

    def test_manual_icon_upload_rejects_non_images_and_survives_refresh(self):
        id = self.imported('Manual icon fixture', 1700000000000)
        route = self.api + '/games/' + id + '/icon'
        self.assertEqual(self.client.get(route).status_code, 404)
        invalid = self.client.post(route, content=b'<svg><script>bad</script></svg>', headers={'Content-Type': 'image/svg+xml'})
        self.assertEqual(invalid.status_code, 400)
        uploaded = self.client.post(route, content=self.image('JPEG'), headers={'Content-Type': 'image/jpeg'})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertEqual(uploaded.json()['iconSource'], 'manual')
        self.desktop.minigames._refresh(id)
        picture = self.client.get(route)
        self.assertEqual(picture.status_code, 200)
        self.assertEqual(picture.headers['content-type'], 'image/png')
        self.assertEqual(Image.open(io.BytesIO(picture.content)).size, (32, 32))
        oversized = self.client.post(route, content=b'x' * (2 * 1024 * 1024 + 1), headers={'Content-Type': 'image/png'})
        self.assertEqual(oversized.status_code, 413)


if __name__ == '__main__':
    unittest.main()
