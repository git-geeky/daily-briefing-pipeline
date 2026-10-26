from pathlib import Path

from _shared_imports import SHARED_TESTS_DIR  # noqa: F401
from shared_test_support import SharedRunnerContractTests


class RunnerContractTests(SharedRunnerContractTests):
    __unittest_skip__ = False
    SCRIPTS_DIR = Path(__file__).resolve().parents[1]
    EXPECTED_RUNNER_NAME = "claude"
    EXPECTED_ARTIFACT_STEM = "claude-daily-briefing"
    EXPECTED_CLI_NAME = "claude"
    EXPECTED_IMAGE_ROOT_MODE = "flat"
    EXPECTED_BRIEFING_LABEL = "The Daily Intelligence Briefing"
    EXPECTED_BRIEFING_AUTHOR = "Intelligence Report"
    EXPECTED_PREPARED_BY = "Prepared by Claude"
