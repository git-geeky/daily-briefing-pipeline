from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import briefing_paths as briefing_paths_module
from briefing_manifest import HARD_NEWS_SECTIONS, SECTION_RULES
from run_reporting import set_step_timing, update_run_report


SCHEMA_VERSION = 1
VAULT_DB_NAME = "research-vault.db"
SOURCE_NOTE_DIR = "sources"
FTS_TABLE = "source_fts"
PRIMARY_SOURCE_TOKENS = (
    ".gov",
    ".mil",
    "official",
    "ministry",
    "department",
    "agency",
    "commission",
    "central bank",
    "bank of japan",
    "sec ",
    "filing",
    "dataset",
    "github",
    "arxiv",
    "paper",
    "journal",
    "open source",
    "source code",
    "united nations",
    "world bank",
    "imf",
    "oecd",
    "who",
)
AUTHORITY_SOURCE_TOKENS = (
    "reuters",
    "associated press",
    "ap news",
    "afp",
    "financial times",
    "bloomberg",
    "wall street journal",
    "new york times",
    "economist",
    "bbc",
    "nikkei",
    "wired",
    "ars technica",
)
TERTIARY_SOURCE_TOKENS = (
    "reddit",
    "hacker news",
    "news.ycombinator.com",
    "x.com",
    "twitter",
    "social",
    "forum",
    "podcast",
    "youtube",
    "opinion",
    "commentary",
)


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _optional_path(name: str, *args) -> Path | None:
    helper = getattr(briefing_paths_module, name, None)
    if not callable(helper):
        return None
    path = Path(helper(*args))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def vault_dir() -> Path:
    helper = getattr(briefing_paths_module, "research_vault_dir", None)
    if not callable(helper):
        path = Path.home() / "Documents" / "Daily-Briefings" / "research-vault"
    else:
        path = Path(helper())
    path.mkdir(parents=True, exist_ok=True)
    return path


def vault_db_path() -> Path:
    return vault_dir() / VAULT_DB_NAME


def vault_index_path() -> Path:
    helper = getattr(briefing_paths_module, "research_vault_index_path", None)
    if callable(helper):
        path = Path(helper())
    else:
        path = vault_dir() / "source-index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def source_note_root() -> Path:
    path = vault_dir() / SOURCE_NOTE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(value: str, *, default: str = "source") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or default


