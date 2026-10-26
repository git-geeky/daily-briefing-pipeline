from pathlib import Path

from _shared_imports import SHARED_TESTS_DIR  # noqa: F401
from shared_test_support import SharedBriefingPathsTests


class BriefingPathsTests(SharedBriefingPathsTests):
    __unittest_skip__ = False
    SCRIPTS_DIR = Path(__file__).resolve().parents[1]
    EXPECTED_ARTIFACT_STEM = "claude-daily-briefing"
    IMAGE_ROOT_INCLUDES_STEM = False
    SUPPORTS_IDENTITY_OVERRIDES = True
    EXPECTED_BRIEFING_LABEL = "The Daily Intelligence Briefing"
    EXPECTED_BRIEFING_AUTHOR = "Intelligence Report"
    EXPECTED_PREPARED_BY = "Prepared by Claude"
