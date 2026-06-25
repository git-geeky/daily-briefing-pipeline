from __future__ import annotations

import argparse

from briefing_paths import cleanup_generated_outputs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prune ephemeral generated/ outputs. PDFs are gzip-archived "
        "under archive/pdfs/{YYYY-MM}/ before deletion (unless --no-archive); "
        "scratch/ has its own --scratch-days retention; archived PDFs older "
        "than --pdf-archive-prune-days are pruned too.",
    )
    parser.add_argument("--days", type=int, default=3, help="Retention for generated/ files (default 3)")
    parser.add_argument(
        "--scratch-days",
        type=int,
        default=7,
        help="Retention for generated/scratch/ recovery scripts (default 7)",
    )
    parser.add_argument(
        "--pdf-archive-prune-days",
        type=int,
        default=365,
        help="Retention for gzip-archived PDFs under archive/pdfs/ (default 365)",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip PDF archival — revert to legacy 'delete everything at TTL' behavior.",
    )
    args = parser.parse_args()

    deleted = cleanup_generated_outputs(
        ttl_days=args.days,
        scratch_ttl_days=args.scratch_days,
        pdf_archive_prune_days=args.pdf_archive_prune_days,
        archive_pdfs=not args.no_archive,
    )
    print(f"Deleted {len(deleted)} expired files (PDFs archived under archive/pdfs if not --no-archive)")
    for path in deleted:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
