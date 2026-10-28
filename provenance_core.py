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
# Scale words in English and the main languages of the catalog (a Dutch "2,5 miljoen" must support an English "2.5 million").
_SCALE_CANON = {
    "thousand": ("thousand", 1e3), "k": ("thousand", 1e3), "duizend": ("thousand", 1e3), "tausend": ("thousand", 1e3),
    "million": ("million", 1e6), "millions": ("million", 1e6), "m": ("million", 1e6), "miljoen": ("million", 1e6), "miljoenen": ("million", 1e6),
    "millionen": ("million", 1e6), "millón": ("million", 1e6), "millones": ("million", 1e6), "milhão": ("million", 1e6), "milhões": ("million", 1e6),
    "milione": ("million", 1e6), "milioni": ("million", 1e6),
    "billion": ("billion", 1e9), "bn": ("billion", 1e9), "miljard": ("billion", 1e9), "miljarden": ("billion", 1e9), "milliarde": ("billion", 1e9),
    "milliarden": ("billion", 1e9), "milliard": ("billion", 1e9), "milliards": ("billion", 1e9),
    "trillion": ("trillion", 1e12),
}
_SCALES = {word: value for word, (_canon, value) in _SCALE_CANON.items()}
_ABBREVIATED_SCALES = {"k", "m", "bn"}  # "2.5 m" is as likely metres as millions: the bare number stays acceptable
_PERCENT_WORDS = ("percent", "per cent", "procent", "prozent", "pour cent", "por ciento", "por cento", "per cento")
_SCALE_ALT = "|".join(sorted(map(re.escape, _SCALE_CANON), key=len, reverse=True))
_PERCENT_ALT = "|".join(sorted((re.escape(w).replace("\\ ", r"\s+") for w in _PERCENT_WORDS), key=len, reverse=True))
_NUMBER_RE = re.compile(
    rf"(?<![\w./:%-])(?P<int>\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.(?P<frac>\d+))?"
    rf"(?P<tail>\s*(?:%|\uff05|(?:{_PERCENT_ALT})\b|(?:{_SCALE_ALT})\b)?)",
    re.I,
)
_CURLY_QUOTE_RE = re.compile(r"“([^”]*)”")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s)\]>\"'|,;]+[^\s)\]>\"'|,;.]")
_URL_RE = re.compile(r"https?://[^\s)>\]]+")


def clean_doi(doi: str) -> str:
    """A DOI as written in prose is often wrapped in markdown ("**doi:10.1038/x.**") or followed by punctuation; neither is part of it."""
    return re.sub(r"[*_`.,;:)\]\u2019\u201d\"']+$", "", doi.strip())
_SKIP_SECTIONS = ("primary sources", "watch next", "sources")


@dataclass
class NumberClaim:
    text: str
    forms: tuple[str, ...]
    context: str
    matched: bool = False
    value: float | None = None       # the figure as a number (a percentage stays as written: 9.6 for "9.6%")
    window: str = ""                 # the surrounding prose, where the operands of a stated calculation appear
    sentence: str = ""               # the claim's own sentence: the calculation wording must be here, not merely nearby
    local: str = ""                  # that sentence and the one before it: the only place a figure without explicit calculation wording may look
    decimals: int = 0


