# Daily Briefing Pipeline

Shared Python cores, validation contracts, and reference files for producing
daily briefing artifacts.

This public repository is sanitized. Machine-specific delivery details,
recipient addresses, private paths, and secret-file locations belong in a local
restore file outside git. Copy `config/restore.example.json` to a private path
such as `%LOCALAPPDATA%\daily-briefing-pipeline\restore.local.json`, edit it for
your machine, and set `BRIEFING_RESTORE_FILE` if you use another location.

The restore file may point to a local encrypted secret file or credential-backed
path, but it must not be committed and should not contain raw passwords or API
tokens.

## Checks

```powershell
python -m compileall -q .
python -m unittest discover -s tests
```
