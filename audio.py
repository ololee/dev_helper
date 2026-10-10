"""Offline audio envelope and cancellable AAC/M4A copies, with bounded memory.

Waveform peaks are absolute amplitude, never a frequency spectrum or loudness
normalization. PCM keeps every source channel so opposite-phase stereo cannot
silently cancel its envelope. No raw recording leaves this computer.
"""
from __future__ import annotations

import array
from collections import OrderedDict
import copy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave

if __package__:
    from .knowledge import KnowledgeError, _identifier, _instant, _int, _known, _name
    from .media import _Cancelled, _mp4_duration, _number, _run, _seconds, _terminate, ffmpeg_executable
else:
    from knowledge import KnowledgeError, _identifier, _instant, _int, _known, _name
    from media import _Cancelled, _mp4_duration, _number, _run, _seconds, _terminate, ffmpeg_executable

MAX_DURATION_SECONDS = 86_400
MAX_WAVEFORM_PCM_BYTES = 8 * 1024 ** 3
PCM_PADDING_ALLOWANCE_BYTES = 65536
AAC_SAMPLE_RATES = {7350, 8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000}
TERMINAL = {"completed", "failed", "cancelled"}


def _audio_run(command, **options):
    try:
        return _run(command, **options)
    except KnowledgeError as failed:
        raise KnowledgeError(str(failed).replace("视频", "音频"), failed.status) from None


@dataclass(frozen=True)
class AudioInfo:
    duration: float
    sample_rate: int
    channels: int
    channel_layout: str = ""

    @property
    def editable(self):
        return self.sample_rate in AAC_SAMPLE_RATES

    def metadata(self):
        return {"durationSeconds": self.duration, "durationMillis": round(self.duration * 1000),
                "sampleRate": self.sample_rate, "channels": self.channels, "audioTracks": 1,
                "channelLayout": self.channel_layout, "hasAudio": True,
                "audioEditingSupported": self.editable, "editingSupported": self.editable,
                "editable": self.editable, "waveformSupported": True,
                "waveformType": "absolute_peak_envelope", "waveformMaxBuckets": 4096,
                "audioEditingCapabilities": {"timeRange": self.editable, "asyncExport": self.editable,
                                             "waveform": True, "originalRetained": True}}


def probe_audio(path: Path, executable: str | None = None, cancel: threading.Event | None = None) -> AudioInfo:
    executable = executable or ffmpeg_executable()
    probe = shutil.which("ffprobe")
    if probe:
        raw, _ = _audio_run([probe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                            "format=duration:stream=codec_type,sample_rate,channels,channel_layout,duration:stream_disposition=attached_pic",
                            "-of", "json", str(path)], cancel=cancel)
        try:
            metadata = json.loads(raw)
            streams = metadata["streams"]
            tracks = [s for s in streams if s.get("codec_type") == "audio"]
            if len(tracks) != 1 or any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams):
                raise KnowledgeError("请选择包含单条音轨的音频附件，视频请使用视频编辑。")
            track = tracks[0]
            info = AudioInfo(float(metadata.get("format", {}).get("duration") or track["duration"]),
                             int(track["sample_rate"]), int(track["channels"]), str(track.get("channel_layout", "")))
        except (ValueError, KeyError, TypeError) as failed:
            if isinstance(failed, KnowledgeError):
                raise
            raise KnowledgeError("无法读取音频时长、采样率或声道。") from None
    else:
        _, stderr = _audio_run([executable, "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe",
                               "-probesize", "10000000", "-analyzeduration", "10000000", "-i", str(path)], cancel=cancel, check=False)
        text = stderr.decode("utf-8", "replace")
        duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        tracks = re.findall(r"^\s*Stream #\d+:\d+[^\n]*: Audio:([^\n]+)", text, re.M)
        videos = re.findall(r"^\s*Stream #\d+:\d+[^\n]*: Video:([^\n]+)", text, re.M)
        if len(tracks) != 1 or any("attached pic" not in line for line in videos):
            raise KnowledgeError("请选择包含单条音轨的音频附件，视频请使用视频编辑。")
        stream = re.search(r"(\d+) Hz,\s*([^,]+)", tracks[0])
        if not duration or not stream:
            raise KnowledgeError("无法读取音频时长、采样率或声道，请检查文件格式。")
        layout = stream[2].strip()
        channels = _layout_channels(layout)
        reported_duration = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
        exact_duration = _mp4_duration(path)
        if exact_duration is None:
            try:
                with wave.open(str(path), "rb") as source:
                    exact_duration = source.getnframes() / source.getframerate()
            except (wave.Error, EOFError, OSError):
                pass
        if exact_duration is not None and abs(exact_duration - reported_duration) <= .02:
            reported_duration = exact_duration
        info = AudioInfo(reported_duration, int(stream[1]), channels, layout)
    if not math.isfinite(info.duration) or not 0 < info.duration <= MAX_DURATION_SECONDS or not 1 <= info.channels <= 8 or not 1000 <= info.sample_rate <= 192000:
        raise KnowledgeError("音频需要时长不超过 24 小时、1–8 个声道、采样率 1000–192000 Hz。")
    return info


