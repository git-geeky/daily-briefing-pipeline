from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


HOME = Path.home()
RUNNERS = {
    "codex": HOME / ".codex" / "skills" / "news-briefing" / "scripts",
    "claude": HOME / ".claude" / "scheduled-tasks" / "daily-intelligence-briefing" / "scripts",
}
SHARED_WRAPPERS = (
    "briefing_shared_loader.py",
    "generate_source_audit.py",
    "assemble_briefing.py",
    "briefing_manifest.py",
    "build_pdf.py",
    "fetch_briefing_images.py",
    "validate_briefing.py",
    "clean_briefing_images.py",
    "smoke_check.py",
)
DEFAULT_DATE = "2026-04-19"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_wrapper_parity() -> None:
    codex_dir = RUNNERS["codex"]
    claude_dir = RUNNERS["claude"]
    mismatches: list[str] = []
    for name in SHARED_WRAPPERS:
        if sha256(codex_dir / name) != sha256(claude_dir / name):
            mismatches.append(name)
    if mismatches:
        joined = ", ".join(mismatches)
        raise RuntimeError(f"Shared wrapper parity check failed: {joined}")


def fixture_titles() -> dict[str, list[str]]:
    return {
        "part1": [
            "Global Supply Chains Tighten as Port Insurance Costs Climb",
            "Central Banks Brace for Commodity-Driven Inflation Pass-Through",
            "Regional Defense Pacts Shift as Maritime Patrols Expand",
            "UN Relief Corridors Reopen Under Tight Military Supervision",
            "Debt Restructuring Talks Harden Across Frontier Markets",
        ],
        "part2": [
            "Frontier Labs Reprice Inference Around Enterprise Security",
            "Focus Company and Cloud Provider Reset the Economics of Frontier Compute",
            "Hacker News Signals a New Wave of Agent Tooling Skepticism",
            "Export Controls Push AI Hardware Buyers Toward Mixed Topologies",
            "Model Evaluation Vendors Face a Reproducibility Reckoning",
            "Open-Weight Chinese Models Pressure Western Enterprise Pricing",
            "Regulators Push for Auditability in High-Risk Model Deployment",
            "Synthetic Data Startups Collide with Real-World Provenance Limits",
        ],
        "part3": [
            "Submarine Cable Repairs Expose the Limits of N+1 Planning",
            "Battery Materials Contracts Shift Toward Longer-Duration Supply",
            "Compute Cooling Vendors Rework Water-Treatment Assumptions",
        ],
        "part4": [
            "Japan Grid Modernization Runs Into Transmission Permitting Reality",
            "Kyushu Port Digitization Reveals a Quiet Labor Bottleneck",
            "Osaka Flood-Control Procurement Moves Into an Oversight Phase",
        ],
        "humaninterest": [
            "Neighborhood Volunteers Rebuild a Community Kitchen Block by Block",
        ],
        "updates": [
            "Update: Frontier Procurement Rules Move from Drafting to Implementation",
        ],
        "closing": [
            "The Week's News Through a Structural Lens",
        ],
    }


