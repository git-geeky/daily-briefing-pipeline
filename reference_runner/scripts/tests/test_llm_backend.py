"""Backend: stream parsing, command construction, failure classification, watchdogs (no real CLI)."""
from __future__ import annotations

import io
import json
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from _shared_imports import REPO_ROOT  # noqa: F401

import llm_backend_core as backend  # noqa: E402

SUCCESS_STREAM = [
    {"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "session_id": "s1", "tools": []},
    {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "resetsAt": 1792995600, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 0.34, "resetsAt": 1792995600, "status": "allowed"},
                           "seven_day": {"utilization": 0.05, "resetsAt": 1793314800, "status": "allowed"}}}},
    {"type": "assistant", "message": {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": "ok"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "ok", "duration_ms": 1404, "num_turns": 1,
     "total_cost_usd": 0.00016, "terminal_reason": "completed", "session_id": "s1",
     "usage": {"input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 3567, "cache_creation_input_tokens": 10,
               "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 2}}},
]


def lines_of(events):
    return [json.dumps(event) + "\n" for event in events]


class FakeProc:
    def __init__(self, stdout_lines=(), stderr="", returncode=0, block=False):
        self.pid = 4242
        self.stdin = io.StringIO()
        self.returncode = returncode
        self.killed = threading.Event()
        self._block = block
        self._lines = list(stdout_lines)
        outer = self

        class Out:
            def readline(self_inner):
                if outer._lines:
                    return outer._lines.pop(0)
                if outer._block:
                    outer.killed.wait(timeout=20)
                return ""

        self.stdout = Out()
        self.stderr = io.StringIO(stderr)

    def poll(self):
        return self.returncode if (self.killed.is_set() or not self._block) else None

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed.set()


class ParseStreamTests(unittest.TestCase):
    def test_success_stream_yields_text_usage_and_rate_limit(self) -> None:
        result = backend.parse_stream(lines_of(SUCCESS_STREAM))
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "ok")
        self.assertEqual((result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_creation_tokens),
                         (2, 4, 3567, 10))
        self.assertAlmostEqual(result.cost_usd, 0.00016)
        self.assertEqual(result.web_requests, 3)
        self.assertEqual(result.rate_limit.status, "allowed")
        self.assertAlmostEqual(result.rate_limit.five_hour_utilization, 0.34)
        self.assertAlmostEqual(result.rate_limit.max_utilization, 0.34)
        self.assertEqual(result.model_used, "claude-haiku-5-5")

    def test_tool_results_become_the_evidence_corpus(self) -> None:
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "WebFetch", "input": {"url": "https://example.org/paper"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "Sample size n = 412 animals."}]}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "done", "usage": {}},
        ]
        result = backend.parse_stream(lines_of(events))
        self.assertEqual(result.fetched_urls, ["https://example.org/paper"])
        self.assertIn("n = 412", result.tool_corpus[0])

    def test_garbage_lines_are_ignored_and_missing_result_is_not_ok(self) -> None:
        result = backend.parse_stream(["not json\n", "{broken\n", json.dumps(SUCCESS_STREAM[2]) + "\n"])
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "ok")


class ClassificationTests(unittest.TestCase):
    def test_real_production_messages(self) -> None:
        cases = {
            "You've hit your org's monthly usage limit": "org_limit",
            "You've hit your monthly spend limit · raise it at claude.ai/settings/usage": "spend_limit",
            "You've hit your session limit · resets 4:30pm (Asia/Tokyo)": "usage_limit",
            "This request triggered restrictions on violative cyber content and was blocked under Anthropic's Usage Policy.": "refusal",
            "API Error: 529 overloaded_error": "overloaded",
            "429 Too Many Requests": "rate_limited",
            "Invalid API key · Please run /login": "auth",
            "ECONNRESET while fetching": "network",
            "something odd": "error",
        }
        for text, expected in cases.items():
            self.assertEqual(backend.classify_failure(text), expected, text)

    def test_reset_time_is_the_next_occurrence_in_the_stated_zone(self) -> None:
        now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)  # 12:00 in Tokyo
        epoch = backend.parse_reset_time("You've hit your session limit · resets 4:30pm (Asia/Tokyo)", now=now)
        self.assertEqual(datetime.fromtimestamp(epoch, timezone.utc), datetime(2026, 10, 26, 7, 30, tzinfo=timezone.utc))
        later = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)  # 18:00 Tokyo: already past 4:30pm -> tomorrow
        epoch = backend.parse_reset_time("resets 4:30pm (Asia/Tokyo)", now=later)
        self.assertEqual(datetime.fromtimestamp(epoch, timezone.utc), datetime(2026, 10, 27, 7, 30, tzinfo=timezone.utc))
        self.assertIsNone(backend.parse_reset_time("no time here"))


