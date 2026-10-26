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
