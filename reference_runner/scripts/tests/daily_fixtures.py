"""Synthetic daily-edition content for the conductor's multi-section end-to-end runs (structure only, filler prose)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from animal_fixtures import filler_paragraphs

SECTION_LABEL = {"part1": "Part I", "part2": "Part II", "part3": "Part III", "part4": "Part IV", "humaninterest": "Part V", "updates": "Part VI"}


def section_catalog_urls(section: str, count: int = 3) -> list[str]:
    import source_audit_core as audit

    root = Path(audit.__file__).resolve().parent / "references"
    entries = audit.parse_source_catalogs([root / "sources.md"])
    return [e.url for e in entries if section in e.credit_sections and e.url.startswith("https://")][:count]


def make_daily_article(story: dict[str, Any], section: str, *, words: int = 1600) -> str:
    per = max(words // 4, 60)
    sources = [
        "Reuters report on the development. https://www.reuters.com/world/fixture-story",
        "Government statement. https://www.whitehouse.gov/briefing-room/fixture-statement",
        "Peer-reviewed analysis. https://www.nature.com/articles/fixture-analysis",
        "Bloomberg coverage. https://www.bloomberg.com/news/articles/fixture-coverage",
        *[f"Catalog source. {url}" for url in section_catalog_urls(section, 3)],
    ]
    marker = f"<!-- story: {story['story_id']}" + (f"; prior: {story['prior_story_id']}" if story.get("prior_story_id") else "") + " -->"
    parts = [
        f"## {story['title']}", marker, "",
        "On 2026-10-26 officials confirmed the change at 09:00 JST. " + filler_paragraphs(per, story["story_id"] + "a"),
        "", filler_paragraphs(per, story["story_id"] + "b"),
        "", filler_paragraphs(per, story["story_id"] + "c"),
        "", filler_paragraphs(per, story["story_id"] + "d"),
        "", "### Counterpoint Matrix", "",
        "| Claim | Interested Party | Structural Incentive | Tier-4 Counterpoint Source | Net Assessment |",
        "|---|---|---|---|---|",
        "| The change is permanent | Ministry | Wants a durable policy win | Independent analyst note | Plausible but unproven |",
        "| The change is temporary | Opposition | Wants to reverse it | Trade association memo | Weakly supported |",
        "", "### Watch Next 48h", "",
        "- 2026-10-27 09:00 JST: follow-up statement expected (https://example.org/statement)",
        "- 2026-10-28 15:00 JST: scheduled committee session (https://example.org/committee)",
        "", "### Primary Sources", "", *[f"- {s}" for s in sources],
    ]
    return "\n".join(parts) + "\n"


def make_daily_slate(*, part4: int = 4, updates: int = 2) -> dict[str, Any]:
    counts = {"part1": 6, "part2": 9, "part3": 4, "part4": part4, "humaninterest": 2, "updates": updates}
    stories: list[dict[str, Any]] = []
    rank = 0
    for section, n in counts.items():
        for i in range(n):
            rank += 1
            is_update = section == "updates"
            stories.append({
                "story_id": f"{section}-story-{i + 1}",
                "title": f"{SECTION_LABEL[section]} fixture story {i + 1}: officials confirm a structural change in policy and markets",
                "section": section, "rank": rank, "region": "Global", "angle": "What changed and why it matters",
                "why_now": "Confirmed 2026-10-26 09:00 JST", "novelty": "development" if is_update else "new",
                "prior_story_id": "earlier-story" if is_update else None, "material": "deep" if i == 0 else "moderate",
                "sources": [{"url": f"https://example.org/{section}-{i}-a", "role": "wire", "lang": "en"},
                            {"url": f"https://example.org/{section}-{i}-b", "role": "primary", "lang": "en"}],
            })
    return {
        "stories": stories,
        "no_update_decisions": [{"beat": "focus_company", "reason": "Checked the company's newsroom, filings and wires; nothing material since the last briefing."}],
    }
