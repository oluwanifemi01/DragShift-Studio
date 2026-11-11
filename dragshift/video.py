"""Video work through ffmpeg: probe, convert, compress, trim, resize, GIF, audio, mute, frames.

Every operation writes a new file next to the source (never over it), reports progress
through a `Control`, and stops cleanly (leaving nothing behind) when cancelled."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .util import UserError, file_size, human_size, sibling

TARGETS = ["MP4", "WEBM", "MOV", "GIF", "MP3", "M4A"]
EXT = {"MP4": ".mp4", "WEBM": ".webm", "MOV": ".mov", "GIF": ".gif", "MP3": ".mp3", "M4A": ".m4a"}
SAME_AS = {"mp4": "MP4", "m4v": "MP4", "mov": "MOV", "webm": "WEBM"}

QUALITY_CRF = {"High": 23, "Balanced": 27, "Small": 31}
GIF_MAX_SECONDS = 20          # Convert → GIF uses the first 20 s; the GIF tool lets you pick
MIN_VIDEO_KBPS = 60           # below this a "fit a size" result would be unwatchable
CONTAINER_OVERHEAD = 0.96     # leave room for MP4 headers when fitting a size


class Cancelled(Exception):
    """The user pressed Cancel. Nothing was written."""


# ---------------------------------------------------------------- progress + cancel
class Control:
    """Passed to every operation: reports progress (0..1) and carries the cancel flag."""

    def __init__(self, on_progress: Callable[[float], None] | None = None):
        self.on_progress = on_progress
        self._cancel = threading.Event()
        self._span = (0.0, 1.0)
        self._last = -1

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def wait_cancel(self, timeout: float) -> bool:
        return self._cancel.wait(timeout)

    def report(self, frac: float) -> None:
        a, b = self._span
        value = a + (b - a) * max(0.0, min(1.0, frac))
        pct = int(value * 100)
        if pct != self._last and self.on_progress:   # at most ~100 updates per job
            self._last = pct
            self.on_progress(value)

    @contextmanager
    def span(self, start: float, end: float) -> Iterator[None]:
        """Map the 0..1 progress of a sub-step onto [start, end] of the parent's range."""
        old = self._span
        a, b = old
        self._span = (a + (b - a) * start, a + (b - a) * end)
        try:
            yield
        finally:
            self._span = old


def each(paths: list, ctl: Control, fn: Callable[[Path, Control], list[Path]]) -> list[Path]:
    """Run `fn` on several files, splitting the progress bar evenly between them."""
    outs: list[Path] = []
    n = len(paths)
    for i, p in enumerate(paths):
        with ctl.span(i / n, (i + 1) / n):
            outs += fn(Path(p), ctl)
    return outs


# ---------------------------------------------------------------- finding ffmpeg
_FFMPEG: str | None = None


def ffmpeg_exe() -> str:
    """Bundled ffmpeg (imageio-ffmpeg) first, then the system one. Finder-launched Mac apps
    don't see Homebrew's PATH, so its usual folders are checked by name."""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    cands = [os.environ.get("DRAGSHIFT_FFMPEG")]
    try:
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 - not installed or no binary for this platform
        pass
    cands += [shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
    for c in cands:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            _FFMPEG = c
            return c
    raise UserError("Video needs ffmpeg, which wasn't found. Reinstall DragShift, or install ffmpeg "
                    "(Mac: brew install ffmpeg · Linux: sudo apt install ffmpeg).")


def available() -> bool:
    try:
        ffmpeg_exe()
        return True
    except UserError:
        return False


def _popen_kw() -> dict:
    # pythonw / the packaged .exe would flash a console window for every ffmpeg call
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# ---------------------------------------------------------------- probing
@dataclass(frozen=True)
class Info:
    duration: float
    width: int
    height: int
    vcodec: str
    acodec: str
    fps: float

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec)

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec)

    @property
    def size_text(self) -> str:
        return f"{self.width}×{self.height}" if self.has_video else ""


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+)(.*)")
_AUDIO = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+)")
_DIMS = re.compile(r"[ ,](\d{2,5})x(\d{2,5})[ ,\[]")
_FPS = re.compile(r"(\d+(?:\.\d+)?) fps")
_ROTATION = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees")


