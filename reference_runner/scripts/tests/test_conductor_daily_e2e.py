"""Daily edition through the conductor: seven sections, per-section quotas, closing analysis, focus-company decision."""
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


def run_daily(scenario: str, root: Path) -> dict:
    env = {**os.environ, "E2E_WORDS": "1600"}
    proc = subprocess.run([sys.executable, str(HERE / "e2e_harness_daily.py"), scenario, str(root)], capture_output=True, text=True,
                          env=env, timeout=900, cwd=str(HERE))
    for line in proc.stdout.splitlines():
        if line.startswith("E2E_RESULT "):
            return json.loads(line[len("E2E_RESULT "):])
    raise AssertionError(f"harness produced no result (exit {proc.returncode}):\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")


class DailyConductorTests(unittest.TestCase):
    def fresh(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_happy_path_writes_every_section_the_closing_and_delivers(self) -> None:
        result = run_daily("happy", self.fresh())
        self.assertEqual(result["exit"], 0, result["events"])
        # 5 + 8 + 3 + 3 + 1 + 1 stories at their section targets, plus the closing analysis.
        self.assertEqual(result["calls"]["article"], 21)
        self.assertEqual(result["calls"]["closing"], 1)
        self.assertNotIn("digest", result["calls"])
        self.assertEqual(result["short_sections"], {})
        text = result["assembled_text"]
        for heading in ("# PART I - GENERAL NEWS", "# PART II - AI NEWS", "# PART III - NON-AI TECH NEWS", "# PART IV - JAPAN NEWS",
                        "# PART V - HUMAN INTEREST", "# PART VI - UPDATES", "# CLOSING ANALYSIS"):
            self.assertIn(heading, text)
        self.assertIn("### Top 3 Structural Forces", text)
        self.assertIn('Cross-reference: Part', text)
        self.assertNotIn("<!-- story:", text)
        self.assertEqual(result["validation"]["blocking_errors"], [])
        self.assertTrue(any("no-update" in w for w in result["validation"]["warnings"]), "the recorded focus-company decision is surfaced")
        self.assertTrue(result["delivered"] and result["pdf_exists"])
        args = result["deliver_args"]
        self.assertEqual(args[args.index("--edition") + 1], "daily")
        self.assertNotIn("--recipient", args, "no recipients configured: the delivery config's default recipient applies")
        self.assertEqual(result["accounting_errors"], [])
        self.assertEqual(result["usage_calls"], sum(result["calls"].values()))

    def test_a_section_below_its_minimum_ships_a_visible_gap_not_padding(self) -> None:
        result = run_daily("short_section", self.fresh())
        self.assertEqual(result["exit"], 10, result["events"])  # delivered, but flagged as degraded
        self.assertEqual(result["short_sections"], {"part4": 1})
        self.assertIn("## Quality Failure: only 1 of the 2 minimum stories met the evidence bar for PART IV - JAPAN NEWS", result["assembled_text"])
        self.assertTrue(any("PART IV" in n for n in result["notes"]), result["notes"])
        self.assertTrue(result["delivered"])

    def test_a_closing_the_model_cannot_write_becomes_a_visible_quality_failure(self) -> None:
        result = run_daily("closing_garbage", self.fresh())
        self.assertEqual(result["exit"], 0, result["events"])
        self.assertTrue(result["closing_fallback"])
        self.assertEqual(result["calls"]["closing"], 2, "one retry with the findings before falling back")
        self.assertIn("Quality Failure: the closing analysis could not be produced", result["assembled_text"])
        self.assertTrue(result["delivered"])

    def test_without_a_focus_company_decision_validation_blocks_delivery(self) -> None:
        result = run_daily("no_decision", self.fresh())
        self.assertFalse(result["delivered"])
        self.assertEqual(result["exit"], 20)
        self.assertTrue(any("focus-company" in e for e in (result["validation"] or {}).get("blocking_errors", [])))


if __name__ == "__main__":
    unittest.main()
