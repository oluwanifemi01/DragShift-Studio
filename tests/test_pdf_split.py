"""Splitting chosen pages out of a PDF: range parsing, output files, notifications and the window."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from dragshift import pages, report
from dragshift.convert import fitz
from dragshift.util import UserError


def make_pdf(path, n=10):
    doc = fitz.open()
    for i in range(n):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}", fontsize=24)
    doc.save(path)
    doc.close()
    return path


def texts(path):
    with fitz.open(path) as d:
        return [p.get_text().strip() for p in d]


@pytest.fixture
def pdf(tmp_path):
    return make_pdf(tmp_path / "Report.pdf")


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("text, expected", [
    ("1-3, 7, 10", [(1, 3), (7, 7), (10, 10)]),
    ("  2 – 4 ;5", [(2, 4), (5, 5)]),         # en dash, semicolon, odd spacing
    ("8-", [(8, 10)]),                         # open end = to the last page
    ("-2", [(1, 2)]),                          # open start = from page 1
    ("6-4", [(4, 6)]),                         # reversed is fine
    ("9, 1-2", [(9, 9), (1, 2)]),              # keeps the order typed
])
def test_parse_ranges(text, expected):
    assert pages.parse_ranges(text, 10) == expected


@pytest.mark.parametrize("text, msg", [
    ("", "Type the pages"),
    ("1-3, abc", "Couldn't read"),
    ("0", "start at 1"),
    ("4, 12", "has 10 pages"),
])
def test_parse_ranges_errors(text, msg):
    with pytest.raises(UserError, match=msg):
        pages.parse_ranges(text, 10)


def test_compact_and_runs():
    assert pages.runs([7, 1, 2, 3, 10, 2]) == [(1, 3), (7, 7), (10, 10)]
    assert pages.compact([(1, 3), (7, 7), (10, 10)]) == "1-3, 7, 10"
    assert pages.pages_in([(1, 3), (7, 7)]) == [1, 2, 3, 7]


def test_chunks():
    assert pages.chunks(10, 4) == [(1, 4), (5, 8), (9, 10)]
    assert pages.chunks(3, 5) == [(1, 3)]


# ---------------------------------------------------------------- splitting
def test_one_pdf_per_range(pdf):
    outs = pages.split_ranges(pdf, [(1, 3), (7, 7)])
    assert [o.name for o in outs] == ["Report pages 1-3.pdf", "Report page 7.pdf"]
    assert texts(outs[0]) == ["Page 1", "Page 2", "Page 3"]
    assert texts(outs[1]) == ["Page 7"]


def test_all_in_one_pdf_keeps_typed_order(pdf):
    out, = pages.split_ranges(pdf, [(9, 10), (1, 1)], combine=True)
    assert out.name == "Report pages 9-10,1.pdf"
    assert texts(out) == ["Page 9", "Page 10", "Page 1"]


def test_long_selection_gets_a_short_name(pdf):
    many = [(i, i) for i in range(1, 11, 2)] * 3
    out, = pages.split_ranges(pdf, many, combine=True)
    assert out.name == "Report Extracted pages.pdf"


def test_split_every(pdf):
    outs = pages.split_every(pdf, 4)
    assert [o.name for o in outs] == ["Report pages 1-4.pdf", "Report pages 5-8.pdf", "Report pages 9-10.pdf"]
    assert [len(texts(o)) for o in outs] == [4, 4, 2]


def test_original_is_untouched(pdf):
    before = pdf.read_bytes()
    pages.split_ranges(pdf, [(2, 3)])
    assert pdf.read_bytes() == before


def test_password_protected_pdf_is_refused(tmp_path):
    p = make_pdf(tmp_path / "Locked.pdf", 2)
    with fitz.open(p) as d:
        d.save(tmp_path / "L2.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    with pytest.raises(UserError, match="password"):
        pages.split_ranges(tmp_path / "L2.pdf", [(1, 1)])


def test_report_titles(pdf):
    outs = pages.split_ranges(pdf, [(1, 3), (7, 7)])
    assert report.summarize("tool:Split", [pdf], outs).title == "Split into 2 PDFs"
    one = pages.split_ranges(pdf, [(2, 4), (8, 8)], combine=True)
    r = report.summarize("tool:Split", [pdf], one)
    assert r.title == "Extracted 4 pages" and one[0].name in r.detail


# ---------------------------------------------------------------- the window
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class Notes:
    def __init__(self):
        self.calls = []

    def __call__(self, action, srcs, outs, extra=""):
        self.calls.append((action, outs, extra))


def test_window_typing_selects_pages(qapp, pdf):
    from dragshift.tools_pdf import SplitWindow
    w = SplitWindow(pdf, Notes())
    w.ranges.setText("2-4, 9")
    assert w.selected_pages() == [2, 3, 4, 9]
    assert w.summary.text().startswith("2 PDFs")
    w.output.button(1).click()                  # all in one PDF
    assert w.summary.text().startswith("1 PDF with 4 pages")


def test_window_clicking_pages_writes_ranges(qapp, pdf):
    from dragshift.tools_pdf import SplitWindow
    w = SplitWindow(pdf, Notes())
    w.ranges.setText("")
    for page in (5, 6, 7, 1):
        w.grid.item(page - 1).setSelected(True)
    assert w.ranges.text() == "1, 5-7"
    assert w.primary_btn.isEnabled()


def test_window_bad_text_disables_split(qapp, pdf):
    from dragshift.tools_pdf import SplitWindow
    w = SplitWindow(pdf, Notes())
    w.ranges.setText("3-99")
    assert not w.primary_btn.isEnabled() and "has 10 pages" in w.summary.text()


def test_window_modes_and_save(qapp, pdf):
    from dragshift.tools_pdf import SplitWindow
    notes = Notes()
    w = SplitWindow(pdf, notes)
    w.mode.button(1).click()                    # every N pages
    w.every.setValue(3)
    assert w.summary.text().startswith("4 PDFs")
    w.save()
    (action, outs, _), = notes.calls
    assert action == "tool:Split" and len(outs) == 4


def test_window_every_page_mode(qapp, pdf):
    from dragshift.tools_pdf import SplitWindow
    notes = Notes()
    w = SplitWindow(pdf, notes)
    w.mode.button(2).click()
    assert w.summary.text().startswith("10 PDFs")
    w.save()
    assert len(notes.calls[0][1]) == 10
