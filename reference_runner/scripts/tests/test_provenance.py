"""Provenance checker: numbers, quotes, DOIs, URLs against the evidence corpus (no network)."""
from __future__ import annotations

import unittest

from _shared_imports import REPO_ROOT  # noqa: F401

import provenance_core as prov  # noqa: E402

CORPUS = [
    "Researchers tagged 412 animals between 2019 and 2023; annual survival was 0.87 (95% CI 0.81-0.92). "
    "The population fell by 62% to 1,050 individuals. Funding totalled 3.4 million dollars.",
    'The authors write: "mortality was concentrated in the first winter after weaning" in the discussion.',
    "doi:10.1234/abcd.5678 appears in the paper header.",
]

ARTICLE = """## Title
<!-- story: x -->

![Photo caption 2021 CC BY-SA 4.0](/tmp/a.jpg)

**Bottom line:** Survival was 0.87 and the population fell by 62% to 1,050 individuals.

### The evidence
The study tagged 412 animals. Costs reached 3.4 million dollars. Another 9,999 animals were mentioned without support.
As the authors put it, "mortality was concentrated in the first winter after weaning" and also "a wholly invented sentence that no source contains".
The species was assessed in 2021 on 14 March 2021 (page 12, Table 3, Appendix II).

### Watch Next
- 2026-11-03: hearing with 7000 attendees (https://example.org/hearing)

### Primary Sources
- Doe J, et al. (2024). Winter survival of tagged ungulates. *J Wildl Res*. doi:10.1234/abcd.5678 https://doi.org/10.1234/abcd.5678
- Fake A (2024). Imaginary study of spectral wolves. doi:10.9999/does.not.exist https://example.org/gone
- Real B (2024). A page. https://example.org/ok
"""


class FakeCrossref:
    def get_json(self, url, params=None):
        if "10.9999/does.not.exist" in url:
            raise RuntimeError("404 Not Found")
        if "10.1234/abcd.5678" in url:
            return {"message": {"title": ["Winter survival of tagged ungulates in a changing climate"]}}
        return {"message": {"title": ["Something entirely unrelated about databases"]}}


class NumberTests(unittest.TestCase):
    def test_matched_and_unmatched_numbers(self) -> None:
        report = prov.check_article(ARTICLE, CORPUS, check_network=False)
        unmatched = {item["text"] for item in report.unmatched_numbers}
        self.assertIn("9,999", unmatched)
        for ok in ("412", "62%", "1,050", "0.87"):
            self.assertFalse(any(ok in text for text in unmatched), ok)
        self.assertGreater(report.numbers_matched, 3)

    def test_scale_words_match_expanded_forms(self) -> None:
        corpus = ["The fund holds 3,400,000 dollars."]
        report = prov.check_article("Costs reached 3.4 million dollars.", corpus, check_network=False)
        self.assertEqual(report.unmatched_numbers, [])

    def test_years_dates_page_numbers_urls_and_sources_are_not_claims(self) -> None:
        texts = {c.text for c in prov.extract_numeric_claims(ARTICLE)}
        self.assertFalse(any(t.startswith(("2021", "14", "12", "3", "2026", "7000")) and t not in {"3.4 million"} for t in texts), texts)

    def test_small_bare_integers_are_ignored(self) -> None:
        self.assertEqual(prov.extract_numeric_claims("There were 3 groups and 7 sites."), [])


