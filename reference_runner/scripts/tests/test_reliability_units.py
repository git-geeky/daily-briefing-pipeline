"""Unit tests for the reliability fixes that need no full conductor run: lock, time zones, attribution, cost estimates, parsing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import types
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



class RenderPromptTests(unittest.TestCase):
    def test_placeholder_lookalikes_inside_fetched_text_are_left_alone(self) -> None:
        out = cc.render_prompt("A {{DOSSIER}} B {{TITLE}}", {"DOSSIER": "page says {{TITLE}} and {{UNKNOWN}}", "TITLE": "real"})
        self.assertEqual(out, "A page says {{TITLE}} and {{UNKNOWN}} B real")

    def test_a_missing_value_for_a_template_placeholder_is_still_an_error(self) -> None:
        with self.assertRaises(ValueError) as caught:
            cc.render_prompt("{{A}} {{B}}", {"A": "x"})
        self.assertIn("B", str(caught.exception))


RUN_REPORT_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import run_reporting as rr
D = "2026-10-26"
rr.ensure_run_attempt(D)
rr.update_run_report(D, source_audit={"status": "complete", "all_sources_checked": True},
                     timings={"pdf_build": {"status": "passed"}, "visual_qa": {"status": "passed"}}, validation_status="passed")
rr.set_step_timing(D, "source_audit", status="passed", started_at="2026-10-26T00:00:00", ended_at="2026-10-26T00:01:00")
rr.update_run_report(D, run_status="completed")           # the first pass has finished ...
rr.ensure_run_attempt(D)                                   # ... and a second assemble starts a new attempt
report = rr.load_run_report(D)
report["timings"]["pdf_build"] = {"status": "passed"}
report["timings"]["visual_qa"] = {"status": "passed"}
report["validation_status"] = "passed"
rr.write_run_report(report)
ok_with_audit = rr.stage_prerequisites_satisfied(rr.load_run_report(D), "delivery")
# an incomplete audit blocks, unless the conductor recorded a waiver (which a later attempt must not lose)
rr.update_run_report(D, source_audit={"status": "failed", "systemic_failure": True})
blocked = rr.stage_prerequisites_satisfied(rr.load_run_report(D), "delivery")
rr.update_run_report(D, delivery_audit_waiver="systemic source-check failures", run_status="completed")
rr.ensure_run_attempt(D)
again = rr.load_run_report(D)
again["timings"].update({"pdf_build": {"status": "passed"}, "visual_qa": {"status": "passed"}}); again["validation_status"] = "passed"
rr.write_run_report(again)
waived = rr.stage_prerequisites_satisfied(rr.load_run_report(D), "delivery")
print("RESULT " + json.dumps({"ok": list(ok_with_audit), "blocked": list(blocked), "waived": list(waived)}))
"""


class RunReportAttemptTests(unittest.TestCase):
    """Live-run review finding: a second pass through the last mile reset the report and dropped the source audit, so the Windows
    delivery gate refused every send for the rest of the day."""

    def test_the_audit_and_its_waiver_survive_a_new_attempt_so_the_gate_still_opens(self) -> None:
        scripts = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "DAILY_BRIEFING_ROOT": tmp, "DAILY_BRIEFING_SHARED_ROOT": str(REPO_ROOT), "BRIEFING_EDITION": "daily"}
            proc = subprocess.run([sys.executable, "-c", RUN_REPORT_PROBE, scripts], capture_output=True, text=True, env=env, cwd=scripts, timeout=120)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        self.assertIsNotNone(line, proc.stdout[-1500:] + proc.stderr[-1500:])
        result = json.loads(line[len("RESULT "):])
        self.assertEqual(result["ok"], [True, None], "audit must survive the second attempt")
        self.assertFalse(result["blocked"][0])
        self.assertEqual(result["waived"], [True, None], "a recorded waiver must survive too")


