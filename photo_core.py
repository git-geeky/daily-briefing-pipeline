"""Licensed-photograph stage for editions that publish per-article photographs (animal).

Deterministic and token-free: for each story the slate supplies photo queries (scientific
name first); this module searches Wikimedia Commons, iNaturalist and Openverse, accepts only
photographs under an allowed licence (CC0, public domain, CC BY, CC BY-SA), downloads and
verifies the file, and writes a caption whose licence/creator/source come straight from the
provider's metadata. Nothing about a licence is inferred; a candidate whose licence cannot be
normalised is rejected and the rejection is recorded.

The result is an ``image-report.json`` in the same shape the fetch-images stage writes (so the
manifest sees these as high-confidence, non-placeholder images) plus ready-to-insert markdown
image lines. Writers never author image lines for this edition.

CLI:
    python photo_core.py --date 2026-10-26 --slate slate.json [--apply]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import edition_core  # noqa: E402

DEFAULT_LICENSES = ("cc0", "pd", "cc-by", "cc-by-sa")
PROVIDER_PRIORITY = {"wikimedia": 3.0, "inaturalist": 2.0, "openverse": 1.0}
# Subjects that are almost never what a reader wants as "the photograph of the animal".
AVOID_TITLE_RE = re.compile(
    r"\b(?:skeleton|skull|taxidermy|specimens?|drawing|illustration|painting|map|distribution map|range map|"
    r"stamp|coin|diagram|logo|flag|poster|cover|bookplate|engraving|lithograph|sketch|cartoon|icon|silhouette|"
    r"tracks?|footprints?|scat|dung|pelt|museum|exhibit|lithographs?|plates?|woodcuts?|etchings?|watercolou?rs?|gouache|prints?|"
    r"mezzotint|chromolithograph|frontispiece|manuscript)\b",
    re.IGNORECASE,
)
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_STORED_WIDTH = 1800
USER_AGENT = os.environ.get(
    "BRIEFING_HTTP_USER_AGENT",
    "AnimalBriefingPhotoStage/1.0 (https://github.com/git-geeky/daily-briefing-pipeline; briefing automation)",
)


class Fetcher(Protocol):
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any: ...
    def get_bytes(self, url: str) -> tuple[bytes, str]: ...


@dataclass
class PhotoCandidate:
    provider: str
    title: str
    creator: str
    license: str
    license_family: str
    width: int
    height: int
    image_url: str
    page_url: str
    description: str = ""
    categories: str = ""
    taxon: str = ""
    year: int | None = None
    score: float = 0.0
    reject_reason: str | None = None


@dataclass
class StoryPhotos:
    story_id: str
    queries: list[str]
    chosen: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    status: str = "missing"
    markdown_lines: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Licence handling — the only place a licence label is produced.
# --------------------------------------------------------------------------------------------
_REJECT_LICENSE_RE = re.compile(
    r"\b(?:nc|nd)\b|non[- ]?commercial|no[- ]?deriv|all rights reserved|fair use|unfree|gfdl only", re.IGNORECASE
)
_BY_SA_RE = re.compile(r"^(?:cc[ -])?by[ -]sa(?:[ -](\d\.\d))?(?![\w])")
_BY_RE = re.compile(r"^(?:cc[ -])?by(?:[ -](\d\.\d))?(?![\w-])")


def normalize_license(raw: str | None, version: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(label, family)`` for an allowed licence, else ``(None, None)``.

    Anything NC/ND, "all rights reserved", fair-use, or unknown is not allowed. A version is only
    ever copied from the provider's metadata; if the provider gives none the label carries none.
    """
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return None, None
    lowered = text.lower().replace("_", " ")
    if _REJECT_LICENSE_RE.search(lowered):
        return None, None
    if lowered.startswith("cc0") or lowered in {"cc-zero", "zero"}:
        return "CC0 1.0", "cc0"
    if lowered in {"pd", "pdm"} or lowered.startswith(("public domain", "pd-", "pd ")):
        return "Public domain", "pd"
    match = _BY_SA_RE.match(lowered)
    if match:
        found = match.group(1) or version
        return "CC BY-SA" + (f" {found}" if found else ""), "cc-by-sa"
    match = _BY_RE.match(lowered)
    if match:
        found = match.group(1) or version
        return "CC BY" + (f" {found}" if found else ""), "cc-by"
    return None, None


