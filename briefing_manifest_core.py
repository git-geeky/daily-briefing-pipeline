from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from briefing_paths import (
    SECTIONS,
    assembled_manifest_path,
    assembled_markdown_path,
    fragment_manifest_path,
    fragment_path,
    image_cache_dir,
    source_audit_path,
)


HARD_NEWS_SECTIONS = {"part1", "part2", "part3", "part4"}
COUNTERPOINT_REQUIRED_SECTIONS = {"part1", "part2", "part3", "part4"}
WATCH_REQUIRED_SECTIONS = {"part1", "part2", "part3", "part4"}
FOCUS_COMPANY_TERMS = tuple(
    term.strip().lower()
    for term in os.environ.get("BRIEFING_FOCUS_COMPANY_TERMS", "focus company,focus-company").split(",")
    if term.strip()
)


def mentions_focus_company(text: str) -> bool:
    haystack = text.lower()
    return any(term in haystack for term in FOCUS_COMPANY_TERMS)


SECTION_RULES = {
    "part1": {
        "min_count": 4,
        "target_count": 5,
        "max_count": 6,
        "min_words": 1500,
        "soft_min_words": 900,
        "hard_floor": 700,
        "min_sources": 5,
        "min_source_classes": 3,
        "min_section_sources": 2,
        "image_floor": 1,
        "score_floor": 62,
        "expansion_score_floor": 78,
    },
    "part2": {
        "min_count": 6,
        "target_count": 8,
        "max_count": 9,
        "min_words": 1500,
        "soft_min_words": 900,
        "hard_floor": 700,
        "min_sources": 5,
        "min_source_classes": 3,
        "min_section_sources": 2,
        "image_floor": 1,
        "score_floor": 64,
        "expansion_score_floor": 80,
    },
    "part3": {
        "min_count": 2,
        "target_count": 3,
        "max_count": 4,
        "min_words": 1500,
        "soft_min_words": 900,
        "hard_floor": 700,
        "min_sources": 5,
        "min_source_classes": 3,
        "min_section_sources": 2,
        "image_floor": 1,
        "score_floor": 62,
        "expansion_score_floor": 78,
    },
    "part4": {
        "min_count": 2,
        "target_count": 3,
        "max_count": 4,
        "min_words": 1500,
        "soft_min_words": 900,
        "hard_floor": 700,
        "min_sources": 5,
        "min_source_classes": 3,
        "min_section_sources": 2,
        "image_floor": 1,
        "score_floor": 64,
        "expansion_score_floor": 80,
        "reader_value_required": True,
    },
    "humaninterest": {
        "min_count": 1,
        "target_count": 1,
        "max_count": 3,
        "min_words": 1200,
        "soft_min_words": 700,
        "hard_floor": 500,
        "min_sources": 4,
        "min_source_classes": 2,
        "score_floor": 58,
        "expansion_score_floor": 72,
    },
    "updates": {
        "min_count": 0,
        "target_count": 1,
        "max_count": 3,
        "min_words": 800,
        "soft_min_words": 500,
        "hard_floor": 400,
        "min_sources": 3,
        "min_source_classes": 2,
        "score_floor": 56,
        "expansion_score_floor": 70,
        "reader_value_required": True,
    },
    "closing": {
        "min_count": 1,
        "target_count": 1,
        "max_count": 1,
        "min_words": 1200,
        "soft_min_words": 700,
        "hard_floor": 500,
        "min_sources": 4,
        "min_source_classes": 2,
        "score_floor": 58,
    },
}

SECTION_FALLBACK_TITLES = {
    "part1": "PART I - GENERAL NEWS",
    "part2": "PART II - AI NEWS",
    "part3": "PART III - NON-AI TECH NEWS",
    "part4": "PART IV - JAPAN NEWS",
    "humaninterest": "PART V - HUMAN INTEREST",
    "updates": "PART VI - UPDATES",
    "closing": "CLOSING ANALYSIS",
}

