from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_SAMPLE_BYTES = 65536
USER_AGENT_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
USER_AGENT_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
USER_AGENT_GOOGLEBOT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1; +http://www.google.com/bot.html) Chrome/124.0 Safari/537.36"
BLOCKED_STATUSES = {401, 402, 403, 407, 429, 430, 451, 503}
AUTH_STATUSES = {401, 402, 407}
CHALLENGE_MARKERS = (
    "access denied",
    "akamai",
    "attention required",
    "bot challenge",
    "captcha",
    "cf-chl",
    "checking your browser",
    "cloudflare",
    "datadome",
    "enable javascript",
    "hcaptcha",
    "incapsula",
    "just a moment",
    "perimeterx",
    "please verify",
    "request unsuccessful",
    "sec-if-cpt",
    "turnstile",
    "verify you are human",
)
AUTH_MARKERS = (
    "log in",
    "login",
    "member-only",
    "paywall",
    "sign in",
    "subscribe to read",
    "subscription required",
    "로그인",
    "구독",
)
SPA_MARKERS = (
    '<div id="root"></div>',
    '<div id="app"></div>',
    "__next_data__",
)
METADATA_RE = re.compile(
    r"<meta\s+[^>]*(?:property|name)=[\"'](?P<key>[^\"']+)[\"'][^>]*content=[\"'](?P<value>[^\"']*)[\"'][^>]*>",
    re.IGNORECASE,
)
TITLE_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
JSONLD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
FEED_LINK_RE = re.compile(
    r"<link\s+[^>]*type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>",
    re.IGNORECASE,
)
HREF_RE = re.compile(r"href=[\"'](?P<href>[^\"']+)[\"']", re.IGNORECASE)


