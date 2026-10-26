from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import briefing_paths as briefing_paths_module


RUN_REPORT_SCHEMA_VERSION = 1
UTC = timezone.utc
STAGE_ORDER = (
    "source_audit",
    "research_vault_sync",
    "research_depth",
    "image_hydration",
    "image_fetch",
    "assembly",
    "research_artifacts",
    "validation",
    "pdf_build",
    "visual_qa",
    "delivery",
)
VALIDATION_OK_STATUSES = {"passed", "warning"}
DELIVERY_OK_STATUSES = {"sent", "sent_with_override"}


def iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _optional_path_attr(name: str, *args) -> Path | None:
    func = getattr(briefing_paths_module, name, None)
    if not callable(func):
        return None
    return Path(func(*args))


def _existing_file(path: Path | None) -> Path | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return path


def file_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_paths_for(date: str) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    candidates = {
        "source_audit": _optional_path_attr("source_audit_path", date),
        "source_audit_markdown": _optional_path_attr("source_audit_markdown_path", date),
        "adaptive_fetch": _optional_path_attr("adaptive_fetch_report_path", date),
        "evidence_digest": _optional_path_attr("evidence_digest_path", date),
        "source_tensions": _optional_path_attr("source_tensions_path", date),
        "research_lint": _optional_path_attr("research_lint_path", date),
        "source_confidence": _optional_path_attr("source_confidence_path", date),
        "research_depth": _optional_path_attr("research_depth_path", date),
        "research_depth_markdown": _optional_path_attr("research_depth_markdown_path", date),
        "research_vault_index": _existing_file(_optional_path_attr("research_vault_index_path")),
        "markdown": _optional_path_attr("assembled_markdown_path", date),
        "assembled_manifest": _optional_path_attr("assembled_manifest_path", date),
        "validation_report": _optional_path_attr("validation_report_path", date),
        "pdf": _optional_path_attr("pdf_path", date),
        "image_report": _existing_file(_optional_path_attr("image_cache_dir", date) / "image-report.json" if _optional_path_attr("image_cache_dir", date) else None),
        "visual_audit": current_visual_audit_path(date),
    }
    for key, path in candidates.items():
        existing = _existing_file(path)
        if existing is not None:
            artifacts[key] = str(existing)
    return artifacts


