"""patterns loader (core + check-sensitive-files) の契約テスト。

XDG_CONFIG_HOME / HOME を tmpdir に隔離し、実ホームを汚染しない。
両モジュールが同じ fixture から同じ rules を返すことを契約テストで固定する。
0.6.0 から ``~/.claude/sensitive-files-guardrail/patterns.local.txt`` 単一パス
(0.4.0〜0.5.x の 2-tier lookup は撤去済み)。

0.14.1 で rename 前 (sensitive-files-guard) の旧 patterns.local.txt への
fallback 読み込み + 移行警告を追加。新パスが無く旧パスがある場合のみ旧パスを
読み、両方ある場合は新パス優先 (旧パス無視) であることを固定する。
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
    """HOME を tmpdir に隔離する基底クラス。

    XDG_CONFIG_HOME も同時に差し替えるが 0.6.0 では参照されない (旧 fallback の
    名残テスト前提)。新規テストでは HOME のみが意味を持つ。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup_dir)
        self.xdg_dir = Path(self.tmp) / "xdg"
        self.home_dir = Path(self.tmp) / "home"
        self.xdg_dir.mkdir()
        self.home_dir.mkdir()
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
        """``~/.claude/sensitive-files-guardrail/`` に patterns.local.txt を書く。"""
        d = self.home_dir / ".claude" / "sensitive-files-guardrail"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "patterns.local.txt"
        p.write_text(content)
        return p

    def _write_local(self, content: str) -> Path:
        """互換 alias: 既存テストとの後方互換のため preferred に書く。"""
        return self._write_preferred(content)

    def _write_legacy(self, content: str) -> Path:
        """rename 前 ``~/.claude/sensitive-files-guard/`` に patterns.local.txt を書く。"""
        d = self.home_dir / ".claude" / "sensitive-files-guard"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "patterns.local.txt"
        p.write_text(content)
        return p


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

    def test_resolve_local_path_is_home_claude(self):
        from core.patterns import _resolve_local_patterns_path
        p = _resolve_local_patterns_path()
        self.assertEqual(
            p,
            self.home_dir / ".claude" / "sensitive-files-guardrail" / "patterns.local.txt",
        )

    def test_legacy_fallback_loads_rules_and_warns(self):
        """新パス不在 + 旧パスのみ → 旧パスの rule をロードし移行 warning を出す。"""
        from core import patterns as core_patterns
        default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "!*.pub"]
        )
        # rename 前の旧パスにのみ custom rule を置く (新パスは書かない)
        self._write_legacy("*.pub\n!secret.pem\n")

        with mock.patch.object(core_patterns.L, "log_error") as mock_log:
            rules = core_patterns.load_patterns(default_file)

        # 旧パスの rule が既定の後ろに連結される (last-match-wins 維持)
        self.assertEqual(
            rules,
            [
                ("*.pem", False),
                ("*.pub", True),
                ("*.pub", False),
                ("secret.pem", True),
            ],
        )
        # 移行 warning が固定トークンで出る (具体パスは載せない)
        categories = [c.args[0] for c in mock_log.call_args_list]
        self.assertIn("local_patterns_legacy_path", categories)

    def test_preferred_wins_over_legacy_no_warn(self):
        """新パスと旧パス両方あり → 新パス優先・旧パス無視・移行 warning 無し。"""
        from core import patterns as core_patterns
        default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "!*.pub"]
        )
        self._write_preferred("!new.pem\n")     # 新パス (移行済みユーザの現行設定)
        self._write_legacy("!OLD_STALE.pem\n")  # 旧パス (無視されるべき)

        with mock.patch.object(core_patterns.L, "log_error") as mock_log:
            rules = core_patterns.load_patterns(default_file)

        # 新パスの rule のみ連結され、旧パスの stale rule は含まれない
        self.assertEqual(
            rules,
            [("*.pem", False), ("*.pub", True), ("new.pem", True)],
        )
        self.assertNotIn(("OLD_STALE.pem", True), rules)
        # 移行 warning は出ない (新パスがある = 移行済み)
        categories = [c.args[0] for c in mock_log.call_args_list]
        self.assertNotIn("local_patterns_legacy_path", categories)

    def test_no_local_anywhere_no_warn(self):
        """新パスも旧パスも無い → 既定のみ・移行 warning 無し (既存契約維持)。"""
        from core import patterns as core_patterns
        default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "!*.pub"]
        )
        with mock.patch.object(core_patterns.L, "log_error") as mock_log:
            rules = core_patterns.load_patterns(default_file)
        self.assertEqual(rules, [("*.pem", False), ("*.pub", True)])
        self.assertEqual(mock_log.call_count, 0)


