"""gitscan.py のパス正規化・status スナップショット・パス単位 diff のテスト。"""
from __future__ import annotations

import os
import tempfile
import time
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
        _testutil.scrub_outer_git_env()
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


class TestToRelativeCaseOnlyAlias(GitScanTestCase):
    """大文字小文字だけ違う名前を、ディスク上の (index と同じ) 綴りの root 相対にする。"""

    def setUp(self) -> None:
        super().setUp()
        write(self.repo, "pkg/mod.py", "x\n")
        if not os.path.exists(os.path.join(self.repo, "PKG", "MOD.py")):
            self.skipTest("大文字小文字を区別する FS では同じ実体を指す別名にならない")
        self.upper_root = os.path.join(os.path.dirname(self.repo), "REPO")

    def test_file_and_directory_parts(self):
        self.assertEqual(gitscan.to_relative(self.repo, os.path.join(self.repo, "PKG", "Mod.py")),
                         "pkg/mod.py")

    def test_root_written_in_another_case(self):
        self.assertEqual(gitscan.to_relative(self.upper_root, os.path.join(self.repo, "pkg/mod.py")),
                         "pkg/mod.py")
        self.assertEqual(gitscan.to_relative(self.repo, os.path.join(self.upper_root, "pkg/mod.py")),
                         "pkg/mod.py")

    def test_root_itself_in_another_case_is_none(self):
        self.assertIsNone(gitscan.to_relative(self.repo, self.upper_root))
        self.assertIsNone(gitscan.to_relative(self.upper_root, self.repo))

    def test_case_only_diff_reaches_the_tracked_file(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "seed")
        write(self.repo, "pkg/mod.py", "y\n")
        rel = gitscan.to_relative(self.repo, os.path.join(self.repo, "pkg", "MOD.PY"))
        diff = gitscan.path_diff(self.repo, rel, untracked=False, has_head=True)
        self.assertIn("+y", diff or "")


class TestStatusSnapshot(GitScanTestCase):
    def test_untracked_files_are_listed_individually(self):
        """`-uall` を使わないと新規ディレクトリが `dir/` に畳まれて個別に拾えない。"""
        write(self.repo, "newdir/deep/file.txt", "x\n")
        snapshot = gitscan.status_snapshot(self.repo)
        self.assertIn("newdir/deep/file.txt", snapshot)

    def test_modified_file_records_size_mtime_and_ctime(self):
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        snapshot = gitscan.status_snapshot(self.repo)
        code, size, mtime, ctime = snapshot["seed.txt"]
        self.assertIn("M", code)
        self.assertEqual(size, os.path.getsize(os.path.join(self.repo, "seed.txt")))
        self.assertGreater(mtime, 0)
        self.assertGreater(ctime, 0)

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
    """失敗/不完全は {} ではなく None (比較不能) を返す。"""

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


