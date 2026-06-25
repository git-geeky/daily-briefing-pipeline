from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def install_briefing_paths_stub() -> None:
    module = types.ModuleType("briefing_paths")
    module.SECTIONS = ("part1", "part2", "part3", "part4", "humaninterest", "updates", "closing")
    module.runner_name = lambda: "shared-core-test"
    module.fragment_path = lambda section, date: Path("archive") / f"briefing-{section}-{date}.md"
    module.fragment_manifest_path = lambda section, date: Path("archive") / f"briefing-{section}-{date}.json"
    module.assembled_markdown_path = lambda date: Path("archive") / f"briefing-{date}.md"
    module.assembled_manifest_path = lambda date: Path("archive") / f"briefing-{date}.json"
    module.source_audit_path = lambda date: Path("generated") / f"briefing-{date}-source-audit.json"
    module.source_audit_markdown_path = lambda date: Path("generated") / f"briefing-{date}-source-audit.md"
    module.adaptive_fetch_report_path = lambda date: Path("generated") / f"briefing-{date}-adaptive-fetch.json"
    module.evidence_digest_path = lambda date: Path("generated") / f"briefing-{date}-evidence-digest.md"
    module.source_tensions_path = lambda date: Path("generated") / f"briefing-{date}-source-tensions.json"
    module.research_lint_path = lambda date: Path("generated") / f"briefing-{date}-research-lint.json"
    module.source_confidence_path = lambda date: Path("generated") / f"briefing-{date}-source-confidence.json"
    module.research_depth_path = lambda date: Path("generated") / f"briefing-{date}-research-depth.json"
    module.research_depth_markdown_path = lambda date: Path("generated") / f"briefing-{date}-research-depth.md"
    module.research_vault_index_path = lambda: Path("research-vault") / "source-index.json"
    module.image_cache_dir = lambda date: Path("generated") / "briefing-images" / date
    module.validation_report_path = lambda date: Path("generated") / f"briefing-{date}-validation.json"
    module.pdf_path = lambda date: Path("generated") / f"briefing-{date}.pdf"
    module.run_report_path = lambda date: Path("generated") / f"briefing-{date}-run.json"
    module.visual_audit_path = lambda date, digest: Path("generated") / f"briefing-{date}-visual-audit-{digest[:8]}.md"
    sys.modules["briefing_paths"] = module


class SharedCoreSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        install_briefing_paths_stub()

    def test_adaptive_fetch_detects_blocking_and_extracts_metadata(self) -> None:
        adaptive_fetch = importlib.import_module("adaptive_fetch_core")
        signals = adaptive_fetch.detect_blocking_signals(
            403,
            {"Server": "cloudflare", "CF-Ray": "abc"},
            "<html><title>Just a moment...</title><body>Verify you are human</body></html>",
        )
        self.assertIn("status:403", signals)
        self.assertIn("header:cf-ray", signals)
        self.assertTrue(any(signal.startswith("marker:just a moment") for signal in signals))

        metadata = adaptive_fetch.extract_metadata(
            '<meta property="og:title" content="OG Story"><script type="application/ld+json">{"headline":"Structured"}</script>',
            "https://example.com/story",
        )
        self.assertEqual(metadata["og"]["og:title"], "OG Story")
        self.assertEqual(metadata["headline"], "Structured")

    def test_manifest_helpers_classify_sources_images_and_watch_blocks(self) -> None:
        manifest = importlib.import_module("briefing_manifest_core")
        sources = manifest.extract_primary_sources(
            "\n".join(
                [
                    "### Primary Sources",
                    "- Reuters report - https://reuters.com/example",
                    "- Ministry release - https://example.gov/release",
                ]
            )
        )
        self.assertEqual(len(sources), 2)
        self.assertIn("wire", manifest.classify_sources(sources))
        self.assertIn("official", manifest.classify_sources(sources))

        watch = manifest.extract_watch_entries(
            "\n".join(["> **Watch Next 48h:**", "> - June 23, 2026: Event one"])
        )
        self.assertEqual(watch, ["June 23, 2026: Event one"])

        images = manifest.extract_images("![Satellite map](C:/tmp/map.png)")
        quality = manifest.classify_images(images)
        self.assertEqual(quality["total"], 1)
        self.assertEqual(quality["evidentiary_count"], 1)
        self.assertEqual(quality["local_raster_count"], 1)

    def test_run_reporting_default_report_uses_stubbed_runner(self) -> None:
        run_reporting = importlib.import_module("run_reporting_core")
        report = run_reporting.default_run_report("2026-06-22")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["runner"], "shared-core-test")
        self.assertEqual(report["validation_status"], "not_run")


if __name__ == "__main__":
    unittest.main()
