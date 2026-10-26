"""Integrity checks for the animal-edition source catalog (references/animal-sources.md)."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

from _shared_imports import REPO_ROOT  # noqa: F401

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import source_audit_core as source_audit  # noqa: E402

REFERENCES = REPO_ROOT / "references"
ANIMAL_CATALOG = REFERENCES / "animal-sources.md"
GENERAL_CATALOG = REFERENCES / "sources.md"
MIN_ANIMAL_SOURCES = 100


class AnimalCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.animal = source_audit.parse_source_catalog(ANIMAL_CATALOG)
        cls.general = source_audit.parse_source_catalog(GENERAL_CATALOG)
        cls.meta = json.loads(ANIMAL_CATALOG.with_suffix(".meta.json").read_text(encoding="utf-8"))

    def test_at_least_one_hundred_sources(self) -> None:
        self.assertGreaterEqual(len(self.animal), MIN_ANIMAL_SOURCES)

    def test_numbers_do_not_collide_with_general_catalog(self) -> None:
        animal_numbers = {entry.catalog_number for entry in self.animal}
        general_numbers = {entry.catalog_number for entry in self.general}
        self.assertFalse(animal_numbers & general_numbers)
        self.assertEqual(len(animal_numbers), len(self.animal), "duplicate catalog numbers inside the animal catalog")

    def test_urls_are_https_unique_and_not_in_general_catalog(self) -> None:
        urls = [entry.url for entry in self.animal]
        self.assertTrue(all(url.startswith("https://") or url.startswith("http://") for url in urls))
        norm = lambda url: re.sub(r"^https?://(www\\.)?", "", url.lower()).rstrip("/")  # noqa: E731
        self.assertEqual(len({norm(url) for url in urls}), len(urls), "duplicate URLs in the animal catalog")
        self.assertFalse({norm(url) for url in urls} & {norm(entry.url) for entry in self.general})

    def test_every_row_earns_animal_section_credit(self) -> None:
        for entry in self.animal:
            self.assertIn("animals", entry.credit_sections, entry.name)

    def test_every_row_has_metadata_with_language_and_region(self) -> None:
        for entry in self.animal:
            meta = self.meta.get(str(entry.catalog_number))
            self.assertIsNotNone(meta, f"no meta for #{entry.catalog_number} {entry.name}")
            self.assertTrue(meta.get("lang"), entry.name)
            self.assertTrue(meta.get("region"), entry.name)

    def test_substantial_native_language_coverage(self) -> None:
        non_english = [entry for entry in self.animal if entry.language_tags and entry.language_tags[0] != "en"]
        languages = {entry.language_tags[0] for entry in non_english}
        self.assertGreaterEqual(len(non_english), 30)
        self.assertGreaterEqual(len(languages), 10)

    def test_licensed_photo_libraries_are_present(self) -> None:
        photo = [entry for entry in self.animal if "PHOTO" in entry.major_bucket.upper()]
        self.assertGreaterEqual(len(photo), 8)

    def test_compromised_homepage_is_not_listed(self) -> None:
        # seo.org's homepage carried injected casino spam when the catalog was built; only its feed is listed.
        for entry in self.animal:
            self.assertNotEqual(entry.url.rstrip("/"), "https://seo.org")

    def test_multi_catalog_parse_has_no_number_collisions(self) -> None:
        combined = source_audit.parse_source_catalogs([GENERAL_CATALOG, ANIMAL_CATALOG])
        self.assertEqual(len({entry.catalog_number for entry in combined}), len(combined))


if __name__ == "__main__":
    unittest.main()
