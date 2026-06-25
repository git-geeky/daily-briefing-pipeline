from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_reporting import latest_run_report, summarize_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Print the latest local briefing run summary.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a compact text summary.")
    args = parser.parse_args()

    report = latest_run_report()
    if report is None:
        print("No run reports found.")
        return 0

    summary = summarize_run(report) or {}
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    print(f"runner: {summary.get('runner')}")
    print(f"date: {summary.get('date')}")
    print(f"run_attempt_id: {summary.get('run_attempt_id')}")
    print(f"run_status: {summary.get('run_status')}")
    print(f"validation_status: {summary.get('validation_status')}")
    print(f"delivery_status: {summary.get('delivery_status')}")
    if summary.get("delivery_blocked_reason"):
        print(f"delivery_blocked_reason: {summary.get('delivery_blocked_reason')}")
    print(f"delivered_with_override: {summary.get('delivered_with_override')}")
    if summary.get("delivery_status") in {"sent", "sent_with_override"} and summary.get("validation_status") in {"failed", "bypassed"}:
        print("warning: delivery succeeded while validation was failed or bypassed")
    if summary.get("artifact_paths"):
        for key, path in summary["artifact_paths"].items():
            print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
