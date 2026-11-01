"""パスのディスク上の綴り (`_common.physpath`)。

大文字小文字を区別しない FS (macOS の既定) でだけ意味を持つ形は skip 付き。配置は
symlink (対象側・親ディレクトリ) / hard link / 大文字小文字だけ違う名前 / 存在しないパスを回す。
呼び出しの例外は assertion の失敗に変える (取り違えた変異を ERROR ではなく FAIL で数えるため)。
"""
import os
import tempfile
import unicodedata
import unittest
from unittest import mock

import _testutil  # noqa: F401  (sys.path 設定)

from _common import physpath


def _case_insensitive(directory: str) -> bool:
    probe = os.path.join(directory, "caseProbe")
    with open(probe, "w"):
        pass
    try:
        return os.path.exists(os.path.join(directory, "CASEPROBE"))
    finally:
        os.unlink(probe)


class PhyspathTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        os.mkdir(os.path.join(self.root, "sub"))
        self.b = os.path.join(self.root, "sub", "b.py")
        with open(self.b, "w") as f:
            f.write("x\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, *parts: str) -> str:
        return self._call(physpath.physical_path, os.path.join(self.root, *parts))

    def dir(self, *parts: str) -> str:
        return self._call(physpath.physical_dir, os.path.join(self.root, *parts))

    def _call(self, fn, arg):
        try:
            return fn(arg)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"{fn.__name__}({arg!r}) が例外を漏らした: {exc!r}")

    def needs_case_insensitive(self) -> None:
        if physpath._GETPATH is None:
            self.skipTest("F_GETPATH の無い OS では綴りを揃えない")
        if not _case_insensitive(self.root):
            self.skipTest("大文字小文字を区別する FS では同じ実体を指す別名にならない")


class TestUnchangedForms(PhyspathTestCase):
    """綴りが既にディスク上と同じ形は realpath と同じ結果。"""

    def test_exact_name(self):
        self.assertEqual(self.path("sub", "b.py"), self.b)

    def test_symlinked_file_resolves_to_target(self):
        os.symlink(self.b, os.path.join(self.root, "link.py"))
        self.assertEqual(self.path("link.py"), self.b)

    def test_symlinked_parent_resolves_to_target(self):
        os.symlink("sub", os.path.join(self.root, "alias"))
        self.assertEqual(self.path("alias", "b.py"), self.b)

    def test_hard_link_keeps_its_own_name(self):
        h = os.path.join(self.root, "sub", "h.py")
        os.link(self.b, h)
        self.assertEqual(self.path("sub", "h.py"), h)
        self.assertEqual(self.path("sub", "b.py"), self.b)

    def test_missing_path_is_realpath(self):
        missing = os.path.join(self.root, "sub", "Nope.py")
        self.assertEqual(self.path("sub", "Nope.py"), os.path.realpath(missing))

    def test_missing_parent_is_realpath(self):
        missing = os.path.join(self.root, "Nodir", "X.py")
        self.assertEqual(self.path("Nodir", "X.py"), os.path.realpath(missing))
        self.assertEqual(self.dir("Nodir"), os.path.join(self.root, "Nodir"))

    def test_physical_dir_of_exact_dir(self):
        self.assertEqual(self.dir("sub"), os.path.join(self.root, "sub"))

    def test_without_getpath_it_is_realpath(self):
        with mock.patch.object(physpath, "_GETPATH", None):
            self.assertEqual(self.path("SUB", "B.py"), os.path.join(self.root, "SUB", "B.py"))
            self.assertEqual(self.dir("SUB"), os.path.join(self.root, "SUB"))