def parse_info(text: str) -> Info:
    """Parse the stream summary `ffmpeg -i` prints (no ffprobe in the bundled build)."""
    duration = 0.0
    m = _DURATION.search(text)
    if m:
        duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    vcodec, w, h, fps = "", 0, 0, 0.0
    for line in text.splitlines():
        vm = _VIDEO.search(line)
        if vm and not vcodec and "attached pic" not in line:   # skip cover art
            vcodec = vm[1]
            dm = _DIMS.search(vm[2] + " ")
            if dm:
                w, h = int(dm[1]), int(dm[2])
            fm = _FPS.search(line)
            fps = float(fm[1]) if fm else 0.0
    rm = _ROTATION.search(text)
    if rm and round(abs(float(rm[1]))) % 180 == 90:     # phone videos shot upright
        w, h = h, w
    am = _AUDIO.search(text)
    return Info(duration, w, h, vcodec, am[1] if am else "", fps)


def probe(path: str | Path) -> Info:
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)],
                       capture_output=True, text=True, errors="replace", **_popen_kw())
    info = parse_info(r.stderr)
    if not info.has_video and not info.has_audio:
        raise UserError("This file doesn't look like a video DragShift can read.")
    return info


def try_probe(path: str | Path) -> Info | None:
    try:
        return probe(path)
    except Exception:  # noqa: BLE001 - only used for nice-to-have summaries
        return None


# ---------------------------------------------------------------- running ffmpeg
def _friendly(stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    low = stderr.lower()
    if "unknown encoder" in low or "encoder not found" in low:
        return "This ffmpeg build can't write that format. Reinstall DragShift or update ffmpeg."
    if "no space left" in low:
        return "The disk is full."
    if "permission denied" in low:
        return "No permission to write next to this file."
    if "invalid data found" in low:
        return "This video looks damaged or isn't a format ffmpeg can read."
    return ("ffmpeg: " + lines[-1][:200]) if lines else "ffmpeg stopped unexpectedly."


def run(args: list[str], ctl: Control, duration: float) -> None:
    """Run ffmpeg, turning its `-progress` output into ctl.report() calls; kill it on cancel."""
    if ctl.cancelled:
        raise Cancelled()
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            errors="replace", **_popen_kw())
    err: list[str] = []
    readers = [threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True),
               threading.Thread(target=_watch_cancel, args=(proc, ctl), daemon=True)]
    for t in readers:
        t.start()
    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration > 0:
            us = line.split("=", 1)[1].strip()
            if us.lstrip("-").isdigit():
                ctl.report(int(us) / 1e6 / duration)
    rc = proc.wait()
    readers[0].join(timeout=2)
    if ctl.cancelled:
        raise Cancelled()
    if rc != 0:
        raise UserError(_friendly("".join(err)))
    ctl.report(1.0)


def _watch_cancel(proc: subprocess.Popen, ctl: Control) -> None:
    while proc.poll() is None:
        if ctl.wait_cancel(0.1):
            proc.kill()
            return


@contextmanager
def writing(out: Path) -> Iterator[Path]:
    """Yield a hidden temp path beside `out`; move it into place only if everything worked."""
    tmp = out.with_name(f".{out.stem}.dragshift-part{out.suffix}")
    try:
        yield tmp
        if not tmp.is_file() or file_size(tmp) == 0:
            raise UserError("ffmpeg didn't produce any output.")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- codec settings
EVEN = "scale=trunc(iw/2)*2:trunc(ih/2)*2"     # H.264 4:2:0 needs even dimensions


