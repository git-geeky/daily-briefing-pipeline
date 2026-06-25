from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from briefing_manifest import (
    HIGH_CONFIDENCE_IMAGE_THRESHOLD,
    IMAGE_RE,
    extract_top_level_title,
    generate_all_manifests,
    load_source_audit,
    normalize_section_title,
    select_section_items,
    split_items,
)
from briefing_paths import (
    SECTIONS,
    assembled_manifest_path,
    assembled_markdown_path,
    ensure_layout,
    fragment_path,
    image_cache_dir,
)
from run_reporting import ensure_run_attempt, set_step_timing, update_run_report
from research_vault import build_daily_research_artifacts


SECTION_TOC_ORDER = ("part1", "part2", "part3", "part4", "humaninterest", "updates")
IMAGE_QUALITY_FAILURE_MARKER = (
    "> **Image Quality Failure:** No trustworthy image validated for this briefing; "
    "no decorative fallback image was embedded."
)


def normalize_closing_fragment(text: str) -> str:
    lines = text.splitlines()
    seen_primary_item = False
    normalized: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            if not seen_primary_item:
                seen_primary_item = True
            else:
                line = line.replace("## ", "### ", 1)
        normalized.append(line)
    return "\n".join(normalized).rstrip() + "\n"


def rebuild_section_text(section: str, text: str, accepted_indices: list[int]) -> str:
    section_title = normalize_section_title(section, extract_top_level_title(text))
    items = split_items(text)
    kept_blocks: list[str] = []
    accepted_set = set(accepted_indices)
    for index, (heading, body) in enumerate(items):
        if index not in accepted_set:
            continue
        block = f"## {heading}".rstrip()
        if body:
            block = f"{block}\n\n{body.strip()}".rstrip()
        kept_blocks.append(block)
    if not section_title:
        return "\n\n".join(kept_blocks).rstrip() + "\n"
    if not kept_blocks:
        return f"# {section_title}\n"
    return f"# {section_title}\n\n" + "\n\n".join(kept_blocks).rstrip() + "\n"


def sync_header_table_of_contents(header_text: str, manifests: dict[str, dict]) -> str:
    toc_lines: list[str] = []
    for section in SECTION_TOC_ORDER:
        manifest = manifests.get(section) or {}
        titles = [item["title"] for item in manifest.get("items", []) if not item.get("quality_failure")]
        if not titles:
            continue
        toc_title = normalize_section_title(section, manifest.get("section_title")) or section.upper()
        toc_lines.append(f"**{toc_title}**")
        for index, title in enumerate(titles, start=1):
            toc_lines.append(f"{index}. {title}")
        toc_lines.append("")

    closing_manifest = manifests.get("closing") or {}
    closing_titles = [item["title"] for item in closing_manifest.get("items", []) if not item.get("quality_failure")]
    if closing_titles:
        closing_title = normalize_section_title("closing", closing_manifest.get("section_title")) or "CLOSING ANALYSIS"
        toc_lines.append(f"**{closing_title}**")
        toc_lines.append(f"- {closing_titles[0]}")

    lines = header_text.rstrip().splitlines()
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        if line.strip().lower() == "### table of contents":
            start = index + 1
            continue
        if start is not None and line.strip().startswith("## "):
            end = index
            break
    if start is None:
        return header_text

    while start < len(lines) and not lines[start].strip():
        start += 1
    rebuilt = lines[:start]
    rebuilt.extend(toc_lines)
    if end < len(lines):
        rebuilt.extend([""] + lines[end:])
    return "\n".join(rebuilt).rstrip() + "\n"


def select_single_valid_image_url(manifests: dict[str, dict]) -> str | None:
    for section in SECTIONS:
        for item in (manifests.get(section) or {}).get("items", []):
            if item.get("quality_failure"):
                continue
            for image in item.get("image_candidates") or []:
                confidence = float(image.get("confidence") or 0.0)
                if (
                    confidence >= HIGH_CONFIDENCE_IMAGE_THRESHOLD
                    and not image.get("placeholder")
                    and image.get("kind") != "weak"
                ):
                    return str(image.get("url") or "").strip() or None
    return None


