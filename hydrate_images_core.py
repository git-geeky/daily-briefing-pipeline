"""Safety-net image hydration for zero-image articles.

`fetch_briefing_images_core.py` does NOT insert images into zero-image articles
by design (per the briefing-spec empty-article-fallback policy). That policy
assumes the research step reliably embeds inline image tags. The 2026-04-21
Codex run violated that assumption — 22 of 22 articles had zero inline images,
and the fetcher correctly found nothing to fetch.

This module provides an opt-in "hydrate zero-image articles" pass that reuses
the Phase 2 (og:image extraction) and Phase 3 (Commons search) helpers from
`fetch_briefing_images_core` to recover 1–3 images per zero-image article,
specifically for the Codex runner until its research step reliably embeds
inline.

**Usage** (run AFTER the research step but BEFORE `fetch_briefing_images.py`):

    python3 hydrate_images_core.py --date 2026-04-21

The Codex runner wraps this via its own per-runner `scripts/hydrate_images.py`
stub that imports from shared core — matching the wrapper pattern used by
every other `*_core.py` file in this directory.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Import siblings from the same pipeline-shared directory.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import briefing_paths as briefing_paths_module  # noqa: E402
import fetch_briefing_images_core as fi  # noqa: E402
from briefing_paths import fragment_path, image_cache_dir  # noqa: E402


IMAGE_TAG_RE = fi.re.compile(r"!\[[^\]]*\]\([^)]+\)")


def count_inline_images(body: str) -> int:
    return sum(
        1
        for alt, url in fi.re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", body)
        if not fi.is_placeholder_image_tag(alt, url.strip())
    )


def strip_placeholder_images(body: str) -> tuple[str, int]:
    removed = 0

    def replace(match) -> str:
        nonlocal removed
        alt = match.group(1)
        url = match.group(2).strip()
        if fi.is_placeholder_image_tag(alt, url):
            removed += 1
            return ""
        return match.group(0)

    cleaned = fi.re.sub(r"!\[([^\]]*)\]\(([^)]+)\)\n?", replace, body)
    return cleaned, removed


def hydrate_article(
    sess,
    title: str,
    body: str,
    img_cache: Path,
    *,
    max_images: int = 2,
) -> tuple[str, list[dict]]:
    """Try to add up to `max_images` images to an article that has none.

    Phase 2: extract og:image from each URL in the Primary Sources block.
    Phase 3: Wikipedia Commons search using article keywords.

    Returns `(new_body, recoveries)` where `recoveries` is a list of
    `{"source": "og-image"|"commons", "url": ..., "local_path": ...}` dicts.
    Never fabricates URLs — if both phases return no candidates, the body
    is returned unchanged and `recoveries` is empty.
    """
    body, placeholders_removed = strip_placeholder_images(body)

    if count_inline_images(body) > 0:
        return body, []

    recoveries: list[dict] = []

    # Phase 2: og:image from Primary Sources URLs
    primary_urls = fi.extract_body_urls(body)
    for url in primary_urls:
        if len(recoveries) >= max_images:
            break
        og_url = fi.extract_og_image(sess, url)
        if not og_url:
            continue
        local_path = fi.download_image(sess, og_url, img_cache)
        if local_path is None:
            continue
        alt = f"Hero image from {url} (Source: og:image)"
        body = fi.insert_image_after_subtitle(body, str(local_path), alt)
        recoveries.append(
            {
                "source": "og-image",
                "source_url": url,
                "image_url": og_url,
                "local_path": str(local_path),
                "placeholders_removed": placeholders_removed,
            }
        )

    if len(recoveries) >= max_images:
        return body, recoveries

    # Phase 3: Commons search using article keywords
    keywords = fi.article_keywords(f"{title}\n{body}", n=5)
    if keywords:
        query = " ".join(keywords[:3])
        candidates = fi.commons_search(sess, query, n=max_images - len(recoveries))
        for candidate in candidates:
            if len(recoveries) >= max_images:
                break
            candidate_url = candidate.get("url") if isinstance(candidate, dict) else candidate
            if not candidate_url:
                continue
            local_path = fi.download_image(sess, candidate_url, img_cache)
            if local_path is None:
                continue
            alt = f"{title} (Source: Wikimedia Commons)"
            body = fi.insert_image_after_subtitle(body, str(local_path), alt)
            recoveries.append(
                {
                    "source": "commons",
                    "query": query,
                    "image_url": candidate_url,
                    "local_path": str(local_path),
                    "placeholders_removed": placeholders_removed,
                }
            )

    if not recoveries and placeholders_removed:
        recoveries.append(
            {
                "source": "placeholder-removal",
                "local_path": None,
                "placeholders_removed": placeholders_removed,
            }
        )

    return body, recoveries


def hydrate_date(date: str, *, max_images_per_article: int = 2) -> dict[str, Any]:
    """Walk all fragments for a date and hydrate zero-image articles."""
    sess = fi.make_session()
    img_cache = image_cache_dir(date)
    img_cache.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "date": date,
        "runner": getattr(briefing_paths_module, "runner_name", lambda: "unknown")(),
        "sections": {},
        "total_articles_scanned": 0,
        "total_zero_image_articles": 0,
        "total_placeholder_only_articles": 0,
        "total_placeholders_removed": 0,
        "total_recoveries": 0,
    }

    for section in fi.SECTIONS:
        frag = fragment_path(section, date)
        if not frag.exists():
            continue
        text = frag.read_text(encoding="utf-8")
        pre, articles = fi.split_articles(text)

        section_report: dict[str, Any] = {
            "articles_scanned": len(articles),
            "zero_image_articles": 0,
            "placeholder_only_articles": 0,
            "hydrated": [],
        }
        new_articles: list[tuple[str, str]] = []

        for title, body in articles:
            report["total_articles_scanned"] += 1
            raw_image_count = len(IMAGE_TAG_RE.findall(body))
            non_placeholder_count = count_inline_images(body)
            if non_placeholder_count > 0:
                new_articles.append((title, body))
                continue

            if raw_image_count:
                section_report["placeholder_only_articles"] += 1
                report["total_placeholder_only_articles"] += 1
            else:
                section_report["zero_image_articles"] += 1
                report["total_zero_image_articles"] += 1

            new_body, recoveries = hydrate_article(
                sess,
                title,
                body,
                img_cache,
                max_images=max_images_per_article,
            )
            new_articles.append((title, new_body))
            if recoveries:
                removed_count = max((int(item.get("placeholders_removed") or 0) for item in recoveries), default=0)
                report["total_placeholders_removed"] += removed_count
                section_report["hydrated"].append(
                    {
                        "article_title": title,
                        "recoveries": recoveries,
                    }
                )
                report["total_recoveries"] += sum(1 for item in recoveries if item.get("local_path"))

        report["sections"][section] = section_report

        # Only rewrite if we added something
        if any(h.get("recoveries") for h in section_report["hydrated"]):
            rebuilt = fi.reassemble(pre, new_articles)
            frag.write_text(rebuilt, encoding="utf-8")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hydrate zero-image articles with og:image + Commons fallback.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    parser.add_argument(
        "--max-images-per-article",
        type=int,
        default=2,
        help="Maximum number of images to insert per zero-image article (default: 2).",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional path to write the hydration report JSON.",
    )
    args = parser.parse_args()

    report = hydrate_date(args.date, max_images_per_article=args.max_images_per_article)

    print(
        f"Hydration complete: scanned {report['total_articles_scanned']} articles, "
        f"{report['total_zero_image_articles']} had zero images, "
        f"{report['total_recoveries']} recoveries inserted."
    )

    if args.report_path:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Report: {args.report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
