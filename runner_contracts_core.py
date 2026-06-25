from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


SECTIONS = (
    "header",
    "part1",
    "part2",
    "part3",
    "part4",
    "humaninterest",
    "updates",
    "closing",
)

_ENV_ROOT = "DAILY_BRIEFING_ROOT"
_ENV_STEM = "DAILY_BRIEFING_STEM"
_ENV_LABEL = "DAILY_BRIEFING_LABEL"
_ENV_AUTHOR = "DAILY_BRIEFING_AUTHOR"
_ENV_PREPARED_BY = "DAILY_BRIEFING_PREPARED_BY"
_ENV_RUNNER = "DAILY_BRIEFING_RUNNER"
_ENV_RESEARCH_VAULT = "DAILY_BRIEFING_RESEARCH_VAULT"


@dataclass(frozen=True)
class RunnerProfile:
    runner_name: str
    briefing_root: Path
    artifact_stem: str
    briefing_label: str
    briefing_author: str
    prepared_by_line: str
    image_root_mode: str = "flat"
    feature_flags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StaticRunnerBackend:
    runner_name: str
    cli_name: str
    supports_research_drafting: bool = True
    supports_delivery: bool = False
    preflight_checks: tuple[str, ...] = ()

    def capability_snapshot(self) -> dict[str, Any]:
        return {
            "runner_name": self.runner_name,
            "cli_name": self.cli_name,
            "supports_research_drafting": self.supports_research_drafting,
            "supports_delivery": self.supports_delivery,
            "preflight_checks": list(self.preflight_checks),
        }


