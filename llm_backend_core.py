"""Model backends for the conductor: one function, ``backend.run(request) -> LLMResult``.

``ClaudeBackend`` drives ``claude -p --output-format stream-json`` as a child process, feeds the
prompt on stdin, and turns the event stream into a structured result: final text, token and cost
accounting, the account's rate-limit state (the CLI emits ``rate_limit_event`` with five-hour and
seven-day utilisation), the text of every tool result (the evidence corpus the provenance checker
verifies claims against), and a classified failure when something goes wrong.

Failure classification matters more than the happy path. The briefings stopped in 2026 because
runs died on ``You've hit your org's monthly usage limit`` / ``monthly spend limit`` /
``session limit`` and nothing noticed. Each of those is a distinct ``error_kind`` here so the
conductor can defer, alert, or fall back instead of retrying blindly.

Nothing here touches the network itself; the only side effect is the subprocess.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

try:  # zoneinfo needs tzdata on Windows; fall back to UTC offsets we can derive from the epoch
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

# --------------------------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------------------------
FAILURE_KINDS = (
    "usage_limit",   # five-hour/session window exhausted; resets soon
    "weekly_limit",  # seven-day window exhausted
    "org_limit",     # organisation monthly usage limit: a human has to act
    "spend_limit",   # monthly spend cap: a human has to act
    "rate_limited",  # transient 429
    "overloaded",    # transient 529 / capacity
    "refusal",       # usage-policy classifier blocked the request (try the fallback model)
    "budget",        # per-call --max-budget-usd reached
    "auth",          # not logged in / token expired: a human has to act
    "timeout",       # wall-clock or idle watchdog fired
    "network",       # connection problems
    "empty",         # the model returned nothing usable
    "error",         # anything else
)
HUMAN_ACTION_KINDS = frozenset({"org_limit", "spend_limit", "auth"})
RETRYABLE_KINDS = frozenset({"rate_limited", "overloaded", "timeout", "network", "empty", "error"})
DEFER_KINDS = frozenset({"usage_limit", "weekly_limit", "org_limit", "spend_limit"})


@dataclass
class RateLimitInfo:
    status: str = "unknown"             # allowed | allowed_warning | rejected | unknown
    resets_at: float | None = None      # epoch seconds
    five_hour_utilization: float | None = None
    seven_day_utilization: float | None = None
    window: str | None = None

    @property
    def max_utilization(self) -> float:
        values = [v for v in (self.five_hour_utilization, self.seven_day_utilization) if v is not None]
        return max(values) if values else 0.0


@dataclass
class LLMRequest:
    stage: str
    prompt: str
    model: str = "sonnet"
    effort: str | None = None
    tools: list[str] | None = None            # built-in tool set; [] disables all tools
    system_append: str | None = None
    max_budget_usd: float | None = None
    fallback_models: list[str] = field(default_factory=list)
    timeout_s: float = 1800.0
    idle_timeout_s: float = 600.0
    label: str | None = None                  # e.g. the article slug, for logs
    json_schema: dict | None = None
    cwd: str | None = None
    keep_tool_corpus: bool = True


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    error_kind: str | None = None
    error_detail: str = ""
    model_used: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    num_turns: int = 0
    session_id: str | None = None
    terminal_reason: str | None = None
    rate_limit: RateLimitInfo = field(default_factory=RateLimitInfo)
    tool_corpus: list[str] = field(default_factory=list)   # text of tool results (evidence the model actually saw)
    fetched_urls: list[str] = field(default_factory=list)
    web_requests: int = 0
    reset_hint: float | None = None                         # epoch when a limit says it clears
    stderr_tail: str = ""
    cost_estimated: bool = False                            # True when the CLI was killed before it reported a cost

    def usage_dict(self) -> dict[str, Any]:
        return {
            "model": self.model_used, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens, "cache_creation_tokens": self.cache_creation_tokens,
            "cost_usd": self.cost_usd, "duration_seconds": self.duration_s, "cost_estimated": self.cost_estimated,
        }


class Backend(Protocol):
    name: str

    def run(self, request: LLMRequest) -> LLMResult: ...


# --------------------------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------------------------
_FAILURE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"monthly spend limit|spend limit", re.I), "spend_limit"),
    (re.compile(r"org(?:anization)?(?:'s|’s)?\s+monthly usage limit|monthly usage limit", re.I), "org_limit"),
    (re.compile(r"weekly limit|7[- ]day limit|seven[- ]day limit", re.I), "weekly_limit"),
    (re.compile(r"session limit|five[- ]hour limit|usage limit reached|hit your .*limit", re.I), "usage_limit"),
    (re.compile(r"violative cyber content|violative .* content|usage policy|content policy", re.I), "refusal"),
    (re.compile(r"overloaded|\b529\b|capacity", re.I), "overloaded"),
    (re.compile(r"rate[ _-]?limit|\b429\b|too many requests", re.I), "rate_limited"),
    (re.compile(r"invalid api key|not logged in|please run /login|oauth token|authentication[_ ]error|\b401\b|credit balance is too low", re.I), "auth"),
    (re.compile(r"econnreset|etimedout|enotfound|econnrefused|network error|fetch failed|socket hang up|connection (?:reset|refused|error)", re.I), "network"),
)

_RESET_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([A-Za-z_]+/[A-Za-z_]+|UTC)\))?", re.I)


def classify_failure(*texts: str | None) -> str:
    haystack = " \n".join(t for t in texts if t)
    if not haystack.strip():
        return "error"
    for pattern, kind in _FAILURE_PATTERNS:
        if pattern.search(haystack):
            return kind
    return "error"


def parse_reset_time(text: str | None, *, now: datetime | None = None) -> float | None:
    """``You've hit your session limit · resets 4:30pm (Asia/Tokyo)`` -> epoch of the next 4:30pm Tokyo."""
    match = _RESET_RE.search(text or "")
    if not match:
        return None
    hour, minute, meridian, tz_name = int(match.group(1)), int(match.group(2) or 0), (match.group(3) or "").lower(), match.group(4)
    if meridian == "pm" and hour < 12:
        hour += 12
    elif meridian == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    tz = timezone.utc
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - unknown zone: assume UTC rather than guess wrongly
            tz = timezone.utc
    reference = (now or datetime.now(timezone.utc)).astimezone(tz)
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= reference:
        candidate += timedelta(days=1)
    return candidate.timestamp()


