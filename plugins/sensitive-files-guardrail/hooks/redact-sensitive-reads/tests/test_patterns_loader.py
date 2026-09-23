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

from _testutil import FIXTURES, checker_dir_on_path  # noqa: F401


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
                "HOME": str(self.home_dir), "USERPROFILE": str(self.home_dir),
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
        p.write_text(content, encoding="utf-8")
        return p

    def _write_local(self, content: str) -> Path:
        """互換 alias: 既存テストとの後方互換のため preferred に書く。"""
        return self._write_preferred(content)

    def _write_legacy(self, content: str) -> Path:
        """rename 前 ``~/.claude/sensitive-files-guard/`` に patterns.local.txt を書く。"""
        d = self.home_dir / ".claude" / "sensitive-files-guard"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "patterns.local.txt"
        p.write_text(content, encoding="utf-8")
        return p


def _make_default_patterns_file(tmp: Path, lines: list[str]) -> Path:
    f = tmp / "patterns.txt"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
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


def _make_worktree(tmp: Path, *, main: str = "main", name: str = "wt") -> tuple[Path, Path]:
    """``git worktree add`` 相当のディレクトリ構成を作る (git は呼ばない)。

    実測した実構成 (git 2.50.1) に合わせる:

    - main repo: ``<main>/.git/`` (ディレクトリ)
    - worktree の管理領域: ``<main>/.git/worktrees/<name>/commondir`` = ``../..``
    - worktree の checkout: ``<main>/.claude/worktrees/<name>/.git`` (ファイル) が
      ``gitdir: <main>/.git/worktrees/<name>`` を持つ

    Returns:
        ``(main_root, worktree_root)``
    """
    main_root = tmp / main
    admin = main_root / ".git" / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    wt_root = main_root / ".claude" / "worktrees" / name
    wt_root.mkdir(parents=True)
    (wt_root / ".git").write_text(f"gitdir: {admin}\n")
    return main_root, wt_root


