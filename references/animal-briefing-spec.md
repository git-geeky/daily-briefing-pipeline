# Animal Science Briefing — Editorial & Structural Spec

Edition id: `animal` (`BRIEFING_EDITION=animal`). Config: `editions/animal.json`. Source catalogs: `sources.md` (filtered to science, wires, regional and native-language buckets) plus `animal-sources.md` (390 animal-specific sources in 37 languages, with `animal-sources.meta.json` carrying language, region, topics and feed URLs). The catalogs are a **floor**: follow leads to the paper, dataset, agency decision, or local-language report behind every press release.

## Reader

An expert. Assume fluency in zoology, ecology, evolutionary biology, conservation status systems (IUCN Red List categories and criteria, CITES appendices, ESA/EPBC/national listings), veterinary and One Health vocabulary, and basic statistics. Never define IUCN, apex predator, phylogeny, or p-value. Do not explain what a reader at this level already knows; spend the words on mechanism, design, effect size, uncertainty, prior literature, and what would change the conclusion.

Voice: precise, sober, specific, **impersonal**. Never write in the first person ("I could not open...", "my subtraction", "we"). Say plainly when a figure is derived ("the ratio is computed here from the two totals"). **Sources you could not open do not factor in:** they are not cited, not listed in Primary Sources, not discussed and not mentioned (no "the paper was not read", no "HTTP 403"); the article stands on what was read. Calibrated hedging ("suggests", "is consistent with", "demonstrates") that matches the study design. No anthropomorphism, no "amazing", no listicle rhythm, no padded transitions. Numbers carry units, sample sizes, intervals and years.

## What gets covered (selection)

Rank candidates by: (1) scientific significance and robustness of the evidence, (2) consequence for species, populations, ecosystems, animal health, or policy, (3) novelty against the previous four briefings, (4) how much *unique* source material exists to support a long article. Cover the whole discipline — behaviour and cognition, physiology, genomics and evolution, paleontology of animals, ecology and population biology, conservation status changes and decisions, disease and One Health (wildlife, livestock, zoonoses), veterinary science, welfare *science*, zoo and aquarium science (breeding programmes, husbandry research), marine and freshwater fauna, insects and other invertebrates, biologging and bioacoustics methods, human–wildlife coexistence, reintroductions.

Balance (applied by the slate stage, checked by the reviewer): at most three articles per major taxonomic group (mammals, birds, fish and marine fauna, herps, invertebrates); at least one invertebrate story when material exists; at least two stories centred outside the US and Western Europe when material exists; at least three articles whose evidence includes a **native-language primary source** from the catalog. A story that only restates a prior briefing item is excluded unless there is a material new development, in which case mark it `prior:` (see story markers).

### Exclusions

- **Animal cruelty.** No stories whose subject is cruelty, abuse, torture, neglect cases, animal fighting, hoarding, puppy mills, slaughterhouse exposés, or graphic harm. Enforcement and trade *science* (forensic genetics of seized ivory, trade-volume analysis, CITES compliance data) and welfare *science* (pain, sentience and stress evidence, husbandry research) are in scope when treated analytically without graphic description. Disease-control culling is covered only as epidemiology and policy.
- Pet-care advice, product reviews, viral-video stories, celebrity-animal stories, and press-release repackaging with no accessible evidence.
- De-extinction and "first of its kind" corporate claims are covered only with explicit skepticism and the underlying data (or the absence of it).

## Quota and length

Adaptive: **target 10** full articles, **minimum 3**, **maximum 12**. Quality outranks the count: an article whose honest **Confidence is Low** (including "Low to Medium") is not written at all, and the story is replaced by the next reserve. Aim for **3,500 words** per article *when the unique source material supports it*; a story with less genuine material is written shorter, honestly, never padded. Bands (validator): below 1,500 words blocks; 1,500–2,499 is non-blocking; 2,500–3,499 is a warning. Filler is a defect: restated sentences, generic background the reader already has, and vague attribution all count against an article. A story that cannot reach the hard floor with real material is replaced from the candidate pool or dropped with a recorded reason; never fabricate depth.

