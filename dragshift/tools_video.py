"""Video tool windows: Compress, Resize, Trim, GIF and Frame.

Previews come from ffmpeg in the background (a thumbnail strip plus the frame under the
handle you're dragging), so the window never freezes while you scrub."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget)

from . import video
from .tools import ToolWindow, section, toggle_row
from .util import UserError, file_size, human_size, keep, release, ui_font
from .window import ImageView

MB = 1000 * 1000      # decimal MB: what upload limits like "10 MB" mean (and the safer side)
PREVIEW_WIDTH = 960
STRIP_THUMBS = 10


# ---------------------------------------------------------------- background frame grabs
class _CallSig(QObject):
    done = Signal(object, object)    # (tag, result or None)


class _Call(QRunnable):
    """Run `fn` on the thread pool and hand its result (None on failure) back to the UI thread."""

    def __init__(self, tag, fn: Callable[[], object]):
        super().__init__()
        self.tag, self.fn = tag, fn
        self.sig = _CallSig()

    def run(self):
        try:
            res = self.fn()
        except Exception:  # noqa: BLE001 - a missed preview frame is harmless
            res = None
        self.sig.done.emit(self.tag, res)


def call_async(tag, fn: Callable[[], object], on_done: Callable[[object, object], None]) -> None:
    job = _Call(tag, fn)

    def finished(t, res):
        release(job)
        on_done(t, res)

    job.sig.done.connect(finished)
    QThreadPool.globalInstance().start(keep(job))


def frame_image(path: Path, t: float, width: int) -> QImage | None:
    img = QImage.fromData(video.grab_frame(path, t, width))
    return None if img.isNull() else img


class FrameLoader(QObject):
    """Fetches one preview frame at a time; while busy, only the latest request is kept."""
    ready = Signal(QImage)

    def __init__(self, path: Path, duration: float, width: int = PREVIEW_WIDTH, parent=None):
        super().__init__(parent)
        self.path, self.duration, self.width = path, duration, width
        self._busy = False
        self._want: float | None = None
        self._alive = True
        self.destroyed.connect(self._stop)

    def _stop(self, *_):
        self._alive = False

    def request(self, t: float) -> None:
        # never ask for the very last instant: there may be no frame to decode there
        self._want = min(max(0.0, t), max(0.0, self.duration - 0.08))
        if not self._busy:
            self._start()

    def _start(self):
        t, self._want = self._want, None
        self._busy = True
        call_async(t, lambda: frame_image(self.path, t, self.width), self._done)

    def _done(self, _t, img):
        self._busy = False
        if not self._alive:
            return
        if img is not None:
            self.ready.emit(img)
        if self._want is not None:
            self._start()


# ---------------------------------------------------------------- timeline
class Timeline(QWidget):
    """Thumbnail strip with either two trim handles (range mode) or one playhead (point mode)."""
    range_changed = Signal(float, float)
    scrubbed = Signal(float)

    HANDLE = 10
    STRIP_H = 54

    def __init__(self, duration: float, mode: str = "range", start: float = 0.0, end: float | None = None,
                 parent=None):
        super().__init__(parent)
        self.duration = max(duration, 0.1)
        self.mode = mode
        self.start = start
        self.end = self.duration if end is None else min(end, self.duration)
        self.pos = start
        self.thumbs: dict[int, QImage] = {}
        self._drag: str | None = None
        self._active = "pos" if mode == "point" else "start"
        self.setMinimumHeight(self.STRIP_H + 30)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)

    # geometry
    def strip(self) -> QRectF:
        return QRectF(self.HANDLE + 2, 4, self.width() - 2 * self.HANDLE - 4, self.STRIP_H)

    def x_of(self, t: float) -> float:
        s = self.strip()
        return s.left() + s.width() * t / self.duration

    def t_of(self, x: float) -> float:
        s = self.strip()
        return min(max((x - s.left()) / max(1.0, s.width()) * self.duration, 0.0), self.duration)

    def set_thumb(self, i: int, img: QImage) -> None:
        self.thumbs[i] = img
        self.update()

    # input
    def mousePressEvent(self, e):
        x = e.position().x()
        if self.mode == "range":
            ds, de = abs(x - self.x_of(self.start)), abs(x - self.x_of(self.end))
            if min(ds, de) <= self.HANDLE + 4:
                self._drag = "start" if ds <= de else "end"
            else:
                self._drag = "pos"
        else:
            self._drag = "pos"
        self._active = self._drag
        self._move_to(self.t_of(x))

    def mouseMoveEvent(self, e):
        if self._drag:
            self._move_to(self.t_of(e.position().x()))

    def mouseReleaseEvent(self, e):
        self._drag = None

    def keyPressEvent(self, e):
        step = 1.0 if e.modifiers() & Qt.ShiftModifier else 0.1
        if e.key() in (Qt.Key_Left, Qt.Key_Right):
            cur = {"start": self.start, "end": self.end}.get(self._active, self.pos)
            self._drag = self._active
            self._move_to(cur + (step if e.key() == Qt.Key_Right else -step))
            self._drag = None
        else:
            super().keyPressEvent(e)

    def _move_to(self, t: float):
        t = min(max(t, 0.0), self.duration)
        gap = min(0.1, self.duration)
        if self._drag == "start":
            self.start = min(t, self.end - gap)
            self.pos = self.start
            self.range_changed.emit(self.start, self.end)
        elif self._drag == "end":
            self.end = max(t, self.start + gap)
            self.pos = self.end
            self.range_changed.emit(self.start, self.end)
        else:
            self.pos = t
        self.scrubbed.emit(self.pos)
        self.update()

    # paint
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        s = self.strip()
        clip = QPainterPath()
        clip.addRoundedRect(s, 8, 8)
        p.save()
        p.setClipPath(clip)
        p.fillRect(s, QColor(255, 255, 255, 25))
        cell = s.width() / STRIP_THUMBS
        for i, img in self.thumbs.items():
            r = QRectF(s.left() + i * cell, s.top(), cell + 1, s.height())
            src = QRectF(img.rect())
            want = r.width() / r.height()          # crop the thumb to the cell's shape
            if src.width() / src.height() > want:
                w = src.height() * want
                src = QRectF(src.center().x() - w / 2, 0, w, src.height())
            p.drawImage(r, img, src)
        if self.mode == "range":                   # dim what will be cut away
            shade = QColor(0, 0, 0, 150)
            p.fillRect(QRectF(s.left(), s.top(), self.x_of(self.start) - s.left(), s.height()), shade)
            p.fillRect(QRectF(self.x_of(self.end), s.top(), s.right() - self.x_of(self.end), s.height()), shade)
        p.restore()

        white = QColor(255, 255, 255)
        if self.mode == "range":
            x0, x1 = self.x_of(self.start), self.x_of(self.end)
            p.setPen(QPen(white, 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(x0, s.top(), x1 - x0, s.height()), 6, 6)
            p.setPen(Qt.NoPen)
            p.setBrush(white)
            for x in (x0, x1):
                p.drawRoundedRect(QRectF(x - self.HANDLE / 2, s.top() - 2, self.HANDLE, s.height() + 4), 4, 4)
            p.setBrush(QColor(0, 0, 0, 120))
            for x in (x0, x1):
                p.drawRoundedRect(QRectF(x - 1, s.center().y() - 9, 2, 18), 1, 1)
        # playhead
        xp = self.x_of(self.pos)
        p.setPen(QPen(QColor(255, 220, 120), 2))
        p.drawLine(QPointF(xp, s.top() - 3), QPointF(xp, s.bottom() + 3))

        # times underneath
        p.setFont(ui_font(11, display=False))
        p.setPen(QColor(255, 255, 255, 190))
        y = s.bottom() + 6
        if self.mode == "range":
            p.drawText(QRectF(s.left(), y, s.width(), 18), Qt.AlignLeft,
                       video.fmt_time(self.start, precise=True))
            p.drawText(QRectF(s.left(), y, s.width(), 18), Qt.AlignRight,
                       video.fmt_time(self.end, precise=True))
        else:
            p.drawText(QRectF(s.left(), y, s.width(), 18), Qt.AlignHCenter,
                       f"{video.fmt_time(self.pos, precise=True)} / {video.fmt_time(self.duration)}")


# ---------------------------------------------------------------- base windows
class VideoToolWindow(ToolWindow):
    """Adds probing, cancellable background saving and a subtitle with length and size."""

    def __init__(self, title: str, paths, notify, size=(760, 600)):
        super().__init__(title, paths, notify, size=size)
        self.infos = [video.probe(p) for p in self.paths]
        if not all(i.has_video for i in self.infos):
            raise UserError("This file has sound but no picture.")
        self.info = self.infos[0]
        if len(self.paths) == 1:
            i = self.info
            self.subtitle.setText(f"{self.path.name}  ·  {human_size(file_size(self.path))}  ·  "
                                  f"{i.size_text}  ·  {video.fmt_time(i.duration)}")

    def run_video(self, fn: Callable[[video.Control], list], working: str, extra: str = ""):
        action = f"tool:{self.tool_name}"
        srcs = [str(p) for p in self.paths]
        if self.submit is not None:
            self.submit(action, srcs, fn, working, extra, cancellable=True)
        else:
            self.notify(action, srcs, fn(video.Control()), extra)
        self.close()


class TimelineWindow(VideoToolWindow):
    """A big frame preview over a timeline. Used by Trim, GIF and Frame."""
    mode = "range"

    def __init__(self, title: str, path, notify, start: float = 0.0, end: float | None = None):
        super().__init__(title, path, notify, size=(820, 640))
        self.view = ImageView(QImage())
        self.body.addWidget(self.view, 1)
        self.timeline = Timeline(self.info.duration, self.mode, start, end)
        self.body.addWidget(self.timeline)
        self.loader = FrameLoader(self.path, self.info.duration, parent=self)
        self.loader.ready.connect(self.view.set_image)
        self.timeline.scrubbed.connect(self.loader.request)
        self.loader.request(start)
        self._load_strip()

    def _load_strip(self):
        d = self.info.duration
        for i in range(STRIP_THUMBS):
            t = d * (i + 0.5) / STRIP_THUMBS
            call_async(i, lambda t=t: frame_image(self.path, t, 160), self._thumb)

    def _thumb(self, i, img):
        try:
            if img is not None:
                self.timeline.set_thumb(i, img)
        except RuntimeError:   # window closed before the strip finished
            pass


# ---------------------------------------------------------------- Trim
class TrimWindow(TimelineWindow):
    tool_name = "Trim"

    def __init__(self, path, notify):
        super().__init__("Trim", path, notify)
        self.length = QLabel()
        self.length.setObjectName("big")
        self.body.addWidget(self.length)
        self.precise = QCheckBox("Exact frame (slower: re-encodes the video)")
        self.precise.setToolTip("Without this, the cut starts on the nearest keyframe and saving is instant.")
        self.body.addWidget(self.precise)
        self.body.addLayout(self.footer("Save trimmed", self.save, on_reset=self.reset))
        self.timeline.range_changed.connect(self._update)
        self._update(self.timeline.start, self.timeline.end)

    def _update(self, a: float, b: float):
        self.length.setText(f"{video.fmt_time(a, True)} – {video.fmt_time(b, True)}   ·   "
                            f"{b - a:.1f} s of {video.fmt_time(self.info.duration)}")

    def reset(self):
        tl = self.timeline
        tl.start, tl.end, tl.pos = 0.0, tl.duration, 0.0
        tl.update()
        self._update(tl.start, tl.end)
        self.loader.request(0.0)

    def save(self):
        a, b, exact, src = self.timeline.start, self.timeline.end, self.precise.isChecked(), self.path
        self.run_video(lambda ctl: video.trim(src, ctl, a, b, precise=exact), "Trimming…")


# ---------------------------------------------------------------- GIF
class GifWindow(TimelineWindow):
    tool_name = "GIF"
    FPS = [10, 12, 15, 24]
    WIDTHS = [320, 480, 640, 800]
    MAX_SECONDS = 60

    def __init__(self, path, notify):
        info = video.probe(path)
        super().__init__("Make GIF", path, notify, end=min(info.duration, 5.0))
        row = QHBoxLayout()
        row.addWidget(QLabel("Frames per second"))
        r1, self.fps = toggle_row([str(f) for f in self.FPS], 1)
        row.addLayout(r1)
        row.addSpacing(12)
        row.addWidget(QLabel("Width"))
        r2, self.width_grp = toggle_row([f"{w} px" for w in self.WIDTHS], 1)
        row.addLayout(r2)
        self.body.addLayout(row)
        self.summary = QLabel()
        self.summary.setObjectName("big")
        self.body.addWidget(self.summary)
        self.body.addLayout(self.footer("Make GIF", self.save))
        self.timeline.range_changed.connect(lambda *_: self._update())
        self.fps.idClicked.connect(lambda *_: self._update())
        self.width_grp.idClicked.connect(lambda *_: self._update())
        self._update()

    def _choice(self) -> tuple[float, float, int, int]:
        a, b = self.timeline.start, self.timeline.end
        return a, b - a, self.FPS[self.fps.checkedId()], self.WIDTHS[self.width_grp.checkedId()]

    def _update(self):
        _, length, fps, width = self._choice()
        w, h = video.gif_size(self.info, width)
        text = f"{w}×{h}  ·  {length:.1f} s  ·  {int(length * fps)} frames"
        too_long = length > self.MAX_SECONDS
        if too_long:
            text = f"Pick {self.MAX_SECONDS} s or less (GIFs get huge)"
        elif length > 15:
            text += "  ·  long GIFs get big"
        self.summary.setText(text)
        if self.primary_btn:
            self.primary_btn.setEnabled(not too_long)

    def save(self):
        start, length, fps, width = self._choice()
        src, info = self.path, self.info
        self.run_video(lambda ctl: video.to_gif(src, ctl, start, length, fps, width, info=info),
                       "Making GIF…", extra=f"{length:.1f} s · {fps} fps")


# ---------------------------------------------------------------- Frame
class FrameWindow(TimelineWindow):
    tool_name = "Frame"
    mode = "point"

    def __init__(self, path, notify):
        super().__init__("Save a frame", path, notify)
        hint = QLabel("Drag along the strip, or use ← → (hold Shift for 1 s steps).")
        hint.setObjectName("dim")
        self.body.addWidget(hint)
        self.body.addLayout(self.footer("Save PNG", self.save))
        self.timeline.setFocus()

    def save(self):
        t, src = self.timeline.pos, self.path
        self.run_video(lambda ctl: video.save_frame(src, t), "Saving frame…",
                       extra=f"at {video.fmt_time(t, precise=True)}")


# ---------------------------------------------------------------- Compress
class CompressVideoWindow(VideoToolWindow):
    tool_name = "Compress"
    QUALITY = ["High", "Balanced", "Small"]
    QUALITY_TEXT = {"High": "Looks the same, saves a little", "Balanced": "Hard to tell apart, much smaller",
                    "Small": "Smallest file, some softness"}
    LIMITS = [("10 MB", 10), ("25 MB", 25), ("50 MB", 50), ("100 MB", 100), ("Custom", None)]
    HEIGHTS = [None, 1080, 720, 480]

    def __init__(self, paths, notify):
        super().__init__("Compress video", paths, notify, size=(640, 470))
        row, self.mode = toggle_row(["Smaller file", "Fit a size limit"])
        self.body.addLayout(row)

        self.q_box = QWidget()
        ql = QVBoxLayout(self.q_box)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.addWidget(section("Quality"))
        r, self.quality = toggle_row(self.QUALITY, 1)
        ql.addLayout(r)
        self.q_hint = QLabel()
        self.q_hint.setObjectName("dim")
        ql.addWidget(self.q_hint)
        self.body.addWidget(self.q_box)

        self.s_box = QWidget()
        sl = QVBoxLayout(self.s_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(section("Make each video under"))
        r, self.limit = toggle_row([n for n, _ in self.LIMITS], 0)
        self.custom = QSpinBox()
        self.custom.setRange(1, 4000)
        self.custom.setValue(15)
        self.custom.setSuffix(" MB")
        self.custom.setFixedWidth(100)
        r.insertWidget(r.count() - 1, self.custom)
        sl.addLayout(r)
        tip = QLabel("10 MB fits Discord · 25 MB fits most email · 100 MB fits WhatsApp")
        tip.setObjectName("dim")
        sl.addWidget(tip)
        self.s_box.setVisible(False)
        self.body.addWidget(self.s_box)

        self.body.addWidget(section("Resolution"))
        r, self.res = toggle_row(["Original", "1080p", "720p", "480p"], 0)
        self.body.addLayout(r)

        self.estimate = QLabel()
        self.estimate.setObjectName("big")
        self.estimate.setWordWrap(True)
        self.body.addWidget(self.estimate)
        self.body.addStretch(1)
        n = len(self.paths)
        self.body.addLayout(self.footer("Compress" if n == 1 else f"Compress {n} videos", self.save))
        for grp in (self.mode, self.quality, self.limit, self.res):
            grp.idClicked.connect(lambda *_: self._update())
        self.custom.valueChanged.connect(lambda *_: self._update())
        self._update()

    def _target(self) -> int | None:
        if self.mode.checkedId() != 1:
            return None
        mb = self.LIMITS[self.limit.checkedId()][1] or self.custom.value()
        return mb * MB

    def _height(self) -> int | None:
        return self.HEIGHTS[self.res.checkedId()]

    def _update(self):
        sizing = self.mode.checkedId() == 1
        self.q_box.setVisible(not sizing)
        self.s_box.setVisible(sizing)
        self.custom.setVisible(sizing and self.LIMITS[self.limit.checkedId()][1] is None)
        self.res.button(0).setText("Auto" if sizing else "Original")
        self.q_hint.setText(self.QUALITY_TEXT[self.QUALITY[self.quality.checkedId()]])
        ok, text = True, ""
        info, height = self.info, self._height()
        if sizing:
            try:
                plan = video.plan_for_size(info, self._target(), height)
                res = f"{plan.height}p" if plan.height else f"{info.height}p (original)"
                text = f"≈ {plan.video_kbps:,} kbps video  ·  {res}"
                if len(self.paths) == 1 and file_size(self.path) <= self._target() and height is None:
                    ok, text = False, f"Already under that ({human_size(file_size(self.path))})"
            except UserError as e:
                ok, text = False, str(e)
        else:
            out_h = height if height and height < info.height else info.height
            text = f"{info.height}p → {out_h}p  ·  MP4 (H.264)"
        self.estimate.setText(text)
        if self.primary_btn:
            self.primary_btn.setEnabled(ok)

    def save(self):
        paths, target, height = list(self.paths), self._target(), self._height()
        quality = self.QUALITY[self.quality.checkedId()]

        def one(p: Path, ctl: video.Control) -> list[Path]:
            return video.compress(p, ctl, quality=quality, target_bytes=target, height=height)

        extra = f"under {human_size(target)}" if target else quality.lower()
        self.run_video(lambda ctl: video.each(paths, ctl, one), "Compressing…", extra=extra)


# ---------------------------------------------------------------- Resize
class ResizeVideoWindow(VideoToolWindow):
    tool_name = "Resize"
    HEIGHTS = [2160, 1440, 1080, 720, 540, 480, 360]

    def __init__(self, paths, notify):
        super().__init__("Resize video", paths, notify, size=(600, 320))
        top = max(i.height for i in self.infos)
        self.choices = [h for h in self.HEIGHTS if h < top]
        if not self.choices:
            raise UserError(f"It's already {top}p, the smallest preset.")
        self.body.addWidget(section("New height"))
        default = self.choices.index(720) if 720 in self.choices else 0
        row, self.grp = toggle_row([f"{h}p" for h in self.choices], default)
        self.body.addLayout(row)
        self.out = QLabel()
        self.out.setObjectName("big")
        self.body.addWidget(self.out)
        self.body.addStretch(1)
        n = len(self.paths)
        self.body.addLayout(self.footer("Resize" if n == 1 else f"Resize {n} videos", self.save))
        self.grp.idClicked.connect(lambda *_: self._update())
        self._update()

    def _update(self):
        h = self.choices[self.grp.checkedId()]
        w = round(self.info.width * h / self.info.height)
        self.out.setText(f"{self.info.size_text}  →  {w - w % 2}×{h}")   # ffmpeg keeps width even

    def save(self):
        h, paths = self.choices[self.grp.checkedId()], list(self.paths)
        small = [p for p, i in zip(paths, self.infos) if i.height > h]

        def one(p: Path, ctl: video.Control) -> list[Path]:
            return video.resize(p, ctl, h)

        self.run_video(lambda ctl: video.each(small, ctl, one), f"Resizing to {h}p…")