@dataclass
class ProvenanceReport:
    numbers_checked: int = 0
    numbers_matched: int = 0
    unmatched_numbers: list[dict] = field(default_factory=list)
    quotes_checked: int = 0
    unmatched_quotes: list[str] = field(default_factory=list)
    derived_numbers: list[str] = field(default_factory=list)     # the writer's own arithmetic, reproduced from figures that are in the sources
    summary_only_limit: int = 3                                  # how many summary-only quotations are tolerated before a repair
    translated_quotes: list[str] = field(default_factory=list)   # marked as the writer's translation, with a verified original beside it
    summary_only_quotes: list[str] = field(default_factory=list)  # found only in a fetch *answer* (a small model's words), not in page text
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
        unmatched = len(self.unmatched_numbers)
        return bool(
            self.unmatched_quotes
            or self.bad_dois
            or self.dead_urls
            or len(self.summary_only_quotes) >= max(1, self.summary_only_limit)
            or (self.numbers_checked >= 8 and self.unmatched_ratio > max_ratio)
            or (self.numbers_checked >= 3 and unmatched >= 3 and self.unmatched_ratio > 0.5)  # a short article of invented figures
            or (unmatched >= 8 and self.unmatched_ratio > 0.12)  # a table's worth of figures the writer could not show it read (live run: 17 of 94)
            or unmatched > max_unmatched
        )

    def reader_should_be_told(self) -> bool:
        """Anything the gate tolerated but a reader of figures would want to know: three or more figures, or a fifth of them, or
        quotations resting on a fetch summary alone, are not backed by a page the writer was given."""
        return bool(self.summary_only_quotes or len(self.unmatched_numbers) >= 3
                    or (self.numbers_checked >= 5 and self.unmatched_ratio > 0.2))

    def to_dict(self) -> dict[str, Any]:
        return {
            "numbers_checked": self.numbers_checked, "numbers_matched": self.numbers_matched,
            "unmatched_ratio": round(self.unmatched_ratio, 3), "unmatched_numbers": self.unmatched_numbers[:60],
            "quotes_checked": self.quotes_checked, "unmatched_quotes": self.unmatched_quotes[:20],
            "translated_quotes": len(self.translated_quotes), "summary_only_quotes": len(self.summary_only_quotes),
            "derived_numbers": self.derived_numbers[:30],
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
        if len(self.summary_only_quotes) >= max(1, self.summary_only_limit):
            for quote in self.summary_only_quotes[:6]:
                lines.append(f'- quotation found only in a fetch summary, not in page text (paraphrase it or quote what the page itself says): "{quote[:140]}"')
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


def _tail_kind(tail: str) -> tuple[str | None, str | None]:
    """("percent", None) | ("scale", canonical word) | (None, None)."""
    word = re.sub(r"\s+", " ", (tail or "").strip().lower().rstrip("."))
    if word in {"%", "\uff05"} or word in _PERCENT_WORDS:
        return "percent", None
    if word in _SCALE_CANON:
        return "scale", word
    return None, None


def _forms(int_part: str, frac: str | None, tail: str) -> tuple[str, ...]:
    """Forms an *article's* figure must find in the evidence. A scaled or percentage figure must match as such: "2.5 billion" is
    not supported by "2.5 million euros", and "37%" is not supported by "page 37"."""
    digits = int_part.replace(",", "")
    base = digits + (f".{frac}" if frac else "")
    kind, word = _tail_kind(tail)
    zeros = bool(frac) and set(frac) == {"0"}
    if kind == "scale":
        canon, value = _SCALE_CANON[word]
        forms = {f"{base} {canon}"}
        try:
            forms.add(str(int(round(float(base) * value))))
        except ValueError:
            pass
        if word in _ABBREVIATED_SCALES:
            forms.add(base)
        return tuple(sorted(forms))
    if kind == "percent":
        forms = {f"{base}%"}
        if zeros:
            forms.add(f"{digits}%")
        return tuple(sorted(forms))
    forms = {base}
    if zeros:
        forms.add(digits)
    return tuple(sorted(forms))


def _corpus_forms(int_part: str, frac: str | None, tail: str) -> set[str]:
    """Everything a figure in the evidence can support: the bare number, its scaled/expanded reading, its percentage reading."""
    digits = int_part.replace(",", "")
    base = digits + (f".{frac}" if frac else "")
    forms = {base, digits}
    kind, word = _tail_kind(tail)
    if kind == "scale":
        canon, value = _SCALE_CANON[word]
        forms.add(f"{base} {canon}")
        forms.add(f"{digits} {canon}")
        try:
            forms.add(str(int(round(float(base) * value))))
        except ValueError:
            pass
    elif kind == "percent":
        forms.add(f"{base}%")
        forms.add(f"{digits}%")
    return forms


def _is_checkable(int_part: str, frac: str | None, tail: str, context_before: str) -> bool:
    digits = int_part.replace(",", "")
    has_unit = bool(tail.strip())
    if not frac and not has_unit and len(digits) == 4 and "," not in int_part and digits[:2] in {"15", "16", "17", "18", "19", "20"}:
        return False  # a year, including a taxon authority's ("Linnaeus, 1758"); "1,950 animals" is a count and is checked
    if not frac and not has_unit and len(digits) < 2:
        return False
    if not frac and not has_unit and int(digits) < 10:
        return False
    if re.search(r"(?:appendix|figure|fig\.|table|section|page|p\.|n°|no\.|#)\s*$", context_before, re.I):
        return False
    return True


# A specimen or accession identifier ("CD13-PAL.2018.3.1.1-3, 5-15 and 18"): the bare integers listed beside it are parts of an ID, not claims.
_IDENTIFIER_RE = re.compile(r"(?<![\d.])[A-Za-z][\w-]*\.\d{2,4}(?:\.\d+){2,}")


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(". ", 0, start), text.rfind("\n", 0, start))
    right_candidates = [i for i in (text.find(". ", end), text.find("\n", end)) if i != -1]
    return text[left + 1: min(right_candidates) if right_candidates else len(text)]


