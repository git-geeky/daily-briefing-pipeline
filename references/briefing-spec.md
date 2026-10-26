# Daily Intelligence Briefing Shared Spec

Use this file as the shared operating spec for both the Codex and Claude briefing runners.

This document is runner-neutral. Resolve all artifact paths, stems, and branding through the local `briefing_paths.py` wrapper for the active runner rather than hardcoding Codex- or Claude-specific output locations here.

`generation-spec.md` remains the validation-critical companion document. Use it together with this file. This file owns the editorial/structural/curation rules; `generation-spec.md` owns the machine-parsed formats and the validator's word-count bands.

## Scope

- Produce a same-day long-form briefing for a JST date.
- Generate a shared source audit first, then draft fragments, hydrate/fetch/remediate images, assemble markdown, validate, build the PDF, run visual QA, and optionally deliver.
- Use live web research with named sources and exact dates.

## Canonical Structure

- `PART I - GENERAL NEWS`: adaptive quota, target 5 articles, minimum 4, maximum 6 — the biggest world news stories of the day.
- `PART II - AI NEWS`: adaptive quota, target 8 articles, minimum 6, maximum 9 — AI industry developments: models, funding, regulation, employment, safety, technical breakthroughs.
- `PART III - NON-AI TECH NEWS`: adaptive quota, target 3 articles, minimum 2, maximum 4 — cybersecurity, semiconductors, infrastructure, developer tools, open source. Include focus-company coverage every day when there is material movement; if there is no material focus company development, record that no-update decision in the candidate pool/run report rather than padding.
- `PART IV - JAPAN NEWS`: adaptive quota, target 3 articles, minimum 2, maximum 4 — national Japan politics/economy plus the home-region and arts beats named in the owner's private coverage notes, when there is material movement. Watch-listed people named in those notes are an explicit watch item: include them when there is a substantive development (an appearance, cancellation, program change, review, interview, festival update); otherwise record a no-update decision rather than padding.
- `PART V - HUMAN INTEREST`: adaptive quota, target 1 article, minimum 1, maximum 3 — acts of courage, unusual lives, small communities, scientific curiosities about people, philanthropy, profiles of ordinary people in extraordinary circumstances. **NEVER about sports, athletes, athletic achievements, film/TV, music, celebrities, gaming, or the entertainment industry.** Real human stories worth telling, not fluff and not celebrity gossip.
- `PART VI - UPDATES`: adaptive quota, target 1 article, minimum 0, maximum 3; only when prior-day stories materially changed in the last 24h. Skip entirely if nothing evolved — zero is a valid answer.
- `CLOSING ANALYSIS`: 1 synthesis section.
- Expansion above target is allowed only when the added article clears the image, sourcing, and score floor. Thin-news days should stop at the minimum or target rather than padding.
- `CLOSING ANALYSIS` stays a single `##` item. Any `What to Watch` or `Default Skepticism Flags` material belongs inside that article as `###` subheads, not as additional top-level closing items.

## Layout And Naming

- Use the active runner profile for:
  - root directory
  - artifact stem
  - PDF name
  - archive/generated split
  - image cache location
- Required artifact families:
  - source-audit JSON artifact
  - optional source-audit markdown companion
  - adaptive-fetch JSON report from source-audit recovery attempts
  - persistent research-vault source index under `research-vault`
  - evidence-digest artifact
  - source-tensions artifact
  - research-lint artifact
  - source-confidence artifact
  - research-depth JSON and Markdown artifacts
  - fragment markdown files
  - per-fragment JSON manifests
  - assembled markdown
  - assembled JSON manifest
  - candidate-pool artifact with accepted/rejected story decisions
  - validation report
  - run report
  - PDF
  - visual audit, when generated

## Quality Bar

- Hard-news articles target `1500+` words.
- Human interest and closing target `1200+` words.
- Updates target `800+` words.
- Between the shared blocking floors and target floors is a warning band, not a free pass. The authoritative three-band numbers (target / soft-min / hard-floor) live in `generation-spec.md` (§Editorial Floors) and `briefing_manifest_core.py SECTION_RULES`.
- Prefer shorter but fully supported reporting over padded analysis.
- If a section cannot honestly meet the bar, use a visible `## Quality Failure: ...` block instead of filler.
- Prefer an explicit `Quality Failure` block over a low-signal local follow-up or a repetitive update with no verifiable movement.