A **Human Interest** section (maximum 3, never part of the target) exists only to fill the gap left by animal-science stories that could not be written with confidence: it is used after the animal-science reserves are exhausted, and left out when no good human-interest story exists; the briefing is then simply shorter.

A short **Also Noted** section (target 6, maximum 10) carries real but smaller items in 110–300 words each, each with a bottom line and two or more sources.

## Document layout

```
# The Animal Science Briefing
**<Weekday, Month D, YYYY>** | <prepared-by line>

## Executive Summary
250–750 words: the day's through-line, then the stories in order with one-clause bottom lines.

### Cross-currents
- <theme in one sentence>. Cross-reference: "<exact article title>"
- <theme>. Cross-reference: "<exact article title>"          (at least 2; one reference per line)

### Table of Contents
(rebuilt automatically from article titles)

# THE DAY IN ANIMAL SCIENCE
## <Article title>
...
# HUMAN INTEREST                       (only when it is needed to fill a gap)
## <Story title>
...
# ALSO NOTED
## <Item title>
...
```

### Article template (section `animals`)

```
## <Specific, informative title — what was found, where, in which taxon>
<!-- story: <stable-slug>[; prior: <slug-of-earlier-briefing-story>] -->

![<caption with creator, licence, source URL>](<local image path>)     <- inserted by the photo stage; do not write image lines

**Bottom line:** two to three sentences stating what is established, what is not, and why it matters.
**Confidence:** High | Medium — one clause on why (design, replication, source quality). The pipeline does not publish an article rated Low.

### Taxonomy & Status
| Taxon | Scientific name | IUCN Red List | CITES | Range / population |
|---|---|---|---|---|
| African savanna elephant | *Loxodonta africana* | EN (2021, decreasing) | Appendix I/II (by population) | ... |

### What happened
Dated, exact, sourced. Absolute dates with the briefing's time zone; no "yesterday".

### Natural history and ecology
Species-specific context that bears on the finding: life history, physiology, behaviour, habitat, demography. Expert level.

### The evidence
Study / evidence card: citation with `doi:`; design; sample (n, sites, years); key results with effect sizes and intervals; data/code availability; funding and conflicts; peer-review status (peer-reviewed, preprint, press release only). For non-study events (a listing decision, a birth, an outbreak) this is the evidence trail: which document or dataset says what.

### Methods and limits
What the evidence cannot show: sampling frame, confounding, pseudoreplication, captive vs wild, taxon generalisation, detection error, small n, multiple comparisons, surrogate endpoints, press-release inflation.

### How this fits the literature
Prior work it confirms, contradicts, or extends, with citations in the source list.

### Implications
Consequences for conservation, husbandry, veterinary practice, theory, or policy — separated clearly from the findings.

### Counterpoint Matrix
| Claim | Who holds it | Evidence for | Evidence against or limits | What would change the call |
|---|---|---|---|---|

### Watch Next
- <exact date>: <dated event, decision, release, season, or publication to watch> (<source URL>)

### Primary Sources
- Author(s) (year). Title. *Journal*. doi:10.xxxx/yyyy https://...
- <Institution, document, language: es> https://...
```

Rules that the validators enforce (see `editions/animal.json`):

