"""Track which catalog sources cannot be reached, and retire or replace the ones that never will be.

The daily source audit visits every catalog source. Without memory, a source that has been dead or hard-paywalled for months is
re-fetched, re-failed and re-counted against the audit's failure budget every day, and nobody ever decides anything about it. This
module keeps that memory (one small JSON file, outside the three-day ``generated/`` clean-up) and applies one policy:

* A source that fails is put **under watch**, with the date, the kind of failure and the streak, so a transitory outage (a timeout,
  a server error, a bad network day) is followed until the source answers again, at which point the streak is forgotten.
* A source whose failures are **permanent in kind** is **retired**: it is no longer fetched, no longer counted against the audit,
  and its role is covered by other catalog sources of the same bucket, language and region where there are any (``substitutes``);
  where there are none it is simply removed and listed as uncovered, so a person (or a research pass) can add a different source.
* A retired source is probed again every ``REPROBE_DAYS`` days and **reinstated** the first time it answers, because "permanent" is a
  judgement, not a fact.

Permanent means: a hard paywall (HTTP 402, or a login wall with no headlines, on several different days); a page that is gone
(404/410, a name that no longer resolves) for about a week; a site that refuses automated readers (403/429/challenge pages) for
three weeks; anything else (timeouts, 5xx, empty bodies) for two weeks. The thresholds count distinct audit days, never repeated
attempts in one day, and a day on which a large share of *all* sources failed (this machine's network, a sandbox's IP range) counts
for nothing, so one bad day cannot retire anything.

Pure functions over a state dictionary plus small file helpers: no network, no model.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date as _date
from pathlib import Path
from typing import Any, Iterable

STATE_VERSION = 1
SYSTEMIC_DAY_FRACTION = 0.40       # a day on which this share of the checked sources failed says nothing about any one source
PAYWALL_DAYS = 3                   # distinct days of a hard paywall / login wall before retiring
GONE_DAYS, GONE_SPAN = 5, 7        # distinct failing days and calendar span for a page that is gone
BLOCKED_DAYS, BLOCKED_SPAN = 14, 21
OTHER_DAYS, OTHER_SPAN = 10, 14
REPROBE_DAYS = 30
KEEP_DAY_RECORDS = 90
MAX_HISTORY = 12

_SUCCESS = {"checked_used", "checked_relevant_not_selected", "checked_no_relevant_update"}
_DNS_RE = re.compile(r"(?i)name or service not known|nodename nor servname|getaddrinfo|name resolution|no address associated|nxdomain|temporary failure in name")
_PAYWALL_TEXT = ("paywall", "subscribe to read", "subscription required", "member-only")


def _day(value: str) -> _date:
    return _date.fromisoformat(value)


def classify_result(source: dict[str, Any]) -> str:
    """``ok | paywall | gone | blocked | transient`` for one audit result (a dict as written by ``source_audit_core.check_source``)."""
    status = str(source.get("status") or "")
    if status in _SUCCESS:
        return "ok"
    try:
        code = int(source.get("http_status"))
    except (TypeError, ValueError):
        code = None
    adaptive = source.get("adaptive_fetch") if isinstance(source.get("adaptive_fetch"), dict) else {}
    verdict = str(adaptive.get("verdict") or "")
    errors = " ".join(str(e) for e in (source.get("errors") or []))
    headlines = len(source.get("items") or [])
    if code == 402:
        return "paywall"
    if code == 401 or (verdict == "auth_required" and headlines == 0 and any(t in errors.lower() for t in _PAYWALL_TEXT + ("auth_required",))):
        return "paywall"
    if code in (404, 410):
        return "gone"
    if code is None and _DNS_RE.search(errors):
        return "gone"
    if code in (403, 407, 429, 451) or verdict in {"challenge", "browser_required"}:
        return "blocked"
    return "transient"


def default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "sources": {}, "days": {}}


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_state()
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return default_state()
    data.setdefault("sources", {})
    data.setdefault("days", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    for attempt in range(8):  # a reader (antivirus, a diagnosis run) can hold the file for a moment on Windows
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    os.replace(tmp, path)


# --------------------------------------------------------------------------------------------
# Before the audit: which sources are still worth fetching
# --------------------------------------------------------------------------------------------
def retired_ids(state: dict[str, Any]) -> set[str]:
    return {sid for sid, rec in state["sources"].items() if rec.get("state") == "retired"}


def due_for_probe(state: dict[str, Any], today: str) -> set[str]:
    due = set()
    for sid, rec in state["sources"].items():
        retired = rec.get("retired") or {}
        if rec.get("state") != "retired":
            continue
        last = retired.get("last_probe") or retired.get("on")
        if last and (_day(today) - _day(last)).days >= REPROBE_DAYS:
            due.add(sid)
    return due


def filter_catalog(catalog: list[Any], state: dict[str, Any], today: str, *, floor: int = 0) -> tuple[list[Any], list[str]]:
    """The catalog entries to audit today and the ids left out. Retired sources are left out except the ones due a re-probe, and
    never so many that fewer than ``floor`` remain (the audit refuses to run on a catalog below its floor)."""
    retired, probing = retired_ids(state), due_for_probe(state, today)
    skip = [sid for sid in (e.source_id for e in catalog) if sid in retired and sid not in probing]
    active = [e for e in catalog if e.source_id not in set(skip)]
    if floor and len(active) < floor <= len(catalog):  # keep the oldest retirements out, bring the most recent back
        by_recency = sorted(skip, key=lambda sid: (state["sources"][sid].get("retired") or {}).get("on", ""), reverse=True)
        restore = set(by_recency[: floor - len(active)])
        active = [e for e in catalog if e.source_id not in set(skip) or e.source_id in restore]
        skip = [sid for sid in skip if sid not in restore]
    return active, skip


# --------------------------------------------------------------------------------------------
# After the audit: update the memory and decide
# --------------------------------------------------------------------------------------------
def _entry_facts(entry: Any) -> dict[str, Any]:
    return {"name": entry.name, "url": entry.url, "bucket": entry.major_bucket, "languages": list(entry.language_tags),
            "regions": list(entry.region_tags), "topics": list(entry.topics)}


def substitutes_for(source_id: str, catalog: Iterable[Any], state: dict[str, Any], *, limit: int = 3) -> list[str]:
    """Other catalog sources that cover the same ground: same bucket, a shared language, a shared region (when the source names one),
    currently healthy. Ranked by how many topics they share. Empty when nothing else covers it."""
    entries = {e.source_id: e for e in catalog}
    me = entries.get(source_id)
    if me is None:
        return []
    scored: list[tuple[int, str]] = []
    for other in entries.values():
        if other.source_id == source_id or other.major_bucket != me.major_bucket:
            continue
        record = state["sources"].get(other.source_id, {})
        if record.get("state") in {"retired", "watching"}:
            continue
        if me.language_tags and not set(me.language_tags) & set(other.language_tags):
            continue
        if me.region_tags and not set(me.region_tags) & set(other.region_tags):
            continue
        scored.append((len(set(me.topics) & set(other.topics)), other.source_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [sid for _score, sid in scored[:limit]]


def _decide(record: dict[str, Any]) -> tuple[str, str] | None:
    """(kind, reason) when the failing streak is permanent in kind, else None."""
    streak = record.get("streak") or {}
    days = sorted(streak.get("days") or [])
    if not days:
        return None
    kinds = streak.get("kinds") or {}
    span = (_day(days[-1]) - _day(days[0])).days
    only = next(iter(kinds)) if len(kinds) == 1 else "mixed"
    if only == "paywall" and len(days) >= PAYWALL_DAYS:
        return "paywall", f"hard paywall or login wall on {len(days)} separate days since {days[0]}"
    if only == "gone" and len(days) >= GONE_DAYS and span >= GONE_SPAN:
        return "gone", f"page gone or name no longer resolving on {len(days)} days over {span} days since {days[0]}"
    if only == "blocked":
        if len(days) >= BLOCKED_DAYS and span >= BLOCKED_SPAN:
            return "blocked", f"refuses automated readers on {len(days)} days over {span} days since {days[0]} (may open in a browser)"
        return None
    if only in {"paywall", "gone"}:
        return None  # only their own, stricter-in-kind thresholds retire them
    if len(days) >= OTHER_DAYS and span >= OTHER_SPAN:
        return "unreachable", f"unreachable on {len(days)} days over {span} days since {days[0]} ({', '.join(sorted(kinds))})"
    return None


def record_results(state: dict[str, Any], sources: list[dict[str, Any]], today: str, catalog: list[Any]) -> dict[str, Any]:
    """Fold one audit's results into the state; returns what changed."""
    checked = [s for s in sources if str(s.get("status") or "")]
    kinds = {str(s["source_id"]): classify_result(s) for s in checked if s.get("source_id")}
    failed = sum(1 for k in kinds.values() if k != "ok")
    systemic = bool(kinds) and failed / len(kinds) > SYSTEMIC_DAY_FRACTION
    state["days"][today] = {"checked": len(kinds), "failed": failed, "systemic": systemic}
    for old in sorted(state["days"])[:-KEEP_DAY_RECORDS]:
        del state["days"][old]
    report: dict[str, Any] = {"date": today, "checked": len(kinds), "failed": failed, "systemic_day": systemic,
                              "newly_retired": [], "reinstated": [], "recovered": [], "newly_watched": []}
    if systemic:
        return _finish(state, report)  # a day like this counts for nothing, for or against any source
    by_id = {e.source_id: e for e in catalog}
    for sid, kind in kinds.items():
        entry = by_id.get(sid)
        record = state["sources"].setdefault(sid, {"state": "ok"})
        if entry is not None:
            record.update(_entry_facts(entry))
        history = record.setdefault("history", [])
        history.append([today, kind])
        del history[:-MAX_HISTORY]
        retired = record.get("retired") if record.get("state") == "retired" else None
        if kind == "ok":
            if retired:
                report["reinstated"].append(sid)
            elif record.get("state") == "watching":
                report["recovered"].append(sid)
            record.update({"state": "ok", "last_ok": today})
            record.pop("streak", None)
            record.pop("retired", None)
            continue
        if retired:
            retired["last_probe"] = today
            retired["probe_failures"] = int(retired.get("probe_failures", 0)) + 1
            continue
        streak = record.setdefault("streak", {"first": today, "days": [], "kinds": {}})
        if today not in streak["days"]:
            streak["days"].append(today)
            streak["kinds"][kind] = int(streak["kinds"].get(kind, 0)) + 1
        if record.get("state") != "watching":
            record["state"] = "watching"
            report["newly_watched"].append(sid)
        decision = _decide(record)
        if decision:
            retired_kind, reason = decision
            record["state"] = "retired"
            record["retired"] = {"on": today, "kind": retired_kind, "reason": reason, "last_probe": today, "probe_failures": 0}
            report["newly_retired"].append(sid)
            record.pop("streak", None)
    for sid in report["newly_retired"]:
        state["sources"][sid]["retired"]["substitutes"] = substitutes_for(sid, catalog, state)
    return _finish(state, report)