## Mandatory Editorial Rules

- No crypto or DeFi coverage.
- No sports.
- No cars, EVs, or robotaxis.
- No entertainment or celebrity filler.
- Classical music in Japan and the watch-listed artists named in the private coverage notes are allowed despite the entertainment filter because the user explicitly requested it. Treat it as arts/culture reporting: prefer official schedules, venue notices, program changes, artist/festival sources, and substantive reviews; avoid celebrity gossip, fan posts, unsourced rumor, and promotional filler.
- **No LGBT-topic features** (obituaries, activism profiles, cultural retrospectives, identity/culture human-interest) EXCEPT when the central news hook is a U.S. Supreme Court case (treated as a legal story). Filter at curation. Carve-out scope is detailed under §Source Curation below.
- **No Wikipedia pass-through citations.** Wikipedia is fine as an orientation index and footnote miner, but never cite it as a meta-source ("Wikipedia's compilation, drawing on AP, Reuters, NBC News, Washington Post"). When Wikipedia points to AP/Reuters/NBC/etc., fetch each named outlet directly and cite *those* in the Primary Sources block. The reader's source list should contain primary publications, not "Wikipedia citing primary publications." Wikimedia Commons images remain acceptable per the image rules — this rule is about textual citations only.
- Each article cites **5+ named sources** (best-effort for breaking/local news).
- AI and tech articles should be maximally technical.
- Include source-bias assessment, points/counterpoints, and critical analysis in every article.
- Include a clearly labeled `Pros / Cons / Caveats` block or equivalent balanced assessment in every substantive article. For interim updates, explicitly label the item as `Update` when it changes a previously covered story and state what changed since the prior briefing.
- Include substantive synthesis, not article-stitching.
- Include focus-company-relevant coverage every day when the news supports it.
- Required recurring watch beats: world news, US news, focus-company news, AI news, non-AI technology news, Japan national news, the home-region beats and watch-listed people named in the private coverage notes, and classical music in Japan. These are coverage requirements, not filler licenses: weak days should emit candidate-pool no-update records or visible quality-failure markers rather than low-signal articles.
- Use Hacker News, Reddit, X, and practitioner communities as counterpoints, not as the primary factual spine.
- Review Hacker News daily and aim to land at least 5 worthwhile HN-driven or HN-corroborated AI/tech stories. If fewer than 5 clear the bar, record that shortfall explicitly rather than silently drifting to zero.
- Put source appendices at article ends, not inline in the body.
- Human-interest and closing sections still require real source appendices; they are not exempt from sourcing discipline.

## Source Curation — Framing Discipline

Apply these at the **curation step** — when ranking candidate stories before fan-out — not at the writing step. Don't spend a research subagent on a story that will be cut. This applies to both runners and to any ad-hoc topical research.

### Filter longform advocacy prose (regardless of political direction)

Filter out pieces that read as longform advocacy journalism — prose-with-a-position written from inside one camp — **regardless of political direction**. The same real event reported by a wire service is fine; the same event told as a hero-narrative is not. (Origin: 2026-05-25, an Operation Metro Surge piece headlined *"The Neighborhood That Wouldn't Back Down"* shipped with framing like "masked agents in ballistic vests" and "the damage they left behind." The event was real and covered straight by NPR/MinnPost/Sahan Journal, but the briefing ran the advocacy version — flagged as propaganda.)

**This is a prose-style filter, NOT a source block.** Keep seeing outlets you generally disagree with — that's how assumptions get checked. The filter is on the **prose style** (argument-building), not the **outlet** (which direction it leans). An AP wire syndicated to Mother Jones is fine; a longform Atlantic editorial is not. Symmetrically, a City Journal / Federalist / Breitbart pro-enforcement hero-narrative gets filtered the same way.

Reject any piece showing **two or more** of these markers:
- **Heroic-resistance framing**: "wouldn't back down", "stood up to", "extraordinary response", "communities rallied" — casting one side as protagonists.
- **Loaded imagery used as fact**: "masked agents", "ballistic vests", "stormed", "raided" (when "arrested"/"detained" is what happened) — emotional adjectives that aren't factual claims.
- **Editorial conclusions stated as fact**: "the damage they left behind", "the wreckage of the policy" — "damage" is the writer's position, not a neutral descriptor.
- **Novelistic / literary register**: "the sirens have stopped", "a quieter crisis than the one that summoned them" — atmospheric scene-setting that signals magazine-longform over news reporting.
- **One-sided sourcing**: only one camp humanized (named families, kitchens, dialogue); the other appears only as antagonists or numbers.

