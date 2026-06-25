# Shared Briefing Generation Spec

Use this as the shared validation-critical reference for both Codex and Claude briefing runs.

## Required Conventions

- Header cross-references should use the quoted form by default:
  `Cross-reference: Part X - "Exact Article Title"`
- The validator also accepts:
  `See Part X, Article Y — Exact Article Title`
- Relative time inside article prose is anchored to the briefing date in JST unless a more specific date is given.
- `Watch Next 48h` entries still require explicit, verifiable dates and times.
- Short but sound beats padded. If a section cannot honestly meet quota, emit a visible `Quality Failure` block rather than filler.
- Header cross-reference matching is normalized for harmless punctuation and quote drift, but the safest path is still to reuse the exact article title text.

## Image Policy

- Empty-article fallback insertion is off by default.
- Auto-added images must be high-confidence and article-relevant.
- The assembled daily briefing must contain exactly one validated inline image total. Additional embedded images are validation failures.
- Approved automatic image origins:
  - direct authored inline image that validates
  - source `og:image` / source hero image
  - exact entity-match Wikimedia Commons image
- Disabled automatic fallbacks for current-affairs stories:
  - generic archival material
  - Library of Congress substitutes
  - generic stock-photo sources
  - weak Phase 3 entity guesses
- If no high-confidence image validates, ship a visible image-quality failure marker rather than a decorative substitute.

## Structural Requirements

- Parts I-IV require a `Counterpoint Matrix`.
- Parts I-IV require at least two real `Counterpoint Matrix` rows so the source-tensions artifact can capture disagreement rather than a token caveat.
- Parts I-IV require a `Watch Next 48h` block sourced from verifiable schedules or calendars.
- Source appendices belong at the end of the article. Do not leave inline body links outside the appendix.
- Human-interest and closing sections still require source appendices.
- Closing must remain a single `##` item. Fold any extra close-out material into `###` subheads inside that one article.

## Article Prose Quality

- Never narrate the generation process inside an article. Do not mention Perplexity, MCP, source audit, candidate pool, discovery pass, validator behavior, source-quality decisions, or why the article was selected. Those belong in run reports and provenance artifacts, not reader-facing copy.
- Do not pad articles with reusable editorial templates. A paragraph that could be pasted into another section with only the headline changed is not analysis; replace it with article-specific facts, chronology, named actors, stakes, constraints, and source-grounded uncertainty.
- Keep analogies inside the article's domain. A Lebanon evacuation story should not talk about model cards, AI vendors, enterprise lock-in, market disclosures, or procurement unless those are actually part of that story.
- Every hard-news article should answer: what happened, where, when, who is affected, what changed from the prior baseline, what evidence is strongest, what evidence is missing, and what exact follow-up would change the assessment.
- If the available evidence cannot support concrete article-specific prose above the hard floor, emit `## Quality Failure: <reason>` instead of filling the article with meta commentary about research limitations.

## Provenance Artifacts

- `generate_source_audit.py` automatically syncs checked catalog sources into the persistent research vault.
- Assembly and validation write:
  - evidence digest markdown
  - source tensions JSON extracted from Counterpoint Matrix rows
  - research lint JSON
  - source confidence JSON with CRAAP-lite source type/tier counts, per-article confidence, falsifiable checks, and open questions
  - research depth JSON/Markdown with per-section breadth, depth, learnings, visited source URLs, and recursive follow-up queries
  - adaptive fetch JSON with source-audit recovery attempts, public endpoint/Jina/RSS variants, metadata extraction, and low-trust sidecar provenance
- A research-lint `error` is a blocking validation failure. A `warning` should be treated like other validation warnings.
- The research-depth artifact adapts the useful dzhng/deep-research pattern without adding a live Firecrawl/LLM dependency to the scheduler: broad first pass, narrower follow-up breadth, retained learnings, and explicit source URLs.
- The source-confidence artifact adapts the useful Aeon deep-research pattern: honest source tiering, source diversity audit, confidence calibration, and falsifiable checks. Treat T1/T2/T3 as a research aid, not a license to cite weak sources.
- The adaptive-fetch artifact adapts the useful insane-search pattern without launching a browser or installing bypass tooling in unattended runs: public/specific endpoints first, lightweight desktop/mobile/googlebot probes, RSS/feed variants, Jina Reader fallback, challenge/auth/tiny-body classification, metadata extraction, optional TLS impersonation only when explicitly allowed, and archive/Wayback as low-trust provenance only.
- After validation, repair by patching the smallest affected article/table/source appendix. Do not regenerate the entire briefing unless the run is structurally unrecoverable.