class TestFlaggedIndexEntries(GitScanTestCase):
    """commit レビューの P4': `git diff HEAD` が作業ツリーを見ないエントリを拾う。"""

    def test_normal_entry_is_not_flagged(self):
        self.assertEqual(gitscan.flagged_index_entries(self.repo, ["seed.txt"]), set())

    def test_assume_unchanged_and_skip_worktree_are_flagged(self):
        write(self.repo, "other.txt", "x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "another file")
        git(self.repo, "update-index", "--assume-unchanged", "seed.txt")
        git(self.repo, "update-index", "--skip-worktree", "other.txt")
        self.assertEqual(
            gitscan.flagged_index_entries(self.repo, ["seed.txt", "other.txt"]),
            {"seed.txt", "other.txt"},
        )

    def test_git_failure_returns_none(self):
        with mock.patch.object(gitscan, "_git", return_value=None):
            self.assertIsNone(gitscan.flagged_index_entries(self.repo, ["seed.txt"]))

    def test_empty_input(self):
        self.assertEqual(gitscan.flagged_index_entries(self.repo, []), set())


class TestBlobDigests(GitScanTestCase):
    """commit レビューの P6: commit された blob の生バイトの sha256。"""

    LARGE = 1 << 20

    def _sha(self, text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode()).hexdigest()

    def test_digest_matches_the_committed_bytes(self):
        write(self.repo, "with space.txt", "hello\nworld\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "blobs")
        result = gitscan.blob_digests(
            self.repo, "HEAD", ["with space.txt", "seed.txt"], self.LARGE, self.LARGE * 8
        )
        self.assertEqual(result["with space.txt"], self._sha("hello\nworld\n"))
        self.assertEqual(result["seed.txt"], self._sha("alpha\nbeta\ngamma\n"))

    def test_missing_path_is_omitted(self):
        result = gitscan.blob_digests(
            self.repo, "HEAD", ["seed.txt", "never.txt"], self.LARGE, self.LARGE * 8
        )
        self.assertIn("seed.txt", result)
        self.assertNotIn("never.txt", result)

    def test_symlink_and_directory_are_omitted(self):
        """blob 以外 (tree / symlink の中身以外) は返さない。

        symlink は blob ではあるが中身はリンク先の文字列で、`_file_fingerprint` が
        記録するのは通常ファイルの内容だけなので、どのみち一致しない。
        """
        os.makedirs(os.path.join(self.repo, "dir"))
        write(self.repo, "dir/inner.txt", "x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "a directory")
        result = gitscan.blob_digests(
            self.repo, "HEAD", ["dir"], self.LARGE, self.LARGE * 8
        )
        self.assertEqual(result, {})

    def test_size_cap_drops_the_path(self):
        write(self.repo, "big.txt", "x" * 100)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "big")
        self.assertEqual(
            gitscan.blob_digests(self.repo, "HEAD", ["big.txt"], 10, self.LARGE), {}
        )

    def test_total_cap_stops_after_the_budget(self):
        for i in range(3):
            write(self.repo, f"f{i}.txt", "x" * 40)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "three files")
        result = gitscan.blob_digests(
            self.repo, "HEAD", ["f0.txt", "f1.txt", "f2.txt"], 100, 90
        )
        self.assertEqual(list(result), ["f0.txt", "f1.txt"])

    def test_path_with_a_newline_is_skipped(self):
        """改行区切りの入力なので、パス名に改行があると 1:1 対応が崩れる。"""
        self.assertEqual(
            gitscan.blob_digests(self.repo, "HEAD", ["a\nb.txt"], self.LARGE, self.LARGE),
            {},
        )

    def test_git_failure_returns_empty(self):
        with mock.patch.object(gitscan, "_git", return_value=None):
            self.assertEqual(
                gitscan.blob_digests(
                    self.repo, "HEAD", ["seed.txt"], self.LARGE, self.LARGE
                ),
                {},
            )

    def test_misaligned_output_is_abandoned(self):
        """出力が入力とズレたら (oid が合わなければ) そこで読むのをやめる。"""
        wanted = [("a.txt", "0" * 40), ("b.txt", "1" * 40)]
        out = b"%s blob 2\nhi\n%s blob 2\nyo\n" % (b"0" * 40, b"9" * 40)
        self.assertEqual(
            list(gitscan._parse_batch_blobs(out, wanted)), ["a.txt"]
        )


class PlumbingTestCase(GitScanTestCase):
    """index を書かない git で差分を取る (0.18.0)。repo の形は次の 6 種を 1 つずつ持つ:

    - `stat.txt`: stat 情報だけが変わった (内容は commit と同じ)
    - `seed.txt`: 内容を変えた
    - `run.sh`: mode だけを変えた (+x)
    - `blob.bin`: バイナリの内容を変えた
    - `gone.txt`: 作業ツリーから消した
    - `fresh.txt`: untracked

    stat だけの変更は「index に記録した stat と違う」ことが要点なので、まず全ファイルの mtime を
    過去にして index に記録させ (index の書き込みはこの準備だけ)、そのあと `stat.txt` の mtime を
    未来に動かす。
    """

    def setUp(self) -> None:
        super().setUp()
        write(self.repo, "stat.txt", "same\n")
        write(self.repo, "run.sh", "echo hi\n")
        write(self.repo, "gone.txt", "bye\n")
        with open(os.path.join(self.repo, "blob.bin"), "wb") as f:
            f.write(b"\x00\x01binary\x00")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "fixture")
        past = time.time() - 60
        for rel in ("seed.txt", "stat.txt", "run.sh", "gone.txt", "blob.bin"):
            os.utime(os.path.join(self.repo, rel), (past, past))
        git(self.repo, "update-index", "-q", "--really-refresh")

        future = time.time() + 60
        os.utime(os.path.join(self.repo, "stat.txt"), (future, future))
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        os.chmod(os.path.join(self.repo, "run.sh"), 0o755)
        with open(os.path.join(self.repo, "blob.bin"), "wb") as f:
            f.write(b"\x00\x02binary\x00")
        os.remove(os.path.join(self.repo, "gone.txt"))
        write(self.repo, "fresh.txt", "brand new\n")
        self.tracked = ["stat.txt", "seed.txt", "run.sh", "blob.bin", "gone.txt"]

    def index_state(self) -> tuple[int, bytes]:
        path = os.path.join(self.repo, ".git", "index")
        with open(path, "rb") as f:
            return os.stat(path).st_mtime_ns, f.read()