def _sentence_with_previous(text: str, start: int, end: int) -> str:
    sentence = _sentence_around(text, start, end)
    begin = text.find(sentence, max(0, start - len(sentence)))
    if begin <= 0:
        return sentence
    left = max(text.rfind(". ", 0, begin - 1), text.rfind("\n", 0, begin - 1))
    return text[left + 1: begin] + sentence


def _claim_value(int_part: str, frac: str | None, tail: str) -> float | None:
    try:
        value = float(int_part.replace(",", "") + (f".{frac}" if frac else ""))
    except ValueError:
        return None
    kind, word = _tail_kind(tail)
    return value * _SCALE_CANON[word][1] if kind == "scale" else value


def extract_numeric_claims(markdown: str) -> list[NumberClaim]:
    text = _strip_non_prose(markdown)
    claims: list[NumberClaim] = []
    seen: set[tuple[str, ...]] = set()
    for match in _NUMBER_RE.finditer(text):
        before = text[max(0, match.start() - 14): match.start()]
        if not _is_checkable(match.group("int"), match.group("frac"), match.group("tail") or "", before):
            continue
        tail = match.group("tail") or ""
        if not match.group("frac") and not tail.strip() and len(match.group("int").replace(",", "")) <= 3 \
                and _IDENTIFIER_RE.search(_sentence_around(text, match.start(), match.end())):
            continue
        forms = _forms(match.group("int"), match.group("frac"), tail)
        shown = match.group(0).strip()
        if forms in seen:
            continue
        seen.add(forms)
        context = re.sub(r"\s+", " ", text[max(0, match.start() - 60): match.end() + 40]).strip()
        window = re.sub(r"\s+", " ", text[max(0, match.start() - 300): match.end() + 300])
        claims.append(NumberClaim(text=shown, forms=forms, context=context, value=_claim_value(match.group("int"), match.group("frac"), tail),
                                  window=window, sentence=_sentence_around(text, match.start(), match.end()),
                                  local=_sentence_with_previous(text, match.start(), match.end()), decimals=len(match.group("frac") or "")))
    return claims


# --------------------------------------------------------------------------------------------
# The writer's own arithmetic
# --------------------------------------------------------------------------------------------
_DERIVATION_CUE_RE = re.compile(r"(?i)\b(?:comput|calculat|deriv|subtract|difference|ratio|sum of|total of|divid|multipl|percentage points?|out of)\w*|[=\u00f7\u00d7]")