## Editorial Floors

Three-tier word count band (see `SECTION_RULES` in `briefing_manifest_core.py` for the authoritative per-section numbers):

- **Target** (`min_words`): the editorial aspiration — hard news 1500, human interest / closing 1200, updates 800.
- **Soft min** (`soft_min_words`): below this, articles pass as non-blocking advisories — hard news 900, human interest / closing 700, updates 500.
- **Hard floor** (`hard_floor`): below this, the article is a blocking validation failure — hard news 700, human interest / closing 500, updates 400.

Shipping discipline:
- Target → hard floor is the three-band ramp. The middle band is a "we noticed" advisory, not a blocker.
- A clean `## Quality Failure: <reason>` block always beats a padded article.
- Invoking `--skip-validation` to ship below the hard floor is an explicit override and must be recorded in the close-out.

## Literal Format Specifications (Machine-Parsed)

These are the canonical strings the validator regexes match. Both runner prompt templates should reference this file rather than embedding their own examples — any drift here risks re-introducing the 2026-04-20 / 2026-04-21 validator bypass pattern.

### Header: Top 3 Structural Forces cross-reference token

Each structural-force bullet in the header MUST end with a cross-reference token. Canonical form:

```
- **Force Name:** One sentence describing the force. Cross-reference: Part II - "Exact Article Title From Content Fragment".
```

Accepted variants (all parsed by `HEADER_CROSS_REF_PATTERNS` in `briefing_manifest_core.py`):

- Case-insensitive prefix: `Cross-reference:`, `cross-reference:`, `Cross Reference:`, `cross reference:`
- Quote styles: ASCII `"..."`, ASCII `'...'`, curly `"..."` (U+201C/201D), curly `'...'` (U+2018/2019), backtick `` `...` ``
- Fallback narrative form: `See Part IV, Article 2 — Exact Article Title` (em-dash U+2014, en-dash U+2013, or ASCII hyphen all accepted)

The quoted string must match an `##` article heading in the content fragment character-for-character (same punctuation, same dashes).
The validator now also accepts canonical-title matches where only punctuation, quote style, or dash style drifted.

### Article: Primary Sources block heading

Accepted canonical forms (all parsed via `PRIMARY_SOURCES_HEADINGS` in `briefing_manifest_core.py`):

```
### Primary Sources
### Sources
### Source Articles
**Primary Sources**:
**Primary Sources:**
**Sources**:
**Sources:**
**Source Articles**:
**Source Articles:**
```

The block content is parsed as `-` bullet list entries until the next blank line followed by another heading or the next `##`/`###`.

### Article: Watch Next 48h block heading

Accepted canonical forms (all matched via `WATCH_NEXT_HEADING_RE`):

```
> **Watch Next 48h:**
> **Watch Next 48 hours:**
> **Watch Next 48hr:**
> **Watch Next 48hrs:**
> **Watch Next 48 hour:**
```

Whitespace between `48` and `h`/`hours` is flexible. The line must be followed by `-` bullets, each bullet describing one verifiable upcoming event in the form `Event description (Source URL)`.

### Article: Table of Contents line format

Either numbered or bulleted list formats are accepted under Part headers:

```
**PART I — GENERAL NEWS**
1. First Article Title
2. Second Article Title
```

or

```
**PART I — GENERAL NEWS**
- First Article Title
- Second Article Title
```

### Banned topic matching

The validator uses `BANNED_TOPIC_RULES` in `briefing_manifest_core.py` — a tuple of compiled regex patterns, each with a descriptive label. Regex patterns use negative lookaheads to avoid substring false positives (`crypto` excludes `cryptography`, `film` excludes `film festival`/`documentary`, `tv show` excludes `tv show called news`). If a new banned topic is added, add it as a new pattern row — do not fall back to substring matching.

Current false-positive protections explicitly allow:
- `cryptography`
- documentary / documentary-film / archive / oral-history contexts around `film`
- state-television wording that is not entertainment coverage
