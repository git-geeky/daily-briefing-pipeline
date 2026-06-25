"""
fetch_briefing_images.py — Pre-fetch, validate, and remediate briefing images.

Runs as Step 1.9 of the daily-intelligence-briefing pipeline, AFTER the
fact-check subagent and BEFORE the header subagent + PDF build. Its job is
to eliminate the "hammer Wikimedia at PDF build time" failure mode that
produced the 2026-04-16 run's ~90% image-loss due to upstream 429s.

Pipeline (per SKILL.md Step 1.9):
  Phase 1 — Validate every ![alt](url) tag by downloading via a rate-
           limited, UA-identified HTTP client. Cache under the same
           md5(url)[:16].{ext} scheme that build_pdf.py uses, so a
           partial rewrite still benefits from the cache.
  Phase 2 — For failures, fetch the article's in-body source URLs and
           extract og:image / twitter:image meta tags.
  Phase 3 — Pluggable image-source pipeline with short-circuit fallback.
           Runs a progression of query variants (alt, alt-truncated, mixed,
           title-only) against each enabled source until one lands an
           image. Sources without a provisioned API key return [] silently.
             - commons        (no auth)    Wikimedia Commons — primary
             - loc            (no auth)    Library of Congress — historical
             - dvids          (key)        DVIDS — US DoD defense imagery
             - smithsonian    (key)        Smithsonian Open Access
             - europeana      (key)        Europeana — European cultural
             - flickr         (key)        Flickr (incl. British Library)
             - unsplash       (key)        Unsplash — modern photojournalism
             - pexels         (key)        Pexels — free photojournalism
             - pixabay        (key)        Pixabay — generic stock
  Phase 4 — Rewrite the fragment in-place: successful images become local
           Windows paths; unrecoverable tags are removed (no holes in
           the PDF).

  Zero-image articles remain imageless by default. A manual opt-in flag can
  still enable the old best-effort Phase 3 lookup for empty articles when a
  user explicitly wants that tradeoff.

  A JSON audit report lands in the run-specific image cache directory.

API keys: keyed sources read from env vars, auto-loaded from
~/.claude/briefing-api-keys.env at import time (see the .env.template
shipped in the repo for registration URLs). Missing keys skip the source
silently — safe to run with zero keys provisioned.

Usage:
    py -3.12 fetch_briefing_images.py --date 2026-04-17
    py -3.12 fetch_briefing_images.py --date 2026-04-17 --dry-run

Dependencies: requests, beautifulsoup4, lxml (pre-installed on 3.12).
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from briefing_manifest import (
    HIGH_CONFIDENCE_IMAGE_THRESHOLD as HIGH_CONFIDENCE_THRESHOLD,
    LOW_CONFIDENCE_IMAGE_THRESHOLD as LOW_CONFIDENCE_THRESHOLD,
    RASTER_IMAGE_EXTENSIONS as ACCEPTED_EXTS,
)
from briefing_paths import SECTIONS as ALL_SECTIONS, ensure_layout, fragment_path, image_cache_dir
from run_reporting import set_step_timing, update_run_report


# ── API key loader ───────────────────────────────────────
# Keys live in ~/.claude/briefing-api-keys.env as KEY=value lines. Not tracked
# in git (the .env file path is outside the repo). Missing-key sources skip
# silently at search time so the pipeline degrades gracefully rather than
# erroring out. Env vars already set in the shell take precedence.
_API_KEYS_FILE = Path.home() / ".claude" / "briefing-api-keys.env"


def _load_api_keys_file() -> None:
    if not _API_KEYS_FILE.exists():
        return
    for raw in _API_KEYS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and os.environ.get(key) is None:
            os.environ[key] = value


_load_api_keys_file()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# ── Constants ────────────────────────────────────────────
JST = timezone(timedelta(hours=9))
ensure_layout()

# Wikimedia's User-Agent policy (meta.wikimedia.org/wiki/User-Agent_policy)
# requires a descriptive UA with contact info. Generic Mozilla UAs are
# actively blocked on upload.wikimedia.org for bulk bot traffic.
USER_AGENT = (
    os.environ.get("BRIEFING_IMAGE_USER_AGENT")
    or "DailyBriefingImageFetcher/1.0 python-requests/{rv}".format(rv=requests.__version__)
)

# ACCEPTED_EXTS = canonical RASTER_IMAGE_EXTENSIONS, imported from briefing_manifest.
DOWNLOADABLE_EXTS = ACCEPTED_EXTS + (".svg",)
MIN_IMAGE_BYTES = 200
# Content sections only: derived from the canonical briefing_paths.SECTIONS, minus
# "header" (the header fragment has no articles to fetch/hydrate images for). Single
# source of truth for section names; the header exclusion is the deliberate difference.
SECTIONS = tuple(section for section in ALL_SECTIONS if section != "header")
# HIGH_CONFIDENCE_THRESHOLD / LOW_CONFIDENCE_THRESHOLD imported from briefing_manifest
# (canonical IMAGE confidence thresholds) at the top of this module.

# Per-host minimum gap between consecutive requests (seconds). Wikimedia
# hosts are throttled hard; everyone else gets gentle politeness.
HOST_RATE_LIMITS = {
    "upload.wikimedia.org": 1.0,
    "commons.wikimedia.org": 1.0,
    "en.wikipedia.org": 1.0,
    "api.wikimedia.org": 1.0,
}
DEFAULT_RATE_LIMIT = 0.25

STOP_WORDS = set(
    "the a an of for and or but in on at to from with about over under by as "
    "is are was were be been being this that these those it its their them "
    "his her he she we they you your our i me my said says say new 2026 2025 "
    "article ceo also more most very just like only even many much some any "
    "how what when where why while during before after since until through "
    "into onto upon inside outside between among against amid amidst"
    .split()
)

# ── Rate limiter ─────────────────────────────────────────
_last_request_by_host: dict[str, float] = defaultdict(float)


def throttle(url: str) -> None:
    host = urlparse(url).netloc
    delay = HOST_RATE_LIMITS.get(host, DEFAULT_RATE_LIMIT)
    elapsed = time.monotonic() - _last_request_by_host[host]
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_request_by_host[host] = time.monotonic()


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
        }
    )
    return s


# ── Phase 1: direct download ─────────────────────────────
def cache_path_for(url: str, img_cache: Path) -> Path:
    """Match build_pdf.py's hashing scheme exactly — md5(url)[:16].{ext}."""
    clean = url.split("?")[0].split("#")[0]
    tail = clean.rsplit("/", 1)[-1]
    ext = "." + tail.rsplit(".", 1)[-1].lower() if "." in tail else ".jpg"
    if ext not in DOWNLOADABLE_EXTS:
        ext = ".jpg"
    h = hashlib.md5(url.encode()).hexdigest()[:16]
    return img_cache / f"{h}{ext}"


