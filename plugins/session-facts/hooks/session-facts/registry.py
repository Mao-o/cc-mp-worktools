from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path
from typing import List


def discover_plugins(package_dir: Path, base_package: str) -> List:
    """Import all modules in a package directory, return plugin instances via register().

    A module that fails to import, or whose register() raises, is skipped
    with a stderr warning rather than aborting discovery for the rest of the
    package — one broken detector/collector must not silence every other
    one.
    """
    instances: List = []
    if not package_dir.is_dir():
        return instances
    for finder, name, ispkg in pkgutil.iter_modules([str(package_dir)]):
        if name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{base_package}.{name}")
            if hasattr(module, "register"):
                result = module.register()
                if isinstance(result, list):
                    instances.extend(result)
                else:
                    instances.append(result)
        except Exception as e:
            print(
                f"[session-facts] WARNING: plugin {base_package}.{name} failed to load: {e}",
                file=sys.stderr,
            )
    return instances


def discover_custom_plugins(custom_dir: Path) -> List:
    """Import custom user plugins from a standalone directory (not a package).

    Isolates both failure points: a module that fails to exec (syntax error,
    bad import) and a module whose register() itself raises. Either is
    skipped with a stderr warning rather than losing every other custom
    plugin (or the whole run) to one broken file.
    """
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
        except Exception as e:
            print(f"[session-facts] WARNING: failed to load {py_file.name}: {e}", file=sys.stderr)
            continue
        if hasattr(module, "register"):
            try:
                result = module.register()
            except Exception as e:
                print(
                    f"[session-facts] WARNING: plugin custom.{py_file.stem} failed to register: {e}",
                    file=sys.stderr,
                )
                continue
            if isinstance(result, list):
                instances.extend(result)
            else:
                instances.append(result)
    return instances
