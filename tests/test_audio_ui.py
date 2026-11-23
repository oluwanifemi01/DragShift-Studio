"""Audio windows and menu wiring, driven offscreen."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from dragshift import tools_audio, video
from dragshift.util import UserError

from test_audio import make_tone, mean_volume

pytestmark = pytest.mark.skipif(not video.available(), reason="ffmpeg not available")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def song(tmp_path):
    return make_tone(tmp_path / "Song.mp3", seconds=6)


class Notes:
    def __init__(self):
        self.calls = []

    def __call__(self, action, srcs, outs, extra=""):
        self.calls.append((action, outs, extra))


def settle(qapp):
    QThreadPool.globalInstance().waitForDone(10000)
    for _ in range(5):
        qapp.processEvents()


def test_menu_rings_for_audio(qapp, song):
    from dragshift.app import DragShiftApp
    rings = DragShiftApp.build_rings(None, [str(song)])
    assert [i.label for i in rings["Convert"]] == ["M4A", "WAV", "FLAC", "OGG"]
    assert [i.label for i in rings["Tools"]] == ["Compress", "Trim", "Volume"]


def test_trim_window_shows_waveform_and_saves(qapp, song):
    notes = Notes()
    w = tools_audio.AudioTrimWindow(song, notes)
    assert "Song.mp3" in w.subtitle.text() and "stereo" in w.subtitle.text()
    settle(qapp)
    assert w.timeline.backdrop is not None and not w.timeline.backdrop.isNull()
    w.timeline.start, w.timeline.end = 1.0, 3.5
    w.save()
    (action, outs, _), = notes.calls
    assert action == "tool:Trim" and video.probe(outs[0]).duration == pytest.approx(2.5, abs=0.15)


def test_compress_window_estimates(qapp, song):
    w = tools_audio.AudioCompressWindow([song], Notes())
    assert w.estimate.text().startswith("128 kbps MP3")
    w.quality.button(3).click()                       # Voice
    assert "mono" in w.estimate.text() and "speech" in w.q_hint.text()
    w.mode.button(1).click()                          # fit a size limit
    assert not w.primary_btn.isEnabled()              # a 6 s tone is already under 10 MB


def test_volume_window_saves(qapp, song):
    notes = Notes()
    w = tools_audio.AudioVolumeWindow([song], notes)
    w.mode.button(2).click()                          # +6 dB
    assert "louder" in w.desc.text().lower()
    w.save()
    (action, outs, extra), = notes.calls
    assert action == "tool:Volume" and extra == "+6 dB"
    assert mean_volume(outs[0]) > mean_volume(song) + 4


def test_video_file_is_refused_by_audio_window_only_if_silent(qapp, tmp_path):
    import subprocess
    silent = tmp_path / "Silent.mp4"
    subprocess.run([video.ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x64",
                    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)], check=True)
    with pytest.raises(UserError, match="no sound"):
        tools_audio.AudioVolumeWindow([silent], Notes())