def download_image(sess: requests.Session, url: str, img_cache: Path) -> Path | None:
    """Download `url` into img_cache. Return local Path, or None on failure.
    Idempotent: returns cached path without a request if the file is already there."""
    if not url.startswith(("http://", "https://")):
        return None

    svg_path = cache_path_for(f"{url}#svg", img_cache).with_name(f"{hashlib.md5(url.encode()).hexdigest()[:16]}.svg")
    if svg_path.exists() and svg_path.stat().st_size > 0:
        return svg_path

    path = cache_path_for(url, img_cache)
    if path.exists() and (path.suffix.lower() == ".svg" or path.stat().st_size >= MIN_IMAGE_BYTES):
        return path

    for attempt in range(3):
        throttle(url)
        try:
            r = sess.get(url, timeout=25, allow_redirects=True)
        except requests.RequestException as e:
            print(f"  ! network error attempt {attempt + 1}: {url[:80]}  ({type(e).__name__}: {e})")
            time.sleep(2 ** (attempt + 1))
            continue

        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "0") or 0) or (2 ** (attempt + 2))
            print(f"  ! 429 from {urlparse(url).netloc} — sleeping {retry_after}s before retry")
            time.sleep(retry_after)
            continue
        if 500 <= r.status_code < 600:
            print(f"  ! {r.status_code} from {urlparse(url).netloc} — backoff")
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code != 200:
            print(f"  ! HTTP {r.status_code} for {url[:80]}")
            return None

        data = r.content
        ctype = r.headers.get("Content-Type", "").lower()
        if "svg" in ctype or _looks_like_svg_bytes(data):
            path = img_cache / f"{hashlib.md5(url.encode()).hexdigest()[:16]}.svg"
        if path.suffix.lower() != ".svg" and len(data) < MIN_IMAGE_BYTES:
            print(f"  ! undersized ({len(data)} bytes) {url[:80]}")
            return None
        if ctype and "image" not in ctype and "octet-stream" not in ctype:
            print(f"  ! non-image Content-Type {ctype!r} for {url[:80]}")
            return None
        path.write_bytes(data)
        return path

    return None


def _looks_like_svg_bytes(data: bytes) -> bool:
    return b"<svg" in data[:512].lower()


# ── Phase 2: og:image / twitter:image fallback ───────────
def extract_body_urls(text: str) -> list[str]:
    """Candidate source URLs in article body — non-image HTTP(S) URLs."""
    image_urls = set(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text))
    all_urls = re.findall(r"https?://[^\s)>\]]+", text)
    seen: set[str] = set()
    out: list[str] = []
    for u in all_urls:
        u = u.rstrip(".,;)")
        if u in image_urls:
            continue
        if any(u.lower().split("?")[0].endswith(e) for e in ACCEPTED_EXTS):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out[:8]  # cap


def extract_og_image(sess: requests.Session, url: str) -> str | None:
    """Fetch a source page and extract og:image / twitter:image meta."""
    throttle(url)
    try:
        r = sess.get(url, timeout=15, allow_redirects=True,
                     headers={"Accept": "text/html,*/*;q=0.5"})
    except requests.RequestException:
        return None
    if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
        return None
    try:
        soup = BeautifulSoup(r.text, "lxml")
    except Exception:
        return None
    for prop in ("og:image:secure_url", "og:image:url", "og:image", "twitter:image", "twitter:image:src"):
        for attr in ("property", "name"):
            m = soup.find("meta", attrs={attr: prop})
            if m and m.get("content"):
                resolved = urljoin(url, m["content"].strip())
                if resolved.startswith("http"):
                    return resolved
    return None


# ── Phase 3: image-source search pipeline ────────────────
# Each source is a callable `search(sess, query, n) -> list[url]`. Phase 3
# tries them in priority order for each query variant. Add new sources here
# by defining a function with the signature below and appending to
# IMAGE_SOURCES; the pipeline wires them in automatically.
#
# Inventory of potential sources considered on 2026-04-17:
#   ENABLED (no auth):
#     - Wikimedia Commons        — primary, broadest general coverage
#     - Library of Congress      — historical photos, maps, documents
#   REQUIRES REGISTRATION (free, not yet enabled — see docstring):
#     - Smithsonian Open Access  (api.data.gov key)
#     - Europeana                (europeana.eu key)
#     - DVIDS                    (dvidshub.net key; US DoD imagery)
#     - Pixabay, Pexels, Unsplash, Flickr (per-vendor keys)
#   NEEDS BROWSER SCRAPING (no search API):
#     - Atlantic "In Focus", Boston.com "Big Picture", Al Jazeera Galleries,
#       Texas Tribune photo essays, ProPublica, British Library Flickr,
#       National Archives (US) catalog
#   PAYWALLED / UNSUITABLE:
#     - Magnum Photos (licensing paywall)
#     - Magdeleine / Gratisography / Stocksnap (generic stock, low editorial
#       value for a news briefing)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
LOC_API = "https://www.loc.gov/search/"


