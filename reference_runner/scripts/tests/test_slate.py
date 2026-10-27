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


class HackerNewsTests(unittest.TestCase):
    """The daily briefing's spec asks for Hacker News-driven stories; the conductor's harvest read only the source catalogue."""

    class Fetcher:
        def __init__(self, hits=None, fail=False):
            self.hits, self.fail, self.calls = hits or [], fail, []

        def get_json(self, url, params=None):
            self.calls.append((url, params))
            if self.fail:
                raise RuntimeError("503")
            return {"hits": self.hits}

    @staticmethod
    def hit(object_id, title, points, url="https://example.org/a", comments=40, created=1_792_990_000):
        return {"objectID": str(object_id), "title": title, "points": points, "num_comments": comments, "url": url, "created_at_i": created}

    def ingest(self):
        audit = {"sources": [src("r", "Reuters", [{"title": "Anthropic files S-1 showing a large first-half loss", "url": "https://sec.gov/s1", "published": "2026-10-26"}],
                                 bucket="PART II - AI NEWS")]}
        return slate.build_ingest(audit, now=NOW, keywords=())  # the daily edition keeps every headline

    def test_front_page_hits_are_read_ranked_and_filtered_by_points(self) -> None:
        fetcher = self.Fetcher([self.hit(1, "A short one", 900), self.hit(2, "Postgres 19 ships logical replication of sequences", 150),
                                self.hit(3, "Rust compiler memory use halved in nightly builds", 640), self.hit(4, "Low points story on a quiet topic", 20),
                                self.hit(5, "Ask HN: what are you working on this month", 300, url="")])
        hits = slate.fetch_hacker_news(fetcher, min_points=100, limit=3)
        self.assertEqual([h["points"] for h in hits], [640, 300, 150])  # 'A short one' is under 12 characters, the 20-point story under the bar
        self.assertEqual(hits[1]["url"], hits[1]["thread"])  # an Ask HN post has no article: the thread is its own source
        self.assertEqual(fetcher.calls[0][1]["tags"], "front_page")

    def test_an_unreadable_front_page_is_none_not_an_empty_day(self) -> None:
        self.assertIsNone(slate.fetch_hacker_news(self.Fetcher(fail=True)))

    def test_a_story_the_harvest_already_has_is_annotated_not_duplicated(self) -> None:
        ingest = self.ingest()
        before = ingest["items"][0]["score"]
        hits = [{"title": "Anthropic S-1 filing", "url": "https://sec.gov/s1/", "thread": "https://news.ycombinator.com/item?id=9", "points": 800,
                 "comments": 500, "published": "2026-10-26T01:00:00+00:00"}]
        counts = slate.add_hacker_news(ingest, hits, now=NOW)
        self.assertEqual((counts["annotated"], counts["added"]), (1, 0))
        self.assertEqual(len(ingest["items"]), 1)
        self.assertIn("HN 800 points, 500 comments; thread https://news.ycombinator.com/item?id=9", ingest["items"][0]["source_name"])
        self.assertGreater(ingest["items"][0]["score"], before)

    def test_a_new_story_becomes_a_candidate_with_the_article_as_its_url_and_the_thread_in_the_label(self) -> None:
        ingest = self.ingest()
        hits = [{"title": "Rust compiler memory use halved in nightly builds", "url": "https://blog.rust-lang.org/x", "thread": "https://news.ycombinator.com/item?id=3",
                 "points": 640, "comments": 210, "published": "2026-10-26T01:00:00+00:00"}]
        slate.add_hacker_news(ingest, hits, now=NOW)
        added = next(i for i in ingest["items"] if i["source_id"] == "hacker-news")
        self.assertEqual(added["url"], "https://blog.rust-lang.org/x")
        self.assertIn("thread https://news.ycombinator.com/item?id=3", added["source_name"])
        self.assertEqual(ingest["stats"]["hacker_news"], {"fetched": 1, "annotated": 0, "added": 1, "dropped_banned": 0})
        self.assertEqual((ingest["stats"]["kept"], ingest["stats"]["candidates"]), (2, 2))  # the counts stay consistent with the list
        self.assertIn("Hacker News front page (HN 640 points", slate.ingest_markdown(ingest))

    def test_banned_topics_and_repeats_are_handled_like_any_other_candidate(self) -> None:
        ingest = self.ingest()
        hits = [{"title": "Celebrity gossip about a streaming series finale", "url": "https://e.org/1", "thread": "https://news.ycombinator.com/item?id=5",
                 "points": 500, "comments": 5, "published": "2026-10-26T01:00:00+00:00"}]
        counts = slate.add_hacker_news(ingest, hits, now=NOW, banned_check=lambda title: ["celebrity"] if "gossip" in title else [])
        self.assertEqual((counts["added"], counts["dropped_banned"]), (0, 1))


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

    def test_internal_urls_in_a_slate_entry_do_not_count_as_sources(self) -> None:
        report = slate.validate_slate([entry("lan", 1, sources=[{"url": "http://192.168.1.1/admin"}, {"url": "http://localhost:8080/x"}, {"url": "https://a.org/1"}])])
        self.assertEqual(report.kept, [])
        self.assertIn("fewer than 2 sources", report.dropped[0]["reason"])

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


