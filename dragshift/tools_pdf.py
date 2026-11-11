"""PDF Split window: pick pages (click thumbnails or type ranges), every N pages, or every page.

Thumbnails render in the background a few pages at a time, so big PDFs open instantly."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QListView,
                               QListWidget, QListWidgetItem, QSpinBox, QVBoxLayout, QWidget)

from . import pages
from .convert import fitz, open_pdf
from .tools import ToolWindow, toggle_row
from .util import UserError, keep, release, ui_font

THUMB = QSize(96, 124)
BATCH = 8                       # pages rendered per background job

GRID_QSS = """
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { color: rgba(255,255,255,0.85); border-radius: 10px; padding: 4px; }
QListWidget::item:selected { background: rgba(120,170,255,0.38); color: white; }
QListWidget::item:hover:!selected { background: rgba(255,255,255,0.10); }
"""


# ---------------------------------------------------------------- thumbnails
class _ThumbSig(QObject):
    done = Signal(int, list)        # first page index, [QImage]


class _ThumbJob(QRunnable):
    def __init__(self, path: Path, start: int, count: int):
        super().__init__()
        self.path, self.start, self.count = path, start, count
        self.sig = _ThumbSig()

    def run(self):
        images: list[QImage] = []
        try:
            with fitz.open(self.path) as doc:
                for i in range(self.start, min(self.start + self.count, doc.page_count)):
                    page = doc[i]
                    zoom = THUMB.height() / max(1.0, page.rect.height)
                    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
                    images.append(img.copy())     # detach from fitz's buffer
        except Exception:  # noqa: BLE001 - placeholders stay; splitting still works
            pass
        self.sig.done.emit(self.start, images)


def placeholder(n: int) -> QIcon:
    pm = QPixmap(THUMB.width(), THUMB.height())
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 40))
    p.drawRoundedRect(8, 0, THUMB.width() - 16, THUMB.height(), 6, 6)
    p.setPen(QColor(255, 255, 255, 120))
    p.setFont(ui_font(18, bold=True))
    p.drawText(pm.rect(), Qt.AlignCenter, str(n))
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- window
class SplitWindow(ToolWindow):
    tool_name = "Split"
    MODES = ["Pick pages", "Every few pages", "Every page"]

    def __init__(self, path, notify):
        super().__init__("Split PDF", path, notify, size=(780, 640))
        with open_pdf(self.path) as doc:
            self.count = doc.page_count
        self.subtitle.setText(f"{self.subtitle.text()}  ·  {self.count} page{'s' if self.count != 1 else ''}")
        self._syncing = False
        self._alive = True
        self.destroyed.connect(lambda *_: setattr(self, "_alive", False))

        row, self.mode = toggle_row(self.MODES)
        self.body.addLayout(row)
        self._build_pick()
        self._build_every()
        self._build_grid()

        self.summary = QLabel()
        self.summary.setObjectName("big")
        self.summary.setWordWrap(True)
        self.body.addWidget(self.summary)
        self.body.addLayout(self.footer("Split", self.save))

        self.mode.idClicked.connect(lambda *_: self._update())
        self.output.idClicked.connect(lambda *_: self._update())
        self.every.valueChanged.connect(lambda *_: self._update())
        self.ranges.textChanged.connect(self._typed)
        self.grid.itemSelectionChanged.connect(self._clicked)
        self._update()
        self._load_thumbs(0)
        QTimer.singleShot(0, self.ranges.setFocus)

    # ---- layout
    def _build_pick(self):
        self.pick_box = QWidget()
        v = QVBoxLayout(self.pick_box)
        v.setContentsMargins(0, 0, 0, 0)
        h = QHBoxLayout()
        h.addWidget(QLabel("Pages"))
        self.ranges = QLineEdit()
        self.ranges.setPlaceholderText("e.g. 1-3, 7, 10-12   ·   8- means page 8 to the end")
        self.ranges.setClearButtonEnabled(True)
        h.addWidget(self.ranges, 1)
        v.addLayout(h)
        r, self.output = toggle_row(["One PDF per range", "All in one PDF"])
        v.addLayout(r)
        self.body.addWidget(self.pick_box)

    def _build_every(self):
        self.every_box = QWidget()
        h = QHBoxLayout(self.every_box)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel("A new PDF every"))
        self.every = QSpinBox()
        self.every.setRange(1, max(1, self.count))
        self.every.setValue(min(2, self.count))
        self.every.setFixedWidth(80)
        h.addWidget(self.every)
        h.addWidget(QLabel("pages"))
        h.addStretch(1)
        self.body.addWidget(self.every_box)

    def _build_grid(self):
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setIconSize(THUMB)
        self.grid.setGridSize(QSize(THUMB.width() + 16, THUMB.height() + 30))
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setSelectionMode(QAbstractItemView.MultiSelection)   # click toggles a page
        self.grid.setUniformItemSizes(True)
        self.grid.setStyleSheet(GRID_QSS)
        for i in range(1, self.count + 1):
            item = QListWidgetItem(placeholder(i), str(i))
            item.setTextAlignment(Qt.AlignHCenter)
            self.grid.addItem(item)
        self.hint = QLabel("Click pages to pick them, or type the pages above.")
        self.hint.setObjectName("dim")
        self.body.addWidget(self.hint)
        self.body.addWidget(self.grid, 1)

    # ---- thumbnails
    def _load_thumbs(self, start: int):
        if start >= self.count:
            return
        job = _ThumbJob(self.path, start, BATCH)

        def done(first, images):
            release(job)
            if not self._alive:
                return
            for k, img in enumerate(images):
                item = self.grid.item(first + k)
                if item is not None:
                    item.setIcon(QIcon(QPixmap.fromImage(img)))
            self._load_thumbs(first + BATCH)

        job.sig.done.connect(done)
        QThreadPool.globalInstance().start(keep(job))

    # ---- selection <-> text
    def _parsed(self) -> list[pages.Range]:
        return pages.parse_ranges(self.ranges.text(), self.count)

    def selected_pages(self) -> list[int]:
        try:
            return pages.pages_in(self._parsed())
        except UserError:
            return []

    def _typed(self, _text: str):
        if self._syncing:
            return
        wanted = set(self.selected_pages())
        self._syncing = True
        for i in range(self.count):
            self.grid.item(i).setSelected(i + 1 in wanted)
        self._syncing = False
        self._update()

    def _clicked(self):
        if self._syncing:
            return
        chosen = [self.grid.row(it) + 1 for it in self.grid.selectedItems()]
        self._syncing = True
        self.ranges.setText(pages.compact(pages.runs(chosen)))
        self._syncing = False
        self._update()

    # ---- summary
    def _plan(self) -> tuple[bool, str]:
        m = self.mode.checkedId()
        if m == 2:
            return True, f"{self.count} PDFs, one per page"
        if m == 1:
            n = self.every.value()
            parts = pages.chunks(self.count, n)
            if len(parts) == 1:
                return False, f"That's all {self.count} pages in one PDF. Pick a smaller number."
            return True, f"{len(parts)} PDFs of up to {n} page{'s' if n != 1 else ''}"
        if not self.ranges.text().strip():
            return False, "Pick some pages"
        try:
            ranges = self._parsed()
        except UserError as e:
            return False, str(e)
        total = len(pages.pages_in(ranges))
        if self.output.checkedId() == 1:
            return True, f"1 PDF with {total} page{'s' if total != 1 else ''}  ·  {pages.compact(ranges)}"
        n = len(ranges)
        return True, f"{n} PDF{'s' if n != 1 else ''}  ·  " + "  ·  ".join(
            f"page {a}" if a == b else f"pages {a}-{b}" for a, b in ranges[:6]) + (" …" if n > 6 else "")

    def _update(self):
        m = self.mode.checkedId()
        self.pick_box.setVisible(m == 0)
        self.every_box.setVisible(m == 1)
        self.grid.setEnabled(m == 0)
        self.hint.setVisible(m == 0)
        ok, text = self._plan()
        self.summary.setText(text)
        if self.primary_btn:
            combine = m == 0 and self.output.checkedId() == 1
            self.primary_btn.setText("Extract" if combine else "Split")
            self.primary_btn.setEnabled(ok)

    # ---- save
    def save(self):
        src, m = self.path, self.mode.checkedId()
        if m == 2:
            ranges, combine = pages.chunks(self.count, 1), False
        elif m == 1:
            ranges, combine = pages.chunks(self.count, self.every.value()), False
        else:
            try:
                ranges = self._parsed()
            except UserError as e:
                self.summary.setText(str(e))
                return
            combine = self.output.checkedId() == 1
        working = "Extracting pages…" if combine else "Splitting…"
        self.run(lambda: pages.split_ranges(src, ranges, combine), working)
