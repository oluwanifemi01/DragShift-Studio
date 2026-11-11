"""Video windows and menu wiring, driven offscreen (no real display needed)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, Qt, QThreadPool
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from dragshift import tools_video, video
from dragshift.util import UserError

from test_video import make_clip

pytestmark = pytest.mark.skipif(not video.available(), reason="ffmpeg not available")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def clip(tmp_path):
    return make_clip(tmp_path / "Clip.mp4", seconds=4)


class Notes:
    def __init__(self):
        self.calls = []

    def __call__(self, action, srcs, outs, extra=""):
        self.calls.append((action, outs, extra))


def settle(qapp):
    """Let background preview jobs finish and deliver their results."""
    QThreadPool.globalInstance().waitForDone(10000)
    for _ in range(5):
        qapp.processEvents()


def press(widget, x, y=20, kind=QMouseEvent.MouseButtonPress):
    e = QMouseEvent(kind, QPointF(x, y), QPointF(x, y), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(widget, e)


def test_menu_rings_for_video(qapp, clip):
    from dragshift.app import DragShiftApp
    rings = DragShiftApp.build_rings(None, [str(clip)])
    assert [i.label for i in rings["Convert"]] == ["WEBM", "MOV", "GIF", "MP3", "M4A"]
    assert [i.label for i in rings["Tools"]] == ["Compress", "Trim", "Resize", "GIF", "Mute", "Frame"]


def test_trim_window_drag_and_save(qapp, clip):
    notes = Notes()
    w = tools_video.TrimWindow(clip, notes)
    w.resize(820, 640)
    tl = w.timeline
    tl.resize(800, 90)
    settle(qapp)
    assert not w.view.image.isNull()                 # first frame arrived
    assert len(tl.thumbs) == tools_video.STRIP_THUMBS
    press(tl, tl.x_of(0))                            # grab the start handle…
    press(tl, tl.x_of(1.0), kind=QMouseEvent.MouseMove)
    press(tl, tl.x_of(1.0), kind=QMouseEvent.MouseButtonRelease)
    assert tl.start == pytest.approx(1.0, abs=0.05) and tl.end == pytest.approx(4.0, abs=0.1)
    assert w.length.text().startswith("0:01.0")
    w.precise.setChecked(True)
    w.save()
    (action, outs, _), = notes.calls
    assert action == "tool:Trim" and video.probe(outs[0]).duration == pytest.approx(3.0, abs=0.15)


def test_frame_window_keyboard(qapp, clip):
    notes = Notes()
    w = tools_video.FrameWindow(clip, notes)
    w.timeline.resize(800, 90)
    from PySide6.QtGui import QKeyEvent
    for _ in range(2):
        QApplication.sendEvent(w.timeline, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Right, Qt.ShiftModifier))
    assert w.timeline.pos == pytest.approx(2.0)
    w.save()
    assert notes.calls[0][1][0].name == "Clip Frame 0-02.0.png"
    settle(qapp)


def test_gif_window_limits(qapp, clip):
    notes = Notes()
    w = tools_video.GifWindow(clip, notes)
    assert "480×270" in w.summary.text() and w.primary_btn.isEnabled()
    w.fps.button(0).click()
    w.width_grp.button(0).click()
    assert "320×180" in w.summary.text() and "40 frames" in w.summary.text()
    w.save()
    assert notes.calls[0][0] == "tool:GIF"
    settle(qapp)


def test_compress_window_estimates(qapp, clip):
    notes = Notes()
    w = tools_video.CompressVideoWindow([clip], notes)
    assert "360p → 360p" in w.estimate.text()
    w.mode.button(1).click()                     # fit a size limit
    assert w.res.button(0).text() == "Auto"
    assert not w.primary_btn.isEnabled()         # a 4 s test clip is already under 10 MB
    w.limit.button(4).click()                    # custom
    assert w.custom.isVisible() or not w.isVisible()
    w.custom.setValue(1)
    w.res.button(3).click()                      # 480p is above the source: keeps original
    assert "kbps" in w.estimate.text()


def test_resize_window_choices(qapp, tmp_path):
    hd = make_clip(tmp_path / "HD.mp4", seconds=1, size="1280x720")
    notes = Notes()
    w = tools_video.ResizeVideoWindow([hd], notes)
    assert w.choices == [540, 480, 360]
    assert w.out.text() == "1280×720  →  960×540"
    w.grp.button(2).click()
    assert w.out.text().endswith("640×360")
    w.save()
    assert video.probe(notes.calls[0][1][0]).height == 360


def test_resize_window_refuses_tiny_video(qapp, tmp_path):
    tiny = make_clip(tmp_path / "Tiny.mp4", seconds=1, size="320x240")
    with pytest.raises(UserError, match="smallest"):
        tools_video.ResizeVideoWindow([tiny], Notes())


def test_audio_only_file_is_refused(qapp, tmp_path, clip):
    m4a = video.convert(clip, "M4A", video.Control())[0]
    renamed = m4a.with_name("Sound only.mp4")   # a new name: Windows won't rename over Clip.mp4
    m4a.rename(renamed)
    with pytest.raises(UserError, match="no picture"):
        tools_video.TrimWindow(renamed, Notes())