class SlateQualityGuardTests(unittest.TestCase):
    """Found comparing two models on the same candidates: balance swaps imported thin stories, "global" counted as outside the West, and
    unresolved placeholders ("confirm study population") reached the writers."""

    def test_a_balance_swap_never_brings_in_a_thin_story(self) -> None:
        raw = [entry(f"mam-{i}", i, group="mammal", region="United States", native=False) for i in range(1, 8)]
        raw += [entry("bug-1", 8, group="invertebrate", region="United States"), entry("bird-1", 9, group="bird", region="United States"),
                entry("thin-native", 10, group="bird", region="Peru", material="thin", native=True),
                entry("thin-east", 11, group="herp", region="India", material="thin")]
        selected, _, notes = slate.select_slate(slate.validate_slate(raw).kept, target=5, minimum=3, maximum=8)
        self.assertFalse([e for e in selected if e["material"] == "thin"])
        self.assertTrue(any("could not meet balance target" in n for n in notes))

    def test_a_balance_swap_still_prefers_a_solid_story_that_meets_the_rule(self) -> None:
        raw = [entry(f"mam-{i}", i, group="mammal", region="United States") for i in range(1, 8)]
        raw += [entry("solid-east", 12, group="bird", region="Japan", native=True), entry("thin-east", 9, group="herp", region="India", material="thin")]
        selected, _, _ = slate.select_slate(slate.validate_slate(raw).kept, target=5, minimum=3, maximum=8)
        self.assertIn("solid-east", [e["story_id"] for e in selected])

    def test_a_global_region_is_not_a_place_outside_the_west(self) -> None:
        raw = [entry(f"mam-{i}", i, group="mammal", region="Germany") for i in range(1, 4)]
        raw += [entry("g1", 4, group="bird", region="global (Max Planck Institute of Animal Behavior)"),
                entry("g2", 5, group="herp", region="Worldwide")]
        _, _, notes = slate.select_slate(slate.validate_slate(raw).kept, target=5, minimum=3, maximum=8)
        self.assertTrue(any("could not meet balance target: 2 stories centred outside" in n for n in notes))

    def test_placeholders_make_a_story_thin_and_are_reported(self) -> None:
        raw = [entry("p1", 1, region="North Pacific (confirm study population)"),
               entry("p2", 2, why_now="Paper and DOI must be traced before drafting."),
               entry("fine", 3, why_now="Officials confirmed the figures on 2026-10-26.", title="Court confirms the ruling after a long appeal process")]
        report = slate.validate_slate(raw)
        by_id = {e["story_id"]: e for e in report.kept}
        self.assertEqual((by_id["p1"]["material"], by_id["p2"]["material"], by_id["fine"]["material"]), ("thin", "thin", "deep"))
        self.assertEqual({f["story_id"] for f in report.flagged}, {"p1", "p2"})


if __name__ == "__main__":
    unittest.main()
