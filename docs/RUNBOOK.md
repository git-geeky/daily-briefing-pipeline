# Briefing pipeline v2: runbook

Two editions run through one engine: **daily** (The Daily Intelligence Briefing) and **animal** (The Animal Science Briefing, written for an expert reader). This document is for whoever restarts and operates it.

## 1. What changed and why

The old runner was one scheduled Claude session (Opus, maximum effort, `ULTRATHINK`) that read the whole spec, spawned subagents, re-typed every fragment and then ran assembly, PDF and delivery itself. It stopped producing briefings because:

| Failure seen in the logs | Why it was fatal |
|---|---|
| `You've hit your org's monthly usage limit`, `monthly spend limit`, `session limit` | One enormous session burned the allowance; nothing recorded where the tokens went |
| Destructive in-place fragment truncation during assembly | A single bad selection erased drafted work |
| Gate flag off, task disabled | The task logged "skipped" and **exited 0**, so no alert ever fired |
| Cyber-content classifier false positive; unattended `AskUserQuestion` | Runs stalled or died with no retry path |
| Validator forced content edits | Quality rules rewarded padding |

v2 replaces the model-as-orchestrator with a program (`conductor_core.py`) that calls the model only for narrow jobs, saves every result to disk, checks the account's real usage before and during the run, and finishes deterministically.

## 2. Pipeline (per edition)

```
preflight (limit probe)  ->  source audit  ->  ingest (headline harvest)  ->  slate (Opus + search)
   -> photos (animal)  ->  per article: dossier (script fetch) -> Sonnet draft -> GBIF name/status check
      -> provenance gate -> repair rounds   (parallel x2)
   -> digest (animal; Sonnet, no tools, from script-fetched pages)  ->  header (Sonnet, deterministic fallback)
   -> closing (daily; visible fallback)
   -> assemble -> validate (drops offending articles if enough remain) -> PDF -> visual QA
   -> advisory review (animal) -> delivery (aiserver) -> receipt
```

Every stage writes its result and is skipped on re-run, so a deferred or killed run **resumes** where it stopped. Model calls are `claude -p --output-format stream-json --safe-mode --permission-prompts none`, so no prompt can hang the run and the user's CLAUDE.md, hooks and MCP servers do not bloat context.

Script first, subscription second. Everything an HTTP client can do is done by script before or after a model call: the source audit and headline harvest, photograph search and licence checks, fetching each story's source pages and paper records (Europe PMC, Crossref), GBIF accepted-name and IUCN-category lookups, and the number, quotation, DOI and URL checks. The model writes. Everything the model writes defaults to Sonnet. The one exception is the slate (one call a day, the point where a poor choice spoils the whole briefing), which runs on Opus; Opus is also the one-shot fallback when a call is refused. The Fable adversarial review is off (`review_enabled`). The dossier needs the network from the machine running the conductor; where a page cannot be fetched (bot-blocked, script-rendered) the writer is told so and opens it with `WebFetch`.

**One combined briefing.** The animal edition builds its PDF and email text as usual but does not email them (`conductor.delivery.standalone: false`). The daily edition, which starts later, puts the animal PDF in front of its own behind a one-page index with bookmarks (`<stem>-<date>-combined.pdf`), prepends the animal summary to its email, and sends that single message to the daily recipients. If the animal PDF is missing or unreadable the daily briefing goes out alone, says so in its editor's notes, and writes `<daily stem>-<date>-combined.json` with `included: false`; if the animal edition finishes afterwards it then sends itself on its own, marked "(late)", to the animal recipients. Set `BRIEFING_FORCE_STANDALONE=1` to make the animal edition always email itself (for example while testing it by itself).

Guarantees worth knowing:

