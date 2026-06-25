from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


HOME = Path.home()
RUNNERS = {
    "codex": HOME / ".codex" / "skills" / "news-briefing" / "scripts" / "briefing_paths.py",
    "claude": HOME / ".claude" / "scheduled-tasks" / "daily-intelligence-briefing" / "scripts" / "briefing_paths.py",
}
COMPARISON_ROOT = HOME / "Documents" / "Daily-Briefings" / "generated"
SECTION_ORDER = ("part1", "part2", "part3", "part4", "humaninterest", "updates", "closing")


def load_module(path: Path, name: str):
    parent = str(path.parent)
    added = False
    if parent not in sys.path:
        sys.path.insert(0, parent)
        added = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to load module: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if added and False:  # intentionally keep importable for sibling bare imports
            sys.path.remove(parent)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_path(value: Any) -> Path | None:
    if not value:
        return None
    try:
        return Path(str(value))
    except (TypeError, ValueError):
        return None


def _avg(values: list[int]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _minimum_without_rejections(section_pool: dict[str, Any] | None) -> bool:
    if not isinstance(section_pool, dict):
        return False
    selected = int(section_pool.get("selected_count") or 0)
    min_count = int(section_pool.get("min_count") or 0)
    target_count = int(section_pool.get("target_count") or min_count)
    rejected = len(section_pool.get("rejected") or [])
    quality_failures = len(section_pool.get("quality_failures") or [])
    return target_count > min_count and selected == min_count and rejected == 0 and quality_failures == 0


def _below_target_without_rejections(section_pool: dict[str, Any] | None) -> bool:
    if not isinstance(section_pool, dict):
        return False
    selected = int(section_pool.get("selected_count") or 0)
    min_count = int(section_pool.get("min_count") or 0)
    target_count = int(section_pool.get("target_count") or min_count)
    rejected = len(section_pool.get("rejected") or [])
    quality_failures = len(section_pool.get("quality_failures") or [])
    return target_count > min_count and min_count < selected < target_count and rejected == 0 and quality_failures == 0


def load_runner_state(name: str, date: str) -> dict:
    paths = load_module(RUNNERS[name], f"_comparison_{name}_briefing_paths")
    run_report_path = Path(paths.run_report_path(date))
    assembled_manifest_path = Path(paths.assembled_manifest_path(date))
    validation_report_path = _safe_path((paths.validation_report_path(date) if hasattr(paths, "validation_report_path") else None))
    candidate_pool_path = _safe_path((paths.candidate_pool_path(date) if hasattr(paths, "candidate_pool_path") else None))
    source_audit_path = _safe_path((paths.source_audit_path(date) if hasattr(paths, "source_audit_path") else None))

    run_report = _load_json(run_report_path)
    artifact_paths = run_report.get("artifact_paths") or {}
    validation_report_path = validation_report_path or _safe_path(artifact_paths.get("validation_report"))
    candidate_pool_from_report = _safe_path((run_report.get("candidate_pool") or {}).get("path"))
    source_audit_from_report = _safe_path((run_report.get("source_audit") or {}).get("artifact_path"))
    candidate_pool_path = candidate_pool_path or candidate_pool_from_report
    source_audit_path = source_audit_path or source_audit_from_report

    assembled_manifest = _load_json(assembled_manifest_path)
    validation_report = _load_json(validation_report_path) if validation_report_path else {}
    candidate_pool = _load_json(candidate_pool_path) if candidate_pool_path else {}
    source_audit = _load_json(source_audit_path) if source_audit_path else {}

    summary = (assembled_manifest.get("summary") or {})
    titles = set(summary.get("article_titles") or [])
    article_scorecards = summary.get("article_scorecards") or []
    high_conf_images = sum(item.get("high_confidence_images", 0) for item in article_scorecards)
    section_selected_counts = summary.get("section_selected_counts") or {}
    candidate_sections = candidate_pool.get("sections") or {}
    avg_source_count = _avg([int(item.get("source_count", 0)) for item in article_scorecards])
    avg_watch_next_count = _avg([int(item.get("watch_next_48h_count", 0)) for item in article_scorecards])
    avg_score = _avg([int(item.get("overall_score", 0)) for item in article_scorecards])
    source_summary = source_audit.get("summary") or {}
    source_audit_report = run_report.get("source_audit") or {}

    return {
        "runner": name,
        "run_attempt_id": run_report.get("run_attempt_id"),
        "run_report_path": str(run_report_path),
        "assembled_manifest_path": str(assembled_manifest_path),
        "validation_report_path": str(validation_report_path) if validation_report_path else None,
        "candidate_pool_path": str(candidate_pool_path) if candidate_pool_path else None,
        "source_audit_path": str(source_audit_path) if source_audit_path else None,
        "run_status": run_report.get("run_status"),
        "validation_status": run_report.get("validation_status"),
        "delivery_status": run_report.get("delivery_status"),
        "article_count": len(titles),
        "article_titles": sorted(titles),
        "section_selected_counts": section_selected_counts,
        "focus_company_article_count": int(summary.get("focus_company_article_count") or 0),
        "hn_origin_count": int(summary.get("hn_origin_count") or 0),
        "low_image_article_count": int(summary.get("low_image_article_count") or 0),
        "quality_failure_count": int(summary.get("quality_failure_count") or 0),
        "high_confidence_images": high_conf_images,
        "total_inline_images": int(summary.get("total_inline_images") or 0),
        "total_high_confidence_images": int(summary.get("total_high_confidence_images") or high_conf_images or 0),
        "low_image_articles": run_report.get("low_image_articles") or [],
        "avg_source_count": avg_source_count,
        "avg_watch_next_count": avg_watch_next_count,
        "avg_score": avg_score,
        "warning_count": len(validation_report.get("warnings") or []),
        "nonblocking_error_count": len(validation_report.get("nonblocking_errors") or []),
        "blocking_error_count": len(validation_report.get("blocking_errors") or []),
        "candidate_pool_rejected_count": len(candidate_pool.get("rejected") or []),
        "source_audit_status": source_audit.get("status") or source_audit_report.get("status"),
        "source_audit_catalog_count": int(source_summary.get("catalog_count") or source_audit_report.get("catalog_count") or 0),
        "source_audit_checked_count": int(source_summary.get("checked_count") or source_audit_report.get("checked_count") or 0),
        "source_audit_failed_after_retry_count": int(source_summary.get("failed_after_retry_count") or source_audit_report.get("failed_after_retry_count") or 0),
        "source_audit_unavailable_count": int(source_summary.get("unavailable_count") or source_audit_report.get("unavailable_count") or 0),
        "source_audit_all_sources_checked": bool(source_summary.get("all_sources_checked") or source_audit_report.get("all_sources_checked")),
        "source_audit_used_source_count": int(summary.get("source_audit_used_source_count") or 0),
        "source_audit_incorporated_article_count": int(summary.get("source_audit_incorporated_article_count") or 0),
        "under_target_sections": [
            section
            for section in SECTION_ORDER
            if isinstance(candidate_sections.get(section), dict)
            and int(candidate_sections[section].get("selected_count") or 0) < int(candidate_sections[section].get("target_count") or 0)
        ],
        "minimum_without_rejections_sections": [
            section for section in SECTION_ORDER if _minimum_without_rejections(candidate_sections.get(section))
        ],
        "below_target_without_rejections_sections": [
            section for section in SECTION_ORDER if _below_target_without_rejections(candidate_sections.get(section))
        ],
    }


def comparison_gate(states: dict[str, dict]) -> dict:
    failures: list[str] = []
    for runner, state in states.items():
        if state.get("validation_status") not in {"passed", "warning"}:
            failures.append(f"{runner}: validation_status={state.get('validation_status')}")
        total_inline_images = int(state.get("total_inline_images") or 0)
        total_high_confidence_images = int(state.get("total_high_confidence_images") or 0)
        if state.get("article_count", 0) > 0 and (
            total_inline_images > 1 or (total_inline_images == 1 and total_high_confidence_images != 1)
        ):
            failures.append(
                f"{runner}: image contract failed ({total_inline_images} inline, "
                f"{total_high_confidence_images} validated high-confidence)"
            )
        if state.get("focus_company_article_count", 0) < 1:
            failures.append(f"{runner}: missing focus-company coverage")
        if state.get("source_audit_catalog_count", 0) < 250:
            failures.append(f"{runner}: source audit below 250-source floor")
        if not state.get("source_audit_all_sources_checked"):
            failures.append(f"{runner}: source audit did not check all catalog sources")
        if state.get("source_audit_incorporated_article_count", 0) < state.get("article_count", 0):
            failures.append(f"{runner}: one or more selected articles lack source-audit provenance")
    return {
        "status": "failed" if failures else "passed",
        "failures": failures,
    }


def comparison_report(date: str) -> dict:
    states = {name: load_runner_state(name, date) for name in RUNNERS}
    codex_titles = set(states["codex"]["article_titles"])
    claude_titles = set(states["claude"]["article_titles"])
    shared_gaps: list[str] = []
    if states["claude"].get("hn_origin_count", 0) < 5 and states["codex"].get("hn_origin_count", 0) < 5:
        shared_gaps.append(
            "Both runners are below the Hacker News coverage target; daily AI/tech discovery is not encoding enough HN-driven stories."
        )
    if states["claude"].get("candidate_pool_rejected_count", 0) == 0 and states["codex"].get("candidate_pool_rejected_count", 0) == 0:
        shared_gaps.append(
            "Neither runner recorded rejected candidates; the candidate pool is not yet proving that a second-pass screen happened."
        )
    if not states["claude"].get("source_audit_all_sources_checked") and not states["codex"].get("source_audit_all_sources_checked"):
        shared_gaps.append(
            "Both runners missed the full source-audit floor; the daily 250+ source check is not yet reliable."
        )
    return {
        "date": date,
        "runners": states,
        "title_overlap": sorted(codex_titles & claude_titles),
        "codex_only_titles": sorted(codex_titles - claude_titles),
        "claude_only_titles": sorted(claude_titles - codex_titles),
        "article_count_delta": states["codex"]["article_count"] - states["claude"]["article_count"],
        "high_confidence_image_delta": states["codex"]["high_confidence_images"] - states["claude"]["high_confidence_images"],
        "warning_delta": states["codex"]["warning_count"] - states["claude"]["warning_count"],
        "nonblocking_error_delta": states["codex"]["nonblocking_error_count"] - states["claude"]["nonblocking_error_count"],
        "section_count_delta": {
            section: int(states["codex"]["section_selected_counts"].get(section, 0))
            - int(states["claude"]["section_selected_counts"].get(section, 0))
            for section in SECTION_ORDER
        },
        "quality_delta": {
            "avg_source_count": round(states["codex"]["avg_source_count"] - states["claude"]["avg_source_count"], 2),
            "avg_watch_next_count": round(
                states["codex"]["avg_watch_next_count"] - states["claude"]["avg_watch_next_count"], 2
            ),
            "avg_score": round(states["codex"]["avg_score"] - states["claude"]["avg_score"], 2),
            "hn_origin_count": states["codex"]["hn_origin_count"] - states["claude"]["hn_origin_count"],
            "focus_company_article_count": states["codex"]["focus_company_article_count"] - states["claude"]["focus_company_article_count"],
            "low_image_article_count": states["codex"]["low_image_article_count"] - states["claude"]["low_image_article_count"],
            "source_audit_checked_count": states["codex"]["source_audit_checked_count"] - states["claude"]["source_audit_checked_count"],
            "source_audit_failed_after_retry_count": states["codex"]["source_audit_failed_after_retry_count"] - states["claude"]["source_audit_failed_after_retry_count"],
            "source_audit_used_source_count": states["codex"]["source_audit_used_source_count"] - states["claude"]["source_audit_used_source_count"],
            "source_audit_incorporated_article_count": states["codex"]["source_audit_incorporated_article_count"] - states["claude"]["source_audit_incorporated_article_count"],
        },
        "shared_gaps": shared_gaps,
        "delivery_gate": comparison_gate(states),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Codex and Claude briefing outputs for the same date.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    args = parser.parse_args()

    report = comparison_report(args.date)
    COMPARISON_ROOT.mkdir(parents=True, exist_ok=True)
    path = COMPARISON_ROOT / f"briefing-runner-comparison-{args.date}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

