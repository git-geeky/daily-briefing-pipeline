"""Deterministic article plumbing around the model calls (no model, no network).

* ``normalize_article`` repairs *form* only (heading levels, canonical block headings, story
  marker, body links, stray fences/chatter, image lines the writer should not have authored).
  It never invents or rewrites content, and it reports every fix it makes.
* ``deterministic_header`` builds an honest, validator-clean header from the articles' own bottom
  lines when the header model call fails or is skipped (the "last mile" fallback).
* ``repair_cross_references`` snaps a header cross-reference that almost matches a real title back
  to that title and drops ones that match nothing.
* ``compose_email_bodies`` writes the covering text/HTML for the delivery email.
"""
from __future__ import annotations

import difflib
import html
import re
from collections import Counter
from typing import Any

_FENCE_RE = re.compile(r"(?m)^\s*```[a-z]*\s*$")
_IMAGE_LINE_RE = re.compile(r"(?m)^[ \t]*!\[[^\]]*\]\([^)]*\)[ \t]*\n?")
_MARKER_RE = re.compile(r"(?m)^[ \t]*<!--\s*story:[^>]*-->[ \t]*\n?")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_DOI_URL_RE = re.compile(r"https?://(?:dx\.)?doi\.org/(10\.[^\s)\]>]+)", re.I)
_BARE_URL_RE = re.compile(r"\s*<?https?://[^\s)>\]]+>?")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_CANONICAL_HEADINGS = (
    (re.compile(r"^counterpoint\b", re.I), "Counterpoint Matrix"),
    (re.compile(r"^watch\s+next\b", re.I), None),  # keep the writer's suffix ("Watch Next (30 days)")
    (re.compile(r"^(?:primary\s+sources?|sources?|references?|bibliography|source\s+list)\b", re.I), "Primary Sources"),
)
_BOTTOM_RE = re.compile(r"(?im)^[ \t>]*\*{0,2}\s*bottom line\s*:?\s*\*{0,2}\s*:?\s*")
_CONF_RE = re.compile(r"(?im)^[ \t>]*\*{0,2}\s*confidence\s*:?\s*\*{0,2}\s*:?\s*")
_SOURCE_BLOCK_HEADINGS = ("primary sources", "watch next")


def normalize_article(raw: str, story: dict[str, Any]) -> tuple[str, list[str]]:
    fixes: list[str] = []
    text = _FENCE_RE.sub("", raw or "").replace("\r\n", "\n")
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if re.match(r"^#{1,2}\s+\S", line)), None)
    if start is None:
        raise ValueError("article has no title heading")
    if start:
        fixes.append(f"removed {start} line(s) of preamble before the title")
    lines = lines[start:]

    out: list[str] = []
    title_seen = False
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            level, label = len(heading.group(1)), heading.group(2).strip()
            if not title_seen:
                title_seen = True
                title = re.sub(r"^\*+|\*+$", "", label).strip() or story["title"]
                out.append(f"## {title}")
                continue
            label = re.sub(r"^\*+|\*+$", "", label).strip().rstrip(":")
            if level <= 2:
                fixes.append(f"demoted heading {label!r} to level 3")
            for pattern, canonical in _CANONICAL_HEADINGS:
                if pattern.match(label):
                    if canonical and label != canonical:
                        fixes.append(f"canonicalised heading {label!r} -> {canonical!r}")
                        label = canonical
                    break
            out.append(f"### {label}")
            continue
        out.append(line)

    text = "\n".join(out)
    if _IMAGE_LINE_RE.search(text):
        text = _IMAGE_LINE_RE.sub("", text)
        fixes.append("removed writer-authored image lines (photos are inserted by the photo stage)")
    text = _MARKER_RE.sub("", text)
    marker = f"<!-- story: {story['story_id']}" + (f"; prior: {story['prior_story_id']}" if story.get("prior_story_id") else "") + " -->"
    first, _, rest = text.partition("\n")
    text = f"{first}\n{marker}\n{rest.lstrip(chr(10))}"
    if "<!-- story:" not in raw:
        fixes.append("added story marker")

    text = _BOTTOM_RE.sub("**Bottom line:** ", text)
    text = _CONF_RE.sub("**Confidence:** ", text)
    text, link_fixes = _strip_body_links(text)
    if link_fixes:
        fixes.append(f"removed {link_fixes} link(s) from the body (URLs belong in Primary Sources / Watch Next)")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text, fixes


