"""`core/paths.py` の解決ロジック (3-tier lookup + 親ディレクトリ遡及) のテスト。

特に **遡及の停止条件** (git repo toplevel / `$HOME`) を固定する。従来は
階層数 (`ANCESTOR_SEARCH_MAX_LEVELS`) だけが上限だったため、
`/Users/<u>/dev/<org>/<repo>` のような配置では 5 階層で `$HOME` に届き、
無関係な `~/.claude/accounts.json` を継承して検証していた (内部バックログ)。
"""
from __future__ import annotations

import json
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