class TestWorktreeProjectKeys(BaseWithIsolatedHome):
    """0.32.0 (内部バックログ): worktree セッションで ``[project:<main repo>]``
    セクションが効くこと。``claude --worktree`` / ``--bg`` / sub-agent の
    ``isolation: worktree`` は別 checkout でセッションを開き
    ``$CLAUDE_PROJECT_DIR`` も worktree 自身のパスになるため、0.15.0 の
    プロジェクト固有除外が worktree 作業では黙って無効化されていた。"""

    def setUp(self):
        super().setUp()
        self._project_env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._project_env_patcher.start()
        self.addCleanup(self._project_env_patcher.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def test_main_repo_root_resolved_from_worktree(self):
        from _shared.patterns import _main_repo_root
        main_root, wt_root = _make_worktree(Path(self.tmp))
        self.assertEqual(_main_repo_root(str(wt_root)), str(main_root))

    def test_section_keys_include_worktree_and_main_root(self):
        from _shared.patterns import _project_section_keys
        main_root, wt_root = _make_worktree(Path(self.tmp))
        os.environ["CLAUDE_PROJECT_DIR"] = str(wt_root)
        self.assertEqual(
            _project_section_keys(str(wt_root)),
            [str(wt_root), str(main_root)],
        )

    def test_section_keys_from_cwd_walk_up_inside_worktree(self):
        """``CLAUDE_PROJECT_DIR`` が無い経路 (CLI 2.1.196 未満) でも効くこと。"""
        from _shared.patterns import _project_section_keys
        main_root, wt_root = _make_worktree(Path(self.tmp))
        sub = wt_root / "packages" / "api"
        sub.mkdir(parents=True)
        self.assertEqual(
            _project_section_keys(str(sub)), [str(wt_root), str(main_root)]
        )

    def test_normal_checkout_has_single_key(self):
        from _shared.patterns import _project_section_keys
        proj = Path(self.tmp) / "plain"
        (proj / ".git").mkdir(parents=True)
        self.assertEqual(_project_section_keys(str(proj)), [str(proj)])

    def test_submodule_git_file_is_not_treated_as_worktree(self):
        """submodule の ``.git`` もファイル (``gitdir: <super>/.git/modules/<name>``)
        なので、worktree と誤認すると project key が superproject に差し替わり
        読み込む rule が黙って変わる。

        実構成では submodule の git dir に ``commondir`` が無いため構造上
        弾かれる (``worktrees`` 要素の判定に到達しない)。その依存を明示する
        ため下の synthetic テストで guard 自体の契約も別に固定する。"""
        from _shared.patterns import _main_repo_root, _project_section_keys
        super_root = Path(self.tmp) / "super"
        (super_root / ".git" / "modules" / "sub").mkdir(parents=True)
        sub = super_root / "sub"
        sub.mkdir()
        (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
        self.assertIsNone(_main_repo_root(str(sub)))
        self.assertEqual(_project_section_keys(str(sub)), [str(sub)])

    def test_non_worktree_gitdir_rejected_even_when_commondir_resolves(self):
        """``worktrees`` 要素の guard 自体の契約 (defense-in-depth)。

        **合成 fixture** — git は submodule の git dir に ``commondir`` を
        作らないのでこの構成は現行 git では発生しない。guard が無いと
        「``.git`` がファイル + commondir が ``<x>/.git`` に解決する」だけで
        main repo root と見なしてしまうので、worktree 管理領域であることを
        要求する条件をテストで固定しておく (git のレイアウト変更で
        ``commondir`` が他の場所に現れても誤検出しない)。"""
        from _shared.patterns import _main_repo_root
        super_root = Path(self.tmp) / "super2"
        modules = super_root / ".git" / "modules" / "sub"
        modules.mkdir(parents=True)
        (modules / "commondir").write_text("../..\n")
        sub = super_root / "sub"
        sub.mkdir()
        (sub / ".git").write_text(f"gitdir: {modules}\n")
        self.assertIsNone(_main_repo_root(str(sub)))

    def test_missing_commondir_yields_single_key(self):
        from _shared.patterns import _main_repo_root
        main_root, wt_root = _make_worktree(Path(self.tmp))
        (main_root / ".git" / "worktrees" / "wt" / "commondir").unlink()
        self.assertIsNone(_main_repo_root(str(wt_root)))

    def test_bare_common_dir_is_rejected(self):
        """bare repo の worktree には「main repo の working tree」が無い。"""
        from _shared.patterns import _main_repo_root
        bare = Path(self.tmp) / "repo.git"
        admin = bare / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "commondir").write_text("../..\n")
        wt_root = Path(self.tmp) / "wt-of-bare"
        wt_root.mkdir()
        (wt_root / ".git").write_text(f"gitdir: {admin}\n")
        self.assertIsNone(_main_repo_root(str(wt_root)))

    def test_project_root_for_path_rules_stays_on_worktree(self):
        """path 形 rule の基準 root は worktree のまま (main repo root にすると
        worktree 配下のファイルが「root 配下でない」と判定され path 形 rule が
        一切効かなくなる)。"""
        from _shared.patterns import resolve_project_root
        _main_root, wt_root = _make_worktree(Path(self.tmp))
        os.environ["CLAUDE_PROJECT_DIR"] = str(wt_root)
        self.assertEqual(resolve_project_root(str(wt_root)), str(wt_root))

    def test_load_patterns_applies_main_repo_section_in_worktree(self):
        """end-to-end: main repo のパスで書いたセクションが worktree で効く。"""
        from core.patterns import load_patterns
        main_root, wt_root = _make_worktree(Path(self.tmp))
        os.environ["CLAUDE_PROJECT_DIR"] = str(wt_root)
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred(f"[project:{main_root}]\n!fixture.pem\n")
        self.assertEqual(
            load_patterns(default_file, cwd=str(wt_root)),
            [("*.pem", False), ("fixture.pem", True)],
        )

    def test_load_patterns_still_matches_worktree_own_path(self):
        """第 1 候補 (worktree 自身) の一致挙動は不変。"""
        from core.patterns import load_patterns
        _main_root, wt_root = _make_worktree(Path(self.tmp))
        os.environ["CLAUDE_PROJECT_DIR"] = str(wt_root)
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred(f"[project:{wt_root}]\n!fixture.pem\n")
        self.assertEqual(
            load_patterns(default_file, cwd=str(wt_root)),
            [("*.pem", False), ("fixture.pem", True)],
        )

    def test_unrelated_project_section_still_ignored_in_worktree(self):
        from core.patterns import load_patterns
        _main_root, wt_root = _make_worktree(Path(self.tmp))
        os.environ["CLAUDE_PROJECT_DIR"] = str(wt_root)
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred("[project:/elsewhere/repo]\n!fixture.pem\n")
        self.assertEqual(
            load_patterns(default_file, cwd=str(wt_root)),
            [("*.pem", False)],
        )


class TestProjectCommittedPatternsTier(BaseWithIsolatedHome):
    """0.32.0 (内部バックログ): repo 同梱 tier
    (``<root>/.claude/sensitive-files-guardrail/patterns.txt``) を読む。

    user 単位ファイルしか無かったため、テスト fixture / サンプルのダミー鍵を
    持つ repo では貢献者全員が毎セッション block され、CI では除外が一切
    効かなかった (各自がホーム配下に書くしかない)。"""

    def setUp(self):
        super().setUp()
        self._project_env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._project_env_patcher.start()
        self.addCleanup(self._project_env_patcher.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.proj = Path(self.tmp) / "repo"
        (self.proj / ".git").mkdir(parents=True)
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.proj)
        self.default_file = _make_default_patterns_file(
            Path(self.tmp), ["*.pem", "*.env"]
        )

    def _write_project(self, content: str) -> Path:
        d = self.proj / ".claude" / "sensitive-files-guardrail"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "patterns.txt"
        p.write_text(content)
        return p

    def _load(self):
        from core.patterns import load_patterns
        return load_patterns(self.default_file, cwd=str(self.proj))

    def test_absent_project_file_changes_nothing(self):
        self.assertEqual(self._load(), [("*.pem", False), ("*.env", False)])

    def test_exclude_line_from_repo_file_is_applied(self):
        self._write_project("!fixtures/synthetic.pem\n")
        self.assertEqual(
            self._load(),
            [("*.pem", False), ("*.env", False), ("fixtures/synthetic.pem", True)],
        )

    def test_include_line_from_repo_file_is_applied(self):
        """include 行も有効 (保護を足す方向にしか働かないため)。"""
        self._write_project("*.secret\n")
        self.assertEqual(
            self._load(),
            [("*.pem", False), ("*.env", False), ("*.secret", False)],
        )

    def test_user_tier_is_stronger_than_repo_tier(self):
        """優先順 user > project: repo が持ち込んだ除外をユーザーが打ち消せる。"""
        self._write_project("!fixtures/synthetic.pem\n")
        self._write_preferred("fixtures/synthetic.pem\n")
        rules = self._load()
        self.assertEqual(
            rules,
            [
                ("*.pem", False),
                ("*.env", False),
                ("fixtures/synthetic.pem", True),
                ("fixtures/synthetic.pem", False),
            ],
        )
        # last-match-wins なので user 側の include が勝つ
        self.assertFalse(rules[-1][1])

    def test_repo_tier_beats_default_tier(self):
        """既定 patterns.txt より後に連結される (= repo の除外が既定に勝つ)。"""
        from _shared.matcher import is_sensitive
        self._write_project("!/.env\n")
        rules = self._load()
        self.assertFalse(is_sensitive(".env", rules, root=str(self.proj)))

    def test_project_section_header_inside_repo_file_is_honored(self):
        """書式は user tier と同じ (``[project:]`` も解釈する)。"""
        self._write_project(f"[project:{self.proj}]\n!only-here.pem\n")
        self.assertEqual(
            self._load(),
            [("*.pem", False), ("*.env", False), ("only-here.pem", True)],
        )

    def test_unresolvable_project_root_skips_tier(self):
        from core.patterns import load_patterns
        lone = Path(self.tmp) / "lonely"
        lone.mkdir()
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.assertEqual(
            load_patterns(self.default_file, cwd=str(lone)),
            [("*.pem", False), ("*.env", False)],
        )

    def test_read_error_is_warned_and_tier_skipped(self):
        """FileNotFound 以外の OSError は warn_callback に委ねて空を返す
        (user tier と同じ契約)。"""
        from _shared import patterns as P
        self._write_project("!x.pem\n")
        calls: list[str] = []

        def boom(_self, *a, **k):
            raise PermissionError("nope")

        with mock.patch.object(Path, "read_text", boom):
            rules = P._load_project_patterns(
                str(self.proj), calls.append, None, [str(self.proj)], None
            )
        self.assertEqual(rules, [])
        self.assertEqual(calls, ["PermissionError"])

    def test_callback_fires_only_when_rules_were_read(self):
        from _shared import patterns as P
        seen: list[str] = []
        # ファイル無し → 呼ばれない
        P._load_project_patterns(
            str(self.proj), None, None, [str(self.proj)], seen.append
        )
        self.assertEqual(seen, [])
        # コメントだけ (rule 0 件) → 呼ばれない
        self._write_project("# just a comment\n")
        P._load_project_patterns(
            str(self.proj), None, None, [str(self.proj)], seen.append
        )
        self.assertEqual(seen, [])
        # rule あり → 固定トークンで 1 回
        self._write_project("!x.pem\n")
        P._load_project_patterns(
            str(self.proj), None, None, [str(self.proj)], seen.append
        )
        self.assertEqual(seen, [P.PROJECT_PATTERNS_IN_USE])

    def test_header_warning_fires_once_across_repo_and_user_tiers(self):
        """書き損じヘッダーの警告は tier をまたいでも種別ごとに 1 回
        (マージ前レビューの指摘)。

        警告済み集合が ``_parse_local_patterns_text`` の呼出ローカルだったため、
        repo 同梱 tier と user tier を別々にパースするようになった時点で
        「種別ごとに 1 回」の契約 (同関数の docstring) が破れ、同じ書き損じが
        両方にあると同一種別が 2 回 callback されていた。
        """
        from _shared import patterns as P
        self._write_project("[project:$CLAUDE_PROJECT_DIR]\n!repo-only.pem\n")
        self._write_preferred("[project:$CLAUDE_PROJECT_DIR]\n!user-only.pem\n")
        seen: list[str] = []
        rules = P.load_patterns(
            self.default_file,
            cwd=str(self.proj),
            header_warn_callback=seen.append,
        )
        self.assertEqual(seen, [P.PROJECT_HEADER_WARN_PLACEHOLDER])
        # 判定は変わらない (どちらのセクションも非 active のまま)
        self.assertEqual(rules, [("*.pem", False), ("*.env", False)])

    def test_header_warning_fires_once_with_legacy_user_tier(self):
        """rename 前の旧 user tier (fallback 経路) でも同じ契約。"""
        from _shared import patterns as P
        self._write_project("[project:]\n!repo-only.pem\n")
        self._write_legacy("[project:]\n!user-only.pem\n")
        seen: list[str] = []
        P.load_patterns(
            self.default_file,
            cwd=str(self.proj),
            header_warn_callback=seen.append,
        )
        self.assertEqual(seen, [P.PROJECT_HEADER_WARN_EMPTY])

    def test_distinct_header_kinds_still_warn_separately(self):
        """種別が違えばそれぞれ 1 回ずつ出る (共有集合が過剰に抑制しないこと)。"""
        from _shared import patterns as P
        self._write_project("[project:]\n!repo-only.pem\n")
        self._write_preferred("[project:$CLAUDE_PROJECT_DIR]\n!user-only.pem\n")
        seen: list[str] = []
        P.load_patterns(
            self.default_file,
            cwd=str(self.proj),
            header_warn_callback=seen.append,
        )
        self.assertEqual(
            sorted(seen),
            sorted([P.PROJECT_HEADER_WARN_EMPTY,
                    P.PROJECT_HEADER_WARN_PLACEHOLDER]),
        )

    def test_worktree_reads_the_worktree_checkout(self):
        """worktree では worktree 側の checkout を読む (commit 済みなら同内容)。"""
        from _shared.patterns import _resolve_project_patterns_path
        _main_root, wt_root = _make_worktree(Path(self.tmp), main="wtmain")
        os.environ["CLAUDE_PROJECT_DIR"] = str(wt_root)
        self.assertEqual(
            _resolve_project_patterns_path(str(wt_root)),
            wt_root / ".claude" / "sensitive-files-guardrail" / "patterns.txt",
        )


class TestProjectHeaderTildeExpansion(BaseWithIsolatedHome):
    """0.32.0 (内部バックログ): ``[project:~/…]`` を ``expanduser`` で展開する。

    展開前は ``$`` を含まないため ``_bad_header_token`` の警告にも掛からず、
    完全に無音でそのセクションが捨てられていた。"""

    def test_tilde_header_matches_expanded_path(self):
        from _shared.patterns import _parse_local_patterns_text
        proj = self.home_dir / "work" / "repo"
        text = "[project:~/work/repo]\n!foo.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, str(proj)),
            [("foo.pem", True)],
        )

    def test_tilde_header_does_not_match_unrelated_project(self):
        from _shared.patterns import _parse_local_patterns_text
        text = "[project:~/work/repo]\n!foo.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, str(self.home_dir / "other")), []
        )

    def test_absolute_header_unchanged_by_expansion(self):
        from _shared.patterns import _parse_local_patterns_text
        text = "[project:/work/repo]\n!foo.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, "/work/repo"), [("foo.pem", True)]
        )