# Comparisons that are plainly arithmetic ("a margin of 4.43 million votes", "0.7 points lower than", "2.5 hours elapsed"): too common in
# ordinary prose to accept anywhere, so they count only when both operands are in the same or the preceding sentence as well as in the sources.
_WEAK_DERIVATION_CUE_RE = re.compile(
    r"(?i)\b(?:margin|gap|elapsed|combined|altogether|in total|(?:points?|pp)\s+(?:above|below|higher|lower|ahead|behind)|"
    r"(?:higher|lower|larger|smaller|greater|fewer|less|more|longer|shorter|faster|slower|older|younger)\s+than|times\s+(?:as\s+\w+|the|higher|more|larger))\b")


def _numeric_values(known: set[str]) -> list[float]:
    values: set[float] = set()
    for form in known:
        if re.fullmatch(r"\d+(?:\.\d+)?", form):
            values.add(float(form))
    return sorted(values)


def _near(values: list[float], target: float, *, relative: float = 0.002, absolute: float = 1e-9) -> bool:
    import bisect

    tolerance = max(abs(target) * relative, absolute)
    index = bisect.bisect_left(values, target - tolerance)
    return index < len(values) and values[index] <= target + tolerance


def derivable(claim: NumberClaim, known_values: list[float]) -> bool:
    """True when ``claim`` is arithmetic the writer says it did ("computed here", "by subtraction", "18/434") and the result can be
    reproduced from two figures that appear both in the sources and in the article's own text beside the claim. High precision on
    purpose: without the cue wording, or without both operands in the sources, a figure stays unverified."""
    cue_text = claim.window if "|" in claim.sentence else claim.sentence  # a table row has no sentence: its note sits above or beside it
    if claim.value is None:
        return False
    strong = bool(_DERIVATION_CUE_RE.search(cue_text))
    if not strong and not (claim.local and _WEAK_DERIVATION_CUE_RE.search(claim.sentence)):
        return False
    operands: set[float] = set()
    for match in _NUMBER_RE.finditer(claim.window if strong else claim.local):
        value = _claim_value(match.group("int"), match.group("frac"), match.group("tail") or "")
        if value is not None and _near(known_values, value, relative=1e-9, absolute=1e-9):
            operands.add(value)
    operands = set(sorted(operands)[:40])
    target = claim.value
    tolerance = max(abs(target) * 0.006, 0.5 * 10 ** -claim.decimals)
    for a in operands:
        for b in operands:
            if a == b:
                continue
            results = [a - b, a + b, a * b] if strong else [a - b, a + b]
            if b:
                results += [a / b, 100 * a / b, 100 * (a - b) / b] if strong else [a / b] + ([100 * (a - b) / b] if "%" in claim.text else [])
            if any(abs(abs(result) - target) <= tolerance for result in results):
                return True
    return False


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


_EU_DECIMAL_UNIT_RE = re.compile(rf"(?<![\w.,])(\d{{1,3}}),(\d{{1,2}})(?=\s*(?:%|\uff05|(?:{_PERCENT_ALT})\b|(?:{_SCALE_ALT})\b))", re.I)


def corpus_number_set(corpus_text: str) -> set[str]:
    numbers: set[str] = _european_numbers(corpus_text)
    text = _EU_DECIMAL_UNIT_RE.sub(r"\1.\2", corpus_text)  # "2,5 miljoen" / "3,4 procent": the decimal comma before a unit
    for match in _NUMBER_RE.finditer(text):
        numbers.update(_corpus_forms(match.group("int"), match.group("frac"), match.group("tail") or ""))
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
MAX_TRANSLATED_QUOTES = 3   # a translation label buys at most this many unchecked quotations per article
# Paired quotation marks other than the straight/curly double quote: guillemets, German low-high, CJK corner brackets.
_OTHER_PAIRS = ((r"\u00ab", r"\u00bb"), (r"\u2039", r"\u203a"), (r"\u201e", r"[\u201c\u201d]"), (r"\u300c", r"\u300d"), (r"\u300e", r"\u300f"))
_OTHER_PAIR_RES = tuple(re.compile(f"{a}([^{b[1:-1] if b.startswith('[') else b}]*){b}") for a, b in _OTHER_PAIRS)
# A single-quoted passage: the opening mark follows a non-word character and the closing one is not followed by a letter, so
# apostrophes inside words (don't, the dogs' bark) never open a quotation.
_SINGLE_QUOTED_RES = (
    re.compile(r"(?<![\w\u2019'])'([^'\n]{15,}?)'(?![\w])"),
    re.compile(r"(?<![\w\u2019'])\u2018([^\u2018\u2019\n]{15,}?)\u2019(?![\w])"),
)


