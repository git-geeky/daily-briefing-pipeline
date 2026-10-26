"""Synthetic animal-edition content for structural tests and the end-to-end fixture run.

The prose is deliberately meaningless filler: these fixtures exist to exercise *structure*
(validators, assembly, normalisation, PDF, delivery), never to stand in for editorial quality.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SECTION_FILLER = [
    "Detection probability varied with canopy density and survey effort across the {n} transects, and the authors modelled it explicitly rather than treating counts as abundance.",
    "Males dispersed farther than females in every cohort, consistent with the sex-biased dispersal reported for congeners, and natal philopatry was weaker than earlier estimates implied.",
    "Body-mass trajectories diverged after the second winter, a pattern the supplementary tables attribute to forage quality rather than density dependence.",
    "Telemetry fixes were screened for dilution of precision above {n}, and the remaining tracks were resampled to regular intervals before home-range estimation.",
    "Seroprevalence rose across the study window while recorded mortality did not, which suggests exposure without clinical disease in most individuals.",
    "The phylogenetic placement is robust to the choice of substitution model, though one node retains modest support and the authors say so plainly.",
    "Reproductive output tracked rainfall with a one-season lag, and the lag was longer at the drier sites included in the sample.",
    "Previous work using a coarser design reported the opposite sign for this effect; the present analysis resolves the discrepancy by conditioning on age class.",
    "Data and code are archived in a public repository, and the analysis scripts reproduce the main figures from raw detections without manual steps.",
    "Management agencies in the range states have not yet commented, and the draft recovery plan does not mention the new estimate.",
]


def filler_paragraphs(words: int, seed: str) -> str:
    out: list[str] = []
    count, i = 0, 0
    while count < words:
        sentence = SECTION_FILLER[(i + len(seed)) % len(SECTION_FILLER)].format(n=10 + (i * 7 + len(seed)) % 90)
        out.append(sentence)
        count += len(sentence.split())
        i += 1
        if len(out) % 4 == 0:
            out.append("\n")
    return " ".join(out).replace(" \n ", "\n\n")


def make_article(story: dict[str, Any], *, words: int = 3700, source_lines: list[str] | None = None, confidence: str = "Medium",
                 messy: bool = False, binomial: str = "Genus species") -> str:
    """A spec-complete article for ``story`` (title/story_id/prior_story_id)."""
    title = story["title"]
    sid = story["story_id"]
    sources = source_lines or default_sources()
    per_section = max(words // 6, 60)
    marker = f"<!-- story: {sid}" + (f"; prior: {story['prior_story_id']}" if story.get("prior_story_id") else "") + " -->"
    parts = [
        f"## {title}", marker, "",
        f"**Bottom line:** The study reports a clear effect in *{binomial}* but the design cannot separate two explanations. "
        f"The finding matters for how the population is managed; it does not settle the mechanism.",
        f"**Confidence:** {confidence} — peer-reviewed, single study, data archived, no independent replication yet.",
        "", "### Taxonomy & Status", "",
        "| Taxon | Scientific name | IUCN Red List | CITES | Range / population |",
        "|---|---|---|---|---|",
        f"| Example species | *{binomial}* | VU (2022, decreasing) | Appendix II | Savanna and woodland, three range states |",
        "", "### What happened", "", f"On 2026-10-24 the group published its analysis of *{binomial}*. " + filler_paragraphs(per_section, sid + "a"),
        "", "### Natural history and ecology", "", filler_paragraphs(per_section, sid + "b"),
        "", "### The evidence", "",
        "Study card: Doe J, Roe R (2026), doi:10.1234/fixture.2026.001. Design: longitudinal telemetry, n = 412 animals, 2019-2023. "
        "Annual survival 0.87 (95% CI 0.81-0.92). Data and code archived; no conflicts declared; peer-reviewed.",
        "", filler_paragraphs(per_section, sid + "c"),
        "", "### Methods and limits", "", filler_paragraphs(per_section, sid + "d"),
        "", "### How this fits the literature", "", filler_paragraphs(per_section, sid + "e"),
        "", "### Implications", "", filler_paragraphs(per_section, sid + "f"),
        "", "### Counterpoint Matrix", "",
        "| Claim | Who holds it | Evidence for | Evidence against or limits | What would change the call |",
        "|---|---|---|---|---|",
        "| Effect is driven by forage quality | Study authors | Survival tracks rainfall lag | Single site cluster | Replication at wetter sites |",
        "| Effect is a detection artefact | Independent ecologists | Detection varied | Authors modelled detection | Mark-recapture re-analysis |",
        "", "### Watch Next", "",
        "- 2026-11-12: agency comment period closes on the draft recovery plan (https://example.org/plan)",
        "- 2027-03-01: next annual census release (https://example.org/census)",
        "", "### Primary Sources", "",
        *[f"- {line}" for line in sources],
    ]
    text = "\n".join(parts) + "\n"
    if messy:
        text = ("Here is the article you asked for:\n\n```markdown\n" + text.replace("### Counterpoint Matrix", "## Counterpoint matrix")
                .replace("### Primary Sources", "## References")
                .replace("**Bottom line:**", "**Bottom Line**:")
                .replace("### What happened", "### What happened\n\nSee [the agency page](https://example.org/agency) and https://example.org/raw for details.")
                + "\n```\n")
    return text


_DEFAULT_SOURCE_POOL: list[str] = []


def default_sources(extra_catalog_urls: list[str] | None = None) -> list[str]:
    urls = list(extra_catalog_urls or [])
    base = [
        "Doe J, Roe R (2026). Winter survival of tagged ungulates. *J Wildl Res*. doi:10.1234/fixture.2026.001 https://www.nature.com/articles/fixture-2026",
        "IUCN Red List species page, retrieved 2026. https://www.iucnredlist.org/species/12345/67890",
        "US Fish and Wildlife Service, recovery plan page. https://www.fws.gov/program/endangered-species/news",
        "Reuters, report on the agency decision. https://www.reuters.com/world/fixture-story",
        "Mongabay, field reporting. https://news.mongabay.com/2026/10/fixture-story/",
    ]
    return base + [f"Catalog source. {u}" for u in urls]


def catalog_urls(count: int = 4) -> list[str]:
    """URLs of animal-credited catalog rows so the source-incorporation rules are really exercised."""
    import source_audit_core as audit

    root = Path(audit.__file__).resolve().parent / "references"
    entries = audit.parse_source_catalogs([root / "sources.md", root / "animal-sources.md"])
    animal = [e for e in entries if e.catalog_number >= 401 and "animals" in e.credit_sections]
    return [e.url for e in animal[:count]]


def make_slate_json(count: int = 12) -> list[dict[str, Any]]:
    groups = ["mammal", "bird", "fish_marine", "herp", "invertebrate", "mammal", "bird", "other", "mammal", "fish_marine", "herp", "invertebrate"]
    regions = ["Kenya", "Japan", "Australia", "India", "Brazil", "Mongolia", "Peru", "Global", "United States", "Norway", "South Africa", "Indonesia"]
    entries = []
    for i in range(count):
        entries.append({
            "story_id": f"fixture-story-{i + 1}", "title": f"Fixture story {i + 1}: survival and dispersal in a monitored population of {groups[i % 12]} taxa",
            "rank": i + 1, "group": groups[i % 12], "region": regions[i % 12],
            "taxa": [{"common": "Example species", "scientific": "Genus species"}],
            "angle": "Telemetry study of survival and dispersal", "why_now": "Published this week",
            "novelty": "new", "material": "deep" if i < 10 else "moderate",
            "sources": [{"url": f"https://example.org/paper-{i}", "role": "paper", "lang": "en" if i % 3 else "es"},
                        {"url": f"https://example.org/agency-{i}", "role": "agency", "lang": "en"}],
            "photo_queries": ["Genus species", "Example species"],
        })
    return entries


def count_words(markdown: str) -> int:
    return len(re.findall(r"\w+", markdown))