* **Never silent.** Disabled, deferred past the deadline, needs-a-human (login, org limit, spend limit) and hard failures all write `generated\alerts\ALERT-*.txt` and, when `BRIEFING_ALERT_COMMAND` is set, email it. The watchdog task raises an alert if there is nothing to ship.
* **Honest depth.** Articles aim for 3,500 words (animal) only when distinct material exists; the three-band floors block below 1,500. A story that cannot honestly reach the floor is replaced from the backup list, never padded.
* **Quality over count (animal edition).** *Unread sources do not factor in*: a source the writer could not open is not cited, not listed and not mentioned (no "the paper was not read"); if the sources it did read are strong, the article is written from them. *A Low-confidence article is never written*: when the Confidence line says Low (including "Low to medium"), the draft is dropped before any repair is paid for (`conductor.skip_low_confidence`) and the story is replaced. Replacement order: solid animal-science reserves, then **Human Interest** stories (people and animals; a separate prompt; at most three; only ever used to fill a gap; no heading when none are needed), then thin reserves. A Human Interest candidate must be about what people did with or for animals: prizes, awards, appointments, grants, funding, training courses, workshops, ceremonies and launches, and any story the slate calls thin, are dropped before anything is written (`conductor.fill_blocklist`). The slate is asked for eight reserves as solid as the first ten, because about four in ten stories end in a skip. A single repair round also removes sentences about sources the writer could not open, and the header never mentions what could not be read. If nothing good is left the briefing is simply shorter (animal minimum is now 3; below that the animal part is not sent). The thresholds are in `editions/animal.json`.
* **Sources that cannot be reached are tracked, then retired or replaced** (`source_health_core.py`; state `research-vault/source-health.json`, daily report `generated/*-source-health.md`). A failing catalog source goes *under watch* with the dates and kinds of failure and is followed until it answers again (then forgotten). It is **retired**, meaning no longer fetched or counted against the audit, when the failure is permanent in kind: hard paywall or login wall (HTTP 402, or a login wall with no headlines) on 3 separate days; page gone (404/410, name no longer resolves) on 5 days over a week; refuses automated readers (403/429/challenge) on 14 days over three weeks; anything else (timeouts, 5xx, empty pages) on 10 days over two weeks. Only distinct audit days count, and a day on which more than 40% of all sources failed counts for nothing (a bad network day or a sandbox's blocked IP range retires nothing). A retired source's role is covered by other catalog sources of the same bucket, language and region (`substitutes`); where there are none it is removed and listed under "no substitute" in the report. A retired source is probed again every 30 days and reinstated the first time it answers. The catalog files are never edited: retirement lives in the state file (`BRIEFING_SOURCE_HEALTH=0` switches it off).
* **Provenance gate.** Numbers, quotations, DOIs and URLs in each article are checked against what the writer actually saw (`provenance_core.py`; best-effort, see below). Invented quotations and non-resolving DOIs trigger a targeted repair; if they survive, the story is replaced. How strict it is:
  * *Evidence excludes the writer's own words.* The `Web search results for query: "..."` line and the `prompt:` a redirect notice hands back are stripped from tool results (`llm_backend_core.strip_tool_echo`), so searching for an invented quotation cannot make it "verify".
  * *Quotations are verified against page text the script fetched* (the dossier). A quotation found only in a `WebFetch` **answer** (a small model's summary of a page) is reported as `summary_only`; the daily edition tolerates two, the animal edition tolerates none (`conductor.max_summary_only_quotes`: one is enough to send the article back for a paraphrase).
  * *Figures must match as the same kind of figure*: "2.5 billion" is not supported by "2.5 million", "37%" is not supported by "page 37" (scales and percent words are understood in English, Dutch, German, French, Spanish, Portuguese and Italian). The writer's **own arithmetic** ("computed here", "by subtraction", "18/434 = 4.147%") is accepted, and reported as `derived_numbers`, when the sentence says so and the result reproduces from two figures that are in the sources and in the article's text beside it; without the calculation wording, or without both operands in the sources, a figure stays unverified. Specimen and accession identifiers are not figures. A short article of invented figures (three or more, more than half of those checked) needs repair; the reader is told (editor's note "figures not fully verified") when three or more figures, a fifth of them, or any summary-only quotation remain unverified.
  * *Translations*: a quotation labelled as the writer's translation is exempt only when the original wording is quoted beside it and found in the evidence (at most three per article). Quotations in `"..."`, `“...”`, `«...»`, `„...“`, `「...」` and `'...'` are all checked; ellipsis pieces must occur in order.
  * *DOIs* are checked wherever they appear in the article (existence in Crossref; title comparison for Primary Sources entries). The header and the closing analysis are checked against the day's articles (an invented quotation, or figures found in no article, rejects the header or sends the closing back for a rewrite).
  * *Not a fact-checker.* It proves a figure or quotation appears in something the writer was given; it does not prove the source is right, that a figure is attributed to the right subject, or that nothing was omitted.
* **Degrade, don't die.** With at least the minimum number of articles (animal 6, daily 12) the run ships a smaller briefing and says so in the email; sections below their minimum carry a visible `Quality Failure` block.
* **Idempotent delivery.** `deliver_core.py` writes one receipt per runner, edition and date (not per PDF hash: a resumed run rebuilds the PDF and its bytes differ), so a repetition-trigger retry, or a resume after an ambiguous send, never double-sends. `--force` re-sends deliberately.
* **Paid work survives.** A drafted article is saved before its repair rounds, so a limit or crash during repair resumes into the repair, not back to a fresh draft. A story that failed its checks after full repair is marked terminal and not bought again on resume.
* **A crash is an alert.** Any unexpected exception in the conductor, and any failure of the launcher itself, writes an alert file (and emails it when the alert command works) and still tries to ship what exists.
* **Outbound fetches stay on the public internet.** Every `requests` session that follows a URL chosen by a model or a web page (dossier, photos, image downloads, citation checks, PDF images) goes through `net_safety_core.py`: http(s) only, no credentials, every resolved address must be globally routable (loopback, RFC1918, link-local and metadata addresses are refused, including on each redirect hop), and the decoded body is capped (a gzip bomb cannot exhaust memory). Images are refused above 60 megapixels. A writer whose tool calls reach a non-public address has its tool results discarded, the article flagged and an alert raised (`unsafe-fetch`). Residual risk: DNS rebinding within seconds of the check; the model CLI's own `WebFetch` runs inside the CLI and is not covered beyond this after-the-fact check.
* **Fetched text is data.** Page text is placed between `<source>` fences in the prompt with every tag-like `<` neutralised, hidden elements (`display:none`, `aria-hidden`, zero font size) are dropped before the text is shown or counted as evidence, and lines that read as instructions to an AI (several languages, also when split over two lines) are removed. The removal is a best-effort filter, **not the control**: the prompts say fetched content is never instructions, the model CLI runs with `--safe-mode`, only `WebFetch` and `WebSearch`, and the environment handed to it excludes the pipeline's own settings (`BRIEFING_*`, recipients, SMTP and unrelated credentials).
* **Cost caps do not trust one field.** A run stops at `max_cost_usd_per_run` and also at `max_output_tokens_per_run` (1.5 million), which does not depend on the cost the CLI reports; a reported cost of zero beside real token counts is priced from the tokens. If the CLI stops reporting five-hour/seven-day utilisation the run continues under those caps and raises the alert `telemetry-missing` (the 80/90/92% guards cannot see anything then).
* **Quality is a trend, not a gate.** `quality-history.jsonl` (next to the run state, one line per day and edition) records articles, words, figures and quotations checked and unmatched, summary-only quotations, flagged articles, repair attempts and cost. The validators reward form; read this file weekly for drift (shrinking words, rising unmatched or summary-only counts, more repair rounds).

## 3. Where things live

| What | Where |
|---|---|
| Edition definitions (quotas, floors, required blocks, models, budget) | `editions/daily.json`, `editions/animal.json` |
| Writer/editor prompts | `prompts/<edition>/*.md` |
| Editorial contract | `references/animal-briefing-spec.md`; daily uses `briefing-spec.md` + `generation-spec.md` |
| Source catalogs | `references/sources.md`; `references/animal-sources.md` (390 sources, 37 languages) + `.meta.json` |
| Engine | `conductor_core.py`, `llm_backend_core.py`, `slate_core.py`, `photo_core.py`, `provenance_core.py`, `article_tools_core.py`, `dossier_core.py`, `taxonomy_core.py`, `pdf_merge_core.py`, `net_safety_core.py` |
| Run state, log, usage | `<root>\generated\*-conductor.json`, `*-conductor.log`, `*-run.json` (`usage` rollup by stage and model) |
| Alerts | `<root>\generated\alerts\` (files older than 30 days are pruned) |
| Quality trend | `<root>\generated\quality-history.jsonl` |
| Source health (watch list, retired sources, substitutes) | `research-vault\source-health.json`; readable report `<root>\generated\*-source-health.md`; print it with `py -3 source_health_core.py <path-to-state>` |
| Windows entry points | `windows\*.ps1` |

`<root>` is `Documents\Daily-Briefings` (daily) and `Documents\Daily-Briefings\animal` (animal).

## 4. Restart checklist (Windows desktop + aiserver)

None of the Windows or aiserver steps could be executed from the cloud session that built this; run them in order and stop at the first surprise.

1. **Get the code onto the machine.** Merge the branch, then update the working copy the launcher runs from. The 04:30 `sync-briefing-pipeline` task is disabled in the exported task list, so do it by hand and confirm:
   ```powershell
   git -C "$env:USERPROFILE\Documents\Daily-Briefings\pipeline-shared" pull --ff-only
   git -C "$env:USERPROFILE\Documents\Daily-Briefings\pipeline-shared" rev-parse --short HEAD
   py -3 -m pip install -r "$env:USERPROFILE\Documents\Daily-Briefings\pipeline-shared\requirements.txt"
   ```
   **One-off, because the history was rewritten (2026-10-28, to remove personal wording): `pull --ff-only` fails on any clone made before then ("not possible to fast-forward"; the two histories share no commits from the first rewritten one on).** In each such clone (the launcher's `pipeline-shared`, any other checkout) check that nothing local is worth keeping, then replace the checkout with the published history; after that `pull --ff-only` works as before:
   ```powershell
   $repo = "$env:USERPROFILE\Documents\Daily-Briefings\pipeline-shared"
   git -C $repo status --short          # expect nothing; local edits would be lost by the reset
   git -C $repo fetch origin
   git -C $repo reset --hard origin/main      # or origin/<the branch you run from>
   ```
   (Deleting the folder and cloning again does the same.) Never merge or pull the old history back into the new: the personal wording would return with it.
   The pip step installs `tzdata`, which Windows needs for time zones (without it the conductor falls back to fixed offsets for Asia/Tokyo and UTC only). The launcher logs the pipeline revision at the start of every run.
2. **Claude login and models.** `claude auth status` must show the account. `ANTHROPIC_DEFAULT_OPUS_MODEL` still decides what `opus` means. The animal review uses `claude-fable-5-1` with an Opus fallback; Fable needs 30-day retention enabled and refuses some biology content, which is handled (the review is advisory and skippable).
3. **aiserver.** Update `/home/agent/briefing-pipeline/deliver_core.py` to this version (adds `--edition`, `--body-text-file`, `--body-html-file`, multi-recipient, per-edition-and-date receipts, verified TLS, retries). If the existing install imports `private_config.py`, copy that file next to it; this version also runs without it (no private overlay then). The conductor always passes `--subject` and, when a recipients file exists, `--recipient`, so no `editions.animal` block is required in `delivery-config.json`; if you add one, its `recipients` are only the fallback when no recipient is passed. The app-password path stays as it is. Delivery must stay on this path (never the Gmail connector: wrong account).
4. **Recipients** (files in `%USERPROFILE%\.briefing\`, outside git; comma- or newline-separated addresses):
   * `daily-extra-recipients.txt`: **added to** the delivery config's default recipient on every combined briefing. This is where anyone else who should receive it goes. It can never drop the default recipient.
   * `animal-extra-recipients.txt`: the same, for the late stand-alone animal email.
   * `daily-recipients.txt` / `animal-recipients.txt`: **replace** the default recipient. Use them only to change who the owner is; a list that leaves the owner out removes the owner.
   Remember the combined email carries the whole daily intelligence section, so everyone on the daily list sees it.
5. **Private coverage overlay (the daily edition).** The public repository carries only generic text for what is personal to the owner: the home-region and arts beats of Part IV, the watch-listed people and company, and the regional search tips. They live in the restore file outside git, under `"edition:daily"` (`conductor.section_briefs` and `spec_notes`), and are merged over the public edition when it loads. Merge the prepared overlay file into the restore file once, **before the first daily run** (the file is kept out of the repository; fill in the company name where it says so):
   ```powershell
   py -3 "$env:USERPROFILE\Documents\Daily-Briefings\pipeline-shared\merge_private_overlay.py" "$env:USERPROFILE\Downloads\restore.daily-overlay.json" --dry-run   # key paths only, nothing written
   py -3 "$env:USERPROFILE\Documents\Daily-Briefings\pipeline-shared\merge_private_overlay.py" "$env:USERPROFILE\Downloads\restore.daily-overlay.json"
   ```
   The restore file is `%LOCALAPPDATA%\daily-briefing-pipeline\restore.local.json` (or the file named by `BRIEFING_RESTORE_FILE`); the merge keeps what is already there and saves a `.bak`. If the overlay is missing when the daily edition runs, the briefing still runs with the generic text and the alert `private-overlay-missing` is written. The personal source rows that used to be in `references/sources.md` (15 rows of the daily catalog: the regional and watch-list sources) are in **`private-sources-daily.md`**, which was handed to you with that change and is never committed. Put it beside the restore file, `%LOCALAPPDATA%\daily-briefing-pipeline\private-sources-daily.md` (or the file named by `BRIEFING_PRIVATE_CATALOG`); the source audit reads it after the public catalog, keeping the old row numbers. If the overlay is installed but the catalog is not, the alert `private-catalog-missing` is written and the audit simply leaves those rows out. The git history was rewritten on 2026-10-28 to remove the earlier public wording from every commit (the one-off reset is in step 1). What a rewrite cannot reach: commits already fetched into any clone or fork made before, and the old commits themselves, which GitHub keeps reachable by their SHA until it garbage-collects them (GitHub Support can be asked to purge them sooner). There were no pull requests or tags pinning them. A few animal-catalog rows (public zoos and an aquarium in Japan, `references/animal-sources.md`) were left as ordinary entries.
6. **Dry runs, in this order:**
   ```powershell
   .\windows\Run-Briefing.ps1 -Edition animal -WhatIf          # prints the command, writes nothing
   .\windows\Run-Briefing.ps1 -Edition animal -DryRun -NoDeliver
   .\windows\Run-Briefing.ps1 -Edition animal -NoDeliver        # real run, builds the PDF, sends nothing
   ```
   Read the PDF. Then send one for real: `.\windows\Run-Briefing.ps1 -Edition animal`.
7. **Schedule.** `.\windows\Register-BriefingTasks.ps1 -Edition animal -WhatIf`, then with `-Enable`; the same for `-Edition daily`. Defaults per edition, in the machine's local time (all overridable):

   | Edition | Start | Repeats | Conductor deadline | Watchdog (`--finish`) |
   |---|---|---|---|---|
   | animal | 06:30 | every 30 min until 11:30 | 11:30 | 11:45 |
   | daily | 12:30 | every 30 min until 17:30 | 17:30 | 17:45 |

   A third animal task, `Briefing-animal-Release` (18:00, `-Finish`), sends the animal briefing on its own, marked "(late)", if the daily briefing has shipped without it or is overdue; without it a combined-briefing failure would leave the animal briefing unsent. Every scheduled task passes `-Scheduled`: the launcher refuses a start outside the edition's window (animal 06:00-12:30, daily 12:00-18:30) and writes the alert `missed-window` instead, so a machine that was off at 06:30 and wakes at 20:00 does not start a briefing for the wrong hour.
   The animal task produces its PDF but does not email it; the 12:30 daily task sends the combined briefing. The editions are staggered so they never share one five-hour usage window. Keep each watchdog after its edition's `conductor.deadline_local`. Tasks are registered **disabled** unless `-Enable` is given, and even when enabled the first run is **tomorrow's** occurrence: the registration pins each trigger's start boundary to tomorrow. Pass `-AllowToday` only if you want today's occurrence; it is never the default. Nothing runs until you enable the tasks.

   **Burn-in (recommended for the first days):** add `-NoDeliver` to every registration (`.\windows\Register-BriefingTasks.ps1 -Edition daily -Enable -NoDeliver`, and the same for animal). Each scheduled run then builds the briefing, including the combined PDF, and sends nothing; the half-hourly repetitions exit at once once today's briefing is built. Read the PDFs under `Documents\Daily-Briefings`, then re-run the registration without `-NoDeliver` to start sending. A manual `Run-Briefing.ps1 -Edition daily` (without `-NoDeliver`) after a burn-in build sends that day's briefing.
8. **Retire duplicates.** The legacy `\ClaudeCode\Claude-DailyBriefing` task is already disabled; leave it. `briefing-runner-flags.json` currently has `use_codex_for_scheduled_tasks: true`: decide whether the Codex runner should keep sending a second daily briefing. The flags file only gates the legacy launchers, not the conductor.

## 5. Operating it

```powershell
.\windows\Run-Briefing.ps1 -Edition animal -Diagnose        # state of today's run: stages, spend, limits, last events
Get-Content "$env:USERPROFILE\Documents\Daily-Briefings\logs\last-run-animal.json"
```

Exit codes: `0` delivered (also returned when today's briefing was already delivered), `10` delivered with fewer stories or dropped articles (see the email notes), `75` deferred on a transient window limit (the next repetition resumes), `76` another run for this edition and day is active (the repetition backs off), `20` failed (alert written; this includes the organisation monthly limit, a spend limit and a login problem, which need a human), `30` disabled (alerted), `2` usage error, `21` the launcher itself failed (alert written). The launcher also alerts on any code it does not know.

Resume semantics: every stage and every finished article is saved, so launching again continues. Unfinished drafts are kept as `<story>.draft.json` next to the article and continue into repair; stories that failed after full repair (`"terminal": true` in `<story>.json`) are not retried. A run that was killed leaves a lock file; the next launch takes it over at once if its process is gone (or after 240 minutes if it is still alive but hung).

To rerun a day from scratch, delete that day's `*-conductor.json` and the `*-articles-<date>` folder under `archive`. To redo one article, delete its `<story>.json`, `<story>.draft.json` and `.md`. `-Date YYYY-MM-DD` (launcher) or `--date` (conductor) runs a past day; `-Diagnose` never touches the last-run record.

### Budget and limits

* The conductor reads five-hour and seven-day utilisation from the CLI's own `rate_limit_event`. Defaults (edit `conductor.budget` in the edition file): defer a run that *starts* above 80% (five-hour) or 90% (seven-day); stop launching new articles above 92%; cap a run at `max_cost_usd_per_run` and an article at `max_cost_usd_per_article` (API-equivalent dollars; your plan is not billed per token, but the figure is a good relative meter).
* A limit with at least the minimum number of finished articles ships a partial briefing unless the reset is within 90 minutes, in which case the run waits and completes.
* A story whose model call fails without a verdict is retried once on the next repetition and then marked terminal (a story is never bought more than twice across repetitions). Alerts are keyed by stage and reason, so one failing stage does not hide a different one later the same day.
* To spend less: lower `article_sections` targets or `max_parallel_articles`. Since 2026-10-27 the defaults are Opus for the article writers in both editions, Fable for the animal slate (Opus for the daily slate), and Sonnet for repairs, header, digest and closing, with the review off. To spend less, move `article` back to `sonnet` (about half the day's spend, shorter and less careful articles: see the writer comparison), and the animal `slate` to `opus`; for one run only, use `--set models.article.model=sonnet`. To spend more on quality, move `header` or `closing` to `opus` in `conductor.models`, or set `review_enabled: true` (Fable review, Opus fallback). `conductor.dossier.enabled: false` returns the writer to fetching its own sources.

### Measured cost and time (one live sample each, extrapolated; treat as an estimate)

Measured in the build environment against the real CLI and network on 2026-10-26:

| Stage | Measured | API-equivalent cost |
|---|---|---|
| Source audit, 537 sources (24 workers) | 346 s; 242 usable headlines from 50 sources | none (HTTP only) |
| Slate (Opus, medium, web search) | 150 s, 14.5k output tokens | $0.83 |
| One article (Sonnet, medium, fetch + search) | ~140 s, 9.5k output tokens | $0.28 |
| One repair round (Sonnet, low) | ~40 s | $0.12-0.13 |
| Header (Opus, low, no tools) | seconds | $0.07 |
| One article with the script-built dossier (Sonnet, medium; new fossil species; 3 of 4 sources read, paper record and GBIF lookup by script), including one repair round | 165 s, 2,185 words, 21 of 21 numbers verified from fetched text | $0.34 |

The dossier did **not** make an article measurably cheaper: the earlier article without it cost $0.28 plus a $0.12 repair, the dossier article $0.34 in total, which is within the noise of different stories. Its value is verification (verbatim page text, paper abstracts and funders, registry facts), not savings. The savings come from using Sonnet for header, closing and digest, a digest that needs no tools, and the Fable review being off.

**With the Opus writers (from 2026-10-27), expect about $17-20 and 70-80 minutes for an animal run and about $24-28 and 50-60 minutes for a daily run (API-equivalent, extrapolated from the writer comparison, not yet measured end to end); the five-hour window may reach 60% in one run, which is why the editions are staggered by six hours.** The figures that follow are the earlier Sonnet-writer measurements. Extrapolation for a full animal run (measured before the Sonnet defaults; the slate is still Opus, header and digest are now cheaper): about $5-7 and 35-45 minutes (audit 6 min, slate 3, photos 2, ten articles two at a time ~20, digest, header, assemble/validate/PDF ~5). A full daily run (21 stories + closing) is about $8-11 and 50-70 minutes. Your plan is not billed per token. A first complete live animal run (36 minutes, $4.90 API-equivalent, ten articles with repairs) left the account's five-hour window at 7% and the seven-day window at 9%, so the budget guard (defer at 80% / 90%) is nowhere near binding for one edition. (An earlier estimate of "about three points per article" was wrong.) The editions are still staggered by default (06:30 and 12:30) for timing and headroom, but running both in one window is not a problem at this scale. The old design spent an unknown multiple of this in a single Opus session at maximum effort, which is what hit the monthly limits.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Alert `org_limit` / `spend_limit` | Organisation monthly cap | A human must raise it at claude.ai/settings/usage; the next repetition resumes from saved state |
| Alert `auth` | Not logged in / token expired | `claude auth login` on the desktop |
| Alert `launcher-failed` / `unexpected-exit-N` | The launcher could not start the conductor (missing path, no `py`, Python crash) | Read the alert text and `logs\briefing-<edition>-*.log` |
| Alert `crash` / `fail-crash` | Unexpected exception inside the conductor | The alert carries the traceback; the run already tried to ship what existed |
| Alert `busy-past-deadline` | The watchdog found a live run past the deadline with nothing delivered | `-Diagnose`; if the process is truly hung, end it (the lock is then taken over) |
| Deferred repeatedly all day | Five-hour/seven-day window used | Lower targets or parallelism; check `-Diagnose` for the utilisation at the last call |
| `usage-policy block ... retrying on opus` in the log | Classifier false positive on a story | Automatic one-shot fallback; if it persists the story is replaced |
| Log line `photos: ... given up on upload.wikimedia.org` (or another host) | The host answered 429 and asked for more than 45 s (Wikimedia asked a cloud IP for 600 s), or two requests in a row failed | Working as designed: that host is not asked again in the run and the other providers are used. A story left without a photograph is reported in the stage's log line and in `image-report.json`. Persistent on a home connection: check the machine's network or wait; the next run asks again |
| Article ends `skipped: material_limited` | Not enough distinct source material | Working as designed; a backup story is used |
| Briefing arrives with fewer stories | Limit, repeated skips, or validation drops | Read the "Editor's notes" in the email and the conductor log |
| No email and no alert | Task not running (machine off / logged out) | The task is `InteractiveToken`; the machine must be on and the user logged in. `StartWhenAvailable` catches up after wake |

## 6. What was and was not verified

**Verified in the build environment** (cloud Linux, real `claude` CLI, real network, no email sent, no Windows desktop, no aiserver): 547 automated tests in the reference runner (67 skipped by design) and 4 in the shared-core suite, including end-to-end runs of both editions through the real assemble, validate, PDF and QA scripts against a scripted model; and **five full live runs** on 2026-10-26 (the fifth was the combined path):

| Run | What happened | Cost (API-equivalent) |
|---|---|---|
| Animal, first | Failed: 5 of 10 articles finished (minimum 6). Causes, all fixed: a short quoted phrase handed its closing quote to the next quotation (phantom "invented quote"); Dutch `200.000` did not match `200,000`; the catalog-pool minimum blocked three verified articles; the slate returned no reserves | $4.90, 36 min |
| Animal, second and resume | 10 of 10 articles, 66-page PDF with 20 licensed photographs, "Also noted" 2 items. The first PDF attempt died on a mis-nested bold/italic in one source bullet (fixed: tags balanced, plain-text fallback) | $4.21, about 37 min |
| Daily, first | Failed: 9 of 12 articles. Causes, all fixed: the heading cleanup renamed "Source bias" to "Primary Sources" (a prefix match; eleven articles); the headline harvest was filtered with the animal keyword list (6 of 156 kept) | $13.59, 27 min |
| Daily, second | 19 of 21 target articles, closing analysis, 80-page PDF, combined PDF of 148 pages with the animal briefing first | $8.43, 28 min |
| Animal, fourth (after the quality rules: no Low-confidence articles, Human Interest fill-in, source health) | 6 science articles of 2,279-3,179 words (up from 1,300-2,700), 3 Human Interest fill-ins (641-990 words), 51 pages with 10 licensed photographs. Four of the ten selected stories were dropped by the writer (three `SKIP: low_confidence`, one because its sources did not support the angle); the slate's four reserves were all thin, so three Human Interest stories filled in and all four thin reserves were tried and dropped (one could not reach the six-source minimum, one was about a different species than the paper, two were low confidence); eight of the fourteen animal stories attempted ended in a skip. Source health ran on a systemic day (305 of 537 sources failed from the sandbox) and counted nothing. Three of nine articles still carried "was not read" sentences, which led to the unread-source repair trigger | $4.47, 33 min, 128k output tokens; the account's five-hour window was 66% when it started (this session shares it) |
| Combined, fifth (the real scheduled path: animal held, daily carries it) | Animal: 8 articles (2 skipped before repair as Low confidence, 1 good article lost to a DOI regex capturing trailing markdown, since fixed), Human Interest fill-ins kept thin; daily: 20 articles, closing analysis, 76 pages; combined PDF of 143 pages with the animal briefing first and a combined email. Found and fixed: DOIs from other registration agencies (Zenodo, DataCite) called fabricated; a writer note before `SKIP:` read as a broken article; table percentages and plain comparisons ("a margin of 4.4 million") not recognised as arithmetic; the slate re-selecting a story skipped the day before (now remembered for ten days); photo search 16 min (now parallel across stories); a top-up slate call for one missing reserve returned twelve extra; the editor's note named ten of twenty daily stories by slug (now titles for the serious cases and a count for the rest); the combined index spilled two lines onto a second page (now shrinks to one); an empty "Part VI - Updates" heading (now omitted). Source health ran on systemic days (animal 57%, daily 66% failing from the sandbox) and counted nothing | $7.42 animal (50 min, 230k output tokens) + $8.15 daily (about 25 min, 212k output tokens); the five-hour window read 17% afterwards |

**Quiet days in the daily edition.** Updates may be empty by design ("empty is a valid answer"). An empty section the edition lets disappear (`omit_when_empty`) shows no heading, counts only what it holds towards the target (`effective_target`), and so no longer turns a day into exit code 10 with the note "20 of 21 target stories were finished".

**Hacker News in the daily harvest.** The daily edition reads the Hacker News front page (one request to the Algolia mirror, stories with at least 100 points) after the source audit: a story the harvest already holds is annotated with its thread and points, a new one becomes a candidate whose URL is the linked article and whose label carries the thread. The slate prompt asks for at least five worthwhile Hacker News-driven or corroborated AI/tech stories, with the thread in `sources` (role `community`), and for a `hacker_news` entry in `no_update_decisions` when fewer clear the bar; the validator then reports the recorded shortfall instead of the generic warning. If the front page cannot be read the run says so and goes on.

**Unread sources in the daily edition (decided after the fifth run).** The daily writer prompt itself told writers to state access limits ("the preprint returned HTTP 429 and was not read"), which produced 39 such sentences across 20 articles, against the spec's own rule that source-access caveats stay out of reader-facing prose. The prompt now says a source that could not be opened does not factor in and is not mentioned; the one exception is a specific figure or claim known only second-hand, which is attributed to the outlet that reported it with one short caveat in the claim's own sentence. The conductor sends sentences about access back for one repair round (`strip_unread_mentions`), with wording that keeps such a caveat (`unread_mentions_keep_load_bearing`). Unlike the animal edition, a weakly supported daily story is not skipped for low confidence: the daily is a news product with fixed sections, and a skip would leave it short of its targets.

After both editions the account's five-hour window read 27% and the seven-day window 12%. A re-finish of the daily briefing then exposed that the deterministic fallback header produced about 1,100 words for a 600-800 word summary (fixed and tested), so a failed model header can no longer stop the briefing.

Findings that shape expectations:
* The cloud sandbox is a datacenter IP: many publishers (bioRxiv, gcrmn.net, several news sites) refuse its fetches, and the daily source audit reported 238 of 363 sources unavailable. Several animal articles therefore say a paper's full text "was not read". Run from your desktop (`WebFetch` runs on the machine that runs the CLI) the fetch success rate should be higher; this is not verified.
* Articles run 1,300 to 2,700 words, below the 3,500-word aim, because the writer stops when the material it could open runs out. A depth guide and a wider dossier budget are in place; reaching 3,500 depends on source access.
* The per-section catalog-source minimum cannot be satisfied by a writer who cannot see the catalog. It is advisory (a warning and an editor's note) in the conductor flow for both editions; the legacy validators keep it as a hard gate.

### Slate model comparison: Fable 5.1 against Opus 5.5 (2026-10-27)

Same fresh source audit and ingest per edition (animal: 245 candidates from 51 sources; daily: 156 from 21 sources plus 18 from the Hacker News front page), the previous briefing seeded as prior titles, `medium` effort, web search on, refusal fallback off so a refusal would show. Two runs per model and edition. Judged blind by a Sonnet reviewer (model names withheld, order randomised), by script (sources, domains, search-found URLs, liveness) and by how many of each slate's cited sources a script could read (the dossier). Fable did not refuse anything.

| | Animal, Opus | Animal, Fable | Daily, Opus | Daily, Fable |
|---|---|---|---|---|
| Cost of the slate call (two runs) | $1.20, $1.21 | $3.12, $3.53 | $0.91, $0.76 | $2.27, $1.60 |
| Time | 4.0, 4.5 min | 5.0, 5.6 min | 3.2, 3.4 min | 4.0, 4.4 min |
| Sources per story / distinct domains | 3.05, 3.47 / 39, 47 | 3.75, 5.14 / 59, 77 | 2.80, 3.12 / 32, 45 | 3.29, 2.91 / 45, 31 |
| Stories marked `deep` in the selected set | 2, 2 | 3, 8 | 4, 5 | 7, 5 |
| Readable sources per story (script fetch) | 2.4, 2.6 | 3.2, 3.9 | 2.5, 3.1 | 3.3, 2.9 |
| Same stories again on a second run | 8 of 10 | 4 of 10 | 15 of 21 | 17 of 21 |
| Blind verdict, run 1 / run 2 | Fable / Fable (moderate) | | Fable / Opus (moderate) | |

* **Animal: a noticeable difference in Fable's favour, consistent across both runs.** Fable searches for the primary paper (94 of the 113 cited URLs in run 2 were its own finds, against 44 of 66 for Opus), so more of its ten rest on a retrievable paper or dataset, the judge rated it less likely to lose stories to the writer's low-confidence rule, and a script could read 0.8 to 1.3 more sources per story. It costs 2.6 to 2.9 times as much ($1.9 to $2.3 more per day) and picks a different set each run. Its weaknesses: a narrower taxonomic and geographic spread (the balance rules are now enforced in code, see below).
* **Daily: no reliable difference.** Fable led in run 1 (it found the Anthropic S-1 story, the day's biggest AI item, which Opus left out of selected and reserves) and trailed in run 2 (an Updates slot filled by "Update: Anthropic's S-1 Draws 1,500 HN Comments...", which is not a development, and a Japan section that was all one region; its run-1 S-1 title also led with 1,554 comments, so the prompt now forbids Hacker News counts in titles and the next daily run shows whether that is enough). Sourcing measures reversed between runs. Opus selected four `thin` stories both times. At 2 to 2.5 times the cost this does not justify the switch.
* **Decision (applied 2026-10-27):** Fable 5.1 makes the animal slate (`conductor.models.slate` in `editions/animal.json`; a refusal is retried on Opus) and Opus keeps the daily slate. It adds $1.9 to $2.3 of usage per animal run. Two days is a small sample; every run now records `slate_model` and `slate_selected_published` in `quality-history.jsonl`, so a week of real runs shows which slate's stories actually reach publication.

Defects found by the comparison, all fixed: balance swaps could import a `thin` story to satisfy "three native-language stories" or "two outside the West" (the spec says "when material exists"); a region such as "global (Max Planck Institute...)" counted as outside the West; unresolved placeholders ("confirm study population", "DOI must be traced") reached the writers (the story is now marked `thin` and the log says so); the region field must be one place name; a daily title must not carry Hacker News points or comment counts; the article prompt now tells the writer the slate's `taxa` and the dossier's registry facts are a guess; `fontTools` was missing, so text from PDFs with embedded CFF fonts could be garbled (it is now in `requirements.txt`; run `pip install -r requirements.txt` on the desktop).

### Writer model comparison: Sonnet 5.5, Opus 5.5 and Fable 5.1 (sixth live run, 2026-10-27)

The sixth live run was the combined path (animal slate on Fable by `--set models.slate.model=claude-fable-5-1`, daily slate on Opus, Sonnet writers: the baseline). It finished with 10 animal articles (3 skipped), 20 daily articles (5 failed, 3 skipped), a 147-page combined PDF, $8.88 + $11.28. Afterwards three stories per edition (chosen to include stories Sonnet skipped or failed) were rewritten by Opus and by Fable from the same slate and the same cached source dossiers; only `models.article` changed (repairs stay on Sonnet). Three blind judges (one each of Sonnet, Opus and Fable, so no judge model is favoured) ranked the versions per story; the letters were re-randomised per story and mapped back afterwards.

| | Sonnet | Opus | Fable |
|---|---|---|---|
| Animal: stories written / 3 | 2 (one skipped as low confidence after 16 s) | 3 | 3 |
| Animal: words | 2,524 and 3,259 | 4,095, 4,074, 4,504 | 3,571, 4,462, 4,350 |
| Animal: cost per article, with repairs | $0.29, $0.45 | $1.66, $1.65, $1.06 | $3.29, $3.64, $3.06 |
| Animal: per-story rank by the three judges | last in every story it wrote | first in 2 of 3 | first in 1 of 3 |
| Daily: stories written / 3 | 1 (one failed thin sourcing, one skipped) | 3 | 3 |
| Daily: cost per article | $0.53, $0.21, $0.61 | $0.85, $0.93, $1.24 | $2.24, $2.69, $2.62 |
| Daily: per-story rank (all three judges agreed on every story) | last | second | first |

* **Sonnet is clearly the weakest writer**: shorter (the aim is 3,500 words on animal), more first-person and process talk, thinner literature sections, and it gave up quickly on stories the others wrote at Medium confidence. Opus and Fable both wrote every story Sonnet failed or skipped.
* **Animal: Opus and Fable are close**; no consistent gap, Fable costs about 2.2 times as much. **Daily: Fable led on all three stories**, at about 2.5 times Opus.
* **Re-asking Sonnet does not rescue a quick skip** (second attempt: 24 s, SKIP again). Re-asking with Opus did for the slate's top animal story ($1.55 with repairs) and not for a second (written, then failed the gate). `recheck_quick_skip` and `recheck_model` implement this (default off).
* **Estimated daily cost by writer** (API-equivalent, from the per-article figures above): Sonnet about $20 (what the run cost), Opus about $40 to $42, Fable about $85. Opus articles take roughly 1.5 to 2 times as long: expect the animal run near 70 to 80 minutes and the daily near 50 to 60.
* **Decision (applied 2026-10-27):** Opus writes the articles in both editions (`conductor.models.article`), Sonnet still repairs, writes the header and the closing analysis; Fable writers only for lead stories if it is ever wanted. It doubles the day's spend (see the estimate above).
* **Caveats**: three stories per edition; sources seen only through the judges' text; the judges slipped on facts now and then (every claim used here was one several judges agreed on). Stronger writers quote more from web-page summaries (Fable up to 28 summary-only quotations in one article), which the gate flags but ships.

Other defects found in the run and fixed: a table's worth of unverified figures (17 of 94) shipped without a repair (`needs_repair` now triggers at 8 or more unmatched with a ratio above 0.12); the repair finding "thin sourcing requires a second corroboration pass" gave the writer nothing to act on and five daily articles failed three attempts each (it now states the domain count, the source classes and what is needed); the executive summary came back 44 words over its limit and the fallback header was used (one corrective retry now); the daily edition did not enforce the impersonal voice; the photo stage took 12 minutes (its request and wait accounting is now logged to show why); `--set KEY=VALUE` lets a single run use another model or setting.

### Photo stage (debugged after the sixth live run)

The stage took 12 minutes in the sixth run. An instrumented replay of the same slate (16 stories, no model calls, from the cloud sandbox, whose shared address Wikimedia already throttles) found three causes: (1) Openverse, the lowest-ranked provider, was asked for every query of every story and timed out (about 176 s per call; 1,936 s of call time in the first replay) although Wikimedia had already supplied enough candidates; (2) a request that timed out was retried three times (3 x 40 s); (3) Wikimedia's 429 answers were waited out call by call: `upload.wikimedia.org` asked for 600 s and each image slept 3 x 45 s before failing, while three other workers kept asking the same host. Now: the providers are asked in priority order and the rest are skipped once a story has three times as many usable candidates as it needs; a timeout is retried once (20 s timeout); a `Retry-After` longer than 45 s gives the host up for the rest of the run at once, a shorter one is waited out (at most 60 s of waiting per request) and holds every other worker back from that host for as long; two failed requests in a row give a host up (a 404 does not count against it, and is no longer retried); and a story that has searched for three minutes (`photos.story_budget_seconds`, 180) stops looking and keeps the photographs it has found. There is deliberately no limit on the stage as a whole: a stage clock only stops stories that have not begun, which are the backups, and it would have cut photographs on exactly the slow runs this section is about; the per-story limit is what bounds the tail, and every story still gets its turn. Replay: 701 s, then 367 s with the first three changes, then **69 s** with all of them, the same 24 photographs, against the same throttled hosts. One story (an insect-species paper) still gets no photograph in the sandbox because every candidate was a file on a host that had given itself up; on an unthrottled connection that is not expected. Not measured: the stage from the Windows desktop.

### Quotations and Watch Next (after the sixth live run)

**Quotations.** A quotation is checked word for word against page text the script fetched. Stronger writers quote more, mostly from web-fetch summaries (a small model's answer about a page, not the page), which the gate could only flag. Now: (1) the pages an article cites (Primary Sources and Watch Next addresses not already in the dossier, at most 14, 80,000 characters each, six at a time, public addresses only) are fetched by script before the check and their text counts as page text (`verify_cited_pages`); on the twelve Opus and Fable articles of the writer comparison, 47 of the 67 quotations missing from the dossier (70%) were found word for word on the cited pages; (2) a repair is asked for only when six or more quotations rest on summaries (`max_summary_only_quotes`, was 3 daily and 1 animal, where it bought a repair Sonnet could not make); (3) whatever is still unverifiable at the end is given as paraphrase, its quotation marks removed and its words kept (`demote_unverified_quotes`), and the editor's note says how many ("N quotation(s) could not be checked word for word against a page and are given as paraphrase"). A quotation found nowhere at all (not even in a summary) still fails the article. Verified quotations stay quotations; fabricated ones cannot ship as quotations. Live check (Opus, four of the same stories, new code): of 70 quotations, 63 stayed quotations after verification on the cited pages and 7 were given as paraphrase (seabird 1 of 27, airflow 2 of 14, Type Server Protocol 4 of 12, Comey 0 of 17); no article was flagged for summary-only quotations any more, and Watch Next held an event (1 article) or an honest "No scheduled external event found" line (3), no check-ins.

**Watch Next.** A line is something that will happen outside the briefing (a hearing, vote, release, deadline, expiry, publication, season) with its date and a source. In the sixth live run 11 of 88 lines (17% of the Sonnet daily lines) were check-ins the reader is told to make ("Check the S.D.N.Y. docket ... 09:00 JST", "one month after publication, check the page"), with invented times, from every writer model. `classify_watch_entry` finds them (a line that starts with check/re-check/monitor, "re-check on/at/by", "N months after publication"); the validator blocks an article with one in the sections that require Watch Next, and the repair finding says what a line is and what to write when nothing is scheduled: the next dated external event, or only "No scheduled external event found for the next 48 hours." An honest statement that nothing is scheduled is accepted. Prompts and both specs say the same.

### Independent adversarial reviews (three, 2026-10-26)

Three reviewers with no stake in the design attacked the finished pipeline: operations over time (restarts, reboots, limits, Windows behaviour), security / privacy / cost control, and content integrity. Every claim below was reproduced by the reviewer or by the fix's test.

| Area | Found | Status |
|---|---|---|
| Delivery gate | A second pass through the last mile wiped `source_audit` from the run report, after which the Windows delivery gate refused every send for the rest of the day (critical). An incomplete audit also made the gate refuse after the conductor had already spent the run | Fixed: the audit is carried through re-entry; an incomplete audit becomes an explicit waiver the gate honours; covered by subprocess tests of the real report code. **The PowerShell side is still untested on Windows** |
| Visual QA | A one-image-per-briefing contract blocked the animal edition (an image per article) whenever it was forced to send itself | Fixed (per-article contract) |
| Restarts | A lock written before a reboot could be honoured because its PID was reused; failing stages hid each other's alerts; one story could be bought on every repetition; atomic writes raised on Windows when a reader held the file; held briefings had no release path | Fixed (boot-aware lock, alerts keyed by stage and reason, two failed passes make a story terminal, retried writes, release task) |
| SSRF and size | Every fetcher followed redirects into loopback/LAN and read unbounded bodies | Fixed (`net_safety_core.py`), plus a static screen of the slate's URLs; DNS rebinding is the stated residual |
| Evidence integrity | The writer's own search query and fetch prompt were counted as evidence; any "(translation)" label exempted a quotation; "2.5 billion" matched "2.5 million"; seven invented figures in a seven-figure article needed no repair; body DOIs, single-quoted and guillemet quotations were never checked; the header and closing were unchecked | Fixed (see Provenance gate) |
| Prompt injection | The scrub was a short English blacklist; hidden page text counted as evidence; a page could forge a `<source>` fence | Hardened, and documented as best-effort rather than the control |
| Cost | A missing or reshaped limit telemetry made the 80/90/92% guards silent no-ops; a zero reported cost blinded the run cap | Fixed (alert, token cap, estimated cost) |
| Content | A fuzzy GBIF match could "correct" a newly described species into its relative; photo captions lacked the licence link and a modification notice; ported licences lost their port; US-only public-domain tags were accepted; scheme-less links passed the body-link rule; the cruelty exclusion was English-only; prior-day memory counted days that were never sent | Fixed |
| Hygiene | Header injection through `--subject`; unbounded dependency majors; no quality trend | Fixed (sanitised headers, bounded `requirements.txt` plus `requirements-tested.txt`, `quality-history.jsonl`) |

**Raised and deliberately not changed (your decisions):**
* *The daily editorial spec instructs the writer to identify anonymous or pseudonymous people ("likely [name]" from a few public attributes).* This is the daily briefing's own editorial section, not a bug, and it carries defamation and privacy risk for a multi-recipient newsletter. **Decided:** it stays, but every identification now states a confidence level (High needs three independent public attributes, Moderate two; below that no name is given), and a validator flags an identification without one.
* *The daily edition's section briefs carry personal details about the owner (where they live and what they follow) in this public repository.* **Decided and done:** they moved to the private overlay and the private source catalog (section 4 step 5); the public edition now carries generic wording only, the briefing alerts (`private-overlay-missing`, `private-catalog-missing`) if either is not installed, a test checks the public files against hashes of the personal words (without listing them), and the git history was rewritten to match.
* *The legacy source-audit fetcher (`adaptive_fetch_core.py`) spoofs a crawler user agent, impersonates a browser's TLS and routes blocked pages through a third-party reader.* It predates this work and affects only the audit. Not changed here.
* *The daily ingest's tier and language scoring is inert* (only the animal catalog has metadata). The slate prompt describes flags the daily items do not carry. Harmless, but the daily slate is chosen with less signal than the prompt implies.

**Still not done, and worth doing:** recorded real CLI stream fixtures per CLI version (the parser tests agree with themselves), Pester tests for the PowerShell gate, and a contract test that fails when the CLI's telemetry changes shape.

**Not verified:** everything on the Windows desktop (PowerShell launchers, Task Scheduler registration, `Invoke-RemoteBash`/scp path), the aiserver deployment of the new `deliver_core.py`, delivery by email of any briefing (all live runs used `--no-deliver`), the Fable review (off by default), and the experimental Codex backend. Treat the first week as a burn-in: run with `-NoDeliver`, read the PDFs, and watch `-Diagnose`.

## 7. Maintenance

* Quarterly: re-check the animal catalog (`references/animal-sources.md`): hijacked sites (one compromised site was found during the build), new national agencies. Dead and hard-paywalled sources now retire themselves (see "Sources that cannot be reached"); what needs a person is the report's **"retired with no substitute in the catalog"** list: find a different source for each (same language and region) and add it to the catalog. Liveness evidence in the meta file dates from 2026-10-26.
* Weekly: skim the review file (`*-review.json`) and the per-article `flags` in `archive\*-articles-<date>\*.json` for repeated provenance problems.