class TestDiffDoesNotWriteTheIndex(PlumbingTestCase):
    """差分の取得で `.git/index` を書かない (背景のレビューが Claude の `git commit` と
    `index.lock` でぶつからない)。"""

    def test_every_scan_leaves_the_index_alone(self):
        before = self.index_state()
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        calls = {
            "path_diff tracked": lambda: [
                gitscan.path_diff(self.repo, rel, untracked=False, has_head=True)
                for rel in self.tracked
            ],
            "path_diff untracked": lambda: gitscan.path_diff(
                self.repo, "fresh.txt", untracked=True, has_head=True
            ),
            "changed_vs_head": lambda: gitscan.changed_vs_head(self.repo, self.tracked),
            "changed_vs_commit": lambda: gitscan.changed_vs_commit(self.repo, head, self.tracked),
            "range_paths": lambda: gitscan.range_paths(self.repo, f"{head}~1", head),
            "range_diff": lambda: gitscan.range_diff(self.repo, f"{head}~1", head, "run.sh"),
            "status_snapshot": lambda: gitscan.status_snapshot(self.repo),
            "untracked_among": lambda: gitscan.untracked_among(self.repo, ["fresh.txt"]),
            "flagged_index_entries": lambda: gitscan.flagged_index_entries(self.repo, self.tracked),
            "symlink_map": lambda: gitscan.symlink_map(self.repo),
        }
        for label, call in calls.items():
            with self.subTest(label):
                call()
                self.assertEqual(self.index_state(), before, f"{label} が index を書いた")

    def test_the_fixture_is_one_that_porcelain_would_rewrite(self):
        """陽性対照: この repo の形では porcelain の `git diff` が index を書く (前提が生きている)。"""
        before = self.index_state()
        git(self.repo, "diff", "--no-color", "HEAD", "--", "stat.txt")
        self.assertNotEqual(self.index_state(), before)


