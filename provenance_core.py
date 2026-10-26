"""Deterministic provenance checks for an article draft.

The writer model is told that every number, quotation and citation must trace to something it
actually opened. This module checks that claim mechanically, with no model involved:

* **Numbers** with units, decimals, percentages or magnitude >= 10 must appear (as a number, with
  comma/scale normalisation) in the *evidence corpus* — the text of every tool result from the
  writing run (the ``WebFetch``/``WebSearch`` outputs the model really saw) plus any dossier text.
* **Quotations** of six or more words must appear verbatim (after whitespace/quote/dash
  normalisation) in the corpus.
* **DOIs** must resolve (Crossref) and the registered title must overlap the citation text.
* **Source URLs** must not be dead (404/410/DNS failure). Bot-blocked (401/403/429) is "unknown".

The check never "fixes" anything. Its output decides whether a targeted repair round is worth
spending tokens on and what the run report tells the reader's editor.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_RES = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\.?(?:\s+\d{{4}})?\b", re.I),
    re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b", re.I),
)
_NUMBER_RE = re.compile(
    r"(?<![\w./:%-])(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d+))?"
    r"(?P<tail>\s*(?:%|percent\b|per cent\b|(?:million|billion|trillion|thousand|bn|m|k)\b)?)",
    re.I,
)
_SCALES = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12}
_CURLY_QUOTE_RE = re.compile(r"“([^”]*)”")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)\]>\"'|,;]+[^\s)\]>\"'|,;.]")
_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_SKIP_SECTIONS = ("primary sources", "watch next", "sources")


@dataclass
class NumberClaim:
    text: str
    forms: tuple[str, ...]
    context: str
    matched: bool = False


@dataclass
class ProvenanceReport:
    numbers_checked: int = 0
    numbers_matched: int = 0
    unmatched_numbers: list[dict] = field(default_factory=list)
    quotes_checked: int = 0
    unmatched_quotes: list[str] = field(default_factory=list)
    translated_quotes: list[str] = field(default_factory=list)   # marked as the writer's translation: not verifiable verbatim
    dois: dict[str, dict] = field(default_factory=dict)
    dead_urls: list[dict] = field(default_factory=list)
    unknown_urls: list[str] = field(default_factory=list)
    corpus_chars: int = 0

    @property
    def unmatched_ratio(self) -> float:
        return (len(self.unmatched_numbers) / self.numbers_checked) if self.numbers_checked else 0.0

    @property
    def bad_dois(self) -> list[str]:
        return [doi for doi, info in self.dois.items() if info.get("status") in {"not_found", "mismatch"}]

    def needs_repair(self, *, max_ratio: float = 0.35, max_unmatched: int = 25) -> bool:
        return bool(
            self.unmatched_quotes
            or self.bad_dois
            or self.dead_urls
            or (self.numbers_checked >= 8 and self.unmatched_ratio > max_ratio)
            or len(self.unmatched_numbers) > max_unmatched
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "numbers_checked": self.numbers_checked, "numbers_matched": self.numbers_matched,
            "unmatched_ratio": round(self.unmatched_ratio, 3), "unmatched_numbers": self.unmatched_numbers[:60],
            "quotes_checked": self.quotes_checked, "unmatched_quotes": self.unmatched_quotes[:20],
            "translated_quotes": len(self.translated_quotes),
            "dois": self.dois, "dead_urls": self.dead_urls, "unknown_urls": self.unknown_urls[:40],
            "corpus_chars": self.corpus_chars,
        }

    def repair_brief(self) -> str:
        """Bullet list handed to the repair prompt: exactly what could not be verified."""
        lines: list[str] = []
        for item in self.unmatched_numbers[:30]:
            lines.append(f'- number "{item["text"]}" in: ...{item["context"]}...')
        for quote in self.unmatched_quotes[:10]:
            lines.append(f'- quotation not found in any opened source: "{quote[:160]}"')
        for doi in self.bad_dois:
            lines.append(f"- DOI {doi}: {self.dois[doi].get('detail', 'did not resolve or does not match the citation')}")
        for dead in self.dead_urls[:10]:
            lines.append(f"- source URL is dead ({dead['status']}): {dead['url']}")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―−]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.lower().strip()


def _strip_non_prose(markdown: str) -> str:
    """Drop blocks whose numbers are not factual claims: image lines, sources, watch list, DOIs, URLs, markers."""
    lines: list[str] = []
    skipping = False
    for line in markdown.splitlines():
        stripped = line.strip().lower().lstrip("#* ").rstrip(":* ")
        if line.lstrip().startswith("#"):
            skipping = stripped.startswith(_SKIP_SECTIONS)
        if skipping or line.lstrip().startswith("![") or line.lstrip().startswith("<!--"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = DOI_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    for pattern in _DATE_RES:
        text = pattern.sub(" ", text)
    return text


def _forms(int_part: str, frac: str | None, tail: str) -> tuple[str, ...]:
    digits = int_part.replace(",", "")
    base = digits + (f".{frac}" if frac else "")
    forms = {base}
    word = tail.strip().lower().rstrip(".")
    if frac and set(frac) == {"0"}:
        forms.add(digits)
    if word in _SCALES:
        try:
            expanded = float(base) * _SCALES[word]
            forms.add(str(int(round(expanded))))
        except ValueError:
            pass
    return tuple(sorted(forms))


def _is_checkable(int_part: str, frac: str | None, tail: str, context_before: str) -> bool:
    digits = int_part.replace(",", "")
    has_unit = bool(tail.strip())
    if not frac and not has_unit and len(digits) == 4 and digits[:2] in {"18", "19", "20"}:
        return False  # a year
    if not frac and not has_unit and len(digits) < 2:
        return False
    if not frac and not has_unit and int(digits) < 10:
        return False
    if re.search(r"(?:appendix|figure|fig\.|table|section|page|p\.|n°|no\.|#)\s*$", context_before, re.I):
        return False
    return True


def extract_numeric_claims(markdown: str) -> list[NumberClaim]:
    text = _strip_non_prose(markdown)
    claims: list[NumberClaim] = []
    seen: set[tuple[str, ...]] = set()
    for match in _NUMBER_RE.finditer(text):
        before = text[max(0, match.start() - 14): match.start()]
        if not _is_checkable(match.group("int"), match.group("frac"), match.group("tail") or "", before):
            continue
        forms = _forms(match.group("int"), match.group("frac"), match.group("tail") or "")
        shown = match.group(0).strip()
        if forms in seen:
            continue
        seen.add(forms)
        context = re.sub(r"\s+", " ", text[max(0, match.start() - 60): match.end() + 40]).strip()
        claims.append(NumberClaim(text=shown, forms=forms, context=context))
    return claims


_EU_THOUSANDS_RE = re.compile(r"(?<![\w.,])\d{1,3}(?:[. \u00a0\u202f]\d{3})+(?:,(?P<frac>\d+))?(?![\w])")
_EU_DECIMAL_RE = re.compile(r"(?<![\w.,])(?P<int>\d{1,3}),(?P<frac>\d{1,2})(?![\d])")


def _european_numbers(corpus_text: str) -> set[str]:
    """Sources in Dutch, German, French, Spanish... write 200.000 and 3,4 where English writes 200,000 and 3.4. Both readings
    go into the evidence set (it only ever lets a correct figure match; the article's own figures are still read English-style)."""
    numbers: set[str] = set()
    for match in _EU_THOUSANDS_RE.finditer(corpus_text):
        digits = re.sub(r"[. \u00a0\u202f]", "", match.group(0).split(",")[0])
        numbers.add(digits)
        if match.group("frac"):
            numbers.add(f"{digits}.{match.group('frac')}")
    for match in _EU_DECIMAL_RE.finditer(corpus_text):
        numbers.add(f"{match.group('int')}.{match.group('frac')}")
    return numbers


def corpus_number_set(corpus_text: str) -> set[str]:
    numbers: set[str] = _european_numbers(corpus_text)
    for match in _NUMBER_RE.finditer(corpus_text):
        numbers.update(_forms(match.group("int"), match.group("frac"), match.group("tail") or ""))
        digits = match.group("int").replace(",", "")
        numbers.add(digits)
        if match.group("frac"):
            numbers.add(f"{digits}.{match.group('frac')}")
    return numbers


def _strip_blocks(markdown: str) -> str:
    """Prose only (no images, markers, Primary Sources, Watch Next) but otherwise untouched: dates, numbers and URLs stay.

    Quotations are checked on this text. The number extractor's date/URL blanking would turn a verbatim quote such as
    "closes on 12 March 2026" into "closes on   " and report an invented quotation.
    """
    lines: list[str] = []
    skipping = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("#"):
            skipping = line.lstrip("# ").strip().lower().rstrip(":* ").startswith(_SKIP_SECTIONS)
        if skipping or line.lstrip().startswith(("![", "<!--")):
            continue
        lines.append(line)
    return "\n".join(lines)


_TRANSLATION_CUE_RE = re.compile(r"(?i)\btranslat\w*")


def _quote_spans(paragraph: str) -> list[tuple[int, int]]:
    """(start, end) of the text inside each quotation in one paragraph.

    Curly quotes pair themselves. Straight quotes pair in order (1st with 2nd, 3rd with 4th), which matters: a short quoted
    phrase ("65 million years") must not hand its closing quote to the *next* quotation and turn the prose between them into a
    phantom quotation. An odd straight quote at the end (5" mesh) is left unpaired."""
    spans = [(m.start(1), m.end(1)) for m in _CURLY_QUOTE_RE.finditer(paragraph)]
    inside = [(m.start(), m.end()) for m in _CURLY_QUOTE_RE.finditer(paragraph)]
    straight = [i for i, ch in enumerate(paragraph) if ch == '"' and not any(a <= i < b for a, b in inside)]
    for k in range(0, len(straight) - 1, 2):
        spans.append((straight[k] + 1, straight[k + 1]))
    return sorted(spans)


def classify_quotes(markdown: str) -> tuple[list[str], list[str]]:
    """Split quotations into (verbatim, translated). A quotation that the writer marks as a translation (the spec asks for
    native-language sources to be translated and marked) cannot match the fetched original verbatim, so it is exempt from
    the verbatim check and reported separately; its figures are still number-checked like any other prose."""
    verbatim: list[str] = []
    translated: list[str] = []
    for paragraph in re.split(r"\n\s*\n", _strip_blocks(markdown)):
        paragraph = re.sub(r"\s*\n\s*", " ", paragraph)  # a quotation may wrap across source lines
        for start, end in _quote_spans(paragraph):
            quote = re.sub(r"\s+", " ", paragraph[start:end]).strip()
            if len(paragraph[start:end]) < 20 or len(quote.split()) < 6:
                continue
            around = paragraph[max(0, start - 61):start - 1] + " " + paragraph[end + 1:end + 91]
            (translated if _TRANSLATION_CUE_RE.search(around) else verbatim).append(quote)
    return verbatim, translated


def extract_quotes(markdown: str) -> list[str]:
    verbatim, translated = classify_quotes(markdown)
    return verbatim + translated


def quote_in_corpus(quote: str, normalized_corpus: str) -> bool:
    pieces = [piece for piece in re.split(r"\s*(?:\.\.\.|…|\[…\]|\[\.\.\.\])\s*", normalize_text(quote)) if len(piece) >= 12]
    if not pieces:
        pieces = [normalize_text(quote)]
    return all(piece.strip(" .,;:") in normalized_corpus for piece in pieces)


# --------------------------------------------------------------------------------------------
# Network checks (injectable)
# --------------------------------------------------------------------------------------------
class JsonFetcher(Protocol):
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any: ...


def _title_tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z]{4,}", normalize_text(text))}