def _clean_image_url_filter(url: str, mime: str = "") -> bool:
    """Shared acceptance check: absolute HTTPS with a supported extension."""
    if not url or not url.startswith("http"):
        return False
    tail = url.split("?")[0].rsplit("/", 1)[-1].lower()
    if not any(tail.endswith(e) for e in ACCEPTED_EXTS):
        return False
    if mime and mime.startswith("image/svg"):
        return False  # reportlab can't render SVG
    return True


def commons_search(sess: requests.Session, query: str, n: int = 3) -> list[dict]:
    """Search Wikipedia Commons for File:* pages matching query."""
    if not query:
        return []
    throttle(COMMONS_API)
    try:
        r = sess.get(
            COMMONS_API,
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrnamespace": 6,
                "gsrsearch": query,
                "gsrlimit": n * 2,
                "prop": "imageinfo",
                "iiprop": "url|size|mime",
                "iiurlwidth": 1280,
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! commons search network error: {e}")
        return []

    if r.status_code == 429:
        print(f"  ! 429 from Commons API — skipping this query")
        time.sleep(10)
        return []
    if r.status_code != 200:
        return []

    try:
        data = r.json()
    except ValueError:
        return []

    pages = data.get("query", {}).get("pages", {}) or {}
    results: list[dict] = []
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        url = ii.get("thumburl") or ii.get("url")
        if _clean_image_url_filter(url, ii.get("mime", "") or ""):
            results.append(
                {
                    "url": url,
                    "title": str(p.get("title") or ""),
                    "page_url": f"https://commons.wikimedia.org/wiki/{str(p.get('title') or '').replace(' ', '_')}",
                }
            )
        if len(results) >= n:
            break
    return results


def loc_search(sess: requests.Session, query: str, n: int = 3) -> list[str]:
    """Search the Library of Congress for items tagged online-format:image.
    Best for historical material (pre-2000 figures, government documents,
    war photography, maps); not a useful source for modern tech-industry
    topics since LoC doesn't catalog contemporary private figures."""
    if not query:
        return []
    throttle(LOC_API)
    try:
        r = sess.get(
            LOC_API,
            params={
                "q": query,
                "fo": "json",
                "c": n * 2,
                "fa": "online-format:image",
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! LoC search network error: {e}")
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []

    results: list[str] = []
    for item in data.get("results", []) or []:
        # image_url is a list of progressively larger URLs; take the last
        # (largest) since we normalize to 5.5"×4.0" at PDF build time.
        image_urls = item.get("image_url") or []
        if isinstance(image_urls, list) and image_urls:
            url = image_urls[-1]
            # LoC IIIF URLs sometimes have no file extension in the path.
            # They still return JPEGs — the /full/ path is a live renderer.
            # Coerce by appending .jpg if missing.
            if not any(url.split("?")[0].lower().endswith(e) for e in ACCEPTED_EXTS):
                url = url.rstrip("/") + "/default.jpg"
            if url.startswith("http"):
                results.append(url)
        if len(results) >= n:
            break
    return results


# ── Keyed sources (graceful no-op when the env var is unset) ─────────────
# Each function returns [] immediately if its API key is missing, so the
# pipeline never errors because a key isn't provisioned yet. Registration
# URLs and key-variable names are documented in the playbook that
# accompanies this script (see `briefing-api-keys.env.template`).

def smithsonian_search(sess: requests.Session, query: str, n: int = 3) -> list[str]:
    """Smithsonian Open Access — portraits, historical objects, science imagery.
    Requires SMITHSONIAN_API_KEY from api.data.gov (one key works across agencies)."""
    key = os.environ.get("SMITHSONIAN_API_KEY")
    if not key or not query:
        return []
    throttle("https://api.si.edu/")
    try:
        r = sess.get(
            "https://api.si.edu/openaccess/api/v1.0/search",
            params={"api_key": key, "q": f"{query} AND online_media_type:Images", "rows": n * 2},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! smithsonian network error: {e}")
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    results: list[str] = []
    for row in (data.get("response", {}).get("rows") or []):
        media = ((row.get("content", {}).get("descriptiveNonRepeating", {}) or {})
                 .get("online_media", {}).get("media") or [])
        for m in media:
            # Smithsonian returns multiple resource entries per media item;
            # 'idsId' + 'Screen' resource gives a viewable JPEG.
            url = None
            for res in (m.get("resources") or []):
                label = (res.get("label") or "").lower()
                if "screen" in label or "high" in label:
                    url = res.get("url")
                    break
            url = url or m.get("content")
            if _clean_image_url_filter(url):
                results.append(url)
                break
        if len(results) >= n:
            break
    return results


def europeana_search(sess: requests.Session, query: str, n: int = 3) -> list[str]:
    """Europeana — European cultural heritage. Requires EUROPEANA_API_KEY."""
    key = os.environ.get("EUROPEANA_API_KEY")
    if not key or not query:
        return []
    throttle("https://api.europeana.eu/")
    try:
        r = sess.get(
            "https://api.europeana.eu/record/v2/search.json",
            params={
                "wskey": key, "query": query, "rows": n * 2,
                "media": "true", "reusability": "open",
                "qf": "TYPE:IMAGE",
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! europeana network error: {e}")
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    results: list[str] = []
    for item in (data.get("items") or []):
        url = None
        # Prefer edmIsShownBy (full-resolution direct URL); fall back to preview.
        for candidate in (item.get("edmIsShownBy") or []):
            if _clean_image_url_filter(candidate):
                url = candidate
                break
        if not url:
            for candidate in (item.get("edmPreview") or []):
                if _clean_image_url_filter(candidate):
                    url = candidate
                    break
        if url:
            results.append(url)
        if len(results) >= n:
            break
    return results


def dvids_search(sess: requests.Session, query: str, n: int = 3) -> list[str]:
    """DVIDS — US DoD defense imagery (conflict, military, humanitarian ops).
    Requires DVIDS_API_KEY from dvidshub.net."""
    key = os.environ.get("DVIDS_API_KEY")
    if not key or not query:
        return []
    throttle("https://api.dvidshub.net/")
    try:
        r = sess.get(
            "https://api.dvidshub.net/search",
            params={"api_key": key, "q": query, "type": "image", "max_results": n * 2},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! dvids network error: {e}")
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    results: list[str] = []
    for item in (data.get("results") or []):
        # DVIDS returns a top-level 'image' object with URLs at different sizes.
        img = item.get("image") or {}
        url = img.get("src") or img.get("url")
        if _clean_image_url_filter(url):
            results.append(url)
        if len(results) >= n:
            break
    return results


def pixabay_search(sess: requests.Session, query: str, n: int = 3) -> list[str]:
    """Pixabay — generic free-license stock (low editorial density for news).
    Requires PIXABAY_API_KEY."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key or not query:
        return []
    throttle("https://pixabay.com/")
    try:
        r = sess.get(
            "https://pixabay.com/api/",
            params={"key": key, "q": query, "image_type": "photo", "per_page": max(n * 2, 3)},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! pixabay network error: {e}")
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    results: list[str] = []
    for hit in (data.get("hits") or []):
        url = hit.get("largeImageURL") or hit.get("webformatURL")
        if _clean_image_url_filter(url):
            results.append(url)
        if len(results) >= n:
            break
    return results


def pexels_search(sess: requests.Session, query: str, n: int = 3) -> list[str]:
    """Pexels — free photojournalism-quality stock. Requires PEXELS_API_KEY
    (sent via Authorization header, not query string)."""
    key = os.environ.get("PEXELS_API_KEY")
    if not key or not query:
        return []
    throttle("https://api.pexels.com/")
    try:
        r = sess.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": max(n * 2, 3)},
            headers={"Authorization": key},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! pexels network error: {e}")
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    results: list[str] = []
    for photo in (data.get("photos") or []):
        url = (photo.get("src") or {}).get("large") or (photo.get("src") or {}).get("original")
        if _clean_image_url_filter(url):
            results.append(url)
        if len(results) >= n:
            break
    return results


def unsplash_search(sess: requests.Session, query: str, n: int = 3) -> list[str]:
    """Unsplash — modern photojournalism-style free stock. Requires
    UNSPLASH_ACCESS_KEY (sent as Authorization: Client-ID ...)."""
    key = os.environ.get("UNSPLASH_ACCESS_KEY")
    if not key or not query:
        return []
    throttle("https://api.unsplash.com/")
    try:
        r = sess.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": max(n * 2, 3)},
            headers={"Authorization": f"Client-ID {key}"},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! unsplash network error: {e}")
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    results: list[str] = []
    for photo in (data.get("results") or []):
        url = (photo.get("urls") or {}).get("regular") or (photo.get("urls") or {}).get("full")
        if _clean_image_url_filter(url):
            results.append(url)
        if len(results) >= n:
            break
    return results


def flickr_search(sess: requests.Session, query: str, n: int = 3) -> list[str]:
    """Flickr — catch-all, including the British Library's Commons stream.
    Requires FLICKR_API_KEY. Filters to CC-licensed content only (license
    IDs 1-10 map to the permissive subset of Flickr's license taxonomy)."""
    key = os.environ.get("FLICKR_API_KEY")
    if not key or not query:
        return []
    throttle("https://api.flickr.com/")
    try:
        r = sess.get(
            "https://api.flickr.com/services/rest/",
            params={
                "method": "flickr.photos.search",
                "api_key": key,
                "format": "json",
                "nojsoncallback": "1",
                "text": query,
                "per_page": str(max(n * 2, 3)),
                "sort": "relevance",
                "content_type": "1",  # photos only
                "license": "1,2,3,4,5,6,7,8,9,10",
                "extras": "url_b,url_l",
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! flickr network error: {e}")
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    if data.get("stat") != "ok":
        return []
    results: list[str] = []
    for photo in ((data.get("photos") or {}).get("photo") or []):
        url = photo.get("url_b") or photo.get("url_l")
        # Fallback: synthesize the URL from id/server/secret
        if not url and photo.get("server") and photo.get("id") and photo.get("secret"):
            url = f"https://live.staticflickr.com/{photo['server']}/{photo['id']}_{photo['secret']}_b.jpg"
        if _clean_image_url_filter(url):
            results.append(url)
        if len(results) >= n:
            break
    return results


# Phase 3 source registry. Order matters — earlier sources are tried first
# per query variant, with short-circuit (first non-empty source wins).
# Sources without a provisioned API key return [] silently, so a fresh
# install with no keys falls through to Commons+LoC without drama.
#
# Priority rationale:
#   - commons        : works without auth, broadest coverage — always first
#   - loc            : historical/document material, no auth
#   - dvids          : highest editorial value for defense/conflict stories
#   - smithsonian    : portraits, scientific imagery, historical objects
#   - europeana      : European historical/cultural material
#   - flickr         : general + British Library hosted content
#   - unsplash       : modern photojournalism-style stock
#   - pexels         : photojournalism-style stock (smaller archive)
#   - pixabay        : generic stock (last resort; generic aesthetic)
IMAGE_SOURCES: list[tuple[str, callable]] = [
    ("commons", commons_search),
    ("loc", loc_search),
    ("dvids", dvids_search),
    ("smithsonian", smithsonian_search),
    ("europeana", europeana_search),
    ("flickr", flickr_search),
    ("unsplash", unsplash_search),
    ("pexels", pexels_search),
    ("pixabay", pixabay_search),
]
AUTO_PHASE3_SOURCES = {"commons"}


def normalize_tokens(text: str) -> list[str]:
    clean = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return [token for token in clean.split() if len(token) > 2 and token not in STOP_WORDS]


def phase3_candidate_decision(
    *,
    source_name: str,
    candidate_title: str,
    query: str,
    article_heading: str,
    article_body: str,
) -> dict:
    candidate_tokens = set(normalize_tokens(candidate_title))
    article_tokens = set(normalize_tokens(article_heading))
    query_tokens = set(normalize_tokens(query))
    overlap = len(candidate_tokens & article_tokens)
    query_overlap = len(candidate_tokens & query_tokens)

    if source_name != "commons":
        return {
            "accepted": False,
            "origin_type": "fallback",
            "confidence": 0.3,
            "relevance_reason": f"{source_name} is disabled for automatic current-affairs recovery",
            "accepted_by_rule": "rejected_low_trust_source",
        }

    if overlap >= 2 and query_overlap >= 2:
        confidence = min(0.94, 0.78 + 0.04 * min(overlap + query_overlap - 2, 4))
        return {
            "accepted": True,
            "origin_type": "entity_match",
            "confidence": round(confidence, 2),
            "relevance_reason": "Commons filename/title matches the main entity tokens in the article heading",
            "accepted_by_rule": "commons_exact_entity_match",
        }

    return {
        "accepted": False,
        "origin_type": "fallback",
        "confidence": 0.45 if overlap else 0.25,
        "relevance_reason": "Commons candidate title does not sufficiently match the article's primary entity tokens",
        "accepted_by_rule": "rejected_insufficient_entity_overlap",
    }


def phase3_search(sess: requests.Session, query: str, n: int = 3) -> list[dict]:
    """Short-circuit Phase 3: try each source in priority order and return
    the FIRST source's hits. Falls through only when a source returns zero
    candidates, so we don't waste a network round-trip on LoC when Commons
    already has what we need."""
    for name, fn in IMAGE_SOURCES:
        if name not in AUTO_PHASE3_SOURCES:
            continue
        try:
            urls = fn(sess, query, n=n)
        except Exception as e:
            print(f"  ! source {name} raised: {type(e).__name__}: {e}")
            continue
        if urls:
            normalized = []
            for item in urls:
                if isinstance(item, dict):
                    normalized.append({"source_name": name, **item})
                else:
                    normalized.append({"source_name": name, "url": item, "title": ""})
            return normalized
    return []


# ── Keyword extraction for search queries ────────────────
def article_keywords(article_text: str, n: int = 5) -> list[str]:
    """Top distinctive words from the article's ## heading."""
    m = re.search(r"^##\s+(.+)$", article_text, re.MULTILINE)
    title = (m.group(1) if m else "").lower()
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    seen: set[str] = set()
    out: list[str] = []
    for w in title.split():
        if len(w) < 3 or w in STOP_WORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= n:
            break
    return out


# ── Fragment parsing ─────────────────────────────────────
def split_articles(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split into (preamble, [(heading_line, body), ...])."""
    parts = re.split(r"(?m)^##\s+", text)
    pre = parts[0]
    articles: list[tuple[str, str]] = []
    for a in parts[1:]:
        heading, _, body = a.partition("\n")
        articles.append((heading.rstrip(), body))
    return pre, articles


def reassemble(pre: str, articles: list[tuple[str, str]]) -> str:
    buf = [pre]
    for heading, body in articles:
        buf.append(f"## {heading}\n{body}")
    return "".join(buf)


def insert_image_after_subtitle(body: str, local_path: str, alt: str) -> str:
    """Insert image tag after the italicized subtitle (or at top) of a body."""
    lines = body.split("\n")
    insert_at = 0
    for i, line in enumerate(lines[:12]):
        stripped = line.strip()
        # An italic subtitle is a single line wrapped in * but not **
        if (
            stripped.startswith("*")
            and stripped.endswith("*")
            and not stripped.startswith("**")
            and len(stripped) > 4
        ):
            insert_at = i + 1
            # Skip the blank line after the subtitle if present
            if insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            break
    tag = f"![{alt}]({local_path})"
    lines.insert(insert_at, "")
    lines.insert(insert_at + 1, tag)
    lines.insert(insert_at + 2, "")
    return "\n".join(lines)


def detail_record(
    *,
    section: str,
    article: str,
    alt: str,
    original_url: str | None,
    phase: str,
    result: str,
    local_path: str | None,
    origin_type: str,
    confidence: float,
    relevance_reason: str,
    accepted_by_rule: str,
) -> dict:
    return {
        "section": section,
        "article": article[:80],
        "alt": alt[:100],
        "original_url": original_url,
        "phase": phase,
        "result": result,
        "local_path": local_path,
        "origin_type": origin_type,
        "confidence": round(confidence, 2),
        "relevance_reason": relevance_reason,
        "accepted_by_rule": accepted_by_rule,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        default=datetime.now(JST).strftime("%Y-%m-%d"),
        help="Briefing date YYYY-MM-DD (default: today JST)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not modify fragment files or write the report",
    )
    parser.add_argument(
        "--skip-empty-articles",
        action="store_true",
        help="Don't attempt to add an image to articles that have zero images",
    )
    parser.add_argument(
        "--add-empty-article-fallbacks",
        action="store_true",
        help="Opt in to fallback-image insertion for articles with zero images",
    )
    return parser


def empty_article_fallbacks_enabled(args) -> bool:
    return bool(getattr(args, "add_empty_article_fallbacks", False)) and not bool(
        getattr(args, "skip_empty_articles", False)
    )


PLACEHOLDER_IMAGE_TOKENS = (
    "briefing overview",
    "overview",
    "pressure map",
    "pressure points",
    "next-48h",
    "next 48h",
    "watch board",
)
PLACEHOLDER_IMAGE_STEM_SUFFIXES = ("-overview", "-pressure", "-watch")


def is_local_image_reference(url: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", url)) or url.startswith(("/", "\\"))


def is_placeholder_image_tag(alt: str, url: str) -> bool:
    clean_url = url.split("?", 1)[0].split("#", 1)[0]
    stem = Path(clean_url).stem.lower()
    if not is_local_image_reference(url) or not clean_url.lower().endswith(".svg"):
        return False
    haystack = f"{alt} {stem}".lower()
    if stem == "briefing-overview":
        return True
    if any(stem.endswith(suffix) for suffix in PLACEHOLDER_IMAGE_STEM_SUFFIXES):
        return True
    return any(token in haystack for token in PLACEHOLDER_IMAGE_TOKENS)


def count_non_placeholder_images(body: str) -> int:
    count = 0
    for alt, url in re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", body):
        if not is_placeholder_image_tag(alt, url.strip()):
            count += 1
    return count


def measure_article_image_coverage(date: str) -> dict:
    total_articles = 0
    zero_image_articles = 0
    placeholder_only_articles = 0
    for section in SECTIONS:
        frag_path = fragment_path(section, date)
        if not frag_path.exists():
            continue
        original = frag_path.read_text(encoding="utf-8")
        pre, articles = split_articles(original)
        for _heading, body in articles:
            total_articles += 1
            image_tags = re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", body)
            if not image_tags:
                zero_image_articles += 1
            elif all(is_placeholder_image_tag(alt, url.strip()) for alt, url in image_tags):
                placeholder_only_articles += 1
    zero_or_placeholder_articles = zero_image_articles + placeholder_only_articles
    zero_image_ratio = (zero_image_articles / total_articles) if total_articles else 0.0
    zero_or_placeholder_ratio = (zero_or_placeholder_articles / total_articles) if total_articles else 0.0
    return {
        "total_articles": total_articles,
        "zero_image_articles": zero_image_articles,
        "placeholder_only_articles": placeholder_only_articles,
        "zero_or_placeholder_articles": zero_or_placeholder_articles,
        "zero_image_ratio": round(zero_image_ratio, 3),
        "zero_or_placeholder_ratio": round(zero_or_placeholder_ratio, 3),
    }


def maybe_auto_hydrate(
    date: str,
    *,
    threshold_ratio: float = 0.5,
    min_zero_image_articles: int = 3,
) -> dict:
    coverage = measure_article_image_coverage(date)
    if coverage["total_articles"] == 0:
        return {"triggered": False, "coverage": coverage, "reason": "no_articles"}
    degraded_articles = coverage.get("zero_or_placeholder_articles", coverage["zero_image_articles"])
    degraded_ratio = coverage.get("zero_or_placeholder_ratio", coverage["zero_image_ratio"])
    if degraded_articles < min_zero_image_articles:
        return {"triggered": False, "coverage": coverage, "reason": "below_min_degraded_image_articles"}
    if degraded_ratio < threshold_ratio:
        return {"triggered": False, "coverage": coverage, "reason": "below_threshold_ratio"}

    hydration = load_hydration_module()

    started = datetime.now(JST)
    t_start = time.monotonic()
    hydration_report = hydration.hydrate_date(date)
    elapsed = time.monotonic() - t_start
    update_run_report(
        date,
        image_hydration={
            "triggered": True,
            "threshold_ratio": threshold_ratio,
            "coverage": coverage,
            "report": hydration_report,
        },
        remove_note_substrings=[
            "Auto-hydration triggered",
            "Auto-hydration could not recover",
        ],
        notes=[
            (
                "Auto-hydration triggered because zero-image or placeholder-only coverage collapsed "
                f"({degraded_articles}/{coverage['total_articles']})."
            )
        ],
    )
    recovery_count = int(hydration_report.get("total_recoveries", 0) or 0)
    placeholder_removal_count = int(hydration_report.get("total_placeholders_removed", 0) or 0)
    status = "passed" if recovery_count > 0 else "warning"
    if recovery_count == 0:
        notes = [
            (
                "Auto-hydration could not recover source-derived or exact-match replacement images"
                + (
                    " after removing placeholder image cards."
                    if placeholder_removal_count
                    else "."
                )
            )
        ]
        update_run_report(
            date,
            notes=notes,
            remove_note_substrings=["Auto-hydration could not recover"],
        )
    else:
        notes = []

    set_step_timing(
        date,
        "image_hydration",
        status=status,
        started_at=started.isoformat(),
        ended_at=datetime.now(JST).isoformat(),
        duration_seconds=round(elapsed, 3),
        details={
            "threshold_ratio": threshold_ratio,
            "zero_image_articles": coverage["zero_image_articles"],
            "placeholder_only_articles": coverage.get("placeholder_only_articles", 0),
            "zero_or_placeholder_articles": degraded_articles,
            "total_articles": coverage["total_articles"],
            "total_recoveries": recovery_count,
            "placeholders_removed": placeholder_removal_count,
            "warnings": notes,
        },
    )
    return {
        "triggered": True,
        "threshold_ratio": threshold_ratio,
        "coverage": coverage,
        "recovered_articles": sum(
            1
            for section in hydration_report.get("sections", {}).values()
            for article in section.get("hydrated", [])
            if any(item.get("local_path") for item in article.get("recoveries", []))
        ),
        "placeholder_cleanup_articles": sum(
            1
            for section in hydration_report.get("sections", {}).values()
            for article in section.get("hydrated", [])
            if any(item.get("source") == "placeholder-removal" for item in article.get("recoveries", []))
        ),
        "report": hydration_report,
    }


def load_hydration_module():
    module_name = "_daily_briefing_hydrate_images_core"
    if module_name in sys.modules:
        return sys.modules[module_name]

    module_path = Path(__file__).resolve().with_name("hydrate_images_core.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load hydration module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ── Main ─────────────────────────────────────────────────
def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    date = args.date
    img_cache = image_cache_dir(date)
    img_cache.mkdir(parents=True, exist_ok=True)

    hydration_trigger = {"triggered": False}
    if not args.dry_run:
        hydration_trigger = maybe_auto_hydrate(date, threshold_ratio=0.5)
        if not hydration_trigger.get("triggered"):
            update_run_report(
                date,
                remove_note_substrings=[
                    "Auto-hydration triggered",
                    "Auto-hydration could not recover",
                ],
            )

    sess = make_session()
    allow_empty_article_fallbacks = empty_article_fallbacks_enabled(args)

    report = {
        "date": date,
        "dry_run": args.dry_run,
        "empty_article_fallbacks_enabled": allow_empty_article_fallbacks,
        "total_images": 0,
        "phase1_ok": 0,
        "phase2_recovered": 0,
        "phase3_recovered": 0,
        "still_failed": 0,
        "articles_without_images": 0,
        "images_added": 0,
        "high_confidence_images": 0,
        "low_confidence_images": 0,
        "details": [],
    }
    if hydration_trigger.get("triggered"):
        report["hydration_trigger"] = hydration_trigger

    started_wall = datetime.now(JST)
    t_start = time.monotonic()

    for section in SECTIONS:
        frag_path = fragment_path(section, date)
        if not frag_path.exists():
            print(f"[{section}] fragment missing — skipping")
            continue

        print(f"\n[{section}] processing {frag_path.name}")
        original = frag_path.read_text(encoding="utf-8")
        pre, articles = split_articles(original)

        if not articles:
            # No ## headings (e.g. closing fragment may be a single block).
            # Treat the entire fragment as one pseudo-article for image processing.
            articles = [("", pre)]
            pre = ""

        changed = False

        for article_idx, (heading, body) in enumerate(articles):
            image_tags = list(re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", body))

            if not image_tags and heading and allow_empty_article_fallbacks:
                kws = article_keywords(f"## {heading}\n{body}")
                if kws:
                    query = " ".join(kws)
                    candidates = phase3_search(sess, query, n=3)
                    chosen = None
                    chosen_meta = None
                    for candidate in candidates:
                        decision = phase3_candidate_decision(
                            source_name=candidate["source_name"],
                            candidate_title=str(candidate.get("title") or ""),
                            query=query,
                            article_heading=heading,
                            article_body=body,
                        )
                        if not decision["accepted"]:
                            continue
                        p = download_image(sess, candidate["url"], img_cache)
                        if p:
                            chosen = p
                            chosen_meta = {
                                "source_name": candidate["source_name"],
                                **decision,
                            }
                            break
                    if chosen:
                        source_label = {
                            "commons": "Wikimedia Commons",
                            "loc": "Library of Congress",
                        }.get(chosen_meta["source_name"], chosen_meta["source_name"] or "external source")
                        alt = f"{' '.join(kws[:5]).title()} (Source: {source_label})"
                        new_body = insert_image_after_subtitle(body, str(chosen), alt)
                        articles[article_idx] = (heading, new_body)
                        changed = True
                        report["images_added"] += 1
                        if chosen_meta["confidence"] >= HIGH_CONFIDENCE_THRESHOLD:
                            report["high_confidence_images"] += 1
                        report["details"].append(
                            detail_record(
                                section=section,
                                article=heading,
                                alt=alt,
                                original_url=None,
                                phase=f"phase3_added_to_empty_{chosen_meta['source_name']}",
                                result="added",
                                local_path=str(chosen),
                                origin_type=chosen_meta["origin_type"],
                                confidence=chosen_meta["confidence"],
                                relevance_reason=chosen_meta["relevance_reason"],
                                accepted_by_rule=chosen_meta["accepted_by_rule"],
                            )
                        )
                        print(f"  + empty article: added {source_label} image {chosen.name}")
                        continue
                report["articles_without_images"] += 1
                continue

            if not image_tags and heading:
                report["articles_without_images"] += 1
                continue

            report["total_images"] += len(image_tags)
            new_body = body
            # Process tags in-place, reverse order so offsets stay valid
            for m in reversed(image_tags):
                alt = m.group(1)
                url = m.group(2).strip()
                original_url = url
                result_phase: str | None = None
                local_path: Path | None = None
                result_meta = {
                    "origin_type": "authored",
                    "confidence": 0.95,
                    "relevance_reason": "Inline image chosen during drafting",
                    "accepted_by_rule": "authored_inline",
                }

                # Idempotent path: if URL is already a local file that exists,
                # count it as phase1 and don't re-fetch.
                if not url.startswith(("http://", "https://")):
                    try:
                        candidate = Path(url)
                        if candidate.is_absolute() and candidate.exists() and (
                            candidate.suffix.lower() == ".svg" or candidate.stat().st_size >= MIN_IMAGE_BYTES
                        ):
                            placeholder = is_placeholder_image_tag(alt, url)
                            report["phase1_ok"] += 1
                            report["details"].append(
                                detail_record(
                                    section=section,
                                    article=heading,
                                    alt=alt,
                                    original_url=str(candidate),
                                    phase="phase1_already_local",
                                    result="ok",
                                    local_path=str(candidate),
                                    origin_type="authored",
                                    confidence=0.35 if placeholder else 0.9,
                                    relevance_reason=(
                                        "Local placeholder SVG was already cached; it does not satisfy image quality"
                                        if placeholder
                                        else "Image was already cached locally before the fetch step"
                                    ),
                                    accepted_by_rule="already_local_placeholder" if placeholder else "already_local",
                                )
                            )
                            if placeholder:
                                report["low_confidence_images"] += 1
                            continue
                    except (OSError, ValueError):
                        pass

                # Phase 1: direct download
                p = download_image(sess, url, img_cache)
                if p:
                    report["phase1_ok"] += 1
                    result_phase = "phase1_direct"
                    local_path = p
                    result_meta = {
                        "origin_type": "authored",
                        "confidence": 0.95,
                        "relevance_reason": "Inline image URL was reachable and preserved as authored content",
                        "accepted_by_rule": "phase1_direct",
                    }

                # Phase 2: og:image from body source URLs
                if not local_path:
                    source_urls = extract_body_urls(body)
                    for src in source_urls[:5]:
                        og = extract_og_image(sess, src)
                        if not og:
                            continue
                        p2 = download_image(sess, og, img_cache)
                        if p2:
                            report["phase2_recovered"] += 1
                            result_phase = "phase2_og_image"
                            local_path = p2
                            result_meta = {
                                "origin_type": "source_og",
                                "confidence": 0.92,
                                "relevance_reason": f"Recovered the article's Open Graph image from {urlparse(src).netloc}",
                                "accepted_by_rule": "source_og",
                            }
                            print(f"  ✓ phase2 recovered via {urlparse(src).netloc}")
                            break

                # Phase 3: Commons search with progressive-fallback queries.
                # Over-specific queries (e.g. "sam altman ceo openai spud") hit
                # zero results, so we try 4 query shapes from most to least
                # specific and stop at the first that lands a working image.
                if not local_path:
                    title_kws = article_keywords(f"## {heading}\n{body}") if heading else []
                    alt_words: list[str] = []
                    if alt:
                        clean_alt = re.sub(r"[^a-z0-9\s]", " ", alt.lower())
                        alt_words = [w for w in clean_alt.split() if len(w) > 2 and w not in STOP_WORDS][:5]

                    title_unique = [w for w in title_kws if w not in set(alt_words)]
                    queries: list[str] = []
                    if alt_words:
                        queries.append(" ".join(alt_words))                  # full alt
                        queries.append(" ".join(alt_words[:3]))              # first 3 alt words
                        if title_unique:
                            queries.append(" ".join(alt_words[:2] + title_unique[:1]))
                    if title_unique:
                        queries.append(" ".join(title_unique[:3]))           # pure title fallback

                    seen_queries: set[str] = set()
                    for query in queries:
                        query = query.strip()
                        if not query or query in seen_queries:
                            continue
                        seen_queries.add(query)
                        candidates = phase3_search(sess, query, n=3)
                        if not candidates:
                            continue
                        for candidate in candidates:
                            decision = phase3_candidate_decision(
                                source_name=candidate["source_name"],
                                candidate_title=str(candidate.get("title") or ""),
                                query=query,
                                article_heading=heading,
                                article_body=body,
                            )
                            if not decision["accepted"]:
                                continue
                            p3 = download_image(sess, candidate["url"], img_cache)
                            if p3:
                                report["phase3_recovered"] += 1
                                result_phase = f"phase3_{candidate['source_name']}:{query[:40]}"
                                local_path = p3
                                result_meta = {
                                    "origin_type": decision["origin_type"],
                                    "confidence": decision["confidence"],
                                    "relevance_reason": decision["relevance_reason"],
                                    "accepted_by_rule": decision["accepted_by_rule"],
                                }
                                print(f"  ✓ phase3 recovered via {candidate['source_name']} ({query[:40]!r}): {p3.name}")
                                break
                        if local_path:
                            break

                old_tag = m.group(0)
                start, end = m.start(), m.end()
                if local_path:
                    new_tag = f"![{alt}]({local_path})"
                    new_body = new_body[:start] + new_tag + new_body[end:]
                    changed = True
                    if result_meta["confidence"] >= HIGH_CONFIDENCE_THRESHOLD:
                        report["high_confidence_images"] += 1
                    if result_meta["confidence"] < LOW_CONFIDENCE_THRESHOLD:
                        report["low_confidence_images"] += 1
                    report["details"].append(
                        detail_record(
                            section=section,
                            article=heading,
                            alt=alt,
                            original_url=original_url,
                            phase=result_phase or "phase1_direct",
                            result="recovered" if result_phase != "phase1_direct" else "ok",
                            local_path=str(local_path),
                            origin_type=result_meta["origin_type"],
                            confidence=result_meta["confidence"],
                            relevance_reason=result_meta["relevance_reason"],
                            accepted_by_rule=result_meta["accepted_by_rule"],
                        )
                    )
                else:
                    # All phases failed — remove the tag plus one trailing newline.
                    rm_end = end
                    if rm_end < len(new_body) and new_body[rm_end] == "\n":
                        rm_end += 1
                    new_body = new_body[:start] + new_body[rm_end:]
                    changed = True
                    report["still_failed"] += 1
                    report["details"].append(
                        detail_record(
                            section=section,
                            article=heading,
                            alt=alt,
                            original_url=original_url,
                            phase="all_failed",
                            result="removed",
                            local_path=None,
                            origin_type="fallback",
                            confidence=0.0,
                            relevance_reason="No high-confidence image could be validated or recovered",
                            accepted_by_rule="removed_untrusted_or_unreachable",
                        )
                    )
                    print(f"  ✗ removed: {alt[:60]} ({urlparse(url).netloc})")

            articles[article_idx] = (heading, new_body)

        if changed and not args.dry_run:
            new_text = reassemble(pre, articles)
            if original.endswith("\n") and not new_text.endswith("\n"):
                new_text += "\n"
            frag_path.write_text(new_text, encoding="utf-8")
            print(f"  wrote {frag_path.name} ({len(new_text)} bytes)")
        elif changed:
            print(f"  would write {frag_path.name} (dry-run; no changes on disk)")
        else:
            print(f"  no changes")

    # Report
    report_path = img_cache / "image-report.json"
    if not args.dry_run:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    elapsed = time.monotonic() - t_start
    if not args.dry_run:
        update_run_report(
            date,
            image_report={
                "path": str(report_path),
                "high_confidence_images": report["high_confidence_images"],
                "low_confidence_images": report["low_confidence_images"],
                "articles_without_images": report["articles_without_images"],
            },
        )
        set_step_timing(
            date,
            "image_fetch",
            status="passed",
            started_at=started_wall.isoformat(),
            ended_at=datetime.now(JST).isoformat(),
            duration_seconds=round(elapsed, 3),
            details={
                "image_report_path": str(report_path),
                "high_confidence_images": report["high_confidence_images"],
                "articles_without_images": report["articles_without_images"],
            },
        )
    print("\n" + "=" * 62)
    print(f"  Image Report — {date}" + ("  (DRY RUN)" if args.dry_run else ""))
    print("=" * 62)
    print(f"  Total image tags processed:     {report['total_images']}")
    print(f"  Phase 1 (direct download):      {report['phase1_ok']}")
    print(f"  Phase 2 (og:image fallback):    {report['phase2_recovered']}")
    print(f"  Phase 3 (Commons search):       {report['phase3_recovered']}")
    print(f"  Still failed (tag removed):     {report['still_failed']}")
    print(f"  Articles with 0 images:         {report['articles_without_images']}")
    print(f"  Images added to empty articles: {report['images_added']}")
    print(f"  Elapsed:                        {elapsed:.1f}s")
    if not args.dry_run:
        print(f"  Report:                         {report_path}")
    print("=" * 62)

    return 0


if __name__ == "__main__":
    sys.exit(main())
