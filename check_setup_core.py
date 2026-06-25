from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


HOME = Path.home()
RUNNERS = {
    "codex": HOME / ".codex" / "skills" / "news-briefing" / "scripts",
    "claude": HOME / ".claude" / "scheduled-tasks" / "daily-intelligence-briefing" / "scripts",
}


def run_command(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
    if proc.stdout.strip():
        print(proc.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the shared briefing setup checks.")
    parser.add_argument("--skip-smoke", action="store_true", help="Skip the dual-run smoke test.")
    args = parser.parse_args()

    for name, scripts_dir in RUNNERS.items():
        print(f"[{name}] unit tests")
        run_command(
            [
                "py",
                "-3",
                "-m",
                "unittest",
                "discover",
                "-s",
                str(scripts_dir / "tests"),
                "-p",
                "test_*.py",
                "-v",
            ]
        )
        print(f"[{name}] shared-core version")
        run_command(["py", "-3", str(scripts_dir / "build_pdf.py"), "--version"])

    if not args.skip_smoke:
        print("[shared] dual-run smoke check")
        run_command(["py", "-3", str(RUNNERS["codex"] / "smoke_check.py")])

    print("Setup check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
