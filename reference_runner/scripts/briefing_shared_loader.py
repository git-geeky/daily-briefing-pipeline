from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


DEFAULT_SHARED_ROOT = Path.home() / "Documents" / "Daily-Briefings" / "pipeline-shared"
SHARED_ROOT_ENV = "DAILY_BRIEFING_SHARED_ROOT"


def shared_root() -> Path:
    return Path(os.environ.get(SHARED_ROOT_ENV, str(DEFAULT_SHARED_ROOT)))


def shared_module_path(filename: str) -> Path:
    return shared_root() / filename


def shared_module_sha256(filename: str) -> str:
    return hashlib.sha256(shared_module_path(filename).read_bytes()).hexdigest()


def shared_module_version_line(filename: str) -> str:
    path = shared_module_path(filename)
    return f"{path.name} path={path} sha256={shared_module_sha256(filename)}"


def handle_version_flag(filename: str, argv: list[str] | None = None) -> bool:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" not in args:
        return False
    print(shared_module_version_line(filename))
    return True


def load_shared_module(filename: str, local_file: str) -> ModuleType:
    shared_path = shared_module_path(filename)
    if not shared_path.exists():
        raise ImportError(f"Shared briefing module not found: {shared_path}")

    local_path = Path(local_file).resolve()
    module_name = f"_briefing_shared_{local_path.parent.name}_{local_path.stem}_{shared_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, shared_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load shared briefing module: {shared_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def reexport(module: ModuleType, namespace: dict) -> None:
    namespace["__doc__"] = getattr(module, "__doc__", None)
    exports = getattr(module, "__all__", None)
    if exports is None:
        exports = [name for name in dir(module) if not name.startswith("_")]
    for name in exports:
        namespace[name] = getattr(module, name)