def scale_filter(height: int | None, info: Info) -> str:
    if height and info.height and height < info.height:
        return f"scale=-2:{height}"
    return EVEN


def h264(crf: int, vf: str, preset: str = "medium") -> list[str]:
    return ["-vf", vf, "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]


def aac(info: Info, kbps: int = 160) -> list[str]:
    if not info.has_audio:
        return ["-an"]
    if info.acodec == "aac":
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", f"{kbps}k"]


MAPS = ["-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn"]
MP4_FLAGS = ["-movflags", "+faststart"]


def mp4_ext(src: Path) -> str:
    return src.suffix.lower() if src.suffix.lower() in (".mp4", ".mov", ".m4v") else ".mp4"


def fmt_time(sec: float, precise: bool = False) -> str:
    sec = max(0.0, sec)
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    s_txt = f"{s:04.1f}" if precise else f"{int(s):02d}"
    return f"{h}:{m:02d}:{s_txt}" if h else f"{m}:{s_txt}"


# ---------------------------------------------------------------- convert
def targets_for(src_ext: str) -> list[str]:
    same = SAME_AS.get(src_ext.lower().lstrip("."))
    return [t for t in TARGETS if t != same]


def convert(src: str | Path, target: str, ctl: Control) -> list[Path]:
    src = Path(src)
    info = probe(src)
    if target in ("MP3", "M4A"):
        return extract_audio(src, target, ctl, info)
    if target == "GIF":
        return to_gif(src, ctl, 0, min(info.duration, GIF_MAX_SECONDS), info=info)
    if not info.has_video:
        raise UserError("This file has no picture to convert.")
    out = sibling(src, ext=EXT[target])
    if target == "WEBM":
        if info.vcodec in ("vp8", "vp9", "av1") and info.acodec in ("", "opus", "vorbis"):
            codec = ["-c", "copy"]
        else:
            audio = ["-c:a", "libopus", "-b:a", "128k"] if info.has_audio else ["-an"]
            codec = ["-vf", EVEN, "-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0", "-row-mt", "1",
                     "-deadline", "good", "-cpu-used", "4", "-pix_fmt", "yuv420p", *audio]
    elif info.vcodec in ("h264", "hevc") and info.acodec in ("", "aac", "mp3"):
        codec = ["-c", "copy", *MP4_FLAGS]          # MP4 ↔ MOV: just rewrap, instant
        if info.vcodec == "hevc":
            codec += ["-tag:v", "hvc1"]             # so QuickTime / iOS will play it
    else:
        codec = [*h264(20, EVEN), *aac(info), *MP4_FLAGS]
    with writing(out) as tmp:
        run(["-i", str(src), *MAPS, *codec, str(tmp)], ctl, info.duration)
    return [out]


# ---------------------------------------------------------------- compress
@dataclass(frozen=True)
class SizePlan:
    video_kbps: int
    audio_kbps: int
    height: int | None      # None = keep the original resolution


def plan_for_size(info: Info, target_bytes: int, height: int | None = None) -> SizePlan:
    """Bitrates (and, unless given, a resolution) that land the file under `target_bytes`."""
    if info.duration <= 0:
        raise UserError("Couldn't read this video's length, so it can't be fitted to a size.")
    total = target_bytes * 8 / 1000 / info.duration * CONTAINER_OVERHEAD
    audio = 0 if not info.has_audio else (128 if total >= 1200 else 64)
    video = int(total - audio)
    if video < MIN_VIDEO_KBPS:
        need = (MIN_VIDEO_KBPS + (64 if info.has_audio else 0)) * 1000 / 8 * info.duration / CONTAINER_OVERHEAD
        raise UserError(f"{human_size(target_bytes)} is too small for a {fmt_time(info.duration)} video. "
                        f"Try at least {human_size(need)}, or trim it first.")
    if height is None:
        steps = [(5000, None), (2500, 1080), (1200, 720), (600, 540), (300, 480)]
        height = next((h for kbps, h in steps if video >= kbps), 360)
    if height and info.height and height >= info.height:
        height = None
    return SizePlan(video, audio, height)


def compress(src: str | Path, ctl: Control, quality: str = "Balanced",
             target_bytes: int | None = None, height: int | None = None) -> list[Path]:
    src = Path(src)
    info = probe(src)
    if not info.has_video:
        raise UserError("This file has no picture to compress.")
    out = sibling(src, "Compressed", ext=mp4_ext(src))
    if target_bytes is None:
        args = ["-i", str(src), *MAPS, *h264(QUALITY_CRF[quality], scale_filter(height, info)),
                *_audio_for_compress(info, 128), *MP4_FLAGS]
        with writing(out) as tmp:
            run([*args, str(tmp)], ctl, info.duration)
        return [out]
    if target_bytes >= file_size(src) and height is None:
        raise UserError(f"It's already {human_size(file_size(src))}, under {human_size(target_bytes)}.")
    return [_two_pass(src, out, info, plan_for_size(info, target_bytes, height), target_bytes, ctl)]


def _audio_for_compress(info: Info, kbps: int) -> list[str]:
    return ["-c:a", "aac", "-b:a", f"{kbps}k"] if info.has_audio else ["-an"]


def _two_pass(src: Path, out: Path, info: Info, plan: SizePlan, target: int, ctl: Control) -> Path:
    """Classic two-pass x264: the first pass measures, the second spends bits where needed."""
    vf = scale_filter(plan.height, info)
    with tempfile.TemporaryDirectory(prefix="dragshift-") as tmpdir:
        log = str(Path(tmpdir) / "pass")
        common = ["-i", str(src), "-map", "0:v:0", "-vf", vf, "-c:v", "libx264", "-preset", "medium",
                  "-pix_fmt", "yuv420p", "-passlogfile", log]
        with ctl.span(0.0, 0.45):
            run([*common, "-b:v", f"{plan.video_kbps}k", "-pass", "1", "-an", "-f", "null", "-"],
                ctl, info.duration)
        kbps = plan.video_kbps
        with writing(out) as tmp:
            for attempt, (a, b) in enumerate(((0.45, 1.0), (0.45, 1.0))):
                with ctl.span(a, b):
                    run([*common, "-map", "0:a:0?", "-b:v", f"{kbps}k", "-pass", "2",
                         *_audio_for_compress(info, plan.audio_kbps), *MP4_FLAGS, str(tmp)],
                        ctl, info.duration)
                size = file_size(tmp)
                if size <= target or attempt == 1:
                    break
                kbps = max(MIN_VIDEO_KBPS, int(kbps * target / size * 0.95))   # overshot: once more
    return out


# ---------------------------------------------------------------- trim / resize / mute
def trim(src: str | Path, ctl: Control, start: float, end: float, precise: bool = False) -> list[Path]:
    src = Path(src)
    info = probe(src)
    end = min(end, info.duration) if info.duration else end
    if start < 0 or end - start < 0.1:
        raise UserError("Pick a part of the video at least 0.1 seconds long.")
    length = end - start
    seek = ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}", *MAPS]
    if precise:   # re-encode: cuts land on the exact frame
        out = sibling(src, "Trimmed", ext=mp4_ext(src))
        codec = [*h264(18, EVEN), *_audio_for_compress(info, 160), *MP4_FLAGS]
    else:         # stream copy: instant, but starts on the nearest keyframe
        out = sibling(src, "Trimmed")
        codec = ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        if out.suffix.lower() in (".mp4", ".mov", ".m4v"):
            codec += MP4_FLAGS
    with writing(out) as tmp:
        run([*seek, *codec, str(tmp)], ctl, length)
    return [out]


