"""Photo stage: licence gating, caption provenance, selection, failure handling (no network)."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from _shared_imports import REPO_ROOT  # noqa: F401

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import briefing_manifest as manifest  # noqa: E402
import edition_core  # noqa: E402
import photo_core  # noqa: E402


def jpeg_bytes(width: int, height: int, shade: int = 120) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (shade, 90, 60)).save(buffer, "JPEG")
    return buffer.getvalue()


def commons_page(title, width=4000, height=2600, license_name="CC BY-SA 4.0", artist="<a href='x'>Jane Doe</a>",
                 description="African bush elephant <b>Loxodonta africana</b> drinking", mime="image/jpeg", restrictions=""):
    safe = title.replace(" ", "_")
    return {
        "title": f"File:{title}",
        "categories": [{"title": "Category:Loxodonta africana"}],
        "imageinfo": [{
            "width": width, "height": height, "mime": mime,
            "thumburl": f"https://upload.example/thumb/{safe}",
            "url": f"https://upload.example/orig/{safe}",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{safe.replace('.jpg', '')}_(Loxodonta_africana).jpg",
            "extmetadata": {
                "LicenseShortName": {"value": license_name}, "Artist": {"value": artist},
                "ImageDescription": {"value": description}, "Restrictions": {"value": restrictions},
            },
        }],
    }


class FakeFetcher:
    def __init__(self, commons=None, inat=None, openverse=None, images=None, fail=()):
        self.commons, self.inat, self.openverse = commons or [], inat or [], openverse or []
        self.images, self.fail = images or {}, set(fail)
        self.calls: list[str] = []

    def get_json(self, url, params=None):
        self.calls.append(url)
        if any(name in url for name in self.fail):
            raise RuntimeError("provider down")
        if "wikimedia" in url:
            return {"query": {"pages": {str(i): p for i, p in enumerate(self.commons)}}}
        if "inaturalist" in url:
            return {"results": self.inat}
        return {"results": self.openverse}

    def get_bytes(self, url):
        self.calls.append(url)
        if url in self.images:
            return self.images[url], "image/jpeg"
        return jpeg_bytes(1600, 1000), "image/jpeg"


def policy_edition() -> edition_core.Edition:
    return edition_core.load_edition("animal")


class PhotoStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_stage(self, fetcher, stories=None, **kwargs):
        stories = stories or [{"story_id": "elephant-story", "photo_queries": ["Loxodonta africana", "African bush elephant"]}]
        return photo_core.run_photo_stage(
            stories, image_dir=self.dir / "img", report_path=self.dir / "img" / "image-report.json",
            fetcher=fetcher, edition=policy_edition(), date="2026-10-26", **kwargs,
        )

    def test_selects_licensed_photo_and_writes_provenance_caption(self) -> None:
        fetcher = FakeFetcher(commons=[commons_page("Elephant drinking.jpg")])
        report = self.run_stage(fetcher)
        self.assertEqual(report["total_images"], 1)
        detail = report["details"][0]
        self.assertEqual(detail["license"], "CC BY-SA 4.0")
        self.assertEqual(detail["creator"], "Jane Doe")
        self.assertTrue(Path(detail["local_path"]).exists())
        line = report["_outcomes"]["elephant-story"][0]
        # The caption carries creator, licence, provider and a parenthesis-safe URL.
        self.assertIn("Photo: Jane Doe, CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/), via Wikimedia Commons", line)
        self.assertNotIn("(Loxodonta_africana)", line.split("](")[0])
        self.assertIn("%28Loxodonta_africana%29", line)
        # The manifest accepts it: licence token found, kind not weak, extracted as an image.
        images = manifest.extract_images(line, image_metadata={detail["local_path"]: detail})
        self.assertEqual(len(images), 1)
        self.assertTrue(manifest.IMAGE_LICENSE_RE.search(images[0]["alt"]))
        self.assertNotEqual(images[0]["kind"], "weak")
        report_json = json.loads((self.dir / "img" / "image-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report_json["details"][0]["accepted_by_rule"], "licensed_photo")
        self.assertGreaterEqual(report_json["details"][0]["confidence"], 0.8)

    def test_disallowed_licences_small_files_and_off_subject_items_are_rejected(self) -> None:
        fetcher = FakeFetcher(commons=[
            commons_page("NC photo.jpg", license_name="CC BY-NC 4.0"),
            commons_page("ND photo.jpg", license_name="CC BY-ND 2.0"),
            commons_page("Tiny.jpg", width=640, height=480),
            commons_page("Distribution map.jpg"),
            commons_page("Restricted.jpg", restrictions="personality"),
            commons_page("Diagram.png", mime="image/png"),
            commons_page("Orange-bellied elephant keeper photo.jpg", description="Loxodonta africana at dusk"),
        ])
        report = self.run_stage(fetcher)
        self.assertEqual(report["total_images"], 1)
        reasons = " | ".join(r["reason"] for r in report["stories"]["elephant-story"]["rejected"])
        for expected in ("licence not allowed", "too small", "non-photographic", "restrictions present", "mime image/png"):
            self.assertIn(expected, reasons)

    def test_historic_illustrations_are_not_accepted_as_photographs(self) -> None:
        page = commons_page("Myrmecobius fasciatus Gould.jpg", description="Myrmecobius fasciatus numbat", license_name="Public domain")
        page["imageinfo"][0]["extmetadata"]["DateTimeOriginal"] = {"value": "1863"}
        lithograph = commons_page("Numbat lithograph plate.jpg", description="Myrmecobius fasciatus numbat")
        current = commons_page("Numbat in woodland.jpg", description="Myrmecobius fasciatus numbat in woodland")
        current["imageinfo"][0]["extmetadata"]["DateTimeOriginal"] = {"value": "2019-08-02"}
        report = self.run_stage(FakeFetcher(commons=[page, lithograph, current]),
                                [{"story_id": "numbat", "photo_queries": ["Myrmecobius fasciatus", "numbat"]}])
        self.assertEqual([d["page_url"].split("File:")[1][:12] for d in report["details"]], ["Numbat_in_wo"])
        reasons = " | ".join(r["reason"] for r in report["stories"]["numbat"]["rejected"])
        self.assertIn("historic image (1863)", reasons)
        self.assertIn("non-photographic", reasons)

    def test_provider_outage_falls_through_to_next_provider(self) -> None:
        inat = [{
            "id": 99, "uri": "https://www.inaturalist.org/observations/99", "taxon": {"name": "Loxodonta africana", "preferred_common_name": "African savanna elephant"},
            "user": {"login": "naturelover", "name": "Nat Lover"},
            "photos": [{"license_code": "cc-by", "url": "https://inat.example/photos/1/square.jpg", "original_dimensions": {"width": 2048, "height": 1365}}],
        }]
        fetcher = FakeFetcher(inat=inat, fail=("wikimedia",))
        report = self.run_stage(fetcher)
        self.assertEqual(report["total_images"], 1)
        detail = report["details"][0]
        self.assertEqual(detail["provider"], "inaturalist")
        self.assertEqual(detail["license"], "CC BY")  # no version invented when the provider gives none
        self.assertTrue(any("provider error" in r.get("reason", "") for r in report["stories"]["elephant-story"]["rejected"]))

    def test_max_total_and_cross_story_dedupe(self) -> None:
        page = commons_page("Shared photo.jpg")
        fetcher = FakeFetcher(commons=[page, commons_page("Second photo.jpg", description="Loxodonta africana herd")])
        stories = [{"story_id": f"s{i}", "photo_queries": ["Loxodonta africana"]} for i in range(3)]
        report = self.run_stage(fetcher, stories)
        pages = [d["page_url"] for d in report["details"]]
        self.assertEqual(len(pages), len(set(pages)), "the same photograph was used twice")
        self.assertLessEqual(report["total_images"], 24)
        self.assertTrue(report["stories_missing_photo"], "third story should have run out of distinct photographs")

    def test_no_candidates_reports_missing_and_insert_strips_images(self) -> None:
        report = self.run_stage(FakeFetcher())
        self.assertEqual(report["stories_missing_photo"], ["elephant-story"])
        article = "## Title\n<!-- story: x -->\n\n![old](x.png)\n\n**Bottom line:** y\n"
        stripped = photo_core.insert_photos(article, [])
        self.assertNotIn("![", stripped)
        self.assertIn("**Bottom line:**", stripped)

    def test_insert_photos_places_images_after_marker(self) -> None:
        article = "## Title\n<!-- story: x -->\n\n**Bottom line:** y\n"
        result = photo_core.insert_photos(article, ["![cap one](a.jpg)", "![cap two](b.jpg)"])
        lines = [line for line in result.split("\n") if line.strip()]
        self.assertEqual(lines[:2], ["## Title", "<!-- story: x -->"])
        self.assertEqual(lines[2:4], ["![cap one](a.jpg)", "![cap two](b.jpg)"])

    def test_licence_normalisation_never_invents_a_version(self) -> None:
        self.assertEqual(photo_core.normalize_license("cc-by-sa"), ("CC BY-SA", "cc-by-sa"))
        self.assertEqual(photo_core.normalize_license("by-sa", "2.0"), ("CC BY-SA 2.0", "cc-by-sa"))
        for denied in ("CC BY-NC 4.0", "CC BY-ND 2.0", "All rights reserved", "GFDL", "", None, "PD-US-expired", "PD US 1923"):
            self.assertEqual(photo_core.normalize_license(denied), (None, None), denied)
        self.assertEqual(photo_core.normalize_license("PD-USGov-NASA"), ("Public domain", "pd"))

    def test_the_jurisdiction_port_stays_in_the_label_and_the_deed_link_follows_it(self) -> None:
        self.assertEqual(photo_core.normalize_license("CC BY 2.5 PL"), ("CC BY 2.5 PL", "cc-by"))
        self.assertEqual(photo_core.normalize_license("CC BY-SA 3.0 de"), ("CC BY-SA 3.0 DE", "cc-by-sa"))
        self.assertEqual(photo_core.license_url("CC BY 2.5 PL"), "https://creativecommons.org/licenses/by/2.5/pl/")
        self.assertEqual(photo_core.license_url("CC BY-SA 4.0"), "https://creativecommons.org/licenses/by-sa/4.0/")
        self.assertEqual(photo_core.license_url("CC0 1.0"), "https://creativecommons.org/publicdomain/zero/1.0/")
        self.assertEqual(photo_core.license_url("Public domain"), "")

    def test_the_caption_carries_the_deed_link_and_the_modification_notice(self) -> None:
        import json

        template = json.loads((REPO_ROOT / "editions" / "animal.json").read_text(encoding="utf-8"))["photos"]["caption_template"]
        candidate = photo_core.PhotoCandidate(provider="wikimedia", title="Lynx.jpg", creator="A. Photographer", license="CC BY 2.5 PL",
                                              license_family="cc-by", width=2000, height=1200, image_url="https://x/y.jpg",
                                              page_url="https://commons.wikimedia.org/wiki/File:Lynx.jpg", description="A lynx at dusk")
        caption = photo_core.make_caption(candidate, template)
        self.assertIn("CC BY 2.5 PL (https://creativecommons.org/licenses/by/2.5/pl/)", caption)
        self.assertIn("resized", caption)


class RequestsFetcherTests(unittest.TestCase):
    class Resp:
        def __init__(self, status, headers=None, payload=None):
            self.status_code, self.headers, self._payload = status, headers or {}, payload or {}
            self.content = b"bytes"

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests

                raise requests.HTTPError(f"{self.status_code}")

        def json(self):
            return self._payload

    def make(self, responses, breaker_threshold: int = 0):
        sleeps: list[float] = []
        now = [1000.0]

        def sleep(seconds: float) -> None:    # the clock moves on while a thread sleeps, as it does in a run
            sleeps.append(seconds)
            now[0] += seconds

        fetcher = photo_core.RequestsFetcher(retries=3, sleep=sleep, clock=lambda: now[0], breaker_threshold=breaker_threshold)
        queue = list(responses)
        fetcher._session.get = lambda url, params=None, timeout=None: queue.pop(0)  # type: ignore[assignment]
        fetcher.unused = queue  # type: ignore[attr-defined]  # the responses no request has asked for
        return fetcher, sleeps

    def test_honours_a_short_retry_after(self) -> None:
        fetcher, sleeps = self.make([self.Resp(429, {"Retry-After": "7"}), self.Resp(200, payload={"ok": 1})])
        self.assertEqual(fetcher.get_json("https://commons.example/api"), {"ok": 1})
        self.assertIn(7.0, sleeps)

    def test_a_retry_after_beyond_the_cap_gives_the_host_up_for_the_run_without_waiting(self) -> None:
        """Seventh live run: upload.wikimedia.org answered 429 with Retry-After 600 and each image slept 3 x 45 s before failing."""
        fetcher, sleeps = self.make([self.Resp(429, {"Retry-After": "600"}), self.Resp(200, payload={"ok": 1})])
        with self.assertRaises(RuntimeError) as caught:
            fetcher.get_json("https://upload.example/a.jpg")
        self.assertIn("asked for 600 s", str(caught.exception))
        self.assertEqual([s for s in sleeps if s > 1.5], [], "no retry wait at all")
        self.assertEqual(fetcher.summary()["given_up_on"], ["upload.example"])
        with self.assertRaises(RuntimeError) as skipped:
            fetcher.get_json("https://upload.example/b.jpg")
        self.assertIn("skipped", str(skipped.exception))
        self.assertEqual(len(fetcher.unused), 1, "the second request never reached the host")  # type: ignore[attr-defined]
        self.assertEqual(fetcher.get_json("https://other.example/api"), {"ok": 1}, "other hosts are not affected")

    def test_the_retry_waits_of_one_call_share_one_budget(self) -> None:
        fetcher, sleeps = self.make([self.Resp(429, {"Retry-After": "30"})] * 4 + [self.Resp(200, payload={"ok": 1})])
        with self.assertRaises(RuntimeError):
            fetcher.get_json("https://commons.example/api")
        self.assertEqual([s for s in sleeps if s > 1.5], [30.0, 30.0], "a third 30 s wait would pass the 60 s budget")
        self.assertEqual(len(fetcher.unused), 2, "three requests were made, not four or five")  # type: ignore[attr-defined]

    def test_a_wait_for_one_thread_holds_the_other_threads_back_from_that_host(self) -> None:
        sleeps: list[float] = []
        fetcher = photo_core.RequestsFetcher(sleep=sleeps.append, clock=lambda: 1000.0)   # a clock that stands still: the other thread asks at once
        fetcher._wait_for_retry("https://commons.example/api", 30.0)
        sleeps.clear()
        fetcher._space("https://commons.example/api")     # another thread's request, clock unchanged: it queues behind the cooldown
        self.assertEqual(sleeps, [30.0])
        sleeps.clear()
        fetcher._space("https://api.inaturalist.example/v1")
        self.assertEqual(sleeps, [], "another host is not held back")

    def test_a_missing_file_is_not_retried_and_does_not_count_against_the_host(self) -> None:
        fetcher, sleeps = self.make([self.Resp(404), self.Resp(404), self.Resp(200, payload={"ok": 1})], breaker_threshold=2)
        for _ in range(2):
            with self.assertRaises(RuntimeError) as caught:
                fetcher.get_json("https://commons.example/missing")
            self.assertIn("HTTP 404", str(caught.exception))
        self.assertEqual([s for s in sleeps if s > 1.5], [], "a 404 is not asked again after a wait")
        self.assertEqual(fetcher.get_json("https://commons.example/api"), {"ok": 1}, "two 404s do not open the breaker")
        self.assertEqual(fetcher.summary()["given_up_on"], [])

    def test_the_fetcher_reports_where_its_time_went(self) -> None:
        fetcher, sleeps = self.make([self.Resp(429, {"Retry-After": "7"}), self.Resp(200, payload={"ok": 1}), self.Resp(200, payload={"ok": 2})])
        fetcher.get_json("https://commons.example/api")
        fetcher.get_json("https://commons.example/api")   # the same host again: spaced by the clock slot, one second after the first
        summary = fetcher.summary()
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["top_hosts"], {"commons.example": 3})
        self.assertEqual((summary["retries"], summary["retry_wait_s"]), (1, 7))
        self.assertGreater(summary["spacing_wait_s"], 0)

    def test_gives_up_with_a_clear_error_after_retries(self) -> None:
        fetcher, _ = self.make([self.Resp(503)] * 5)
        with self.assertRaises(RuntimeError) as caught:
            fetcher.get_json("https://commons.example/api")
        self.assertIn("failed", str(caught.exception))


class ProviderOrderAndBreakerTests(unittest.TestCase):
    """Sixth live run: Openverse (the lowest-priority provider) timed out on every query of every story and cost 8 of the stage's 12 minutes."""

    def test_lower_priority_providers_are_not_asked_once_enough_candidates_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = FakeFetcher(commons=[commons_page(f"Elephant {n}.jpg") for n in range(8)])
            photo_core.photos_for_story(fetcher, {"story_id": "s1", "photo_queries": ["Loxodonta africana", "African elephant", "elephant"]},
                                        image_dir=Path(tmp), policy={}, per_min=1, per_max=2, used_pages=set())
        asked = [url for url in fetcher.calls if "wikimedia" in url or "inaturalist" in url or "openverse" in url]
        self.assertTrue(any("wikimedia" in url for url in asked))
        self.assertFalse(any("inaturalist" in url or "openverse" in url for url in asked), asked)

    def test_the_other_providers_are_asked_when_the_first_one_finds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = FakeFetcher(commons=[], inat=[], openverse=[])
            photo_core.photos_for_story(fetcher, {"story_id": "s1", "photo_queries": ["Loxodonta africana"]},
                                        image_dir=Path(tmp), policy={}, per_min=1, per_max=2, used_pages=set())
        self.assertEqual([("wikimedia" in u, "inaturalist" in u, "openverse" in u) for u in fetcher.calls],
                         [(True, False, False), (False, True, False), (False, False, True)])  # in priority order

    def make(self, breaker_threshold: int):
        import requests

        sleeps: list[float] = []
        fetcher = photo_core.RequestsFetcher(retries=3, sleep=sleeps.append, clock=lambda: 1000.0, breaker_threshold=breaker_threshold)
        calls: list[str] = []

        def get(url, params=None, timeout=None):
            calls.append(url)
            raise requests.ReadTimeout("read timed out")

        fetcher._session.get = get  # type: ignore[assignment]
        return fetcher, calls

    def test_a_timeout_is_retried_once_not_three_times(self) -> None:
        fetcher, calls = self.make(0)
        with self.assertRaises(RuntimeError):
            fetcher.get_json("https://api.openverse.example/v1/images/")
        self.assertEqual(len(calls), 2)

    def test_a_host_that_keeps_failing_is_not_asked_again(self) -> None:
        fetcher, calls = self.make(2)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                fetcher.get_json("https://api.openverse.example/v1/images/")
        before = len(calls)
        with self.assertRaises(RuntimeError) as caught:
            fetcher.get_json("https://api.openverse.example/v1/images/?q=x")
        self.assertIn("skipped", str(caught.exception))
        self.assertEqual(len(calls), before, "no request is made to a host that has given up")
        self.assertIn("api.openverse.example", fetcher.summary()["given_up_on"])

    def test_without_a_breaker_a_failing_host_is_asked_every_time(self) -> None:
        fetcher, calls = self.make(0)
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                fetcher.get_json("https://api.openverse.example/v1/images/")
        self.assertEqual(len(calls), 6)


