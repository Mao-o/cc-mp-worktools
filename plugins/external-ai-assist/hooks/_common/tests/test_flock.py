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


if __name__ == "__main__":
    unittest.main()
