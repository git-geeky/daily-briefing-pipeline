from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import briefing_paths as briefing_paths_module
import fetch_briefing_images_core as fi
from run_reporting import ensure_run_attempt, set_step_timing, update_run_report


META_IMAGE_KEYS = (
    "og:image:secure_url",
    "og:image:url",
    "og:image",
    "twitter:image",
    "twitter:image:src",
    "thumbnail",
    "thumbnailurl",
)
BAD_IMAGE_TOKENS = (
    "app-store",
    "avatar",
    "badge",
    "blank",
    "default.jpg",
    "default.png",
    "getitongoogleplay",
    "favicon",
    "icon",
    "logo",
    "og_img",
    "open-graph/og-image",
    "place holder",
    "place-holder",
    "pixel",
    "placeholder",
    "social-default",
    "scorecardresearch",
    "sprite",
    "tracking",
    "transparent",
    "y18.svg",
)
CHART_IMAGE_TOKENS = (
    "benchmark",
    "chart",
    "figure",
    "figures",
    "graph",
    "result",
    "results",
    "score",
)
PROMPT_INJECTION_TOKENS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "you are now",
    "system prompt",
    "developer message",
    "assistant instructions",
)
SOURCE_LABELS = {
    "about.fb.com": "Meta",
    "aljazeera.com": "Al Jazeera",
    "al-monitor.com": "Al-Monitor",
    "anthropic.com": "Anthropic",
    "apnews.com": "AP",
    "artificialanalysis.ai": "Artificial Analysis",
    "blog.google": "Google",
    "boj.or.jp": "Bank of Japan",
    "cli.github.com": "GitHub",
    "deploymentsafety.openai.com": "OpenAI",
    "ecb.europa.eu": "ECB",
    "github.blog": "GitHub",
    "github.com": "GitHub",
    "huggingface.co": "Hugging Face",
    "ifo.de": "ifo Institute",
    "microsoft.com": "Microsoft",
    "news.microsoft.com": "Microsoft",
    "news.skhynix.com": "SK hynix",
    "news.ycombinator.com": "Hacker News",
    "oecd.org": "OECD",
    "openai.com": "OpenAI",
    "profit.pakistantoday.com.pk": "Profit by Pakistan Today",
    "qwen.ai": "Qwen",
    "reliefweb.int": "ReliefWeb",
    "simonwillison.net": "Simon Willison",
    "stats.gov.cn": "National Bureau of Statistics of China",
    "tomshardware.com": "Tom's Hardware",
    "trendmicro.com": "Trend Micro",
    "un.org": "United Nations",
    "vercel.com": "Vercel",
    "who.int": "WHO",
    "wsau.com": "Reuters syndication",
    "wtaq.com": "Reuters syndication",
}


@dataclass
class Candidate:
    image_url: str
    source_url: str
    source_label: str
    origin: str
    score: float
    reason: str