When found, do ONE of: (1) substitute coverage of the same event from a wire (AP, Reuters) or a news section (NPR News, BBC, NYT News, regional desks); (2) present the underlying facts directly without the advocacy frame; or (3) if only editorial coverage exists, present it briefly and **label it explicitly** ("editorial framing — [outlet]") alongside ONE opposing-perspective piece — never solo.

### LGBT topic exclusion — scope of the carve-out

- This is a **topic-exclusion rule, not a viewpoint rule** — apply it symmetrically regardless of how the underlying piece is framed.
- The SCOTUS carve-out is **narrow**: it applies only when the central news hook is a Supreme Court proceeding (docket activity, oral arguments, opinions, cert grants/denials). A lower-court ruling, an executive-branch action, or a state-legislature bill on an LGBT topic does **not** qualify on this exception alone (though it may qualify on other policy grounds).
- Example rejected article: an obituary of an LGBT-movement figure with no SCOTUS hook (e.g. "The Chemist Who Read the Footnotes: Iris Long, ACT UP's Scientific North Star, Dies at 92").

## Default to Skepticism

Treat statements from industry leaders, executives, lab founders, and prominent commentators as **interested speech**, not neutral analysis. When a CEO, founder, or senior executive (Altman, Amodei, Pichai, Huang, Musk, Zuckerberg, etc.) issues a "warning," "prediction," or "call to action," default to three questions:

1. **Whose interests does this serve?** Calls for AI regulation from frontier labs frequently function as regulatory capture — raising compliance costs that disadvantage smaller competitors while entrenching incumbents. Warnings about AI risk from people building the riskiest systems are not disinterested expert testimony.
2. **What's the business-model angle?** "We need safety standards" often translates to "we need standards that match what our company already does." "AI will replace jobs" from an AI vendor is also a sales pitch.
3. **Is the speaker rewarded for being correct, or just for saying it loudly?** Most pundits face no accountability for being wrong.

Do NOT use phrases like "X's warning carries distinctive credibility" or "Y's perspective is uniquely informed" for industry insiders making claims that align with their commercial interests. If you cite such a statement, immediately follow it with the obvious counter-reading (regulatory capture, marketing, fundraising narrative, competitive positioning). Apply this broadly: government officials defending their own policies, analysts with positions in assets they cover, researchers funded by interested parties, activists framing data through their cause, journalists with documented sourcing relationships. When in doubt, be more skeptical, not less.

### Accelerationism / e-acc coverage — daily review

Review the e/acc / effective-accelerationism source tiers in `sources.md` daily; they feed mainly into Part II (occasionally III/V). The movement generates significant AI-industry narrative framing — apply the skepticism above *especially* aggressively here: Tier-1 voices are unusually prone to interested speech (VC fundraising, regulatory-capture framing, founder hype), and ANY Tier-1 quote must be paired with a Tier-4 counterpoint when cited.

- **Tier 1 — inside the movement** (interested speech): Beff Jezos / Guillaume Verdon (`beff.substack.com`, "Notes on e/acc principles and tenets"), Bayeslord, Marc Andreessen (`pmarca.substack.com`, a16z; "Techno-Optimist Manifesto"), Extropic AI blog.
- **Tier 2 — sympathetic analysis**: Stratechery, Marginal Revolution, Asterisk, Noahpinion, Dwarkesh Podcast, Works in Progress, Palladium.
- **Tier 3 — mainstream record**: Forbes (broke the Verdon identity story), NYT, FT tech desk, WaPo, Vox.
- **Tier 4 — essential counterpoint (consult when citing Tier 1)**: LessWrong, EA Forum, Astral Codex Ten, The New Atlantis, 404 Media.
- **Philosophical roots**: Hacker News (developer-sentiment pulse-check); Nick Land (philosophical accelerationism — note his later neo-reactionary drift; distinguish from e/acc); Srnicek & Williams ("#ACCELERATE: Manifesto for an Accelerationist Politics", the left-accelerationist counterpart both movements respond to).
- **Source-quality rules**: do NOT use unvetted X threads, crypto newsletters rebranded "accelerationist," or `r/accelerationism` as primary sources. Reddit/X are fine as *secondary* sources for direct quotes and sentiment — name the account and quote the post, don't summarize.

