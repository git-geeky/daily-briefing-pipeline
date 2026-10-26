from __future__ import annotations

from pathlib import Path

from briefing_shared_loader import load_shared_module


_shared = load_shared_module("runner_contracts_core.py", __file__)
SECTIONS = _shared.SECTIONS
RunnerProfile = _shared.RunnerProfile
StaticRunnerBackend = _shared.StaticRunnerBackend
build_runner_context = _shared.build_runner_context

_DEFAULT_PROFILE = RunnerProfile(
    runner_name="claude",
    briefing_root=Path.home() / "Documents" / "Daily-Briefings",
    artifact_stem="claude-daily-briefing",
    briefing_label="The Daily Intelligence Briefing",
    briefing_author="Intelligence Report",
    prepared_by_line="Prepared by Claude",
    image_root_mode="flat",
)
RUNNER_BACKEND = StaticRunnerBackend(
    runner_name="claude",
    cli_name="claude",
)


def runner_context():
    return build_runner_context(_DEFAULT_PROFILE, RUNNER_BACKEND)


RUNNER_PROFILE = runner_context().profile


def briefing_root() -> Path:
    return runner_context().profile.briefing_root


def artifact_stem() -> str:
    return runner_context().profile.artifact_stem


# These three must come from the *resolved* profile: the default profile ignores the edition
# (animal) and the DAILY_BRIEFING_LABEL/AUTHOR overrides, which put "The Daily Intelligence
# Briefing" in the animal PDF's page headers.
def briefing_label() -> str:
    return runner_context().profile.briefing_label


def briefing_author() -> str:
    return runner_context().profile.briefing_author


def prepared_by_line() -> str:
    return runner_context().profile.prepared_by_line


def runner_name() -> str:
    return runner_context().profile.runner_name


def archive_dir() -> Path:
    return runner_context().archive_dir


def generated_dir() -> Path:
    return runner_context().generated_dir


def image_root() -> Path:
    return runner_context().image_root


def fragment_path(section: str, date: str) -> Path:
    return runner_context().fragment_path(section, date)


def fragment_manifest_path(section: str, date: str) -> Path:
    return runner_context().fragment_manifest_path(section, date)


def assembled_markdown_path(date: str) -> Path:
    return runner_context().assembled_markdown_path(date)


def assembled_manifest_path(date: str) -> Path:
    return runner_context().assembled_manifest_path(date)


def pdf_path(date: str) -> Path:
    return runner_context().pdf_path(date)


def validation_report_path(date: str) -> Path:
    return runner_context().validation_report_path(date)


def run_report_path(date: str) -> Path:
    return runner_context().run_report_path(date)


def candidate_pool_path(date: str) -> Path:
    return runner_context().candidate_pool_path(date)


def source_audit_path(date: str) -> Path:
    return runner_context().source_audit_path(date)


def source_audit_markdown_path(date: str) -> Path:
    return runner_context().source_audit_markdown_path(date)


def adaptive_fetch_report_path(date: str) -> Path:
    return runner_context().adaptive_fetch_report_path(date)


def evidence_digest_path(date: str) -> Path:
    return runner_context().evidence_digest_path(date)


def source_tensions_path(date: str) -> Path:
    return runner_context().source_tensions_path(date)


def research_lint_path(date: str) -> Path:
    return runner_context().research_lint_path(date)


def source_confidence_path(date: str) -> Path:
    return runner_context().source_confidence_path(date)


def research_depth_path(date: str) -> Path:
    return runner_context().research_depth_path(date)


def research_depth_markdown_path(date: str) -> Path:
    return runner_context().research_depth_markdown_path(date)


def perplexity_discovery_path(date: str) -> Path:
    return runner_context().perplexity_discovery_path(date)


def perplexity_discovery_markdown_path(date: str) -> Path:
    return runner_context().perplexity_discovery_markdown_path(date)


def research_vault_dir() -> Path:
    return runner_context().research_vault_dir()


def research_vault_index_path() -> Path:
    return runner_context().research_vault_index_path()


def health_dashboard_path() -> Path:
    return runner_context().health_dashboard_path()


def visual_audit_path(date: str, pdf_sha256: str) -> Path:
    return runner_context().visual_audit_path(date, pdf_sha256)


def delivery_attachment_name(date: str) -> str:
    return runner_context().delivery_attachment_name(date)


def image_cache_dir(date: str) -> Path:
    return runner_context().image_cache_dir(date)


def ensure_layout() -> None:
    runner_context().ensure_layout()


def cleanup_generated_outputs(**kwargs) -> list[Path]:
    return runner_context().cleanup_generated_outputs(**kwargs)