def check_doi(doi: str, citation_text: str, fetcher: JsonFetcher) -> dict[str, Any]:
    try:
        payload = fetcher.get_json(f"https://api.crossref.org/works/{doi}")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "404" in message:
            return {"status": "not_found", "detail": "Crossref has no record of this DOI"}
        return {"status": "unchecked", "detail": f"Crossref lookup failed: {message[:120]}"}
    message = (payload or {}).get("message") or {}
    titles = message.get("title") or []
    registered = titles[0] if titles else ""
    if not registered:
        return {"status": "ok", "detail": "registered; no title to compare"}
    overlap = len(_title_tokens(registered) & _title_tokens(citation_text))
    needed = max(2, min(4, len(_title_tokens(registered)) // 3))
    if citation_text and overlap < needed:
        return {"status": "mismatch", "registered_title": registered,
                "detail": f"registered title is {registered!r}; the citation text shares only {overlap} distinctive words with it"}
    return {"status": "ok", "registered_title": registered}


class UrlChecker(Protocol):
    """Return an HTTP status; 0 for a name that does not resolve; None when unreachable for other reasons."""

    def __call__(self, url: str) -> int | None: ...


def default_url_checker(timeout: float = 20.0) -> UrlChecker:  # pragma: no cover - network
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; BriefingCitationCheck/1.0)"

    def check(url: str) -> int | None:
        try:
            response = session.head(url, timeout=timeout, allow_redirects=True)
            if response.status_code in (400, 403, 405, 429, 501):
                response = session.get(url, timeout=timeout, stream=True)
            return response.status_code
        except requests.RequestException as exc:
            text = repr(exc)
            # A name that does not resolve is how fabricated citations look; resets/timeouts are just flaky networks.
            return 0 if ("NameResolutionError" in text or "getaddrinfo" in text or "Name or service not known" in text) else None

    return check


def _primary_source_entries(markdown: str) -> list[str]:
    entries: list[str] = []
    in_block = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("#"):
            in_block = line.lstrip("# ").strip().lower().startswith(("primary sources", "sources"))
            continue
        if in_block and line.strip().startswith("- "):
            entries.append(line.strip()[2:])
    return entries


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------
def check_article(
    markdown: str,
    corpus: list[str],
    *,
    json_fetcher: JsonFetcher | None = None,
    url_checker: UrlChecker | None = None,
    check_network: bool = True,
) -> ProvenanceReport:
    report = ProvenanceReport()
    corpus_text = "\n".join(corpus)
    report.corpus_chars = len(corpus_text)
    normalized_corpus = normalize_text(corpus_text)
    known_numbers = corpus_number_set(corpus_text)

    for claim in extract_numeric_claims(markdown):
        report.numbers_checked += 1
        if any(form in known_numbers for form in claim.forms):
            report.numbers_matched += 1
        else:
            report.unmatched_numbers.append({"text": claim.text, "context": claim.context})

    verbatim, report.translated_quotes = classify_quotes(markdown)
    for quote in verbatim:
        report.quotes_checked += 1
        if not quote_in_corpus(quote, normalized_corpus):
            report.unmatched_quotes.append(quote)

    entries = _primary_source_entries(markdown)
    for entry in entries:
        for doi in DOI_RE.findall(entry):
            doi = doi.rstrip(".,;")
            if doi in report.dois:
                continue
            if doi.lower() in normalized_corpus:
                report.dois[doi] = {"status": "ok", "detail": "appeared in an opened source"}
            elif check_network and json_fetcher is not None:
                report.dois[doi] = check_doi(doi, entry, json_fetcher)
            else:
                report.dois[doi] = {"status": "unchecked", "detail": "network checks disabled"}

    if check_network and url_checker is not None:
        seen: set[str] = set()
        for entry in entries:
            for url in _URL_RE.findall(entry):
                url = url.rstrip(".,;")
                if url in seen or "doi.org/" in url:
                    continue
                seen.add(url)
                status = url_checker(url)
                if status in (0, 404, 410):
                    report.dead_urls.append({"url": url, "status": "dns failure" if status == 0 else status})
                elif status is None or status in (401, 403, 429, 451, 999) or status >= 500:
                    report.unknown_urls.append(url)
    return report


__all__ = [
    "NumberClaim", "ProvenanceReport", "check_article", "check_doi", "default_url_checker", "extract_numeric_claims",
    "extract_quotes", "normalize_text", "quote_in_corpus",
]