def _finish(state: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    records = state["sources"]
    report["watching"] = sorted(sid for sid, r in records.items() if r.get("state") == "watching")
    report["retired_total"] = sum(1 for r in records.values() if r.get("state") == "retired")
    report["uncovered"] = sorted(sid for sid, r in records.items()
                                 if r.get("state") == "retired" and not (r.get("retired") or {}).get("substitutes"))
    return report


# --------------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------------
def render_markdown(state: dict[str, Any], report: dict[str, Any]) -> str:
    records = state["sources"]
    lines = [f"# Source health, {report['date']}", "",
             f"{report['checked']} sources checked, {report['failed']} failed"
             + (" (a systemic day: nothing was counted for or against any source)" if report.get("systemic_day") else "") + ".",
             f"Under watch: {len(report['watching'])}. Retired in total: {report['retired_total']} "
             f"({len(report['uncovered'])} with no substitute in the catalog).", ""]

    def row(sid: str) -> str:
        rec = records.get(sid, {})
        retired = rec.get("retired") or {}
        streak = rec.get("streak") or {}
        if retired:
            subs = ", ".join(f"{records.get(s, {}).get('name', s)}" for s in retired.get("substitutes") or []) or "none: removed, no substitute"
            return f"- **{rec.get('name', sid)}** ({rec.get('url', '')}): retired {retired.get('on')} as {retired.get('kind')}, {retired.get('reason')}. Covered by: {subs}"
        days = streak.get("days") or []
        return (f"- **{rec.get('name', sid)}** ({rec.get('url', '')}): failing since {streak.get('first')}, {len(days)} day(s), "
                f"{', '.join(f'{k} x{v}' for k, v in sorted((streak.get('kinds') or {}).items()))}")

    for title, ids in (("Retired today", report["newly_retired"]), ("Reinstated today", report["reinstated"]),
                       ("Recovered today (was under watch)", report["recovered"])):
        if ids:
            lines += [f"## {title}", *[row(sid) for sid in ids], ""]
    if report["watching"]:
        lines += ["## Under watch (possibly transitory)", *[row(sid) for sid in report["watching"][:40]], ""]
    if report["uncovered"]:
        lines += ["## Retired with no substitute in the catalog (a different source for these would help)",
                  *[row(sid) for sid in report["uncovered"][:40]], ""]
    return "\n".join(lines).rstrip() + "\n"


def summary_for_report(report: dict[str, Any]) -> dict[str, Any]:
    """The small dictionary stored in the run report."""
    return {"systemic_day": report["systemic_day"], "failed": report["failed"], "watching": len(report["watching"]),
            "retired_total": report["retired_total"], "newly_retired": report["newly_retired"], "reinstated": report["reinstated"],
            "recovered": report["recovered"], "uncovered": len(report["uncovered"])}


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - operator convenience
    import argparse

    parser = argparse.ArgumentParser(description="Print the source-health state.")
    parser.add_argument("state", type=Path)
    args = parser.parse_args(argv)
    state = load_state(args.state)
    report = _finish(state, {"date": max(state["days"], default="-"), "checked": 0, "failed": 0, "systemic_day": False,
                             "newly_retired": [], "reinstated": [], "recovered": [], "newly_watched": []})
    print(render_markdown(state, report))
    return 0


__all__ = ["classify_result", "default_state", "due_for_probe", "filter_catalog", "load_state", "record_results", "render_markdown",
           "retired_ids", "save_state", "substitutes_for", "summary_for_report"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