def resize(src: str | Path, ctl: Control, height: int) -> list[Path]:
    src = Path(src)
    info = probe(src)
    if not info.has_video:
        raise UserError("This file has no picture to resize.")
    if info.height and height >= info.height:
        raise UserError(f"It's already {info.height}p or smaller.")
    out = sibling(src, f"{height}p", ext=mp4_ext(src))
    with writing(out) as tmp:
        run(["-i", str(src), *MAPS, *h264(21, scale_filter(height, info)), *aac(info), *MP4_FLAGS,
             str(tmp)], ctl, info.duration)
    return [out]


def mute(src: str | Path, ctl: Control) -> list[Path]:
    src = Path(src)
    info = probe(src)
    if not info.has_video:
        raise UserError("This file has no picture, only sound.")
    out = sibling(src, "Muted")
    flags = MP4_FLAGS if out.suffix.lower() in (".mp4", ".mov", ".m4v") else []
    with writing(out) as tmp:
        run(["-i", str(src), "-map", "0:v:0", "-c", "copy", "-an", "-sn", "-dn", *flags, str(tmp)],
            ctl, info.duration)
    return [out]


# ---------------------------------------------------------------- audio
def extract_audio(src: str | Path, fmt: str, ctl: Control, info: Info | None = None) -> list[Path]:
    src = Path(src)
    info = info or probe(src)
    if not info.has_audio:
        raise UserError("This video has no sound to save.")
    if fmt == "M4A":
        codec = ["-c:a", "copy"] if info.acodec == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    else:
        codec = ["-c:a", "copy"] if info.acodec == "mp3" else ["-c:a", "libmp3lame", "-q:a", "2"]
    out = sibling(src, ext=EXT[fmt])
    with writing(out) as tmp:
        run(["-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-dn", *codec, str(tmp)], ctl, info.duration)
    return [out]


