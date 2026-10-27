"""Story discovery and selection that does not need a model: ingest, slate validation, balance.

``build_ingest`` turns the source audit (which already downloaded every catalog source's front
page and harvested headlines) into a compact, scored candidate list. A model (the *slate* stage)
then ranks stories from that list plus its own search; ``validate_slate`` and ``select_slate``
check and balance the model's answer in plain code, so the rules in the spec (taxonomic balance,
native-language coverage, no repeats, no cruelty coverage, thin-material honesty) are enforced
even when the model forgets them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import net_safety_core

GROUPS = ("mammal", "bird", "fish_marine", "herp", "invertebrate", "other")
MATERIAL_ORDER = {"deep": 3, "moderate": 2, "thin": 1}
SUCCESS_STATUSES = {"checked_no_relevant_update", "checked_relevant_update", "checked_incorporated", "checked_with_update"}
_NAV_TITLES = re.compile(
    r"^(?:home|about(?: us)?|contact(?: us)?|news|menu|search|login|log in|sign in|subscribe|donate|privacy|terms|cookies?|"
    r"skip to (?:main )?content|read more|more|latest news|all news|press|media|careers|jobs|events?)$",
    re.I,
)
DEFAULT_KEYWORDS = (
    "species", "wildlife", "animal", "fauna", "population", "extinct", "endangered", "threatened", "conservation", "habitat",
    "bird", "mammal", "fish", "reptile", "amphibian", "insect", "invertebrate", "marine", "whale", "dolphin", "shark", "turtle",
    "elephant", "tiger", "lion", "wolf", "bear", "primate", "chimpanzee", "gorilla", "bat", "bee", "pollinator", "coral", "frog",
    "snake", "lizard", "genome", "fossil", "zoo", "aquarium", "veterinary", "livestock", "avian influenza", "h5n1", "outbreak",
    "migration", "breeding", "poaching", "iucn", "cites", "red list", "rewilding", "reintroduc", "biodiversity", "ecolog",
    "especie", "fauna", "poblaci", "conservaci", "espèce", "faune", "oiseau", "tier", "vogel", "art ", "naturschutz",
    "動物", "絶滅", "保護", "鳥", "魚", "动物", "物种", "濒危", "保护", "동물", "멸종", "животн", "птиц", "вид ",
)
_TIER_WEIGHT = {"primary-research": 2.0, "primary-data": 1.5, "institutional": 1.5, "news": 1.0, "aggregator": 1.0, "trade-press": 1.0}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zÀ-ɏ]{4,}|[぀-ヿ一-鿿가-힯]{2,}", (text or "").lower())}


def _similar(a: str, b: str, *, loose: bool = False) -> float:
    """Jaccard overlap of title tokens. ``loose`` compares 4-letter stems (seized/seizure) and also
    accepts a strong overlap coefficient; use it only where a false positive is cheap (ingest scoring)."""
    ta, tb = _tokens(a), _tokens(b)
    if loose:
        ta, tb = {t[:4] for t in ta}, {t[:4] for t in tb}
    if not ta or not tb:
        return 0.0
    shared = len(ta & tb)
    jaccard = shared / len(ta | tb)
    if loose and shared >= 3:
        jaccard = max(jaccard, shared / min(len(ta), len(tb)) * 0.8)
    return jaccard


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            parsed = datetime.strptime(text[:31] if "%a" in fmt else text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


@dataclass
class IngestItem:
    source_id: str
    source_name: str
    tier: str
    lang: str
    region: str
    title: str
    url: str
    published: str
    score: float
    seen_before: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def build_ingest(
    audit: dict,
    *,
    now: datetime,
    prior_titles: list[str] | None = None,
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS,
    banned_check: Callable[[str], list[str]] | None = None,
    meta_by_source: dict[str, dict] | None = None,
    max_items: int = 260,
) -> dict[str, Any]:
    prior_titles = prior_titles or []
    meta_by_source = meta_by_source or {}
    items: list[IngestItem] = []
    stats = {"sources_with_items": 0, "raw_items": 0, "dropped_nav": 0, "dropped_irrelevant": 0, "dropped_banned": 0, "dropped_duplicate": 0}
    seen_urls: set[str] = set()
    seen_titles: list[str] = []
    keyword_re = re.compile("|".join(re.escape(k) for k in keywords), re.I) if keywords else None  # no keywords: keep every headline
    for source in audit.get("sources", []):
        headlines = source.get("items") or []
        if not headlines or str(source.get("status")) not in SUCCESS_STATUSES:
            continue
        stats["sources_with_items"] += 1
        meta = meta_by_source.get(str(source.get("source_id")), {})
        tier = str(meta.get("tier") or source.get("tier") or "institutional")
        is_animal_source = "ANIMAL" in str(source.get("major_bucket") or source.get("bucket") or "").upper()
        lang = str(meta.get("lang") or (source.get("language_tags") or ["en"])[0])
        region = str(meta.get("region") or ", ".join(source.get("region_tags") or []) or "")
        for headline in headlines:
            stats["raw_items"] += 1
            title = re.sub(r"\s+", " ", str(headline.get("title") or "")).strip()
            url = str(headline.get("url") or "").strip()
            if len(title) < 12 or _NAV_TITLES.match(title) or not url:
                stats["dropped_nav"] += 1
                continue
            hits = len(keyword_re.findall(title)) if keyword_re else 0
            if keyword_re and not hits and not is_animal_source:
                stats["dropped_irrelevant"] += 1
                continue
            if banned_check and banned_check(title):
                stats["dropped_banned"] += 1
                continue
            norm_url = re.sub(r"[?#].*$", "", url).rstrip("/").lower()
            if norm_url in seen_urls or any(_similar(title, other) >= 0.8 for other in seen_titles):
                stats["dropped_duplicate"] += 1
                continue
            seen_urls.add(norm_url)
            seen_titles.append(title)
            score = _TIER_WEIGHT.get(tier, 1.0) + min(hits, 3) * 0.7
            published = _parse_date(headline.get("published"))
            if published:
                age = (now - published).total_seconds() / 86400.0
                score += 2.0 if age <= 3 else 1.0 if age <= 7 else -1.0 if age > 21 else 0.0
            if lang != "en":
                score += 0.8
            seen = any(_similar(title, prior, loose=True) >= 0.55 for prior in prior_titles)
            if seen:
                score -= 2.0
            items.append(IngestItem(str(source.get("source_id")), str(source.get("name")), tier, lang, region, title, url,
                                    str(headline.get("published") or ""), round(score, 2), seen))
    items.sort(key=lambda i: i.score, reverse=True)
    kept = items[:max_items]
    return {
        "schema_version": 1, "generated_at": now.isoformat(), "stats": {**stats, "kept": len(kept), "candidates": len(items)},
        "items": [i.as_dict() for i in kept],
    }


HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"


def fetch_hacker_news(fetcher: Any, *, min_points: int = 100, limit: int = 30) -> list[dict[str, Any]] | None:
    """Today's Hacker News front page (one request to the Algolia mirror): title, the linked article, points, comments and the thread.
    Returns None when the page cannot be read, so a run says so instead of treating it as an empty day."""
    try:
        data = fetcher.get_json(HN_SEARCH_URL, {"tags": "front_page", "hitsPerPage": 60})
    except Exception:  # noqa: BLE001 - any network or parse failure: the harvest goes on without it
        return None
    hits: list[dict[str, Any]] = []
    for hit in (data or {}).get("hits", []) if isinstance(data, dict) else []:
        title = re.sub(r"\s+", " ", str(hit.get("title") or "")).strip()
        points = int(hit.get("points") or 0)
        object_id = str(hit.get("objectID") or "")
        if len(title) < 12 or points < min_points or not object_id.isdigit():
            continue
        thread = f"https://news.ycombinator.com/item?id={object_id}"
        try:
            published = datetime.fromtimestamp(int(hit.get("created_at_i")), timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            published = ""
        hits.append({"title": title, "url": str(hit.get("url") or "").strip() or thread, "thread": thread, "points": points,
                     "comments": int(hit.get("num_comments") or 0), "published": published})
    hits.sort(key=lambda h: h["points"], reverse=True)
    return hits[:limit]


def add_hacker_news(ingest: dict, hits: list[dict[str, Any]], *, now: datetime, prior_titles: list[str] | None = None,
                    banned_check: Callable[[str], list[str]] | None = None, max_items: int = 260) -> dict[str, int]:
    """Fold front-page items into an ingest. A story the harvest already has is annotated with its thread and points (the writer is then
    told that a community discussion exists and the briefing's HN rule is met); a new one is added as a candidate. The linked article, not
    the thread, is the candidate's URL; the thread rides along in the source label."""
    prior_titles = prior_titles or []
    items = ingest["items"]
    by_url = {re.sub(r"[?#].*$", "", i["url"]).rstrip("/").lower(): i for i in items}
    counts = {"fetched": len(hits), "annotated": 0, "added": 0, "dropped_banned": 0}
    for hit in hits:
        note = f"HN {hit['points']} points, {hit['comments']} comments; thread {hit['thread']}"
        norm = re.sub(r"[?#].*$", "", hit["url"]).rstrip("/").lower()
        twin = by_url.get(norm) or next((i for i in items if _similar(hit["title"], i["title"]) >= 0.8), None)
        if twin is not None:
            if "HN " not in twin["source_name"]:
                twin["source_name"] += f" (also {note})"
                twin["score"] = round(twin["score"] + 0.7, 2)
                counts["annotated"] += 1
            continue
        if banned_check and banned_check(hit["title"]):
            counts["dropped_banned"] += 1
            continue
        published = _parse_date(hit["published"])
        age = (now - published).total_seconds() / 86400.0 if published else 99.0
        score = 1.0 + (2.0 if age <= 3 else 1.0 if age <= 7 else 0.0) + min(hit["points"] / 400.0, 1.5)
        seen = any(_similar(hit["title"], prior, loose=True) >= 0.55 for prior in prior_titles)
        if seen:
            score -= 2.0
        item = IngestItem("hacker-news", f"Hacker News front page ({note})", "aggregator", "en", "", hit["title"], hit["url"],
                          hit["published"], round(score, 2), seen)
        items.append(item.as_dict())
        by_url[norm] = items[-1]
        counts["added"] += 1
    items.sort(key=lambda i: i["score"], reverse=True)
    del items[max_items:]
    ingest["stats"].update(hacker_news=counts, kept=len(items), candidates=int(ingest["stats"].get("candidates", 0)) + counts["added"])
    return counts


def ingest_markdown(ingest: dict, *, limit: int | None = None) -> str:
    """Compact digest for the slate prompt: one line per candidate, ids are line numbers."""
    lines = []
    for index, item in enumerate(ingest["items"][:limit] if limit else ingest["items"], start=1):
        flags = f"{item['lang']}, {item['tier']}" + (", seen-before" if item.get("seen_before") else "")
        date = item["published"][:10] if item["published"] else "undated"
        lines.append(f"{index}. [{flags}; {date}] {item['title']} — {item['source_name']} <{item['url']}>")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Slate parsing, validation, selection
# --------------------------------------------------------------------------------------------
_SCI_NAME_RE = re.compile(r"^[A-Z][a-z]{2,}(?: [a-z]{3,}){1,2}$")


def extract_json(text: str) -> Any:
    """Parse the model's JSON answer, tolerating fences and chatter.

    The answer is the top-level value that looks like one: an object holding a ``stories`` list, or a non-empty list of
    objects. A stray ``[1]`` or ``{}`` in the model's preamble is not mistaken for it; only when nothing looks like an
    answer does the first decodable value win."""
    cleaned = re.sub(r"```(?:json)?", "", text or "")
    decoder = json.JSONDecoder()
    found: list[Any] = []
    index = 0
    while True:
        match = re.compile(r"[\[{]").search(cleaned, index)
        if not match:
            break
        try:
            value, length = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            index = match.start() + 1
            continue
        index = match.start() + length  # nested values inside this one are part of it, not separate candidates
        if isinstance(value, (list, dict)):
            found.append(value)
    for value in found:
        if isinstance(value, dict) and isinstance(value.get("stories"), list):
            return value
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            return value
    if found:
        return found[0]
    raise ValueError("no JSON found in model output")


def slugify(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit].strip("-") or "story"


@dataclass
class SlateReport:
    kept: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    flagged: list[dict] = field(default_factory=list)   # kept, but marked thin because the entry still carries a placeholder


# What a slate writes when it has not identified the study or the place ("confirm study population", "paper and DOI must be traced").
_PLACEHOLDER_RE = re.compile(
    r"(?i)\((?:confirm|tbc|tbd|to be (?:confirmed|identified|traced))[^)]*\)|\bconfirm (?:the )?(?:study|species|population|paper|doi|journal|dates?|figures?|identity)\b"
    r"|\b(?:must|needs? to|to) be (?:traced|identified|confirmed|verified)\b|\bTBD\b|\?\?")


def validate_slate(
    raw: Any,
    *,
    prior_titles: list[str] | None = None,
    banned_check: Callable[[str], list[str]] | None = None,
    prior_similarity: float = 0.6,
    sections: tuple[str, ...] = (),
    default_section: str | None = None,
) -> SlateReport:
    """Validate the slate model's answer. With ``sections`` (multi-section editions) every story must name a
    valid ``section``; ``updates`` stories must be developments of an earlier item."""
    entries = raw.get("stories") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("slate must be a JSON array (or an object with a 'stories' array)")
    report = SlateReport()
    used: set[str] = set()
    prior_titles = prior_titles or []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            report.dropped.append({"entry": position, "reason": "not an object"})
            continue
        title = str(entry.get("title") or "").strip()
        reasons: list[str] = []
        if len(title) < 15:
            reasons.append("missing or too-short title")
        sources = [s for s in entry.get("sources") or [] if isinstance(s, dict) and net_safety_core.looks_public(str(s.get("url") or ""))]
        if len(sources) < 2:
            reasons.append("fewer than 2 sources with URLs")
        group = str(entry.get("group") or "other").lower().replace("/", "_").replace(" ", "_")
        group = group if group in GROUPS else ("fish_marine" if group in {"fish", "marine"} else "other")
        story_id = slugify(str(entry.get("story_id") or title))
        base, suffix = story_id, 2
        while story_id in used:
            story_id, suffix = f"{base}-{suffix}", suffix + 1
        section = str(entry.get("section") or default_section or "").strip()
        if sections and section not in sections:
            if default_section and not entry.get("section"):
                section = default_section
            else:
                reasons.append(f"unknown section {section!r}")
        if section == "updates" and str(entry.get("novelty") or "").lower() != "development":
            reasons.append("an Updates story must be a development of an earlier briefing item")
        if banned_check:
            flagged = banned_check(f"{title}. {entry.get('angle') or ''}")
            if flagged:
                reasons.append(f"excluded topic: {', '.join(flagged)}")
        novelty = str(entry.get("novelty") or "new").lower()
        similar_prior = next((p for p in prior_titles if _similar(title, p) >= prior_similarity), None)
        if similar_prior and novelty != "development":
            reasons.append(f"repeats earlier briefing item {similar_prior!r} without a stated development")
        taxa = []
        for taxon in entry.get("taxa") or []:
            if isinstance(taxon, dict):
                scientific = str(taxon.get("scientific") or "").strip()
                taxa.append({"common": str(taxon.get("common") or "").strip(),
                             "scientific": scientific if _SCI_NAME_RE.match(scientific) else ""})
        if reasons:
            report.dropped.append({"entry": position, "title": title[:100], "reason": "; ".join(reasons)})
            continue
        used.add(story_id)
        material = str(entry.get("material") or "moderate").lower()
        material = material if material in MATERIAL_ORDER else "moderate"
        placeholder = next((m.group(0) for field_name in ("title", "region", "angle", "why_now")
                            if (m := _PLACEHOLDER_RE.search(str(entry.get(field_name) or "")))), None)
        if placeholder and material != "thin":
            material = "thin"
            report.flagged.append({"story_id": story_id, "title": title[:100], "placeholder": placeholder})
        queries = [str(q).strip() for q in entry.get("photo_queries") or [] if str(q).strip()]
        if not queries:
            queries = [t["scientific"] for t in taxa if t["scientific"]] + [t["common"] for t in taxa if t["common"]]
        report.kept.append({
            "story_id": story_id, "title": title, "section": section, "rank": int(entry.get("rank") or position), "group": group,
            "region": str(entry.get("region") or "").strip(), "taxa": taxa, "angle": str(entry.get("angle") or "").strip(),
            "why_now": str(entry.get("why_now") or "").strip(), "novelty": novelty if novelty in {"new", "development"} else "new",
            "prior_story_id": entry.get("prior_story_id") or None,
            "material": material, "sources": sources, "native_language": bool(entry.get("native_language")) or any(
                str(s.get("lang") or "en").lower() != "en" for s in sources),
            "photo_queries": queries[:4], "photo_allow_words": [str(w) for w in entry.get("photo_allow_words") or []][:4],
        })
    return report


_NO_PLACE = re.compile(r"\s*(?:global|worldwide|international|multinational|multiple|various|several|comparative|n/?a|unknown)\b", re.I)
_WESTERN = re.compile(r"\b(?:united states|usa?|u\.s\.|america|canada|uk|united kingdom|england|scotland|wales|ireland|europe|germany|france|spain|italy|netherlands|sweden|norway|denmark|finland|switzerland|belgium|austria|portugal|poland)\b", re.I)


def select_slate(
    entries: list[dict],
    *,
    target: int,
    minimum: int,
    maximum: int,
    max_per_group: int = 3,
    want_invertebrate: bool = True,
    want_non_western: int = 2,
    want_native: int = 3,
) -> tuple[list[dict], list[dict], list[str]]:
    """Pick ``target`` stories honouring the balance rules; return (selected, backups, notes).

    Thin-material stories are only used when nothing else fills the quota, because the writer
    will not reach the length target honestly and a replacement would be needed anyway.
    """
    notes: list[str] = []
    ranked = sorted(entries, key=lambda e: (e["material"] == "thin", e["rank"]))
    selected: list[dict] = []
    counts: dict[str, int] = {}
    for entry in ranked:
        if len(selected) >= target:
            break
        if counts.get(entry["group"], 0) >= max_per_group:
            continue
        selected.append(entry)
        counts[entry["group"]] = counts.get(entry["group"], 0) + 1
    if len(selected) < target:  # relax the per-group cap before accepting a short slate
        for entry in ranked:
            if len(selected) >= target:
                break
            if entry not in selected:
                selected.append(entry)
                notes.append(f"relaxed group cap to fill slate with {entry['story_id']}")

    def non_western(e: dict) -> bool:
        region = e["region"]
        return bool(region) and not _WESTERN.search(region) and not _NO_PLACE.match(region)

    def swap_in(predicate: Callable[[dict], bool], label: str, have: int, need: int) -> None:
        while have < need:
            # A balance rule is "when material exists": a thin story brought in only to satisfy it would end as a low-confidence skip.
            candidate = next((e for e in ranked if e not in selected and predicate(e) and e["material"] != "thin"), None)
            if candidate is None:
                notes.append(f"could not meet balance target: {label}")
                return
            victim = next((e for e in reversed(selected) if not predicate(e) and e["rank"] >= 1
                           and sum(1 for s in selected if s["group"] == e["group"]) > 1), None)
            if victim is None:
                notes.append(f"could not meet balance target: {label}")
                return
            selected[selected.index(victim)] = candidate
            notes.append(f"swapped {victim['story_id']} -> {candidate['story_id']} for balance ({label})")
            have += 1

    if want_invertebrate:
        swap_in(lambda e: e["group"] == "invertebrate", "at least one invertebrate story", sum(e["group"] == "invertebrate" for e in selected), 1)
    swap_in(non_western, f"{want_non_western} stories centred outside the US/Western Europe", sum(non_western(e) for e in selected), want_non_western)
    swap_in(lambda e: e["native_language"], f"{want_native} stories with a native-language source", sum(e["native_language"] for e in selected), want_native)
    selected = sorted(selected, key=lambda e: e["rank"])[:maximum]
    backups = [e for e in ranked if e not in selected]
    if len(selected) < minimum:
        notes.append(f"slate has only {len(selected)} stories (minimum {minimum})")
    return selected, backups, notes


def select_by_sections(entries: list[dict], quotas: dict[str, dict[str, int]]) -> tuple[list[dict], list[dict], list[str]]:
    """Multi-section selection: top ``target`` per section (thin material last); the rest are that section's backups."""
    selected: list[dict] = []
    backups: list[dict] = []
    notes: list[str] = []
    for section, quota in quotas.items():
        pool = sorted([e for e in entries if e.get("section") == section], key=lambda e: (e["material"] == "thin", e["rank"]))
        selected += pool[: quota["target"]]
        backups += pool[quota["target"]:]
        if len(pool) < quota["min"]:
            notes.append(f"{section}: only {len(pool)} candidate(s) (minimum {quota['min']})")
    return selected, backups, notes


__all__ = [
    "GROUPS", "SlateReport", "build_ingest", "extract_json", "ingest_markdown", "select_by_sections", "select_slate", "slugify",
    "validate_slate",
]
