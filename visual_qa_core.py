from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import briefing_paths as briefing_paths_module
import edition_core
from run_reporting import file_sha256, set_step_timing, update_run_report


def _poppler_tool(name: str) -> str | None:
    """Prefer scoop's Poppler binary over PATH (Git Bash mingw64 ships Xpdf 4.00 that shadows it)."""
    scoop_root = Path(os.environ.get("SCOOP", Path.home() / "scoop"))
    scoop_shim = scoop_root / "shims" / f"{name}.exe"
    if scoop_shim.exists():
        return str(scoop_shim)
    return shutil.which(name)


MAX_RENDER_PAGES = 10


def page_count(pdf_path: Path) -> int | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return None


def sparse_pages(pdf_path: Path) -> list[int]:
    pdftotext = _poppler_tool("pdftotext")
    if not pdftotext:
        return []
    out_prefix = pdf_path.with_suffix("")
    proc = subprocess.run(
        [pdftotext, str(pdf_path), "-"],
        capture_output=True,
        check=False,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    if proc.returncode != 0 or not stdout:
        return []
    pages = [page.strip() for page in stdout.split("\f")]
    return [index + 1 for index, page in enumerate(pages) if page and len(page) < 220]


def pdf_pages_text(pdf_path: Path) -> list[str]:
    pdftotext = _poppler_tool("pdftotext")
    if not pdftotext:
        return []
    proc = subprocess.run(
        [pdftotext, "-layout", str(pdf_path), "-"],
        capture_output=True,
        check=False,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    if proc.returncode != 0 or not stdout:
        return []
    return stdout.split("\f")


def image_caption_pages(pdf_path: Path) -> list[int]:
    pages = pdf_pages_text(pdf_path)
    matched: list[int] = []
    for index, text in enumerate(pages, start=1):
        lower = text.lower()
        if "(source:" in lower or "authored svg" in lower or "codex authored" in lower:
            matched.append(index)
    return matched


def render_pages(pdf_path: Path, pages: list[int]) -> list[str]:
    pdftoppm = _poppler_tool("pdftoppm")
    if not pdftoppm:
        return []
    rendered: list[str] = []
    for page in pages[:MAX_RENDER_PAGES]:
        out_prefix = pdf_path.parent / f"{pdf_path.stem}-qa-p{page:03d}"
        proc = subprocess.run(
            [pdftoppm, "-f", str(page), "-l", str(page), "-png", str(pdf_path), str(out_prefix)],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            rendered.extend(str(path) for path in sorted(pdf_path.parent.glob(f"{out_prefix.name}-*.png")))
    return rendered


def render_sentinels(pdf_path: Path) -> list[str]:
    pages: list[int] = [1, 2, 3]
    for page in image_caption_pages(pdf_path):
        if page not in pages:
            pages.append(page)
    return render_pages(pdf_path, pages)


def build_visual_audit(date: str) -> dict:
    pdf_path = Path(briefing_paths_module.pdf_path(date))
    manifest_path = Path(briefing_paths_module.assembled_manifest_path(date))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    summary = manifest.get("summary") or {}
    scorecards = summary.get("article_scorecards") or []
    total_inline_images = int(summary.get("total_inline_images") or 0)
    total_high_confidence_images = int(summary.get("total_high_confidence_images") or 0)
    placeholder_titles = [
        card["title"]
        for card in scorecards
        if card.get("image_quality", {}).get("all_placeholder")
    ]
    placeholder_image_count = sum(
        int(card.get("image_quality", {}).get("placeholder_count") or 0) for card in scorecards
    )
    local_svg_image_count = sum(
        int(card.get("image_quality", {}).get("local_svg_count") or 0) for card in scorecards
    )
    source_derived_image_count = sum(
        int(card.get("image_quality", {}).get("source_derived_count") or 0) for card in scorecards
    )
    pdf_hash = file_sha256(pdf_path)

    return {
        "date": date,
        "pdf_path": str(pdf_path),
        "pdf_sha256": pdf_hash,
        "page_count": page_count(pdf_path),
        "sparse_pages": sparse_pages(pdf_path),
        "sentinel_renders": render_sentinels(pdf_path),
        "total_inline_images": total_inline_images,
        "total_high_confidence_images": total_high_confidence_images,
        "low_image_articles": [],
        "placeholder_only_articles": placeholder_titles,
        "placeholder_image_count": placeholder_image_count,
        "local_svg_image_count": local_svg_image_count,
        "source_derived_image_count": source_derived_image_count,
    }


def write_visual_audit(date: str) -> Path:
    started = time.time()
    audit = build_visual_audit(date)
    path = Path(briefing_paths_module.visual_audit_path(date, audit["pdf_sha256"]))
    lines = [
        f"# Visual QA Audit - {date}",
        "",
        f"- PDF: {audit['pdf_path']}",
        f"- PDF SHA256: `{audit['pdf_sha256']}`",
        f"- Page count: {audit['page_count']}",
        f"- Sparse pages: {', '.join(str(page) for page in audit['sparse_pages']) or 'none'}",
        f"- Inline images: {audit['total_inline_images']}",
        f"- Validated high-confidence images: {audit['total_high_confidence_images']}",
        f"- Placeholder-only articles: {len(audit['placeholder_only_articles'])}",
        f"- Placeholder images: {audit['placeholder_image_count']}",
        f"- Local SVG images: {audit['local_svg_image_count']}",
        f"- Source-derived images: {audit['source_derived_image_count']}",
        "",
    ]
    if audit["sentinel_renders"]:
        lines.append("## Sentinel Renders")
        lines.append("")
        for render in audit["sentinel_renders"]:
            lines.append(f"- {render}")
        lines.append("")
    if audit["placeholder_only_articles"]:
        lines.append("## Placeholder-Only Articles")
        lines.append("")
        for title in audit["placeholder_only_articles"]:
            lines.append(f"- {title}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    ended = time.time()
    if edition_core.load_edition().image_mode == "per_article":
        # One licensed photograph or two per article is this edition's design; the one-image contract is the daily briefing's.
        visual_blocked = bool(audit["placeholder_only_articles"])
    else:
        visual_blocked = bool(
            audit["placeholder_only_articles"]
            or audit["total_inline_images"] > 1
            or (audit["total_inline_images"] == 1 and audit["total_high_confidence_images"] != 1)
        )
    visual_warned = bool(
        audit["sparse_pages"]
        or audit["placeholder_image_count"]
        or audit["total_inline_images"] == 0
    )
    visual_status = "failed" if visual_blocked else ("warning" if visual_warned else "passed")
    notes: list[str] = []
    if visual_status == "failed":
        notes.append(
            f"Visual QA failed: {len(audit['placeholder_only_articles'])} article(s) still use placeholder-only images."
        )
    elif visual_status == "warning":
        notes.append(
            "Visual QA warning: "
            f"{audit['total_inline_images']} inline image(s), "
            f"{audit['placeholder_image_count']} placeholder image(s), "
            f"{audit['source_derived_image_count']} source-derived image(s)."
        )
    else:
        notes.append("Visual QA passed for the current PDF hash.")
    update_run_report(
        date,
        visual_audit={"path": str(path), "pdf_sha256": audit["pdf_sha256"]},
        notes=notes,
        remove_note_substrings=["visual-QA-passed", "Visual QA warning:", "Visual QA failed:", "Visual QA passed"],
    )
    set_step_timing(
        date,
        "visual_qa",
        status=visual_status,
        started_at=datetime.fromtimestamp(started).astimezone().isoformat(),
        ended_at=datetime.fromtimestamp(ended).astimezone().isoformat(),
        duration_seconds=round(ended - started, 3),
        details={
            "visual_audit_path": str(path),
            "sparse_pages": audit["sparse_pages"],
            "low_image_articles": len(audit["low_image_articles"]),
            "placeholder_only_articles": len(audit["placeholder_only_articles"]),
            "placeholder_image_count": audit["placeholder_image_count"],
            "local_svg_image_count": audit["local_svg_image_count"],
            "source_derived_image_count": audit["source_derived_image_count"],
            "rendered_pages": audit["sentinel_renders"],
        },
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a lightweight PDF visual QA pass and bind the audit to the PDF hash.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD format.")
    args = parser.parse_args()

    path = write_visual_audit(args.date)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
