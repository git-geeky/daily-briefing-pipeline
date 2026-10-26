"""The private overlay: personal coverage details live outside git and merge over the generic public edition."""
from __future__ import annotations

import json
import os
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

    def test_the_public_edition_carries_no_personal_coverage_details(self) -> None:
        text = (REPO_ROOT / "editions" / "daily.json").read_text(encoding="utf-8") + (REPO_ROOT / "references" / "briefing-spec.md").read_text(encoding="utf-8")
        for private in ("PRIVATE-TERM",):
            self.assertNotIn(private, text)

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
