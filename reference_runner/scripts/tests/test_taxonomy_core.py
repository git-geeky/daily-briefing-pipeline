"""Scripted taxonomy and IUCN checks against a fake GBIF (no network)."""
from __future__ import annotations

import unittest

from _shared_imports import REPO_ROOT  # noqa: F401

import taxonomy_core as tax  # noqa: E402

MATCHES = {
    "Myrmecobius fasciatus": {"matchType": "EXACT", "canonicalName": "Myrmecobius fasciatus", "status": "ACCEPTED", "rank": "SPECIES", "usageKey": 1},
    "Myrmecobius fasciatuss": {"matchType": "FUZZY", "canonicalName": "Myrmecobius fasciatus", "status": "ACCEPTED", "rank": "SPECIES", "usageKey": 1},
    "Loxodonta africana": {"matchType": "EXACT", "canonicalName": "Loxodonta africana", "status": "ACCEPTED", "rank": "SPECIES", "usageKey": 2},
    "Old synonymus": {"matchType": "EXACT", "canonicalName": "Loxodonta africana", "status": "SYNONYM", "rank": "SPECIES", "usageKey": 3, "acceptedUsageKey": 2},
    "Newly describa": {"matchType": "HIGHERRANK", "canonicalName": "Newly", "status": "ACCEPTED", "rank": "GENUS", "usageKey": 4},
}
IUCN = {1: {"code": "NT", "category": "NEAR_THREATENED"}, 2: {"code": "EN", "category": "ENDANGERED"}}


class FakeGbif:
    def __init__(self, fail_for=()):
        self.calls: list[str] = []
        self.fail_for = set(fail_for)

    def get_json(self, url, params=None):
        self.calls.append(url if not params else f"{url}?{params['name']}")
        if "/species/match" in url:
            name = params["name"]
            if name in self.fail_for:
                raise RuntimeError("gbif down")
            return MATCHES.get(name, {"matchType": "NONE"})
        key = int(url.split("/species/")[1].split("/")[0])
        if key not in IUCN:
            raise RuntimeError("404")
        return IUCN[key]


ARTICLE = """## T
**Bottom line:** The numbat *Myrmecobius fasciatus* rebounded.

### Taxonomy & Status

| Taxon | Scientific name | IUCN Red List | CITES |
|---|---|---|---|
| Numbat | *Myrmecobius fasciatus* | EN (2021, decreasing) | none |
| African elephant | *Loxodonta africana* | Endangered (2021) | Appendix I |

Prose mentions *Loxodonta africana* again and the plain words Genus species without italics.
"""


class LookupTests(unittest.TestCase):
    def test_exact_name_carries_the_iucn_category(self) -> None:
        facts = tax.lookup_name("Myrmecobius fasciatus", FakeGbif())
        self.assertTrue(facts.in_backbone)
        self.assertEqual((facts.iucn_code, facts.iucn_name), ("NT", "Near Threatened"))
        self.assertIn("/species/1/iucnRedListCategory", facts.iucn_url)

    def test_a_synonym_uses_the_accepted_taxons_category(self) -> None:
        facts = tax.lookup_name("Old synonymus", FakeGbif())
        self.assertEqual(facts.status, "SYNONYM")
        self.assertEqual(facts.iucn_code, "EN")

    def test_a_name_the_backbone_does_not_know_is_not_an_error_just_unknown(self) -> None:
        facts = tax.lookup_name("Newly describa", FakeGbif())
        self.assertFalse(facts.in_backbone)
        self.assertIsNone(facts.iucn_code)

    def test_an_outage_is_recorded_and_not_cached(self) -> None:
        cache: dict = {}
        facts = tax.lookup_names(["Myrmecobius fasciatus"], FakeGbif(fail_for=["Myrmecobius fasciatus"]), cache=cache)["Myrmecobius fasciatus"]
        self.assertTrue(facts.error)
        self.assertEqual(cache, {})

    def test_the_cache_saves_repeat_lookups(self) -> None:
        gbif, cache = FakeGbif(), {}
        tax.lookup_names(["Loxodonta africana"], gbif, cache=cache)
        before = len(gbif.calls)
        tax.lookup_names(["Loxodonta africana"], gbif, cache=cache)
        self.assertEqual(len(gbif.calls), before)


class ArticleCheckTests(unittest.TestCase):
    def test_only_italic_binomials_are_extracted(self) -> None:
        self.assertEqual(tax.extract_binomials(ARTICLE), ["Myrmecobius fasciatus", "Loxodonta africana"])

    def test_status_table_disagreeing_with_the_registry_is_reported_but_agreement_is_not(self) -> None:
        report = tax.check_article_taxonomy(ARTICLE, FakeGbif())
        self.assertEqual([m["name"] for m in report.status_mismatches], ["Myrmecobius fasciatus"])
        self.assertEqual(report.status_mismatches[0]["table"], "EN")
        self.assertIn("NT", report.status_mismatches[0]["registry"])
        self.assertTrue(report.needs_repair())
        self.assertIn("Confirm against the Red List", report.repair_brief())

    def test_a_misspelt_name_gets_the_accepted_spelling_in_the_brief(self) -> None:
        report = tax.check_article_taxonomy("## T\nThe *Myrmecobius fasciatuss* survived.\n", FakeGbif())
        self.assertEqual(report.misspellings, {"Myrmecobius fasciatuss": "Myrmecobius fasciatus"})
        self.assertIn('closest accepted name in the GBIF backbone is "Myrmecobius fasciatus"', report.repair_brief())

    def test_synonyms_and_unknown_names_are_noted_never_repaired(self) -> None:
        report = tax.check_article_taxonomy("## T\n*Old synonymus* and *Newly describa* were compared.\n", FakeGbif())
        self.assertEqual(report.synonyms, {"Old synonymus": "Loxodonta africana"})
        self.assertEqual(report.not_in_backbone, ["Newly describa"])
        self.assertFalse(report.needs_repair())

    def test_a_lookup_outage_never_triggers_a_repair(self) -> None:
        report = tax.check_article_taxonomy(ARTICLE, FakeGbif(fail_for=["Myrmecobius fasciatus", "Loxodonta africana"]))
        self.assertFalse(report.needs_repair())
        self.assertEqual(len(report.lookup_errors), 2)

    def test_first_category_in_a_cell_wins_so_a_downlisting_note_is_not_a_mismatch(self) -> None:
        article = "## T\n\n| Taxon | Scientific name | IUCN Red List |\n|---|---|---|\n| Numbat | *Myrmecobius fasciatus* | NT (from EN in 2008) |\n"
        self.assertEqual(tax.check_article_taxonomy(article, FakeGbif()).status_mismatches, [])


class RegistryFactsTests(unittest.TestCase):
    def test_facts_block_lists_each_taxon_with_status_and_flags_problems(self) -> None:
        block = tax.registry_facts([{"scientific": "Myrmecobius fasciatus"}, {"scientific": "Myrmecobius fasciatuss"}, {"scientific": "Newly describa"}, {"common": "no binomial"}], FakeGbif())
        self.assertIn("## Registry facts", block)
        self.assertIn("IUCN Red List: NT (Near Threatened)", block)
        self.assertIn("NOT a valid name; closest accepted name is *Myrmecobius fasciatus*", block)
        self.assertIn("not in the GBIF backbone", block)

    def test_no_taxa_means_no_block(self) -> None:
        self.assertEqual(tax.registry_facts([{"common": "x"}], FakeGbif()), "")


if __name__ == "__main__":
    unittest.main()
