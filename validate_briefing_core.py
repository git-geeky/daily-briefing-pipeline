from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from briefing_manifest import (
    COUNTERPOINT_REQUIRED_SECTIONS,
    HARD_NEWS_SECTIONS,
    SECTION_RULES,
    WATCH_REQUIRED_SECTIONS,
    build_assembled_manifest,
    generate_all_manifests,
    titles_match,
)
from briefing_paths import assembled_markdown_path, candidate_pool_path, source_audit_path, validation_report_path
from research_vault import build_daily_research_artifacts
from run_reporting import set_step_timing, update_run_report


class BriefingValidationError(RuntimeError):
    pass


IMAGE_QUALITY_FAILURE_TEXT = "Image Quality Failure"


def format_source_audit_breakdown(breakdown: dict) -> str:
    labels = {
        "archive_sidecar_only": "archive sidecar only",
        "auth_required": "auth required",
        "blocked_or_forbidden": "blocked/forbidden",
        "browser_required": "browser required",
        "challenge": "challenge page",
        "rate_limited": "rate limited",
        "unavailable_other": "other unavailable",
    }
    parts = []
    for key, value in sorted(breakdown.items()):
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        parts.append(f"{labels.get(str(key), str(key).replace('_', ' '))}: {count}")
    return ", ".join(parts)


def load_candidate_pool_sections(date: str) -> dict[str, dict]:
    return (load_candidate_pool(date).get("sections") or {})


