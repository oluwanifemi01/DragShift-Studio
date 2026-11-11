"""Choosing pages from a PDF: parse "1-3, 7, 10-" style ranges and save the chosen pages.

Page numbers are 1-based and ranges inclusive, as people type them. No UI here."""
from __future__ import annotations

import re
from pathlib import Path

from .convert import fitz, open_pdf
from .util import UserError, sibling

Range = tuple[int, int]          # (first, last), 1-based, inclusive
MAX_NAME_LABEL = 24              # longer selections get a generic file name

_SPLIT = re.compile(r"[,;]+")
_DASH = re.compile(r"\s*[-–—]\s*")
_TOKEN = re.compile(r"^(\d*)\s*[-–—]\s*(\d*)$|^(\d+)$")


def parse_ranges(text: str, page_count: int) -> list[Range]:
    """'1-3, 7, 10-' → [(1, 3), (7, 7), (10, last)]. Keeps the typed order; reversed
    ranges are flipped. Raises UserError with a message that says what to fix."""
    tokens = [t.strip() for t in _SPLIT.split(text or "") if t.strip()]
    if not tokens:
        raise UserError("Type the pages you want, like 1-3, 7, 10-12.")
    out: list[Range] = []
    for tok in tokens:
        m = _TOKEN.match(tok)
        if not m or tok in ("-", "–", "—"):
            raise UserError(f"Couldn't read “{tok}”. Use page numbers and ranges, like 1-3, 7.")
        if m[3]:
            a = b = int(m[3])
        else:
            a = int(m[1]) if m[1] else 1
            b = int(m[2]) if m[2] else page_count
        a, b = min(a, b), max(a, b)
        if a < 1:
            raise UserError("Pages start at 1.")
        if b > page_count:
            raise UserError(f"This PDF has {page_count} pages, so page {b} doesn't exist.")
        out.append((a, b))
    return out


def runs(page_numbers: list[int]) -> list[Range]:
    """Group pages into contiguous ranges: [7, 1, 2, 3] → [(1, 3), (7, 7)]."""
    out: list[Range] = []
    for p in sorted(set(page_numbers)):
        if out and p == out[-1][1] + 1:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


def compact(ranges: list[Range], sep: str = ", ") -> str:
    return sep.join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def pages_in(ranges: list[Range]) -> list[int]:
    return [p for a, b in ranges for p in range(a, b + 1)]


def chunks(page_count: int, size: int) -> list[Range]:
    """Every `size` pages: chunks(10, 4) → [(1, 4), (5, 8), (9, 10)]."""
    size = max(1, size)
    return [(a, min(a + size - 1, page_count)) for a in range(1, page_count + 1, size)]


def page_count(src: str | Path) -> int:
    with open_pdf(src) as doc:
        return doc.page_count


def _label(r: Range) -> str:
    a, b = r
    return f"page {a}" if a == b else f"pages {a}-{b}"


def _write(doc, src: Path, ranges: list[Range], label: str) -> Path:
    nd = fitz.open()
    try:
        for a, b in ranges:
            nd.insert_pdf(doc, from_page=a - 1, to_page=b - 1)
        out = sibling(src, label, ".pdf")
        nd.save(out, garbage=3, deflate=True)
    finally:
        nd.close()
    return out


def split_ranges(src: str | Path, ranges: list[Range], combine: bool = False) -> list[Path]:
    """One PDF per range, or (combine) all the ranges in one PDF in the order given."""
    src = Path(src)
    if not ranges:
        raise UserError("Choose at least one page.")
    with open_pdf(src) as doc:
        for a, b in ranges:      # callers may skip parse_ranges; never trust the numbers
            if not 1 <= a <= b <= doc.page_count:
                raise UserError(f"This PDF has {doc.page_count} pages; {compact([(a, b)])} isn't in it.")
        if combine:
            text = compact(ranges, sep=",")
            label = f"pages {text}" if len(text) <= MAX_NAME_LABEL else "Extracted pages"
            if len(ranges) == 1:
                label = _label(ranges[0])
            return [_write(doc, src, ranges, label)]
        return [_write(doc, src, [r], _label(r)) for r in ranges]


def split_every(src: str | Path, size: int) -> list[Path]:
    return split_ranges(src, chunks(page_count(src), size))
