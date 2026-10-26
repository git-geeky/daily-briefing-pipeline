"""Edition configuration for the shared briefing cores.

An *edition* is one product that runs through the same pipeline: the long-running
``daily`` intelligence briefing and the ``animal`` science briefing are the two
shipped today. Everything that used to be hard-coded in ``briefing_manifest_core``
(section keys, quotas, word floors, banned topics, image policy, header rules) is
now data in ``editions/<edition>.json`` and is selected with ``BRIEFING_EDITION``.

Design rules:

* ``daily.json`` must reproduce the historical constants exactly. A regression
  test pins that (``tests/test_edition_core.py``).
* The module has no dependencies on ``briefing_paths`` so every core (and the
  conductor) can import it before a runner profile exists.
* Unknown keys are preserved in ``Edition.extra`` so editions can carry
  conductor/delivery settings without every consumer needing a schema change.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

EDITION_ENV = "BRIEFING_EDITION"
EDITIONS_DIR_ENV = "BRIEFING_EDITIONS_DIR"
DEFAULT_EDITION = "daily"

_REQUIRED_KEYS = ("edition_id", "label", "sections", "section_titles", "section_rules")
_RULE_DEFAULTS: dict[str, Any] = {
    "min_count": 0,
    "target_count": 0,
    "max_count": 0,
    "min_words": 0,
    "soft_min_words": 0,
    "hard_floor": 0,
    "min_sources": 0,
    "min_source_classes": 1,
    "score_floor": 0,
}


class EditionError(ValueError):
    """Raised when an edition file is missing or malformed."""


def shared_root() -> Path:
    return Path(__file__).resolve().parent


def editions_dir() -> Path:
    override = os.environ.get(EDITIONS_DIR_ENV)
    return Path(override) if override else shared_root() / "editions"


def available_editions() -> list[str]:
    directory = editions_dir()
    if not directory.exists():
        return []
    return sorted(path.stem for path in directory.glob("*.json"))


@dataclass(frozen=True)
class Edition:
    edition_id: str
    label: str
    description: str
    sections: tuple[str, ...]
    section_titles: dict[str, str]
    section_rules: dict[str, dict[str, Any]]
    hard_news_sections: frozenset[str]
    counterpoint_required_sections: frozenset[str]
    watch_required_sections: frozenset[str]
    toc_sections: tuple[str, ...]
    closing_section: str | None
    header: dict[str, Any]
    images: dict[str, Any]
    rules: dict[str, Any]
    banned_topic_labels: tuple[str, ...] | None
    extra_banned_topic_rules: tuple[dict[str, Any], ...]
    required_blocks: dict[str, tuple[dict[str, Any], ...]]
    technical_keywords: tuple[str, ...]
    technical_sections: frozenset[str]
    catalogs: tuple[str, ...]
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def all_sections(self) -> tuple[str, ...]:
        """Section keys including the header, in assembly order (== briefing_paths.SECTIONS)."""
        return ("header",) + self.sections

    @property
    def image_mode(self) -> str:
        return str(self.images.get("mode") or "single")

    def rule(self, section: str) -> dict[str, Any]:
        return self.section_rules.get(section, {})

    def get(self, dotted: str, default: Any = None) -> Any:
        """Read ``a.b.c`` from the edition's raw ``extra`` + known dicts."""
        head, _, tail = dotted.partition(".")
        container: Any = {
            "header": self.header,
            "images": self.images,
            "rules": self.rules,
            **self.extra,
        }.get(head, default)
        for part in filter(None, tail.split(".")):
            if not isinstance(container, dict) or part not in container:
                return default
            container = container[part]
        return container


