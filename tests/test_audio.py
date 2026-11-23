"""Audio engine tests. Test sounds are generated with ffmpeg's built-in tone source."""
import re
import subprocess
from pathlib import Path

import pytest

from dragshift import audio, report, video
from dragshift.util import UserError, category, file_size

pytestmark = pytest.mark.skipif(not video.available(), reason="ffmpeg not available")


def make_tone(path: Path, seconds: float = 3, stereo: bool = True, volume_db: float = -20) -> Path:
    """A 440 Hz tone; ffmpeg picks the codec from the file extension."""
    layout = "stereo" if stereo else "mono"
    src = f"sine=frequency=440:sample_rate=44100:duration={seconds}"
    subprocess.run([video.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src,
                    "-af", f"volume={volume_db}dB,aformat=channel_layouts={layout}", str(path)], check=True)
    return path


def mean_volume(path: Path) -> float:
    r = subprocess.run([video.ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path), "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    return float(re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)[1])


@pytest.fixture
def wav(tmp_path) -> Path:
    return make_tone(tmp_path / "Voice memo.wav")


@pytest.fixture
def mp3(tmp_path) -> Path:
    return make_tone(tmp_path / "Song.mp3")


# ---------------------------------------------------------------- basics
@pytest.mark.parametrize("name", ["a.mp3", "b.M4A", "c.wav", "d.flac", "e.ogg", "f.opus", "g.aac", "h.aiff", "i.wma"])
def test_audio_category(name):
    assert category(name) == "audio"


def test_video_stays_video():
    assert category("clip.mp4") == "video"


def test_targets_skip_own_format():
    assert "MP3" not in audio.targets_for(".mp3")
    assert "M4A" not in audio.targets_for(".aac")       # same codec: nothing to convert
    assert audio.targets_for(".aiff") == audio.TARGETS


def test_probe_reads_channels(wav, tmp_path):
    assert video.probe(wav).channels == 2
    assert video.probe(make_tone(tmp_path / "mono.wav", stereo=False)).channels == 1


# ---------------------------------------------------------------- convert
@pytest.mark.parametrize("target, codec", [("MP3", "mp3"), ("M4A", "aac"), ("FLAC", "flac"), ("OGG", "vorbis")])
def test_convert_from_wav(wav, target, codec):
    out = audio.convert(wav, target, video.Control())[0]
    assert out.suffix == audio.EXT[target] and out.parent == wav.parent
    info = video.probe(out)
    assert info.acodec == codec and not info.has_video
    assert info.duration == pytest.approx(3, abs=0.2)


def test_convert_mp3_to_wav(mp3):
    out = audio.convert(mp3, "WAV", video.Control())[0]
    assert video.probe(out).acodec == "pcm_s16le"
    assert file_size(out) > file_size(mp3)


def test_convert_rejects_silent_file(tmp_path):
    silent = tmp_path / "picture.mp3"
    subprocess.run([video.ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x64",
                    "-frames:v", "1", "-c:v", "png", "-f", "image2", str(tmp_path / "p.png")], check=True)
    silent.write_bytes((tmp_path / "p.png").read_bytes())
    with pytest.raises(UserError):
        audio.convert(silent, "WAV", video.Control())


# ---------------------------------------------------------------- compress
def test_compress_quality_makes_mp3(wav):
    out = audio.compress(wav, video.Control(), quality="Balanced")[0]
    assert out.name == "Voice memo Compressed.mp3"
    assert file_size(out) < file_size(wav) / 5


def test_compress_voice_is_mono(wav):
    out = audio.compress(wav, video.Control(), quality="Voice")[0]
    assert video.probe(out).channels == 1


def test_compress_keeps_m4a_as_m4a(tmp_path):
    src = make_tone(tmp_path / "Memo.m4a")
    out = audio.compress(src, video.Control(), quality="Small")[0]
    assert out.suffix == ".m4a" and video.probe(out).acodec == "aac"


def test_compress_fits_size(tmp_path):
    src = make_tone(tmp_path / "Long.wav", seconds=20)
    target = 100_000
    out = audio.compress(src, video.Control(), target_bytes=target)[0]
    assert file_size(out) <= target
    assert video.probe(out).duration == pytest.approx(20, abs=0.5)


def test_compress_target_too_small(wav):
    with pytest.raises(UserError, match="too small"):
        audio.compress(wav, video.Control(), target_bytes=500)


def test_plan_bitrate():
    info = video.Info(duration=60, width=0, height=0, vcodec="", acodec="mp3", fps=0, channels=2)
    assert audio.bitrate_for_size(info, 1_000_000) == 128        # ≈130 kbps budget → nearest step down
    assert audio.bitrate_for_size(info, 100_000_000) == 320      # never above 320


# ---------------------------------------------------------------- trim + volume
def test_trim_keeps_format(mp3):
    out = audio.trim(mp3, video.Control(), 0.5, 2.0)[0]
    assert out.name == "Song Trimmed.mp3"
    assert video.probe(out).duration == pytest.approx(1.5, abs=0.15)


def test_trim_rejects_empty_range(mp3):
    with pytest.raises(UserError):
        audio.trim(mp3, video.Control(), 1.0, 1.0)


def test_volume_louder_and_quieter(wav):
    before = mean_volume(wav)
    louder = audio.change_volume(wav, video.Control(), "+6 dB")[0]
    quieter = audio.change_volume(wav, video.Control(), "-6 dB")[0]
    assert louder.name == "Voice memo Louder.wav" and quieter.name == "Voice memo Quieter.wav"
    assert mean_volume(louder) == pytest.approx(before + 6, abs=1)
    assert mean_volume(quieter) == pytest.approx(before - 6, abs=1)


def test_volume_even_out_raises_quiet_audio(tmp_path):
    quiet = make_tone(tmp_path / "Quiet.mp3", volume_db=-40)
    out = audio.change_volume(quiet, video.Control(), "Even out")[0]
    assert out.suffix == ".mp3" and mean_volume(out) > mean_volume(quiet) + 10


def test_unwritable_format_saves_as_mp3(tmp_path):
    src = make_tone(tmp_path / "Old.wma")
    out = audio.change_volume(src, video.Control(), "+3 dB")[0]
    assert out.suffix == ".mp3"


# ---------------------------------------------------------------- waveform + progress
def test_waveform_png(wav):
    data = audio.waveform(wav, 600, 80)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_cancel_leaves_nothing(wav):
    ctl = video.Control()
    ctl.cancel()
    with pytest.raises(video.Cancelled):
        audio.convert(wav, "FLAC", ctl)
    assert sorted(p.name for p in wav.parent.iterdir()) == ["Voice memo.wav"]


# ---------------------------------------------------------------- notification text
def test_summaries(wav):
    mp3 = audio.convert(wav, "MP3", video.Control())
    r = report.summarize("convert:MP3", [wav], mp3)
    assert r.title == "WAV → MP3" and "0:03" in r.detail
    loud = audio.change_volume(wav, video.Control(), "+3 dB")
    r = report.summarize("tool:Volume", [wav], loud, "+3 dB")
    assert r.title == "Volume changed" and "+3 dB" in r.detail
    c = audio.compress(wav, video.Control(), quality="Small")
    assert report.summarize("tool:Compress", [wav], c).good.endswith("smaller")