def strip_html(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


# --------------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------------
def search_wikimedia(fetcher: Fetcher, query: str, *, limit: int = 12) -> list[PhotoCandidate]:
    data = fetcher.get_json(
        "https://commons.wikimedia.org/w/api.php",
        {
            "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo|categories", "cllimit": 30,
            "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600,
        },
    )
    out: list[PhotoCandidate] = []
    for page in ((data or {}).get("query") or {}).get("pages", {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        value = lambda key: (meta.get(key) or {}).get("value")  # noqa: E731
        label, family = normalize_license(value("LicenseShortName"))
        categories = " ".join(c.get("title", "") for c in page.get("categories", []))
        candidate = PhotoCandidate(
            provider="wikimedia",
            title=page.get("title", "").removeprefix("File:"),
            creator=strip_html(value("Artist")) or strip_html(value("Credit")) or "Unknown author",
            license=label or str(value("LicenseShortName") or "unknown"),
            license_family=family or "",
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            image_url=info.get("thumburl") or info.get("url") or "",
            page_url=info.get("descriptionurl") or "",
            description=strip_html(value("ImageDescription")),
            categories=categories,
        )
        year_match = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(value("DateTimeOriginal") or value("DateTime") or ""))
        candidate.year = int(year_match.group(1)) if year_match else None
        if info.get("mime") not in {"image/jpeg", "image/webp"}:
            candidate.reject_reason = f"mime {info.get('mime')} is not a photograph format"
        elif not family:
            candidate.reject_reason = f"licence not allowed: {value('LicenseShortName')!r}"
        elif str(value("NonFree") or "").lower() == "true" or (value("Restrictions") or "").strip():
            candidate.reject_reason = f"restrictions present: {value('Restrictions')!r}"
        out.append(candidate)
    return out


def search_inaturalist(fetcher: Fetcher, taxon_name: str, *, licenses: tuple[str, ...], limit: int = 10) -> list[PhotoCandidate]:
    codes = {"cc0": "cc0", "cc-by": "cc-by", "cc-by-sa": "cc-by-sa"}
    wanted = ",".join(codes[f] for f in licenses if f in codes)
    if not wanted:
        return []
    data = fetcher.get_json(
        "https://api.inaturalist.org/v1/observations",
        {"taxon_name": taxon_name, "photo_license": wanted, "quality_grade": "research", "per_page": limit,
         "order_by": "votes", "photos": "true"},
    )
    out: list[PhotoCandidate] = []
    for obs in (data or {}).get("results", []):
        taxon = (obs.get("taxon") or {}).get("name") or ""
        for photo in (obs.get("photos") or [])[:1]:
            label, family = normalize_license(photo.get("license_code"))
            dims = photo.get("original_dimensions") or {}
            url = (photo.get("url") or "").replace("square", "large")
            who = (obs.get("user") or {}).get("name") or (obs.get("user") or {}).get("login") or "iNaturalist contributor"
            candidate = PhotoCandidate(
                provider="inaturalist",
                title=f"{taxon} observation {obs.get('id')}",
                creator=who,
                license=label or str(photo.get("license_code")),
                license_family=family or "",
                width=int(dims.get("width") or 0),
                height=int(dims.get("height") or 0),
                image_url=url,
                page_url=obs.get("uri") or f"https://www.inaturalist.org/observations/{obs.get('id')}",
                description=f"{(obs.get('taxon') or {}).get('preferred_common_name') or taxon} (research-grade observation)",
                taxon=taxon,
            )
            if not family:
                candidate.reject_reason = f"licence not allowed: {photo.get('license_code')!r}"
            out.append(candidate)
    return out


def search_openverse(fetcher: Fetcher, query: str, *, limit: int = 10) -> list[PhotoCandidate]:
    data = fetcher.get_json(
        "https://api.openverse.org/v1/images/",
        {"q": query, "license": "cc0,pdm,by,by-sa", "page_size": limit, "category": "photograph"},
    )
    out: list[PhotoCandidate] = []
    for item in (data or {}).get("results", []):
        label, family = normalize_license(item.get("license"), item.get("license_version"))
        candidate = PhotoCandidate(
            provider="openverse",
            title=item.get("title") or "",
            creator=item.get("creator") or "Unknown author",
            license=label or str(item.get("license")),
            license_family=family or "",
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
            image_url=item.get("url") or "",
            page_url=item.get("foreign_landing_url") or "",
            description=" ".join(t.get("name", "") for t in (item.get("tags") or [])[:12]),
        )
        if not family:
            candidate.reject_reason = f"licence not allowed: {item.get('license')!r}"
        out.append(candidate)
    return out


# --------------------------------------------------------------------------------------------
# Scoring and selection
# --------------------------------------------------------------------------------------------
def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", text.lower())}


def score_candidate(candidate: PhotoCandidate, *, species_tokens: set[str], allow_words: tuple[str, ...], min_width: int,
                    prefer_aspect: tuple[float, float]) -> float:
    haystack = f"{candidate.title} {candidate.description} {candidate.categories} {candidate.taxon}".lower()
    if candidate.reject_reason:
        return -1.0
    for match in AVOID_TITLE_RE.finditer(haystack):
        if match.group(0).lower() not in allow_words:
            candidate.reject_reason = f"subject looks like non-photographic or off-subject material ({match.group(0)!r})"
            return -1.0
    if candidate.year and candidate.year < 1960 and "historical" not in allow_words:
        candidate.reject_reason = f"historic image ({candidate.year}); the briefing wants contemporary photographs"
        return -1.0
    if candidate.width and candidate.width < min_width:
        candidate.reject_reason = f"too small ({candidate.width}px < {min_width}px)"
        return -1.0
    overlap = len(species_tokens & _tokens(haystack))
    if species_tokens and overlap == 0 and candidate.provider != "inaturalist":
        candidate.reject_reason = "title/description/categories never mention the species"
        return -1.0
    score = PROVIDER_PRIORITY.get(candidate.provider, 0.0)
    score += min(overlap, 4) * 0.75
    if candidate.width and candidate.height:
        aspect = candidate.width / candidate.height
        score += 1.0 if prefer_aspect[0] <= aspect <= prefer_aspect[1] else 0.0
        score += min(candidate.width / 4000.0, 1.0)
    if candidate.license_family in {"cc0", "pd"}:
        score += 0.4  # no attribution friction
    return score


def species_tokens_for(queries: list[str]) -> set[str]:
    tokens: set[str] = set()
    for query in queries:
        tokens |= _tokens(query)
    return tokens - {"photo", "photograph", "wild", "adult", "male", "female"}


# --------------------------------------------------------------------------------------------
# Download + caption
# --------------------------------------------------------------------------------------------
def _slug(text: str, limit: int = 40) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "photo")


def make_caption(candidate: PhotoCandidate, template: str) -> str:
    description = candidate.description or candidate.title
    description = re.sub(r"\.(jpe?g|png|webp)$", "", description, flags=re.IGNORECASE)
    description = re.sub(r"[\[\]\r\n]+", " ", description).strip().rstrip(".")
    if len(description) > 160:
        description = description[:157].rstrip() + "..."
    safe_url = candidate.page_url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    provider = {"wikimedia": "Wikimedia Commons", "inaturalist": "iNaturalist", "openverse": "Openverse"}.get(
        candidate.provider, candidate.provider
    )
    creator = re.sub(r"[\[\]\r\n]+", " ", candidate.creator).strip() or "Unknown author"
    return template.format(
        description=description, creator=creator, license=candidate.license, provider=provider, page_url=safe_url
    )


def _image_ok(data: bytes) -> tuple[int, int, str] | None:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return None
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            return image.width, image.height, image.format or ""
    except Exception:  # noqa: BLE001 - any decode failure means unusable
        return None


def _store_jpeg(data: bytes, destination: Path) -> tuple[int, int, str]:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        if image.width > MAX_STORED_WIDTH:
            height = round(image.height * MAX_STORED_WIDTH / image.width)
            image = image.resize((MAX_STORED_WIDTH, height))
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, "JPEG", quality=88, optimize=True)
        width, height = image.width, image.height
    return width, height, hashlib.sha256(destination.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------------
# Stage
# --------------------------------------------------------------------------------------------
def photos_for_story(
    fetcher: Fetcher,
    story: dict,
    *,
    image_dir: Path,
    policy: dict,
    per_min: int,
    per_max: int,
    used_pages: set[str],
) -> StoryPhotos:
    queries = [q for q in (story.get("photo_queries") or []) if str(q).strip()]
    result = StoryPhotos(story_id=str(story["story_id"]), queries=queries)
    if not queries:
        result.rejected.append({"reason": "slate supplied no photo_queries"})
        return result
    licenses = tuple(policy.get("licenses") or DEFAULT_LICENSES)
    min_width = int(policy.get("min_width", 1200))
    prefer = tuple(policy.get("prefer_aspect") or (1.2, 1.9))
    template = str(policy.get("caption_template") or "{description}. Photo: {creator}, {license}, via {provider} ({page_url})")
    allow_words = tuple(str(w).lower() for w in story.get("photo_allow_words") or ())
    species = species_tokens_for(queries[:2])
    providers = [p for p in policy.get("providers") or ("wikimedia", "inaturalist", "openverse") if p in PROVIDER_PRIORITY]

    pool: list[PhotoCandidate] = []
    for query in queries:
        for provider in providers:
            try:
                if provider == "wikimedia":
                    found = search_wikimedia(fetcher, query)
                elif provider == "inaturalist":
                    found = search_inaturalist(fetcher, query, licenses=licenses)
                else:
                    found = search_openverse(fetcher, query)
            except Exception as exc:  # noqa: BLE001 - a provider outage must not stop the stage
                result.rejected.append({"provider": provider, "query": query, "reason": f"provider error: {exc!r}"[:200]})
                continue
            pool.extend(found)
        usable = [c for c in pool if c.license_family in licenses and c.page_url not in used_pages and not c.reject_reason]
        if len(usable) >= per_max * 3:
            break

    scored: list[PhotoCandidate] = []
    for candidate in pool:
        if candidate.license_family and candidate.license_family not in licenses:
            candidate.reject_reason = candidate.reject_reason or f"licence family {candidate.license_family} not in policy"
        candidate.score = score_candidate(
            candidate, species_tokens=species, allow_words=allow_words, min_width=min_width, prefer_aspect=prefer
        )
        if candidate.page_url in used_pages and not candidate.reject_reason:
            candidate.reject_reason = "already used by another article in this briefing"
            candidate.score = -1.0
        if candidate.score < 0:
            result.rejected.append({"title": candidate.title[:90], "provider": candidate.provider, "page_url": candidate.page_url,
                                    "reason": candidate.reject_reason})
        else:
            scored.append(candidate)
    scored.sort(key=lambda c: c.score, reverse=True)

    seen_hashes: set[str] = set()
    for candidate in scored:
        if len(result.chosen) >= per_max:
            break
        try:
            data, content_type = fetcher.get_bytes(candidate.image_url)
        except Exception as exc:  # noqa: BLE001
            result.rejected.append({"title": candidate.title[:90], "provider": candidate.provider, "reason": f"download failed: {exc!r}"[:160]})
            continue
        if len(data) > MAX_DOWNLOAD_BYTES or not content_type.lower().startswith("image/"):
            result.rejected.append({"title": candidate.title[:90], "provider": candidate.provider, "reason": f"bad download ({content_type}, {len(data)} bytes)"})
            continue
        probe = _image_ok(data)
        if not probe:
            result.rejected.append({"title": candidate.title[:90], "provider": candidate.provider, "reason": "file is not a decodable image"})
            continue
        if probe[0] < min(min_width, 900):
            result.rejected.append({"title": candidate.title[:90], "provider": candidate.provider, "reason": f"downloaded image too small ({probe[0]}px)"})
            continue
        stem = f"{_slug(result.story_id, 28)}-{len(result.chosen) + 1}-{hashlib.sha256(data).hexdigest()[:8]}"
        path = image_dir / f"{stem}.jpg"
        width, height, digest = _store_jpeg(data, path)
        if digest in seen_hashes:
            path.unlink(missing_ok=True)
            continue
        seen_hashes.add(digest)
        caption = make_caption(candidate, template)
        used_pages.add(candidate.page_url)
        entry = {**asdict(candidate), "local_path": str(path), "caption": caption, "stored_width": width,
                 "stored_height": height, "sha256": digest}
        result.chosen.append(entry)
        result.markdown_lines.append(f"![{caption}]({path})")
    result.status = "ok" if len(result.chosen) >= max(per_min, 1) else "missing"
    return result


class RequestsFetcher:
    """Plain ``requests`` fetcher: descriptive User-Agent, per-host spacing, ``Retry-After`` aware retries."""

    MIN_INTERVAL_SECONDS = 1.0
    MAX_RETRY_AFTER_SECONDS = 45.0

    def __init__(self, *, timeout: float = 40.0, retries: int = 3, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        import requests

        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
        self.timeout, self.retries, self._sleep, self._clock = timeout, retries, sleep, clock
        self._last_request: dict[str, float] = {}

    def _space(self, url: str) -> None:
        host = re.sub(r"^https?://", "", url).split("/", 1)[0]
        wait = self.MIN_INTERVAL_SECONDS - (self._clock() - self._last_request.get(host, -1e9))
        if wait > 0:
            self._sleep(wait)
        self._last_request[host] = self._clock()

    def _get(self, url: str, params: dict[str, Any] | None = None):
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self._space(url)
            try:
                response = self._session.get(url, params=params, timeout=self.timeout)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    retry_after = response.headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt * 3
                    self._sleep(min(delay, self.MAX_RETRY_AFTER_SECONDS))
                    continue
                response.raise_for_status()
                return response
            except self._requests.RequestException as exc:
                last = exc
                if attempt < self.retries:
                    self._sleep(2 ** attempt * 2)
        raise RuntimeError(f"GET {url} failed: {last!r}")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._get(url, params).json()

    def get_bytes(self, url: str) -> tuple[bytes, str]:
        response = self._get(url)
        return response.content, response.headers.get("content-type", "")


def run_photo_stage(
    stories: list[dict],
    *,
    image_dir: Path,
    report_path: Path | None = None,
    fetcher: Fetcher | None = None,
    edition: edition_core.Edition | None = None,
    date: str | None = None,
) -> dict:
    edition = edition or edition_core.load_edition()
    policy = dict(edition.extra.get("photos") or {})
    per_min = int(edition.images.get("per_article_min", 1) or 1)
    per_max = int(edition.images.get("per_article_max", 2) or 2)
    max_total = int(edition.images.get("max_total", 0) or 0)
    fetcher = fetcher or RequestsFetcher()
    image_dir.mkdir(parents=True, exist_ok=True)

    used_pages: set[str] = set()
    outcomes: dict[str, StoryPhotos] = {}
    total = 0
    for story in stories:
        remaining = (max_total - total) if max_total else per_max
        if remaining <= 0:
            outcomes[str(story["story_id"])] = StoryPhotos(str(story["story_id"]), [], rejected=[{"reason": "max_total reached"}])
            continue
        outcome = photos_for_story(
            fetcher, story, image_dir=image_dir, policy=policy, per_min=per_min,
            per_max=min(per_max, remaining), used_pages=used_pages,
        )
        total += len(outcome.chosen)
        outcomes[outcome.story_id] = outcome

    details = []
    for outcome in outcomes.values():
        for entry in outcome.chosen:
            details.append({
                "local_path": entry["local_path"],
                "origin_type": "licensed_photo",
                "confidence": 0.95,
                "relevance_reason": f"{entry['provider']} photograph matched on {', '.join(outcome.queries[:2])}; licence {entry['license']}",
                "accepted_by_rule": "licensed_photo",
                "story_id": outcome.story_id,
                "provider": entry["provider"],
                "license": entry["license"],
                "creator": entry["creator"],
                "page_url": entry["page_url"],
                "source_url": entry["image_url"],
                "sha256": entry["sha256"],
                "width": entry["stored_width"],
                "height": entry["stored_height"],
            })
    report = {
        "schema_version": 1,
        "date": date,
        "stage": "photo_core",
        "total_images": len(details),
        "stories_missing_photo": [s.story_id for s in outcomes.values() if s.status != "ok"],
        "details": details,
        "stories": {sid: {"queries": o.queries, "status": o.status, "chosen": [e["local_path"] for e in o.chosen],
                          "rejected": o.rejected[:40]} for sid, o in outcomes.items()},
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report["_outcomes"] = {sid: o.markdown_lines for sid, o in outcomes.items()}
    return report


_STORY_MARKER_LINE = re.compile(r"(?m)^[ \t]*<!--\s*story:[^>]*-->[ \t]*$")
_IMAGE_LINE = re.compile(r"(?m)^[ \t]*!\[[^\]]*\]\([^)]*\)[ \t]*\n?")


def insert_photos(article_markdown: str, markdown_lines: list[str]) -> str:
    """Replace any images in the article with the stage's captioned photographs.

    Photos go directly after the title (and story marker, if present). With no photographs
    the article is returned with its images stripped and the caller adds the failure marker.
    """
    text = _IMAGE_LINE.sub("", article_markdown)
    if not markdown_lines:
        return text
    lines = text.split("\n")
    insert_at = 1 if lines and lines[0].startswith("## ") else 0
    if insert_at and len(lines) > 1 and _STORY_MARKER_LINE.match(lines[1]):
        insert_at = 2
    block = ["", *sum(([line, ""] for line in markdown_lines), [])]
    return "\n".join(lines[:insert_at] + block + lines[insert_at:]).replace("\n\n\n", "\n\n")


def main() -> int:  # pragma: no cover - thin CLI over run_photo_stage
    parser = argparse.ArgumentParser(description="Fetch licensed photographs for the day's slate.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--slate", type=Path, required=True, help="JSON list (or {'stories': [...]}) with story_id and photo_queries")
    args = parser.parse_args()
    import briefing_paths

    payload = json.loads(args.slate.read_text(encoding="utf-8"))
    stories = payload.get("stories") if isinstance(payload, dict) else payload
    image_dir = briefing_paths.image_cache_dir(args.date)
    report = run_photo_stage(stories, image_dir=image_dir, report_path=image_dir / "image-report.json", date=args.date)
    print(f"photos: {report['total_images']} stored; missing: {', '.join(report['stories_missing_photo']) or 'none'}")
    return 0 if not report["stories_missing_photo"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
