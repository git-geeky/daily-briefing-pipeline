# Daily Briefing Pipeline

Shared Python cores, validation contracts, editions, prompts and reference files for producing
long-form briefing artifacts. Two editions ship today:

* **daily** — The Daily Intelligence Briefing (seven sections plus a closing analysis).
* **animal** — The Animal Science Briefing: up to ten in-depth articles a day for an expert reader,
  licensed photographs, taxonomy/status tables, study cards, stated limits, counterpoint matrices,
  390 animal-specific sources in 37 languages. Animal-cruelty coverage is excluded.

Operating it: see [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## How it runs

`conductor_core.py` drives an edition end to end as a resumable sequence of narrow stages (limit
probe, source audit, headline ingest, slate, photos, one model call per article with repair and a
provenance gate, header, closing, assemble, validate, PDF, visual QA, delivery). It checks the
account's real rate-limit state, defers instead of burning calls at a limit, ships a smaller
briefing rather than nothing when enough articles exist, and alerts on anything that needs a
human. Editions are data (`editions/*.json`); prompts live in `prompts/<edition>/`.

```text
editions/            quotas, floors, required blocks, images, models, budget per edition
prompts/             slate / article / repair / header / closing (+ digest, review for animal)
references/          specs and source catalogs (sources.md, animal-sources.md + .meta.json)
*_core.py            shared cores loaded by the thin per-runner wrappers
conductor_core.py    the orchestrator;  llm_backend_core.py  claude -p backend
photo_core.py        licensed photographs;  provenance_core.py  number/quote/DOI/URL checks
windows/             launcher, delivery and task-registration scripts for the Windows host
reference_runner/    vendored runner wrappers so the shared test suite runs in CI
```

## Private configuration

This public repository is sanitized. Machine-specific delivery details, recipient addresses,
private paths, and secret-file locations belong in a local restore file outside git. Copy
`config/restore.example.json` to a private path such as
`%LOCALAPPDATA%\daily-briefing-pipeline\restore.local.json`, edit it for your machine, and set
`BRIEFING_RESTORE_FILE` if you use another location.

The restore file may point to a local encrypted secret file or credential-backed path, but it must
not be committed and should not contain raw passwords or API tokens. Recipients for the animal
edition are read from `%USERPROFILE%\.briefing\animal-recipients.txt` and extra recipients (added to the default, never replacing it) from `<edition>-extra-recipients.txt` in the same folder (also outside git).

## Checks

```powershell
python -m compileall -q .
python -m unittest discover -s tests
python -m unittest discover -s reference_runner/scripts/tests
```

The second suite includes end-to-end conductor runs (about four minutes); they use a scripted model
and the real assemble/validate/PDF scripts, with no network or real model calls.
