"""Video engine tests. Test clips are generated with ffmpeg's built-in test sources."""
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from dragshift import report, video
from dragshift.util import UserError, category, file_size

pytestmark = pytest.mark.skipif(not video.available(), reason="ffmpeg not available")


def make_clip(path: Path, seconds: float = 3, size: str = "640x360", audio: bool = True,
              noisy: bool = False) -> Path:
    src = f"testsrc2=size={size}:rate=25"
    if noisy:   # random noise doesn't compress, so the file is big and fitting a size is a real test
        src += ",noise=alls=60:allf=t"
    args = ["-f", "lavfi", "-i", src]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    args += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-shortest"] if audio else []
    subprocess.run([video.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args, str(path)],
                   check=True)
    return path


@pytest.fixture
def clip(tmp_path) -> Path:
    return make_clip(tmp_path / "Clip.mp4")


@pytest.fixture
def silent(tmp_path) -> Path:
    return make_clip(tmp_path / "Silent.mp4", audio=False)


def no_leftovers(folder: Path) -> bool:
    return not any(".dragshift-part" in p.name for p in folder.iterdir())


# ---------------------------------------------------------------- basics
@pytest.mark.parametrize("name", ["a.mp4", "b.MOV", "c.mkv", "d.webm", "e.avi", "f.m4v"])
def test_video_category(name):
    assert category(name) == "video"


def test_gif_stays_an_image():
    assert category("x.gif") == "image"


def test_probe(clip):
    info = video.probe(clip)
    assert (info.width, info.height) == (640, 360)
    assert info.vcodec == "h264" and info.acodec == "aac"
    assert info.duration == pytest.approx(3, abs=0.2)
    assert info.fps == pytest.approx(25)


def test_probe_rejects_non_video(tmp_path):
    p = tmp_path / "fake.mp4"
    p.write_text("not a video")
    with pytest.raises(UserError):
        video.probe(p)


def test_parse_info_rotated_phone_video_and_cover_art():
    text = """
  Duration: 00:01:02.50, start: 0.000000, bitrate: 9000 kb/s
  Stream #0:0[0x1](und): Video: hevc (Main) (hvc1 / 0x31637668), yuv420p(tv), 1920x1080, 30 fps
      Side data:
        displaymatrix: rotation of -90.00 degrees
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo
  Stream #0:2: Video: mjpeg (Baseline), yuvj420p, 600x600 [SAR 1:1 DAR 1:1], 90k tbr (attached pic)
"""
    info = video.parse_info(text)
    assert info.duration == pytest.approx(62.5)
    assert (info.width, info.height) == (1080, 1920)
    assert info.vcodec == "hevc" and info.acodec == "aac" and info.fps == 30


def test_targets_skip_own_format():
    assert "MP4" not in video.targets_for(".mp4")
    assert "MOV" not in video.targets_for(".MOV")
    assert video.targets_for(".mkv") == video.TARGETS


def test_fmt_time():
    assert video.fmt_time(5) == "0:05"
    assert video.fmt_time(83.4, precise=True) == "1:23.4"
    assert video.fmt_time(3725) == "1:02:05"


# ---------------------------------------------------------------- convert
@pytest.mark.parametrize("target", ["MOV", "WEBM", "MP3", "M4A"])
def test_convert_targets(clip, target):
    out = video.convert(clip, target, video.Control())[0]
    assert out.suffix == video.EXT[target] and out.parent == clip.parent
    info = video.probe(out)
    assert info.duration == pytest.approx(3, abs=0.3)
    if target in ("MP3", "M4A"):
        assert not info.has_video and info.acodec == {"MP3": "mp3", "M4A": "aac"}[target]
    else:
        assert (info.width, info.height) == (640, 360)
        assert info.vcodec == {"MOV": "h264", "WEBM": "vp9"}[target]
    assert no_leftovers(clip.parent)


def test_convert_to_gif(clip):
    out = video.convert(clip, "GIF", video.Control())[0]
    with Image.open(out) as im:
        assert im.format == "GIF" and im.size == (480, 270) and im.n_frames > 20