def artifact_hashes_for(date: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for key, path_text in artifact_paths_for(date).items():
        path = Path(path_text)
        if path.name.endswith(".json") and key == "run_report":
            continue
        hashes[key] = file_sha256(path)
    return hashes


def current_visual_audit_path(date: str) -> Path | None:
    pdf = _existing_file(_optional_path_attr("pdf_path", date))
    if pdf is None:
        return None
    helper = getattr(briefing_paths_module, "visual_audit_path", None)
    if not callable(helper):
        return None
    expected = Path(helper(date, file_sha256(pdf)))
    return expected if expected.exists() else None


def default_run_report(date: str) -> dict[str, Any]:
    runner_name = getattr(briefing_paths_module, "runner_name", lambda: "briefing")()
    return {
        "schema_version": RUN_REPORT_SCHEMA_VERSION,
        "date": date,
        "runner": runner_name,
        "quota_mode": "adaptive",
        "run_attempt_id": None,
        "run_status": "pending",
        "validation_status": "not_run",
        "delivery_status": "not_requested",
        "delivery_blocked_reason": None,
        "delivered_with_override": False,
        "artifact_paths": {},
        "sha256": {},
        "timings": {},
        "error_counts": {
            "blocking": 0,
            "nonblocking": 0,
            "warnings": 0,
        },
        "low_image_articles": [],
        "manifest_summary": {},
        "candidate_rejections": [],
        "source_audit": {},
        "usage": {"calls": [], "totals": {}, "by_stage": {}, "by_model": {}},
        "updated_at": iso_now(),
    }


def run_report_path(date: str) -> Path:
    helper = getattr(briefing_paths_module, "run_report_path", None)
    if not callable(helper):
        raise AttributeError("briefing_paths.run_report_path is required for run reporting")
    path = Path(helper(date))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_run_report(date: str) -> dict[str, Any]:
    path = run_report_path(date)
    if not path.exists():
        return default_run_report(date)
    return json.loads(path.read_text(encoding="utf-8"))


def new_run_attempt_id() -> str:
    return f"attempt-{uuid4().hex[:12]}"


def start_run_attempt(date: str) -> dict[str, Any]:
    prior = load_run_report(date)
    report = default_run_report(date)
    report["run_attempt_id"] = new_run_attempt_id()
    report["run_status"] = "running"
    report["attempt_started_at"] = iso_now()
    if prior.get("scheduled_log_path"):
        report["scheduled_log_path"] = prior["scheduled_log_path"]
    if prior.get("notes"):
        report["notes"] = list(prior["notes"])
    write_run_report(report)
    return report


def ensure_run_attempt(date: str) -> dict[str, Any]:
    report = load_run_report(date)
    if report.get("run_status") == "running" and report.get("run_attempt_id"):
        return report
    return start_run_attempt(date)


def _merge_dict(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def stage_order_index(step: str) -> int:
    try:
        return STAGE_ORDER.index(step)
    except ValueError:
        return len(STAGE_ORDER)


def stage_prerequisites_satisfied(report: dict[str, Any], step: str) -> tuple[bool, str | None]:
    if step != "delivery":
        return True, None

    validation_status = report.get("validation_status")
    if validation_status not in VALIDATION_OK_STATUSES:
        return False, f"Delivery blocked until validation passes; current validation_status={validation_status}"

    timings = report.get("timings") or {}
    pdf_step = timings.get("pdf_build") or {}
    if pdf_step.get("status") not in {"passed", "bypassed"}:
        return False, "Delivery blocked until pdf_build completes"

    visual_step = timings.get("visual_qa") or {}
    if visual_step.get("status") not in {"passed", "warning", "bypassed"}:
        return False, "Delivery blocked until visual_qa completes"

    source_audit = report.get("source_audit") or {}
    if source_audit.get("status") not in {"complete", "fixture", "degraded"}:
        return False, f"Delivery blocked until source_audit completes; current status={source_audit.get('status')}"
    if not source_audit.get("all_sources_checked"):
        return False, "Delivery blocked because source_audit did not check the full source catalog"
    if source_audit.get("systemic_failure"):
        return False, "Delivery blocked because source_audit recorded systemic source-check failure"

    return True, None


def write_run_report(report: dict[str, Any]) -> Path:
    date = report["date"]
    path = run_report_path(date)
    report["artifact_paths"] = _merge_dict(report.get("artifact_paths", {}), artifact_paths_for(date))
    report["artifact_paths"]["run_report"] = str(path)
    report["sha256"] = _merge_dict(report.get("sha256", {}), artifact_hashes_for(date))
    report["updated_at"] = iso_now()
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report["sha256"]["run_report"] = file_sha256(path)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    refresh_health_dashboard()
    return path


def update_run_report(
    date: str,
    *,
    run_status: str | None = None,
    validation_status: str | None = None,
    delivery_status: str | None = None,
    delivery_blocked_reason: str | None = None,
    delivered_with_override: bool | None = None,
    error_counts: dict[str, int] | None = None,
    low_image_articles: list[dict[str, Any]] | None = None,
    manifest_summary: dict[str, Any] | None = None,
    candidate_rejections: list[dict[str, Any]] | None = None,
    source_audit: dict[str, Any] | None = None,
    timings: dict[str, Any] | None = None,
    notes: list[str] | None = None,
    remove_note_substrings: list[str] | None = None,
    scheduled_log_path: str | None = None,
    quota_mode: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    report = load_run_report(date)
    force_delivery_state = bool(extra.pop("force_delivery_state", False))
    if quota_mode is not None:
        report["quota_mode"] = quota_mode
    if run_status is not None:
        report["run_status"] = run_status
    if validation_status is not None:
        report["validation_status"] = validation_status
    if delivery_status is not None:
        if delivery_status in DELIVERY_OK_STATUSES:
            allowed, reason = stage_prerequisites_satisfied(report, "delivery")
            if not allowed and not force_delivery_state:
                report["delivery_status"] = "blocked"
                report["delivery_blocked_reason"] = reason
                write_run_report(report)
                return report
        report["delivery_status"] = delivery_status
        if delivery_status in DELIVERY_OK_STATUSES:
            report["delivery_blocked_reason"] = None
    if delivery_blocked_reason is not None:
        report["delivery_blocked_reason"] = delivery_blocked_reason
    if delivered_with_override is not None:
        report["delivered_with_override"] = bool(delivered_with_override)
    if error_counts is not None:
        report["error_counts"] = _merge_dict(report.get("error_counts", {}), error_counts)
    if low_image_articles is not None:
        report["low_image_articles"] = low_image_articles
    if manifest_summary is not None:
        report["manifest_summary"] = manifest_summary
    if candidate_rejections is not None:
        report["candidate_rejections"] = candidate_rejections
    if source_audit is not None:
        report["source_audit"] = _merge_dict(report.get("source_audit", {}), source_audit)
    if timings:
        report["timings"] = _merge_dict(report.get("timings", {}), timings)
    if remove_note_substrings:
        existing_notes = list(report.get("notes", []))
        report["notes"] = [
            note
            for note in existing_notes
            if not any(token in note for token in remove_note_substrings)
        ]
    if notes:
        existing_notes = list(report.get("notes", []))
        existing_notes.extend(note for note in notes if note not in existing_notes)
        report["notes"] = existing_notes
    if scheduled_log_path:
        report["scheduled_log_path"] = scheduled_log_path
    if extra:
        report = _merge_dict(report, extra)
    write_run_report(report)
    return report


def set_step_timing(
    date: str,
    step: str,
    *,
    status: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    duration_seconds: float | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        step: {
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": duration_seconds,
        }
    }
    if details:
        payload[step]["details"] = details
    return update_run_report(date, timings=payload)


USAGE_CALL_LIMIT = 400
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd", "duration_seconds")


def _blank_usage() -> dict[str, float]:
    return {"calls": 0, **{field: 0 for field in _USAGE_FIELDS}}


def _recompute_usage(usage: dict[str, Any]) -> dict[str, Any]:
    totals, by_stage, by_model = _blank_usage(), {}, {}
    for call in usage.get("calls", []):
        for bucket in (totals, by_stage.setdefault(call.get("stage") or "?", _blank_usage()),
                       by_model.setdefault(call.get("model") or "?", _blank_usage())):
            bucket["calls"] += 1
            for field in _USAGE_FIELDS:
                bucket[field] += float(call.get(field) or 0)
    for bucket in (totals, *by_stage.values(), *by_model.values()):
        bucket["cost_usd"] = round(bucket["cost_usd"], 4)
        bucket["duration_seconds"] = round(bucket["duration_seconds"], 1)
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            bucket[field] = int(bucket[field])
    usage["totals"], usage["by_stage"], usage["by_model"] = totals, by_stage, by_model
    return usage


def record_usage(
    date: str,
    stage: str,
    *,
    model: str | None = None,
    backend: str = "claude",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_usd: float = 0.0,
    duration_seconds: float = 0.0,
    ok: bool = True,
    error: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Append one model call to the run report and refresh the usage rollups.

    The scheduled runs that died in 2026 never recorded what they spent, so nobody could see
    which stage drained the allowance. Every conductor call goes through here.
    """
    report = load_run_report(date)
    usage = report.get("usage")
    if not isinstance(usage, dict):
        usage = {"calls": []}
    calls = list(usage.get("calls") or [])
    calls.append(
        {
            "at": iso_now(),
            "stage": stage,
            "label": label,
            "model": model,
            "backend": backend,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read_tokens or 0),
            "cache_creation_tokens": int(cache_creation_tokens or 0),
            "cost_usd": round(float(cost_usd or 0.0), 4),
            "duration_seconds": round(float(duration_seconds or 0.0), 1),
            "ok": bool(ok),
            "error": (error or None) and str(error)[:300],
        }
    )
    usage["calls"] = calls[-USAGE_CALL_LIMIT:]
    report["usage"] = _recompute_usage(usage)
    write_run_report(report)
    return report["usage"]


def _run_report_dir() -> Path:
    return run_report_path("1970-01-01").parent


def scan_run_reports() -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(_run_report_dir().glob("*-run.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        data["_path"] = str(path)
        data["_mtime"] = path.stat().st_mtime
        reports.append(data)
    reports.sort(key=lambda item: item.get("_mtime", 0), reverse=True)
    return reports


def detect_regressions(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Surface recent degradations worth alerting on.

    Today this catches final-image contract failures and any validation-error
    spike that wouldn't otherwise be visible in `latest_successful_run`.
    """
    regressions: list[dict[str, Any]] = []
    recent = reports[:5]
    for report in recent:
        summary = report.get("manifest_summary") or {}
        total = summary.get("total_articles") or 0
        total_inline_images = int(summary.get("total_inline_images") or 0)
        total_high_confidence_images = int(summary.get("total_high_confidence_images") or 0)
        if total >= 5 and (total_inline_images > 1 or (total_inline_images == 1 and total_high_confidence_images != 1)):
            regressions.append(
                {
                    "date": report.get("date"),
                    "runner": report.get("runner"),
                    "kind": "image_contract_failed",
                    "total_articles": total,
                    "total_inline_images": total_inline_images,
                    "total_high_confidence_images": total_high_confidence_images,
                    "detail": (
                        f"Briefing has {total_inline_images} inline image(s) and "
                        f"{total_high_confidence_images} validated high-confidence image(s); "
                        "the final contract is exactly one validated image total."
                    ),
                }
            )
        error_counts = report.get("error_counts") or {}
        blocking = error_counts.get("blocking", 0) or 0
        if blocking >= 5:
            regressions.append(
                {
                    "date": report.get("date"),
                    "runner": report.get("runner"),
                    "kind": "validation_error_spike",
                    "blocking_errors": blocking,
                    "detail": (
                        f"{blocking} blocking validation errors — "
                        "likely indicates fragment-format drift or a prompt regression."
                    ),
                }
            )
        source_audit = report.get("source_audit") or {}
        if source_audit and not source_audit.get("all_sources_checked"):
            regressions.append(
                {
                    "date": report.get("date"),
                    "runner": report.get("runner"),
                    "kind": "source_audit_incomplete",
                    "detail": (
                        "Source audit did not check the full catalog; "
                        "daily source-floor enforcement is incomplete."
                    ),
                }
            )
        if source_audit.get("systemic_failure"):
            regressions.append(
                {
                    "date": report.get("date"),
                    "runner": report.get("runner"),
                    "kind": "source_audit_systemic_failure",
                    "failed_after_retry_count": source_audit.get("failed_after_retry_count"),
                    "detail": "Source audit recorded systemic source-check failure.",
                }
            )
    return regressions


def refresh_health_dashboard() -> Path | None:
    helper = getattr(briefing_paths_module, "health_dashboard_path", None)
    if not callable(helper):
        return None

    reports = scan_run_reports()
    latest_attempt = reports[0] if reports else None
    latest_valid_run = next(
        (
            r
            for r in reports
            if r.get("validation_status") in VALIDATION_OK_STATUSES
            and (r.get("timings") or {}).get("pdf_build", {}).get("status") in {"passed", "bypassed"}
        ),
        None,
    )
    latest_degraded = next(
        (
            r
            for r in reports
            if r.get("run_status") in {"degraded", "failed"}
            or r.get("validation_status") in {"failed", "bypassed"}
            or r.get("delivered_with_override")
        ),
        None,
    )
    latest_delivery = next(
        (r for r in reports if r.get("delivery_status") in DELIVERY_OK_STATUSES),
        None,
    )

    payload = {
        "runner": getattr(briefing_paths_module, "runner_name", lambda: "briefing")(),
        "updated_at": iso_now(),
        "latest_attempt": summarize_run(latest_attempt),
        "latest_valid_run": summarize_run(latest_valid_run),
        "latest_delivery": summarize_run(latest_delivery),
        "latest_successful_run": summarize_run(latest_valid_run),
        "latest_degraded_run": summarize_run(latest_degraded),
        "regressions": detect_regressions(reports),
    }
    path = Path(helper())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def summarize_run(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    keys = (
        "date",
        "runner",
        "run_attempt_id",
        "quota_mode",
        "run_status",
        "validation_status",
        "delivery_status",
        "delivery_blocked_reason",
        "delivered_with_override",
        "error_counts",
        "artifact_paths",
        "source_audit",
        "updated_at",
    )
    return {key: report.get(key) for key in keys}


def latest_run_report() -> dict[str, Any] | None:
    reports = scan_run_reports()
    return reports[0] if reports else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or initialize daily briefing run reports.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    parser.add_argument("--show", action="store_true", help="Print the run report JSON and exit.")
    args = parser.parse_args()

    report = load_run_report(args.date)
    write_run_report(report)
    if args.show:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(run_report_path(args.date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
