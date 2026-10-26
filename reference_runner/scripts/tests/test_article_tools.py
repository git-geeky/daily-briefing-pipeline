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
  "one_first_person": base.replace("### What happened\n", "### What happened\n\nThe totals are my own division of the two reported figures.\n"),
  "roman_numerals": base.replace("### What happened\n", "### What happened\n\nThe species is on CITES Appendix I species lists, and Part I taxa are covered. The authors wrote \"I could not find any such record in the files we examined\" in the discussion.\n"),
}}
print(json.dumps({{k: rules(v) for k, v in variants.items()}}))
'''


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

    def test_source_minimum_and_taxonomy_warning(self) -> None:
        self.assertIn("source threshold", self.blocking("few_sources"))
        self.assertTrue(any("Taxonomy & Status" in w for w in self.result["no_taxonomy"]["warnings"]))
        self.assertEqual(self.result["no_taxonomy"]["blocking"], [])


if __name__ == "__main__":
    unittest.main()