class QuoteTests(unittest.TestCase):
    def test_verbatim_quote_found_and_invented_quote_flagged(self) -> None:
        report = prov.check_article(ARTICLE, CORPUS, check_network=False)
        self.assertEqual(report.quotes_checked, 2)
        self.assertEqual(report.unmatched_quotes, ["a wholly invented sentence that no source contains"])

    def test_quote_matching_ignores_case_whitespace_and_smart_quotes(self) -> None:
        corpus = prov.normalize_text("He said: “Mortality  was concentrated\nin the FIRST winter”.")
        self.assertTrue(prov.quote_in_corpus("mortality was concentrated in the first winter", corpus))

    def test_ellipsis_quotes_are_checked_piecewise(self) -> None:
        corpus = prov.normalize_text("The committee agreed that the population is declining rapidly and that action is needed now.")
        self.assertTrue(prov.quote_in_corpus("the population is declining rapidly ... action is needed now", corpus))
        self.assertFalse(prov.quote_in_corpus("the population is declining rapidly ... nobody said this part at all", corpus))


class CitationTests(unittest.TestCase):
    def test_doi_in_corpus_ok_missing_doi_not_found(self) -> None:
        report = prov.check_article(ARTICLE, CORPUS, json_fetcher=FakeCrossref(), url_checker=lambda url: 200)
        self.assertEqual(report.dois["10.1234/abcd.5678"]["status"], "ok")
        self.assertEqual(report.dois["10.9999/does.not.exist"]["status"], "not_found")
        self.assertIn("10.9999/does.not.exist", report.bad_dois)

    def test_doi_title_mismatch_is_flagged(self) -> None:
        article = "### Primary Sources\n- Smith (2024). Spectral wolves of the northern forest. doi:10.5555/other https://example.org/x\n"
        report = prov.check_article(article, [], json_fetcher=FakeCrossref(), url_checker=lambda url: 200)
        self.assertEqual(report.dois["10.5555/other"]["status"], "mismatch")

    def test_dead_vs_blocked_vs_unreachable_urls(self) -> None:
        statuses = {"https://example.org/gone": 404, "https://example.org/ok": 200, "https://example.org/blocked": 403,
                    "https://example.org/flaky": None, "https://nx.invalid/x": 0}
        article = "### Primary Sources\n" + "".join(f"- Item {u}\n" for u in statuses)
        report = prov.check_article(article, [], url_checker=lambda url: statuses[url])
        dead = {d["url"] for d in report.dead_urls}
        self.assertEqual(dead, {"https://example.org/gone", "https://nx.invalid/x"})
        self.assertEqual(set(report.unknown_urls), {"https://example.org/blocked", "https://example.org/flaky"})

    def test_lookup_failure_is_unchecked_not_failed(self) -> None:
        class Down:
            def get_json(self, url, params=None):
                raise RuntimeError("connection reset")

        article = "### Primary Sources\n- A. Paper. doi:10.1111/xyz https://example.org/x\n"
        report = prov.check_article(article, [], json_fetcher=Down(), url_checker=lambda url: 200)
        self.assertEqual(report.dois["10.1111/xyz"]["status"], "unchecked")
        self.assertEqual(report.bad_dois, [])


class RepairPolicyTests(unittest.TestCase):
    def test_needs_repair_on_invented_quote_or_bad_doi_but_not_on_a_few_numbers(self) -> None:
        clean = prov.check_article("Costs reached 3.4 million dollars. Weight 120 kg.", ["3,400,000 dollars; 120 kg"], check_network=False)
        self.assertFalse(clean.needs_repair())
        a_few = prov.check_article("Weight 120 kg and 45 cm.", ["120 kg"], check_network=False)
        self.assertFalse(a_few.needs_repair())
        report = prov.check_article(ARTICLE, CORPUS, json_fetcher=FakeCrossref(), url_checker=lambda url: 200)
        self.assertTrue(report.needs_repair())
        brief = report.repair_brief()
        self.assertIn("quotation not found", brief)
        self.assertIn("10.9999/does.not.exist", brief)

    def test_many_unmatched_numbers_trigger_repair(self) -> None:
        article = " ".join(f"Count {n}0 kg." for n in range(11, 24))
        report = prov.check_article(article, ["nothing numeric"], check_network=False)
        self.assertTrue(report.needs_repair())


if __name__ == "__main__":
    unittest.main()