def _quote_spans(paragraph: str) -> list[tuple[int, int]]:
    """(start, end) of the text inside each quotation in one paragraph.

    Curly double quotes pair themselves. Straight double quotes pair in order (1st with 2nd, 3rd with 4th), which matters: a
    short quoted phrase ("65 million years") must not hand its closing quote to the *next* quotation and turn the prose between
    them into a phantom quotation. An odd straight quote at the end (5" mesh) is left unpaired. Guillemets, German „…“, CJK
    corner brackets and single-quoted passages count too: the checker must see a quotation however it is punctuated."""
    spans = [(m.start(1), m.end(1)) for m in _CURLY_QUOTE_RE.finditer(paragraph)]
    inside = [(m.start(), m.end()) for m in _CURLY_QUOTE_RE.finditer(paragraph)]
    for pattern in _OTHER_PAIR_RES:
        for m in pattern.finditer(paragraph):
            spans.append((m.start(1), m.end(1)))
            inside.append((m.start(), m.end()))
    straight = [i for i, ch in enumerate(paragraph) if ch == '"' and not any(a <= i < b for a, b in inside)]
    for k in range(0, len(straight) - 1, 2):
        spans.append((straight[k] + 1, straight[k + 1]))
        inside.append((straight[k], straight[k + 1] + 1))
    for pattern in _SINGLE_QUOTED_RES:
        for m in pattern.finditer(paragraph):
            if not any(a <= m.start() < b for a, b in inside):  # a 'quote' inside a "quote" is the outer quote's business
                spans.append((m.start(1), m.end(1)))
    return sorted(spans)


def _paragraph_quotes(markdown: str) -> list[tuple[str, str]]:
    """[(quote, context around it)] for every quotation of six or more words."""
    found: list[tuple[str, str]] = []
    for paragraph in re.split(r"\n\s*\n", _strip_blocks(markdown)):
        paragraph = re.sub(r"\s*\n\s*", " ", paragraph)  # a quotation may wrap across source lines
        for start, end in _quote_spans(paragraph):
            quote = re.sub(r"\s+", " ", paragraph[start:end]).strip()
            if len(paragraph[start:end]) < 20 or len(quote.split()) < 6:
                continue
            found.append((quote, paragraph[max(0, start - 61):start - 1] + " " + paragraph[end + 1:end + 91]))
    return found


def classify_quotes(markdown: str) -> tuple[list[str], list[str]]:
    """Split quotations into (to_verify, labelled_translation). A quotation labelled as the writer's translation cannot match a
    foreign-language page verbatim, but the label alone proves nothing: ``check_article`` exempts it only when the original
    wording is quoted beside it and found in the evidence (see ``_translation_originals``)."""
    verbatim: list[str] = []
    translated: list[str] = []
    for quote, around in _paragraph_quotes(markdown):
        (translated if _TRANSLATION_CUE_RE.search(around) else verbatim).append(quote)
    return verbatim, translated


def extract_quotes(markdown: str) -> list[str]:
    verbatim, translated = classify_quotes(markdown)
    return verbatim + translated


def quote_in_corpus(quote: str, normalized_corpus: str) -> bool:
    """Every piece of the quotation (split at an ellipsis) must occur in the corpus, in the order the quotation gives them."""
    pieces = [piece.strip(" .,;:") for piece in re.split(r"\s*(?:\.\.\.|\u2026|\[\u2026\]|\[\.\.\.\])\s*", normalize_text(quote)) if len(piece) >= 12]
    if not pieces:
        pieces = [normalize_text(quote).strip(" .,;:")]
    position = 0
    for piece in pieces:
        found = normalized_corpus.find(piece, position)
        if found < 0:
            return False
        position = found + len(piece)
    return True


