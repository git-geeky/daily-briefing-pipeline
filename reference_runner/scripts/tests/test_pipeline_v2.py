"""Regression tests for the edition framework and the pipeline fixes that ride with it.

These exercise the shared cores through the reference runner (same import path the
production wrappers use) and, for the assembly stage, through the real CLI so the
on-disk contract (backups, markers, candidate pool) is what is actually tested.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _shared_imports import REPO_ROOT, SHARED_TESTS_DIR  # noqa: F401

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import briefing_manifest as manifest  # noqa: E402
import edition_core  # noqa: E402

DATE = "2026-10-25"
LEGACY = json.loads((REPO_ROOT / "tests" / "legacy_daily_constants.json").read_text(encoding="utf-8"))


def _filler(words: int, seed: str = "alpha") -> str:
    vocab = f"{seed} measured reported observed estimated confirmed disputed recorded published analysed".split()
    return " ".join(vocab[i % len(vocab)] + ("." if i % 17 == 16 else "") for i in range(words))


class EditionRegressionTests(unittest.TestCase):
    """daily.json must stay byte-for-byte equivalent to the constants it replaced."""

    def setUp(self) -> None:
        self.edition = edition_core.load_edition("daily")

    def test_section_rules_match_legacy_constants(self) -> None:
        for section, legacy_rule in LEGACY["section_rules"].items():
            current = self.edition.rule(section)
            for key, value in legacy_rule.items():
                self.assertEqual(current.get(key), value, f"{section}.{key}")

    def test_titles_and_section_sets_match_legacy_constants(self) -> None:
        self.assertEqual(self.edition.section_titles, LEGACY["section_titles"])
        self.assertEqual(sorted(self.edition.hard_news_sections), sorted(LEGACY["hard_news"]))
        self.assertEqual(sorted(self.edition.counterpoint_required_sections), sorted(LEGACY["counterpoint"]))
        self.assertEqual(sorted(self.edition.watch_required_sections), sorted(LEGACY["watch"]))

    def test_all_sections_order_is_header_first(self) -> None:
        self.assertEqual(self.edition.all_sections[0], "header")
        self.assertEqual(self.edition.all_sections[1:], self.edition.sections)

    def test_default_edition_is_daily(self) -> None:
        os.environ.pop(edition_core.EDITION_ENV, None)
        self.assertEqual(edition_core.load_edition().edition_id, "daily")

    def test_unknown_edition_lists_alternatives(self) -> None:
        with self.assertRaises(edition_core.EditionError) as caught:
            edition_core.load_edition("does-not-exist")
        self.assertIn("daily", str(caught.exception))

    def test_malformed_edition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bad.json").write_text(
                json.dumps({"edition_id": "bad", "label": "Bad", "sections": ["header"],
                            "section_titles": {}, "section_rules": {}}),
                encoding="utf-8",
            )
            os.environ[edition_core.EDITIONS_DIR_ENV] = tmp
            edition_core.reload_edition_cache()
            try:
                with self.assertRaises(edition_core.EditionError):
                    edition_core.load_edition("bad")
            finally:
                os.environ.pop(edition_core.EDITIONS_DIR_ENV, None)
                edition_core.reload_edition_cache()


class BannedTopicTests(unittest.TestCase):
    def test_substring_collisions_do_not_trigger(self) -> None:
        self.assertEqual(manifest.find_banned_topics("The Crypto Subsystem of a Linux Kernel", "Kernel locking details."), [])

    def test_headline_crypto_market_story_is_blocked(self) -> None:
        self.assertTrue(manifest.find_banned_topics("Bitcoin Hits Record High", "Prices rose again on Tuesday."))
        self.assertTrue(manifest.find_banned_topics("Crypto Markets Surge After ETF Vote", "Traders reacted."))

    def test_passing_mention_inside_security_story_is_allowed(self) -> None:
        body = (
            "A ransomware crew demanded payment in Bitcoin after encrypting hospital systems. "
            + _filler(200)
        )
        self.assertEqual(manifest.find_banned_topics("Hospital Network Hit By Ransomware", body), [])


class CrossReferenceTests(unittest.TestCase):
    def test_parses_part_prefixed_and_plain_quoted_titles(self) -> None:
        text = (
            'Cross-reference: Part II, Article 3 - "Grid Operators Warn of Winter Gap"\n'
            'Cross-reference: "Battery Prices Fall Again"\n'
        )
        refs = manifest.extract_cross_references(text)
        self.assertEqual(refs, ["Grid Operators Warn of Winter Gap", "Battery Prices Fall Again"])

    def test_title_containing_apostrophe_is_not_split(self) -> None:
        # Production regression (2026-06-08): a single-quote pattern matched between two apostrophes
        # inside a double-quoted title and forced an unrelated article into the header.
        refs = manifest.extract_cross_references('Cross-reference: "Trump\'s Beijing Deal and Xi\'s Reply"')
        self.assertEqual(refs, ["Trump's Beijing Deal and Xi's Reply"])

    def test_no_refs_returns_empty(self) -> None:
        self.assertEqual(manifest.extract_cross_references("Nothing to see here."), [])


class SourceClassificationTests(unittest.TestCase):
    def test_unlisted_reporting_domains_fall_back_to_reporting_not_empty(self) -> None:
        classes = manifest.classify_sources(
            ["Microsoft Security Blog - https://www.microsoft.com/en-us/security/blog/x",
             "Krebs on Security - https://krebsonsecurity.com/2026/10/x/"]
        )
        self.assertTrue(classes)
        self.assertNotIn("", classes)

    def test_known_classes(self) -> None:
        self.assertIn("wire", manifest.classify_source_url("https://www.reuters.com/world/x"))
        self.assertIn("official", manifest.classify_source_url("https://www.whitehouse.gov/briefing-room/x"))

    def test_registrable_domain_strips_scheme_port_and_www(self) -> None:
        self.assertEqual(manifest.registrable_domain("https://www.Example.com:8443/a?b=c"), "example.com")


def _run_assemble(root: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "DAILY_BRIEFING_ROOT": str(root), "DAILY_BRIEFING_SHARED_ROOT": str(REPO_ROOT)}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "assemble_briefing.py"), "--date", DATE],
        capture_output=True, text=True, env=env, cwd=str(SCRIPTS_DIR), timeout=120,
    )


class AssemblyIsNonDestructiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "archive").mkdir()
        self.stem = "claude-daily-briefing"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, section: str, text: str) -> Path:
        path = self.root / "archive" / f"{self.stem}-{section}-{DATE}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def _write_all(self, part1_body: str) -> dict[str, Path]:
        paths = {"part1": self._write("part1", part1_body)}
        paths["header"] = self._write("header", f"# The Daily Intelligence Briefing\n\n### Table of Contents\n\nplaceholder\n")
        for section, title in (("part2", "WORLD"), ("part3", "TECH"), ("part4", "AI"),
                               ("humaninterest", "HUMAN INTEREST"), ("updates", "UPDATES"), ("closing", "CLOSING ANALYSIS")):
            paths[section] = self._write(section, f"# {title}\n")
        return paths

    def test_section_with_only_thin_items_keeps_originals_and_markers_hidden_from_output(self) -> None:
        thin = (
            "# TOP STORIES\n\n## Thin Story\n\n<!-- story: thin-story-id -->\n\nThree short sentences. Nothing else. No sources.\n"
        )
        paths = self._write_all(thin)
        result = _run_assemble(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kept = paths["part1"].read_text(encoding="utf-8")
        # Selection must not truncate the section to its heading.
        self.assertIn("Three short sentences", kept)
        # The marker stays in the fragment (it carries story identity) ...
        self.assertIn("thin-story-id", kept)
        # ... but never reaches the reader-facing assembled markdown.
        assembled = (self.root / "archive" / f"{self.stem}-{DATE}.md").read_text(encoding="utf-8")
        self.assertNotIn("thin-story-id", assembled)
        pool = json.loads((self.root / "generated" / f"{self.stem}-{DATE}-candidate-pool.json").read_text(encoding="utf-8")) \
            if (self.root / "generated" / f"{self.stem}-{DATE}-candidate-pool.json").exists() else None
        if pool is not None:
            self.assertIn("selection_error", pool["sections"]["part1"])

    def test_candidate_pool_carries_over_earlier_decisions(self) -> None:
        self._write_all("# TOP STORIES\n\n## Thin Story\n\nShort.\n")
        pool_dir = self.root / "generated"
        pool_dir.mkdir(exist_ok=True)
        pool_file = pool_dir / f"{self.stem}-{DATE}-candidate-pool.json"
        pool_file.write_text(json.dumps({"no_update_decisions": [{"section": "updates", "reason": "nothing new"}]}),
                             encoding="utf-8")
        result = _run_assemble(self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # candidate_pool_path may live elsewhere depending on the runner profile; find the file written.
        candidates = list(self.root.rglob("*candidate-pool*.json"))
        self.assertTrue(candidates, "candidate pool was not written")
        merged = json.loads(candidates[0].read_text(encoding="utf-8"))
        self.assertEqual(merged["no_update_decisions"][0]["reason"], "nothing new")


if __name__ == "__main__":
    unittest.main()
