"""Resumable, budget-aware conductor for the briefing editions.

Why this exists. The monolithic scheduled task (one Opus session at maximum effort that read the
whole spec, spawned subagents, re-typed every fragment and ran the PDF/delivery steps) died on
usage limits, lost drafted work to a destructive assembly step, hung on unattended prompts, and
failed silently. This conductor replaces the model-as-orchestrator with a small program:

* each stage is a separate, narrowly scoped ``claude -p`` call (or plain code) with the cheapest
  adequate model and effort, and its own output file, so a run can stop and resume anywhere;
* the account's real rate-limit utilisation (reported by the CLI) is checked before and during
  the run; at the limit the run *defers* (exit 75, state saved) instead of burning calls;
* the last mile (header, assembly, validation, PDF, delivery) is deterministic and has a
  degraded path: if enough articles exist it ships a smaller briefing rather than nothing;
* every failure that needs a human, and every deferral past the deadline, raises an alert.

Exit codes: 0 delivered, 10 delivered degraded, 20 failed, 30 disabled, 75 deferred (retry later),
2 usage/configuration error.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import edition_core  # noqa: E402
import llm_backend_core as llm  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

EXIT_OK, EXIT_DEGRADED, EXIT_FAILED, EXIT_DISABLED, EXIT_USAGE, EXIT_DEFERRED, EXIT_BUSY = 0, 10, 20, 30, 2, 75, 76

# Windows has no system time-zone database: ``zoneinfo`` needs the ``tzdata`` wheel there. Without it the run
# would crash before doing anything, so the zones this pipeline is configured for fall back to fixed offsets.
_FIXED_OFFSET_HOURS = {"Asia/Tokyo": 9, "UTC": 0, "Etc/UTC": 0}


def resolve_timezone(name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, or a malformed key
            pass
    return timezone(timedelta(hours=_FIXED_OFFSET_HOURS.get(name, 0)), name=name)

DEFAULT_SETTINGS: dict[str, Any] = {
    "slate_candidates": 14, "slate_reserves": 4, "digest_items": 6, "max_parallel_articles": 2,
    "article_sections": None, "article_section": None, "digest_section": None, "article_min_total": None,
    # An article whose own Confidence line is Low is not written at all, and a story is replaced rather than shipped weak.
    "skip_low_confidence": False,
    # One repair round removes sentences about sources the writer could not open (they must not factor into the article).
    "strip_unread_mentions": False,
    # News: a short caveat attached to one specific figure or claim that is known only second-hand is information the reader needs and stays;
    # every other sentence about the writer's own access still goes (science: all of them go).
    "unread_mentions_keep_load_bearing": False,
    # {"humaninterest": "animals"}: stories of the first section are written only to fill what the second could not (low-confidence
    # or unusable stories), up to the section's maximum, and never as part of the target count.
    "fill_sections": {}, "fill_reserves": 4, "slate_reserve_slack": 2,
    # A fill-in is a story about people and animals: titles and angles matching this pattern (prizes, appointments, funding, courses)
    # and fill-ins the slate itself calls thin are dropped before anything is written.
    "fill_blocklist": "",
    # Quotations found only in a fetch summary (not in page text the script fetched) that are tolerated before a repair is asked for.
    "max_summary_only_quotes": 3,
    "section_briefs": {},
    "timezone": "Asia/Tokyo", "deadline_local": "11:30",
    "models": {
        "classify": {"model": "haiku", "effort": "low"}, "slate": {"model": "opus", "effort": "medium"},
        "article": {"model": "sonnet", "effort": "medium"}, "repair": {"model": "sonnet", "effort": "low"},
        "header": {"model": "sonnet", "effort": "low"}, "digest": {"model": "sonnet", "effort": "low"},
        "closing": {"model": "sonnet", "effort": "medium"},
        "review": {"model": "opus", "effort": "medium", "optional": True},
    },
    "budget": {
        "max_cost_usd_per_run": 40.0, "max_cost_usd_per_article": 4.0, "max_output_tokens_per_run": 1_500_000,
        "defer_if_five_hour_above": 0.80, "defer_if_seven_day_above": 0.90,
        "stop_launching_above": 0.92,
    },
    "timeouts": {"article_s": 2400, "slate_s": 1200, "header_s": 600, "digest_s": 1500, "repair_s": 1500, "probe_s": 120, "idle_s": 600},
    "repairs_per_article": 2, "call_retries": 2, "retry_backoff_s": 20, "review_enabled": False,
    "stage_timeout_s": {"assemble": 600, "validate": 600, "pdf": 900, "visual_qa": 900, "audit": 1800, "images": 900},
    "lock_stale_minutes": 240, "fallback_on_refusal": "opus", "enabled": True, "wait_for_reset_minutes": 90,
    # Script-first: what an HTTP client can fetch is fetched before the writer starts (dossier_core), and names and IUCN
    # categories are checked against GBIF after drafting (taxonomy_core). Both need network access and cost no subscription usage.
    "dossier": {"enabled": True, "max_sources": 7, "per_source_chars": 12000, "total_chars": 48000, "max_papers": 4,
                "repair_chars": 14000, "digest_sources": 2, "digest_chars": 5000},
    "taxonomy_check": True,
    # The per-section catalog-source minimum becomes a reported warning instead of a gate (see briefing_manifest_core).
    "section_sources_advisory": False,
}

NO_DOSSIER = "(the script fetched nothing for this story: research it with WebFetch and WebSearch)"


class Deferred(Exception):
    def __init__(self, reason: str, until: float | None = None, kind: str = "limit"):
        super().__init__(reason)
        self.reason, self.until, self.kind = reason, until, kind


class HumanActionNeeded(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


class StageFailed(Exception):
    def __init__(self, stage: str, reason: str):
        super().__init__(f"{stage}: {reason}")
        self.stage, self.reason = stage, reason


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------
def merge_settings(edition: edition_core.Edition) -> dict[str, Any]:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    override = edition.extra.get("conductor") or {}

    def merge(base: dict, extra: dict) -> None:
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value

    merge(settings, override)
    return settings


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(8):  # on Windows another reader (antivirus, OneDrive sync, -Diagnose) can briefly hold the target open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def render_prompt(template: str, values: dict[str, Any]) -> str:
    """Fill ``{{NAME}}`` placeholders in one pass over the *template*. Values (fetched page text among them) are never
    re-scanned, so a source page that happens to contain ``{{TITLE}}`` neither crashes the run nor gets substituted."""
    names = set(re.findall(r"\{\{([A-Z_]+)\}\}", template))
    missing = sorted(names - set(values))
    if missing:
        raise ValueError(f"unfilled prompt placeholders: {', '.join(missing)}")
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: str(values[match.group(1)]), template)


def human_date(date: str) -> str:
    dt = datetime.strptime(date, "%Y-%m-%d")
    return dt.strftime("%A, %B %d, %Y").replace(" 0", " ")


def default_runner(argv: list[str], *, env: dict[str, str], cwd: str | None, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return 124, f"timed out after {timeout:.0f}s\n{(exc.stdout or '')[-2000:] if isinstance(exc.stdout, str) else ''}"
    except FileNotFoundError as exc:
        return 127, f"executable not found: {exc}"


# --------------------------------------------------------------------------------------------
# State, lock, alerts
# --------------------------------------------------------------------------------------------
class StateStore:
    def __init__(self, path: Path, date: str, edition_id: str):
        self.path = path
        self.state = load_json(path) or {}
        if self.state.get("date") != date or self.state.get("edition") != edition_id:
            self.state = {"date": date, "edition": edition_id, "attempts": 0, "stages": {}, "events": [], "alerts_sent": [],
                          "spent_usd": 0.0}
        self._lock = threading.RLock()

    def save(self) -> None:
        with self._lock:
            self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write(self.path, json.dumps(self.state, indent=2, ensure_ascii=False) + "\n")

    def stage(self, name: str) -> dict[str, Any]:
        return self.state["stages"].setdefault(name, {"status": "pending"})

    def mark(self, name: str, status: str, **details: Any) -> None:
        with self._lock:
            entry = self.state["stages"].setdefault(name, {})
            entry.update({"status": status, "at": datetime.now(timezone.utc).isoformat(), **details})
        self.save()

    def event(self, message: str, **fields: Any) -> None:
        with self._lock:
            self.state["events"].append({"at": datetime.now(timezone.utc).isoformat(), "message": message, **fields})
            self.state["events"] = self.state["events"][-200:]

    def done(self, name: str) -> bool:
        return self.state["stages"].get(name, {}).get("status") == "done"


def pid_alive(pid: int) -> bool:
    """Is a process with this id running? (Used to tell a live run from the lock a killed run left behind.)"""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised only on the Windows host
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: the process exists
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def boot_epoch() -> float | None:
    """Seconds since the epoch at which this machine booted (None when unknown). A pid recorded before a reboot means nothing after it."""
    try:
        if os.name == "nt":  # pragma: no cover - exercised only on the Windows host
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return float(round(time.time() - kernel32.GetTickCount64() / 1000.0))
        with open("/proc/stat", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime"):
                    return float(line.split()[1])
    except Exception:  # noqa: BLE001
        return None
    return None


class FileLock:
    """One conductor per edition-day. Created atomically; a lock is taken over when its process is gone or it is
    older than ``stale_minutes`` (a hung run), so a killed run never blocks the next scheduled repetition."""

    def __init__(self, path: Path, stale_minutes: float, *, alive: Callable[[int], bool] = pid_alive):
        self.path, self.stale, self.alive = path, stale_minutes * 60, alive
        self.held = False
        self.reclaimed: dict[str, Any] | None = None

    def holder(self) -> dict[str, Any]:
        return load_json(self.path, {}) or {}

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._take_over_if_dead():
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "started_epoch": time.time(), "boot_epoch": boot_epoch()}))
            self.held = True
            return True
        return False

    def _take_over_if_dead(self) -> bool:
        info = load_json(self.path, None)
        if info is None:  # unreadable: the other process is mid-write, or the file is corrupt
            try:
                if time.time() - self.path.stat().st_mtime < 10:
                    return False
            except OSError:
                return True
            info = {}
        age = time.time() - float(info.get("started_epoch", 0) or 0)
        booted_then, booted_now = info.get("boot_epoch"), boot_epoch()
        rebooted = bool(booted_then and booted_now and abs(float(booted_then) - booted_now) > 600)  # tolerance for clock and sleep drift
        if info.get("pid") and not rebooted and self.alive(int(info["pid"])) and age < self.stale:
            return False
        self.reclaimed = info
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return True

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


class Notifier:
    """Alerts are files first (always visible on disk) and an optional command second (email via aiserver)."""

    def __init__(self, directory: Path, command: list[str] | None, runner: Callable[..., tuple[int, str]] = default_runner):
        self.directory, self.command, self.runner = directory, command, runner

    def send(self, subject: str, body: str, *, key: str) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        file = self.directory / f"ALERT-{stamp}-{re.sub(r'[^a-z0-9]+', '-', key.lower())[:40]}.txt"
        file.write_text(f"{subject}\n\n{body}\n", encoding="utf-8")
        if not self.command:
            return False
        argv = [part.replace("{subject}", subject).replace("{body_file}", str(file)) for part in self.command]
        code, output = self.runner(argv, env=dict(os.environ), cwd=None, timeout=180)
        return code == 0


# --------------------------------------------------------------------------------------------
# Conductor
# --------------------------------------------------------------------------------------------
@dataclass
class ArticleOutcome:
    story_id: str
    status: str                       # done | skipped | failed
    reason: str = ""
    words: int = 0
    attempts: int = 0
    cost_usd: float = 0.0
    provenance: dict[str, Any] = field(default_factory=dict)
    fixes: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    terminal: bool = False            # a failed verdict reached after full repair: resuming must not buy it again
    failed_passes: int = 0            # launches that ended in a failure without a verdict; two in a row make it terminal


class Conductor:
    def __init__(
        self,
        *,
        edition: edition_core.Edition,
        date: str,
        scripts_dir: Path,
        backend: llm.Backend,
        prompts_dir: Path | None = None,
        runner: Callable[..., tuple[int, str]] = default_runner,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        photo_fetcher: Any = None,
        json_fetcher: Any = None,
        url_checker: Callable[[str], int | None] | None = None,
        web_fetcher: Any = None,
        settings_override: dict[str, Any] | None = None,
        dry_run: bool = False,
        deliver: bool = True,
        check_network: bool = True,
        audit_fixture: Path | None = None,
        env: dict[str, str] | None = None,
    ):
        self.edition, self.date, self.scripts_dir, self.backend = edition, date, scripts_dir, backend
        self.prompts_dir = prompts_dir or _HERE / "prompts" / edition.edition_id
        self.runner, self.sleep = runner, sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.dry_run, self.deliver_enabled, self.check_network = dry_run, deliver, check_network
        if check_network:
            # Without these, DOI and URL provenance silently never runs in production (the checks need a fetcher).
            import photo_core
            import provenance_core

            json_fetcher = json_fetcher or photo_core.RequestsFetcher()
            url_checker = url_checker or provenance_core.default_url_checker()
            # Fewer retries and a shorter timeout than the photo fetcher: a dead source must cost seconds, not minutes.
            web_fetcher = web_fetcher or photo_core.RequestsFetcher(timeout=20.0, retries=1)
        self.web_fetcher = web_fetcher
        self._taxon_cache: dict[str, Any] = {}
        self.photo_fetcher, self.json_fetcher, self.url_checker = photo_fetcher, json_fetcher, url_checker
        self.audit_fixture = audit_fixture
        self.settings = merge_settings(edition)
        for key, value in (settings_override or {}).items():
            if isinstance(value, dict) and isinstance(self.settings.get(key), dict):
                self.settings[key].update(value)
            else:
                self.settings[key] = value
        advisory = {"BRIEFING_SECTION_SOURCES_ADVISORY": "1"} if self.settings.get("section_sources_advisory") else {}
        os.environ.update(advisory)  # in-process validators; the stage scripts get it through self.env
        self.env = {**os.environ, "BRIEFING_EDITION": edition.edition_id, **advisory, **(env or {})}
        self._stop = threading.Event()
        self._cost_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self.last_rate_limit = llm.RateLimitInfo()
        self._load_runtime()

    # ---- wiring ------------------------------------------------------------------------
    def _load_runtime(self) -> None:
        if str(self.scripts_dir) not in sys.path:
            sys.path.insert(0, str(self.scripts_dir))
        os.environ["BRIEFING_EDITION"] = self.edition.edition_id
        for key in ("DAILY_BRIEFING_ROOT", "DAILY_BRIEFING_SHARED_ROOT", "DAILY_BRIEFING_STEM"):
            if key in self.env:
                os.environ[key] = self.env[key]
        import briefing_paths as paths  # noqa: WPS433 - wrapper module from the runner scripts dir

        self.paths = paths
        self.ctx = paths.runner_context()
        self.article_section = self.settings.get("article_section") or self.edition.sections[0]
        self.digest_section = self.settings.get("digest_section") or (
            "digest" if "digest" in self.edition.sections else None)
        generated = Path(self.ctx.generated_dir)
        archive = Path(self.ctx.archive_dir)
        self.generated_dir, self.archive_dir = generated, archive
        self.state = StateStore(Path(self.ctx.conductor_state_path(self.date)), self.date, self.edition.edition_id)
        self.log_path = generated / f"{self.ctx.profile.artifact_stem}-{self.date}-conductor.log"
        self.articles_dir = Path(self.ctx.articles_dir(self.date))
        alert_cmd = os.environ.get("BRIEFING_ALERT_COMMAND")
        command = json.loads(alert_cmd) if alert_cmd else (self.edition.get("conductor.alert.command") or None)
        self.notifier = Notifier(generated / "alerts", command, self.runner)
        self.lock = FileLock(generated / f"{self.ctx.profile.artifact_stem}.conductor.lock", float(self.settings["lock_stale_minutes"]))

    # ---- section plan ------------------------------------------------------------------
    def plan(self) -> dict[str, dict[str, int]]:
        """Content sections that receive per-story articles, with quotas from the edition's section rules."""
        keys = self.settings.get("article_sections") or [self.article_section]
        out: dict[str, dict[str, int]] = {}
        for key in keys:
            rule = self.edition.rule(key)
            target = int(rule.get("target_count", 0))
            out[key] = {"min": int(rule.get("min_count", 0)), "target": target, "max": int(rule.get("max_count", target) or target)}
        return out

    def total_target(self) -> int:
        return sum(q["target"] for q in self.plan().values())

    def total_min(self) -> int:
        configured = self.settings.get("article_min_total")
        return int(configured) if configured else sum(q["min"] for q in self.plan().values())

    def section_of(self, story: dict[str, Any]) -> str:
        return str(story.get("section") or self.article_section)

    def log(self, message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        print(line, flush=True)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        self.state.event(message)

    # ---- time and budget ---------------------------------------------------------------
    def deadline(self) -> datetime | None:
        spec = self.settings.get("deadline_local")
        if not spec:
            return None
        hour, minute = (int(x) for x in str(spec).split(":"))
        tz = resolve_timezone(self.settings["timezone"])
        day = datetime.strptime(self.date, "%Y-%m-%d")
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)

    def past_deadline(self) -> bool:
        deadline = self.deadline()
        return bool(deadline and self.now() > deadline)

    def _record(self, stage: str, request: llm.LLMRequest, result: llm.LLMResult) -> None:
        with self._cost_lock, self.state._lock:
            self.state.state["spent_usd"] = round(float(self.state.state.get("spent_usd", 0.0)) + result.cost_usd, 4)
            self.state.state["output_tokens"] = int(self.state.state.get("output_tokens", 0)) + int(result.output_tokens or 0)
            if result.rate_limit.status != "unknown":
                self.last_rate_limit = result.rate_limit
                self.state.state["rate_limit"] = {
                    "status": result.rate_limit.status, "five_hour": result.rate_limit.five_hour_utilization,
                    "seven_day": result.rate_limit.seven_day_utilization, "resets_at": result.rate_limit.resets_at,
                }
        try:
            import run_reporting

            with self._usage_lock:  # parallel article workers share one run-report file
                self._record_usage_locked(run_reporting, stage, request, result)
        except Exception as exc:  # noqa: BLE001 - accounting must never break a run
            self.state.event(f"usage accounting failed: {exc!r}")
        self.state.save()

    def _record_usage_locked(self, run_reporting: Any, stage: str, request: llm.LLMRequest, result: llm.LLMResult) -> None:
        run_reporting.record_usage(
            self.date, stage, model=result.model_used or request.model, backend=self.backend.name,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens, cache_creation_tokens=result.cache_creation_tokens,
            cost_usd=result.cost_usd, duration_seconds=result.duration_s, ok=result.ok,
            error=result.error_detail if not result.ok else None, label=request.label, cost_estimated=result.cost_estimated,
        )

    def check_budget(self, *, launching: bool = True) -> None:
        budget = self.settings["budget"]
        if self._stop.is_set():
            raise Deferred("stopped by an earlier limit", until=self.last_rate_limit.resets_at)
        spent = float(self.state.state.get("spent_usd", 0.0))
        if budget.get("max_cost_usd_per_run") and spent >= float(budget["max_cost_usd_per_run"]):
            self._stop.set()
            raise Deferred(f"run budget of ${budget['max_cost_usd_per_run']:.2f} reached (${spent:.2f} spent)", kind="budget")
        # An independent cap on what the models wrote: it does not depend on the cost field the CLI reports.
        produced = int(self.state.state.get("output_tokens", 0))
        if budget.get("max_output_tokens_per_run") and produced >= int(budget["max_output_tokens_per_run"]):
            self._stop.set()
            raise Deferred(f"output-token cap of {int(budget['max_output_tokens_per_run']):,} reached ({produced:,} written)", kind="budget")
        info = self.last_rate_limit
        if launching and info.status == "rejected":
            self._stop.set()
            raise Deferred("account is rate limited", until=info.resets_at)
        if launching and (info.five_hour_utilization or 0) >= float(budget["stop_launching_above"]):
            self._stop.set()
            raise Deferred(f"five-hour utilisation {info.five_hour_utilization:.0%} is above the stop threshold",
                           until=info.resets_at)

    # ---- model calls -------------------------------------------------------------------
    def call(self, stage: str, prompt: str, *, role: str, tools: list[str] | None, label: str | None = None,
             budget_usd: float | None = None, timeout_key: str | None = None) -> llm.LLMResult:
        cfg = self.settings["models"][role]
        timeouts = self.settings["timeouts"]
        refusal_model = self.settings.get("fallback_on_refusal")
        attempt = 0
        model = cfg["model"]
        while True:
            attempt += 1
            self.check_budget()
            request = llm.LLMRequest(
                stage=stage, prompt=prompt, model=model, effort=cfg.get("effort"), tools=tools, label=label,
                max_budget_usd=budget_usd, fallback_models=[cfg["fallback"]] if cfg.get("fallback") else [],
                timeout_s=float(timeouts.get(timeout_key or f"{role}_s", 1800)), idle_timeout_s=float(timeouts.get("idle_s", 600)),
                cwd=str(self.generated_dir),
            )
            result = self.backend.run(request)
            self._record(stage, request, result)
            if result.ok:
                return result
            kind = result.error_kind or "error"
            self.log(f"{stage}{'/' + label if label else ''}: {kind}: {result.error_detail[:200]}")
            if kind in llm.HUMAN_ACTION_KINDS:
                self._stop.set()
                raise HumanActionNeeded(kind, result.error_detail or kind)
            if kind in {"usage_limit", "weekly_limit"}:
                self._stop.set()
                raise Deferred(result.error_detail or kind, until=result.reset_hint, kind=kind)
            if kind == "refusal" and refusal_model and model != refusal_model:
                self.log(f"{stage}: usage-policy block on {model}; retrying once on {refusal_model}")
                model = refusal_model
                continue
            if kind in llm.RETRYABLE_KINDS and attempt <= int(self.settings["call_retries"]):
                self.sleep(float(self.settings["retry_backoff_s"]) * attempt)
                continue
            return result

    # ---- stage runner ------------------------------------------------------------------
    def run_script(self, name: str, *args: str, timeout_key: str) -> tuple[int, str]:
        script = self.scripts_dir / name
        argv = [sys.executable, str(script), "--date", self.date, *args]
        code, output = self.runner(argv, env=dict(self.env), cwd=str(self.scripts_dir),
                                   timeout=float(self.settings["stage_timeout_s"][timeout_key]))
        self.log(f"{name} exit={code}")
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(output[-6000:] + "\n")
        except OSError:
            pass
        return code, output

    def prompt(self, name: str, values: dict[str, Any], section: str | None = None) -> str:
        """``prompts/<edition>/<name>-<section>.md`` when it exists (a section with its own brief), else ``<name>.md``."""
        path = self.prompts_dir / f"{name}-{section}.md" if section else None
        if path is None or not path.exists():
            path = self.prompts_dir / f"{name}.md"
        return render_prompt(path.read_text(encoding="utf-8"), values)

    def fill_sections(self) -> dict[str, str]:
        """{filler section: primary section it fills for}, only for sections that are part of this edition's plan."""
        plan = self.plan()
        return {str(k): str(v) for k, v in (self.settings.get("fill_sections") or {}).items() if k in plan and v in plan}

    def is_fill(self, story: dict[str, Any]) -> bool:
        return self.section_of(story) in self.fill_sections()

    # ================================================================================
    # Entry points
    # ================================================================================
    def _check_private_overlay(self) -> None:
        """Personal coverage details come from the private restore file; without it the run uses the generic public text and says so."""
        if self.edition.get("private_overlay.required") and not self.edition.get("private_overlay_applied"):
            self.log("the private coverage overlay for this edition was not found: coverage that depends on it falls back to the generic text")
            self._alert_once("private-overlay-missing", f"{self.edition.label}: private coverage overlay not found",
                             "This edition expects owner-specific coverage details from the private restore file "
                             f"(key 'edition:{self.edition.edition_id}'; see docs/RUNBOOK.md, 'Private overlay'). It was not found, so today's briefing "
                             f"runs with the generic public text ({self.edition.get('private_overlay.covers', 'regional and watch-list coverage')}). "
                             "Nothing else is affected.")

    def run(self, *, finish_only: bool = False) -> int:
        if not self.settings.get("enabled", True) or os.environ.get("BRIEFING_DISABLED") == "1":
            self._alert_once("disabled", "Briefing pipeline is disabled",
                             f"{self.edition.label} did not run on {self.date} because it is disabled "
                             "(conductor.enabled=false or BRIEFING_DISABLED=1). Remove the flag to resume.")
            return EXIT_DISABLED
        self._check_private_overlay()
        delivered = self.state.state.get("delivered")
        if delivered and delivered.get("held") and self._release_due():
            self.log("the briefing carrying this edition went out without it (or is overdue): sending this edition on its own")
            self.state.state.pop("delivered", None)
            self.state.state["stages"].pop("deliver", None)
            self.state.save()
            finish_only = True
        elif delivered:
            self.log("already delivered; nothing to do")
            return EXIT_OK
        if not self.lock.acquire():
            holder = self.lock.holder()
            self.log(f"another conductor run is active (pid {holder.get('pid')}); exiting")
            if finish_only and self.past_deadline():
                self._alert_once("busy-past-deadline", f"{self.edition.label}: still running past the deadline",
                                 f"The watchdog found the {self.date} run still active (pid {holder.get('pid')}, started "
                                 f"{datetime.fromtimestamp(float(holder.get('started_epoch') or 0), timezone.utc).isoformat()}) after the "
                                 f"{self.settings['deadline_local']} deadline and nothing delivered.\n\n" + self.diagnosis())
            return EXIT_BUSY
        if self.lock.reclaimed is not None:
            self.log(f"took over a stale lock from pid {self.lock.reclaimed.get('pid')}")
        self.state.state["attempts"] = int(self.state.state.get("attempts", 0)) + 1
        self.state.save()
        try:
            if not finish_only:
                self._pipeline()
            return self._finish(degraded=False) if not finish_only else self._finish(degraded=True)
        except Deferred as exc:
            return self._on_deferred(exc)
        except HumanActionNeeded as exc:
            self.log(f"human action needed: {exc}")
            self._alert_once(exc.kind, f"{self.edition.label}: action needed ({exc.kind})",
                             f"{exc.detail}\n\nThe run stopped on {self.date}. Fix the cause (usage limit, spend limit, or "
                             "login) and the next scheduled attempt will resume from saved state.\n\n" + self.diagnosis())
            self.state.mark("run", "blocked", reason=str(exc))
            return EXIT_FAILED
        except StageFailed as exc:
            self.log(f"stage failed: {exc}")
            self.state.mark(exc.stage, "failed", reason=exc.reason)
            return self._on_failure(exc)
        except Exception as exc:  # noqa: BLE001 - a crash must still alert and try to ship what exists
            import traceback

            trace = traceback.format_exc()
            self.log(f"unexpected crash: {exc!r}\n{trace[-1500:]}")
            self.state.mark("run", "crashed", reason=repr(exc)[:300])
            crash = StageFailed("crash", f"{type(exc).__name__}: {exc}\n\n{trace[-1800:]}")
            try:
                return self._on_failure(crash)
            except Exception as inner:  # noqa: BLE001
                self._alert_once("crash", f"{self.edition.label}: briefing crashed for {self.date}", f"{crash}\n\n{inner!r}")
                return EXIT_FAILED
        finally:
            self.state.save()
            self.lock.release()

    def _on_deferred(self, exc: Deferred) -> int:
        until = exc.until
        when = datetime.fromtimestamp(until, timezone.utc).isoformat() if until else "unknown"
        self.state.mark("run", "deferred", reason=exc.reason, until=until)
        self.log(f"deferred: {exc.reason} (retry after {when})")
        done = self._finished_articles()
        if self.past_deadline() and len(done) >= self.total_min():
            self.log("past the deadline with enough finished articles: shipping a smaller briefing")
            try:
                return self._finish(degraded=True)
            except Exception as inner:  # noqa: BLE001
                return self._on_failure(StageFailed("finish", str(inner)))
        if self.past_deadline():
            self._alert_once("late", f"{self.edition.label}: no briefing yet for {self.date}",
                             f"Deferred ({exc.reason}) and past the {self.settings['deadline_local']} deadline with only "
                             f"{len(done)} of {self.total_min()} required articles finished.\n\n" + self.diagnosis())
        return EXIT_DEFERRED

    def _on_failure(self, exc: StageFailed) -> int:
        done = self._finished_articles()
        if len(done) >= self.total_min() and exc.stage not in {"deliver", "finish"}:
            self.log("attempting degraded last-mile delivery")
            try:
                return self._finish(degraded=True)
            except Exception as inner:  # noqa: BLE001 - includes a crash inside the degraded path itself
                exc = StageFailed("finish", f"{exc.reason}; degraded attempt also failed: {inner}")
        self._alert_once(f"fail-{exc.stage}-{hashlib.sha1(exc.reason.encode('utf-8')).hexdigest()[:6]}",
                         f"{self.edition.label}: briefing failed for {self.date}", f"{exc}\n\n" + self.diagnosis())
        return EXIT_FAILED

    def _alert_once(self, key: str, subject: str, body: str) -> None:
        marker = f"{self.date}:{key}"
        if marker in self.state.state["alerts_sent"]:
            return
        sent = self.notifier.send(subject, body, key=key)
        if sent or not self.notifier.command:  # a failed email leg is retried by the next event; the file is always written
            self.state.state["alerts_sent"].append(marker)
        self.log(f"alert {key}: {'emailed' if sent else 'written to alerts folder'}")
        self.state.save()

    # ================================================================================
    # Pipeline
    # ================================================================================
    def _pipeline(self) -> None:
        self.stage_preflight()
        self.stage_audit()
        self.stage_ingest()
        self.stage_slate()
        self.stage_photos()
        self.stage_articles()
        self.stage_digest()

    def _finish(self, *, degraded: bool) -> int:
        notes: list[str] = []
        articles = self._finished_articles()
        if len(articles) < self.total_min():
            raise StageFailed("finish", f"only {len(articles)} finished article(s); minimum is {self.total_min()}")
        fill = self.fill_sections()
        primary = [a for a in articles if self.section_of(a["story"]) not in fill]
        if len(primary) < self.total_target():
            fills = len(articles) - len(primary)
            notes.append(f"{len(primary)} of {self.total_target()} target stories were finished"
                         + (f"; {fills} human-interest piece(s) fill the gap" if fills else ""))
        waived = self.state.state["stages"].get("audit", {}).get("waived")
        if waived:
            notes.append(f"the source audit was incomplete ({waived}); stories rest on the sources that could be opened")
        for section, quota in self.plan().items():
            have = sum(1 for a in articles if self.section_of(a["story"]) == section)
            if have < quota["min"]:
                notes.append(f"{self.edition.section_titles.get(section, section)}: {have} of the {quota['min']} minimum stories met the bar")
        self.stage_header(articles)
        self.stage_closing(articles)
        articles, extra_notes = self.stage_assemble_validate(articles)
        notes.extend(extra_notes)
        self.stage_pdf()
        notes.extend(self.stage_review(articles))
        combined = self.stage_combine(notes)
        degraded_run = degraded or len(articles) < self.total_target() or any("dropped" in n for n in notes)
        self.stage_deliver(articles, notes, degraded=degraded_run, combined=combined)
        return EXIT_DEGRADED if degraded_run else EXIT_OK

    # ---- stage: preflight --------------------------------------------------------------
    def stage_preflight(self) -> None:
        if self.state.done("preflight") and not self.past_stage_ttl("preflight", 3600):
            return
        self.log("preflight: probing account limits")
        if not self.dry_run:
            result = self.backend.run(llm.LLMRequest(
                stage="probe", prompt="Reply with exactly: ok", model=self.settings["models"]["classify"]["model"],
                effort="low", tools=[], max_budget_usd=0.05, timeout_s=float(self.settings["timeouts"]["probe_s"]),
                idle_timeout_s=90, cwd=str(self.generated_dir), keep_tool_corpus=False))
            self._record("preflight", llm.LLMRequest(stage="probe", prompt="", model="probe"), result)
            if not result.ok:
                if result.error_kind in llm.HUMAN_ACTION_KINDS:
                    raise HumanActionNeeded(result.error_kind or "error", result.error_detail or "probe failed")
                if result.error_kind in {"usage_limit", "weekly_limit"}:
                    raise Deferred(result.error_detail or "limit", until=result.reset_hint, kind=result.error_kind or "limit")
                raise StageFailed("preflight", f"probe call failed ({result.error_kind}): {result.error_detail[:200]}")
            budget = self.settings["budget"]
            info = result.rate_limit
            if info.status == "rejected":
                raise Deferred("account is rate limited", until=info.resets_at)
            if self.backend.name == "claude" and not info.has_utilization:
                # The 80/90/92 % guards read these numbers; without them they never fire. Keep running (an API-key account has no
                # such windows), but say so loudly and lean on the cost and output-token caps, which do not depend on them.
                self.log("preflight: the CLI reported no five-hour/seven-day utilisation; limit guards are inactive")
                self._alert_once("telemetry-missing", f"{self.edition.label}: usage-limit telemetry is missing",
                                 "The claude CLI's rate_limit_event carried no five-hour/seven-day utilisation, so the pipeline cannot see "
                                 "how close the subscription is to its limits. The run continues under the per-run cost and output-token "
                                 "caps only. If the CLI was updated, check whether its stream format changed (llm_backend_core.parse_stream).")
            if (info.five_hour_utilization or 0) >= float(budget["defer_if_five_hour_above"]):
                raise Deferred(f"five-hour window is {info.five_hour_utilization:.0%} used", until=info.resets_at)
            if (info.seven_day_utilization or 0) >= float(budget["defer_if_seven_day_above"]):
                raise Deferred(f"seven-day window is {info.seven_day_utilization:.0%} used", until=info.resets_at)
        self.state.mark("preflight", "done")

    def past_stage_ttl(self, stage: str, seconds: float) -> bool:
        at = self.state.state["stages"].get(stage, {}).get("at")
        if not at:
            return True
        return (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds() > seconds

    # ---- stage: source audit -----------------------------------------------------------
    def stage_audit(self) -> None:
        if self.state.done("audit"):
            return
        self.check_budget(launching=False)
        args: list[str] = []
        if self.audit_fixture:
            args += ["--fixture", str(self.audit_fixture)]
        code, output = self.run_script("generate_source_audit.py", *args, timeout_key="audit")
        if code not in (0, 1):  # exit 1 is "audit status failed" (systemic source failure): continue, say so, do not die
            raise StageFailed("audit", f"source audit crashed (exit {code}): {output[-300:]}")
        issue = self.audit_gate_issue()
        if issue:
            # The delivery gate (PowerShell side) refuses to send after an incomplete audit. The conductor's policy is to degrade, not to
            # die after spending the money, so the waiver is recorded where the gate looks, and the editor's note says so.
            import run_reporting

            run_reporting.update_run_report(self.date, delivery_audit_waiver=issue)
            self.log(f"source audit incomplete ({issue}); delivery gate waived, the briefing will say so")
        self.state.mark("audit", "done", exit=code, **({"waived": issue} if issue else {}))

    def audit_gate_issue(self) -> str:
        import run_reporting

        audit = run_reporting.load_run_report(self.date).get("source_audit") or {}
        if audit.get("status") not in {"complete", "fixture", "degraded"}:
            return f"audit status {audit.get('status') or 'missing'}"
        if not audit.get("all_sources_checked"):
            return "the full catalog was not checked"
        if audit.get("systemic_failure"):
            return "systemic source-check failures"
        return ""

    # ---- stage: ingest -----------------------------------------------------------------
    def prior_titles(self) -> list[str]:
        try:
            import briefing_manifest

            titles = [str(i.get("title")) for i in briefing_manifest.load_prior_items(self.date) if i.get("title")][:60]
        except Exception:  # noqa: BLE001
            titles = []
        return titles + self.recent_skip_titles()

    # ---- memory of stories that were dropped -------------------------------------------
    SKIP_MEMORY_DAYS = 10

    def _skip_memory_path(self) -> Path:
        return Path(self.paths.research_vault_dir()) / f"recent-skips-{self.edition.edition_id}.json"

    def recent_skip_titles(self) -> list[str]:
        """Titles of stories dropped in the last ten days (low confidence, sources that did not support the angle, no usable evidence).
        They are listed with the recent briefing titles, so the slate does not re-select them unless it marks a real development."""
        try:
            records = load_json(self._skip_memory_path(), []) or []
            cutoff = (datetime.fromisoformat(f"{self.date}T00:00:00") - timedelta(days=self.SKIP_MEMORY_DAYS)).date().isoformat()
            return [str(r["title"]) for r in records if r.get("title") and str(r.get("date", "")) >= cutoff and r.get("date") != self.date]
        except Exception:  # noqa: BLE001 - a memory must never stop a run
            return []

    def _story_title(self, sid: str) -> str:
        slate = load_json(Path(self.ctx.slate_path(self.date)), {}) or {}
        return next((str(s.get("title") or "") for s in list(slate.get("selected", [])) + list(slate.get("backups", [])) if s.get("story_id") == sid), "")

    def _remember_skip(self, sid: str, title: str, reason: str) -> None:
        try:
            path = self._skip_memory_path()
            cutoff = (datetime.fromisoformat(f"{self.date}T00:00:00") - timedelta(days=self.SKIP_MEMORY_DAYS * 3)).date().isoformat()
            records = [r for r in (load_json(path, []) or []) if str(r.get("date", "")) >= cutoff and r.get("story_id") != sid]
            records.append({"story_id": sid, "title": title, "reason": reason[:160], "date": self.date})
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps(records, ensure_ascii=False, indent=1) + "\n")
        except Exception as exc:  # noqa: BLE001
            self.log(f"skip memory not written: {exc!r}")

    def stage_ingest(self) -> None:
        if self.state.done("ingest"):
            return
        import briefing_manifest
        import slate_core

        audit = load_json(Path(self.paths.source_audit_path(self.date)), {})
        meta: dict[str, dict] = {}
        for name in self.edition.catalogs:
            sidecar = (_HERE / "references" / name).with_suffix(".meta.json")
            payload = load_json(sidecar, {})
            for source in audit.get("sources", []):
                entry = payload.get(str(source.get("catalog_number")))
                if entry:
                    meta[str(source.get("source_id"))] = entry
        configured = self.edition.get("ingest.keywords")
        # "default" is the animal term list; an edition with no list (the daily briefing covers everything) keeps every headline.
        keywords = tuple(slate_core.DEFAULT_KEYWORDS) if configured == "default" else tuple(configured or ())
        ingest = slate_core.build_ingest(
            audit, now=self.now(), prior_titles=self.prior_titles(), keywords=keywords, meta_by_source=meta,
            banned_check=lambda title: briefing_manifest.find_banned_topics(title, ""))
        atomic_write(Path(self.ctx.ingest_path(self.date)), json.dumps(ingest, indent=1, ensure_ascii=False) + "\n")
        atomic_write(Path(self.ctx.ingest_markdown_path(self.date)), slate_core.ingest_markdown(ingest, limit=220) + "\n")
        self.state.mark("ingest", "done", **ingest["stats"])
        self.log(f"ingest: {ingest['stats']['kept']} candidate headlines from {ingest['stats']['sources_with_items']} sources")

    # ---- stage: slate ------------------------------------------------------------------
    def spec_text(self) -> str:
        references = _HERE / "references"
        dedicated = references / f"{self.edition.edition_id}-briefing-spec.md"
        if dedicated.exists():
            parts = [dedicated.read_text(encoding="utf-8")]
        else:
            parts = [(references / name).read_text(encoding="utf-8") for name in ("briefing-spec.md", "generation-spec.md") if (references / name).exists()]
        notes = str(self.edition.get("spec_notes") or "").strip()  # owner-specific coverage, from the private overlay only
        if notes:
            parts.append("# Owner-specific coverage notes (private; binding)\n\n" + notes)
        return "\n\n---\n\n".join(parts)

    def quota_text(self) -> str:
        fill = self.fill_sections()
        return "\n".join(
            (f"- {section} ({self.edition.section_titles.get(section, section)}): reserves only, never part of the first {self.total_target()}; "
             f"used only to replace {fill[section]} stories that fail verification (up to {q['max']}); return about {self.settings['fill_reserves']}")
            if section in fill else
            f"- {section} ({self.edition.section_titles.get(section, section)}): target {q['target']}, minimum {q['min']}, maximum {q['max']}"
            for section, q in self.plan().items())

    def _gate_fill_entries(self, report: Any) -> None:
        """Keep only fill-in stories (Human Interest) that are about people and animals and are not thin."""
        fill = self.fill_sections()
        if not fill:
            return
        pattern = re.compile(str(self.settings.get("fill_blocklist") or "(?!x)x"))
        kept = []
        for entry in report.kept:
            if entry.get("section") in fill:
                text = f"{entry.get('title', '')}. {entry.get('angle', '')}"
                if pattern.search(text) or entry.get("material") == "thin":
                    report.dropped.append({"entry": entry.get("story_id"), "title": str(entry.get("title", ""))[:100],
                                           "reason": "a fill-in must be a story about people and animals with solid sources, not an award, appointment, "
                                                     "funding or course notice, and not thin"})
                    continue
            kept.append(entry)
        report.kept = kept

    def stage_slate(self) -> None:
        if self.state.done("slate") and Path(self.ctx.slate_path(self.date)).exists():
            return
        import briefing_manifest
        import slate_core

        ingest_md_path = Path(self.ctx.ingest_markdown_path(self.date))
        ingest_md = ingest_md_path.read_text(encoding="utf-8") if ingest_md_path.exists() else "(no headlines were harvested today; rely on search)"
        ingest = load_json(Path(self.ctx.ingest_path(self.date)), {"stats": {}})
        prior = self.prior_titles()
        plan = self.plan()
        values = {
            "EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": human_date(self.date), "SPEC": self.spec_text(),
            "INGEST": ingest_md, "SOURCE_COUNT": ingest.get("stats", {}).get("sources_with_items", 0),
            "PRIOR_TITLES": "\n".join(f"- {t}" for t in prior) or "- (none)", "SLATE_CANDIDATES": self.settings["slate_candidates"],
            "QUOTAS": self.quota_text(), "SECTION_BRIEFS": "\n".join(f"- {k}: {v}" for k, v in self.settings["section_briefs"].items()),
        }
        banned = lambda text: briefing_manifest.find_banned_topics(text, "")  # noqa: E731
        fill = self.fill_sections()
        multi = len(plan) - len(fill) > 1
        report, raw = None, None
        for attempt in (1, 2):
            result = self.call("slate", self.prompt("slate", values), role="slate", tools=["WebSearch"], label="slate")
            if not result.ok:
                raise StageFailed("slate", f"slate call failed ({result.error_kind}): {result.error_detail[:200]}")
            try:
                raw = slate_core.extract_json(result.text)
                report = slate_core.validate_slate(
                    raw, prior_titles=prior, banned_check=banned, sections=tuple(plan) if (multi or fill) else (),
                    default_section=None if multi else self.article_section)
            except ValueError as exc:
                self.log(f"slate attempt {attempt}: unusable output ({exc})")
                continue
            self._gate_fill_entries(report)
            if len([e for e in report.kept if e.get("section") not in fill]) >= self.total_min():
                break
            self.log(f"slate attempt {attempt}: only {len(report.kept)} valid stories")
            values["SPEC"] += f"\n\nNOTE: your previous answer had only {len(report.kept)} usable stories; return at least {self.total_min() + 4}."
        if report is None or len([e for e in report.kept if e.get("section") not in fill]) < self.total_min():
            raise StageFailed("slate", f"could not obtain {self.total_min()} valid stories")
        self._top_up_reserves(report, values, prior, banned, plan, multi)
        primary_entries = [e for e in report.kept if e.get("section") not in fill]
        if multi:
            selected, backups, notes = slate_core.select_by_sections(primary_entries, {k: v for k, v in plan.items() if k not in fill})
        else:
            quota = plan[self.article_section]
            selected, backups, notes = slate_core.select_slate(
                primary_entries, target=quota["target"], minimum=quota["min"], maximum=quota["max"])
        fillers = sorted((e for e in report.kept if e.get("section") in fill), key=lambda e: (e["material"] == "thin", e["rank"]))
        backups = backups + fillers[: int(self.settings["fill_reserves"])]
        if fillers:
            notes.append(f"{min(len(fillers), int(self.settings['fill_reserves']))} human-interest reserve(s)")
        atomic_write(Path(self.ctx.slate_path(self.date)), json.dumps(
            {"selected": selected, "backups": backups, "dropped": report.dropped, "notes": notes}, indent=1, ensure_ascii=False) + "\n")
        decisions = raw.get("no_update_decisions") if isinstance(raw, dict) else None
        if decisions:
            self._record_no_update_decisions(decisions)
        self.state.mark("slate", "done", selected=len(selected), backups=len(backups), dropped=len(report.dropped))
        self.log(f"slate: {len(selected)} selected, {len(backups)} backups, {len(report.dropped)} dropped; {'; '.join(notes) or 'balanced'}")

    def _top_up_reserves(self, report: Any, values: dict[str, Any], prior: list[str], banned: Callable[[str], Any],
                         plan: dict[str, dict[str, int]], multi: bool) -> None:
        """A story that fails verification is replaced from the reserves. A live run got ten stories back and no reserve, so four
        failures meant no briefing; ask once for the missing reserves (never fatal)."""
        import slate_core

        fill = self.fill_sections()
        wanted = self.total_target() + int(self.settings.get("slate_reserves", 4))
        have = len([e for e in report.kept if e.get("section") not in fill])
        # One reserve short is not worth a second Opus call (a live run paid one for 12 extra reserves when 1 was missing).
        if have >= wanted - int(self.settings.get("slate_reserve_slack", 2)) or self.past_deadline():
            return
        self.log(f"slate: {have} usable stories and {wanted} wanted; asking once for reserves")
        note = (f"\n\nNOTE: these stories are already chosen. Return ONLY additional stories as a JSON array in the same format, at least "
                f"{wanted - have} and at most {wanted - have + 3} of them, to serve as reserves for stories that fail verification. Thin material is acceptable for "
                "reserves. Do not repeat or rephrase any of these:\n" + "\n".join(f"- {s['title']}" for s in report.kept))
        try:
            result = self.call("slate", self.prompt("slate", {**values, "SPEC": values["SPEC"] + note}), role="slate",
                               tools=["WebSearch"], label="slate-reserves")
            if not result.ok:
                return
            extra = slate_core.validate_slate(
                slate_core.extract_json(result.text), prior_titles=prior + [s["title"] for s in report.kept], banned_check=banned,
                sections=tuple(plan) if (multi or fill) else (), default_section=None if multi else self.article_section)
            self._gate_fill_entries(extra)
        except (ValueError, Deferred, HumanActionNeeded) as exc:
            self.log(f"slate reserves unavailable: {exc}")
            return
        taken = {s["story_id"] for s in report.kept}
        added = 0
        for story in extra.kept:
            if story["story_id"] in taken:
                continue
            story["rank"] = 1000 + int(story.get("rank") or 0)  # reserves rank after every story already chosen
            report.kept.append(story)
            taken.add(story["story_id"])
            added += 1
        self.log(f"slate: {added} reserve stories added")

    def _record_no_update_decisions(self, decisions: list[Any]) -> None:
        """Beats with nothing to report (focus company, a watch-list person) are recorded, not padded; the validator reads them."""
        pool_path = Path(self.paths.candidate_pool_path(self.date))
        pool = load_json(pool_path, {}) or {}
        clean = [d for d in decisions if isinstance(d, dict) and d.get("beat") and len(str(d.get("reason") or "").strip()) >= 40]
        if clean:
            pool["no_update_decisions"] = clean
            atomic_write(pool_path, json.dumps(pool, indent=2, ensure_ascii=False) + "\n")

    def load_slate(self) -> dict[str, Any]:
        slate = load_json(Path(self.ctx.slate_path(self.date)))
        if not slate:
            raise StageFailed("slate", "slate file missing")
        return slate

    # ---- stage: photos -----------------------------------------------------------------
    def stage_photos(self) -> None:
        if self.state.done("photos"):
            return
        if self.edition.image_mode != "per_article":
            self.state.mark("photos", "done", skipped="edition does not use per-article photographs")
            return
        import photo_core

        slate = self.load_slate()
        stories = slate["selected"] + slate["backups"][:4]
        stories += [b for b in slate["backups"][4:] if self.is_fill(b)][: int(self.settings["fill_reserves"])]  # human-interest fill-ins
        image_dir = Path(self.paths.image_cache_dir(self.date))
        fetcher = self.photo_fetcher or photo_core.RequestsFetcher()
        report = photo_core.run_photo_stage(stories, image_dir=image_dir, report_path=image_dir / "image-report.json",
                                            fetcher=fetcher, edition=self.edition, date=self.date)
        atomic_write(image_dir / "photo-lines.json", json.dumps(report.pop("_outcomes"), indent=1, ensure_ascii=False) + "\n")
        self.state.mark("photos", "done", stored=report["total_images"], missing=report["stories_missing_photo"])
        self.log(f"photos: {report['total_images']} stored; missing for {len(report['stories_missing_photo'])} stories")

    def photo_lines(self, story_id: str) -> list[str]:
        lines = load_json(Path(self.paths.image_cache_dir(self.date)) / "photo-lines.json", {})
        return list(lines.get(story_id) or [])

    # ---- stage: articles ---------------------------------------------------------------
    def article_path(self, story_id: str) -> Path:
        return self.articles_dir / f"{story_id}.md"

    def article_meta_path(self, story_id: str) -> Path:
        return self.articles_dir / f"{story_id}.json"

    def _finished_articles(self, section: str | None = None) -> list[dict[str, Any]]:
        """Finished articles ordered by section then slate rank, each ``{'story', 'path', 'meta'}``; capped per section."""
        slate = load_json(Path(self.ctx.slate_path(self.date)), None)
        if not slate:
            return []
        stories = slate["selected"] + slate["backups"]
        order = {s["story_id"]: i for i, s in enumerate(stories)}
        plan = self.plan()
        section_order = {name: i for i, name in enumerate(plan)}
        out: list[dict[str, Any]] = []
        for story in stories:
            sid = story["story_id"]
            meta = load_json(self.article_meta_path(sid), {})
            if meta.get("status") == "done" and self.article_path(sid).exists() and not meta.get("dropped"):
                out.append({"story": story, "path": self.article_path(sid), "meta": meta})
        out.sort(key=lambda a: (section_order.get(self.section_of(a["story"]), 99), order[a["story"]["story_id"]]))
        capped: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for article in out:
            name = self.section_of(article["story"])
            if counts.get(name, 0) >= plan.get(name, {"max": 99})["max"]:
                continue
            counts[name] = counts.get(name, 0) + 1
            capped.append(article)
        return [a for a in capped if section is None or self.section_of(a["story"]) == section]

    def stage_articles(self) -> None:
        slate = self.load_slate()
        plan = self.plan()
        queue = list(slate["selected"])
        attempted: set[str] = set()
        replacement_rounds = 0
        for round_no in range(1, int(self.settings.get("article_rounds", 6)) + 1):
            todo = [s for s in queue if s["story_id"] not in attempted]
            if not todo:
                break
            self.log(f"articles: round {round_no}, {len(todo)} stories")
            self._write_many(todo)
            attempted.update(s["story_id"] for s in todo)
            if round_no > 1:
                replacement_rounds += 1
            queue = []
            fill = self.fill_sections()
            for section, quota in plan.items():
                if section in fill:
                    continue  # a fill section is never a target of its own
                missing = quota["target"] - len(self._finished_articles(section))
                if missing <= 0:
                    continue
                reserves = [b for b in slate["backups"] if self.section_of(b) == section and b["story_id"] not in attempted]
                # Stories the slate itself called thin come last: a human-interest piece with real sources beats a thin story that is
                # likely to end up low-confidence, and the confidence gate catches whatever thin story still gets written.
                solid = [b for b in reserves if b.get("material") != "thin"]
                thin = [b for b in reserves if b.get("material") == "thin"]
                # The first replacement round asks for exactly what is missing. If that fell short too, the next ones ask for one more: about
                # four in ten stories end in a skip, and a live run spent four rounds on two slots while thirteen solid reserves sat unused.
                chosen = solid[: missing + (1 if replacement_rounds >= 1 else 0)]
                for filler_section, primary in fill.items():
                    if primary != section or len(chosen) >= missing:
                        continue
                    room = int(plan[filler_section]["max"]) - len(self._finished_articles(filler_section)) - len(
                        [q for q in queue if self.section_of(q) == filler_section])
                    fillers = [b for b in slate["backups"] if self.section_of(b) == filler_section and b["story_id"] not in attempted]
                    chosen += fillers[: max(0, min(missing - len(chosen), room))]
                chosen += thin[: max(0, missing - len(chosen))]
                if chosen:
                    kinds = ", ".join(sorted({self.section_of(b) for b in chosen}))
                    self.log(f"articles: {section} is {missing} short of target; trying {len(chosen)} backup stories ({kinds})")
                    queue += chosen
        have = len(self._finished_articles())
        short = {sec: len(self._finished_articles(sec)) for sec, q in plan.items() if len(self._finished_articles(sec)) < q["min"]}
        self.state.mark("articles", "done", finished=have, target=self.total_target(), short_sections=short)
        if have < self.total_min():
            raise StageFailed("articles", f"only {have} article(s) finished; minimum is {self.total_min()}")

    def _write_many(self, stories: list[dict[str, Any]]) -> list[ArticleOutcome]:
        workers = max(1, int(self.settings["max_parallel_articles"]))
        outcomes: list[ArticleOutcome] = []
        first_error: Exception | None = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.write_article, s): s for s in stories}
            for future in concurrent.futures.as_completed(futures):
                try:
                    outcomes.append(future.result())
                except (Deferred, HumanActionNeeded) as exc:
                    self._stop.set()
                    first_error = first_error or exc
                except Exception as exc:  # noqa: BLE001 - one broken article must not sink the rest
                    sid = futures[future]["story_id"]
                    self.log(f"article {sid}: unexpected error {exc!r}")
                    outcomes.append(ArticleOutcome(sid, "failed", f"unexpected error: {exc!r}"))
        if first_error:
            enough = len(self._finished_articles()) >= self.total_min()
            if isinstance(first_error, Deferred) and enough:
                wait_s = (first_error.until - self.now().timestamp()) if first_error.until else None
                patience = float(self.settings["wait_for_reset_minutes"]) * 60
                if wait_s is not None and wait_s <= patience and not self.past_deadline():
                    self.log("limit resets soon; deferring so the remaining stories can be written")
                    raise first_error
                self.log(f"{first_error.kind} with {len(self._finished_articles())} finished articles; continuing with what exists")
            else:
                raise first_error
        return outcomes

    def check_article_text(self, markdown: str, section: str | None = None) -> tuple[list[str], dict[str, Any]]:
        """Run the edition's per-article rules on one article. Returns (blocking findings, manifest item)."""
        import briefing_manifest as manifest

        section = section or self.article_section
        heading, _, body = markdown.partition("\n")
        audit = manifest.load_source_audit(self.date)
        item = manifest.build_item_manifest(
            section, self.date, heading.lstrip("# ").strip(), body, prior_items=manifest.load_prior_items(self.date),
            image_metadata=manifest.load_image_metadata(self.date), source_audit=audit)
        rules = manifest.SECTION_RULES[section]
        result = manifest.evaluate_item_rules(section, item, rules, audit_available=bool(audit))
        return result["blocking"], item

    def story_dossier(self, story: dict[str, Any], *, light: bool = False):
        """Fetch the story's sources, paper records and registry facts by script (cached on disk for retries and resume)."""
        cfg = self.settings.get("dossier") or {}
        if not cfg.get("enabled", True) or self.web_fetcher is None:
            return None
        import dossier_core

        sid = story["story_id"]
        path = self.articles_dir / f"{sid}.dossier{'-light' if light else ''}.json"
        cached = load_json(path, None)
        if isinstance(cached, dict) and cached.get("story_id") == sid:
            return dossier_core.Dossier.from_dict(cached)
        registry = ""
        taxa = [t for t in story.get("taxa") or [] if isinstance(t, dict) and t.get("scientific")]
        if taxa and not light:
            try:
                import taxonomy_core

                registry = taxonomy_core.registry_facts(taxa, self.web_fetcher, cache=self._taxon_cache)
            except Exception as exc:  # noqa: BLE001 - registry outage: the writer opens the registry pages itself
                self.log(f"registry facts for {sid} unavailable: {exc!r}")
        try:
            dossier = dossier_core.build_dossier(
                story, self.web_fetcher, max_sources=int(cfg["digest_sources"] if light else cfg["max_sources"]),
                per_source_chars=int(cfg["digest_chars"] if light else cfg["per_source_chars"]),
                total_chars=int(cfg["digest_chars"] if light else cfg["total_chars"]),
                max_papers=1 if light else int(cfg["max_papers"]), registry=registry)
        except Exception as exc:  # noqa: BLE001 - never let a fetch problem stop an article
            self.log(f"dossier for {sid} failed: {exc!r}")
            return None
        atomic_write(path, json.dumps(dossier.to_dict(), ensure_ascii=False))
        self.log(f"dossier {sid}{' (light)' if light else ''}: {dossier.summary()}")
        return dossier

    def check_taxonomy(self, article: str):
        if self.web_fetcher is None or not self.settings.get("taxonomy_check", True):
            return None
        try:
            import taxonomy_core

            return taxonomy_core.check_article_taxonomy(article, self.web_fetcher, cache=self._taxon_cache)
        except Exception as exc:  # noqa: BLE001 - a registry outage must never fail an article
            self.log(f"taxonomy check skipped: {exc!r}")
            return None

    def draft_path(self, story_id: str) -> Path:
        return self.articles_dir / f"{story_id}.draft.json"

    def _trusted_tool_corpus(self, result: llm.LLMResult, outcome: ArticleOutcome) -> list[str]:
        """What the writer's tools returned counts as evidence only if every address it opened is public. A writer steered (by a
        planted page) to the desktop's own network gets its results discarded, the article flagged and an alert raised."""
        if not self.check_network:
            return list(result.tool_corpus)
        import net_safety_core

        bad: list[str] = []
        for url in dict.fromkeys(result.fetched_urls):
            try:
                net_safety_core.check_public_url(url)
            except net_safety_core.UnsafeUrl as exc:
                bad.append(f"{url[:100]} ({exc})")
        if not bad:
            return list(result.tool_corpus)
        outcome.flags.append(f"the writer opened non-public address(es); its tool results were discarded: {'; '.join(bad)[:300]}")
        self.log(f"article {outcome.story_id}: {outcome.flags[-1]}")
        self._alert_once("unsafe-fetch", f"{self.edition.label}: a writer opened a non-public address",
                         f"Story {outcome.story_id}: {'; '.join(bad)[:600]}\n\nThe fetched text was not used as evidence. A source page may be trying to "
                         "steer the model toward the local network; review the story's sources.")
        return []

    def _save_draft(self, sid: str, article: str, corpus: list[str], outcome: ArticleOutcome) -> None:
        """The paid-for draft survives a limit or a crash: resuming goes straight to the repair loop, not back to $0.28 of writing."""
        atomic_write(self.draft_path(sid), json.dumps(
            {"article": article, "corpus": corpus, "attempts": outcome.attempts, "cost_usd": round(outcome.cost_usd, 4),
             "fixes": outcome.fixes}, ensure_ascii=False))

    def write_article(self, story: dict[str, Any]) -> ArticleOutcome:
        import article_tools_core as tools
        import photo_core
        import provenance_core

        sid = story["story_id"]
        section = self.section_of(story)
        existing = load_json(self.article_meta_path(sid), {})
        if existing.get("status") in {"done", "skipped"} or (existing.get("status") == "failed" and existing.get("terminal")):
            return ArticleOutcome(sid, existing["status"], existing.get("reason", ""), existing.get("words", 0),
                                  attempts=int(existing.get("attempts", 0)), cost_usd=float(existing.get("cost_usd", 0.0)),
                                  terminal=bool(existing.get("terminal")))
        budget = float(self.settings["budget"]["max_cost_usd_per_article"])
        values = {
            "EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": human_date(self.date), "SPEC": self.spec_text(),
            "STORY_JSON": json.dumps({k: v for k, v in story.items() if k not in {"photo_allow_words"}}, indent=1, ensure_ascii=False),
            "PRIOR_TITLES": "\n".join(f"- {t}" for t in self.prior_titles()[:30]) or "- (none)",
            "WORDS_TARGET": int(self.edition.rule(section).get("min_words", 1500)), "STORY_ID": sid,
            "SECTION": section, "SECTION_TITLE": self.edition.section_titles.get(section, section),
            "SECTION_BRIEF": self.settings["section_briefs"].get(section, ""),
            "PRIOR_MARKER": f"; prior: {story['prior_story_id']}" if story.get("prior_story_id") else "",
            "DOSSIER": NO_DOSSIER,
        }
        outcome = ArticleOutcome(sid, "failed", failed_passes=int(existing.get("failed_passes", 0)))
        corpus: list[str] = []
        draft = load_json(self.draft_path(sid), None)
        if isinstance(draft, dict) and draft.get("article"):
            article = str(draft["article"])
            corpus = [str(c) for c in draft.get("corpus") or []]
            outcome.attempts, outcome.cost_usd = int(draft.get("attempts", 1)), float(draft.get("cost_usd", 0.0))
            outcome.fixes = list(draft.get("fixes") or [])
            self.log(f"article {sid}: resuming from the saved draft")
            dossier = self.story_dossier(story)
        else:
            if self.past_deadline() and len(self._finished_articles()) >= self.total_min():
                raise Deferred("past the deadline; no new articles are started", kind="deadline")
            dossier = self.story_dossier(story)
            if dossier:
                values["DOSSIER"] = dossier.render()
                corpus += dossier.corpus()
            result = self.call("article", self.prompt("article", values, section), role="article", tools=["WebFetch", "WebSearch"],
                               label=sid, budget_usd=budget)
            outcome.attempts, outcome.cost_usd = 1, result.cost_usd
            if not result.ok:
                outcome.reason = f"{result.error_kind}: {result.error_detail[:160]}"
                self._save_article_meta(sid, outcome)
                return outcome
            corpus += self._trusted_tool_corpus(result, outcome)
            raw = result.text.strip()
            atomic_write(self.articles_dir / f"{sid}.raw.md", raw + "\n")
            skip = tools.skip_reason(raw)
            if skip:
                outcome.status, outcome.reason = "skipped", skip[:200]
                self._save_article_meta(sid, outcome)
                return outcome
            try:
                article, fixes = tools.normalize_article(raw, story)
            except ValueError as exc:
                outcome.reason = f"unusable output: {exc}"
                self._save_article_meta(sid, outcome)
                return outcome
            outcome.fixes += fixes
            self._save_draft(sid, article, corpus, outcome)
        if self._skip_low_confidence(article, sid, outcome):
            return outcome  # before any repair is paid for
        article = photo_core.insert_photos(article, self.photo_lines(sid))
        repair_dossier = dossier.render(total_chars=int(self.settings["dossier"]["repair_chars"])) if dossier else NO_DOSSIER

        tax = None
        for round_no in range(int(self.settings["repairs_per_article"]) + 1):
            blocking, item = self.check_article_text(article, section)
            # Quotations are verified against page text the script fetched; what a fetch call *summarised* counts for figures only.
            prov = provenance_core.check_article(
                article, corpus, json_fetcher=self.json_fetcher, url_checker=self.url_checker, check_network=self.check_network,
                verbatim_corpus=dossier.corpus() if dossier else None,
                summary_only_limit=int(self.settings.get("max_summary_only_quotes", 3)))
            tax = self.check_taxonomy(article)
            tax_bad = bool(tax and tax.needs_repair())
            word_only = bool(blocking) and all("hard word floor" in b for b in blocking)
            unread = tools.find_unread_source_mentions(article) if (self.settings.get("strip_unread_mentions") and round_no == 0) else []
            if not blocking and not prov.needs_repair() and not tax_bad and not unread:
                break
            if word_only and round_no >= 1:
                break  # repairs cannot create honest material; let the story be replaced
            if round_no >= int(self.settings["repairs_per_article"]):
                break
            findings = [f"- {b}" for b in blocking]
            if prov.needs_repair():
                findings.append("Provenance check (could not be matched to any source opened in this run):\n" + prov.repair_brief())
            if tax_bad:
                findings.append("Taxonomy and status check (script lookup against the GBIF backbone):\n" + tax.repair_brief())
            if unread:
                keep = ("Keep a sentence that is about the world (for example 'age data were not available for 12 animals'), and keep one short caveat "
                        "that is attached to a specific figure or claim you state and that is known only second-hand (say who reported it and that the "
                        "original could not be checked, in that claim's own sentence); remove every other sentence about your access, and any claim that "
                        "rests only on a source you could not open and is not carried by an outlet you did read"
                        if self.settings.get("unread_mentions_keep_load_bearing") else
                        "remove each one that is about your access to a source, and the claims that rest only on it. Keep a sentence that is about "
                        "the study or the world (for example 'age data were not available for 12 animals')")
                findings.append("Sentences about sources you could not open (a source you could not read must not factor into the article): "
                                + keep + ":\n" + "\n".join(f'- "{sentence}"' for sentence in unread))
            if word_only:
                findings.append("The article is below the hard word floor. Add only real, sourced material (open more sources); "
                                "if there is not enough genuine material, return SKIP: material_limited.")
            fix = self.call("repair", self.prompt("repair", {**values, "DOSSIER": repair_dossier, "FINDINGS": "\n".join(findings), "ARTICLE": article},
                                                  section),
                            role="repair", tools=["WebFetch", "WebSearch"], label=sid, budget_usd=budget,
                            timeout_key="repair_s")
            outcome.attempts += 1
            outcome.cost_usd += fix.cost_usd
            if not fix.ok:
                outcome.flags.append(f"repair call failed: {fix.error_kind}")
                break
            corpus += self._trusted_tool_corpus(fix, outcome)
            fix_skip = tools.skip_reason(fix.text)
            if fix_skip:
                outcome.status, outcome.reason = "skipped", fix_skip[:200]
                self._save_article_meta(sid, outcome)
                return outcome
            try:
                article, more = tools.normalize_article(fix.text, story)
            except ValueError:
                outcome.flags.append("repair returned unusable output")
                break
            outcome.fixes += more
            article = photo_core.insert_photos(article, self.photo_lines(sid))
            self._save_draft(sid, article, corpus, outcome)
            if self._skip_low_confidence(article, sid, outcome):
                return outcome  # a repair that leaves the writer unsure of its own conclusion does not make it publishable

        outcome.words = int(item.get("word_count", 0))
        outcome.provenance = prov.to_dict()
        outcome.provenance["unread_mentions"] = len(tools.find_unread_source_mentions(article))  # what is left after repair, for the trend
        if tax is not None:
            outcome.provenance["taxonomy"] = tax.to_dict()
            if tax.needs_repair():
                outcome.flags.append(f"taxonomy: {len(tax.misspellings)} name(s) and {len(tax.status_mismatches)} status entr(ies) not confirmed against GBIF")
        outcome.blocking = blocking
        fatal_dois = [doi for doi, info in prov.dois.items() if info.get("status") == "not_found"]
        if prov.needs_repair() or prov.reader_should_be_told():
            outcome.flags.append(
                f"unverified after repair: {len(prov.unmatched_numbers)} number(s), {len(prov.unmatched_quotes)} quotation(s), "
                f"{len(prov.summary_only_quotes)} quotation(s) seen only in a fetch summary, "
                f"{len(prov.bad_dois)} DOI(s), {len(prov.dead_urls)} dead URL(s)")
        # A repair that never ran (call failed, unusable output) is a transient fault: keep the draft and retry on resume.
        # A verdict reached after repair is deterministic: marking it terminal stops a resume from paying for it again.
        transient = any(f.startswith(("repair call failed", "repair returned unusable")) for f in outcome.flags)
        if blocking:
            word_only = all("hard word floor" in b for b in blocking)
            outcome.status = "skipped" if word_only else "failed"
            outcome.reason = "material_limited: " + "; ".join(blocking)[:200] if word_only else "; ".join(blocking)[:300]
            outcome.terminal = outcome.status == "failed" and not transient
        elif fatal_dois or prov.unmatched_quotes:
            outcome.status, outcome.reason = "failed", "citations or quotations could not be verified"
            outcome.terminal = not transient
        else:
            outcome.status = "done"
            atomic_write(self.article_path(sid), article)
        if outcome.status == "failed":
            atomic_write(self.articles_dir / f"{sid}.last.md", article)  # what the writer produced, for diagnosis; never shipped
        self._save_article_meta(sid, outcome)
        self.log(f"article {sid}: {outcome.status} ({outcome.words} words, ${outcome.cost_usd:.2f}"
                 f"{', ' + '; '.join(outcome.flags) if outcome.flags else ''})")
        return outcome

    def _skip_low_confidence(self, article: str, sid: str, outcome: ArticleOutcome) -> bool:
        """An article whose own Confidence line says Low (including "Low to medium") is not worth the reader's time: it is dropped and
        the story replaced, never shipped with an apology."""
        if not self.settings.get("skip_low_confidence"):
            return False
        import article_tools_core as tools

        if tools.confidence_rating(article) != "low":
            return False
        outcome.status, outcome.terminal = "skipped", False
        outcome.reason = f"low_confidence: {tools.confidence_line(article)[:200]}"
        self.log(f"article {sid}: skipped, the writer's own confidence is low ({tools.confidence_line(article)[:120]})")
        atomic_write(self.articles_dir / f"{sid}.last.md", article)  # kept for diagnosis; never shipped
        self._save_article_meta(sid, outcome)
        return True

    def _save_article_meta(self, sid: str, outcome: ArticleOutcome) -> None:
        if outcome.status == "failed" and not outcome.terminal:
            # Every 30-minute repetition would otherwise buy the same broken story again (3 attempts x the per-article budget).
            outcome.failed_passes += 1
            outcome.terminal = outcome.failed_passes >= 2
        atomic_write(self.article_meta_path(sid), json.dumps(outcome.__dict__, indent=1, ensure_ascii=False) + "\n")
        if outcome.status == "skipped" or (outcome.status == "failed" and outcome.terminal):
            title = self._story_title(sid)
            if title:
                self._remember_skip(sid, title, outcome.reason)
        self.state.mark(f"article:{sid}", outcome.status, words=outcome.words, cost=round(outcome.cost_usd, 3))
        if outcome.status in {"done", "skipped"}:
            self.draft_path(sid).unlink(missing_ok=True)  # a terminal failure keeps its draft (and the evidence corpus) for diagnosis

    # ---- stage: digest -----------------------------------------------------------------
    def stage_digest(self) -> None:
        if not self.digest_section or self.state.done("digest"):
            return
        if self.past_deadline() or int(self.settings["digest_items"]) <= 0:
            self.state.mark("digest", "done", items=0, skipped="deadline or disabled")
            return
        import article_tools_core as tools

        slate = self.load_slate()
        used = {a["story"]["story_id"] for a in self._finished_articles()}
        attempted = {sid for sid in (p.stem for p in self.articles_dir.glob("*.json"))}
        pool = [s for s in slate["backups"] if s["story_id"] not in used and s["story_id"] not in attempted and not self.is_fill(s)]
        pool = pool[: int(self.settings["digest_items"]) + 2]
        dossiers: dict[str, Any] = {}
        if self.web_fetcher is not None:
            # Script-first: the pages are fetched here, so the writer needs no tools and cannot spend turns on fetching.
            for story in pool:
                found = self.story_dossier(story, light=True)
                if found and (found.summary()["sources_ok"] or found.papers):
                    dossiers[story["story_id"]] = found
            pool = [s for s in pool if s["story_id"] in dossiers]
        if not pool:
            self.state.mark("digest", "done", items=0)
            return
        cfg = self.settings["dossier"]
        values = {
            "EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": human_date(self.date), "SPEC": self.spec_text(),
            "ITEMS_JSON": json.dumps([{k: s[k] for k in ("story_id", "title", "angle", "why_now", "taxa", "sources")} for s in pool],
                                     indent=1, ensure_ascii=False),
            "DIGEST_ITEMS": self.settings["digest_items"],
            "DOSSIERS": "\n\n".join(f"### Dossier for {sid}\n{d.render(total_chars=int(cfg['digest_chars']), max_sources=int(cfg['digest_sources']))}"
                                     for sid, d in dossiers.items()) or NO_DOSSIER,
        }
        try:
            result = self.call("digest", self.prompt("digest", values), role="digest",
                               tools=[] if dossiers else ["WebFetch", "WebSearch"], label="digest",
                               budget_usd=float(self.settings["budget"]["max_cost_usd_per_article"]))
        except Deferred:
            self.state.mark("digest", "done", items=0, skipped="deferred")
            return
        items: list[str] = []
        if result.ok:
            import briefing_manifest as manifest
            import provenance_core

            by_id = {s["story_id"]: s for s in pool}
            for heading, body in manifest.split_items("\n" + result.text):
                marker = manifest.STORY_MARKER_RE.search(body)
                sid = marker.group("id") if marker else None
                story = by_id.get(sid) or {"story_id": manifest.story_slug(heading), "title": heading, "prior_story_id": None}
                try:
                    text, _ = tools.normalize_article(f"## {heading}\n{body}", story)
                except ValueError:
                    continue
                blocking, _item = self._check_section_item(self.digest_section, text)
                if blocking:
                    self.log(f"digest item {story['story_id']} dropped: {'; '.join(blocking)[:160]}")
                    continue
                if sid in dossiers:  # the writer had no tools: every figure and quotation must come from its dossier
                    prov = provenance_core.check_article(text, dossiers[sid].corpus(), check_network=False)
                    if prov.unmatched_quotes or prov.needs_repair() or len(prov.unmatched_numbers) >= 3:
                        self.log(f"digest item {sid} dropped: figures or quotations not found in its dossier")
                        continue
                items.append(text)
        atomic_write(self.articles_dir / "digest.json", json.dumps({"items": items}, ensure_ascii=False))
        self.state.mark("digest", "done", items=len(items))

    def _check_section_item(self, section: str, markdown: str) -> tuple[list[str], dict]:
        import briefing_manifest as manifest

        heading, _, body = markdown.partition("\n")
        audit = manifest.load_source_audit(self.date)
        item = manifest.build_item_manifest(section, self.date, heading.lstrip("# ").strip(), body,
                                            prior_items=manifest.load_prior_items(self.date),
                                            image_metadata=manifest.load_image_metadata(self.date), source_audit=audit)
        result = manifest.evaluate_item_rules(section, item, manifest.SECTION_RULES[section], audit_available=bool(audit))
        return result["blocking"], item

    def _synthesis_unsupported(self, text: str, articles: list[dict[str, Any]]) -> str:
        """The header and the closing analysis may only restate what the day's articles say. Checked with the articles themselves as
        the corpus (no network): an invented quotation, or a short text of figures none of the articles contain, is reported."""
        import provenance_core

        try:
            corpus = [a["path"].read_text(encoding="utf-8") for a in articles]
        except OSError:
            return ""
        prov = provenance_core.check_article(text, corpus, check_network=False)
        problems = []
        if prov.unmatched_quotes:
            problems.append(f"{len(prov.unmatched_quotes)} quotation(s) found in no article")
        # Counting words ("17 stories", "the 3 sections") are the synthesis's own arithmetic, not claims about the world.
        ceiling = len(articles) + 10
        remaining = [n for n in prov.unmatched_numbers if not (n["text"].strip().isdigit() and int(n["text"].strip()) <= ceiling)]
        if len(remaining) >= 3 and len(remaining) / max(prov.numbers_checked, 1) > 0.5:
            problems.append("figures found in no article: " + ", ".join(n["text"] for n in remaining[:5]))
        return "; ".join(problems)

    # ---- stage: header -----------------------------------------------------------------
    def part_label(self, section: str) -> str:
        """"PART II - AI NEWS" -> "Part II" (the cross-reference prefix the daily spec asks for); empty for other editions."""
        match = re.match(r"(?i)^part\s+([ivx]+)\b", self.edition.section_titles.get(section, ""))
        return f"Part {match.group(1).upper()}" if match else ""

    def summaries_for(self, articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import article_tools_core as tools

        out = []
        for a in articles:
            summary = tools.article_summary(a["path"].read_text(encoding="utf-8"), a["story"])
            summary["part"] = self.part_label(self.section_of(a["story"]))
            out.append(summary)
        return out

    def stage_header(self, articles: list[dict[str, Any]]) -> None:
        import article_tools_core as tools

        summaries = self.summaries_for(articles)
        titles = [s["title"] for s in summaries]
        key = hashlib.sha1("|".join(titles).encode("utf-8")).hexdigest()[:12]
        previous = self.state.state["stages"].get("header", {})
        header_path = Path(self.paths.fragment_path("header", self.date))
        if (previous.get("status") == "done" and previous.get("key") == key and header_path.exists()
                and bool(previous.get("fallback")) == (previous.get("source") == "fallback")):
            self.log("header: reusing the accepted header (same stories)")  # every re-entry of the last mile used to buy a new header call
            return
        low, high = (int(v) for v in self.edition.header.get("exec_summary_words", [250, 750]))
        min_cross = int(self.edition.header.get("min_cross_references", 2))
        cross_label = str(self.edition.header.get("cross_reference_label", "Cross-currents"))
        frame_date = human_date(self.date)
        prepared = getattr(self.ctx.profile, "prepared_by_line", "Prepared by Claude")
        frame = lambda middle: (f"# {self.edition.label}\n**{frame_date}** | {prepared}\n\n{middle.strip()}\n\n### Table of Contents\n\n"  # noqa: E731
                                + "\n".join(f"{i}. {t}" for i, t in enumerate(titles, start=1)) + "\n")
        header_text, source = None, "model"
        use_parts = any(s["part"] for s in summaries)
        if not self.past_deadline() and not self.state.state["stages"].get("header", {}).get("fallback"):
            try:
                margin = int((high - low) * 0.15)
                values = {
                    "EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": frame_date,
                    "SUMMARIES": json.dumps(summaries, indent=1, ensure_ascii=False), "SUMMARY_LOW": low + margin,
                    "SUMMARY_HIGH": high - margin, "MIN_CROSS": min_cross, "CROSS_LABEL": cross_label,
                    "CROSS_FORMAT": ('Cross-reference: <part> - "<exact article title>" using the story\'s "part" value (for example Cross-reference: Part II - "Title")'
                                     if use_parts else 'Cross-reference: "<exact article title>"'),
                }
                result = self.call("header", self.prompt("header", values), role="header", tools=[], label="header")
                if result.ok:
                    middle, fixes = tools.repair_cross_references(result.text.strip(), titles)
                    words = len(middle.split(f"### {cross_label}")[0].replace("## Executive Summary", "").split())
                    refs = tools.extract_header_titles(middle)
                    unsupported = self._synthesis_unsupported(middle, articles)
                    if unsupported:
                        self.log(f"header: model output rejected (not supported by the articles: {unsupported}); using fallback")
                    elif low <= words <= high and len(refs) >= min_cross and all(r in titles for r in refs):
                        header_text = frame(middle)
                    else:
                        self.log(f"header: model output rejected (words={words}, refs={len(refs)}); using fallback")
            except (Deferred, HumanActionNeeded):
                self.log("header: model unavailable; using fallback")
        if header_text is None:
            source = "fallback"
            header_text = tools.deterministic_header(label=self.edition.label, date_human=frame_date, prepared_by=prepared,
                                                     summaries=summaries, summary_words=(low, high), cross_label=cross_label,
                                                     min_cross=min_cross)
        atomic_write(Path(self.paths.fragment_path("header", self.date)), header_text)
        self.state.mark("header", "done", source=source, fallback=source == "fallback", key=key)

    # ---- stage: closing analysis (editions with a closing section) ---------------------------
    def stage_closing(self, articles: list[dict[str, Any]]) -> None:
        closing = self.edition.closing_section
        if not closing or "closing" not in {p.stem for p in self.prompts_dir.glob("*.md")}:
            return
        path = self.articles_dir / "closing.md"
        if path.exists():
            return
        import article_tools_core as tools

        story = {"story_id": "closing-analysis", "title": "Closing analysis", "prior_story_id": None}
        summaries = self.summaries_for(articles)
        for summary, article in zip(summaries, articles):
            text = article["path"].read_text(encoding="utf-8")
            summary["sources"] = re.findall(r"https?://[^\s)>\]]+", text.split("### Primary Sources")[-1])[:5]
        values = {
            "EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": human_date(self.date), "SPEC": self.spec_text(),
            "SUMMARIES": json.dumps(summaries, indent=1, ensure_ascii=False),
            "WORDS_TARGET": int(self.edition.rule(closing).get("min_words", 1200)),
        }
        text = None
        if not self.past_deadline():
            findings = ""
            for attempt in (1, 2):
                try:
                    result = self.call("closing", self.prompt("closing", {**values, "FINDINGS": findings or "(none)"}), role="closing",
                                       tools=[], label="closing", timeout_key="header_s")
                except (Deferred, HumanActionNeeded):
                    break
                if not result.ok:
                    break
                try:
                    candidate, _ = tools.normalize_article(result.text, story)
                except ValueError:
                    findings = "The output had no `## Title` heading."
                    continue
                blocking, _item = self._check_section_item(closing, candidate)
                unsupported = self._synthesis_unsupported(candidate, articles)
                if unsupported:
                    blocking = blocking + [f"The analysis states things its source articles do not support: {unsupported}. "
                                           "Use only figures and quotations that appear in the summaries and articles."]
                if not blocking:
                    text = candidate
                    break
                findings = "\n".join(f"- {b}" for b in blocking)
                self.log(f"closing attempt {attempt}: {'; '.join(blocking)[:200]}")
        if text is None:
            text = ("## Quality Failure: the closing analysis could not be produced to standard today\n\n"
                    "The stories above stand on their own sources; no synthesis is offered rather than a padded one.\n")
        atomic_write(path, text)
        self.state.mark("closing", "done", fallback=text.startswith("## Quality Failure"))

    # ---- stage: assemble + validate ----------------------------------------------------
    def _write_fragments(self, articles: list[dict[str, Any]]) -> None:
        import article_tools_core as tools

        titles = self.edition.section_titles
        for section, quota in self.plan().items():
            mine = [a for a in articles if self.section_of(a["story"]) == section]
            bodies = [a["path"].read_text(encoding="utf-8") for a in mine]
            if len(mine) < quota["min"] and quota["min"] > 0:
                bodies.append(f"## Quality Failure: only {len(mine)} of the {quota['min']} minimum stories met the evidence bar for "
                              f"{titles[section]} today\n\nA visible gap is reported instead of padding a weak article.\n")
            if bodies:
                fragment = tools.compose_section(titles[section], bodies)
            else:  # a fill-in section, or one the edition lets disappear, with nothing in it shows no empty heading
                hidden = section in self.fill_sections() or bool(self.edition.rule(section).get("omit_when_empty"))
                fragment = "" if hidden else f"# {titles[section]}\n"
            atomic_write(Path(self.paths.fragment_path(section, self.date)), fragment)
        if self.digest_section:
            digest = load_json(self.articles_dir / "digest.json", {}).get("items", [])
            fragment = tools.compose_section(titles[self.digest_section], digest) if digest else f"# {titles[self.digest_section]}\n"
            atomic_write(Path(self.paths.fragment_path(self.digest_section, self.date)), fragment)
        closing = self.edition.closing_section
        if closing:
            path = self.articles_dir / "closing.md"
            body = path.read_text(encoding="utf-8") if path.exists() else (
                "## Quality Failure: closing analysis was not produced\n\nNo synthesis was written for this run.\n")
            atomic_write(Path(self.paths.fragment_path(closing, self.date)), tools.compose_section(titles[closing], [body]))

    def _fetch_images(self) -> None:
        """Single-image editions pick one validated source image after drafting (non-fatal: a failure marker is shipped)."""
        if self.edition.image_mode != "single":
            return
        code, _output = self.run_script("fetch_briefing_images.py", timeout_key="images")
        if code != 0:
            self.log("image fetch did not complete; the briefing will carry an image-quality failure marker")

    def stage_assemble_validate(self, articles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        notes: list[str] = []
        dropped: list[str] = []
        for attempt in range(1, 4):
            self._write_fragments(articles)
            self._fetch_images()
            code, output = self.run_script("assemble_briefing.py", timeout_key="assemble")
            if code != 0:
                raise StageFailed("assemble", f"assembly failed (exit {code}): {output[-300:]}")
            code, output = self.run_script("validate_briefing.py", timeout_key="validate")
            if code == 0:
                self.state.mark("validate", "done", attempt=attempt)
                if dropped:
                    notes.append("dropped after validation: " + "; ".join(dropped))
                short = [w for w in (load_json(Path(self.paths.validation_report_path(self.date)), {}).get("warnings") or [])
                         if "catalog pool" in w]
                if short:
                    notes.append(f"{len(short)} article(s) cite fewer catalog sources than the section minimum (advisory)")
                return articles, notes
            report = load_json(Path(self.paths.validation_report_path(self.date)), {})
            blocking = report.get("blocking_errors") or []
            # Errors arrive as "<section>/<title>: ..." and, from the research lint, "Research lint <rule>: <section>/<title>: ...".
            offenders = {a["story"]["story_id"] for a in articles if self._errors_for(a, blocking)}
            self.log(f"validation blocked ({len(blocking)} errors); offending articles: {sorted(offenders) or 'none'}")
            remaining = [a for a in articles if a["story"]["story_id"] not in offenders]
            if offenders and len(remaining) >= self.total_min() and attempt < 3:
                for a in articles:
                    if a["story"]["story_id"] in offenders:
                        dropped.append(a["story"]["story_id"])
                        meta = load_json(self.article_meta_path(a["story"]["story_id"]), {})
                        meta["dropped"] = "validation: " + "; ".join(self._errors_for(a, blocking))[:300]
                        atomic_write(self.article_meta_path(a["story"]["story_id"]), json.dumps(meta, indent=1))
                articles = remaining
                self.stage_header(articles)
                continue
            if not offenders and attempt == 1 and any("Header" in e or "Table of Contents" in e for e in blocking):
                self.state.state["stages"].setdefault("header", {})["fallback"] = True
                self.stage_header(articles)
                continue
            raise StageFailed("validate", "; ".join(blocking)[:600] or f"validation failed (exit {code})")
        raise StageFailed("validate", "validation did not converge")

    def _title_of(self, article: dict[str, Any]) -> str:
        import briefing_manifest as manifest

        first = article["path"].read_text(encoding="utf-8").split("\n", 1)[0]
        return manifest.normalize_item_title(first.lstrip("# ").strip())

    def _errors_for(self, article: dict[str, Any], errors: list[str]) -> list[str]:
        needle = f"{self.section_of(article['story'])}/{self._title_of(article)}:"
        return [e for e in errors if needle in e]

    # ---- stage: PDF + visual QA --------------------------------------------------------
    def stage_pdf(self) -> None:
        code, output = self.run_script("build_pdf.py", timeout_key="pdf")
        if code != 0:
            raise StageFailed("pdf", f"PDF build failed (exit {code}): {output[-300:]}")
        self.state.mark("pdf", "done")
        code, output = self.run_script("visual_qa.py", timeout_key="visual_qa")
        if code != 0:
            raise StageFailed("visual_qa", f"visual QA failed (exit {code}): {output[-300:]}")
        self.state.mark("visual_qa", "done")

    # ---- stage: review (advisory) ------------------------------------------------------
    def stage_review(self, articles: list[dict[str, Any]]) -> list[str]:
        if not self.settings.get("review_enabled") or self.past_deadline() or self.dry_run:
            return []
        if "review" not in self.settings["models"] or not (self.prompts_dir / "review.md").exists():
            return []
        import slate_core

        parts = []
        for a in articles:
            text = a["path"].read_text(encoding="utf-8")
            keep = []
            for heading in ("Bottom line", "Confidence", "Taxonomy & Status", "The evidence", "Counterpoint Matrix"):
                m = re.search(rf"(?ms)^(?:\*\*{re.escape(heading)}:?\*\*.*?$|###\s+{re.escape(heading)}\s*$.*?)(?=^###\s|^\*\*|\Z)", text)
                if m:
                    keep.append(m.group(0).strip())
            prov = a["meta"].get("provenance", {})
            parts.append(f"## {a['story']['story_id']}: {self._title_of(a)}\n" + "\n\n".join(keep)
                         + f"\n\nProvenance: {prov.get('numbers_matched', '?')}/{prov.get('numbers_checked', '?')} numbers matched; "
                           f"unmatched: {[u['text'] for u in prov.get('unmatched_numbers', [])[:8]]}")
        values = {"EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": human_date(self.date),
                  "ARTICLES": "\n\n---\n\n".join(parts)}
        try:
            result = self.call("review", self.prompt("review", values), role="review", tools=[], label="review",
                               timeout_key="header_s")
        except (Deferred, HumanActionNeeded) as exc:
            return [f"adversarial review skipped ({exc})"]
        if not result.ok:
            return [f"adversarial review unavailable ({result.error_kind})"]
        try:
            issues = slate_core.extract_json(result.text)
        except ValueError:
            return ["adversarial review returned unreadable output"]
        atomic_write(self.generated_dir / f"{self.ctx.profile.artifact_stem}-{self.date}-review.json",
                     json.dumps(issues, indent=1, ensure_ascii=False))
        high = [i for i in issues if isinstance(i, dict) and i.get("severity") == "high"]
        self.state.mark("review", "done", issues=len(issues), high=len(high))
        return [f"reviewer flagged {len(high)} high-severity issue(s): " + "; ".join(
            f"{i.get('story_id')}: {str(i.get('issue'))[:140]}" for i in high[:5])] if high else []

    # ---- stage: combine (editions that carry a sibling edition in front of them) -------------------
    def edition_layout(self, edition_id: str) -> tuple[Path, str]:
        """(root directory, artifact stem) of another edition, derived the way the runner profile derives them.
        ``BRIEFING_<EDITION>_ROOT`` overrides the root (tests, unusual installs)."""
        other = edition_core.load_edition(edition_id)
        profile = other.extra.get("profile") or {}
        own = self.edition.extra.get("profile") or {}
        top = self.ctx.profile.briefing_root.parent if own.get("root_subdir") else self.ctx.profile.briefing_root
        override = os.environ.get(f"BRIEFING_{edition_id.upper()}_ROOT")
        root = Path(override) if override else (top / str(profile["root_subdir"]) if profile.get("root_subdir") else top)
        runner = self.ctx.profile.runner_name
        stem = f"{runner}-{profile['stem_suffix']}" if profile.get("stem_suffix") else f"{runner}-{edition_id}-briefing"
        return root, stem

    def _combined_marker_path(self) -> Path:
        return self.generated_dir / f"{self.ctx.profile.artifact_stem}-{self.date}-combined.json"

    def stage_combine(self, notes: list[str]) -> dict[str, Any] | None:
        """Put the sibling edition's finished PDF (and email text) in front of this edition's. Never fatal: without the sibling,
        this edition ships alone and says so; the marker tells the sibling it may send itself late."""
        cfg = self.edition.get("conductor.combine")
        if not cfg or self.dry_run:
            return None
        import pdf_merge_core

        sibling_id = str(cfg["prepend_edition"])
        sibling = edition_core.load_edition(sibling_id)
        root, stem = self.edition_layout(sibling_id)
        gen = root / "generated"
        pdf, text_file, html_file = gen / f"{stem}-{self.date}.pdf", gen / f"{stem}-{self.date}-email.txt", gen / f"{stem}-{self.date}-email.html"
        state = load_json(gen / f"{stem}-{self.date}-conductor.json", {}) or {}
        reason = ""
        if not pdf.exists():
            run = (state.get("stages") or {}).get("run", {})
            reason = f"its run was {run.get('status')}" if run.get("status") else ("its run reported nothing" if not state else "it had not finished")
        combined_pdf = self.generated_dir / f"{self.ctx.profile.artifact_stem}-{self.date}-combined.pdf"
        result: dict[str, Any] | None = None
        if not reason:
            body_text = text_file.read_text(encoding="utf-8") if text_file.exists() else ""
            titles = re.findall(r"(?m)^\d+\.\s+(.+)$", body_text)
            try:
                summary = pdf_merge_core.combine_pdfs(
                    [pdf_merge_core.Part(sibling.label, pdf, titles=titles[:14]),
                     pdf_merge_core.Part(self.edition.label, Path(self.paths.pdf_path(self.date)), titles=self._daily_titles())],
                    combined_pdf, heading=f"{sibling.label} and {self.edition.label}", date_human=human_date(self.date))
                result = {"pdf": combined_pdf, "label": sibling.label, "summary": summary,
                          "text": body_text, "html": html_file.read_text(encoding="utf-8") if html_file.exists() else "",
                          "articles": (state.get("delivered") or {}).get("articles")}
                self.log(f"combined briefing: {summary['pages']} pages ({sibling.label} first)")
            except ValueError as exc:
                reason = f"its PDF could not be merged ({exc})"
        if result is None:
            notes.append(f"{sibling.label} was not included today: {reason}. It will be sent on its own if it finishes later.")
            self.log(f"combined briefing skipped: {reason}")
        atomic_write(self._combined_marker_path(), json.dumps({"date": self.date, "included": result is not None, "reason": reason,
                                                               "sibling": sibling_id}) + "\n")
        return result

    def _daily_titles(self) -> list[str]:
        try:
            return [t for t in (self._title_of(a) for a in self._finished_articles())][:30]
        except Exception:  # noqa: BLE001 - the index page is a convenience
            return []

    def _carrier_overdue(self, partner_id: str, grace_minutes: int = 60) -> bool:
        """The carrying edition's own deadline (plus a grace period) has passed on this date and it never delivered."""
        partner = merge_settings(edition_core.load_edition(partner_id))
        spec = partner.get("deadline_local")
        if not spec:
            return False
        hour, minute = (int(x) for x in str(spec).split(":"))
        day = datetime.strptime(self.date, "%Y-%m-%d")
        deadline = datetime(day.year, day.month, day.day, hour, minute, tzinfo=resolve_timezone(partner["timezone"]))
        return self.now() > deadline + timedelta(minutes=grace_minutes)

    def _shipped_without_me(self, partner_id: str) -> bool:
        """True when the edition that carries this one (``partner_id``) went out without this edition's section, or never went out
        at all and is past its deadline: either way nobody will receive this edition unless it sends itself."""
        root, stem = self.edition_layout(partner_id)
        state = load_json(root / "generated" / f"{stem}-{self.date}-conductor.json", {}) or {}
        marker = load_json(root / "generated" / f"{stem}-{self.date}-combined.json", {}) or {}
        if state.get("delivered"):
            return marker.get("included") is False
        return self._carrier_overdue(partner_id)

    def _release_due(self) -> bool:
        carrier = self.edition.get("conductor.delivery.held_for")
        return bool(carrier) and self._shipped_without_me(str(carrier))

    def verification_notes(self, articles: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> list[str]:
        """What the reader is told about unverified material, without a list of slugs: the stories where it matters by title (three or
        more figures, or any quotation or DOI, not found in what the writer had), and only a count for the many that merely rest on
        page summaries or carry a stray figure (half of a live daily briefing would otherwise be named)."""
        serious, mild = [], 0
        for article, summary in zip(articles, summaries):
            if not article["meta"].get("flags"):
                continue
            prov = article["meta"].get("provenance") or {}
            title = str(summary.get("title") or article["story"].get("title") or article["story"]["story_id"])
            if len(prov.get("unmatched_numbers") or []) >= 3 or prov.get("unmatched_quotes") or [d for d, i in (prov.get("dois") or {}).items() if i.get("status") in {"not_found", "mismatch"}]:
                serious.append(title)
            else:
                mild += 1
        notes = []
        if serious:
            notes.append("figures or quotations not verified against the sources fetched in: " + "; ".join(serious))
        if mild:
            notes.append(f"{mild} more stor{'y rests' if mild == 1 else 'ies rest'} partly on summaries of pages or {'carries' if mild == 1 else 'carry'} a stray unverified figure")
        return notes

    def quality_metrics(self, articles: list[dict[str, Any]]) -> dict[str, Any]:
        """One comparable record per day: validators reward form, so drift toward thin, unverified or boilerplate output has to be
        visible as a trend (quality-history.jsonl beside the run state)."""
        metas = [a.get("meta") or {} for a in articles]
        prov = [m.get("provenance") or {} for m in metas]
        words = [int(m.get("words") or 0) for m in metas]
        total = lambda key: sum(int(p.get(key) or 0) for p in prov)  # noqa: E731
        return {
            "date": self.date, "edition": self.edition.edition_id, "articles": len(articles), "words_total": sum(words),
            "words_mean": round(sum(words) / len(words)) if words else 0, "words_min": min(words) if words else 0,
            "numbers_checked": total("numbers_checked"), "numbers_unmatched": sum(len(p.get("unmatched_numbers") or []) for p in prov),
            "quotes_checked": total("quotes_checked"), "quotes_unmatched": sum(len(p.get("unmatched_quotes") or []) for p in prov),
            "quotes_summary_only": total("summary_only_quotes"), "quotes_translated": total("translated_quotes"),
            "unread_mentions_remaining": total("unread_mentions"), "numbers_derived": sum(len(p.get("derived_numbers") or []) for p in prov),
            "articles_flagged": sum(1 for m in metas if m.get("flags")), "attempts_total": sum(int(m.get("attempts") or 0) for m in metas),
            "cost_usd": round(float(self.state.state.get("spent_usd", 0.0)), 2), "output_tokens": int(self.state.state.get("output_tokens", 0)),
            "notes": len(self.state.state.get("delivery_notes") or []),
        }

    def _write_quality_metrics(self, articles: list[dict[str, Any]]) -> None:
        try:
            path = Path(self.ctx.conductor_state_path(self.date)).parent / "quality-history.jsonl"
            record = self.quality_metrics(articles)
            lines = [line for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else [])
                     if line.strip() and not (f'"date": "{self.date}"' in line and f'"edition": "{self.edition.edition_id}"' in line)]
            lines.append(json.dumps(record, ensure_ascii=False))
            atomic_write(path, "\n".join(lines[-400:]) + "\n")
        except Exception as exc:  # noqa: BLE001 - a metrics file must never fail a briefing
            self.log(f"quality metrics not written: {exc!r}")

    # ---- stage: deliver ------------------------------------------------------------
    def stage_deliver(self, articles: list[dict[str, Any]], notes: list[str], degraded: bool = False,
                      combined: dict[str, Any] | None = None) -> bool:
        import article_tools_core as tools
        import run_reporting

        summaries = [tools.article_summary(a["path"].read_text(encoding="utf-8"), a["story"]) for a in articles]
        notes.extend(self.verification_notes(articles, summaries))
        digest = [i.splitlines()[0].lstrip("# ").strip() for i in load_json(self.articles_dir / "digest.json", {}).get("items", [])]
        text, body_html = tools.compose_email_bodies(label=self.edition.label, date_human=human_date(self.date), summaries=summaries,
                                                    notes=notes, digest_titles=digest)
        text_path = Path(self.ctx.email_body_path(self.date)).with_suffix(".txt")
        html_path = Path(self.ctx.email_body_path(self.date))
        atomic_write(text_path, text)  # the stand-alone bodies stay on disk: a carrying edition builds its email from them
        atomic_write(html_path, body_html)
        pdf = Path(self.paths.pdf_path(self.date))
        subject_suffix = ""
        if combined:
            pdf = combined["pdf"]
            subject_suffix = f" + {combined['label']}"
            text = f"{combined['label'].upper()}\n{'=' * len(combined['label'])}\n{combined['text'].strip()}\n\n\n{self.edition.label.upper()}\n{'=' * len(self.edition.label)}\n{text}"
            body_html = f"{combined['html']}\n<hr>\n{body_html}"
            combined_text, combined_html = text_path.with_name(text_path.stem + "-combined.txt"), html_path.with_name(html_path.stem + "-combined.html")
            atomic_write(combined_text, text)
            atomic_write(combined_html, body_html)
            text_path, html_path = combined_text, combined_html
        self.state.state["delivery_notes"] = notes
        self._write_quality_metrics(articles)
        undelivered_marker = Path(self.paths.pdf_path(self.date)).with_suffix(".undelivered")
        if self.dry_run or not self.deliver_enabled:
            self.log("delivery skipped (dry run)")
            self.state.mark("deliver", "done", skipped=True)
            try:  # a day nobody received must not make its stories count as "already covered" for the next two days
                undelivered_marker.write_text(f"not delivered (dry run or delivery disabled) at {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
            except OSError:
                pass
            return True
        carrier = self.edition.get("conductor.delivery.held_for")
        if self.edition.get("conductor.delivery.standalone") is False and os.environ.get("BRIEFING_FORCE_STANDALONE") != "1":
            if not (carrier and self._shipped_without_me(str(carrier))):
                import run_reporting as _rr

                self.state.state["delivered"] = {"at": datetime.now(timezone.utc).isoformat(), "articles": len(articles), "notes": notes,
                                                 "held": f"carried inside the {carrier or 'combined'} briefing"}
                self.state.mark("deliver", "done", held=True)
                try:
                    _rr.update_run_report(self.date, run_status="degraded" if degraded else "completed", delivery_status="held")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"run report update failed: {exc!r}")
                self.log(f"held {len(articles)} articles for the {carrier or 'combined'} briefing (no stand-alone email)")
                return True
            notes.append(f"the {carrier} briefing went out without this section (or never went out), so this edition is sent on its own (late)")
            subject_suffix = " (late)"
        command_json = os.environ.get("BRIEFING_DELIVER_COMMAND")
        command = json.loads(command_json) if command_json else self.edition.get("conductor.delivery.command")
        if not command:
            raise StageFailed("deliver", "no delivery command configured (set BRIEFING_DELIVER_COMMAND or conductor.delivery.command)")
        subject = self.edition.get("conductor.delivery.subject_template", "{label} - {human_date}").format(
            label=self.edition.label, human_date=human_date(self.date)) + subject_suffix
        recipients_env = self.edition.get("conductor.delivery.recipients_env")
        recipients = os.environ.get(recipients_env or "", "")
        extra_env = self.edition.get("conductor.delivery.extra_recipients_env")
        extra_recipients = os.environ.get(extra_env or "", "")
        mapping = {"{extra_recipients}": extra_recipients, "{date}": self.date, "{pdf}": str(pdf), "{body_text}": str(text_path), "{body_html}": str(html_path),
                   "{edition}": self.edition.edition_id, "{recipients}": recipients, "{subject}": subject}
        argv = []
        for part in command:
            for key, value in mapping.items():
                part = part.replace(key, value)
            argv.append(part)
        cleaned: list[str] = []
        skip = False
        for index, part in enumerate(argv):
            if skip:
                skip = False
                continue
            if part.startswith("-") and index + 1 < len(argv) and argv[index + 1] == "":
                skip = True  # a flag whose value expanded to nothing (e.g. no recipients configured): drop both
                continue
            cleaned.append(part)
        argv = cleaned
        for attempt in range(1, 4):
            code, output = self.runner(argv, env=dict(self.env), cwd=str(self.scripts_dir), timeout=900)
            self.log(f"deliver attempt {attempt}: exit={code}")
            if code == 0 and "Email sent" in output:
                break
            if "stage-order gate" in output or "Delivery blocked" in output:
                reason = next((line.strip() for line in output.splitlines() if "Delivery blocked" in line or "stage-order gate" in line), "blocked")
                run_reporting.update_run_report(self.date, run_status="failed", delivery_status="blocked")
                raise StageFailed("deliver", f"the delivery gate refused to send: {reason[:240]}")  # retrying cannot change its answer
            self.sleep(30 * attempt)
        else:
            run_reporting.update_run_report(self.date, run_status="failed", delivery_status="failed")
            raise StageFailed("deliver", f"delivery command failed: {output[-300:]}")
        self.state.state["delivered"] = {"at": datetime.now(timezone.utc).isoformat(), "articles": len(articles), "notes": notes}
        self.state.mark("deliver", "done")
        undelivered_marker.unlink(missing_ok=True)
        try:
            run_reporting.update_run_report(self.date, run_status="degraded" if degraded else "completed", delivery_status="sent")
        except Exception as exc:  # noqa: BLE001
            self.log(f"run report update failed: {exc!r}")
        self.log(f"delivered {len(articles)} articles")
        return True

    # ---- diagnosis ---------------------------------------------------------------------
    def diagnosis(self) -> str:
        return render_diagnosis(self.state.state, self.settings)


def render_diagnosis(state: dict[str, Any], settings: dict[str, Any] | None = None) -> str:
    lines = [f"Run state for {state.get('date')} ({state.get('edition')}); attempts: {state.get('attempts', 0)}; "
             f"spent: ${state.get('spent_usd', 0):.2f}"]
    limit = state.get("rate_limit")
    if limit:
        lines.append(f"Account limits at last call: five-hour {limit.get('five_hour')}, seven-day {limit.get('seven_day')}, status {limit.get('status')}")
    for name, info in state.get("stages", {}).items():
        extras = {k: v for k, v in info.items() if k not in {"status", "at"}}
        lines.append(f"- {name}: {info.get('status')}" + (f" {extras}" if extras else ""))
    events = state.get("events", [])[-8:]
    if events:
        lines.append("Recent events:")
        lines += [f"  {e['at'][11:19]} {e['message']}" for e in events]
    run = state.get("stages", {}).get("run", {})
    if run.get("status") == "deferred":
        until = run.get("until")
        lines.append("Deferred: " + str(run.get("reason")) + (
            f"; retry after {datetime.fromtimestamp(until, timezone.utc).isoformat()}" if until else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def default_scripts_dir() -> Path:
    override = os.environ.get("DAILY_BRIEFING_SCRIPTS_DIR")
    if override:
        return Path(override)
    return _HERE / "reference_runner" / "scripts"


def today_in(tz_name: str) -> str:
    return datetime.now(resolve_timezone(tz_name)).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None, *, backend: llm.Backend | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a briefing edition end to end (resumable).")
    parser.add_argument("--edition", default=os.environ.get("BRIEFING_EDITION", edition_core.DEFAULT_EDITION))
    parser.add_argument("--date", help="Briefing date YYYY-MM-DD (default: today in the edition's time zone).")
    parser.add_argument("--scripts-dir", type=Path, default=None, help="Runner scripts directory (wrapper modules).")
    parser.add_argument("--dry-run", action="store_true", help="Run every stage except model probes' limits gate and delivery.")
    parser.add_argument("--no-deliver", action="store_true", help="Build the PDF but do not send it.")
    parser.add_argument("--finish", action="store_true", help="Watchdog mode: do not draft; assemble and deliver what exists.")
    parser.add_argument("--diagnose", action="store_true", help="Print the run state and exit.")
    parser.add_argument("--audit-fixture", type=Path, default=None, help="Use a source-audit fixture (no network).")
    parser.add_argument("--deadline", default=None, metavar="HH:MM|none",
                        help="Override the edition's local deadline for this run ('none' removes it; for manual runs started late in the day).")
    args = parser.parse_args(argv)

    os.environ["BRIEFING_EDITION"] = args.edition
    edition_core.reload_edition_cache()
    try:
        edition = edition_core.load_edition(args.edition)
    except edition_core.EditionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    settings = merge_settings(edition)
    date = args.date or today_in(settings["timezone"])
    scripts_dir = args.scripts_dir or default_scripts_dir()
    override: dict[str, Any] = {}
    if args.deadline:
        if args.deadline.lower() != "none" and not re.fullmatch(r"\d{1,2}:\d{2}", args.deadline):
            print("error: --deadline must be HH:MM or 'none'", file=sys.stderr)
            return EXIT_USAGE
        override["deadline_local"] = None if args.deadline.lower() == "none" else args.deadline
    conductor = Conductor(
        edition=edition, date=date, scripts_dir=scripts_dir, backend=backend or llm.ClaudeBackend(),
        dry_run=args.dry_run, deliver=not args.no_deliver, audit_fixture=args.audit_fixture, settings_override=override,
    )
    if args.diagnose:
        print(conductor.diagnosis())
        return EXIT_OK
    return conductor.run(finish_only=args.finish)


if __name__ == "__main__":
    raise SystemExit(main())