WIRE_TOKENS = ("reuters", "ap news", "ap ", "associated press", "afp")
OFFICIAL_TOKENS = (
    ".gov",
    ".mil",
    "official",
    "ministry",
    "department",
    "agency",
    "commission",
    "calendar",
    "schedule",
    "press office",
    "sec",
    "un ",
    "united nations",
    "imf",
    "world bank",
    "bank of japan",
    "office",
)
ANALYSIS_TOKENS = (
    "analysis",
    "csis",
    "cfr",
    "rusi",
    "iiss",
    "brookings",
    "atlantic council",
    "institute",
)
COMMUNITY_TOKENS = ("hacker news", "news.ycombinator.com", "reddit", "x.com", "twitter", "github")
PAYWALLED_TOKENS = ("wsj", "wall street journal", "ft.com", "financial times", "economist", "bloomberg")
EVIDENTIARY_IMAGE_TOKENS = (
    "chart",
    "map",
    "graph",
    "satellite",
    "document",
    "filing",
    "screenshot",
    "calendar",
    "schedule",
    "interface",
    "dashboard",
    "table",
    "diagram",
)
WEAK_IMAGE_TOKENS = ("flag", "logo", "portrait", "headshot")
PLACEHOLDER_IMAGE_TOKENS = (
    "briefing overview",
    "overview",
    "pressure map",
    "pressure points",
    "next-48h",
    "next 48h",
    "watch board",
)
PLACEHOLDER_IMAGE_STEM_SUFFIXES = (
    "-overview",
    "-pressure",
    "-watch",
)
SOURCE_DERIVED_IMAGE_RULES = {
    "source_og",
    "commons_exact_entity_match",
    "phase1_direct",
}
SOURCE_DERIVED_ORIGINS = {
    "source_og",
    "entity_match",
}
BANNED_TOPIC_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcrypto\b", re.IGNORECASE), "crypto"),
    (re.compile(r"\b(?:defi|bitcoin|ethereum|dogecoin|nft)\b", re.IGNORECASE), "cryptocurrency"),
    (re.compile(r"\brobotaxi(?:s|es)?\b", re.IGNORECASE), "robotaxi"),
    (re.compile(r"\bcelebrity\b(?!\s+chef)", re.IGNORECASE), "celebrity"),
    (
        re.compile(
            r"\bfilm\b(?!\s+(?:festival|documentary|documentaries|documentary\s+archive|archive|archives|oral\s+history|industry\s+regulator|canister|grain|stock))",
            re.IGNORECASE,
        ),
        "film industry",
    ),
    (re.compile(r"\btelevision\s+series\b", re.IGNORECASE), "television series"),
    (
        re.compile(
            r"\btv\s+show\b(?!\s+(?:called\s+)?['\"]?(?:news|60\s+minutes|frontline))",
            re.IGNORECASE,
        ),
        "tv show",
    ),
    (re.compile(r"\breality\s+(?:tv\s+)?show\b", re.IGNORECASE), "reality show"),
    (re.compile(r"\bbox\s+office\b", re.IGNORECASE), "box office"),
    (re.compile(r"\bmusic\s+(?:gossip|industry\s+news)\b", re.IGNORECASE), "music gossip"),
)
EDITORIAL_BOILERPLATE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bThat is why this article uses a mixed source stack\b", re.IGNORECASE), "mixed source stack explainer"),
    (re.compile(r"\bThe useful reader posture is disciplined skepticism\b", re.IGNORECASE), "reader posture explainer"),
    (re.compile(r"\bThis briefing also treats weak source access as information\b", re.IGNORECASE), "source access meta explainer"),
    (re.compile(r"\bThe decision rule for escalation is deliberately operational\b", re.IGNORECASE), "escalation rule explainer"),
    (re.compile(r"\bThis also gives editors a clean way to use dissent\b", re.IGNORECASE), "editor dissent explainer"),
    (re.compile(r"\bThe practical consequence is that this article stays deliberately bounded\b", re.IGNORECASE), "boundedness explainer"),
    (re.compile(r"\bPerplexity MCP discovery pass\b", re.IGNORECASE), "Perplexity process narration"),
    (re.compile(r"\bPerplexity-assisted (?:run|path)\b", re.IGNORECASE), "Perplexity process narration"),
    (re.compile(r"\bThat does not make Perplexity itself a source\b", re.IGNORECASE), "tool provenance explainer"),
    (re.compile(r"\bit makes the tool a discovery layer\b", re.IGNORECASE), "tool provenance explainer"),
    (re.compile(r"\bsource-quality decision before drafting\b", re.IGNORECASE), "source process explainer"),
    (re.compile(r"\bSeveral Perplexity leads were broad feeds\b", re.IGNORECASE), "source process explainer"),
    (re.compile(r"\bsource stack therefore mixes the dated lead\b", re.IGNORECASE), "source stack explainer"),
    (re.compile(r"\bA wire or official page establishes the event\b", re.IGNORECASE), "source stack explainer"),
    (re.compile(r"\bthe acceptance bar has to be consequence, not novelty alone\b", re.IGNORECASE), "acceptance-bar explainer"),
    (re.compile(r"\buse broader discovery, then be stricter about what survives\b", re.IGNORECASE), "discovery process explainer"),
    (re.compile(r"\bThe next forty-eight hours are a verification window\b", re.IGNORECASE), "generic watch-window explainer"),
    (re.compile(r"\bmodel-card revision\b", re.IGNORECASE), "cross-domain template residue"),
    (re.compile(r"\bprocurement detail\b", re.IGNORECASE), "cross-domain template residue"),
    (re.compile(r"\bmarket disclosure\b", re.IGNORECASE), "cross-domain template residue"),
    (re.compile(r"\bAI vendors can announce capability faster than customers can audit it\b", re.IGNORECASE), "cross-domain template residue"),
    (re.compile(r"\bCompanies can win contracts faster than users can judge lock-in\b", re.IGNORECASE), "cross-domain template residue"),
    (re.compile(r"\bclinical AI on workflow design\b", re.IGNORECASE), "cross-domain template residue"),
    (re.compile(r"\benterprise cloud contracts on trust\b", re.IGNORECASE), "cross-domain template residue"),
)
HIGH_CONFIDENCE_IMAGE_THRESHOLD = 0.8
LOW_CONFIDENCE_IMAGE_THRESHOLD = 0.75
# Canonical raster image extensions (dotted, lowercase). Single source for the
# fetcher's accepted extensions, the local-raster classifier, and build_pdf's
# render/optimize paths. `.bmp` is INCLUDED: build_pdf embeds raster images via
# Pillow, which reads BMP, so a BMP that reaches the PDF stage must stay
# renderable; the inclusive set is also the safer choice for raster detection.
RASTER_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
HEADER_CROSS_REF_PATTERNS = (
    re.compile(
        r'cross[-\s]reference:\s*[^"\u201c\u201d`\n]*'
        r'["\u201c`]([^"\u201d`\n]+)["\u201d`]',
        re.IGNORECASE,
    ),
    re.compile(
        r"cross[-\s]reference:\s*[^\u2018\u2019'\n]*"
        r"[\u2018']([^\u2019'\n]+)[\u2019']",
        re.IGNORECASE,
    ),
    re.compile(
        r"see\s+part\s+[ivx]+(?:\s*,\s*article\s+\d+)?\s+[\u2013\u2014\-]\s*([^\n.]+)",
        re.IGNORECASE,
    ),
)
PRIMARY_SOURCES_HEADINGS = (
    "### Primary Sources",
    "### Sources",
    "### Source Articles",
    "**Primary Sources**",
    "**Primary Sources**:",
    "**Primary Sources:**",
    "**Sources**",
    "**Sources**:",
    "**Sources:**",
    "**Source Articles**",
    "**Source Articles**:",
    "**Source Articles:**",
)

TOP_LEVEL_TITLE_RE = re.compile(r"(?m)^#\s+(.+)$")
H2_SPLIT_RE = re.compile(r"(?m)^##\s+")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
WATCH_NEXT_HEADING_RE = re.compile(
    r"watch\s+next\s+48\s*(?:h|hr|hrs|hours?)\b",
    re.IGNORECASE,
)

# Native-language source audit: if an article body/title mentions one of the
# region keywords on the left, the Primary Sources appendix should cite at
# least one URL from the corresponding native-language domain on the right.
# The check produces a WARNING (not blocking) — it's a visibility check, not a
# shipment gate. Rationale: SKILL.md mandates native-language research for the
# briefing's top coverage regions, but there's no automated enforcement today.
REGION_KEYWORD_DOMAINS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "iran",
        ("iran", "tehran", "khamenei", "pezeshkian", "irgc", "revolutionary guard", "hormuz"),
        ("irna.ir", "tasnimnews.com", "farsnews.ir", "mehrnews.com", "presstv.ir"),
    ),
    (
        "japan",
        ("japan", "tokyo", "osaka", "kyoto", "takaichi", "boj", "nikkei 225", "diet"),
        ("nikkei.com", "asahi.com", "chunichi.co.jp", "nhk.or.jp", "mainichi.jp", "yomiuri.co.jp", "jiji.com"),
    ),
    (
        "china",
        ("china", "beijing", "shanghai", "xi jinping", "xinhua", "pboc", "communist party", "plenum"),
        ("caixin.com", "36kr.com", "xinhuanet.com", "people.com.cn", "scmp.com", "globaltimes.cn"),
    ),
    (
        "israel",
        ("israel", "netanyahu", "idf", "knesset", "jerusalem", "tel aviv"),
        ("haaretz.co.il", "ynet.co.il", "calcalist.co.il", "timesofisrael.com", "jpost.com"),
    ),
    (
        "arab-mideast",
        ("gaza", "lebanon", "hamas", "hezbollah", "syria", "saudi", "uae", "qatar"),
        ("aljazeera.net", "alarabiya.net", "almanar.com.lb", "almasryalyoum.com"),
    ),
    (
        "korea",
        ("north korea", "south korea", "pyongyang", "seoul", "kim jong un", "dprk"),
        ("yna.co.kr", "chosun.com", "hani.co.kr", "donga.com", "koreaherald.com"),
    ),
    (
        "ukraine",
        ("ukraine", "kyiv", "zelensky", "kharkiv", "donetsk", "luhansk", "odesa"),
        ("pravda.com.ua", "ukrinform.net", "kyivindependent.com", "unian.ua"),
    ),
    (
        "russia",
        ("russia", "moscow", "putin", "kremlin", "lavrov", "medvedev"),
        ("meduza.io", "rbc.ru", "kommersant.ru", "novayagazeta.eu"),
    ),
    (
        "germany",
        ("germany", "berlin", "merz", "bundesbank", "bundestag"),
        ("handelsblatt.com", "sueddeutsche.de", "faz.net", "spiegel.de"),
    ),
)


