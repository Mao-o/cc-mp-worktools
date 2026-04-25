"""patterns loader (core + check-sensitive-files) の契約テスト。

XDG_CONFIG_HOME / HOME を tmpdir に隔離し、実ホームを汚染しない。
両モジュールが同じ fixture から同じ rules を返すことを契約テストで固定する。
0.4.0 から 2-tier lookup (``~/.claude/`` preferred, ``$XDG_CONFIG_HOME/`` fallback)。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401


class BaseWithIsolatedHome(unittest.TestCase):
    """XDG_CONFIG_HOME / HOME を tmpdir に隔離する基底クラス。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup_dir)
        self.xdg_dir = Path(self.tmp) / "xdg"
        self.home_dir = Path(self.tmp) / "home"
        self.xdg_dir.mkdir()
        self.home_dir.mkdir()
        # 実ホームを汚染しないよう patch.dict で HOME/XDG を差し替え
        self._env_patcher = mock.patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.xdg_dir),
                "HOME": str(self.home_dir),
            },
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _cleanup_dir(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_preferred(self, content: str) -> Path:
        """新・優先パス (``~/.claude/sensitive-files-guard/``) に patterns.local.txt を書く。"""
        d = self.home_dir / ".claude" / "sensitive-files-guard"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "patterns.local.txt"
        p.write_text(content)
        return p

    def _write_fallback(self, content: str) -> Path:
        """fallback パス (``$XDG_CONFIG_HOME/sensitive-files-guard/``) に patterns.local.txt を書く。"""
        d = self.xdg_dir / "sensitive-files-guard"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "patterns.local.txt"
        p.write_text(content)
        return p

    def _write_local(self, content: str) -> Path:
        """互換 alias: 既存テストとの後方互換のため preferred に書く。"""
        return self._write_preferred(content)


def _make_default_patterns_file(tmp: Path, lines: list[str]) -> Path:
    f = tmp / "patterns.txt"
    f.write_text("\n".join(lines) + "\n")
    return f


class TestCorePatternsLoader(BaseWithIsolatedHome):
    """redact-sensitive-reads/core/patterns.py の挙動。"""

    def test_default_only_when_local_missing(self):
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "!*.pub"]
        )
        rules = load_patterns(default_file)
        self.assertEqual(rules, [("*.pem", False), ("*.pub", True)])

    def test_preferred_appended_when_present(self):
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "!*.pub"]
        )
        self._write_preferred("*.pub\n!foo.pem\n")
        rules = load_patterns(default_file)
        self.assertEqual(
            rules,
            [
                ("*.pem", False),
                ("*.pub", True),
                ("*.pub", False),
                ("foo.pem", True),
            ],
        )

    def test_local_oserror_emits_warning_and_keeps_default(self):
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "!*.pub"]
        )
        original_read_text = Path.read_text

        def fake_read_text(self_path: Path, *args, **kwargs):
            if self_path.name == "patterns.local.txt":
                raise PermissionError("mock permission denied")
            return original_read_text(self_path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", fake_read_text):
            rules = load_patterns(default_file)
        self.assertEqual(rules, [("*.pem", False), ("*.pub", True)])

    def test_parse_skips_blank_and_comments(self):
        from core.patterns import _parse_patterns_text
        text = "# comment\n\n*.pem\n  # indented comment\n!*.pub\n"
        rules = _parse_patterns_text(text)
        self.assertEqual(rules, [("*.pem", False), ("*.pub", True)])

    def test_resolve_local_preferred_is_home_claude(self):
        from core.patterns import _resolve_local_patterns_paths
        paths = _resolve_local_patterns_paths()
        self.assertEqual(
            paths[0],
            self.home_dir / ".claude" / "sensitive-files-guard" / "patterns.local.txt",
        )

    def test_resolve_local_fallback_uses_xdg(self):
        from core.patterns import _resolve_local_patterns_paths
        paths = _resolve_local_patterns_paths()
        self.assertIn(
            self.xdg_dir / "sensitive-files-guard" / "patterns.local.txt",
            paths,
        )

    def test_resolve_local_fallback_defaults_to_home_config_when_xdg_unset(self):
        from core.patterns import _resolve_local_patterns_paths
        with mock.patch.dict(os.environ):
            os.environ.pop("XDG_CONFIG_HOME", None)
            paths = _resolve_local_patterns_paths()
        self.assertEqual(
            paths[1],
            self.home_dir / ".config" / "sensitive-files-guard" / "patterns.local.txt",
        )

    def test_resolve_local_path_alias_returns_preferred(self):
        """_resolve_local_patterns_path (単数) は preferred を返す後方互換 alias。"""
        from core.patterns import _resolve_local_patterns_path
        p = _resolve_local_patterns_path()
        self.assertEqual(
            p, self.home_dir / ".claude" / "sensitive-files-guard" / "patterns.local.txt"
        )


class TestPreferredFallback2Tier(BaseWithIsolatedHome):
    """2-tier lookup の挙動 (Phase 4 新規)。"""

    def test_preferred_wins_when_both_exist(self):
        """両方存在時は preferred (~/.claude/) のみ採用、fallback は無視。"""
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred("# preferred\n*.from-preferred\n")
        self._write_fallback("# fallback\n*.from-fallback\n")
        rules = load_patterns(default_file)
        patterns = [p for p, _ex in rules]
        self.assertIn("*.from-preferred", patterns)
        self.assertNotIn("*.from-fallback", patterns)

    def test_fallback_used_when_preferred_absent(self):
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_fallback("*.from-fallback\n")
        rules = load_patterns(default_file)
        patterns = [p for p, _ex in rules]
        self.assertIn("*.from-fallback", patterns)

    def test_fallback_triggers_deprecation_warn(self):
        """fallback 採用時は warn_callback("deprecated_config_dir") が呼ばれる。"""
        from _shared.patterns import load_patterns as shared_load
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_fallback("*.from-fallback\n")
        messages: list[str] = []
        shared_load(default_file, warn_callback=lambda m: messages.append(m))
        self.assertIn("deprecated_config_dir", messages)

    def test_preferred_does_not_trigger_deprecation_warn(self):
        from _shared.patterns import load_patterns as shared_load
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred("*.from-preferred\n")
        messages: list[str] = []
        shared_load(default_file, warn_callback=lambda m: messages.append(m))
        self.assertNotIn("deprecated_config_dir", messages)


class TestCheckerLoaderContract(BaseWithIsolatedHome):
    """check-sensitive-files/checker.py::load_patterns が core と同じ rules を返すこと。"""

    def _import_checker(self):
        # check-sensitive-files をパスに追加して import
        checker_dir = (
            Path(__file__).resolve().parent.parent.parent / "check-sensitive-files"
        )
        if str(checker_dir) not in sys.path:
            sys.path.insert(0, str(checker_dir))
        import checker as _checker  # noqa: WPS433
        importlib.reload(_checker)
        return _checker

    def test_both_loaders_agree(self):
        from core.patterns import load_patterns as core_load
        checker = self._import_checker()

        default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "!*.pub", "id_rsa*"]
        )
        self._write_preferred("!foo.pem\n*.foo\n")

        core_rules = core_load(default_file)
        checker_rules = checker.load_patterns(default_file)
        self.assertEqual(core_rules, checker_rules)


if __name__ == "__main__":
    unittest.main()
