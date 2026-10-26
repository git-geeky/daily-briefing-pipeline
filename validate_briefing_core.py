from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from briefing_manifest import (
    COUNTERPOINT_REQUIRED_SECTIONS,
    EDITION,
    HARD_NEWS_SECTIONS,
    IMAGE_LICENSE_RE,
    SECTION_RULES,
    WATCH_REQUIRED_SECTIONS,
    build_assembled_manifest,
    evaluate_item_rules,
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

    min_bytes = int(EDITION.rules.get("min_assembled_bytes", 100 * 1024))
    if assembled_path.stat().st_size < min_bytes:
        blocking_errors.append(
            f"Assembled markdown is below {min_bytes // 1024} KB: {assembled_path.stat().st_size} bytes"
        )

    header = manifests["header"]["header"]
    summary_low, summary_high = (int(v) for v in EDITION.header.get("exec_summary_words", [600, 800]))
    if not summary_low <= header["executive_summary_word_count"] <= summary_high:
        blocking_errors.append(
            f"Header executive summary is outside the {summary_low}-{summary_high} word range: "
            f"{header['executive_summary_word_count']}"
        )
    min_cross_refs = int(EDITION.header.get("min_cross_references", 3))
    if len(header["cross_references"]) < min_cross_refs:
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

    if EDITION.rules.get("focus_beat_required", True) and assembled_manifest["summary"]["focus_company_article_count"] < 1:
        # briefing-spec: "if there is no material focus-company development, record that
        # no-update decision in the candidate pool/run report rather than padding".
        # The validator used to block regardless, which forced padding on quiet days.
        decision = next(
            (
                item
                for item in candidate_pool.get("no_update_decisions") or []
                if isinstance(item, dict)
                and str(item.get("beat")) == "focus_company"
                and len(str(item.get("reason") or "").strip()) >= 40
            ),
            None,
        )
        if decision:
            warnings.append(f"Focus-company beat recorded as no-update: {str(decision['reason']).strip()[:160]}")
        else:
            blocking_errors.append(
                "Briefing is missing a focus-company-relevant story "
                "(or a no-update decision with a reason in the candidate pool)"
            )
    hn_target = int(EDITION.rules.get("hn_target", 5))
    if hn_target and assembled_manifest["summary"].get("hn_origin_count", 0) < hn_target:
        warnings.append(
            f"Briefing includes fewer than {hn_target} Hacker News-driven stories; "
            "run a second AI/tech discovery pass or record the shortfall explicitly"
        )
    total_inline_images = int(assembled_manifest["summary"].get("total_inline_images") or 0)
    total_high_confidence_images = int(assembled_manifest["summary"].get("total_high_confidence_images") or 0)
    has_image_quality_failure = IMAGE_QUALITY_FAILURE_TEXT in assembled_text
    if EDITION.image_mode == "single":
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
    else:
        max_total = int(EDITION.images.get("max_total", 0) or 0)
        if max_total and total_inline_images > max_total:
            blocking_errors.append(
                f"Briefing embeds {total_inline_images} inline images; this edition allows at most {max_total}"
            )
        if total_inline_images != total_high_confidence_images:
            blocking_errors.append(
                f"{total_inline_images - total_high_confidence_images} embedded image(s) are not classified as "
                "validated high-confidence images"
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
            result = evaluate_item_rules(section, item, rules, audit_available=True)
            blocking_errors.extend(result["blocking"])
            nonblocking_errors.extend(result["nonblocking"])
            warnings.extend(result["warnings"])
            low_image_articles.extend(result["low_image"])

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

