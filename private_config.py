"""Private local config overlay helpers.

Public config files in this repository stay sanitized. Machine-specific values
can be restored from a JSON file outside git, selected with an environment
variable. This module never prints loaded values.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


RESTORE_FILE_ENV = "BRIEFING_RESTORE_FILE"


def default_restore_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "daily-briefing-pipeline" / "restore.local.json"
    return Path.home() / ".config" / "daily-briefing-pipeline" / "restore.local.json"


def _load_restore_payload(env_var: str = RESTORE_FILE_ENV) -> dict[str, Any]:
    explicit = os.environ.get(env_var)
    path = Path(explicit).expanduser() if explicit else default_restore_path()
    if not path.exists():
        if explicit:
            raise FileNotFoundError(f"{env_var} points to a missing file")
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("restore file must be a JSON object")
    return dict(payload)


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def load_json_with_private_overlay(
    public_path: Path,
    *,
    env_var: str = RESTORE_FILE_ENV,
    overlay_key: str | None = None,
) -> dict[str, Any]:
    public_config = json.loads(public_path.read_text(encoding="utf-8"))
    if not isinstance(public_config, Mapping):
        raise ValueError(f"public config must be a JSON object: {public_path}")
    overlay = _load_restore_payload(env_var)
    if overlay_key and overlay_key in overlay:
        selected = overlay[overlay_key]
        if not isinstance(selected, Mapping):
            raise ValueError(f"restore key '{overlay_key}' must be a JSON object")
        overlay = dict(selected)
    return _deep_merge(dict(public_config), overlay)