def note_path_for(source: dict[str, Any]) -> Path:
    domain = str(source.get("domain") or "unknown").lower().removeprefix("www.")
    domain_slug = _slug(domain, default="unknown")
    source_id = _slug(str(source.get("source_id") or source.get("name") or "source"))
    path = source_note_root() / domain_slug / f"{source_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(vault_db_path())
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            domain TEXT NOT NULL,
            bucket TEXT,
            major_bucket TEXT,
            section TEXT,
            status TEXT,
            first_seen_date TEXT,
            last_seen_date TEXT,
            last_checked_at TEXT,
            run_count INTEGER NOT NULL DEFAULT 0,
            latest_http_status INTEGER,
            latest_final_url TEXT,
            suggested_by TEXT NOT NULL DEFAULT 'sources.md',
            note_path TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_runs (
            source_id TEXT NOT NULL,
            run_date TEXT NOT NULL,
            status TEXT,
            checked_at TEXT,
            http_status INTEGER,
            final_url TEXT,
            errors_json TEXT,
            PRIMARY KEY (source_id, run_date),
            FOREIGN KEY (source_id) REFERENCES sources(source_id)
        );
        """
    )
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
            "USING fts5(source_id, name, domain, bucket, body)"
        )
    except sqlite3.OperationalError:
        # Some Python builds omit FTS5. Search falls back to LIKE queries.
        pass
    conn.commit()


def _json_field(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _frontmatter_line(key: str, value: Any) -> str:
    return f"{key}: {_json_field(value)}"


def source_body(source: dict[str, Any], run_rows: list[sqlite3.Row]) -> str:
    run_lines = [
        "| Date | Status | HTTP | Final URL |",
        "|---|---|---:|---|",
    ]
    for row in run_rows:
        run_lines.append(
            "| {date} | {status} | {http} | {url} |".format(
                date=row["run_date"] or "",
                status=row["status"] or "",
                http=row["http_status"] if row["http_status"] is not None else "",
                url=row["final_url"] or "",
            )
        )
    return "\n".join(
        [
            f"# {source.get('name') or source.get('source_id')}",
            "",
            "## Source",
            "",
            f"- URL: {source.get('url')}",
            f"- Domain: {source.get('domain')}",
            f"- Bucket: {source.get('bucket') or source.get('major_bucket') or 'Unbucketed'}",
            f"- Section: {source.get('section') or 'cross_section'}",
            f"- Latest status: {source.get('status')}",
            f"- Suggested by: {source.get('suggested_by') or 'sources.md'}",
            "",
            "## Provenance",
            "",
            "This note was generated from the shared daily briefing source audit. "
            "The Markdown note is durable; the SQLite index is a rebuildable cache.",
            "",
            "## Run History",
            "",
            *run_lines,
            "",
        ]
    )


def write_source_note(source: dict[str, Any], run_rows: list[sqlite3.Row], note_path: Path) -> None:
    frontmatter = [
        "---",
        _frontmatter_line("source_id", source.get("source_id")),
        _frontmatter_line("title", source.get("name")),
        _frontmatter_line("type", "source"),
        _frontmatter_line("domain", source.get("domain")),
        _frontmatter_line("bucket", source.get("bucket") or source.get("major_bucket")),
        _frontmatter_line("section", source.get("section") or "cross_section"),
        _frontmatter_line("status", source.get("status")),
        _frontmatter_line("first_seen_date", source.get("first_seen_date")),
        _frontmatter_line("last_seen_date", source.get("last_seen_date")),
        _frontmatter_line("run_count", source.get("run_count")),
        _frontmatter_line("suggested_by", source.get("suggested_by") or "sources.md"),
        "---",
        "",
    ]
    note_path.write_text("\n".join(frontmatter) + source_body(source, run_rows), encoding="utf-8")


def _fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (FTS_TABLE,),
    ).fetchone()
    return row is not None


def _source_text_for_search(source: dict[str, Any]) -> str:
    fields = [
        source.get("source_id"),
        source.get("name"),
        source.get("url"),
        source.get("domain"),
        source.get("bucket"),
        source.get("major_bucket"),
        source.get("section"),
        source.get("status"),
        " ".join(source.get("region_tags") or []),
        " ".join(source.get("language_tags") or []),
    ]
    return " ".join(str(field) for field in fields if field)


def upsert_source(conn: sqlite3.Connection, date: str, source: dict[str, Any]) -> dict[str, Any]:
    source_id = str(source.get("source_id") or "")
    if not source_id:
        raise ValueError("source_id is required for vault sync")

    existing = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    first_seen = existing["first_seen_date"] if existing else date
    note_path = note_path_for(source)
    suggested_by = str(source.get("suggested_by") or "sources.md")

    conn.execute(
        """
        INSERT OR REPLACE INTO sources (
            source_id, name, url, domain, bucket, major_bucket, section, status,
            first_seen_date, last_seen_date, last_checked_at, run_count,
            latest_http_status, latest_final_url, suggested_by, note_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT run_count FROM sources WHERE source_id = ?), 0), ?, ?, ?, ?)
        """,
        (
            source_id,
            str(source.get("name") or source_id),
            str(source.get("url") or ""),
            str(source.get("domain") or ""),
            str(source.get("bucket") or ""),
            str(source.get("major_bucket") or ""),
            str(source.get("section") or ""),
            str(source.get("status") or ""),
            first_seen,
            date,
            str(source.get("checked_at") or ""),
            source_id,
            source.get("http_status"),
            str(source.get("final_url") or source.get("url") or ""),
            suggested_by,
            str(note_path),
        ),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO source_runs (
            source_id, run_date, status, checked_at, http_status, final_url, errors_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            date,
            str(source.get("status") or ""),
            str(source.get("checked_at") or ""),
            source.get("http_status"),
            str(source.get("final_url") or source.get("url") or ""),
            json.dumps(source.get("errors") or [], ensure_ascii=False),
        ),
    )
    run_count = conn.execute(
        "SELECT COUNT(*) AS count FROM source_runs WHERE source_id = ?",
        (source_id,),
    ).fetchone()["count"]
    conn.execute("UPDATE sources SET run_count = ? WHERE source_id = ?", (run_count, source_id))

    row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    source_record = dict(row)
    run_rows = list(
        conn.execute(
            "SELECT * FROM source_runs WHERE source_id = ? ORDER BY run_date DESC LIMIT 20",
            (source_id,),
        )
    )
    write_source_note(source_record, run_rows, note_path)
    if _fts_available(conn):
        conn.execute(f"DELETE FROM {FTS_TABLE} WHERE source_id = ?", (source_id,))
        conn.execute(
            f"INSERT INTO {FTS_TABLE} (source_id, name, domain, bucket, body) VALUES (?, ?, ?, ?, ?)",
            (
                source_id,
                source_record["name"],
                source_record["domain"],
                source_record["bucket"] or source_record["major_bucket"] or "",
                _source_text_for_search({**source, **source_record}),
            ),
        )
    return source_record


def write_vault_index(conn: sqlite3.Connection, *, last_sync_date: str, last_sync_count: int) -> dict[str, Any]:
    rows = list(conn.execute("SELECT * FROM sources ORDER BY domain, name"))
    status_counts: dict[str, int] = {}
    section_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["status"] or "unknown"] = status_counts.get(row["status"] or "unknown", 0) + 1
        section = row["section"] or "cross_section"
        section_counts[section] = section_counts.get(section, 0) + 1

    payload = {
        "schema_version": SCHEMA_VERSION,
        "vault_dir": str(vault_dir()),
        "db_path": str(vault_db_path()),
        "updated_at": iso_now(),
        "last_sync_date": last_sync_date,
        "last_sync_count": last_sync_count,
        "summary": {
            "source_count": len(rows),
            "status_counts": status_counts,
            "section_counts": section_counts,
            "fts_available": _fts_available(conn),
        },
        "sources": [
            {
                "source_id": row["source_id"],
                "name": row["name"],
                "domain": row["domain"],
                "status": row["status"],
                "section": row["section"],
                "run_count": row["run_count"],
                "note_path": row["note_path"],
            }
            for row in rows
        ],
    }
    vault_index_path().write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def sync_source_audit(date: str, source_audit: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.time()
    payload = source_audit or {}
    if not payload:
        path = _optional_path("source_audit_path", date)
        if path and path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
    sources = [source for source in payload.get("sources") or [] if isinstance(source, dict)]
    conn = connect()
    try:
        synced = [upsert_source(conn, date, source) for source in sources]
        conn.commit()
        index_payload = write_vault_index(conn, last_sync_date=date, last_sync_count=len(synced))
    finally:
        conn.close()
    ended = time.time()
    summary = {
        "status": "synced",
        "vault_dir": str(vault_dir()),
        "index_path": str(vault_index_path()),
        "db_path": str(vault_db_path()),
        "synced_source_count": len(synced),
        "total_source_count": index_payload["summary"]["source_count"],
        "fts_available": index_payload["summary"]["fts_available"],
    }
    update_run_report(date, research_vault=summary)
    set_step_timing(
        date,
        "research_vault_sync",
        status="passed",
        started_at=datetime.fromtimestamp(started).astimezone().isoformat(),
        ended_at=datetime.fromtimestamp(ended).astimezone().isoformat(),
        duration_seconds=round(ended - started, 3),
        details=summary,
    )
    return summary


def search_vault(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    conn = connect()
    try:
        if query and _fts_available(conn):
            rows = conn.execute(
                f"""
                SELECT s.*, bm25({FTS_TABLE}) AS rank
                FROM {FTS_TABLE}
                JOIN sources s USING (source_id)
                WHERE {FTS_TABLE} MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        elif query:
            like = f"%{query}%"
            rows = conn.execute(
                """
                SELECT * FROM sources
                WHERE source_id LIKE ? OR name LIKE ? OR domain LIKE ? OR bucket LIKE ? OR section LIKE ?
                ORDER BY domain, name
                LIMIT ?
                """,
                (like, like, like, like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sources ORDER BY last_seen_date DESC, domain, name LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def lint_vault() -> dict[str, Any]:
    index_path = vault_index_path()
    issues: list[dict[str, Any]] = []
    if not index_path.exists():
        issues.append(
            {
                "rule": "missing-vault-index",
                "severity": "error",
                "message": "Research vault index is missing; run generate_source_audit.py for a synced briefing date.",
            }
        )
        return {"schema_version": SCHEMA_VERSION, "status": "failed", "issues": issues}

    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    for source in index_payload.get("sources") or []:
        note_path = Path(source.get("note_path") or "")
        if not note_path.exists():
            issues.append(
                {
                    "rule": "missing-source-note",
                    "severity": "error",
                    "source_id": source.get("source_id"),
                    "message": f"Vault index points to a missing source note: {note_path}",
                }
            )
    conn = connect()
    try:
        db_count = conn.execute("SELECT COUNT(*) AS count FROM sources").fetchone()["count"]
    finally:
        conn.close()
    index_count = len(index_payload.get("sources") or [])
    if db_count != index_count:
        issues.append(
            {
                "rule": "db-index-drift",
                "severity": "warning",
                "message": f"SQLite cache has {db_count} sources but index has {index_count}; rerun source-vault sync.",
            }
        )
    status = "failed" if any(issue["severity"] == "error" for issue in issues) else ("warning" if issues else "ok")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "vault_dir": str(vault_dir()),
        "index_path": str(index_path),
        "issues": issues,
    }


@dataclass(frozen=True)
class DailyArtifactPaths:
    evidence_digest: Path
    source_tensions: Path
    research_lint: Path
    source_confidence: Path


def daily_artifact_paths(date: str) -> DailyArtifactPaths:
    evidence_digest = _optional_path("evidence_digest_path", date) or (
        Path(getattr(briefing_paths_module, "generated_dir")()) / f"briefing-{date}-evidence-digest.md"
    )
    source_tensions = _optional_path("source_tensions_path", date) or evidence_digest.with_name(
        evidence_digest.name.replace("evidence-digest.md", "source-tensions.json")
    )
    research_lint = _optional_path("research_lint_path", date) or evidence_digest.with_name(
        evidence_digest.name.replace("evidence-digest.md", "research-lint.json")
    )
    source_confidence = _optional_path("source_confidence_path", date) or evidence_digest.with_name(
        evidence_digest.name.replace("evidence-digest.md", "source-confidence.json")
    )
    return DailyArtifactPaths(
        evidence_digest=evidence_digest,
        source_tensions=source_tensions,
        research_lint=research_lint,
        source_confidence=source_confidence,
    )


def _source_lookup(source_audit: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(source.get("source_id")): source
        for source in (source_audit or {}).get("sources") or []
        if isinstance(source, dict) and source.get("source_id")
    }


def _source_text(source: dict[str, Any]) -> str:
    fields = [
        source.get("source_id"),
        source.get("name"),
        source.get("url"),
        source.get("domain"),
        source.get("bucket"),
        source.get("major_bucket"),
        source.get("sub_bucket"),
        source.get("why"),
        source.get("status"),
    ]
    return " ".join(str(field).lower() for field in fields if field)


def classify_source_confidence(source: dict[str, Any]) -> dict[str, Any]:
    """Assign Aeon-style CRAAP-lite source type, score, and tier from catalog metadata."""
    text = _source_text(source)
    status = str(source.get("status") or "")
    successful = status in {"checked_used", "checked_relevant_not_selected", "checked_no_relevant_update"}

    if any(token in text for token in TERTIARY_SOURCE_TOKENS):
        source_type = "tertiary"
    elif any(token in text for token in PRIMARY_SOURCE_TOKENS):
        source_type = "primary"
    else:
        source_type = "secondary"

    if source_type == "primary" or any(token in text for token in AUTHORITY_SOURCE_TOKENS):
        authority = 3
    elif source_type == "secondary":
        authority = 2
    else:
        authority = 1

    # The source audit is a daily check, so catalog recency measures whether this
    # source was inspected in the current run rather than the article publication date.
    recency = 3 if source.get("checked_at") else 2

    if successful and source_type == "primary":
        verifiability = 3
    elif successful:
        verifiability = 2
    else:
        verifiability = 1

    total = authority + recency + verifiability
    if total >= 8 and (source_type == "primary" or (source_type == "secondary" and authority == 3)):
        tier = "T1"
    elif total >= 5:
        tier = "T2"
    else:
        tier = "T3"
    if source_type == "tertiary" and tier == "T1":
        tier = "T2"

    return {
        "source_id": source.get("source_id"),
        "name": source.get("name"),
        "url": source.get("final_url") or source.get("url"),
        "domain": source.get("domain"),
        "status": status,
        "source_type": source_type,
        "tier": tier,
        "craap_lite": {
            "authority": authority,
            "recency": recency,
            "verifiability": verifiability,
            "total": total,
        },
    }


def _empty_tier_counts() -> dict[str, int]:
    return {"T1": 0, "T2": 0, "T3": 0, "unmatched": 0}


def _empty_type_counts() -> dict[str, int]:
    return {"primary": 0, "secondary": 0, "tertiary": 0, "unmatched": 0}


def source_confidence_lookup(source_lookup: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        source_id: classify_source_confidence(source)
        for source_id, source in source_lookup.items()
    }


def confidence_for_article(item: dict[str, Any], source_confidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    source_ids = [str(source_id) for source_id in item.get("source_audit_source_ids") or []]
    tier_counts = _empty_tier_counts()
    type_counts = _empty_type_counts()
    matched_sources: list[dict[str, Any]] = []
    for source_id in source_ids:
        source = source_confidence.get(source_id)
        if not source:
            tier_counts["unmatched"] += 1
            type_counts["unmatched"] += 1
            continue
        tier_counts[source["tier"]] = tier_counts.get(source["tier"], 0) + 1
        type_counts[source["source_type"]] = type_counts.get(source["source_type"], 0) + 1
        matched_sources.append(source)

    source_count = int(item.get("source_count") or len(source_ids) or 0)
    source_class_count = len(item.get("source_classes") or [])
    counterpoint_rows = int(item.get("counterpoint_row_count") or 0)
    has_second_pass_gap = bool(item.get("needs_second_pass"))
    has_watch = bool(item.get("watch_next_48h"))

    downgrade_reasons: list[str] = []
    if tier_counts["T1"] < 1:
        downgrade_reasons.append("No T1 catalog source matched the article.")
    if source_count < 5:
        downgrade_reasons.append(f"Only {source_count} primary-source bullets were captured.")
    if source_class_count < 3 and item.get("section") in HARD_NEWS_SECTIONS:
        downgrade_reasons.append(f"Only {source_class_count} source classes were captured.")
    if has_second_pass_gap:
        downgrade_reasons.append("Manifest marked the story as needing a second corroboration pass.")
    if item.get("section") in HARD_NEWS_SECTIONS and counterpoint_rows < 2:
        downgrade_reasons.append("Counterpoint Matrix has fewer than two rows.")

    if (
        tier_counts["T1"] >= 2
        and source_count >= 5
        and source_class_count >= 3
        and not has_second_pass_gap
        and (item.get("section") not in HARD_NEWS_SECTIONS or counterpoint_rows >= 2)
    ):
        confidence = "High"
        rationale = "At least two T1 catalog sources, adequate source depth, source-class diversity, and no unresolved second-pass gap."
    elif (
        (tier_counts["T1"] >= 1 or tier_counts["T2"] >= 4)
        and source_count >= 4
        and not has_second_pass_gap
    ):
        confidence = "Medium"
        rationale = "Supported by at least one T1 or several T2 sources, but below the preferred High-confidence mix."
    else:
        confidence = "Low"
        rationale = "Evidence is thin, weakly tiered, or explicitly marked for additional corroboration."

    falsifiable_checks = [
        f"Revisit if this watch item moves against the article thesis: {entry}"
        for entry in (item.get("watch_next_48h") or [])[:3]
    ]
    if not falsifiable_checks and has_watch:
        falsifiable_checks.append("Revisit after the next Watch Next 48h event resolves.")
    elif not falsifiable_checks:
        falsifiable_checks.append("Revisit if the next daily source audit adds a T1 source contradicting the article's central claim.")

    open_questions: list[str] = []
    if tier_counts["T1"] < 2:
        open_questions.append("Can two independent T1 sources corroborate the central claim?")
    if type_counts["primary"] == 0:
        open_questions.append("Is there a primary document, filing, dataset, code release, or official schedule behind the story?")
    if tier_counts["T3"] > 0:
        open_questions.append("Are tertiary/community sources only adding counterpoints, or are they carrying factual claims?")
    if not has_watch and item.get("section") in HARD_NEWS_SECTIONS:
        open_questions.append("What dated external event would falsify or confirm the next-step assessment?")

    return {
        "story_id": item.get("story_id"),
        "section": item.get("section"),
        "title": item.get("title"),
        "confidence": confidence,
        "rationale": rationale,
        "downgrade_reasons": downgrade_reasons,
        "tier_counts": tier_counts,
        "type_counts": type_counts,
        "source_count": source_count,
        "source_class_count": source_class_count,
        "source_audit_source_ids": source_ids,
        "matched_sources": matched_sources,
        "falsifiable_checks": falsifiable_checks,
        "open_questions": open_questions,
    }


def build_source_confidence_audit(
    date: str,
    articles: list[dict[str, Any]],
    source_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_confidence = source_confidence_lookup(source_lookup)
    article_confidence = [
        confidence_for_article(item, source_confidence)
        for item in articles
    ]
    tier_counts = _empty_tier_counts()
    type_counts = _empty_type_counts()
    for source in source_confidence.values():
        tier_counts[source["tier"]] = tier_counts.get(source["tier"], 0) + 1
        type_counts[source["source_type"]] = type_counts.get(source["source_type"], 0) + 1
    confidence_counts: dict[str, int] = {"High": 0, "Medium": 0, "Low": 0}
    for article in article_confidence:
        confidence_counts[article["confidence"]] = confidence_counts.get(article["confidence"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "generated_at": iso_now(),
        "inspired_by": {
            "project": "aaronjmars/aeon deep-research skill",
            "pattern": "CRAAP-lite tiering, source diversity audit, confidence calibration, falsifiable checks, and open questions",
        },
        "policy": {
            "tiering": "T1/T2/T3 is a deterministic catalog-source aid, not a replacement for editorial judgment.",
            "high_confidence_preference": "Prefer >=2 T1 sources for High confidence; state gaps when T1 coverage is unavailable.",
            "tertiary_usage": "Tertiary/community sources may add counterpoints, but should not carry primary factual claims.",
        },
        "summary": {
            "article_count": len(article_confidence),
            "confidence_counts": confidence_counts,
            "catalog_tier_counts": tier_counts,
            "catalog_type_counts": type_counts,
            "low_confidence_articles": [
                {"section": item["section"], "title": item["title"]}
                for item in article_confidence
                if item["confidence"] == "Low"
            ],
        },
        "articles": article_confidence,
        "catalog_sources": sorted(source_confidence.values(), key=lambda item: (item["tier"], str(item["domain"]), str(item["name"]))),
    }


def _iter_articles(assembled_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    sections = assembled_manifest.get("sections") or {}
    articles: list[dict[str, Any]] = []
    for section, section_payload in sections.items():
        if section == "header":
            continue
        for item in section_payload.get("items") or []:
            if item.get("quality_failure"):
                continue
            articles.append(item)
    return articles


def build_source_tensions(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tensions: list[dict[str, Any]] = []
    for item in articles:
        rows = item.get("counterpoint_rows") or []
        for index, row in enumerate(rows, start=1):
            cells = [str(cell).strip() for cell in row if str(cell).strip()]
            if not cells:
                continue
            tensions.append(
                {
                    "tension_id": f"{item.get('story_id')}-counterpoint-{index}",
                    "section": item.get("section"),
                    "article_title": item.get("title"),
                    "origin": "counterpoint_matrix",
                    "row_index": index,
                    "cells": cells,
                    "claim": cells[0] if cells else "",
                    "counterpoint_source": cells[3] if len(cells) > 3 else "",
                    "net_assessment": cells[4] if len(cells) > 4 else "",
                    "source_audit_source_ids": item.get("source_audit_source_ids") or [],
                }
            )
    return tensions


def build_research_lint(
    date: str,
    articles: list[dict[str, Any]],
    tensions: list[dict[str, Any]],
    candidate_pool: dict[str, Any] | None,
    research_depth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    tension_by_story: dict[str, int] = {}
    for tension in tensions:
        story_id = str(tension.get("tension_id") or "").rsplit("-counterpoint-", 1)[0]
        tension_by_story[story_id] = tension_by_story.get(story_id, 0) + 1

    for item in articles:
        section = item.get("section")
        prefix = f"{section}/{item.get('title')}"
        if section in HARD_NEWS_SECTIONS:
            if not item.get("source_audit_source_ids"):
                issues.append(
                    {
                        "rule": "source-provenance",
                        "severity": "error",
                        "article": prefix,
                        "message": "Hard-news article cites no source from the checked daily source-audit catalog.",
                    }
                )
            if int(item.get("counterpoint_row_count") or 0) < 2:
                issues.append(
                    {
                        "rule": "source-tension-coverage",
                        "severity": "error",
                        "article": prefix,
                        "message": "Hard-news article has fewer than two Counterpoint Matrix rows.",
                    }
                )
            if item.get("needs_second_pass"):
                issues.append(
                    {
                        "rule": "corpus-critic-gap",
                        "severity": "error",
                        "article": prefix,
                        "message": "Thin source-class mix requires a second corroboration pass before shipping.",
                    }
                )
            if tension_by_story.get(str(item.get("story_id")), 0) < 2:
                issues.append(
                    {
                        "rule": "counterpoint-artifact-gap",
                        "severity": "error",
                        "article": prefix,
                        "message": "The source-tensions artifact did not capture at least two tensions for this hard-news article.",
                    }
                )

    sections = (candidate_pool or {}).get("sections") or {}
    depth_sections = (research_depth or {}).get("sections") or {}
    for section, pool in sections.items():
        if section not in HARD_NEWS_SECTIONS:
            continue
        selected = int(pool.get("selected_count") or 0)
        target = int(pool.get("target_count") or 0)
        minimum = int(pool.get("min_count") or 0)
        rejected = pool.get("rejected") or []
        if target and minimum and selected <= minimum and selected < target and not rejected:
            depth_plan = depth_sections.get(section) or {}
            follow_up_queries = [
                str(item.get("query"))
                for item in depth_plan.get("follow_up_questions") or []
                if isinstance(item, dict) and item.get("query")
            ]
            suffix = ""
            if follow_up_queries:
                suffix = " Suggested follow-up queries: " + "; ".join(follow_up_queries[:2])
            issues.append(
                {
                    "rule": "corpus-width-gap",
                    "severity": "warning",
                    "section": section,
                    "message": "Section stopped at/below the adaptive minimum with zero rejected candidates; discovery likely ended too early." + suffix,
                }
            )

    status = "failed" if any(issue["severity"] == "error" for issue in issues) else ("warning" if issues else "ok")
    return {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "status": status,
        "policy": {
            "prompt_gospel": "briefing-spec.md and generation-spec.md are the canonical generation contract",
            "patch_only_after_validation": True,
            "persistent_vault": str(vault_dir()),
        },
        "summary": {
            "article_count": len(articles),
            "hard_news_article_count": sum(1 for item in articles if item.get("section") in HARD_NEWS_SECTIONS),
            "source_tension_count": len(tensions),
            "issue_count": len(issues),
            "error_count": sum(1 for issue in issues if issue["severity"] == "error"),
            "warning_count": sum(1 for issue in issues if issue["severity"] == "warning"),
        },
        "issues": issues,
    }


def write_evidence_digest(
    path: Path,
    date: str,
    articles: list[dict[str, Any]],
    source_lookup: dict[str, dict[str, Any]],
    source_confidence_audit: dict[str, Any] | None = None,
) -> None:
    confidence_by_story = {
        str(item.get("story_id")): item
        for item in (source_confidence_audit or {}).get("articles") or []
        if isinstance(item, dict) and item.get("story_id")
    }
    lines = [
        f"# Evidence Digest - {date}",
        "",
        "Deterministic evidence index generated from the assembled briefing manifest.",
        "",
    ]
    for item in articles:
        confidence = confidence_by_story.get(str(item.get("story_id"))) or {}
        tier_counts = confidence.get("tier_counts") or {}
        lines.extend(
            [
                f"## {item.get('section')} - {item.get('title')}",
                "",
                f"- Story ID: `{item.get('story_id')}`",
                f"- Word count: {item.get('word_count')}",
                f"- Overall score: {((item.get('score') or {}).get('overall'))}",
                f"- Source confidence: {confidence.get('confidence', 'not calculated')}",
                f"- Source tier mix: T1={tier_counts.get('T1', 0)}, T2={tier_counts.get('T2', 0)}, T3={tier_counts.get('T3', 0)}, unmatched={tier_counts.get('unmatched', 0)}",
                f"- Source classes: {', '.join(item.get('source_classes') or []) or 'none'}",
                f"- Source audit IDs: {', '.join(item.get('source_audit_source_ids') or []) or 'none'}",
                f"- Counterpoint rows: {item.get('counterpoint_row_count')}",
                f"- Watch Next 48h entries: {len(item.get('watch_next_48h') or [])}",
                "",
                "### Primary Sources",
                "",
            ]
        )
        for source in item.get("primary_sources") or []:
            lines.append(f"- {source}")
        if not item.get("primary_sources"):
            lines.append("- none captured")
        if item.get("source_audit_source_ids"):
            lines.extend(["", "### Source Audit Matches", ""])
            for source_id in item.get("source_audit_source_ids") or []:
                source = source_lookup.get(source_id) or {}
                lines.append(
                    f"- `{source_id}` - {source.get('name', 'unknown')} - {source.get('status', 'unknown')} - {source.get('url', '')}"
                )
        if confidence.get("falsifiable_checks"):
            lines.extend(["", "### Falsifiable Checks", ""])
            for check in confidence.get("falsifiable_checks") or []:
                lines.append(f"- {check}")
        if confidence.get("open_questions"):
            lines.extend(["", "### Open Questions", ""])
            for question in confidence.get("open_questions") or []:
                lines.append(f"- {question}")
        if item.get("counterpoint_rows"):
            lines.extend(["", "### Counterpoints", ""])
            for row in item.get("counterpoint_rows") or []:
                cleaned = " | ".join(str(cell).strip() for cell in row if str(cell).strip())
                lines.append(f"- {cleaned}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_daily_research_artifacts(
    date: str,
    assembled_manifest: dict[str, Any],
    *,
    source_audit: dict[str, Any] | None = None,
    candidate_pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.time()
    paths = daily_artifact_paths(date)
    articles = _iter_articles(assembled_manifest)
    tensions = build_source_tensions(articles)
    source_lookup = _source_lookup(source_audit)
    source_confidence_audit = build_source_confidence_audit(date, articles, source_lookup)
    try:
        from research_depth import generate_research_depth_plan

        depth_plan = generate_research_depth_plan(
            date,
            source_audit=source_audit,
            assembled_manifest=assembled_manifest,
            candidate_pool=candidate_pool,
        )
    except Exception as exc:  # pragma: no cover - defensive artifact path
        depth_plan = {
            "status": "failed",
            "summary": {"planned_query_count": 0, "follow_up_count": 0, "gap_count": 0},
            "error": f"{type(exc).__name__}: {exc}",
        }
    lint = build_research_lint(date, articles, tensions, candidate_pool, research_depth=depth_plan)

    write_evidence_digest(paths.evidence_digest, date, articles, source_lookup, source_confidence_audit)
    paths.source_tensions.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "date": date,
                "summary": {
                    "source_tension_count": len(tensions),
                    "article_count": len(articles),
                },
                "tensions": tensions,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    paths.source_confidence.write_text(
        json.dumps(source_confidence_audit, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    paths.research_lint.write_text(json.dumps(lint, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = {
        "status": lint["status"],
        "evidence_digest_path": str(paths.evidence_digest),
        "source_tensions_path": str(paths.source_tensions),
        "research_lint_path": str(paths.research_lint),
        "source_confidence_path": str(paths.source_confidence),
        "source_confidence_counts": source_confidence_audit["summary"]["confidence_counts"],
        "source_tier_counts": source_confidence_audit["summary"]["catalog_tier_counts"],
        "research_depth_status": depth_plan.get("status"),
        "research_depth_planned_query_count": (depth_plan.get("summary") or {}).get("planned_query_count", 0),
        "research_depth_follow_up_count": (depth_plan.get("summary") or {}).get("follow_up_count", 0),
        "source_tension_count": len(tensions),
        "issue_count": lint["summary"]["issue_count"],
        "error_count": lint["summary"]["error_count"],
        "warning_count": lint["summary"]["warning_count"],
    }
    update_run_report(date, research_artifacts=summary)
    ended = time.time()
    set_step_timing(
        date,
        "research_artifacts",
        status="failed" if lint["status"] == "failed" else ("warning" if lint["status"] == "warning" else "passed"),
        started_at=datetime.fromtimestamp(started).astimezone().isoformat(),
        ended_at=datetime.fromtimestamp(ended).astimezone().isoformat(),
        duration_seconds=round(ended - started, 3),
        details=summary,
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the persistent briefing research vault.")
    parser.add_argument("--date", help="Sync source-audit artifacts for a briefing date.")
    parser.add_argument("--search", help="Search source notes in the persistent vault.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum search results.")
    parser.add_argument("--lint", action="store_true", help="Run vault health checks.")
    args = parser.parse_args()

    if args.date:
        summary = sync_source_audit(args.date)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    if args.search is not None:
        print(json.dumps(search_vault(args.search, limit=args.limit), indent=2, ensure_ascii=False))
        return 0
    if args.lint:
        result = lint_vault()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if result["status"] == "failed" else 0
    parser.print_help()
    return 0


__all__ = [
    "build_source_confidence_audit",
    "build_daily_research_artifacts",
    "classify_source_confidence",
    "lint_vault",
    "search_vault",
    "sync_source_audit",
    "vault_dir",
    "vault_index_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
