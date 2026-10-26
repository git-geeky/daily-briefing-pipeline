from pathlib import Path

from _shared_imports import SHARED_TESTS_DIR  # noqa: F401
from shared_test_support import SharedAdaptiveFetchTests


class AdaptiveFetchTests(SharedAdaptiveFetchTests):
    __unittest_skip__ = False
    SCRIPTS_DIR = Path(__file__).resolve().parents[1]
