"""gitscan.py のパス正規化・status スナップショット・パス単位 diff のテスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _testutil
from _testutil import git, init_repo, write

import gitscan


class GitScanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, _testutil.HERMETIC_GIT_ENV)
        self._env.start()
        self.repo = init_repo(os.path.join(self._tmp.name, "repo"))

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()


class TestLiteralPathspec(GitScanTestCase):
    """pathspec を glob として解釈させない (L2 P1: claim していないファイルの diff が混入する)。"""

    def test_bracket_name_does_not_match_sibling(self):
        write(self.repo, "app/[id]/page.tsx", "dynamic\n")
        write(self.repo, "app/i/page.tsx", "static\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "routes")
        write(self.repo, "app/[id]/page.tsx", "dynamic v2\n")
        write(self.repo, "app/i/page.tsx", "OTHER_SESSION_EDIT\n")

        diff = gitscan.path_diff(self.repo, "app/[id]/page.tsx", untracked=False, has_head=True)
        self.assertIn("dynamic v2", diff)
        self.assertNotIn("OTHER_SESSION_EDIT", diff)
        self.assertNotIn("app/i/page.tsx", diff)

    def test_bracket_named_untracked_file(self):
        write(self.repo, "app/[slug]/page.tsx", "new\n")
        write(self.repo, "app/s/page.tsx", "other\n")
        self.assertEqual(
            gitscan.untracked_among(self.repo, ["app/[slug]/page.tsx"]),
            {"app/[slug]/page.tsx"},
        )
        diff = gitscan.path_diff(self.repo, "app/[slug]/page.tsx", untracked=True, has_head=True)
        self.assertIn("+new", diff)
        self.assertNotIn("other", diff)

    def test_glob_looking_entry_matches_nothing(self):
        """旧 state の `[.]env` / `?env` / `*` が tracked の `.env` を拾わない。"""
        write(self.repo, ".env", "A=1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "env")
        write(self.repo, ".env", "A=sk-live-LEAK\n")
        write(self.repo, "fresh.txt", "x\n")
        for rel in ("[.]env", "?env", "*", "src/*"):
            with self.subTest(rel=rel):
                self.assertEqual(
                    gitscan.path_diff(self.repo, rel, untracked=False, has_head=True), ""
                )
                self.assertEqual(
                    gitscan.path_diff(self.repo, rel, untracked=True, has_head=True), ""
                )
        self.assertEqual(gitscan.untracked_among(self.repo, ["*", "[f]resh.txt"]), set())


class TestSymlinkMap(GitScanTestCase):
    """repo 内 symlink の列挙 (tracked は index、untracked は scandir)。"""

    def _link(self, rel: str, target_rel: str) -> None:
        full = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        os.symlink(os.path.join(self.repo, target_rel), full)

    def test_repo_without_symlinks_is_empty(self):
        write(self.repo, "ordinary/data.json", "{}\n")
        self.assertEqual(gitscan.symlink_map(self.repo), {})

    def test_tracked_symlink_dir_is_listed(self):
        write(self.repo, "ordinary/data.json", "{}\n")
        self._link("credentials", "ordinary")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "link")
        self.assertEqual(gitscan.symlink_map(self.repo), {"credentials": "ordinary"})

    def test_untracked_symlink_dir_is_listed(self):
        write(self.repo, "ordinary/data.json", "{}\n")
        self._link("credentials", "ordinary")
        self.assertEqual(gitscan.symlink_map(self.repo), {"credentials": "ordinary"})

    def test_file_symlink_and_nested_symlink(self):
        write(self.repo, "vault/c.json", "{}\n")
        write(self.repo, "ordinary/data.json", "{}\n")
        self._link("credentials.json", "vault/c.json")
        self._link("config/secrets", "ordinary")
        self.assertEqual(
            gitscan.symlink_map(self.repo),
            {"credentials.json": "vault/c.json", "config/secrets": "ordinary"},
        )

    def test_symlink_to_outside_is_ignored(self):
        outside = os.path.join(self._tmp.name, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.repo, "credentials"))
        self.assertEqual(gitscan.symlink_map(self.repo), {})

    def test_tracked_symlink_beyond_scan_depth_is_still_found(self):
        write(self.repo, "ordinary/data.json", "{}\n")
        deep = "a/b/c/d/credentials"
        self._link(deep, "ordinary")
        self.assertEqual(gitscan.symlink_map(self.repo), {}, "untracked は深さ上限で見ない")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "deep link")
        self.assertEqual(gitscan.symlink_map(self.repo), {deep: "ordinary"})

    def test_caps_limit_the_scan(self):
        write(self.repo, "ordinary/data.json", "{}\n")
        for i in range(6):
            self._link(f"link{i}", "ordinary")
        with mock.patch.object(gitscan, "MAX_SYMLINKS", 2):
            self.assertEqual(len(gitscan.symlink_map(self.repo)), 2)
        with mock.patch.object(gitscan, "SYMLINK_SCAN_BUDGET", 1):
            self.assertLessEqual(len(gitscan.symlink_map(self.repo)), 1)


class TestNoColor(GitScanTestCase):
    def test_diff_ignores_color_config(self):
        """`color.ui=always` でも ANSI を混ぜない (hash とバイト予算が狂う)。"""
        git(self.repo, "config", "color.ui", "always")
        git(self.repo, "config", "color.diff", "always")
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        write(self.repo, "fresh.txt", "x\n")
        tracked = gitscan.path_diff(self.repo, "seed.txt", untracked=False, has_head=True)
        untracked = gitscan.path_diff(self.repo, "fresh.txt", untracked=True, has_head=True)
        self.assertIn("+BETA", tracked)
        self.assertNotIn("\x1b[", tracked)
        self.assertIn("+x", untracked)
        self.assertNotIn("\x1b[", untracked)


class TestWorktreeRoot(GitScanTestCase):
    def test_returns_realpath(self):
        root = gitscan.worktree_root(self.repo)
        self.assertEqual(root, os.path.realpath(self.repo))

    def test_symlinked_cwd_resolves(self):
        """macOS の /tmp -> /private/tmp のように cwd が symlink 経由でも解決すること。"""
        link = os.path.join(self._tmp.name, "link")
        os.symlink(self.repo, link)
        self.assertEqual(gitscan.worktree_root(link), os.path.realpath(self.repo))

    def test_outside_repo_returns_none(self):
        outside = os.path.join(self._tmp.name, "plain")
        os.makedirs(outside)
        self.assertIsNone(gitscan.worktree_root(outside))


class TestToRelative(GitScanTestCase):
    def test_inside_path(self):
        target = os.path.join(self.repo, "pkg", "mod.py")
        self.assertEqual(gitscan.to_relative(self.repo, target), "pkg/mod.py")

    def test_symlinked_input_path(self):
        link = os.path.join(self._tmp.name, "link")
        os.symlink(self.repo, link)
        self.assertEqual(gitscan.to_relative(self.repo, f"{link}/a.py"), "a.py")

    def test_outside_path_is_none(self):
        outside = os.path.join(self._tmp.name, "outside.txt")
        self.assertIsNone(gitscan.to_relative(self.repo, outside))

    def test_sibling_prefix_is_not_inside(self):
        """`/repo` と `/repo-other` を素の startswith で誤判定しないこと。"""
        sibling = self.repo + "-other/x.py"
        self.assertIsNone(gitscan.to_relative(self.repo, sibling))

    def test_root_itself_is_none(self):
        self.assertIsNone(gitscan.to_relative(self.repo, self.repo))

    def test_empty_path(self):
        self.assertIsNone(gitscan.to_relative(self.repo, ""))


class TestStatusSnapshot(GitScanTestCase):
    def test_untracked_files_are_listed_individually(self):
        """`-uall` を使わないと新規ディレクトリが `dir/` に畳まれて個別に拾えない。"""
        write(self.repo, "newdir/deep/file.txt", "x\n")
        snapshot = gitscan.status_snapshot(self.repo)
        self.assertIn("newdir/deep/file.txt", snapshot)

    def test_modified_file_records_size_and_mtime(self):
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        snapshot = gitscan.status_snapshot(self.repo)
        code, size, mtime = snapshot["seed.txt"]
        self.assertIn("M", code)
        self.assertEqual(size, os.path.getsize(os.path.join(self.repo, "seed.txt")))
        self.assertGreater(mtime, 0)

    def test_clean_repo_is_empty(self):
        self.assertEqual(gitscan.status_snapshot(self.repo), {})

    def test_nested_git_repo_is_skipped(self):
        """入れ子の git リポジトリは `-uall` でも `dir/` のまま返る。

        本番で `.claude/worktrees/<name>/` が pending に混入したケースの回帰テスト。
        中身は別リポジトリの変更なのでレビュー対象にしてはならない。
        """
        inner = os.path.join(self.repo, ".claude", "worktrees", "inner")
        os.makedirs(inner)
        git(inner, "init", "-q")
        write(inner, "file.txt", "belongs to another repo\n")

        raw = gitscan._git(
            self.repo, ["status", "--porcelain", "-z", "--untracked-files=all"]
        )
        entries = [rel for _code, rel in gitscan._parse_porcelain_z(
            gitscan._decode(raw.stdout)
        )]
        self.assertIn(
            ".claude/worktrees/inner/", entries, "git が dir/ を返す前提が崩れている"
        )

        snapshot = gitscan.status_snapshot(self.repo)
        self.assertEqual(
            snapshot, {}, "入れ子 repo のディレクトリを snapshot に入れてはいけない"
        )

    def test_nested_repo_does_not_hide_sibling_files(self):
        """入れ子 repo を除外しても、同階層の通常ファイルは拾うこと。"""
        inner = os.path.join(self.repo, "vendor", "inner")
        os.makedirs(inner)
        git(inner, "init", "-q")
        write(self.repo, "vendor/notes.md", "mine\n")

        snapshot = gitscan.status_snapshot(self.repo)
        self.assertIn("vendor/notes.md", snapshot)
        self.assertNotIn("vendor/inner/", snapshot)

    def test_rename_entry_does_not_shift_parsing(self):
        """rename エントリは元パスが余分なトークンとして続く — 読み飛ばし忘れると崩れる。

        読み飛ばさないと元パス `seed.txt` 自体がエントリとして解釈され、
        `code="se" / path="d.txt"` のような実在しないパスが混入する。
        """
        git(self.repo, "mv", "seed.txt", "renamed.txt")
        write(self.repo, "other.txt", "o\n")
        snapshot = gitscan.status_snapshot(self.repo)
        self.assertEqual(
            set(snapshot),
            {"renamed.txt", "other.txt"},
            "rename の元パストークンからゴミエントリが生えていないこと",
        )


class TestStatusSnapshotFailure(GitScanTestCase):
    """bd_092a232e-zh5.7: 失敗/不完全は {} ではなく None (比較不能) を返す。"""

    def test_git_failure_returns_none(self):
        with mock.patch.object(gitscan, "_git", return_value=None):
            self.assertIsNone(gitscan.status_snapshot(self.repo))

    def test_nonzero_exit_returns_none(self):
        fake = mock.Mock(returncode=1, stdout=b"")
        with mock.patch.object(gitscan, "_git", return_value=fake):
            self.assertIsNone(gitscan.status_snapshot(self.repo))

    def test_truncated_snapshot_returns_none(self):
        """MAX_SNAPSHOT_ENTRIES に収まらない (打ち切りが起きる) なら None。"""
        for i in range(3):
            write(self.repo, f"many{i}.txt", "x\n")
        with mock.patch.object(gitscan, "MAX_SNAPSHOT_ENTRIES", 1):
            self.assertIsNone(gitscan.status_snapshot(self.repo))

    def test_exactly_at_cap_is_not_treated_as_truncated(self):
        """総エントリ数が上限とちょうど一致する場合は打ち切りではないので通常どおり返す。"""
        for i in range(2):
            write(self.repo, f"many{i}.txt", "x\n")
        with mock.patch.object(gitscan, "MAX_SNAPSHOT_ENTRIES", 2):
            snapshot = gitscan.status_snapshot(self.repo)
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot), 2)


class TestChangedBetween(GitScanTestCase):
    def test_detects_new_modified_and_vanished(self):
        pre = {"a.txt": ["??", 3, 100], "b.txt": [" M", 5, 200]}
        post = {"a.txt": ["??", 3, 100], "b.txt": [" M", 5, 999], "c.txt": ["??", 1, 1]}
        self.assertEqual(gitscan.changed_between(pre, post), ["b.txt", "c.txt"])

    def test_detects_removal_from_status(self):
        pre = {"a.txt": [" M", 3, 100]}
        self.assertEqual(gitscan.changed_between(pre, {}), ["a.txt"])

    def test_identical_snapshots(self):
        snap = {"a.txt": [" M", 3, 100]}
        self.assertEqual(gitscan.changed_between(snap, dict(snap)), [])

    def test_same_status_code_but_new_mtime(self):
        """すでに dirty なファイルの上書き — status 行は不変で mtime だけ動く。"""
        pre = {"seed.txt": [" M", 18, 111]}
        post = {"seed.txt": [" M", 18, 222]}
        self.assertEqual(gitscan.changed_between(pre, post), ["seed.txt"])


class TestPathDiff(GitScanTestCase):
    def test_tracked_modification(self):
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        diff = gitscan.path_diff(self.repo, "seed.txt", untracked=False, has_head=True)
        self.assertIn("-beta", diff)
        self.assertIn("+BETA", diff)

    def test_untracked_file(self):
        write(self.repo, "fresh.txt", "brand new\n")
        diff = gitscan.path_diff(self.repo, "fresh.txt", untracked=True, has_head=True)
        self.assertIn("+brand new", diff)

    def test_unchanged_tracked_file_is_empty(self):
        diff = gitscan.path_diff(self.repo, "seed.txt", untracked=False, has_head=True)
        self.assertEqual(diff, "")

    def test_repo_without_head_uses_staged_diff(self):
        fresh = os.path.join(self._tmp.name, "fresh-repo")
        os.makedirs(fresh)
        git(fresh, "init", "-q")
        git(fresh, "config", "user.email", "t@example.com")
        git(fresh, "config", "user.name", "t")
        write(fresh, "first.txt", "hello\n")
        git(fresh, "add", "-A")

        self.assertFalse(gitscan.head_exists(fresh))
        diff = gitscan.path_diff(fresh, "first.txt", untracked=False, has_head=False)
        self.assertIn("+hello", diff)


class TestUntrackedAmong(GitScanTestCase):
    def test_separates_tracked_and_untracked(self):
        write(self.repo, "seed.txt", "changed\n")
        write(self.repo, "fresh.txt", "new\n")
        result = gitscan.untracked_among(self.repo, ["seed.txt", "fresh.txt"])
        self.assertEqual(result, {"fresh.txt"})

    def test_gitignored_file_is_not_untracked(self):
        write(self.repo, ".gitignore", "ignored.txt\n")
        write(self.repo, "ignored.txt", "secret\n")
        self.assertEqual(gitscan.untracked_among(self.repo, ["ignored.txt"]), set())

    def test_empty_input(self):
        self.assertEqual(gitscan.untracked_among(self.repo, []), set())


if __name__ == "__main__":
    unittest.main()
