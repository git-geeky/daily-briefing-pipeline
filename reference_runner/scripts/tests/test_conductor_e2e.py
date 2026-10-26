"""End-to-end conductor runs against a scripted fake model (real assemble/validate/PDF/QA scripts, no network)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _shared_imports import REPO_ROOT  # noqa: F401

HERE = Path(__file__).resolve().parent


def run_harness(scenario: str, root: Path, *extra: str, env: dict[str, str] | None = None) -> dict:
    full_env = {**os.environ, "E2E_WORDS": "1600", **(env or {})}
    proc = subprocess.run([sys.executable, str(HERE / "e2e_harness.py"), scenario, str(root), *extra], capture_output=True,
                          text=True, env=full_env, timeout=900, cwd=str(HERE))
    for line in proc.stdout.splitlines():
        if line.startswith("E2E_RESULT "):
            return json.loads(line[len("E2E_RESULT "):])
    raise AssertionError(f"harness produced no result (exit {proc.returncode}):\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")


class ConductorE2ETests(unittest.TestCase):
    def fresh(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_happy_path_delivers_a_ten_article_edition_pdf(self) -> None:
        result = run_harness("happy", self.fresh())
        self.assertEqual(result["exit"], 0, result["events"])
        self.assertEqual(len(result["articles_done"]), 10)
        self.assertEqual(result["calls"]["article"], 10)
        self.assertNotIn("repair", result["calls"])
        self.assertTrue(result["pdf_exists"] and result["delivered"])
        # The PDF carries the edition's identity, not the daily briefing's.
        self.assertIn("THE ANIMAL SCIENCE BRIEFING", result["pdf_page1"].upper())
        self.assertNotIn("DAILY INTELLIGENCE", result["pdf_page1"].upper())
        self.assertGreaterEqual(result["pdf_images"], 10)
        args = result["deliver_args"]
        self.assertEqual(args[args.index("--edition") + 1], "animal")
        self.assertEqual(args[args.index("--recipient") + 1], "a@example.com,b@example.com")
        text = result["assembled_text"]
        self.assertIn("Photo: Jane Doe, CC BY-SA 4.0, via Wikimedia Commons", text)
        self.assertNotIn("<!-- story:", text)
        self.assertEqual(text.count("Image Quality Failure"), 0)
        # Token/cost accounting reached the run report for every model call, even with parallel writers.
        self.assertEqual(result["accounting_errors"], [])
        self.assertEqual(result["report_run_status"], "completed")
        self.assertEqual(result["usage_calls"], sum(result["calls"].values()))
        self.assertAlmostEqual(result["usage_cost"], result["spent"], places=2)
        for stage in ("audit", "ingest", "slate", "photos", "articles", "header", "validate", "pdf", "visual_qa", "deliver"):
            self.assertEqual(result["stages"][stage], "done", stage)

    def test_a_skipped_story_is_replaced_from_the_backups(self) -> None:
        result = run_harness("skip_story", self.fresh())
        self.assertEqual(result["exit"], 0, result["events"])
        self.assertNotIn("fixture-story-3", result["articles_done"])
        self.assertEqual(len(result["articles_done"]), 10)
        self.assertEqual(result["calls"]["article"], 11)

    def test_messy_writer_output_is_normalised_without_a_model_call(self) -> None:
        result = run_harness("messy_writer", self.fresh())
        self.assertEqual(result["exit"], 0, result["events"])
        self.assertNotIn("repair", result["calls"])
        self.assertNotIn("```", result["assembled_text"])
        self.assertNotIn("Here is the article", result["assembled_text"])

    def test_a_missing_required_block_triggers_one_targeted_repair(self) -> None:
        result = run_harness("missing_block", self.fresh())
        self.assertEqual(result["exit"], 0, result["events"])
        self.assertEqual(result["calls"]["repair"], 1)
        self.assertIn("Counterpoint Matrix", result["repair_prompts"][0])
        self.assertEqual(len(result["articles_done"]), 10)

    def test_an_invented_quotation_is_caught_and_repaired(self) -> None:
        result = run_harness("invented_quote", self.fresh())
        self.assertEqual(result["exit"], 0, result["events"])
        self.assertEqual(result["calls"]["repair"], 1)
        self.assertIn("quotation not found", result["repair_prompts"][0])
        self.assertNotIn("this exact sentence was never published", result["assembled_text"])

    def test_an_unusable_header_falls_back_to_the_deterministic_one(self) -> None:
        result = run_harness("header_garbage", self.fresh())
        self.assertEqual(result["exit"], 0, result["events"])
        self.assertEqual(result["header_source"], "fallback")
        self.assertIn("## Executive Summary", result["assembled_text"])
        self.assertEqual(result["stages"]["validate"], "done")

    def test_past_the_deadline_optional_work_is_skipped_but_the_briefing_still_ships(self) -> None:
        result = run_harness("past_deadline", self.fresh())
        # Already past the deadline when the run starts: it drafts only until the minimum (6) is finished, then ships
        # a smaller briefing (exit 10) instead of drifting on for hours.
        self.assertEqual(result["exit"], 10, result["events"])
        self.assertTrue(6 <= len(result["articles_done"]) <= 8, result["articles_done"])
        for stage in ("header", "digest", "review"):
            self.assertNotIn(stage, result["calls"])
        self.assertEqual(result["header_source"], "fallback")
        self.assertTrue(result["delivered"])

    def test_org_limit_stops_immediately_and_raises_an_alert(self) -> None:
        result = run_harness("org_limit", self.fresh())
        self.assertEqual(result["exit"], 20)
        self.assertEqual(list(result["calls"]), ["probe"])
        self.assertFalse(result["delivered"])
        self.assertEqual(len(result["alerts"]), 1)
        self.assertIn("org-limit", result["alerts"][0])

    def test_limit_midway_defers_then_resumes_without_redoing_finished_articles(self) -> None:
        root = self.fresh()
        first = run_harness("limit_midway", root, env={"E2E_LIMIT_AFTER": "4"})
        self.assertEqual(first["exit"], 75, first["events"])
        self.assertFalse(first["delivered"])
        self.assertEqual(len(first["articles_done"]), 4)
        second = run_harness("limit_midway", root, "--second-pass", env={"E2E_LIMIT_AFTER": "4"})
        self.assertEqual(second["exit"], 0, second["events"])
        self.assertEqual(second["calls"]["article"], 6, "only the six unfinished stories may be written on resume")
        self.assertEqual(len(second["articles_done"]), 10)
        self.assertTrue(second["delivered"])

    def test_watchdog_finish_mode_delivers_what_an_interrupted_run_left_behind(self) -> None:
        root = self.fresh()
        first = run_harness("interrupted", root)
        self.assertEqual(first["exit"], 75, first["events"])
        self.assertFalse(first["delivered"])
        self.assertEqual(len(first["articles_done"]), 10)
        second = run_harness("interrupted", root, "--second-pass", "--finish")
        self.assertEqual(second["exit"], 10, second["events"])
        self.assertTrue(second["delivered"] and second["pdf_exists"])
        self.assertNotIn("article", second["calls"], "finish mode must not draft anything")
        self.assertNotIn("slate", second["calls"])

    def test_watchdog_with_too_little_to_ship_raises_an_alert(self) -> None:
        root = self.fresh()
        first = run_harness("limit_midway", root, env={"E2E_LIMIT_AFTER": "3"})
        self.assertEqual(first["exit"], 75)
        second = run_harness("limit_midway", root, "--second-pass", "--finish", env={"E2E_LIMIT_AFTER": "3"})
        self.assertEqual(second["exit"], 20, second["events"])
        self.assertFalse(second["delivered"])
        self.assertEqual(len(second["alerts"]), 1)
        self.assertIn("fail-finish", second["alerts"][0])

    def test_limit_with_enough_finished_articles_ships_a_degraded_briefing_instead_of_waiting_all_day(self) -> None:
        result = run_harness("limit_partial", self.fresh(), env={"E2E_LIMIT_AFTER": "7"})
        self.assertEqual(result["exit"], 10, result["events"])
        self.assertTrue(result["delivered"])
        self.assertEqual(len(result["articles_done"]), 7)
        self.assertEqual(result["report_run_status"], "degraded", "a smaller briefing must not be recorded as a plain success")
        self.assertTrue(any("7 of 10" in n for n in result["notes"]), result["notes"])


    def test_a_crash_before_any_article_is_alerted_not_swallowed(self) -> None:
        result = run_harness("crash_early", self.fresh())
        self.assertEqual(result["exit"], 20, result["events"])
        self.assertEqual(result["run_status"], "crashed")
        self.assertFalse(result["delivered"])
        self.assertTrue(any("fail-crash" in a for a in result["alerts"]), result["alerts"])
        self.assertFalse(result["lock_left_behind"], "a crashed run must release its lock")

    def test_a_crash_in_the_last_mile_is_alerted_after_a_degraded_attempt(self) -> None:
        result = run_harness("crash_late", self.fresh())
        self.assertEqual(result["exit"], 20, result["events"])
        self.assertEqual(len(result["articles_done"]), 10)
        self.assertTrue(any("fail-finish" in a for a in result["alerts"]), result["alerts"])
        self.assertTrue(any("degraded" in e for e in result["events"]), result["events"])

    def test_a_second_run_while_the_first_is_alive_backs_off_with_the_busy_exit_code(self) -> None:
        result = run_harness("busy", self.fresh())
        self.assertEqual(result["exit"], 76, result["events"])
        self.assertEqual(result["calls"], {})
        self.assertTrue(result["lock_left_behind"], "the live run's lock must not be removed by the one that backed off")

    def test_a_limit_during_repair_keeps_the_paid_draft_and_resumes_into_the_repair(self) -> None:
        root = self.fresh()
        first = run_harness("limit_in_repair", root)
        self.assertEqual(first["exit"], 75, first["events"])
        second = run_harness("limit_in_repair", root, "--second-pass")
        self.assertEqual(second["exit"], 0, second["events"])
        self.assertNotIn("fixture-story-2", second["article_story_ids"], "the first draft was paid for; it must not be bought again")
        self.assertTrue(any("resuming from the saved draft" in e for e in second["events"]), second["events"])
        self.assertEqual(second["calls"]["repair"], 1)
        self.assertIn("fixture-story-2", second["articles_done"])

    def test_a_story_that_failed_after_full_repair_is_not_bought_again_on_resume(self) -> None:
        root = self.fresh()
        first = run_harness("terminal_failed", root)
        self.assertEqual(first["exit"], 75, first["events"])
        self.assertNotIn("fixture-story-2", first["articles_done"])
        second = run_harness("terminal_failed", root, "--second-pass")
        self.assertEqual(second["exit"], 0, second["events"])
        self.assertNotIn("fixture-story-2", second["article_story_ids"])
        self.assertNotIn("repair", second["calls"])
        self.assertEqual(len(second["articles_done"]), 10)

    def test_past_the_deadline_no_new_articles_start_once_enough_are_finished(self) -> None:
        result = run_harness("deadline_midway", self.fresh())
        self.assertEqual(result["exit"], 10, result["events"])
        self.assertTrue(result["delivered"])
        self.assertLessEqual(result["calls"]["article"], 9, "drafting must stop at the deadline, not run the full queue")
        self.assertGreaterEqual(len(result["articles_done"]), 6)


class ConductorCliTests(unittest.TestCase):
    def cli(self, *args: str, root: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "DAILY_BRIEFING_ROOT": str(root), "DAILY_BRIEFING_SHARED_ROOT": str(REPO_ROOT)}
        return subprocess.run([sys.executable, str(REPO_ROOT / "conductor_core.py"), *args, "--scripts-dir", str(HERE.parent)],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_diagnose_prints_state_without_calling_any_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = self.cli("--edition", "animal", "--date", "2026-10-26", "--diagnose", root=Path(tmp))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Run state for 2026-10-26 (animal)", done.stdout)

    def test_unknown_edition_is_a_usage_error_with_the_available_ones_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = self.cli("--edition", "nonexistent", "--date", "2026-10-26", root=Path(tmp))
        self.assertEqual(done.returncode, 2)
        self.assertIn("animal", done.stderr)

    def test_disabled_pipeline_alerts_instead_of_exiting_silently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"BRIEFING_DISABLED": "1"}
            full = {**os.environ, "DAILY_BRIEFING_ROOT": tmp, "DAILY_BRIEFING_SHARED_ROOT": str(REPO_ROOT), **env}
            done = subprocess.run([sys.executable, str(REPO_ROOT / "conductor_core.py"), "--edition", "animal", "--date", "2026-10-26",
                                   "--scripts-dir", str(HERE.parent)], capture_output=True, text=True, env=full, timeout=120)
            alerts = list(Path(tmp).rglob("ALERT-*disabled*.txt"))
        self.assertEqual(done.returncode, 30)
        self.assertEqual(len(alerts), 1, "the legacy runner exited 0 silently when gated off; the conductor must leave a trace")


if __name__ == "__main__":
    unittest.main()
