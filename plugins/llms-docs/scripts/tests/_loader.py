"""Test-only helpers for loading and driving the hyphenated parse-*.py scripts.

``parse-claude-docs.py`` / ``parse-ai-sdk.py`` / ``parse-firebase.py`` cannot
be imported with a normal ``import`` statement because their filenames
contain hyphens (not valid Python identifiers). We load them with
``importlib.util.spec_from_file_location`` instead.

Importing this module has the side effect of inserting the real
``scripts/`` directory at the front of ``sys.path`` — the same thing each
``parse-*.py`` does for itself at runtime (``sys.path.insert(0,
os.path.dirname(os.path.realpath(__file__)))``) so its own ``from _common
import ...`` resolves. Tests that need ``_common`` directly can rely on
this import happening first (e.g. ``import _loader`` then ``import
_common``).

Uses bare PEP 604 / PEP 585 type hints (``list[str]``, ``str | None``)
without ``from __future__ import annotations`` — this plugin's floor is
Python 3.11+ (see README.md "前提条件"), matching the ``parse-*.py`` scripts
under test, so no 3.9-compat shim is added here either.
"""

import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_script(filename: str) -> ModuleType:
    """Import ``<scripts_dir>/<filename>`` (e.g. ``parse-claude-docs.py``).

    Cached in ``sys.modules`` under a sanitized name so repeated calls
    within a test process return the same module object, mirroring normal
    ``import`` caching.
    """
    module_name = "_llms_docs_under_test_" + filename[:-3].replace("-", "_")
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_cli(module: ModuleType, argv: list[str]) -> tuple[int, str, str]:
    """Run *module*'s ``main()`` with ``sys.argv`` set to *argv*.

    Captures stdout/stderr and normalizes ``SystemExit`` (raised by argparse
    on usage errors and by our own ``sys.exit`` calls) into a plain exit
    code. Restores the real ``sys.argv`` afterwards regardless of outcome.

    Returns ``(exit_code, stdout, stderr)``. A clean return from ``main()``
    (no ``SystemExit``) is reported as exit code 0.
    """
    old_argv = sys.argv
    sys.argv = list(argv)
    out = io.StringIO()
    err = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            module.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
    finally:
        sys.argv = old_argv
    return code, out.getvalue(), err.getvalue()