def _translation_originals(markdown: str, translated: list[str], normalized_corpus: str) -> set[str]:
    """Labelled translations that have a verifying original: another quotation in the same paragraph (any language, twelve or
    more characters because Japanese has no spaces) that is found in the evidence. Without it the label is an unchecked claim,
    so the quotation is verified like any other."""
    verified: set[str] = set()
    for paragraph in re.split(r"\n\s*\n", _strip_blocks(markdown)):
        paragraph = re.sub(r"\s*\n\s*", " ", paragraph)
        spans = [re.sub(r"\s+", " ", paragraph[a:b]).strip() for a, b in _quote_spans(paragraph)]
        spans = [q for q in spans if len(q) >= 12]
        claimed = [q for q in spans if q in translated and not quote_in_corpus(q, normalized_corpus)]
        originals = [q for q in spans if q not in claimed and quote_in_corpus(q, normalized_corpus)]
        if originals:
            verified.update(claimed)
    return verified


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
            # Crossref is one registration agency of several: DataCite (Zenodo, figshare, Dryad), JaLC, mEDRA... also issue real DOIs.
            try:
                handle = fetcher.get_json(f"https://doi.org/api/handles/{doi}")
            except Exception as handle_exc:  # noqa: BLE001
                if "404" in str(handle_exc):
                    return {"status": "not_found", "detail": "no registration agency has a record of this DOI"}
                return {"status": "unchecked", "detail": f"doi.org lookup failed: {str(handle_exc)[:120]}"}
            if isinstance(handle, dict) and handle.get("responseCode") == 1:
                return {"status": "ok", "detail": "registered with an agency other than Crossref (title not compared)"}
            return {"status": "not_found", "detail": "no registration agency has a record of this DOI"}
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

    import net_safety_core

    session = net_safety_core.mount_safe(requests.Session(), max_bytes=1_000_000)
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; BriefingCitationCheck/1.0)"

    def check(url: str) -> int | None:
        try:
            response = session.head(url, timeout=timeout, allow_redirects=True)
            if response.status_code in (400, 403, 405, 429, 501):
                response = session.get(url, timeout=timeout, stream=True)
            return response.status_code
        except net_safety_core.UnsafeUrl:
            return -1  # a citation that points at a non-public address is not a source
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