class TestParseLocalPatternsMultipleKeys(unittest.TestCase):
    """0.32.0: ``project_key`` に key 列を渡せる (worktree の 2 候補)。"""

    def test_any_key_activates_section(self):
        from _shared.patterns import _parse_local_patterns_text
        text = "!common.pem\n[project:/x]\n!only-x.pem\n[project:/y]\n!only-y.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, ["/y", "/x"]),
            [("common.pem", True), ("only-x.pem", True), ("only-y.pem", True)],
        )

    def test_empty_sequence_behaves_like_none(self):
        from _shared.patterns import _parse_local_patterns_text
        text = "!common.pem\n[project:/x]\n!only-x.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, []), [("common.pem", True)]
        )
        self.assertEqual(
            _parse_local_patterns_text(text, []),
            _parse_local_patterns_text(text, None),
        )

    def test_empty_string_key_matches_nothing(self):
        """``[project:]`` は ``_bad_header_token`` で弾かれるので、空 key が
        空ヘッダーに一致することはない (念のため固定する)。"""
        from _shared.patterns import _parse_local_patterns_text
        text = "!common.pem\n[project:]\n!empty-header.pem\n"
        self.assertEqual(
            _parse_local_patterns_text(text, ""), [("common.pem", True)]
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
        checker_dir_on_path()
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
        checker_dir_on_path()
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

    def test_both_loaders_read_the_repo_committed_tier(self):
        """0.32.0: repo 同梱 tier も両 hook で同じ rules になり、Stop 側は
        固定トークンを stderr に出す (``claude --debug`` で追える可視化)。"""
        import io
        from contextlib import redirect_stderr

        from core.patterns import load_patterns as core_load
        checker = self._import_checker()

        proj = Path(self.tmp) / "repo"
        (proj / ".git").mkdir(parents=True)
        shared = proj / ".claude" / "sensitive-files-guardrail"
        shared.mkdir(parents=True)
        (shared / "patterns.txt").write_text("!fixtures/synthetic.pem\n")
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])

        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(proj)}):
            core_rules = core_load(default_file, cwd=str(proj))
            buf = io.StringIO()
            with redirect_stderr(buf):
                checker_rules = checker.load_patterns(default_file, cwd=str(proj))

        self.assertEqual(
            core_rules, [("*.pem", False), ("fixtures/synthetic.pem", True)]
        )
        self.assertEqual(core_rules, checker_rules)
        self.assertIn("project_patterns_in_use", buf.getvalue())

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



