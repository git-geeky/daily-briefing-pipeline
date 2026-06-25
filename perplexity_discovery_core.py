from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from briefing_manifest import BANNED_TOPIC_RULES


URL_RE = re.compile(r"https?://[^\s)\]]+")


def _banned_topic_exclusion_phrase() -> str:
    """Human-readable exclusion clause derived from the canonical BANNED_TOPIC_RULES
    in briefing_manifest. Keeps the discovery prompts in sync with the validator's
    hard banned-topic list so the two can't drift. Discovery prompts may add their
    own *soft* exclusions (sports, cars/EVs, gadget/viral filler) on top of this."""
    labels = [label for _pattern, label in BANNED_TOPIC_RULES]
    if len(labels) > 1:
        return ", ".join(labels[:-1]) + ", and " + labels[-1]
    return labels[0] if labels else ""


# SYNC: derived from briefing_manifest.BANNED_TOPIC_RULES — do not hand-edit the
# banned-topic names here; add/remove them in BANNED_TOPIC_RULES instead.
BANNED_TOPIC_EXCLUSION_PHRASE = _banned_topic_exclusion_phrase()

DEFAULT_NODE = Path(r"C:\Program Files\nodejs\node.exe")
DEFAULT_CLI = (
    Path.home()
    / ".codex"
    / "mcp-test"
    / "perplexity-user-mcp"
    / "node_modules"
    / "perplexity-user-mcp"
    / "dist"
    / "cli.mjs"
)
DEFAULT_CONFIG_DIR = Path.home() / ".perplexity-mcp-community-test"
DEFAULT_BROWSER = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


@dataclass(frozen=True)
class DiscoveryQuery:
    section: str
    label: str
    tool: str
    sources: tuple[str, ...]
    query: str
    timeout_seconds: int = 180

    def arguments(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "sources": list(self.sources),
            "language": "en-US",
        }


def default_queries(date: str) -> list[DiscoveryQuery]:
    date_hint = f"as of {date} JST"
    return [
        DiscoveryQuery(
            section="part1",
            label="general-news-wire-discovery",
            tool="perplexity_search",
            sources=("web",),
            query=(
                f"Find the strongest general world-news stories for a daily intelligence briefing {date_hint}. "
                f"Exclude banned topics ({BANNED_TOPIC_EXCLUSION_PHRASE}), and also exclude sports, cars, and EVs. "
                "Prioritize primary sources, major wires, official schedules, and stories with clear consequences. "
                "Return candidate story titles, exact dates, and source URLs."
            ),
        ),
        DiscoveryQuery(
            section="part2",
            label="ai-news-product-policy-infrastructure",
            tool="perplexity_search",
            sources=("web",),
            query=(
                f"Find current AI product, policy, model, safety, and infrastructure stories for a daily briefing {date_hint}. "
                "Include OpenAI, Anthropic, Google, Meta, Nvidia, focus company, Microsoft, data centers, chips, and regulation when newsworthy. "
                "Return dated candidate story titles and source URLs; prefer primary sources and major wires."
            ),
        ),
        DiscoveryQuery(
            section="part2",
            label="ai-social-counterpoints",
            tool="perplexity_search",
            sources=("social",),
            query=(
                f"What are Hacker News, Reddit, X, and practitioner communities discussing this week about AI models, "
                f"AI coding agents, infrastructure, and developer tools {date_hint}? "
                "Return discussion URLs and explain which threads are useful as counterpoints rather than primary sources."
            ),
        ),
        DiscoveryQuery(
            section="part2",
            label="ai-scholar-research",
            tool="perplexity_search",
            sources=("scholar",),
            query=(
                f"Find recent 2025 or 2026 scholarly papers relevant to AI search, deep research agents, "
                f"citation faithfulness, RAG evaluation, and AI coding-agent evaluation {date_hint}. "
                "Return paper titles, dates, and URLs."
            ),
        ),
        DiscoveryQuery(
            section="part3",
            label="non-ai-tech-semiconductors-cloud-cyber",
            tool="perplexity_search",
            sources=("web",),
            query=(
                f"Find non-AI technology stories for a daily intelligence briefing {date_hint}: "
                "semiconductors, cloud infrastructure, cybersecurity, telecom, enterprise software, supply chains, and focus-company-relevant developments. "
                f"Exclude banned topics ({BANNED_TOPIC_EXCLUSION_PHRASE}), and also exclude cars, sports, and gadget filler. Return dated source URLs."
            ),
        ),
        DiscoveryQuery(
            section="part4",
            label="japan-news-reader-value",
            tool="perplexity_search",
            sources=("web",),
            query=(
                f"Find Japan news with reader value for an international daily intelligence briefing {date_hint}: "
                "policy, markets, technology, security, demographics, disasters, courts, and major corporate moves. "
                "Return exact dates, source URLs, and why each story materially moved."
            ),
        ),
        DiscoveryQuery(
            section="humaninterest",
            label="human-interest-substantive",
            tool="perplexity_search",
            sources=("web",),
            query=(
                f"Find one substantive human-interest story suitable for a serious daily intelligence briefing {date_hint}. "
                f"Avoid banned topics ({BANNED_TOPIC_EXCLUSION_PHRASE}), and also avoid sports and viral filler. Prefer stories grounded in public records, science, community impact, or institutional change. "
                "Return source URLs."
            ),
        ),
        DiscoveryQuery(
            section="updates",
            label="material-updates",
            tool="perplexity_search",
            sources=("web",),
            query=(
                f"Find material updates in the last 48 hours to major world, AI, tech, or Japan stories {date_hint}. "
                "Only include items with verifiable movement, not repeated background. Return source URLs and dates."
            ),
        ),
        DiscoveryQuery(
            section="closing",
            label="closing-synthesis",
            tool="perplexity_reason",
            sources=("web",),
            query=(
                f"Reason across current world, AI, infrastructure, and Japan news {date_hint}. "
                "Identify 3 structural forces for the closing analysis of a daily intelligence briefing. "
                "Use source URLs only as discovery leads; do not ask the final briefing to cite Perplexity itself."
            ),
            timeout_seconds=240,
        ),
    ]