class TestSharedLegacyFallback(BaseWithIsolatedHome):
    """_shared.load_patterns の旧パス fallback ロジックを callback スタブで直接検証。"""

    def test_resolve_legacy_path_is_old_guard_dir(self):
        from _shared.patterns import _resolve_legacy_local_patterns_path
        p = _resolve_legacy_local_patterns_path()
        self.assertEqual(
            p,
            self.home_dir / ".claude" / "sensitive-files-guard" / "patterns.local.txt",
        )

    def test_legacy_only_fires_migrate_callback_with_token(self):
        from _shared.patterns import (
            LEGACY_LOCAL_PATTERNS_WARN,
            load_patterns,
        )
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_legacy("!keep.pem\n")

        warn_calls: list[str] = []
        migrate_calls: list[str] = []
        rules = load_patterns(
            default_file,
            warn_callback=warn_calls.append,
            migrate_warn_callback=migrate_calls.append,
        )

        self.assertEqual(rules, [("*.pem", False), ("keep.pem", True)])
        # 移行 callback は固定トークンで 1 回だけ発火 (OS エラー callback は無発火)
        self.assertEqual(migrate_calls, [LEGACY_LOCAL_PATTERNS_WARN])
        self.assertEqual(warn_calls, [])

    def test_legacy_token_is_log_safe(self):
        """移行トークンは core.logging の detail 文字種ホワイトリストを通る。"""
        from core.logging import _sanitize_detail
        from _shared.patterns import LEGACY_LOCAL_PATTERNS_WARN
        self.assertEqual(
            _sanitize_detail(LEGACY_LOCAL_PATTERNS_WARN),
            LEGACY_LOCAL_PATTERNS_WARN,
        )

    def test_preferred_present_skips_legacy_and_migrate_callback(self):
        from _shared.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred("!new.pem\n")
        self._write_legacy("!OLD_STALE.pem\n")

        migrate_calls: list[str] = []
        rules = load_patterns(
            default_file,
            migrate_warn_callback=migrate_calls.append,
        )
        self.assertEqual(rules, [("*.pem", False), ("new.pem", True)])
        self.assertEqual(migrate_calls, [])

    def test_legacy_oserror_delegates_to_warn_not_migrate(self):
        """旧パス読込で FileNotFound 以外の OSError → warn_callback に委譲・migrate 無発火。"""
        from _shared.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_legacy("!keep.pem\n")  # 存在はするが read で OSError を強制
        original_read_text = Path.read_text

        def fake_read_text(self_path: Path, *args, **kwargs):
            # 旧パス (sensitive-files-guard 配下) の read だけ失敗させる
            if (
                self_path.name == "patterns.local.txt"
                and "sensitive-files-guard" in str(self_path)
                and "sensitive-files-guardrail" not in str(self_path)
            ):
                raise PermissionError("mock permission denied")
            return original_read_text(self_path, *args, **kwargs)

        warn_calls: list[str] = []
        migrate_calls: list[str] = []
        with mock.patch.object(Path, "read_text", fake_read_text):
            rules = load_patterns(
                default_file,
                warn_callback=warn_calls.append,
                migrate_warn_callback=migrate_calls.append,
            )
        # 既定のみ返り、OS エラーは warn に委譲、移行 callback は無発火
        self.assertEqual(rules, [("*.pem", False)])
        self.assertEqual(warn_calls, ["PermissionError"])
        self.assertEqual(migrate_calls, [])


class TestCheckerLoaderContract(BaseWithIsolatedHome):
    """check-sensitive-files/checker.py::load_patterns が core と同じ rules を返すこと。"""

    def _import_checker(self):
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

    def test_checker_legacy_fallback_loads_and_warns_stderr(self):
        """Stop 側 (checker) も旧パスを fallback ロードし stderr に移行 warning を出す。"""
        import io
        from contextlib import redirect_stderr

        checker = self._import_checker()
        default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "!*.pub"]
        )
        self._write_legacy("*.pub\n!secret.pem\n")  # 旧パスのみ

        buf = io.StringIO()
        with redirect_stderr(buf):
            rules = checker.load_patterns(default_file)

        self.assertEqual(
            rules,
            [
                ("*.pem", False),
                ("*.pub", True),
                ("*.pub", False),
                ("secret.pem", True),
            ],
        )
        stderr_text = buf.getvalue()
        # 固定トークンが stderr に出る (具体パス案内も含むが値・秘密は無し)
        self.assertIn("legacy_patterns_local_in_use", stderr_text)

    def test_checker_preferred_wins_over_legacy_no_warn(self):
        """Stop 側も新パス優先・旧パス無視・移行 warning 無し。"""
        import io
        from contextlib import redirect_stderr

        checker = self._import_checker()
        default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "!*.pub"]
        )
        self._write_preferred("!new.pem\n")
        self._write_legacy("!OLD_STALE.pem\n")

        buf = io.StringIO()
        with redirect_stderr(buf):
            rules = checker.load_patterns(default_file)

        self.assertEqual(
            rules,
            [("*.pem", False), ("*.pub", True), ("new.pem", True)],
        )
        self.assertNotIn(("OLD_STALE.pem", True), rules)
        self.assertNotIn("legacy_patterns_local_in_use", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
