"""Local, cancellable video copies. Source files are never edited in place.

Coordinates refer to the upright decoded video, before cropping. Display-matrix
rotation is applied explicitly; annotations use the same upright coordinates.
Only validated numeric values reach filter expressions, and no command uses a shell.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from fractions import Fraction
import copy
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

if __package__:
    from .knowledge import KnowledgeError, _identifier, _instant, _int, _known, _name
else:
    from knowledge import KnowledgeError, _identifier, _instant, _int, _known, _name

MAX_DIMENSION = 8192
MAX_ANNOTATION_PIXELS = 12_000_000
MAX_DURATION_SECONDS = 21_600
OUTPUT_CAPTURE_BYTES = 131_072
TERMINAL = {"completed", "failed", "cancelled"}


class _Cancelled(Exception):
    pass


def ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        executable = shutil.which("ffmpeg")
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise KnowledgeError("请安装电脑版依赖中的 imageio-ffmpeg，再重试视频编辑。", 503)
    return str(Path(executable).resolve())


def _number(value, name: str, low: float, high: float) -> float:
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and low <= value <= high
    except OverflowError:
        valid = False
    if not valid:
        raise KnowledgeError(f"{name} must be a finite number in {low}..{high}")
    return float(value)


def _seconds(value: float) -> str:
    return f"{value:.6f}"


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait()


def _run(command: list[str], *, timeout: float = 20, cancel: threading.Event | None = None,
         process_hook=None, progress=None, check: bool = True) -> tuple[bytes, bytes]:
    """Drain both pipes with bounded capture even if the input contains huge tags."""
    if cancel is not None and cancel.is_set():
        raise _Cancelled()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False)
    captures = [bytearray(), bytearray()]
    errors = []

    def read_pipe(pipe, target, parse_progress=False):
        pending = bytearray()
        try:
            while chunk := pipe.read1(4096):
                # Keep the diagnostic tail; stdout JSON is bounded and checked below.
                target.extend(chunk)
                if len(target) > OUTPUT_CAPTURE_BYTES:
                    del target[:-OUTPUT_CAPTURE_BYTES]
                if parse_progress:
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, remaining = pending.partition(b"\n")
                        pending[:] = remaining
                        if line.startswith(b"out_time_us="):
                            try:
                                progress(int(line.split(b"=", 1)[1]) / 1_000_000)
                            except ValueError:
                                pass
                    if len(pending) > 4096:
                        pending.clear()
        except (OSError, ValueError) as failed:
            errors.append(failed)

    readers = [threading.Thread(target=read_pipe, args=(process.stdout, captures[0], progress is not None), daemon=True),
               threading.Thread(target=read_pipe, args=(process.stderr, captures[1]), daemon=True)]
    started_readers = []
    try:
        for reader in readers:
            reader.start()
            started_readers.append(reader)
        if process_hook:
            process_hook(process)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancel is not None and cancel.wait(.05):
                raise _Cancelled()
            if cancel is None:
                time.sleep(.05)
            if time.monotonic() >= deadline:
                raise KnowledgeError("视频处理超时；请缩短区间或降低原视频分辨率后重试。", 503)
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
    finally:
        _terminate(process)
        for reader in started_readers:
            reader.join(timeout=3)
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        for reader in started_readers:
            reader.join(timeout=1)
        if process_hook:
            process_hook(None)
    if errors:
        raise KnowledgeError("无法读取视频处理结果。", 503)
    if check and process.returncode:
        diagnostic = captures[1].decode("utf-8", "replace")[-1200:]
        # Input paths and metadata are private; do not return raw FFmpeg logs to clients.
        hints = ("Unknown encoder", "No space left on device", "Invalid data found", "Permission denied")
        hint = next((h for h in hints if h in diagnostic), "请检查视频格式、可用空间或缩短区间")
        raise KnowledgeError("FFmpeg 视频处理失败：" + hint, 503)
    return bytes(captures[0]), bytes(captures[1])


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float
    rotation: int = 0
    audio_tracks: int = 0
    frame_rate: float | None = None

    @property
    def display_width(self):
        return self.height if self.rotation in (90, 270) else self.width

    @property
    def display_height(self):
        return self.width if self.rotation in (90, 270) else self.height

    def metadata(self):
        return {"width": self.width, "height": self.height, "durationSeconds": self.duration,
                "durationMillis": round(self.duration * 1000), "rotationDegrees": self.rotation,
                "displayWidth": self.display_width, "displayHeight": self.display_height,
                "hasAudio": self.audio_tracks > 0, "audioTracks": self.audio_tracks,
                "frameRate": self.frame_rate, "frameRateTrusted": False,
                "coordinateSpace": "rotation_oriented_original_pixels"}


def probe_video(path: Path, executable: str | None = None, cancel: threading.Event | None = None) -> VideoInfo:
    executable = executable or ffmpeg_executable()
    probe = shutil.which("ffprobe")
    info = None
    if probe:
        raw, _ = _run([probe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                       "format=duration:stream=codec_type,width,height,duration,avg_frame_rate:stream_tags=rotate:stream_side_data=rotation",
                       "-of", "json", str(path)], cancel=cancel)
        try:
            data = json.loads(raw)
            streams = data["streams"]
            stream = next(s for s in streams if s.get("codec_type") == "video")
            angle = next((s["rotation"] for s in stream.get("side_data_list", []) if "rotation" in s),
                         stream.get("tags", {}).get("rotate", 0))
            duration = float(data.get("format", {}).get("duration") or stream["duration"])
            rate = stream.get("avg_frame_rate", "0/0")
            try:
                fps = float(Fraction(rate))
            except (ValueError, ZeroDivisionError):
                fps = None
            info = VideoInfo(int(stream["width"]), int(stream["height"]), duration,
                             _rotation(float(angle)), sum(s.get("codec_type") == "audio" for s in streams), fps)
        except (ValueError, KeyError, StopIteration, TypeError):
            raise KnowledgeError("无法读取视频尺寸、旋转或时长。") from None
    else:
        _, stderr = _run([executable, "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe", "-probesize", "10000000", "-analyzeduration", "10000000",
                          "-i", str(path)], cancel=cancel, check=False)
        text = stderr.decode("utf-8", "replace")
        duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        video = re.search(r"^\s*Stream #\d+:\d+[^\n]*: Video:([^\n]+)", text, re.M)
        dimensions = re.search(r"(?:^|[, ])(\d{1,6})x(\d{1,6})(?:[, \[]|$)", video[1]) if video else None
        if not duration or not dimensions:
            raise KnowledgeError("无法读取视频尺寸和时长，请选择兼容的视频。")
        # Side data follows its stream; stop at the next stream so audio metadata
        # cannot be mistaken for the video display rotation.
        next_stream = re.search(r"\n\s*Stream #", text[video.end():])
        details = text[video.end():video.end() + next_stream.start()] if next_stream else text[video.end():]
        rotation = re.search(r"displaymatrix: rotation of ([+-]?\d+(?:\.\d+)?) degrees", details)
        tag = re.search(r"^\s*rotate\s*:\s*([+-]?\d+(?:\.\d+)?)", details, re.M)
        fps = re.search(r"(\d+(?:\.\d+)?) fps", video[1])
        reported_duration = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
        precise_duration = _mp4_duration(path)
        if precise_duration is not None and abs(precise_duration - reported_duration) <= .02:
            reported_duration = precise_duration
        info = VideoInfo(int(dimensions[1]), int(dimensions[2]), reported_duration,
                         _rotation(float((rotation or tag)[1])) if rotation or tag else 0,
                         len(re.findall(r"^\s*Stream #\d+:\d+[^\n]*: Audio:", text, re.M)),
                         float(fps[1]) if fps else None)
    if not (2 <= info.width <= MAX_DIMENSION and 2 <= info.height <= MAX_DIMENSION
            and math.isfinite(info.duration) and 0 < info.duration <= MAX_DURATION_SECONDS and 0 <= info.audio_tracks <= 15):
        raise KnowledgeError("视频需要单边不超过 8192 像素、时长不超过 6 小时，且最多 15 条音轨。")
    return info


def _mp4_duration(path: Path) -> float | None:
    """Read only atom headers and 32 mvhd bytes; avoid loading a large moov box.

    FFmpeg's human-readable Duration rounds to centiseconds. An MP4 movie header
    provides its actual time base. Use it only when it agrees with FFmpeg's
    reported duration (edit lists and unusual track layouts may differ).
    """
    import struct
    try:
        with path.open("rb") as stream:
            count = 0

            def atoms(start, end):
                nonlocal count
                while start + 8 <= end and count < 10000:
                    count += 1
                    stream.seek(start)
                    raw = stream.read(8)
                    if len(raw) != 8:
                        return
                    size, kind = struct.unpack(">I4s", raw)
                    header = 8
                    if size == 1:
                        raw = stream.read(8)
                        if len(raw) != 8:
                            return
                        size = struct.unpack(">Q", raw)[0]
                        header = 16
                    elif size == 0:
                        size = end - start
                    if size < header or start + size > end:
                        return
                    yield kind, start + header, start + size
                    start += size

            for kind, start, end in atoms(0, path.stat().st_size):
                if kind != b"moov":
                    continue
                for child, content, stop in atoms(start, end):
                    if child != b"mvhd":
                        continue
                    stream.seek(content)
                    raw = stream.read(min(32, stop - content))
                    if len(raw) >= 20 and raw[0] == 0:
                        scale, duration = struct.unpack(">II", raw[12:20])
                    elif len(raw) >= 32 and raw[0] == 1:
                        scale, duration = struct.unpack(">IQ", raw[20:32])
                    else:
                        return None
                    return duration / scale if scale and duration else None
    except (OSError, ValueError):
        pass
    return None


def _rotation(value: float) -> int:
    if not math.isfinite(value) or abs(value - round(value / 90) * 90) > .1:
        raise KnowledgeError("目前仅支持 0、90、180、270 度的视频显示旋转。")
    return round(value / 90) * 90 % 360


def _operations(raw, width: int, height: int) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > 128:
        raise KnowledgeError("operations must contain at most 128 operations")
    total = 0
    result = []
    for item in raw:
        _known(item, "type", "points", "color", "width", "text", "fontSize")
        kind, points, color = item.get("type"), item.get("points"), item.get("color")
        if kind not in ("pen", "line", "arrow", "rect", "text") or not isinstance(color, str) or not re.fullmatch(r"#[\da-fA-F]{6}", color):
            raise KnowledgeError("Invalid drawing type or #RRGGBB color")
        if not isinstance(points, list) or not (2 <= len(points) <= 1024 if kind == "pen" else len(points) == (1 if kind == "text" else 2)):
            raise KnowledgeError("Invalid drawing point count")
        total += len(points)
        if total > 4096:
            raise KnowledgeError("At most 4096 drawing points are supported")
        coordinates = []
        for point in points:
            _known(point, "x", "y")
            coordinates.append((_number(point.get("x"), "point.x", 0, width), _number(point.get("y"), "point.y", 0, height)))
        operation = {"type": kind, "points": coordinates, "color": color, "width": _int(item.get("width"), "width", 1, 32)}
        if kind == "text":
            text = item.get("text")
            if not isinstance(text, str) or not text or len(text) > 256 or text.count("\n") >= 8 or any(ord(c) < 32 and c != "\n" or ord(c) == 127 for c in text):
                raise KnowledgeError("text must contain 1..256 characters, at most 8 lines and no control characters")
            operation.update(text=text, fontSize=_int(item.get("fontSize", 32), "fontSize", 12, 128))
        elif "text" in item or "fontSize" in item:
            raise KnowledgeError("text/fontSize require a text operation")
        result.append(operation)
    if result and width * height > MAX_ANNOTATION_PIXELS:
        raise KnowledgeError("标注最多支持 1200 万像素原视频，请先降低原视频分辨率。")
    return result


@dataclass(frozen=True)
class VideoPlan:
    source_id: str
    info: VideoInfo
    start: float
    end: float
    requested_crop: dict
    actual_crop: dict
    operations: list[dict]
    bitrate: int
    frame_rate: int
    fast: bool = False

    @classmethod
    def parse(cls, fields: dict, info: VideoInfo, fast: bool = False):
        _known(fields, *( ("id", "startSeconds", "endSeconds") if fast else
                          ("id", "startSeconds", "endSeconds", "crop", "operations", "bitrate", "frameRate")))
        source_id = _identifier(fields.get("id"))
        start = _number(fields.get("startSeconds"), "startSeconds", 0, info.duration)
        end = _number(fields.get("endSeconds"), "endSeconds", 0, info.duration)
        if end - start + 1e-6 < .1:
            raise KnowledgeError("endSeconds must be at least 0.1 seconds after startSeconds")
        crop = fields.get("crop", {"x": 0, "y": 0, "width": info.display_width, "height": info.display_height})
        _known(crop, "x", "y", "width", "height")
        requested = {"x": _int(crop.get("x"), "crop.x", 0, info.display_width - 1),
                     "y": _int(crop.get("y"), "crop.y", 0, info.display_height - 1),
                     "width": _int(crop.get("width"), "crop.width", 1, info.display_width),
                     "height": _int(crop.get("height"), "crop.height", 1, info.display_height)}
        if requested["x"] + requested["width"] > info.display_width or requested["y"] + requested["height"] > info.display_height:
            raise KnowledgeError("crop must lie within the upright original video")
        actual = {**requested, "width": requested["width"] & ~1, "height": requested["height"] & ~1}
        if actual["width"] < 2 or actual["height"] < 2:
            raise KnowledgeError("Video crop must contain at least 2 by 2 pixels")
        return cls(source_id, info, start, end, requested, actual,
                   _operations(fields.get("operations", []), info.display_width, info.display_height),
                   _int(fields.get("bitrate", 4_000_000), "bitrate", 100_000, 20_000_000),
                   _int(fields.get("frameRate", 30), "frameRate", 1, 60), fast)

    def geometry(self):
        return {"requestedCrop": self.requested_crop, "actualCrop": self.actual_crop,
                "cropAdjusted": self.requested_crop != self.actual_crop,
                "width": self.actual_crop["width"], "height": self.actual_crop["height"],
                "sourceWidth": self.info.display_width, "sourceHeight": self.info.display_height,
                "rotationDegrees": 0, "sourceRotationDegrees": self.info.rotation,
                "frameRate": self.frame_rate, "bitrate": self.bitrate, "operationsApplied": len(self.operations),
                "requestedStartSeconds": self.start, "requestedEndSeconds": self.end,
                "requestedDurationSeconds": self.end - self.start,
                "coordinateSpace": "rotation_oriented_original_pixels", "audioTracks": self.info.audio_tracks,
                "editMode": "stream_copy" if self.fast else "software_h264_annotation_crop_trim",
                "timePrecision": "keyframe_boundaries" if self.fast else "source_frames_and_output_frame_rate",
                "precise": False, "outputFrameIntervalSeconds": 1 / self.frame_rate,
                "timePrecisionNote": "Source frames and audio encoder boundaries determine the actual cut; an output frame interval does not guarantee arbitrary millisecond cuts."}

    def command(self, executable: str, source: Path, output: Path, overlay: Path | None = None):
        args = [executable, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y",
                "-threads", "2", "-filter_threads", "2", "-filter_complex_threads", "2",
                "-ss", _seconds(self.start), "-protocol_whitelist", "file,pipe"]
        if self.fast:
            return args + ["-noaccurate_seek", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                           "-t", _seconds(self.end - self.start), "-sn", "-dn", "-avoid_negative_ts", "make_zero",
                           "-movflags", "+faststart", "-progress", "pipe:1", "-f", "mp4", str(output)]
        args += ["-accurate_seek", "-noautorotate", "-display_rotation:v:0", "0", "-i", str(source)]
        if overlay:
            args += ["-loop", "1", "-framerate", str(self.frame_rate), "-protocol_whitelist", "file,pipe", "-i", str(overlay)]
        crop = self.actual_crop
        # yuv444p keeps an odd x/y origin exact before final chroma subsampling.
        rotation_filter = {0: "", 90: "transpose=cclock,", 180: "hflip,vflip,", 270: "transpose=clock,"}[self.info.rotation]
        graph = "[0:v:0]" + rotation_filter + "format=yuv444p,setsar=1[v]"
        if overlay:
            graph += ";[v][1:v:0]overlay=0:0:format=yuv444:shortest=1[marked]"
        graph += ";" + ("[marked]" if overlay else "[v]") + f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}:exact=1,fps={self.frame_rate},format=yuv420p[edited]"
        return args + ["-filter_complex", graph, "-map", "[edited]", "-map", "0:a?", "-t", _seconds(self.end - self.start),
                       "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-b:v", str(self.bitrate), "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "192k", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
                       "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart", "-progress", "pipe:1", "-f", "mp4", str(output)]


def _font(size: int):
    from PIL import ImageFont
    candidates = ["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc",
                  "C:/Windows/Fonts/msyh.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                  "DejaVuSans.ttf"]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def render_overlay(plan: VideoPlan, path: Path):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise KnowledgeError("请安装电脑版依赖中的 Pillow，以导出视频标注。", 503) from None
    image = Image.new("RGBA", (plan.info.display_width, plan.info.display_height))
    try:
        draw = ImageDraw.Draw(image)
        for operation in plan.operations:
            points, color, width = operation["points"], operation["color"], operation["width"]
            kind = operation["type"]
            if kind in ("pen", "line", "arrow"):
                draw.line(points, fill=color, width=width, joint="curve")
                radius = width / 2
                for x, y in points:
                    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
                if kind == "arrow":
                    a, b = points
                    angle = math.atan2(b[1] - a[1], b[0] - a[0])
                    length = max(12, width * 3)
                    ends = [(b[0] - length * math.cos(angle + offset), b[1] - length * math.sin(angle + offset)) for offset in (-math.pi / 6, math.pi / 6)]
                    draw.line([ends[0], b, ends[1]], fill=color, width=width, joint="curve")
            elif kind == "rect":
                a, b = points
                draw.rectangle((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), outline=color, width=width)
            else:
                font = _font(operation["fontSize"])
                x, y = points[0]
                for i, line in enumerate(operation["text"].split("\n")):
                    # Android Canvas text y denotes the baseline.
                    draw.text((x, y + i * operation["fontSize"] * 1.25), line, fill=color, font=font, anchor="ls")
        image.save(path, format="PNG")
    finally:
        image.close()


class _SourceLease:
    def __init__(self, service, identifier):
        self.service, self.identifier, self.closed = service, identifier, False
        with service.lock:
            metadata, self.path = service._attachment(identifier)
            if metadata["kind"] != "video":
                raise KnowledgeError("id must identify a video attachment")
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


@dataclass
class _Job:
    plan: VideoPlan
    source: _SourceLease
    identifier: str = field(default_factory=lambda: str(uuid.uuid4()))
    created: str = field(default_factory=_instant)
    state: str = "queued"
    progress: float = 0
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    finished: str | None = None
    attachment: dict | None = None
    output_info: VideoInfo | None = None
    error: str | None = None
    cleanup_warning: str | None = None


class DesktopVideoEditor:
    """One bounded encoder at a time; job history lasts for this server process."""
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.jobs = OrderedDict()
        self.active: _Job | None = None
        self.closed = False

    def info(self, identifier: str):
        lease = _SourceLease(self.service, _identifier(identifier))
        try:
            result = probe_video(lease.path).metadata()
            result.update(editingSupported=True, editable=True, trimMode="stream_copy", precise=False,
                          videoEditingCapabilities={"crop": True, "timeRange": True, "audioPreserved": True,
                                                    "annotations": True, "fastTrim": True, "asyncExport": True},
                          maxDimension=MAX_DIMENSION, maxAnnotationPixels=MAX_ANNOTATION_PIXELS)
            return result
        finally:
            lease.close()

    def start(self, fields: dict, *, fast: bool = False):
        _known(fields, *( ("id", "startSeconds", "endSeconds") if fast else
                          ("id", "startSeconds", "endSeconds", "crop", "operations", "bitrate", "frameRate")))
        # Reservation and startup validation share a lock: concurrent requests
        # cannot pin a second source or outlive close() before they are accepted.
        with self.lock:
            if self.closed:
                raise KnowledgeError("Video editor has stopped", 503)
            if self.active is not None:
                raise KnowledgeError("另一个视频任务正在处理，请等待完成或先取消。", 409)
            source = _SourceLease(self.service, _identifier(fields.get("id")))
            accepted = False
            try:
                executable = ffmpeg_executable()
                info = probe_video(source.path, executable)
                plan = VideoPlan.parse(fields, info, fast)
                if plan.operations:
                    try:
                        import PIL.Image
                    except ImportError:
                        raise KnowledgeError("请安装电脑版依赖中的 Pillow，以导出视频标注。", 503) from None
                if fast and source.metadata["mimeType"] not in ("video/mp4", "video/quicktime"):
                    raise KnowledgeError("Fast trimming supports MP4 or MOV attachments")
                # Guard this request's decoder and x264 allocations, without
                # limiting the number or total size of stored user attachments.
                if not fast:
                    _check_memory(info, bool(plan.operations))
                job = _Job(plan, source)
                job.thread = threading.Thread(target=self._export, args=(job, executable), name="desktop-video-editor", daemon=True)
                self.active = job
                self.jobs[job.identifier] = job
                try:
                    job.thread.start()
                except Exception:
                    self.jobs.pop(job.identifier, None)
                    self.active = None
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

    def latest(self):
        with self.lock:
            return self._snapshot(next(reversed(self.jobs.values()))) if self.jobs else None

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

    def trim(self, fields: dict):
        snapshot = self.start(fields, fast=True)
        with self.lock:
            job = self.jobs[snapshot["jobId"]]
        job.thread.join()
        result = self.status({"jobId": job.identifier})
        if result["state"] != "completed":
            raise KnowledgeError(result.get("error", "视频剪辑已取消。"), 503)
        info = job.output_info
        return {**result["attachment"], "sourceAttachmentId": job.plan.source_id, "sourceRetained": True,
                "requestedStartSeconds": job.plan.start, "requestedEndSeconds": job.plan.end,
                "requestedDurationSeconds": job.plan.end - job.plan.start, "actualDurationSeconds": info.duration,
                "actualDurationMillis": round(info.duration * 1000), "mode": "stream_copy", "precise": False,
                "audioPreserved": True, **info.metadata()}

    def close(self):
        with self.lock:
            self.closed = True
            job = self.active
            if job:
                job.cancel.set()
                if job.state not in TERMINAL:
                    job.state = "cancelling"
                if job.process and job.process.poll() is None:
                    job.process.terminate()
        if job and job.thread and job.thread is not threading.current_thread():
            job.thread.join()

    def _job(self, identifier):
        identifier = _identifier(identifier)
        job = self.jobs.get(identifier)
        if job is None:
            raise KnowledgeError("Video job not found in this server process", 404)
        return job

    def _snapshot(self, job):
        result = {**job.plan.geometry(), "jobId": job.identifier, "state": job.state, "progress": job.progress,
                  "sourceAttachmentId": job.plan.source_id, "sourceAttachment": copy.deepcopy(job.source.metadata),
                  "sourceRetained": True, "createdAt": job.created}
        if job.error:
            result["error"] = job.error
        if job.cleanup_warning:
            result["cleanupWarning"] = job.cleanup_warning
        if job.finished:
            result["finishedAt"] = job.finished
        if job.attachment:
            result.update(attachment=copy.deepcopy(job.attachment), actualDurationSeconds=job.output_info.duration,
                          encoder="stream_copy" if job.plan.fast else "libx264", audioPreserved=True)
        return copy.deepcopy(result)

    def _export(self, job: _Job, executable: str):
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
            # Unique process-private directory; only this request's partials are
            # removed. User attachments and other jobs' files are never swept.
            with tempfile.TemporaryDirectory(prefix="devhelper-video-") as directory:
                output = Path(directory) / "edited.mp4"
                overlay = Path(directory) / "overlay.png" if job.plan.operations else None
                if overlay:
                    render_overlay(job.plan, overlay)
                timeout = min(3600., max(60., (job.plan.end - job.plan.start) * 8 + 30))
                _run(job.plan.command(executable, job.source.path, output, overlay), timeout=timeout,
                     cancel=job.cancel, process_hook=hook, progress=progress)
                info = probe_video(output, executable, job.cancel)
                if info.audio_tracks != job.plan.info.audio_tracks:
                    raise KnowledgeError("导出未保留所有音轨；原视频保留，请重试。", 503)
                if not job.plan.fast and (info.width != job.plan.actual_crop["width"] or info.height != job.plan.actual_crop["height"] or info.rotation):
                    raise KnowledgeError("导出的视频尺寸或旋转验证失败；原视频保留。", 503)
                # Commit and cancel share the lock: a cancellation acknowledged
                # before publication cannot create a late attachment. Once
                # committed, cancelling keeps the completed immutable copy.
                with self.lock:
                    if job.cancel.is_set():
                        raise _Cancelled()
                    name = _name(Path(job.source.metadata["name"]).stem + "-edited.mp4")
                    with output.open("rb") as stream:
                        attachment = self.service.import_attachment(stream, name, "video/mp4", output.stat().st_size)
                    job.attachment, job.output_info = attachment, info
                    job.state, job.progress = "completed", 100.
        except _Cancelled:
            with self.lock:
                job.state = "cancelled"
        except Exception as failed:
            with self.lock:
                if job.attachment:
                    # Publication already committed. A temporary-directory
                    # cleanup failure must not misreport or discard that copy.
                    job.state = "completed"
                    job.cleanup_warning = "副本已保存，但临时文件清理未完成。"
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


def _check_memory(info: VideoInfo, annotations: bool):
    estimated = info.width * info.height * (96 if annotations else 80) + 64 * 1024 * 1024
    available = None
    try:
        available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        # macOS does not expose SC_AVPHYS_PAGES. Avoid another dependency or a
        # process per export; its physical RAM provides a conservative ceiling.
        if hasattr(os, "sysconf"):
            try:
                available = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") * .5
            except (ValueError, OSError):
                pass
    if estimated > (available * .6 if available else 2 * 1024 ** 3):
        raise KnowledgeError("原视频分辨率需要过多工作内存，请降低分辨率或关闭其他程序后重试。", 409)