## Benchmark Skepticism

When reporting AI model benchmark scores (MMLU, ARC-AGI, HumanEval, OSWorld, Chatbot Arena, etc.) or leaderboard standings ("leads the Artificial Analysis Intelligence Index"), include critical context:
- **Who ran it**: first-party (the lab) vs. independent (LMSYS, Scale AI HELM, academic). First-party claims require explicit flagging.
- **Known limitations**: contamination risk, saturation, gap between benchmark and real-world task performance.
- **Ecological validity**: does it measure something that matters for actual use, or a proxy that may not generalize?
- **Replication status**: have independent evaluators confirmed the score, or is it unverified at publication?

Do NOT present scores as clean fact. Phrase as: "OpenAI *claims* a score of X on benchmark Y, which measures Z — though independent replication is pending and the benchmark has known limitations including [...]." A claim to "lead" any index reflects the specific metrics chosen and does not straightforwardly predict real-world performance.

## Factual Claim Standards

**Do NOT state quantified claims (statistics, ratios, percentages, dollar figures, body counts, funding gaps) unless you have a named, verifiable source for that specific number.** This includes:
- Aid/funding ratios — only if sourced from a published report (e.g. a UN OCHA Financial Tracking Service figure, a named NGO report with methodology).
- Casualty figures — attribute to a specific named body (WHO, OCHA, government ministry) with date.
- Economic statistics — cite the specific institution and report (IMF WEO, World Bank, OECD).
- Model benchmark scores — cite the specific paper or official release.
- Market/revenue figures — cite the analyst firm or company filing.

If you cannot find a named source for a number, either **omit it** or **rephrase as qualitative** ("the funding gap is described by OCHA as severe" rather than inventing a ratio). Fabricated statistics that sound authoritative are worse than none — they erode the briefing's credibility.

## Logical Coherence — No Analysis Filler

Every implication or "what this means" paragraph must survive a sanity check: **does this actually follow from the premises, or does it just sound like analysis?** Common failure modes:
- **Jurisdiction bleed**: asserting a legal precedent in country A constrains actors in country B under an entirely different legal system (e.g. a US ruling affecting whether Chinese labs comply with PLA requests — the PLA does not need US precedent to compel domestic firms).
- **False symmetry**: treating structurally different actors as interchangeable because they share a surface label ("AI labs" in democracies vs. authoritarian states).
- **Speculative chain-linking**: A→B→C→D presented as likely when each link is individually uncertain and the chain probability is negligible.
- **Word-count padding**: stretching a real story into implications that were inferred to fill space, not researched.
- **False constraint claims**: stating someone "can't" do something when they can but choose not to for economic/practical reasons. Name the genuine constraint (legal, physical, biological) if one exists; otherwise say it's a choice shaped by incentives. Never manufacture captivity or helplessness for effect.
- **Dramatic escalation**: "catastrophic" when the data shows "significant"; "unprecedented" when precedents exist; "existential" when the threat is real but survivable. Use the word that matches the evidence.

If an implication wouldn't survive a skeptical reader thinking about it for 10 seconds, cut it. End the article where the sourced facts end.