class TestGetpathFailures(PhyspathTestCase):
    """`F_GETPATH` が使えない・おかしな値を返すときは realpath に倒す。"""

    def setUp(self) -> None:
        super().setUp()
        if physpath._GETPATH is None:
            self.skipTest("F_GETPATH の無い OS")

    def test_fcntl_error_falls_back(self):
        with mock.patch.object(physpath.fcntl, "fcntl", side_effect=OSError("boom")):
            self.assertEqual(self.dir("SUB"), os.path.join(self.root, "SUB"))

    def test_relative_answer_falls_back(self):
        with mock.patch.object(physpath.fcntl, "fcntl", return_value=b"sub\0junk"):
            self.assertEqual(self.dir("SUB"), os.path.join(self.root, "SUB"))


class TestCaseOnlyAlias(PhyspathTestCase):
    def test_file_name_is_spelled_as_on_disk(self):
        self.needs_case_insensitive()
        self.assertEqual(self.path("sub", "B.py"), self.b)

    def test_directory_part_is_spelled_as_on_disk(self):
        self.needs_case_insensitive()
        self.assertEqual(self.path("SUB", "b.py"), self.b)
        self.assertEqual(self.dir("Sub"), os.path.join(self.root, "sub"))

    def test_both_parts_and_a_symlinked_parent(self):
        self.needs_case_insensitive()
        os.symlink("sub", os.path.join(self.root, "alias"))
        self.assertEqual(self.path("ALIAS", "B.PY"), self.b)

    def test_case_alias_of_a_hard_link_keeps_the_link_name(self):
        """`H.py` (実体は hard link の `h.py`) は `h.py` に揃え、同じ inode の `b.py` にしない。"""
        self.needs_case_insensitive()
        h = os.path.join(self.root, "sub", "h.py")
        os.link(self.b, h)
        self.assertEqual(self.path("sub", "H.py"), h)
        self.assertEqual(self.path("sub", "B.py"), self.b)

    def test_unicode_normalization_alias(self):
        self.needs_case_insensitive()
        nfd = unicodedata.normalize("NFD", "café.py")
        nfc = unicodedata.normalize("NFC", nfd)
        on_disk = os.path.join(self.root, "sub", nfd)
        with open(on_disk, "w"):
            pass
        if not os.path.exists(os.path.join(self.root, "sub", nfc)):
            self.skipTest("正規化の違いを区別する FS")
        if nfd not in os.listdir(os.path.join(self.root, "sub")):
            self.skipTest("FS が名前を作成時の形で保存しない")
        self.assertEqual(self.path("sub", nfc), on_disk)

    def test_unreadable_directory_keeps_the_written_name(self):
        self.needs_case_insensitive()
        if os.geteuid() == 0:
            self.skipTest("root は読み取り権限を無視する")
        sub = os.path.join(self.root, "sub")
        os.chmod(sub, 0o311)  # 一覧は読めない、辿れる
        try:
            got = self.path("sub", "B.py")
        finally:
            os.chmod(sub, 0o755)
        self.assertEqual(os.path.basename(got), "B.py")

    def test_different_file_with_same_folded_name_is_not_chosen(self):
        """綴りが畳むと同じでも別の実体なら選ばない (一覧の候補は samestat で絞る)。"""
        self.needs_case_insensitive()
        with mock.patch.object(physpath.os, "listdir", return_value=["B.PY", "other.py"]), \
                mock.patch.object(physpath.os.path, "samestat", return_value=False):
            got = self.path("sub", "B.py")
        self.assertEqual(os.path.basename(got), "B.py")

    def test_entry_that_vanishes_while_listing_is_skipped(self):
        """一覧の項目が lstat の前に消えていても例外にせず、書かれた名前のまま返す。"""
        self.needs_case_insensitive()
        real_lstat = os.lstat
        target = os.path.join(self.root, "sub", "B.py")

        def lstat(p, *a, **k):
            if p == target:
                return real_lstat(p, *a, **k)
            raise FileNotFoundError(p)

        with mock.patch.object(physpath.os, "lstat", side_effect=lstat):
            got = self.path("sub", "B.py")
        self.assertEqual(os.path.basename(got), "B.py")


if __name__ == "__main__":
    unittest.main()
