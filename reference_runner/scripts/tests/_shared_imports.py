"""Test bootstrap for the in-repo reference runner.

The production runners load the shared ``*_core.py`` modules from
``~/Documents/Daily-Briefings/pipeline-shared``. This bootstrap points the same
loader (``briefing_shared_loader``) at this repository instead, puts the shared
test mixins on ``sys.path``, and provides a ``py`` launcher shim so tests that
shell out to ``py -3`` also pass on Linux/macOS CI.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SHARED_TESTS_DIR = REPO_ROOT / "tests"

os.environ.setdefault("DAILY_BRIEFING_SHARED_ROOT", str(REPO_ROOT))
for entry in (str(SHARED_TESTS_DIR), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _install_py_shim() -> None:
    if shutil.which("py") or os.name == "nt":
        return
    shim_dir = Path(tempfile.mkdtemp(prefix="briefing-py-shim-"))
    shim = shim_dir / "py"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-3" ]; then shift; fi\n'
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"


_install_py_shim()
