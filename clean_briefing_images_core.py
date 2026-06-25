from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from briefing_manifest import LOW_CONFIDENCE_IMAGE_THRESHOLD as LOW_CONFIDENCE_THRESHOLD
from briefing_paths import fragment_path, image_cache_dir


EMPTY_FALLBACK_PHASE_PREFIX = "phase3_added_to_empty_"
LOW_CONFIDENCE_PHASE_PREFIX = "phase3_"
# LOW_CONFIDENCE_THRESHOLD imported from briefing_manifest (canonical image threshold).


def _report_path(date: str) -> Path:
    return image_cache_dir(date) / "image-report.json"


def reported_empty_fallbacks(date: str) -> dict[str, set[str]]:
    report_path = _report_path(date)
    if not report_path.exists():
        return {}

    data = json.loads(report_path.read_text(encoding="utf-8"))
    grouped: dict[str, set[str]] = defaultdict(set)
    for detail in data.get("details", []):
        section = detail.get("section")
        phase = detail.get("phase", "")
        local_path = detail.get("local_path")
        if not section or not local_path:
            continue
        if not str(phase).startswith(EMPTY_FALLBACK_PHASE_PREFIX):
            continue
        grouped[section].add(str(local_path))
    return grouped


def remove_reported_empty_fallback_images(date: str) -> int:
    grouped = reported_empty_fallbacks(date)
    return _remove_reported_images(date, grouped)


def reported_low_confidence_phase3(date: str) -> dict[str, set[str]]:
    report_path = _report_path(date)
    if not report_path.exists():
        return {}

    data = json.loads(report_path.read_text(encoding="utf-8"))
    grouped: dict[str, set[str]] = defaultdict(set)
    for detail in data.get("details", []):
        section = detail.get("section")
        phase = str(detail.get("phase", ""))
        local_path = detail.get("local_path")
        confidence = float(detail.get("confidence") or 0.0)
        if not section or not local_path:
            continue
        if not phase.startswith(LOW_CONFIDENCE_PHASE_PREFIX):
            continue
        if confidence >= LOW_CONFIDENCE_THRESHOLD:
            continue
        grouped[section].add(str(local_path))
    return grouped


def remove_low_confidence_phase3_images(date: str) -> int:
    grouped = reported_low_confidence_phase3(date)
    return _remove_reported_images(date, grouped)


def _remove_reported_images(date: str, grouped: dict[str, set[str]]) -> int:
    removed = 0

    for section, local_paths in grouped.items():
        frag_path = fragment_path(section, date)
        if not frag_path.exists():
            continue

        lines = frag_path.read_text(encoding="utf-8").splitlines()
        kept_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            matched = False
            for local_path in local_paths:
                pattern = rf'!\[[^\]]*]\({re.escape(local_path)}\)'
                if re.fullmatch(pattern, stripped):
                    removed += 1
                    matched = True
                    break
            if not matched:
                kept_lines.append(line)

        new_text = re.sub(r'\n{3,}', '\n\n', "\n".join(kept_lines)).rstrip() + "\n"
        frag_path.write_text(new_text, encoding="utf-8")

    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove report-identified empty-article fallback images from fragments.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    parser.add_argument(
        "--remove-low-confidence-phase3",
        action="store_true",
        help="Also remove report-identified low-confidence Phase 3 recovered images.",
    )
    args = parser.parse_args()

    removed = remove_reported_empty_fallback_images(args.date)
    if args.remove_low_confidence_phase3:
        removed += remove_low_confidence_phase3_images(args.date)
    print(f"Removed {removed} empty-article fallback image tags for {args.date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