class TestPlumbingOutputMatchesPorcelain(PlumbingTestCase):
    """plumbing に替えても、既定の設定では差分の本文と集合が porcelain と同じ。"""

    def porcelain(self, *args: str) -> str:
        # 比較のためだけに porcelain を流す (index を書くので、比べる側より後に呼ぶ)
        return git(self.repo, "diff", "--no-color", *args).stdout

    def test_path_diff_is_byte_identical(self):
        mine = {rel: gitscan.path_diff(self.repo, rel, untracked=False, has_head=True)
                for rel in self.tracked}
        for rel in self.tracked:
            with self.subTest(rel):
                self.assertEqual(mine[rel], self.porcelain("HEAD", "--", rel))
        self.assertEqual(mine["stat.txt"], "", "stat だけの変更は空")
        self.assertIn("new mode 100755", mine["run.sh"])
        self.assertIn("Binary files", mine["blob.bin"])
        self.assertIn("deleted file mode", mine["gone.txt"])

    def test_changed_sets_drop_stat_only_changes(self):
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        expected = {"seed.txt", "run.sh", "blob.bin", "gone.txt"}
        self.assertEqual(gitscan.changed_vs_head(self.repo, self.tracked), expected)
        self.assertEqual(gitscan.changed_vs_commit(self.repo, head, self.tracked), expected)
        porcelain = set(self.porcelain("--name-only", "-z", "HEAD", "--", *self.tracked)
                        .split("\0")) - {""}
        self.assertEqual(porcelain, expected)

    def test_changed_sets_keep_names_with_tabs_and_spaces(self):
        odd = ["tab\tname.txt", "space name.txt"]
        for rel in odd:
            write(self.repo, rel, "v1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "odd")
        for rel in odd:
            write(self.repo, rel, "v2\n")
        self.assertEqual(gitscan.changed_vs_head(self.repo, odd), set(odd))

    def test_changed_sets_report_failure_as_none(self):
        self.assertIsNone(gitscan.changed_vs_commit(self.repo, "0" * 40, self.tracked))
        with mock.patch.object(gitscan, "_git", return_value=None):
            self.assertIsNone(gitscan.changed_vs_head(self.repo, self.tracked))

    def test_range_paths_and_range_diff_match_porcelain(self):
        git(self.repo, "add", "-A")
        git(self.repo, "mv", "seed.txt", "moved.txt")
        os.makedirs(os.path.join(self.repo, "sub"))
        write(self.repo, "sub/deep.txt", "deep\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "range")
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        base = f"{head}~1"
        names = gitscan.range_paths(self.repo, base, head)
        expected = sorted(set(self.porcelain("--name-only", "-z", base, head).split("\0")) - {""})
        self.assertEqual(sorted(names), expected)
        self.assertIn("moved.txt", names)
        self.assertNotIn("seed.txt", names, "rename は新しいパスだけ (porcelain の既定と同じ)")
        self.assertIn("sub/deep.txt", names, "subtree の中まで降りる")
        for rel in expected:
            with self.subTest(rel):
                self.assertEqual(gitscan.range_diff(self.repo, base, head, rel),
                                 self.porcelain(base, head, "--", rel))

    def test_repo_without_head_matches_porcelain_cached(self):
        fresh = os.path.join(self._tmp.name, "fresh-repo")
        os.makedirs(fresh)
        git(fresh, "init", "-q")
        write(fresh, "first.txt", "hello\n")
        git(fresh, "add", "-A")
        mine = gitscan.path_diff(fresh, "first.txt", untracked=False, has_head=False)
        self.assertEqual(mine, git(fresh, "diff", "--no-color", "--cached", "--", "first.txt").stdout)
        with mock.patch.object(gitscan, "empty_tree", return_value=None):
            try:
                text = gitscan.path_diff(fresh, "first.txt", untracked=False, has_head=False)
            except Exception as e:  # noqa: BLE001 - 例外の漏れは検出として数える
                self.fail(f"空ツリーを引けないときに例外が漏れた: {e!r}")
            self.assertEqual(text, "")

    def test_range_paths_ignores_the_renames_setting(self):
        """`diff.renames=false` でも rename は新しいパスだけ (plumbing は利用者の設定を読まない)。"""
        git(self.repo, "add", "-A")
        git(self.repo, "mv", "seed.txt", "moved.txt")
        git(self.repo, "commit", "-qm", "mv")
        git(self.repo, "config", "diff.renames", "false")
        names = gitscan.range_paths(self.repo, "HEAD~1", "HEAD")
        self.assertIn("moved.txt", names)
        self.assertNotIn("seed.txt", names)


class TestFirstCommitSharesTheFormat(GitScanTestCase):
    """HEAD の無い repo の staged 差分も、最初の commit の `range_diff` と同じ本文
    (porcelain の接頭辞の設定があっても)。"""

    def test_staged_text_equals_the_first_commit_text(self):
        fresh = os.path.join(self._tmp.name, "fresh-repo")
        os.makedirs(fresh)
        git(fresh, "init", "-q")
        git(fresh, "config", "user.email", "t@example.com")
        git(fresh, "config", "user.name", "t")
        git(fresh, "config", "diff.noprefix", "true")
        write(fresh, "first.txt", "hello\n")
        git(fresh, "add", "-A")
        staged = gitscan.path_diff(fresh, "first.txt", untracked=False, has_head=False)
        git(fresh, "commit", "-qm", "first")
        empty = gitscan.empty_tree(fresh)
        self.assertIn("+hello", staged)
        self.assertEqual(staged, gitscan.range_diff(fresh, empty, "HEAD", "first.txt"))


class TestStopAndCommitReviewShareOneFormat(GitScanTestCase):
    """Stop の `path_diff` と commit レビューの `range_diff` は、porcelain の設定があっても同じ本文。

    commit レビューは本文の hash で「Stop がレビュー済み」を判定する。書式がずれると同じ内容を
    2 回送る。
    """

    CONFIGS = [
        (),
        (("diff.noprefix", "true"),),
        (("diff.mnemonicPrefix", "true"),),
        (("diff.srcPrefix", "x/"), ("diff.dstPrefix", "y/")),
        (("diff.context", "1"),),
        (("diff.interHunkContext", "30"),),
        (("diff.up.textconv", "tr a-z A-Z <"),),
        (("diff.external", "echo EXTERNAL"),),
    ]

    def test_stop_text_equals_commit_review_text(self):
        body = "".join(f"line {i}\n" for i in range(40))
        for cfg in self.CONFIGS:
            label = ",".join(f"{k}={v}" for k, v in cfg) or "(none)"
            with self.subTest(label):
                repo = init_repo(os.path.join(self._tmp.name, f"repo-{self.CONFIGS.index(cfg)}"))
                write(repo, ".gitattributes", "*.txt diff=up\n")
                write(repo, "mod.txt", body)
                git(repo, "add", "-A")
                git(repo, "commit", "-qm", "base")
                for key, value in cfg:
                    git(repo, "config", key, value)
                lines = body.splitlines(keepends=True)
                lines[3], lines[30] = "changed 3\n", "changed 30\n"
                write(repo, "mod.txt", "".join(lines))
                write(repo, "new.txt", "hello\n\nworld\n")
                stop_mod = gitscan.path_diff(repo, "mod.txt", untracked=False, has_head=True)
                stop_new = gitscan.path_diff(repo, "new.txt", untracked=True, has_head=True)
                git(repo, "add", "-A")
                git(repo, "commit", "-qm", "work")
                self.assertIn("+changed 3", stop_mod)
                self.assertIn("+hello", stop_new)
                self.assertEqual(stop_mod, gitscan.range_diff(repo, "HEAD~1", "HEAD", "mod.txt"))
                self.assertEqual(stop_new, gitscan.range_diff(repo, "HEAD~1", "HEAD", "new.txt"))


if __name__ == "__main__":
    unittest.main()
