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

EXIT_OK, EXIT_DEGRADED, EXIT_FAILED, EXIT_DISABLED, EXIT_USAGE, EXIT_DEFERRED = 0, 10, 20, 30, 2, 75

DEFAULT_SETTINGS: dict[str, Any] = {
    "article_target": 10, "article_min": 6, "article_max": 12, "slate_candidates": 14, "digest_items": 6,
    "article_words_target": 3500, "article_words_floor": 1500, "max_parallel_articles": 2,
    "article_section": None, "digest_section": None,
    "timezone": "Asia/Tokyo", "deadline_local": "11:30",
    "models": {
        "classify": {"model": "haiku", "effort": "low"}, "slate": {"model": "opus", "effort": "medium"},
        "article": {"model": "sonnet", "effort": "medium"}, "repair": {"model": "sonnet", "effort": "low"},
        "header": {"model": "opus", "effort": "low"}, "digest": {"model": "sonnet", "effort": "low"},
        "review": {"model": "opus", "effort": "medium", "optional": True},
    },
    "budget": {
        "max_cost_usd_per_run": 40.0, "max_cost_usd_per_article": 4.0,
        "defer_if_five_hour_above": 0.80, "defer_if_seven_day_above": 0.90,
        "stop_launching_above": 0.92,
    },
    "timeouts": {"article_s": 2400, "slate_s": 1200, "header_s": 600, "digest_s": 1500, "repair_s": 1500, "probe_s": 120, "idle_s": 600},
    "repairs_per_article": 2, "call_retries": 2, "retry_backoff_s": 20, "review_enabled": True,
    "stage_timeout_s": {"assemble": 600, "validate": 600, "pdf": 900, "visual_qa": 900, "audit": 1800},
    "lock_stale_minutes": 360, "fallback_on_refusal": "opus", "enabled": True, "wait_for_reset_minutes": 90,
}


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
    os.replace(tmp, path)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def render_prompt(template: str, values: dict[str, Any]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    leftover = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", out)))
    if leftover:
        raise ValueError(f"unfilled prompt placeholders: {', '.join(leftover)}")
    return out


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


class FileLock:
    def __init__(self, path: Path, stale_minutes: float):
        self.path, self.stale = path, stale_minutes * 60
        self.held = False

    def acquire(self) -> bool:
        if self.path.exists():
            info = load_json(self.path, {})
            age = time.time() - float(info.get("started_epoch", 0))
            if age < self.stale:
                return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"pid": os.getpid(), "started_epoch": time.time()}), encoding="utf-8")
        self.held = True
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
        self.photo_fetcher, self.json_fetcher, self.url_checker = photo_fetcher, json_fetcher, url_checker
        self.dry_run, self.deliver_enabled, self.check_network = dry_run, deliver, check_network
        self.audit_fixture = audit_fixture
        self.settings = merge_settings(edition)
        for key, value in (settings_override or {}).items():
            if isinstance(value, dict) and isinstance(self.settings.get(key), dict):
                self.settings[key].update(value)
            else:
                self.settings[key] = value
        self.env = {**os.environ, "BRIEFING_EDITION": edition.edition_id, **(env or {})}
        self._stop = threading.Event()
        self._cost_lock = threading.Lock()
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
        tz = ZoneInfo(self.settings["timezone"]) if ZoneInfo else timezone.utc
        day = datetime.strptime(self.date, "%Y-%m-%d")
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)

    def past_deadline(self) -> bool:
        deadline = self.deadline()
        return bool(deadline and self.now() > deadline)

    def _record(self, stage: str, request: llm.LLMRequest, result: llm.LLMResult) -> None:
        with self._cost_lock, self.state._lock:
            self.state.state["spent_usd"] = round(float(self.state.state.get("spent_usd", 0.0)) + result.cost_usd, 4)
            if result.rate_limit.status != "unknown":
                self.last_rate_limit = result.rate_limit
                self.state.state["rate_limit"] = {
                    "status": result.rate_limit.status, "five_hour": result.rate_limit.five_hour_utilization,
                    "seven_day": result.rate_limit.seven_day_utilization, "resets_at": result.rate_limit.resets_at,
                }
        try:
            import run_reporting

            run_reporting.record_usage(
                self.date, stage, model=result.model_used or request.model, backend=self.backend.name,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens, cache_creation_tokens=result.cache_creation_tokens,
                cost_usd=result.cost_usd, duration_seconds=result.duration_s, ok=result.ok,
                error=result.error_detail if not result.ok else None, label=request.label,
            )
        except Exception as exc:  # noqa: BLE001 - accounting must never break a run
            self.state.event(f"usage accounting failed: {exc!r}")
        self.state.save()

    def check_budget(self, *, launching: bool = True) -> None:
        budget = self.settings["budget"]
        if self._stop.is_set():
            raise Deferred("stopped by an earlier limit", until=self.last_rate_limit.resets_at)
        spent = float(self.state.state.get("spent_usd", 0.0))
        if budget.get("max_cost_usd_per_run") and spent >= float(budget["max_cost_usd_per_run"]):
            self._stop.set()
            raise Deferred(f"run budget of ${budget['max_cost_usd_per_run']:.2f} reached (${spent:.2f} spent)", kind="budget")
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

    def prompt(self, name: str, values: dict[str, Any]) -> str:
        return render_prompt((self.prompts_dir / f"{name}.md").read_text(encoding="utf-8"), values)

    # ================================================================================
    # Entry points
    # ================================================================================
    def run(self, *, finish_only: bool = False) -> int:
        if not self.settings.get("enabled", True) or os.environ.get("BRIEFING_DISABLED") == "1":
            self._alert_once("disabled", "Briefing pipeline is disabled",
                             f"{self.edition.label} did not run on {self.date} because it is disabled "
                             "(conductor.enabled=false or BRIEFING_DISABLED=1). Remove the flag to resume.")
            return EXIT_DISABLED
        if self.state.state.get("delivered"):
            self.log("already delivered; nothing to do")
            return EXIT_OK
        if not self.lock.acquire():
            self.log("another conductor run is active; exiting")
            return EXIT_OK
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
        finally:
            self.state.save()
            self.lock.release()

    def _on_deferred(self, exc: Deferred) -> int:
        until = exc.until
        when = datetime.fromtimestamp(until, timezone.utc).isoformat() if until else "unknown"
        self.state.mark("run", "deferred", reason=exc.reason, until=until)
        self.log(f"deferred: {exc.reason} (retry after {when})")
        done = self._finished_articles()
        if self.past_deadline() and len(done) >= int(self.settings["article_min"]):
            self.log("past the deadline with enough finished articles: shipping a smaller briefing")
            try:
                return self._finish(degraded=True)
            except (StageFailed, Deferred, HumanActionNeeded) as inner:
                return self._on_failure(StageFailed("finish", str(inner)))
        if self.past_deadline():
            self._alert_once("late", f"{self.edition.label}: no briefing yet for {self.date}",
                             f"Deferred ({exc.reason}) and past the {self.settings['deadline_local']} deadline with only "
                             f"{len(done)} of {self.settings['article_min']} required articles finished.\n\n" + self.diagnosis())
        return EXIT_DEFERRED

    def _on_failure(self, exc: StageFailed) -> int:
        done = self._finished_articles()
        if len(done) >= int(self.settings["article_min"]) and exc.stage not in {"deliver", "finish"}:
            self.log("attempting degraded last-mile delivery")
            try:
                return self._finish(degraded=True)
            except (StageFailed, Deferred, HumanActionNeeded) as inner:
                exc = StageFailed("finish", f"{exc.reason}; degraded attempt also failed: {inner}")
        self._alert_once(f"fail-{exc.stage}", f"{self.edition.label}: briefing failed for {self.date}",
                         f"{exc}\n\n" + self.diagnosis())
        return EXIT_FAILED

    def _alert_once(self, key: str, subject: str, body: str) -> None:
        marker = f"{self.date}:{key}"
        if marker in self.state.state["alerts_sent"]:
            return
        self.state.state["alerts_sent"].append(marker)
        sent = self.notifier.send(subject, body, key=key)
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
        if len(articles) < int(self.settings["article_min"]):
            raise StageFailed("finish", f"only {len(articles)} finished article(s); minimum is {self.settings['article_min']}")
        if degraded or len(articles) < int(self.settings["article_target"]):
            notes.append(f"{len(articles)} of {self.settings['article_target']} target stories were finished")
        self.stage_header(articles)
        articles, extra_notes = self.stage_assemble_validate(articles)
        notes.extend(extra_notes)
        self.stage_pdf()
        notes.extend(self.stage_review(articles))
        self.stage_deliver(articles, notes)
        short = len(articles) < int(self.settings["article_target"])
        return EXIT_DEGRADED if (degraded or short or any("dropped" in n for n in notes)) else EXIT_OK

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
        if code not in (0, 1):  # exit 1 is "audit status failed" (systemic source failure): continue, the gate reports it
            raise StageFailed("audit", f"source audit crashed (exit {code}): {output[-300:]}")
        self.state.mark("audit", "done", exit=code)

    # ---- stage: ingest -----------------------------------------------------------------
    def prior_titles(self) -> list[str]:
        try:
            import briefing_manifest

            return [str(i.get("title")) for i in briefing_manifest.load_prior_items(self.date) if i.get("title")][:60]
        except Exception:  # noqa: BLE001
            return []

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
        keywords = tuple(self.edition.get("ingest.keywords") or slate_core.DEFAULT_KEYWORDS)
        ingest = slate_core.build_ingest(
            audit, now=self.now(), prior_titles=self.prior_titles(), keywords=keywords, meta_by_source=meta,
            banned_check=lambda title: briefing_manifest.find_banned_topics(title, ""))
        atomic_write(Path(self.ctx.ingest_path(self.date)), json.dumps(ingest, indent=1, ensure_ascii=False) + "\n")
        atomic_write(Path(self.ctx.ingest_markdown_path(self.date)), slate_core.ingest_markdown(ingest, limit=220) + "\n")
        self.state.mark("ingest", "done", **ingest["stats"])
        self.log(f"ingest: {ingest['stats']['kept']} candidate headlines from {ingest['stats']['sources_with_items']} sources")

    # ---- stage: slate ------------------------------------------------------------------
    def spec_text(self) -> str:
        path = _HERE / "references" / f"{self.edition.edition_id}-briefing-spec.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def stage_slate(self) -> None:
        if self.state.done("slate") and Path(self.ctx.slate_path(self.date)).exists():
            return
        import briefing_manifest
        import slate_core

        ingest_md_path = Path(self.ctx.ingest_markdown_path(self.date))
        ingest_md = ingest_md_path.read_text(encoding="utf-8") if ingest_md_path.exists() else "(no headlines were harvested today; rely on search)"
        ingest = load_json(Path(self.ctx.ingest_path(self.date)), {"stats": {}})
        prior = self.prior_titles()
        values = {
            "EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": human_date(self.date), "SPEC": self.spec_text(),
            "INGEST": ingest_md, "SOURCE_COUNT": ingest.get("stats", {}).get("sources_with_items", 0),
            "PRIOR_TITLES": "\n".join(f"- {t}" for t in prior) or "- (none)", "SLATE_CANDIDATES": self.settings["slate_candidates"],
        }
        banned = lambda text: briefing_manifest.find_banned_topics(text, "")  # noqa: E731
        report = None
        for attempt in (1, 2):
            result = self.call("slate", self.prompt("slate", values), role="slate", tools=["WebSearch"], label="slate")
            if not result.ok:
                raise StageFailed("slate", f"slate call failed ({result.error_kind}): {result.error_detail[:200]}")
            try:
                report = slate_core.validate_slate(slate_core.extract_json(result.text), prior_titles=prior, banned_check=banned)
            except ValueError as exc:
                self.log(f"slate attempt {attempt}: unusable output ({exc})")
                continue
            if len(report.kept) >= int(self.settings["article_min"]):
                break
            self.log(f"slate attempt {attempt}: only {len(report.kept)} valid stories")
            values["SPEC"] += f"\n\nNOTE: your previous answer had only {len(report.kept)} usable stories; return at least {self.settings['article_min'] + 4}."
        if report is None or len(report.kept) < int(self.settings["article_min"]):
            raise StageFailed("slate", f"could not obtain {self.settings['article_min']} valid stories")
        selected, backups, notes = slate_core.select_slate(
            report.kept, target=int(self.settings["article_target"]), minimum=int(self.settings["article_min"]),
            maximum=int(self.settings["article_max"]))
        atomic_write(Path(self.ctx.slate_path(self.date)), json.dumps(
            {"selected": selected, "backups": backups, "dropped": report.dropped, "notes": notes}, indent=1, ensure_ascii=False) + "\n")
        self.state.mark("slate", "done", selected=len(selected), backups=len(backups), dropped=len(report.dropped))
        self.log(f"slate: {len(selected)} selected, {len(backups)} backups, {len(report.dropped)} dropped; {'; '.join(notes) or 'balanced'}")

    def load_slate(self) -> dict[str, Any]:
        slate = load_json(Path(self.ctx.slate_path(self.date)))
        if not slate:
            raise StageFailed("slate", "slate file missing")
        return slate

    # ---- stage: photos -----------------------------------------------------------------
    def stage_photos(self) -> None:
        if self.state.done("photos"):
            return
        import photo_core

        slate = self.load_slate()
        stories = slate["selected"] + slate["backups"][:4]
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

    def _finished_articles(self) -> list[dict[str, Any]]:
        """Finished articles in slate order, each ``{'story': ..., 'path': ..., 'meta': ...}``."""
        slate = load_json(Path(self.ctx.slate_path(self.date)), None)
        if not slate:
            return []
        by_id = {s["story_id"]: s for s in slate["selected"] + slate["backups"]}
        out = []
        for sid, story in by_id.items():
            meta = load_json(self.article_meta_path(sid), {})
            if meta.get("status") == "done" and self.article_path(sid).exists() and not meta.get("dropped"):
                out.append({"story": story, "path": self.article_path(sid), "meta": meta})
        order = {s["story_id"]: i for i, s in enumerate(slate["selected"] + slate["backups"])}
        return sorted(out, key=lambda a: order[a["story"]["story_id"]])[: int(self.settings["article_max"])]

    def stage_articles(self) -> None:
        slate = self.load_slate()
        target = int(self.settings["article_target"])
        queue = list(slate["selected"])
        backups = list(slate["backups"])
        attempted: set[str] = set()
        rounds = 0
        while rounds < 4:
            rounds += 1
            todo = [s for s in queue if s["story_id"] not in attempted]
            if not todo:
                break
            self.log(f"articles: round {rounds}, {len(todo)} stories")
            outcomes = self._write_many(todo)
            attempted.update(s["story_id"] for s in todo)
            done = [o for o in outcomes if o.status == "done"]
            have = len(self._finished_articles())
            missing = target - have
            if missing <= 0:
                break
            replacements = [b for b in backups if b["story_id"] not in attempted][:missing]
            if not replacements:
                break
            self.log(f"articles: {missing} short of target; trying {len(replacements)} backup stories")
            queue = replacements
            _ = done
        have = len(self._finished_articles())
        self.state.mark("articles", "done", finished=have, target=target)
        if have < int(self.settings["article_min"]):
            raise StageFailed("articles", f"only {have} article(s) finished; minimum is {self.settings['article_min']}")

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
            enough = len(self._finished_articles()) >= int(self.settings["article_min"])
            if isinstance(first_error, Deferred) and enough:
                wait_s = (first_error.until - self.now().timestamp()) if first_error.until else None
                patience = float(self.settings["wait_for_reset_minutes"]) * 60
                if wait_s is not None and wait_s <= patience and not self.past_deadline():
                    self.log("limit resets soon; deferring so the remaining stories can be written")
                    raise first_error
                self.log("limit hit with enough finished articles; continuing with what exists")
            else:
                raise first_error
        return outcomes

    def check_article_text(self, markdown: str) -> tuple[list[str], dict[str, Any]]:
        """Run the edition's per-article rules on one article. Returns (blocking findings, manifest item)."""
        import briefing_manifest as manifest

        heading, _, body = markdown.partition("\n")
        audit = manifest.load_source_audit(self.date)
        item = manifest.build_item_manifest(
            self.article_section, self.date, heading.lstrip("# ").strip(), body, prior_items=manifest.load_prior_items(self.date),
            image_metadata=manifest.load_image_metadata(self.date), source_audit=audit)
        rules = manifest.SECTION_RULES[self.article_section]
        result = manifest.evaluate_item_rules(self.article_section, item, rules, audit_available=bool(audit))
        return result["blocking"], item

    def write_article(self, story: dict[str, Any]) -> ArticleOutcome:
        import article_tools_core as tools
        import photo_core
        import provenance_core

        sid = story["story_id"]
        existing = load_json(self.article_meta_path(sid), {})
        if existing.get("status") in {"done", "skipped"}:
            return ArticleOutcome(sid, existing["status"], existing.get("reason", ""), existing.get("words", 0))
        budget = float(self.settings["budget"]["max_cost_usd_per_article"])
        values = {
            "EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": human_date(self.date), "SPEC": self.spec_text(),
            "STORY_JSON": json.dumps({k: v for k, v in story.items() if k not in {"photo_allow_words"}}, indent=1, ensure_ascii=False),
            "PRIOR_TITLES": "\n".join(f"- {t}" for t in self.prior_titles()[:30]) or "- (none)",
            "WORDS_TARGET": self.settings["article_words_target"], "STORY_ID": sid,
            "PRIOR_MARKER": f"; prior: {story['prior_story_id']}" if story.get("prior_story_id") else "",
        }
        outcome = ArticleOutcome(sid, "failed")
        corpus: list[str] = []
        result = self.call("article", self.prompt("article", values), role="article", tools=["WebFetch", "WebSearch"],
                           label=sid, budget_usd=budget)
        outcome.attempts, outcome.cost_usd = 1, result.cost_usd
        if not result.ok:
            outcome.reason = f"{result.error_kind}: {result.error_detail[:160]}"
            self._save_article_meta(sid, outcome)
            return outcome
        corpus += result.tool_corpus
        raw = result.text.strip()
        atomic_write(self.articles_dir / f"{sid}.raw.md", raw + "\n")
        if raw.upper().startswith("SKIP"):
            outcome.status, outcome.reason = "skipped", raw[:200]
            self._save_article_meta(sid, outcome)
            return outcome

        try:
            article, fixes = tools.normalize_article(raw, story)
        except ValueError as exc:
            outcome.reason = f"unusable output: {exc}"
            self._save_article_meta(sid, outcome)
            return outcome
        outcome.fixes += fixes
        article = photo_core.insert_photos(article, self.photo_lines(sid))

        for round_no in range(int(self.settings["repairs_per_article"]) + 1):
            blocking, item = self.check_article_text(article)
            prov = provenance_core.check_article(
                article, corpus, json_fetcher=self.json_fetcher, url_checker=self.url_checker, check_network=self.check_network)
            word_only = bool(blocking) and all("hard word floor" in b for b in blocking)
            if not blocking and not prov.needs_repair():
                break
            if word_only and round_no >= 1:
                break  # repairs cannot create honest material; let the story be replaced
            if round_no >= int(self.settings["repairs_per_article"]):
                break
            findings = [f"- {b}" for b in blocking]
            if prov.needs_repair():
                findings.append("Provenance check (could not be matched to any source opened in this run):\n" + prov.repair_brief())
            if word_only:
                findings.append("The article is below the hard word floor. Add only real, sourced material (open more sources); "
                                "if there is not enough genuine material, return SKIP: material_limited.")
            fix = self.call("repair", self.prompt("repair", {**values, "FINDINGS": "\n".join(findings), "ARTICLE": article}),
                            role="repair", tools=["WebFetch", "WebSearch"], label=sid, budget_usd=budget,
                            timeout_key="repair_s")
            outcome.attempts += 1
            outcome.cost_usd += fix.cost_usd
            if not fix.ok:
                outcome.flags.append(f"repair call failed: {fix.error_kind}")
                break
            corpus += fix.tool_corpus
            if fix.text.strip().upper().startswith("SKIP"):
                outcome.status, outcome.reason = "skipped", fix.text.strip()[:200]
                self._save_article_meta(sid, outcome)
                return outcome
            try:
                article, more = tools.normalize_article(fix.text, story)
            except ValueError:
                outcome.flags.append("repair returned unusable output")
                break
            outcome.fixes += more
            article = photo_core.insert_photos(article, self.photo_lines(sid))

        outcome.words = int(item.get("word_count", 0))
        outcome.provenance = prov.to_dict()
        outcome.blocking = blocking
        fatal_dois = [doi for doi, info in prov.dois.items() if info.get("status") == "not_found"]
        if prov.needs_repair():
            outcome.flags.append(
                f"unverified after repair: {len(prov.unmatched_numbers)} number(s), {len(prov.unmatched_quotes)} quotation(s), "
                f"{len(prov.bad_dois)} DOI(s), {len(prov.dead_urls)} dead URL(s)")
        if blocking:
            word_only = all("hard word floor" in b for b in blocking)
            outcome.status = "skipped" if word_only else "failed"
            outcome.reason = "material_limited: " + "; ".join(blocking)[:200] if word_only else "; ".join(blocking)[:300]
        elif fatal_dois or prov.unmatched_quotes:
            outcome.status, outcome.reason = "failed", "citations or quotations could not be verified"
        else:
            outcome.status = "done"
            atomic_write(self.article_path(sid), article)
        self._save_article_meta(sid, outcome)
        self.log(f"article {sid}: {outcome.status} ({outcome.words} words, ${outcome.cost_usd:.2f}"
                 f"{', ' + '; '.join(outcome.flags) if outcome.flags else ''})")
        return outcome

    def _save_article_meta(self, sid: str, outcome: ArticleOutcome) -> None:
        atomic_write(self.article_meta_path(sid), json.dumps(outcome.__dict__, indent=1, ensure_ascii=False) + "\n")
        self.state.mark(f"article:{sid}", outcome.status, words=outcome.words, cost=round(outcome.cost_usd, 3))

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
        pool = [s for s in slate["backups"] if s["story_id"] not in used and s["story_id"] not in attempted]
        pool = pool[: int(self.settings["digest_items"]) + 2]
        if not pool:
            self.state.mark("digest", "done", items=0)
            return
        values = {
            "EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": human_date(self.date), "SPEC": self.spec_text(),
            "ITEMS_JSON": json.dumps([{k: s[k] for k in ("story_id", "title", "angle", "why_now", "taxa", "sources")} for s in pool],
                                     indent=1, ensure_ascii=False),
            "DIGEST_ITEMS": self.settings["digest_items"],
        }
        try:
            result = self.call("digest", self.prompt("digest", values), role="digest", tools=["WebFetch", "WebSearch"],
                               label="digest", budget_usd=float(self.settings["budget"]["max_cost_usd_per_article"]))
        except Deferred:
            self.state.mark("digest", "done", items=0, skipped="deferred")
            return
        items: list[str] = []
        if result.ok:
            import briefing_manifest as manifest

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

    # ---- stage: header -----------------------------------------------------------------
    def stage_header(self, articles: list[dict[str, Any]]) -> None:
        import article_tools_core as tools

        summaries = []
        for a in articles:
            summaries.append(tools.article_summary(a["path"].read_text(encoding="utf-8"), a["story"]))
        titles = [s["title"] for s in summaries]
        low, high = (int(v) for v in self.edition.header.get("exec_summary_words", [250, 750]))
        min_cross = int(self.edition.header.get("min_cross_references", 2))
        frame_date = human_date(self.date)
        prepared = getattr(self.ctx.profile, "prepared_by_line", "Prepared by Claude")
        frame = lambda middle: (f"# {self.edition.label}\n**{frame_date}** | {prepared}\n\n{middle.strip()}\n\n### Table of Contents\n\n"  # noqa: E731
                                + "\n".join(f"{i}. {t}" for i, t in enumerate(titles, start=1)) + "\n")
        header_text, source = None, "model"
        if not self.past_deadline() and not self.state.state["stages"].get("header", {}).get("fallback"):
            try:
                values = {
                    "EDITION_LABEL": self.edition.label, "DATE": self.date, "DATE_HUMAN": frame_date,
                    "SUMMARIES": json.dumps(summaries, indent=1, ensure_ascii=False), "SUMMARY_LOW": max(low, 350),
                    "SUMMARY_HIGH": min(high, 650), "MIN_CROSS": min_cross,
                }
                result = self.call("header", self.prompt("header", values), role="header", tools=[], label="header")
                if result.ok:
                    middle, fixes = tools.repair_cross_references(result.text.strip(), titles)
                    words = len(middle.split("### Cross-currents")[0].replace("## Executive Summary", "").split())
                    refs = tools.extract_header_titles(middle)
                    if low <= words <= high and len(refs) >= min_cross and all(r in titles for r in refs):
                        header_text = frame(middle)
                    else:
                        self.log(f"header: model output rejected (words={words}, refs={len(refs)}); using fallback")
            except (Deferred, HumanActionNeeded):
                self.log("header: model unavailable; using fallback")
        if header_text is None:
            source = "fallback"
            header_text = tools.deterministic_header(label=self.edition.label, date_human=frame_date, prepared_by=prepared,
                                                     summaries=summaries, summary_words=(low, high))
        atomic_write(Path(self.paths.fragment_path("header", self.date)), header_text)
        self.state.mark("header", "done", source=source, fallback=source == "fallback")

    # ---- stage: assemble + validate ----------------------------------------------------
    def _write_fragments(self, articles: list[dict[str, Any]]) -> None:
        import article_tools_core as tools

        titles = self.edition.section_titles
        body = [a["path"].read_text(encoding="utf-8") for a in articles]
        atomic_write(Path(self.paths.fragment_path(self.article_section, self.date)),
                     tools.compose_section(titles[self.article_section], body))
        if self.digest_section:
            digest = load_json(self.articles_dir / "digest.json", {}).get("items", [])
            fragment = tools.compose_section(titles[self.digest_section], digest) if digest else f"# {titles[self.digest_section]}\n"
            atomic_write(Path(self.paths.fragment_path(self.digest_section, self.date)), fragment)

    def stage_assemble_validate(self, articles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        notes: list[str] = []
        dropped: list[str] = []
        for attempt in range(1, 4):
            self._write_fragments(articles)
            code, output = self.run_script("assemble_briefing.py", timeout_key="assemble")
            if code != 0:
                raise StageFailed("assemble", f"assembly failed (exit {code}): {output[-300:]}")
            code, output = self.run_script("validate_briefing.py", timeout_key="validate")
            if code == 0:
                self.state.mark("validate", "done", attempt=attempt)
                if dropped:
                    notes.append("dropped after validation: " + "; ".join(dropped))
                return articles, notes
            report = load_json(Path(self.paths.validation_report_path(self.date)), {})
            blocking = report.get("blocking_errors") or []
            offenders = {
                a["story"]["story_id"] for a in articles
                if any(e.startswith(f"{self.article_section}/{self._title_of(a)}:") for e in blocking)
            }
            self.log(f"validation blocked ({len(blocking)} errors); offending articles: {sorted(offenders) or 'none'}")
            remaining = [a for a in articles if a["story"]["story_id"] not in offenders]
            if offenders and len(remaining) >= int(self.settings["article_min"]) and attempt < 3:
                for a in articles:
                    if a["story"]["story_id"] in offenders:
                        dropped.append(a["story"]["story_id"])
                        meta = load_json(self.article_meta_path(a["story"]["story_id"]), {})
                        meta["dropped"] = "validation: " + "; ".join(e for e in blocking if f"/{self._title_of(a)}:" in e)[:300]
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
        first = article["path"].read_text(encoding="utf-8").split("\n", 1)[0]
        return first.lstrip("# ").strip()

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
        if "review" not in self.settings["models"]:
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

    # ---- stage: deliver ----------------------------------------------------------------
    def stage_deliver(self, articles: list[dict[str, Any]], notes: list[str]) -> bool:
        import article_tools_core as tools
        import run_reporting

        summaries = [tools.article_summary(a["path"].read_text(encoding="utf-8"), a["story"]) for a in articles]
        flagged = [a["story"]["story_id"] for a in articles if (a["meta"].get("flags"))]
        if flagged:
            notes.append(f"figures not fully verified against fetched sources in: {', '.join(flagged)}")
        digest = [i.splitlines()[0].lstrip("# ").strip() for i in load_json(self.articles_dir / "digest.json", {}).get("items", [])]
        text, body_html = tools.compose_email_bodies(label=self.edition.label, date_human=human_date(self.date), summaries=summaries,
                                                    notes=notes, digest_titles=digest)
        text_path = Path(self.ctx.email_body_path(self.date)).with_suffix(".txt")
        html_path = Path(self.ctx.email_body_path(self.date))
        atomic_write(text_path, text)
        atomic_write(html_path, body_html)
        pdf = Path(self.paths.pdf_path(self.date))
        self.state.state["delivery_notes"] = notes
        if self.dry_run or not self.deliver_enabled:
            self.log("delivery skipped (dry run)")
            self.state.mark("deliver", "done", skipped=True)
            return True
        command_json = os.environ.get("BRIEFING_DELIVER_COMMAND")
        command = json.loads(command_json) if command_json else self.edition.get("conductor.delivery.command")
        if not command:
            raise StageFailed("deliver", "no delivery command configured (set BRIEFING_DELIVER_COMMAND or conductor.delivery.command)")
        subject = self.edition.get("conductor.delivery.subject_template", "{label} - {human_date}").format(
            label=self.edition.label, human_date=human_date(self.date))
        recipients_env = self.edition.get("conductor.delivery.recipients_env")
        recipients = os.environ.get(recipients_env or "", "")
        mapping = {"{date}": self.date, "{pdf}": str(pdf), "{body_text}": str(text_path), "{body_html}": str(html_path),
                   "{edition}": self.edition.edition_id, "{recipients}": recipients, "{subject}": subject}
        argv = []
        for part in command:
            for key, value in mapping.items():
                part = part.replace(key, value)
            argv.append(part)
        if not recipients and "--recipient" in argv:
            index = argv.index("--recipient")
            del argv[index:index + 2]
        for attempt in range(1, 4):
            code, output = self.runner(argv, env=dict(self.env), cwd=str(self.scripts_dir), timeout=900)
            self.log(f"deliver attempt {attempt}: exit={code}")
            if code == 0 and "Email sent" in output:
                break
            self.sleep(30 * attempt)
        else:
            run_reporting.update_run_report(self.date, run_status="failed", delivery_status="failed")
            raise StageFailed("deliver", f"delivery command failed: {output[-300:]}")
        self.state.state["delivered"] = {"at": datetime.now(timezone.utc).isoformat(), "articles": len(articles), "notes": notes}
        self.state.mark("deliver", "done")
        try:
            run_reporting.update_run_report(self.date, run_status="completed", delivery_status="sent")
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
    tz = ZoneInfo(tz_name) if ZoneInfo else timezone.utc
    return datetime.now(tz).strftime("%Y-%m-%d")


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
    conductor = Conductor(
        edition=edition, date=date, scripts_dir=scripts_dir, backend=backend or llm.ClaudeBackend(),
        dry_run=args.dry_run, deliver=not args.no_deliver, audit_fixture=args.audit_fixture,
    )
    if args.diagnose:
        print(conductor.diagnosis())
        return EXIT_OK
    return conductor.run(finish_only=args.finish)


if __name__ == "__main__":
    raise SystemExit(main())
