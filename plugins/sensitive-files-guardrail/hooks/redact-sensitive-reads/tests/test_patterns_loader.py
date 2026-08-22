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


class TestResolveProjectKey(BaseWithIsolatedHome):
    """_resolve_project_key の解決順位 (CLAUDE_PROJECT_DIR → cwd 遡上) を検証。"""

    def setUp(self):
        super().setUp()
        self._project_env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._project_env_patcher.start()
        self.addCleanup(self._project_env_patcher.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def test_env_var_wins_and_is_normalized(self):
        from _shared.patterns import _resolve_project_key
        os.environ["CLAUDE_PROJECT_DIR"] = str(Path(self.tmp) / "proj") + "/"
        self.assertEqual(
            _resolve_project_key("/somewhere/else"),
            os.path.normpath(str(Path(self.tmp) / "proj") + "/"),
        )

    def test_walks_up_to_git_dir(self):
        from _shared.patterns import _resolve_project_key
        proj = Path(self.tmp) / "proj"
        sub = proj / "packages" / "api"
        sub.mkdir(parents=True)
        (proj / ".git").mkdir()
        self.assertEqual(_resolve_project_key(str(sub)), str(proj))

    def test_no_git_dir_found_returns_none(self):
        from _shared.patterns import _resolve_project_key
        lone = Path(self.tmp) / "lonely"
        lone.mkdir()
        self.assertIsNone(_resolve_project_key(str(lone)))

    def test_home_itself_is_not_a_project(self):
        from _shared.patterns import _resolve_project_key
        (self.home_dir / ".git").mkdir()
        self.assertIsNone(_resolve_project_key(str(self.home_dir)))

    def test_empty_cwd_returns_none(self):
        from _shared.patterns import _resolve_project_key
        self.assertIsNone(_resolve_project_key(""))


class TestParseLocalPatternsText(unittest.TestCase):
    """``[project:...]`` セクション対応パーサの契約テスト (HOME 非依存)。"""

    def test_no_sections_matches_legacy_parser(self):
        from _shared.patterns import _parse_local_patterns_text, _parse_patterns_text
        text = "# comment\n*.pem\n!*.pub\n"
        self.assertEqual(
            _parse_local_patterns_text(text, None),
            _parse_patterns_text(text),
        )

    def test_project_key_none_skips_all_sections(self):
        from _shared.patterns import _parse_local_patterns_text
        text = "!common.pem\n[project:/x]\n!only-x.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, None),
            [("common.pem", True)],
        )

    def test_matching_section_included_in_file_order(self):
        from _shared.patterns import _parse_local_patterns_text
        text = (
            "!common.pem\n[project:/x]\n!only-x.pem\n[project:/y]\n!only-y.pem\n"
        )
        self.assertEqual(
            _parse_local_patterns_text(text, "/x"),
            [("common.pem", True), ("only-x.pem", True)],
        )
        self.assertEqual(
            _parse_local_patterns_text(text, "/y"),
            [("common.pem", True), ("only-y.pem", True)],
        )

    def test_section_before_common_lines_preserves_file_order(self):
        """セクションを共通行より前に書けば、出現順どおりセクション側が先に
        評価される (last-match-wins は出現順で決まる、という既存契約を維持)。"""
        from _shared.patterns import _parse_local_patterns_text
        text = "[project:/x]\n*.pem\n\n!*.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, "/x"),
            [("*.pem", False), ("*.pem", True)],
        )
        # ヘッダー登場前の共通行が無いので、セクション不一致なら空。
        self.assertEqual(_parse_local_patterns_text(text, None), [])

    def test_header_path_normalized_trailing_slash(self):
        from _shared.patterns import _parse_local_patterns_text
        text = "[project:/x/y/]\n!foo.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, "/x/y"),
            [("foo.pem", True)],
        )

    def test_unmatched_section_ignored(self):
        from _shared.patterns import _parse_local_patterns_text
        text = "!common.pem\n[project:/other]\n!ignored.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, "/x"),
            [("common.pem", True)],
        )