class CommandTests(unittest.TestCase):
    def test_article_command_has_the_unattended_safety_flags(self) -> None:
        request = backend.LLMRequest(stage="article", prompt="x", model="sonnet", effort="medium",
                                     tools=["WebFetch", "WebSearch"], max_budget_usd=4, fallback_models=["opus"])
        argv = backend.build_claude_command(request)
        for flag in ("-p", "--no-session-persistence", "--safe-mode", "--verbose"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "AskUserQuestion")
        self.assertEqual(argv[argv.index("--tools") + 1], "WebFetch,WebSearch")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "WebFetch,WebSearch")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "4.00")
        self.assertEqual(argv[argv.index("--fallback-model") + 1], "opus")
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_toolless_stage_disables_all_tools(self) -> None:
        argv = backend.build_claude_command(backend.LLMRequest(stage="header", prompt="x", tools=[]))
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", argv)


class RunTests(unittest.TestCase):
    def run_with(self, proc, **request_overrides):
        be = backend.ClaudeBackend(popen=lambda *a, **k: proc)
        request = backend.LLMRequest(stage="t", prompt="hello", **request_overrides)
        return be.run(request), proc

    def test_success(self) -> None:
        result, proc = self.run_with(FakeProc(lines_of(SUCCESS_STREAM)))
        self.assertTrue(result.ok)
        self.assertEqual(proc.stdin.getvalue(), "hello") if not proc.stdin.closed else None
        self.assertEqual(result.text, "ok")

    def test_session_limit_is_classified_with_a_reset_hint(self) -> None:
        events = [{"type": "result", "subtype": "success", "is_error": True,
                   "result": "You've hit your session limit · resets 4:30pm (Asia/Tokyo)", "usage": {}}]
        result, _ = self.run_with(FakeProc(lines_of(events)))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "usage_limit")
        self.assertIsNotNone(result.reset_hint)

    def test_the_models_own_prose_about_limits_is_not_a_platform_limit(self) -> None:
        """Review finding: an AI-news article mentioning a 'monthly spend limit' halted the run as a human-action failure."""
        events = [
            {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "The vendor raised its monthly spend limit and the rate limit for enterprise tiers."}]}},
            {"type": "result", "subtype": "error_max_turns", "is_error": True, "usage": {}},
        ]
        result, _ = self.run_with(FakeProc(lines_of(events)))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "error")

    def test_org_limit_needs_a_human(self) -> None:
        events = [{"type": "result", "subtype": "error_during_execution", "is_error": True,
                   "result": "You've hit your org's monthly usage limit", "usage": {}}]
        result, _ = self.run_with(FakeProc(lines_of(events)))
        self.assertEqual(result.error_kind, "org_limit")
        self.assertIn(result.error_kind, backend.HUMAN_ACTION_KINDS)

    def test_budget_exhaustion_is_its_own_kind(self) -> None:
        events = [{"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": "", "usage": {},
                   "terminal_reason": "error_max_budget_usd"}]
        result, _ = self.run_with(FakeProc(lines_of(events)))
        self.assertEqual(result.error_kind, "budget")

    def test_no_output_at_all_reports_an_error_not_success(self) -> None:
        result, _ = self.run_with(FakeProc([], stderr="boom", returncode=1))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "error")

    def test_idle_watchdog_kills_a_hung_process(self) -> None:
        proc = FakeProc([], block=True, returncode=-9)
        result, _ = self.run_with(proc, idle_timeout_s=1.2, timeout_s=30)
        self.assertEqual(result.error_kind, "timeout")
        self.assertTrue(proc.killed.is_set())

    def test_missing_executable_reports_what_failed_to_start(self) -> None:
        def boom(*a, **k):
            raise FileNotFoundError("No such file or directory: 'claude'")

        result = backend.ClaudeBackend(popen=boom).run(backend.LLMRequest(stage="t", prompt="x"))
        self.assertFalse(result.ok)
        self.assertIn("could not start", result.error_detail)
        self.assertIn("No such file", result.error_detail)

    def test_missing_working_directory_is_created_not_misreported(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "does" / "not" / "exist"
            seen = {}

            def fake_popen(*a, **k):
                seen["cwd_exists"] = Path(k["cwd"]).is_dir()
                return FakeProc(lines_of(SUCCESS_STREAM))

            result = backend.ClaudeBackend(popen=fake_popen).run(backend.LLMRequest(stage="t", prompt="x", cwd=str(target)))
        self.assertTrue(result.ok)
        self.assertTrue(seen["cwd_exists"])


class ExecutableResolutionTests(unittest.TestCase):
    def test_explicit_env_override_wins_then_path_lookup_then_the_bare_name(self) -> None:
        import os

        previous = os.environ.pop("BRIEFING_CLAUDE_EXE", None)
        try:
            os.environ["BRIEFING_CLAUDE_EXE"] = r"C:\Users\admin\bin\claude.exe"
            self.assertEqual(backend.ClaudeBackend().executable, r"C:\Users\admin\bin\claude.exe")
            del os.environ["BRIEFING_CLAUDE_EXE"]
            self.assertEqual(backend.ClaudeBackend(executable="definitely-not-installed-xyz").executable, "definitely-not-installed-xyz")
        finally:
            os.environ.pop("BRIEFING_CLAUDE_EXE", None)
            if previous is not None:
                os.environ["BRIEFING_CLAUDE_EXE"] = previous


class CodexCommandTests(unittest.TestCase):
    def test_exec_command_is_read_only_and_reads_stdin(self) -> None:
        argv = backend.CodexBackend(model="gpt-x").build_command(backend.LLMRequest(stage="t", prompt="x"), Path("/tmp/o.txt"))
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertIn("read-only", argv)
        self.assertEqual(argv[-1], "-")


class ToolEchoTests(unittest.TestCase):
    """Review T1: what the model itself sent (its search query, the prompt a redirect notice hands back) is not evidence."""

    def stream(self, results):
        events = [{"type": "assistant", "message": {"model": "m", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": name, "input": {"url": "https://e.org/x"}} for i, (name, _) in enumerate(results)]}}]
        events.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": text} for i, (_, text) in enumerate(results)]}})
        events.append({"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.01,
                       "usage": {"input_tokens": 1, "output_tokens": 1}})
        return lines_of(events)

    def test_the_search_query_line_is_not_added_to_the_corpus(self) -> None:
        invented = 'Web search results for query: "the entire population collapsed by 83 percent within two breeding seasons"\n\nNo results found.'
        result = backend.parse_stream(self.stream([("WebSearch", invented)]))
        joined = "\n".join(result.tool_corpus)
        self.assertNotIn("83 percent", joined)
        self.assertIn("No results found", joined)

    def test_the_prompt_echoed_by_a_redirect_notice_is_not_added_to_the_corpus(self) -> None:
        notice = ('REDIRECT DETECTED: The URL redirects to a different host.\n\nOriginal URL: https://doi.org/10.1/x\nRedirect URL: https://pub.org/x\n'
                  'Status: 301\n\nTo complete your request, use WebFetch again with these parameters:\n- url: "https://pub.org/x"\n'
                  '- prompt: "Quote the sentence stating that 91% of voles died in the first winter"')
        result = backend.parse_stream(self.stream([("WebFetch", notice)]))
        joined = "\n".join(result.tool_corpus)
        self.assertNotIn("91%", joined)
        self.assertIn("https://pub.org/x", joined)

    def test_real_page_answers_are_kept(self) -> None:
        result = backend.parse_stream(self.stream([("WebFetch", "The page says the population fell by 62% to 1,050 individuals.")]))
        self.assertIn("62%", "\n".join(result.tool_corpus))


class CostTelemetryTests(unittest.TestCase):
    """Review COST-1: a reported cost of zero beside real token counts must not blind the run budget."""

    def test_zero_reported_cost_with_tokens_is_estimated_and_marked(self) -> None:
        events = [{"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "x"}]}},
                  {"type": "result", "subtype": "success", "is_error": False, "result": "x", "total_cost_usd": 0,
                   "usage": {"input_tokens": 100_000, "output_tokens": 20_000}}]
        result = backend.parse_stream(lines_of(events))
        self.assertGreater(result.cost_usd, 1.0)
        self.assertTrue(result.cost_estimated)

    def test_a_genuine_small_cost_is_kept(self) -> None:
        result = backend.parse_stream(lines_of(SUCCESS_STREAM))
        self.assertAlmostEqual(result.cost_usd, 0.00016)
        self.assertFalse(result.cost_estimated)

    def test_utilisation_telemetry_is_detected_as_missing_when_the_stream_changes_shape(self) -> None:
        renamed = json.loads(json.dumps(SUCCESS_STREAM))
        renamed[1]["rate_limit_info"]["windows"] = renamed[1]["rate_limit_info"].pop("unifiedWindows")
        self.assertTrue(backend.parse_stream(lines_of(SUCCESS_STREAM)).rate_limit.has_utilization)
        self.assertFalse(backend.parse_stream(lines_of(renamed)).rate_limit.has_utilization)


class ChildEnvironmentTests(unittest.TestCase):
    def test_private_settings_and_unrelated_credentials_are_not_passed_to_the_model_cli(self) -> None:
        env = backend.child_environment({
            "PATH": "/usr/bin", "HOME": "/home/u", "USERPROFILE": "C:\\Users\\u", "HTTPS_PROXY": "http://p:1", "ANTHROPIC_API_KEY": "k",
            "CLAUDE_CODE_OAUTH_TOKEN": "t", "BRIEFING_DELIVER_COMMAND": "powershell C:\\Users\\u\\x.ps1", "BRIEFING_RECIPIENTS": "a@b.c",
            "ANIMAL_BRIEFING_EXTRA_RECIPIENTS": "d@e.f", "GMAIL_APP_PASSWORD": "p", "GITHUB_TOKEN": "g", "AWS_SECRET_ACCESS_KEY": "s",
            "OPENAI_API_KEY": "o", "SMTP_HOST": "h"}, extra={"CUSTOM": "1"})
        self.assertEqual(sorted(env), ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CUSTOM", "HOME", "HTTPS_PROXY", "PATH", "USERPROFILE"])


class ResetHintTests(unittest.TestCase):
    def run_backend(self, events):
        proc = FakeProc(lines_of(events))
        return backend.ClaudeBackend(executable="claude", popen=lambda *a, **k: proc).run(backend.LLMRequest(stage="t", prompt="hello"))

    def test_the_structured_timestamp_wins_over_the_prose(self) -> None:
        events = [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1792995600}},
                  {"type": "result", "subtype": "success", "is_error": True,
                   "result": "You've hit your session limit \u00b7 resets 4:30pm (Asia/Tokyo)", "usage": {}}]
        result = self.run_backend(events)
        self.assertEqual(result.reset_hint, 1792995600)

    def test_prose_without_a_zone_is_read_in_local_time_not_utc(self) -> None:
        now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
        epoch = backend.parse_reset_time("resets 4:30pm", now=now)
        when = datetime.fromtimestamp(epoch, timezone.utc).astimezone()  # the machine's own zone
        self.assertEqual((when.hour, when.minute), (16, 30))
        self.assertGreater(epoch, now.timestamp())
        self.assertLessEqual(epoch - now.timestamp(), 25 * 3600)


if __name__ == "__main__":
    unittest.main()
