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
* **Provenance gate.** Numbers, quotations, DOIs and URLs in each article are checked against what the writer actually fetched. Invented quotations and non-resolving DOIs trigger a targeted repair; if they survive, the story is replaced.
* **Degrade, don't die.** With at least the minimum number of articles (animal 6, daily 12) the run ships a smaller briefing and says so in the email; sections below their minimum carry a visible `Quality Failure` block.
* **Idempotent delivery.** `deliver_core.py` writes one receipt per runner, edition and date (not per PDF hash: a resumed run rebuilds the PDF and its bytes differ), so a repetition-trigger retry, or a resume after an ambiguous send, never double-sends. `--force` re-sends deliberately.
* **Paid work survives.** A drafted article is saved before its repair rounds, so a limit or crash during repair resumes into the repair, not back to a fresh draft. A story that failed its checks after full repair is marked terminal and not bought again on resume.
* **A crash is an alert.** Any unexpected exception in the conductor, and any failure of the launcher itself, writes an alert file (and emails it when the alert command works) and still tries to ship what exists.

## 3. Where things live

| What | Where |
|---|---|
| Edition definitions (quotas, floors, required blocks, models, budget) | `editions/daily.json`, `editions/animal.json` |
| Writer/editor prompts | `prompts/<edition>/*.md` |
| Editorial contract | `references/animal-briefing-spec.md`; daily uses `briefing-spec.md` + `generation-spec.md` |
| Source catalogs | `references/sources.md`; `references/animal-sources.md` (390 sources, 37 languages) + `.meta.json` |
| Engine | `conductor_core.py`, `llm_backend_core.py`, `slate_core.py`, `photo_core.py`, `provenance_core.py`, `article_tools_core.py` |
| Run state, log, usage | `<root>\generated\*-conductor.json`, `*-conductor.log`, `*-run.json` (`usage` rollup by stage and model) |
| Alerts | `<root>\generated\alerts\` |
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
5. **Dry runs, in this order:**
   ```powershell
   .\windows\Run-Briefing.ps1 -Edition animal -WhatIf          # prints the command, writes nothing
   .\windows\Run-Briefing.ps1 -Edition animal -DryRun -NoDeliver
   .\windows\Run-Briefing.ps1 -Edition animal -NoDeliver        # real run, builds the PDF, sends nothing
   ```
   Read the PDF. Then send one for real: `.\windows\Run-Briefing.ps1 -Edition animal`.
6. **Schedule.** `.\windows\Register-BriefingTasks.ps1 -Edition animal -WhatIf`, then with `-Enable`. Defaults per edition, in the machine's local time (all overridable):

   | Edition | Start | Repeats | Conductor deadline | Watchdog (`--finish`) |
   |---|---|---|---|---|
   | animal | 06:30 | every 30 min until 11:30 | 11:30 | 11:45 |
   | daily | 12:30 | every 30 min until 17:30 | 17:30 | 17:45 |

   The animal task produces its PDF but does not email it; the 12:30 daily task sends the combined briefing. The editions are staggered so they never share one five-hour usage window. Keep each watchdog after its edition's `conductor.deadline_local`. Tasks are registered **disabled** unless `-Enable` is given, and even when enabled the first run is **tomorrow's** occurrence: the registration pins each trigger's start boundary to tomorrow. Pass `-AllowToday` only if you want today's occurrence; it is never the default. Nothing runs until you enable the tasks.
7. **Retire duplicates.** The legacy `\ClaudeCode\Claude-DailyBriefing` task is already disabled; leave it. `briefing-runner-flags.json` currently has `use_codex_for_scheduled_tasks: true`: decide whether the Codex runner should keep sending a second daily briefing. The flags file only gates the legacy launchers, not the conductor.

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

Verified in the build environment (cloud Linux, real `claude` CLI and real network): 345 automated tests (66 skipped by design: they need the legacy runner scripts) including end-to-end runs of both editions through the real assemble, validate, PDF and QA scripts against a scripted model; one **real** Sonnet article through the real validators and provenance checker (including a real repair round that caught a paraphrase inside quotation marks, and a real repair that removed first-person narration without losing content); a **real** source audit, headline ingest and Opus slate for the animal edition on the day's news; a real Opus header; the real `claude -p` stream format, usage and rate-limit fields; the live Wikimedia, iNaturalist and Openverse photo providers.

The script-built dossier and the GBIF checks are verified against fakes and unit tests only; they have not yet been run end to end against live pages in a full article. Not verified, and marked untested in the scripts: everything on the Windows desktop (PowerShell launchers, Task Scheduler registration, `Invoke-RemoteBash`/scp path), the aiserver deployment of the new `deliver_core.py`, a full real ten-article run, the real digest, daily closing and Fable review calls, any real run of the daily edition, and the experimental Codex backend. Treat the first week as a burn-in: run with `-NoDeliver`, read the PDFs, and watch `-Diagnose`.

## 7. Maintenance

* Quarterly: re-check the animal catalog (`references/animal-sources.md`): dead domains, hijacked sites (one compromised site was found during the build), new national agencies. Liveness evidence in the meta file dates from 2026-10-26.
* Weekly: skim the review file (`*-review.json`) and the per-article `flags` in `archive\*-articles-<date>\*.json` for repeated provenance problems.
