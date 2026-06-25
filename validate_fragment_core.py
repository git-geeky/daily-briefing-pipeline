"""Per-fragment (single-section) validator.

The full validator at `validate_briefing_core.py` runs once at the end of the pipeline,
AFTER all seven content subagents have completed and the briefing has been assembled.
If a single section's fragment has format drift (missing Counterpoint Matrix, wrong
Primary Sources heading, short word count, banned-topic false positive), the full
validator surfaces it too late to course-correct without hand-patching.

This module runs the **per-item rules** from `validate_briefing_core.py` against a
single fragment, using the same `generate_fragment_manifest()` parser. Rules that
require cross-fragment context (TOC sync, focus-company coverage, header cross-reference
title match) are deliberately skipped — they can only be checked after full assembly.

Exit code:
    0 — fragment passed (no blocking errors specific to its content)
    1 — one or more blocking errors detected
    2 — argparse / setup error

Use from the orchestrator (in SKILL.md's save-as-returned loop):

    result = subprocess.run(
        ["py", "scripts/validate_fragment_core.py", "--section", section, "--date", date],
        capture_output=True, text=True,
    )
    if result.returncode == 1:
        # Surface the violations to the respawn prompt for that section only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Local sibling imports
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import briefing_paths  # noqa: E402
from briefing_manifest_core import (  # noqa: E402
    COUNTERPOINT_REQUIRED_SECTIONS,
    HARD_NEWS_SECTIONS,
    SECTION_RULES,
    WATCH_REQUIRED_SECTIONS,
    generate_fragment_manifest,
    load_image_metadata,
    load_prior_items,
)


def validate_fragment(
    section: str,
    date: str,
    *,
    text: str | None = None,
) -> dict[str, Any]:
    """Return `{blocking: [], nonblocking: [], warnings: [], items: [...]}`.

    If `text` is None, reads from `briefing_paths.fragment_path(section, date)`.
    Respects the three-tier word floor (`hard_floor` → blocking, `soft_min_words` →
    nonblocking, `min_words` → warning) added in Phase A5.
    """
    if text is None:
        frag_path = briefing_paths.fragment_path(section, date)
        if not frag_path.exists():
            raise FileNotFoundError(f"Fragment not found: {frag_path}")
        text = frag_path.read_text(encoding="utf-8")

    prior_items = load_prior_items(date)
    image_metadata = load_image_metadata(date)
    manifest = generate_fragment_manifest(
        section, date, text, prior_items=prior_items, image_metadata=image_metadata
    )

    blocking: list[str] = []
    nonblocking: list[str] = []
    warnings: list[str] = []

    if section == "header":
        # Cross-fragment rules only — per-fragment validator has nothing to check.
        return {"section": section, "blocking": [], "nonblocking": [], "warnings": [], "items": []}

    rules = SECTION_RULES.get(section)
    if not rules:
        return {
            "section": section,
            "blocking": [f"Unknown section: {section}"],
            "nonblocking": [],
            "warnings": [],
            "items": [],
        }

    items = manifest["items"]
    real_items = [item for item in items if not item.get("quality_failure")]
    failure_items = [item for item in items if item.get("quality_failure")]

    if "exact_count" in rules:
        if len(real_items) != rules["exact_count"]:
            if len(real_items) < rules["exact_count"] and failure_items:
                warnings.append(
                    f"{section}: short of quota with a quality-failure marker "
                    f"({len(real_items)}/{rules['exact_count']})"
                )
            else:
                blocking.append(
                    f"{section}: article count is {len(real_items)} but expected {rules['exact_count']}"
                )
    else:
        if len(real_items) < rules["min_count"] and not failure_items:
            blocking.append(
                f"{section}: article count is {len(real_items)} but minimum is {rules['min_count']}"
            )
        if len(real_items) > rules["max_count"]:
            blocking.append(
                f"{section}: article count is {len(real_items)} but maximum is {rules['max_count']}"
            )

    for item in real_items:
        prefix = f'{section}/{item["title"]}'
        if section in COUNTERPOINT_REQUIRED_SECTIONS and not item["has_counterpoint_matrix"]:
            blocking.append(f"{prefix}: missing Counterpoint Matrix")
        if section in WATCH_REQUIRED_SECTIONS and not item["watch_next_48h"]:
            blocking.append(f"{prefix}: missing Watch Next 48h block")
        if not item["source_appendix_present"]:
            blocking.append(f"{prefix}: missing source appendix")

        soft_min = rules.get("soft_min_words", rules["min_words"])
        hard_floor = rules.get("hard_floor", soft_min)
        wc = item["word_count"]
        if wc < hard_floor:
            blocking.append(
                f"{prefix}: article is below the hard word floor "
                f"({wc} < {hard_floor}); target is {rules['min_words']}"
            )
        elif wc < soft_min:
            nonblocking.append(
                f"{prefix}: article is below the soft word threshold but above the hard floor "
                f"({wc} < {soft_min}, >= {hard_floor})"
            )
        elif wc < rules["min_words"]:
            warnings.append(
                f"{prefix}: article is below the target word count but within the soft band "
                f"({wc} < {rules['min_words']}, >= {soft_min})"
            )

        if item["source_count"] < rules["min_sources"]:
            blocking.append(
                f"{prefix}: article is below the source threshold ({item['source_count']} < {rules['min_sources']})"
            )
        if item["banned_topics"]:
            blocking.append(f"{prefix}: contains banned topic(s): {', '.join(item['banned_topics'])}")
        if item["has_body_links"]:
            blocking.append(f"{prefix}: contains external links in the body outside the source appendix")
        if item["relative_time_mentions"] and not item["has_exact_jst_date"]:
            blocking.append(f"{prefix}: uses relative timing without an exact JST date/time anchor")
        if item["needs_second_pass"]:
            blocking.append(f"{prefix}: thin sourcing requires a second corroboration pass")
        if item["image_quality"]["duplicate_urls"]:
            blocking.append(f"{prefix}: contains duplicate image URLs in a single article")
        if section in HARD_NEWS_SECTIONS and item["image_quality"].get("all_placeholder"):
            blocking.append(
                f"{prefix}: image set is only placeholder local SVG cards; replace with source-derived "
                "or genuinely evidentiary visuals"
            )
        if section in HARD_NEWS_SECTIONS and item["image_quality"].get("placeholder_count", 0) > 0:
            warnings.append(f"{prefix}: contains {item['image_quality']['placeholder_count']} placeholder image card(s)")

    return {
        "section": section,
        "blocking": blocking,
        "nonblocking": nonblocking,
        "warnings": warnings,
        "items": [
            {
                "title": item.get("title"),
                "word_count": item.get("word_count"),
                "source_count": item.get("source_count"),
                "banned_topics": item.get("banned_topics"),
            }
            for item in real_items
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a single section fragment without needing the assembled manifest.",
    )
    parser.add_argument("--section", required=True, help="Section key (part1, part2, part3, part4, humaninterest, updates, closing, header).")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable output.")
    args = parser.parse_args()

    try:
        result = validate_fragment(args.section, args.date)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        section = result["section"]
        print(f"[{section}] blocking: {len(result['blocking'])}, nonblocking: {len(result['nonblocking'])}, warnings: {len(result['warnings'])}")
        for line in result["blocking"]:
            print(f"  BLOCKING: {line}")
        for line in result["nonblocking"]:
            print(f"  nonblocking: {line}")
        for line in result["warnings"]:
            print(f"  warning: {line}")

    return 1 if result["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