# --------------------------------------------------------------------------------------------
# Claude CLI backend
# --------------------------------------------------------------------------------------------
def build_claude_command(request: LLMRequest, *, executable: str = "claude", safe_mode: bool = True,
                         skip_permissions: bool = False) -> list[str]:
    argv = [
        executable, "-p", "--model", request.model, "--output-format", "stream-json", "--verbose",
        "--no-session-persistence", "--permission-prompts", "none", "--exclude-dynamic-system-prompt-sections",
        "--disallowedTools", "AskUserQuestion",
    ]
    if request.effort:
        argv += ["--effort", request.effort]
    if safe_mode:
        argv.append("--safe-mode")  # no user CLAUDE.md / skills / hooks / MCP: reproducible and cheap
    if request.tools is not None:
        argv += ["--tools", ",".join(request.tools)]
        if request.tools:
            argv += ["--allowedTools", ",".join(request.tools)]
    if skip_permissions:
        argv.append("--dangerously-skip-permissions")
    if request.max_budget_usd:
        argv += ["--max-budget-usd", f"{request.max_budget_usd:.2f}"]
    if request.fallback_models:
        argv += ["--fallback-model", ",".join(request.fallback_models)]
    if request.system_append:
        argv += ["--append-system-prompt", request.system_append]
    if request.json_schema:
        argv += ["--json-schema", json.dumps(request.json_schema)]
    return argv


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