def audit_native_language_sources(
    title: str,
    body: str,
    sources: list[str],
) -> list[dict]:
    """Return a list of `{region, mentioned, sourced}` findings.

    Only regions that are MENTIONED appear in the list. If a mentioned region
    has zero native-language URLs in the appendix, `sourced=False` — the
    validator surfaces this as a warning (not blocking).
    """
    haystack = f"{title}\n{body}".lower()
    sources_text = " ".join(sources).lower()
    findings: list[dict] = []
    for region, keywords, domains in REGION_KEYWORD_DOMAINS:
        mentioned = any(kw in haystack for kw in keywords)
        if not mentioned:
            continue
        sourced = any(domain in sources_text for domain in domains)
        findings.append(
            {
                "region": region,
                "mentioned": True,
                "sourced": sourced,
                "domains_expected": list(domains[:3]),
            }
        )
    return findings


def detect_editorial_boilerplate(text: str) -> list[str]:
    return [
        label
        for pattern, label in EDITORIAL_BOILERPLATE_RULES
        if pattern.search(text)
    ]


RELATIVE_TIME_RE = re.compile(
    r"\b(today|tomorrow|yesterday|this morning|this afternoon|this evening|last night|tonight)\b",
    re.IGNORECASE,
)
EXPLICIT_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}\b|\b20\d{2}-\d{2}-\d{2}\b"
)
MOVEMENT_SIGNAL_RE = re.compile(
    r"\b(approv|expand|filed|launch|moved|moves|moving|opened|passed|reopen|reset|reprice|rolled out|shift|signed|started|tighten|won|oversight|implementation|procurement|permitting)\w*\b",
    re.IGNORECASE,
)
LOW_VALUE_SIGNAL_RE = re.compile(
    r"\b(lacks?\s+verifiable\s+movement|no\s+new\s+movement|follow-?up\s+only|routine\s+check-?in|quiet\s+day|nothing\s+materially\s+changed)\b",
    re.IGNORECASE,
)