class AlertDedupeTests(unittest.TestCase):
    def conductor(self, sends: list[bool], command=("mail",)):
        c = cc.Conductor.__new__(cc.Conductor)
        c.date = "2026-10-26"
        c.state = types.SimpleNamespace(state={"alerts_sent": []}, save=lambda: None)
        c.log = lambda message: None
        outcomes = iter(sends)
        c.notifier = types.SimpleNamespace(command=list(command) if command else None, send=lambda subject, body, key: next(outcomes))
        return c

    def test_a_failed_email_leg_is_retried_by_the_next_event_and_a_sent_one_is_not_repeated(self) -> None:
        c = self.conductor([False, True, True])
        c._alert_once("fail-deliver-abc", "s", "b")
        self.assertEqual(c.state.state["alerts_sent"], [])
        c._alert_once("fail-deliver-abc", "s", "b")
        self.assertEqual(len(c.state.state["alerts_sent"]), 1)
        c._alert_once("fail-deliver-abc", "s", "b")  # already sent: the third outcome must not be consumed
        self.assertEqual(len(c.state.state["alerts_sent"]), 1)

    def test_without_an_alert_command_the_file_is_the_alert_and_it_is_recorded_once(self) -> None:
        c = self.conductor([False], command=None)
        c._alert_once("fail-x-1", "s", "b")
        c._alert_once("fail-x-1", "s", "b")
        self.assertEqual(len(c.state.state["alerts_sent"]), 1)

    def test_distinct_reasons_are_distinct_alerts(self) -> None:
        c = self.conductor([True, True])
        c._alert_once("fail-finish-aaaaaa", "s", "b")
        c._alert_once("fail-finish-bbbbbb", "s", "b")
        self.assertEqual(len(c.state.state["alerts_sent"]), 2)


class BudgetGuardTests(unittest.TestCase):
    """Review COST-1: caps that do not depend on the CLI's own cost field, and a loud signal when limit telemetry disappears."""

    def conductor(self, **state):
        import threading

        c = cc.Conductor.__new__(cc.Conductor)
        c.settings = {"budget": {"max_cost_usd_per_run": 40.0, "max_output_tokens_per_run": 1_000_000, "stop_launching_above": 0.92,
                                 "defer_if_five_hour_above": 0.8, "defer_if_seven_day_above": 0.9}, "timeouts": {"probe_s": 5},
                      "models": {"classify": {"model": "haiku"}}}
        c._stop = threading.Event()
        c.last_rate_limit = backend.RateLimitInfo()
        c.state = types.SimpleNamespace(state={"alerts_sent": [], "stages": {}, **state}, save=lambda: None, done=lambda name: False,
                                        mark=lambda *a, **k: None)
        c.log = lambda message: None
        return c

    def test_the_output_token_cap_stops_a_run_even_when_cost_reads_zero(self) -> None:
        c = self.conductor(spent_usd=0.0, output_tokens=1_200_000)
        with self.assertRaises(cc.Deferred) as caught:
            c.check_budget()
        self.assertEqual(caught.exception.kind, "budget")
        self.assertTrue(c._stop.is_set())

    def test_below_the_caps_the_run_continues(self) -> None:
        self.conductor(spent_usd=3.0, output_tokens=200_000).check_budget()

    def test_missing_utilisation_telemetry_raises_one_alert_and_the_run_continues(self) -> None:
        c = self.conductor()
        alerts = []
        c._alert_once = lambda key, subject, body: alerts.append(key)
        c.dry_run, c.generated_dir, c.edition = False, Path("."), types.SimpleNamespace(label="Daily")
        c.past_stage_ttl = lambda stage, seconds: True
        c._record = lambda *a, **k: None
        c.backend = types.SimpleNamespace(name="claude", run=lambda request: backend.LLMResult(
            ok=True, text="ok", rate_limit=backend.RateLimitInfo(status="allowed")))
        c.stage_preflight()
        self.assertEqual(alerts, ["telemetry-missing"])

    def test_present_telemetry_raises_no_alert(self) -> None:
        c = self.conductor()
        alerts = []
        c._alert_once = lambda key, subject, body: alerts.append(key)
        c.dry_run, c.generated_dir, c.edition = False, Path("."), types.SimpleNamespace(label="Daily")
        c.past_stage_ttl = lambda stage, seconds: True
        c._record = lambda *a, **k: None
        c.backend = types.SimpleNamespace(name="claude", run=lambda request: backend.LLMResult(
            ok=True, text="ok", rate_limit=backend.RateLimitInfo(status="allowed", five_hour_utilization=0.1, seven_day_utilization=0.1)))
        c.stage_preflight()
        self.assertEqual(alerts, [])


