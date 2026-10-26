from __future__ import annotations

import json
import os
import re
import sys
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

_SHARED_DIR = str(Path(__file__).resolve().parent)
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)
import edition_core  # noqa: E402

EDITION = edition_core.load_edition()
HARD_NEWS_SECTIONS = set(EDITION.hard_news_sections)
COUNTERPOINT_REQUIRED_SECTIONS = set(EDITION.counterpoint_required_sections)
WATCH_REQUIRED_SECTIONS = set(EDITION.watch_required_sections)
FOCUS_COMPANY_TERMS = tuple(
    term.strip().lower()
    for term in os.environ.get("BRIEFING_FOCUS_COMPANY_TERMS", "focus company,focus-company").split(",")
    if term.strip()
)


def mentions_focus_company(text: str) -> bool:
    haystack = text.lower()
    return any(term in haystack for term in FOCUS_COMPANY_TERMS)


# Per-section quotas, word bands and source floors live in editions/<edition>.json
# (editions/daily.json reproduces the historical constants; pinned by tests).
SECTION_RULES = EDITION.section_rules
SECTION_FALLBACK_TITLES = EDITION.section_titles

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
# A banned *topic* is something the briefing covers, not a word that happens to appear.
# Production evidence (2026-06-08 recovery): validators forced accurate technical terms
# ("Crypto Subsystem", thin-film, ransomware-payment Bitcoin mentions) to be rewritten.
# A hit now counts only when it is in the title, in the lede, or repeated, and it is
# discarded when it sits inside a known technical/security context.
BANNED_TOPIC_MIN_BODY_MENTIONS = 3
BANNED_TOPIC_LEDE_CHARS = 600
_I = re.IGNORECASE
BANNED_TOPIC_ALLOW_SPANS: dict[str, tuple[re.Pattern[str], ...]] = {
    "crypto": (
        re.compile(r"(?:kernel|linux|post[-\s]quantum|pqc|quantum[-\s]safe|openssl|hardware|cipher|nist|tls)\s+crypto\b", _I),
        re.compile(
            r"\bcrypto[-\s](?:agility|agile|library|libraries|module|modules|api|apis|subsystem|policy|primitive|"
            r"primitives|stack|accelerator|engine|extension|extensions|algorithm|algorithms|standard|standards|"
            r"migration|inventory|cbom|offload|hardware)\b",
            _I,
        ),
    ),
    "film industry": (
        re.compile(
            r"(?:thin|dry|photoresist|polymer|plastic|packaging|stretch|window|protective|optical|capacitor|solar|"
            r"wafer|oxide|nitride|metal|conductive|adhesive|barrier|dielectric|resist|laminate|carbon|graphene|"
            r"nano|bio|cling|shrink)[-\s]film\b",
            _I,
        ),
        re.compile(
            r"\bfilm[-\s](?:capacitors?|deposition|transistors?|stack|layers?|thickness|coatings?|solar|"
            r"resistors?|lithography|photoresist|etch(?:ing)?|substrates?|bulk|acoustic|stress)\b",
            _I,
        ),
    ),
}
# Security reporting often names the payment rail; treat as context, not coverage.
BANNED_TOPIC_ALLOW_WINDOWS: dict[str, tuple[re.Pattern[str], int]] = {
    "cryptocurrency": (
        re.compile(
            r"ransom|extort|decrypt|threat actor|malware|botnet|breach|phishing|sanction|indictment|"
            r"seiz|forfeit|money[-\s]laundering|darknet|stolen funds|wallet address",
            _I,
        ),
        200,
    ),
}


def _lede_of(body: str) -> str:
    cleaned = IMAGE_RE.sub(" ", body)
    return cleaned.strip()[:BANNED_TOPIC_LEDE_CHARS]


