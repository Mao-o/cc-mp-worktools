from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import List


def discover_plugins(package_dir: Path, base_package: str) -> List:
    """Import all modules in a package directory, return plugin instances via register()."""
    instances: List = []
    if not package_dir.is_dir():
        return instances
    for finder, name, ispkg in pkgutil.iter_modules([str(package_dir)]):
        if name.startswith("_"):
            continue
        module = importlib.import_module(f"{base_package}.{name}")
        if hasattr(module, "register"):
            result = module.register()
            if isinstance(result, list):
                instances.extend(result)
            else:
                instances.append(result)
    return instances


def discover_custom_plugins(custom_dir: Path) -> List:
    """Import custom user plugins from a standalone directory (not a package)."""
    import importlib.util
    instances: List = []
    if not custom_dir.is_dir():
        return instances
    for py_file in sorted(custom_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(
            f"custom.{py_file.stem}", str(py_file)
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            continue
        if hasattr(module, "register"):
            result = module.register()
            if isinstance(result, list):
                instances.extend(result)
            else:
                instances.append(result)
    return instances