# ---------------------------------------------------------------- GIF
def gif_size(info: Info, width: int) -> tuple[int, int]:
    w = min(width, info.width) if info.width else width
    h = round(info.height * w / info.width) if info.width else w
    return w, max(1, h)


def to_gif(src: str | Path, ctl: Control, start: float = 0.0, length: float | None = None,
           fps: int = 12, width: int = 480, info: Info | None = None) -> list[Path]:
    """Two-step palette (generate, then apply) keeps GIF colours clean instead of banded."""
    src = Path(src)
    info = info or probe(src)
    if not info.has_video:
        raise UserError("This file has no picture to turn into a GIF.")
    length = length if length is not None else info.duration - start
    if length <= 0.05:
        raise UserError("Pick a part of the video to turn into a GIF.")
    w, _ = gif_size(info, width)
    graph = (f"[0:v:0]fps={fps},scale={w}:-1:flags=lanczos,split[a][b];"
             f"[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4")
    out = sibling(src, ext=".gif")
    with writing(out) as tmp:
        run(["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src), "-filter_complex", graph,
             "-loop", "0", str(tmp)], ctl, length)
    return [out]


# ---------------------------------------------------------------- frames
def grab_frame(src: str | Path, t: float, width: int | None = None) -> bytes:
    """One frame as PNG bytes (fast keyframe seek, then decode to the exact time)."""
    vf = ["-vf", f"scale={int(width)}:-2"] if width else []
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error",
                        "-ss", f"{max(0.0, t):.3f}", "-i", str(src), "-frames:v", "1", *vf,
                        "-f", "image2pipe", "-c:v", "png", "-"],
                       capture_output=True, **_popen_kw())
    if r.returncode != 0 or not r.stdout:
        raise UserError(_friendly(r.stderr.decode("utf-8", "replace")))
    return r.stdout


def save_frame(src: str | Path, t: float) -> list[Path]:
    src = Path(src)
    info = probe(src)
    t = min(max(0.0, t), max(0.0, info.duration - 0.05))
    label = "Frame " + fmt_time(t, precise=True).replace(":", "-")
    out = sibling(src, label, ext=".png")
    data = grab_frame(src, t)
    with writing(out) as tmp:
        tmp.write_bytes(data)
    return [out]
