"""Validator auto-fix mode: mechanical format patches for known-safe drifts.

Intended as a first-class replacement for the ad-hoc scratch scripts that
accumulated during past briefing-run recoveries (see
`generated/scratch/_normalize_validators.py`, `_patch_part1_format.py`,
`_hydrate_part1.py`). Each fixer:

- Operates on a single fragment file
- Is idempotent (running twice leaves the file unchanged after the first pass)
- Writes a `.proposed` file by default (dry-run)
- Only rewrites in place when `--apply` is passed

Currently supported:

- `jst_dateline_prepend` — when an article uses relative time tokens ("today",
  "yesterday", "tomorrow") without an explicit JST date anchor, prepend a
  `*Dateline: <Month D, YYYY> (JST).*` sentence at the top of the body.

Narrow by design: if a drift requires re-synthesis (e.g. a missing Counterpoint
Matrix, a missing `Cross-reference:` token in a header bullet), the fixer
REFUSES to auto-repair and emits a skip marker in the diff. Auto-fix is for
mechanical patches only.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import briefing_paths  # noqa: E402
from briefing_manifest_core import (  # noqa: E402
    EXPLICIT_DATE_RE,
    H2_SPLIT_RE,
    IMAGE_RE,
    RELATIVE_TIME_RE,
    SECTIONS,
    split_items,
)


def jst_dateline_for(date: str) -> str:
    dt = datetime.strptime(date, "%Y-%m-%d")
    fmt = "%B %-d, %Y" if sys.platform != "win32" else "%B %#d, %Y"
    return f"*Dateline: {dt.strftime(fmt)} (JST). Publication anchor for relative timing in this article.*"


def article_needs_dateline(body: str) -> bool:
    """True if body uses relative time tokens but has no JST date anchor."""
    if not RELATIVE_TIME_RE.search(body):
        return False
    if EXPLICIT_DATE_RE.search(body) and "JST" in body:
        return False
    return True


def apply_dateline(body: str, dateline: str) -> str:
    if dateline in body:
        return body  # idempotent
    lines = body.splitlines()
    # Insert dateline on its own line AFTER the first image tag (if any) so the
    # hero image stays visually at the top of the article, then the dateline
    # sits above the lede paragraph.
    insertion_point = 0
    for idx, line in enumerate(lines[:3]):
        stripped = line.strip()
        if IMAGE_RE.search(stripped):
            insertion_point = idx + 1
            continue
        if stripped == "":
            insertion_point = idx + 1
            continue
        break
    lines.insert(insertion_point, dateline)
    # Also insert a trailing blank line if the next line is non-blank
    if insertion_point + 1 < len(lines) and lines[insertion_point + 1].strip():
        lines.insert(insertion_point + 1, "")
    return "\n".join(lines)


def fix_fragment(section: str, date: str) -> dict[str, Any]:
    """Compute auto-fixes for a single fragment. Does NOT write."""
    frag = briefing_paths.fragment_path(section, date)
    if not frag.exists():
        return {"section": section, "exists": False, "changes": 0}

    original = frag.read_text(encoding="utf-8")
    # Keep whatever is before the first "## " heading (the section prologue)
    parts = H2_SPLIT_RE.split(original)
    prologue = parts[0]
    items = split_items(original)

    dateline = jst_dateline_for(date)
    changes: list[dict[str, Any]] = []
    new_article_blocks: list[str] = []

    for heading, body in items:
        needs = article_needs_dateline(body)
        if needs:
            new_body = apply_dateline(body, dateline)
            if new_body != body:
                changes.append(
                    {
                        "section": section,
                        "article": heading,
                        "fixer": "jst_dateline_prepend",
                    }
                )
            new_article_blocks.append(f"## {heading}\n{new_body}")
        else:
            new_article_blocks.append(f"## {heading}\n{body}")

    if not changes:
        return {
            "section": section,
            "exists": True,
            "changes": 0,
            "original_path": str(frag),
            "proposed_path": None,
            "patched_text": None,
        }

    patched = prologue + "\n".join(new_article_blocks)
    if not patched.endswith("\n"):
        patched += "\n"

    return {
        "section": section,
        "exists": True,
        "changes": len(changes),
        "original_path": str(frag),
        "proposed_path": str(frag.with_suffix(".md.proposed")),
        "patched_text": patched,
        "diff": "\n".join(difflib.unified_diff(
            original.splitlines(),
            patched.splitlines(),
            fromfile=str(frag),
            tofile=str(frag) + ".proposed",
            lineterm="",
        )),
        "changes_detail": changes,
    }


def autofix_date(date: str, *, sections: list[str] | None = None, apply: bool = False) -> dict[str, Any]:
    target_sections = sections or list(SECTIONS)
    results: list[dict[str, Any]] = []
    for section in target_sections:
        r = fix_fragment(section, date)
        results.append(r)
        if not r.get("patched_text"):
            continue
        if apply:
            Path(r["original_path"]).write_text(r["patched_text"], encoding="utf-8")
        else:
            Path(r["proposed_path"]).write_text(r["patched_text"], encoding="utf-8")
    return {
        "date": date,
        "apply": apply,
        "total_changes": sum(r.get("changes", 0) for r in results),
        "sections": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-fix known-safe format drifts in a briefing's fragments.",
    )
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite fragments in place. Without this flag, writes `.md.proposed` files instead.",
    )
    parser.add_argument(
        "--section",
        action="append",
        help="Limit fixes to these section(s). Repeatable. Default: all sections.",
    )
    parser.add_argument("--show-diff", action="store_true", help="Print unified diffs to stdout.")
    args = parser.parse_args()

    report = autofix_date(args.date, sections=args.section, apply=args.apply)
    for section_result in report["sections"]:
        if section_result.get("changes", 0) == 0:
            continue
        verb = "APPLIED" if args.apply else "PROPOSED"
        print(f"[{section_result['section']}] {verb}: {section_result['changes']} change(s)")
        for change in section_result.get("changes_detail", []):
            print(f"  - {change['fixer']}: {change['article']}")
        if args.show_diff:
            print(section_result.get("diff", ""))

    print(
        f"\nTotal changes: {report['total_changes']} "
        f"({'applied' if args.apply else 'written to .proposed'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