class McpClient:
    def __init__(self, *, node: Path, cli: Path, config_dir: Path, browser_path: Path | None) -> None:
        self.node = node
        self.cli = cli
        self.config_dir = config_dir
        self.browser_path = browser_path
        self.proc: subprocess.Popen[str] | None = None
        self.responses: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.stderr_lines: list[str] = []
        self._next_id = 1

    def start(self) -> dict[str, Any]:
        env = os.environ.copy()
        env.update(
            {
                "PERPLEXITY_CONFIG_DIR": str(self.config_dir),
                "PERPLEXITY_MCP_STDIO": "1",
                "PERPLEXITY_NO_DAEMON": "1",
                "PERPLEXITY_BROWSER_CHANNEL": "chrome",
            }
        )
        if self.browser_path:
            env["PERPLEXITY_BROWSER_PATH"] = str(self.browser_path)

        self.proc = subprocess.Popen(
            [str(self.node), str(self.cli)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        init_id = self._send(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "daily-briefing-perplexity-discovery", "version": "0.1"},
            },
        )
        init = self._wait_for(init_id, 20)
        self._notify("notifications/initialized", {})
        return init

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        call_id = self._send("tools/call", {"name": name, "arguments": arguments})
        return self._wait_for(call_id, timeout)

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.responses.put(json.loads(line))
            except json.JSONDecodeError:
                self.responses.put({"parse_error": line})

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            if line.strip():
                self.stderr_lines.append(line.rstrip())

    def _send(self, method: str, params: dict[str, Any]) -> int:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("MCP server is not running")
        request_id = self._next_id
        self._next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        return request_id

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("MCP server is not running")
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()

    def _wait_for(self, request_id: int, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        pending: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                message = self.responses.get(timeout=0.25)
            except queue.Empty:
                continue
            if message.get("id") == request_id:
                for item in pending:
                    self.responses.put(item)
                return message
            pending.append(message)
        for item in pending:
            self.responses.put(item)
        return {"timeout": True, "id": request_id}


def extract_text(response: dict[str, Any]) -> str:
    if response.get("timeout"):
        return ""
    result = response.get("result") or {}
    content = result.get("content") or []
    return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))


def analyze_text(text: str) -> dict[str, Any]:
    urls = [match.group(0).rstrip(".,;:") for match in URL_RE.finditer(text)]
    return {
        "chars": len(text),
        "words": len(text.split()),
        "url_count": len(urls),
        "unique_url_count": len(set(urls)),
        "sample_urls": sorted(set(urls))[:12],
        "says_no_live_access": bool(re.search(r"do not have live access|don't have live access|can't access current", text, re.I)),
    }


def markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Perplexity MCP Discovery Report",
        "",
        f"- Date: {payload['date']}",
        f"- Generated: {payload['generated_at']}",
        f"- Runner: {payload['runner']}",
        f"- MCP server: {payload.get('server_info', {}).get('name', 'unknown')} {payload.get('server_info', {}).get('version', '')}",
        f"- Query count: {len(payload['results'])}",
        "",
        "Use this report as a source-discovery and counterpoint artifact. Do not cite Perplexity itself in the final briefing; cite the underlying source URLs after validation.",
        "",
    ]
    for item in payload["results"]:
        analysis = item["analysis"]
        lines.extend(
            [
                f"## {item['section']} / {item['label']}",
                "",
                f"- Tool: `{item['tool']}`",
                f"- Sources mode: `{', '.join(item['sources'])}`",
                f"- Elapsed: {item['elapsed_seconds']:.1f}s",
                f"- Error: `{item['is_error']}`",
                f"- Words: {analysis['words']}",
                f"- Unique URLs: {analysis['unique_url_count']}",
                f"- Says no live access: `{analysis['says_no_live_access']}`",
                "",
                "### Query",
                "",
                item["query"],
                "",
                "### URL Leads",
                "",
            ]
        )
        if analysis["sample_urls"]:
            lines.extend(f"- {url}" for url in analysis["sample_urls"])
        else:
            lines.append("- none")
        lines.extend(["", "### Preview", "", item["text"][:1800].strip() or "_No text returned._", ""])
    if payload.get("stderr_tail"):
        lines.extend(["## MCP stderr tail", "", "```text", payload["stderr_tail"], "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def run_discovery(
    *,
    date: str,
    briefing_paths_module: Any,
    node: Path,
    cli: Path,
    config_dir: Path,
    browser_path: Path | None,
    query_limit: int | None,
    dry_run: bool,
) -> dict[str, Any]:
    context = briefing_paths_module.runner_context()
    context.ensure_layout()
    queries = default_queries(date)
    if query_limit is not None:
        queries = queries[:query_limit]
    if dry_run:
        return {
            "date": date,
            "runner": context.profile.runner_name,
            "query_count": len(queries),
            "queries": [query.__dict__ for query in queries],
        }

    for required_path, label in ((node, "node"), (cli, "perplexity-user-mcp cli")):
        if not required_path.exists():
            raise FileNotFoundError(f"{label} not found at {required_path}")

    client = McpClient(node=node, cli=cli, config_dir=config_dir, browser_path=browser_path)
    init = client.start()
    started = datetime.now().isoformat(timespec="seconds")
    results: list[dict[str, Any]] = []
    try:
        for query in queries:
            start = time.monotonic()
            response = client.call_tool(query.tool, query.arguments(), timeout=query.timeout_seconds)
            elapsed = time.monotonic() - start
            text = extract_text(response)
            results.append(
                {
                    "section": query.section,
                    "label": query.label,
                    "tool": query.tool,
                    "sources": list(query.sources),
                    "query": query.query,
                    "elapsed_seconds": round(elapsed, 3),
                    "is_error": bool((response.get("result") or {}).get("isError") or response.get("error") or response.get("timeout")),
                    "response": response,
                    "text": text,
                    "analysis": analyze_text(text),
                }
            )
    finally:
        stderr_tail = "\n".join(client.stderr_lines[-40:])
        client.close()

    payload = {
        "date": date,
        "generated_at": started,
        "runner": context.profile.runner_name,
        "artifact_stem": context.profile.artifact_stem,
        "provider": "perplexity-user-mcp",
        "server_info": (init.get("result") or {}).get("serverInfo", {}),
        "tool_policy": "Perplexity MCP is used for discovery, clustering, and counterpoints only; final citations must use underlying sources.",
        "paths": {
            "json": str(context.perplexity_discovery_path(date)),
            "markdown": str(context.perplexity_discovery_markdown_path(date)),
        },
        "results": results,
        "stderr_tail": stderr_tail,
    }
    context.perplexity_discovery_path(date).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    context.perplexity_discovery_markdown_path(date).write_text(markdown_report(payload), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Perplexity MCP discovery for a daily briefing.")
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD.")
    parser.add_argument("--node", default=os.environ.get("PERPLEXITY_MCP_NODE", str(DEFAULT_NODE)))
    parser.add_argument("--cli", default=os.environ.get("PERPLEXITY_MCP_CLI", str(DEFAULT_CLI)))
    parser.add_argument("--config-dir", default=os.environ.get("PERPLEXITY_CONFIG_DIR", str(DEFAULT_CONFIG_DIR)))
    parser.add_argument("--browser-path", default=os.environ.get("PERPLEXITY_BROWSER_PATH", str(DEFAULT_BROWSER)))
    parser.add_argument("--query-limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, briefing_paths_module: Any | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if briefing_paths_module is None:
        import briefing_paths as briefing_paths_module

    payload = run_discovery(
        date=args.date,
        briefing_paths_module=briefing_paths_module,
        node=Path(args.node),
        cli=Path(args.cli),
        config_dir=Path(args.config_dir),
        browser_path=Path(args.browser_path) if args.browser_path else None,
        query_limit=args.query_limit,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Wrote {payload['paths']['json']}")
        print(f"Wrote {payload['paths']['markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