def write_valid_fixture(briefing_paths, date: str) -> None:
    titles = fixture_titles()

    fragments = {
        "header": make_header(date, titles),
        "part1": make_section("PART I - GENERAL NEWS", titles["part1"]),
        "part2": make_section("PART II - AI NEWS", titles["part2"], hn_index=2),
        "part3": make_section("PART III - NON-AI TECH NEWS", titles["part3"]),
        "part4": make_section("PART IV - JAPAN NEWS", titles["part4"]),
        "humaninterest": make_section(
            "PART V - HUMAN INTEREST",
            titles["humaninterest"],
            include_watch=False,
            include_counterpoint=False,
            min_sources=4,
            min_words=1200,
        ),
        "updates": make_section(
            "PART VI - UPDATES",
            titles["updates"],
            include_watch=False,
            include_counterpoint=False,
            min_sources=3,
            min_words=900,
        ),
        "closing": make_section(
            "CLOSING ANALYSIS",
            titles["closing"],
            include_watch=False,
            include_counterpoint=False,
            min_sources=4,
            min_words=1200,
            article_label="Analysis",
        ),
    }

    for section, text in fragments.items():
        path = briefing_paths.fragment_path(section, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def build_source_audit_fixture() -> dict[str, object]:
    return {
        "default_status": "checked_no_relevant_update",
        "notes": ["Smoke fixture source audit marks the full catalog checked without live network calls."],
    }


def make_header(date: str, titles: dict[str, list[str]]) -> str:
    executive_summary = paragraph("Executive summary", 680)
    toc_lines = []
    toc_order = (
        ("PART I - GENERAL NEWS", titles["part1"]),
        ("PART II - AI NEWS", titles["part2"]),
        ("PART III - NON-AI TECH NEWS", titles["part3"]),
        ("PART IV - JAPAN NEWS", titles["part4"]),
        ("PART V - HUMAN INTEREST", titles["humaninterest"]),
        ("PART VI - UPDATES", titles["updates"]),
    )
    for label, entries in toc_order:
        toc_lines.append(f"**{label}**")
        for index, title in enumerate(entries, start=1):
            toc_lines.append(f"{index}. {title}")
        toc_lines.append("")
    toc_lines.append("**CLOSING ANALYSIS**")
    toc_lines.append(f"- {titles['closing'][0]}")

    return "\n".join(
        [
            f"# Daily Intelligence Briefing - {date}",
            "",
            "*Saturday, April 19, 2026 (JST)*",
            "",
            "## Executive Summary",
            "",
            executive_summary,
            "",
            "## Top 3 Structural Forces",
            "",
            f'- Structural force one. Cross-reference: Part I - "{titles["part1"][0]}".',
            f'- Structural force two. Cross-reference: Part II - "{titles["part2"][1]}".',
            f'- Structural force three. Cross-reference: Part III - "{titles["part3"][0]}".',
            "",
            "### Table of Contents",
            "",
            *toc_lines,
            "",
        ]
    )


def make_section(
    heading: str,
    titles: list[str],
    *,
    include_watch: bool = True,
    include_counterpoint: bool = True,
    min_sources: int = 5,
    min_words: int = 1600,
    hn_index: int | None = None,
    article_label: str = "Article",
) -> str:
    parts = [f"# {heading}", ""]
    for index, title in enumerate(titles, start=1):
        parts.append(
            make_article(
                index=index,
                title=title,
                include_watch=include_watch,
                include_counterpoint=include_counterpoint,
                min_sources=min_sources,
                min_words=min_words,
                hn_origin=(hn_index is not None and index == hn_index + 1),
                article_label=article_label,
                update_of_prior_story="UPDATE" in heading,
            )
        )
    return "\n\n".join(parts) + "\n"


def make_article(
    *,
    index: int,
    title: str,
    include_watch: bool,
    include_counterpoint: bool,
    min_sources: int,
    min_words: int,
    hn_origin: bool,
    article_label: str,
    update_of_prior_story: bool,
) -> str:
    article_lines = [f"## {article_label} {index}: {title}", ""]
    article_lines.append(paragraph(title, min_words))
    article_lines.append("")

    if include_counterpoint:
        article_lines.extend(
            [
                "### Counterpoint Matrix",
                "",
                "| Claim | Interested Party | Structural Incentive | Tier-4 Counterpoint Source | Net Assessment |",
                "|---|---|---|---|---|",
                "| Claimed acceleration | Vendor consortium | Pricing power | CSIS | Mixed but credible |",
                "| Claimed safety controls | Regulator | Institutional legitimacy | Brookings | Useful but incomplete |",
                "",
            ]
        )

    if include_watch:
        article_lines.extend(
            [
                "> **Watch Next 48h:**",
                "> - April 20, 2026 16:00 JST: Event one on the record",
                "> - April 21, 2026 09:00 JST: Event two on the record",
                "",
            ]
        )

    article_lines.extend(["### Primary Sources", ""])
    source_catalog = [
        ("Reuters", "https://www.reuters.com"),
        ("Associated Press", "https://apnews.com"),
        ("BBC News", "https://www.bbc.com/news"),
        ("Financial Times", "https://www.ft.com"),
        ("Brookings Institution", "https://www.brookings.edu"),
    ]
    if min_sources < 5:
        source_catalog = [
            ("BBC News", "https://www.bbc.com/news"),
            ("Financial Times", "https://www.ft.com"),
            ("Brookings Institution", "https://www.brookings.edu"),
            ("NPR", "https://www.npr.org"),
        ]
    for src_index in range(1, min_sources + 1):
        source_name, source_url = source_catalog[(src_index - 1) % len(source_catalog)]
        if hn_origin and src_index == min_sources:
            source_name = "Hacker News"
            source_url = "https://news.ycombinator.com"
        article_lines.append(
            f"- {source_name}: coverage for {title} - {source_url}/briefing-{index}-{src_index}"
        )
    article_lines.append("")

    if update_of_prior_story:
        article_lines.append("Previous briefing context is used only to mark the update.")
        article_lines.append("")

    return "\n".join(article_lines)


def paragraph(seed: str, min_words: int) -> str:
    words: list[str] = []
    base_words = (
        f"{seed} analysis context structure evidence pressure market policy risk credibility "
        f"coordination logistics enforcement timeline assessment implication"
    ).split()
    while len(words) < min_words:
        words.extend(base_words)
    return " ".join(words[:min_words])


def run_wrapper(wrapper: Path, root: Path, date: str, *extra_args: str) -> str:
    env = os.environ.copy()
    env["DAILY_BRIEFING_ROOT"] = str(root)
    proc = subprocess.run(
        ["py", "-3", str(wrapper), "--date", date, *extra_args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{wrapper.name} failed for {root.name}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def render_sentinel_pages(pdf_path: Path) -> None:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        print("pdftoppm not found; skipping sentinel page render")
        return

    out_prefix = pdf_path.parent / f"{pdf_path.stem}-smoke"
    proc = subprocess.run(
        [pdftoppm, "-f", "1", "-l", "2", "-png", str(pdf_path), str(out_prefix)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pdftoppm failed for {pdf_path}:\n{proc.stdout}\n{proc.stderr}")
    if not any(pdf_path.parent.glob(f"{pdf_path.stem}-smoke-*.png")):
        raise RuntimeError(f"Sentinel render produced no PNGs for {pdf_path}")


def run_runner(name: str, scripts_dir: Path, date: str, workspace: Path) -> dict[str, str]:
    root = workspace / name
    root.mkdir(parents=True, exist_ok=True)

    prior_root = os.environ.get("DAILY_BRIEFING_ROOT")
    try:
        os.environ["DAILY_BRIEFING_ROOT"] = str(root)
        briefing_paths = load_module(scripts_dir / "briefing_paths.py", f"_smoke_{name}_briefing_paths")
        briefing_paths.ensure_layout()
        source_audit_fixture = root / "source-audit-fixture.json"
        source_audit_fixture.write_text(json.dumps(build_source_audit_fixture(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        run_wrapper(scripts_dir / "generate_source_audit.py", root, date, "--fixture", str(source_audit_fixture))
        write_valid_fixture(briefing_paths, date)
        run_wrapper(scripts_dir / "fetch_briefing_images.py", root, date)
        run_wrapper(scripts_dir / "assemble_briefing.py", root, date)
        run_wrapper(scripts_dir / "validate_briefing.py", root, date)
        run_wrapper(scripts_dir / "build_pdf.py", root, date)

        assembled = briefing_paths.assembled_markdown_path(date)
        pdf = briefing_paths.pdf_path(date)
        if not assembled.exists():
            raise RuntimeError(f"Assembled markdown missing for {name}: {assembled}")
        if not pdf.exists():
            raise RuntimeError(f"PDF missing for {name}: {pdf}")
        run_report = json.loads(briefing_paths.run_report_path(date).read_text(encoding="utf-8"))
        manifest = json.loads(briefing_paths.assembled_manifest_path(date).read_text(encoding="utf-8"))
        if not (run_report.get("source_audit") or {}).get("artifact_path"):
            raise RuntimeError(f"Source audit artifact not recorded in run report for {name}")
        if int((manifest.get("summary") or {}).get("source_audit_incorporated_article_count") or 0) < 1:
            raise RuntimeError(f"Assembled manifest did not consume source-audit provenance for {name}")
        render_sentinel_pages(pdf)
        return {
            "source_audit": str(briefing_paths.source_audit_path(date)),
            "assembled": str(assembled),
            "assembled_bytes": str(assembled.stat().st_size),
            "pdf": str(pdf),
            "pdf_bytes": str(pdf.stat().st_size),
        }
    finally:
        if prior_root is None:
            os.environ.pop("DAILY_BRIEFING_ROOT", None)
        else:
            os.environ["DAILY_BRIEFING_ROOT"] = prior_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a dual-run smoke check for the shared Codex/Claude briefing pipeline.")
    parser.add_argument("--date", default=DEFAULT_DATE, help="Smoke-fixture date in YYYY-MM-DD format.")
    parser.add_argument("--keep-temp", action="store_true", help="Preserve the temporary smoke workspace.")
    args = parser.parse_args()

    verify_wrapper_parity()

    with tempfile.TemporaryDirectory(prefix="daily-briefing-smoke-") as tmpdir:
        workspace = Path(tmpdir)
        results = {
            name: run_runner(name, scripts_dir, args.date, workspace)
            for name, scripts_dir in RUNNERS.items()
        }

        print("Smoke check passed")
        for name, result in results.items():
            print(
                f"{name}: source_audit={result['source_audit']}, "
                f"markdown={result['assembled']} ({result['assembled_bytes']} bytes), "
                f"pdf={result['pdf']} ({result['pdf_bytes']} bytes)"
            )

        if args.keep_temp:
            kept = HOME / "Documents" / "Daily-Briefings" / f"smoke-{args.date}"
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(workspace, kept)
            print(f"Preserved smoke workspace at {kept}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

