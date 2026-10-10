"""Generated videos and isolated temporary stores only; no phone or user data."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from desktop import media
    from desktop.knowledge import API, KnowledgeError, KnowledgeService
except ImportError:
    import media
    from knowledge import API, KnowledgeError, KnowledgeService


class VideoPlanTests(unittest.TestCase):
    def setUp(self):
        self.identifier = '80000000-0000-4000-8000-000000000001'
        self.info = media.VideoInfo(240, 160, 4.123, 90, 2, 12.)
        self.fields = {'id': self.identifier, 'startSeconds': 1.123, 'endSeconds': 2.234}

    def test_upright_even_crop_and_literal_argv(self):
        crop = {'x': 7, 'y': 9, 'width': 101, 'height': 123}
        plan = media.VideoPlan.parse({**self.fields, 'crop': crop}, self.info)
        self.assertEqual(plan.requested_crop, crop)
        self.assertEqual(plan.actual_crop, {'x': 7, 'y': 9, 'width': 100, 'height': 122})
        command = plan.command('/ffmpeg', Path('/source $().mp4'), Path('/output.mp4'))
        self.assertIn('/source $().mp4', command)
        self.assertIn('1.123000', command)
        graph = command[command.index('-filter_complex') + 1]
        self.assertIn('transpose=cclock', graph)
        self.assertIn('crop=100:122:7:9:exact=1', graph)
        self.assertEqual(command[command.index('-c:v') + 1], 'libx264')
        self.assertIn('0:a?', command)

    def test_invalid_range_geometry_and_payloads(self):
        for extra in ({'startSeconds': True}, {'endSeconds': float('nan')}, {'startSeconds': 10 ** 400},
                      {'endSeconds': 1.2}, {'endSeconds': 4.2}, {'frameRate': 0}, {'bitrate': 20_000_001},
                      {'crop': {'x': 150, 'y': 0, 'width': 20, 'height': 10}},
                      {'crop': {'x': 0, 'y': 0, 'width': 1, 'height': 10}}, {'unexpected': 1}):
            with self.subTest(extra=extra), self.assertRaises(KnowledgeError):
                media.VideoPlan.parse({**self.fields, **extra}, self.info)

    def test_annotations_validate_boundaries_and_limits(self):
        line = {'type': 'line', 'color': '#ff00ff', 'width': 4, 'points': [{'x': 0, 'y': 0}, {'x': 160, 'y': 240}]}
        plan = media.VideoPlan.parse({**self.fields, 'operations': [line]}, self.info)
        self.assertEqual(len(plan.operations), 1)
        for operation in ({**line, 'color': '#ff00ff;evil'}, {**line, 'width': True}, {**line, 'points': [{'x': -1, 'y': 0}, {'x': 2, 'y': 0}]},
                          {**line, 'text': 'ignored'}, {**line, 'points': [{'x': 0, 'y': 0}]},
                          {'type': 'text', 'color': '#000000', 'width': 1, 'points': [{'x': 1, 'y': 1}], 'text': 'control\0'}):
            with self.subTest(operation=operation), self.assertRaises(KnowledgeError):
                media.VideoPlan.parse({**self.fields, 'operations': [operation]}, self.info)
        with self.assertRaises(KnowledgeError):
            media.VideoPlan.parse({**self.fields, 'operations': [line] * 129}, self.info)

    def test_memory_guard_accounts_for_source_not_crop(self):
        with patch.object(media.os, 'sysconf', side_effect=lambda key: 4096 if key == 'SC_PAGE_SIZE' else 131072):
            media._check_memory(media.VideoInfo(320, 240, 4), False)
            with self.assertRaises(KnowledgeError):
                media._check_memory(media.VideoInfo(8192, 8192, 4), False)

    def test_process_cancel_deadline_and_bounded_output(self):
        stdout, stderr = media._run([sys.executable, '-c', 'import sys;sys.stdout.write("x"*400000);sys.stderr.write("y"*400000)'])
        self.assertEqual(len(stdout), media.OUTPUT_CAPTURE_BYTES)
        self.assertEqual(len(stderr), media.OUTPUT_CAPTURE_BYTES)
        with self.assertRaises(KnowledgeError):
            media._run([sys.executable, '-c', 'import time;time.sleep(30)'], timeout=.1)
        event = threading.Event()
        event.set()
        with self.assertRaises(media._Cancelled):
            media._run([sys.executable, '-c', 'raise RuntimeError()'], cancel=event)


class LocalVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg = media.ffmpeg_executable()
        except KnowledgeError:
            raise unittest.SkipTest('Install project dependencies to run generated FFmpeg fixture tests')
        cls.fixture_directory = tempfile.TemporaryDirectory()
        cls.source_path = Path(cls.fixture_directory.name) / 'quadrants.mp4'
        # 2.4 seconds / 48 frames, with different colors after the first second.
        # Synthetic pixels + synthetic two audio tones contain no private media.
        frames = bytearray()
        colors = [(230, 20, 20), (20, 230, 20), (20, 20, 230), (230, 230, 20)]
        for frame in range(48):
            offset = 0 if frame < 20 else 2
            for y in range(48):
                for x in range(64):
                    quadrant = (2 if y >= 24 else 0) + (1 if x >= 32 else 0)
                    frames.extend(colors[(quadrant + offset) % 4])
        args = [cls.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-y',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-video_size', '64x48', '-framerate', '20', '-i', 'pipe:0',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000:duration=2.4',
                '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=16000:duration=2.4',
                '-map', '0:v:0', '-map', '1:a:0', '-map', '2:a:0', '-t', '2.4',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-g', '20', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-movflags', '+faststart', str(cls.source_path)]
        subprocess.run(args, input=bytes(frames), capture_output=True, check=True, timeout=20)
        cls.rotated = {}
        for angle in (90, 180, 270):
            output = Path(cls.fixture_directory.name) / f'rotation-{angle}.mp4'
            subprocess.run([cls.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-y',
                            '-display_rotation:v:0', str(angle), '-i', str(cls.source_path), '-map', '0', '-c', 'copy', str(output)],
                           capture_output=True, check=True, timeout=20)
            cls.rotated[angle] = output

    @classmethod
    def tearDownClass(cls):
        cls.fixture_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = KnowledgeService(Path(self.directory.name) / 'store')
        with self.source_path.open('rb') as stream:
            self.source = self.service.import_attachment(stream, 'owned.mp4', 'video/mp4')

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def wait(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = self.service.video_editor.status({'jobId': job['jobId']})
            if value['state'] in media.TERMINAL:
                # Pin release and temp cleanup finish before a new job is started.
                with self.service.video_editor.lock:
                    thread = self.service.video_editor.jobs[job['jobId']].thread
                thread.join(timeout=2)
                return value
            time.sleep(.02)
        self.fail('Generated video task did not finish')

    def pixel(self, path, width, x, y):
        result = subprocess.run([self.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-i', str(path),
                                 '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'], capture_output=True, check=True, timeout=10)
        offset = (y * width + x) * 3
        return tuple(result.stdout[offset:offset + 3])

    def assert_color(self, actual, expected):
        self.assertEqual(len(actual), 3)
        self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), 35)

    def test_info_reports_rotated_geometry_and_real_capabilities(self):
        value = self.service.media_info(self.source['id'])
        self.assertTrue(value['editingSupported'])
        self.assertTrue(value['videoEditingCapabilities']['annotations'])
        self.assertFalse(value['frameRateTrusted'])
        self.assertEqual(value['audioTracks'], 2)
        self.assertAlmostEqual(value['durationSeconds'], 2.4, places=3)
        with self.rotated[90].open('rb') as stream:
            rotated = self.service.import_attachment(stream, 'rotation.mp4', 'video/mp4')
        value = self.service.media_info(rotated['id'])
        self.assertEqual((value['width'], value['height']), (64, 48))
        self.assertEqual((value['displayWidth'], value['displayHeight']), (48, 64))

    def test_accurate_seek_crop_export_preserves_original_and_all_audio(self):
        before = self.source_path.read_bytes()
        job = self.service.handle('POST', API + '/media/video-edit/start', data={
            'id': self.source['id'], 'startSeconds': 1.25, 'endSeconds': 1.85,
            'crop': {'x': 3, 'y': 5, 'width': 27, 'height': 17}, 'frameRate': 20})
        self.assertEqual(job.status, 200)
        result = self.wait(job.data)
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertNotEqual(result['attachment']['id'], self.source['id'])
        self.assertEqual((result['width'], result['height']), (26, 16))
        self.assertTrue(result['cropAdjusted'])
        self.assertAlmostEqual(result['actualDurationSeconds'], .6, delta=.06)
        self.assertEqual(result['audioTracks'], 2)
        self.assertTrue(result['audioPreserved'])
        _, source = self.service._attachment(self.source['id'])
        self.assertEqual(source.read_bytes(), before)
        _, output = self.service._attachment(result['attachment']['id'])
        self.assert_color(self.pixel(output, 26, 10, 8), (20, 20, 230))
        self.assertTrue(self.service.delete_attachment(self.source['id'])['deleted'])

    def test_all_right_angle_rotations_match_browser_upright_roi(self):
        # FFmpeg/browser display-matrix 90 rotates counterclockwise. The upright
        # top-left therefore comes from original top-right, bottom-right, etc.
        expected = {90: (20, 230, 20), 180: (230, 230, 20), 270: (20, 20, 230)}
        for angle, color in expected.items():
            with self.subTest(angle=angle):
                with self.rotated[angle].open('rb') as stream:
                    source = self.service.import_attachment(stream, f'rotation-{angle}.mp4', 'video/mp4')
                result = self.wait(self.service.video_editor.start({'id': source['id'], 'startSeconds': .1, 'endSeconds': .6,
                    'crop': {'x': 3, 'y': 3, 'width': 14, 'height': 14}, 'frameRate': 20}))
                self.assertEqual(result['state'], 'completed', result.get('error'))
                _, output = self.service._attachment(result['attachment']['id'])
                self.assertEqual(media.probe_video(output).rotation, 0)
                self.assert_color(self.pixel(output, 14, 5, 5), color)

    def test_overlay_applied_before_crop_and_text_renders(self):
        line = {'type': 'line', 'color': '#ff00ff', 'width': 6,
                'points': [{'x': 8, 'y': 16}, {'x': 40, 'y': 16}]}
        text = {'type': 'text', 'color': '#ffffff', 'width': 1, 'text': '注释 T', 'fontSize': 12,
                'points': [{'x': 4, 'y': 42}]}
        result = self.wait(self.service.video_editor.start({'id': self.source['id'], 'startSeconds': .1, 'endSeconds': .6,
            'crop': {'x': 8, 'y': 8, 'width': 32, 'height': 36}, 'operations': [line, text], 'frameRate': 20}))
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertEqual(result['operationsApplied'], 2)
        _, output = self.service._attachment(result['attachment']['id'])
        self.assert_color(self.pixel(output, 32, 8, 8), (255, 0, 255))
        from PIL import Image
        plan = media.VideoPlan.parse({'id': self.source['id'], 'startSeconds': .1, 'endSeconds': .6, 'operations': [text]},
                                     media.VideoInfo(64, 48, 2.4))
        overlay = Path(self.directory.name) / 'owned-overlay.png'
        media.render_overlay(plan, overlay)
        with Image.open(overlay) as image:
            self.assertIsNotNone(image.getbbox())

    def test_fast_trim_and_mcp_return_an_immutable_copy(self):
        result = self.service.call_tool('knowledge_trim_video_attachment', {'id': self.source['id'], 'startSeconds': .25, 'endSeconds': .85})
        self.assertFalse(result['isError'], result)
        result = result['structuredContent']
        self.assertFalse(result['precise'])
        self.assertEqual(result['mode'], 'stream_copy')
        self.assertTrue(result['audioPreserved'])
        self.assertEqual(result['audioTracks'], 2)
        self.assertNotEqual(result['id'], self.source['id'])
        self.assertEqual(self.service.list_attachments()['total'], 2)
        self.assertEqual(len({x['name'] for x in self.service.tool_specs()}), len(self.service.tool_specs()))

    def test_cancel_pins_source_blocks_another_job_and_does_not_publish(self):
        args = {'id': self.source['id'], 'startSeconds': .1, 'endSeconds': .6}
        with patch.object(media.VideoPlan, 'command', return_value=[sys.executable, '-c', 'import time;time.sleep(30)']):
            job = self.service.video_editor.start(args)
            with self.assertRaises(KnowledgeError) as busy:
                self.service.video_editor.start(args)
            self.assertEqual(busy.exception.status, 409)
            with self.assertRaises(KnowledgeError) as pinned:
                self.service.delete_attachment(self.source['id'])
            self.assertEqual(pinned.exception.status, 409)
            self.assertFalse(self.service.list_resources({})['items'][0]['canDelete'])
            response = self.service.handle('POST', API + '/media/video-edit/cancel', data={'jobId': job['jobId']})
            self.assertEqual(response.status, 200)
            result = self.wait(job)
        self.assertEqual(result['state'], 'cancelled')
        self.assertNotIn('attachment', result)
        self.assertEqual(self.service.list_attachments()['total'], 1)
        self.assertTrue(self.service.delete_attachment(self.source['id'])['deleted'])

    def test_failure_releases_pin_and_close_drains_active_worker(self):
        args = {'id': self.source['id'], 'startSeconds': .1, 'endSeconds': .6}
        with patch.object(media.VideoPlan, 'command', return_value=[sys.executable, '-c', 'raise SystemExit(2)']):
            result = self.wait(self.service.video_editor.start(args))
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(self.service._media_pins)
        with patch.object(media.VideoPlan, 'command', return_value=[sys.executable, '-c', 'import time;time.sleep(30)']):
            job = self.service.video_editor.start(args)
            self.service.video_editor.close()
            self.assertEqual(self.service.video_editor.status({'jobId': job['jobId']})['state'], 'cancelled')
        self.assertFalse(self.service._media_pins)
        self.assertTrue(self.service.delete_attachment(self.source['id'])['deleted'])

    def test_invalid_http_and_mcp_requests_leave_no_job_or_pin(self):
        for fields in ({'id': self.source['id'], 'startSeconds': .1, 'endSeconds': .11},
                       {'id': self.source['id'], 'startSeconds': 0, 'endSeconds': 20},
                       {'id': self.source['id'], 'startSeconds': 0, 'endSeconds': .5, 'path': '/private/file'}):
            response = self.service.handle('POST', API + '/media/video-edit/start', data=fields)
            self.assertEqual(response.status, 400)
            self.assertFalse(self.service._media_pins)
        error = self.service.call_tool('knowledge_start_video_attachment_edit', {'id': self.source['id']})
        self.assertTrue(error['isError'])
        self.assertIsNone(self.service.video_editor.latest())
        response = self.service.handle('POST', API + '/media/video-edit/status', data={'jobId': '80000000-0000-4000-8000-000000000002'})
        self.assertEqual(response.status, 404)

    def test_worker_start_failure_rolls_back_reservation_and_pin(self):
        real_start = threading.Thread.start

        def start(thread):
            if thread.name == 'desktop-video-editor':
                raise RuntimeError('owned injected worker failure')
            return real_start(thread)

        with patch.object(threading.Thread, 'start', start), self.assertRaises(RuntimeError):
            self.service.video_editor.start({'id': self.source['id'], 'startSeconds': .1, 'endSeconds': .6})
        self.assertFalse(self.service._media_pins)
        self.assertIsNone(self.service.video_editor.active)
        self.assertIsNone(self.service.video_editor.latest())

    def test_published_copy_remains_completed_if_temporary_cleanup_fails(self):
        real_temporary = tempfile.TemporaryDirectory

        class FailingCleanup(real_temporary):
            def __exit__(self, *args):
                super().__exit__(*args)
                raise OSError('owned injected cleanup failure')

        with patch.object(media.tempfile, 'TemporaryDirectory', FailingCleanup):
            result = self.wait(self.service.video_editor.start({'id': self.source['id'], 'startSeconds': .1, 'endSeconds': .6}))
        self.assertEqual(result['state'], 'completed')
        self.assertIn('cleanupWarning', result)
        self.assertEqual(self.service.list_attachments()['total'], 2)
        self.assertFalse(self.service._media_pins)


if __name__ == '__main__':
    unittest.main()
