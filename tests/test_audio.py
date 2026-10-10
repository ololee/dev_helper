"""Only generated tones and temporary stores. Never inspect personal recordings."""
import array
import hashlib
import io
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

try:
    from desktop import audio
    from desktop.knowledge import API, KnowledgeError, KnowledgeService
except ImportError:
    import audio
    from knowledge import API, KnowledgeError, KnowledgeService


SOURCE_ID = '80000000-0000-4000-8000-000000000001'


def owned_recording():
    source = io.BytesIO()
    with wave.open(source, 'wb') as stream:
        stream.setparams((2, 2, 16000, 0, 'NONE', 'not compressed'))
        pcm = bytearray()
        for index in range(64000):
            level = (0, 4096, 16384, 30000)[index // 16000]
            sample = round(level * math.sin(2 * math.pi * 400 * index / 16000))
            # A mono downmix would cancel this generated stereo signal.
            pcm.extend(struct.pack('<hh', sample, -sample))
        stream.writeframes(pcm)
    return source.getvalue()


class AudioPlanTests(unittest.TestCase):
    def setUp(self):
        self.info = audio.AudioInfo(10000.123, 16000, 2, 'stereo')
        self.fields = {'id': SOURCE_ID, 'startSeconds': 3600.123, 'endSeconds': 3601.234}

    def test_long_precise_times_channels_literal_argv_and_name(self):
        plan = audio.AudioPlan.parse({**self.fields, 'name': '合成测试.wav'}, self.info)
        args = plan.command('/ffmpeg', Path('/synthetic $() source.wav'), Path('/output.m4a'))
        self.assertIn('3600.123000', args)
        self.assertIn('1.111000', args)
        self.assertIn('/synthetic $() source.wav', args)
        self.assertEqual(args[args.index('-ar') + 1], '16000')
        self.assertEqual(args[args.index('-ac') + 1], '2')
        self.assertEqual(args[args.index('-c:a') + 1], 'aac')
        self.assertEqual(plan.name, '合成测试.m4a')

    def test_invalid_numbers_names_unknown_fields_and_unsupported_aac_rate(self):
        for extra in ({'startSeconds': True}, {'endSeconds': float('nan')}, {'startSeconds': 10 ** 400},
                      {'endSeconds': 3600.13}, {'endSeconds': 10001}, {'name': '../private.m4a'},
                      {'name': 'bad\x00.m4a'}, {'name': ''}, {'name': False}, {'format': 'wav'}):
            with self.subTest(extra=extra), self.assertRaises(KnowledgeError):
                audio.AudioPlan.parse({**self.fields, **extra}, self.info)
        with self.assertRaises(KnowledgeError):
            audio.AudioPlan.parse(self.fields, audio.AudioInfo(10000.123, 192000, 2))

    def test_peak_reducer_keeps_cross_channel_peak_and_handles_split_frames(self):
        envelope = audio.PeakEnvelope(1, 4, 2, 4)
        data = struct.pack('<hhhhhhhh', 1000, -1000, 12000, -12000, -32768, 32767, 0, 0)
        for chunk in (data[:1], data[1:5], data[5:11], data[11:]):
            envelope.append(chunk)
        self.assertEqual(envelope.frames, 4)
        self.assertFalse(envelope.pending)
        self.assertEqual(envelope.peaks, [1000 / 32768, 12000 / 32768, 1., 0.])

    def test_channel_layout_counts(self):
        for layout, channels in (('mono', 1), ('stereo', 2), ('5.1(side)', 6), ('7.1(wide)', 8), ('2 channels (FL+FR)', 2)):
            self.assertEqual(audio._layout_channels(layout), channels)
        with self.assertRaises(KnowledgeError):
            audio._layout_channels('unsupported')

    def test_padding_is_drained_without_contaminating_last_bucket_and_work_is_bounded(self):
        envelope = audio.PeakEnvelope(1, 4, 2, 4)
        envelope.append(struct.pack('<hhhhhhhh', 1000, -1000, 2000, -2000, 3000, -3000, 4000, -4000))
        envelope.append(struct.pack('<hh', -32768, 32767) * 1024)
        self.assertEqual(envelope.frames, 4)
        self.assertEqual(envelope.decoded_frames, 1028)
        self.assertEqual(envelope.peaks[-1], 4000 / 32768)
        with self.assertRaisesRegex(KnowledgeError, '工作量'):
            envelope.append(b'\x00' * audio.PCM_PADDING_ALLOWANCE_BYTES)

    def test_fractional_sample_duration_keeps_last_sample_inside_exclusive_end(self):
        envelope = audio.PeakEnvelope(.0016, 16000, 1, 4)
        self.assertEqual(envelope.expected_frames, 26)
        envelope.append(struct.pack('<h', 0) * 25 + struct.pack('<h', 12000))
        self.assertEqual(envelope.frames, 26)
        self.assertEqual(envelope.peaks[-1], 12000 / 32768)

    def test_streaming_pcm_process_deadline_cancel_and_bad_pcm(self):
        event = threading.Event()
        envelope = audio.PeakEnvelope(1, 16000, 2, 64)
        with self.assertRaises(KnowledgeError):
            audio.stream_peaks([sys.executable, '-c', 'import time;time.sleep(30)'], envelope, event, .1)
        timer = threading.Timer(.1, event.set)
        timer.start()
        try:
            with self.assertRaises(audio._Cancelled):
                audio.stream_peaks([sys.executable, '-c', 'import time;time.sleep(30)'], envelope, event, 30)
        finally:
            timer.join()
        with self.assertRaises(KnowledgeError):
            audio.stream_peaks([sys.executable, '-c', 'import sys;sys.stdout.buffer.write(b"x")'], envelope, threading.Event(), 5)
        # A child that ignores the requested interval must be stopped rather
        # than drained forever after the envelope has enough samples.
        endless = [sys.executable, '-c',
                   'import sys\nwhile True:\n sys.stdout.buffer.write(bytes(65536));sys.stdout.buffer.flush()']
        started = time.monotonic()
        with self.assertRaisesRegex(KnowledgeError, '工作量'):
            audio.stream_peaks(endless, audio.PeakEnvelope(.1, 16000, 2, 64), threading.Event(), 5)
        self.assertLess(time.monotonic() - started, 3)


class AudioBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg = audio.ffmpeg_executable()
        except KnowledgeError:
            raise unittest.SkipTest('Install the existing imageio-ffmpeg project dependency')
        cls.recording = owned_recording()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = KnowledgeService(Path(self.directory.name) / 'store')
        self.source = self.service.import_attachment(self.recording, 'owned.wav', 'audio/wav')

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def wait(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.service.audio_editor.status({'jobId': job['jobId']})
            if result['state'] in audio.TERMINAL:
                with self.service.audio_editor.lock:
                    thread = self.service.audio_editor.jobs[job['jobId']].thread
                thread.join(timeout=2)
                return result
            time.sleep(.02)
        self.fail('Generated audio export timed out')

    def test_info_and_full_envelope_preserve_stereo_amplitudes_and_source(self):
        response = self.service.handle('POST', API + '/media/info', data={'id': self.source['id']})
        self.assertEqual(response.status, 200)
        info = response.data
        self.assertEqual((info['durationSeconds'], info['sampleRate'], info['channels']), (4., 16000, 2))
        self.assertTrue(info['audioEditingSupported'])
        result = self.service.handle('POST', API + '/media/audio-waveform', data={'id': self.source['id'], 'buckets': 64})
        self.assertEqual(result.status, 200)
        value = result.data
        self.assertEqual(value['decodedFrames'], 64000)
        self.assertEqual(value['buckets'], 64)
        self.assertEqual(len(value['peaks']), 64)
        for quarter, level in enumerate((0, 4096, 16384, 30000)):
            for peak in value['peaks'][quarter * 16:(quarter + 1) * 16]:
                self.assertAlmostEqual(peak, level / 32768, places=5)
        self.assertFalse(value['normalized'])
        self.assertEqual(value['channelAggregation'], 'absolute_maximum')
        self.assertFalse(self.service._media_pins)
        _, path = self.service._attachment(self.source['id'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.source['sha256'])

    def test_subrange_envelope_default_buckets_and_mcp(self):
        result = self.service.call_tool('knowledge_get_audio_waveform', {'id': self.source['id'], 'startSeconds': 1.125, 'endSeconds': 1.875})
        self.assertFalse(result['isError'], result)
        value = result['structuredContent']
        self.assertEqual(value['buckets'], 1024)
        self.assertEqual(value['decodedFrames'], 12000)
        self.assertEqual(value['startSeconds'], 1.125)
        self.assertEqual(value['endSeconds'], 1.875)
        self.assertAlmostEqual(max(value['peaks']), .125, places=5)
        # A low-level recording is not normalized to 1.
        self.assertLess(max(value['peaks']), .2)

    def test_generated_aac_container_padding_is_drained_and_full_waveform_succeeds(self):
        # 2448 source frames do not fill AAC's 1024-frame blocks; decoding the
        # entire generated container exposes real codec padding on FFmpeg.
        generated = Path(self.directory.name) / 'owned-padding.m4a'
        subprocess.run([self.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000:duration=0.153',
                        '-c:a', 'aac', '-ac', '2', '-y', str(generated)], check=True, timeout=15)
        info = audio.probe_audio(generated)
        envelope = audio.PeakEnvelope(info.duration, info.sample_rate, info.channels, 64)
        audio.stream_peaks([self.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error',
                            '-i', str(generated), '-map', '0:a:0', '-c:a', 'pcm_s16le',
                            '-f', 's16le', 'pipe:1'], envelope, threading.Event(), 15)
        self.assertGreater(envelope.decoded_frames, envelope.expected_frames)
        self.assertEqual(envelope.frames, envelope.expected_frames)
        self.assertLessEqual(envelope.decoded_bytes, envelope.max_decoded_bytes)
        attachment = self.service.import_attachment(generated.read_bytes(), 'owned-padding.m4a', 'audio/mp4')
        value = self.service.audio_editor.waveform({'id': attachment['id'], 'buckets': 64})
        self.assertGreater(max(value['peaks']), .03)
        self.assertEqual(value['decodedFrames'], round(value['durationSeconds'] * info.sample_rate))
        self.assertEqual(self.service.read_attachment(attachment['id'])['sha256'], attachment['sha256'])
        self.assertFalse(self.service._media_pins)

    def test_real_clip_reencodes_new_m4a_keeps_channels_rate_and_original_hash(self):
        response = self.service.call_tool('knowledge_start_audio_attachment_edit', {'id': self.source['id'], 'startSeconds': 2.125, 'endSeconds': 2.875, 'name': '自己的合成片段'})
        self.assertFalse(response['isError'], response)
        result = self.wait(response['structuredContent'])
        self.assertEqual(result['state'], 'completed', result.get('error'))
        self.assertEqual(result['progress'], 100)
        self.assertEqual(result['attachment']['kind'], 'audio')
        self.assertEqual(result['attachment']['mimeType'], 'audio/mp4')
        self.assertEqual(result['attachment']['name'], '自己的合成片段.m4a')
        self.assertNotEqual(result['attachment']['id'], self.source['id'])
        self.assertEqual((result['sampleRate'], result['channels']), (16000, 2))
        self.assertAlmostEqual(result['actualDurationSeconds'], .75, delta=.065)
        _, source_path = self.service._attachment(self.source['id'])
        self.assertEqual(source_path.read_bytes(), self.recording)
        self.assertEqual(self.service.read_attachment(self.source['id'])['sha256'], self.source['sha256'])
        _, output_path = self.service._attachment(result['attachment']['id'])
        info = audio.probe_audio(output_path)
        self.assertEqual((info.sample_rate, info.channels), (16000, 2))
        data = self.service.audio_editor.waveform({'id': result['attachment']['id'], 'buckets': 64})
        self.assertAlmostEqual(max(data['peaks']), .5, delta=.06)
        # Cancellation after completion retains the already published copy.
        cancelled = self.service.audio_editor.cancel({'jobId': result['jobId']})
        self.assertEqual(cancelled['state'], 'completed')
        self.assertEqual(cancelled['attachment']['id'], result['attachment']['id'])
        self.assertTrue(self.service.delete_attachment(self.source['id'])['deleted'])

    def test_invalid_waveform_edit_video_and_corrupt_audio_are_rejected(self):
        for extra in ({'buckets': 63}, {'buckets': 4097}, {'buckets': True}, {'startSeconds': -1},
                      {'endSeconds': 5}, {'startSeconds': 1, 'endSeconds': 1}, {'endSeconds': float('nan')}, {'extra': True}):
            response = self.service.handle('POST', API + '/media/audio-waveform', data={'id': self.source['id'], **extra})
            self.assertEqual(response.status, 400, extra)
            self.assertFalse(self.service._media_pins)
        for extra in ({'endSeconds': .05}, {'name': '../escape.m4a'}, {'startSeconds': True}):
            response = self.service.handle('POST', API + '/media/audio-edit/start', data={'id': self.source['id'], 'startSeconds': 0, 'endSeconds': .5, **extra})
            self.assertEqual(response.status, 400)
            self.assertFalse(self.service._media_pins)
        corrupt = self.service.import_attachment(b'RIFF\x00\x00\x00\x00WAVEbroken-body', 'owned-corrupt.wav', 'audio/wav')
        for method in (self.service.audio_editor.info, lambda identifier: self.service.audio_editor.waveform({'id': identifier}),
                       lambda identifier: self.service.audio_editor.start({'id': identifier, 'startSeconds': 0, 'endSeconds': .5})):
            with self.assertRaises(KnowledgeError):
                method(corrupt['id'])
            self.assertFalse(self.service._media_pins)
        video = self.service.import_attachment(b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42', 'owned-video.mp4', 'video/mp4')
        with self.assertRaises(KnowledgeError):
            self.service.audio_editor.waveform({'id': video['id']})
        with self.assertRaises(KnowledgeError):
            self.service.audio_editor.start({'id': video['id'], 'startSeconds': 0, 'endSeconds': .5})
        self.assertFalse(self.service._media_pins)

    def test_workload_guard_releases_waveform_source(self):
        with patch.object(audio, 'MAX_WAVEFORM_PCM_BYTES', 1), self.assertRaises(KnowledgeError) as error:
            self.service.audio_editor.waveform({'id': self.source['id']})
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(self.service._media_pins)
        self.assertIsNone(self.service.audio_editor.waveform_active)

    def test_cancel_edit_preserves_source_blocks_delete_and_other_edit(self):
        fields = {'id': self.source['id'], 'startSeconds': .2, 'endSeconds': .8}
        with patch.object(audio.AudioPlan, 'command', return_value=[sys.executable, '-c', 'import time;time.sleep(30)']):
            job = self.service.audio_editor.start(fields)
            with self.assertRaises(KnowledgeError) as busy:
                self.service.audio_editor.start(fields)
            self.assertEqual(busy.exception.status, 409)
            with self.assertRaises(KnowledgeError) as pinned:
                self.service.delete_attachment(self.source['id'])
            self.assertEqual(pinned.exception.status, 409)
            self.assertFalse(self.service.list_resources({})['items'][0]['canDelete'])
            response = self.service.handle('POST', API + '/media/audio-edit/cancel', data={'jobId': job['jobId']})
            self.assertEqual(response.status, 200)
            result = self.wait(job)
        self.assertEqual(result['state'], 'cancelled')
        self.assertNotIn('attachment', result)
        self.assertEqual(self.service.list_attachments()['total'], 1)
        self.assertFalse(self.service._media_pins)
        self.assertTrue(self.service.delete_attachment(self.source['id'])['deleted'])

    def test_editor_close_cancels_active_waveform_and_waits_for_lease(self):
        entered, results = threading.Event(), []

        def wait_until_cancelled(command, envelope, cancel, timeout):
            entered.set()
            cancel.wait(timeout=5)
            raise audio._Cancelled()

        def waveform():
            try:
                self.service.audio_editor.waveform({'id': self.source['id']})
            except KnowledgeError as failed:
                results.append(failed.status)

        with patch.object(audio, 'stream_peaks', wait_until_cancelled):
            thread = threading.Thread(target=waveform)
            thread.start()
            self.assertTrue(entered.wait(5))
            with self.assertRaises(KnowledgeError) as busy:
                self.service.audio_editor.waveform({'id': self.source['id']})
            self.assertEqual(busy.exception.status, 409)
            self.service.audio_editor.close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [409])
        self.assertFalse(self.service._media_pins)

    def test_failure_and_close_edit_do_not_leave_a_published_partial(self):
        fields = {'id': self.source['id'], 'startSeconds': .2, 'endSeconds': .8}
        with patch.object(audio.AudioPlan, 'command', return_value=[sys.executable, '-c', 'raise SystemExit(2)']):
            result = self.wait(self.service.audio_editor.start(fields))
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(self.service._media_pins)
        with patch.object(audio.AudioPlan, 'command', return_value=[sys.executable, '-c', 'import time;time.sleep(30)']):
            job = self.service.audio_editor.start(fields)
            self.service.audio_editor.close()
        self.assertEqual(self.service.audio_editor.status({'jobId': job['jobId']})['state'], 'cancelled')
        self.assertEqual(self.service.list_attachments()['total'], 1)
        self.assertFalse(self.service._media_pins)

    def test_worker_start_rollback_and_published_cleanup_failure(self):
        fields = {'id': self.source['id'], 'startSeconds': .2, 'endSeconds': .8}
        real_start = threading.Thread.start

        def start(thread):
            if thread.name == 'desktop-audio-editor':
                raise RuntimeError('owned injected start failure')
            return real_start(thread)

        with patch.object(threading.Thread, 'start', start), self.assertRaises(RuntimeError):
            self.service.audio_editor.start(fields)
        self.assertFalse(self.service._media_pins)
        self.assertIsNone(self.service.audio_editor.active)
        real_temporary = tempfile.TemporaryDirectory

        class FailingCleanup(real_temporary):
            def __exit__(self, *args):
                super().__exit__(*args)
                raise OSError('owned injected cleanup failure')

        with patch.object(audio.tempfile, 'TemporaryDirectory', FailingCleanup):
            result = self.wait(self.service.audio_editor.start(fields))
        self.assertEqual(result['state'], 'completed')
        self.assertIn('cleanupWarning', result)
        self.assertEqual(self.service.list_attachments()['total'], 2)
        self.assertFalse(self.service._media_pins)


if __name__ == '__main__':
    unittest.main()