def _strip_body_links(text: str) -> tuple[str, int]:
    """Remove URLs outside the Primary Sources and Watch Next blocks (body links are a blocking validator rule)."""
    removed = 0
    result: list[str] = []
    protected = False
    for line in text.split("\n"):
        heading = _HEADING_RE.match(line)
        if heading:
            protected = heading.group(2).strip().lower().startswith(_SOURCE_BLOCK_HEADINGS)
            result.append(line)
            continue
        if protected or not re.search(r"https?://", line) or line.lstrip().startswith("![") or "<!--" in line:
            result.append(line)
            continue
        original = line
        line = _DOI_URL_RE.sub(lambda m: f"doi:{m.group(1)}", line)
        line = _MD_LINK_RE.sub(lambda m: m.group(1), line)
        line = _BARE_URL_RE.sub("", line)
        line = re.sub(r"\(\s*\)", "", line)
        line = re.sub(r"\s{2,}", " ", line).rstrip()
        removed += len(re.findall(r"https?://", original)) - len(re.findall(r"https?://", line))
        result.append(line)
    return "\n".join(result), removed


# --------------------------------------------------------------------------------------------
# Summaries, header, cross-references
# --------------------------------------------------------------------------------------------
def article_summary(markdown: str, story: dict[str, Any] | None = None) -> dict[str, Any]:
    title = re.search(r"(?m)^##\s+(.+)$", markdown)
    bottom = re.search(r"(?im)^[ \t>]*\*\*bottom line:?\*\*:?[ \t]*(.+)$", markdown)
    conf = re.search(r"(?im)^[ \t>]*\*\*confidence:?\*\*:?[ \t]*((?:high|medium|low)\b.*)$", markdown)
    bottom_text = bottom.group(1).strip() if bottom else ""
    if not bottom_text:  # editions without a Bottom line block (daily): first prose paragraph, trimmed
        for block in re.split(r"\n\s*\n", markdown):
            text = block.strip()
            if text and not text.startswith(("#", "|", "![", ">", "<!--", "- ", "*", "**")):
                bottom_text = _trim_words(re.sub(r"\s+", " ", text), 55)
                break
    return {
        "title": title.group(1).strip() if title else (story or {}).get("title", ""),
        "bottom_line": bottom_text,
        "confidence": conf.group(1).strip() if conf else "",
        "group": (story or {}).get("group", "other"),
        "story_id": (story or {}).get("story_id", ""),
        "region": (story or {}).get("region", ""),
        "taxa": "; ".join(
            f"*{t['scientific']}* ({t['common']})" if t.get("scientific") and t.get("common") else (t.get("scientific") and f"*{t['scientific']}*") or t.get("common", "")
            for t in (story or {}).get("taxa", []) if t.get("scientific") or t.get("common")
        ),
    }


_GROUP_LABEL = {"mammal": "mammals", "bird": "birds", "fish_marine": "fish and marine fauna", "herp": "reptiles and amphibians",
                "invertebrate": "invertebrates", "other": "other topics"}


def _trim_words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]).rstrip(",;:") + "..."


