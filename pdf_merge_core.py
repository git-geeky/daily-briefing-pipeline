"""Merge finished briefing PDFs into one document with an index page and bookmarks.

Used for the combined briefing: the animal edition's PDF goes first, the daily briefing follows, and a one-page index in
front says what is inside and on which page each part starts. Each part keeps its own layout, headers and footers (page
numbers inside a part restart at 1; the index and the PDF bookmarks give the position in the whole file).

Pure file work: no model, no network. A part that cannot be opened is an error for the caller to handle, never silently
dropped here.
"""
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape


@dataclass
class Part:
    label: str
    path: Path
    titles: list[str] = field(default_factory=list)   # article titles listed on the index page
    note: str = ""                                     # one line under the label (e.g. "6 of 10 target stories")


_PUNCTUATION = {"\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2010": "-", "\u2011": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"',
                "\u201d": '"', "\u2026": "...", "\u00a0": " ", "\u2022": "-", "\u00d7": "x", "\u2192": "->", "\u2020": "", "\u2248": "~",
                "\u2264": "<=", "\u2265": ">="}


def _latin(text: str) -> str:
    """The index uses the built-in Helvetica (Latin only): fold dashes, quotes and accents, drop markdown emphasis marks, and
    replace anything else with '?'."""
    text = "".join(_PUNCTUATION.get(ch, ch) for ch in (text or "")).replace("*", "").replace("`", "")
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return folded.encode("latin-1", "replace").decode("latin-1")


def page_count(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def _build_index(path: Path, parts: list[Part], counts: list[int], index_pages: int, *, heading: str, date_human: str, footnote: str) -> int:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=20, leading=24, spaceAfter=2)
    sub = ParagraphStyle("s", fontName="Helvetica", fontSize=11, leading=14, textColor=colors.HexColor("#555555"), spaceAfter=10)
    label = ParagraphStyle("l", fontName="Helvetica-Bold", fontSize=13, leading=16, spaceBefore=10)
    meta = ParagraphStyle("m", fontName="Helvetica", fontSize=9.5, leading=12, textColor=colors.HexColor("#444444"), spaceAfter=3)
    item = ParagraphStyle("i", fontName="Helvetica", fontSize=9, leading=11.5, leftIndent=14, firstLineIndent=-14)
    foot = ParagraphStyle("f", fontName="Helvetica-Oblique", fontSize=8.5, leading=11, textColor=colors.HexColor("#666666"), spaceBefore=14)

    story: list[Any] = [Paragraph(escape(_latin(heading)), title), Paragraph(escape(_latin(date_human)), sub)]
    first = index_pages + 1
    for part, pages in zip(parts, counts):
        story.append(Paragraph(escape(_latin(part.label)), label))
        last = first + pages - 1
        story.append(Paragraph(f"Pages {first}-{last} of this file ({pages} pages)", meta))
        if part.note:
            story.append(Paragraph(escape(_latin(part.note)), meta))
        for number, text in enumerate(part.titles, start=1):
            story.append(Paragraph(f"{number}.&nbsp;&nbsp;{escape(_latin(text))}", item))
        story.append(Spacer(1, 4))
        first = last + 1
    if footnote:
        story.append(Paragraph(escape(_latin(footnote)), foot))
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=22 * mm, bottomMargin=20 * mm,
                            title=_latin(heading))
    doc.build(story)
    return page_count(path)


def combine_pdfs(parts: list[Part], out_path: Path, *, heading: str, date_human: str,
                 footnote: str = "Page numbers printed inside each part restart at 1. Use the PDF bookmarks to jump between parts.") -> dict[str, Any]:
    """Write ``out_path``: index page(s), then each part in order, with a bookmark per part. Returns a summary."""
    from pypdf import PdfReader, PdfWriter

    if not parts:
        raise ValueError("nothing to combine")
    counts: list[int] = []
    for part in parts:
        try:
            pages = len(PdfReader(str(part.path)).pages)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{part.label}: cannot read {part.path}: {exc!r}") from exc
        if pages < 1:
            raise ValueError(f"{part.label}: {part.path} has no pages")
        counts.append(pages)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    index_path = out_path.with_name(out_path.stem + ".index.pdf")
    tmp_out = out_path.with_name(out_path.name + ".tmp")
    try:
        index_pages = 1
        for _ in range(3):  # the page count of the index must be known before the page ranges in it are printed
            actual = _build_index(index_path, parts, counts, index_pages, heading=heading, date_human=date_human, footnote=footnote)
            if actual == index_pages:
                break
            index_pages = actual
        writer = PdfWriter()
        writer.append(str(index_path), outline_item="Index")
        starts: list[int] = []
        for part, pages in zip(parts, counts):
            starts.append(len(writer.pages) + 1)
            writer.append(str(part.path), outline_item=part.label, import_outline=True)
        writer.add_metadata({"/Title": heading, "/Subject": date_human})
        with tmp_out.open("wb") as handle:
            writer.write(handle)
        os.replace(tmp_out, out_path)
    finally:
        for leftover in (index_path, tmp_out):
            try:
                leftover.unlink()
            except OSError:
                pass
    total = page_count(out_path)
    return {"path": str(out_path), "pages": total, "index_pages": index_pages,
            "parts": [{"label": p.label, "first_page": start, "pages": pages} for p, start, pages in zip(parts, starts, counts)]}


__all__ = ["Part", "combine_pdfs", "page_count"]
