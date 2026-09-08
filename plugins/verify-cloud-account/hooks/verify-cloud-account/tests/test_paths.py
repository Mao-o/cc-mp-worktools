"""`core/paths.py` の解決ロジック (3-tier lookup + 親ディレクトリ遡及) のテスト。

特に **遡及の停止条件** (git repo toplevel / `$HOME`) を固定する。従来は
階層数 (`ANCESTOR_SEARCH_MAX_LEVELS`) だけが上限だったため、
`/Users/<u>/dev/<org>/<repo>` のような配置では 5 階層で `$HOME` に届き、
無関係な `~/.claude/accounts.json` を継承して検証していた (内部バックログ)。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import paths  # noqa: E402


class BaseAncestorBoundary(unittest.TestCase):
    def setUp(self):
        import shutil
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # 既定の `$HOME` は fixture と無関係な別ディレクトリに固定する
        # (fixture の中に置くと `$HOME` 境界が先に効いてしまい、repo toplevel
        # 側の判定を測れない)。実 HOME の配置にも依存しなくなる。
        fake_home = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: shutil.rmtree(fake_home, ignore_errors=True))
        self._set_home(fake_home)

    def _set_home(self, home: Path):
        home.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(Path, "home", staticmethod(lambda: home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_new(self, directory: Path, data: dict | None = None):
        d = directory / ".claude" / "verify-cloud-account"
        d.mkdir(parents=True, exist_ok=True)
        (d / "accounts.local.json").write_text(
            json.dumps(data or {"github": "u"}), encoding="utf-8"
        )
        return d / "accounts.local.json"

    def _write_legacy(self, directory: Path, data: dict | None = None):
        d = directory / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        (d / "accounts.json").write_text(
            json.dumps(data or {"github": "u"}), encoding="utf-8"
        )
        return d / "accounts.json"

    def _resolved_dir(self, start: Path):
        _found, resolved = paths.discover_accounts_files_with_ancestors(str(start))
        return resolved


class TestAncestorStopsAtRepoToplevel(BaseAncestorBoundary):
    """git repo の toplevel を越えて上らない (その階層自身は探す)。"""

    def setUp(self):
        super().setUp()
        self.outer = self.tmp / "outer"
        self.repo = self.outer / "repo"
        self.sub = self.repo / "src" / "pkg"
        self.sub.mkdir(parents=True)
        (self.repo / ".git").mkdir()

    def test_does_not_inherit_from_above_repo_toplevel(self):
        """repo の外 (親コレクションディレクトリ) の設定は拾わない。"""
        self._write_new(self.outer)
        self.assertIsNone(self._resolved_dir(self.sub))

    def test_repo_toplevel_itself_is_searched(self):
        """toplevel は「越えない」だけで探索対象からは外さない。"""
        self._write_new(self.repo)
        self.assertEqual(self._resolved_dir(self.sub), self.repo)

    def test_worktree_dot_git_file_does_not_stop_search(self):
        """linked worktree (`.git` がファイル) は親 repo まで上れる。

        worktree に accounts.local.json を複製しない運用 (README の
        「親ディレクトリ遡及」) を壊さないための境界。
        """
        worktree = self.repo / ".worktrees" / "feature-x"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(
            f"gitdir: {self.repo}/.git/worktrees/feature-x\n", encoding="utf-8"
        )
        self._write_new(self.repo)
        self.assertEqual(self._resolved_dir(worktree), self.repo)

    def test_start_dir_is_searched_even_if_it_is_toplevel(self):
        self._write_new(self.repo)
        self.assertEqual(self._resolved_dir(self.repo), self.repo)


class TestDotGitFileIsClassified(BaseAncestorBoundary):
    """`.git` **ファイル**は種別で扱いが変わる (マージ前レビューの指摘)。

    ファイル形を一律に通過扱いすると、submodule をプロジェクトとして起動した
    ときに探索が superproject へ続き、**未設定の submodule で状態変更コマンドが
    repo 境界で fail-closed せずに allow される**。linked worktree だけを通し、
    submodule と判読不能な内容は境界として止める。

    種別は gitdir の**末尾 2 要素** (`worktrees/<name>` / `modules/<name>`) で
    決まり、common directory の名前には依存しない。
    """

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir(parents=True)
        (self.repo / ".git").mkdir()
        # superproject 側にだけ設定がある状態を作る
        self._write_new(self.repo, {"github": "super"})

    def _child_with_dot_git_file(self, name: str, content: str) -> Path:
        child = self.repo / name
        child.mkdir(parents=True, exist_ok=True)
        (child / ".git").write_text(content, encoding="utf-8")
        return child

    # --- (a) submodule は境界 -------------------------------------------------

    def test_submodule_does_not_inherit_superproject(self):
        sub = self._child_with_dot_git_file("sub", "gitdir: ../.git/modules/sub\n")
        self.assertIsNone(self._resolved_dir(sub))

    def test_submodule_boundary_applies_from_subdirectory(self):
        """submodule 配下の作業ディレクトリからでも submodule root で止まる。"""
        sub = self._child_with_dot_git_file("sub", "gitdir: ../.git/modules/sub\n")
        inner = sub / "src" / "pkg"
        inner.mkdir(parents=True)
        self.assertIsNone(self._resolved_dir(inner))

    def test_submodule_root_itself_is_searched(self):
        """境界は「越えない」だけで、その階層自身は探索対象。"""
        sub = self._child_with_dot_git_file("sub", "gitdir: ../.git/modules/sub\n")
        self._write_new(sub, {"github": "sub"})
        self.assertEqual(self._resolved_dir(sub), sub)

    def test_submodule_with_absolute_gitdir_is_boundary(self):
        sub = self._child_with_dot_git_file(
            "sub", f"gitdir: {self.repo}/.git/modules/sub\n"
        )
        self.assertIsNone(self._resolved_dir(sub))

    def test_nested_submodule_is_boundary(self):
        sub = self._child_with_dot_git_file(
            "sub", "gitdir: ../../.git/modules/outer/modules/inner\n"
        )
        self.assertIsNone(self._resolved_dir(sub))

    def test_submodule_with_windows_separators_is_boundary(self):
        """区切りが `\\` でも判定は変わらない (OS 非依存に分解する)。"""
        sub = self._child_with_dot_git_file(
            "sub", "gitdir: ..\\.git\\modules\\sub\n"
        )
        self.assertIsNone(self._resolved_dir(sub))

    # --- (b) linked worktree は従来どおり通過 --------------------------------

    def test_linked_worktree_still_inherits(self):
        worktree = self._child_with_dot_git_file(
            "wt", f"gitdir: {self.repo}/.git/worktrees/wt\n"
        )
        self.assertEqual(self._resolved_dir(worktree), self.repo)

    def test_linked_worktree_with_relative_gitdir_still_inherits(self):
        worktree = self._child_with_dot_git_file(
            "wt", "gitdir: ../.git/worktrees/wt\n"
        )
        self.assertEqual(self._resolved_dir(worktree), self.repo)

    def test_linked_worktree_with_windows_separators_still_inherits(self):
        worktree = self._child_with_dot_git_file(
            "wt", "gitdir: ..\\.git\\worktrees\\wt\n"
        )
        self.assertEqual(self._resolved_dir(worktree), self.repo)

    def test_worktree_of_submodule_inherits_from_its_submodule_root(self):
        """submodule の linked worktree は最後のキーワードが `worktrees`。

        通過先は **その worktree を持つ submodule** 側 (common dir が
        `<super>/.git/modules/sub` で一致する階層)。
        """
        sub = self._child_with_dot_git_file(
            "sub", f"gitdir: {self.repo}/.git/modules/sub\n"
        )
        self._write_new(sub, {"github": "sub"})
        worktree = sub / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {self.repo}/.git/modules/sub/worktrees/wt\n", encoding="utf-8"
        )
        self.assertEqual(self._resolved_dir(worktree), sub)

    def test_worktree_of_submodule_does_not_inherit_superproject(self):
        """submodule の worktree を superproject 直下に置いても継承しない。

        common dir (`<super>/.git/modules/sub`) は superproject の git dir
        (`<super>/.git`) と別物なので、所属を確立できず worktree root で止まる。
        """
        worktree = self._child_with_dot_git_file(
            "wt", f"gitdir: {self.repo}/.git/modules/sub/worktrees/wt\n"
        )
        self.assertIsNone(self._resolved_dir(worktree))

    def test_bare_repo_linked_worktree_inherits_from_owning_checkout(self):
        """bare repository から作った worktree (common dir が `<name>.git`)。

        `git --git-dir=/path/repo.git worktree add ...` の gitdir は
        `/path/repo.git/worktrees/<name>` で、パス中に `.git` という**要素**が
        現れない。`.git` を要求すると正当な worktree が境界に落ち、その repo の
        accounts.local.json を継承できなくなる (マージ前レビューの指摘)。

        祖先側は同じ common dir を指す `.git` ファイルを持つ階層。
        """
        common = self.tmp / "store" / "repo.git"
        host = self._child_with_dot_git_file("host", f"gitdir: {common}\n")
        self._write_new(host, {"github": "host"})
        worktree = host / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {common}/worktrees/wt\n", encoding="utf-8"
        )
        self.assertEqual(self._resolved_dir(worktree), host)

    def test_separate_git_dir_linked_worktree_inherits_from_owning_checkout(self):
        """`--separate-git-dir` で初期化した repo から作った worktree。

        common dir が `.git` と無関係な名前 (`/custom/gitdir`) になる。main
        worktree 側の `.git` ファイルは同じ common dir を直接指す。
        """
        common = self.tmp / "custom" / "gitdir"
        host = self._child_with_dot_git_file("host", f"gitdir: {common}\n")
        self._write_new(host, {"github": "host"})
        worktree = host / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {common}/worktrees/wt\n", encoding="utf-8"
        )
        self.assertEqual(self._resolved_dir(worktree), host)

    def test_bare_repo_submodule_is_still_a_boundary(self):
        """common dir 名に依存しないのは submodule 側も同じ。"""
        sub = self._child_with_dot_git_file(
            "sub", f"gitdir: {self.tmp}/custom/gitdir/modules/sub\n"
        )
        self.assertIsNone(self._resolved_dir(sub))

    # --- (c) 判読できない `.git` ファイルは停止側 (fail-closed) --------------

    def test_empty_dot_git_file_is_boundary(self):
        child = self._child_with_dot_git_file("odd", "")
        self.assertIsNone(self._resolved_dir(child))

    def test_dot_git_file_without_gitdir_prefix_is_boundary(self):
        child = self._child_with_dot_git_file("odd", "ref: refs/heads/main\n")
        self.assertIsNone(self._resolved_dir(child))

    def test_gitdir_without_dot_git_component_is_boundary(self):
        """common dir を直接指す形 (`--separate-git-dir` の main worktree) は境界。

        `worktrees/` / `modules/` レイアウトでない = repo 本体の toplevel なので
        止めてよい。linked worktree だけが `<common>/worktrees/<name>` になる。
        """
        child = self._child_with_dot_git_file(
            "odd", f"gitdir: {self.tmp}/elsewhere/store\n"
        )
        self.assertIsNone(self._resolved_dir(child))

    def test_gitdir_with_unknown_keyword_is_boundary(self):
        child = self._child_with_dot_git_file("odd", "gitdir: ../.git/objects/x\n")
        self.assertIsNone(self._resolved_dir(child))

    def test_gitdir_pointing_at_dot_git_itself_is_boundary(self):
        child = self._child_with_dot_git_file("odd", "gitdir: ../.git\n")
        self.assertIsNone(self._resolved_dir(child))

    def test_binary_dot_git_file_is_boundary(self):
        child = self.repo / "odd"
        child.mkdir(parents=True, exist_ok=True)
        (child / ".git").write_bytes(b"\xff\xfe\x00\x01")
        self.assertIsNone(self._resolved_dir(child))

    def test_unreadable_dot_git_file_is_boundary(self):
        """内容を読めない場合も境界 (分からないなら止める)。"""
        child = self._child_with_dot_git_file(
            "wt", f"gitdir: {self.repo}/.git/worktrees/wt\n"
        )
        real_open = Path.open

        def fake_open(self_path, *args, **kwargs):
            if self_path.name == ".git":
                raise OSError("unreadable")
            return real_open(self_path, *args, **kwargs)

        with mock.patch.object(Path, "open", fake_open):
            self.assertIsNone(self._resolved_dir(child))


class TestLinkedWorktreeOwnership(BaseAncestorBoundary):
    """linked worktree は「その worktree を持つ repo」の側にしか上らない。

    正当な linked worktree は無関係な repo の中にも置ける (repo A の
    `repo-a/vendor/b-wt` に repo B の worktree を追加する形)。gitdir の形だけで
    通過させると探索が repo B を離れ、**repo A の accounts.local.json を継承**
    する。repo A の期待アカウントが active session と一致すれば、未設定の
    repo B worktree で状態変更コマンドが allow される (マージ前レビューの指摘)。

    通過の条件は「gitdir の common dir が、この後探索する祖先 repo のものと
    一致すること」。確立できなければ worktree root で止める (fail-closed)。
    """

    def setUp(self):
        super().setUp()
        self.repo_a = self.tmp / "repo-a"
        self.repo_b = self.tmp / "repo-b"
        for repo in (self.repo_a, self.repo_b):
            (repo / ".git").mkdir(parents=True)

    def _worktree(self, path: Path, gitdir: str) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        return path

    # --- (a) 無関係な repo の中に置かれた worktree ---------------------------

    def test_worktree_in_unrelated_repo_does_not_inherit_that_repo(self):
        self._write_new(self.repo_a, {"github": "repo-a"})
        worktree = self._worktree(
            self.repo_a / "vendor" / "b-wt",
            f"{self.repo_b}/.git/worktrees/b-wt",
        )
        self.assertIsNone(self._resolved_dir(worktree))

    def test_worktree_in_unrelated_repo_does_not_inherit_intermediate_dir(self):
        """外側 repo の中間ディレクトリ (repo ではない階層) も対象外。

        worktree root で止めるため、`repo-a/vendor/` に置かれた設定にも届かない。
        """
        self._write_new(self.repo_a / "vendor", {"github": "vendor"})
        worktree = self._worktree(
            self.repo_a / "vendor" / "b-wt",
            f"{self.repo_b}/.git/worktrees/b-wt",
        )
        self.assertIsNone(self._resolved_dir(worktree))

    def test_worktree_root_itself_is_still_searched(self):
        """境界は「越えない」だけ。worktree 自身の設定は従来どおり使う。"""
        worktree = self._worktree(
            self.repo_a / "vendor" / "b-wt",
            f"{self.repo_b}/.git/worktrees/b-wt",
        )
        self._write_new(worktree, {"github": "b-wt"})
        self.assertEqual(self._resolved_dir(worktree), worktree)

    def test_ancestor_with_unreadable_dot_git_is_a_boundary(self):
        """祖先の `.git` が判読できない = 所属を比較できない → 止める。"""
        host = self.repo_a / "host"
        host.mkdir()
        (host / ".git").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self._write_new(host, {"github": "host"})
        worktree = self._worktree(
            host / "wt", f"{self.tmp}/custom/gitdir/worktrees/wt"
        )
        self.assertIsNone(self._resolved_dir(worktree))

    # --- (b) 自分の repo に属する worktree は従来どおり継承 ------------------

    def test_worktree_inside_its_own_repo_inherits(self):
        self._write_new(self.repo_b, {"github": "repo-b"})
        worktree = self._worktree(
            self.repo_b / ".worktrees" / "wt", f"{self.repo_b}/.git/worktrees/wt"
        )
        self.assertEqual(self._resolved_dir(worktree), self.repo_b)

    def test_worktree_outside_its_repo_inherits_from_workspace(self):
        """repo の外に置いた worktree (`git worktree add ../wt` の通常配置)。

        祖先に repo が 1 つも無ければ継承元を取り違えようがないため、workspace
        直下の設定を従来どおり継承する。
        """
        workspace = self.tmp / "ws"
        repo = workspace / "repo"
        (repo / ".git").mkdir(parents=True)
        self._write_new(workspace, {"github": "ws"})
        worktree = self._worktree(
            workspace / "wt", f"{repo}/.git/worktrees/wt"
        )
        self.assertEqual(self._resolved_dir(worktree), workspace)

    def test_nested_worktree_of_same_repo_inherits(self):
        """祖先自身が linked worktree でも、同じ common dir なら通過する。"""
        outer = self._worktree(
            self.repo_b / ".worktrees" / "wt1", f"{self.repo_b}/.git/worktrees/wt1"
        )
        self._write_new(outer, {"github": "wt1"})
        inner = self._worktree(
            outer / "inner", f"{self.repo_b}/.git/worktrees/inner"
        )
        self.assertEqual(self._resolved_dir(inner), outer)


class TestAncestorStopsAtHome(BaseAncestorBoundary):
    """`$HOME` およびその上へは上らない。"""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.project = self.home / "dev" / "org" / "proj"
        self.project.mkdir(parents=True)
        self._set_home(self.home)

    def test_does_not_inherit_home_new_path(self):
        self._write_new(self.home)
        self.assertIsNone(self._resolved_dir(self.project))

    def test_does_not_inherit_home_legacy_accounts_json(self):
        """起票時の具体例: `~/.claude/accounts.json` を拾わない。"""
        self._write_legacy(self.home)
        self.assertIsNone(self._resolved_dir(self.project))

    def test_does_not_inherit_from_above_home_when_project_is_outside_home(self):
        """`$HOME` の外にある project から、`$HOME` より上の階層を拾わない。"""
        outside = self.tmp / "other" / "proj"
        outside.mkdir(parents=True)
        self._write_new(self.tmp)
        self.assertIsNone(self._resolved_dir(outside))

    def test_intermediate_dir_below_home_is_still_searched(self):
        """`$HOME` 未満の中間ディレクトリからの継承は従来どおり有効。"""
        self._write_new(self.home / "dev")
        self.assertEqual(self._resolved_dir(self.project), self.home / "dev")

    def test_home_itself_as_project_dir_is_searched(self):
        """`$HOME` をプロジェクトにしている場合、開始階層なので探す。"""
        self._write_new(self.home)
        self.assertEqual(self._resolved_dir(self.home), self.home)


class TestAncestorSearchUnchangedBehaviour(BaseAncestorBoundary):
    """境界追加で壊してはいけない従来挙動。"""

    def test_cwd_takes_priority_over_ancestor(self):
        parent = self.tmp / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        self._write_new(parent, {"github": "parent"})
        self._write_new(child, {"github": "child"})
        self.assertEqual(self._resolved_dir(child), child)

    def test_nothing_anywhere_returns_none(self):
        deep = self.tmp / "a" / "b" / "c"
        deep.mkdir(parents=True)
        self.assertIsNone(self._resolved_dir(deep))

    def test_max_levels_still_applies(self):
        deep = self.tmp
        for name in ("l1", "l2", "l3"):
            deep = deep / name
        deep.mkdir(parents=True)
        self._write_new(self.tmp)
        self.assertEqual(self._resolved_dir(deep), self.tmp)
        _found, resolved = paths.discover_accounts_files_with_ancestors(
            str(deep), max_levels=2
        )
        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()


class TestWindowsAbsoluteGitdir(unittest.TestCase):
    """ドライブ文字 / UNC の gitdir は相対として directory に繋がない。"""

    def _resolve(self, raw: str) -> Path | None:
        from core import paths

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        target = paths._resolve_gitdir_value(directory, raw, paths._split_path_components(raw))
        self.assertIsNotNone(target)
        assert target is not None
        self.assertFalse(
            str(target).startswith(str(directory.resolve())),
            f"絶対パスを {directory} に繋いでいる: {target}",
        )
        return target

    def test_drive_letter_path_is_not_joined_onto_directory(self):
        self._resolve("C:/repo/.git/worktrees/wt")

    def test_backslash_drive_letter_path_is_not_joined_onto_directory(self):
        self._resolve("C:\\repo\\.git\\worktrees\\wt")

    def test_unc_path_is_not_joined_onto_directory(self):
        self._resolve("//server/share/repo/.git/worktrees/wt")