def deterministic_header(*, label: str, date_human: str, prepared_by: str, summaries: list[dict[str, Any]],
                         summary_words: tuple[int, int] = (250, 750), cross_label: str = "Cross-currents",
                         min_cross: int = 2) -> str:
    """Executive summary assembled only from the articles' own titles and bottom lines."""
    low, high = summary_words
    paragraphs = [f"Today's briefing carries {len(summaries)} stories in depth. The bottom lines, in order:"]
    budget = high - 20
    per_item = max(30, budget // max(len(summaries), 1) - 8)
    for index, item in enumerate(summaries, start=1):
        bottom = _trim_words(item["bottom_line"] or "See the article for the evidence and its limits.", per_item)
        confidence = f" Confidence: {item['confidence'].split('—')[0].split('-')[0].strip()}." if item["confidence"] else ""
        context = " ".join(part for part in (
            f"Taxa: {item['taxa']}." if item.get("taxa") else "",
            f"Setting: {item['region']}." if item.get("region") else "",
        ) if part)
        paragraphs.append(f"{index}. {item['title']}. {bottom}{confidence} {context}".rstrip())
    body = "\n\n".join(paragraphs)
    count = len(body.split())
    if count < low:  # top up only with true, structural statements derived from the articles themselves
        groups = ", ".join(f"{n} {_GROUP_LABEL.get(g, g)}" for g, n in sorted(group_counts(summaries).items(), key=lambda kv: -kv[1]))
        levels = Counter((s["confidence"].split()[0].lower().strip("—-:,") if s["confidence"] else "unstated") for s in summaries)
        confidence_text = ", ".join(f"{n} {level}" for level, n in levels.most_common())
        regions = sorted({s["region"] for s in summaries if s["region"]})
        body += (f"\n\nBy taxonomic group the stories divide into {groups}. Stated confidence across the briefing: {confidence_text}."
                 + (f" Settings covered: {', '.join(regions)}." if regions else ""))
    refs = _cross_current_lines(summaries, minimum=min_cross)
    return (
        f"# {label}\n**{date_human}** | {prepared_by}\n\n## Executive Summary\n\n{body}\n\n"
        f"### {cross_label}\n\n{refs}\n\n### Table of Contents\n\n"
        + "\n".join(f"{i}. {s['title']}" for i, s in enumerate(summaries, start=1))
        + "\n"
    )


def _ref(item: dict[str, Any]) -> str:
    prefix = f"{item['part']} - " if item.get("part") else ""
    return f'Cross-reference: {prefix}"{item["title"]}"'


def _cross_current_lines(summaries: list[dict[str, Any]], *, minimum: int = 2) -> str:
    """Structural, true-by-construction connections: stories that share a taxonomic group or a section."""
    lines: list[str] = []
    by_group: dict[str, list[dict[str, Any]]] = {}
    for item in summaries:
        by_group.setdefault(item["group"], []).append(item)
    for group, members in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
        if len(members) >= 2 and group != "other" and len(lines) < 4:
            for member in members[:2]:
                lines.append(f"- {len(members)} of today's stories concern {_GROUP_LABEL.get(group, group)}. {_ref(member)}")
    by_part: dict[str, list[dict[str, Any]]] = {}
    for item in summaries:
        by_part.setdefault(item.get("part") or "", []).append(item)
    for part, members in sorted(by_part.items(), key=lambda kv: -len(kv[1])):
        if part and len(members) >= 2 and len(lines) < max(minimum, 4):
            lines.append(f"- {len(members)} stories sit in {part} today. {_ref(members[0])}")
    for item in summaries:
        if len(lines) >= minimum:
            break
        lines.append(f"- Lead story. {_ref(item)}")
    return "\n".join(lines)


def repair_cross_references(header: str, titles: list[str], *, cutoff: float = 0.82) -> tuple[str, list[str]]:
    """Snap near-miss cross-reference titles to real ones; drop references that match nothing."""
    fixes: list[str] = []
    out: list[str] = []
    for line in header.split("\n"):
        match = re.search(r'(?i)(cross[-\s]reference:\s*[^"“]*)["“]([^"”]+)["”]', line)
        if not match:
            out.append(line)
            continue
        quoted = match.group(2).strip()
        if quoted in titles:
            out.append(line)
            continue
        close = difflib.get_close_matches(quoted, titles, n=1, cutoff=cutoff)
        if close:
            out.append(line.replace(quoted, close[0]))
            fixes.append(f"cross-reference {quoted!r} -> {close[0]!r}")
        else:
            fixes.append(f"dropped cross-reference to unknown title {quoted!r}")
    return "\n".join(out), fixes


def extract_header_titles(header: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r'(?i)cross[-\s]reference:\s*[^"“]*["“]([^"”]+)["”]', header)]


def compose_section(section_title: str, articles: list[str]) -> str:
    return f"# {section_title}\n\n" + "\n\n".join(a.strip() for a in articles).strip() + "\n"


# --------------------------------------------------------------------------------------------
# Email body
# --------------------------------------------------------------------------------------------
def compose_email_bodies(*, label: str, date_human: str, summaries: list[dict[str, Any]], notes: list[str] | None = None,
                         digest_titles: list[str] | None = None) -> tuple[str, str]:
    notes = notes or []
    text = [f"{label} — {date_human}", "", f"{len(summaries)} stories in depth (full text and photographs in the attached PDF):", ""]
    for i, s in enumerate(summaries, start=1):
        text.append(f"{i}. {s['title']}")
        if s["bottom_line"]:
            text.append(f"   {s['bottom_line']}")
        if s["confidence"]:
            text.append(f"   Confidence: {s['confidence']}")
        text.append("")
    if digest_titles:
        text += ["Also noted: " + "; ".join(digest_titles), ""]
    if notes:
        text += ["Editor's notes on this run:", *[f"- {n}" for n in notes], ""]
    body_html = [f"<h2>{html.escape(label)} &mdash; {html.escape(date_human)}</h2>",
                 f"<p>{len(summaries)} stories in depth (full text and photographs in the attached PDF).</p><ol>"]
    for s in summaries:
        body_html.append(f"<li><strong>{html.escape(s['title'])}</strong>"
                         + (f"<br>{html.escape(s['bottom_line'])}" if s["bottom_line"] else "")
                         + (f"<br><em>Confidence: {html.escape(s['confidence'])}</em>" if s["confidence"] else "") + "</li>")
    body_html.append("</ol>")
    if digest_titles:
        body_html.append(f"<p><strong>Also noted:</strong> {html.escape('; '.join(digest_titles))}</p>")
    if notes:
        body_html.append("<p><strong>Editor's notes on this run:</strong></p><ul>" + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul>")
    return "\n".join(text).rstrip() + "\n", "\n".join(body_html)


def group_counts(summaries: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(s["group"] for s in summaries))


__all__ = [
    "article_summary", "compose_email_bodies", "compose_section", "deterministic_header", "extract_header_titles",
    "group_counts", "normalize_article", "repair_cross_references",
]