class TestBadProjectHeaderWarn(BaseWithIsolatedHome):
    """0.19.0 (L2 review): ``[project:]`` ヘッダーが空 / 未展開 placeholder のとき
    黙って捨てず固定トークンで警告する。除外案内が ``$CLAUDE_PROJECT_DIR`` を
    変数名で示すため、unquoted echo (空に展開) / literal 書込のどちらでも silent
    no-op になるのを可視化する。判定 (非 active) は変えない。"""

    def test_parse_warns_once_per_token_and_keeps_section_inactive(self):
        from _shared.patterns import (
            PROJECT_HEADER_WARN_EMPTY,
            PROJECT_HEADER_WARN_PLACEHOLDER,
            _parse_local_patterns_text,
        )
        text = (
            "!common.pem\n"
            "[project:]\n!empty.pem\n"
            "[project:$CLAUDE_PROJECT_DIR]\n!placeholder.pem\n"
            "[project:$CLAUDE_PROJECT_DIR]\n!again.pem\n"
            "[project:/work/p]\n!real.pem\n"
        )
        calls: list[str] = []
        rules = _parse_local_patterns_text(text, "/work/p", calls.append)
        self.assertEqual(rules, [("common.pem", True), ("real.pem", True)])
        self.assertEqual(
            calls, [PROJECT_HEADER_WARN_EMPTY, PROJECT_HEADER_WARN_PLACEHOLDER]
        )

    def test_parse_without_callback_is_silent_and_inactive(self):
        from _shared.patterns import _parse_local_patterns_text
        rules = _parse_local_patterns_text(
            "[project:]\n!x.pem\n[project:$CLAUDE_PROJECT_DIR]\n!y.pem\n",
            "/work/p",
        )
        self.assertEqual(rules, [])

    def test_literal_dollar_in_path_is_a_valid_header(self):
        # `/work/project$prod` のような `$` 入り literal パスは正当なヘッダー
        # (Codex R2 P2-1: 当初は placeholder 扱いでセクションが黙って落ちていた)
        from _shared.patterns import _parse_local_patterns_text
        calls: list[str] = []
        text = "[project:/work/project$prod]\n!x.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, "/work/project$prod", calls.append),
            [("x.pem", True)],
        )
        self.assertEqual(calls, [])
        # 別プロジェクトでは不一致 (通常のセクション挙動) で警告も出ない
        self.assertEqual(
            _parse_local_patterns_text(text, "/work/other", calls.append), []
        )
        self.assertEqual(calls, [])

    def test_placeholder_syntax_forms_are_detected(self):
        from _shared.patterns import (
            PROJECT_HEADER_WARN_PLACEHOLDER,
            _parse_local_patterns_text,
        )
        for header in (
            "$CLAUDE_PROJECT_DIR",
            "${CLAUDE_PROJECT_DIR}",
            "$CLAUDE_PROJECT_DIR/sub",
            "${CLAUDE_PROJECT_DIR}/sub",
            "$HOME/work",
            "${PWD}",
            "$PWD",
        ):
            calls: list[str] = []
            rules = _parse_local_patterns_text(
                f"[project:{header}]\n!x.pem\n", "/work/p", calls.append
            )
            self.assertEqual(rules, [], msg=header)
            self.assertEqual(
                calls, [PROJECT_HEADER_WARN_PLACEHOLDER], msg=header
            )

    def test_dollar_mid_path_forms_are_not_placeholders(self):
        from _shared.patterns import _bad_header_token
        for header in (
            "/work/project$prod",
            "/srv/app$1",
            "/x/$",
            "/a$b/c",
            # 予約語を部分文字列として含むだけの literal パス (Codex R4 P2-2)
            "/work/repo$CLAUDE_PROJECT_DIR-prod",
            "/work/${CLAUDE_PROJECT_DIR}-archive",
            "/prefix/$CLAUDE_PROJECT_DIR",
            "$CLAUDE_PROJECT_DIR-prod",
        ):
            self.assertIsNone(_bad_header_token(header), msg=header)

    def test_reserved_word_substring_paths_are_valid_headers(self):
        # `/work/repo$CLAUDE_PROJECT_DIR-prod` のような正当なパスの section が
        # project_key と比較される (Codex R4 P2-2: 当初は任意位置の予約語で無効化)
        from _shared.patterns import _parse_local_patterns_text
        for header in (
            "/work/repo$CLAUDE_PROJECT_DIR-prod",
            "/work/${CLAUDE_PROJECT_DIR}-archive",
        ):
            calls: list[str] = []
            text = f"[project:{header}]\n!x.pem\n"
            self.assertEqual(
                _parse_local_patterns_text(text, header, calls.append),
                [("x.pem", True)],
                msg=header,
            )
            self.assertEqual(
                _parse_local_patterns_text(text, "/work/other", calls.append),
                [],
                msg=header,
            )
            self.assertEqual(calls, [], msg=header)

    def test_tokens_are_log_safe(self):
        from core.logging import _sanitize_detail
        from _shared.patterns import (
            PROJECT_HEADER_WARN_EMPTY,
            PROJECT_HEADER_WARN_PLACEHOLDER,
        )
        for token in (PROJECT_HEADER_WARN_EMPTY, PROJECT_HEADER_WARN_PLACEHOLDER):
            self.assertEqual(_sanitize_detail(token), token)

    def test_core_loader_logs_header_invalid(self):
        from core import patterns as P
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred("[project:$CLAUDE_PROJECT_DIR]\n!x.pem\n")
        with mock.patch("core.patterns.L.log_error") as spy:
            rules = P.load_patterns(default_file, cwd=self.tmp)
        self.assertEqual(rules, [("*.pem", False)])
        spy.assert_called_once_with(
            "local_patterns_header_invalid",
            "project_header_unexpanded_placeholder",
        )

    def test_core_loader_no_log_for_valid_header(self):
        from core import patterns as P
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred("[project:/work/p]\n!x.pem\n")
        with mock.patch("core.patterns.L.log_error") as spy:
            P.load_patterns(default_file, cwd=self.tmp)
        spy.assert_not_called()