def _windows_like_read_text(monkey_encoding: str = "cp1252"):
    """``Path.read_text`` の **encoding 省略時**だけ非 UTF-8 locale を模擬する。

    Windows で ``PYTHONUTF8`` が無いときの ``read_text()`` 既定 (locale の
    ANSI code page、西欧なら cp1252) を再現する。``encoding`` を明示した呼出は
    そのまま通すので、本体側が encoding を明示していれば影響を受けない =
    「明示を外す mutation で落ちる」床テストになる。CPython の locale encoding
    は C 層で決まり Python から差し替えられないため、この wrapper で代用する。
    """
    original = Path.read_text

    def read_text(self, encoding=None, errors=None, *args, **kwargs):
        if encoding is None:
            encoding = monkey_encoding
        return original(self, encoding=encoding, errors=errors, *args, **kwargs)

    return mock.patch.object(Path, "read_text", read_text)


class TestPatternsFilesAreReadAsUtf8(BaseWithIsolatedHome):
    """patterns ファイルの読込は locale に依らず UTF-8 (0.34.1、内部バックログ)。

    0.34.0 の Windows CI 初回実行で、同梱 ``patterns.txt`` (日本語コメント入り)
    を ``read_text()`` が cp1252 で読もうとして ``UnicodeDecodeError`` になり、
    両 hook の**全 tool 呼出**が内部エラー → catch-all に落ちていた
    (redact 1,812 / check 79 errors の支配的原因)。``UnicodeDecodeError`` は
    ``OSError`` ではないので loader の ``except OSError`` には掛からず、
    そのまま上へ抜ける。
    """

    def test_bundled_patterns_load_under_non_utf8_locale(self):
        """同梱 patterns.txt が cp1252 既定の環境でも読める (CI 実測の再現)。"""
        from core.patterns import SHARED_PATTERNS, load_patterns
        with _windows_like_read_text():
            rules = load_patterns(SHARED_PATTERNS)
        self.assertIn((".env", False), rules)

    def test_local_rule_with_non_ascii_name_is_not_mojibaked(self):
        """cp1252 で「読めてしまう」バイト列でも文字化けせず一致に使える。

        ``秘密.txt`` の UTF-8 バイト列は cp1252 に未定義のバイトを含まないので
        例外にはならず、別の文字列 (``ç§˜å¯†.txt``) として rule に載る = その
        ファイルは保護されない。例外より見つけにくい失敗なので別に固定する。
        """
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        self._write_preferred("# ローカル追加\n秘密.txt\n")
        with _windows_like_read_text():
            rules = load_patterns(default_file)
        self.assertIn(("秘密.txt", False), rules)

    def test_default_patterns_with_japanese_comment(self):
        """既定側にも日本語コメントがあるとき、``あ`` (0x81 を含む) で落ちない。"""
        from core.patterns import load_patterns
        default_file = _make_default_patterns_file(
            Path(self.tmp), ["# あいう", "*.pem"]
        )
        with _windows_like_read_text():
            rules = load_patterns(default_file)
        self.assertEqual(rules, [("*.pem", False)])



