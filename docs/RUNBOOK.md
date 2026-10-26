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
* **Quality over count (animal edition).** *Unread sources do not factor in*: a source the writer could not open is not cited, not listed and not mentioned (no "the paper was not read"); if the sources it did read are strong, the article is written from them. *A Low-confidence article is never written*: when the Confidence line says Low (including "Low to medium"), the draft is dropped before any repair is paid for (`conductor.skip_low_confidence`) and the story is replaced. Replacement order: solid animal-science reserves, then **Human Interest** stories (people and animals; a separate prompt; at most three; only ever used to fill a gap; no heading when none are needed), then thin reserves. If nothing good is left the briefing is simply shorter (animal minimum is now 3; below that the animal part is not sent). The thresholds are in `editions/animal.json`.
* **Sources that cannot be reached are tracked, then retired or replaced** (`source_health_core.py`; state `research-vault/source-health.json`, daily report `generated/*-source-health.md`). A failing catalog source goes *under watch* with the dates and kinds of failure and is followed until it answers again (then forgotten). It is **retired**, meaning no longer fetched or counted against the audit, when the failure is permanent in kind: hard paywall or login wall (HTTP 402, or a login wall with no headlines) on 3 separate days; page gone (404/410, name no longer resolves) on 5 days over a week; refuses automated readers (403/429/challenge) on 14 days over three weeks; anything else (timeouts, 5xx, empty pages) on 10 days over two weeks. Only distinct audit days count, and a day on which more than 40% of all sources failed counts for nothing (a bad network day or a sandbox's blocked IP range retires nothing). A retired source's role is covered by other catalog sources of the same bucket, language and region (`substitutes`); where there are none it is removed and listed under "no substitute" in the report. A retired source is probed again every 30 days and reinstated the first time it answers. The catalog files are never edited: retirement lives in the state file (`BRIEFING_SOURCE_HEALTH=0` switches it off).
* **Provenance gate.** Numbers, quotations, DOIs and URLs in each article are checked against what the writer actually saw (`provenance_core.py`; best-effort, see below). Invented quotations and non-resolving DOIs trigger a targeted repair; if they survive, the story is replaced. How strict it is:
  * *Evidence excludes the writer's own words.* The `Web search results for query: "..."` line and the `prompt:` a redirect notice hands back are stripped from tool results (`llm_backend_core.strip_tool_echo`), so searching for an invented quotation cannot make it "verify".
  * *Quotations are verified against page text the script fetched* (the dossier). A quotation found only in a `WebFetch` **answer** (a small model's summary of a page) is reported as `summary_only`; one is tolerated and noted, three trigger a repair.
  * *Figures must match as the same kind of figure*: "2.5 billion" is not supported by "2.5 million", "37%" is not supported by "page 37" (scales and percent words are understood in English, Dutch, German, French, Spanish, Portuguese and Italian). A short article of invented figures (three or more, more than half of those checked) needs repair; the reader is told (editor's note "figures not fully verified") when three or more figures, a fifth of them, or any summary-only quotation remain unverified.
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
   The restore file is `%LOCALAPPDATA%\daily-briefing-pipeline\restore.local.json` (or the file named by `BRIEFING_RESTORE_FILE`); the merge keeps what is already there and saves a `.bak`. If the overlay is missing when the daily edition runs, the briefing still runs with the generic text and the alert `private-overlay-missing` is written. Git history of this repository still contains the earlier public wording; removing it needs a history rewrite, which has not been done.
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
* To spend less: lower `article_sections` targets or `max_parallel_articles`. The defaults are Sonnet for everything but the slate, with the review off; to spend less, move `slate` to `sonnet`; to spend more on quality, move `header` or `closing` to `opus` in `conductor.models`, or set `review_enabled: true` (Fable review, Opus fallback). `conductor.dossier.enabled: false` returns the writer to fetching its own sources.

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

Extrapolation for a full animal run (measured before the Sonnet defaults; the slate is still Opus, header and digest are now cheaper): about $5-7 and 35-45 minutes (audit 6 min, slate 3, photos 2, ten articles two at a time ~20, digest, header, assemble/validate/PDF ~5). A full daily run (21 stories + closing) is about $8-11 and 50-70 minutes. Your plan is not billed per token. A first complete live animal run (36 minutes, $4.90 API-equivalent, ten articles with repairs) left the account's five-hour window at 7% and the seven-day window at 9%, so the budget guard (defer at 80% / 90%) is nowhere near binding for one edition. (An earlier estimate of "about three points per article" was wrong.) The editions are still staggered by default (06:30 and 12:30) for timing and headroom, but running both in one window is not a problem at this scale. The old design spent an unknown multiple of this in a single Opus session at maximum effort, which is what hit the monthly limits.

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
| Article ends `skipped: material_limited` | Not enough distinct source material | Working as designed; a backup story is used |
| Briefing arrives with fewer stories | Limit, repeated skips, or validation drops | Read the "Editor's notes" in the email and the conductor log |
| No email and no alert | Task not running (machine off / logged out) | The task is `InteractiveToken`; the machine must be on and the user logged in. `StartWhenAvailable` catches up after wake |

## 6. What was and was not verified

**Verified in the build environment** (cloud Linux, real `claude` CLI, real network, no email sent, no Windows desktop, no aiserver): 469 automated tests in the reference runner (67 skipped by design) and 4 in the shared-core suite, including end-to-end runs of both editions through the real assemble, validate, PDF and QA scripts against a scripted model; and **four full live runs** (two per edition) on 2026-10-26:

| Run | What happened | Cost (API-equivalent) |
|---|---|---|
| Animal, first | Failed: 5 of 10 articles finished (minimum 6). Causes, all fixed: a short quoted phrase handed its closing quote to the next quotation (phantom "invented quote"); Dutch `200.000` did not match `200,000`; the catalog-pool minimum blocked three verified articles; the slate returned no reserves | $4.90, 36 min |
| Animal, second and resume | 10 of 10 articles, 66-page PDF with 20 licensed photographs, "Also noted" 2 items. The first PDF attempt died on a mis-nested bold/italic in one source bullet (fixed: tags balanced, plain-text fallback) | $4.21, about 37 min |
| Daily, first | Failed: 9 of 12 articles. Causes, all fixed: the heading cleanup renamed "Source bias" to "Primary Sources" (a prefix match; eleven articles); the headline harvest was filtered with the animal keyword list (6 of 156 kept) | $13.59, 27 min |
| Daily, second | 19 of 21 target articles, closing analysis, 80-page PDF, combined PDF of 148 pages with the animal briefing first | $8.43, 28 min |
| Animal, fourth (after the quality rules: no Low-confidence articles, Human Interest fill-in, source health) | 6 science articles of 2,279-3,179 words (up from 1,300-2,700), 3 Human Interest fill-ins (641-990 words), 51 pages with 10 licensed photographs. Four of the ten selected stories were dropped by the writer (three `SKIP: low_confidence`, one because its sources did not support the angle); the slate's four reserves were all thin, so three Human Interest stories filled in and three thin reserves were tried and dropped (one was about a different species than the paper, two low confidence). Source health ran on a systemic day (305 of 537 sources failed from the sandbox) and counted nothing. Three of nine articles still carried "was not read" sentences, which led to the unread-source repair trigger | $4.47, 33 min, 128k output tokens; the account's five-hour window was 66% when it started (this session shares it) |

After both editions the account's five-hour window read 27% and the seven-day window 12%. A re-finish of the daily briefing then exposed that the deterministic fallback header produced about 1,100 words for a 600-800 word summary (fixed and tested), so a failed model header can no longer stop the briefing.

Findings that shape expectations:
* The cloud sandbox is a datacenter IP: many publishers (bioRxiv, gcrmn.net, several news sites) refuse its fetches, and the daily source audit reported 238 of 363 sources unavailable. Several animal articles therefore say a paper's full text "was not read". Run from your desktop (`WebFetch` runs on the machine that runs the CLI) the fetch success rate should be higher; this is not verified.
* Articles run 1,300 to 2,700 words, below the 3,500-word aim, because the writer stops when the material it could open runs out. A depth guide and a wider dossier budget are in place; reaching 3,500 depends on source access.
* The per-section catalog-source minimum cannot be satisfied by a writer who cannot see the catalog. It is advisory (a warning and an editor's note) in the conductor flow for both editions; the legacy validators keep it as a hard gate.

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
* *The daily editorial spec instructs the writer to identify anonymous or pseudonymous people ("likely [name]" from a few public attributes).* This is the daily briefing's own editorial section, not a bug, and it carries defamation and privacy risk for a multi-recipient newsletter. It is left as written.
* *The daily edition's section briefs carry personal details about the owner (where they live and what they follow) in this public repository.* Moving them to the private overlay is a one-time edit that touches what the daily briefing covers, so it waits for you.
* *The legacy source-audit fetcher (`adaptive_fetch_core.py`) spoofs a crawler user agent, impersonates a browser's TLS and routes blocked pages through a third-party reader.* It predates this work and affects only the audit. Not changed here.
* *The daily ingest's tier and language scoring is inert* (only the animal catalog has metadata). The slate prompt describes flags the daily items do not carry. Harmless, but the daily slate is chosen with less signal than the prompt implies.

**Still not done, and worth doing:** recorded real CLI stream fixtures per CLI version (the parser tests agree with themselves), Pester tests for the PowerShell gate, and a contract test that fails when the CLI's telemetry changes shape.

**Not verified:** everything on the Windows desktop (PowerShell launchers, Task Scheduler registration, `Invoke-RemoteBash`/scp path), the aiserver deployment of the new `deliver_core.py`, delivery by email of any briefing (all live runs used `--no-deliver`), the Fable review (off by default), and the experimental Codex backend. Treat the first week as a burn-in: run with `-NoDeliver`, read the PDFs, and watch `-Diagnose`.

## 7. Maintenance

* Quarterly: re-check the animal catalog (`references/animal-sources.md`): hijacked sites (one compromised site was found during the build), new national agencies. Dead and hard-paywalled sources now retire themselves (see "Sources that cannot be reached"); what needs a person is the report's **"retired with no substitute in the catalog"** list: find a different source for each (same language and region) and add it to the catalog. Liveness evidence in the meta file dates from 2026-10-26.
* Weekly: skim the review file (`*-review.json`) and the per-article `flags` in `archive\*-articles-<date>\*.json` for repeated provenance problems.
