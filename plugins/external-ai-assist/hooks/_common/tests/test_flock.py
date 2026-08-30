"""flock 付き read-modify-write。"""
import fcntl
import os
import tempfile
import threading
import unittest
from unittest import mock

import _testutil  # noqa: F401  (sys.path 設定)

from _common import flock


class TestLockedFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "nested", "dir", "marker")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_parent_and_round_trips(self):
        with flock.locked_file(self.path) as f:
            self.assertEqual(flock.read_all(f), "")
            flock.rewrite(f, "hash\n1")
        with flock.locked_file(self.path) as f:
            self.assertEqual(flock.read_all(f), "hash\n1")

    def test_rewrite_truncates_longer_previous_content(self):
        with flock.locked_file(self.path) as f:
            flock.rewrite(f, "a much longer content")
        with flock.locked_file(self.path) as f:
            flock.rewrite(f, "x")
        with open(self.path) as f:
            self.assertEqual(f.read(), "x")

    def test_lock_is_released_after_exception(self):
        with self.assertRaises(RuntimeError):
            with flock.locked_file(self.path):
                raise RuntimeError("boom")
        with open(self.path, "a+") as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # 取れなければ OSError
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)

    def test_concurrent_increments_are_serialized(self):
        threads_n, per_thread = 8, 25

        def worker():
            for _ in range(per_thread):
                with flock.locked_file(self.path) as f:
                    current = int(flock.read_all(f).strip() or 0)
                    flock.rewrite(f, str(current + 1))

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(self.path) as f:
            self.assertEqual(int(f.read()), threads_n * per_thread)