def generate_all_manifests(
    date: str,
    *,
    fragment_texts: dict[str, str] | None = None,
    write: bool = True,
) -> dict[str, dict]:
    prior_items = load_prior_items(date)
    image_metadata = load_image_metadata(date)
    source_audit = load_source_audit(date)
    manifests: dict[str, dict] = {}

    for section in SECTIONS:
        if fragment_texts is not None:
            try:
                text = fragment_texts[section]
            except KeyError as exc:
                raise FileNotFoundError(f"Missing fragment text for section: {section}") from exc
        else:
            path = fragment_path(section, date)
            if not path.exists():
                raise FileNotFoundError(f"Missing fragment file: {path}")
            text = path.read_text(encoding="utf-8")

        manifest = generate_fragment_manifest(
            section,
            date,
            text,
            prior_items=prior_items,
            image_metadata=image_metadata,
            source_audit=source_audit,
        )
        manifests[section] = manifest
        if write:
            fragment_manifest_path(section, date).write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    assembled = build_assembled_manifest(date, manifests, source_audit=source_audit)
    if write:
        assembled_manifest_path(date).write_text(
            json.dumps(assembled, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return manifests


def build_assembled_manifest(date: str, manifests: dict[str, dict], *, source_audit: dict | None = None) -> dict:
    all_articles = [
        item
        for section in SECTIONS
        for item in manifests[section]["items"]
        if not item.get("quality_failure")
    ]
    section_selected_counts = {
        section: sum(1 for item in manifests[section]["items"] if not item.get("quality_failure"))
        for section in SECTION_RULES
    }
    article_scorecards = [
        {
            "section": item["section"],
            "title": item["title"],
            "story_id": item["story_id"],
            "source_count": item["source_count"],
            "source_classes": item["source_classes"],
            "primary_sources": item.get("primary_sources") or [],
            "watch_next_48h_count": len(item["watch_next_48h"]),
            "counterpoint_row_count": item.get("counterpoint_row_count", 0),
            "high_confidence_images": item["image_quality"]["high_confidence_count"],
            "image_quality": item["image_quality"],
            "overall_score": item["score"]["overall"],
            "preflight_status": item.get("preflight", {}).get("status"),
            "discovery_source": item.get("discovery_source"),
            "source_audit_source_ids": item.get("source_audit_source_ids") or [],
            "source_check_status": item.get("source_check_status") or {},
            "source_incorporated": item.get("source_incorporated", False),
            "source_bucket": item.get("source_bucket") or [],
        }
        for item in all_articles
    ]
    total_inline_images = sum(item["image_quality"]["total"] for item in all_articles)
    total_high_confidence_images = sum(
        item["image_quality"]["high_confidence_count"] for item in all_articles
    )
    total_placeholder_images = sum(item["image_quality"]["placeholder_count"] for item in all_articles)
    source_summary = (source_audit or {}).get("summary") or {}
    used_source_ids = sorted(
        {
            source_id
            for item in all_articles
            for source_id in (item.get("source_audit_source_ids") or [])
        }
    )
    return {
        "schema_version": 1,
        "date": date,
        "quota_mode": "adaptive",
        "sections": manifests,
        "summary": {
            "quota_mode": "adaptive",
            "total_articles": len(all_articles),
            "article_titles": [item["title"] for item in all_articles],
            "story_ids": [item["story_id"] for item in all_articles],
            "section_selected_counts": section_selected_counts,
            "focus_company_article_count": sum(
                1
                for item in all_articles
                if mentions_focus_company(item["title"]) or item.get("mentions_focus_company")
            ),
            "hn_origin_count": sum(1 for item in all_articles if item.get("hn_origin")),
            "low_image_article_count": sum(
                1 for item in all_articles if item["image_quality"]["high_confidence_count"] < 1
            ),
            "total_inline_images": total_inline_images,
            "total_high_confidence_images": total_high_confidence_images,
            "total_placeholder_images": total_placeholder_images,
            "quality_failure_count": sum(
                1
                for section in SECTIONS
                for item in manifests[section]["items"]
                if item.get("quality_failure")
            ),
            "source_audit_status": (source_audit or {}).get("status"),
            "source_audit_catalog_count": source_summary.get("catalog_count", 0),
            "source_audit_checked_count": source_summary.get("checked_count", 0),
            "source_audit_failed_after_retry_count": source_summary.get("failed_after_retry_count", 0),
            "source_audit_unavailable_count": source_summary.get("unavailable_count", 0),
            "source_audit_all_sources_checked": bool(source_summary.get("all_sources_checked")),
            "source_audit_used_source_count": len(used_source_ids),
            "source_audit_used_source_ids": used_source_ids,
            "source_audit_incorporated_article_count": sum(
                1 for item in all_articles if item.get("source_incorporated")
            ),
            "article_scorecards": article_scorecards,
        },
    }


def generate_fragment_manifest(
    section: str,
    date: str,
    text: str,
    *,
    prior_items: list[dict] | None = None,
    image_metadata: dict[str, dict] | None = None,
    source_audit: dict | None = None,
) -> dict:
    section_title = normalize_section_title(section, extract_top_level_title(text))
    items = split_items(text)
    prior_items = prior_items or []
    image_metadata = image_metadata or {}

    if section == "header":
        return {
            "schema_version": 1,
            "date": date,
            "section": section,
            "section_title": section_title,
            "items": [],
            "header": build_header_manifest(text),
        }

    manifest_items = [
        build_item_manifest(
            section,
            date,
            heading,
            body,
            prior_items=prior_items,
            image_metadata=image_metadata,
            source_audit=source_audit,
        )
        for heading, body in items
    ]
    return {
        "schema_version": 1,
        "date": date,
        "section": section,
        "section_title": section_title,
        "items": manifest_items,
        "quota_mode": "adaptive",
        "quota": {
            "min_count": SECTION_RULES.get(section, {}).get("min_count"),
            "target_count": SECTION_RULES.get(section, {}).get("target_count"),
            "max_count": SECTION_RULES.get(section, {}).get("max_count"),
            "selected_count": sum(1 for item in manifest_items if not item.get("quality_failure")),
            "quality_failure_count": sum(1 for item in manifest_items if item.get("quality_failure")),
        },
    }


def canonical_section_title(section: str) -> str | None:
    return SECTION_FALLBACK_TITLES.get(section)


def normalize_section_title(section: str, section_title: str | None) -> str:
    title = (section_title or "").strip()
    return title or (canonical_section_title(section) or "")


def build_header_manifest(text: str) -> dict:
    executive_summary = extract_named_block(text, ("## Executive Summary",))
    toc_text = extract_named_block(text, ("### Table of Contents",))
    cross_refs: list[str] = []
    for pattern in HEADER_CROSS_REF_PATTERNS:
        for match in pattern.findall(text):
            cross_refs.append(match.strip().strip('"'))
    cross_refs = dedupe_preserve(cross_refs)
    toc_titles: list[str] = []
    for line in toc_text.splitlines():
        stripped = line.strip()
        match = re.match(r"^\d+\.\s+(.*)$", stripped)
        if match:
            toc_titles.append(match.group(1).strip())
            continue
        bullet = re.match(r"^-\s+(.*)$", stripped)
        if bullet:
            toc_titles.append(bullet.group(1).strip())

    return {
        "executive_summary_word_count": count_words(executive_summary),
        "cross_references": cross_refs,
        "toc_titles": toc_titles,
    }


def build_item_manifest(
    section: str,
    date: str,
    heading: str,
    body: str,
    *,
    prior_items: list[dict],
    image_metadata: dict[str, dict],
    source_audit: dict | None = None,
) -> dict:
    title = normalize_item_title(heading)
    quality_failure = title.lower().startswith("quality failure")
    sources = extract_primary_sources(body)
    source_classes = classify_sources(sources)
    watch_entries = extract_watch_entries(body)
    counterpoint_rows = extract_counterpoint_rows(body)
    images = extract_images(body, image_metadata=image_metadata)
    image_quality = classify_images(images)
    source_block = extract_named_block(body, PRIMARY_SOURCES_HEADINGS)
    body_without_sources = body.replace(source_block, "")
    body_without_sources = IMAGE_RE.sub("", body_without_sources)

    has_body_links = bool(re.search(r"https?://", body_without_sources))
    banned_topics = [
        label for pattern, label in BANNED_TOPIC_RULES if pattern.search(f"{title}\n{body}")
    ]
    editorial_boilerplate = detect_editorial_boilerplate(body)
    relative_time_mentions = RELATIVE_TIME_RE.findall(body)
    has_explicit_jst_date = bool((EXPLICIT_DATE_RE.search(body) and "JST" in body) or valid_briefing_date(date))
    primary_documents = [entry for entry in sources if classify_sources([entry]).count("official")]
    official_watch_sources = []
    for entry in watch_entries:
        match = re.search(r"\(([^)]+)\)", entry)
        if match:
            official_watch_sources.append(match.group(1).strip())
    counterpoint_sources = []
    for row in counterpoint_rows:
        for cell in row[3:5]:
            if cell:
                counterpoint_sources.append(cell)

    needs_second_pass = False
    if sources:
        only_thin_classes = len(set(source_classes)) < 3
        starts_from_thin_anchor = any(
            token in " ".join(sources).lower()
            for token in ("hacker news", "news.ycombinator.com", "ft.com", "wall street journal", "reuters")
        )
        needs_second_pass = starts_from_thin_anchor and only_thin_classes

    prior_story = best_prior_story_match(title, prior_items)
    update_of_prior_story = section == "updates" and prior_story is not None
    story_id = prior_story["story_id"] if prior_story else story_slug(title)
    source_matches = match_sources_to_audit(sources, source_audit or {})
    source_ids = [match["source_id"] for match in source_matches]
    source_check_status = {match["source_id"]: match.get("status") for match in source_matches}
    source_buckets = dedupe_preserve([match.get("bucket") for match in source_matches if match.get("bucket")])
    source_sections_cited = [match.get("section") for match in source_matches if match.get("section")]
    source_incorporated = bool(source_ids)
    discovery_source = "source_audit" if source_incorporated else "supplemental_live_research"
    novelty_score = score_novelty(title, [item["title"] for item in prior_items], update_of_prior_story=update_of_prior_story)
    score = score_story(
        section=section,
        word_count=count_words(body_without_sources),
        source_count=len(sources),
        source_classes=source_classes,
        counterpoint_rows=len(counterpoint_rows),
        watch_entries=len(watch_entries),
        technical_substance_score=score_technical_substance(section, body),
        novelty_score=novelty_score,
    )
    preflight = build_preflight_scorecard(
        section=section,
        title=title,
        body=body,
        quality_failure=quality_failure,
        word_count=count_words(body_without_sources),
        source_count=len(sources),
        source_classes=source_classes,
        counterpoint_rows=len(counterpoint_rows),
        watch_entries=len(watch_entries),
        source_appendix_present=bool(sources),
        image_quality=image_quality,
        banned_topics=banned_topics,
        native_language_audit=audit_native_language_sources(title, body, sources),
        update_of_prior_story=update_of_prior_story,
        score=score,
    )

    return {
        "title": title,
        "section": section,
        "heading": heading,
        "story_id": story_id,
        "prior_story_id": prior_story["story_id"] if prior_story else None,
        "quality_failure": quality_failure,
        "word_count": count_words(body_without_sources),
        "source_appendix_present": bool(sources),
        "source_count": len(sources),
        "primary_sources": sources,
        "source_classes": source_classes,
        "primary_documents": primary_documents,
        "counterpoint_sources": dedupe_preserve(counterpoint_sources),
        "counterpoint_rows": counterpoint_rows,
        "official_next_48h_sources": dedupe_preserve(official_watch_sources),
        "watch_next_48h": watch_entries,
        "image_candidates": images,
        "image_quality": image_quality,
        "hn_origin": any("hacker news" in source.lower() or "news.ycombinator.com" in source.lower() for source in sources)
        or "hacker news" in title.lower(),
        "update_of_prior_story": update_of_prior_story,
        "has_counterpoint_matrix": bool(counterpoint_rows),
        "counterpoint_row_count": len(counterpoint_rows),
        "has_body_links": has_body_links,
        "banned_topics": banned_topics,
        "editorial_boilerplate": editorial_boilerplate,
        "relative_time_mentions": dedupe_preserve([match.lower() for match in relative_time_mentions]),
        "has_exact_jst_date": has_explicit_jst_date,
        "briefing_date_anchor": date,
        "mentions_focus_company": mentions_focus_company(f"{title}\n{body}"),
        "native_language_audit": preflight["native_language_audit"],
        "needs_second_pass": needs_second_pass,
        "discovery_source": discovery_source,
        "source_audit_source_ids": source_ids,
        "source_check_status": source_check_status,
        "source_incorporated": source_incorporated,
        "source_rejection_reason": None,
        "source_bucket": source_buckets,
        "source_sections_cited": source_sections_cited,
        "score": score,
        "preflight": preflight,
    }


def load_prior_items(date: str) -> list[dict]:
    try:
        current = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return []
    prior_date = (current - timedelta(days=1)).strftime("%Y-%m-%d")
    prior_manifest = assembled_manifest_path(prior_date)
    if prior_manifest.exists():
        try:
            data = json.loads(prior_manifest.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError):
            data = {}
        items: list[dict] = []
        for section in SECTIONS:
            section_items = ((data.get("sections") or {}).get(section) or {}).get("items") or []
            for item in section_items:
                if not item.get("quality_failure"):
                    items.append(item)
        if items:
            return items

    prior_path = assembled_markdown_path(prior_date)
    if not prior_path.exists():
        return []
    prior_text = prior_path.read_text(encoding="utf-8")
    return [
        {
            "title": normalize_item_title(heading),
            "story_id": story_slug(normalize_item_title(heading)),
        }
        for heading, _body in split_items(prior_text)
    ]


def load_image_metadata(date: str) -> dict[str, dict]:
    report_path = image_cache_dir(date) / "image-report.json"
    if not report_path.exists():
        return {}
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}

    metadata: dict[str, dict] = {}
    for detail in data.get("details", []):
        local_path = detail.get("local_path")
        if not local_path:
            continue
        metadata[str(local_path)] = {
            "origin_type": detail.get("origin_type"),
            "confidence": detail.get("confidence"),
            "relevance_reason": detail.get("relevance_reason"),
            "accepted_by_rule": detail.get("accepted_by_rule"),
        }
    return metadata


def valid_briefing_date(date: str) -> bool:
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def story_slug(title: str) -> str:
    tokens = sorted(title_token_set(title))
    if not tokens:
        return "story-untitled"
    return "story-" + "-".join(tokens[:6])


def best_prior_story_match(title: str, prior_items: list[dict]) -> dict | None:
    title_tokens = title_token_set(title)
    best_match = None
    best_score = 0.0
    for item in prior_items:
        prior_tokens = title_token_set(item["title"])
        if not prior_tokens:
            continue
        overlap = len(title_tokens & prior_tokens) / max(len(title_tokens | prior_tokens), 1)
        if overlap > best_score:
            best_match = item
            best_score = overlap
    if best_match is None or best_score < 0.55:
        return None
    return best_match


def canonicalize_title(text: str) -> str:
    normalized = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized.lower())
    return normalized.strip()