class TestProjectScopedLoadPatterns(BaseWithIsolatedHome):
    """load_patterns の ``cwd`` 引数によるプロジェクトセクション適用を検証。"""

    def setUp(self):
        super().setUp()
        self._project_env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._project_env_patcher.start()
        self.addCleanup(self._project_env_patcher.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def test_project_section_applied_via_cwd(self):
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        proj = Path(self.tmp) / "proj"
        proj.mkdir()
        (proj / ".git").mkdir()
        self._write_preferred(f"!global-exclude.pem\n[project:{proj}]\n!.npmrc\n")

        rules = load_patterns(default_file, cwd=str(proj))
        self.assertEqual(
            rules,
            [
                ("*.pem", False),
                ("global-exclude.pem", True),
                (".npmrc", True),
            ],
        )

    def test_project_section_not_applied_for_other_cwd(self):
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        proj = Path(self.tmp) / "proj"
        proj.mkdir()
        (proj / ".git").mkdir()
        other = Path(self.tmp) / "other"
        other.mkdir()
        (other / ".git").mkdir()
        self._write_preferred(f"[project:{proj}]\n!.npmrc\n")

        rules = load_patterns(default_file, cwd=str(other))
        self.assertEqual(rules, [("*.pem", False)])

    def test_project_section_via_subdirectory_cwd(self):
        """monorepo 等でサブディレクトリが cwd でも project root の .git まで遡る。"""
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        proj = Path(self.tmp) / "proj"
        sub = proj / "packages" / "api"
        sub.mkdir(parents=True)
        (proj / ".git").mkdir()
        self._write_preferred(f"[project:{proj}]\n!.npmrc\n")

        rules = load_patterns(default_file, cwd=str(sub))
        self.assertEqual(rules, [("*.pem", False), (".npmrc", True)])

    def test_no_cwd_behaves_as_before(self):
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred("!global.pem\n")

        rules = load_patterns(default_file)
        self.assertEqual(rules, [("*.pem", False), ("global.pem", True)])

    def test_checker_and_core_agree_with_project_section(self):
        from core.patterns import load_patterns as core_load
        checker_dir = (
            Path(__file__).resolve().parent.parent.parent / "check-sensitive-files"
        )
        if str(checker_dir) not in sys.path:
            sys.path.insert(0, str(checker_dir))
        import checker as _checker
        importlib.reload(_checker)

        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        proj = Path(self.tmp) / "proj"
        proj.mkdir()
        (proj / ".git").mkdir()
        self._write_preferred(f"[project:{proj}]\n!.npmrc\n")

        core_rules = core_load(default_file, cwd=str(proj))
        checker_rules = _checker.load_patterns(default_file, cwd=str(proj))
        self.assertEqual(core_rules, checker_rules)


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
