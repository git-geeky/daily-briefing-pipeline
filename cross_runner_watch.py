"""Cross-runner completion watcher + fleet-diff email.

Runs once per day at 18:30 JST (scheduled via \\ClaudeCode\\Claude-BriefingFleetDiff).
By that time both the Claude runner (16:00 JST, ~60 min) and Codex runner (16:00 JST,
~20 min) should have completed.

The watcher:
1. Checks `run_status` in both runners' per-date run reports.
2. If both completed for today, invokes `runner_comparison_core.comparison_report()`
   to produce an article-level diff (overlap, unique-to-each, image-count delta).
3. Builds a plaintext markdown summary.
4. Emails the summary via the shared `deliver_core.py` on aiserver.

If either runner hasn't completed, the watcher writes a degraded status to
`fleet-health.json` but does NOT send a diff email — the diff is only
meaningful when both runs produced output.

Usage:
    python3 cross_runner_watch.py --date 2026-04-22
    python3 cross_runner_watch.py --date 2026-04-22 --no-email  # dry-run
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
HOME = Path.home()

# Both runners' briefing_paths.py files provide `run_report_path(date)` and
# `assembled_manifest_path(date)` — we import them dynamically rather than
# sys.path-juggling because each runner's `briefing_paths.py` depends on its
# own `runner_contracts.py` peer file.
RUNNER_PATHS = {
    "claude": HOME / ".claude" / "scheduled-tasks" / "daily-intelligence-briefing" / "scripts" / "briefing_paths.py",
    "codex": HOME / ".codex" / "skills" / "news-briefing" / "scripts" / "briefing_paths.py",
}

FLEET_HEALTH_PATH = HOME / "Documents" / "Daily-Briefings" / "generated" / "fleet-health.json"


def _load_module(path: Path, name: str):
    # The runner's briefing_paths.py imports siblings like briefing_shared_loader
    # via bare-name imports, so we need its parent directory on sys.path.
    parent = str(path.parent)
    added = False
    if parent not in sys.path:
        sys.path.insert(0, parent)
        added = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        # Leave sys.path alone — the shared_loader may cache per-runner state
        # that assumes its own directory stays importable within the process.
        if added and False:  # noqa: intentionally leave on path
            sys.path.remove(parent)


def load_run_report(runner: str, date: str) -> dict[str, Any] | None:
    paths_mod = _load_module(RUNNER_PATHS[runner], f"_watch_{runner}_paths")
    report_path = paths_mod.run_report_path(date)
    if not report_path.exists():
        return None
    return json.loads(report_path.read_text(encoding="utf-8"))


def both_runners_completed(date: str) -> tuple[bool, dict[str, dict[str, Any] | None]]:
    reports = {runner: load_run_report(runner, date) for runner in RUNNER_PATHS}
    all_done = all(r is not None and r.get("run_status") == "completed" for r in reports.values())
    return all_done, reports


def comparison_report(date: str) -> dict[str, Any]:
    """Shell out to `runner_comparison_core.py` to reuse the existing diff logic.

    We could import and call directly, but shelling out means a single source
    of truth for the comparison output format.
    """
    rc_path = HERE / "runner_comparison_core.py"
    proc = subprocess.run(
        [sys.executable, str(rc_path), "--date", date],
        capture_output=True,
        text=True,
        check=True,
    )
    output_path = Path(proc.stdout.strip())
    return json.loads(output_path.read_text(encoding="utf-8"))


def render_summary(date: str, diff: dict[str, Any], reports: dict[str, dict[str, Any]]) -> str:
    runners = diff.get("runners", {})
    claude = runners.get("claude", {})
    codex = runners.get("codex", {})
    overlap = diff.get("title_overlap", [])
    claude_only = diff.get("claude_only_titles", [])
    codex_only = diff.get("codex_only_titles", [])
    image_delta = diff.get("high_confidence_image_delta", 0)
    section_delta = diff.get("section_count_delta", {})
    quality_delta = diff.get("quality_delta", {})
    shared_gaps = diff.get("shared_gaps", [])

    lines = [
        f"# Daily Briefing Fleet Diff — {date}",
        "",
        f"Both runners completed for {date}.",
        "",
        "## Summary",
        "",
        f"- **Claude articles**: {claude.get('article_count', 0)}",
        f"- **Codex articles**: {codex.get('article_count', 0)}",
        f"- **Overlapping stories**: {len(overlap)}",
        f"- **Claude-only stories**: {len(claude_only)}",
        f"- **Codex-only stories**: {len(codex_only)}",
        f"- **High-confidence image delta (codex - claude)**: {image_delta:+d}",
        f"- **Warning delta (codex - claude)**: {diff.get('warning_delta', 0):+d}",
        f"- **Nonblocking-error delta (codex - claude)**: {diff.get('nonblocking_error_delta', 0):+d}",
        "",
        "## Status",
        "",
        f"- **Claude**: run_status={claude.get('run_status')}, validation_status={claude.get('validation_status')}, delivery_status={claude.get('delivery_status')}",
        f"- **Codex**:  run_status={codex.get('run_status')}, validation_status={codex.get('validation_status')}, delivery_status={codex.get('delivery_status')}",
        "",
        "## Quality Signals",
        "",
        f"- **Claude**: focus-company={claude.get('focus_company_article_count', 0)}, hn_origin={claude.get('hn_origin_count', 0)}, low_image={claude.get('low_image_article_count', 0)}, avg_sources={claude.get('avg_source_count', 0):.2f}, avg_watch_next={claude.get('avg_watch_next_count', 0):.2f}, avg_score={claude.get('avg_score', 0):.2f}",
        f"- **Codex**:  focus-company={codex.get('focus_company_article_count', 0)}, hn_origin={codex.get('hn_origin_count', 0)}, low_image={codex.get('low_image_article_count', 0)}, avg_sources={codex.get('avg_source_count', 0):.2f}, avg_watch_next={codex.get('avg_watch_next_count', 0):.2f}, avg_score={codex.get('avg_score', 0):.2f}",
        f"- **Delta (codex - claude)**: focus-company={quality_delta.get('focus_company_article_count', 0):+d}, hn_origin={quality_delta.get('hn_origin_count', 0):+d}, low_image={quality_delta.get('low_image_article_count', 0):+d}, avg_sources={quality_delta.get('avg_source_count', 0):+.2f}, avg_watch_next={quality_delta.get('avg_watch_next_count', 0):+.2f}, avg_score={quality_delta.get('avg_score', 0):+.2f}",
        "",
    ]

    if section_delta:
        lines.append("## Section Count Delta (codex - claude)")
        lines.append("")
        for section, delta in section_delta.items():
            lines.append(f"- **{section}**: {delta:+d}")
        lines.append("")

    if shared_gaps:
        lines.append("## Shared Gaps")
        lines.append("")
        for gap in shared_gaps:
            lines.append(f"- {gap}")
        lines.append("")

    for runner_name, state in (("Claude", claude), ("Codex", codex)):
        minimum_sections = state.get("minimum_without_rejections_sections") or []
        below_target_sections = state.get("below_target_without_rejections_sections") or []
        if minimum_sections or below_target_sections:
            lines.append(f"## Discovery Depth Alert — {runner_name}")
            lines.append("")
            if minimum_sections:
                lines.append(f"- Stopped at adaptive minimum with zero rejected candidates: {', '.join(minimum_sections)}")
            if below_target_sections:
                lines.append(f"- Below target with zero rejected candidates: {', '.join(below_target_sections)}")
            lines.append("")

    if overlap:
        lines.append("## Overlapping Stories (corroborated by both runners)")
        lines.append("")
        for title in overlap[:20]:
            lines.append(f"- {title}")
        if len(overlap) > 20:
            lines.append(f"- ... and {len(overlap) - 20} more")
        lines.append("")

    if claude_only:
        lines.append("## Claude-only Stories")
        lines.append("")
        for title in claude_only[:10]:
            lines.append(f"- {title}")
        if len(claude_only) > 10:
            lines.append(f"- ... and {len(claude_only) - 10} more")
        lines.append("")

    if codex_only:
        lines.append("## Codex-only Stories")
        lines.append("")
        for title in codex_only[:10]:
            lines.append(f"- {title}")
        if len(codex_only) > 10:
            lines.append(f"- ... and {len(codex_only) - 10} more")
        lines.append("")

    # Low-image articles — surface regressions
    for runner_name, runner_report in reports.items():
        if not runner_report:
            continue
        low_img = runner_report.get("low_image_articles") or []
        if len(low_img) >= 5:
            lines.append(f"## Regression: {runner_name} has {len(low_img)} low-image articles")
            lines.append("")
            for item in low_img[:5]:
                lines.append(f"- {item.get('section')}: {item.get('title')}")
            if len(low_img) > 5:
                lines.append(f"- ... and {len(low_img) - 5} more")
            lines.append("")

    return "\n".join(lines)


def write_fleet_health(date: str, reports: dict[str, dict[str, Any]], diff: dict[str, Any] | None) -> Path:
    """Aggregated cross-runner dashboard (Phase D3)."""
    summary = {
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "date": date,
        "runners": {
            runner: {
                "run_status": (rep or {}).get("run_status"),
                "validation_status": (rep or {}).get("validation_status"),
                "delivery_status": (rep or {}).get("delivery_status"),
                "error_counts": (rep or {}).get("error_counts"),
                "article_count": ((rep or {}).get("manifest_summary") or {}).get("total_articles"),
                "low_image_article_count": ((rep or {}).get("manifest_summary") or {}).get("low_image_article_count"),
                "artifact_paths": (rep or {}).get("artifact_paths"),
            }
            for runner, rep in reports.items()
        },
        "cross_runner_diff": {
            "title_overlap": (diff or {}).get("title_overlap", []),
            "claude_only": (diff or {}).get("claude_only_titles", []),
            "codex_only": (diff or {}).get("codex_only_titles", []),
            "article_count_delta": (diff or {}).get("article_count_delta"),
            "high_confidence_image_delta": (diff or {}).get("high_confidence_image_delta"),
            "warning_delta": (diff or {}).get("warning_delta"),
            "nonblocking_error_delta": (diff or {}).get("nonblocking_error_delta"),
            "section_count_delta": (diff or {}).get("section_count_delta"),
            "quality_delta": (diff or {}).get("quality_delta"),
            "shared_gaps": (diff or {}).get("shared_gaps"),
            "delivery_gate": (diff or {}).get("delivery_gate"),
        } if diff else None,
    }
    FLEET_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    FLEET_HEALTH_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return FLEET_HEALTH_PATH


def send_diff_email(date: str, body: str) -> bool:
    """Deliver the summary via aiserver's deliver_core module using Invoke-RemoteBash."""
    subject = f"Daily Briefing Fleet Diff - {date}"
    body_b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
    remote_script = "\n".join(
        [
            "set -euo pipefail",
            f"BODY_B64={shlex.quote(body_b64)}",
            "printf '%s' \"$BODY_B64\" | base64 -d | "
            f"python3 /home/agent/briefing-pipeline/deliver_core.py --runner diff --body-from-stdin --subject {shlex.quote(subject)}",
        ]
    )
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
        handle.write(
            "\n".join(
                [
                    "$ErrorActionPreference = 'Stop'",
                    "if (-not (Get-Command Invoke-RemoteBash -ErrorAction SilentlyContinue)) {",
                    "    $toolsPath = Join-Path $HOME 'bin\\codex-ssh-tools.ps1'",
                    "    if (Test-Path -LiteralPath $toolsPath) { . $toolsPath }",
                    "}",
                    "if (-not (Get-Command Invoke-RemoteBash -ErrorAction SilentlyContinue)) {",
                    "    throw 'Invoke-RemoteBash is not available.'",
                    "}",
                    "$script = @'",
                    remote_script,
                    "'@",
                    "$result = Invoke-RemoteBash -Target aiserver -RunAsUser agent -Script $script 2>&1 | Out-String",
                    "Write-Output $result",
                ]
            )
        )
        temp_path = handle.name
    proc = subprocess.run(["pwsh", "-NoProfile", "-File", temp_path], text=True, capture_output=True, check=False)
    try:
        os.unlink(temp_path)
    except OSError:
        pass
    combined_output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        sys.stderr.write(f"Fleet-diff email failed (exit {proc.returncode}):\n{combined_output}\n")
        return False
    if "Email sent" not in combined_output:
        sys.stderr.write(f"Fleet-diff email did not confirm success:\n{combined_output}\n")
        return False
    return True


