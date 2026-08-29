"""registry.py プラグイン検出の隔離テスト (v0.7): 1 モジュールの import 失敗
または register() 失敗が、同じディレクトリの他モジュールの検出を道連れに
しないこと。cli.py 側 (detector.detect() / collector.collect() 呼び出し時の
例外隔離) は test_cli.py の ExceptionIsolationTest でカバーする。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from registry import discover_custom_plugins, discover_plugins


def _make_fake_package(root: Path, pkg_name: str) -> Path:
    pkg_dir = root / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    return pkg_dir


class _ImportedModuleCleanup:
    """Removes fake package modules from sys.modules / sys.path after a test
    so fixtures from one test can't leak into (or shadow) another."""

    def __init__(self, sys_path_entry: str, module_names):
        self._sys_path_entry = sys_path_entry
        self._module_names = list(module_names)

    def __enter__(self):
        sys.path.insert(0, self._sys_path_entry)
        return self

    def __exit__(self, *exc_info):
        if self._sys_path_entry in sys.path:
            sys.path.remove(self._sys_path_entry)
        for name in self._module_names:
            sys.modules.pop(name, None)
        return False


class DiscoverPluginsIsolationTest(unittest.TestCase):
    def test_register_exception_does_not_block_sibling_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg_dir = _make_fake_package(root, "fake_plugins_register")
            (pkg_dir / "good.py").write_text(
                "class G:\n"
                "    name = 'g'\n"
                "    priority = 1\n"
                "def register():\n"
                "    return G()\n"
            )
            (pkg_dir / "bad.py").write_text(
                "def register():\n"
                "    raise RuntimeError('register boom')\n"
            )
            with _ImportedModuleCleanup(
                str(root),
                ["fake_plugins_register", "fake_plugins_register.good", "fake_plugins_register.bad"],
            ):
                instances = discover_plugins(pkg_dir, "fake_plugins_register")
            names = sorted(getattr(i, "name", None) for i in instances)
            self.assertEqual(names, ["g"])

    def test_import_exception_does_not_block_sibling_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg_dir = _make_fake_package(root, "fake_plugins_import")
            (pkg_dir / "good.py").write_text(
                "class G:\n"
                "    name = 'g'\n"
                "    priority = 1\n"
                "def register():\n"
                "    return G()\n"
            )
            (pkg_dir / "broken.py").write_text("raise ImportError('boom at import time')\n")
            with _ImportedModuleCleanup(
                str(root),
                ["fake_plugins_import", "fake_plugins_import.good", "fake_plugins_import.broken"],
            ):
                instances = discover_plugins(pkg_dir, "fake_plugins_import")
            names = sorted(getattr(i, "name", None) for i in instances)
            self.assertEqual(names, ["g"])

    def test_module_without_register_is_silently_skipped(self):
        # Pre-existing behavior (not part of this fix): a module with no
        # register() contributes nothing and is not an error.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg_dir = _make_fake_package(root, "fake_plugins_noregister")
            (pkg_dir / "helper.py").write_text("VALUE = 1\n")
            with _ImportedModuleCleanup(
                str(root), ["fake_plugins_noregister", "fake_plugins_noregister.helper"]
            ):
                instances = discover_plugins(pkg_dir, "fake_plugins_noregister")
            self.assertEqual(instances, [])


class DiscoverCustomPluginsIsolationTest(unittest.TestCase):
    def test_register_exception_does_not_block_sibling_custom_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp)
            (custom_dir / "good.py").write_text(
                "class G:\n"
                "    name = 'g'\n"
                "    priority = 1\n"
                "def register():\n"
                "    return G()\n"
            )
            (custom_dir / "bad.py").write_text(
                "def register():\n"
                "    raise RuntimeError('register boom')\n"
            )
            instances = discover_custom_plugins(custom_dir)
            names = sorted(getattr(i, "name", None) for i in instances)
            self.assertEqual(names, ["g"])

    def test_exec_exception_does_not_block_sibling_custom_plugins(self):
        # Pre-existing isolation (module fails to exec_module at all);
        # kept here as a baseline alongside the new register()-failure case.
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp)
            (custom_dir / "good.py").write_text(
                "class G:\n"
                "    name = 'g'\n"
                "    priority = 1\n"
                "def register():\n"
                "    return G()\n"
            )
            (custom_dir / "broken.py").write_text("raise ImportError('boom at exec time')\n")
            instances = discover_custom_plugins(custom_dir)
            names = sorted(getattr(i, "name", None) for i in instances)
            self.assertEqual(names, ["g"])


if __name__ == "__main__":
    unittest.main()
