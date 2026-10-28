"""The private overlay: personal coverage details live outside git and merge over the generic public edition."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from _shared_imports import REPO_ROOT  # noqa: F401

import conductor_core as cc  # noqa: E402
import edition_core  # noqa: E402
import merge_private_overlay as merge  # noqa: E402
import private_config  # noqa: E402

OVERLAY = {"edition:daily": {"conductor": {"section_briefs": {"part4": "PRIVATE-REGION coverage brief"}}, "spec_notes": "PRIVATE-NOTE owner coverage"},
           "recipients": ["someone@example.invalid"]}


class OverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = os.environ.get(private_config.RESTORE_FILE_ENV)
        self.addCleanup(self.restore)
        edition_core._load_cached.cache_clear()
        self.addCleanup(edition_core._load_cached.cache_clear)

    def restore(self) -> None:
        if self.previous is None:
            os.environ.pop(private_config.RESTORE_FILE_ENV, None)
        else:
            os.environ[private_config.RESTORE_FILE_ENV] = self.previous

    def write_overlay(self, payload=None) -> Path:
        path = Path(self.tmp.name) / "restore.local.json"
        path.write_text(json.dumps(OVERLAY if payload is None else payload), encoding="utf-8")
        os.environ[private_config.RESTORE_FILE_ENV] = str(path)
        edition_core._load_cached.cache_clear()
        return path

    # SHA-256 of lower-cased words, word pairs and (for Japanese) three-character runs that name the owner's home region and interests. The
    # words themselves are not written here: a test that lists them would publish them.
    PRIVATE_HASHES = frozenset({
        "29008210f6124abd1a2aa827619795b827a05bc06c054340d2f66fa92cf5b69f", "0718921b48647a0cffdd7303fe2f47bdd1b7186df76265c03a1f9c404815f5fe",
        "fefd90d50bea5e5cebe31680ec4b6196b04cb7b5b97896bf270cf2135e6f7d8c", "2a95e2ef9e4d24f0b8925c98881c05cd71927bb8ca794d1a5857ee33ed79ffc0",
        "85b5f697eb22357b4bbbc403d4db0a350b15cecf4371b25ec88f9f840c2eb0b4", "bd64d4aaf40e73ef5ee0c34266763271685da1554076b074cec14885cb876073"})

    @classmethod
    def private_hits(cls, text: str) -> list[str]:
        import hashlib
        import re

        words = re.findall(r"[a-z]+", text.lower())
        candidates = set(words) | {" ".join(pair) for pair in zip(words, words[1:])}
        for run in re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]{3,}", text):
            candidates |= {run[i:i + 3] for i in range(len(run) - 2)}
        return sorted(c for c in candidates if hashlib.sha256(c.encode()).hexdigest() in cls.PRIVATE_HASHES)

    def test_the_public_edition_carries_no_personal_coverage_details(self) -> None:
        files = [REPO_ROOT / "editions" / "daily.json", REPO_ROOT / "references" / "briefing-spec.md", REPO_ROOT / "references" / "sources.md",
                 *sorted((REPO_ROOT / "prompts" / "daily").glob("*.md"))]
        for path in files:
            self.assertEqual(self.private_hits(path.read_text(encoding="utf-8")), [], f"{path.name} names the owner's home region or interests")

    def test_the_leak_check_itself_finds_what_it_is_for(self) -> None:
        import hashlib
        from unittest import mock

        harmless = frozenset(hashlib.sha256(w.encode()).hexdigest() for w in ("zebra crossing", "quokka", "\u3053\u3093\u306b"))
        with mock.patch.object(type(self), "PRIVATE_HASHES", harmless):
            hits = self.private_hits("A quokka near the Zebra Crossing, then \u3053\u3093\u306b\u3061\u306f.")
        self.assertEqual(hits, ["quokka", "zebra crossing", "\u3053\u3093\u306b"])  # a word, a word pair and a Japanese run

    def test_the_owners_private_source_catalog_joins_the_audit_without_clashing_with_the_public_numbers(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import source_audit_core

        catalog = Path(self.tmp.name) / "private-sources-daily.md"
        catalog.write_text("## PART IV \u2014 JAPAN NEWS\n\n### Regional\n\n| # | Source | URL | Why |\n|---|--------|-----|-----|\n"
                           "| 75 | **A Private Regional Paper** | https://regional.example/ | A regional paper. |\n", encoding="utf-8")
        previous = os.environ.get(private_config.CATALOG_ENV)
        os.environ[private_config.CATALOG_ENV] = str(catalog)
        edition_core._load_cached.cache_clear()
        try:
            names = [e.name for e in source_audit_core.parse_source_catalogs()]
        finally:
            if previous is None:
                os.environ.pop(private_config.CATALOG_ENV, None)
            else:
                os.environ[private_config.CATALOG_ENV] = previous
        self.assertIn("A Private Regional Paper", names)
        self.assertGreater(len(names), 300)  # and the public catalog is still read in full

    def test_without_a_private_catalog_the_public_one_is_read_alone(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import source_audit_core

        previous = os.environ.get(private_config.CATALOG_ENV)
        os.environ[private_config.CATALOG_ENV] = str(Path(self.tmp.name) / "missing.md")
        try:
            names = [e.name for e in source_audit_core.parse_source_catalogs()]
        finally:
            if previous is None:
                os.environ.pop(private_config.CATALOG_ENV, None)
            else:
                os.environ[private_config.CATALOG_ENV] = previous
        self.assertGreater(len(names), 300)
        self.assertNotIn("A Private Regional Paper", names)

    def test_without_an_overlay_the_edition_is_generic_and_says_so(self) -> None:
        os.environ[private_config.RESTORE_FILE_ENV] = str(Path(self.tmp.name) / "missing.local.json")
        edition_core._load_cached.cache_clear()
        edition = edition_core.load_edition("daily")
        self.assertFalse(edition.get("private_overlay_applied"))
        self.assertTrue(edition.get("private_overlay.required"))
        self.assertNotIn("PRIVATE-REGION", json.dumps(edition.extra))

    def test_an_overlay_merges_over_only_its_own_edition_and_only_that_key(self) -> None:
        self.write_overlay()
        edition = edition_core.load_edition("daily")
        self.assertTrue(edition.get("private_overlay_applied"))
        self.assertEqual(edition.get("conductor.section_briefs.part4"), "PRIVATE-REGION coverage brief")
        self.assertIn("Non-AI tech", edition.get("conductor.section_briefs.part3"), "other keys of the public edition survive the merge")
        self.assertNotIn("recipients", edition.extra, "nothing else in the restore file reaches an edition")
        self.assertNotIn("PRIVATE", json.dumps(edition_core.load_edition("animal").extra))

    def test_a_damaged_overlay_file_is_an_empty_overlay_not_a_crash(self) -> None:
        path = self.write_overlay()
        path.write_text("{not json", encoding="utf-8")
        edition_core._load_cached.cache_clear()
        self.assertFalse(edition_core.load_edition("daily").get("private_overlay_applied"))

    def test_the_spec_handed_to_the_writer_carries_the_private_notes_only_when_the_overlay_exists(self) -> None:
        def spec_for(edition) -> str:
            c = cc.Conductor.__new__(cc.Conductor)
            c.edition = edition
            return c.spec_text()

        os.environ[private_config.RESTORE_FILE_ENV] = str(Path(self.tmp.name) / "missing.local.json")
        edition_core._load_cached.cache_clear()
        self.assertNotIn("PRIVATE-NOTE", spec_for(edition_core.load_edition("daily")))
        self.write_overlay()
        self.assertIn("PRIVATE-NOTE owner coverage", spec_for(edition_core.load_edition("daily")))

    def test_a_missing_overlay_raises_one_alert_and_does_not_stop_the_run(self) -> None:
        alerts: list[str] = []
        c = cc.Conductor.__new__(cc.Conductor)
        c.edition = types.SimpleNamespace(label="Daily", edition_id="daily", get=lambda key, default=None: {"private_overlay.required": True}.get(key, default))
        c.log = lambda message: None
        c._alert_once = lambda key, subject, body: alerts.append(key)
        c._check_private_overlay()
        self.assertEqual(alerts, ["private-overlay-missing"])
        alerts.clear()
        c.edition = types.SimpleNamespace(label="Daily", edition_id="daily", get=lambda key, default=None: {"private_overlay.required": True, "private_overlay_applied": True}.get(key, default))
        c._check_private_overlay()
        self.assertEqual(alerts, [], "no alert when the overlay was found")
        c.edition = types.SimpleNamespace(label="Animal", edition_id="animal", get=lambda key, default=None: default)
        c._check_private_overlay()
        self.assertEqual(alerts, [], "an edition that needs no overlay never alerts")


class MergeHelperTests(unittest.TestCase):
    def test_merge_keeps_existing_settings_backs_up_and_reports_key_paths_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "restore.local.json"
            target.write_text(json.dumps({"recipients": ["a@example.invalid"], "edition:daily": {"conductor": {"section_briefs": {"part1": "keep"}}}}), encoding="utf-8")
            overlay = Path(tmp) / "overlay.json"
            overlay.write_text(json.dumps(OVERLAY), encoding="utf-8")
            dry = merge.merge_file(overlay, target, dry_run=True)
            self.assertIn("edition:daily.conductor.section_briefs.part4", dry)
            self.assertNotIn("PRIVATE-REGION", " ".join(dry))
            self.assertFalse(target.with_name(target.name + ".bak").exists())
            merge.merge_file(overlay, target)
            merged = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(merged["edition:daily"]["conductor"]["section_briefs"], {"part1": "keep", "part4": "PRIVATE-REGION coverage brief"})
            self.assertEqual(merged["recipients"], ["someone@example.invalid"], "an overlay value replaces the old one")
            self.assertTrue(target.with_name(target.name + ".bak").exists())

    def test_merge_creates_the_file_when_there_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / "overlay.json"
            overlay.write_text(json.dumps(OVERLAY), encoding="utf-8")
            target = Path(tmp) / "new" / "restore.local.json"
            merge.merge_file(overlay, target)
            self.assertIn("edition:daily", json.loads(target.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
