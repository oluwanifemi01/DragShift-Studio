"""Audio tool windows: Compress, Trim (on a waveform) and Volume."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLabel, QSpinBox, QVBoxLayout, QWidget

from . import audio, video
from .tools import section, toggle_row
from .tools_video import Timeline, VideoToolWindow, call_async
from .util import UserError, file_size, human_size

MB = 1000 * 1000


def channels_text(n: int) -> str:
    return {0: "", 1: "mono", 2: "stereo"}.get(n, f"{n} channels")


class AudioToolWindow(VideoToolWindow):
    needs = "sound"

    def __init__(self, title: str, paths, notify, size=(640, 420)):
        super().__init__(title, paths, notify, size=size)
        if len(self.paths) == 1:
            i = self.info
            bits = [self.path.name, human_size(file_size(self.path)), video.fmt_time(i.duration),
                    channels_text(i.channels)]
            self.subtitle.setText("  ·  ".join(b for b in bits if b))


# ---------------------------------------------------------------- Trim
class AudioTrimWindow(AudioToolWindow):
    tool_name = "Trim"

    def __init__(self, path, notify):
        super().__init__("Trim audio", path, notify, size=(780, 380))
        self.timeline = Timeline(self.info.duration, "range")
        self.timeline.STRIP_H = 110
        self.timeline.setMinimumHeight(self.timeline.STRIP_H + 30)
        self.body.addWidget(self.timeline)
        self.length = QLabel()
        self.length.setObjectName("big")
        self.body.addWidget(self.length)
        hint = QLabel("Drag the white handles, or click one and use ← → (Shift for 1 s steps). "
                      "The sound is copied as-is, so there's no quality loss.")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        self.body.addWidget(hint)
        self.body.addStretch(1)
        self.body.addLayout(self.footer("Save trimmed", self.save, on_reset=self.reset))
        self.timeline.range_changed.connect(self._update)
        self._update(self.timeline.start, self.timeline.end)
        src = self.path
        call_async(0, lambda: QImage.fromData(audio.waveform(src, 1400, 220)), self._wave)

    def _wave(self, _tag, img):
        try:
            if img is not None and not img.isNull():
                self.timeline.set_backdrop(img)
        except RuntimeError:     # window closed before the waveform arrived
            pass

    def _update(self, a: float, b: float):
        self.length.setText(f"{video.fmt_time(a, True)} – {video.fmt_time(b, True)}   ·   "
                            f"{b - a:.1f} s of {video.fmt_time(self.info.duration)}")

    def reset(self):
        tl = self.timeline
        tl.start, tl.end, tl.pos = 0.0, tl.duration, 0.0
        tl.update()
        self._update(tl.start, tl.end)

    def save(self):
        a, b, src = self.timeline.start, self.timeline.end, self.path
        self.run_video(lambda ctl: audio.trim(src, ctl, a, b), "Trimming…")


# ---------------------------------------------------------------- Compress
class AudioCompressWindow(AudioToolWindow):
    tool_name = "Compress"
    QUALITY = ["High", "Balanced", "Small", "Voice"]
    QUALITY_TEXT = {"High": "192 kbps · music sounds the same",
                    "Balanced": "128 kbps · fine for most music",
                    "Small": "96 kbps · podcasts and casual listening",
                    "Voice": "64 kbps mono · speech, lectures, voice memos"}
    LIMITS = [("5 MB", 5), ("10 MB", 10), ("25 MB", 25), ("Custom", None)]

    def __init__(self, paths, notify):
        super().__init__("Compress audio", paths, notify, size=(620, 430))
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
        sl.addWidget(section("Make each file under"))
        r, self.limit = toggle_row([n for n, _ in self.LIMITS], 1)
        self.custom = QSpinBox()
        self.custom.setRange(1, 2000)
        self.custom.setValue(8)
        self.custom.setSuffix(" MB")
        self.custom.setFixedWidth(100)
        r.insertWidget(r.count() - 1, self.custom)
        sl.addLayout(r)
        self.s_box.setVisible(False)
        self.body.addWidget(self.s_box)

        self.estimate = QLabel()
        self.estimate.setObjectName("big")
        self.estimate.setWordWrap(True)
        self.body.addWidget(self.estimate)
        self.body.addStretch(1)
        n = len(self.paths)
        self.body.addLayout(self.footer("Compress" if n == 1 else f"Compress {n} files", self.save))
        for grp in (self.mode, self.quality, self.limit):
            grp.idClicked.connect(lambda *_: self._update())
        self.custom.valueChanged.connect(lambda *_: self._update())
        self._update()

    def _target(self) -> int | None:
        if self.mode.checkedId() != 1:
            return None
        return (self.LIMITS[self.limit.checkedId()][1] or self.custom.value()) * MB

    def _format(self) -> str:
        return "M4A (AAC)" if self.path.suffix.lower() in (".m4a", ".aac") else "MP3"

    def _update(self):
        sizing = self.mode.checkedId() == 1
        self.q_box.setVisible(not sizing)
        self.s_box.setVisible(sizing)
        self.custom.setVisible(sizing and self.LIMITS[self.limit.checkedId()][1] is None)
        q = self.QUALITY[self.quality.checkedId()]
        self.q_hint.setText(self.QUALITY_TEXT[q])
        ok, info = True, self.info
        try:
            if sizing:
                target = self._target()
                kbps = audio.bitrate_for_size(info, target)
                if len(self.paths) == 1 and file_size(self.path) <= target:
                    raise UserError(f"Already under that ({human_size(file_size(self.path))})")
            else:
                kbps = audio.QUALITY_KBPS[q]
            size = kbps * 1000 / 8 * info.duration
            mono = "  ·  mono" if (sizing and kbps <= 64) or (not sizing and q == "Voice") else ""
            text = f"{kbps} kbps {self._format()}{mono}  ·  about {human_size(size)}"
        except UserError as e:
            ok, text = False, str(e)
        self.estimate.setText(text)
        if self.primary_btn:
            self.primary_btn.setEnabled(ok)

    def save(self):
        paths, target = list(self.paths), self._target()
        quality = self.QUALITY[self.quality.checkedId()]

        def one(p: Path, ctl: video.Control) -> list[Path]:
            return audio.compress(p, ctl, quality=quality, target_bytes=target)

        extra = f"under {human_size(target)}" if target else quality.lower()
        self.run_video(lambda ctl: video.each(paths, ctl, one), "Compressing…", extra=extra)


# ---------------------------------------------------------------- Volume
class AudioVolumeWindow(AudioToolWindow):
    tool_name = "Volume"
    MODES = ["Even out", "+3 dB", "+6 dB", "-3 dB", "-6 dB"]
    LABELS = ["Even out", "Louder +3 dB", "Louder +6 dB", "Quieter −3 dB", "Quieter −6 dB"]
    TEXT = {"Even out": "Brings the loudness to a standard level (like streaming apps do). "
                        "Best for quiet recordings or files that sound too loud next to others.",
            "+3 dB": "A little louder. Peaks are held back so it never distorts.",
            "+6 dB": "Clearly louder (about twice the sound pressure). Peaks are held back so it never distorts.",
            "-3 dB": "A little quieter.",
            "-6 dB": "Clearly quieter."}

    def __init__(self, paths, notify):
        super().__init__("Volume", paths, notify, size=(640, 300))
        self.body.addWidget(section("Change"))
        row, self.mode = toggle_row(self.LABELS, 0)
        self.body.addLayout(row)
        self.desc = QLabel()
        self.desc.setObjectName("dim")
        self.desc.setWordWrap(True)
        self.body.addWidget(self.desc)
        self.body.addStretch(1)
        n = len(self.paths)
        self.body.addLayout(self.footer("Apply" if n == 1 else f"Apply to {n} files", self.save))
        self.mode.idClicked.connect(lambda *_: self._update())
        self._update()

    def _update(self):
        self.desc.setText(self.TEXT[self.MODES[self.mode.checkedId()]])

    def save(self):
        paths, mode = list(self.paths), self.MODES[self.mode.checkedId()]

        def one(p: Path, ctl: video.Control) -> list[Path]:
            return audio.change_volume(p, ctl, mode)

        extra = "evened out" if mode == "Even out" else mode.replace("-", "−")
        self.run_video(lambda ctl: video.each(paths, ctl, one), "Adjusting volume…", extra=extra)