@dataclass
class FetchAttempt:
    phase: str
    method: str
    url: str
    status: int | None = None
    ok: bool = False
    verdict: str = "unknown"
    trust: str = "normal"
    final_url: str | None = None
    content_type: str | None = None
    bytes_sampled: int = 0
    blocking_signals: list[str] = field(default_factory=list)
    metadata_keys: list[str] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FetchResult:
    ok: bool
    verdict: str
    method: str | None
    final_url: str
    status: int | None
    trust: str
    content: str = ""
    content_type: str | None = None
    bytes_sampled: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    attempts: list[FetchAttempt] = field(default_factory=list)

    @property
    def auth_required(self) -> bool:
        return self.verdict == "auth_required"

    @property
    def sidecar_only(self) -> bool:
        return self.trust == "low_sidecar"

    def as_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "verdict": self.verdict,
            "method": self.method,
            "final_url": self.final_url,
            "status": self.status,
            "trust": self.trust,
            "content_type": self.content_type,
            "bytes_sampled": self.bytes_sampled,
            "metadata": self.metadata,
            "attempt_count": len(self.attempts),
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }
        if include_content:
            payload["content"] = self.content
        return payload

    def source_audit_summary(self) -> dict[str, Any]:
        return {
            "used": True,
            "ok": self.ok,
            "verdict": self.verdict,
            "method": self.method,
            "status": self.status,
            "final_url": self.final_url,
            "trust": self.trust,
            "content_type": self.content_type,
            "bytes_sampled": self.bytes_sampled,
            "metadata": self.metadata,
            "attempt_count": len(self.attempts),
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_headers(headers: Any) -> dict[str, str]:
    try:
        items = headers.items()
    except AttributeError:
        return {}
    return {str(key).lower(): str(value) for key, value in items}


def decode_body(raw: bytes, headers: dict[str, str]) -> str:
    content_type = headers.get("content-type", "")
    match = re.search(r"charset=([A-Za-z0-9_.-]+)", content_type)
    encoding = match.group(1) if match else "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def detect_blocking_signals(status: int | None, headers: Any, body: str) -> list[str]:
    signals: list[str] = []
    header_map = normalize_headers(headers)
    lower_body = (body or "").lower()
    if status in BLOCKED_STATUSES:
        signals.append(f"status:{status}")
    for key in ("cf-ray", "x-datadome", "x-akamai", "server", "x-cache"):
        value = header_map.get(key, "").lower()
        if key in {"cf-ray", "x-datadome", "x-akamai"} and value:
            signals.append(f"header:{key}")
        elif any(token in value for token in ("cloudflare", "akamai", "datadome", "incapsula", "waf")):
            signals.append(f"header:{key}")
    for marker in CHALLENGE_MARKERS:
        if marker in lower_body:
            signals.append(f"marker:{marker}")
            if len(signals) >= 8:
                break
    if status in AUTH_STATUSES or any(marker in lower_body for marker in AUTH_MARKERS):
        signals.append("auth_required")
    body_without_ws = re.sub(r"\s+", "", lower_body)
    if len(body_without_ws) < 300 and any(marker in lower_body for marker in SPA_MARKERS):
        signals.append("empty_spa")
    return list(dict.fromkeys(signals))


def extract_metadata(body: str, base_url: str = "") -> dict[str, Any]:
    metadata: dict[str, Any] = {"og": {}, "jsonld_types": [], "feed_links": []}
    for match in METADATA_RE.finditer(body or ""):
        key = html.unescape(match.group("key")).strip().lower()
        value = html.unescape(match.group("value")).strip()
        if not value:
            continue
        if key.startswith("og:") or key.startswith("twitter:") or key in {"description", "author"}:
            metadata["og"][key] = value[:1000]
    title_match = TITLE_RE.search(body or "")
    if title_match:
        metadata["title"] = re.sub(r"\s+", " ", html.unescape(title_match.group("title"))).strip()[:500]
    for match in JSONLD_RE.finditer(body or ""):
        raw = html.unescape(match.group("body")).strip()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            typ = item.get("@type")
            if isinstance(typ, list):
                metadata["jsonld_types"].extend(str(value) for value in typ)
            elif typ:
                metadata["jsonld_types"].append(str(typ))
            for key in ("headline", "name", "description", "datePublished", "articleBody"):
                if key in item and key not in metadata:
                    value = str(item.get(key) or "")
                    metadata[key] = value[:2000]
    for match in FEED_LINK_RE.finditer(body or ""):
        href_match = HREF_RE.search(match.group(0))
        href = href_match.group("href") if href_match else None
        if href:
            metadata["feed_links"].append(urllib.parse.urljoin(base_url, html.unescape(href)))
    metadata["jsonld_types"] = list(dict.fromkeys(metadata["jsonld_types"]))[:12]
    metadata["feed_links"] = list(dict.fromkeys(metadata["feed_links"]))[:12]
    return {key: value for key, value in metadata.items() if value}


def metadata_keys(metadata: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for key, value in metadata.items():
        if isinstance(value, dict):
            keys.extend(f"{key}.{inner}" for inner in value)
        elif value:
            keys.append(key)
    return keys[:20]


def validate_fetch_response(status: int | None, headers: Any, body: str, *, base_url: str = "", trust: str = "normal") -> tuple[bool, str, list[str], dict[str, Any]]:
    signals = detect_blocking_signals(status, headers, body)
    metadata = extract_metadata(body, base_url)
    content_type = normalize_headers(headers).get("content-type", "").lower()
    if "auth_required" in signals:
        return False, "auth_required", signals, metadata
    if signals:
        return False, "challenge", signals, metadata
    if status is None:
        return False, "unknown", signals, metadata
    if not (200 <= int(status) < 400):
        return False, "blocked", signals or [f"status:{status}"], metadata
    body_len = len((body or "").strip())
    structured = any(token in content_type for token in ("json", "rss", "atom", "xml"))
    if body_len >= 300 or structured or metadata:
        return True, "weak_ok" if trust == "normal" else "sidecar_ok", signals, metadata
    return False, "empty_or_tiny", signals or [f"tiny_body:{body_len}"], metadata


def request_headers(user_agent: str, *, referer: str | None = None, accept: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": accept or "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ja;q=0.8,ko;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def http_get_text(url: str, *, headers: dict[str, str], timeout: float, max_bytes: int) -> tuple[int | None, dict[str, str], str, str, int, str | None]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            final_url = response.geturl()
            header_map = normalize_headers(response.headers)
            raw = response.read(max_bytes)
            return status, header_map, decode_body(raw, header_map), final_url, len(raw), None
    except urllib.error.HTTPError as exc:
        header_map = normalize_headers(exc.headers)
        raw = exc.read(max_bytes)
        body = decode_body(raw, header_map)
        return int(exc.code), header_map, body, exc.geturl() or url, len(raw), f"HTTPError {exc.code}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, {}, "", url, 0, f"{type(exc).__name__}: {exc}"


def origin_for(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


def mobile_variant(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if host.startswith("www."):
        return urllib.parse.urlunparse(parsed._replace(netloc="m." + host[4:]))
    if host and host.count(".") <= 1 and not host.startswith("m."):
        return urllib.parse.urlunparse(parsed._replace(netloc="m." + host))
    return None


def feed_variants(url: str) -> list[str]:
    origin = origin_for(url)
    return [f"{origin}{suffix}" for suffix in ("/rss", "/feed", "/atom.xml", "/rss.xml", "/index.xml")]


def jina_reader_url(url: str) -> str:
    return "https://r.jina.ai/" + url


def public_endpoint_candidates(url: str) -> list[tuple[str, str]]:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.strip("/")
    candidates: list[tuple[str, str]] = []
    if host == "reddit.com" and path.startswith("r/"):
        subreddit = path.split("/")[1] if len(path.split("/")) > 1 else ""
        if subreddit:
            candidates.append(("reddit_json", f"https://www.reddit.com/r/{subreddit}/hot.json?limit=10"))
    if host == "news.ycombinator.com":
        candidates.append(("hn_firebase_topstories", "https://hacker-news.firebaseio.com/v0/topstories.json"))
        candidates.append(("hn_rss_frontpage", "https://hnrss.org/frontpage"))
    if host == "github.com" and len(path.split("/")) >= 2:
        owner, repo = path.split("/")[:2]
        candidates.append(("github_releases_atom", f"https://github.com/{owner}/{repo}/releases.atom"))
    if host.endswith("wikipedia.org") and path.startswith("wiki/"):
        title = path.split("/", 1)[1]
        lang = host.split(".")[0] or "en"
        candidates.append(("wikipedia_summary", f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"))
    return candidates


def _attempt_http(
    attempts: list[FetchAttempt],
    *,
    phase: str,
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
    trust: str = "normal",
) -> tuple[FetchAttempt, str]:
    started = time.time()
    status, response_headers, body, final_url, sampled, error = http_get_text(
        url,
        headers=headers,
        timeout=timeout,
        max_bytes=max_bytes,
    )
    ok, verdict, signals, metadata = validate_fetch_response(status, response_headers, body, base_url=final_url, trust=trust)
    attempt = FetchAttempt(
        phase=phase,
        method=method,
        url=url,
        status=status,
        ok=ok,
        verdict=verdict,
        trust=trust,
        final_url=final_url,
        content_type=response_headers.get("content-type"),
        bytes_sampled=sampled,
        blocking_signals=signals,
        metadata_keys=metadata_keys(metadata),
        error=error if not ok else None,
        duration_seconds=round(time.time() - started, 3),
    )
    attempts.append(attempt)
    return attempt, body


def _result_from_attempt(attempt: FetchAttempt, body: str) -> FetchResult:
    return FetchResult(
        ok=attempt.ok,
        verdict=attempt.verdict,
        method=attempt.method,
        final_url=attempt.final_url or attempt.url,
        status=attempt.status,
        trust=attempt.trust,
        content=body,
        content_type=attempt.content_type,
        bytes_sampled=attempt.bytes_sampled,
        metadata=extract_metadata(body, attempt.final_url or attempt.url),
        attempts=[],
    )


def _curl_cffi_available(allow_install: bool) -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        if not allow_install:
            return False
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "curl_cffi", "-q"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
        )
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


def _attempt_curl_cffi(
    attempts: list[FetchAttempt],
    *,
    url: str,
    impersonate: str,
    timeout: float,
    max_bytes: int,
) -> tuple[FetchAttempt, str]:
    started = time.time()
    body = ""
    status: int | None = None
    headers: dict[str, str] = {}
    final_url = url
    error = None
    try:
        from curl_cffi import requests as cffi_requests

        response = cffi_requests.get(
            url,
            impersonate=impersonate,
            headers=request_headers(USER_AGENT_DESKTOP, referer=origin_for(url)),
            timeout=timeout,
            allow_redirects=True,
        )
        status = int(response.status_code)
        headers = normalize_headers(response.headers)
        final_url = str(response.url)
        body = (response.text or "")[:max_bytes]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    ok, verdict, signals, metadata = validate_fetch_response(status, headers, body, base_url=final_url)
    attempt = FetchAttempt(
        phase="phase2_tls",
        method=f"curl_cffi:{impersonate}",
        url=url,
        status=status,
        ok=ok,
        verdict=verdict,
        final_url=final_url,
        content_type=headers.get("content-type"),
        bytes_sampled=len(body.encode("utf-8", errors="ignore")),
        blocking_signals=signals,
        metadata_keys=metadata_keys(metadata),
        error=error if not ok else None,
        duration_seconds=round(time.time() - started, 3),
    )
    attempts.append(attempt)
    return attempt, body


def fetch_adaptive_url(
    url: str,
    *,
    timeout: float = 8.0,
    max_bytes: int = MAX_SAMPLE_BYTES,
    enable_jina: bool = True,
    enable_tls: bool = True,
    allow_install: bool | None = None,
) -> FetchResult:
    attempts: list[FetchAttempt] = []
    allow_install = bool(
        os.environ.get("DAILY_BRIEFING_ADAPTIVE_FETCH_INSTALL") == "1"
        if allow_install is None
        else allow_install
    )
    candidates: list[tuple[str, str, dict[str, str], str, str]] = []
    candidates.extend(
        (
            ("phase0_public", method, endpoint, request_headers(USER_AGENT_MOBILE), "normal")
            for method, endpoint in public_endpoint_candidates(url)
        )
    )
    candidates.extend(
        [
            ("phase1_light", "direct_desktop", url, request_headers(USER_AGENT_DESKTOP, referer=origin_for(url)), "normal"),
            ("phase1_light", "direct_mobile", url, request_headers(USER_AGENT_MOBILE, referer=origin_for(url)), "normal"),
            ("phase1_light", "direct_googlebot", url, request_headers(USER_AGENT_GOOGLEBOT), "normal"),
        ]
    )
    variant = mobile_variant(url)
    if variant:
        candidates.append(("phase1_light", "mobile_url_variant", variant, request_headers(USER_AGENT_MOBILE, referer=origin_for(url)), "normal"))
    for feed_url in feed_variants(url):
        candidates.append(("phase1_light", "feed_variant", feed_url, request_headers(USER_AGENT_DESKTOP, accept="application/rss+xml,application/atom+xml,application/xml,text/xml,*/*"), "normal"))
    if enable_jina:
        candidates.append(("phase1_jina", "jina_json", jina_reader_url(url), request_headers(USER_AGENT_DESKTOP, accept="application/json"), "normal"))
        candidates.append(("phase1_jina", "jina_markdown", jina_reader_url(url), request_headers(USER_AGENT_DESKTOP, accept="text/plain,*/*"), "normal"))

    best_body = ""
    best_attempt: FetchAttempt | None = None
    seen_urls: set[tuple[str, str]] = set()
    for phase, method, candidate_url, headers, trust in candidates:
        key = (method, candidate_url)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        attempt, body = _attempt_http(
            attempts,
            phase=phase,
            method=method,
            url=candidate_url,
            headers=headers,
            timeout=timeout,
            max_bytes=max_bytes,
            trust=trust,
        )
        if best_attempt is None or (attempt.bytes_sampled > best_attempt.bytes_sampled and attempt.metadata_keys):
            best_attempt, best_body = attempt, body
        if attempt.ok:
            result = _result_from_attempt(attempt, body)
            result.attempts = attempts
            return result

    if enable_tls and _curl_cffi_available(allow_install):
        for target in ("safari", "chrome", "firefox"):
            attempt, body = _attempt_curl_cffi(
                attempts,
                url=url,
                impersonate=target,
                timeout=timeout,
                max_bytes=max_bytes,
            )
            if best_attempt is None or (attempt.bytes_sampled > best_attempt.bytes_sampled and attempt.metadata_keys):
                best_attempt, best_body = attempt, body
            if attempt.ok:
                result = _result_from_attempt(attempt, body)
                result.attempts = attempts
                return result

    sidecar_url = "https://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe="")
    attempt, body = _attempt_http(
        attempts,
        phase="sidecar_low_trust",
        method="wayback_available",
        url=sidecar_url,
        headers=request_headers(USER_AGENT_DESKTOP, accept="application/json,*/*"),
        timeout=timeout,
        max_bytes=max_bytes,
        trust="low_sidecar",
    )
    if attempt.ok:
        result = _result_from_attempt(attempt, body)
        result.attempts = attempts
        result.verdict = "sidecar_ok"
        result.trust = "low_sidecar"
        return result

    auth_seen = any("auth_required" in attempt.blocking_signals or attempt.verdict == "auth_required" for attempt in attempts)
    browser_seen = any("empty_spa" in attempt.blocking_signals or attempt.verdict == "challenge" for attempt in attempts)
    verdict = "auth_required" if auth_seen else ("browser_required" if browser_seen else "failed")
    final_attempt = best_attempt or (attempts[-1] if attempts else None)
    return FetchResult(
        ok=False,
        verdict=verdict,
        method=final_attempt.method if final_attempt else None,
        final_url=final_attempt.final_url or url if final_attempt else url,
        status=final_attempt.status if final_attempt else None,
        trust=final_attempt.trust if final_attempt else "normal",
        content=best_body,
        content_type=final_attempt.content_type if final_attempt else None,
        bytes_sampled=final_attempt.bytes_sampled if final_attempt else 0,
        metadata=extract_metadata(best_body, final_attempt.final_url if final_attempt else url),
        attempts=attempts,
    )


def _optional_path(name: str, *args) -> Path | None:
    try:
        import briefing_paths as briefing_paths_module
    except Exception:
        return None
    helper = getattr(briefing_paths_module, name, None)
    if not callable(helper):
        return None
    path = Path(helper(*args))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def adaptive_fetch_report_path(date: str) -> Path:
    path = _optional_path("adaptive_fetch_report_path", date)
    if path is not None:
        return path
    try:
        import briefing_paths as briefing_paths_module

        generated = Path(getattr(briefing_paths_module, "generated_dir")())
        stem = getattr(briefing_paths_module, "artifact_stem", lambda: "briefing")()
    except Exception:
        generated = Path.cwd()
        stem = "briefing"
    generated.mkdir(parents=True, exist_ok=True)
    return generated / f"{stem}-{date}-adaptive-fetch.json"


def build_adaptive_fetch_report(date: str, source_audit: dict[str, Any]) -> dict[str, Any]:
    sources = [source for source in source_audit.get("sources") or [] if isinstance(source, dict)]
    method_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    recovered: list[dict[str, Any]] = []
    sidecar: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    attempted_count = 0
    for source in sources:
        adaptive = source.get("adaptive_fetch") or {}
        if not adaptive.get("used"):
            continue
        attempted_count += 1
        method = str(adaptive.get("method") or "unknown")
        verdict = str(adaptive.get("verdict") or "unknown")
        method_counts[method] = method_counts.get(method, 0) + 1
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        row = {
            "source_id": source.get("source_id"),
            "name": source.get("name"),
            "url": source.get("url"),
            "method": method,
            "verdict": verdict,
            "trust": adaptive.get("trust"),
            "attempt_count": adaptive.get("attempt_count"),
        }
        if adaptive.get("ok") and adaptive.get("trust") != "low_sidecar":
            recovered.append(row)
        elif adaptive.get("trust") == "low_sidecar":
            sidecar.append(row)
        else:
            failed.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "generated_at": iso_now(),
        "inspired_by": {
            "project": "fivetaku/insane-search",
            "pattern": "Phase 0 public endpoints, Phase 1 lightweight probes, Jina/RSS variants, metadata extraction, optional TLS impersonation, low-trust archive sidecar",
        },
        "policy": {
            "no_api_keys": True,
            "sidecar_low_trust": "Archive/Wayback availability is provenance only; it does not prove a live source is reachable.",
            "no_browser_in_scheduler": "Full browser/hidden API discovery is reported as browser_required for manual follow-up rather than launched inside unattended source audit.",
        },
        "summary": {
            "source_count": len(sources),
            "adaptive_attempted_count": attempted_count,
            "recovered_count": len(recovered),
            "sidecar_only_count": len(sidecar),
            "failed_count": len(failed),
            "method_counts": method_counts,
            "verdict_counts": verdict_counts,
        },
        "recovered_sources": recovered[:100],
        "sidecar_sources": sidecar[:100],
        "failed_sources": failed[:100],
    }


def write_adaptive_fetch_report(date: str, source_audit: dict[str, Any]) -> dict[str, Any]:
    payload = build_adaptive_fetch_report(date, source_audit)
    path = adaptive_fetch_report_path(date)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {
        "status": "ok",
        "artifact_path": str(path),
        **payload["summary"],
    }
    try:
        from run_reporting import update_run_report

        update_run_report(date, adaptive_fetch=summary)
    except Exception:
        pass
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the shared adaptive fetch chain for one URL.")
    parser.add_argument("url", nargs="?", help="URL to fetch.")
    parser.add_argument("--date", help="Write adaptive-fetch report from an existing source audit for this date.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    parser.add_argument("--allow-install", action="store_true", help="Allow installing curl_cffi if TLS impersonation is needed.")
    args = parser.parse_args()

    if args.date:
        path = _optional_path("source_audit_path", args.date)
        if path is None or not path.exists():
            raise FileNotFoundError(f"Source audit not found for {args.date}")
        audit = json.loads(path.read_text(encoding="utf-8"))
        summary = write_adaptive_fetch_report(args.date, audit)
        print(json.dumps(summary, indent=2, ensure_ascii=False) if args.json else summary["artifact_path"])
        return 0

    if not args.url:
        parser.error("url is required unless --date is used")
    result = fetch_adaptive_url(args.url, allow_install=args.allow_install)
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False) if args.json else f"{result.verdict} {result.method} {result.final_url}")
    return 0 if result.ok else 1


__all__ = [
    "FetchAttempt",
    "FetchResult",
    "adaptive_fetch_report_path",
    "build_adaptive_fetch_report",
    "detect_blocking_signals",
    "extract_metadata",
    "fetch_adaptive_url",
    "jina_reader_url",
    "validate_fetch_response",
    "write_adaptive_fetch_report",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