class TestUndecodablePatternsAreReportedNotRewritten(BaseWithIsolatedHome):
    """UTF-8 として読めない patterns は**黙って書き換えず**「読めない」に倒す (0.34.1)。

    ``errors="replace"`` で読み進めると、cp932 で保存された ``秘密.txt`` の rule は
    U+FFFD の並びになって一致しなくなり、警告も出ないまま保護が消える (外部
    レビューの指摘)。decode 失敗は ``PatternsDecodeError`` (``OSError``) にして、
    既存の「読めない patterns」の扱い (既定なら全呼出元の ``patterns_unavailable``、
    追加 tier なら warn + その tier を読まない) に乗せる。
    """

    def test_user_tier_in_cp932_warns_and_is_skipped(self):
        from _shared.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        local = self._write_preferred("")
        local.write_bytes("秘密.txt\n".encode("cp932"))
        warned: list[str] = []
        rules = load_patterns(default_file, warn_callback=warned.append)
        self.assertEqual(warned, ["PatternsDecodeError"], "読めないことを警告していない")
        self.assertEqual(rules, [("*.pem", False)], "化けた rule を黙って足している")

    def test_undecodable_default_raises_an_oserror(self):
        """既定側は例外で上げる。呼出元は全て ``except OSError`` で受ける契約。"""
        from _shared.patterns import PatternsDecodeError, load_patterns
        default_file = Path(self.tmp) / "patterns.txt"
        default_file.write_bytes(b"*.pem\n\xff\xfe\n")
        with self.assertRaises(PatternsDecodeError) as ctx:
            load_patterns(default_file)
        self.assertIsInstance(ctx.exception, OSError)

    def test_leading_bom_does_not_break_the_first_rule(self):
        """Windows のメモ帳が付けうる BOM で 1 行目の rule が死なない。"""
        from _shared.patterns import load_patterns
        default_file = _make_default_patterns_file(Path(self.tmp), ["*.pem"])
        local = self._write_preferred("")
        local.write_bytes("\ufeff秘密.txt\n".encode("utf-8"))
        rules = load_patterns(default_file)
        self.assertIn(("秘密.txt", False), rules)