def source_label(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for token, label in SOURCE_LABELS.items():
        if host == token or host.endswith("." + token):
            return label
    stem = host.split(":")[0]
    return stem or "source"


def clean_image_url(url: str, base_url: str) -> str | None:
    value = html.unescape((url or "").strip().strip("'\""))
    if not value or value.startswith(("data:", "blob:", "javascript:")):
        return None
    resolved = urljoin(base_url, value)
    if not resolved.startswith(("http://", "https://")):
        return None
    lower = resolved.lower()
    if any(token in lower for token in BAD_IMAGE_TOKENS):
        return None
    if lower.split("?", 1)[0].endswith(".svg"):
        return None
    return resolved


def choose_srcset_url(srcset: str | None) -> str | None:
    if not srcset:
        return None
    choices = []
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                width = int(bits[1][:-1])
            except ValueError:
                width = 0
        choices.append((width, url))
    if not choices:
        return None
    choices.sort(key=lambda item: item[0])
    return choices[-1][1]


def image_url_from_jsonld(value) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key in ("url", "contentUrl"):
            if isinstance(value.get(key), str):
                out.append(value[key])
    elif isinstance(value, list):
        for item in value:
            out.extend(image_url_from_jsonld(item))
    return out


def jsonld_candidates(soup: BeautifulSoup, page_url: str, label: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        text = script.string or script.get_text() or ""
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        for item in stack:
            if not isinstance(item, dict):
                continue
            images = image_url_from_jsonld(item.get("image"))
            for image in images:
                clean = clean_image_url(image, page_url)
                if clean:
                    candidates.append(
                        Candidate(
                            image_url=clean,
                            source_url=page_url,
                            source_label=label,
                            origin="jsonld_image",
                            score=0.88,
                            reason="JSON-LD image from the cited source page",
                        )
                    )
    return candidates


def relevance_boost(text: str) -> float:
    lower = text.lower()
    boost = 0.0
    if any(token in lower for token in CHART_IMAGE_TOKENS):
        boost += 0.24
    if "hero" in lower:
        boost += 0.04
    if "social-thumbnails" in lower or "social-thumbnail" in lower:
        boost -= 0.04
    return boost


def page_image_candidates(sess: requests.Session, source_url: str) -> list[Candidate]:
    fi.throttle(source_url)
    try:
        response = sess.get(
            source_url,
            timeout=20,
            allow_redirects=True,
            headers={"Accept": "text/html,*/*;q=0.5"},
        )
    except requests.RequestException:
        return []
    if response.status_code != 200 or "text/html" not in response.headers.get("Content-Type", ""):
        return []
    if any(token in response.text[:200_000].lower() for token in PROMPT_INJECTION_TOKENS):
        # The image pass only needs source-page metadata. If a fetched page contains
        # obvious agent-directed text, discard it rather than preserving hostile
        # instructions in downstream artifacts or prompts.
        return []

    label = source_label(response.url)
    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[Candidate] = []
    meta_alt_text = " ".join(
        str(meta.get("content") or "")
        for meta in soup.find_all("meta")
        if (meta.get("property") or meta.get("name") or "").strip().lower()
        in {"og:image:alt", "twitter:image:alt"}
    ).lower()

    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").strip().lower()
        if key not in META_IMAGE_KEYS:
            continue
        if any(token in meta_alt_text for token in BAD_IMAGE_TOKENS):
            continue
        clean = clean_image_url(meta.get("content") or "", response.url)
        if clean:
            score = 0.95 if key.startswith("og:image") else 0.9
            score += relevance_boost(clean)
            candidates.append(
                Candidate(
                    image_url=clean,
                    source_url=response.url,
                    source_label=label,
                    origin=key,
                    score=round(min(score, 0.99), 3),
                    reason=f"{key} from the cited source page",
                )
            )

    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        if "image_src" not in rel:
            continue
        clean = clean_image_url(link.get("href") or "", response.url)
        if clean:
            score = 0.86 + relevance_boost(clean)
            candidates.append(
                Candidate(
                    image_url=clean,
                    source_url=response.url,
                    source_label=label,
                    origin="link_image_src",
                    score=round(min(score, 0.99), 3),
                    reason="image_src link from the cited source page",
                )
            )

    candidates.extend(jsonld_candidates(soup, response.url, label))

    for img in soup.find_all("img")[:200]:
        src = (
            choose_srcset_url(img.get("srcset") or img.get("data-srcset"))
            or img.get("src")
            or img.get("data-src")
            or img.get("data-original")
        )
        clean = clean_image_url(src or "", response.url)
        if not clean:
            continue
        haystack = " ".join(
            [
                clean,
                str(img.get("alt") or ""),
                " ".join(img.get("class") or []),
                str(img.get("role") or ""),
            ]
        ).lower()
        if any(token in haystack for token in BAD_IMAGE_TOKENS):
            continue
        width = int(str(img.get("width") or "0").split(".")[0] or 0) if str(img.get("width") or "").split(".")[0].isdigit() else 0
        height = int(str(img.get("height") or "0").split(".")[0] or 0) if str(img.get("height") or "").split(".")[0].isdigit() else 0
        size_boost = 0.05 if width >= 500 or height >= 300 else 0.0
        boost = relevance_boost(haystack)
        candidates.append(
            Candidate(
                image_url=clean,
                source_url=response.url,
                source_label=label,
                origin="inline_img",
                score=round(min(0.76 + size_boost + boost, 0.98), 3),
                reason="inline image from the cited source page",
            )
        )

    return dedupe_candidates(candidates)


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        prior = best.get(candidate.image_url)
        if prior is None or candidate.score > prior.score:
            best[candidate.image_url] = candidate
    return sorted(best.values(), key=lambda item: item.score, reverse=True)


def commons_candidates(sess: requests.Session, heading: str, body: str) -> list[Candidate]:
    keywords = fi.article_keywords(f"## {heading}\n{body}", n=5)
    queries = []
    if len(keywords) >= 3:
        queries.append(" ".join(keywords[:3]))
    if len(keywords) >= 2:
        queries.append(" ".join(keywords[:2]))
    out: list[Candidate] = []
    seen_queries: set[str] = set()
    for query in queries:
        if not query or query in seen_queries:
            continue
        seen_queries.add(query)
        for candidate in fi.phase3_search(sess, query, n=4):
            decision = fi.phase3_candidate_decision(
                source_name=candidate["source_name"],
                candidate_title=str(candidate.get("title") or ""),
                query=query,
                article_heading=heading,
                article_body=body,
            )
            if not decision["accepted"]:
                continue
            url = candidate.get("url")
            if not url:
                continue
            out.append(
                Candidate(
                    image_url=url,
                    source_url="https://commons.wikimedia.org/",
                    source_label="Wikimedia Commons",
                    origin="commons_exact_entity_match",
                    score=float(decision["confidence"]),
                    reason=decision["relevance_reason"],
                )
            )
    return dedupe_candidates(out)


def image_block(candidates: list[Candidate], heading: str) -> str:
    lines = []
    safe_heading = re.sub(r"\s+", " ", heading).strip()
    for candidate in candidates:
        descriptor = "chart" if relevance_boost(candidate.image_url) > 0 else "image"
        alt = f"Source {descriptor} for {safe_heading} (Source: {candidate.source_label})"
        lines.append(f"![{alt}]({candidate.image_url})")
    return "\n\n".join(lines) + ("\n\n" if lines else "")


def insert_image_block(body: str, block: str) -> str:
    if not block:
        return body
    lines = body.split("\n")
    insert_at = 0
    for index, line in enumerate(lines[:12]):
        stripped = line.strip()
        if stripped.startswith("*") and stripped.endswith("*") and not stripped.startswith("**") and len(stripped) > 4:
            insert_at = index + 1
            if insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            break
    return "\n".join(lines[:insert_at] + [block.rstrip(), ""] + lines[insert_at:])


def existing_non_placeholder_image_count(body: str) -> int:
    return fi.count_non_placeholder_images(body)


def strip_image_tags(body: str) -> tuple[str, int]:
    removed = 0

    def replace(match) -> str:
        nonlocal removed
        removed += 1
        return ""

    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)\n?", replace, body)
    return cleaned, removed


def research_article_images(
    sess: requests.Session,
    *,
    heading: str,
    body: str,
    img_cache: Path,
    needed: int,
) -> list[Candidate]:
    chosen: list[Candidate] = []
    article_used_urls: set[str] = set()
    source_urls = fi.extract_body_urls(body)
    candidates: list[Candidate] = []
    for source_url in source_urls:
        candidates.extend(page_image_candidates(sess, source_url))
    candidates = dedupe_candidates(candidates)

    for candidate in candidates:
        if len(chosen) >= needed:
            return chosen
        if candidate.image_url in article_used_urls:
            continue
        local_path = fi.download_image(sess, candidate.image_url, img_cache)
        if not local_path:
            continue
        article_used_urls.add(candidate.image_url)
        chosen.append(candidate)

    if len(chosen) >= needed:
        return chosen

    for candidate in commons_candidates(sess, heading, body):
        if len(chosen) >= needed:
            break
        if candidate.image_url in article_used_urls:
            continue
        local_path = fi.download_image(sess, candidate.image_url, img_cache)
        if not local_path:
            continue
        article_used_urls.add(candidate.image_url)
        chosen.append(candidate)
    return chosen


def source_article_images(
    date: str,
    *,
    max_images_per_article: int = 1,
    force: bool = False,
    replace_existing: bool = False,
    dry_run: bool = False,
) -> dict:
    started = datetime.now(fi.JST)
    t_start = time.monotonic()
    ensure_run_attempt(date)
    fi.ensure_layout()
    sess = fi.make_session()
    img_cache = fi.image_cache_dir(date)
    img_cache.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "date": date,
        "runner": getattr(briefing_paths_module, "runner_name", lambda: "unknown")(),
        "dry_run": dry_run,
        "max_images_per_article": max_images_per_article,
        "replace_existing": replace_existing,
        "articles_scanned": 0,
        "articles_changed": 0,
        "images_inserted": 0,
        "articles_without_recovery": [],
        "details": [],
        "sections": {},
    }

    for section in fi.SECTIONS:
        frag_path = fi.fragment_path(section, date)
        if not frag_path.exists():
            continue
        original = frag_path.read_text(encoding="utf-8")
        pre, articles = fi.split_articles(original)
        section_report = {"articles_scanned": len(articles), "articles_changed": 0, "images_inserted": 0}
        new_articles: list[tuple[str, str]] = []
        changed = False

        for heading, body in articles:
            report["articles_scanned"] += 1
            if replace_existing:
                body, removed_count = strip_image_tags(body)
            else:
                removed_count = 0
            current_count = existing_non_placeholder_image_count(body)
            if not force and current_count >= max_images_per_article:
                new_articles.append((heading, body))
                continue
            needed = max_images_per_article if force else max(0, max_images_per_article - current_count)
            candidates = research_article_images(
                sess,
                heading=heading,
                body=body,
                img_cache=img_cache,
                needed=needed,
            )
            if candidates:
                block = image_block(candidates, heading)
                new_body = insert_image_block(body, block)
                new_articles.append((heading, new_body))
                changed = True
                report["articles_changed"] += 1
                report["images_inserted"] += len(candidates)
                section_report["articles_changed"] += 1
                section_report["images_inserted"] += len(candidates)
                for candidate in candidates:
                    report["details"].append(
                        {
                            "section": section,
                            "article": heading,
                            "image_url": candidate.image_url,
                            "source_url": candidate.source_url,
                            "source_label": candidate.source_label,
                            "origin": candidate.origin,
                            "score": candidate.score,
                            "reason": candidate.reason,
                            "replaced_images": removed_count,
                        }
                    )
            else:
                new_articles.append((heading, body))
                report["articles_without_recovery"].append({"section": section, "title": heading})

        if changed and not dry_run:
            frag_path.write_text(fi.reassemble(pre, new_articles), encoding="utf-8")
        report["sections"][section] = section_report

    elapsed = time.monotonic() - t_start
    status = "passed" if not report["articles_without_recovery"] else ("warning" if report["images_inserted"] else "failed")
    report["duration_seconds"] = round(elapsed, 3)
    update_run_report(
        date,
        source_image_research=report,
        notes=[
            (
                "Source-image research inserted "
                f"{report['images_inserted']} image(s) across {report['articles_changed']} article(s); "
                f"{len(report['articles_without_recovery'])} article(s) still need manual image sourcing."
            )
        ],
        remove_note_substrings=["Source-image research inserted"],
    )
    set_step_timing(
        date,
        "source_image_research",
        status=status,
        started_at=started.isoformat(),
        ended_at=datetime.now(fi.JST).isoformat(),
        duration_seconds=round(elapsed, 3),
        details={
            "images_inserted": report["images_inserted"],
            "articles_changed": report["articles_changed"],
            "articles_without_recovery": len(report["articles_without_recovery"]),
        },
    )
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research and insert source-derived images for each briefing article.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    parser.add_argument("--max-images-per-article", type=int, default=1)
    parser.add_argument("--force", action="store_true", help="Insert up to the max even if the article already has images.")
    parser.add_argument("--replace-existing", action="store_true", help="Remove existing inline images before inserting researched source images.")
    parser.add_argument("--dry-run", action="store_true", help="Research images and write the report without editing fragments.")
    parser.add_argument("--report-path", type=Path)
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    report = source_article_images(
        args.date,
        max_images_per_article=max(1, args.max_images_per_article),
        force=args.force,
        replace_existing=args.replace_existing,
        dry_run=args.dry_run,
    )
    if args.report_path:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"Source-image research complete: inserted {report['images_inserted']} image(s) "
        f"across {report['articles_changed']} article(s); "
        f"{len(report['articles_without_recovery'])} article(s) unrecovered."
    )
    return 1 if report["articles_without_recovery"] and not report["images_inserted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