def test_convert_odd_size_to_mp4(tmp_path):
    src = tmp_path / "Odd.mkv"
    subprocess.run([video.ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=321x241:rate=10", "-t", "1", "-c:v", "ffv1", str(src)], check=True)
    out = video.convert(src, "MP4", video.Control())[0]
    info = video.probe(out)
    assert info.vcodec == "h264" and (info.width, info.height) == (320, 240)


def test_never_overwrites(clip):
    a = video.convert(clip, "MOV", video.Control())[0]
    b = video.convert(clip, "MOV", video.Control())[0]
    assert a != b and a.exists() and b.exists()


# ---------------------------------------------------------------- compress
def test_compress_quality(tmp_path):
    src = make_clip(tmp_path / "Big.mp4", noisy=True)
    out = video.compress(src, video.Control(), quality="Small", height=240)[0]
    assert out.name == "Big Compressed.mp4"
    assert file_size(out) < file_size(src)
    assert video.probe(out).height == 240


def test_compress_fits_target_size(tmp_path):
    src = make_clip(tmp_path / "Noise.mp4", seconds=4, size="1280x720", noisy=True)
    target = 300 * 1024
    assert file_size(src) > target
    out = video.compress(src, video.Control(), target_bytes=target)[0]
    assert file_size(out) <= target
    assert video.probe(out).acodec == "aac"
    assert no_leftovers(tmp_path)


def test_compress_target_too_small(clip):
    with pytest.raises(UserError, match="too small"):
        video.compress(clip, video.Control(), target_bytes=1000)


def test_plan_for_size_picks_resolution():
    info = video.Info(duration=60, width=1920, height=1080, vcodec="h264", acodec="aac", fps=30)
    small = video.plan_for_size(info, 10 * 1024 * 1024)        # ~1.3 Mbps total
    assert small.height == 720 and small.audio_kbps == 128
    big = video.plan_for_size(info, 500 * 1024 * 1024)
    assert big.height is None                                  # plenty of room: keep 1080p


# ---------------------------------------------------------------- trim / resize / mute
def test_trim_fast_and_precise(clip):
    fast = video.trim(clip, video.Control(), 1.0, 2.0)[0]
    assert fast.name == "Clip Trimmed.mp4"
    assert video.probe(fast).duration < 2.6
    exact = video.trim(clip, video.Control(), 0.5, 1.5, precise=True)[0]
    assert video.probe(exact).duration == pytest.approx(1.0, abs=0.1)


def test_trim_rejects_empty_range(clip):
    with pytest.raises(UserError):
        video.trim(clip, video.Control(), 2.0, 2.0)


def test_resize(clip):
    out = video.resize(clip, video.Control(), 240)[0]
    assert out.name == "Clip 240p.mp4"
    info = video.probe(out)
    assert (info.width, info.height) == (426, 240) and info.has_audio
    with pytest.raises(UserError):
        video.resize(clip, video.Control(), 1080)


def test_mute(clip):
    out = video.mute(clip, video.Control())[0]
    info = video.probe(out)
    assert info.has_video and not info.has_audio


def test_extract_audio_needs_sound(silent):
    with pytest.raises(UserError, match="no sound"):
        video.convert(silent, "MP3", video.Control())


# ---------------------------------------------------------------- GIF + frames
def test_gif_tool_range(clip):
    out = video.to_gif(clip, video.Control(), start=1.0, length=1.0, fps=10, width=320)[0]
    with Image.open(out) as im:
        assert im.size == (320, 180)
        assert 8 <= im.n_frames <= 12


def test_save_frame(clip):
    out = video.save_frame(clip, 1.5)[0]
    assert out.name == "Clip Frame 0-01.5.png"
    with Image.open(out) as im:
        assert im.size == (640, 360)
    preview = video.grab_frame(clip, 1.0, width=160)
    assert preview[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------- progress + cancel
def test_progress_reaches_the_end(clip):
    seen = []
    video.convert(clip, "WEBM", video.Control(seen.append))
    assert seen and seen[-1] == pytest.approx(1.0) and seen == sorted(seen)


def test_cancel_leaves_nothing(clip):
    ctl = video.Control()
    ctl.cancel()
    with pytest.raises(video.Cancelled):
        video.convert(clip, "WEBM", ctl)
    assert sorted(p.name for p in clip.parent.iterdir()) == ["Clip.mp4"]


def test_cancel_mid_run_kills_ffmpeg(tmp_path):
    src = make_clip(tmp_path / "Long.mp4", seconds=20, size="1280x720")
    ctl = video.Control()
    ctl.on_progress = lambda f: ctl.cancel() if f > 0.05 else None
    with pytest.raises(video.Cancelled):
        video.convert(src, "WEBM", ctl)
    assert no_leftovers(tmp_path) and not (tmp_path / "Long.webm").exists()


def test_each_splits_progress(clip, tmp_path):
    other = make_clip(tmp_path / "Two.mp4", seconds=1)
    seen = []
    outs = video.each([clip, other], video.Control(seen.append), video.mute)
    assert len(outs) == 2
    assert any(0.4 <= v <= 0.6 for v in seen) and seen[-1] == pytest.approx(1.0)


# ---------------------------------------------------------------- notification text
def test_summaries(clip):
    mov = video.convert(clip, "MOV", video.Control())
    r = report.summarize("convert:MOV", [clip], mov)
    assert r.title == "MP4 → MOV" and "640×360" in r.detail and "0:03" in r.detail
    t = video.trim(clip, video.Control(), 0, 1, precise=True)
    r = report.summarize("tool:Trim", [clip], t)
    assert r.title == "Trimmed" and "0:03 → 0:01" in r.detail
    s = video.resize(clip, video.Control(), 240)
    r = report.summarize("tool:Resize", [clip], s)
    assert "640×360 → 426×240" in r.detail
    m = video.mute(clip, video.Control())
    assert report.summarize("tool:Mute", [clip], m).title == "Sound removed"


def test_typescript_is_not_video():
    assert category("app.ts") is None and category("clip.mts") == "video"
