"""Audio work through the bundled ffmpeg: convert, compress, trim, volume and waveforms.

Shares the ffmpeg plumbing in video.py (probing, progress, cancel, write-then-move), so every
operation writes a new file next to the source and leaves nothing behind if it stops early."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .util import UserError, file_size, human_size, sibling
from .video import Control, Info, _friendly, _popen_kw, ffmpeg_exe, fmt_time, probe, run, writing

TARGETS = ["MP3", "M4A", "WAV", "FLAC", "OGG"]
EXT = {"MP3": ".mp3", "M4A": ".m4a", "WAV": ".wav", "FLAC": ".flac", "OGG": ".ogg"}
SAME_AS = {"mp3": "MP3", "m4a": "M4A", "aac": "M4A", "wav": "WAV", "flac": "FLAC", "ogg": "OGG", "oga": "OGG"}

# How each format is written (also used to re-save edits in the original's format)
CODEC = {
    ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k"],
    ".aac": ["-c:a", "aac", "-b:a", "192k"],
    ".wav": ["-c:a", "pcm_s16le"],
    ".flac": ["-c:a", "flac"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    ".oga": ["-c:a", "libvorbis", "-q:a", "5"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k"],
    ".aiff": ["-c:a", "pcm_s16be"],
    ".aif": ["-c:a", "pcm_s16be"],
}
ALREADY = {".mp3": "mp3", ".m4a": "aac", ".aac": "aac", ".flac": "flac", ".ogg": "vorbis", ".oga": "vorbis",
           ".opus": "opus", ".wav": "pcm_s16le"}

QUALITY_KBPS = {"High": 192, "Balanced": 128, "Small": 96, "Voice": 64}
MP3_STEPS = [320, 256, 192, 160, 128, 112, 96, 80, 64, 56, 48, 40, 32]   # valid MP3 bitrates
MIN_KBPS = 32
OVERHEAD = 0.97          # leave room for headers and tags when fitting a size
VOLUME_MODES = {"Even out": "loudnorm=I=-16:TP=-1.5:LRA=11", "+6 dB": "volume=6dB", "+3 dB": "volume=3dB",
                "-3 dB": "volume=-3dB", "-6 dB": "volume=-6dB"}
LIMITER = "alimiter=limit=0.97:level=disabled"   # louder never means clipped
NO_VIDEO = ["-vn", "-sn", "-dn"]                  # drop cover art and anything else that isn't sound


def targets_for(src_ext: str) -> list[str]:
    same = SAME_AS.get(src_ext.lower().lstrip("."))
    return [t for t in TARGETS if t != same]


def _sound(src: Path) -> Info:
    info = probe(src)
    if not info.has_audio:
        raise UserError("This file has no sound to work with.")
    return info


def _out_ext(src: Path) -> str:
    """Edits are saved in the original's format when we can write it, otherwise as MP3."""
    ext = src.suffix.lower()
    return ext if ext in CODEC else ".mp3"


# ---------------------------------------------------------------- convert
def convert(src: str | Path, target: str, ctl: Control) -> list[Path]:
    src = Path(src)
    info = _sound(src)
    ext = EXT[target]
    codec = ["-c:a", "copy"] if ALREADY.get(ext) == info.acodec else CODEC[ext]
    out = sibling(src, ext=ext)
    with writing(out) as tmp:
        run(["-i", str(src), "-map", "0:a:0", *NO_VIDEO, *codec, str(tmp)], ctl, info.duration)
    return [out]


# ---------------------------------------------------------------- compress
def bitrate_for_size(info: Info, target_bytes: int) -> int:
    """The highest standard bitrate that keeps the file under `target_bytes`."""
    if info.duration <= 0:
        raise UserError("Couldn't read this file's length, so it can't be fitted to a size.")
    budget = target_bytes * 8 / 1000 / info.duration * OVERHEAD
    if budget < MIN_KBPS:
        need = MIN_KBPS * 1000 / 8 * info.duration / OVERHEAD
        raise UserError(f"{human_size(target_bytes)} is too small for {fmt_time(info.duration)} of audio. "
                        f"Try at least {human_size(need)}, or trim it first.")
    return next(k for k in MP3_STEPS if k <= budget)


