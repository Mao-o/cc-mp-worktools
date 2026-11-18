"""Test-only helpers for loading and driving the hyphenated parse-*.py scripts.

``parse-llms-txt.py`` / ``check-preset-urls.py`` cannot be imported with a
normal ``import`` statement because their filenames contain hyphens (not
valid Python identifiers). We load them with
``importlib.util.spec_from_file_location`` instead.

Importing this module has the side effect of inserting the real
``scripts/`` directory at the front of ``sys.path`` — the same thing each
``parse-*.py`` does for itself at runtime (``sys.path.insert(0,
os.path.dirname(os.path.realpath(__file__)))``) so its own ``from _common
import ...`` resolves. Tests that need ``_common`` directly can rely on
this import happening first (e.g. ``import _loader`` then ``import
_common``).

It also moves the scripts' default cache into a temp dir of the test
process (``SANDBOX``, see ``_sandbox``), so nothing a test runs can write
into the developer's own cache.

Uses bare PEP 604 / PEP 585 type hints (``list[str]``, ``str | None``)
without ``from __future__ import annotations`` — this plugin's floor is
Python 3.11+ (see README.md "前提条件"), matching the ``parse-*.py`` scripts
under test, so no 3.9-compat shim is added here either.
"""

import atexit
import contextlib
import importlib.util
import io
import json
import os
import shutil
import site
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# What ``_common.default_cache_dir()`` reads, in its order of precedence
CACHE_ENV = ("LLMS_DOCS_CACHE_DIR", "XDG_CACHE_HOME", "HOME")
_SANDBOX_ENV = "LLMS_DOCS_TEST_SANDBOX"


def _sandbox() -> tuple[str, dict]:
    """Point the scripts' default cache into a temp dir of this process, before
    any test runs: ``(that dir, the values CACHE_ENV had before)``.

    A command run without ``--cache-dir`` puts its files in
    ``default_cache_dir()``, the developer's real cache unless the environment
    says otherwise. The tests run commands as they print them, so a printed
    command that lost its ``--cache-dir`` (a broken hint, or work in progress)
    wrote the fixtures over the real cache, which ``--max-age`` then served for
    a week. Here ``$HOME`` and ``$XDG_CACHE_HOME`` point into the temp dir and
    ``$LLMS_DOCS_CACHE_DIR`` is unset, so a test that removes some of them (to
    test the order of precedence, or to keep the printed ``--cache-dir``
    stable) still lands there. ``$PYTHONUSERBASE`` keeps the user
    site-packages of a ``python`` the suite starts where they were.

    Once per process: this file is imported as ``_loader`` and, by one test, as
    ``tests._loader``; the second import must not take the first temp dir for
    the developer's. A process the suite starts makes its own. The dir is
    removed at exit."""
    state = json.loads(os.environ.get(_SANDBOX_ENV) or "null")
    if state and state["pid"] == os.getpid():
        return state["root"], state["outer"]
    outer = {k: os.environ.get(k) for k in CACHE_ENV}
    root = os.path.realpath(tempfile.mkdtemp(prefix="llms-docs-tests-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    home = os.path.join(root, "home")
    os.mkdir(home)
    os.environ.setdefault("PYTHONUSERBASE", site.getuserbase())
    os.environ["HOME"] = home
    os.environ["XDG_CACHE_HOME"] = os.path.join(home, ".cache")
    os.environ.pop("LLMS_DOCS_CACHE_DIR", None)
    os.environ[_SANDBOX_ENV] = json.dumps({"pid": os.getpid(), "root": root, "outer": outer})
    return root, outer


SANDBOX, OUTER_CACHE_ENV = _sandbox()


def local_cache_dir(env: dict | None = None) -> str:
    """The scripts' default cache outside the suite: ``default_cache_dir()``
    with *env* (default: the values this process started with) as CACHE_ENV.
    For the opt-in full-corpus gates, which read the developer's cache; they
    pass a copy of it to the commands, never the cache itself."""
    import _common
    values = {k: v for k, v in (OUTER_CACHE_ENV if env is None else env).items() if v is not None}
    with mock.patch.dict(os.environ, values):
        for key in CACHE_ENV:
            if key not in values:
                os.environ.pop(key, None)
        return _common.default_cache_dir()


def load_script(filename: str) -> ModuleType:
    """Import ``<scripts_dir>/<filename>`` (e.g. ``parse-llms-txt.py``).

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


def without_user_sources(test: unittest.TestCase) -> str:
    """Point ``$LLMS_DOCS_SOURCES_FILE`` at an empty ``sources.json`` for the
    rest of *test*, so a bundled preset (``--source claude-code``) runs as
    shipped and the developer's own ``sources.json`` cannot redefine it or
    add a warning to stderr. The ``Next:`` lines carry it as
    ``--sources-file <path>``, so a printed command reads the same file.
    Returns that path."""
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    path = os.path.join(tmp, "sources.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"sources": {}}, f)
    patcher = mock.patch.dict(os.environ, {"LLMS_DOCS_SOURCES_FILE": path})
    patcher.start()
    test.addCleanup(patcher.stop)
    return path


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
