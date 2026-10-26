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
   -> photos (animal)  ->  articles (Sonnet, one call each, parallel x2; repair rounds; provenance gate)
   -> digest (animal)  ->  header (Opus, deterministic fallback)  ->  closing (daily; visible fallback)
   -> assemble -> validate (drops offending articles if enough remain) -> PDF -> visual QA
   -> advisory review (animal) -> delivery (aiserver) -> receipt
```

Every stage writes its result and is skipped on re-run, so a deferred or killed run **resumes** where it stopped. Model calls are `claude -p --output-format stream-json --safe-mode --permission-prompts none`, so no prompt can hang the run and the user's CLAUDE.md, hooks and MCP servers do not bloat context.

Guarantees worth knowing:

* **Never silent.** Disabled, deferred past the deadline, needs-a-human (login, org limit, spend limit) and hard failures all write `generated\alerts\ALERT-*.txt` and, when `BRIEFING_ALERT_COMMAND` is set, email it. The watchdog task raises an alert if there is nothing to ship.
* **Honest depth.** Articles aim for 3,500 words (animal) only when distinct material exists; the three-band floors block below 1,500. A story that cannot honestly reach the floor is replaced from the backup list, never padded.
* **Provenance gate.** Numbers, quotations, DOIs and URLs in each article are checked against what the writer actually fetched. Invented quotations and non-resolving DOIs trigger a targeted repair; if they survive, the story is replaced.
* **Degrade, don't die.** With at least the minimum number of articles (animal 6, daily 12) the run ships a smaller briefing and says so in the email; sections below their minimum carry a visible `Quality Failure` block.
* **Idempotent delivery.** `deliver_core.py` writes a receipt per PDF hash and recipient, so a repetition-trigger retry never double-sends.

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

1. **Get the code onto the machine.** Merge the branch; the existing 04:30 `sync-briefing-pipeline` task updates `Documents\Daily-Briefings\pipeline-shared` (or `git pull` it by hand).
2. **Claude login and models.** `claude auth status` must show the account. `ANTHROPIC_DEFAULT_OPUS_MODEL` still decides what `opus` means. The animal review uses `claude-fable-5-1` with an Opus fallback; Fable needs 30-day retention enabled and refuses some biology content, which is handled (the review is advisory and skippable).
3. **aiserver.** Update `/home/agent/briefing-pipeline/deliver_core.py` to this version (adds `--edition`, `--body-text-file`, `--body-html-file`, multi-recipient, receipts, verified TLS, retries). Add to its `delivery-config.json`:
   ```json
   "editions": { "animal": { "subject_template": "The Animal Science Briefing - {human_date}" } }
   ```
   The app-password path stays as it is. Delivery must stay on this path (never the Gmail connector: wrong account).
4. **Recipients.** Create `%USERPROFILE%\.briefing\animal-recipients.txt` with the owner's address and the animal-briefing recipient(s), comma- or newline-separated. Keep it out of git.
5. **Dry runs, in this order:**
   ```powershell
   .\windows\Run-Briefing.ps1 -Edition animal -WhatIf          # prints the command, writes nothing
   .\windows\Run-Briefing.ps1 -Edition animal -DryRun -NoDeliver
   .\windows\Run-Briefing.ps1 -Edition animal -NoDeliver        # real run, builds the PDF, sends nothing
   ```
   Read the PDF. Then send one for real: `.\windows\Run-Briefing.ps1 -Edition animal`.
6. **Schedule.** `.\windows\Register-BriefingTasks.ps1 -Edition animal -WhatIf`, then with `-Enable`. It registers `\Briefing\Briefing-animal` (06:30, repeating every 30 minutes for 5 hours) and `...-Watchdog` (11:45, `--finish`). Repeat for `-Edition daily`.
7. **Retire duplicates.** The legacy `\ClaudeCode\Claude-DailyBriefing` task is already disabled; leave it. `briefing-runner-flags.json` currently has `use_codex_for_scheduled_tasks: true`: decide whether the Codex runner should keep sending a second daily briefing. The flags file only gates the legacy launchers, not the conductor.

## 5. Operating it

```powershell
.\windows\Run-Briefing.ps1 -Edition animal -Diagnose        # state of today's run: stages, spend, limits, last events
Get-Content "$env:USERPROFILE\Documents\Daily-Briefings\logs\last-run-animal.json"
```

Exit codes: `0` delivered, `10` delivered with fewer stories or dropped articles (see the email notes), `75` deferred on a limit (the next repetition resumes), `20` failed (alert written), `30` disabled, `2` usage error.

To rerun a day from scratch, delete that day's `*-conductor.json` and the `*-articles-<date>` folder under `archive`. To redo one article, delete its `<story>.json` and `.md`. `--date YYYY-MM-DD` runs a past day.

### Budget and limits

* The conductor reads five-hour and seven-day utilisation from the CLI's own `rate_limit_event`. Defaults (edit `conductor.budget` in the edition file): defer a run that *starts* above 80% (five-hour) or 90% (seven-day); stop launching new articles above 92%; cap a run at `max_cost_usd_per_run` and an article at `max_cost_usd_per_article` (API-equivalent dollars; your plan is not billed per token, but the figure is a good relative meter).
* A limit with at least the minimum number of finished articles ships a partial briefing unless the reset is within 90 minutes, in which case the run waits and completes.
* To spend less: lower `article_sections` targets or `max_parallel_articles`, move `slate`/`header` from Opus to Sonnet, or set `review_enabled: false`.

### Measured cost and time (one live sample each, extrapolated; treat as an estimate)

Measured in the build environment against the real CLI and network on 2026-10-26:

| Stage | Measured | API-equivalent cost |
|---|---|---|
| Source audit, 537 sources (24 workers) | 346 s; 242 usable headlines from 50 sources | none (HTTP only) |
| Slate (Opus, medium, web search) | 150 s, 14.5k output tokens | $0.83 |
| One article (Sonnet, medium, fetch + search) | ~140 s, 9.5k output tokens | $0.28 |
| One repair round (Sonnet, low) | ~40 s | $0.12-0.13 |
| Header (Opus, low, no tools) | seconds | $0.07 |

Extrapolation for a full animal run: about $5-7 and 35-45 minutes (audit 6 min, slate 3, photos 2, ten articles two at a time ~20, digest, header, assemble/validate/PDF ~5). A full daily run (21 stories + closing) is about $8-11 and 50-70 minutes. Your plan is not billed per token, but the account's five-hour window moved by roughly three percentage points per article-sized job in the sample, so **do not run both editions in the same five-hour window**: schedule the animal edition early (06:30) and the daily at least five hours later (the old daily time, around 12:30-13:00 JST, works). The conductor's own utilisation check will defer rather than overrun, but staggering avoids the deferral. The old design spent an unknown multiple of this in a single Opus session at maximum effort, which is what hit the monthly limits.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Alert `org_limit` / `spend_limit` | Organisation monthly cap | A human must raise it at claude.ai/settings/usage; the next repetition resumes from saved state |
| Alert `auth` | Not logged in / token expired | `claude auth login` on the desktop |
| Deferred repeatedly all day | Five-hour/seven-day window used | Lower targets or parallelism; check `-Diagnose` for the utilisation at the last call |
| `usage-policy block ... retrying on opus` in the log | Classifier false positive on a story | Automatic one-shot fallback; if it persists the story is replaced |
| Article ends `skipped: material_limited` | Not enough distinct source material | Working as designed; a backup story is used |
| Briefing arrives with fewer stories | Limit, repeated skips, or validation drops | Read the "Editor's notes" in the email and the conductor log |
| No email and no alert | Task not running (machine off / logged out) | The task is `InteractiveToken`; the machine must be on and the user logged in. `StartWhenAvailable` catches up after wake |

## 6. What was and was not verified

Verified in the build environment (cloud Linux, real `claude` CLI and real network): 254 automated tests including end-to-end runs of both editions through the real assemble, validate, PDF and QA scripts against a scripted model; one **real** Sonnet article through the real validators and provenance checker (including a real repair round that caught a paraphrase inside quotation marks, and a real repair that removed first-person narration without losing content); a **real** source audit, headline ingest and Opus slate for the animal edition on the day's news; a real Opus header; the real `claude -p` stream format, usage and rate-limit fields; the live Wikimedia, iNaturalist and Openverse photo providers.

Not verified, and marked untested in the scripts: everything on the Windows desktop (PowerShell launchers, Task Scheduler registration, `Invoke-RemoteBash`/scp path), the aiserver deployment of the new `deliver_core.py`, a full real ten-article run, the real digest, daily closing and Fable review calls, any real run of the daily edition, and the experimental Codex backend. Treat the first week as a burn-in: run with `-NoDeliver`, read the PDFs, and watch `-Diagnose`.

## 7. Maintenance

* Quarterly: re-check the animal catalog (`references/animal-sources.md`): dead domains, hijacked sites (one compromised site was found during the build), new national agencies. Liveness evidence in the meta file dates from 2026-10-26.
* Weekly: skim the review file (`*-review.json`) and the per-article `flags` in `archive\*-articles-<date>\*.json` for repeated provenance problems.