def today_jst() -> str:
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%d")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None, help="Briefing date in YYYY-MM-DD (default: today JST).")
    parser.add_argument("--no-email", action="store_true", help="Write fleet-health.json but do not send the email.")
    args = parser.parse_args()

    date = args.date or today_jst()

    all_done, reports = both_runners_completed(date)

    if not all_done:
        status = {runner: (r or {}).get("run_status", "MISSING") for runner, r in reports.items()}
        sys.stderr.write(
            f"Both runners have NOT completed for {date}. Current status: {status}. "
            f"Skipping fleet-diff email.\n"
        )
        write_fleet_health(date, reports, diff=None)
        return 0

    diff = comparison_report(date)
    write_fleet_health(date, reports, diff)
    body = render_summary(date, diff, reports)
    gate = diff.get("delivery_gate") or {}

    if gate.get("status") == "failed":
        sys.stderr.write(
            f"Runner comparison gate failed for {date}; keeping the diff as a QA artifact only. "
            f"Failures: {gate.get('failures', [])}\n"
        )
        if args.no_email:
            sys.stdout.write(body)
        return 0

    consensus_ok = True
    if os.environ.get("BRIEFING_SEND_CONSENSUS") == "1" and not args.no_email:
        consensus_ok = build_and_send_consensus(date)

    if args.no_email:
        sys.stdout.write(body)
        return 0

    ok = send_diff_email(date, body)
    return 0 if (ok and consensus_ok) else 1


def build_and_send_consensus(date: str) -> bool:
    """Build the consensus briefing via merge_briefings_core, but do not email it.

    Consensus is retained as a QA/comparison artifact, not a shipping target.
    Gated by `BRIEFING_SEND_CONSENSUS=1` only for the build step.
    """
    merge_path = HERE / "merge_briefings_core.py"
    proc = subprocess.run(
        [sys.executable, str(merge_path), "--date", date],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"Consensus merge failed:\n{proc.stdout}\n{proc.stderr}\n")
        return False

    consensus_md = HOME / "Documents" / "Daily-Briefings" / "generated" / f"consensus-briefing-{date}.md"
    if not consensus_md.exists():
        sys.stderr.write(f"Consensus markdown missing after merge: {consensus_md}\n")
        return False

    sys.stderr.write(
        f"Consensus artifact built at {consensus_md} but not delivered; policy keeps consensus diagnostic-only.\n"
    )
    return True


if __name__ == "__main__":
    raise SystemExit(main())

