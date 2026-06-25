"""Consensus briefing: merge Claude + Codex outputs into a single corroborated report.

Given assembled manifests and markdowns from both runners for the same date,
produces a consensus briefing where:

- Stories covered by BOTH runners (overlap by `story_id`) → keep the higher-
  scoring version and insert a `*Corroboration: both runners (Claude + Codex).*`
  tag at the top of the article body.
- Stories unique to one runner → include with a `*Source: Claude only.*` or
  `*Source: Codex only.*` tag.
- Disagreements (both covered but first-200-word Jaccard similarity < 0.4) →
  include the higher-scoring version with a `### Disagreement` block appended
  that shows the other runner's framing for the same story.

Output: `Documents/Daily-Briefings/generated/consensus-briefing-{date}.md`

Gated behind `BRIEFING_SEND_CONSENSUS=1` env var at the delivery layer
(see `cross_runner_watch.py`). This module itself always produces the merged
markdown; the env var only controls whether it's emailed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HOME = Path.home()

RUNNER_PATHS = {
    "claude": HOME / ".claude" / "scheduled-tasks" / "daily-intelligence-briefing" / "scripts" / "briefing_paths.py",
    "codex": HOME / ".codex" / "skills" / "news-briefing" / "scripts" / "briefing_paths.py",
}

CONSENSUS_OUTPUT_DIR = HOME / "Documents" / "Daily-Briefings" / "generated"


def _load_runner_paths(runner: str):
    path = RUNNER_PATHS[runner]
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"_merge_{runner}_paths", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_runner_bundle(runner: str, date: str) -> dict[str, Any]:
    paths_mod = _load_runner_paths(runner)
    md_path = Path(paths_mod.assembled_markdown_path(date))
    manifest_path = Path(paths_mod.assembled_manifest_path(date))
    if not md_path.exists() or not manifest_path.exists():
        raise FileNotFoundError(f"{runner} outputs not found for {date}: {md_path} / {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    markdown = md_path.read_text(encoding="utf-8")
    # Index articles by story_id by walking the manifest sections
    articles: dict[str, dict[str, Any]] = {}
    for section_key, section in manifest.get("sections", {}).items():
        if section_key == "header":
            continue
        for item in section.get("items", []):
            sid = item.get("story_id")
            if sid:
                articles[sid] = {
                    "story_id": sid,
                    "section": section_key,
                    "title": item.get("title"),
                    "heading": item.get("heading"),
                    "word_count": item.get("word_count", 0),
                    "score": (item.get("score") or {}).get("overall", 0),
                    "source_count": item.get("source_count", 0),
                    "high_confidence_images": (item.get("image_quality") or {}).get("high_confidence_count", 0),
                }

    # Pull article body text from the assembled markdown by heading
    for sid, meta in articles.items():
        heading = meta["heading"]
        meta["body"] = extract_body_by_heading(markdown, heading)
    return {"runner": runner, "manifest": manifest, "articles": articles, "markdown_path": md_path}


def extract_body_by_heading(markdown: str, heading: str) -> str:
    """Find `## {heading}` in the assembled markdown and return the body up to the next `##`."""
    pattern = re.compile(r"(?m)^##\s+" + re.escape(heading) + r"\s*$")
    match = pattern.search(markdown)
    if not match:
        return ""
    start = match.end()
    next_h2 = re.search(r"(?m)^##\s", markdown[start:])
    if next_h2:
        return markdown[start : start + next_h2.start()].strip()
    return markdown[start:].strip()


def jaccard_first_n_words(a: str, b: str, n: int = 200) -> float:
    """Word-level Jaccard similarity on the first `n` words of each body."""
    def tokenize(text: str, limit: int) -> set[str]:
        words = re.findall(r"\w+", text.lower())
        return set(words[:limit])
    sa, sb = tokenize(a, n), tokenize(b, n)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def build_consensus(date: str) -> str:
    bundles = {runner: load_runner_bundle(runner, date) for runner in RUNNER_PATHS}
    claude = bundles["claude"]
    codex = bundles["codex"]

    claude_articles = claude["articles"]
    codex_articles = codex["articles"]

    overlap_ids = sorted(set(claude_articles) & set(codex_articles))
    claude_only = sorted(set(claude_articles) - set(codex_articles))
    codex_only = sorted(set(codex_articles) - set(claude_articles))

    sections_output: dict[str, list[str]] = {
        "part1": [], "part2": [], "part3": [], "part4": [],
        "humaninterest": [], "updates": [], "closing": [],
    }

    for sid in overlap_ids:
        ca = claude_articles[sid]
        co = codex_articles[sid]
        winner = ca if ca["score"] >= co["score"] else co
        loser = co if winner is ca else ca
        winner_runner = "Claude" if winner is ca else "Codex"
        loser_runner = "Codex" if winner is ca else "Claude"

        similarity = jaccard_first_n_words(ca["body"], co["body"])
        tag = f"*Corroboration: both runners (Claude + Codex). Framing-similarity: {similarity:.2f}.*"
        block = [f"## {winner['heading']}", "", tag, "", winner["body"]]

        if similarity < 0.4:
            block.extend([
                "",
                f"### Disagreement — {loser_runner} framing",
                "",
                f"*{loser_runner} covered the same story with materially different framing (Jaccard {similarity:.2f}). First 500 words of their version:*",
                "",
                loser["body"][:3000] + ("..." if len(loser["body"]) > 3000 else ""),
                "",
            ])

        section = winner["section"]
        if section in sections_output:
            sections_output[section].append("\n".join(block))

    for sid in claude_only:
        a = claude_articles[sid]
        tag = "*Source: Claude only (Codex did not cover this story).*"
        block = [f"## {a['heading']}", "", tag, "", a["body"]]
        if a["section"] in sections_output:
            sections_output[a["section"]].append("\n".join(block))

    for sid in codex_only:
        a = codex_articles[sid]
        tag = "*Source: Codex only (Claude did not cover this story).*"
        block = [f"## {a['heading']}", "", tag, "", a["body"]]
        if a["section"] in sections_output:
            sections_output[a["section"]].append("\n".join(block))

    # Build the consensus markdown with a header + all sections
    header = render_consensus_header(date, overlap_ids, claude_only, codex_only, claude, codex)
    parts_order = [
        ("part1", "PART I — GENERAL NEWS"),
        ("part2", "PART II — AI NEWS"),
        ("part3", "PART III — NON-AI TECH NEWS"),
        ("part4", "PART IV — JAPAN NEWS"),
        ("humaninterest", "PART V — HUMAN INTEREST"),
        ("updates", "PART VI — UPDATES"),
        ("closing", "CLOSING ANALYSIS"),
    ]
    body_parts = [header]
    for key, label in parts_order:
        if not sections_output[key]:
            continue
        body_parts.append(f"\n\n# {label}\n")
        body_parts.extend(sections_output[key])
    return "\n\n".join(body_parts) + "\n"


def render_consensus_header(
    date: str,
    overlap_ids: list[str],
    claude_only: list[str],
    codex_only: list[str],
    claude_bundle: dict[str, Any],
    codex_bundle: dict[str, Any],
) -> str:
    dt = datetime.strptime(date, "%Y-%m-%d")
    fmt = "%A, %B %-d, %Y" if sys.platform != "win32" else "%A, %B %#d, %Y"
    human_date = dt.strftime(fmt)
    total_articles = len(overlap_ids) + len(claude_only) + len(codex_only)

    lines = [
        f"# Daily Intelligence Briefing — Consensus Edition — {human_date}",
        "",
        "*Merged from Claude (Opus 4.7 [1m]) and Codex (GPT-5.5 xhigh). Both runners independently researched today's top stories; this briefing prefers the higher-scoring version of each corroborated story and flags material framing disagreements.*",
        "",
        "## Editorial Note",
        "",
        f"- **Total articles in this consensus edition**: {total_articles}",
        f"- **Corroborated** (both Claude and Codex covered): {len(overlap_ids)}",
        f"- **Claude only**: {len(claude_only)}",
        f"- **Codex only**: {len(codex_only)}",
        "",
        "Every article carries one of three provenance tags:",
        "",
        "- `*Corroboration: both runners ...*` — story appeared in both runs; the higher-scoring version was selected.",
        "- `*Source: Claude only.*` / `*Source: Codex only.*` — story unique to one runner.",
        "- `### Disagreement — <runner> framing` — both runners covered but with substantially different lead-paragraph framing (word-level Jaccard < 0.4 on first 200 words).",
        "",
        "Underlying manifests:",
        f"- Claude: `{claude_bundle['markdown_path']}`",
        f"- Codex: `{codex_bundle['markdown_path']}`",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True, help="Briefing date in YYYY-MM-DD.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path for the consensus markdown. Default: generated/consensus-briefing-{date}.md",
    )
    args = parser.parse_args()

    output_path = args.output or (CONSENSUS_OUTPUT_DIR / f"consensus-briefing-{args.date}.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = build_consensus(args.date)
    output_path.write_text(text, encoding="utf-8")
    print(f"Consensus briefing written: {output_path} ({output_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