def _layout_channels(layout: str) -> int:
    base = layout.split("(", 1)[0].strip()
    counts = {"mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "3.1": 4, "4.0": 4, "quad": 4,
              "4.1": 5, "5.0": 5, "5.1": 6, "6.0": 6, "6.1": 7, "7.0": 7, "7.1": 8}
    if base in counts:
        return counts[base]
    count = re.match(r"(\d+) channels?", base)
    if count:
        return int(count[1])
    raise KnowledgeError("无法识别音频声道布局。")


@dataclass(frozen=True)
class AudioPlan:
    source_id: str
    info: AudioInfo
    start: float
    end: float
    name: str | None = None

    @classmethod
    def parse(cls, fields: dict, info: AudioInfo):
        _known(fields, "id", "startSeconds", "endSeconds", "name")
        identifier = _identifier(fields.get("id"))
        start = _number(fields.get("startSeconds"), "startSeconds", 0, info.duration)
        end = _number(fields.get("endSeconds"), "endSeconds", 0, info.duration)
        if end - start + 1e-6 < .1:
            raise KnowledgeError("endSeconds must be at least 0.1 seconds after startSeconds")
        if not info.editable:
            raise KnowledgeError("原音频采样率不适合 AAC，请先转换至 8000–96000 Hz 的标准采样率。")
        name = fields.get("name")
        if name is not None:
            if not isinstance(name, str) or not name.strip() or len(name) > 160 or name != _name(name):
                raise KnowledgeError("name must be nonempty text without paths or control characters, at most 160 characters")
            name = Path(name).stem[:156] + ".m4a"
        return cls(identifier, info, start, end, name)

    def command(self, executable: str, source: Path, output: Path):
        bitrate = 128000 if self.info.channels == 1 else min(512000, self.info.channels * 96000)
        return [executable, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y", "-threads", "2",
                "-ss", _seconds(self.start), "-accurate_seek", "-protocol_whitelist", "file,pipe", "-i", str(source),
                "-map", "0:a:0", "-t", _seconds(self.end - self.start), "-vn", "-sn", "-dn",
                "-c:a", "aac", "-threads", "2", "-b:a", str(bitrate), "-ar", str(self.info.sample_rate),
                "-ac", str(self.info.channels), "-map_metadata", "-1", "-map_chapters", "-1",
                "-movflags", "+faststart", "-progress", "pipe:1", "-f", "ipod", str(output)]

    def summary(self):
        return {"sourceAttachmentId": self.source_id, "sourceRetained": True,
                "requestedStartSeconds": self.start, "requestedEndSeconds": self.end,
                "requestedDurationSeconds": self.end - self.start, "sampleRate": self.info.sample_rate,
                "channels": self.info.channels, "format": "m4a", "encoder": "aac",
                "timePrecision": "source_audio_samples_and_aac_encoder_boundaries",
                "timePrecisionNote": "Cuts follow available source samples. AAC priming, padding and container timestamps can affect playback duration; inspect actualDurationSeconds."}


