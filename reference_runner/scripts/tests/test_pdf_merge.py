"""Combined-briefing PDF: index page, part order, bookmarks, page ranges, failure on unreadable parts."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _shared_imports import REPO_ROOT  # noqa: F401

import pdf_merge_core as merge  # noqa: E402


def make_pdf(path: Path, pages: int, label: str) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path), pagesize=A4)
    for i in range(pages):
        pdf.drawString(60, 780, f"{label} body page {i + 1}")
        pdf.showPage()
    pdf.save()
    return path


class CombineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.animal = make_pdf(self.dir / "animal.pdf", 4, "ANIMAL")
        self.daily = make_pdf(self.dir / "daily.pdf", 6, "DAILY")

    def combine(self, **kwargs):
        parts = [merge.Part("The Animal Science Briefing", self.animal, titles=["Numbats rebound in Dryandra", "Québec bullhead melanoma 日本"], note="2 of 10 target stories"),
                 merge.Part("The Daily Intelligence Briefing", self.daily, titles=["Story A", "Story B"])]
        return merge.combine_pdfs(parts, self.dir / "combined.pdf", heading="Today's briefings", date_human="Sunday, October 25, 2026", **kwargs)

    def test_parts_follow_the_index_in_order_with_correct_page_ranges(self) -> None:
        from pypdf import PdfReader

        summary = self.combine()
        reader = PdfReader(str(self.dir / "combined.pdf"))
        self.assertEqual(summary["pages"], 1 + 4 + 6)
        self.assertEqual([p["first_page"] for p in summary["parts"]], [2, 6])
        self.assertIn("ANIMAL body page 1", reader.pages[1].extract_text())
        self.assertIn("DAILY body page 1", reader.pages[5].extract_text())
        index_text = reader.pages[0].extract_text()
        self.assertIn("Pages 2-5 of this file", index_text)
        self.assertIn("Pages 6-11 of this file", index_text)
        self.assertIn("Numbats rebound in Dryandra", index_text)
        self.assertIn("2 of 10 target stories", index_text)

    def test_bookmarks_name_each_part_and_non_latin_titles_do_not_break_the_index(self) -> None:
        from pypdf import PdfReader

        self.combine()
        outline = [item.title for item in PdfReader(str(self.dir / "combined.pdf")).outline if not isinstance(item, list)]
        self.assertEqual(outline, ["Index", "The Animal Science Briefing", "The Daily Intelligence Briefing"])
        self.assertIn("Quebec bullhead melanoma ?", PdfReader(str(self.dir / "combined.pdf")).pages[0].extract_text())

    def test_metadata_title_and_no_leftover_temp_files(self) -> None:
        from pypdf import PdfReader

        self.combine()
        self.assertEqual(PdfReader(str(self.dir / "combined.pdf")).metadata.title, "Today's briefings")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["animal.pdf", "combined.pdf", "daily.pdf"])

    def test_a_long_index_that_needs_two_pages_prints_correct_ranges(self) -> None:
        parts = [merge.Part("Big part", self.animal, titles=[f"Article title number {i} with some extra words to fill the line" for i in range(120)]),
                 merge.Part("Second", self.daily)]
        summary = merge.combine_pdfs(parts, self.dir / "long.pdf", heading="H", date_human="D")
        self.assertGreaterEqual(summary["index_pages"], 2)
        self.assertEqual(summary["parts"][0]["first_page"], summary["index_pages"] + 1)
        from pypdf import PdfReader

        reader = PdfReader(str(self.dir / "long.pdf"))
        first = summary["parts"][0]["first_page"]
        self.assertIn("ANIMAL body page 1", reader.pages[first - 1].extract_text())

    def test_an_index_that_only_just_spills_over_is_shrunk_to_one_page(self) -> None:
        """Found in the fifth live run: two titles and the footnote sat alone on a second index page."""
        def parts(count: int) -> list:
            return [merge.Part("Animal", self.animal, titles=[f"Article title number {i} with some extra words" for i in range(count)]),
                    merge.Part("Daily", self.daily, titles=[f"Daily title {i}" for i in range(8)])]

        def full_size_pages(count: int) -> int:
            return merge._build_index(self.dir / "probe.pdf", parts(count), [4, 6], 1, heading="H", date_human="D", footnote="Footnote.")

        count = next(n for n in range(20, 120) if full_size_pages(n) == 2)  # the first length that overflows at full size
        summary = merge.combine_pdfs(parts(count), self.dir / "fit.pdf", heading="H", date_human="D", footnote="Footnote.")
        self.assertEqual(summary["index_pages"], 1)
        self.assertEqual(summary["parts"][0]["first_page"], 2)

    def test_an_unreadable_part_is_an_error_not_a_silent_omission(self) -> None:
        (self.dir / "broken.pdf").write_bytes(b"not a pdf at all")
        with self.assertRaises(ValueError) as caught:
            merge.combine_pdfs([merge.Part("Broken", self.dir / "broken.pdf")], self.dir / "x.pdf", heading="H", date_human="D")
        self.assertIn("Broken", str(caught.exception))
        self.assertFalse((self.dir / "x.pdf").exists())

    def test_dashes_quotes_and_markdown_marks_are_folded_not_turned_into_question_marks(self) -> None:
        self.assertEqual(merge._latin("Season 2026\u201327: \u201cfirst\u201d *Salmo salar* \u2013 1980\u20132009\u2026"), 'Season 2026-27: "first" Salmo salar - 1980-2009...')

    def test_nothing_to_combine_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            merge.combine_pdfs([], self.dir / "x.pdf", heading="H", date_human="D")


class InlineMarkupTests(unittest.TestCase):
    """Live-run finding: a mis-nested bold/italic in one source-list bullet cost the whole briefing its PDF."""

    @classmethod
    def setUpClass(cls) -> None:
        import sys

        scripts = str(Path(__file__).resolve().parents[1])
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import build_pdf

        cls.build_pdf = build_pdf

    def test_bold_wrapping_an_italic_that_ends_the_run_is_nested_properly(self) -> None:
        self.assertEqual(self.build_pdf.md_inline("**Teeling et al. (2005), *Science***").replace("<i></i>", ""),
                         "<b>Teeling et al. (2005), <i>Science</i></b>")

    def test_stray_and_unclosed_tags_are_repaired(self) -> None:
        self.assertEqual(self.build_pdf.balance_inline_tags("</b>stray <i>open"), "stray <i>open</i>")

    def test_a_paragraph_with_broken_markup_falls_back_to_plain_text_instead_of_raising(self) -> None:
        from reportlab.lib.styles import getSampleStyleSheet

        paragraph = self.build_pdf.Paragraph("<b>x<i>y</b></i>", getSampleStyleSheet()["Normal"])
        self.assertIn("xy", paragraph.getPlainText())


if __name__ == "__main__":
    unittest.main()
