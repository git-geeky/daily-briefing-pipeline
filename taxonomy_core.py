"""Scripted taxonomy and conservation-status facts (GBIF backbone + IUCN Red List category via GBIF).

Why a script and not a model: whether *Myrmecobius fasciatus* is a real, accepted name and whether the
IUCN category in an article's status table is the registry's category are lookups, not judgments.
Doing them here costs no subscription usage, gives the writer facts it would otherwise spend tool calls
(and sometimes hit HTTP 403 registry pages) to find, and catches typos and stale categories after drafting.

* ``registry_facts`` - for the slate's taxa, before drafting: accepted/synonym/unknown + IUCN category.
* ``check_article_taxonomy`` - after drafting: misspelt names (FUZZY match), synonyms, and a status table
  whose IUCN category disagrees with the registry.

Names the backbone does not know (``HIGHERRANK``/``NONE``) are *not* errors: newly described species are not
in the backbone yet. They are reported, never repaired.
GBIF's IUCN data can lag a very recent Red List release, so a mismatch is a prompt to verify, not a verdict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

GBIF = "https://api.gbif.org/v1"
_BINOMIAL_RE = re.compile(r"(?<![\w*])\*(?P<name>[A-Z][a-z]{2,}(?: [a-z]{3,}){1,2})\*(?![\w*])")
_IUCN_CODES = {
    "EXTINCT": "EX", "EXTINCT IN THE WILD": "EW", "CRITICALLY ENDANGERED": "CR", "ENDANGERED": "EN", "VULNERABLE": "VU",
    "NEAR THREATENED": "NT", "LEAST CONCERN": "LC", "DATA DEFICIENT": "DD", "NOT EVALUATED": "NE",
}
_CODE_RE = re.compile(r"\b(EX|EW|CR|EN|VU|NT|LC|DD|NE)\b")
_WORD_RE = re.compile("|".join(sorted(map(re.escape, _IUCN_CODES), key=len, reverse=True)), re.IGNORECASE)


class JsonFetcher(Protocol):
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any: ...


@dataclass
class NameFacts:
    given: str
    match: str = "NONE"                 # EXACT | FUZZY | HIGHERRANK | NONE
    canonical: str | None = None
    status: str | None = None           # ACCEPTED | SYNONYM | DOUBTFUL
    rank: str | None = None
    usage_key: int | None = None
    iucn_code: str | None = None
    iucn_name: str | None = None
    iucn_url: str | None = None
    error: str | None = None

    @property
    def in_backbone(self) -> bool:
        return self.match in {"EXACT", "FUZZY"} and self.rank in {"SPECIES", "SUBSPECIES", "VARIETY", "FORM"}


@dataclass
class TaxonomyReport:
    facts: dict[str, NameFacts] = field(default_factory=dict)
    misspellings: dict[str, str] = field(default_factory=dict)      # given -> suggested canonical name
    synonyms: dict[str, str] = field(default_factory=dict)          # given -> accepted name
    not_in_backbone: list[str] = field(default_factory=list)
    status_mismatches: list[dict[str, str]] = field(default_factory=list)
    lookup_errors: list[str] = field(default_factory=list)

    def needs_repair(self) -> bool:
        return bool(self.misspellings or self.status_mismatches)

    def repair_brief(self) -> str:
        lines = []
        for given, suggested in self.misspellings.items():
            lines.append(f'- scientific name "{given}" is not a valid name; the closest accepted name in the GBIF backbone is "{suggested}". Correct it everywhere, or remove it if the animal is something else.')
        for item in self.status_mismatches:
            lines.append(f'- the status table gives {item["name"]} as {item["table"]}, but the IUCN category held by the GBIF integration is {item["registry"]} '
                         f'({item["url"]}). Confirm against the Red List (it may be a very recent reassessment) and either correct the table or say in the table which source and date the category comes from.')
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "misspellings": self.misspellings, "synonyms": self.synonyms, "not_in_backbone": self.not_in_backbone,
            "status_mismatches": self.status_mismatches, "lookup_errors": self.lookup_errors,
            "checked": len(self.facts),
        }


def extract_binomials(markdown: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in _BINOMIAL_RE.finditer(markdown):
        seen.setdefault(match.group("name"), None)
    return list(seen)


def lookup_name(name: str, fetcher: JsonFetcher, *, with_iucn: bool = True) -> NameFacts:
    facts = NameFacts(given=name)
    try:
        match = fetcher.get_json(f"{GBIF}/species/match", {"name": name, "kingdom": "Animalia"})
    except Exception as exc:  # noqa: BLE001 - an outage must never fail a run
        facts.error = f"{exc!r}"[:160]
        return facts
    facts.match = str(match.get("matchType") or "NONE")
    facts.canonical = match.get("canonicalName")
    facts.status = match.get("status")
    facts.rank = match.get("rank")
    facts.usage_key = match.get("usageKey")
    if with_iucn and facts.in_backbone and facts.usage_key:
        key = match.get("acceptedUsageKey") or facts.usage_key
        url = f"{GBIF}/species/{key}/iucnRedListCategory"
        try:
            category = fetcher.get_json(url)
            code = category.get("code")
            if code:
                facts.iucn_code, facts.iucn_name, facts.iucn_url = str(code), str(category.get("category") or "").replace("_", " ").title(), url
        except Exception:  # noqa: BLE001 - 404 simply means GBIF holds no IUCN category for this taxon
            pass
    return facts


def lookup_names(names: list[str], fetcher: JsonFetcher, *, with_iucn: bool = True) -> dict[str, NameFacts]:
    return {name: lookup_name(name, fetcher, with_iucn=with_iucn) for name in dict.fromkeys(names)}


def registry_facts(taxa: list[dict[str, str]], fetcher: JsonFetcher) -> str:
    """Markdown facts block for the writer's dossier, from the slate's scientific names."""
    names = [t["scientific"] for t in taxa if t.get("scientific")]
    if not names:
        return ""
    lines = ["## Registry facts (script lookup: GBIF backbone; IUCN category as held by GBIF, which can lag the newest Red List release)"]
    for name, facts in lookup_names(names, fetcher).items():
        if facts.error:
            lines.append(f"- *{name}*: lookup failed ({facts.error}); verify on the registry")
        elif facts.match == "FUZZY":
            lines.append(f"- *{name}*: NOT a valid name; closest accepted name is *{facts.canonical}* (check the spelling against the source)")
        elif not facts.in_backbone:
            lines.append(f"- *{name}*: not in the GBIF backbone (probably newly described or very recent); take taxonomy from the paper")
        else:
            synonym = f" (synonym of *{facts.canonical}*)" if facts.status == "SYNONYM" else ""
            iucn = f"; IUCN Red List: {facts.iucn_code} ({facts.iucn_name}) <{facts.iucn_url}>" if facts.iucn_code else "; no IUCN category held by GBIF"
            lines.append(f"- *{name}*: accepted {facts.rank.lower() if facts.rank else 'taxon'}{synonym}{iucn}")
    return "\n".join(lines)


def _table_status_by_name(markdown: str) -> dict[str, str]:
    """Binomial -> the IUCN code in that row's IUCN column ("NT (from EN)" -> NT; first category wins)."""
    out: dict[str, str] = {}
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        header = [c.strip() for c in line.strip().strip("|").split("|")]
        iucn_col = next((i for i, c in enumerate(header) if re.search(r"iucn|red list", c, re.IGNORECASE)), None)
        if iucn_col is None:
            continue
        for row in lines[index + 2:]:
            if not row.strip().startswith("|"):
                break
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cells) <= iucn_col:
                continue
            names = _BINOMIAL_RE.findall(" ".join(cells))
            cell = cells[iucn_col]
            code = None
            word = _WORD_RE.search(cell)
            code_match = _CODE_RE.search(cell)
            candidates = [(word.start(), _IUCN_CODES[word.group(0).upper()])] if word else []
            if code_match:
                candidates.append((code_match.start(), code_match.group(1)))
            if candidates:
                code = min(candidates)[1]
            if code:
                for name in names:
                    out.setdefault(name, code)
    return out


def check_article_taxonomy(markdown: str, fetcher: JsonFetcher) -> TaxonomyReport:
    report = TaxonomyReport()
    names = extract_binomials(markdown)
    report.facts = lookup_names(names, fetcher)
    table = _table_status_by_name(markdown)
    for name, facts in report.facts.items():
        if facts.error:
            report.lookup_errors.append(f"{name}: {facts.error}")
        elif facts.match == "FUZZY" and facts.canonical and facts.canonical != name:
            report.misspellings[name] = facts.canonical
        elif not facts.in_backbone:
            report.not_in_backbone.append(name)
        else:
            if facts.status == "SYNONYM" and facts.canonical:
                report.synonyms[name] = facts.canonical
            claimed = table.get(name)
            if claimed and facts.iucn_code and claimed != facts.iucn_code:
                report.status_mismatches.append({"name": name, "table": claimed, "registry": f"{facts.iucn_code} ({facts.iucn_name})", "url": facts.iucn_url or ""})
    return report


__all__ = ["NameFacts", "TaxonomyReport", "check_article_taxonomy", "extract_binomials", "lookup_name", "lookup_names", "registry_facts"]
