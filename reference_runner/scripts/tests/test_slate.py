"""Ingest scoring, slate validation, and balance-aware selection (no model, no network)."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from _shared_imports import REPO_ROOT  # noqa: F401

import slate_core as slate  # noqa: E402

NOW = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)


def src(source_id, name, items, *, status="checked_no_relevant_update", bucket="ANIMAL — JOURNALS", tags=("en",)):
    return {"source_id": source_id, "name": name, "status": status, "major_bucket": bucket, "language_tags": list(tags),
            "region_tags": [], "items": items}


class IngestTests(unittest.TestCase):
    def test_scores_filters_and_dedupes(self) -> None:
        audit = {"sources": [
            src("a", "Journal A", [
                {"title": "Genome of the saiga reveals bottleneck after 2015 die-off", "url": "https://a.org/1", "published": "2026-10-25"},
                {"title": "Home", "url": "https://a.org/home", "published": ""},
                {"title": "Genome of the saiga reveals bottleneck after 2015 die-off!", "url": "https://a.org/dup", "published": ""},
            ]),
            src("b", "General News", [
                {"title": "Stock market rallies on rate-cut hopes today", "url": "https://b.org/1", "published": "2026-10-25"},
                {"title": "Endangered pangolin seized at port in record haul", "url": "https://b.org/2", "published": "2026-10-24"},
            ], bucket="PART I — GENERAL NEWS"),
            src("c", "Dead source", [{"title": "Whale song study published today", "url": "https://c.org/1"}], status="checked_failed_after_retry"),
            src("d", "Spanish Agency", [
                {"title": "Censo de lince ibérico alcanza nuevo máximo de población", "url": "https://d.es/1", "published": "2026-10-25"},
            ], tags=("es",)),
        ]}
        result = slate.build_ingest(audit, now=NOW, prior_titles=["Pangolin seizure at port sets record haul"],
                                    meta_by_source={"d": {"lang": "es", "tier": "institutional", "region": "Spain"}})
        titles = [i["title"] for i in result["items"]]
        self.assertIn("Genome of the saiga reveals bottleneck after 2015 die-off", titles)
        self.assertNotIn("Home", titles)
        self.assertFalse(any("Stock market" in t for t in titles), "non-animal headline from a general source must be dropped")
        self.assertFalse(any("Whale song" in t for t in titles), "sources that failed the audit contribute nothing")
        self.assertEqual(sum("saiga" in t for t in titles), 1, "near-duplicate headlines collapse")
        pangolin = next(i for i in result["items"] if "pangolin" in i["title"].lower())
        self.assertTrue(pangolin["seen_before"])
        lince = next(i for i in result["items"] if "lince" in i["title"].lower())
        self.assertEqual(lince["lang"], "es")
        self.assertGreater(lince["score"], pangolin["score"])  # native-language bonus, not penalised as seen
        self.assertEqual(result["stats"]["dropped_nav"], 1)

    def test_banned_topics_are_dropped_before_the_model_sees_them(self) -> None:
        audit = {"sources": [src("a", "A", [{"title": "Dogfighting ring broken up in rural county sting", "url": "https://a.org/1"}])]}
        result = slate.build_ingest(audit, now=NOW, banned_check=lambda t: ["cruelty"] if "dogfighting" in t.lower() else [])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["stats"]["dropped_banned"], 1)

    def test_markdown_digest_is_compact_and_flags_repeats(self) -> None:
        audit = {"sources": [src("a", "Journal A", [{"title": "Saiga genome reveals bottleneck after die-off", "url": "https://a.org/1", "published": "2026-10-25"}])]}
        md = slate.ingest_markdown(slate.build_ingest(audit, now=NOW, prior_titles=["Saiga genome reveals bottleneck after die-off"]))
        self.assertTrue(md.startswith("1. [en, institutional, seen-before; 2026-10-25]"))


def entry(story_id, rank, group="mammal", region="Kenya", material="deep", native=False, sources=None, title=None, **extra):
    return {"story_id": story_id, "title": title or f"A sufficiently long title about {story_id}", "rank": rank, "group": group,
            "region": region, "material": material, "native_language": native,
            "sources": sources if sources is not None else [{"url": "https://a.org/1", "lang": "en"}, {"url": "https://b.org/2", "lang": "en"}],
            "taxa": [{"common": "Thing", "scientific": "Genus species"}], "angle": "angle", **extra}


class ValidateTests(unittest.TestCase):
    def test_drops_bad_entries_with_reasons_and_dedupes_ids(self) -> None:
        raw = [
            entry("ok-1", 1),
            entry("ok-1", 2),
            entry("one-source", 3, sources=[{"url": "https://a.org/1"}]),
            entry("cruel", 4, title="Dogfighting ring uncovered in county operation"),
            entry("repeat", 5, title="Saiga genome reveals bottleneck after die-off in Kazakhstan"),
            {"story_id": "x", "title": "short"},
        ]
        report = slate.validate_slate(raw, prior_titles=["Saiga genome reveals bottleneck after die-off"],
                                      banned_check=lambda t: ["cruelty"] if "dogfighting" in t.lower() else [])
        self.assertEqual([e["story_id"] for e in report.kept], ["ok-1", "ok-1-2"])
        reasons = " | ".join(d["reason"] for d in report.dropped)
        for expected in ("fewer than 2 sources", "excluded topic", "repeats earlier briefing item", "too-short title"):
            self.assertIn(expected, reasons)

    def test_development_of_a_prior_story_is_allowed(self) -> None:
        report = slate.validate_slate([entry("dev", 1, title="Saiga genome reveals bottleneck after die-off: new data", novelty="development")],
                                      prior_titles=["Saiga genome reveals bottleneck after die-off"])
        self.assertEqual(len(report.kept), 1)

    def test_scientific_names_are_checked_and_photo_queries_default_to_taxa(self) -> None:
        raw = [entry("t", 1, taxa=[{"common": "Saiga", "scientific": "saiga tatarica"}, {"common": "Elephant", "scientific": "Loxodonta africana"}])]
        kept = slate.validate_slate(raw).kept[0]
        self.assertEqual([t["scientific"] for t in kept["taxa"]], ["", "Loxodonta africana"])
        self.assertEqual(kept["photo_queries"], ["Loxodonta africana", "Saiga", "Elephant"])

    def test_native_flag_is_inferred_from_source_languages(self) -> None:
        raw = [entry("n", 1, sources=[{"url": "https://a.org/1", "lang": "es"}, {"url": "https://b.org/2", "lang": "en"}])]
        self.assertTrue(slate.validate_slate(raw).kept[0]["native_language"])

    def test_an_edition_with_no_keyword_list_keeps_every_headline(self) -> None:
        audit = {"sources": [{"source_id": "s1", "name": "Wire", "status": "checked_no_relevant_update", "items": [
            {"title": "Parliament passes the budget after a late-night session", "url": "https://e.org/1", "published": "2026-10-25"},
            {"title": "A wolf pack was photographed near the border crossing", "url": "https://e.org/2", "published": "2026-10-25"}]}]}
        from datetime import datetime, timezone

        now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        everything = slate.build_ingest(audit, now=now, keywords=())
        self.assertEqual(everything["stats"]["kept"], 2)
        animal_only = slate.build_ingest(audit, now=now)  # default animal terms
        self.assertEqual(animal_only["stats"]["kept"], 1)

    def test_extract_json_tolerates_fences_and_chatter(self) -> None:
        self.assertEqual(slate.extract_json('Here you go:\n```json\n[{"a": 1}]\n```\nDone.'), [{"a": 1}])
        self.assertEqual(slate.extract_json('{"stories": []}'), {"stories": []})
        with self.assertRaises(ValueError):
            slate.extract_json("no json here")


class SelectTests(unittest.TestCase):
    def build(self):
        raw = []
        for i in range(1, 7):
            raw.append(entry(f"mam-{i}", i, group="mammal", region="United States"))
        raw += [
            entry("bird-1", 7, group="bird", region="Germany"),
            entry("bird-2", 8, group="bird", region="Japan", native=True),
            entry("bug-1", 9, group="invertebrate", region="Brazil", native=True),
            entry("fish-1", 10, group="fish_marine", region="Australia"),
            entry("herp-1", 11, group="herp", region="India", native=True),
            entry("thin-1", 12, group="bird", region="Peru", material="thin"),
        ]
        return slate.validate_slate(raw).kept

    def test_group_cap_and_balance_targets(self) -> None:
        selected, backups, notes = slate.select_slate(self.build(), target=8, minimum=6, maximum=12)
        groups = [e["group"] for e in selected]
        self.assertEqual(len(selected), 8)
        self.assertLessEqual(groups.count("mammal"), 3)
        self.assertGreaterEqual(groups.count("invertebrate"), 1)
        self.assertGreaterEqual(sum(1 for e in selected if e["native_language"]), 3)
        self.assertGreaterEqual(sum(1 for e in selected if not any(w in e["region"].lower() for w in ("united states", "germany"))), 2)
        self.assertFalse(set(e["story_id"] for e in selected) & set(e["story_id"] for e in backups))

    def test_thin_material_is_used_only_when_needed(self) -> None:
        selected, backups, _ = slate.select_slate(self.build(), target=8, minimum=6, maximum=12)
        self.assertNotIn("thin-1", [e["story_id"] for e in selected])
        self.assertIn("thin-1", [e["story_id"] for e in backups])

    def test_short_slate_is_reported(self) -> None:
        few = slate.validate_slate([entry("a", 1), entry("b", 2, group="bird")]).kept
        selected, _, notes = slate.select_slate(few, target=10, minimum=6, maximum=12)
        self.assertEqual(len(selected), 2)
        self.assertTrue(any("minimum 6" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
