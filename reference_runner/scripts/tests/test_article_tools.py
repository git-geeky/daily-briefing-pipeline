"""Normalisation, fallback header, cross-reference repair, email bodies, and the animal validators (subprocess)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _shared_imports import REPO_ROOT  # noqa: F401

import article_tools_core as tools  # noqa: E402
import animal_fixtures as fx  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
STORY = {"story_id": "saiga-genome", "title": "Saiga genome reveals a bottleneck after the 2015 die-off", "prior_story_id": None}


class SchemelessLinkTests(unittest.TestCase):
    """Review T18: "no URLs in the body" must also catch links written without a scheme."""

    def test_links_without_a_scheme_are_removed_from_the_body_but_not_from_the_source_blocks(self) -> None:
        text = ("## T\n\nSee www.evil.example/login and evil.example/phish, also [click here](www.evil.example/x). Reported by nature.com on "
                "3.5 million voles, e.g. in the north.\n\n### Primary Sources\n- Journal. www.journal.example/paper\n")
        cleaned, removed = tools._strip_body_links(text)
        self.assertEqual(removed, 3)
        body = cleaned.split("### Primary Sources")[0]
        self.assertNotIn("evil.example", body)
        self.assertIn("click here", body)
        self.assertIn("nature.com", body)
        self.assertIn("www.journal.example/paper", cleaned)

    def test_the_validator_sees_a_schemeless_body_link(self) -> None:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import briefing_manifest as manifest

        def has_links(body: str) -> bool:
            item = manifest.build_item_manifest("animals", "2026-10-26", "T", body, prior_items=[], image_metadata={}, source_audit=None)
            return bool(item["has_body_links"])

        self.assertTrue(has_links("Log in at www.evil.example/login now to read the rest of this story.\n"))
        self.assertTrue(has_links("Visit evil.example/phish for the data.\n"))
        self.assertFalse(has_links("The report on nature.com said 3.5 million voles, e.g. in the north.\n"))


class PriorMemoryTests(unittest.TestCase):
    """Review F10: a day that was built but never sent must not make its stories count as already covered."""

    def test_an_undelivered_day_is_skipped_and_the_next_existing_days_are_used(self) -> None:
        from unittest import mock

        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import briefing_manifest as manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day, title in (("2026-10-25", "Wolves return to the island"), ("2026-10-24", "Otters in the estuary"), ("2026-10-23", "Voles in the meadow")):
                (root / f"{day}.md").write_text(f"## {title}\n\nBody text.\n", encoding="utf-8")
            (root / "2026-10-25.undelivered").write_text("not delivered", encoding="utf-8")
            patched = {"assembled_markdown_path": lambda d: root / f"{d}.md", "assembled_manifest_path": lambda d: root / f"{d}.json",
                       "pdf_path": lambda d: root / f"{d}.pdf"}
            with mock.patch.dict(manifest.load_prior_items.__globals__, patched):
                titles = [item["title"] for item in manifest.load_prior_items("2026-10-26")]
        self.assertEqual(titles, ["Otters in the estuary", "Voles in the meadow"])


class ConfidenceRatingTests(unittest.TestCase):
    """The Confidence line decides whether an article is written at all: Low (including a range that reaches Low) is not."""

    def rating(self, text: str):
        return tools.confidence_rating(f"## T\n**Confidence:** {text}\n")

    def test_low_in_any_part_of_the_stated_range_is_low(self) -> None:
        for text in ("Low", "Low to medium because the paper's statistics were unavailable.", "Confidence is low, as the notice has no case counts.",
                     "Medium to low: single survey", "Low-medium; preprint", "Low, because the report was not read"):
            self.assertEqual(self.rating(text), "low", text)

    def test_a_qualifier_after_the_rating_does_not_change_it(self) -> None:
        for text, expected in (("Medium (low for the dating)", "medium"), ("Medium, though the low-tide counts were estimated", "medium"),
                               ("Medium - peer-reviewed, low sample size", "medium"), ("High: replicated twice", "high"), ("Moderate", "medium")):
            self.assertEqual(self.rating(text), expected, text)

    def test_no_confidence_line_is_not_a_rating(self) -> None:
        self.assertIsNone(tools.confidence_rating("## T\n\nNo such line here.\n"))
        self.assertEqual(tools.confidence_line("## T\n**Confidence:** High: yes\n"), "High: yes")


class IdentificationConfidenceTests(unittest.TestCase):
    """Daily spec: every identification of a withheld person states its confidence level in the text."""

    def find(self, text: str) -> list[str]:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import briefing_manifest as manifest

        return manifest.find_unlabelled_identifications(text)

    def test_a_named_identification_without_a_confidence_label_is_found(self) -> None:
        self.assertEqual(self.find("The filing was made by a John Doe who is likely Jane Roe, a former engineer."), ["likely Jane Roe"])

    def test_a_labelled_identification_passes(self) -> None:
        self.assertEqual(self.find("The filer is likely Jane Roe (identification confidence: Moderate, 2 matching attributes: employer and title; "
                                   "the briefing's own research)."), [])

    def test_offices_and_outlets_are_not_identifications(self) -> None:
        self.assertEqual(self.find("The ruling is likely Supreme Court Justice territory, and the story is likely New York Times coverage next week."), [])


class UnreadSourceMentionTests(unittest.TestCase):
    """A source the writer could not open must not factor into the article; sentences about that access are sent back for removal."""

    ARTICLE = ("## T\n<!-- story: x -->\n\n**Bottom line:** The count rose. Funding and conflict statements were not read.\n\n### The evidence\n"
               "The survey found 429 tigers. The full PDF text was not readable, so species assignments are not made here. Age data were not available "
               "for 12 animals in the cohort.\n\n| Claim | Evidence | Limits |\n|---|---|---|\n| a | the source was not accessed | b |\n\n"
               "### Primary Sources\n- Author. A paper that was unavailable. https://x.org/a\n")

    def test_access_sentences_in_prose_are_found_and_tables_and_source_blocks_are_skipped(self) -> None:
        found = tools.find_unread_source_mentions(self.ARTICLE)
        self.assertEqual(len(found), 3)
        self.assertIn("Funding and conflict statements were not read.", found)
        self.assertFalse(any("source was not accessed" in s or "paper that was unavailable" in s for s in found))

    def test_a_table_cell_that_only_reports_a_failed_lookup_is_found(self) -> None:
        table = "## T\n\n| Taxon | IUCN |\n|---|---|\n| Greylag goose | Registry page not retrieved |\n| Pink-footed goose | Not retrieved |\n| Brent goose | LC (2024) |\n"
        self.assertEqual(tools.find_unread_source_mentions(table), ["Registry page not retrieved", "Not retrieved"])

    def test_http_error_wording_counts(self) -> None:
        self.assertTrue(tools.find_unread_source_mentions("## T\n\nThe registry returned HTTP 403, so the status is taken from the press release.\n"))

    def test_a_clean_article_has_none(self) -> None:
        self.assertEqual(tools.find_unread_source_mentions("## T\n\nThe survey counted 429 tigers across five parks. Counts rose in three.\n"), [])


class NormalizeTests(unittest.TestCase):
    def test_messy_output_is_repaired_without_touching_content(self) -> None:
        raw = fx.make_article(STORY, words=600, messy=True)
        text, fixes = tools.normalize_article(raw, STORY)
        self.assertTrue(text.startswith("## Saiga genome reveals a bottleneck after the 2015 die-off\n<!-- story: saiga-genome -->\n"))
        self.assertNotIn("```", text)
        self.assertNotIn("Here is the article", text)
        for heading in ("### Counterpoint Matrix", "### Primary Sources"):
            self.assertIn(heading, text)
        self.assertNotIn("## References", text)
        self.assertIn("**Bottom line:** ", text)
        body = text.split("### Primary Sources")[0].split("### Watch Next")[0]
        self.assertNotIn("http", body, "body links must be gone")
        self.assertIn("the agency page", body, "link text is kept")
        sources = text.split("### Primary Sources")[1]
        self.assertIn("https://www.nature.com/articles/fixture-2026", sources, "source URLs are untouched")
        self.assertTrue(any("demoted" in f or "canonicalised" in f for f in fixes))
        self.assertTrue(any("link" in f for f in fixes))

    def test_clean_article_is_a_fixed_point(self) -> None:
        raw = fx.make_article(STORY, words=600)
        once, _ = tools.normalize_article(raw, STORY)
        twice, fixes = tools.normalize_article(once, STORY)
        self.assertEqual(once, twice)
        self.assertEqual(fixes, [])

    def test_inner_h2_headings_are_demoted_so_the_article_stays_one_item(self) -> None:
        raw = "## Title here for the story\n\n**Bottom line:** x\n\n## Extra Section\n\ntext\n"
        text, _ = tools.normalize_article(raw, {**STORY, "title": "Title here for the story"})
        self.assertEqual(text.count("\n## "), 0)
        self.assertIn("### Extra Section", text)

    def test_prior_marker_and_doi_urls(self) -> None:
        story = {**STORY, "prior_story_id": "saiga-die-off"}
        raw = "## T title long enough\n\nSee https://doi.org/10.1234/abc.5 for details.\n\n### Primary Sources\n- Paper https://doi.org/10.1234/abc.5\n"
        text, _ = tools.normalize_article(raw, story)
        self.assertIn("<!-- story: saiga-genome; prior: saiga-die-off -->", text)
        self.assertIn("doi:10.1234/abc.5 for details", text)
        self.assertIn("- Paper https://doi.org/10.1234/abc.5", text)

    def test_bottom_line_and_confidence_become_separate_paragraphs(self) -> None:
        raw = "## A title long enough to count\n\n**Bottom line:** the verdict.\n**Confidence:** Medium. Reasons.\n\ntext\n"
        text, _ = tools.normalize_article(raw, {**STORY, "title": "A title long enough to count"})
        self.assertIn("**Bottom line:** the verdict.\n\n**Confidence:** Medium. Reasons.", text)

    def test_no_heading_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            tools.normalize_article("just prose, no heading at all", STORY)


class HeaderTests(unittest.TestCase):
    def summaries(self, n):
        groups = ["mammal", "bird", "mammal", "fish_marine", "bird", "invertebrate", "mammal", "herp", "other", "bird"]
        return [{"title": f"Story number {i} about animals in the field", "bottom_line": "The study reports a clear effect but cannot separate two explanations, which matters for management of the population.",
                 "confidence": "Medium — single study", "group": groups[i % 10], "story_id": f"s{i}", "region": "Kenya",
                 "taxa": "*Loxodonta africana* (African savanna elephant)"} for i in range(n)]

    def test_fallback_header_meets_validator_limits_for_six_and_ten_stories(self) -> None:
        for n in (6, 10):
            header = tools.deterministic_header(label="The Animal Science Briefing", date_human="Monday, October 26, 2026",
                                                prepared_by="Prepared by Claude", summaries=self.summaries(n))
            summary = header.split("## Executive Summary")[1].split("###")[0]
            words = len(summary.split())
            self.assertTrue(250 <= words <= 750, (n, words))
            titles = [s["title"] for s in self.summaries(n)]
            refs = tools.extract_header_titles(header)
            self.assertGreaterEqual(len(refs), 2)
            self.assertTrue(all(r in titles for r in refs))

    def test_cross_reference_repair_snaps_near_misses_and_drops_unknowns(self) -> None:
        titles = ["Saiga genome reveals a bottleneck after the 2015 die-off", "Kakapo breeding season yields record chicks"]
        header = ('- Genetics. Cross-reference: "Saiga genome reveals bottleneck after the 2015 die off"\n'
                  '- Birds. Cross-reference: "Kakapo breeding season yields record chicks"\n'
                  '- Ghost. Cross-reference: "A story that does not exist anywhere"\n')
        fixed, notes = tools.repair_cross_references(header, titles)
        self.assertIn('"Saiga genome reveals a bottleneck after the 2015 die-off"', fixed)
        self.assertNotIn("Ghost", fixed)
        self.assertEqual(len(notes), 2)


class EmailTests(unittest.TestCase):
    def test_html_is_escaped_and_notes_listed(self) -> None:
        text, html_body = tools.compose_email_bodies(
            label="The Animal Science Briefing", date_human="Monday", summaries=[{"title": "A <b>bold</b> claim", "bottom_line": "x & y", "confidence": "High", "group": "bird"}],
            notes=["2 articles had unverified figures"], digest_titles=["Small item"])
        self.assertIn("A <b>bold</b> claim", text)
        self.assertIn("A &lt;b&gt;bold&lt;/b&gt; claim", html_body)
        self.assertIn("x &amp; y", html_body)
        self.assertIn("unverified figures", html_body)


RUNNER = r'''
import json, sys
sys.path.insert(0, {scripts!r})
import animal_fixtures as fx
import briefing_manifest as m
import re
date = "2026-10-26"
urls = fx.catalog_urls(4)
story = {{"story_id": "s", "title": "Saiga genome reveals a bottleneck after the 2015 die-off"}}
base = fx.make_article(story, words=3700, source_lines=fx.default_sources(urls))
def rules(text):
    heading, _, body = text.partition("\n")
    item = m.build_item_manifest("animals", date, heading.lstrip("# ").strip(), body, prior_items=[], image_metadata={{}}, source_audit=None)
    out = m.evaluate_item_rules("animals", item, m.SECTION_RULES["animals"], audit_available=False)
    return {{"blocking": out["blocking"], "nonblocking": out["nonblocking"], "warnings": out["warnings"], "words": item["word_count"]}}
variants = {{
  "clean": base,
  "no_bottom_line": re.sub(r"(?m)^\*\*Bottom line:.*$", "", base),
  "no_confidence": re.sub(r"(?m)^\*\*Confidence:.*$", "", base),
  "no_counterpoint": base.replace("### Counterpoint Matrix", "### Misc"),
  "no_watch": base.replace("### Watch Next", "### Misc2"),
  "no_binomial": base.replace("*Genus species*", "Genus species"),
  "body_link": base.replace("### What happened\n", "### What happened\n\nSee https://example.org/x for more.\n"),
  "cruelty": base.replace("### What happened\n", "### What happened\n\nThis story concerns dogfighting and animal cruelty cases, and animal cruelty enforcement.\n"),
  "short": fx.make_article(story, words=900, source_lines=fx.default_sources(urls)),
  "medium": fx.make_article(story, words=2800, source_lines=fx.default_sources(urls)),
  "few_sources": fx.make_article(story, words=3700, source_lines=fx.default_sources()[:3]),
  "no_taxonomy": base.replace("IUCN Red List", "Status").replace("CITES", "Trade"),
  "first_person": base.replace("### What happened\n", "### What happened\n\nI could not open the registry, so I treat the list as unconfirmed and my own division gives 62%.\n"),
  "process_leak": base.replace("### What happened\n", "### What happened\n\nThe dossier copy of that page is dated 22 October, and the status is unconfirmed at the time of the dossier lookup.\n"),
  "dossier_in_a_quote": base.replace("### What happened\n", "### What happened\n\nThe agency wrote \"the dossier copy of the file was submitted to the regulator in March\" in its notice.\n"),
  "one_first_person": base.replace("### What happened\n", "### What happened\n\nThe totals are my own division of the two reported figures.\n"),
  "roman_numerals": base.replace("### What happened\n", "### What happened\n\nThe species is on CITES Appendix I species lists, and Part I taxa are covered. The authors wrote \"I could not find any such record in the files we examined\" in the discussion.\n"),
}}
result = {{k: rules(v) for k, v in variants.items()}}
pool_item = m.build_item_manifest("animals", date, "T", base.partition("\\n")[2], prior_items=[], image_metadata={{}}, source_audit=None)
pool_item["source_incorporated"] = True
pool_item["source_sections_cited"] = ["animals"]
pool = m.evaluate_item_rules("animals", pool_item, m.SECTION_RULES["animals"], audit_available=True)
result["pool_rule"] = {{"blocking": pool["blocking"], "warnings": pool["warnings"]}}
print(json.dumps(result))
'''


class EmailBodyTests(unittest.TestCase):
    def test_markdown_emphasis_marks_do_not_reach_the_email(self) -> None:
        summaries = [{"title": "Trout in *Salmo trutta* streams", "bottom_line": "A **clear** result in *Salmo salar*.", "confidence": "Medium",
                      "group": "fish_marine", "region": "Norway", "taxa": "", "part": ""}]
        text, html_body = tools.compose_email_bodies(label="L", date_human="D", summaries=summaries, digest_titles=["Item on *Mola mola*"])
        for body in (text, html_body):
            self.assertNotIn("*", body)
        self.assertIn("Salmo salar", text)
        self.assertIn("Mola mola", text)


class DeterministicHeaderSizeTests(unittest.TestCase):
    """Live-run finding: the fallback header was ~1,100 words for twenty stories (limit 600-800), so a failed model header meant no briefing."""

    def summaries(self, n: int) -> list[dict]:
        return [{"title": f"Story {i}: officials confirm a structural change in policy, markets and the balance of power in region {i}",
                 "bottom_line": " ".join(f"word{j}" for j in range(45)) + ".", "confidence": "Medium - one clause", "group": "other",
                 "region": f"Region {i}", "taxa": "", "part": "Part I"} for i in range(1, n + 1)]

    def words_before_cross_currents(self, text: str) -> int:
        return len(text.split("## Executive Summary")[1].split("### ")[0].split())

    def test_twenty_stories_fit_a_600_to_800_word_summary(self) -> None:
        text = tools.deterministic_header(label="L", date_human="D", prepared_by="P", summaries=self.summaries(20), summary_words=(600, 800), cross_label="Top 3 Structural Forces", min_cross=3)
        self.assertTrue(600 <= self.words_before_cross_currents(text) <= 800, self.words_before_cross_currents(text))
        for i in range(1, 21):
            self.assertIn(f"Story {i}:", text)

    def test_ten_stories_still_fit_the_animal_range_and_small_slates_are_topped_up(self) -> None:
        text = tools.deterministic_header(label="L", date_human="D", prepared_by="P", summaries=self.summaries(10), summary_words=(250, 750), min_cross=2)
        self.assertTrue(250 <= self.words_before_cross_currents(text) <= 750)
        small = tools.deterministic_header(label="L", date_human="D", prepared_by="P", summaries=self.summaries(2), summary_words=(250, 750), min_cross=2)
        self.assertGreaterEqual(self.words_before_cross_currents(small), 100)


class HeadingCanonicalisationTests(unittest.TestCase):
    """Live-run finding: 'Source bias' was renamed 'Primary Sources' (a prefix match), giving two of them."""

    def normalize(self, heading: str) -> str:
        article = f"## T\n<!-- story: s -->\n\n**Bottom line:** x\n\n### {heading}\n\nbody text here\n"
        text, _ = tools.normalize_article(article, {"story_id": "s", "title": "T", "prior_story_id": None})
        return [line for line in text.splitlines() if line.startswith("### ")][0]

    def test_headings_that_merely_start_with_a_known_word_are_left_alone(self) -> None:
        for heading in ("Source bias", "Source bias and the other side", "Sources of uncertainty", "Counterpoint view", "References to earlier work"):
            self.assertEqual(self.normalize(heading), f"### {heading}", heading)

    def test_genuine_aliases_are_still_canonicalised(self) -> None:
        for heading, expected in (("Sources", "Primary Sources"), ("References", "Primary Sources"), ("Source list", "Primary Sources"),
                                  ("Primary Source", "Primary Sources"), ("Sources (JST)", "Primary Sources"), ("Counterpoint", "Counterpoint Matrix"),
                                  ("Counterpoints", "Counterpoint Matrix"), ("Counterpoint matrix", "Counterpoint Matrix")):
            self.assertEqual(self.normalize(heading), f"### {expected}", heading)


class AnimalValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        env = {**os.environ, "BRIEFING_EDITION": "animal", "DAILY_BRIEFING_ROOT": cls.tmp.name,
               "DAILY_BRIEFING_SHARED_ROOT": str(REPO_ROOT)}
        proc = subprocess.run([sys.executable, "-c", RUNNER.format(scripts=str(Path(__file__).resolve().parent))],
                              capture_output=True, text=True, env=env, cwd=str(SCRIPTS_DIR), timeout=180)
        if proc.returncode:
            raise AssertionError(proc.stdout + proc.stderr)
        cls.result = json.loads(proc.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def blocking(self, key: str) -> str:
        return " | ".join(self.result[key]["blocking"])

    def test_clean_fixture_has_no_blocking_findings(self) -> None:
        self.assertEqual(self.result["clean"]["blocking"], [], self.result["clean"])
        self.assertGreaterEqual(self.result["clean"]["words"], 3500)

    def test_required_blocks_are_blocking(self) -> None:
        self.assertIn("Bottom line", self.blocking("no_bottom_line"))
        self.assertIn("Confidence", self.blocking("no_confidence"))
        self.assertIn("Counterpoint Matrix", self.blocking("no_counterpoint"))
        self.assertIn("Watch Next", self.blocking("no_watch"))
        self.assertIn("scientific binomial", self.blocking("no_binomial"))

    def test_watch_label_is_the_editions_not_the_dailys(self) -> None:
        self.assertNotIn("48h", self.blocking("no_watch"))

    def test_body_links_and_cruelty_are_blocking(self) -> None:
        self.assertIn("external links in the body", self.blocking("body_link"))
        self.assertIn("banned topic", self.blocking("cruelty"))

    def test_length_bands(self) -> None:
        self.assertIn("hard word floor", self.blocking("short"))
        self.assertEqual(self.result["medium"]["blocking"], [])
        self.assertTrue(any("soft word threshold" in w or "below the target" in w
                            for w in self.result["medium"]["nonblocking"] + self.result["medium"]["warnings"]))

    def test_first_person_is_blocking_when_pervasive_and_ignores_roman_numerals_and_quotes(self) -> None:
        self.assertIn("first person", self.blocking("first_person"))
        self.assertEqual(self.result["one_first_person"]["blocking"], [])
        self.assertTrue(any("first person" in w for w in self.result["one_first_person"]["warnings"]))
        self.assertEqual(self.result["roman_numerals"]["blocking"], [])
        self.assertFalse(any("first person" in w for w in self.result["roman_numerals"]["warnings"]))

    def test_references_to_the_pipelines_own_machinery_are_blocking_but_quoted_text_is_not(self) -> None:
        self.assertIn("pipeline's own machinery", self.blocking("process_leak"))
        self.assertEqual(self.result["dossier_in_a_quote"]["blocking"], [])

    def test_too_few_catalog_sources_is_a_warning_not_a_gate_for_this_edition(self) -> None:
        # A live run failed three otherwise verified articles on this rule alone.
        self.assertEqual([b for b in self.result["pool_rule"]["blocking"] if "catalog pool" in b], [])
        self.assertTrue(any("cites only 1 sources from the animals-tagged catalog pool" in w for w in self.result["pool_rule"]["warnings"]))

    def test_source_minimum_and_taxonomy_warning(self) -> None:
        self.assertIn("source threshold", self.blocking("few_sources"))
        self.assertTrue(any("Taxonomy & Status" in w for w in self.result["no_taxonomy"]["warnings"]))
        self.assertEqual(self.result["no_taxonomy"]["blocking"], [])


if __name__ == "__main__":
    unittest.main()