def enforce_single_inline_image(fragment_texts: dict[str, str], selected_url: str | None) -> dict[str, str]:
    selected_used = False
    pruned: dict[str, str] = {}

    def replace_image(match):
        nonlocal selected_used
        url = match.group(2).strip()
        if selected_url and not selected_used and url == selected_url:
            selected_used = True
            return match.group(0)
        return ""

    for part, text in fragment_texts.items():
        pruned[part] = IMAGE_RE.sub(replace_image, text).rstrip() + "\n"

    if selected_url and selected_used:
        return pruned

    header = pruned.get("header")
    if header is not None and IMAGE_QUALITY_FAILURE_MARKER not in header:
        pruned["header"] = header.rstrip() + "\n\n" + IMAGE_QUALITY_FAILURE_MARKER + "\n"
    return pruned


def write_candidate_pool(date: str, candidate_pool: dict) -> Path | None:
    import briefing_paths as briefing_paths_module

    helper = getattr(briefing_paths_module, "candidate_pool_path", None)
    if not callable(helper):
        return None
    path = Path(helper(date))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate_pool, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def assemble(date: str):
    started = time.time()
    missing = []
    fragments: list[str] = []
    fragment_texts: dict[str, str] = {}

    ensure_layout()
    ensure_run_attempt(date)
    for part in SECTIONS:
        fragment = fragment_path(part, date)
        if not fragment.exists():
            missing.append(fragment)
            continue
        text = fragment.read_text(encoding="utf-8").rstrip()
        if part == "closing":
            text = normalize_closing_fragment(text)
        fragment_texts[part] = text
        fragments.append(text)

    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise FileNotFoundError(f"Missing fragment files:\n{joined}")

    initial_manifests = generate_all_manifests(date, fragment_texts=fragment_texts, write=False)
    candidate_pool = {
        "schema_version": 1,
        "date": date,
        "quota_mode": "adaptive",
        "sections": {},
        "accepted": [],
        "rejected": [],
    }
    for section in SECTIONS:
        if section == "header":
            continue
        selection = select_section_items(section, initial_manifests[section]["items"])
        candidate_pool["sections"][section] = {
            "selected_count": selection["selected_count"],
            "target_count": selection["target_count"],
            "min_count": selection["min_count"],
            "max_count": selection["max_count"],
            "accepted": selection["accepted"],
            "rejected": selection["rejected"],
            "quality_failures": selection["quality_failures"],
        }
        candidate_pool["accepted"].extend(selection["accepted"])
        candidate_pool["rejected"].extend(selection["rejected"])
        fragment_texts[section] = rebuild_section_text(section, fragment_texts[section], selection["accepted_indices"]).rstrip()

    pruned_manifests = generate_all_manifests(date, fragment_texts=fragment_texts, write=False)
    if "header" in fragment_texts:
        fragment_texts["header"] = sync_header_table_of_contents(fragment_texts["header"], pruned_manifests).rstrip()
    selected_image_url = select_single_valid_image_url(pruned_manifests)
    fragment_texts = enforce_single_inline_image(fragment_texts, selected_image_url)

    for part in SECTIONS:
        fragment = fragment_path(part, date)
        normalized = fragment_texts[part].rstrip() + "\n"
        if fragment.read_text(encoding="utf-8") != normalized:
            fragment.write_text(normalized, encoding="utf-8")

    final_manifests = generate_all_manifests(date, fragment_texts=fragment_texts, write=True)
    candidate_pool_path = write_candidate_pool(date, candidate_pool)
    assembled_manifest_payload = json.loads(assembled_manifest_path(date).read_text(encoding="utf-8"))
    research_artifacts = build_daily_research_artifacts(
        date,
        assembled_manifest_payload,
        source_audit=load_source_audit(date),
        candidate_pool=candidate_pool,
    )
    output = assembled_markdown_path(date)
    ordered_fragments = [fragment_texts[part].rstrip() for part in SECTIONS if part in fragment_texts]
    output.write_text("\n\n".join(ordered_fragments) + "\n", encoding="utf-8")
    ended = time.time()
    final_summary = json.loads(assembled_manifest_path(date).read_text(encoding="utf-8")).get("summary") or {}
    update_run_report(
        date,
        run_status="pending",
        quota_mode="adaptive",
        manifest_summary=final_summary,
        candidate_rejections=candidate_pool["rejected"],
        candidate_pool={
            "path": str(candidate_pool_path) if candidate_pool_path else None,
            "rejected_count": len(candidate_pool["rejected"]),
        },
        research_artifacts=research_artifacts,
    )
    set_step_timing(
        date,
        "assembly",
        status="passed",
        started_at=datetime.fromtimestamp(started).astimezone().isoformat(),
        ended_at=datetime.fromtimestamp(ended).astimezone().isoformat(),
        duration_seconds=round(ended - started, 3),
        details={
            "markdown_path": str(output),
            "markdown_bytes": output.stat().st_size,
            "candidate_pool_path": str(candidate_pool_path) if candidate_pool_path else None,
            "selected_counts": final_summary.get("section_selected_counts"),
        },
    )
    return output