class SynthesisCheckTests(unittest.TestCase):
    """Review T13: the header and the closing analysis may restate only what the day's articles say."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "a.md"
        path.write_text("## Wolves return\n\nThe survey counted 1,240 wolves across 14 sites. The agency said \"the recovery plan has met every "
                        "interim target set for the decade\" at the hearing.\n", encoding="utf-8")
        self.articles = [{"path": path, "story": {"story_id": "a"}}]
        self.c = cc.Conductor.__new__(cc.Conductor)

    def test_a_restatement_passes(self) -> None:
        text = "Wolves are back: 1,240 counted across 14 sites. The agency said \"the recovery plan has met every interim target set for the decade\"."
        self.assertEqual(self.c._synthesis_unsupported(text, self.articles), "")

    def test_an_invented_quotation_is_reported(self) -> None:
        text = 'The agency admitted "the whole recovery programme has been a costly and embarrassing failure from the start" yesterday.'
        self.assertIn("quotation", self.c._synthesis_unsupported(text, self.articles))

    def test_story_and_section_counts_are_not_claims(self) -> None:
        self.assertEqual(self.c._synthesis_unsupported("Across 17 stories in 12 sections, 14 sources and 11 regions agree: 1,240 wolves.", self.articles), "")

    def test_a_short_text_of_figures_found_in_no_article_is_reported(self) -> None:
        text = "Populations rose 83% to 9,400 animals, with 31 new packs across 612 square kilometres."
        self.assertIn("figures found in no article", self.c._synthesis_unsupported(text, self.articles))


class QualityMetricsTests(unittest.TestCase):
    def test_a_daily_record_is_written_once_per_day_and_edition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = cc.Conductor.__new__(cc.Conductor)
            c.date = "2026-10-26"
            c.edition = types.SimpleNamespace(edition_id="animal")
            c.ctx = types.SimpleNamespace(conductor_state_path=lambda date: str(Path(tmp) / "state" / f"{date}.json"))
            c.state = types.SimpleNamespace(state={"spent_usd": 4.567, "output_tokens": 123456, "delivery_notes": ["n"]})
            c.log = lambda message: None
            articles = [{"meta": {"words": 1800, "attempts": 2, "flags": ["x"], "provenance": {
                "numbers_checked": 10, "unmatched_numbers": [{"text": "1"}], "quotes_checked": 3, "unmatched_quotes": [], "summary_only_quotes": 1,
                "translated_quotes": 0}}}, {"meta": {"words": 2200, "attempts": 1, "provenance": {"numbers_checked": 4}}}]
            c._write_quality_metrics(articles)
            c._write_quality_metrics(articles)
            lines = (Path(tmp) / "state" / "quality-history.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual((record["articles"], record["words_mean"], record["words_min"], record["numbers_checked"], record["numbers_unmatched"],
                          record["quotes_summary_only"], record["articles_flagged"], record["attempts_total"]), (2, 2000, 1800, 14, 1, 1, 1, 3))
        self.assertEqual(record["cost_usd"], 4.57)


class UnsafeFetchTests(unittest.TestCase):
    """Review SEC-3/T10: a writer steered to a local address must not have what it read counted as evidence."""

    def conductor(self):
        c = cc.Conductor.__new__(cc.Conductor)
        c.check_network = True
        c.edition = types.SimpleNamespace(label="Animal")
        c.log = lambda message: None
        c.alerts = []
        c._alert_once = lambda key, subject, body: c.alerts.append(key)
        return c

    def test_results_from_a_loopback_fetch_are_discarded_flagged_and_alerted(self) -> None:
        c = self.conductor()
        outcome = cc.ArticleOutcome("story-1", "failed")
        result = backend.LLMResult(ok=True, text="x", tool_corpus=["secret router page: 83% of voles"], fetched_urls=["http://127.0.0.1:8080/admin"])
        self.assertEqual(c._trusted_tool_corpus(result, outcome), [])
        self.assertTrue(outcome.flags and "non-public" in outcome.flags[0])
        self.assertEqual(c.alerts, ["unsafe-fetch"])

    def test_public_addresses_and_unreachable_names_keep_their_evidence(self) -> None:
        c = self.conductor()
        outcome = cc.ArticleOutcome("story-1", "failed")
        result = backend.LLMResult(ok=True, text="x", tool_corpus=["page text"], fetched_urls=["https://no-such-host.invalid/page"])
        self.assertEqual(c._trusted_tool_corpus(result, outcome), ["page text"])
        self.assertEqual((outcome.flags, c.alerts), ([], []))


class FillGateTests(unittest.TestCase):
    """Human Interest fill-ins must be about people and animals: award, funding and course notices and thin stories never get written."""

    def test_notices_and_thin_stories_are_dropped_from_the_fill_section_only(self) -> None:
        import json

        edition = json.loads((Path(cc.__file__).parent / "editions" / "animal.json").read_text(encoding="utf-8"))["conductor"]
        c = cc.Conductor.__new__(cc.Conductor)
        c.settings = {"fill_sections": {"humaninterest": "animals"}, "fill_blocklist": edition["fill_blocklist"]}
        c.plan = lambda: {"animals": {}, "humaninterest": {}}
        story = lambda sid, title, section="humaninterest", material="moderate": {"story_id": sid, "title": title, "angle": "", "section": section, "material": material}
        report = types.SimpleNamespace(dropped=[], kept=[
            story("a", "Wang Ding receives the 2026 Kiessling Prize"), story("b", "Rangers complete a three-week refresher course"),
            story("c", "A ranger's winter feeding rounds for stranded seals"), story("d", "Villagers rebuilt a heron colony", material="thin"),
            story("e", "Rehabilitator raises orphaned owls for a decade"),
            story("f", "Society honours prize-winning study of grants and funding in wolf recovery", section="animals"),
        ])
        c._gate_fill_entries(report)
        self.assertEqual([e["story_id"] for e in report.kept], ["c", "e", "f"], "science stories are never touched by the fill-in gate")
        self.assertEqual({d["entry"] for d in report.dropped}, {"a", "b", "d"})


class SkipMemoryTests(unittest.TestCase):
    """Found in the fifth live run: a story skipped the previous day was proposed again, with nothing new to say about it."""

    def conductor(self, tmp: str, date: str):
        c = cc.Conductor.__new__(cc.Conductor)
        c.date = date
        c.edition = types.SimpleNamespace(edition_id="animal")
        c.paths = types.SimpleNamespace(research_vault_dir=lambda: Path(tmp))
        c.log = lambda message: None
        return c

    def test_a_skip_is_remembered_for_ten_days_but_not_on_the_day_it_happened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = self.conductor(tmp, "2026-10-26")
            first._remember_skip("deep-sea-siphonophores", "Two new deep-sea siphonophore species described", "SKIP: sources do not support the angle")
            self.assertEqual(first.recent_skip_titles(), [], "the same day's skips are not 'prior' titles")
            next_day = self.conductor(tmp, "2026-10-27")
            self.assertEqual(next_day.recent_skip_titles(), ["Two new deep-sea siphonophore species described"])
            later = self.conductor(tmp, "2026-11-08")
            self.assertEqual(later.recent_skip_titles(), [], "after ten days the story may come back")

    def test_remembering_the_same_story_twice_keeps_one_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = self.conductor(tmp, "2026-10-26")
            c._remember_skip("a", "Story A title", "reason one")
            c._remember_skip("a", "Story A title", "reason two")
            records = json.loads((Path(tmp) / "recent-skips-animal.json").read_text(encoding="utf-8"))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["reason"], "reason two")


class ReserveSlackTests(unittest.TestCase):
    def conductor(self, kept: int):
        c = cc.Conductor.__new__(cc.Conductor)
        c.settings = {"slate_reserves": 8, "slate_reserve_slack": 2, "fill_sections": {}, "article_sections": ["animals"],
                      "article_section": "animals"}
        c.edition = types.SimpleNamespace(section_titles={"animals": "A"}, rule=lambda key: {"target_count": 10, "min_count": 3, "max_count": 12})
        c.article_section = "animals"
        c.calls = []
        c.past_deadline = lambda: False
        c.log = lambda message: None
        c.call = lambda *a, **k: c.calls.append("slate") or backend.LLMResult(ok=False)
        c.prompt = lambda *a, **k: ""
        report = types.SimpleNamespace(kept=[{"story_id": f"s{i}", "title": f"T{i}", "section": "animals"} for i in range(kept)], dropped=[])
        return c, report

    def test_one_or_two_reserves_short_does_not_buy_a_second_slate_call(self) -> None:
        for kept in (18, 17, 16):
            c, report = self.conductor(kept)
            c._top_up_reserves(report, {"SPEC": ""}, [], lambda text: [], c.plan(), False)
            self.assertEqual(c.calls, [], f"{kept} of 18 wanted")

    def test_three_short_asks_once(self) -> None:
        c, report = self.conductor(15)
        c._top_up_reserves(report, {"SPEC": ""}, [], lambda text: [], c.plan(), False)
        self.assertEqual(c.calls, ["slate"])


class LockRebootTests(unittest.TestCase):
    def test_a_lock_written_before_a_reboot_is_dead_even_if_its_pid_now_belongs_to_another_process(self) -> None:
        boot = cc.boot_epoch()
        if boot is None:
            self.skipTest("boot time unknown on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            path.write_text(json.dumps({"pid": os.getpid(), "started_epoch": time.time() - 60, "boot_epoch": boot - 7200}), encoding="utf-8")
            lock = cc.FileLock(path, 240, alive=lambda pid: True)  # the pid is "alive" (reused by something else)
            self.assertTrue(lock.acquire())
            self.assertIsNotNone(lock.reclaimed)

    def test_a_lock_from_this_boot_with_a_live_pid_still_blocks(self) -> None:
        boot = cc.boot_epoch()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            path.write_text(json.dumps({"pid": os.getpid(), "started_epoch": time.time() - 60, "boot_epoch": boot}), encoding="utf-8")
            self.assertFalse(cc.FileLock(path, 240, alive=lambda pid: True).acquire())


if __name__ == "__main__":
    unittest.main()


class VerificationNoteTests(unittest.TestCase):
    """Found in the fifth live run: the editor's note named ten of twenty daily stories by slug."""

    def note(self, flagged: list[dict]) -> list[str]:
        c = cc.Conductor.__new__(cc.Conductor)
        articles = [{"story": {"story_id": f"s{i}", "title": f"Story {i}"}, "meta": {"flags": ["x"] if item is not None else [], "provenance": item or {}}}
                    for i, item in enumerate(flagged)]
        summaries = [{"title": f"Title of story {i}"} for i in range(len(flagged))]
        return c.verification_notes(articles, summaries)

    def test_nothing_flagged_says_nothing(self) -> None:
        self.assertEqual(self.note([None, None]), [])

    def test_serious_cases_are_named_by_title_and_the_rest_are_only_counted(self) -> None:
        notes = self.note([
            {"unmatched_numbers": [{"text": "1"}, {"text": "2"}, {"text": "3"}]},     # three figures
            {"unmatched_numbers": [{"text": "1"}]},                                  # a stray figure
            {"unmatched_quotes": ["a quotation"]},                                   # a quotation
            {"dois": {"10.1/x": {"status": "not_found"}}},                           # a DOI that does not resolve
            {"summary_only_quotes": 1},                                              # a quotation resting on a page summary
            None,
        ])
        self.assertEqual(len(notes), 2)
        self.assertIn("Title of story 0; Title of story 2; Title of story 3", notes[0])
        self.assertNotIn("s0", notes[0])
        self.assertNotIn("Title of story 1", notes[0])
        self.assertEqual(notes[1], "2 more stories rest partly on summaries of pages or carry a stray unverified figure")

    def test_a_single_mild_case_reads_in_the_singular(self) -> None:
        self.assertEqual(self.note([{"unmatched_numbers": [{"text": "1"}]}]),
                         ["1 more story rests partly on summaries of pages or carries a stray unverified figure"])