def _rule_hits(pattern: re.Pattern[str], label: str, text: str) -> int:
    spans = [
        (m.start(), m.end())
        for allow in BANNED_TOPIC_ALLOW_SPANS.get(label, ())
        for m in allow.finditer(text)
    ]
    window = BANNED_TOPIC_ALLOW_WINDOWS.get(label)
    hits = 0
    for match in pattern.finditer(text):
        start, end = match.span()
        if any(a <= start and end <= b for a, b in spans):
            continue
        if window is not None:
            context = text[max(0, start - window[1]) : end + window[1]]
            if window[0].search(context):
                continue
        hits += 1
    return hits


def find_banned_topics(title: str, body: str, *, edition=None) -> list[str]:
    """Return labels of banned topics the article actually covers (see notes above)."""
    edition = edition or EDITION
    allowed = None if edition.banned_topic_labels is None else set(edition.banned_topic_labels)
    rules: list[tuple[re.Pattern[str], str, int]] = [
        (pattern, label, BANNED_TOPIC_MIN_BODY_MENTIONS)
        for pattern, label in BANNED_TOPIC_RULES
        if allowed is None or label in allowed
    ]
    for extra in edition.extra_banned_topic_rules:
        rules.append(
            (
                re.compile(extra["pattern"], re.IGNORECASE),
                str(extra.get("label") or "banned topic"),
                int(extra.get("min_body_mentions", BANNED_TOPIC_MIN_BODY_MENTIONS)),
            )
        )
    lede = _lede_of(body)
    found: list[str] = []
    for pattern, label, min_mentions in rules:
        if label in found:
            continue
        if _rule_hits(pattern, label, title):
            found.append(label)
        elif _rule_hits(pattern, label, lede) or _rule_hits(pattern, label, body) >= min_mentions:
            found.append(label)
    return found


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

_QUOTE_PAIRS = {'"': '"', "\u201c": "\u201d", "`": "`", "'": "'", "\u2018": "\u2019"}
_CROSS_REF_LINE_RE = re.compile(r"cross[-\s]reference:\s*(?P<rest>.+)$", re.IGNORECASE)
_CROSS_REF_PART_PREFIX_RE = re.compile(
    r"^(?:part\s+[ivx]+(?:\s*,\s*article\s+\d+)?\s*[-\u2013\u2014:]\s*)?(?P<title>.*)$", re.IGNORECASE
)
_NARRATIVE_CROSS_REF_RE = HEADER_CROSS_REF_PATTERNS[2]


def _cross_reference_title(rest: str) -> str | None:
    """Parse the quoted title after ``Cross-reference:``.

    The title may legitimately contain apostrophes or the other quote style
    ("Trump's Beijing Deal"). Production hit this on 2026-06-08: the single-quote
    pattern matched between two apostrophes inside a double-quoted title and
    produced a bogus reference, so the validator forced a different article into
    the header. Parse by opener -> last matching closer on the line instead.
    """
    candidate = _CROSS_REF_PART_PREFIX_RE.match(rest.strip()).group("title").strip()
    candidate = candidate.strip("*_ ")
    if not candidate or candidate[0] not in _QUOTE_PAIRS:
        return None
    opener, closer = candidate[0], _QUOTE_PAIRS[candidate[0]]
    match = re.match(
        rf"^{re.escape(opener)}(?P<t>.+){re.escape(closer)}[\s.)\]*_]*$", candidate
    )
    if match:
        return match.group("t").strip() or None
    # Trailing prose after the closing quote: fall back to the first closer.
    end = candidate.find(closer, 1)
    return candidate[1:end].strip() if end > 1 else None