def write_briefing_overview_svg(date: str) -> Path | None:
    manifest_path = Path(assembled_manifest_path(date))
    if not manifest_path.exists():
        return None

    summary = json.loads(manifest_path.read_text(encoding="utf-8")).get("summary") or {}
    scorecards = summary.get("article_scorecards") or []
    total_articles = summary.get("total_articles", len(scorecards))
    focus_company_count = summary.get("focus_company_article_count", 0)
    hn_count = summary.get("hn_origin_count", 0)
    quality_failures = summary.get("quality_failure_count", 0)
    avg_score = round(
        sum(card.get("overall_score", 0) for card in scorecards) / len(scorecards)
    ) if scorecards else 0

    cards = [
        ("Articles", str(total_articles), "#0f766e"),
        ("Focus Company", str(focus_company_count), "#1d4ed8"),
        ("HN Stories", str(hn_count), "#b45309"),
        ("Avg Score", str(avg_score), "#7c3aed"),
        ("Quality Gaps", str(quality_failures), "#be123c"),
    ]

    svg_lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="190" viewBox="0 0 960 190" role="img" aria-labelledby="title desc">',
        '  <title id="title">Briefing overview metrics</title>',
        f'  <desc id="desc">{escape(f"Autogenerated overview for {date}: {total_articles} articles, {focus_company_count} focus-company stories, {hn_count} Hacker News-driven stories, average score {avg_score}, {quality_failures} quality gaps.")}</desc>',
        '  <rect x="0" y="0" width="960" height="190" rx="20" fill="#f8fafc" />',
        '  <rect x="18" y="18" width="924" height="154" rx="18" fill="#ffffff" stroke="#cbd5e1" stroke-width="2" />',
        '  <text x="36" y="56" font-family="Helvetica, Arial, sans-serif" font-size="28" font-weight="700" fill="#0f172a">Briefing Overview</text>',
        f'  <text x="36" y="84" font-family="Helvetica, Arial, sans-serif" font-size="14" fill="#475569">Autogenerated trusted SVG from the assembled manifest for {escape(date)}</text>',
    ]

    card_width = 164
    gap = 14
    start_x = 36
    y = 104
    for index, (label, value, accent) in enumerate(cards):
        x = start_x + index * (card_width + gap)
        svg_lines.extend(
            [
                f'  <rect x="{x}" y="{y}" width="{card_width}" height="52" rx="14" fill="#ffffff" stroke="{accent}" stroke-width="2" />',
                f'  <rect x="{x}" y="{y}" width="{card_width}" height="8" rx="8" fill="{accent}" />',
                f'  <text x="{x + 14}" y="{y + 28}" font-family="Helvetica, Arial, sans-serif" font-size="13" fill="#475569">{escape(label)}</text>',
                f'  <text x="{x + 14}" y="{y + 46}" font-family="Helvetica, Arial, sans-serif" font-size="22" font-weight="700" fill="#0f172a">{escape(value)}</text>',
            ]
        )
    svg_lines.append("</svg>")

    image_dir = Path(image_cache_dir(date))
    image_dir.mkdir(parents=True, exist_ok=True)
    svg_path = image_dir / "briefing-overview.svg"
    svg_path.write_text("\n".join(svg_lines) + "\n", encoding="utf-8")
    return svg_path


def inject_overview_svg(header_text: str, svg_path: Path) -> str:
    image_block = f"![Briefing overview metrics (authored SVG)]({svg_path})"
    marker = "## Executive Summary"
    if marker in header_text:
        return header_text.replace(marker, f"{image_block}\n\n{marker}", 1)
    return f"{header_text.rstrip()}\n\n{image_block}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble briefing fragments into one markdown file.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    args = parser.parse_args()

    output = assemble(args.date)
    print(f"Wrote {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