def titles_match(left: str, right: str) -> bool:
    if left == right:
        return True
    if canonicalize_title(left) == canonicalize_title(right):
        return True
    left_tokens = title_token_set(left)
    right_tokens = title_token_set(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    return overlap >= 0.85


def load_source_audit(date: str) -> dict:
    try:
        path = Path(source_audit_path(date))
    except Exception:
        return {}
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _source_audit_entries(source_audit: dict | None) -> list[dict]:
    if not isinstance(source_audit, dict):
        return []
    return [entry for entry in source_audit.get("sources") or [] if isinstance(entry, dict)]


def _domain_variants(text: str) -> set[str]:
    variants: set[str] = set()
    for url in re.findall(r"https?://[^\s)>\]]+", text):
        parsed = re.match(r"https?://([^/]+)", url)
        if parsed:
            domain = parsed.group(1).lower().removeprefix("www.")
            variants.add(domain)
    return variants


def _source_name_matches(source_line: str, source_name: str) -> bool:
    line = source_line.lower()
    name = source_name.lower()
    if len(name) >= 5 and name in line:
        return True
    leading = re.split(r"[:\-–—]", source_line, maxsplit=1)[0].strip().lower()
    return len(leading) >= 5 and (leading in name or name in leading)


def match_sources_to_audit(sources: list[str], source_audit: dict | None) -> list[dict]:
    entries = _source_audit_entries(source_audit)
    if not entries or not sources:
        return []
    matches: dict[str, dict] = {}
    for source_line in sources:
        domains = _domain_variants(source_line)
        for entry in entries:
            source_id = str(entry.get("source_id") or "")
            if not source_id or source_id in matches:
                continue
            domain = str(entry.get("domain") or "").lower().removeprefix("www.")
            domain_match = bool(domain and any(domain == item or item.endswith("." + domain) for item in domains))
            name_match = _source_name_matches(source_line, str(entry.get("name") or ""))
            if not domain_match and not name_match:
                continue
            matches[source_id] = {
                "source_id": source_id,
                "name": entry.get("name"),
                "status": entry.get("status"),
                "bucket": entry.get("bucket") or entry.get("major_bucket"),
                "section": entry.get("section"),
                "domain": domain,
            }
    return list(matches.values())


def evaluate_reader_value(
    section: str,
    title: str,
    body: str,
    *,
    update_of_prior_story: bool,
    novelty_score: int,
) -> dict:
    required = bool(SECTION_RULES.get(section, {}).get("reader_value_required"))
    haystack = f"{title}\n{body}"
    movement_signal = bool(MOVEMENT_SIGNAL_RE.search(haystack))
    low_value_signal = bool(LOW_VALUE_SIGNAL_RE.search(haystack))
    passes = not low_value_signal and (not required or movement_signal or novelty_score >= 12 or not update_of_prior_story)
    return {
        "required": required,
        "movement_signal": movement_signal,
        "low_value_signal": low_value_signal,
        "passes": passes,
    }


def build_preflight_scorecard(
    *,
    section: str,
    title: str,
    body: str,
    quality_failure: bool,
    word_count: int,
    source_count: int,
    source_classes: list[str],
    counterpoint_rows: int,
    watch_entries: int,
    source_appendix_present: bool,
    image_quality: dict,
    banned_topics: list[str],
    native_language_audit: list[dict],
    update_of_prior_story: bool,
    score: dict,
) -> dict:
    rules = SECTION_RULES.get(section, {})
    reader_value = evaluate_reader_value(
        section,
        title,
        body,
        update_of_prior_story=update_of_prior_story,
        novelty_score=score.get("novelty", 0),
    )
    hard_failures: list[str] = []
    soft_flags: list[str] = []
    if quality_failure:
        status = "quality_failure"
    else:
        if banned_topics:
            hard_failures.append(f"Banned topic match: {', '.join(banned_topics)}")
        if not source_appendix_present:
            hard_failures.append("Missing source appendix")
        if word_count < rules.get("hard_floor", 0):
            hard_failures.append(
                f"Likely word depth below hard floor ({word_count} < {rules.get('hard_floor')})"
            )
        if source_count < rules.get("min_sources", 0):
            hard_failures.append(
                f"Insufficient source count ({source_count} < {rules.get('min_sources')})"
            )
        if len(source_classes) < rules.get("min_source_classes", 1):
            soft_flags.append(
                f"Insufficient source-class mix ({len(source_classes)} < {rules.get('min_source_classes')})"
            )
        if section in COUNTERPOINT_REQUIRED_SECTIONS and counterpoint_rows < 1:
            hard_failures.append("Missing Counterpoint Matrix")
        if section in WATCH_REQUIRED_SECTIONS and watch_entries < 1:
            hard_failures.append("Missing Watch Next 48h")
        if rules.get("reader_value_required") and not reader_value["passes"]:
            hard_failures.append("Reader value gate failed")
        if section in HARD_NEWS_SECTIONS and image_quality.get("total", 0) < rules.get("image_floor", 0):
            soft_flags.append("No drafted inline image support")
        if section in HARD_NEWS_SECTIONS and image_quality.get("all_low_evidence"):
            soft_flags.append("Only low-evidence images are present")
        if any(
            finding.get("mentioned")
            and not finding.get("sourced")
            and finding.get("region") in {"japan", "iran", "ukraine", "china"}
            for finding in native_language_audit
        ):
            soft_flags.append("Region-sensitive story lacks native-language source coverage")
        status = "ready" if not hard_failures else "reject"

    selection_score = score.get("overall", 0)
    if image_quality.get("high_confidence_count", 0) >= rules.get("image_floor", 0):
        selection_score += 4
    if reader_value.get("passes"):
        selection_score += 4
    if any(finding.get("mentioned") and finding.get("sourced") for finding in native_language_audit):
        selection_score += 3

    return {
        "status": status,
        "selection_score": min(100, selection_score),
        "source_class_count": len(source_classes),
        "counterpoint_rows": counterpoint_rows,
        "watch_entries": watch_entries,
        "imageable": image_quality.get("total", 0) > 0,
        "reader_value": reader_value,
        "native_language_audit": native_language_audit,
        "hard_failures": hard_failures,
        "soft_flags": soft_flags,
    }


def select_section_items(section: str, items: list[dict]) -> dict:
    rules = SECTION_RULES.get(section, {})
    min_count = int(rules.get("min_count", 0))
    target_count = int(rules.get("target_count", min_count))
    max_count = int(rules.get("max_count", target_count))
    expansion_score_floor = int(rules.get("expansion_score_floor", rules.get("score_floor", 0)))

    accepted: list[dict] = []
    rejected: list[dict] = []
    quality_failures: list[dict] = []
    viable: list[tuple[int, dict, dict]] = []

    def selection_entry(item: dict, *, index: int, selection_score: int | None = None, reasons: list[str] | None = None) -> dict:
        return {
            "index": index,
            "section": section,
            "title": item["title"],
            "story_id": item["story_id"],
            "selection_score": selection_score,
            "reasons": reasons or [],
            "discovery_source": item.get("discovery_source"),
            "source_audit_source_ids": item.get("source_audit_source_ids") or [],
            "source_check_status": item.get("source_check_status") or {},
            "source_incorporated": item.get("source_incorporated", False),
            "source_rejection_reason": "; ".join(reasons or []) if reasons else None,
            "source_bucket": item.get("source_bucket") or [],
        }

    for index, item in enumerate(items):
        if item.get("quality_failure"):
            quality_failures.append(
                selection_entry(item, index=index, reasons=["Explicit quality failure marker retained as a gap signal"])
            )
            continue

        preflight = item.get("preflight") or {}
        reasons = list(preflight.get("hard_failures") or [])
        adjacent_quality_failure = index + 1 < len(items) and items[index + 1].get("quality_failure")
        if adjacent_quality_failure:
            reasons.append("Quality failure marker follows this candidate")
        rejectable_failure = any(
            token in reason.lower()
            for reason in reasons
            for token in (
                "missing source appendix",
                "word depth below hard floor",
                "insufficient source count",
                "reader value gate failed",
                "quality failure marker follows",
            )
        )
        if preflight.get("status") == "reject" and rejectable_failure:
            rejected.append(
                selection_entry(
                    item,
                    index=index,
                    selection_score=preflight.get("selection_score", item.get("score", {}).get("overall", 0)),
                    reasons=reasons or ["Preflight rejected candidate"],
                )
            )
            continue
        viable.append((index, item, preflight))

    viable.sort(
        key=lambda entry: (
            entry[2].get("selection_score", entry[1].get("score", {}).get("overall", 0)),
            entry[1].get("score", {}).get("overall", 0),
            entry[1].get("word_count", 0),
            entry[1].get("source_count", 0),
        ),
        reverse=True,
    )

    accepted_indices: set[int] = set()
    for index, item, preflight in viable:
        selection_score = int(preflight.get("selection_score", item.get("score", {}).get("overall", 0)))
        if len(accepted) < target_count:
            accepted.append(
                selection_entry(item, index=index, selection_score=selection_score)
            )
            accepted_indices.add(index)
            continue

        if len(accepted) >= max_count:
            rejected.append(
                selection_entry(
                    item,
                    index=index,
                    selection_score=selection_score,
                    reasons=[f"Did not fit max_count {max_count} under adaptive quota"],
                )
            )
            continue

        if selection_score >= expansion_score_floor:
            accepted.append(
                selection_entry(
                    item,
                    index=index,
                    selection_score=selection_score,
                    reasons=[f"Accepted as adaptive expansion above target_count {target_count}"],
                )
            )
            accepted_indices.add(index)
            continue

        rejected.append(
            selection_entry(
                item,
                index=index,
                selection_score=selection_score,
                reasons=[f"Did not clear adaptive expansion score floor {expansion_score_floor}"],
            )
        )

    kept_quality_failures: list[dict] = []
    if len(accepted) < target_count:
        kept_quality_failures = quality_failures
        accepted_indices.update(entry["index"] for entry in kept_quality_failures)

    return {
        "section": section,
        "quota_mode": "adaptive",
        "min_count": min_count,
        "target_count": target_count,
        "max_count": max_count,
        "selected_count": len(accepted),
        "accepted_indices": sorted(accepted_indices),
        "accepted": accepted,
        "rejected": sorted(rejected, key=lambda entry: entry["index"]),
        "quality_failures": kept_quality_failures,
    }


def extract_top_level_title(text: str) -> str:
    match = TOP_LEVEL_TITLE_RE.search(text)
    return match.group(1).strip() if match else ""


def split_items(text: str) -> list[tuple[str, str]]:
    parts = H2_SPLIT_RE.split(text)
    items: list[tuple[str, str]] = []
    for block in parts[1:]:
        heading, _, body = block.partition("\n")
        items.append((heading.strip(), body.strip()))
    return items


def normalize_item_title(heading: str) -> str:
    heading = heading.strip()
    match = re.match(r"^(?:Article|Analysis|Update|Story)\s+\d+:\s*(.+)$", heading)
    if match:
        return match.group(1).strip()
    return heading


def extract_named_block(text: str, headings: tuple[str, ...]) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if any(line.strip().lower() == heading.lower() for heading in headings):
            collected: list[str] = []
            for candidate in lines[index + 1 :]:
                stripped = candidate.strip()
                if stripped.startswith("## ") or stripped.startswith("### "):
                    break
                collected.append(candidate)
            return "\n".join(collected).strip()
    return ""


def extract_primary_sources(text: str) -> list[str]:
    inline_re = re.compile(
        r"\*\*Primary Sources(?:\*\*:|:\*\*)\s*(.+)$",
        re.MULTILINE | re.IGNORECASE,
    )
    inline_match = inline_re.search(text)
    if inline_match:
        inline_text = inline_match.group(1).strip()
        link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
        inline_entries = [
            f"[{m.group(1)}]({m.group(2)})" for m in link_re.finditer(inline_text)
        ]
        if inline_entries:
            return inline_entries

    block = extract_named_block(text, PRIMARY_SOURCES_HEADINGS)
    entries: list[str] = []
    current = ""
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                entries.append(current.strip())
                current = ""
            continue
        if stripped.startswith("- "):
            if current:
                entries.append(current.strip())
            current = stripped[2:].strip()
        elif current:
            current += " " + stripped
    if current:
        entries.append(current.strip())
    return entries


def extract_watch_entries(text: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not WATCH_NEXT_HEADING_RE.search(line):
            continue
        entries: list[str] = []
        for candidate in lines[index + 1 :]:
            stripped = candidate.strip()
            if not stripped:
                if entries:
                    break
                continue
            if stripped.startswith("### ") or stripped.startswith("## "):
                break
            stripped = stripped.removeprefix(">").strip()
            if stripped.startswith("- "):
                entries.append(stripped[2:].strip())
        return entries
    return []


def extract_counterpoint_rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "counterpoint matrix" not in line.lower():
            continue
        table_lines: list[str] = []
        for candidate in lines[index + 1 :]:
            stripped = candidate.strip()
            if not stripped:
                if table_lines:
                    break
                continue
            if not stripped.startswith("|"):
                if table_lines:
                    break
                continue
            table_lines.append(stripped)
        rows: list[list[str]] = []
        for table_line in table_lines:
            if set(table_line.replace("|", "").strip()) == {"-"}:
                continue
            cells = [cell.strip() for cell in table_line.strip("|").split("|")]
            rows.append(cells)
        return rows[1:] if len(rows) > 1 else []
    return []


def extract_images(text: str, *, image_metadata: dict[str, dict] | None = None) -> list[dict]:
    image_metadata = image_metadata or {}
    images = []
    for alt, url in IMAGE_RE.findall(text):
        metadata = image_metadata.get(url.strip(), default_image_metadata(url.strip()))
        images.append(
            {
                "alt": alt.strip(),
                "url": url.strip(),
                "kind": classify_image_kind(alt, url),
                "placeholder": is_placeholder_image(alt, url, metadata),
                "local_svg": is_local_svg_url(url),
                "local_raster": is_local_raster_url(url),
                "source_derived": is_source_derived_image(metadata),
                "origin_type": metadata.get("origin_type"),
                "confidence": metadata.get("confidence"),
                "relevance_reason": metadata.get("relevance_reason"),
                "accepted_by_rule": metadata.get("accepted_by_rule"),
            }
        )
    return images


def classify_image_kind(alt: str, url: str) -> str:
    haystack = f"{alt} {url}".lower()
    if any(token in haystack for token in EVIDENTIARY_IMAGE_TOKENS):
        return "evidentiary"
    if any(token in haystack for token in WEAK_IMAGE_TOKENS):
        return "weak"
    return "other"


def is_local_image_url(url: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", url)) or url.startswith(("/", "\\"))


def is_local_svg_url(url: str) -> bool:
    return is_local_image_url(url) and url.split("?", 1)[0].split("#", 1)[0].lower().endswith(".svg")


def is_local_raster_url(url: str) -> bool:
    if not is_local_image_url(url):
        return False
    clean = url.split("?", 1)[0].split("#", 1)[0].lower()
    return clean.endswith(RASTER_IMAGE_EXTENSIONS)


def is_placeholder_image(alt: str, url: str, metadata: dict | None = None) -> bool:
    metadata = metadata or {}
    clean_url = url.split("?", 1)[0].split("#", 1)[0]
    stem = Path(clean_url).stem.lower()
    haystack = f"{alt} {stem}".lower()
    if stem == "briefing-overview":
        return True
    if is_local_svg_url(url) and any(stem.endswith(suffix) for suffix in PLACEHOLDER_IMAGE_STEM_SUFFIXES):
        return True
    if is_local_svg_url(url) and any(token in haystack for token in PLACEHOLDER_IMAGE_TOKENS):
        return True
    if metadata.get("accepted_by_rule") in {"already_local", "local_inline"} and is_local_svg_url(url):
        return any(token in haystack for token in PLACEHOLDER_IMAGE_TOKENS)
    return False


def is_source_derived_image(metadata: dict | None) -> bool:
    metadata = metadata or {}
    return (
        metadata.get("accepted_by_rule") in SOURCE_DERIVED_IMAGE_RULES
        or metadata.get("origin_type") in SOURCE_DERIVED_ORIGINS
    )


def default_image_metadata(url: str) -> dict:
    if url.startswith(("http://", "https://")):
        return {
            "origin_type": "authored",
            "confidence": 0.95,
            "relevance_reason": "Inline image chosen during drafting",
            "accepted_by_rule": "authored_inline",
        }
    return {
        "origin_type": "authored",
        "confidence": 0.85,
        "relevance_reason": "Local inline image without explicit fetch-report metadata",
        "accepted_by_rule": "local_inline",
    }


def classify_images(images: list[dict]) -> dict:
    seen: set[str] = set()
    duplicate_urls = 0
    evidentiary_count = 0
    weak_count = 0
    high_confidence_count = 0
    low_confidence_count = 0
    low_evidence_count = 0
    placeholder_count = 0
    local_svg_count = 0
    local_raster_count = 0
    source_derived_count = 0
    authored_evidentiary_count = 0
    raw_high_confidence_count = 0
    origins: set[str] = set()
    for image in images:
        url = image["url"]
        if url in seen:
            duplicate_urls += 1
        seen.add(url)
        confidence = float(image.get("confidence") or 0.0)
        if image["kind"] == "evidentiary":
            evidentiary_count += 1
        else:
            low_evidence_count += 1
        if image["kind"] == "weak":
            weak_count += 1
        if image.get("placeholder"):
            placeholder_count += 1
        if image.get("local_svg"):
            local_svg_count += 1
        if image.get("local_raster"):
            local_raster_count += 1
        if image.get("source_derived"):
            source_derived_count += 1
        if image["kind"] == "evidentiary" and not image.get("placeholder"):
            authored_evidentiary_count += 1
        if confidence >= HIGH_CONFIDENCE_IMAGE_THRESHOLD:
            raw_high_confidence_count += 1
            if not image.get("placeholder") and image["kind"] != "weak":
                high_confidence_count += 1
        if confidence < LOW_CONFIDENCE_IMAGE_THRESHOLD:
            low_confidence_count += 1
        if image.get("origin_type"):
            origins.add(str(image["origin_type"]))
    return {
        "total": len(images),
        "evidentiary_count": evidentiary_count,
        "weak_count": weak_count,
        "duplicate_urls": duplicate_urls,
        "high_confidence_count": high_confidence_count,
        "raw_high_confidence_count": raw_high_confidence_count,
        "low_confidence_count": low_confidence_count,
        "low_evidence_count": low_evidence_count,
        "placeholder_count": placeholder_count,
        "local_svg_count": local_svg_count,
        "local_raster_count": local_raster_count,
        "source_derived_count": source_derived_count,
        "authored_evidentiary_count": authored_evidentiary_count,
        "all_placeholder": bool(images) and placeholder_count == len(images),
        "all_low_evidence": bool(images) and evidentiary_count == 0,
        "origin_types": sorted(origins),
    }


def classify_sources(entries: list[str]) -> list[str]:
    classes: set[str] = set()
    for entry in entries:
        lower = entry.lower()
        matched = False
        if any(token in lower for token in WIRE_TOKENS):
            classes.add("wire")
            matched = True
        if any(token in lower for token in OFFICIAL_TOKENS):
            classes.add("official")
            matched = True
        if any(token in lower for token in ANALYSIS_TOKENS):
            classes.add("analysis")
            matched = True
        if any(token in lower for token in COMMUNITY_TOKENS):
            classes.add("community")
            matched = True
        if any(token in lower for token in PAYWALLED_TOKENS):
            classes.add("paywalled")
            matched = True
        if not matched:
            classes.add("reporting")
    return sorted(classes)


def score_technical_substance(section: str, text: str) -> int:
    lower = text.lower()
    if section not in {"part2", "part3"}:
        return 12
    keywords = (
        "inference",
        "benchmark",
        "interconnect",
        "export control",
        "yield",
        "grid",
        "latency",
        "supply chain",
        "wafer",
        "throughput",
    )
    hits = sum(1 for term in keywords if term in lower)
    return min(20, 8 + hits * 2)


def score_novelty(title: str, prior_titles: list[str], *, update_of_prior_story: bool) -> int:
    if not prior_titles:
        return 18
    title_tokens = title_token_set(title)
    overlap = 0.0
    for prior_title in prior_titles:
        prior_tokens = title_token_set(prior_title)
        if not prior_tokens:
            continue
        score = len(title_tokens & prior_tokens) / max(len(title_tokens | prior_tokens), 1)
        overlap = max(overlap, score)
    if update_of_prior_story:
        return 14
    if overlap >= 0.7:
        return 4
    if overlap >= 0.45:
        return 8
    return 18


def score_story(
    *,
    section: str,
    word_count: int,
    source_count: int,
    source_classes: list[str],
    counterpoint_rows: int,
    watch_entries: int,
    technical_substance_score: int,
    novelty_score: int,
) -> dict:
    significance = min(20, max(6, word_count // 120))
    source_depth = min(20, source_count * 2 + len(source_classes) * 2)
    counterpoint = min(20, 6 + counterpoint_rows * 4) if section in COUNTERPOINT_REQUIRED_SECTIONS else 16
    next_48h = min(20, 6 + watch_entries * 4) if section in WATCH_REQUIRED_SECTIONS else 16
    overall = significance + source_depth + counterpoint + technical_substance_score + next_48h + novelty_score
    return {
        "overall": min(100, overall),
        "significance": significance,
        "source_depth": source_depth,
        "counterpoint": counterpoint,
        "technical_substance": technical_substance_score,
        "next_48h": next_48h,
        "novelty": novelty_score,
    }


def title_token_set(title: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", title.lower())
    return {token for token in tokens if len(token) > 2}


def count_words(text: str) -> int:
    cleaned = re.sub(r"https?://\S+", " ", text)
    cleaned = IMAGE_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("|", " ")
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", cleaned))


def dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out