def compress(src: str | Path, ctl: Control, quality: str = "Balanced",
             target_bytes: int | None = None) -> list[Path]:
    """MP3 (or AAC for .m4a/.aac sources) at a fixed bitrate. 'Voice' is also mono."""
    src = Path(src)
    info = _sound(src)
    aac = src.suffix.lower() in (".m4a", ".aac")
    if target_bytes is not None:
        if target_bytes >= file_size(src):
            raise UserError(f"It's already {human_size(file_size(src))}, under {human_size(target_bytes)}.")
        kbps = bitrate_for_size(info, target_bytes)
        mono = kbps <= 64
    else:
        kbps = QUALITY_KBPS[quality]
        mono = quality == "Voice"
    channels = ["-ac", "1"] if mono or info.channels == 1 else ["-ac", "2"]
    codec = ["-c:a", "aac"] if aac else ["-c:a", "libmp3lame"]
    out = sibling(src, "Compressed", ext=".m4a" if aac else ".mp3")
    with writing(out) as tmp:
        run(["-i", str(src), "-map", "0:a:0", *NO_VIDEO, *codec, "-b:a", f"{kbps}k", *channels, str(tmp)],
            ctl, info.duration)
    return [out]


# ---------------------------------------------------------------- trim
def trim(src: str | Path, ctl: Control, start: float, end: float) -> list[Path]:
    """Copies the sound as-is (no quality loss); audio frames are tiny, so cuts land within ~25 ms."""
    src = Path(src)
    info = _sound(src)
    end = min(end, info.duration) if info.duration else end
    if start < 0 or end - start < 0.1:
        raise UserError("Pick a part of the sound at least 0.1 seconds long.")
    length = end - start
    out = sibling(src, "Trimmed")
    flags = ["-movflags", "+faststart"] if out.suffix.lower() in (".m4a", ".aac") else []
    with writing(out) as tmp:
        run(["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}", "-map", "0:a:0", *NO_VIDEO,
             "-c:a", "copy", *flags, str(tmp)], ctl, length)
    return [out]


# ---------------------------------------------------------------- volume
def change_volume(src: str | Path, ctl: Control, mode: str) -> list[Path]:
    """'Even out' = EBU R128 loudness normalisation; '+/-N dB' = a fixed change, never clipped."""
    if mode not in VOLUME_MODES:
        raise UserError(f"Unknown volume setting: {mode}")
    src = Path(src)
    info = _sound(src)
    af = VOLUME_MODES[mode]
    if mode.startswith("+") or mode == "Even out":
        af += "," + LIMITER
    label = "Normalized" if mode == "Even out" else ("Louder" if mode.startswith("+") else "Quieter")
    ext = _out_ext(src)
    rate = ["-ar", "48000"] if ext == ".opus" else ["-ar", "44100"] if mode == "Even out" else []
    out = sibling(src, label, ext=ext)
    with writing(out) as tmp:
        run(["-i", str(src), "-map", "0:a:0", *NO_VIDEO, "-af", af, *CODEC[ext], *rate, str(tmp)],
            ctl, info.duration)
    return [out]


# ---------------------------------------------------------------- waveform
def waveform(src: str | Path, width: int = 1200, height: int = 120) -> bytes:
    """A white waveform on transparency as PNG bytes, for the Trim timeline."""
    graph = (f"[0:a:0]aformat=channel_layouts=mono,"
             f"showwavespic=s={int(width)}x{int(height)}:colors=white:scale=sqrt")
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(src),
                        "-filter_complex", graph, "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
                       capture_output=True, **_popen_kw())
    if r.returncode != 0 or not r.stdout:
        raise UserError(_friendly(r.stderr.decode("utf-8", "replace")))
    return r.stdout