def load_candidate_pool(date: str) -> dict:
    try:
        path = Path(candidate_pool_path(date))
    except Exception:
        return {}
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_source_audit(date: str) -> dict:
    try:
        path = Path(source_audit_path(date))
    except Exception:
        return {}
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def validate_briefing(
    date: str,
    *,
    write_report: bool = True,
    raise_on_error: bool = True,
) -> dict:
    started = time.time()
    assembled_path = assembled_markdown_path(date)
    if not assembled_path.exists():
        raise FileNotFoundError(f"Assembled markdown not found: {assembled_path}")

    manifests = generate_all_manifests(date, write=True)
    source_audit = load_source_audit(date)
    assembled_manifest = build_assembled_manifest(date, manifests, source_audit=source_audit)
    assembled_text = assembled_path.read_text(encoding="utf-8")

    blocking_errors: list[str] = []
    nonblocking_errors: list[str] = []
    warnings: list[str] = []
    all_titles = assembled_manifest["summary"]["article_titles"]
    title_set = set(all_titles)
    candidate_pool = load_candidate_pool(date)
    candidate_sections = candidate_pool.get("sections") or {}
    research_artifacts = build_daily_research_artifacts(
        date,
        assembled_manifest,
        source_audit=source_audit,
        candidate_pool=candidate_pool,
    )
    try:
        research_lint = json.loads(Path(research_artifacts["research_lint_path"]).read_text(encoding="utf-8"))
    except (OSError, KeyError, json.JSONDecodeError):
        research_lint = {}

    if assembled_path.stat().st_size < 100 * 1024:
        blocking_errors.append(f"Assembled markdown is below 100 KB: {assembled_path.stat().st_size} bytes")

    header = manifests["header"]["header"]
    if not 600 <= header["executive_summary_word_count"] <= 800:
        blocking_errors.append(
            f"Header executive summary is outside the 600-800 word range: {header['executive_summary_word_count']}"
        )
    if len(header["cross_references"]) < 3:
        blocking_errors.append("Header is missing one or more Top 3 Structural Forces cross-references")
    for reference in header["cross_references"]:
        if not any(titles_match(reference, title) for title in title_set):
            blocking_errors.append(f'Header cross-reference does not match a real article title: "{reference}"')

    toc_set = set(header["toc_titles"])
    for title in all_titles:
        if title not in toc_set:
            blocking_errors.append(f'Table of Contents is missing article title: "{title}"')
    for title in header["toc_titles"]:
        if title not in title_set:
            blocking_errors.append(f'Table of Contents includes a title not present in the briefing: "{title}"')

    if assembled_manifest["summary"]["focus_company_article_count"] < 1:
        blocking_errors.append("Briefing is missing a focus-company-relevant story")
    if assembled_manifest["summary"].get("hn_origin_count", 0) < 5:
        warnings.append(
            "Briefing includes fewer than 5 Hacker News-driven stories; "
            "run a second AI/tech discovery pass or record the shortfall explicitly"
        )
    total_inline_images = int(assembled_manifest["summary"].get("total_inline_images") or 0)
    total_high_confidence_images = int(assembled_manifest["summary"].get("total_high_confidence_images") or 0)
    has_image_quality_failure = IMAGE_QUALITY_FAILURE_TEXT in assembled_text
    if total_inline_images > 1:
        blocking_errors.append(
            f"Briefing embeds {total_inline_images} inline images; the final briefing must contain at most one"
        )
    elif total_inline_images == 1 and total_high_confidence_images != 1:
        blocking_errors.append(
            "Briefing embeds one image, but it is not classified as a validated high-confidence image"
        )
    elif total_inline_images == 0:
        if has_image_quality_failure:
            warnings.append("Briefing contains no validated image and includes an explicit image-quality failure marker")
        else:
            blocking_errors.append(
                "Briefing contains no validated image and no explicit image-quality failure marker"
            )
    if not source_audit:
        blocking_errors.append("Source audit artifact is missing; run generate_source_audit.py before drafting or assembly")
    else:
        source_summary = source_audit.get("summary") or {}
        catalog_count = int(source_summary.get("catalog_count") or 0)
        checked_count = int(source_summary.get("checked_count") or 0)
        failed_after_retry = int(source_summary.get("failed_after_retry_count") or 0)
        unavailable = int(source_summary.get("unavailable_count") or 0)
        failure_threshold = int(source_summary.get("failure_threshold") or max(5, catalog_count // 20))
        if catalog_count < 250:
            blocking_errors.append(f"Source audit catalog is below the 250-source floor: {catalog_count}")
        if checked_count < catalog_count:
            blocking_errors.append(f"Source audit checked only {checked_count}/{catalog_count} catalog sources")
        if source_summary.get("systemic_failure") or failed_after_retry > failure_threshold:
            blocking_errors.append(
                f"Source audit has systemic source-check failure ({failed_after_retry} failed after retry; threshold {failure_threshold})"
            )
        elif failed_after_retry:
            warnings.append(f"Source audit recorded {failed_after_retry} sources failed after retry")
        if unavailable:
            breakdown = format_source_audit_breakdown(source_summary.get("unavailable_breakdown") or {})
            if breakdown:
                warnings.append(f"Source audit recorded {unavailable} degraded source-access results ({breakdown})")
            else:
                warnings.append(f"Source audit recorded {unavailable} unavailable, paywalled, blocked, or rate-limited sources")
        bucket_counts = source_summary.get("bucket_counts") or {}
        for bucket, counts in bucket_counts.items():
            total = int((counts or {}).get("total") or 0)
            checked = int((counts or {}).get("checked") or 0)
            if total and checked < total:
                blocking_errors.append(f"Source audit bucket under-covered: {bucket} ({checked}/{total})")

    low_image_articles: list[dict] = []
    for section, rules in SECTION_RULES.items():
        items = manifests[section]["items"]
        real_items = [item for item in items if not item.get("quality_failure")]
        failure_items = [item for item in items if item.get("quality_failure")]
        pool_section = candidate_sections.get(section) or {}

        min_count = rules.get("min_count", 0)
        target_count = rules.get("target_count", min_count)
        max_count = rules.get("max_count", target_count)
        if len(real_items) < min_count:
            if failure_items:
                warnings.append(
                    f"{section} is below adaptive minimum but contains an explicit quality failure marker "
                    f"({len(real_items)}/{target_count})"
                )
            else:
                blocking_errors.append(f"{section} article count is {len(real_items)} but minimum is {min_count}")
        elif len(real_items) < target_count:
            warnings.append(
                f"{section} is below target_count under adaptive quota "
                f"({len(real_items)}/{target_count})"
            )
        if (
            section in HARD_NEWS_SECTIONS
            and target_count > min_count
            and not failure_items
            and len(pool_section.get("rejected") or []) == 0
        ):
            if len(real_items) == min_count:
                warnings.append(
                    f"{section} stopped at the adaptive minimum with zero rejected candidates; "
                    f"run a second discovery pass before accepting {len(real_items)}/{target_count}"
                )
            elif min_count < len(real_items) < target_count:
                warnings.append(
                    f"{section} is below target_count with zero rejected candidates; "
                    f"discovery likely ended before a real second-pass screen ({len(real_items)}/{target_count})"
                )
        if len(real_items) > max_count:
            blocking_errors.append(f"{section} article count is {len(real_items)} but maximum is {max_count}")

        if section == "updates" and real_items and not manifests["part1"]["date"]:
            warnings.append("Updates section is populated; verify it is based on prior-briefing context rather than reused draft text")

        for item in real_items:
            prefix = f'{section}/{item["title"]}'
            if section in COUNTERPOINT_REQUIRED_SECTIONS and not item["has_counterpoint_matrix"]:
                blocking_errors.append(f"{prefix}: missing Counterpoint Matrix")
            if section in WATCH_REQUIRED_SECTIONS and not item["watch_next_48h"]:
                blocking_errors.append(f"{prefix}: missing Watch Next 48h block")
            if not item["source_appendix_present"]:
                blocking_errors.append(f"{prefix}: missing source appendix")
            soft_min = rules.get("soft_min_words", rules["min_words"])
            hard_floor = rules.get("hard_floor", soft_min)
            if item["word_count"] < hard_floor:
                blocking_errors.append(
                    f'{prefix}: article is below the hard word floor '
                    f'({item["word_count"]} < {hard_floor}); target is {rules["min_words"]}'
                )
            elif item["word_count"] < soft_min:
                nonblocking_errors.append(
                    f'{prefix}: article is below the soft word threshold but above the hard floor '
                    f'({item["word_count"]} < {soft_min}, >= {hard_floor})'
                )
            elif item["word_count"] < rules["min_words"]:
                warnings.append(
                    f'{prefix}: article is below the target word count but within the soft band '
                    f'({item["word_count"]} < {rules["min_words"]}, >= {soft_min})'
                )
            if item["source_count"] < rules["min_sources"]:
                blocking_errors.append(
                    f'{prefix}: article is below the source threshold ({item["source_count"]} < {rules["min_sources"]})'
                )
            if not item.get("source_incorporated"):
                blocking_errors.append(f"{prefix}: cites no source checked in the daily source-audit catalog")
            min_section_sources = rules.get("min_section_sources", 0)
            if min_section_sources:
                section_pool_hits = sum(
                    1 for tag in item.get("source_sections_cited", []) if tag == section
                )
                if section_pool_hits < min_section_sources:
                    blocking_errors.append(
                        f"{prefix}: cites only {section_pool_hits} sources from the {section}-tagged "
                        f"catalog pool (minimum is {min_section_sources}); open SOURCES.md and pull "
                        f"from the dedicated section list — TIER 0 wires and cross-section sources do not count"
                    )
            if item["banned_topics"]:
                blocking_errors.append(f"{prefix}: contains banned topic(s): {', '.join(item['banned_topics'])}")
            if item.get("editorial_boilerplate"):
                blocking_errors.append(
                    f"{prefix}: contains repeated editorial process boilerplate "
                    f"({', '.join(item['editorial_boilerplate'])})"
                )
            if item["has_body_links"]:
                blocking_errors.append(f"{prefix}: contains external links in the body outside the source appendix")
            if item["relative_time_mentions"] and not item["has_exact_jst_date"]:
                blocking_errors.append(f"{prefix}: uses relative timing without an exact JST date/time anchor")
            if item["needs_second_pass"]:
                blocking_errors.append(f"{prefix}: thin sourcing requires a second corroboration pass")
            if item["image_quality"]["duplicate_urls"]:
                blocking_errors.append(f"{prefix}: contains duplicate image URLs in a single article")
            if section in HARD_NEWS_SECTIONS and item["image_quality"].get("all_placeholder"):
                blocking_errors.append(
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

    for issue in research_lint.get("issues") or []:
        message = f"Research lint {issue.get('rule')}: {issue.get('article') or issue.get('section') or 'run'}: {issue.get('message')}"
        if issue.get("severity") == "error":
            if message not in blocking_errors:
                blocking_errors.append(message)
        elif issue.get("severity") == "warning":
            if message not in warnings:
                warnings.append(message)

    if blocking_errors:
        validation_status = "failed"
        run_status = "failed"
    elif warnings:
        validation_status = "warning"
        run_status = "completed"
    else:
        validation_status = "passed"
        run_status = "completed"

    report = {
        "date": date,
        "status": "ok" if not blocking_errors else "failed",
        "blocking_errors": blocking_errors,
        "nonblocking_errors": nonblocking_errors,
        "errors": blocking_errors,
        "warnings": warnings,
        "summary": assembled_manifest["summary"],
        "assembled_markdown": {
            "path": str(assembled_path),
            "size_bytes": assembled_path.stat().st_size,
        },
    }

    if write_report:
        validation_report_path(date).write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    ended = time.time()
    update_run_report(
        date,
        run_status=run_status,
        validation_status=validation_status,
        error_counts={
            "blocking": len(blocking_errors),
            "nonblocking": len(nonblocking_errors),
            "warnings": len(warnings),
        },
        low_image_articles=low_image_articles,
        manifest_summary=assembled_manifest["summary"],
    )
    set_step_timing(
        date,
        "validation",
        status=validation_status,
        started_at=datetime.fromtimestamp(started).astimezone().isoformat(),
        ended_at=datetime.fromtimestamp(ended).astimezone().isoformat(),
        duration_seconds=round(ended - started, 3),
        details={"report_path": str(validation_report_path(date))},
    )

    if blocking_errors and raise_on_error:
        raise BriefingValidationError("\n".join(blocking_errors))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate briefing structure and quality gates.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    args = parser.parse_args()

    try:
        report = validate_briefing(args.date)
    except Exception as exc:
        print(str(exc))
        return 1

    print(
        f"Validation passed for {args.date}: "
        f"{report['summary']['total_articles']} articles, "
        f"{report['summary']['focus_company_article_count']} focus-company stories"
    )
    if report["warnings"]:
        print("Warnings:")
        for warning in report["warnings"]:
            print(f"- {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