@dataclass(frozen=True)
class RunnerContext:
    profile: RunnerProfile
    backend: StaticRunnerBackend

    @property
    def archive_dir(self) -> Path:
        return self.profile.briefing_root / "archive"

    @property
    def generated_dir(self) -> Path:
        return self.profile.briefing_root / "generated"

    @property
    def image_root(self) -> Path:
        root = self.generated_dir / "briefing-images"
        if self.profile.image_root_mode == "stemmed":
            return root / self.profile.artifact_stem
        return root

    def fragment_path(self, section: str, date: str) -> Path:
        return self.archive_dir / f"{self.profile.artifact_stem}-{section}-{date}.md"

    def fragment_manifest_path(self, section: str, date: str) -> Path:
        return self.archive_dir / f"{self.profile.artifact_stem}-{section}-{date}.json"

    def assembled_markdown_path(self, date: str) -> Path:
        return self.archive_dir / f"{self.profile.artifact_stem}-{date}.md"

    def assembled_manifest_path(self, date: str) -> Path:
        return self.archive_dir / f"{self.profile.artifact_stem}-{date}.json"

    def pdf_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}.pdf"

    def validation_report_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-validation.json"

    def run_report_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-run.json"

    def candidate_pool_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-candidate-pool.json"

    def source_audit_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-source-audit.json"

    def source_audit_markdown_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-source-audit.md"

    def adaptive_fetch_report_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-adaptive-fetch.json"

    def evidence_digest_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-evidence-digest.md"

    def source_tensions_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-source-tensions.json"

    def research_lint_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-research-lint.json"

    def source_confidence_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-source-confidence.json"

    def research_depth_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-research-depth.json"

    def research_depth_markdown_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-research-depth.md"

    def perplexity_discovery_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-perplexity-discovery.json"

    def perplexity_discovery_markdown_path(self, date: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-perplexity-discovery.md"

    def research_vault_dir(self) -> Path:
        override = os.environ.get(_ENV_RESEARCH_VAULT)
        if override:
            return Path(override)
        if self.profile.briefing_root.name.lower() == "codex":
            return self.profile.briefing_root.parent / "research-vault"
        return self.profile.briefing_root / "research-vault"

    def research_vault_index_path(self) -> Path:
        return self.research_vault_dir() / "source-index.json"

    def health_dashboard_path(self) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-health.json"

    def visual_audit_path(self, date: str, pdf_sha256: str) -> Path:
        return self.generated_dir / f"{self.profile.artifact_stem}-{date}-visual-audit-{pdf_sha256[:8]}.md"

    def delivery_attachment_name(self, date: str) -> str:
        return f"{self.profile.artifact_stem}-{date}.pdf"

    def image_cache_dir(self, date: str) -> Path:
        return self.image_root / date

    def ensure_layout(self) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.image_root.mkdir(parents=True, exist_ok=True)
        self.research_vault_dir().mkdir(parents=True, exist_ok=True)

    def cleanup_generated_outputs(
        self,
        *,
        now: datetime | None = None,
        ttl_days: int = 3,
        time_basis: str = "created",
        archive_pdfs: bool = True,
        pdf_archive_prune_days: int = 365,
        scratch_ttl_days: int = 7,
    ) -> list[Path]:
        """Prune ephemeral generated outputs.

        `archive_pdfs` (default True): before a PDF under `generated/` is deleted,
        gzip-copy it to `archive/pdfs/{YYYY-MM}/{name}.pdf.gz`. Pass False to
        restore the legacy "delete everything" behavior.

        `pdf_archive_prune_days` (default 365): PDFs in the archive tree older
        than this are deleted outright.

        `scratch_ttl_days` (default 7): files under `generated/scratch/` follow
        a separate retention window — 7 days (vs 3 for the rest). The scratch
        directory accumulates recovery scripts from failed runs; those are only
        useful for post-mortem review of the past week's incidents.
        """
        import gzip
        import shutil

        self.ensure_layout()
        now_dt = now or datetime.now()
        cutoff = now_dt - timedelta(days=ttl_days)
        scratch_cutoff = now_dt - timedelta(days=scratch_ttl_days)
        archive_prune_cutoff = now_dt - timedelta(days=pdf_archive_prune_days)
        deleted: list[Path] = []

        pdf_archive_root = self.profile.briefing_root / "archive" / "pdfs"

        scratch_dir = self.generated_dir / "scratch"

        for path in self.generated_dir.rglob("*"):
            if not path.is_file():
                continue
            # scratch/ has its own retention
            try:
                path.relative_to(scratch_dir)
                in_scratch = True
            except ValueError:
                in_scratch = False
            effective_cutoff = scratch_cutoff if in_scratch else cutoff
            if _path_timestamp(path, time_basis) > effective_cutoff:
                continue

            if archive_pdfs and path.suffix.lower() == ".pdf" and not in_scratch:
                try:
                    ts = datetime.fromtimestamp(path.stat().st_mtime)
                    month_dir = pdf_archive_root / ts.strftime("%Y-%m")
                    month_dir.mkdir(parents=True, exist_ok=True)
                    archive_path = month_dir / (path.name + ".gz")
                    if not archive_path.exists():
                        with path.open("rb") as src, gzip.open(archive_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                except OSError:
                    pass  # gzip failed; proceed to delete the original regardless

            path.unlink(missing_ok=True)
            deleted.append(path)

        # Prune archived PDFs older than the archive retention window
        if pdf_archive_root.exists():
            for archived in pdf_archive_root.rglob("*.pdf.gz"):
                try:
                    ts = datetime.fromtimestamp(archived.stat().st_mtime)
                    if ts <= archive_prune_cutoff:
                        archived.unlink(missing_ok=True)
                        deleted.append(archived)
                except OSError:
                    continue

        for directory in sorted(
            (path for path in self.generated_dir.rglob("*") if path.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                continue

        return deleted


def resolve_profile(profile: RunnerProfile) -> RunnerProfile:
    return RunnerProfile(
        runner_name=os.environ.get(_ENV_RUNNER, profile.runner_name),
        briefing_root=Path(os.environ.get(_ENV_ROOT, str(profile.briefing_root))),
        artifact_stem=os.environ.get(_ENV_STEM, profile.artifact_stem),
        briefing_label=os.environ.get(_ENV_LABEL, profile.briefing_label),
        briefing_author=os.environ.get(_ENV_AUTHOR, profile.briefing_author),
        prepared_by_line=os.environ.get(_ENV_PREPARED_BY, profile.prepared_by_line),
        image_root_mode=profile.image_root_mode,
        feature_flags=dict(profile.feature_flags),
    )


def build_runner_context(profile: RunnerProfile, backend: StaticRunnerBackend) -> RunnerContext:
    return RunnerContext(profile=resolve_profile(profile), backend=backend)


def _path_timestamp(path: Path, time_basis: str) -> datetime:
    stat = path.stat()
    if time_basis == "created":
        stamp = getattr(stat, "st_birthtime", None)
        if stamp is None:
            stamp = stat.st_ctime
    elif time_basis == "modified":
        stamp = stat.st_mtime
    else:
        raise ValueError(f"Unsupported time_basis: {time_basis}")
    return datetime.fromtimestamp(stamp)


__all__ = [
    "SECTIONS",
    "RunnerProfile",
    "StaticRunnerBackend",
    "RunnerContext",
    "build_runner_context",
    "resolve_profile",
]
