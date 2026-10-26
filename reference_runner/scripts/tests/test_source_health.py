"""Source health: classify failures, follow possibly-transitory ones, retire permanent ones, substitute or remove, re-probe (no network)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from _shared_imports import REPO_ROOT  # noqa: F401

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import source_audit_core as audit  # noqa: E402
import source_health_core as health  # noqa: E402

START = date(2026, 10, 1)


def day(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


def entry(number: int, *, bucket="ANIMAL", languages=("es",), regions=("latin-america",), topics=("birds",)):
    return audit.SourceCatalogEntry(
        source_id=f"source-{number}", catalog_number=number, name=f"Source {number}", url=f"https://s{number}.example.org/", why="fixture",
        bucket=bucket, major_bucket=bucket, sub_bucket=None, section="animals", domain=f"s{number}.example.org", region_tags=tuple(regions),
        language_tags=tuple(languages), topics=tuple(topics))


def ok(sid):
    return {"source_id": sid, "status": "checked_no_relevant_update", "http_status": 200, "items": [{"title": "x"}], "errors": []}


def failing(sid, *, http=None, errors=(), verdict="", items=0):
    return {"source_id": sid, "status": "checked_failed_after_retry" if http not in (401, 402, 403, 429) else "checked_unavailable",
            "http_status": http, "errors": list(errors), "adaptive_fetch": {"verdict": verdict}, "items": [{"title": "x"}] * items}


def run_days(state, catalog, per_day, days):
    """per_day(sid, day_number) -> audit result; one audit per day for ``days`` days."""
    reports = []
    for n in range(days):
        reports.append(health.record_results(state, [per_day(e.source_id, n) for e in catalog], day(n), catalog))
    return reports


class ClassifyTests(unittest.TestCase):
    def test_failure_kinds(self) -> None:
        cases = [
            (ok("a"), "ok"),
            (failing("a", http=402), "paywall"),
            (failing("a", http=401), "paywall"),
            (failing("a", http=200, verdict="auth_required", errors=["adaptive_trigger: auth_required"]), "paywall"),
            (failing("a", http=200, verdict="auth_required", errors=["adaptive_trigger: auth_required"], items=8), "transient"),  # a login link on a readable page
            (failing("a", http=404), "gone"),
            (failing("a", http=410), "gone"),
            (failing("a", errors=["URLError: <urlopen error [Errno -2] Name or service not known>"]), "gone"),
            (failing("a", http=403), "blocked"),
            (failing("a", http=429), "blocked"),
            (failing("a", http=200, verdict="challenge"), "blocked"),
            (failing("a", errors=["TimeoutError: timed out"]), "transient"),
            (failing("a", http=503), "transient"),
        ]
        for result, expected in cases:
            self.assertEqual(health.classify_result(result), expected, result)


class TrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = [entry(1), entry(2), entry(3, languages=("fr",)), entry(4)]
        self.state = health.default_state()

    def test_a_transitory_failure_is_watched_and_forgotten_when_the_source_answers(self) -> None:
        first = health.record_results(self.state, [failing("source-1", errors=["TimeoutError"]), ok("source-2"), ok("source-3"), ok("source-4")],
                                      day(0), self.catalog)
        self.assertEqual(first["newly_watched"], ["source-1"])
        self.assertEqual(self.state["sources"]["source-1"]["state"], "watching")
        second = health.record_results(self.state, [ok(e.source_id) for e in self.catalog], day(1), self.catalog)
        self.assertEqual(second["recovered"], ["source-1"])
        self.assertEqual(self.state["sources"]["source-1"]["state"], "ok")
        self.assertNotIn("streak", self.state["sources"]["source-1"])

    def test_a_hard_paywall_is_retired_after_three_separate_days_and_its_substitutes_are_named(self) -> None:
        reports = run_days(self.state, self.catalog, lambda sid, n: failing(sid, http=402) if sid == "source-1" else ok(sid), 3)
        self.assertEqual([r["newly_retired"] for r in reports], [[], [], ["source-1"]])
        retired = self.state["sources"]["source-1"]["retired"]
        self.assertEqual(retired["kind"], "paywall")
        self.assertEqual(retired["substitutes"], ["source-2", "source-4"])  # same bucket and language; source-3 is French
        self.assertEqual(reports[-1]["uncovered"], [])

    def test_a_retired_source_with_no_other_source_in_its_bucket_and_language_is_removed_and_listed_uncovered(self) -> None:
        reports = run_days(self.state, self.catalog, lambda sid, n: failing(sid, http=402) if sid == "source-3" else ok(sid), 3)
        self.assertEqual(reports[-1]["newly_retired"], ["source-3"])
        self.assertEqual(self.state["sources"]["source-3"]["retired"]["substitutes"], [])
        self.assertEqual(reports[-1]["uncovered"], ["source-3"])
        self.assertIn("no substitute", health.render_markdown(self.state, reports[-1]))

    def test_repeated_checks_on_one_day_count_once(self) -> None:
        for _ in range(5):
            health.record_results(self.state, [failing("source-1", http=402), ok("source-2"), ok("source-3"), ok("source-4")], day(0), self.catalog)
        self.assertEqual(self.state["sources"]["source-1"]["state"], "watching")
        self.assertEqual(len(self.state["sources"]["source-1"]["streak"]["days"]), 1)

    def test_a_page_that_is_gone_needs_five_days_spread_over_a_week(self) -> None:
        reports = run_days(self.state, self.catalog, lambda sid, n: failing(sid, http=404) if sid == "source-1" else ok(sid), 5)
        self.assertEqual(self.state["sources"]["source-1"]["state"], "watching", "five consecutive days is not yet a week")
        report = health.record_results(self.state, [failing("source-1", http=404)] + [ok(e.source_id) for e in self.catalog[1:]], day(8), self.catalog)
        self.assertEqual(report["newly_retired"], ["source-1"])
        self.assertEqual(self.state["sources"]["source-1"]["retired"]["kind"], "gone")

    def test_a_source_that_refuses_automated_readers_gets_three_weeks_and_mixed_failures_two(self) -> None:
        run_days(self.state, self.catalog, lambda sid, n: failing(sid, http=403) if sid == "source-1" else ok(sid), 20)
        self.assertEqual(self.state["sources"]["source-1"]["state"], "watching")
        run_days(self.state, self.catalog, lambda sid, n: failing(sid, http=403) if sid == "source-1" else ok(sid), 22)
        self.assertEqual(self.state["sources"]["source-1"]["retired"]["kind"], "blocked")
        mixed = health.default_state()
        run_days(mixed, self.catalog, lambda sid, n: (failing(sid, http=404) if n % 2 else failing(sid, errors=["TimeoutError"])) if sid == "source-2" else ok(sid), 15)
        self.assertEqual(mixed["sources"]["source-2"]["retired"]["kind"], "unreachable")

    def test_a_systemic_day_counts_for_nothing(self) -> None:
        everyone_down = [failing(e.source_id, http=404) for e in self.catalog]
        report = health.record_results(self.state, everyone_down, day(0), self.catalog)
        self.assertTrue(report["systemic_day"])
        self.assertEqual(self.state["sources"], {})
        for n in range(1, 12):  # even many such days (a sandbox's blocked IP range) never retire anything
            health.record_results(self.state, everyone_down, day(n), self.catalog)
        self.assertEqual(health.retired_ids(self.state), set())

    def test_a_retired_source_is_probed_after_thirty_days_and_reinstated_the_first_time_it_answers(self) -> None:
        run_days(self.state, self.catalog, lambda sid, n: failing(sid, http=402) if sid == "source-1" else ok(sid), 3)
        retired_on = day(2)
        self.assertEqual(health.due_for_probe(self.state, day(10)), set())
        later = (date.fromisoformat(retired_on) + timedelta(days=30)).isoformat()
        self.assertEqual(health.due_for_probe(self.state, later), {"source-1"})
        active, skipped = health.filter_catalog(self.catalog, self.state, day(10))
        self.assertEqual([e.source_id for e in active], ["source-2", "source-3", "source-4"])
        self.assertEqual(skipped, ["source-1"])
        active, skipped = health.filter_catalog(self.catalog, self.state, later)
        self.assertIn("source-1", [e.source_id for e in active], "due for a re-probe")
        still = health.record_results(self.state, [failing("source-1", http=402)] + [ok(e.source_id) for e in self.catalog[1:]], later, self.catalog)
        self.assertEqual(still["reinstated"], [])
        self.assertEqual(self.state["sources"]["source-1"]["state"], "retired")
        self.assertEqual(health.due_for_probe(self.state, later), set(), "the next probe is a month after this one")
        again = (date.fromisoformat(later) + timedelta(days=31)).isoformat()
        back = health.record_results(self.state, [ok(e.source_id) for e in self.catalog], again, self.catalog)
        self.assertEqual(back["reinstated"], ["source-1"])
        self.assertEqual(self.state["sources"]["source-1"]["state"], "ok")

    def test_retirement_never_shrinks_the_catalog_below_the_audit_floor(self) -> None:
        catalog = [entry(n) for n in range(1, 7)]
        run_days(self.state, catalog, lambda sid, n: failing(sid, http=402) if sid in {"source-1", "source-2"} else ok(sid), 3)
        self.assertEqual(health.retired_ids(self.state), {"source-1", "source-2"})
        active, skipped = health.filter_catalog(catalog, self.state, day(5), floor=5)
        self.assertEqual(len(active), 5)
        self.assertEqual(len(skipped), 1)
        # a catalog that is already below the floor is not "restored": the audit fails on the floor either way
        small, left_out = health.filter_catalog(catalog, self.state, day(5), floor=50)
        self.assertEqual(len(small), 4)

    def test_state_round_trips_and_a_damaged_file_starts_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "source-health.json"
            run_days(self.state, self.catalog, lambda sid, n: failing(sid, http=402) if sid == "source-1" else ok(sid), 3)
            health.save_state(path, self.state)
            self.assertEqual(health.retired_ids(health.load_state(path)), {"source-1"})
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(health.load_state(path), health.default_state())


class AuditIntegrationTests(unittest.TestCase):
    """The audit skips retired sources, does not count their re-probes, and records the day's results (live path, scripted checks)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = {key: os.environ.get(key) for key in ("DAILY_BRIEFING_ROOT", "BRIEFING_SOURCE_HEALTH")}
        os.environ["DAILY_BRIEFING_ROOT"] = self.tmp.name
        os.environ.pop("BRIEFING_SOURCE_HEALTH", None)
        self.addCleanup(self.restore)
        self.catalog = [entry(n) for n in range(1, 7)]
        self.calls: list[str] = []
        self.original = (audit.check_source, audit.parse_source_catalogs)
        audit.parse_source_catalogs = lambda paths=None: list(self.catalog)

        def scripted(e, *, timeout, retries):
            self.calls.append(e.source_id)
            payload = e.as_dict()
            payload.update(failing(e.source_id, http=402) if e.source_id == "source-1" else ok(e.source_id))
            payload.update({"checked_at": "now", "check_method": "scripted", "attempts": 1})
            return payload

        audit.check_source = scripted
        self.addCleanup(lambda: setattr(audit, "check_source", self.original[0]))
        self.addCleanup(lambda: setattr(audit, "parse_source_catalogs", self.original[1]))

    def restore(self) -> None:
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_a_paywalled_source_is_retired_over_three_days_then_no_longer_fetched_or_counted(self) -> None:
        payloads = [audit.generate_source_audit(day(n), max_workers=1) for n in range(3)]
        self.assertEqual(payloads[0]["summary"]["catalog_count"], 6)
        self.assertEqual(payloads[0]["summary"]["unavailable_count"], 1)
        self.assertEqual(payloads[2]["source_health"]["newly_retired"], ["source-1"])
        self.calls.clear()
        fourth = audit.generate_source_audit(day(3), max_workers=1)
        self.assertNotIn("source-1", self.calls)
        self.assertEqual(fourth["summary"]["catalog_count"], 5)
        self.assertEqual(fourth["summary"]["unavailable_count"], 0)
        self.assertEqual(fourth["source_health"]["retired_total"], 1)
        state = json.loads(audit.source_health_path().read_text(encoding="utf-8"))
        self.assertEqual(state["sources"]["source-1"]["retired"]["kind"], "paywall")

    def test_a_reprobe_of_a_retired_source_updates_the_memory_but_not_the_audit(self) -> None:
        for n in range(3):
            audit.generate_source_audit(day(n), max_workers=1)
        self.calls.clear()
        probe_day = day(2 + health.REPROBE_DAYS)
        payload = audit.generate_source_audit(probe_day, max_workers=1)
        self.assertIn("source-1", self.calls)
        self.assertNotIn("source-1", [s["source_id"] for s in payload["sources"]])
        self.assertEqual(payload["summary"]["catalog_count"], 5)
        state = json.loads(audit.source_health_path().read_text(encoding="utf-8"))
        self.assertEqual(state["sources"]["source-1"]["retired"]["last_probe"], probe_day)

    def test_the_memory_can_be_switched_off(self) -> None:
        os.environ["BRIEFING_SOURCE_HEALTH"] = "0"
        payload = audit.generate_source_audit(day(0), max_workers=1)
        self.assertNotIn("source_health", payload)
        self.assertFalse(audit.source_health_path().exists())


if __name__ == "__main__":
    unittest.main()