class AudioSourceLease:
    def __init__(self, service, identifier):
        self.service, self.identifier, self.closed = service, identifier, False
        with service.lock:
            metadata, self.path = service._attachment(identifier)
            if metadata["kind"] != "audio":
                raise KnowledgeError("id must identify an audio attachment")
            self.metadata = copy.deepcopy(metadata)
            service._media_pins[identifier] = service._media_pins.get(identifier, 0) + 1

    def close(self):
        with self.service.lock:
            if not self.closed:
                self.closed = True
                count = self.service._media_pins[self.identifier] - 1
                if count:
                    self.service._media_pins[self.identifier] = count
                else:
                    del self.service._media_pins[self.identifier]


class PeakEnvelope:
    """At most a 64 KiB PCM block and 4096 scalar peaks, independent of duration."""
    def __init__(self, duration: float, sample_rate: int, channels: int, buckets: int):
        self.expected_frames = max(1, math.ceil(duration * sample_rate))
        self.channels, self.buckets = channels, buckets
        self.peaks = [0.] * buckets
        self.frames = 0
        self.decoded_frames = 0
        self.decoded_bytes = 0
        self.max_decoded_bytes = min(MAX_WAVEFORM_PCM_BYTES,
                                     self.expected_frames * channels * 2 + PCM_PADDING_ALLOWANCE_BYTES)
        self.pending = bytearray()

    def append(self, chunk: bytes):
        self.decoded_bytes += len(chunk)
        if self.decoded_bytes > self.max_decoded_bytes:
            raise KnowledgeError("音频解码工作量超过所选区间，请检查文件或缩短区间后重试。", 409)
        self.pending.extend(chunk)
        frame_bytes = self.channels * 2
        usable = len(self.pending) // frame_bytes * frame_bytes
        if not usable:
            return
        samples = array.array("h")
        samples.frombytes(self.pending[:usable])
        del self.pending[:usable]
        if sys.byteorder != "little":
            samples.byteswap()
        frame_count = len(samples) // self.channels
        self.decoded_frames += frame_count
        # AAC decoders can emit the final padded frame after the container's
        # declared duration. Drain it without assigning it to the last bucket.
        frame_count = min(frame_count, self.expected_frames - self.frames)
        offset = 0
        while offset < frame_count:
            bucket = min(self.buckets - 1, self.frames * self.buckets // self.expected_frames)
            boundary = ((bucket + 1) * self.expected_frames + self.buckets - 1) // self.buckets
            count = min(frame_count - offset, max(1, boundary - self.frames))
            segment = samples[offset * self.channels:(offset + count) * self.channels]
            peak = max(abs(min(segment)), abs(max(segment))) / 32768
            self.peaks[bucket] = max(self.peaks[bucket], peak)
            self.frames += count
            offset += count


def stream_peaks(command: list[str], envelope: PeakEnvelope, cancel: threading.Event, timeout: float):
    if cancel.is_set():
        raise _Cancelled()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    diagnostic = bytearray()
    failures = []

    def pcm_reader():
        try:
            while not cancel.is_set():
                chunk = process.stdout.read1(65536)
                if not chunk:
                    break
                envelope.append(chunk)
        except Exception as failed:
            failures.append(failed)

    def error_reader():
        try:
            while chunk := process.stderr.read1(4096):
                diagnostic.extend(chunk)
                if len(diagnostic) > 65536:
                    del diagnostic[:-65536]
        except (OSError, ValueError) as failed:
            failures.append(failed)

    readers = [threading.Thread(target=pcm_reader, daemon=True), threading.Thread(target=error_reader, daemon=True)]
    started = []
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancel.wait(.05):
                raise _Cancelled()
            if failures:
                if isinstance(failures[0], KnowledgeError):
                    raise failures[0]
                raise KnowledgeError("音频波形解码失败。", 503)
            if time.monotonic() >= deadline:
                raise KnowledgeError("波形分析超时，请选择更短的区间后重试。", 503)
        if cancel.is_set():
            raise _Cancelled()
    finally:
        _terminate(process)
        for reader in started:
            reader.join(timeout=3)
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        for reader in started:
            reader.join(timeout=1)
    if failures and isinstance(failures[0], KnowledgeError):
        raise failures[0]
    if failures or process.returncode or envelope.pending or not envelope.frames:
        raise KnowledgeError("无法解码音频波形，请检查文件是否完整。", 503)


@dataclass
class _Job:
    plan: AudioPlan
    source: AudioSourceLease
    identifier: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: str = "queued"
    progress: float = 0
    created: str = field(default_factory=_instant)
    finished: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    attachment: dict | None = None
    output_info: AudioInfo | None = None
    error: str | None = None
    cleanup_warning: str | None = None


@dataclass
class _Waveform:
    thread: threading.Thread = field(default_factory=threading.current_thread)
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


class DesktopAudioEditor:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.closed = False
        self.active: _Job | None = None
        self.waveform_active: _Waveform | None = None
        self.jobs = OrderedDict()

    def info(self, identifier: str):
        with self.lock:
            self._available()
            source = AudioSourceLease(self.service, _identifier(identifier))
            try:
                return probe_audio(source.path).metadata()
            finally:
                source.close()

    def waveform(self, fields: dict):
        _known(fields, "id", "buckets", "startSeconds", "endSeconds")
        identifier = _identifier(fields.get("id"))
        buckets = _int(fields.get("buckets", 1024), "buckets", 64, 4096)
        with self.lock:
            self._available()
            if self.waveform_active:
                raise KnowledgeError("另一个波形正在分析，请等待完成后重试。", 409)
            source = AudioSourceLease(self.service, identifier)
            request = _Waveform()
            self.waveform_active = request
        try:
            executable = ffmpeg_executable()
            info = probe_audio(source.path, executable, request.cancel)
            start = _number(fields.get("startSeconds", 0), "startSeconds", 0, info.duration)
            end = _number(fields.get("endSeconds", info.duration), "endSeconds", 0, info.duration)
            if end <= start:
                raise KnowledgeError("endSeconds must be greater than startSeconds")
            span = end - start
            if span * info.sample_rate * info.channels * 2 > MAX_WAVEFORM_PCM_BYTES:
                raise KnowledgeError("本次波形解码工作量过大，请缩短分析区间后重试；原录音没有大小配额。", 409)
            envelope = PeakEnvelope(span, info.sample_rate, info.channels, buckets)
            command = [executable, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-threads", "2",
                       "-ss", _seconds(start), "-accurate_seek", "-protocol_whitelist", "file,pipe", "-i", str(source.path),
                       "-map", "0:a:0", "-t", _seconds(span), "-vn", "-sn", "-dn", "-c:a", "pcm_s16le",
                       "-af", "atrim=end_sample=" + str(envelope.expected_frames) + ",asetpts=PTS-STARTPTS",
                       "-ar", str(info.sample_rate), "-ac", str(info.channels), "-f", "s16le", "pipe:1"]
            stream_peaks(command, envelope, request.cancel, min(300., max(30., span / 100 + 10)))
            return {"id": identifier, "durationSeconds": info.duration, "startSeconds": start, "endSeconds": end,
                    "sampleRate": info.sample_rate, "channels": info.channels, "buckets": buckets, "peaks": envelope.peaks,
                    "decodedFrames": envelope.frames, "decodedPcmFrames": envelope.decoded_frames,
                    "discardedPaddingFrames": envelope.decoded_frames - envelope.frames,
                    "waveformType": "absolute_peak_envelope", "normalized": False,
                    "channelAggregation": "absolute_maximum", "sourceRetained": True}
        except _Cancelled:
            raise KnowledgeError("波形分析已取消。", 409) from None
        finally:
            source.close()
            with self.lock:
                if self.waveform_active is request:
                    self.waveform_active = None
                request.done.set()

    def start(self, fields: dict):
        _known(fields, "id", "startSeconds", "endSeconds", "name")
        with self.lock:
            self._available()
            if self.active is not None:
                raise KnowledgeError("另一个音频导出正在运行，请等待完成或先取消。", 409)
            source = AudioSourceLease(self.service, _identifier(fields.get("id")))
            accepted = False
            try:
                executable = ffmpeg_executable()
                info = probe_audio(source.path, executable)
                plan = AudioPlan.parse(fields, info)
                job = _Job(plan, source)
                job.thread = threading.Thread(target=self._export, args=(job, executable), name="desktop-audio-editor", daemon=True)
                self.active = job
                self.jobs[job.identifier] = job
                try:
                    job.thread.start()
                except Exception:
                    self.active = None
                    self.jobs.pop(job.identifier, None)
                    raise
                while len(self.jobs) > 32:
                    self.jobs.popitem(last=False)
                accepted = True
                return self._snapshot(job)
            finally:
                if not accepted:
                    source.close()

    def status(self, fields: dict):
        _known(fields, "jobId")
        with self.lock:
            return self._snapshot(self._job(fields.get("jobId")))

    def cancel(self, fields: dict):
        _known(fields, "jobId")
        with self.lock:
            job = self._job(fields.get("jobId"))
            if job.state not in TERMINAL:
                job.cancel.set()
                job.state = "cancelling"
                if job.process and job.process.poll() is None:
                    job.process.terminate()
            return self._snapshot(job)

    def latest(self):
        with self.lock:
            return self._snapshot(next(reversed(self.jobs.values()))) if self.jobs else None

    def close(self):
        with self.lock:
            self.closed = True
            job, waveform = self.active, self.waveform_active
            if waveform:
                waveform.cancel.set()
            if job:
                job.cancel.set()
                if job.state not in TERMINAL:
                    job.state = "cancelling"
                if job.process and job.process.poll() is None:
                    job.process.terminate()
        if job and job.thread and job.thread is not threading.current_thread():
            job.thread.join()
        if waveform and waveform.thread is not threading.current_thread():
            waveform.done.wait()

    def _available(self):
        if self.closed:
            raise KnowledgeError("Audio editor has stopped", 503)

    def _job(self, identifier):
        identifier = _identifier(identifier)
        job = self.jobs.get(identifier)
        if job is None:
            raise KnowledgeError("Audio job not found in this server process", 404)
        return job

    def _snapshot(self, job):
        result = {**job.plan.summary(), "jobId": job.identifier, "state": job.state, "progress": job.progress,
                  "sourceAttachment": copy.deepcopy(job.source.metadata), "createdAt": job.created}
        for key, value in (("finishedAt", job.finished), ("error", job.error), ("cleanupWarning", job.cleanup_warning)):
            if value:
                result[key] = value
        if job.attachment:
            result.update(attachment=copy.deepcopy(job.attachment), actualDurationSeconds=job.output_info.duration,
                          actualDurationMillis=round(job.output_info.duration * 1000))
        return result

    def _export(self, job, executable):
        def hook(process):
            with self.lock:
                job.process = process
                if process and job.cancel.is_set() and process.poll() is None:
                    process.terminate()

        def progress(seconds):
            with self.lock:
                job.progress = max(job.progress, min(95., seconds / (job.plan.end - job.plan.start) * 95))

        try:
            with self.lock:
                if job.cancel.is_set():
                    raise _Cancelled()
                job.state = "running"
            with tempfile.TemporaryDirectory(prefix="devhelper-audio-") as directory:
                output = Path(directory) / "edited.m4a"
                duration = job.plan.end - job.plan.start
                _audio_run(job.plan.command(executable, job.source.path, output), timeout=min(3600., max(60., duration * 2 + 30)),
                           cancel=job.cancel, process_hook=hook, progress=progress)
                info = probe_audio(output, executable, job.cancel)
                if info.sample_rate != job.plan.info.sample_rate or info.channels != job.plan.info.channels:
                    raise KnowledgeError("导出的采样率或声道验证失败；原音频保留。", 503)
                with self.lock:
                    if job.cancel.is_set():
                        raise _Cancelled()
                    name = job.plan.name or _name(Path(job.source.metadata["name"]).stem)[:149] + "-edited.m4a"
                    with output.open("rb") as source:
                        attachment = self.service.import_attachment(source, name, "audio/mp4", output.stat().st_size)
                    job.attachment, job.output_info = attachment, info
                    job.state, job.progress = "completed", 100.
        except _Cancelled:
            with self.lock:
                job.state = "cancelled"
        except Exception as failed:
            with self.lock:
                if job.attachment:
                    job.state = "completed"
                    job.cleanup_warning = "音频副本已保存，但临时文件清理未完成。"
                else:
                    job.state = "cancelled" if job.cancel.is_set() else "failed"
                    if job.state == "failed":
                        job.error = str(failed)[:500] or type(failed).__name__
        finally:
            job.source.close()
            with self.lock:
                job.finished = _instant()
                if self.active is job:
                    self.active = None