# API-equivalent $ per million tokens (input, output). Deliberately on the high side: this prices only calls that were
# killed before the CLI reported a cost, and an over-estimate makes the run-budget guard conservative, not reckless.
_PRICE_PER_MTOK = {"opus": (15.0, 75.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}


def estimate_cost_usd(model: str | None, *, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_creation: int = 0) -> float:
    family = next((name for name in _PRICE_PER_MTOK if name in (model or "").lower()), "sonnet")
    price_in, price_out = _PRICE_PER_MTOK[family]
    return round((input_tokens * price_in + cache_read * price_in * 0.1 + cache_creation * price_in * 1.25
                  + output_tokens * price_out) / 1_000_000, 4)


def parse_stream(lines: list[str], *, keep_corpus: bool = True) -> LLMResult:
    """Fold a ``stream-json`` event list into an LLMResult (no failure classification yet).

    When the stream ends without a ``result`` event (the process was killed on a timeout) the tokens of the
    assistant messages seen so far are summed and priced, so that spend is not silently lost from the budget."""
    result = LLMResult(ok=False)
    final_event: dict | None = None
    assistant_text: list[str] = []
    message_usage: dict[str, dict] = {}
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            result.model_used = event.get("model") or result.model_used
            result.session_id = event.get("session_id") or result.session_id
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            windows = info.get("unifiedWindows") or {}
            result.rate_limit = RateLimitInfo(
                status=str(info.get("status") or "unknown"),
                resets_at=info.get("resetsAt"),
                five_hour_utilization=(windows.get("five_hour") or {}).get("utilization"),
                seven_day_utilization=(windows.get("seven_day") or {}).get("utilization"),
                window=info.get("rateLimitType"),
            )
        elif kind == "assistant":
            message = event.get("message") or {}
            result.model_used = message.get("model") or result.model_used
            if isinstance(message.get("usage"), dict):
                message_usage[str(message.get("id") or f"anon-{len(message_usage)}")] = message["usage"]  # a message may stream repeatedly
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    assistant_text.append(block["text"])
                elif block.get("type") == "tool_use":
                    url = (block.get("input") or {}).get("url")
                    if url:
                        result.fetched_urls.append(str(url))
        elif kind == "user" and keep_corpus:
            content = (event.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        text = _tool_result_text(block)
                        if text:
                            result.tool_corpus.append(text)
        elif kind == "result":
            final_event = event

    if final_event is not None:
        usage = final_event.get("usage") or {}
        result.input_tokens = int(usage.get("input_tokens") or 0)
        result.output_tokens = int(usage.get("output_tokens") or 0)
        result.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        result.cache_creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
        result.cost_usd = float(final_event.get("total_cost_usd") or 0.0)
        result.duration_s = float(final_event.get("duration_ms") or 0.0) / 1000.0
        result.num_turns = int(final_event.get("num_turns") or 0)
        result.terminal_reason = final_event.get("terminal_reason") or final_event.get("subtype")
        result.web_requests = int(((usage.get("server_tool_use") or {}).get("web_search_requests") or 0)
                                  + ((usage.get("server_tool_use") or {}).get("web_fetch_requests") or 0))
        result.session_id = final_event.get("session_id") or result.session_id
        text = final_event.get("result")
        result.text = text if isinstance(text, str) and text.strip() else "\n".join(assistant_text).strip()
        result.ok = not final_event.get("is_error") and str(final_event.get("subtype") or "success") == "success"
        if not result.ok:
            result.error_detail = (text if isinstance(text, str) else "") or str(final_event.get("subtype") or "")
    else:
        result.text = "\n".join(assistant_text).strip()
        if message_usage:
            totals = {key: sum(int(u.get(key) or 0) for u in message_usage.values())
                      for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
            result.input_tokens, result.output_tokens = totals["input_tokens"], totals["output_tokens"]
            result.cache_read_tokens, result.cache_creation_tokens = totals["cache_read_input_tokens"], totals["cache_creation_input_tokens"]
            result.cost_usd = estimate_cost_usd(result.model_used, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                                                cache_read=result.cache_read_tokens, cache_creation=result.cache_creation_tokens)
            result.cost_estimated = True
    return result


def _kill_tree(process: Any) -> None:
    try:
        if os.name == "nt":  # claude spawns children on Windows; terminate the whole tree
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            process.kill()
    except Exception:  # noqa: BLE001 - best effort
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass


class ClaudeBackend:
    name = "claude"

    def __init__(self, *, executable: str = "claude", safe_mode: bool = True, skip_permissions: bool = False,
                 env: dict[str, str] | None = None, popen: Callable[..., Any] = subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic):
        # Scheduled tasks often run with a thinner PATH than an interactive shell; honour an explicit
        # BRIEFING_CLAUDE_EXE (set by the Windows launcher), else resolve through PATH/PATHEXT.
        resolved = os.environ.get("BRIEFING_CLAUDE_EXE") or shutil.which(executable) or executable
        self.executable, self.safe_mode, self.skip_permissions = resolved, safe_mode, skip_permissions
        self.env, self.popen, self.clock = env, popen, clock

    def run(self, request: LLMRequest) -> LLMResult:
        argv = build_claude_command(request, executable=self.executable, safe_mode=self.safe_mode,
                                    skip_permissions=self.skip_permissions)
        started = self.clock()
        env = {**os.environ, **(self.env or {})}
        if request.cwd:
            try:  # a missing working directory raises FileNotFoundError from Popen, indistinguishable from a missing program
                Path(request.cwd).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        try:
            process = self.popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", env=env, cwd=request.cwd,
            )
        except FileNotFoundError as exc:
            return LLMResult(ok=False, error_kind="error",
                             error_detail=f"could not start {self.executable!r} (cwd={request.cwd!r}): {exc}")
        lines: "queue.Queue[str | None]" = queue.Queue()
        stderr_chunks: list[str] = []

        def pump(stream, sink):
            for line in iter(stream.readline, ""):
                sink(line)
            sink(None)

        threading.Thread(target=pump, args=(process.stdout, lines.put), daemon=True).start()
        threading.Thread(target=pump, args=(process.stderr, lambda l: stderr_chunks.append(l) if l else None), daemon=True).start()
        try:
            process.stdin.write(request.prompt)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        collected: list[str] = []
        timed_out: str | None = None
        last_activity = self.clock()
        while True:
            try:
                item = lines.get(timeout=1.0)
            except queue.Empty:
                now = self.clock()
                if now - started > request.timeout_s:
                    timed_out = f"wall-clock timeout after {request.timeout_s:.0f}s"
                elif now - last_activity > request.idle_timeout_s:
                    timed_out = f"no output for {request.idle_timeout_s:.0f}s"
                if timed_out:
                    _kill_tree(process)
                    break
                if process.poll() is not None and lines.empty():
                    break
                continue
            if item is None:
                break
            last_activity = self.clock()
            collected.append(item)
        try:
            process.wait(timeout=30)
        except Exception:  # noqa: BLE001
            _kill_tree(process)

        result = parse_stream(collected, keep_corpus=request.keep_tool_corpus)
        result.stderr_tail = "".join(stderr_chunks)[-1500:]
        result.duration_s = result.duration_s or (self.clock() - started)
        if timed_out:
            result.ok, result.error_kind, result.error_detail = False, "timeout", timed_out
            return result
        if result.ok and not result.text.strip():
            result.ok, result.error_kind, result.error_detail = False, "empty", "model returned no text"
            return result
        if not result.ok:
            # Classify only error text. The model's own prose ("...monthly spend limit...", "...rate limit...") in an AI-news
            # article must never be mistaken for the platform's limit message and halt the run.
            if result.error_detail:
                evidence = [result.error_detail, result.stderr_tail]
            else:
                stray = [line for line in collected if not line.lstrip().startswith("{")]
                last_prose = result.text if len(result.text) <= 300 else ""  # a short bare message, not an article
                evidence = [result.stderr_tail, " ".join(stray)[-600:], last_prose]
            text_blob = " ".join(evidence)
            result.error_kind = classify_failure(text_blob, result.terminal_reason)
            if result.terminal_reason in ("error_max_budget_usd", "max_budget_usd"):
                result.error_kind = "budget"
            if result.error_kind == "error" and not collected:
                result.error_detail = result.error_detail or f"no output (exit code {process.returncode})"
            if result.error_kind in {"usage_limit", "weekly_limit"}:
                result.reset_hint = parse_reset_time(text_blob) or result.rate_limit.resets_at
            elif result.error_kind in DEFER_KINDS:
                result.reset_hint = result.rate_limit.resets_at
        return result


# --------------------------------------------------------------------------------------------
# Codex backend (experimental, disabled by default; not exercised against a real Codex install)
# --------------------------------------------------------------------------------------------
class CodexBackend:
    """Failover/second-opinion backend using ``codex exec``. Experimental: only argv construction is tested."""

    name = "codex"

    def __init__(self, *, executable: str = "codex", model: str | None = None, popen: Callable[..., Any] = subprocess.run):
        self.executable, self.model, self.popen = executable, model, popen

    def build_command(self, request: LLMRequest, output_file: Path) -> list[str]:
        argv = [self.executable, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-o", str(output_file)]
        if self.model:
            argv += ["-m", self.model]
        argv.append("-")
        return argv

    def run(self, request: LLMRequest) -> LLMResult:
        import tempfile

        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "last-message.txt"
            try:
                completed = self.popen(self.build_command(request, output), input=request.prompt, capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", timeout=request.timeout_s)
            except FileNotFoundError:
                return LLMResult(ok=False, error_kind="error", error_detail="codex executable not found")
            except subprocess.TimeoutExpired:
                return LLMResult(ok=False, error_kind="timeout", error_detail=f"codex timed out after {request.timeout_s:.0f}s")
            text = output.read_text(encoding="utf-8").strip() if output.exists() else ""
        duration = time.monotonic() - started
        if completed.returncode != 0 or not text:
            blob = f"{completed.stdout[-800:]} {completed.stderr[-800:]}"
            return LLMResult(ok=False, error_kind=classify_failure(blob) if completed.returncode else "empty",
                             error_detail=blob.strip()[-400:], duration_s=duration, model_used=self.model or "codex")
        return LLMResult(ok=True, text=text, duration_s=duration, model_used=self.model or "codex")


# --------------------------------------------------------------------------------------------
# Fake backend (tests and dry runs)
# --------------------------------------------------------------------------------------------
class FakeBackend:
    """Scriptable backend. ``script(request)`` returns text, an LLMResult, or raises."""

    name = "fake"

    def __init__(self, script: Callable[[LLMRequest], Any]):
        self.script = script
        self.calls: list[LLMRequest] = []

    def run(self, request: LLMRequest) -> LLMResult:
        self.calls.append(request)
        outcome = self.script(request)
        if isinstance(outcome, LLMResult):
            return outcome
        return LLMResult(
            ok=True, text=str(outcome), model_used=request.model, input_tokens=max(len(request.prompt) // 4, 1),
            output_tokens=max(len(str(outcome)) // 4, 1), cost_usd=0.0, rate_limit=RateLimitInfo(status="allowed"),
        )


__all__ = [
    "Backend", "ClaudeBackend", "CodexBackend", "DEFER_KINDS", "FAILURE_KINDS", "FakeBackend", "HUMAN_ACTION_KINDS",
    "LLMRequest", "LLMResult", "RETRYABLE_KINDS", "RateLimitInfo", "build_claude_command", "classify_failure",
    "parse_reset_time", "parse_stream",
]
