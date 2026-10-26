from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import html as html_lib
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import briefing_paths as briefing_paths_module
from run_reporting import ensure_run_attempt, set_step_timing, update_run_report

_SHARED_DIR = str(Path(__file__).resolve().parent)
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)
import edition_core  # noqa: E402
import source_health_core  # noqa: E402


SCHEMA_VERSION = 1
MINIMUM_SOURCE_FLOOR = 250
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_RETRIES = 1
DEFAULT_MAX_WORKERS = 24
MAX_SAMPLE_BYTES = 65536
CHECKED_STATUSES = {
    "checked_used",
    "checked_relevant_not_selected",
    "checked_no_relevant_update",
    "checked_unavailable",
    "checked_failed_after_retry",
}
SUCCESS_STATUSES = {
    "checked_used",
    "checked_relevant_not_selected",
    "checked_no_relevant_update",
}
TABLE_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(https?://[^|\s]+)\s*\|\s*(.*?)\s*\|\s*$")
HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MARKDOWN_MARKUP_RE = re.compile(r"[*_`]")
NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class SourceCatalogEntry:
    source_id: str
    catalog_number: int
    name: str
    url: str
    why: str
    bucket: str
    major_bucket: str
    sub_bucket: str | None
    section: str | None
    domain: str
    region_tags: tuple[str, ...]
    language_tags: tuple[str, ...]
    credit_sections: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    rss_url: str | None = None
    native_language: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["region_tags"] = list(self.region_tags)
        payload["language_tags"] = list(self.language_tags)
        payload["credit_sections"] = list(self.credit_sections)
        payload["topics"] = list(self.topics)
        return payload


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_catalog_path() -> Path:
    return Path(__file__).resolve().parent / "references" / "sources.md"


def default_catalog_paths() -> list[Path]:
    """Catalogs for the active edition (daily: sources.md; animal: sources.md + animal-sources.md)."""
    root = Path(__file__).resolve().parent / "references"
    return [root / name for name in edition_core.load_edition().catalogs]


def source_audit_path(date: str) -> Path:
    helper = getattr(briefing_paths_module, "source_audit_path", None)
    if not callable(helper):
        raise AttributeError("briefing_paths.source_audit_path is required")
    path = Path(helper(date))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def source_health_path() -> Path:
    """Durable per-source memory (outside the three-day ``generated/`` clean-up, shared by the editions)."""
    helper = getattr(briefing_paths_module, "research_vault_dir", None)
    if not callable(helper):
        raise AttributeError("briefing_paths.research_vault_dir is required")
    path = Path(helper())
    path.mkdir(parents=True, exist_ok=True)
    return path / "source-health.json"


def source_audit_markdown_path(date: str) -> Path:
    helper = getattr(briefing_paths_module, "source_audit_markdown_path", None)
    if not callable(helper):
        raise AttributeError("briefing_paths.source_audit_markdown_path is required")
    path = Path(helper(date))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def clean_markdown(text: str) -> str:
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    text = MARKDOWN_MARKUP_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(value: str, *, max_len: int = 48) -> str:
    slug = NON_SLUG_RE.sub("-", value.lower()).strip("-")
    return (slug[:max_len].strip("-") or "source")


def stable_source_id(catalog_number: int, name: str) -> str:
    return f"source-{catalog_number:03d}-{slugify(name)}"


def domain_for(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return (parsed.netloc or "").lower().removeprefix("www.")


# Catalog bucket headings -> sections whose ``min_section_sources`` floor the source counts toward.
# The previous single-valued mapping left 187 of 364 catalog sources (every native-language,
# think-tank, economics, OSINT, Europe, Africa, LatAm and science source) with no section at
# all, so an Iran article sourced from IRNA, Tasnim, Haaretz and CFR earned zero credit while
# the spec demanded native-language research.
_BUCKET_CREDIT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PART IV", ("part4",)),
    ("PART III", ("part3",)),
    ("PART II", ("part2",)),
    ("PART I", ("part1",)),
    ("ANIMAL", ("animals", "digest")),
    ("HUMAN", ("humaninterest",)),
    ("UPDATE", ("updates",)),
    ("WIRE SERVICES", ("cross_section",)),
    ("AGGREGATORS", ("cross_section",)),
    ("JAPANESE", ("part4", "part1")),
    ("CHINESE", ("part1", "part2", "part3")),
    ("TAIWAN", ("part1", "part2", "part3")),
    ("FARSI", ("part1",)),
    ("ARABIC", ("part1",)),
    ("HEBREW", ("part1",)),
    ("KOREAN", ("part1", "part3")),
    ("TURKISH", ("part1",)),
    ("RUSSIAN", ("part1",)),
    ("OTHER REGIONS", ("part1",)),
    ("THINK TANKS", ("part1", "part2", "part3")),
    ("ECONOMICS", ("part1", "part3")),
    ("EUROPEAN", ("part1", "part2", "part3")),
    ("AFRICA", ("part1",)),
    ("LATIN AMERICA", ("part1",)),
    ("SOUTH & SOUTHEAST ASIA", ("part1", "part3")),
    ("OSINT", ("part1",)),
    ("SPACE, CLIMATE", ("part1", "part3")),
    ("SCIENCE & HEALTH", ("part1", "humaninterest")),
    ("PODCASTS", ("part1", "part2")),
)


def credit_sections_for_bucket(bucket: str) -> tuple[str, ...]:
    upper = bucket.upper()
    # Order matters: "PART II/III/IV" all contain "PART I" as a substring, so the
    # longer roman numerals are listed (and matched) first; the first hit wins.
    base: tuple[str, ...] = ()
    for needle, sections in _BUCKET_CREDIT_RULES:
        if needle in upper:
            base = sections
            break
    # Editions can credit general-catalog buckets to their own sections (the animal edition
    # counts SCIENCE & HEALTH and SPACE, CLIMATE & ENVIRONMENT rows toward its section floor).
    extra: list[str] = []
    for needle, sections in (edition_core.load_edition().extra.get("source_credit_extra") or {}).items():
        if needle.upper() in upper:
            extra.extend(sections)
    return tuple(dict.fromkeys([*base, *extra]))


def bucket_included(bucket: str) -> bool:
    """Edition filter on which catalog buckets the daily audit covers (``catalog_include_buckets``)."""
    include = edition_core.load_edition().extra.get("catalog_include_buckets")
    if not include:
        return True
    upper = bucket.upper()
    return any(str(needle).upper() in upper for needle in include)


def section_for_bucket(bucket: str) -> str | None:
    credited = credit_sections_for_bucket(bucket)
    return credited[0] if credited else None


def infer_region_tags(name: str, url: str, bucket: str, why: str) -> tuple[str, ...]:
    haystack = f"{name} {url} {bucket} {why}".lower()
    tags: list[str] = []
    checks = {
        "japan": ("japan", "japanese", ".jp", "tokyo", "nikkei", "asahi", "yomiuri"),
        "china": ("china", "chinese", "hong kong", "taiwan", "scmp", ".cn"),
        "ukraine": ("ukraine", "kyiv", "russia"),
        "middle_east": ("middle east", "israel", "gaza", "iran", "arab", "lebanon", "al jazeera"),
        "europe": ("europe", "eu ", "brussels", "germany", "france", "uk "),
        "us": (" us ", "united states", "washington", ".gov", "congress", "pentagon"),
    }
    padded = f" {haystack} "
    for tag, needles in checks.items():
        if any(needle in padded for needle in needles):
            tags.append(tag)
    return tuple(tags)


def infer_language_tags(name: str, url: str, bucket: str, why: str) -> tuple[str, ...]:
    haystack = f"{name} {url} {bucket} {why}".lower()
    tags = ["en"]
    if any(token in haystack for token in (".jp", "asahi", "yomiuri", "nikkei", "japan")):
        tags.append("ja")
    if any(token in haystack for token in (".cn", "china", "chinese", "taiwan")):
        tags.append("zh")
    if any(token in haystack for token in ("france", "french", "afp", "francophone")):
        tags.append("fr")
    if any(token in haystack for token in ("arab", "al jazeera", "middle east", "lebanon")):
        tags.append("ar")
    return tuple(dict.fromkeys(tags))


def _load_catalog_meta(catalog_path: Path) -> dict[int, dict[str, Any]]:
    """Optional ``<catalog>.meta.json`` sidecar: {"<number>": {lang, region, topics, rss, native_language}}.

    Exact language/region metadata beats the substring heuristics in ``infer_*_tags``
    (e.g. any row mentioning "china" was tagged Chinese-language).
    """
    meta_path = catalog_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {int(key): value for key, value in raw.items() if str(key).isdigit() and isinstance(value, dict)}


def parse_source_catalog(path: Path | None = None) -> list[SourceCatalogEntry]:
    catalog_path = path or default_catalog_path()
    if not catalog_path.exists():
        raise FileNotFoundError(f"Source catalog not found: {catalog_path}")
    meta_by_number = _load_catalog_meta(catalog_path)

    major_bucket = ""
    sub_bucket: str | None = None
    entries: list[SourceCatalogEntry] = []
    seen_ids: set[str] = set()
    for raw_line in catalog_path.read_text(encoding="utf-8").splitlines():
        heading = HEADING_RE.match(raw_line.strip())
        if heading:
            level, text = heading.groups()
            if level == "##":
                major_bucket = clean_markdown(text)
                sub_bucket = None
            elif level == "###":
                sub_bucket = clean_markdown(text)
            continue

        match = TABLE_ROW_RE.match(raw_line)
        if not match:
            continue
        number_text, name_text, url, why_text = match.groups()
        name = clean_markdown(name_text)
        why = clean_markdown(why_text)
        catalog_number = int(number_text)
        bucket = major_bucket
        if sub_bucket:
            bucket = f"{major_bucket} / {sub_bucket}" if major_bucket else sub_bucket
        source_id = stable_source_id(catalog_number, name)
        if source_id in seen_ids:
            suffix = 2
            base = source_id
            while f"{base}-{suffix}" in seen_ids:
                suffix += 1
            source_id = f"{base}-{suffix}"
        seen_ids.add(source_id)
        meta = meta_by_number.get(catalog_number, {})
        region_tags = infer_region_tags(name, url, bucket, why)
        language_tags = infer_language_tags(name, url, bucket, why)
        if meta.get("region"):
            region_tags = tuple(dict.fromkeys([str(meta["region"]).lower(), *region_tags]))
        if meta.get("lang"):
            language_tags = tuple(dict.fromkeys([str(meta["lang"]).lower(), *(t for t in language_tags if t != "en" or meta["lang"] == "en")]))
        entries.append(
            SourceCatalogEntry(
                source_id=source_id,
                catalog_number=catalog_number,
                name=name,
                url=url.strip(),
                why=why,
                bucket=bucket,
                major_bucket=major_bucket,
                sub_bucket=sub_bucket,
                section=section_for_bucket(bucket),
                domain=domain_for(url),
                region_tags=region_tags,
                language_tags=language_tags,
                credit_sections=credit_sections_for_bucket(bucket),
                topics=tuple(meta.get("topics") or ()),
                rss_url=meta.get("rss") or None,
                native_language=bool(meta.get("native_language", False)),
            )
        )
    return entries


def parse_source_catalogs(paths: list[Path] | None = None) -> list[SourceCatalogEntry]:
    """Parse several catalogs into one list; duplicate catalog numbers across files are an error."""
    filtered_default = not paths
    catalogs = paths or default_catalog_paths()
    combined: list[SourceCatalogEntry] = []
    seen_numbers: dict[int, Path] = {}
    index_by_url: dict[str, int] = {}
    for catalog in catalogs:
        for entry in parse_source_catalog(catalog):
            if filtered_default and not bucket_included(entry.major_bucket):
                continue
            if entry.catalog_number in seen_numbers:
                raise ValueError(
                    f"Catalog number {entry.catalog_number} appears in both {seen_numbers[entry.catalog_number]} and {catalog}"
                )
            seen_numbers[entry.catalog_number] = catalog
            if entry.url in index_by_url:
                # The same URL listed twice would be fetched (and counted) twice; keep one
                # row but merge the section credit of both listings.
                kept_index = index_by_url[entry.url]
                kept = combined[kept_index]
                merged = tuple(dict.fromkeys([*kept.credit_sections, *entry.credit_sections]))
                combined[kept_index] = dataclasses.replace(kept, credit_sections=merged)
                continue
            index_by_url[entry.url] = len(combined)
            combined.append(entry)
    return combined


_FEED_HEAD_RE = re.compile(r"<(?:rss|feed|rdf:RDF)\b", re.IGNORECASE)
_FEED_BLOCK_RE = re.compile(r"<(item|entry)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_NAV_WORDS = frozenset({
    "home", "about", "contact", "login", "log in", "sign in", "subscribe", "menu", "search", "privacy",
    "terms", "cookie", "cookies", "careers", "advertise", "newsletter", "more", "read more", "donate",
})