def _doi_citations(markdown: str, entries: list[str]) -> list[tuple[str, str]]:
    """[(doi, citation text)] for every DOI in the article: those in a Primary Sources entry are compared with the entry's title
    text; a DOI anywhere else in the body (a fabricated one placed in prose, outside the block the checker used to read) is only
    required to exist (no title to compare)."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        for doi in DOI_RE.findall(entry):
            doi = clean_doi(doi)
            if doi not in seen:
                seen.add(doi)
                found.append((doi, entry))
    for doi in DOI_RE.findall("\n".join(line for line in markdown.splitlines() if not line.lstrip().startswith(("![", "<!--")))):
        doi = clean_doi(doi)
        if doi not in seen:
            seen.add(doi)
            found.append((doi, ""))
    return found


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------
_OPEN_QUOTE = "\"\u201c\u201e\u00ab\u2018'\u300c"
_CLOSE_QUOTE = "\"\u201d\u201c\u00bb\u2019'\u300d"


def cited_urls(markdown: str) -> list[str]:
    """Every address the article cites (its Primary Sources and Watch Next lines; the body carries none), in order, without duplicates."""
    seen: dict[str, None] = {}
    for line in markdown.splitlines():
        if line.lstrip().startswith(("![", "<!--")):
            continue
        for url in _URL_RE.findall(line):
            seen.setdefault(url.rstrip(".,;:"), None)
    return list(seen)


def demote_quotes(markdown: str, quotes: list[str]) -> tuple[str, int]:
    """Give each listed quotation as paraphrase: its quotation marks are removed and its words stay. A quotation that could be checked
    only against a page summary (a small model's answer about the page) is not shown as the page's own words. Only text before the
    Primary Sources block is touched. Returns (new markdown, number of quotations changed)."""
    block = re.search(r"(?m)^###\s+Primary Sources\b", markdown)
    head, tail = (markdown[:block.start()], markdown[block.start():]) if block else (markdown, "")
    changed = 0
    for quote in quotes:
        words = [re.escape(w) for w in quote.split()]
        if len(words) < 3:
            continue
        pattern = re.compile(rf"[{re.escape(_OPEN_QUOTE)}]\s*({r'\s+'.join(words)})\s*[{re.escape(_CLOSE_QUOTE)}]")
        head, n = pattern.subn(lambda m: m.group(1), head)
        changed += 1 if n else 0
    return head + tail, changed


def check_article(
    markdown: str,
    corpus: list[str],
    *,
    json_fetcher: JsonFetcher | None = None,
    url_checker: UrlChecker | None = None,
    check_network: bool = True,
    verbatim_corpus: list[str] | None = None,
    summary_only_limit: int = 3,
) -> ProvenanceReport:
    """``corpus`` is everything the writer saw. ``verbatim_corpus`` (when given) is the part that is raw page text fetched by
    the script; the rest is a small model's *answer* about a page. A quotation found only in the answers is reported in
    ``summary_only_quotes``: it is evidence the page said something close, not that the words are the page's own."""
    report = ProvenanceReport(summary_only_limit=summary_only_limit)
    corpus_text = "\n".join(corpus)
    report.corpus_chars = len(corpus_text)
    normalized_corpus = normalize_text(corpus_text)
    known_numbers = corpus_number_set(corpus_text)

    known_values = _numeric_values(known_numbers)
    for claim in extract_numeric_claims(markdown):
        report.numbers_checked += 1
        if any(form in known_numbers for form in claim.forms):
            report.numbers_matched += 1
        elif derivable(claim, known_values):
            report.numbers_matched += 1
            report.derived_numbers.append(claim.text)
        else:
            report.unmatched_numbers.append({"text": claim.text, "context": claim.context})

    normalized_verbatim = normalize_text("\n".join(verbatim_corpus)) if verbatim_corpus is not None else None
    verbatim, labelled = classify_quotes(markdown)
    exempt = _translation_originals(markdown, labelled, normalized_corpus)
    for quote in labelled:
        if quote in exempt and not quote_in_corpus(quote, normalized_corpus) and len(report.translated_quotes) < MAX_TRANSLATED_QUOTES:
            report.translated_quotes.append(quote)
        else:
            verbatim.append(quote)  # no original beside it (or over the cap): the label does not excuse it
    for quote in verbatim:
        report.quotes_checked += 1
        if not quote_in_corpus(quote, normalized_corpus):
            report.unmatched_quotes.append(quote)
        elif normalized_verbatim is not None and not quote_in_corpus(quote, normalized_verbatim):
            report.summary_only_quotes.append(quote)

    entries = _primary_source_entries(markdown)
    for doi, citation in _doi_citations(markdown, entries):
        if doi in report.dois:
            continue
        if doi.lower() in normalized_corpus:
            report.dois[doi] = {"status": "ok", "detail": "appeared in an opened source"}
        elif check_network and json_fetcher is not None:
            report.dois[doi] = check_doi(doi, citation, json_fetcher)
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
                if status in (0, 404, 410, -1):
                    report.dead_urls.append({"url": url, "status": {0: "dns failure", -1: "non-public address"}.get(status, status)})
                elif status is None or status in (401, 403, 429, 451, 999) or status >= 500:
                    report.unknown_urls.append(url)
    return report


__all__ = [
    "NumberClaim", "ProvenanceReport", "check_article", "check_doi", "cited_urls", "clean_doi", "default_url_checker", "demote_quotes",
    "extract_numeric_claims", "extract_quotes", "normalize_text", "quote_in_corpus",
]