class TestPrivatePermissions(unittest.TestCase):
    """内部バックログ: 共有 $TMPDIR で他ユーザーから state/レビュー結果が読めない
    よう、新規作成したファイルは 0o600、ディレクトリは 0o700 で作ること。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mode(self, path: str) -> int:
        return os.stat(path).st_mode & 0o777

    def test_locked_file_creates_new_file_with_0600(self):
        path = os.path.join(self._tmp.name, "a", "b", "marker")
        with flock.locked_file(path):
            pass
        self.assertEqual(self._mode(path), 0o600)

    def test_locked_file_secures_every_intermediate_directory(self):
        """`os.makedirs(mode=...)` は最後の階層にしか mode を適用しない既知の仕様の
        回避を確認する。多階層 (a/b) の両方が 0o700 になっていること。"""
        path = os.path.join(self._tmp.name, "a", "b", "marker")
        with flock.locked_file(path):
            pass
        self.assertEqual(self._mode(os.path.join(self._tmp.name, "a")), 0o700)
        self.assertEqual(self._mode(os.path.join(self._tmp.name, "a", "b")), 0o700)

    def test_locked_file_does_not_touch_existing_file_mode(self):
        """既存ファイルの権限は変更しない (retrofit は harden_dir 側の役割)。"""
        path = os.path.join(self._tmp.name, "marker")
        with open(path, "w") as f:
            f.write("x")
        os.chmod(path, 0o644)
        with flock.locked_file(path) as f:
            flock.rewrite(f, "y")
        self.assertEqual(self._mode(path), 0o644)

    def test_write_private_creates_new_file_with_0600(self):
        path = os.path.join(self._tmp.name, "sub", "review.txt")
        flock.write_private(path, "hello")
        self.assertEqual(self._mode(path), 0o600)
        with open(path) as f:
            self.assertEqual(f.read(), "hello")
        self.assertEqual(self._mode(os.path.join(self._tmp.name, "sub")), 0o700)

    def test_write_private_truncates_existing_file(self):
        path = os.path.join(self._tmp.name, "review.txt")
        flock.write_private(path, "a much longer first write")
        flock.write_private(path, "short")
        with open(path) as f:
            self.assertEqual(f.read(), "short")

    def test_harden_dir_tightens_loose_existing_directory(self):
        path = os.path.join(self._tmp.name, "legacy")
        os.mkdir(path, 0o755)
        flock.harden_dir(path)
        self.assertEqual(self._mode(path), 0o700)

    def test_harden_dir_is_noop_when_already_private(self):
        path = os.path.join(self._tmp.name, "already")
        os.mkdir(path, 0o700)
        flock.harden_dir(path)
        self.assertEqual(self._mode(path), 0o700)

    def test_harden_dir_ignores_missing_path(self):
        flock.harden_dir(os.path.join(self._tmp.name, "does-not-exist"))  # 例外を出さない

    def test_harden_dir_swallows_chmod_failure(self):
        """共有 /tmp で他ユーザー所有のディレクトリを掴んだ場合、chmod 失敗は無視する
        (fail-open)。"""
        path = os.path.join(self._tmp.name, "owned-by-someone-else")
        os.mkdir(path, 0o755)
        with mock.patch("os.chmod", side_effect=PermissionError("nope")):
            flock.harden_dir(path)  # 例外を出さない
        self.assertEqual(self._mode(path), 0o755, "chmod できなければ元のモードのまま")


class TestEnsurePrivateRoot(unittest.TestCase):
    """事前に作られた状態ディレクトリを無条件に信用しない。

    `ensure_private_root` は hook が所有する最上位ディレクトリ (`state_root()` /
    exitplan-review の `marker_dir` 相当) 専用の検査で、`$TMPDIR` 自体のような
    環境が用意した祖先ディレクトリは対象にしない (モジュール docstring 参照)。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mode(self, path: str) -> int:
        return os.stat(path).st_mode & 0o777

    def test_creates_fresh_dir_with_0700(self):
        path = os.path.join(self._tmp.name, "post-implementation-review")
        flock.ensure_private_root(path)
        self.assertEqual(self._mode(path), 0o700)

    def test_accepts_existing_safe_dir_without_raising(self):
        path = os.path.join(self._tmp.name, "post-implementation-review")
        os.mkdir(path, 0o700)
        flock.ensure_private_root(path)  # 例外を出さない
        self.assertEqual(self._mode(path), 0o700)

    def test_accepts_self_owned_dir_with_read_only_group_other_bits(self):
        """所有者は自分で、group/other に読み取り/実行はあっても**書込は無い**
        (旧版が既定 umask で作った 0o755 等) 場合は、`ensure_private_root` の
        判定基準 (マージ前レビューの指摘の 2 条件: 所有者相違 / group・other 書込可) に
        当てはまらないため受け入れる。書込ビットが無ければ他ユーザーは
        ディレクトリ配下のファイルを列挙・削除・差し替えできない
        (この関数が防ぎたい脅威そのものが成立しない) ため、0o700 への締め直しは
        必須ではない — 読み取り専用の露出は `harden_dir` の periodic GC retrofit
        (別の関心事: 内容の秘匿性) に委ねる。"""
        path = os.path.join(self._tmp.name, "post-implementation-review")
        os.mkdir(path, 0o755)
        flock.ensure_private_root(path)  # 例外を出さない
        self.assertEqual(self._mode(path), 0o755, "書込ビットが無ければ変更しない")

    def test_rejects_self_owned_dir_with_world_write_bit_but_self_heals_next_call(self):
        """regression (マージ前レビューの指摘): 自分が所有者でも、group/other に書込権が
        ある状態で**見つかった**なら今回は使わない — 緩かった間に他ユーザーが
        中身を書き換えられた可能性を、その場で締め直せても否定できないため。
        (`os.mkdir(mode=...)` は umask でマスクされうるため、ここでは `os.chmod`
        で明示的に 0o777 にしてから検査する。)

        ただし締め直し自体は次回のために試みるので、2 回目の呼び出しでは
        (既に 0o700 になっているため) 通常どおり使える (自己修復)。
        """
        path = os.path.join(self._tmp.name, "post-implementation-review")
        os.mkdir(path)
        os.chmod(path, 0o777)
        self.assertEqual(self._mode(path), 0o777, "umask でマスクされていないこと")

        with self.assertRaises(flock.UnsafeStateDirError):
            flock.ensure_private_root(path)
        self.assertEqual(self._mode(path), 0o700, "今回は拒否しつつ次回のために締め直すこと")

        flock.ensure_private_root(path)  # 2 回目: 既に安全なので例外を出さない

    def test_rejects_dir_owned_by_other_user(self):
        """所有者が自分でなければ、chmod がどう振る舞おうと (試みても失敗するので
        実質無意味) 使用を拒否する。root 権限なしで実際に別所有者のディレクトリを
        作ることはできないため、`os.geteuid` を偽装して「自分の物ではない」経路を
        再現する。"""
        path = os.path.join(self._tmp.name, "post-implementation-review")
        os.mkdir(path, 0o700)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 12345):
            with self.assertRaises(flock.UnsafeStateDirError):
                flock.ensure_private_root(path)

    def test_chmod_failure_is_not_ignored(self):
        """締め直し (`os.chmod`) 自体が失敗する場合 (典型的には所有者違いで
        `PermissionError`) も、`harden_dir` と違って無視せず使用を拒否する。"""
        path = os.path.join(self._tmp.name, "post-implementation-review")
        os.mkdir(path, 0o777)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 12345):
            with mock.patch("os.chmod", side_effect=PermissionError("nope")):
                with self.assertRaises(flock.UnsafeStateDirError):
                    flock.ensure_private_root(path)

    def test_rejects_symlink_even_if_target_is_safe(self):
        """symlink を状態ディレクトリとして掴まされる経路を弾く。target 自身は
        自分所有・0o700 の安全なディレクトリでも、symlink 越しの使用は拒否する。"""
        target = os.path.join(self._tmp.name, "real-safe-dir")
        os.mkdir(target, 0o700)
        link = os.path.join(self._tmp.name, "post-implementation-review")
        os.symlink(target, link)
        with self.assertRaises(flock.UnsafeStateDirError):
            flock.ensure_private_root(link)

    def test_does_not_chmod_through_symlink(self):
        """symlink を検出したら `os.chmod` を一切呼ばない。

        `os.chmod` は既定で symlink を辿るため、無条件に呼ぶと symlink 先
        (攻撃者が選んだ任意のパスでありうる) の権限を変えてしまう。
        """
        target = os.path.join(self._tmp.name, "attacker-chosen-target")
        os.mkdir(target, 0o755)  # 緩いままなら chmod が誘発されうる状態にしておく
        link = os.path.join(self._tmp.name, "post-implementation-review")
        os.symlink(target, link)
        with mock.patch("os.chmod") as chmod:
            with self.assertRaises(flock.UnsafeStateDirError):
                flock.ensure_private_root(link)
            chmod.assert_not_called()
        self.assertEqual(self._mode(target), 0o755, "symlink 先の権限を変えていないこと")

    def test_rejects_regular_file_blocking_the_path(self):
        """ディレクトリではなく通常ファイルが同名で存在する場合も拒否する。"""
        path = os.path.join(self._tmp.name, "post-implementation-review")
        with open(path, "w") as f:
            f.write("not a directory")
        with self.assertRaises(flock.UnsafeStateDirError):
            flock.ensure_private_root(path)


if __name__ == "__main__":
    unittest.main()