def extract_cross_references(text: str) -> list[str]:
    refs: list[str] = []
    for line in text.splitlines():
        found = _CROSS_REF_LINE_RE.search(line)
        if found:
            title = _cross_reference_title(found.group("rest"))
            if title:
                refs.append(title)
                continue
        narrative = _NARRATIVE_CROSS_REF_RE.search(line)
        if narrative:
            refs.append(narrative.group(1).strip().rstrip(".").strip('"'))
    return dedupe_preserve(refs)


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
            "bottom_line": item.get("bottom_line") or "",
            "confidence_label": item.get("confidence_label") or "",
            "lede": item.get("lede") or "",
            "word_count": item.get("word_count", 0),
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
    cross_refs = extract_cross_references(text)
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

    has_body_links = bool(re.search(r"https?://", strip_watch_block(body_without_sources)))
    banned_topics = find_banned_topics(title, body)
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

    distinct_source_domains = {
        registrable_domain(url) for entry in sources for url in _URL_IN_ENTRY_RE.findall(entry)
    }
    needs_second_pass = False
    if sources:
        only_thin_classes = len(set(source_classes)) < 3 and len(distinct_source_domains) < 5
        starts_from_thin_anchor = any(
            token in " ".join(sources).lower()
            for token in ("hacker news", "news.ycombinator.com", "ft.com", "wall street journal", "reuters")
        )
        needs_second_pass = starts_from_thin_anchor and only_thin_classes

    marker = STORY_MARKER_RE.search(body)
    prior_story = best_prior_story_match(title, prior_items)
    if marker and marker.group("prior"):
        declared = next((i for i in prior_items if str(i.get("story_id")) == marker.group("prior")), None)
        prior_story = declared or prior_story
    update_of_prior_story = section == "updates" and prior_story is not None
    if marker:
        story_id = marker.group("id")
    else:
        story_id = prior_story["story_id"] if prior_story else story_slug(title)
    source_matches = match_sources_to_audit(sources, source_audit or {})
    source_ids = [match["source_id"] for match in source_matches]
    source_check_status = {match["source_id"]: match.get("status") for match in source_matches}
    source_buckets = dedupe_preserve([match.get("bucket") for match in source_matches if match.get("bucket")])
    source_sections_cited = [
        tag for match in source_matches for tag in (match.get("credit_sections") or ([match["section"]] if match.get("section") else []))
    ]
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
        unique_domains=len(distinct_source_domains),
        primary_documents=len(primary_documents),
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

    bottom_line_match = BOTTOM_LINE_RE.search(body)
    confidence_match = CONFIDENCE_LABEL_RE.search(body)
    scientific_names = dedupe_preserve([m.group("name") for m in SCIENTIFIC_NAME_RE.finditer(body)])
    required_blocks = evaluate_required_blocks(section, body)

    return {
        "title": title,
        "section": section,
        "heading": heading,
        "story_id": story_id,
        "bottom_line": bottom_line_match.group("text").strip() if bottom_line_match else "",
        "confidence_label": confidence_match.group("label").capitalize() if confidence_match else "",
        "lede": extract_lede(body),
        "scientific_names": scientific_names,
        "doi_count": len(set(DOI_RE.findall(body))),
        "image_license_lines": len(IMAGE_LICENSE_RE.findall(" ".join(img["alt"] for img in images))),
        "missing_required_blocks": required_blocks,
        "distinct_source_domains": sorted(d for d in distinct_source_domains if d),
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


PRIOR_LOOKBACK_DAYS = 4
PRIOR_ITEM_LIMIT_DAYS = 2  # merge up to this many *existing* prior days


def _load_prior_day_items(prior_date: str) -> list[dict]:
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


def load_prior_items(date: str) -> list[dict]:
    """Items from the most recent prior briefing days that exist.

    Looking only at date-1 meant a missed day (or a Monday after a weekend) silently
    disabled novelty scoring and Updates detection.
    """
    try:
        current = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return []
    merged: list[dict] = []
    seen_ids: set[str] = set()
    days_found = 0
    for offset in range(1, PRIOR_LOOKBACK_DAYS + 1):
        prior_date = (current - timedelta(days=offset)).strftime("%Y-%m-%d")
        items = _load_prior_day_items(prior_date)
        if not items:
            continue
        days_found += 1
        for item in items:
            key = str(item.get("story_id") or item.get("title"))
            if key in seen_ids:
                continue
            seen_ids.add(key)
            merged.append(item)
        if days_found >= PRIOR_ITEM_LIMIT_DAYS:
            break
    return merged


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


# Drafters may declare the story identity explicitly (the slate stage assigns it):
#   <!-- story: iran-hormuz-talks; prior: iran-hormuz-incident -->
# Title-token matching cannot see that a retitled follow-up is the same story.
STORY_MARKER_RE = re.compile(
    r"<!--\s*story:\s*(?P<id>[A-Za-z0-9_.:\-]+)(?:\s*;\s*prior:\s*(?P<prior>[A-Za-z0-9_.:\-]+))?\s*-->"
)
STORY_MARKER_LINE_RE = re.compile(r"(?m)^[ \t]*" + STORY_MARKER_RE.pattern + r"[ \t]*\n?")


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
                "credit_sections": list(entry.get("credit_sections") or ([entry["section"]] if entry.get("section") else [])),
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


def evaluate_item_rules(
    section: str,
    item: dict,
    rules: dict,
    *,
    audit_available: bool = True,
    edition=None,
) -> dict[str, list]:
    """Per-article quality rules shared by the full validator and the per-fragment validator.

    Keeping this in one place is deliberate: the two validators used to carry parallel copies
    and drifted (a fragment passed early validation and then failed the final gate).
    ``audit_available`` is False before the daily source audit exists; the audit-dependent
    rules are skipped rather than failed in that case.
    """
    edition = edition or EDITION
    blocking: list[str] = []
    nonblocking: list[str] = []
    warnings: list[str] = []
    low_image: list[dict] = []
    prefix = f'{section}/{item["title"]}'
    if section in COUNTERPOINT_REQUIRED_SECTIONS and not item["has_counterpoint_matrix"]:
        blocking.append(f"{prefix}: missing Counterpoint Matrix")
    if section in WATCH_REQUIRED_SECTIONS and not item["watch_next_48h"]:
        blocking.append(f"{prefix}: missing Watch Next 48h block")
    if not item["source_appendix_present"]:
        blocking.append(f"{prefix}: missing source appendix")
    soft_min = rules.get("soft_min_words", rules["min_words"])
    hard_floor = rules.get("hard_floor", soft_min)
    if item["word_count"] < hard_floor:
        blocking.append(
            f'{prefix}: article is below the hard word floor '
            f'({item["word_count"]} < {hard_floor}); target is {rules["min_words"]}'
        )
    elif item["word_count"] < soft_min:
        nonblocking.append(
            f'{prefix}: article is below the soft word threshold but above the hard floor '
            f'({item["word_count"]} < {soft_min}, >= {hard_floor})'
        )
    elif item["word_count"] < rules["min_words"]:
        warnings.append(
            f'{prefix}: article is below the target word count but within the soft band '
            f'({item["word_count"]} < {rules["min_words"]}, >= {soft_min})'
        )
    if item["source_count"] < rules["min_sources"]:
        blocking.append(
            f'{prefix}: article is below the source threshold ({item["source_count"]} < {rules["min_sources"]})'
        )
    if audit_available and not item.get("source_incorporated"):
        blocking.append(f"{prefix}: cites no source checked in the daily source-audit catalog")
    min_section_sources = rules.get("min_section_sources", 0)
    if audit_available and min_section_sources:
        section_pool_hits = sum(
            1 for tag in item.get("source_sections_cited", []) if tag == section
        )
        if section_pool_hits < min_section_sources:
            blocking.append(
                f"{prefix}: cites only {section_pool_hits} sources from the {section}-tagged "
                f"catalog pool (minimum is {min_section_sources}); open SOURCES.md and pull "
                f"from the dedicated section list — TIER 0 wires and cross-section sources do not count"
            )
    for label in (item.get("missing_required_blocks") or {}).get("blocking", []):
        blocking.append(f"{prefix}: missing required block: {label}")
    for label in (item.get("missing_required_blocks") or {}).get("warning", []):
        warnings.append(f"{prefix}: missing recommended block: {label}")
    if edition.image_mode != "single" and rules.get("image_floor"):
        per_min = int(edition.images.get("per_article_min", 0) or 0)
        per_max = int(edition.images.get("per_article_max", 0) or 0)
        image_total = item["image_quality"]["total"]
        if per_min and item["image_quality"]["high_confidence_count"] < per_min:
            warnings.append(f"{prefix}: has fewer than {per_min} validated photograph(s)")
            low_image.append(
                {
                    "section": section,
                    "title": item["title"],
                    "story_id": item["story_id"],
                    "high_confidence_images": item["image_quality"]["high_confidence_count"],
                }
            )
        if per_max and image_total > per_max:
            blocking.append(
                f"{prefix}: embeds {image_total} images; this edition allows at most {per_max} per article"
            )
        if edition.images.get("require_license_line") and image_total:
            unlicensed = [
                img["alt"][:60] for img in item.get("image_candidates", []) if not IMAGE_LICENSE_RE.search(img["alt"])
            ]
            if unlicensed:
                blocking.append(
                    f"{prefix}: {len(unlicensed)} image caption(s) carry no licence/credit "
                    f"(CC BY / CC0 / public domain / press use ...): {unlicensed[0]!r}"
                )
    if item["banned_topics"]:
        blocking.append(f"{prefix}: contains banned topic(s): {', '.join(item['banned_topics'])}")
    if item.get("editorial_boilerplate"):
        blocking.append(
            f"{prefix}: contains repeated editorial process boilerplate "
            f"({', '.join(item['editorial_boilerplate'])})"
        )
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
        warnings.append(
            f"{prefix}: contains {item['image_quality']['placeholder_count']} placeholder image card(s)"
        )
    if section in HARD_NEWS_SECTIONS:
        for finding in item.get("native_language_audit", []):
            if finding.get("mentioned") and not finding.get("sourced"):
                region = finding["region"]
                expected = ", ".join(finding.get("domains_expected", []))
                warnings.append(
                    f"{prefix}: mentions {region}-region story but cites no native-language source (expected one of: {expected})"
                )
    if section in HARD_NEWS_SECTIONS and item["image_quality"]["all_low_evidence"]:
        warnings.append(f"{prefix}: images are present but all are low-evidence for the article")
    if item["score"]["overall"] < 60:
        warnings.append(f'{prefix}: story score is low ({item["score"]["overall"]})')
    return {"blocking": blocking, "nonblocking": nonblocking, "warnings": warnings, "low_image": low_image}


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
            len(entry[1].get("source_classes") or []),
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


def strip_watch_block(text: str) -> str:
    """Remove the ``Watch Next 48h`` block; the spec requires a source URL in each bullet."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not WATCH_NEXT_HEADING_RE.search(line):
            continue
        end = index + 1
        seen_entry = False
        while end < len(lines):
            stripped = lines[end].strip()
            if not stripped:
                if seen_entry:
                    break
            elif stripped.startswith(("## ", "### ")):
                break
            else:
                seen_entry = seen_entry or stripped.removeprefix(">").strip().startswith("- ")
            end += 1
        return "\n".join(lines[:index] + lines[end:])
    return text


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


def _markdown_tables(lines: list[str]) -> list[tuple[int, list[str]]]:
    tables: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start = 0
    for index, line in enumerate(lines + [""]):
        stripped = line.strip()
        if stripped.startswith("|"):
            if not current:
                start = index
            current.append(stripped)
        elif current:
            tables.append((start, current))
            current = []
    return tables


def _table_rows(table_lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for table_line in table_lines:
        if set(table_line.replace("|", "").replace(":", "").strip()) <= {"-", " "}:
            continue
        rows.append([cell.strip() for cell in table_line.strip("|").split("|")])
    return rows


def extract_counterpoint_rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    # Subagents emit many heading variants ("Source-bias assessment & counterpoints", ...).
    # The 5-column schema is the real contract, so recognise the table by its header too.
    for _start, table_lines in _markdown_tables(lines):
        rows = _table_rows(table_lines)
        if not rows:
            continue
        header = " ".join(rows[0]).lower()
        if "interested party" in header and ("incentive" in header or "counterpoint" in header):
            return rows[1:] if len(rows) > 1 else []
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


BOTTOM_LINE_RE = re.compile(r"(?im)^[ \t>]*\*\*bottom line:?\*\*:?[ \t]*(?P<text>.+)$")
CONFIDENCE_LABEL_RE = re.compile(r"(?im)^[ \t>]*\*\*confidence:?\*\*:?[ \t]*(?P<label>high|medium|low)\b(?P<rest>.*)$")
SCIENTIFIC_NAME_RE = re.compile(r"(?<![\w*])\*(?P<name>[A-Z][a-z]{2,}(?: [a-z]{3,}){1,2})\*(?![\w*])")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)\]>\"'|]+")
IMAGE_LICENSE_RE = re.compile(
    r"(?i)\b(?:CC[ -]BY(?:-SA|-NC|-ND)*(?:[ -]\d\.\d)?|CC0|public domain|PD[- ]?(?:US|USGov|old|NASA|textlogo)?|"
    r"open access|press (?:kit|use|release|image)|used with permission|licen[cs]ed|no known copyright|"
    r"u\.?s\.? (?:fish and wildlife|geological survey|government)|NOAA|NASA)\b"
)


def extract_lede(body: str, *, limit: int = 600) -> str:
    """First substantive prose paragraph (skips images, bottom-line/confidence lines, headings)."""
    for block in re.split(r"\n\s*\n", body):
        text = block.strip()
        if not text or text.startswith(("#", "|", "![", ">", "<!--", "- ", "*Rank", "*Taxa")):
            continue
        if BOTTOM_LINE_RE.match(text) or CONFIDENCE_LABEL_RE.match(text):
            continue
        return re.sub(r"\s+", " ", text)[:limit]
    return ""


def evaluate_required_blocks(section: str, body: str, *, edition=None) -> dict[str, list[str]]:
    """Check the edition's per-section required blocks (taxonomy card, study card, ...).

    Each block is ``{"id", "label", "severity", "any_of": [regex...], "min_matches": n}``.
    """
    edition = edition or EDITION
    result: dict[str, list[str]] = {"blocking": [], "warning": []}
    for block in edition.required_blocks.get(section, ()):
        patterns = block.get("any_of") or []
        minimum = int(block.get("min_matches", 1))
        matches = sum(len(re.findall(pattern, body, re.IGNORECASE | re.MULTILINE)) for pattern in patterns)
        if matches < minimum:
            severity = "blocking" if block.get("severity") == "blocking" else "warning"
            result[severity].append(str(block.get("label") or block.get("id") or "required block"))
    return result


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


# Source classes are decided from the cited URL's registrable domain first. The old
# substring matching on free text mis-tagged ``microsoft.com`` as paywalled (it
# contains "ft.com"), "Krebs on Security" as official ("sec"), ``linux.com`` as
# community ("x.com") and any line containing "heap" as wire ("ap "). Token matching
# is kept only as a word-boundary fallback for entries that carry no URL.
_WIRE_DOMAINS = frozenset({
    "reuters.com", "apnews.com", "afp.com", "kyodonews.net", "english.kyodonews.net", "yna.co.kr",
    "en.yna.co.kr", "aa.com.tr", "efe.com", "dpa.com", "tass.com", "xinhuanet.com", "english.news.cn",
})
_PAYWALLED_DOMAINS = frozenset({
    "wsj.com", "ft.com", "economist.com", "bloomberg.com", "nytimes.com", "washingtonpost.com",
    "theinformation.com", "stratechery.com", "asia.nikkei.com", "nikkei.com", "caixin.com", "haaretz.com",
    "thewirechina.com", "thecapitolforum.com", "semianalysis.com",
})
_COMMUNITY_DOMAINS = frozenset({
    "news.ycombinator.com", "reddit.com", "old.reddit.com", "x.com", "twitter.com", "github.com",
    "lesswrong.com", "alignmentforum.org", "forum.effectivealtruism.org", "lobste.rs", "stackoverflow.com",
    "weibo.com", "bsky.app", "mastodon.social", "inaturalist.org",
})
_ANALYSIS_DOMAINS = frozenset({
    "csis.org", "cfr.org", "brookings.edu", "rand.org", "iiss.org", "carnegieendowment.org",
    "atlanticcouncil.org", "rusi.org", "chathamhouse.org", "piie.com", "aei.org", "cato.org",
    "crisisgroup.org", "sipri.org", "cset.georgetown.edu", "bruegel.org", "lowyinstitute.org",
    "warontherocks.com", "foreignaffairs.com", "foreignpolicy.com", "lawfaremedia.org", "justsecurity.org",
    "epoch.ai", "artificialanalysis.ai", "semianalysis.com", "stratfor.com", "worldview.stratfor.com",
})
_RESEARCH_DOMAINS = frozenset({
    "doi.org", "dx.doi.org", "arxiv.org", "biorxiv.org", "medrxiv.org", "ecoevorxiv.org", "nature.com",
    "science.org", "sciencedirect.com", "springer.com", "link.springer.com", "wiley.com",
    "onlinelibrary.wiley.com", "cell.com", "pnas.org", "plos.org", "journals.plos.org", "elifesciences.org",
    "royalsocietypublishing.org", "academic.oup.com", "cambridge.org", "frontiersin.org", "mdpi.com",
    "zenodo.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "researchgate.net", "jstage.jst.go.jp",
    "agupubs.onlinelibrary.wiley.com", "peerj.com", "besjournals.onlinelibrary.wiley.com",
})
_DATA_DOMAINS = frozenset({
    "gbif.org", "iucnredlist.org", "cites.org", "speciesplus.net", "wahis.woah.org", "movebank.org",
    "ebird.org", "fred.stlouisfed.org", "ourworldindata.org", "data.worldbank.org", "tradingeconomics.com",
    "acleddata.com", "reliefweb.int", "obis.org", "worldwildlife.org", "fishbase.se", "fishbase.org",
    "catalogueoflife.org", "marinespecies.org", "birdlife.org", "datazone.birdlife.org", "inaturalist.org",
})
_OFFICIAL_SUFFIXES = (
    ".gov", ".mil", ".gov.uk", ".gov.au", ".gov.cn", ".gov.in", ".gov.br", ".gov.za", ".gov.tw", ".gov.kr",
    ".go.jp", ".go.kr", ".go.th", ".go.id", ".go.ke", ".go.tz", ".go.ug", ".gouv.fr", ".gob.mx", ".gob.es",
    ".gob.ar", ".gob.cl", ".gob.pe", ".gov.sg", ".govt.nz", ".gc.ca", ".europa.eu", ".int", ".un.org",
)
_OFFICIAL_DOMAINS = frozenset({
    "un.org", "imf.org", "worldbank.org", "oecd.org", "wto.org", "bis.org", "boj.or.jp", "ecb.europa.eu",
    "federalreserve.gov", "who.int", "fao.org", "woah.org", "ipbes.net", "cbd.int", "cites.org",
})
_OFFICIAL_HOST_RE = re.compile(
    r"(?:^|\.)(?:gov|gouv|gob|govt|go|gc|admin|bund|regjeringen)\.[a-z]{2,3}$"
    r"|(?:^|\.)(?:pref|city|town|vill)\.[a-z\-]+\.(?:lg\.)?jp$|\.lg\.jp$"
)
_WIRE_TOKEN_RE = re.compile(r"\b(?:reuters|associated press|ap news|afp)\b", re.IGNORECASE)
_OFFICIAL_TOKEN_RE = re.compile(
    r"\b(?:ministry|department|agency|commission|press office|united nations|imf|world bank|bank of japan|"
    r"official (?:site|schedule|calendar)|government|parliament|congress|sec filing|sec 8-k)\b",
    re.IGNORECASE,
)
_ANALYSIS_TOKEN_RE = re.compile(
    r"\b(?:csis|cfr|rusi|iiss|brookings|atlantic council|think tank|institute)\b", re.IGNORECASE
)
_COMMUNITY_TOKEN_RE = re.compile(r"\b(?:hacker news|reddit|twitter|github)\b", re.IGNORECASE)
_PAYWALLED_TOKEN_RE = re.compile(
    r"\b(?:wsj|wall street journal|financial times|the economist|bloomberg)\b", re.IGNORECASE
)
_URL_IN_ENTRY_RE = re.compile(r"https?://[^\s)>\]]+")


def registrable_domain(url: str) -> str:
    host = re.sub(r"^https?://", "", url.strip(), flags=re.IGNORECASE).split("/", 1)[0].split("?", 1)[0]
    host = host.split("@")[-1].split(":", 1)[0].lower().removeprefix("www.")
    return host


def _domain_matches(host: str, domains: frozenset[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def classify_source_url(url: str) -> set[str]:
    host = registrable_domain(url)
    if not host:
        return set()
    classes: set[str] = set()
    if _domain_matches(host, _WIRE_DOMAINS):
        classes.add("wire")
    if _domain_matches(host, _PAYWALLED_DOMAINS):
        classes.add("paywalled")
    if _domain_matches(host, _COMMUNITY_DOMAINS):
        classes.add("community")
    if _domain_matches(host, _ANALYSIS_DOMAINS):
        classes.add("analysis")
    if _domain_matches(host, _RESEARCH_DOMAINS) or (
        "analysis" not in classes and (host.endswith(".edu") or ".ac." in host or host.endswith(".edu.au"))
    ):
        classes.add("research")
    if _domain_matches(host, _DATA_DOMAINS):
        classes.add("data")
    if (
        host.endswith(_OFFICIAL_SUFFIXES)
        or _domain_matches(host, _OFFICIAL_DOMAINS)
        or _OFFICIAL_HOST_RE.search(host)
    ):
        classes.add("official")
    return classes


def classify_sources(entries: list[str]) -> list[str]:
    classes: set[str] = set()
    for entry in entries:
        entry_classes: set[str] = set()
        for url in _URL_IN_ENTRY_RE.findall(entry):
            entry_classes |= classify_source_url(url)
        if not entry_classes:
            if _WIRE_TOKEN_RE.search(entry):
                entry_classes.add("wire")
            if _OFFICIAL_TOKEN_RE.search(entry):
                entry_classes.add("official")
            if _ANALYSIS_TOKEN_RE.search(entry):
                entry_classes.add("analysis")
            if _COMMUNITY_TOKEN_RE.search(entry):
                entry_classes.add("community")
            if _PAYWALLED_TOKEN_RE.search(entry):
                entry_classes.add("paywalled")
        classes |= entry_classes or {"reporting"}
    return sorted(classes)


_DEFAULT_TECHNICAL_KEYWORDS = (
    "inference", "benchmark", "interconnect", "export control", "yield",
    "grid", "latency", "supply chain", "wafer", "throughput",
)


def score_technical_substance(section: str, text: str) -> int:
    if section not in EDITION.technical_sections:
        return 12
    lower = text.lower()
    keywords = EDITION.technical_keywords or _DEFAULT_TECHNICAL_KEYWORDS
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
    unique_domains: int = 0,
    primary_documents: int = 0,
) -> dict:
    # ``word_count`` is accepted for compatibility but deliberately NOT rewarded: the old
    # ``word_count // 120`` term scored a 2,400-word article 13 points above an identical
    # 900-word one, so selection favoured padding. Significance now reflects how many
    # independent outlets and primary documents back the story.
    significance = min(20, 6 + 2 * min(6, unique_domains) + 2 * min(2, primary_documents))
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