def _require(payload: dict[str, Any], path: Path) -> None:
    missing = [key for key in _REQUIRED_KEYS if key not in payload]
    if missing:
        raise EditionError(f"{path.name}: missing required keys {missing}")
    sections = payload["sections"]
    if not isinstance(sections, list) or not sections or "header" in sections:
        raise EditionError(f"{path.name}: 'sections' must be a non-empty list without 'header'")
    for section in sections:
        if section not in payload["section_rules"]:
            raise EditionError(f"{path.name}: section '{section}' has no section_rules entry")
        if section not in payload["section_titles"]:
            raise EditionError(f"{path.name}: section '{section}' has no section_titles entry")
    for key in ("hard_news_sections", "counterpoint_required_sections", "watch_required_sections", "toc_sections"):
        unknown = [name for name in payload.get(key, []) if name not in sections]
        if unknown:
            raise EditionError(f"{path.name}: {key} references unknown sections {unknown}")
    for rule_name in payload.get("extra_banned_topic_rules", []):
        try:
            re.compile(rule_name["pattern"])
        except (KeyError, re.error) as exc:
            raise EditionError(f"{path.name}: bad extra_banned_topic_rules entry {rule_name!r}: {exc}") from exc


def _build(payload: dict[str, Any], path: Path) -> Edition:
    _require(payload, path)
    rules = {name: dict(rule) for name, rule in payload["section_rules"].items()}
    for rule in rules.values():
        for key, default in _RULE_DEFAULTS.items():
            rule.setdefault(key, default)
    known = {
        "edition_id", "label", "description", "sections", "section_titles", "section_rules",
        "hard_news_sections", "counterpoint_required_sections", "watch_required_sections",
        "toc_sections", "closing_section", "header", "images", "rules", "banned_topic_labels",
        "extra_banned_topic_rules", "required_blocks", "technical_keywords", "technical_sections",
        "catalogs",
    }
    sections = tuple(payload["sections"])
    labels = payload.get("banned_topic_labels")
    return Edition(
        edition_id=str(payload["edition_id"]),
        label=str(payload["label"]),
        description=str(payload.get("description", "")),
        sections=sections,
        section_titles=dict(payload["section_titles"]),
        section_rules=rules,
        hard_news_sections=frozenset(payload.get("hard_news_sections", [])),
        counterpoint_required_sections=frozenset(payload.get("counterpoint_required_sections", [])),
        watch_required_sections=frozenset(payload.get("watch_required_sections", [])),
        toc_sections=tuple(payload.get("toc_sections", [s for s in sections if s != payload.get("closing_section")])),
        closing_section=payload.get("closing_section"),
        header=dict(payload.get("header", {})),
        images=dict(payload.get("images", {"mode": "single", "max_total": 1})),
        rules=dict(payload.get("rules", {})),
        banned_topic_labels=None if labels is None else tuple(labels),
        extra_banned_topic_rules=tuple(payload.get("extra_banned_topic_rules", [])),
        required_blocks={
            section: tuple(blocks) for section, blocks in dict(payload.get("required_blocks", {})).items()
        },
        technical_keywords=tuple(payload.get("technical_keywords", [])),
        technical_sections=frozenset(payload.get("technical_sections", [])),
        catalogs=tuple(payload.get("catalogs", ["sources.md"])),
        extra={key: value for key, value in payload.items() if key not in known},
    )


@lru_cache(maxsize=8)
def _load_cached(edition_id: str, directory: str) -> Edition:
    path = Path(directory) / f"{edition_id}.json"
    if not path.exists():
        raise EditionError(
            f"Edition '{edition_id}' not found at {path}. Available: {', '.join(available_editions()) or 'none'}"
        )
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise EditionError(f"{path.name}: top-level JSON must be an object")
    try:  # personal coverage details live outside git (private_config.edition_overlay)
        import private_config

        overlay = private_config.edition_overlay(edition_id)
    except ImportError:
        overlay = {}
    if overlay:
        payload = private_config.apply_overlay(payload, overlay)
        payload["private_overlay_applied"] = True
    return _build(payload, path)


def load_edition(edition_id: str | None = None) -> Edition:
    chosen = (edition_id or os.environ.get(EDITION_ENV) or DEFAULT_EDITION).strip().lower()
    return _load_cached(chosen, str(editions_dir()))


def reload_edition_cache() -> None:
    _load_cached.cache_clear()


def current_edition() -> Edition:
    return load_edition()


__all__ = [
    "DEFAULT_EDITION",
    "EDITION_ENV",
    "Edition",
    "EditionError",
    "available_editions",
    "current_edition",
    "editions_dir",
    "load_edition",
    "reload_edition_cache",
]