- `Bottom line`, `Confidence` (High/Medium/Low), and at least one italicised scientific binomial are **blocking**. Taxonomy & Status table, evidence card, methods-and-limits section, and DOIs are expected (warnings when absent; legitimately absent for some event stories).
- Counterpoint Matrix and Watch Next are blocking. Watch Next bullets are dated and carry a source URL, and each names something that will happen outside the briefing (a decision, release, season, data deposit, conference, deadline), never a check the reader is told to make ("one month after publication, check the page"). If none is scheduled, name the next dated one; if there is none, the single bullet "No scheduled external event found for the next 48 hours." is correct.
- **No external links in the body** outside Primary Sources and Watch Next. Cite DOIs in text as `doi:10.xxxx/yyyy`; full URLs go in Primary Sources.
- At least **6 sources** per article, at least **3 distinct source classes** (official/agency, primary research, data/registry, reporting, wire, analysis), and at least **3 sources drawn from the animal catalog pool** (`animals`-credited rows).
- At least one **native-language** source in three or more articles per briefing (warning if fewer); cite the original and translate the load-bearing sentence. Mark language in the source list.
- **Photographs:** every article carries 1–2 licensed photographs placed by the photo stage, caption = description, creator, licence (CC0, public domain, CC BY, CC BY-SA), provider and URL. No unlicensed, watermarked, or AI-generated images; no decorative fallback. If none can be found the article carries an explicit image-failure marker. Never embed an image whose caption lacks a licence.
- Italicise every binomial on first use in each section (`*Panthera tigris*`); use authority and year in the Taxonomy table when the taxonomy is contested.
- Status data (IUCN category and assessment year, CITES appendix, national listing) must come from the registry page cited, with the retrieval year; if a registry could not be reached, leave the cell as an en dash (—) instead of recalling from memory, and do not describe the failure.

### Human Interest template (section `humaninterest`)

People and animals: field researchers and their long-term study animals, rehabilitators, keepers, rangers, communities and citizen scientists living with or protecting wildlife. A real person or place, a real sequence of events and named sources; warm but not sentimental; never cruelty or harm as the subject, never pets and celebrities, never viral clips. 1,200 words when the material supports it (hard floor 500), at least 3 sources, at least one that is the person's or organisation's own account and one independent of it.

```
## <Title: who, where, and what happened>
<!-- story: <stable-slug> -->
**Bottom line:** two to three sentences.
**Confidence:** High | Medium — one clause on why. (A Low rating means the article is not written.)

### The story            dated, sourced, with named people and places
### The animals          the species and what is known about them (italic binomial on first use)
### What it shows        what the account establishes and what it does not
### Primary Sources
```

The same honesty rules apply (verbatim quotations only, no invented detail, no first person). Photographs are inserted by the photo stage when licensed ones exist; none is required.

### Story markers
`<!-- story: slug -->` gives the story a stable identity; add `; prior: slug` when the story is a development of a prior briefing item. Markers are kept in fragments for continuity and stripped from the reader-facing document.

## Honesty rules (all sections)

1. **Provenance.** Every number, date, quotation, and attribution must trace to a source you actually opened in this run. If you could not open it, leave it out. The pipeline cross-checks numbers, quotations, DOIs and URLs against what was fetched; unmatched items are sent back for repair and, if unresolved, flagged to the reader.
2. **Separate** finding, interpretation, and speculation. Mark preprints and press-office claims as such. Where a press release goes beyond its paper, say so.
3. **Name uncertainty** (intervals, detection probability, taxonomy disputes, population-estimate methods). Do not round conflicting estimates into false agreement.
4. **No invention to reach length.** Prefer a shorter, denser article over padding. If the sources you read leave the conclusion at Low confidence, do not write the article (`SKIP: low_confidence`).
5. **Source skepticism.** NGO and zoo material is advocacy- or institution-lensed; trade press is access-lensed; state-aligned outlets carry state framing. Say which lens applies when it matters. Prefer the primary document.
6. **Native-language work.** Search the national-language outlet for locally reported stories; do not rely on English re-reports of foreign events.
7. **Time.** Use absolute dates. Anchor to the briefing date given in the prompt; do not infer "today" from memory.

## Photograph policy

Providers, in order: Wikimedia Commons, iNaturalist (CC0/CC BY/CC BY-SA observations only; never "all rights reserved" or NC/ND), Openverse (licence-filtered), and the cited institution's own press-kit images where it states reuse terms. The photo stage (`photo_core.py`) searches by the scientific name, downloads the best match (≥1200 px wide, licence in the allowed set, creator recorded), and writes the caption. It records every decision in the image report. Licence terms are copied from the provider's metadata at fetch time; they are never inferred.

## Delivery

PDF built by the shared builder, delivered by `deliver_core.py` on the delivery host with `--edition animal`. Recipients come from the delivery config or the `ANIMAL_BRIEFING_RECIPIENTS` environment variable (comma-separated). An idempotent receipt prevents duplicate sends when a scheduled run is retried.