class TestProjectHeaderWindowsComparison(unittest.TestCase):
    """``[project:<key>]`` の比較は Windows の区切り・大文字小文字を畳む (0.34.2)。

    0.34.1 まではヘッダー側だけ ``normpath`` を通し、project key 側は生のまま
    比べていたため、Windows では ``[project:C:/work/repo]`` が ``C:\\work\\repo``
    の cwd と一致しなかった。``os.path`` を ``ntpath`` に差し替えて Windows の
    意味論で検証する (``normcase`` / ``normpath`` の流儀だけが変わる)。
    """

    def _parse(self, text, key):
        import ntpath

        from _shared import patterns
        with mock.patch.object(patterns.os, "path", ntpath):
            return patterns._parse_local_patterns_text(text, key)

    def test_forward_slash_header_matches_backslash_cwd(self):
        self.assertEqual(
            self._parse("[project:C:/work/repo]\n!x.pem\n", "C:\\work\\repo"),
            [("x.pem", True)],
        )

    def test_drive_letter_case_is_folded(self):
        """ドライブ文字は常に大文字小文字を区別しないので揃える。"""
        self.assertEqual(
            self._parse("[project:c:\\work\\repo\\]\n!x.pem\n", "C:\\work\\repo"),
            [("x.pem", True)],
        )

    def test_directory_case_is_not_folded(self):
        """ディレクトリ名の大文字小文字は畳まない (外部レビューの指摘)。

        Windows でもディレクトリ単位で大文字小文字を区別する設定があり、
        ``Repo`` と ``repo`` が別 repo になりうる。畳むと片方で承認した除外が
        他方でも効くので、一致しない (= 除外が効かない) 安全側に倒す。
        """
        self.assertEqual(
            self._parse("[project:C:\\work\\Repo]\n!x.pem\n", "C:\\work\\repo"), []
        )

    def test_other_project_does_not_match(self):
        self.assertEqual(
            self._parse("[project:C:/work/other]\n!x.pem\n", "C:\\work\\repo"), []
        )

    def test_forward_slash_project_key_matches_backslash_header(self):
        """key 側 (cwd 由来) が ``/`` 区切りでも一致する (Git Bash 等の cwd)。"""
        self.assertEqual(
            self._parse("[project:C:\\work\\repo]\n!x.pem\n", "C:/work/repo"),
            [("x.pem", True)],
        )


if __name__ == "__main__":
    unittest.main()
