"""Turns 'what just happened' into a short, useful summary for the notification:
sizes saved, new dimensions, pages, fields removed…"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .util import category, file_size, human_size


@dataclass
class Result:
    title: str
    detail: str
    outs: list[Path]
    srcs: list[Path] = field(default_factory=list)
    good: str = ""     # highlighted figure, e.g. "−75%"

    @property
    def thumb(self) -> Path | None:
        for o in self.outs:
            if category(o) == "image":
                return o
        return None


def dims(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def pages(path: Path) -> int | None:
    try:
        from .convert import fitz
        with fitz.open(path) as d:
            return d.page_count
    except Exception:
        return None


def size_change(before: int, after: int) -> tuple[str, str]:
    """('2.4 MB → 610 KB', '75% smaller') — second part empty if no meaningful change."""
    text = f"{human_size(before)} → {human_size(after)}"
    if before <= 0:
        return text, ""
    pct = (1 - after / before) * 100
    if pct >= 1:
        return text, f"{pct:.0f}% smaller"
    if pct <= -1:
        return text, f"{-pct:.0f}% larger"
    return text, "same size"


def _dim_str(d) -> str:
    return f"{d[0]}×{d[1]}" if d else ""


def summarize(action: str, srcs: list, outs: list, extra: str = "") -> Result:
    srcs = [Path(s) for s in srcs]
    outs = [Path(o) for o in outs if o]
    if not outs:
        return Result(action, extra or "Nothing was written", [], srcs)
    src = srcs[0] if srcs else outs[0]
    before = sum(file_size(s) for s in srcs if s.is_file())
    after = sum(file_size(o) for o in outs if o.is_file())
    kind, _, name = action.partition(":")
    o0 = outs[0]
    good = ""
    if category(src) == "video":
        return summarize_video(kind, name, srcs, outs, before, after, extra)

    if kind == "convert":
        fmt = name
        if len(srcs) > 1 and len(outs) == 1:
            title = f"{len(srcs)} files → 1 {fmt}"
        elif len(outs) > 1 and len(srcs) == 1:
            n = pages(src)
            title = f"{n or len(outs)} pages → {len(outs)} {fmt}s"
        elif len(outs) > 1:
            title = f"{len(outs)} files → {fmt}"
        else:
            title = f"{src.suffix.lstrip('.').upper()} → {fmt}"
        bits = [o0.name if len(outs) == 1 else f"{o0.parent.name}/{o0.name} …"]
        d = dims(o0) if category(o0) == "image" else None
        if d:
            bits.append(_dim_str(d))
        if o0.suffix.lower() == ".pdf":
            n = pages(o0)
            if n:
                bits.append(f"{n} page{'s' if n != 1 else ''}")
        bits.append(human_size(after))
        return Result(title, " · ".join(bits), outs, srcs)

    if name == "Compress":
        sizes, verdict = size_change(before, after)
        title = f"Compressed {len(srcs)} files" if len(srcs) > 1 else "Compressed"
        if verdict.endswith("larger") or verdict == "same size":
            verdict = "already well compressed"
        return Result(title, sizes + (f" · {extra}" if extra else ""), outs, srcs, good=verdict)

    if name in ("Crop", "Resize", "Edit"):
        d0, d1 = dims(src), dims(o0)
        verb = {"Crop": "Cropped", "Resize": "Resized", "Edit": "Edited"}[name]
        if len(outs) > 1:
            return Result(f"{verb} {len(outs)} images", f"{human_size(before)} → {human_size(after)}", outs, srcs)
        detail = f"{_dim_str(d0)} → {_dim_str(d1)}" if d0 and d1 and d0 != d1 else _dim_str(d1)
        if extra:
            detail += f" · {extra}"
        return Result(verb, f"{detail} · {o0.name}", outs, srcs)

    if name == "Add BG":
        return Result("Background added", f"{_dim_str(dims(o0))} PNG · {human_size(after)} · {o0.name}", outs, srcs)

    if name == "Annotate":
        return Result("Annotated", f"{extra} · {o0.name}" if extra else o0.name, outs, srcs)

    if name == "Redact":
        return Result("Redacted", f"{extra} · {o0.name}" if extra else o0.name, outs, srcs,
                      good="burned into pixels")

    if name == "Metadata":
        return Result("Metadata removed", f"{extra} · {o0.name}" if extra else o0.name, outs, srcs,
                      good="private copy")

    if name == "Split":
        return Result(f"Split into {len(outs)} PDFs", f"{o0.name} …", outs, srcs)

    if name == "Merge":
        n = pages(o0)
        return Result(f"Merged {len(srcs)} PDFs", f"{n} pages · {human_size(after)} · {o0.name}", outs, srcs)

    if name == "Rotate":
        n = pages(o0)
        return Result("Rotated 90°", f"{n} page{'s' if n != 1 else ''} · {o0.name}", outs, srcs)

    if name == "Protect":
        return Result("Password protected", f"AES-256 · {o0.name}", outs, srcs, good="encrypted")

    if name == "Extract":
        return Result(f"Extracted {len(outs)} image{'s' if len(outs) != 1 else ''}",
                      f"{o0.parent.name}/ · {human_size(after)}", outs, srcs)

    # generic
    return Result(name or action, f"{o0.name} · {human_size(after)}", outs, srcs)


# ---------------------------------------------------------------- video
def summarize_video(kind: str, name: str, srcs: list[Path], outs: list[Path], before: int, after: int,
                    extra: str = "") -> Result:
    from .video import fmt_time, try_probe
    src, o0 = srcs[0], outs[0]
    i0, i1 = try_probe(src), try_probe(o0) if category(o0) != "image" else None
    out_dims = (i1.size_text if i1 else "") or _dim_str(dims(o0))
    many = len(outs) > 1

    def bits(*parts: str) -> str:
        return " · ".join(p for p in parts if p)

    length = fmt_time(i1.duration) if i1 and i1.duration else ""
    name_bit = f"{len(outs)} files" if many else o0.name
    if kind == "convert":
        title = f"{len(srcs)} videos → {name}" if many else f"{src.suffix.lstrip('.').upper()} → {name}"
        return Result(title, bits(name_bit, out_dims, length, human_size(after)), outs, srcs)
    if name == "Compress":
        sizes, verdict = size_change(before, after)
        if verdict.endswith("larger") or verdict == "same size":
            verdict = "already well compressed"
        res = f"{i0.height}p → {i1.height}p" if i0 and i1 and i0.height != i1.height and not many else ""
        title = f"Compressed {len(srcs)} videos" if many else "Compressed"
        return Result(title, bits(sizes, res, extra), outs, srcs, good=verdict)
    if name == "Trim":
        span = f"{fmt_time(i0.duration)} → {length}" if i0 and length else length
        return Result("Trimmed", bits(span, human_size(after), o0.name), outs, srcs)
    if name == "Resize":
        change = f"{i0.size_text} → {out_dims}" if i0 and not many else out_dims
        return Result(f"Resized {len(srcs)} videos" if many else "Resized",
                      bits(change, human_size(after), name_bit), outs, srcs)
    if name == "GIF":
        return Result("GIF created", bits(out_dims, extra, human_size(after), o0.name), outs, srcs)
    if name == "Mute":
        return Result("Sound removed", bits(human_size(after), name_bit), outs, srcs)
    if name == "Frame":
        return Result("Frame saved", bits(f"{out_dims} PNG", extra, o0.name), outs, srcs)
    return Result(name or kind, bits(name_bit, human_size(after)), outs, srcs)