**Never declare "the real story":** Do NOT write "the actual story is about X," "this is really about Y," or "it is not about Z — it is about W" when the facts support multiple interpretations. Observable asymmetries, structural incentives, and power dynamics are legitimate things to *describe*, not to *declare as the definitive meaning* when key facts are missing (a party hasn't spoken, litigation is pending, motives are inferred). Present the competing interpretations, note which facts support each, and stop. This applies with special force to culture-war stories.

## Identifying Unnamed or Pseudonymous Persons

When a story involves a person whose identity is concealed or withheld (a source using a fake name, a journalist protecting identity, a company referring to "an employee," a John Doe filing), **actively investigate who the person is** — the briefing's value depends on specificity.

**Investigation method:** LinkedIn (employer, title, tenure, location, education; cross-reference employee lists); X/Twitter (employer + role keywords; replies/quote-tweets on the original story; followers of named parties); Reddit (relevant subs); court filings (PACER, state e-filing, Google Scholar); professional directories (state bar, SEC EDGAR, corporate registrations, patents, academic pubs); news archives (other outlets may have named them).

**Reporting: every identification states its confidence level in the text.** Write it in this form, next to the name: `(identification confidence: High, 3 matching attributes: <the attributes>; the briefing's own research)`.
- **High** (3+ independent attributes, from at least two different kinds of source) → name them and cite the matching attributes.
- **Moderate** (2 attributes) → write "likely [name]" with `identification confidence: Moderate`, the confidence range and which attributes match. Never in a headline, title or bottom line.
- **Below Moderate** (one attribute, or attributes that fit a common name or role) → do **not** name the person or hint at a name. Say that identification was attempted and not established, what was tried, and what would confirm.
Never fabricate identities or present guesses as findings. The validator warns when a name is given without a stated identification confidence.

**Japanese-language investigations:** use Japanese terms alongside English — LinkedIn Japan, 日本弁護士連合会, corporate registries (EDINET, 法人番号公表サイト), Japanese news archives (Chunichi, Nikkei, NHK), and platforms like Wantedly / Eight-Sansan.

**Ethics guardrail:** this serves the reader's interest in consequential stories (litigation, corporate disputes, policy decisions) where identity is materially relevant. It is NOT doxxing — do not investigate private citizens who are not parties to the story, and never publish home addresses, phone numbers, or other contact information.

## Counterpoint And Forward-Looking Blocks

- Parts I-IV require a `Counterpoint Matrix` with at least 2 real rows so the source-tensions artifact can capture disagreement rather than a token caveat.
- The matrix is a 5-column markdown table inserted after the source-bias / counterpoint paragraph:

  `| Claim | Interested Party | Structural Incentive | Tier-4 Counterpoint Source | Net Assessment |`

  Each row must name a real party and a real structural incentive. Generic rows ("sources disagree", "various analysts", "critics") are not acceptable — it is a structured audit trail, not decoration.
- Parts I-IV require a `Watch Next 48h` block (placed at the article end, before the Primary Sources block) based on verifiable schedules, filings, calendars, or official event pages — with explicit dates/times in JST (e.g. "TSMC Q1 earnings call: April 10, 2026 16:00 JST"). Fewer than 3 bullets is fine; do not fabricate watch items to fill the box.
- Canonical machine-parsed heading forms for both blocks live in `generation-spec.md` (§Literal Format Specifications).

## Image Policy

- The assembled daily briefing must contain exactly one validated inline image total. More than one embedded image in the final markdown/PDF is a validation failure.
- Use only an image you have good reason to trust as appropriate to the chosen article. If no trustworthy image validates, ship the briefing with a visible image-quality failure marker rather than a decorative fallback.
- Research should still collect candidate images for every serious article, but final assembly selects the single best evidentiary image for the full briefing.
- Do not use generic archival, decorative, or weakly matched fallback images to fill space.
- Embed the single selected image with markdown block syntax on its own line (blank line before and after): `![Factual caption with source attribution](https://full-image-url.jpg)`.
- URLs must be absolute HTTPS ending in `.jpg`, `.jpeg`, `.png`, `.webp`, or `.gif`. Prefer URLs you actually fetched during research over inferred ones (hallucinated URLs fail silently and leave holes in the PDF). Captions are factual descriptions with source attribution in parentheses — not mini-headlines or editorializing.

### Candidate image types (prefer evidentiary over decorative)

1. **Hero image** — the lead visual, from the source's `og:image` / first large inline `<img>`.
2. **Data visualizations** — charts/graphs/infographics from primary sources (FRED, Trading Economics, ACLED, SIPRI, Epoch AI, Artificial Analysis). Highest information density; actively seek these.
3. **Maps** — conflict/territorial/trade-route/infrastructure maps (Liveuamap, OCHA, ACLED). At least one map expected for geopolitics/conflict articles.
4. **Satellite imagery** — OSINT (Maxar News Bureau, NASA Earth Observatory, Copernicus/Sentinel, NASA FIRMS). High-value for conflict/disaster/environment.
5. **People** — portraits from Wikimedia Commons (public-domain / CC).
6. **Organizational logos / building photos** — Wikimedia Commons or official press kits.
7. **Document screenshots** — a report/filing/legislation cover or key chart from the publisher.
8. **Tech product images** — product screenshots, architecture diagrams, benchmark tables (lab blogs, arxiv figures).

### Acceptable vs forbidden sources

- **Acceptable**: article hero images (`og:image`/inline), Wikimedia Commons, official press kits / government reports / institutional sites, data-viz platforms (FRED, Trading Economics, Our World in Data), OSINT platforms (Maxar, NASA EO, FIRMS, Global Forest Watch), lab-blog / arxiv figures, UN/OCHA/WHO maps and infographics.
- **Forbidden**: fabricated URLs, stock photos, clipart, AI-generated imagery, tracking-pixel 1×1 gifs, Getty/AP/Reuters watermarked previews without the original hosting URL, paywalled-source URLs that need auth to resolve.
- Empty-article fallback insertion is off by default. Approved automatic origins are limited to: a direct authored inline image that validates, source `og:image`/hero, or an exact entity-match Wikimedia Commons image. Disabled fallbacks for current-affairs stories: generic archival, Library of Congress substitutes, generic stock, weak entity guesses.
- Trusted authored SVGs are encouraged for editorial graphics derived from the article (mini-charts, timelines, locator maps, scorecards). Do not use arbitrary third-party web SVGs as substitutes for real photos; photos remain raster-only.

### Single-image target

Select one image for the full briefing using this priority order: primary-source data visualization or map; official product/venue/document image; source article hero image that validates; exact entity-match Wikimedia Commons image. Do not embed additional section-level images.

### Active image hunting

During research, actively search for image candidates — don't just passively collect hero images. Economic story → FRED / Trading Economics chart; conflict → Liveuamap / OCHA / Maxar map or satellite; person → Wikimedia Commons portrait; tech release → lab-blog architecture/benchmark figures; disaster/environment → NASA Earth Observatory / FIRMS. If no candidate validates, run the hydration/fetch path before assembly so the shared auto-hydration safety net can recover `og:image` or exact-match evidentiary visuals. Final assembly still embeds only one validated image.

## Research Discipline

- Run `generate_source_audit.py --date YYYY-MM-DD` before drafting fragments. Treat the audit as the shared 250+ source floor for both runners, not as final prose.
- When the Perplexity MCP discovery path is enabled for a run, run `perplexity_discovery.py --date YYYY-MM-DD` after the source audit and before drafting. The resulting `*-perplexity-discovery.json` and `.md` artifacts are discovery/counterpoint inputs only; final citations must point to underlying source URLs after validation, not to Perplexity's generated answer.
- The source audit syncs into a persistent source vault. The vault's Markdown notes are the durable truth; the SQLite database is a rebuildable search cache.
- The source audit also writes an adaptive-fetch report. It records public endpoint, Jina Reader, feed/RSS, user-agent, metadata, optional TLS-impersonation, and low-trust archive sidecar attempts inspired by insane-search. Treat recovered live responses as useful availability evidence, and treat archive/Wayback sidecars as provenance only.
- Every source in `sources.md` must be actively checked and recorded daily before drafting starts. The final briefing cites only the subset that materially supports selected articles, candidates, counterpoints, or no-update decisions.
- If the source audit is missing, partial, or systemically failed, stop and fix the audit before assembly or delivery.
- Keep provenance out of article prose. Perplexity, MCP, source-audit mechanics, candidate-pool reasoning, validator constraints, and source-access caveats should appear in generated run artifacts or close-out notes, not inside a reader-facing article.
- Treat generic process paragraphs as validation failures. If the prose can be reused across unrelated stories, replace it with concrete sourced facts or mark the item as a quality failure.
- Prefer primary sources, official schedules, public filings, major wires, and high-signal counterpoints.
- If the first pass is thin, do a second pass for corroboration rather than accepting weak sourcing.
- If a section stops at the adaptive minimum or remains below target, the candidate-pool artifact should show rejected alternatives or an explicit quality failure. Minimum-with-zero-rejections usually means discovery stopped too early.
- Treat the adaptive-fetch report, evidence digest, source-tensions JSON, and research-depth plan as part of the run's provenance trail. They are generated from the source audit, candidate pool, and assembled manifest and must stay consistent with the final markdown.
- The research-depth plan uses an explicit breadth/depth model: hard-news sections get depth 2, follow-up breadth is narrowed by half, and any below-target/zero-rejection section must carry concrete follow-up queries.
- The source-confidence audit assigns each checked catalog source a CRAAP-lite type and tier, then records per-article confidence, source-diversity counts, falsifiable checks, and open questions. Low confidence does not automatically block shipment, but it is a repair target before delivery.
- Post-validation fixes should be surgical patches to specific articles, source appendices, or tables. Do not regenerate the whole briefing to satisfy one lint or validation finding.

### Story-discovery sequence

1. Use web search extensively to find today's top stories across all categories — start with Tier 0 sources (Reuters, AP, Hacker News, Google News) for the day's story map.
2. For each story, find at least 5 different sources with different perspectives; `sources.md` lists the outlets most likely to carry each topic.
3. Check the Hacker News front page for top tech stories.
4. Check whether any Part II / Part III story relates to a `tech-watchlist.md` item; if a development advances, undermines, or recontextualizes a watchlist technology, note it.

### Native-language research — REQUIRED for top coverage regions

English-language editions are a filtered, delayed subset. Search in native languages (queries via web search):
- **Japanese (日本語):** 日経, 朝日, 中日新聞, Yahoo!ニュース. The regional primary sources are named in the private coverage notes.
- **Chinese (中文):** 财新, 36氪, 新华网, 微博. e.g. `中国半导体 2026政策`
- **Farsi (فارسی):** IRNA, Tasnim, Fars News. Iran is the briefing's #1 country by mention count — Farsi reveals internal regime messaging invisible in English. e.g. `تنگه هرمز توافق آتش‌بس`
- **Arabic (العربية):** Al Jazeera Arabic, Al Arabiya, Al Manar (Hezbollah). e.g. `لبنان غارات إسرائيلية ضحايا`
- **Hebrew (עברית):** Haaretz Hebrew, Ynet, Calcalist — more candid than the English editions. e.g. `מבצע חושך נצחי לבנון`
- **Korean (한국어):** Yonhap Korean, Chosun Ilbo, Hankyoreh. e.g. `북한 미사일 화성 클러스터`
- **Other as relevant:** Dawn (Pakistan/Urdu), The Hindu (India), Meduza (Russia), Handelsblatt (Germany).

State media (IRNA, Xinhua, KCNA, Al Manar) is propaganda — but what a state chooses to propagandize reveals its priorities. Report what they say, attribute clearly, apply maximum skepticism.

- Treat executives, founders, officials, and advocates as interested speakers (see §Default to Skepticism).
- Attribute benchmark claims, economic figures, casualty counts, and schedule claims to named sources (see §Factual Claim Standards, §Benchmark Skepticism).
- Avoid logical filler, speculative chain-linking, and dramatic overstatement (see §Logical Coherence).

## Updates Discipline

- Read the prior day's assembled briefing only for context, title novelty, and update selection.
- Do not reuse prior-briefing prose as current draft text.
- Skip Updates entirely when nothing materially moved.
- In-between-day news alerts must label the item as `Update` when it follows a previously covered story and must include a concise `What changed` explanation. If the item is new rather than an update, say so explicitly.

## Workflow Gates

Run the active runner's scripts in this order:

1. `generate_source_audit.py` before drafting fragments
2. `perplexity_discovery.py` when using the Perplexity MCP discovery path
3. `fetch_briefing_images.py`
4. `assemble_briefing.py`
5. `validate_briefing.py`
6. `build_pdf.py`
7. `visual_qa.py`
7. delivery hook only if explicitly requested or the runner is configured to deliver

Guardrails:
- Delivery must be blocked unless validation passed or the runner uses an explicit degraded-delivery override that is written into the run report.
- The run report should represent the latest attempt truthfully: latest attempt, latest valid run, and latest delivery are distinct states.
- Cross-runner comparison is a QA artifact and regression gate, not the shipping target. Consensus output is diagnostic-only.

## Done Means

- Required fragments exist.
- Assembly succeeded.
- Validation status is explicit.
- Run report is written.
- Candidate-pool artifact is written.
- Adaptive-fetch, evidence digest, source-tensions, research-lint, source-confidence, research-depth, and research-vault index artifacts are written.
- PDF exists and is materially non-trivial.
- Delivery, if attempted, is recorded explicitly as success, override, or failure.

