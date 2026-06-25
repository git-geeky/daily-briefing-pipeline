from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import briefing_paths as briefing_paths_module
from briefing_manifest import HARD_NEWS_SECTIONS, SECTION_FALLBACK_TITLES, SECTION_RULES
from run_reporting import set_step_timing, update_run_report


SCHEMA_VERSION = 1
SUCCESS_STATUSES = {
    "checked_used",
    "checked_relevant_not_selected",
    "checked_no_relevant_update",
}
CHECKED_STATUSES = {
    "checked_used",
    "checked_relevant_not_selected",
    "checked_no_relevant_update",
    "checked_unavailable",
    "checked_failed_after_retry",
}
SOURCE_ATTRIBUTION = "dzhng/deep-research-inspired"


@dataclass(frozen=True)
class ResearchDepthPaths:
    json_path: Path
    markdown_path: Path


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _optional_path(name: str, *args) -> Path | None:
    helper = getattr(briefing_paths_module, name, None)
    if not callable(helper):
        return None
    path = Path(helper(*args))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def research_depth_paths(date: str) -> ResearchDepthPaths:
    json_path = _optional_path("research_depth_path", date)
    markdown_path = _optional_path("research_depth_markdown_path", date)
    if json_path is None:
        generated = Path(getattr(briefing_paths_module, "generated_dir")())
        stem = getattr(briefing_paths_module, "artifact_stem", lambda: "briefing")()
        json_path = generated / f"{stem}-{date}-research-depth.json"
    if markdown_path is None:
        markdown_path = json_path.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    return ResearchDepthPaths(json_path=json_path, markdown_path=markdown_path)


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_default_artifacts(date: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source_audit = _load_json(_optional_path("source_audit_path", date))
    assembled_manifest = _load_json(_optional_path("assembled_manifest_path", date))
    candidate_pool = _load_json(_optional_path("candidate_pool_path", date))
    return source_audit, assembled_manifest, candidate_pool


def _dedupe(values: list[str], *, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if limit is not None and len(out) >= limit:
            break
    return out


def _sources(source_audit: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        source
        for source in (source_audit or {}).get("sources") or []
        if isinstance(source, dict)
    ]


def _source_lookup(source_audit: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(source.get("source_id")): source
        for source in _sources(source_audit)
        if source.get("source_id")
    }


def _section_sources(sources: list[dict[str, Any]], section: str) -> list[dict[str, Any]]:
    return [
        source
        for source in sources
        if str(source.get("section") or "") == section
    ]


def _count_statuses(sources: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in sources:
        status = str(source.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _source_pool_summary(section: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    dedicated = _section_sources(sources, section)
    status_counts = _count_statuses(dedicated)
    checked_count = sum(1 for source in dedicated if str(source.get("status") or "") in CHECKED_STATUSES)
    success_count = sum(1 for source in dedicated if str(source.get("status") or "") in SUCCESS_STATUSES)
    used_count = sum(1 for source in dedicated if str(source.get("status") or "") == "checked_used")
    return {
        "dedicated_source_count": len(dedicated),
        "checked_count": checked_count,
        "success_count": success_count,
        "used_count": used_count,
        "status_counts": status_counts,
        "sample_source_ids": [str(source.get("source_id")) for source in dedicated[:12] if source.get("source_id")],
    }


def _manifest_articles(assembled_manifest: dict[str, Any] | None, section: str) -> list[dict[str, Any]]:
    section_payload = ((assembled_manifest or {}).get("sections") or {}).get(section) or {}
    return [
        item
        for item in section_payload.get("items") or []
        if isinstance(item, dict) and not item.get("quality_failure")
    ]


def _pool_for_section(candidate_pool: dict[str, Any] | None, section: str) -> dict[str, Any]:
    return (((candidate_pool or {}).get("sections") or {}).get(section) or {})


def _candidate_summary(section: str, candidate_pool: dict[str, Any] | None, articles: list[dict[str, Any]]) -> dict[str, Any]:
    rules = SECTION_RULES.get(section, {})
    pool = _pool_for_section(candidate_pool, section)
    selected_count = int(pool.get("selected_count") or len(articles) or 0)
    target_count = int(pool.get("target_count") or rules.get("target_count") or 0)
    min_count = int(pool.get("min_count") or rules.get("min_count") or 0)
    max_count = int(pool.get("max_count") or rules.get("max_count") or target_count or 0)
    rejected = pool.get("rejected") or []
    accepted = pool.get("accepted") or []
    quality_failures = pool.get("quality_failures") or []
    return {
        "selected_count": selected_count,
        "target_count": target_count,
        "min_count": min_count,
        "max_count": max_count,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "quality_failure_count": len(quality_failures),
        "has_candidate_pool": bool(pool),
    }


def _section_breadth(section: str, candidate: dict[str, Any]) -> int:
    target = int(candidate.get("target_count") or 0)
    if section in HARD_NEWS_SECTIONS:
        return max(3, min(10, target or 4))
    return max(1, min(4, target or 1))


def _section_depth(section: str) -> int:
    return 2 if section in HARD_NEWS_SECTIONS else 1


def _seed_queries(section: str, date: str, breadth: int) -> list[dict[str, Any]]:
    label = SECTION_FALLBACK_TITLES.get(section, section).replace(" - ", " ")
    seeds = [
        (
            f"{date} JST {label} latest primary reporting official source",
            "Find live primary reporting and official documents before writing any narrative.",
        ),
        (
            f"{date} JST {label} counterpoint analysis contradictory evidence",
            "Find credible disagreement and incentives for the Counterpoint Matrix.",
        ),
        (
            f"{date} JST {label} schedule filing calendar next 48 hours",
            "Find dated events for Watch Next 48h and avoid relative-time drift.",
        ),
        (
            f"{date} JST {label} regional native language source corroboration",
            "Corroborate with local or native-language sources where the story is regional.",
        ),
        (
            f"{date} JST {label} source images charts maps filings",
            "Find source-derived visual evidence before image remediation.",
        ),
    ]
    if section == "part2":
        seeds.extend(
            [
                (
                    f"{date} JST focus company AI infrastructure custom silicon customers supply chain",
                    "Maintain the daily focus-company-relevant discovery lane.",
                ),
                (
                    f"{date} JST Hacker News AI agents model infrastructure developer tools discussion",
                    "Find high-signal HN-corroborated AI and developer-infrastructure stories.",
                ),
            ]
        )
    if section == "part4":
        seeds.append(
            (
                f"{date} JST Japan official ministry local newspaper source corroboration",
                "Force Japanese-source corroboration before accepting Japan coverage.",
            )
        )
    if section == "updates":
        seeds.append(
            (
                f"{date} JST prior briefing update material change official confirmation",
                "Accept updates only when the prior watch item actually moved.",
            )
        )
    unique_pairs: list[tuple[str, str]] = []
    seen_queries: set[str] = set()
    for query, goal in seeds:
        if query in seen_queries:
            continue
        seen_queries.add(query)
        unique_pairs.append((query, goal))
        if len(unique_pairs) >= breadth:
            break
    return [
        {
            "depth": 0,
            "breadth_slot": index,
            "query": query,
            "research_goal": goal,
        }
        for index, (query, goal) in enumerate(unique_pairs, start=1)
    ]


def _learning_rows(
    section: str,
    articles: list[dict[str, Any]],
    candidate: dict[str, Any],
    source_pool: dict[str, Any],
) -> list[str]:
    learnings: list[str] = []
    if candidate.get("has_candidate_pool"):
        learnings.append(
            "{section} selected {selected}/{target} candidates with {rejected} rejected and {quality} quality-failure markers.".format(
                section=section,
                selected=candidate["selected_count"],
                target=candidate["target_count"],
                rejected=candidate["rejected_count"],
                quality=candidate["quality_failure_count"],
            )
        )
    else:
        learnings.append(
            "{section} has no candidate-pool screen yet; use breadth-first discovery before drafting.".format(
                section=section,
            )
        )
    learnings.append(
        "{section} has {checked} checked dedicated catalog sources, {success} reachable/successful, {used} marked used.".format(
            section=section,
            checked=source_pool["checked_count"],
            success=source_pool["success_count"],
            used=source_pool["used_count"],
        )
    )
    for item in articles[:6]:
        source_ids = item.get("source_audit_source_ids") or []
        source_classes = item.get("source_classes") or []
        learnings.append(
            "{title}: {source_count} sources across {classes}; source-audit ids: {source_ids}.".format(
                title=item.get("title") or item.get("story_id") or "untitled",
                source_count=item.get("source_count", len(source_ids)),
                classes=", ".join(source_classes) or "none",
                source_ids=", ".join(source_ids[:5]) or "none",
            )
        )
    return _dedupe(learnings, limit=10)


def _visited_urls(section: str, articles: list[dict[str, Any]], source_lookup: dict[str, dict[str, Any]], sources: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for item in articles:
        for source_id in item.get("source_audit_source_ids") or []:
            source = source_lookup.get(str(source_id)) or {}
            urls.append(str(source.get("final_url") or source.get("url") or ""))
    for source in _section_sources(sources, section):
        if str(source.get("status") or "") in SUCCESS_STATUSES:
            urls.append(str(source.get("final_url") or source.get("url") or ""))
    return _dedupe(urls, limit=40)


def _follow_up_rows(
    section: str,
    date: str,
    candidate: dict[str, Any],
    source_pool: dict[str, Any],
    breadth: int,
    depth: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    follow_ups: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    next_breadth = max(1, math.ceil(breadth / 2))

    if candidate.get("has_candidate_pool") and section in HARD_NEWS_SECTIONS:
        selected = int(candidate.get("selected_count") or 0)
        target = int(candidate.get("target_count") or 0)
        rejected = int(candidate.get("rejected_count") or 0)
        minimum = int(candidate.get("min_count") or 0)
        if target and selected < target and rejected == 0:
            gap = {
                "rule": "recursive-width-gap",
                "severity": "warning",
                "message": (
                    f"{section} is below target ({selected}/{target}) with zero rejected candidates; "
                    "run a narrower follow-up pass before accepting the shortfall."
                ),
            }
            gaps.append(gap)
            follow_ups.append(
                {
                    "depth": max(1, depth - 1),
                    "breadth": next_breadth,
                    "question": f"What {section} stories were missed after the first pass, and why did they fail selection?",
                    "query": f"{date} JST {SECTION_FALLBACK_TITLES.get(section, section)} missed developments alternatives corroboration",
                    "reason": gap["message"],
                }
            )
        if target and minimum and selected <= minimum and selected < target and rejected == 0:
            follow_ups.append(
                {
                    "depth": max(1, depth - 1),
                    "breadth": next_breadth,
                    "question": f"Can {section} support one more high-score article without filler?",
                    "query": f"{date} JST {SECTION_FALLBACK_TITLES.get(section, section)} high signal candidate source audit",
                    "reason": "The section stopped at the adaptive minimum without a rejected trail.",
                }
            )

    if section in HARD_NEWS_SECTIONS:
        min_section_sources = int(SECTION_RULES.get(section, {}).get("min_section_sources") or 0)
        if min_section_sources and source_pool["success_count"] < min_section_sources:
            gap = {
                "rule": "dedicated-source-pool-thin",
                "severity": "warning",
                "message": (
                    f"{section} has only {source_pool['success_count']} reachable dedicated catalog sources; "
                    f"minimum per article is {min_section_sources}."
                ),
            }
            gaps.append(gap)
            follow_ups.append(
                {
                    "depth": max(1, depth - 1),
                    "breadth": next_breadth,
                    "question": f"Which dedicated {section} catalog sources can corroborate the selected story set?",
                    "query": f"{date} JST {SECTION_FALLBACK_TITLES.get(section, section)} dedicated source corroboration",
                    "reason": gap["message"],
                }
            )

    if not follow_ups and depth > 1:
        follow_ups.append(
            {
                "depth": depth - 1,
                "breadth": next_breadth,
                "question": f"What would change the {section} ranking after the first breadth pass?",
                "query": f"{date} JST {SECTION_FALLBACK_TITLES.get(section, section)} follow up evidence contradiction",
                "reason": "Default recursive narrowing pass for contradiction, source images, and dated events.",
            }
        )
    return follow_ups, gaps


def _section_plan(
    section: str,
    date: str,
    *,
    source_audit: dict[str, Any] | None,
    assembled_manifest: dict[str, Any] | None,
    candidate_pool: dict[str, Any] | None,
) -> dict[str, Any]:
    sources = _sources(source_audit)
    source_lookup = _source_lookup(source_audit)
    articles = _manifest_articles(assembled_manifest, section)
    candidate = _candidate_summary(section, candidate_pool, articles)
    source_pool = _source_pool_summary(section, sources)
    breadth = _section_breadth(section, candidate)
    depth = _section_depth(section)
    follow_ups, gaps = _follow_up_rows(section, date, candidate, source_pool, breadth, depth)
    status = "planned"
    if candidate.get("has_candidate_pool"):
        status = "needs_follow_up" if gaps else "ok"
    return {
        "section": section,
        "section_title": SECTION_FALLBACK_TITLES.get(section, section),
        "status": status,
        "breadth": breadth,
        "depth": depth,
        "recursive_breadth": max(1, math.ceil(breadth / 2)),
        "candidate_pool": candidate,
        "source_pool": source_pool,
        "queries": _seed_queries(section, date, breadth),
        "learnings": _learning_rows(section, articles, candidate, source_pool),
        "visited_urls": _visited_urls(section, articles, source_lookup, sources),
        "follow_up_questions": follow_ups,
        "gaps": gaps,
    }


def build_research_depth_plan(
    date: str,
    *,
    source_audit: dict[str, Any] | None = None,
    assembled_manifest: dict[str, Any] | None = None,
    candidate_pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if source_audit is None and assembled_manifest is None and candidate_pool is None:
        source_audit, assembled_manifest, candidate_pool = _load_default_artifacts(date)
    source_audit = source_audit or {}
    assembled_manifest = assembled_manifest or {}
    candidate_pool = candidate_pool or {}

    sections = {
        section: _section_plan(
            section,
            date,
            source_audit=source_audit,
            assembled_manifest=assembled_manifest,
            candidate_pool=candidate_pool,
        )
        for section in SECTION_RULES
    }
    follow_up_count = sum(len(section["follow_up_questions"]) for section in sections.values())
    gap_count = sum(len(section["gaps"]) for section in sections.values())
    query_count = sum(len(section["queries"]) for section in sections.values())
    sections_needing_follow_up = [
        section for section, plan in sections.items() if plan["status"] == "needs_follow_up"
    ]
    status = "needs_follow_up" if sections_needing_follow_up else ("planned" if not candidate_pool else "ok")
    return {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "generated_at": iso_now(),
        "inspired_by": {
            "project": "dzhng/deep-research",
            "pattern": "breadth/depth query planning, learnings, visited URLs, and recursive follow-up directions",
        },
        "status": status,
        "strategy": {
            "breadth": "per-section discovery width derived from adaptive target_count",
            "depth": "2 for hard-news sections, 1 for feature/update/closing sections",
            "recursion": "follow-up breadth is ceil(previous breadth / 2), matching the deep-research narrowing pattern",
            "runtime_dependency": "none; deterministic artifact generated from source audit, manifests, and candidate pool",
        },
        "summary": {
            "section_count": len(sections),
            "planned_query_count": query_count,
            "follow_up_count": follow_up_count,
            "gap_count": gap_count,
            "sections_needing_follow_up": sections_needing_follow_up,
        },
        "sections": sections,
    }


def build_markdown(plan: dict[str, Any]) -> str:
    lines = [
        f"# Research Depth Plan - {plan.get('date')}",
        "",
        f"- Status: {plan.get('status')}",
        f"- Inspired by: {plan.get('inspired_by', {}).get('project')}",
        f"- Planned queries: {plan.get('summary', {}).get('planned_query_count', 0)}",
        f"- Follow-up prompts: {plan.get('summary', {}).get('follow_up_count', 0)}",
        "",
        "This deterministic artifact adapts breadth/depth research planning, learnings, visited source URLs, and recursive follow-up directions into the briefing pipeline.",
        "",
    ]
    for section, section_plan in plan.get("sections", {}).items():
        lines.extend(
            [
                f"## {section} - {section_plan.get('section_title')}",
                "",
                f"- Status: {section_plan.get('status')}",
                f"- Breadth/depth: {section_plan.get('breadth')}/{section_plan.get('depth')} (recursive breadth {section_plan.get('recursive_breadth')})",
                f"- Candidate screen: {section_plan.get('candidate_pool', {}).get('selected_count', 0)}/{section_plan.get('candidate_pool', {}).get('target_count', 0)} selected, {section_plan.get('candidate_pool', {}).get('rejected_count', 0)} rejected",
                f"- Dedicated source pool: {section_plan.get('source_pool', {}).get('checked_count', 0)} checked, {section_plan.get('source_pool', {}).get('success_count', 0)} successful",
                "",
                "### Queries",
                "",
            ]
        )
        for query in section_plan.get("queries") or []:
            lines.append(f"- D{query.get('depth')} B{query.get('breadth_slot')}: {query.get('query')} - {query.get('research_goal')}")
        lines.extend(["", "### Learnings", ""])
        for learning in section_plan.get("learnings") or []:
            lines.append(f"- {learning}")
        if section_plan.get("follow_up_questions"):
            lines.extend(["", "### Follow-Up Directions", ""])
            for follow_up in section_plan.get("follow_up_questions") or []:
                lines.append(f"- D{follow_up.get('depth')} B{follow_up.get('breadth')}: {follow_up.get('query')} - {follow_up.get('reason')}")
        if section_plan.get("gaps"):
            lines.extend(["", "### Gaps", ""])
            for gap in section_plan.get("gaps") or []:
                lines.append(f"- {gap.get('severity')}: {gap.get('rule')} - {gap.get('message')}")
        if section_plan.get("visited_urls"):
            lines.extend(["", "### Visited URLs", ""])
            for url in section_plan.get("visited_urls")[:12]:
                lines.append(f"- {url}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_research_depth_plan(
    date: str,
    *,
    source_audit: dict[str, Any] | None = None,
    assembled_manifest: dict[str, Any] | None = None,
    candidate_pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.time()
    started_iso = datetime.fromtimestamp(started).astimezone().isoformat()
    paths = research_depth_paths(date)
    plan = build_research_depth_plan(
        date,
        source_audit=source_audit,
        assembled_manifest=assembled_manifest,
        candidate_pool=candidate_pool,
    )
    paths.json_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    paths.markdown_path.write_text(build_markdown(plan), encoding="utf-8")
    summary = {
        "status": plan["status"],
        "source": SOURCE_ATTRIBUTION,
        "artifact_path": str(paths.json_path),
        "markdown_path": str(paths.markdown_path),
        "planned_query_count": plan["summary"]["planned_query_count"],
        "follow_up_count": plan["summary"]["follow_up_count"],
        "gap_count": plan["summary"]["gap_count"],
        "sections_needing_follow_up": plan["summary"]["sections_needing_follow_up"],
    }
    update_run_report(date, research_depth=summary)
    ended = time.time()
    timing_status = "warning" if plan["status"] == "needs_follow_up" else "passed"
    set_step_timing(
        date,
        "research_depth",
        status=timing_status,
        started_at=started_iso,
        ended_at=datetime.fromtimestamp(ended).astimezone().isoformat(),
        duration_seconds=round(ended - started, 3),
        details=summary,
    )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a breadth/depth research plan for a briefing date.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    parser.add_argument("--show", action="store_true", help="Print the JSON plan after writing artifacts.")
    args = parser.parse_args()

    plan = generate_research_depth_plan(args.date)
    paths = research_depth_paths(args.date)
    print(paths.json_path)
    print(
        f"Research depth {plan['status']}: "
        f"{plan['summary']['planned_query_count']} planned queries, "
        f"{plan['summary']['follow_up_count']} follow-up prompts"
    )
    if args.show:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


__all__ = [
    "build_markdown",
    "build_research_depth_plan",
    "generate_research_depth_plan",
    "main",
    "research_depth_paths",
]


if __name__ == "__main__":
    raise SystemExit(main())