class ParallelPhotoTests(unittest.TestCase):
    """Found in the fifth live run: the photo stage took 16 minutes, strictly one request at a time."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def stage(self, fetcher, stories, **kwargs):
        return photo_core.run_photo_stage(stories, image_dir=self.dir / "img", fetcher=fetcher, edition=policy_edition(), date="2026-10-26", **kwargs)

    def test_requests_to_one_host_stay_spaced_however_many_threads_ask_and_other_hosts_do_not_wait(self) -> None:
        import threading

        waits: list[float] = []
        lock = threading.Lock()

        def sleep(seconds):
            with lock:
                waits.append(round(seconds, 3))

        fetcher = photo_core.RequestsFetcher(sleep=sleep, clock=lambda: 100.0)
        threads = [threading.Thread(target=fetcher._space, args=("https://commons.wikimedia.org/w/api.php",)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(waits), [1.0, 2.0, 3.0], "the first caller goes at once, the others take the next free seconds")
        waits.clear()
        fetcher._space("https://api.inaturalist.org/v1/observations")
        self.assertEqual(waits, [], "a different host has its own schedule")

    def test_a_story_whose_search_budget_is_spent_stops_looking_and_keeps_what_it_has(self) -> None:
        fetcher = FakeFetcher(commons=[commons_page(f"Elephant {n}.jpg") for n in range(8)])
        times = iter([0.0, 5.0, 100.0, 200.0, 300.0])      # started at 0; the first check is at 5 s, the second after the 30 s budget
        result = photo_core.photos_for_story(fetcher, {"story_id": "s1", "photo_queries": ["Loxodonta africana"]}, image_dir=self.dir / "img",
                                             policy={"story_budget_seconds": 30}, per_min=1, per_max=2, used_pages=set(), clock=lambda: next(times))
        self.assertEqual(len(result.chosen), 0, "the budget ran out before the first download, so nothing is stored")
        self.assertTrue(any("search budget was spent" in r.get("reason", "") for r in result.rejected))
        self.assertFalse([u for u in fetcher.calls if "upload" in u or "thumb" in u], "no download is started once the budget is spent")

    def test_the_budget_does_not_touch_a_story_that_finishes_in_time_or_the_stories_after_it(self) -> None:
        fetcher = FakeFetcher(commons=[commons_page("Elephant drinking.jpg"), commons_page("Elephant walking.jpg")])
        stories = [{"story_id": f"story-{n}", "photo_queries": ["Loxodonta africana"]} for n in range(1, 4)]
        edition = policy_edition()
        edition.extra["photos"] = {**edition.extra["photos"], "story_budget_seconds": 30}
        report = photo_core.run_photo_stage(stories, image_dir=self.dir / "img", fetcher=fetcher, edition=edition, date="2026-10-28")
        self.assertGreaterEqual(report["total_images"], 1)
        self.assertFalse([r for info in report["stories"].values() for r in info["rejected"] if "budget" in r.get("reason", "")])
        self.assertEqual(sorted(report["stories"]), ["story-1", "story-2", "story-3"], "every story had its turn")

    def test_the_cap_on_the_whole_briefing_and_duplicate_photographs_are_resolved_in_story_order(self) -> None:
        fetcher = FakeFetcher(commons=[commons_page("Elephant drinking.jpg"), commons_page("Elephant walking.jpg")])
        stories = [{"story_id": f"story-{n}", "photo_queries": ["Loxodonta africana"]} for n in range(1, 5)]
        report = self.stage(fetcher, stories)
        per_story = {sid: len(info["chosen"]) for sid, info in report["stories"].items()}
        pages = [d["page_url"] for d in report["details"]]
        self.assertEqual(len(pages), len(set(pages)), "no photograph is used twice in one briefing")
        self.assertEqual(sum(per_story.values()), report["total_images"])
        self.assertGreaterEqual(per_story["story-1"], 1, "the earliest story keeps its photograph")
        for sid, lines in report["_outcomes"].items():
            self.assertEqual(len(lines), per_story[sid], "the markdown lines follow the photographs that were kept")


if __name__ == "__main__":
    unittest.main()
