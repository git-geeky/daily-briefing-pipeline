"""Unit tests for the reliability fixes that need no full conductor run: lock, time zones, attribution, cost estimates, parsing."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from _shared_imports import REPO_ROOT  # noqa: F401

import conductor_core as cc  # noqa: E402
import llm_backend_core as backend  # noqa: E402
import slate_core  # noqa: E402


class FileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "run.lock"

    def test_a_live_holder_blocks_a_second_run_and_release_frees_it(self) -> None:
        first = cc.FileLock(self.path, 240)
        self.assertTrue(first.acquire())
        second = cc.FileLock(self.path, 240)
        self.assertFalse(second.acquire())
        self.assertEqual(second.holder()["pid"], os.getpid())
        first.release()
        self.assertTrue(second.acquire())

    def test_a_lock_left_by_a_dead_process_is_taken_over_immediately(self) -> None:
        self.path.write_text(json.dumps({"pid": 999999, "started_epoch": time.time()}), encoding="utf-8")
        lock = cc.FileLock(self.path, 240, alive=lambda pid: False)
        self.assertTrue(lock.acquire(), "a killed run must not block the next repetition for hours")
        self.assertEqual(lock.reclaimed["pid"], 999999)
        self.assertEqual(json.loads(self.path.read_text())["pid"], os.getpid())

    def test_a_hung_but_alive_holder_is_taken_over_only_after_the_stale_age(self) -> None:
        self.path.write_text(json.dumps({"pid": 4242, "started_epoch": time.time() - 100 * 60}), encoding="utf-8")
        self.assertFalse(cc.FileLock(self.path, 240, alive=lambda pid: True).acquire())
        self.path.write_text(json.dumps({"pid": 4242, "started_epoch": time.time() - 300 * 60}), encoding="utf-8")
        self.assertTrue(cc.FileLock(self.path, 240, alive=lambda pid: True).acquire())

    def test_a_half_written_lock_is_given_a_moment_then_reclaimed(self) -> None:
        self.path.write_text("", encoding="utf-8")
        self.assertFalse(cc.FileLock(self.path, 240).acquire(), "another process may be writing it right now")
        old = time.time() - 60
        os.utime(self.path, (old, old))
        self.assertTrue(cc.FileLock(self.path, 240).acquire())

    def test_pid_alive_knows_this_process_and_not_an_impossible_one(self) -> None:
        self.assertTrue(cc.pid_alive(os.getpid()))
        self.assertFalse(cc.pid_alive(0))
        self.assertFalse(cc.pid_alive(2**22 + 12345))


class TimezoneTests(unittest.TestCase):
    def test_missing_tz_database_falls_back_to_the_configured_fixed_offset(self) -> None:
        saved = cc.ZoneInfo

        def broken(name):
            raise KeyError(name)

        cc.ZoneInfo = broken
        try:
            tokyo = cc.resolve_timezone("Asia/Tokyo")
            self.assertEqual(cc.datetime(2026, 10, 26, 0, 0, tzinfo=cc.timezone.utc).astimezone(tokyo).hour, 9)
            self.assertEqual(cc.resolve_timezone("Nowhere/Land").utcoffset(None).total_seconds(), 0)
        finally:
            cc.ZoneInfo = saved

    def test_real_zone_is_used_when_the_database_exists(self) -> None:
        if cc.ZoneInfo is None:
            self.skipTest("zoneinfo unavailable")
        try:
            cc.ZoneInfo("Asia/Tokyo")
        except Exception:  # noqa: BLE001
            self.skipTest("no tz database on this machine")
        self.assertEqual(str(cc.resolve_timezone("Asia/Tokyo")), "Asia/Tokyo")


class OffenderAttributionTests(unittest.TestCase):
    def conductor(self, title: str) -> "cc.Conductor":
        c = cc.Conductor.__new__(cc.Conductor)
        c.article_section = "part1"
        c._title_of = lambda article: title  # type: ignore[method-assign]
        return c

    def test_research_lint_errors_are_attributed_to_their_article(self) -> None:
        c = self.conductor("Big Story")
        errors = [
            "Research lint source-tension-coverage: part1/Big Story: Hard-news article has fewer than two Counterpoint Matrix rows.",
            "part1/Big Story: missing Watch Next 48h block",
            "part1/Other: missing Watch Next 48h block",
            "part2/Big Story: unrelated section, same title",
        ]
        mine = c._errors_for({"story": {"section": "part1"}}, errors)
        self.assertEqual(len(mine), 2)
        self.assertTrue(all("part1/Big Story:" in e for e in mine))

    def test_a_title_that_is_a_suffix_of_another_does_not_match_it(self) -> None:
        c = self.conductor("Foo")
        self.assertEqual(c._errors_for({"story": {"section": "part1"}}, ["part1/Big Foo: x"]), [])


class CostEstimateTests(unittest.TestCase):
    def test_a_killed_call_is_priced_from_the_assistant_messages_it_streamed(self) -> None:
        events = [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"},
            {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "partial"}],
                                              "usage": {"input_tokens": 1000, "output_tokens": 2000, "cache_read_input_tokens": 50000}}},
            {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "partial more"}],
                                              "usage": {"input_tokens": 1000, "output_tokens": 2500, "cache_read_input_tokens": 50000}}},
            {"type": "assistant", "message": {"id": "m2", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "x"}],
                                              "usage": {"input_tokens": 500, "output_tokens": 500}}},
        ]
        result = backend.parse_stream([json.dumps(e) + "\n" for e in events])
        self.assertTrue(result.cost_estimated)
        self.assertEqual(result.output_tokens, 3000, "a message that streams repeatedly counts once, at its latest usage")
        self.assertEqual(result.input_tokens, 1500)
        expected = (1500 * 3 + 50000 * 0.3 + 3000 * 15) / 1_000_000
        self.assertAlmostEqual(result.cost_usd, expected, places=4)

    def test_a_completed_call_keeps_the_cli_reported_cost_and_is_not_marked_estimated(self) -> None:
        events = [
            {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "ok"}],
                                              "usage": {"input_tokens": 10, "output_tokens": 10}}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "ok", "total_cost_usd": 0.5,
             "usage": {"input_tokens": 10, "output_tokens": 10}},
        ]
        result = backend.parse_stream([json.dumps(e) + "\n" for e in events])
        self.assertFalse(result.cost_estimated)
        self.assertEqual(result.cost_usd, 0.5)

    def test_opus_is_priced_above_sonnet_and_haiku_below(self) -> None:
        price = lambda model: backend.estimate_cost_usd(model, input_tokens=1000, output_tokens=1000)  # noqa: E731
        self.assertGreater(price("claude-opus-5-5"), price("claude-sonnet-5-5"))
        self.assertGreater(price("claude-sonnet-5-5"), price("claude-haiku-5-5"))


class ExtractJsonTests(unittest.TestCase):
    def test_a_stray_array_in_the_preamble_is_not_mistaken_for_the_answer(self) -> None:
        text = 'I weighed options [1] and [2] and {} before deciding.\n```json\n{"stories": [{"title": "A"}]}\n```'
        self.assertEqual(slate_core.extract_json(text), {"stories": [{"title": "A"}]})

    def test_a_list_of_objects_beats_an_earlier_list_of_numbers(self) -> None:
        self.assertEqual(slate_core.extract_json('scores [3, 4] then [{"id": 1}]'), [{"id": 1}])

    def test_with_nothing_answer_shaped_the_first_value_still_wins(self) -> None:
        self.assertEqual(slate_core.extract_json("only [1, 2]"), [1, 2])

    def test_nested_values_are_not_separate_candidates(self) -> None:
        self.assertEqual(slate_core.extract_json('[{"a": [1, 2]}, {"b": {"c": 1}}]'), [{"a": [1, 2]}, {"b": {"c": 1}}])


if __name__ == "__main__":
    unittest.main()
