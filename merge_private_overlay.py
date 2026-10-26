"""Merge an overlay file into the private restore file (the one outside git).

    py -3 merge_private_overlay.py overlay.json            # merge into the default restore file
    py -3 merge_private_overlay.py overlay.json --dry-run  # show what would change, write nothing

The restore file (``%LOCALAPPDATA%\\daily-briefing-pipeline\\restore.local.json`` on Windows, or the path in
``BRIEFING_RESTORE_FILE``) holds everything personal to the owner: delivery settings and, under ``"edition:<id>"``, the coverage
details that the public edition files only describe generically. Keys already in the file are kept; dictionaries are merged
key by key, anything else in the overlay replaces the old value. The previous file is saved next to it as ``*.bak``.
Values are never printed: the output lists key paths only.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

import private_config


def key_paths(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, Mapping):
        out: list[str] = []
        for key, inner in value.items():
            out += key_paths(inner, f"{prefix}{key}." if isinstance(inner, Mapping) else f"{prefix}{key}")
        return out
    return [prefix.rstrip(".")]


def merge_file(overlay_path: Path, target: Path | None = None, *, dry_run: bool = False) -> list[str]:
    overlay = json.loads(overlay_path.read_text(encoding="utf-8-sig"))
    if not isinstance(overlay, dict):
        raise SystemExit("the overlay file must be a JSON object")
    target = target or private_config.default_restore_path()
    existing: dict[str, Any] = {}
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8-sig"))
        if not isinstance(existing, dict):
            raise SystemExit("the existing restore file is not a JSON object; fix or move it first")
    merged = private_config.apply_overlay(existing, overlay)
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        target.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return key_paths(overlay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("overlay", type=Path)
    parser.add_argument("--target", type=Path, help="restore file to update (default: the standard private location)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    target = args.target or private_config.default_restore_path()
    paths = merge_file(args.overlay, args.target, dry_run=args.dry_run)
    print(("Would merge" if args.dry_run else "Merged") + f" {len(paths)} setting(s) into {target}:")
    for path in paths:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