def _clean_text(value: str) -> str:
    value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value, flags=re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


def _feed_field(block: str, *names: str) -> str:
    for name in names:
        match = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", block, re.IGNORECASE | re.DOTALL)
        if match and _clean_text(match.group(1)):
            return _clean_text(match.group(1))
    return ""


def _feed_link(block: str) -> str:
    atom = re.search(r"<link\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>", block, re.IGNORECASE)
    if atom:
        return html_lib.unescape(atom.group(1)).strip()
    return _feed_field(block, "link", "guid")


def extract_headlines(markup: str, base_url: str, *, limit: int = 12) -> list[dict[str, str]]:
    """Cheap headline harvest from a page sample (RSS/Atom, JSON-LD, then front-page anchors).

    The audit already downloads each source's front page; reading it for headlines turns a
    reachability ping into discovery input at no extra request cost.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(title: str, url: str, published: str = "") -> None:
        title = re.sub(r"\s+", " ", title).strip()
        if not title or len(title) < 20 or len(title) > 220 or len(title.split()) < 4:
            return
        if title.lower() in _NAV_WORDS:
            return
        absolute = urllib.parse.urljoin(base_url, url.strip()) if url else base_url
        if absolute.startswith(("mailto:", "javascript:")):
            return
        key = absolute.split("#", 1)[0]
        if key in seen:
            return
        seen.add(key)
        items.append({"title": title, "url": absolute, "published": published})

    head = markup[:3000]
    if _FEED_HEAD_RE.search(head):
        for block in _FEED_BLOCK_RE.finditer(markup):
            body = block.group(0)
            add(
                _feed_field(body, "title"),
                _feed_link(body),
                _feed_field(body, "pubDate", "published", "updated", "dc:date"),
            )
            if len(items) >= limit:
                return items[:limit]
        return items[:limit]

    for script in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", markup, re.IGNORECASE | re.DOTALL
    ):
        try:
            payload = json.loads(html_lib.unescape(script.group(1)))
        except json.JSONDecodeError:
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack and len(items) < limit:
            node = stack.pop(0)
            if isinstance(node, dict):
                if node.get("headline") and node.get("url"):
                    add(str(node["headline"]), str(node["url"]), str(node.get("datePublished") or ""))
                stack.extend(value for value in node.values() if isinstance(value, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)

    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(markup, "lxml")
        for anchor in soup.select("article a[href], h1 a[href], h2 a[href], h3 a[href]"):
            add(anchor.get_text(" ", strip=True), anchor.get("href", ""))
            if len(items) >= limit:
                break
    except Exception:  # bs4/lxml missing or hostile markup: anchors are best-effort only
        for match in re.finditer(r"<h[1-3][^>]*>\s*<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", markup, re.IGNORECASE | re.DOTALL):
            add(_clean_text(match.group(2)), match.group(1))
            if len(items) >= limit:
                break
    return items[:limit]


def request_for(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": "DailyBriefingSourceAudit/1.0 (+https://local.invalid/source-audit)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="GET",
    )


def check_source(entry: SourceCatalogEntry, *, timeout: float, retries: int) -> dict[str, Any]:
    try:
        from adaptive_fetch import detect_blocking_signals, fetch_adaptive_url
    except Exception:  # pragma: no cover - optional recovery path
        detect_blocking_signals = None
        fetch_adaptive_url = None

    started = time.time()
    errors: list[str] = []
    attempts = max(1, retries + 1)
    final_url = entry.url
    http_status: int | None = None
    bytes_sampled = 0
    status = "checked_failed_after_retry"
    adaptive_fetch: dict[str, Any] = {"used": False}
    headlines: list[dict[str, str]] = []

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request_for(entry.url), timeout=timeout) as response:
                http_status = getattr(response, "status", None) or response.getcode()
                final_url = response.geturl()
                response_headers = dict(response.headers.items())
                sample = response.read(MAX_SAMPLE_BYTES)
                bytes_sampled = len(sample)
            if 200 <= int(http_status) < 400:
                sample_text = sample.decode("utf-8", errors="ignore")
                try:
                    headlines = extract_headlines(sample_text, final_url)
                except Exception:  # never let a parsing quirk change the reachability verdict
                    headlines = []
                blocking_signals = detect_blocking_signals(http_status, response_headers, sample_text) if detect_blocking_signals else []
                body_len = len(sample_text.strip())
                if blocking_signals or body_len < 300:
                    status = "checked_failed_after_retry"
                    reason = ", ".join(blocking_signals or [f"tiny_body:{body_len}"])
                    errors.append(f"adaptive_trigger: {reason}")
                else:
                    status = "checked_no_relevant_update"
            elif int(http_status) in {401, 402, 403, 407, 429}:
                status = "checked_unavailable"
            else:
                status = "checked_failed_after_retry"
            break
        except urllib.error.HTTPError as exc:
            http_status = int(exc.code)
            final_url = exc.geturl() or final_url
            errors.append(f"HTTPError {exc.code}: {exc.reason}")
            if http_status in {401, 402, 403, 407, 429}:
                status = "checked_unavailable"
                break
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            status = "checked_failed_after_retry"
        if attempt < attempts:
            time.sleep(min(1.5, 0.25 * attempt))

    if status not in SUCCESS_STATUSES and fetch_adaptive_url is not None:
        try:
            adaptive_result = fetch_adaptive_url(
                entry.url,
                timeout=timeout,
                max_bytes=MAX_SAMPLE_BYTES,
                allow_install=False,
            )
            adaptive_fetch = adaptive_result.source_audit_summary()
            if adaptive_result.ok and not adaptive_result.sidecar_only:
                status = "checked_no_relevant_update"
                http_status = adaptive_result.status
                final_url = adaptive_result.final_url
                bytes_sampled = max(bytes_sampled, adaptive_result.bytes_sampled)
            elif (
                adaptive_result.auth_required
                or adaptive_result.sidecar_only
                or adaptive_result.verdict in {"browser_required", "challenge"}
            ):
                status = "checked_unavailable"
        except Exception as exc:  # pragma: no cover - defensive reporting path
            adaptive_fetch = {
                "used": True,
                "ok": False,
                "verdict": "failed",
                "method": None,
                "status": None,
                "final_url": entry.url,
                "trust": "normal",
                "error": f"{type(exc).__name__}: {exc}",
                "attempt_count": 0,
                "attempts": [],
            }

    ended = time.time()
    payload = entry.as_dict()
    payload.update(
        {
            "status": status,
            "check_method": "http_get_sample",
            "checked_at": iso_now(),
            "attempts": attempt,
            "retry_count": max(0, attempt - 1),
            "http_status": http_status,
            "final_url": final_url,
            "bytes_sampled": bytes_sampled,
            "duration_seconds": round(ended - started, 3),
            "errors": errors,
            "adaptive_fetch": adaptive_fetch,
            "items": headlines,
        }
    )
    return payload


def load_fixture(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Source audit fixture not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_result_for(entry: SourceCatalogEntry, fixture: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    by_id = {
        str(item.get("source_id")): item
        for item in fixture.get("sources", [])
        if isinstance(item, dict) and item.get("source_id")
    }
    overrides = fixture.get("source_status_overrides") or fixture.get("status_by_source_id") or {}
    default_status = str(fixture.get("default_status") or "checked_no_relevant_update")
    if default_status not in CHECKED_STATUSES:
        default_status = "checked_no_relevant_update"

    payload = entry.as_dict()
    source_override = by_id.get(entry.source_id)
    if isinstance(source_override, dict):
        payload.update(source_override)
    override = overrides.get(entry.source_id)
    if isinstance(override, str):
        payload["status"] = override
    elif isinstance(override, dict):
        payload.update(override)
    payload.setdefault("status", default_status)
    if payload["status"] not in CHECKED_STATUSES:
        payload["status"] = "checked_failed_after_retry"
    payload.setdefault("check_method", "fixture")
    payload.setdefault("checked_at", generated_at)
    payload.setdefault("attempts", 1)
    payload.setdefault("retry_count", 0)
    payload.setdefault("http_status", 200 if payload["status"] in SUCCESS_STATUSES else None)
    payload.setdefault("final_url", entry.url)
    payload.setdefault("bytes_sampled", 0)
    payload.setdefault("duration_seconds", 0.0)
    payload.setdefault("errors", [])
    return payload


def build_summary(sources: list[dict[str, Any]], *, catalog_count: int) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    bucket_counts: dict[str, dict[str, int]] = {}
    unavailable_breakdown: dict[str, int] = {}
    checked_source_ids: list[str] = []
    for source in sources:
        status = str(source.get("status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status in CHECKED_STATUSES:
            checked_source_ids.append(str(source.get("source_id")))
        bucket = str(source.get("major_bucket") or source.get("bucket") or "Unbucketed")
        bucket_summary = bucket_counts.setdefault(bucket, {"total": 0, "checked": 0, "failed_after_retry": 0})
        bucket_summary["total"] += 1
        if status in CHECKED_STATUSES:
            bucket_summary["checked"] += 1
        if status == "checked_failed_after_retry":
            bucket_summary["failed_after_retry"] += 1
        if status == "checked_unavailable":
            reason = unavailable_reason(source)
            unavailable_breakdown[reason] = unavailable_breakdown.get(reason, 0) + 1

    checked_count = len(checked_source_ids)
    failed_count = status_counts.get("checked_failed_after_retry", 0)
    unavailable_count = status_counts.get("checked_unavailable", 0)
    used_count = status_counts.get("checked_used", 0)
    failure_threshold = max(5, int(catalog_count * 0.05))
    return {
        "minimum_required_sources": MINIMUM_SOURCE_FLOOR,
        "catalog_count": catalog_count,
        "source_count": len(sources),
        "checked_count": checked_count,
        "reachable_count": sum(1 for source in sources if str(source.get("status") or "") in SUCCESS_STATUSES),
        "items_harvested": sum(len(source.get("items") or []) for source in sources),
        "all_sources_checked": checked_count == catalog_count and len(sources) == catalog_count,
        "failed_after_retry_count": failed_count,
        "unavailable_count": unavailable_count,
        "unavailable_breakdown": unavailable_breakdown,
        "used_count": used_count,
        "status_counts": status_counts,
        "bucket_counts": bucket_counts,
        "failure_threshold": failure_threshold,
        "systemic_failure": failed_count > failure_threshold,
        "checked_source_ids": checked_source_ids,
    }


def unavailable_reason(source: dict[str, Any]) -> str:
    adaptive = source.get("adaptive_fetch") if isinstance(source.get("adaptive_fetch"), dict) else {}
    verdict = str(adaptive.get("verdict") or "").strip()
    method = str(adaptive.get("method") or "").strip()
    trust = str(adaptive.get("trust") or "").strip()
    if verdict == "sidecar_ok" or method == "wayback_available" or trust == "low_sidecar":
        return "archive_sidecar_only"
    if verdict == "auth_required":
        return "auth_required"
    if verdict == "browser_required":
        return "browser_required"
    if verdict == "challenge":
        return "challenge"
    http_status = source.get("http_status")
    try:
        status_code = int(http_status)
    except (TypeError, ValueError):
        return "unavailable_other"
    if status_code in {401, 402, 407}:
        return "auth_required"
    if status_code == 403:
        return "blocked_or_forbidden"
    if status_code == 429:
        return "rate_limited"
    return "unavailable_other"


def derive_status(summary: dict[str, Any], *, fixture: bool) -> str:
    if summary["catalog_count"] < MINIMUM_SOURCE_FLOOR:
        return "failed"
    if not summary["all_sources_checked"]:
        return "failed"
    if summary["systemic_failure"]:
        return "failed"
    if summary["failed_after_retry_count"] > 0 or summary["unavailable_count"] > 0:
        return "degraded"
    return "fixture" if fixture else "complete"


def build_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        f"# Source Audit - {payload.get('date')}",
        "",
        f"- Status: {payload.get('status')}",
        f"- Catalog sources: {summary.get('catalog_count', 0)}",
        f"- Checked sources: {summary.get('checked_count', 0)}",
        f"- Reachable (page fetched and readable): {summary.get('reachable_count', 0)}",
        f"- Headlines harvested: {summary.get('items_harvested', 0)}",
        f"- Failed after retry: {summary.get('failed_after_retry_count', 0)}",
        f"- Degraded source-access results: {summary.get('unavailable_count', 0)}",
    ]
    unavailable_breakdown = summary.get("unavailable_breakdown") or {}
    if unavailable_breakdown:
        formatted = ", ".join(
            f"{key.replace('_', ' ')}: {value}"
            for key, value in sorted(unavailable_breakdown.items())
        )
        lines.append(f"- Unavailable breakdown: {formatted}")
    lines.extend(
        [
            "",
            "## Bucket Coverage",
            "",
            "| Bucket | Checked | Total | Failed |",
            "|---|---:|---:|---:|",
        ]
    )
    for bucket, counts in sorted((summary.get("bucket_counts") or {}).items()):
        lines.append(
            f"| {bucket} | {counts.get('checked', 0)} | {counts.get('total', 0)} | {counts.get('failed_after_retry', 0)} |"
        )
    lines.extend(["", "## Checked Sources", ""])
    for source in payload.get("sources") or []:
        lines.append(
            f"- `{source.get('source_id')}` - {source.get('name')} - {source.get('status')} - {source.get('url')}"
        )
    return "\n".join(lines).rstrip() + "\n"


def generate_source_audit(
    date: str,
    *,
    catalog_path: Path | None = None,
    catalog_paths: list[Path] | None = None,
    fixture: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    started = time.time()
    started_iso = datetime.fromtimestamp(started).astimezone().isoformat()
    generated_at = iso_now()
    ensure_run_attempt(date)
    if catalog_paths:
        catalog = parse_source_catalogs([Path(path) for path in catalog_paths])
    elif catalog_path:
        catalog = parse_source_catalog(catalog_path)
    else:
        catalog = parse_source_catalogs()  # the active edition's catalogs
    fixture_payload = load_fixture(fixture)

    # Source health: sources judged permanently unreachable are not fetched (or counted against the audit) any more; a retired source
    # is probed again every REPROBE_DAYS days and reinstated the first time it answers. Fixture runs never touch the memory.
    health_state: dict[str, Any] | None = None
    probe_ids: set[str] = set()
    full_catalog = catalog
    if not fixture_payload and os.environ.get("BRIEFING_SOURCE_HEALTH", "1") != "0":
        try:
            health_state = source_health_core.load_state(source_health_path())
            catalog, _skipped = source_health_core.filter_catalog(full_catalog, health_state, date, floor=MINIMUM_SOURCE_FLOOR + 10)
            probe_ids = source_health_core.due_for_probe(health_state, date) & {entry.source_id for entry in catalog}
        except Exception as exc:  # noqa: BLE001 - the memory is an optimisation; an unreadable file never stops the audit
            print(f"source health unavailable: {exc!r}")
            health_state, catalog, probe_ids = None, full_catalog, set()

    if fixture_payload:
        sources = [fixture_result_for(entry, fixture_payload, generated_at=generated_at) for entry in catalog]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            futures = [
                executor.submit(check_source, entry, timeout=timeout, retries=retries)
                for entry in catalog
            ]
            sources = [future.result() for future in concurrent.futures.as_completed(futures)]
        source_order = {entry.source_id: index for index, entry in enumerate(catalog)}
        sources.sort(key=lambda item: source_order.get(str(item.get("source_id")), 999999))

    health_report: dict[str, Any] | None = None
    if health_state is not None:
        try:
            health_report = source_health_core.record_results(health_state, sources, date, full_catalog)
            source_health_core.save_state(source_health_path(), health_state)
        except Exception as exc:  # noqa: BLE001
            print(f"source health not recorded: {exc!r}")
    if probe_ids:  # re-probes inform the memory only: they are not part of today's audit
        sources = [source for source in sources if str(source.get("source_id")) not in probe_ids]
        catalog = [entry for entry in catalog if entry.source_id not in probe_ids]

    summary = build_summary(sources, catalog_count=len(catalog))
    status = derive_status(summary, fixture=bool(fixture_payload))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "status": status,
        "request_source": "fixture" if fixture_payload else "live_http",
        "generated_at": generated_at,
        "catalog_path": str(catalog_path or (catalog_paths[0] if catalog_paths else default_catalog_paths()[0])),
        "catalog_paths": [str(path) for path in (catalog_paths or ([catalog_path] if catalog_path else default_catalog_paths()))],
        "edition": edition_core.load_edition().edition_id,
        "summary": summary,
        "sources": sources,
    }
    if health_report is not None and health_state is not None:
        payload["source_health"] = source_health_core.summary_for_report(health_report)
        try:
            health_md = source_audit_path(date).with_name(source_audit_path(date).name.replace("source-audit", "source-health").replace(".json", ".md"))
            health_md.write_text(source_health_core.render_markdown(health_state, health_report), encoding="utf-8")
        except OSError:
            pass

    json_path = source_audit_path(date)
    markdown_path = source_audit_markdown_path(date)
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(build_markdown(payload), encoding="utf-8")
    try:
        from research_vault import sync_source_audit

        vault_summary = sync_source_audit(date, payload)
    except Exception as exc:  # pragma: no cover - defensive reporting path
        vault_summary = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        from research_depth import generate_research_depth_plan

        depth_plan = generate_research_depth_plan(date, source_audit=payload)
        depth_summary = {
            "status": depth_plan.get("status"),
            "planned_query_count": (depth_plan.get("summary") or {}).get("planned_query_count", 0),
            "follow_up_count": (depth_plan.get("summary") or {}).get("follow_up_count", 0),
            "gap_count": (depth_plan.get("summary") or {}).get("gap_count", 0),
        }
    except Exception as exc:  # pragma: no cover - defensive reporting path
        depth_summary = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        from adaptive_fetch import write_adaptive_fetch_report

        adaptive_fetch_summary = write_adaptive_fetch_report(date, payload)
    except Exception as exc:  # pragma: no cover - defensive reporting path
        adaptive_fetch_summary = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    ended = time.time()
    timing_status = "failed" if status == "failed" else ("warning" if status == "degraded" else "passed")
    update_run_report(
        date,
        source_audit={
            "status": status,
            "request_source": payload["request_source"],
            "artifact_path": str(json_path),
            "markdown_path": str(markdown_path),
            "catalog_count": summary["catalog_count"],
            "checked_count": summary["checked_count"],
            "failed_after_retry_count": summary["failed_after_retry_count"],
            "unavailable_count": summary["unavailable_count"],
            "unavailable_breakdown": summary.get("unavailable_breakdown", {}),
            "all_sources_checked": summary["all_sources_checked"],
            "systemic_failure": summary["systemic_failure"],
        },
        research_vault=vault_summary,
        research_depth=depth_summary,
        adaptive_fetch=adaptive_fetch_summary,
    )
    set_step_timing(
        date,
        "source_audit",
        status=timing_status,
        started_at=started_iso,
        ended_at=datetime.fromtimestamp(ended).astimezone().isoformat(),
        duration_seconds=round(ended - started, 3),
        details={
            "artifact_path": str(json_path),
            "markdown_path": str(markdown_path),
            "catalog_count": summary["catalog_count"],
            "checked_count": summary["checked_count"],
            "unavailable_breakdown": summary.get("unavailable_breakdown", {}),
            "adaptive_fetch": adaptive_fetch_summary,
        },
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the daily briefing source catalog and write source-audit artifacts.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    parser.add_argument(
        "--catalog",
        type=Path,
        action="append",
        default=None,
        help="Override the catalog path (repeatable). Default: the active edition's catalogs.",
    )
    parser.add_argument("--fixture", type=Path, default=None, help="Fixture JSON for tests or no-network smoke checks.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-source HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retry count for transient failures.")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="Maximum concurrent source checks.")
    args = parser.parse_args()

    payload = generate_source_audit(
        args.date,
        catalog_paths=args.catalog,
        fixture=args.fixture,
        timeout=args.timeout,
        retries=args.retries,
        max_workers=args.max_workers,
    )
    print(source_audit_path(args.date))
    summary = payload["summary"]
    print(
        f"Source audit {payload['status']}: "
        f"{summary['checked_count']}/{summary['catalog_count']} checked, "
        f"{summary['failed_after_retry_count']} failed after retry, "
        f"{summary['unavailable_count']} unavailable"
    )
    return 1 if payload["status"] == "failed" else 0


__all__ = [
    "CHECKED_STATUSES",
    "MINIMUM_SOURCE_FLOOR",
    "SourceCatalogEntry",
    "build_markdown",
    "build_summary",
    "check_source",
    "derive_status",
    "generate_source_audit",
    "main",
    "parse_source_catalog",
    "parse_source_catalogs",
    "extract_headlines",
    "credit_sections_for_bucket",
    "stable_source_id",
    "unavailable_reason",
]


if __name__ == "__main__":
    raise SystemExit(main())
